"""QnA routing guard'larının runtime değerlendirmesi ve kalıcı upsert'i."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from core.database import QnARoutingGuard


ISTANBUL = ZoneInfo("Europe/Istanbul")
SEMANTIC_SELECTOR_ONLY = "semantic_selector_only"


def _qna_id(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _today() -> date:
    return datetime.now(ISTANBUL).date()


@dataclass(frozen=True)
class GuardDecision:
    selector_allowed: bool
    fallback_allowed: bool
    reason: str | None = None


class RoutingGuardPolicy:
    """Tek request için guard snapshot'ı.

    Guard bulunmayan QnA eski davranışını korur. Guard bulunan QnA ise tarih
    aralığı dışında tamamen bloke edilir; ``semantic_selector_only`` modunda
    yalnız LLM seçici havuzuna girebilir ve doğrudan fallback cevabı olamaz.
    """

    def __init__(self, guards: dict[int, QnARoutingGuard], *, today: date | None = None):
        self.guards = guards
        self.today = today or _today()

    @classmethod
    def load(cls, db: Session, *, today: date | None = None) -> "RoutingGuardPolicy":
        rows = db.query(QnARoutingGuard).all()
        return cls({int(row.qna_id): row for row in rows}, today=today)

    @classmethod
    def empty(cls, *, today: date | None = None) -> "RoutingGuardPolicy":
        return cls({}, today=today)

    def decision(self, qna_id) -> GuardDecision:
        normalized = _qna_id(qna_id)
        guard = self.guards.get(normalized) if normalized is not None else None
        if guard is None:
            return GuardDecision(True, True)
        if guard.valid_from is not None and self.today < guard.valid_from:
            return GuardDecision(False, False, "not_yet_valid")
        if guard.valid_until is not None and self.today > guard.valid_until:
            return GuardDecision(False, False, "expired")

        if guard.selector_mode != SEMANTIC_SELECTOR_ONLY:
            return GuardDecision(False, False, "unsupported_selector_mode")
        return GuardDecision(True, False)

    def selector_allows(self, candidate: dict) -> bool:
        return self.decision(candidate.get("qna_id")).selector_allowed

    def fallback_allows(self, candidate: dict) -> bool:
        return self.decision(candidate.get("qna_id")).fallback_allowed


def upsert_routing_guard(
    db: Session,
    *,
    qna_id: int,
    guard_ref: str,
    exact_bypass_enabled: bool,
    selector_mode: str,
    content_mode: str,
    valid_from: date | None,
    valid_until: date | None,
    on_expiry: str,
    source_of_truth: str,
) -> QnARoutingGuard:
    """Migration araçlarının kullanacağı idempotent guard yazma sözleşmesi."""
    if selector_mode != SEMANTIC_SELECTOR_ONLY:
        raise ValueError(f"Desteklenmeyen selector_mode: {selector_mode}")
    if exact_bypass_enabled:
        raise ValueError("semantic_selector_only guard için exact bypass açılamaz")
    if valid_from and valid_until and valid_from > valid_until:
        raise ValueError("valid_from, valid_until sonrasına gelemez")

    guard = db.get(QnARoutingGuard, qna_id)
    if guard is None:
        guard = QnARoutingGuard(qna_id=qna_id)
        db.add(guard)
    guard.guard_ref = guard_ref
    guard.exact_bypass_enabled = 1 if exact_bypass_enabled else 0
    guard.selector_mode = selector_mode
    guard.content_mode = content_mode
    guard.valid_from = valid_from
    guard.valid_until = valid_until
    guard.on_expiry = on_expiry
    guard.source_of_truth = source_of_truth
    db.flush()
    return guard
