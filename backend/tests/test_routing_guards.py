from datetime import date
from types import SimpleNamespace

import pytest

from core.database import QnA, QnARoutingGuard
from services.routing_guards import RoutingGuardPolicy, upsert_routing_guard


def _guard(**overrides):
    values = {
        "qna_id": 10,
        "guard_ref": "GUARD-10",
        "exact_bypass_enabled": 0,
        "selector_mode": "semantic_selector_only",
        "content_mode": "dynamic",
        "valid_from": None,
        "valid_until": None,
        "on_expiry": "source_check_or_evergreen_answer",
        "source_of_truth": "AUZEF",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_guardless_qna_preserves_existing_routes():
    decision = RoutingGuardPolicy.empty(today=date(2026, 9, 17)).decision(10)
    assert decision.selector_allowed is True
    assert decision.fallback_allowed is True


def test_active_guard_is_selector_only():
    policy = RoutingGuardPolicy({10: _guard()}, today=date(2026, 9, 17))
    decision = policy.decision("10")
    assert decision.selector_allowed is True
    assert decision.fallback_allowed is False
    assert decision.reason is None


@pytest.mark.parametrize(
    ("guard", "reason"),
    [
        (_guard(valid_from=date(2026, 9, 18)), "not_yet_valid"),
        (_guard(valid_until=date(2026, 9, 16)), "expired"),
    ],
)
def test_out_of_window_guard_blocks_selector_and_fallback(guard, reason):
    decision = RoutingGuardPolicy({10: guard}, today=date(2026, 9, 17)).decision(10)
    assert decision.selector_allowed is False
    assert decision.fallback_allowed is False
    assert decision.reason == reason


def test_valid_until_is_inclusive():
    policy = RoutingGuardPolicy(
        {10: _guard(valid_until=date(2026, 9, 17))},
        today=date(2026, 9, 17),
    )
    assert policy.decision(10).selector_allowed is True


def test_upsert_is_idempotent_and_persistent(db):
    qna = QnA(question_text="Guard soru", answer_text="Guard cevap")
    db.add(qna)
    db.flush()

    first = upsert_routing_guard(
        db,
        qna_id=qna.id,
        guard_ref="GUARD-TEST",
        exact_bypass_enabled=False,
        selector_mode="semantic_selector_only",
        content_mode="dynamic",
        valid_from=None,
        valid_until=date(2026, 12, 9),
        on_expiry="block_target_and_fallback_to_current_source",
        source_of_truth="İlk kaynak",
    )
    second = upsert_routing_guard(
        db,
        qna_id=qna.id,
        guard_ref="GUARD-TEST",
        exact_bypass_enabled=False,
        selector_mode="semantic_selector_only",
        content_mode="dynamic",
        valid_from=None,
        valid_until=date(2026, 12, 9),
        on_expiry="block_target_and_fallback_to_current_source",
        source_of_truth="Güncel kaynak",
    )
    db.commit()

    assert first is second
    assert db.query(QnARoutingGuard).count() == 1
    assert db.get(QnARoutingGuard, qna.id).source_of_truth == "Güncel kaynak"


def test_upsert_rejects_unsafe_selector_configuration(db):
    qna = QnA(question_text="Guard soru", answer_text="Guard cevap")
    db.add(qna)
    db.flush()
    with pytest.raises(ValueError, match="exact bypass"):
        upsert_routing_guard(
            db,
            qna_id=qna.id,
            guard_ref="GUARD-TEST",
            exact_bypass_enabled=True,
            selector_mode="semantic_selector_only",
            content_mode="dynamic",
            valid_from=None,
            valid_until=None,
            on_expiry="block",
            source_of_truth="AUZEF",
        )


def test_selector_pool_drops_expired_guarded_candidate(monkeypatch):
    from services import answer_pipeline

    hits = [
        {"id": 10, "qna_id": 10, "question": "eski", "answer": "eski", "score": 1.0, "source": "qdrant"},
        {"id": 11, "qna_id": 11, "question": "güncel", "answer": "güncel", "score": 0.9, "source": "qdrant"},
    ]
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search", lambda _q, limit: hits[:limit])
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", lambda _q, limit: [])
    policy = RoutingGuardPolicy(
        {10: _guard(valid_until=date(2026, 9, 16))},
        today=date(2026, 9, 17),
    )

    pool = answer_pipeline._build_candidate_pool("soru", [], routing_policy=policy)
    assert [candidate["qna_id"] for candidate in pool] == [11]


def test_fallback_skips_active_selector_only_candidate(monkeypatch, db):
    from services import answer_pipeline

    hits = [
        {"id": 10, "qna_id": 10, "question": "korumalı", "answer": "korumalı", "score": 1.0, "source": "meilisearch"},
        {"id": 11, "qna_id": 11, "question": "serbest", "answer": "serbest", "score": 0.99, "source": "meilisearch"},
    ]
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", lambda _q, limit: hits[:limit])
    policy = RoutingGuardPolicy({10: _guard()}, today=date(2026, 9, 17))

    assert answer_pipeline._fallback_answer(
        "soru",
        db,
        use_calendar=False,
        routing_policy=policy,
    ) == ("serbest", "meilisearch")


def test_answer_question_fails_closed_when_guard_store_is_unavailable(monkeypatch, db):
    from services import answer_pipeline

    monkeypatch.setattr(
        answer_pipeline.RoutingGuardPolicy,
        "load",
        classmethod(lambda cls, _db: (_ for _ in ()).throw(RuntimeError("db down"))),
    )
    monkeypatch.setattr(
        answer_pipeline,
        "_fallback_answer",
        lambda *_args, **_kwargs: pytest.fail("fallback must not run"),
    )

    assert answer_pipeline.answer_question("soru", db) == (None, "none")
