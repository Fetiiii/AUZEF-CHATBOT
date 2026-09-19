"""Phase 2 orchestration: analyzer first, then exactly one selector per intent."""
from services.decision_trace import DecisionTrace
from services.llm_config import LLMCapability, resolve_llm_config_set
from services.llm_types import (
    IntentAnalysis,
    IntentAnalyzerResult,
    IntentItem,
    LLMOutcomeStatus,
    LLMParseStatus,
    SelectorResult,
)
from services.routing_guards import RoutingGuardPolicy


class EmptyCalendarDB:
    class Query:
        @staticmethod
        def all():
            return []

    def query(self, _model):
        return self.Query()


def _item(text, *, context=False, calendar=False):
    return IntentItem(
        source_text=text,
        normalized_text=text,
        resolved_text=text,
        context_used=context,
        calendar_relevant=calendar,
    )


class CountingProvider:
    def __init__(self, analysis):
        self.configs = resolve_llm_config_set("openai", environ={})
        self.analysis = analysis
        self.analyzer_calls = 0
        self.selector_calls = 0
        self.previous_user_turns = None
        self.selector_questions = []
        self.events = []

    def effective_config(self, capability):
        return self.configs.for_capability(capability)

    def analyze_intents_with_result(self, current, previous_user_turns=()):
        self.analyzer_calls += 1
        self.previous_user_turns = tuple(previous_user_turns)
        self.events.append(("analyzer", current))
        return IntentAnalyzerResult(analysis=self.analysis)

    def ask_with_result(self, question, candidates):
        self.selector_calls += 1
        self.selector_questions.append(question)
        self.events.append(("selector", question))
        candidate = candidates[0]
        return SelectorResult(
            status=LLMOutcomeStatus.SUCCESS,
            parse_status=LLMParseStatus.SUCCESS,
            answer=candidate.answer_text,
            decision="SELECT",
            selected_candidate_ref=candidate.candidate_ref,
            selected_kind=candidate.kind.value,
            selected_qna_id=candidate.qna_id,
        )


def _candidate_builder(captured_queries, same_answer=False):
    def build(query, calendar_entries, routing_policy=None, **_kwargs):
        from services.candidate_eligibility import build_candidate_set

        captured_queries.append(query)
        qna_id = len(captured_queries)
        answer = "aynı cevap" if same_answer else f"cevap-{qna_id}"
        return build_candidate_set(
            calendar_entries=calendar_entries,
            qna_hits=[{
                "qna_id": qna_id, "question": query, "answer": answer,
                "score": 1.0, "source": "qdrant",
            }],
            routing_policy=routing_policy,
            active_qna_lookup=lambda ids: set(ids),
            max_candidates=32,
            qdrant_candidate_count=1,
        )

    return build


def _run(monkeypatch, analysis, *, context=(), trace=None, same_answer=False):
    from services import answer_pipeline

    provider = CountingProvider(analysis)
    retrieval_queries = []
    monkeypatch.setattr(answer_pipeline, "get_llm_provider", lambda _db: provider)
    monkeypatch.setattr(
        answer_pipeline,
        "_build_candidate_pool_result",
        _candidate_builder(retrieval_queries, same_answer=same_answer),
    )
    monkeypatch.setattr(
        answer_pipeline,
        "retrieve_calendar_candidates",
        lambda _query, _db: answer_pipeline.skipped_calendar_result(relevant=True),
    )
    result = answer_pipeline._llm_answer(
        "current turn",
        EmptyCalendarDB(),
        conversation_context=context,
        routing_policy=RoutingGuardPolicy.empty(),
        trace=trace,
    )
    return provider, retrieval_queries, result


def test_single_path_is_one_analyzer_then_one_selector(monkeypatch):
    analysis = IntentAnalysis(intent_count=1, intents=[_item("resolved single")])
    provider, queries, result = _run(monkeypatch, analysis)
    assert provider.analyzer_calls == 1
    assert provider.selector_calls == 1
    assert provider.events == [
        ("analyzer", "current turn"),
        ("selector", "resolved single"),
    ]
    assert queries == ["resolved single"]
    assert provider.selector_questions == ["resolved single"]
    assert result.answer == "cevap-1"


def test_multi_path_is_one_analyzer_and_two_selectors_without_speculation(monkeypatch):
    analysis = IntentAnalysis(
        intent_count=2,
        intents=[_item("resolved A"), _item("resolved B")],
    )
    trace = DecisionTrace(endpoint="test")
    provider, queries, result = _run(monkeypatch, analysis, trace=trace)
    assert provider.analyzer_calls == 1
    assert provider.selector_calls == 2
    assert queries == ["resolved A", "resolved B"]
    assert provider.selector_questions == ["resolved A", "resolved B"]
    assert result.answer == "cevap-1\n\ncevap-2"
    purposes = [item["purpose"] for item in trace.to_dict()["selectors"]]
    assert purposes == ["intent_1", "intent_2"]
    assert "speculative_full_query" not in purposes


def test_existing_deterministic_composition_still_deduplicates_equal_answers(
    monkeypatch,
):
    analysis = IntentAnalysis(
        intent_count=2,
        intents=[_item("resolved A"), _item("resolved B")],
    )
    _provider, _queries, result = _run(
        monkeypatch, analysis, same_answer=True
    )
    assert result.answer == "aynı cevap"
    assert result.answer_count == 1


def test_only_last_two_user_turns_reach_analyzer_and_no_history_reaches_selector(
    monkeypatch,
):
    analysis = IntentAnalysis(intent_count=1, intents=[_item("resolved follow-up")])
    context = (
        {"role": "user", "content": "User A"},
        {"role": "bot", "content": "Bot secret A"},
        {"role": "user", "content": "User B"},
        {"role": "bot", "content": "Bot secret B"},
        {"role": "user", "content": "User C"},
    )
    provider, queries, _result = _run(monkeypatch, analysis, context=context)
    assert provider.previous_user_turns == ("User B", "User C")
    assert provider.selector_questions == ["resolved follow-up"]
    assert queries == ["resolved follow-up"]
    assert all("Bot secret" not in value for value in provider.selector_questions)


def test_trace_uses_v2_intent_analyzer_schema_without_raw_text(monkeypatch):
    analysis = IntentAnalysis(
        intent_count=1,
        intents=[
            IntentItem(
                source_text="sensitive-current",
                normalized_text="sensitive-current",
                resolved_text="sensitive-current",
                context_used=False,
                calendar_relevant=True,
            )
        ],
    )
    trace = DecisionTrace(endpoint="widget_chat", conversation_id=17)
    _provider, _queries, _result = _run(monkeypatch, analysis, trace=trace)
    snapshot = trace.to_dict()
    analyzer = snapshot["intent_analyzer"]
    assert snapshot["schema_version"] == 5
    assert "splitter" not in snapshot
    assert analyzer["provider"] == "openai"
    assert analyzer["requested_model"] == "gpt-4o-mini"
    assert len(analyzer["config_fingerprint"]) == 64
    assert analyzer["previous_user_context_count"] == 0
    assert analyzer["intent_count"] == 1
    assert analyzer["intents"][0]["context_used"] is False
    assert analyzer["intents"][0]["calendar_relevant"] is True
    assert "sensitive-current" not in str(snapshot)


def test_analyzer_capability_config_is_distinct_from_selector_config(monkeypatch):
    analysis = IntentAnalysis(intent_count=1, intents=[_item("resolved")])
    trace = DecisionTrace(endpoint="test")
    provider, _queries, _result = _run(monkeypatch, analysis, trace=trace)
    assert provider.effective_config(LLMCapability.INTENT_ANALYZER).max_tokens == 300
    assert provider.effective_config(LLMCapability.SELECTOR).max_tokens == 32
    analyzer = trace.to_dict()["intent_analyzer"]
    assert analyzer["effective_config"]["capability"] == "intent_analyzer"
