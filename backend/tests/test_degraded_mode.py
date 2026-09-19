"""Phase 5: LLM failure isolation, circuit breaker and formal degraded mode."""
from __future__ import annotations

import threading
from datetime import date

import pytest

from core.database import AcademicCalendar, QnA, QnARoutingGuard, SystemConfig
from services.calendar_retrieval import (
    CURRENT_TERM_CONFIG_KEY,
    CURRENT_YEAR_CONFIG_KEY,
    CalendarRetrievalResult,
    serialize_aliases,
    skipped_calendar_result,
)
from services.circuit_breaker import (
    BreakerConfig,
    CallOutcomeKind,
    CircuitBreaker,
    CircuitState,
    breaker_key,
)
from services.decision_trace import DecisionTrace
from services.intent_analyzer import safe_single_intent
from services.llm_config import LLMCapability, resolve_llm_config_set
from services.llm_provider import BaseLLMProvider
from services.llm_types import (
    IntentAnalysis,
    IntentAnalyzerResult,
    IntentItem,
    LLMOutcomeStatus,
    LLMParseStatus,
)
from services.routing_guards import RoutingGuardPolicy


TODAY = date(2026, 9, 19)
SELECT_1 = '{"decision":"SELECT","candidate_ref":"qna:1"}'
NONE = '{"decision":"NONE"}'


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# ── helpers ──────────────────────────────────────────────────────────────────

def _intent(text, resolved=None, *, calendar=False):
    return IntentItem(
        source_text=text,
        normalized_text=text,
        resolved_text=resolved or text,
        context_used=resolved is not None,
        calendar_relevant=calendar,
    )


class ScriptedProvider(BaseLLMProvider):
    """Real Selector V2 prompt/parse; scripted analyzer result and network."""

    provider_name = "openai"

    def __init__(self, intents=(), selector=(), *, analyzer_status=LLMOutcomeStatus.SUCCESS,
                 environ=None):
        self.model = "gpt-4o-mini"
        self._configs = resolve_llm_config_set("openai", environ=environ or {})
        self.intents = list(intents)
        self.selector_script = list(selector)
        self.analyzer_status = analyzer_status
        self.analyzer_calls = 0
        self.selector_prompts = []

    def analyze_intents_with_result(self, current, _previous=()):
        self.analyzer_calls += 1
        if self.analyzer_status is not LLMOutcomeStatus.SUCCESS:
            return IntentAnalyzerResult(
                analysis=safe_single_intent(current),
                status=self.analyzer_status,
                parse_status=(
                    LLMParseStatus.INVALID_OUTPUT
                    if self.analyzer_status is LLMOutcomeStatus.INVALID_OUTPUT
                    else LLMParseStatus.FALLBACK
                ),
                fallback_to_single=True,
            )
        return IntentAnalyzerResult(analysis=IntentAnalysis(
            intent_count=len(self.intents), intents=self.intents,
        ))

    def _complete(self, system, user, max_tokens=5):
        self.selector_prompts.append(user)
        item = self.selector_script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def selector_calls(self):
        return len(self.selector_prompts)


def _key(provider, capability=LLMCapability.SELECTOR):
    config = provider.effective_config(capability)
    return breaker_key(capability.value, config.provider, config.model, config.fingerprint)


def _hit(qna_id, question, answer, score=0.95, source="meilisearch"):
    return {"id": qna_id, "qna_id": qna_id, "question": question,
            "answer": answer, "score": score, "source": source}


def _seed(db, rows, status=1):
    for qna_id, answer in rows.items():
        db.add(QnA(id=qna_id, question_text=f"q{qna_id}", answer_text=answer, status=status))
    db.commit()


class Retrieval:
    """Query-aware fake retrieval; records every (source, query, limit) call."""

    def __init__(self, meili=None, qdrant=None):
        self.meili_by_query = meili or {}
        self.qdrant_by_query = qdrant or {}
        self.calls = []

    def meili(self, query, limit):
        self.calls.append(("meili", query, limit))
        return list(self.meili_by_query.get(query, []))[:limit]

    def qdrant(self, query, limit=3):
        self.calls.append(("qdrant", query, limit))
        return list(self.qdrant_by_query.get(query, []))[:limit]

    def degraded_queries(self):
        # Degraded Meili uses limit 3 (retrieval for the selector uses 5).
        return [query for source, query, limit in self.calls
                if source == "meili" and limit == 3]


def _wire(monkeypatch, provider, retrieval, *, enabled=True, breaker=None):
    from services import answer_pipeline

    monkeypatch.setattr(answer_pipeline, "is_llm_enabled", lambda _db: enabled)
    monkeypatch.setattr(answer_pipeline, "get_llm_provider", lambda _db: provider)
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", retrieval.meili)
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search", retrieval.qdrant)
    monkeypatch.setattr(
        answer_pipeline, "retrieve_calendar_candidates",
        lambda _q, _db, limit=2: CalendarRetrievalResult(
            candidates=(), trace_snapshot=skipped_calendar_result(relevant=True).trace_snapshot,
        ),
    )
    monkeypatch.setattr(
        answer_pipeline.RoutingGuardPolicy, "load",
        classmethod(lambda cls, db: RoutingGuardPolicy(
            {int(row.qna_id): row for row in db.query(QnARoutingGuard).all()},
            today=TODAY,
        )),
    )
    if breaker is not None:
        monkeypatch.setattr(answer_pipeline, "LLM_CIRCUIT_BREAKER", breaker)
    return answer_pipeline


def _forbid_degraded(monkeypatch, pipeline):
    monkeypatch.setattr(
        pipeline, "answer_in_degraded_mode",
        lambda *_a, **_k: pytest.fail("degraded path must not run"),
    )


def _set_llm_flag(db, value="true"):
    db.add(SystemConfig(key="LLM_ENABLED", value=value))
    db.commit()


# ── circuit breaker unit behavior (§4–§8, §27, §37) ─────────────────────────

def test_breaker_opens_after_threshold_consecutive_failures():
    breaker = CircuitBreaker(BreakerConfig(3, 60), clock=FakeClock())
    states = []
    for _ in range(3):
        permit = breaker.acquire("k")
        assert permit.allowed
        states.append(breaker.record(permit, CallOutcomeKind.FAILURE).state_after)
    assert states == [CircuitState.CLOSED, CircuitState.CLOSED, CircuitState.OPEN]
    assert breaker.acquire("k").allowed is False


def test_success_resets_consecutive_failures():
    breaker = CircuitBreaker(BreakerConfig(3, 60), clock=FakeClock())
    for kind in (CallOutcomeKind.FAILURE, CallOutcomeKind.FAILURE, CallOutcomeKind.SUCCESS):
        breaker.record(breaker.acquire("k"), kind)
    assert breaker.snapshot("k") == (CircuitState.CLOSED, 0)


def test_neutral_invalid_output_never_counts_or_opens():
    breaker = CircuitBreaker(BreakerConfig(3, 60), clock=FakeClock())
    breaker.record(breaker.acquire("k"), CallOutcomeKind.FAILURE)
    for _ in range(5):
        breaker.record(breaker.acquire("k"), CallOutcomeKind.NEUTRAL)
    assert breaker.snapshot("k") == (CircuitState.CLOSED, 1)


def test_half_open_probe_success_closes():
    clock = FakeClock()
    breaker = CircuitBreaker(BreakerConfig(1, 60), clock=clock)
    breaker.record(breaker.acquire("k"), CallOutcomeKind.FAILURE)
    clock.now += 59.9
    assert breaker.acquire("k").allowed is False
    clock.now += 0.1
    probe = breaker.acquire("k")
    assert probe.allowed and probe.probe
    assert breaker.snapshot("k")[0] is CircuitState.HALF_OPEN
    record = breaker.record(probe, CallOutcomeKind.SUCCESS)
    assert record.state_after is CircuitState.CLOSED and record.transition == "recovered"
    assert breaker.acquire("k").allowed and not breaker.acquire("k").probe


def test_half_open_probe_failure_reopens_and_restarts_cooldown():
    clock = FakeClock()
    breaker = CircuitBreaker(BreakerConfig(1, 60), clock=clock)
    breaker.record(breaker.acquire("k"), CallOutcomeKind.FAILURE)
    clock.now += 60
    probe = breaker.acquire("k")
    record = breaker.record(probe, CallOutcomeKind.FAILURE)
    assert record.state_after is CircuitState.OPEN and record.transition == "reopened"
    clock.now += 30
    assert breaker.acquire("k").allowed is False  # cooldown restarted at probe time
    clock.now += 30
    assert breaker.acquire("k").probe is True


def test_concurrent_half_open_admits_exactly_one_probe():
    clock = FakeClock()
    breaker = CircuitBreaker(BreakerConfig(1, 60), clock=clock)
    breaker.record(breaker.acquire("k"), CallOutcomeKind.FAILURE)
    clock.now += 61
    barrier = threading.Barrier(16)
    permits = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        permit = breaker.acquire("k")
        with lock:
            permits.append(permit)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(p.allowed for p in permits) == 1
    assert sum(p.probe for p in permits) == 1


def test_lost_probe_is_released_after_another_cooldown():
    clock = FakeClock()
    breaker = CircuitBreaker(BreakerConfig(1, 60), clock=clock)
    breaker.record(breaker.acquire("k"), CallOutcomeKind.FAILURE)
    clock.now += 60
    assert breaker.acquire("k").probe  # never recorded (e.g. crashed request)
    assert breaker.acquire("k").allowed is False
    clock.now += 60
    assert breaker.acquire("k").probe


def test_breaker_config_is_env_configurable_and_validated():
    assert BreakerConfig.from_env({}) == BreakerConfig(3, 60.0)
    assert BreakerConfig.from_env({
        "LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD": "5",
        "LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS": "12.5",
    }) == BreakerConfig(5, 12.5)
    for env in (
        {"LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD": "0"},
        {"LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD": "x"},
        {"LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS": "0"},
        {"LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS": "-1"},
    ):
        with pytest.raises(RuntimeError):
            BreakerConfig.from_env(env)


def test_breaker_key_is_capability_and_config_scoped():
    old = resolve_llm_config_set("openai", environ={}).selector
    new = resolve_llm_config_set("openai", environ={"LLM_SELECTOR_MODEL": "gpt-new"}).selector
    analyzer = resolve_llm_config_set("openai", environ={}).intent_analyzer
    keys = {
        breaker_key("selector", old.provider, old.model, old.fingerprint),
        breaker_key("selector", new.provider, new.model, new.fingerprint),
        breaker_key("intent_analyzer", analyzer.provider, analyzer.model, analyzer.fingerprint),
    }
    assert len(keys) == 3
    assert breaker_key("selector", old.provider, old.model, old.fingerprint).startswith(
        "selector:openai:gpt-4o-mini:"
    )


# ── semantic outcomes never touch the breaker or degraded path (§43/§44) ───

def test_semantic_none_is_not_a_failure_and_never_degrades(db, monkeypatch):
    _seed(db, {1: "a1"})
    provider = ScriptedProvider([_intent("soru")], [NONE])
    retrieval = Retrieval(meili={"soru": [_hit(1, "q1", "a1", score=0.99)]})
    pipeline = _wire(monkeypatch, provider, retrieval)
    _forbid_degraded(monkeypatch, pipeline)
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("soru", db, trace=trace) == (None, "none")
    snapshot = trace.to_dict()
    assert snapshot["execution"]["execution_mode"] == "NORMAL_LLM"
    assert pipeline.LLM_CIRCUIT_BREAKER.snapshot(_key(provider)) == (CircuitState.CLOSED, 0)
    selector_circuit = [c for c in snapshot["circuit"] if c["capability"] == "selector"][0]
    assert selector_circuit["availability_outcome"] == "success"
    assert retrieval.degraded_queries() == []


def test_no_eligible_candidates_skips_selector_breaker_and_degraded(db, monkeypatch):
    provider = ScriptedProvider([_intent("soru")])
    pipeline = _wire(monkeypatch, provider, Retrieval())
    _forbid_degraded(monkeypatch, pipeline)
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("soru", db, trace=trace) == (None, "none")
    assert provider.selector_calls == 0
    assert [c["capability"] for c in trace.to_dict()["circuit"]] == ["intent_analyzer"]
    assert pipeline.LLM_CIRCUIT_BREAKER.snapshot(_key(provider)) == (CircuitState.CLOSED, 0)


# ── selector request-level failure and breaker lifecycle (§45–§50) ──────────

def test_single_selector_timeout_degrades_only_that_intent_and_counts_once(
    db, monkeypatch
):
    _seed(db, {1: "a1", 2: "degraded answer"})
    _set_llm_flag(db, "true")
    provider = ScriptedProvider([_intent("soru")], [TimeoutError("late")])
    retrieval = Retrieval(meili={"soru": [_hit(2, "q2", "degraded answer")]})
    pipeline = _wire(monkeypatch, provider, retrieval)
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("soru", db, trace=trace) == (
        "degraded answer", "meilisearch"
    )
    assert pipeline.LLM_CIRCUIT_BREAKER.snapshot(_key(provider)) == (CircuitState.CLOSED, 1)
    snapshot = trace.to_dict()
    assert snapshot["execution"]["execution_mode"] == "REQUEST_DEGRADED"
    assert snapshot["execution"]["intents"][0]["selection_outcome"] == "timeout"
    assert snapshot["execution"]["intents"][0]["resolution"] == "degraded_selected"
    circuit = [c for c in snapshot["circuit"] if c["capability"] == "selector"][0]
    assert circuit["failure_kind"] == "timeout"
    assert circuit["failure_category"] == "TIMEOUT"
    assert circuit["consecutive_failures_after"] == 1
    degraded = snapshot["degraded"][0]
    assert degraded["degraded_selected_source"] == "meilisearch"
    assert degraded["degraded_selected_qna_id"] == 2
    assert degraded["degraded_calendar_attempted"] is False
    assert degraded["degraded_meili_attempted"] is True
    assert degraded["degraded_qdrant_attempted"] is False
    assert db.get(SystemConfig, "LLM_ENABLED").value == "true"  # never auto-OFF


def test_three_infra_failures_open_selector_circuit_and_skip_next_call(db, monkeypatch):
    _seed(db, {1: "a1"})
    provider = ScriptedProvider(
        [_intent("soru")],
        [RuntimeError("down"), TimeoutError("late"), RuntimeError("down")],
    )
    breaker = CircuitBreaker(BreakerConfig(3, 60), clock=FakeClock())
    retrieval = Retrieval(qdrant={"soru": [_hit(1, "q1", "a1", source="qdrant")]})
    pipeline = _wire(monkeypatch, provider, retrieval, breaker=breaker)
    states = []
    for _ in range(3):
        pipeline.answer_question("soru", db)
        states.append(breaker.snapshot(_key(provider))[0])
    assert states == [CircuitState.CLOSED, CircuitState.CLOSED, CircuitState.OPEN]

    trace = DecisionTrace(endpoint="test")
    pipeline.answer_question("soru", db, trace=trace)
    assert provider.selector_calls == 3  # the 4th request did not call the provider
    snapshot = trace.to_dict()
    assert snapshot["execution"]["execution_mode"] == "CIRCUIT_DEGRADED"
    assert snapshot["execution"]["intents"][0]["degraded_reason"] == "selector_circuit_open"
    circuit = [c for c in snapshot["circuit"] if c["capability"] == "selector"][0]
    assert circuit["llm_call_skipped"] is True
    assert circuit["circuit_state_before"] == "OPEN"
    assert snapshot["selectors"][0]["selector_called"] is False
    assert snapshot["degraded"][0]["query_kind"] == "resolved_intent"


def test_selector_half_open_probe_recovers_through_pipeline(db, monkeypatch):
    _seed(db, {1: "a1"})
    clock = FakeClock()
    breaker = CircuitBreaker(BreakerConfig(1, 60), clock=clock)
    provider = ScriptedProvider([_intent("soru")], [RuntimeError("down"), SELECT_1])
    retrieval = Retrieval(qdrant={"soru": [_hit(1, "q1", "a1", source="qdrant")]})
    pipeline = _wire(monkeypatch, provider, retrieval, breaker=breaker)
    pipeline.answer_question("soru", db)
    assert breaker.snapshot(_key(provider))[0] is CircuitState.OPEN
    pipeline.answer_question("soru", db)  # within cooldown: skipped
    assert provider.selector_calls == 1
    clock.now += 60
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("soru", db, trace=trace) == ("a1", "llm")
    circuit = [c for c in trace.to_dict()["circuit"] if c["capability"] == "selector"][0]
    assert circuit["probe_attempted"] is True
    assert circuit["transition"] == "recovered"
    assert breaker.snapshot(_key(provider)) == (CircuitState.CLOSED, 0)


def test_three_invalid_outputs_degrade_but_never_open_the_circuit(db, monkeypatch):
    _seed(db, {1: "a1"})
    provider = ScriptedProvider([_intent("soru")], ["3", '{"decision":"MAYBE"}', "bozuk"])
    retrieval = Retrieval(qdrant={"soru": [_hit(1, "q1", "a1", source="qdrant")]})
    pipeline = _wire(monkeypatch, provider, retrieval)
    for _ in range(3):
        trace = DecisionTrace(endpoint="test")
        pipeline.answer_question("soru", db, trace=trace)
        assert trace.to_dict()["execution"]["intents"][0]["degraded_reason"] == (
            "selector_invalid_output"
        )
    assert pipeline.LLM_CIRCUIT_BREAKER.snapshot(_key(provider)) == (CircuitState.CLOSED, 0)
    assert retrieval.degraded_queries() == ["soru", "soru", "soru"]


# ── admin OFF, OFF → ON, config isolation (§51–§53) ─────────────────────────

def test_admin_off_is_deterministic_calendar_meili_qdrant_without_llm(db, monkeypatch):
    provider = ScriptedProvider([_intent("x")])
    retrieval = Retrieval(
        meili={"Final ne zaman?": [_hit(1, "q1", "a1", score=0.5)]},
        qdrant={"Final ne zaman?": [_hit(1, "q1", "a1", score=0.5, source="qdrant")]},
    )
    _seed(db, {1: "a1"})
    pipeline = _wire(monkeypatch, provider, retrieval, enabled=False)
    calendar_calls = []
    monkeypatch.setattr(
        pipeline, "retrieve_calendar_candidates",
        lambda q, _db, limit=2: calendar_calls.append(q) or CalendarRetrievalResult(
            candidates=(), trace_snapshot=skipped_calendar_result(relevant=True).trace_snapshot,
        ),
    )
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("Final ne zaman?", db, trace=trace) == (None, "none")
    assert provider.analyzer_calls == 0 and provider.selector_calls == 0
    assert pipeline.LLM_CIRCUIT_BREAKER._states == {}
    assert calendar_calls == ["Final ne zaman?"]
    assert [c[0] for c in retrieval.calls] == ["meili", "qdrant"]
    snapshot = trace.to_dict()
    assert snapshot["execution"]["execution_mode"] == "ADMIN_DEGRADED"
    assert snapshot["circuit"] == []
    run = snapshot["degraded"][0]
    assert (run["degraded_calendar_attempted"], run["degraded_meili_attempted"],
            run["degraded_qdrant_attempted"], run["degraded_no_answer"]) == (
        True, True, True, True)


def test_admin_off_to_on_resets_breakers_deterministically(db, monkeypatch):
    _seed(db, {1: "a1"})
    provider = ScriptedProvider([_intent("soru")], [RuntimeError("down"), SELECT_1])
    breaker = CircuitBreaker(BreakerConfig(1, 3600), clock=FakeClock())
    retrieval = Retrieval(qdrant={"soru": [_hit(1, "q1", "a1", source="qdrant")]})
    pipeline = _wire(monkeypatch, provider, retrieval, breaker=breaker)
    pipeline.answer_question("soru", db)
    assert breaker.snapshot(_key(provider))[0] is CircuitState.OPEN

    monkeypatch.setattr(pipeline, "is_llm_enabled", lambda _db: False)
    pipeline.answer_question("soru", db)
    assert breaker.snapshot(_key(provider))[0] is CircuitState.OPEN  # OFF: no mutation
    monkeypatch.setattr(pipeline, "is_llm_enabled", lambda _db: True)
    assert pipeline.answer_question("soru", db) == ("a1", "llm")
    assert provider.selector_calls == 2


def test_settings_api_enable_resets_node_breakers(make_user, login):
    from services.circuit_breaker import LLM_CIRCUIT_BREAKER

    LLM_CIRCUIT_BREAKER.record(LLM_CIRCUIT_BREAKER.acquire("k"), CallOutcomeKind.FAILURE)
    make_user("super@iu.tr", role="super_admin")
    client = login("super@iu.tr")
    assert client.put("/api/settings/llm", json={"enabled": False}).status_code == 200
    assert LLM_CIRCUIT_BREAKER.snapshot("k") == (CircuitState.CLOSED, 1)
    assert client.put("/api/settings/llm", json={"enabled": True}).status_code == 200
    assert LLM_CIRCUIT_BREAKER.snapshot("k") == (CircuitState.CLOSED, 0)


def test_new_selector_config_is_isolated_from_old_open_circuit(db, monkeypatch):
    _seed(db, {1: "a1"})
    old = ScriptedProvider([_intent("soru")], [RuntimeError("down")])
    breaker = CircuitBreaker(BreakerConfig(1, 3600), clock=FakeClock())
    retrieval = Retrieval(qdrant={"soru": [_hit(1, "q1", "a1", source="qdrant")]})
    pipeline = _wire(monkeypatch, old, retrieval, breaker=breaker)
    pipeline.answer_question("soru", db)
    assert breaker.snapshot(_key(old))[0] is CircuitState.OPEN

    new = ScriptedProvider([_intent("soru")], [SELECT_1],
                           environ={"LLM_SELECTOR_MODEL": "gpt-new"})
    monkeypatch.setattr(pipeline, "get_llm_provider", lambda _db: new)
    assert pipeline.answer_question("soru", db) == ("a1", "llm")
    assert new.selector_calls == 1


# ── Intent Analyzer failures (§16/§17/§25/§54) ──────────────────────────────

@pytest.mark.parametrize(
    ("status", "counted"),
    [
        (LLMOutcomeStatus.MODEL_ERROR, 1),
        (LLMOutcomeStatus.TIMEOUT, 1),
        (LLMOutcomeStatus.INVALID_OUTPUT, 0),
    ],
)
def test_analyzer_failure_degrades_whole_raw_turn_without_selector(
    db, monkeypatch, status, counted
):
    _seed(db, {5: "raw answer"})
    provider = ScriptedProvider(analyzer_status=status)
    retrieval = Retrieval(meili={"ham soru": [_hit(5, "q5", "raw answer")]})
    pipeline = _wire(monkeypatch, provider, retrieval)
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("ham soru", db, trace=trace) == (
        "raw answer", "meilisearch"
    )
    assert provider.selector_calls == 0
    assert pipeline.LLM_CIRCUIT_BREAKER.snapshot(
        _key(provider, LLMCapability.INTENT_ANALYZER)
    ) == (CircuitState.CLOSED, counted)
    snapshot = trace.to_dict()
    assert snapshot["execution"]["execution_mode"] == "REQUEST_DEGRADED"
    assert snapshot["execution"]["degraded_reason"] == f"intent_analyzer_{status.value}"
    assert snapshot["degraded"][0]["query_kind"] == "raw_current_turn"
    assert snapshot["intent_analyzer"]["fallback_to_single"] is True


def test_open_analyzer_circuit_skips_analyzer_and_selector(db, monkeypatch):
    _seed(db, {5: "raw answer"})
    provider = ScriptedProvider(analyzer_status=LLMOutcomeStatus.TIMEOUT)
    breaker = CircuitBreaker(BreakerConfig(1, 3600), clock=FakeClock())
    retrieval = Retrieval(meili={"ham soru": [_hit(5, "q5", "raw answer")]})
    pipeline = _wire(monkeypatch, provider, retrieval, breaker=breaker)
    pipeline.answer_question("ham soru", db)
    assert provider.analyzer_calls == 1
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("ham soru", db, trace=trace)[0] == "raw answer"
    assert provider.analyzer_calls == 1 and provider.selector_calls == 0
    snapshot = trace.to_dict()
    assert snapshot["execution"]["execution_mode"] == "CIRCUIT_DEGRADED"
    assert snapshot["execution"]["degraded_reason"] == "intent_analyzer_circuit_open"
    assert snapshot["circuit"][0]["llm_call_skipped"] is True
    assert snapshot["circuit"][0]["circuit_state_before"] == "OPEN"


# ── selector failure uses resolved_text; multi-intent isolation (§55–§58) ───

def test_selector_failure_degrades_on_resolved_text_not_raw_or_context(db, monkeypatch):
    _seed(db, {1: "a1", 9: "resolved answer"})
    provider = ScriptedProvider(
        [_intent("peki bahar?", resolved="bahar dönemi kayıt yenileme")],
        [TimeoutError("late")],
    )
    retrieval = Retrieval(
        qdrant={"bahar dönemi kayıt yenileme": [_hit(1, "q1", "a1", source="qdrant")]},
        meili={"bahar dönemi kayıt yenileme": [_hit(9, "q9", "resolved answer")]},
    )
    pipeline = _wire(monkeypatch, provider, retrieval)
    context = ({"role": "user", "content": "kayıt yenileme"},
               {"role": "bot", "content": "bot metni"})
    assert pipeline.answer_question("peki bahar?", db, conversation_context=context) == (
        "resolved answer", "meilisearch"
    )
    assert retrieval.degraded_queries() == ["bahar dönemi kayıt yenileme"]


def test_g25_none_intent_stays_final_while_timeout_intent_degrades(db, monkeypatch):
    _seed(db, {1: "a1", 2: "B answer"})
    provider = ScriptedProvider([_intent("A"), _intent("B")], [NONE, TimeoutError("late")])
    retrieval = Retrieval(
        qdrant={"A": [_hit(1, "q1", "a1", source="qdrant")],
                "B": [_hit(1, "q1", "a1", source="qdrant")]},
        meili={"A": [_hit(1, "q1", "a1", score=0.99)], "B": [_hit(2, "q2", "B answer")]},
    )
    pipeline = _wire(monkeypatch, provider, retrieval)
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("A ve B", db, trace=trace) == ("B answer", "meilisearch")
    assert retrieval.degraded_queries() == ["B"]  # never A, never the raw turn
    intents = trace.to_dict()["execution"]["intents"]
    assert [(i["resolution"], i["selection_outcome"]) for i in intents] == [
        ("semantic_none", "semantic_none"), ("degraded_selected", "timeout"),
    ]


def test_selected_intent_is_kept_and_error_intent_degrades_independently(db, monkeypatch):
    _seed(db, {1: "A answer", 2: "B answer"})
    provider = ScriptedProvider([_intent("A"), _intent("B")], [SELECT_1, RuntimeError("down")])
    retrieval = Retrieval(
        qdrant={"A": [_hit(1, "q1", "A answer", source="qdrant")],
                "B": [_hit(1, "q1", "A answer", source="qdrant")]},
        meili={"B": [_hit(2, "q2", "B answer")]},
    )
    pipeline = _wire(monkeypatch, provider, retrieval)
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("A ve B", db, trace=trace) == (
        "A answer\n\nB answer", "llm"
    )
    assert retrieval.degraded_queries() == ["B"]
    assert trace.to_dict()["final"]["final_qna_ids"] == [1, 2]
    assert trace.to_dict()["final"]["answer_count"] == 2


def test_both_intent_errors_degrade_each_resolved_text_never_raw_turn(db, monkeypatch):
    _seed(db, {1: "a1"})
    provider = ScriptedProvider([_intent("A"), _intent("B")],
                                [TimeoutError("x"), RuntimeError("y")])
    low = [_hit(1, "q1", "a1", score=0.5, source="qdrant")]  # below thresholds
    retrieval = Retrieval(qdrant={"A": low, "B": low})
    pipeline = _wire(monkeypatch, provider, retrieval)
    assert pipeline.answer_question("A ve B", db) == (None, "none")
    assert retrieval.degraded_queries() == ["A", "B"]


# ── degraded QnA guard/activity safety (§13/§59) ────────────────────────────

def _guard(db, qna_id, **overrides):
    values = dict(qna_id=qna_id, guard_ref=f"G-{qna_id}", exact_bypass_enabled=0,
                  selector_mode="semantic_selector_only", content_mode="dynamic",
                  valid_from=None, valid_until=None, on_expiry="block",
                  source_of_truth="AUZEF")
    values.update(overrides)
    db.add(QnARoutingGuard(**values))
    db.commit()


def _policy(db):
    return RoutingGuardPolicy(
        {int(row.qna_id): row for row in db.query(QnARoutingGuard).all()}, today=TODAY
    )


def test_degraded_never_returns_protected_expired_inactive_or_unknown_qna(db, monkeypatch):
    from services import answer_pipeline

    _seed(db, {1: "selector-only", 2: "expired", 4: "safe"})
    _seed(db, {3: "inactive"}, status=0)
    _guard(db, 1)
    _guard(db, 2, valid_until=date(2026, 1, 1))
    hits = [_hit(1, "q", "selector-only", 1.0), _hit(2, "q", "expired", 0.99),
            _hit(3, "q", "inactive", 0.98), _hit(99, "q", "deleted", 0.97),
            _hit(4, "q", "safe", 0.96)]
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", lambda _q, limit: hits[:limit])
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search", lambda _q, limit=3: [])
    trace = DecisionTrace(endpoint="test")
    result = answer_pipeline.answer_in_degraded_mode(
        "soru", db, routing_policy=_policy(db), calendar_gate="closed", trace=trace
    )
    # Meili fetches 3 hits: all three are ineligible → no Meili answer.
    assert result.answer is None
    assert trace.to_dict()["degraded"][0]["exclusion_reasons"] == {
        "guard_fallback_blocked": 2, "inactive_or_missing": 1,
    }
    monkeypatch.setattr(answer_pipeline, "meili_search_safe",
                        lambda _q, limit: [hits[0], hits[4]])
    assert answer_pipeline.answer_in_degraded_mode(
        "soru", db, routing_policy=_policy(db), calendar_gate="closed"
    ).answer == "safe"


def test_degraded_guard_evaluation_error_fails_closed(db, monkeypatch):
    from services import answer_pipeline

    _seed(db, {1: "a1"})

    class BrokenPolicy:
        def decision(self, _qna_id):
            raise RuntimeError("corrupt guard")

    monkeypatch.setattr(answer_pipeline, "meili_search_safe",
                        lambda _q, limit: [_hit(1, "q", "a1", 1.0)])
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search",
                        lambda _q, limit=3: [_hit(1, "q", "a1", 1.0, "qdrant")])
    trace = DecisionTrace(endpoint="test")
    result = answer_pipeline.answer_in_degraded_mode(
        "soru", db, routing_policy=BrokenPolicy(), calendar_gate="closed", trace=trace
    )
    assert result.answer is None
    assert trace.to_dict()["degraded"][0]["exclusion_reasons"] == {
        "guard_evaluation_error": 2
    }


def test_degraded_thresholds_are_unchanged(db, monkeypatch):
    from services import answer_pipeline

    assert answer_pipeline.MEILI_THERESHOLD == 0.90
    assert answer_pipeline.QDRANT_THERESHOLD == 0.75
    _seed(db, {1: "meili", 2: "qdrant"})
    monkeypatch.setattr(answer_pipeline, "meili_search_safe",
                        lambda _q, limit: [_hit(1, "q", "meili", 0.8999)])
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search",
                        lambda _q, limit=3: [_hit(2, "q", "qdrant", 0.75, "qdrant")])
    assert answer_pipeline.answer_in_degraded_mode(
        "soru", db, calendar_gate="closed"
    ).answer is None  # Meili < 0.90 and Qdrant must be strictly > 0.75


# ── degraded Calendar keeps Phase 3 safety (§14/§41/§60) ────────────────────

def _calendar_setup(db):
    db.add_all([
        SystemConfig(key=CURRENT_YEAR_CONFIG_KEY, value="2026-2027"),
        SystemConfig(key=CURRENT_TERM_CONFIG_KEY, value="BAHAR"),
        AcademicCalendar(period="Bahar", event="Bütünleme Sınavları", start_date="01.06.2027",
                         end_date="05.06.2027", academic_year="2026-2027", term="BAHAR",
                         aliases=serialize_aliases(["büt"])),
        AcademicCalendar(period="Güz", event="Bütünleme Sınavları", start_date="01.02.2027",
                         end_date="05.02.2027", academic_year="2026-2027", term="GUZ",
                         aliases=serialize_aliases(["büt"])),
    ])
    db.commit()


def test_llm_off_calendar_degraded_keeps_phase3_safety(db, monkeypatch):
    from services import answer_pipeline

    _calendar_setup(db)
    meili_queries = []
    monkeypatch.setattr(answer_pipeline, "meili_search_safe",
                        lambda q, limit: meili_queries.append(q) or [])
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search", lambda _q, limit=3: [])

    def degraded(query):
        return answer_pipeline.answer_in_degraded_mode(query, db, calendar_gate="date_query")

    guz = degraded("Güz bütünleme ne zaman?")
    assert guz.source == "academic_calendar" and "01.02.2027" in guz.answer
    assert guz.calendar_id is not None
    assert "01.06.2027" in degraded("Bütünleme ne zaman?").answer  # current term BAHAR
    # Historical year: no current-year event; falls through to Meili.
    assert degraded("2025-2026 bütünleme ne zamandı?").answer is None
    # Unrelated date question: no random event; falls through to Meili.
    assert degraded("Öğrenci kulübü toplantısı ne zaman?").answer is None
    assert meili_queries == [
        "2025-2026 bütünleme ne zamandı?", "Öğrenci kulübü toplantısı ne zaman?",
    ]


def test_selector_failure_uses_intent_calendar_relevance_for_degraded_calendar(
    db, monkeypatch
):
    from services import answer_pipeline

    _calendar_setup(db)
    _seed(db, {1: "a1"})
    provider = ScriptedProvider([_intent("Bahar büt", calendar=True)], [TimeoutError("x")])
    retrieval = Retrieval(qdrant={"Bahar büt": [_hit(1, "q1", "a1", source="qdrant")]})
    pipeline = _wire(monkeypatch, provider, retrieval)
    # Use the real Calendar V2 retriever for this test.
    from services.calendar_retrieval import retrieve_calendar_candidates
    monkeypatch.setattr(pipeline, "retrieve_calendar_candidates", retrieve_calendar_candidates)
    answer, source = answer_pipeline.answer_question("Bahar büt", db)
    # "Bahar büt" has no date wording: only calendar_relevant opens the gate.
    assert source == "academic_calendar" and "01.06.2027" in answer


def test_calendar_retrieval_error_stays_inside_intent_and_keeps_normal_llm(
    db, monkeypatch
):
    """A Calendar V2 exception uses failed_calendar_result(); it must not
    escape into a raw-turn REQUEST_DEGRADED answer (Phase 3 behavior)."""
    _seed(db, {1: "a1"})
    provider = ScriptedProvider([_intent("Büt ne zaman?", calendar=True)], [SELECT_1])
    retrieval = Retrieval(qdrant={"Büt ne zaman?": [_hit(1, "q1", "a1", source="qdrant")]})
    pipeline = _wire(monkeypatch, provider, retrieval)

    def broken(*_a, **_k):
        raise RuntimeError("calendar db down")

    monkeypatch.setattr(pipeline, "retrieve_calendar_candidates", broken)
    _forbid_degraded(monkeypatch, pipeline)
    trace = DecisionTrace(endpoint="test")
    assert pipeline.answer_question("Büt ne zaman?", db, trace=trace) == ("a1", "llm")
    snapshot = trace.to_dict()
    assert snapshot["execution"]["execution_mode"] == "NORMAL_LLM"
    assert snapshot["calendar_routes"][0]["calendar_no_match_reason"] == "retrieval_error"
