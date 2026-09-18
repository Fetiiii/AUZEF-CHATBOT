"""V2 Intent Analyzer contract, context, classification, and fail-safe tests."""
import json

import pytest

from services.llm_config import resolve_llm_config_set
from services.llm_provider import BaseLLMProvider
from services.llm_types import LLMOutcomeStatus, LLMParseStatus


class ScriptedAnalyzerProvider(BaseLLMProvider):
    provider_name = "openai"

    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.model = "gpt-4o-mini"
        self._configs = resolve_llm_config_set("openai", environ={})
        self.prompts = []

    def _complete(self, system, user, max_tokens=5):
        self.prompts.append((system, user, max_tokens))
        if self.error is not None:
            raise self.error
        return self.output


def _analysis(current, intents):
    return json.dumps(
        {"intent_count": len(intents), "intents": intents}, ensure_ascii=False
    )


def _item(source, *, normalized=None, resolved=None, context=False, calendar=False):
    normalized = source if normalized is None else normalized
    return {
        "source_text": source,
        "normalized_text": normalized,
        "resolved_text": normalized if resolved is None else resolved,
        "context_used": context,
        "calendar_relevant": calendar,
    }


@pytest.mark.parametrize(
    "current",
    [
        "Harcı yatırdım ama sisteme giremiyorum.",
        "Kayıt yenileme nasıl yapılır ve hangi adımları izlemeliyim?",
    ],
)
def test_single_is_valid_for_one_goal_even_with_multiple_clauses(current):
    provider = ScriptedAnalyzerProvider(_analysis(current, [_item(current)]))
    result = provider.analyze_intents_with_result(current)
    assert result.status is LLMOutcomeStatus.SUCCESS
    assert result.analysis.intent_count == 1
    assert result.analysis.intents[0].resolved_text == current


def test_multi_requires_two_independent_current_turn_segments():
    current = "AKSİS'e giremiyorum, bir de ders materyallerine nereden ulaşırım?"
    output = _analysis(
        current,
        [
            _item("AKSİS'e giremiyorum"),
            _item("bir de ders materyallerine nereden ulaşırım"),
        ],
    )
    result = ScriptedAnalyzerProvider(output).analyze_intents_with_result(current)
    assert result.status is LLMOutcomeStatus.SUCCESS
    assert result.analysis.intent_count == 2


def test_minimal_spelling_and_grammar_normalization_is_allowed():
    current = "pasoyu nasıl alcam"
    normalized = "Pasoyu nasıl alacağım?"
    output = _analysis(
        current,
        [_item(current, normalized=normalized, resolved=normalized)],
    )
    result = ScriptedAnalyzerProvider(output).analyze_intents_with_result(current)
    assert result.status is LLMOutcomeStatus.SUCCESS
    assert result.analysis.intents[0].normalized_text == normalized


def test_multi_normalization_can_remove_connector_without_adding_meaning():
    current = "sisteme giremiyorum, bir de ders materyallerine nerden ulaşırım"
    output = _analysis(
        current,
        [
            _item(
                "sisteme giremiyorum",
                normalized="Sisteme giremiyorum.",
                resolved="Sisteme giremiyorum.",
            ),
            _item(
                "bir de ders materyallerine nerden ulaşırım",
                normalized="Ders materyallerine nereden ulaşabilirim?",
                resolved="Ders materyallerine nereden ulaşabilirim?",
            ),
        ],
    )
    result = ScriptedAnalyzerProvider(output).analyze_intents_with_result(current)
    assert result.status is LLMOutcomeStatus.SUCCESS
    assert result.analysis.intent_count == 2


def test_genuine_followup_can_resolve_from_previous_user_turn():
    previous = ["Harç ödemesini nasıl yapabilirim?"]
    current = "Peki son günü ne zaman?"
    output = _analysis(
        current,
        [
            _item(
                current,
                resolved="Harç ödemesinin son günü ne zaman?",
                context=True,
                calendar=True,
            )
        ],
    )
    result = ScriptedAnalyzerProvider(output).analyze_intents_with_result(
        current, previous
    )
    intent = result.analysis.intents[0]
    assert result.status is LLMOutcomeStatus.SUCCESS
    assert intent.resolved_text == "Harç ödemesinin son günü ne zaman?"
    assert intent.context_used is True
    assert intent.calendar_relevant is True


def test_clear_topic_switch_does_not_carry_previous_subject():
    previous = ["Harç ödemesi nasıl yapılır?"]
    current = "İstanbulkart nasıl alabilirim?"
    output = _analysis(current, [_item(current)])
    result = ScriptedAnalyzerProvider(output).analyze_intents_with_result(
        current, previous
    )
    intent = result.analysis.intents[0]
    assert intent.context_used is False
    assert "harç" not in intent.resolved_text.casefold()


@pytest.mark.parametrize(
    ("current", "calendar_relevant"),
    [
        ("Kayıt yenileme ne zaman?", True),
        ("Bütünleme sınavı hangi tarihte?", True),
        ("Bahar dönemi ne zaman başlıyor?", True),
        ("Kayıt yenileme nasıl yapılır?", False),
        ("Sınav giriş belgesini nereden alırım?", False),
        ("Ders materyallerine nasıl ulaşırım?", False),
    ],
)
def test_calendar_relevance_is_a_typed_semantic_classification(
    current, calendar_relevant
):
    output = _analysis(current, [_item(current, calendar=calendar_relevant)])
    intent = ScriptedAnalyzerProvider(output).analyze_intents(current).intents[0]
    assert intent.calendar_relevant is calendar_relevant


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        "",
        "```json\n{}\n```",
        json.dumps({"intent_count": 1, "intents": []}),
        _analysis("current", [_item("   ")]),
        _analysis(
            "current",
            [_item("current", normalized="x" * 2001, resolved="x" * 2001)],
        ),
        json.dumps(
            {
                "intent_count": 1,
                "intents": [
                    {
                        **_item("current"),
                        "context_used": "false",
                    }
                ],
            }
        ),
        json.dumps(
            {
                "intent_count": 3,
                "intents": [_item("a"), _item("b"), _item("c")],
            }
        ),
        json.dumps(
            {
                "intent_count": 1,
                "intents": [_item("current")],
                "unexpected": True,
            }
        ),
    ],
)
def test_malformed_empty_or_three_plus_output_falls_back_to_raw_single(output):
    current = "Bir? İki? Üç?"
    result = ScriptedAnalyzerProvider(output).analyze_intents_with_result(current)
    intent = result.analysis.intents[0]
    assert result.status is LLMOutcomeStatus.INVALID_OUTPUT
    assert result.parse_status is LLMParseStatus.INVALID_OUTPUT
    assert result.fallback_to_single is True
    assert result.analysis.intent_count == 1
    assert intent.source_text == current
    assert intent.normalized_text == current
    assert intent.resolved_text == current
    assert intent.context_used is False
    assert intent.calendar_relevant is False


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (RuntimeError("provider down"), LLMOutcomeStatus.MODEL_ERROR),
        (TimeoutError("late"), LLMOutcomeStatus.TIMEOUT),
    ],
)
def test_provider_error_and_timeout_keep_cause_but_use_safe_single(
    error, expected_status
):
    current = "Vize ne zaman? Final ne zaman?"
    result = ScriptedAnalyzerProvider(error=error).analyze_intents_with_result(current)
    assert result.status is expected_status
    assert result.parse_status is LLMParseStatus.FALLBACK
    assert result.fallback_to_single is True
    assert result.analysis.intents[0].resolved_text == current


def test_semantic_expansion_is_rejected_and_falls_back_losslessly():
    current = "pasoyu nasıl alcam"
    expanded = (
        "İstanbulkart öğrenci kartı başvurusunu e-Devlet üzerinden nasıl "
        "yapabilirim?"
    )
    output = _analysis(
        current,
        [_item(current, normalized=expanded, resolved=expanded)],
    )
    result = ScriptedAnalyzerProvider(output).analyze_intents_with_result(current)
    assert result.status is LLMOutcomeStatus.INVALID_OUTPUT
    assert result.fallback_to_single is True
    assert result.analysis.intents[0].resolved_text == current


def test_prompt_is_task_focused_strict_and_uses_only_last_two_user_turns():
    current = "Peki ne zaman?"
    provider = ScriptedAnalyzerProvider(_analysis(current, [_item(current)]))
    provider.analyze_intents_with_result(
        current, ["ilk user", "ikinci user", "üçüncü user"]
    )
    system, user, max_tokens = provider.prompts[0]
    assert "Belirsizlikte SINGLE" in system
    assert "en fazla 2" in system
    assert "semantik bilgi EKLEME" in system
    assert "calendar_relevant" in system
    assert "QnA" in system and "seçmezsin" in system
    assert "ilk user" not in user
    assert "ikinci user" in user and "üçüncü user" in user
    assert max_tokens == 300


def test_no_context_request_keeps_current_turn_standalone():
    current = "Ders materyallerine nasıl ulaşırım?"
    output = _analysis(current, [_item(current)])
    result = ScriptedAnalyzerProvider(output).analyze_intents_with_result(current, ())
    assert result.analysis.intents[0].context_used is False
