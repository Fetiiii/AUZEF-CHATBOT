"""Admin API for the managed LLM model registry and capability config (Phase 6).

Authorization reuses the existing role hierarchy (no parallel permission
system): the middleware gives the whole ``/api/ai-config`` prefix an ``admin``
floor (view_ai_config), and every write handler additionally requires
``super_admin`` (manage_ai_config) through ``require_ai_config_manager``.
Validation lives in ``services.ai_registry`` and is authoritative.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from admin.auth import AUTH_ENFORCED, current_user, role_level
from core.database import AdminUser, SystemConfig
from core.deps import actor_email, get_db, provider_key_configured
from services import ai_registry
from services.ai_registry import AIConfigError
from services.llm_runtime import AI_CONFIG_CACHE

router = APIRouter(prefix="/api/ai-config", tags=["ai-config"])

VIEW_ROLE = "admin"
MANAGE_ROLE = "super_admin"

Provider = Literal["openai", "openrouter", "gemini"]
Capability = Literal["intent_analyzer", "selector"]
Effort = Literal["none", "low", "medium", "high"]


def require_ai_config_manager(me: Optional[AdminUser] = Depends(current_user)):
    """Handler-level write check; authoritative even if a route rule is missed."""
    if not AUTH_ENFORCED:
        return me
    if me is None:
        raise HTTPException(status_code=401, detail="Oturum gerekli.")
    if role_level(me.role) < role_level(MANAGE_ROLE):
        raise HTTPException(status_code=403, detail="AI yapılandırmasını değiştirme yetkiniz yok.")
    return me


def _error(exc: AIConfigError, db: Session) -> JSONResponse:
    db.rollback()
    return JSONResponse(status_code=exc.status, content={"detail": exc.message, "code": exc.code})


class ModelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=120)
    provider: Provider
    model_identifier: str = Field(min_length=1, max_length=200)
    allowed_capabilities: list[Capability] = Field(min_length=1)
    supports_structured_output: bool
    supports_reasoning_effort: bool = False
    allowed_reasoning_efforts: list[Effort] = []


class ModelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    enabled: Optional[bool] = None
    allowed_capabilities: Optional[list[Capability]] = None
    supports_structured_output: Optional[bool] = None
    supports_reasoning_effort: Optional[bool] = None
    allowed_reasoning_efforts: Optional[list[Effort]] = None
    qualification_status: Optional[Literal["UNTESTED", "QUALIFIED", "BLOCKED"]] = None
    qualification_reference: Optional[str] = Field(default=None, max_length=255)


class CapabilityConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: Optional[int]
    model_registry_id: int
    temperature: float
    max_tokens: int
    reasoning_effort: Optional[Effort] = None
    timeout_seconds: Optional[float] = None
    max_retries: Optional[int] = None
    structured_output_enabled: bool = False


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: Optional[int]


def _config_view(db: Session) -> dict:
    llm_row = db.get(SystemConfig, "LLM_ENABLED")
    models = ai_registry.list_models(db)
    try:
        active = ai_registry.load_active_config(db)
        status, error = ("OK" if active else "NOT_CONFIGURED"), None
    except AIConfigError as exc:
        active, status, error = None, "CONFIG_INVALID", exc.message
    capabilities = {}
    for cap in ai_registry.CAPABILITIES:
        eligible = []
        for model in models:
            params = active.assignments[cap].params if active else ai_registry.CapabilityParams(
                max_tokens=ai_registry.MAX_TOKENS_RANGE[cap][0])
            try:
                ai_registry.validate_assignment(
                    cap, model, ai_registry.CapabilityParams(
                        temperature=params.temperature, max_tokens=params.max_tokens))
                eligible.append(model.id)
            except AIConfigError:
                continue
        entry = {
            "eligible_model_ids": eligible,
            "max_tokens_range": list(ai_registry.MAX_TOKENS_RANGE[cap]),
            "provider_key_configured": None,
        }
        if active:
            assignment = active.assignments[cap]
            entry.update({
                "model": assignment.model.to_dict(),
                **assignment.params.to_dict(),
                "config_fingerprint": assignment.effective_config().fingerprint,
                # Only a boolean: the key value is never read into the response.
                "provider_key_configured": provider_key_configured(
                    assignment.model.provider, db),
            })
        capabilities[cap.value] = entry
    return {
        "status": status,
        "error": error,
        "version": active.version_id if active else None,
        "llm_enabled": bool(llm_row and (llm_row.value or "").lower() == "true"),
        "capabilities": capabilities,
        "bounds": {
            "temperature": list(ai_registry.TEMPERATURE_RANGE),
            "timeout_seconds": list(ai_registry.TIMEOUT_RANGE),
            "max_retries": list(ai_registry.RETRY_RANGE),
        },
        "propagation_seconds": AI_CONFIG_CACHE.ttl,
    }


@router.get("/models")
def get_models(db: Session = Depends(get_db)):
    return {"models": [model.to_dict() for model in ai_registry.list_models(db)]}


@router.post("/models", status_code=201)
def create_model(body: ModelCreateRequest, db: Session = Depends(get_db),
                 me=Depends(require_ai_config_manager)):
    try:
        model = ai_registry.register_model(db, body.model_dump(), actor=actor_email(me))
        db.commit()
    except AIConfigError as exc:
        return _error(exc, db)
    return model.to_dict()


@router.patch("/models/{model_id}")
def patch_model(model_id: int, body: ModelUpdateRequest, db: Session = Depends(get_db),
                me=Depends(require_ai_config_manager)):
    try:
        model = ai_registry.update_model(
            db, model_id, body.model_dump(exclude_unset=True), actor=actor_email(me)
        )
        db.commit()
    except AIConfigError as exc:
        return _error(exc, db)
    AI_CONFIG_CACHE.invalidate()
    return model.to_dict()


@router.get("/config")
def get_config(db: Session = Depends(get_db)):
    return _config_view(db)


@router.put("/config/{capability}")
def put_capability_config(capability: Capability, body: CapabilityConfigRequest,
                          db: Session = Depends(get_db),
                          me=Depends(require_ai_config_manager)):
    payload = body.model_dump()
    expected = payload.pop("expected_version")
    try:
        result = ai_registry.update_capability_config(
            db, capability, payload, expected_version=expected, actor=actor_email(me)
        )
        db.commit()
    except AIConfigError as exc:
        return _error(exc, db)
    AI_CONFIG_CACHE.invalidate()
    view = _config_view(db)
    warnings = (
        ["provider_key_missing"]
        if view["capabilities"][capability].get("provider_key_configured") is False else []
    )
    return {**result, "warnings": warnings, "config": view}


@router.get("/config/history")
def get_history(limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_db)):
    return {"versions": ai_registry.history(db, limit)}


@router.post("/config/rollback/{version_id}")
def rollback(version_id: int, body: RollbackRequest, db: Session = Depends(get_db),
             me=Depends(require_ai_config_manager)):
    try:
        result = ai_registry.rollback_to_version(
            db, version_id, expected_version=body.expected_version, actor=actor_email(me)
        )
        db.commit()
    except AIConfigError as exc:
        return _error(exc, db)
    AI_CONFIG_CACHE.invalidate()
    return {**result, "config": _config_view(db)}


@router.get("/audit")
def get_audit(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    return {"events": ai_registry.audit_log(db, limit)}
