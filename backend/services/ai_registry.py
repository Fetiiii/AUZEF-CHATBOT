"""Managed LLM model registry, capability assignments, versions and audit.

Three separate concepts (Phase 6):

- **Model definition** (``ai_model_registry``): an allowlisted provider +
  model identifier with its capability/structured-output/reasoning support and
  qualification state. Soft-disabled, never hard-deleted. No secrets.
- **Capability assignment** (``ai_capability_config``): which registry model
  and generation parameters each capability (intent_analyzer, selector) uses.
- **Config version** (``ai_config_version``): an immutable, secret-free
  snapshot of both assignments. The highest id is active; rollback creates a
  new version from an old snapshot, history is never rewritten.

Every write runs in one transaction under a Postgres advisory lock, checks the
caller's ``expected_version`` (optimistic concurrency) and appends to
``ai_config_audit``. Validation here is authoritative; the admin UI only
mirrors it.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Mapping, Optional

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.database import (
    AICapabilityConfig,
    AIConfigAudit,
    AIConfigVersion,
    AIModelRegistry,
    admin_engine,
    utcnow,
)
from services.llm_config import (
    _DEFAULT_MODELS,
    EffectiveLLMConfig,
    EffectiveLLMConfigSet,
    LLMCapability,
    ReasoningEffort,
    reasoning_transport_supported,
    resolve_llm_config_set,
)

logger = logging.getLogger("auzef")

SUPPORTED_PROVIDERS = tuple(sorted(_DEFAULT_MODELS))
CAPABILITIES = (LLMCapability.INTENT_ANALYZER, LLMCapability.SELECTOR)
SNAPSHOT_SCHEMA = 1
BOOTSTRAP_ACTOR = "system:bootstrap"
_ADVISORY_LOCK_KEY = 710_6060  # serializes AI config writers across nodes

# Conservative, backend-enforced bounds. Both capabilities parse strict JSON,
# so tiny token budgets (e.g. selector max_tokens=1) are rejected.
TEMPERATURE_RANGE = (0.0, 1.0)
MAX_TOKENS_RANGE = {
    LLMCapability.INTENT_ANALYZER: (200, 8192),
    LLMCapability.SELECTOR: (24, 4096),
}
TIMEOUT_RANGE = (1.0, 120.0)
RETRY_RANGE = (0, 5)
DISPLAY_NAME_MAX = 120
MODEL_IDENTIFIER_MAX = 200


class QualificationStatus(str, Enum):
    UNTESTED = "UNTESTED"
    QUALIFIED = "QUALIFIED"
    # Only the bootstrap may assign this: models already used in production
    # (Phase 0 defaults / env-configured) before the registry existed.
    LEGACY_APPROVED = "LEGACY_APPROVED"
    BLOCKED = "BLOCKED"


ASSIGNABLE_QUALIFICATIONS = frozenset({
    QualificationStatus.QUALIFIED, QualificationStatus.LEGACY_APPROVED,
})


class AIConfigError(Exception):
    """Typed, user-safe validation/conflict error (``status`` → HTTP code)."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


# ── value objects ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ModelDefinition:
    id: Optional[int]
    display_name: str
    provider: str
    model_identifier: str
    enabled: bool
    allowed_capabilities: tuple
    supports_structured_output: bool
    supports_reasoning_effort: bool
    allowed_reasoning_efforts: tuple
    qualification_status: QualificationStatus
    qualified_at: Optional[str] = None
    qualified_by: Optional[str] = None
    qualification_reference: Optional[str] = None

    @classmethod
    def from_row(cls, row: AIModelRegistry) -> "ModelDefinition":
        return cls(
            id=int(row.id) if row.id is not None else None,
            display_name=row.display_name,
            provider=row.provider,
            model_identifier=row.model_identifier,
            enabled=bool(row.enabled),
            allowed_capabilities=tuple(_json_list(row.allowed_capabilities)),
            supports_structured_output=bool(row.supports_structured_output),
            supports_reasoning_effort=bool(row.supports_reasoning_effort),
            allowed_reasoning_efforts=tuple(_json_list(row.allowed_reasoning_efforts)),
            qualification_status=QualificationStatus(row.qualification_status),
            qualified_at=row.qualified_at.isoformat() if row.qualified_at else None,
            qualified_by=row.qualified_by,
            qualification_reference=row.qualification_reference,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "provider": self.provider,
            "model_identifier": self.model_identifier,
            "enabled": self.enabled,
            "allowed_capabilities": list(self.allowed_capabilities),
            "supports_structured_output": self.supports_structured_output,
            "supports_reasoning_effort": self.supports_reasoning_effort,
            "allowed_reasoning_efforts": list(self.allowed_reasoning_efforts),
            "qualification_status": self.qualification_status.value,
            "qualified_at": self.qualified_at,
            "qualified_by": self.qualified_by,
            "qualification_reference": self.qualification_reference,
        }


@dataclass(frozen=True)
class CapabilityParams:
    temperature: float = 0.0
    max_tokens: int = 0
    reasoning_effort: Optional[str] = None
    timeout_seconds: Optional[float] = None
    max_retries: Optional[int] = None
    structured_output_enabled: bool = False

    @classmethod
    def from_row(cls, row: AICapabilityConfig) -> "CapabilityParams":
        return cls(
            temperature=float(row.temperature),
            max_tokens=int(row.max_tokens),
            reasoning_effort=row.reasoning_effort,
            timeout_seconds=float(row.timeout_seconds) if row.timeout_seconds is not None else None,
            max_retries=int(row.max_retries) if row.max_retries is not None else None,
            structured_output_enabled=bool(row.structured_output_enabled),
        )

    def to_dict(self) -> dict:
        return {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "structured_output_enabled": self.structured_output_enabled,
        }


@dataclass(frozen=True)
class CapabilityAssignment:
    capability: LLMCapability
    model: ModelDefinition
    params: CapabilityParams

    def effective_config(self) -> EffectiveLLMConfig:
        return EffectiveLLMConfig(
            capability=self.capability,
            provider=self.model.provider,
            model=self.model.model_identifier,
            reasoning_effort=(
                ReasoningEffort(self.params.reasoning_effort)
                if self.params.reasoning_effort else None
            ),
            temperature=self.params.temperature,
            max_tokens=self.params.max_tokens,
            timeout_seconds=self.params.timeout_seconds,
            max_retries=self.params.max_retries,
            structured_output_enabled=self.params.structured_output_enabled,
        )

    def snapshot(self) -> dict:
        config = self.effective_config()
        return {
            "model_registry_id": self.model.id,
            "provider": self.model.provider,
            "model": self.model.model_identifier,
            **self.params.to_dict(),
            "config_fingerprint": config.fingerprint,
        }


@dataclass(frozen=True)
class ActiveConfig:
    version_id: int
    assignments: Mapping[LLMCapability, CapabilityAssignment] = field(default_factory=dict)

    def config_set(self) -> EffectiveLLMConfigSet:
        return EffectiveLLMConfigSet(
            intent_analyzer=self.assignments[LLMCapability.INTENT_ANALYZER].effective_config(),
            selector=self.assignments[LLMCapability.SELECTOR].effective_config(),
        )

    def snapshot(self) -> dict:
        return {
            "schema": SNAPSHOT_SCHEMA,
            "capabilities": {
                cap.value: self.assignments[cap].snapshot() for cap in CAPABILITIES
            },
        }


# ── helpers ──────────────────────────────────────────────────────────────────

def _json_list(value) -> list:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _capability(value) -> LLMCapability:
    try:
        return LLMCapability(value)
    except ValueError:
        raise AIConfigError("invalid_capability", f"Bilinmeyen capability: {value!r}") from None


def _lock(db: Session) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _ADVISORY_LOCK_KEY},
        bind_arguments={"bind": admin_engine},
    )


def _audit(db: Session, event_type: str, *, actor, capability=None, model_id=None,
           old=None, new=None, old_version=None, new_version=None) -> None:
    db.add(AIConfigAudit(
        event_type=event_type,
        actor=actor,
        capability=capability,
        model_registry_id=model_id,
        old_value=_dump(old) if old is not None else None,
        new_value=_dump(new) if new is not None else None,
        old_version_id=old_version,
        new_version_id=new_version,
    ))
    logger.info("ai_config_event=%s", _dump({
        "event": event_type, "capability": capability, "model_registry_id": model_id,
        "old_version": old_version, "new_version": new_version,
    }))


# ── pure validation ──────────────────────────────────────────────────────────

def validate_model_fields(
    *,
    display_name: str,
    provider: str,
    model_identifier: str,
    allowed_capabilities,
    supports_structured_output: bool,
    supports_reasoning_effort: bool,
    allowed_reasoning_efforts,
) -> tuple:
    if provider not in SUPPORTED_PROVIDERS:
        raise AIConfigError(
            "unsupported_provider",
            f"Desteklenmeyen provider: {provider!r}. İzinli: {', '.join(SUPPORTED_PROVIDERS)}",
        )
    name = (display_name or "").strip()
    if not name or len(name) > DISPLAY_NAME_MAX:
        raise AIConfigError("invalid_display_name", "Görünen ad 1-120 karakter olmalı.")
    identifier = (model_identifier or "").strip()
    if (
        not identifier or len(identifier) > MODEL_IDENTIFIER_MAX
        or any(ch.isspace() for ch in identifier)
    ):
        raise AIConfigError("invalid_model_identifier", "Model ID boşluksuz 1-200 karakter olmalı.")
    caps = []
    for value in allowed_capabilities or ():
        cap = _capability(value).value
        if cap not in caps:
            caps.append(cap)
    if not caps:
        raise AIConfigError("invalid_capability", "En az bir izinli capability gerekli.")
    efforts = []
    for value in allowed_reasoning_efforts or ():
        try:
            effort = ReasoningEffort(value).value
        except ValueError:
            raise AIConfigError("invalid_reasoning_effort", f"Geçersiz reasoning değeri: {value!r}") from None
        if effort not in efforts:
            efforts.append(effort)
    if efforts and not supports_reasoning_effort:
        raise AIConfigError(
            "invalid_reasoning_effort",
            "Reasoning desteklemeyen model için reasoning seviyesi tanımlanamaz.",
        )
    if supports_reasoning_effort and not efforts:
        raise AIConfigError(
            "invalid_reasoning_effort", "Reasoning destekleyen model en az bir seviye listelemeli."
        )
    return name, identifier, tuple(caps), tuple(efforts)


def validate_assignment(capability: LLMCapability, model: ModelDefinition,
                        params: CapabilityParams) -> None:
    """Authoritative capability-specific validation (UI only mirrors this)."""
    if not model.enabled:
        raise AIConfigError("model_disabled", f"Model devre dışı: {model.display_name}")
    if model.qualification_status not in ASSIGNABLE_QUALIFICATIONS:
        raise AIConfigError(
            "model_not_qualified",
            f"Model production için qualify edilmemiş ({model.qualification_status.value}).",
        )
    if capability.value not in model.allowed_capabilities:
        raise AIConfigError(
            "capability_not_allowed",
            f"Model {capability.value} capability'si için izinli değil.",
        )
    if not model.supports_structured_output:
        # Both capabilities rely on the strict JSON + Pydantic contract.
        raise AIConfigError(
            "structured_output_unsupported",
            f"{capability.value} strict JSON çıktı sözleşmesi gerektirir; model bunu desteklemiyor.",
        )
    if params.structured_output_enabled:
        raise AIConfigError(
            "native_structured_output_unavailable",
            "Provider-native structured output adapter'larda uygulanmadı; strict JSON doğrulaması kullanılır.",
        )
    temperature = params.temperature
    if (
        not isinstance(temperature, (int, float)) or isinstance(temperature, bool)
        or not math.isfinite(temperature)
        or not TEMPERATURE_RANGE[0] <= temperature <= TEMPERATURE_RANGE[1]
    ):
        raise AIConfigError("invalid_temperature", "Temperature 0 ile 1 arasında olmalı.")
    low, high = MAX_TOKENS_RANGE[capability]
    if isinstance(params.max_tokens, bool) or not isinstance(params.max_tokens, int) \
            or not low <= params.max_tokens <= high:
        raise AIConfigError(
            "invalid_max_tokens", f"{capability.value} max_tokens {low}-{high} aralığında olmalı."
        )
    if params.timeout_seconds is not None and (
        not math.isfinite(params.timeout_seconds)
        or not TIMEOUT_RANGE[0] <= params.timeout_seconds <= TIMEOUT_RANGE[1]
    ):
        raise AIConfigError("invalid_timeout", "Timeout boş (provider varsayılanı) ya da 1-120 sn olmalı.")
    if params.max_retries is not None and (
        isinstance(params.max_retries, bool)
        or not RETRY_RANGE[0] <= params.max_retries <= RETRY_RANGE[1]
    ):
        raise AIConfigError("invalid_retries", "Retry boş (provider varsayılanı) ya da 0-5 olmalı.")
    if params.reasoning_effort is not None:
        if not model.supports_reasoning_effort:
            raise AIConfigError("reasoning_unsupported", "Model reasoning effort desteklemiyor.")
        if params.reasoning_effort not in model.allowed_reasoning_efforts:
            raise AIConfigError(
                "invalid_reasoning_effort",
                f"Model için izinli reasoning seviyeleri: {', '.join(model.allowed_reasoning_efforts)}",
            )
        if not reasoning_transport_supported(model.provider, params.reasoning_effort):
            # Never store a level the adapter would not actually send.
            raise AIConfigError(
                "reasoning_transport_unsupported",
                f"{model.provider} adapter'ı reasoning_effort={params.reasoning_effort!r} "
                "değerini provider'a iletemiyor.",
            )


# ── reads ────────────────────────────────────────────────────────────────────

def current_version_id(db: Session) -> Optional[int]:
    value = db.query(func.max(AIConfigVersion.id)).scalar()
    return int(value) if value is not None else None


def list_models(db: Session) -> list[ModelDefinition]:
    return [ModelDefinition.from_row(row)
            for row in db.query(AIModelRegistry).order_by(AIModelRegistry.id).all()]


def _model(db: Session, model_id) -> AIModelRegistry:
    row = db.get(AIModelRegistry, int(model_id)) if model_id is not None else None
    if row is None:
        raise AIConfigError("model_not_found", f"Registry modeli bulunamadı: {model_id}", 404)
    return row


def load_active_config(db: Session) -> Optional[ActiveConfig]:
    """Active persisted config, validated against the CURRENT registry.

    Returns None when never bootstrapped. Raises AIConfigError
    (``config_invalid``) when the persisted state is inconsistent."""
    version_id = current_version_id(db)
    if version_id is None:
        return None
    rows = {row.capability: row for row in db.query(AICapabilityConfig).all()}
    assignments = {}
    for cap in CAPABILITIES:
        row = rows.get(cap.value)
        if row is None:
            raise AIConfigError("config_invalid", f"{cap.value} capability config eksik.", 500)
        model_row = db.get(AIModelRegistry, row.model_registry_id)
        if model_row is None:
            raise AIConfigError("config_invalid", f"{cap.value} registry modeli eksik.", 500)
        assignment = CapabilityAssignment(
            cap, ModelDefinition.from_row(model_row), CapabilityParams.from_row(row)
        )
        try:
            validate_assignment(cap, assignment.model, assignment.params)
        except AIConfigError as exc:
            raise AIConfigError("config_invalid", f"{cap.value}: {exc.message}", 500) from None
        assignments[cap] = assignment
    return ActiveConfig(version_id=version_id, assignments=assignments)


def history(db: Session, limit: int = 50) -> list[dict]:
    rows = (db.query(AIConfigVersion).order_by(AIConfigVersion.id.desc())
            .limit(max(1, min(int(limit), 500))).all())
    return [{
        "version": int(row.id),
        "change_type": row.change_type,
        "previous_version": row.previous_version_id,
        "source_version": row.source_version_id,
        "summary": row.summary,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "snapshot": json.loads(row.snapshot),
    } for row in rows]


def audit_log(db: Session, limit: int = 100) -> list[dict]:
    rows = (db.query(AIConfigAudit).order_by(AIConfigAudit.id.desc())
            .limit(max(1, min(int(limit), 500))).all())
    return [{
        "id": int(row.id),
        "event_type": row.event_type,
        "actor": row.actor,
        "capability": row.capability,
        "model_registry_id": row.model_registry_id,
        "old_value": json.loads(row.old_value) if row.old_value else None,
        "new_value": json.loads(row.new_value) if row.new_value else None,
        "old_version": row.old_version_id,
        "new_version": row.new_version_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    } for row in rows]


# ── writes (single transaction + advisory lock + audit) ─────────────────────

def _check_expected(current: Optional[int], expected: Optional[int]) -> None:
    if expected != current:
        raise AIConfigError(
            "stale_version",
            f"Yapılandırma başka bir yönetici tarafından değiştirildi "
            f"(beklenen v{expected}, güncel v{current}). Sayfayı yenileyin.",
            409,
        )


def _persist_version(db: Session, *, active: ActiveConfig, new: dict, change_type: str,
                     actor, summary: str, source_version: Optional[int] = None) -> int:
    """Write the new assignments + an immutable version atomically."""
    snapshot = {"schema": SNAPSHOT_SCHEMA, "capabilities": {
        cap.value: new[cap].snapshot() for cap in CAPABILITIES
    }}
    version = AIConfigVersion(
        snapshot=_dump(snapshot),
        change_type=change_type,
        previous_version_id=active.version_id if active else None,
        source_version_id=source_version,
        summary=summary[:500],
        created_by=actor,
    )
    db.add(version)
    db.flush()
    for cap in CAPABILITIES:
        assignment = new[cap]
        row = db.get(AICapabilityConfig, cap.value)
        if row is None:
            row = AICapabilityConfig(capability=cap.value)
            db.add(row)
        row.model_registry_id = assignment.model.id
        row.temperature = assignment.params.temperature
        row.max_tokens = assignment.params.max_tokens
        row.reasoning_effort = assignment.params.reasoning_effort
        row.timeout_seconds = assignment.params.timeout_seconds
        row.max_retries = assignment.params.max_retries
        row.structured_output_enabled = 1 if assignment.params.structured_output_enabled else 0
        row.config_version_id = version.id
        row.updated_by = actor
    db.flush()
    return int(version.id)


def update_capability_config(db: Session, capability, payload: dict, *,
                             expected_version: Optional[int], actor) -> dict:
    cap = _capability(capability)
    _lock(db)
    active = load_active_config(db)
    if active is None:
        raise AIConfigError(
            "config_not_bootstrapped",
            "Aktif AI yapılandırması yok; önce `init_system db` bootstrap'ı çalıştırılmalı.",
            409,
        )
    _check_expected(active.version_id, expected_version)
    model = ModelDefinition.from_row(_model(db, payload.get("model_registry_id")))
    params = CapabilityParams(
        temperature=payload.get("temperature"),
        max_tokens=payload.get("max_tokens"),
        reasoning_effort=payload.get("reasoning_effort"),
        timeout_seconds=payload.get("timeout_seconds"),
        max_retries=payload.get("max_retries"),
        structured_output_enabled=bool(payload.get("structured_output_enabled", False)),
    )
    if isinstance(params.temperature, int) and not isinstance(params.temperature, bool):
        params = replace(params, temperature=float(params.temperature))
    validate_assignment(cap, model, params)
    old = active.assignments[cap]
    new_assignment = CapabilityAssignment(cap, model, params)
    if new_assignment.snapshot() == old.snapshot():
        return {"changed": False, "version": active.version_id}
    new = dict(active.assignments)
    new[cap] = new_assignment
    summary = (
        f"{cap.value}: {old.model.provider}/{old.model.model_identifier} → "
        f"{model.provider}/{model.model_identifier}"
        if old.model.id != model.id else f"{cap.value}: parametre değişikliği"
    )
    version_id = _persist_version(db, active=active, new=new, change_type="UPDATE",
                                  actor=actor, summary=summary)
    _audit(db, "AI_CONFIG_CHANGED", actor=actor, capability=cap.value, model_id=model.id,
           old=old.snapshot(), new=new_assignment.snapshot(),
           old_version=active.version_id, new_version=version_id)
    return {"changed": True, "version": version_id}


def rollback_to_version(db: Session, source_version_id: int, *,
                        expected_version: Optional[int], actor) -> dict:
    _lock(db)
    active = load_active_config(db)
    if active is None:
        raise AIConfigError("config_not_bootstrapped", "Aktif AI yapılandırması yok.", 409)
    _check_expected(active.version_id, expected_version)
    source = db.get(AIConfigVersion, int(source_version_id))
    if source is None:
        raise AIConfigError("version_not_found", f"Versiyon bulunamadı: v{source_version_id}", 404)
    if int(source.id) == active.version_id:
        raise AIConfigError("already_active", f"v{source.id} zaten aktif.")
    snapshot = json.loads(source.snapshot)
    new = {}
    for cap in CAPABILITIES:
        item = snapshot["capabilities"][cap.value]
        model_row = db.get(AIModelRegistry, item["model_registry_id"])
        if model_row is None:
            raise AIConfigError("rollback_invalid", f"{cap.value}: registry modeli artık yok.")
        model = ModelDefinition.from_row(model_row)
        params = CapabilityParams(
            temperature=float(item["temperature"]),
            max_tokens=int(item["max_tokens"]),
            reasoning_effort=item.get("reasoning_effort"),
            timeout_seconds=item.get("timeout_seconds"),
            max_retries=item.get("max_retries"),
            structured_output_enabled=bool(item.get("structured_output_enabled")),
        )
        try:
            validate_assignment(cap, model, params)  # CURRENT registry rules
        except AIConfigError as exc:
            raise AIConfigError(
                "rollback_invalid",
                f"v{source.id} geri alınamaz — {cap.value}: {exc.message}",
            ) from None
        new[cap] = CapabilityAssignment(cap, model, params)
    version_id = _persist_version(
        db, active=active, new=new, change_type="ROLLBACK", actor=actor,
        summary=f"ROLLBACK v{active.version_id} → snapshot(v{source.id}) → yeni versiyon",
        source_version=int(source.id),
    )
    _audit(db, "AI_CONFIG_ROLLED_BACK", actor=actor,
           old=active.snapshot(), new={"source_version": int(source.id), **snapshot},
           old_version=active.version_id, new_version=version_id)
    return {"changed": True, "version": version_id, "source_version": int(source.id)}


def _active_uses(db: Session, model_id: int) -> list[LLMCapability]:
    return [LLMCapability(row.capability) for row in
            db.query(AICapabilityConfig).filter(AICapabilityConfig.model_registry_id == model_id)]


def register_model(db: Session, payload: dict, *, actor) -> ModelDefinition:
    _lock(db)
    name, identifier, caps, efforts = validate_model_fields(
        display_name=payload.get("display_name"),
        provider=payload.get("provider"),
        model_identifier=payload.get("model_identifier"),
        allowed_capabilities=payload.get("allowed_capabilities"),
        supports_structured_output=bool(payload.get("supports_structured_output")),
        supports_reasoning_effort=bool(payload.get("supports_reasoning_effort")),
        allowed_reasoning_efforts=payload.get("allowed_reasoning_efforts"),
    )
    exists = db.query(AIModelRegistry).filter(
        AIModelRegistry.provider == payload["provider"],
        AIModelRegistry.model_identifier == identifier,
    ).first()
    if exists is not None:
        raise AIConfigError(
            "duplicate_model", f"{payload['provider']}/{identifier} registry'de zaten var.", 409
        )
    row = AIModelRegistry(
        display_name=name,
        provider=payload["provider"],
        model_identifier=identifier,
        enabled=1,
        allowed_capabilities=_dump(list(caps)),
        supports_structured_output=1 if payload.get("supports_structured_output") else 0,
        supports_reasoning_effort=1 if payload.get("supports_reasoning_effort") else 0,
        allowed_reasoning_efforts=_dump(list(efforts)),
        # A new model is never self-approved.
        qualification_status=QualificationStatus.UNTESTED.value,
        created_by=actor,
        updated_by=actor,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        raise AIConfigError(
            "duplicate_model", f"{payload['provider']}/{identifier} registry'de zaten var.", 409
        ) from None
    model = ModelDefinition.from_row(row)
    _audit(db, "MODEL_REGISTERED", actor=actor, model_id=model.id, new=model.to_dict())
    return model


def update_model(db: Session, model_id: int, patch: dict, *, actor) -> ModelDefinition:
    """Update mutable registry fields. Provider/model identity is immutable.

    A change that would invalidate an ACTIVE assignment (disable, BLOCK,
    removing its capability, dropping structured output or its reasoning
    level) is rejected: move the capability to another model first."""
    _lock(db)
    row = _model(db, model_id)
    old = ModelDefinition.from_row(row)
    name, _identifier, caps, efforts = validate_model_fields(
        display_name=patch.get("display_name", old.display_name),
        provider=old.provider,
        model_identifier=old.model_identifier,
        allowed_capabilities=patch.get("allowed_capabilities", list(old.allowed_capabilities)),
        supports_structured_output=patch.get(
            "supports_structured_output", old.supports_structured_output),
        supports_reasoning_effort=patch.get(
            "supports_reasoning_effort", old.supports_reasoning_effort),
        allowed_reasoning_efforts=patch.get(
            "allowed_reasoning_efforts", list(old.allowed_reasoning_efforts)),
    )
    status = old.qualification_status
    reference = old.qualification_reference
    if "qualification_status" in patch and patch["qualification_status"] is not None:
        try:
            status = QualificationStatus(patch["qualification_status"])
        except ValueError:
            raise AIConfigError("invalid_qualification", "Geçersiz qualification durumu.") from None
        if status is QualificationStatus.LEGACY_APPROVED and old.qualification_status is not status:
            raise AIConfigError(
                "invalid_qualification", "LEGACY_APPROVED yalnız bootstrap tarafından atanır."
            )
        if status is QualificationStatus.QUALIFIED:
            reference = (patch.get("qualification_reference") or "").strip()
            if not reference:
                raise AIConfigError(
                    "qualification_reference_required",
                    "QUALIFIED için gerçek bir değerlendirme referansı (run/artifact id) gerekli.",
                )
    candidate = replace(
        old,
        display_name=name,
        enabled=bool(patch.get("enabled", old.enabled)),
        allowed_capabilities=caps,
        supports_structured_output=bool(patch.get(
            "supports_structured_output", old.supports_structured_output)),
        supports_reasoning_effort=bool(patch.get(
            "supports_reasoning_effort", old.supports_reasoning_effort)),
        allowed_reasoning_efforts=efforts,
        qualification_status=status,
    )
    for cap in _active_uses(db, old.id):
        params = CapabilityParams.from_row(db.get(AICapabilityConfig, cap.value))
        try:
            validate_assignment(cap, candidate, params)
        except AIConfigError as exc:
            raise AIConfigError(
                "model_in_use",
                f"Model aktif {cap.value} ataması tarafından kullanılıyor; önce capability'yi "
                f"başka modele taşıyın ({exc.message})",
                409,
            ) from None
    row.display_name = candidate.display_name
    row.enabled = 1 if candidate.enabled else 0
    row.allowed_capabilities = _dump(list(candidate.allowed_capabilities))
    row.supports_structured_output = 1 if candidate.supports_structured_output else 0
    row.supports_reasoning_effort = 1 if candidate.supports_reasoning_effort else 0
    row.allowed_reasoning_efforts = _dump(list(candidate.allowed_reasoning_efforts))
    if status is not old.qualification_status or reference != old.qualification_reference:
        row.qualification_status = status.value
        row.qualification_reference = reference if status is QualificationStatus.QUALIFIED else None
        row.qualified_at = utcnow() if status is QualificationStatus.QUALIFIED else None
        row.qualified_by = actor if status is QualificationStatus.QUALIFIED else None
    row.updated_by = actor
    db.flush()
    new = ModelDefinition.from_row(row)
    if old.enabled and not new.enabled:
        event = "MODEL_DISABLED"
    elif not old.enabled and new.enabled:
        event = "MODEL_ENABLED"
    elif old.qualification_status is not new.qualification_status:
        event = "MODEL_QUALIFICATION_CHANGED"
    else:
        event = "MODEL_UPDATED"
    _audit(db, event, actor=actor, model_id=new.id, old=old.to_dict(), new=new.to_dict())
    return new


# ── bootstrap (idempotent; run by `init_system db`) ─────────────────────────

def _seed_model(db: Session, provider: str, identifier: str, display_name: str) -> AIModelRegistry:
    row = db.query(AIModelRegistry).filter(
        AIModelRegistry.provider == provider,
        AIModelRegistry.model_identifier == identifier,
    ).first()
    if row is not None:
        return row
    row = AIModelRegistry(
        display_name=display_name,
        provider=provider,
        model_identifier=identifier,
        enabled=1,
        allowed_capabilities=_dump([cap.value for cap in CAPABILITIES]),
        # Proven with the strict JSON + Pydantic contract since Phase 2/4.
        supports_structured_output=1,
        # Phase 0 defaults are non-reasoning models; reasoning support is
        # declared per model by an operator, never assumed by the bootstrap.
        supports_reasoning_effort=0,
        allowed_reasoning_efforts="[]",
        qualification_status=QualificationStatus.LEGACY_APPROVED.value,
        created_by=BOOTSTRAP_ACTOR,
        updated_by=BOOTSTRAP_ACTOR,
    )
    db.add(row)
    db.flush()
    _audit(db, "MODEL_REGISTERED", actor=BOOTSTRAP_ACTOR, model_id=int(row.id),
           new=ModelDefinition.from_row(row).to_dict())
    return row


def bootstrap_ai_registry(db: Session, environ: Optional[Mapping[str, str]] = None) -> dict:
    """Seed Phase 0 default models and, once, the env-effective assignments.

    The first version reproduces the env-resolved Phase 5 effective configs
    exactly (same fingerprint), so deploying Phase 6 changes no model/param.
    Idempotent: later runs never create another version or duplicate model.
    """
    import os

    env = os.environ if environ is None else environ
    _lock(db)
    for provider in SUPPORTED_PROVIDERS:
        identifier = _DEFAULT_MODELS[provider]
        _seed_model(db, provider, identifier, f"{identifier} ({provider})")
    if current_version_id(db) is not None:
        return {"bootstrapped": False, "reason": "already_versioned"}
    provider = (env.get("LLM_PROVIDER") or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        return {"bootstrapped": False, "reason": "no_env_provider"}
    configs = resolve_llm_config_set(provider, environ=env)
    assignments = {}
    for cap in CAPABILITIES:
        config = configs.for_capability(cap)
        model_row = _seed_model(
            db, config.provider, config.model, f"{config.model} ({config.provider}, env)"
        )
        params = CapabilityParams(
            temperature=float(config.temperature),
            max_tokens=int(config.max_tokens),
            reasoning_effort=config.reasoning_effort.value if config.reasoning_effort else None,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            structured_output_enabled=config.structured_output_enabled,
        )
        assignment = CapabilityAssignment(cap, ModelDefinition.from_row(model_row), params)
        try:
            validate_assignment(cap, assignment.model, params)
        except AIConfigError as exc:
            # Never silently change an operator's env config to fit the bounds:
            # keep the legacy env path and make the reason visible.
            logger.warning("AI config bootstrap skipped (%s): %s", cap.value, exc.message)
            return {"bootstrapped": False, "reason": f"env_config_out_of_bounds:{exc.code}"}
        if assignment.effective_config().fingerprint != config.fingerprint:
            return {"bootstrapped": False, "reason": "fingerprint_mismatch"}
        assignments[cap] = assignment
    version_id = _persist_version(
        db, active=None, new=assignments, change_type="BOOTSTRAP", actor=BOOTSTRAP_ACTOR,
        summary="Bootstrap: env/default effective config (Phase 5 davranışı)",
    )
    _audit(db, "AI_CONFIG_BOOTSTRAPPED", actor=BOOTSTRAP_ACTOR,
           new={cap.value: assignments[cap].snapshot() for cap in CAPABILITIES},
           new_version=version_id)
    return {"bootstrapped": True, "version": version_id}
