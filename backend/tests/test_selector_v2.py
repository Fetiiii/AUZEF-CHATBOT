"""Phase 4: objective candidate eligibility + Selector V2 (SELECT/NONE)."""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from core.database import QnA, QnARoutingGuard
from services.calendar_retrieval import CalendarRetrievalResult, skipped_calendar_result
from services.calendar_utils import format_calendar_answer
from services.candidate_eligibility import (
    DEFAULT_SELECTOR_MAX_CANDIDATES,
    CandidateKind,
    SelectorCandidate,
    build_candidate_set,
    selector_max_candidates,
)
from services.decision_trace import DecisionTrace
from services.llm_config import resolve_llm_config_set
from services.llm_provider import BaseLLMProvider
from services.llm_types import (
    IntentAnalysis,
    IntentAnalyzerResult,
    IntentItem,
    LLMOutcomeStatus,
    SelectionOutcome,
)
from services.routing_guards import RoutingGuardPolicy
from services.selector import (
    SELECTOR_SYSTEM_PROMPT,
    build_selector_prompt,
    parse_selector_output,
)


TODAY = date(2026, 9, 19)


# ── helpers ──────────────────────────────────────────────────────────────────

def _intent(text, *, calendar=False):
    return IntentItem(
        source_text=text,
        normalized_text=text,
        resolved_text=text,
        context_used=False,
        calendar_relevant=calendar,
    )


def _analysis(*texts, calendar=False):
    return IntentAnalysis(
        intent_count=len(texts),
        intents=[_intent(text, calendar=calendar) for text in texts],
    )


class ScriptedProvider(BaseLLMProvider):
    """Real Selector V2 prompt/parse path; only the network call is scripted."""

    provider_name = "openai"

    def __init__(self, analysis, outputs=(), error=None):
        self.model = "gpt-4o-mini"
        self._configs = resolve_llm_config_set("openai", environ={})
        self.analysis = analysis
        self.outputs = list(outputs)
        self.error = error
        self.selector_prompts = []

    def analyze_intents_with_result(self, _current, _previous=()):
        return IntentAnalyzerResult(analysis=self.analysis)

    def _complete(self, system, user, max_tokens=5):
        self.selector_prompts.append((system, user))
        if self.error is not None:
            raise self.error
        return self.outputs.pop(0)

    @property
    def selector_calls(self):
        return len(self.selector_prompts)

    def selector_payload(self, index=0):
        return json.loads(self.selector_prompts[index][1])


def _hit(qna_id, question, answer, *, score=0.9, source="qdrant", alias=None):
    hit = {
        "id": qna_id, "qna_id": qna_id, "question": question,
        "answer": answer, "score": score, "source": source,
    }
    if alias:
        hit["matched_query"] = alias
    return hit


def _seed_qna(db, rows, status=1):
    for qna_id, (question, answer) in rows.items():
        db.add(QnA(id=qna_id, question_text=question, answer_text=answer, status=status))
    db.commit()


def _guard(db, qna_id, **overrides):
    values = dict(
        qna_id=qna_id,
        guard_ref=f"GUARD-{qna_id}",
        exact_bypass_enabled=0,
        selector_mode="semantic_selector_only",
        content_mode="dynamic",
        valid_from=None,
        valid_until=None,
        on_expiry="block",
        source_of_truth="AUZEF",
    )
    values.update(overrides)
    db.add(QnARoutingGuard(**values))
    db.commit()


class RetrievalSpy:
    def __init__(self, qdrant_hits=(), meili_hits=()):
        self.qdrant_hits = list(qdrant_hits)
        self.meili_hits = list(meili_hits)
        self.qdrant_calls = []
        self.meili_calls = []

    def qdrant(self, _query, limit=3):
        self.qdrant_calls.append(limit)
        return self.qdrant_hits[:limit]

    def meili(self, _query, limit):
        self.meili_calls.append(limit)
        return self.meili_hits[:limit]


def _wire(monkeypatch, provider, spy, *, calendar_rows=(), llm_enabled=True):
    from services import answer_pipeline

    monkeypatch.setattr(answer_pipeline, "is_llm_enabled", lambda _db: llm_enabled)
    monkeypatch.setattr(answer_pipeline, "get_llm_provider", lambda _db: provider)
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search", spy.qdrant)
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", spy.meili)
    monkeypatch.setattr(
        answer_pipeline,
        "retrieve_calendar_candidates",
        lambda _query, _db: CalendarRetrievalResult(
            candidates=tuple(calendar_rows),
            trace_snapshot=skipped_calendar_result(relevant=True).trace_snapshot,
        ),
    )
    monkeypatch.setattr(
        answer_pipeline.RoutingGuardPolicy,
        "load",
        classmethod(lambda cls, db: RoutingGuardPolicy(
            {int(row.qna_id): row for row in db.query(QnARoutingGuard).all()},
            today=TODAY,
        )),
    )
    return answer_pipeline


def _forbid_fallback(monkeypatch, answer_pipeline):
    monkeypatch.setattr(
        answer_pipeline, "answer_in_degraded_mode",
        lambda *_a, **_k: pytest.fail("degraded/threshold fallback must not run"),
    )
    monkeypatch.setattr(
        answer_pipeline, "_degraded_calendar",
        lambda *_a, **_k: pytest.fail("calendar fallback must not run"),
    )


def _capture_degraded(monkeypatch, answer_pipeline, answer=None, source="meilisearch"):
    calls = []

    def degraded(query, _db, **kwargs):
        calls.append({"query": query, **kwargs})
        if answer:
            return answer_pipeline.DegradedAnswer(answer, source, qna_id=77)
        return answer_pipeline.DegradedAnswer(None, "none")

    monkeypatch.setattr(answer_pipeline, "answer_in_degraded_mode", degraded)
    return calls


def _calendar_row(row_id=17, event="Bütünleme Sınavları"):
    return SimpleNamespace(
        id=row_id, period="Güz Dönemi", event=event,
        start_date="12.01.2027", end_date="16.01.2027",
    )


def _build(hits, *, calendar=(), policy=None, active=None, budget=32):
    return build_candidate_set(
        calendar_entries=list(calendar),
        qna_hits=list(hits),
        routing_policy=policy,
        active_qna_lookup=(lambda ids: set(ids)) if active is None else active,
        max_candidates=budget,
    )


# ── objective eligibility (§36/§37) ──────────────────────────────────────────

def test_inactive_qna_never_reaches_selector(db, monkeypatch):
    _seed_qna(db, {1: ("aktif", "a1")})
    _seed_qna(db, {2: ("pasif", "a2")}, status=0)
    provider = ScriptedProvider(_analysis("soru"), ['{"decision":"NONE"}'])
    spy = RetrievalSpy([_hit(1, "aktif", "a1"), _hit(2, "pasif", "a2")])
    pipeline = _wire(monkeypatch, provider, spy)
    trace = DecisionTrace(endpoint="test")
    pipeline.answer_question("soru", db, trace=trace)
    refs = [c["candidate_ref"] for c in provider.selector_payload()["candidates"]]
    assert refs == ["qna:1"]
    retrieval = trace.to_dict()["retrieval"][0]
    assert retrieval["excluded_candidate_refs"] == ["qna:2"]
    assert retrieval["eligibility_exclusion_reasons"] == {"inactive_or_missing": 1}


def test_guard_outside_validity_is_excluded_and_selector_only_guard_is_eligible(db):
    policy = RoutingGuardPolicy(
        {
            10: SimpleNamespace(
                valid_from=None, valid_until=date(2026, 9, 1),
                selector_mode="semantic_selector_only",
            ),
            11: SimpleNamespace(
                valid_from=date(2026, 10, 1), valid_until=None,
                selector_mode="semantic_selector_only",
            ),
            12: SimpleNamespace(
                valid_from=None, valid_until=None,
                selector_mode="semantic_selector_only",
            ),
        },
        today=TODAY,
    )
    build = _build(
        [_hit(10, "q10", "a10"), _hit(11, "q11", "a11"), _hit(12, "q12", "a12")],
        policy=policy,
    )
    assert [c.candidate_ref for c in build.candidates] == ["qna:12"]
    assert build.trace_snapshot["eligibility_exclusion_reasons"] == {
        "expired": 1, "not_yet_valid": 1,
    }


def test_guard_evaluation_error_fails_closed_per_candidate():
    class BrokenPolicy:
        def decision(self, qna_id):
            if qna_id == 1:
                raise RuntimeError("corrupt guard row")
            return SimpleNamespace(selector_allowed=True, reason=None)

    build = _build([_hit(1, "q1", "a1"), _hit(2, "q2", "a2")], policy=BrokenPolicy())
    assert [c.candidate_ref for c in build.candidates] == ["qna:2"]
    assert build.exclusions[0].reason == "guard_evaluation_error"


def test_activity_lookup_failure_or_absence_fails_closed_for_qna_only():
    def broken(_ids):
        raise RuntimeError("db down")

    build = _build([_hit(1, "q1", "a1")], calendar=[_calendar_row()], active=broken)
    assert [c.candidate_ref for c in build.candidates] == ["calendar:17"]
    assert build.trace_snapshot["eligibility_exclusion_reasons"] == {
        "activity_lookup_failed": 1
    }
    missing = build_candidate_set(
        calendar_entries=[], qna_hits=[_hit(1, "q1", "a1")], routing_policy=None,
        active_qna_lookup=None, max_candidates=32,
    )
    assert missing.candidates == ()


def test_structurally_invalid_qna_is_excluded():
    build = _build([
        {"id": "x", "qna_id": None, "question": "q", "answer": "a", "score": 1},
        _hit(2, "q2", ""),
        _hit(3, "", "a3"),
        _hit(4, "q4", "a4"),
    ])
    assert [c.candidate_ref for c in build.candidates] == ["qna:4"]
    assert build.trace_snapshot["eligibility_exclusion_reasons"] == {
        "missing_qna_id": 1, "missing_answer": 1, "missing_question": 1,
    }


def test_valid_calendar_candidate_is_eligible_with_deterministic_answer():
    row = _calendar_row()
    build = _build([], calendar=[row])
    (candidate,) = build.candidates
    assert candidate.candidate_ref == "calendar:17"
    assert candidate.kind is CandidateKind.CALENDAR
    assert candidate.calendar_id == 17 and candidate.qna_id is None
    assert candidate.canonical_text == "Güz Dönemi Bütünleme Sınavları"
    assert candidate.answer_text == format_calendar_answer(
        row.period, row.event, row.start_date, row.end_date
    )


def test_qna_and_calendar_with_same_numeric_id_are_not_duplicates():
    build = _build([_hit(17, "q17", "a17")], calendar=[_calendar_row(17)])
    assert [c.candidate_ref for c in build.candidates] == ["calendar:17", "qna:17"]


def test_calendar_route_closed_gives_selector_no_calendar_candidate(db, monkeypatch):
    _seed_qna(db, {5: ("Kayıt nasıl yapılır?", "OBS")})
    provider = ScriptedProvider(_analysis("Kayıt nasıl yapılır?"), ['{"decision":"NONE"}'])
    pipeline = _wire(monkeypatch, provider, RetrievalSpy([_hit(5, "Kayıt nasıl yapılır?", "OBS")]))
    monkeypatch.setattr(
        pipeline, "retrieve_calendar_candidates",
        lambda *_a, **_k: pytest.fail("closed Calendar route must not retrieve"),
    )
    pipeline.answer_question("Kayıt nasıl yapılır?", db)
    kinds = {c["kind"] for c in provider.selector_payload()["candidates"]}
    assert kinds == {"QNA"}


def test_eligibility_does_not_drop_specific_candidate_by_keyword_or_score():
    hits = [
        _hit(129, "Yatay geçiş işlemleri ile ilgili detaylı bilgiye nereden ulaşabiliriz?",
             "genel", score=0.95),
        _hit(342, "Merkezi yatay geçiş başvurusu nasıl yapılır?", "merkezi", score=0.10),
    ]
    build = _build(hits)
    assert [c.candidate_ref for c in build.candidates] == ["qna:129", "qna:342"]
    assert build.trace_snapshot["excluded_candidate_count"] == 0


# ── zero / one eligible candidate (§38/§39) ─────────────────────────────────

def test_zero_eligible_candidates_skips_selector_and_forces_no_answer(db, monkeypatch):
    _seed_qna(db, {10: ("eski", "eski cevap")})
    _guard(db, 10, valid_until=date(2026, 1, 1))
    provider = ScriptedProvider(_analysis("soru"))
    pipeline = _wire(monkeypatch, provider, RetrievalSpy([_hit(10, "eski", "eski cevap")]))
    _forbid_fallback(monkeypatch, pipeline)
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("soru", db, trace=trace) == (None, "none")
    assert provider.selector_calls == 0
    snapshot = trace.to_dict()
    assert snapshot["selectors"][0]["selector_called"] is False
    assert snapshot["selectors"][0]["selector_status"] == "no_eligible_candidates"
    assert snapshot["selectors"][0]["semantic_none"] is False
    assert snapshot["execution"]["intents"][0]["resolution"] == "no_eligible_candidates"
    assert snapshot["execution"]["execution_mode"] == "NORMAL_LLM"
    assert snapshot["fallback"]["fallback_entered"] is False


def test_single_eligible_candidate_still_goes_through_selector_which_may_say_none(
    db, monkeypatch
):
    _seed_qna(db, {1: ("tek", "tek cevap")})
    provider = ScriptedProvider(_analysis("soru"), ['{"decision":"NONE"}'])
    pipeline = _wire(monkeypatch, provider, RetrievalSpy([_hit(1, "tek", "tek cevap")]))
    _forbid_fallback(monkeypatch, pipeline)
    assert pipeline.answer_question("soru", db) == (None, "none")
    assert provider.selector_calls == 1


# ── structured SELECT (§40/§41) ─────────────────────────────────────────────

def test_structured_select_returns_curated_qna_answer_verbatim(db, monkeypatch):
    rows = {
        129: ("Yatay geçiş işlemleri ile ilgili detaylı bilgiye nereden ulaşabiliriz?",
              "Genel yatay geçiş cevabı."),
        342: ("Merkezi yatay geçiş başvurusu nasıl yapılır?",
              "Merkezi yatay geçiş  cevabı\n(biçim korunur)."),
    }
    _seed_qna(db, rows)
    provider = ScriptedProvider(
        _analysis("Merkezi yatay geçiş nasıl yapılır?"),
        ['{"decision":"SELECT","candidate_ref":"qna:342"}'],
    )
    spy = RetrievalSpy([_hit(qna_id, *rows[qna_id]) for qna_id in (129, 342)])
    pipeline = _wire(monkeypatch, provider, spy)
    trace = DecisionTrace(endpoint="test")
    answer, source = pipeline.answer_question(
        "Merkezi yatay geçiş nasıl yapılır?", db, trace=trace
    )
    assert (answer, source) == (rows[342][1], "llm")
    selector = trace.to_dict()["selectors"][0]
    assert selector["selected_candidate_ref"] == "qna:342"
    assert selector["selected_kind"] == "QNA"
    assert selector["selected_qna_id"] == 342
    assert selector["selected_calendar_id"] is None
    assert selector["selector_decision"] == "SELECT"
    assert trace.to_dict()["final"]["final_qna_ids"] == [342]


def test_calendar_select_returns_deterministic_stored_calendar_answer(db, monkeypatch):
    _seed_qna(db, {7: ("Bütünleme notları ne zaman açıklanır?", "qna cevabı")})
    row = _calendar_row(17)
    provider = ScriptedProvider(
        _analysis("Bütünleme ne zaman?", calendar=True),
        ['{"decision":"SELECT","candidate_ref":"calendar:17"}'],
    )
    pipeline = _wire(
        monkeypatch, provider,
        RetrievalSpy([_hit(7, "Bütünleme notları ne zaman açıklanır?", "qna cevabı")]),
        calendar_rows=[row],
    )
    trace = DecisionTrace(endpoint="test")
    answer, source = pipeline.answer_question("Bütünleme ne zaman?", db, trace=trace)
    assert answer == format_calendar_answer(
        row.period, row.event, row.start_date, row.end_date
    )
    assert source == "llm"
    assert provider.selector_calls == 1  # no generative/answer-writing call
    selector = trace.to_dict()["selectors"][0]
    assert selector["selected_candidate_ref"] == "calendar:17"
    assert selector["selected_kind"] == "CALENDAR"
    assert selector["selected_calendar_id"] == 17
    assert selector["candidate_kinds"] == ["CALENDAR", "QNA"]


# ── semantic NONE is final (§42) ────────────────────────────────────────────

def test_semantic_none_is_final_with_no_meili_qdrant_or_calendar_fallback(
    db, monkeypatch
):
    _seed_qna(db, {1: ("Kayıt nasıl yapılır?", "Kayıt cevabı")})
    provider = ScriptedProvider(_analysis("Kayıt ne zaman?", calendar=True),
                                ['{"decision":"NONE"}'])
    # Scores far above both thresholds: V1 would have forced this answer.
    spy = RetrievalSpy(
        qdrant_hits=[_hit(1, "Kayıt nasıl yapılır?", "Kayıt cevabı", score=0.99)],
        meili_hits=[_hit(1, "Kayıt nasıl yapılır?", "Kayıt cevabı", score=0.99,
                         source="meilisearch")],
    )
    pipeline = _wire(monkeypatch, provider, spy, calendar_rows=[_calendar_row()])
    monkeypatch.setattr(
        pipeline, "_degraded_calendar",
        lambda *_a, **_k: pytest.fail("calendar fallback must not run"),
    )
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("Kayıt ne zaman?", db, trace=trace) == (None, "none")
    # Exactly the retrieval calls; no threshold fallback calls (Meili 3 / Qdrant 5).
    assert spy.meili_calls == [5]
    assert spy.qdrant_calls == [24]
    snapshot = trace.to_dict()
    assert snapshot["selectors"][0]["selector_status"] == "semantic_none"
    assert snapshot["selectors"][0]["semantic_none"] is True
    assert snapshot["execution"]["intents"][0]["resolution"] == "semantic_none"
    assert snapshot["execution"]["execution_mode"] == "NORMAL_LLM"
    assert snapshot["degraded"] == []
    assert snapshot["fallback"]["fallback_entered"] is False
    assert snapshot["fallback"]["meili_fallback_used"] is False
    assert snapshot["fallback"]["qdrant_fallback_used"] is False
    assert snapshot["fallback"]["calendar_fallback_used"] is False


def test_semantic_none_endpoint_offers_only_guard_safe_non_answer_suggestions(
    client, db, monkeypatch
):
    import services.providers as providers

    _seed_qna(db, {
        1: ("Kayıt nasıl yapılır?", "Kayıt cevabı"),
        2: ("Korumalı soru", "korumalı"),
    })
    _guard(db, 2)
    provider = ScriptedProvider(_analysis("kayıt"), ['{"decision":"NONE"}'])
    _wire(monkeypatch, provider, RetrievalSpy([_hit(1, "Kayıt nasıl yapılır?", "Kayıt cevabı")]))
    providers.FakeMeili.suggestions = [
        {"qna_id": 1, "question": "Kayıt nasıl yapılır?"},
        {"qna_id": 2, "question": "Korumalı soru"},
    ]
    body = client.get("/api/search", params={"q": "kayıt"}).json()
    assert body["status"] == "suggest"
    assert "answer" not in body
    assert body["suggestions"] == ["Kayıt nasıl yapılır?"]


# ── invalid output / errors are not semantic NONE (§43/§44) ─────────────────

@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("3", "schema_violation"),
        ("0", "schema_violation"),
        ('{"decision":"SELECT","candidate_ref":"qna:999999"}', "unknown_candidate_ref"),
        ('{"decision":"MAYBE"}', "schema_violation"),
        ('{"decision":"SELECT"}', "schema_violation"),
        ('{"decision":"NONE","candidate_ref":"qna:1"}', "schema_violation"),
        ('{"decision":"SELECT","candidate_ref":"qna:1","confidence":0.9}',
         "schema_violation"),
        ('{"decision":"NONE","reason":"yok"}', "schema_violation"),
        ('```json\n{"decision":"NONE"}\n```', "malformed_json"),
        ('{"decision":', "malformed_json"),
        ("", "empty_output"),
        ("   ", "empty_output"),
        (None, "empty_output"),
    ],
)
def test_invalid_outputs_are_invalid_not_semantic_none(raw, reason):
    candidates = [SelectorCandidate("qna:1", CandidateKind.QNA, "q", "a", qna_id=1)]
    result = parse_selector_output(raw, candidates)
    assert result.status is LLMOutcomeStatus.INVALID_OUTPUT
    assert result.invalid_reason == reason
    assert result.answer is None


def test_explicit_null_ref_is_a_valid_none():
    candidates = [SelectorCandidate("qna:1", CandidateKind.QNA, "q", "a", qna_id=1)]
    result = parse_selector_output('{"decision":"NONE","candidate_ref":null}', candidates)
    assert result.status is LLMOutcomeStatus.SEMANTIC_NONE


@pytest.mark.parametrize(
    "raw",
    ["3", '{"decision":"SELECT","candidate_ref":"qna:999999"}', '{"decision":"MAYBE"}'],
)
def test_invalid_output_enters_degraded_path_not_none_path(
    db, monkeypatch, raw
):
    _seed_qna(db, {1: ("q", "a")})
    provider = ScriptedProvider(_analysis("soru"), [raw])
    pipeline = _wire(monkeypatch, provider, RetrievalSpy([_hit(1, "q", "a")]))
    calls = _capture_degraded(monkeypatch, pipeline)
    trace = DecisionTrace(endpoint="test")
    pipeline.answer_question("soru", db, trace=trace)
    assert calls[0]["reason"] == "selector_invalid_output"
    assert calls[0]["calendar_gate"] == "closed"  # intent not calendar_relevant
    assert calls[0]["query_kind"] == "resolved_intent"
    selector = trace.to_dict()["selectors"][0]
    assert selector["invalid_output"] is True
    assert selector["semantic_none"] is False
    assert "3" not in json.dumps(selector.get("invalid_reason"))


@pytest.mark.parametrize(
    ("error", "outcome", "reason"),
    [
        (RuntimeError("provider down"), "model_error", "selector_model_error"),
        (TimeoutError("late"), "timeout", "selector_timeout"),
    ],
)
def test_model_error_and_timeout_use_explicit_degraded_path(
    db, monkeypatch, error, outcome, reason
):
    _seed_qna(db, {1: ("q", "a")})
    provider = ScriptedProvider(_analysis("soru"), error=error)
    pipeline = _wire(monkeypatch, provider, RetrievalSpy([_hit(1, "q", "a")]))
    calls = _capture_degraded(monkeypatch, pipeline, answer="compat")
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("soru", db, trace=trace) == ("compat", "meilisearch")
    assert [(c["query"], c["reason"]) for c in calls] == [("soru", reason)]
    assert trace.to_dict()["execution"]["execution_mode"] == "REQUEST_DEGRADED"
    selector = trace.to_dict()["selectors"][0]
    assert selector["selector_status"] == outcome
    assert selector[outcome] is True
    assert selector["semantic_none"] is False


# ── prompt contract: general/specific, leakage, context (§45–§48) ───────────

def test_prompt_contract_states_verifier_role_and_unstated_qualifier_rule():
    prompt = SELECTOR_SYSTEM_PROMPT
    assert "en benzer adayı bulmak değil, doğrulamaktır" in prompt
    assert "kullanıcının açıkça söylemediği bir qualifier varsayılarak" in prompt
    assert "merkezi yatay geçiş candidate'ı seçilmez" in prompt
    assert "Merkezi yatay geçiş nasıl yapılır?" in prompt
    assert "Ortak kelimeler taşımak tek başına yeterli değildir" in prompt
    assert "Birden fazla candidate kabul edilebilir olsa bile yalnız birini seç" in prompt
    assert "Candidate sırası önem, doğruluk veya güven bildirmez" in prompt
    for forbidden_request in ("güven skoru", "gerekçe", "açıklama"):
        assert forbidden_request in prompt  # only as things NOT to write
    assert '{"decision":"NONE"}' in prompt


def test_selector_input_has_no_score_rank_provider_or_alias_metadata():
    build = _build([
        _hit(129, "Genel soru", "genel", score=0.98765, source="qdrant", alias="yatay"),
        _hit(342, "Merkezi soru", "merkezi", score=0.91234, source="meilisearch"),
    ])
    system, user = build_selector_prompt("Yatay geçiş nasıl yapılır?", build.candidates)
    payload = json.loads(user)
    assert set(payload) == {"resolved_intent", "candidates"}
    for candidate in payload["candidates"]:
        assert set(candidate) == {"candidate_ref", "kind", "canonical_text", "answer_text"}
    serialized = system + user
    for leaked in ("0.98765", "0.91234", "qdrant", "meilisearch", "matched_query",
                   "alias", "score", "rank", "order"):
        assert leaked not in user
    # Internal trace still keeps provenance for observability.
    order = build.trace_snapshot["candidate_order"]
    assert order[0]["score"] == 0.98765 and order[0]["alias_match"] is True
    assert "0.98765" not in serialized


def test_selector_sees_only_resolved_intent_not_conversation_history(db, monkeypatch):
    _seed_qna(db, {1: ("q", "a")})
    provider = ScriptedProvider(_analysis("resolved intent text"), ['{"decision":"NONE"}'])
    pipeline = _wire(monkeypatch, provider, RetrievalSpy([_hit(1, "q", "a")]))
    context = (
        {"role": "user", "content": "Önceki kullanıcı sorusu"},
        {"role": "bot", "content": "Bot secret answer"},
    )
    pipeline.answer_question("şimdiki soru", db, conversation_context=context)
    system, user = provider.selector_prompts[0]
    assert json.loads(user)["resolved_intent"] == "resolved intent text"
    for leaked in ("Önceki kullanıcı sorusu", "Bot secret", "şimdiki soru"):
        assert leaked not in system + user


# Real AUZEF QnA ids/questions verified read-only in the live DB on 2026-09-19;
# answers here are short synthetic stand-ins (live answers are not copied).
NEAR_QNA_PAIRS = (
    ((316, "Çözüm Merkezi'ne giriş yapamıyorum, ne yapmalıyım?"),
     (335, "Çözüm Merkezi'ne talep oluşturamıyorum, ne yapmalıyım?")),
    ((328, "Çocuk Gelişimi bölümü kapatıldı mı?"),
     (336, "YKS ile Çocuk Gelişimi lisans bölümüne tercih yapabilir miyim?")),
    ((310, "Kayıt tarihleri ne zamandır?"),
     (405, "İkinci Üniversite sınavsız kayıt tarihleri ne zaman?")),
    ((333, "Öğrenci affı başvurusu nasıl yapılır?"),
     (319, "Af başvurusu nasıl yapılır?")),
    ((129, "Yatay geçiş işlemleri ile ilgili detaylı bilgiye nereden ulaşabiliriz?"),
     (342, "Merkezi yatay geçiş başvurusu nasıl yapılır?")),
    ((347, "Staj defterindeki eksikleri düzeltmek için ne kadar süre var?"),
     (72, "Endüstri Mühendisliği programı öğrencisiyim staj defterimi en son hangi "
          "tarihe kadar göndermem gerekiyor?")),
)


@pytest.mark.parametrize(("left", "right"), NEAR_QNA_PAIRS)
def test_near_qna_pairs_both_reach_selector_with_stable_refs(monkeypatch, left, right):
    from services import answer_pipeline

    hits = [
        _hit(left[0], left[1], f"synthetic-{left[0]}", score=0.9),
        _hit(right[0], right[1], f"synthetic-{right[0]}", score=0.9),
    ]
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", lambda _q, limit: [])

    def pool(order):
        monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search",
                            lambda _q, limit: order[:limit])
        return answer_pipeline._build_candidate_pool(
            left[1], [], active_qna_lookup=lambda ids: set(ids)
        )

    first = pool(hits)
    second = pool(list(reversed(hits)))
    refs = [c.candidate_ref for c in first]
    # Equal scores: order falls back to qna_id, independent of provider order.
    assert refs == sorted(refs, key=lambda ref: int(ref.split(":")[1]))
    assert refs == [c.candidate_ref for c in second]
    _system, user = build_selector_prompt(left[1], first)
    assert left[1] in user and right[1] in user


# ── multi intent independence (§49) ─────────────────────────────────────────

def test_one_intent_none_does_not_remove_other_intent_answer(db, monkeypatch):
    _seed_qna(db, {1: ("A soru", "A cevap")})
    provider = ScriptedProvider(
        _analysis("A", "B"),
        ['{"decision":"SELECT","candidate_ref":"qna:1"}', '{"decision":"NONE"}'],
    )
    pipeline = _wire(monkeypatch, provider, RetrievalSpy([_hit(1, "A soru", "A cevap")]))
    _forbid_fallback(monkeypatch, pipeline)
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("A ve B", db, trace=trace) == ("A cevap", "llm")
    assert provider.selector_calls == 2
    statuses = [item["selector_status"] for item in trace.to_dict()["selectors"]]
    assert statuses == ["selected", "semantic_none"]


def test_mixed_none_and_error_degrades_only_the_error_intent(db, monkeypatch):
    """Phase 5 (g25): the NONE stays final; only intent B degrades on its text."""
    _seed_qna(db, {1: ("q", "a")})
    provider = ScriptedProvider(_analysis("A", "B"), ['{"decision":"NONE"}', "bozuk"])
    pipeline = _wire(monkeypatch, provider, RetrievalSpy([_hit(1, "q", "a")]))
    calls = _capture_degraded(monkeypatch, pipeline, answer="B cevabı")
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("A ve B", db, trace=trace) == ("B cevabı", "meilisearch")
    assert [c["query"] for c in calls] == ["B"]
    intents = trace.to_dict()["execution"]["intents"]
    assert [item["resolution"] for item in intents] == ["semantic_none", "degraded_selected"]


# ── candidate budget (§50) ──────────────────────────────────────────────────

def test_budget_config_parse():
    assert selector_max_candidates({}) == DEFAULT_SELECTOR_MAX_CANDIDATES == 32
    assert selector_max_candidates({"SELECTOR_MAX_CANDIDATES": "12"}) == 12
    assert selector_max_candidates({"SELECTOR_MAX_CANDIDATES": " "}) == 32
    for bad in ("0", "-1", "abc", "1.5"):
        with pytest.raises(RuntimeError):
            selector_max_candidates({"SELECTOR_MAX_CANDIDATES": bad})


def test_default_budget_covers_phase3_retrieval_ceiling():
    from services.calendar_retrieval import MAX_CALENDAR_CANDIDATE_LIMIT

    qdrant_limit, meili_limit = 24, 5
    assert DEFAULT_SELECTOR_MAX_CANDIDATES >= (
        qdrant_limit + meili_limit + MAX_CALENDAR_CANDIDATE_LIMIT
    )
    hits = [_hit(i, f"q{i}", f"a{i}", score=1 - i / 100) for i in range(1, 30)]
    build = _build(hits, calendar=[_calendar_row(i) for i in (1, 2, 3)])
    assert build.trace_snapshot["candidate_truncated"] is False


def test_truncation_is_deterministic_and_traced():
    hits = [_hit(i, f"q{i}", f"a{i}", score=0.5) for i in (5, 3, 9, 1)]
    first = _build(hits, calendar=[_calendar_row(8)], budget=3)
    second = _build(hits, calendar=[_calendar_row(8)], budget=3)
    refs = [c.candidate_ref for c in first.candidates]
    assert refs == [c.candidate_ref for c in second.candidates]
    assert refs == ["calendar:8", "qna:5", "qna:3"]
    snapshot = first.trace_snapshot
    assert snapshot["candidate_truncated"] is True
    assert snapshot["candidate_budget"] == 3
    assert snapshot["truncated_candidate_refs"] == ["qna:9", "qna:1"]
    assert snapshot["candidate_count_after_eligibility"] == 5


def test_pipeline_ordering_is_stable_for_same_retrieval(monkeypatch):
    from services import answer_pipeline

    hits = [_hit(3, "q3", "a3", score=0.8), _hit(1, "q1", "a1", score=0.8),
            _hit(2, "q2", "a2", score=0.9)]
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search",
                        lambda _q, limit: hits[:limit])
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", lambda _q, limit: [])
    runs = [
        [c.candidate_ref for c in answer_pipeline._build_candidate_pool(
            "soru", [], active_qna_lookup=lambda ids: set(ids))]
        for _ in range(3)
    ]
    assert runs == [["qna:2", "qna:1", "qna:3"]] * 3


# ── exact alias is not a bypass (§51) ───────────────────────────────────────

def test_exact_alias_hit_does_not_bypass_selector(db, monkeypatch):
    _seed_qna(db, {1: ("Kanonik soru", "Kanonik cevap")})
    provider = ScriptedProvider(_analysis("büt"), ['{"decision":"NONE"}'])
    pipeline = _wire(monkeypatch, provider, RetrievalSpy([
        _hit(1, "Kanonik soru", "Kanonik cevap", score=1.0, alias="büt"),
    ]))
    _forbid_fallback(monkeypatch, pipeline)
    assert pipeline.answer_question("büt", db) == (None, "none")
    assert provider.selector_calls == 1


# ── guard-safe suggestions (§23/§35) ────────────────────────────────────────

def test_suggestions_hide_protected_expired_inactive_and_unidentified_titles(
    db, monkeypatch
):
    import services.providers as providers
    from services import answer_pipeline

    monkeypatch.setattr(
        answer_pipeline.RoutingGuardPolicy, "load",
        classmethod(lambda cls, db: RoutingGuardPolicy(
            {int(row.qna_id): row for row in db.query(QnARoutingGuard).all()},
            today=TODAY,
        )),
    )
    _seed_qna(db, {
        1: ("Serbest soru", "a"),
        2: ("Selector-only korumalı soru", "a"),
        3: ("Süresi geçmiş soru", "a"),
        4: ("Henüz geçerli olmayan soru", "a"),
    })
    _seed_qna(db, {5: ("Pasif soru", "a")}, status=0)
    _guard(db, 2)
    _guard(db, 3, valid_until=date(2026, 1, 1))
    _guard(db, 4, valid_from=date(2027, 1, 1))
    providers.FakeMeili.suggestions = [
        {"qna_id": 1, "question": "Serbest soru"},
        {"qna_id": 2, "question": "Selector-only korumalı soru"},
        {"qna_id": 3, "question": "Süresi geçmiş soru"},
        {"qna_id": 4, "question": "Henüz geçerli olmayan soru"},
        {"qna_id": 5, "question": "Pasif soru"},
        {"qna_id": 99, "question": "Silinmiş soru"},
        "Kimliksiz başlık",
    ]
    trace = DecisionTrace(endpoint="test")
    assert answer_pipeline.guard_safe_suggestions("soru", db, trace=trace) == [
        "Serbest soru"
    ]
    suggestions = trace.to_dict()["suggestions"]
    assert suggestions["offered_qna_ids"] == [1]
    assert suggestions["exclusion_reasons"] == {
        "guard_fallback_blocked": 3,
        "inactive_or_missing": 2,
        "missing_qna_id": 1,
    }
    assert "Serbest soru" not in json.dumps(trace.to_dict(), ensure_ascii=False)


def test_suggestions_fail_closed_when_guard_store_unavailable(db, monkeypatch):
    import services.providers as providers
    from services import answer_pipeline

    _seed_qna(db, {1: ("Serbest soru", "a")})
    providers.FakeMeili.suggestions = [{"qna_id": 1, "question": "Serbest soru"}]
    monkeypatch.setattr(
        answer_pipeline.RoutingGuardPolicy, "load",
        classmethod(lambda cls, _db: (_ for _ in ()).throw(RuntimeError("down"))),
    )
    assert answer_pipeline.guard_safe_suggestions("soru", db) == []


# ── LLM OFF is unchanged (§55) ──────────────────────────────────────────────

def test_llm_off_never_calls_analyzer_or_selector(db, monkeypatch):
    from services import answer_pipeline

    monkeypatch.setattr(answer_pipeline, "is_llm_enabled", lambda _db: False)
    monkeypatch.setattr(
        answer_pipeline, "get_llm_provider",
        lambda _db: pytest.fail("LLM provider must not be requested when LLM is off"),
    )
    calls = _capture_degraded(monkeypatch, answer_pipeline)
    answer_pipeline.answer_question("Final ne zaman?", db)
    assert calls[0]["calendar_gate"] == "date_query"
    assert calls[0]["reason"] == "llm_disabled_or_unavailable"


def test_selection_outcome_values_are_distinct():
    values = {item.value for item in SelectionOutcome}
    assert values == {
        "selected", "semantic_none", "no_eligible_candidates",
        "invalid_output", "model_error", "timeout", "circuit_open",
    }
