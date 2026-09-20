"""Intent Analyzer output-contract tests and host-.env isolation tests.

Both guard defects that the internal-pilot acceptance run surfaced and that
the mocked suite had been hiding:

1. **Contract mismatch.** The analyzer prompt declared its schema with string
   type labels (``"intent_count": "1 or 2"``). `gpt-4o-mini` mirrored that and
   returned ``"1"`` as a string, which the strict ``Literal[1, 2]`` contract
   rejects — so every live request degraded and the selector was never
   invoked. The parser stays strict; the *prompt* was aligned to it. These
   tests run the real parser over raw provider payloads so a future mock can
   never re-hide the mismatch.

2. **Host ``.env`` leakage.** Adding the pilot settings to the project ``.env``
   changed six unrelated test outcomes. ``conftest`` now disables
   ``load_dotenv`` inside the test process; these tests assert that isolation
   holds in both modes.

No live LLM call is made in this module.
"""
from __future__ import annotations

import json
import os

import pytest
from pydantic import ValidationError

from services.intent_analyzer import build_intent_analyzer_prompt, parse_intent_analysis
from services.llm_types import IntentAnalysis


def _payload(intent_count, count_items: int = 1) -> dict:
    item = {
        "source_text": "Harç ne kadar?",
        "normalized_text": "Harç ne kadar?",
        "resolved_text": "Harç ne kadar?",
        "context_used": False,
        "calendar_relevant": False,
    }
    return {"intent_count": intent_count, "intents": [dict(item) for _ in range(count_items)]}


# ── Schema contract: integers accepted, strings rejected ────────────────────


@pytest.mark.parametrize("count,items", [(1, 1), (2, 2)])
def test_integer_intent_count_is_accepted(count, items):
    model = IntentAnalysis.model_validate(_payload(count, items))
    assert model.intent_count == count
    assert len(model.intents) == items


@pytest.mark.parametrize("count,items", [("1", 1), ("2", 2)])
def test_string_intent_count_is_rejected(count, items):
    """The exact live failure: a quoted number must NOT be coerced."""
    with pytest.raises(ValidationError):
        IntentAnalysis.model_validate(_payload(count, items))


@pytest.mark.parametrize("count", [0, 3, -1, None])
def test_out_of_contract_intent_counts_are_rejected(count):
    with pytest.raises(ValidationError):
        IntentAnalysis.model_validate(_payload(count, 1))


def test_quoted_number_is_rejected_through_the_json_path():
    """The live failure exactly as it arrives: raw JSON from the provider."""
    raw = json.dumps(_payload("1"), ensure_ascii=False)
    with pytest.raises(ValidationError):
        IntentAnalysis.model_validate_json(raw)
    assert IntentAnalysis.model_validate_json(
        json.dumps(_payload(1), ensure_ascii=False)
    ).intent_count == 1


def test_intent_count_must_equal_intents_length():
    with pytest.raises(ValidationError):
        IntentAnalysis.model_validate(_payload(2, 1))


def test_parser_stays_strict_after_the_fix():
    """Strictness is the contract; the prompt was aligned, not the parser."""
    assert IntentAnalysis.model_config.get("strict") is True
    assert IntentAnalysis.model_config.get("extra") == "forbid"


# ── Raw provider payload -> real parser (the path mocks were hiding) ────────

_TURN = "Harç ne kadar?"


def _parse(raw: str, turn: str = _TURN, previous=()):
    return parse_intent_analysis(
        raw, current_user_turn=turn, previous_user_turns=previous
    )


def test_raw_integer_response_parses_successfully():
    analysis = _parse(json.dumps(_payload(1), ensure_ascii=False))
    assert analysis.intent_count == 1
    assert len(analysis.intents) == 1


def test_raw_string_intent_count_response_is_rejected():
    """Reproduces the acceptance failure end to end through the real parser.

    The provider returned exactly this shape on 4/4 live probes, and it is
    what degraded 14/14 requests. It must stay a hard rejection.
    """
    raw = json.dumps(_payload("1"), ensure_ascii=False)
    with pytest.raises(ValueError, match="invalid analyzer schema"):
        _parse(raw)


def test_raw_multi_integer_response_parses_successfully():
    multi = {
        "intent_count": 2,
        "intents": [
            {"source_text": "Harç ne kadar", "normalized_text": "Harç ne kadar",
             "resolved_text": "Harç ne kadar", "context_used": False,
             "calendar_relevant": False},
            {"source_text": "nasıl öderim", "normalized_text": "nasıl öderim",
             "resolved_text": "nasıl öderim", "context_used": False,
             "calendar_relevant": False},
        ],
    }
    analysis = _parse(json.dumps(multi, ensure_ascii=False),
                      turn="Harç ne kadar nasıl öderim")
    assert analysis.intent_count == 2
    assert len(analysis.intents) == 2


# ── Prompt/contract alignment ───────────────────────────────────────────────


def _user_payload(turn: str = _TURN, previous=()) -> dict:
    _system, user = build_intent_analyzer_prompt(turn, previous)
    return json.loads(user)


def test_prompt_declares_intent_count_as_a_json_integer():
    schema = _user_payload()["output_schema"]
    assert isinstance(schema["intent_count"], int)
    assert not isinstance(schema["intent_count"], str)
    assert schema["intent_count"] in (1, 2)


def test_prompt_has_no_quoted_intent_count_type_label():
    """The precise regression: the label the model mirrored back as a string."""
    _system, user = build_intent_analyzer_prompt(_TURN, ())
    assert '"1 or 2"' not in user
    assert '"intent_count":"' not in user.replace(" ", "")


def test_prompt_declares_booleans_as_real_booleans():
    item = _user_payload()["output_schema"]["intents"][0]
    assert isinstance(item["context_used"], bool)
    assert isinstance(item["calendar_relevant"], bool)


def test_prompt_example_satisfies_the_runtime_contract():
    """The example we show the model must itself validate. Closes the loop."""
    schema = _user_payload()["output_schema"]
    example = {
        "intent_count": schema["intent_count"],
        "intents": [
            {
                "source_text": "x",
                "normalized_text": "x",
                "resolved_text": "x",
                "context_used": schema["intents"][0]["context_used"],
                "calendar_relevant": schema["intents"][0]["calendar_relevant"],
            }
        ],
    }
    assert IntentAnalysis.model_validate(example).intent_count == 1


def test_system_prompt_demands_an_integer_explicitly():
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    assert "JSON integer" in system
    assert "intent_count" in system


def test_analyzer_policy_is_unchanged():
    """Only the output contract was fixed; behaviour policy must not drift."""
    from services.intent_analyzer import MAX_PREVIOUS_USER_TURNS

    assert MAX_PREVIOUS_USER_TURNS == 2
    payload = _user_payload(previous=("a", "b", "c", "d"))
    assert len(payload["previous_user_turns"]) == 2
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    assert "Belirsizlikte SINGLE üret" in system
    assert "en fazla 2 intent" in system


# ── Host .env isolation (Mode A / Mode B) ───────────────────────────────────


def test_dotenv_is_disabled_inside_the_test_process():
    """Root cause guard: app imports must not pull the project .env in."""
    import dotenv

    assert dotenv.load_dotenv() is False
    assert dotenv.load_dotenv.__name__ == "_no_dotenv_in_tests"


def test_pilot_env_values_do_not_leak_from_the_host_file():
    """Mode A: the real .env carries pilot overrides; tests must not see them.

    Asserted against the repo's actual .env when present, so this test fails
    if isolation regresses on a developer machine that has the pilot config.
    """
    from services.internal_pilot_freeze import repo_root

    env_file = repo_root() / ".env"
    if not env_file.exists():
        pytest.skip("no project .env on this machine")

    declared = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            declared[key.strip()] = value.strip()

    for name in ("SELECTOR_PROMPT_VERSION", "LLM_ENABLED_DEFAULT"):
        if name in declared:
            assert os.environ.get(name) != declared[name] or os.environ.get(name) is None, (
                f"{name} leaked from the project .env into the test process"
            )


def test_selector_prompt_resolves_to_the_default_without_explicit_config():
    """Mode B: with nothing set, tests always see the historical default."""
    from services.selector_prompt_catalog import (
        PRODUCTION_V2,
        configured_prompt_version,
        resolve_selector_prompt,
    )

    assert configured_prompt_version({}) == PRODUCTION_V2
    assert resolve_selector_prompt({}).fingerprint == (
        "2d59cfb65f0aaa08ffcc481d81fed5a89f29803251cdc8a5438da673cb867f3c"
    )


def test_llm_enabled_seed_default_is_deterministic_without_host_env():
    from scripts.init_system import llm_enabled_default

    assert llm_enabled_default({}) == "false"


# ── Freeze amendment 1 ──────────────────────────────────────────────────────

AMENDMENT_PATH_PARTS = ("deploy", "internal-pilot", "answer-pipeline-freeze-amendment-1.json")
PARENT_FP = "c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6"
SELECTOR_FP = "1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e"


def _amendment_file():
    from services.internal_pilot_freeze import repo_root

    return repo_root().joinpath(*AMENDMENT_PATH_PARTS)


def _amendment():
    return json.loads(_amendment_file().read_text(encoding="utf-8"))


def test_parent_freeze_is_immutable_and_unchanged():
    """The original freeze is history; the amendment must not rewrite it."""
    from services.internal_pilot_freeze import build_freeze_manifest

    manifest = build_freeze_manifest(
        git_commit="22a63e87af81960a5f376cb26165149d6d69b70c",
        created_at="2026-09-20T08:21:12Z",
    )
    assert manifest["freeze_fingerprint"] == PARENT_FP
    assert _amendment()["parent_freeze_fingerprint"] == PARENT_FP
    assert _amendment()["parent_is_immutable"] is True


def test_amendment_fingerprint_is_deterministic_and_matches_the_file():
    from services.internal_pilot_freeze import amendment_fingerprint, build_freeze_amendment

    stored = _amendment()
    assert amendment_fingerprint(stored) == stored["amendment_fingerprint"]

    first = build_freeze_amendment(git_commit="a" * 40, created_at="2026-01-01T00:00:00Z")
    second = build_freeze_amendment(git_commit="b" * 40, created_at="2027-12-31T23:59:59Z")
    assert first["amendment_fingerprint"] == second["amendment_fingerprint"]
    assert first["amendment_fingerprint"] != PARENT_FP


def test_amendment_is_classified_as_a_bug_fix_only():
    classification = _amendment()["classification"]
    assert classification["bug_fix"] is True
    assert classification["research_change"] is False
    assert classification["selector_change"] is False
    assert classification["model_change"] is False
    assert classification["policy_change"] is False


def test_amendment_records_the_acceptance_rationale():
    rationale = _amendment()["rationale"]
    assert "intent_count" in rationale
    assert "integer" in rationale


def test_amendment_tracks_the_live_analyzer_prompt():
    from services.internal_pilot_freeze import intent_analyzer_prompt_fingerprint

    assert _amendment()["changed"]["intent_analyzer_prompt_fingerprint"] == (
        intent_analyzer_prompt_fingerprint()
    )


# ── Selector must be untouched by this fix ──────────────────────────────────


def test_selector_variant_a_bytes_are_unchanged():
    from services.selector_prompt_catalog import VARIANT_A_V1, load_selector_prompt

    assert load_selector_prompt(VARIANT_A_V1).fingerprint == SELECTOR_FP


def test_selector_identity_is_unchanged_by_the_amendment():
    unchanged = _amendment()["unchanged"]
    assert unchanged["selector_prompt_fingerprint"] == SELECTOR_FP
    assert unchanged["selector_prompt_version"] == "variant_a_v1"
    assert unchanged["selector_provider"] == "openrouter"
    assert unchanged["selector_model"] == "openai/gpt-4o-mini"
    assert unchanged["selector_max_tokens"] == 32
    assert unchanged["selector_temperature"] == 0.0
    assert unchanged["candidate_order"] == "production"
    assert unchanged["selector_config_fingerprint"] == (
        "af9eb2d0767d37cd632799cbae39e7938585b243ceb4c7a1527b8028cd489a6e"
    )


def test_selector_contract_fingerprint_is_unchanged():
    from services.internal_pilot_freeze import SERIALIZER_CONTRACT_FINGERPRINT

    assert SERIALIZER_CONTRACT_FINGERPRINT == (
        "d50fbee416ca98783e499454fe840c1fdc2ff41b5fb7445cbac20f6a72c7f5a9"
    )
    assert _amendment()["unchanged"]["selector_contract_fingerprint"] == (
        SERIALIZER_CONTRACT_FINGERPRINT
    )
