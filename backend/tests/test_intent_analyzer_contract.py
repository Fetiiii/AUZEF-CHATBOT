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


def test_amendment_1_keeps_its_historical_prompt_fingerprint():
    """Amendment 1 is history: it records the prompt as it was at that time.

    Amendment 2 changed the analyzer prompt again, so amendment 1 must NOT
    track the live value — that is exactly what makes it a record.
    """
    from services.internal_pilot_freeze import intent_analyzer_prompt_fingerprint

    recorded = _amendment()["changed"]["intent_analyzer_prompt_fingerprint"]
    assert recorded == (
        "b5f3f8b3da978042f9e6cee13c0f204a1d502100849d3127cfaf99c325a2d25f"
    )
    assert recorded != intent_analyzer_prompt_fingerprint()


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


# ── Context decision rules (re-acceptance blocker: context_used never true) ──


def test_prompt_states_the_context_decision_order():
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    assert "CONTEXT KARAR SIRASI" in system
    for marker in ("1)", "2)", "3)", "4)"):
        assert marker in system


def test_verbatim_equality_is_scoped_to_context_used_false():
    """The re-acceptance root cause: an unscoped verbatim rule killed context.

    Verbatim equality must be stated as mandatory ONLY when context_used is
    false, and explicitly NOT mandatory when context is genuinely needed.
    """
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    assert "YALNIZ context_used = false iken zorunludur" in system
    assert "zorunlu DEĞİLDİR" in system


def test_prompt_allows_resolved_text_to_differ_when_context_is_used():
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    assert "context_used = true" in system
    assert "self-contained" in system


def test_prompt_warns_against_using_context_merely_because_it_exists():
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    assert "AŞIRI TETİKLEME YOK" in system
    assert "eski konuyu taşıma" in system


def test_self_contained_turn_requires_verbatim_resolved_text():
    turn = "Harç ne kadar?"
    ok = {"intent_count": 1, "intents": [{
        "source_text": turn, "normalized_text": turn, "resolved_text": turn,
        "context_used": False, "calendar_relevant": False}]}
    assert _parse(json.dumps(ok, ensure_ascii=False)).intents[0].context_used is False

    drifted = json.loads(json.dumps(ok))
    drifted["intents"][0]["resolved_text"] = "Harç ücreti ne kadar?"
    with pytest.raises(ValueError, match="resolved intent changed without context"):
        _parse(json.dumps(drifted, ensure_ascii=False))


def test_context_resolved_intent_is_accepted():
    """Reference resolved from a previous USER turn must validate."""
    previous = ("Kütüphane hakkında bilgi almak istiyorum",)
    current = "Saatleri nedir?"
    payload = {"intent_count": 1, "intents": [{
        "source_text": current, "normalized_text": current,
        "resolved_text": "Kütüphane saatleri nedir?",
        "context_used": True, "calendar_relevant": False}]}
    analysis = _parse(json.dumps(payload, ensure_ascii=False), turn=current,
                      previous=previous)
    assert analysis.intents[0].context_used is True
    assert analysis.intents[0].resolved_text != analysis.intents[0].normalized_text


def test_context_used_true_without_any_resolution_is_rejected():
    turn = "Saatleri nedir?"
    payload = {"intent_count": 1, "intents": [{
        "source_text": turn, "normalized_text": turn, "resolved_text": turn,
        "context_used": True, "calendar_relevant": False}]}
    with pytest.raises(ValueError, match="context_used does not reflect"):
        _parse(json.dumps(payload, ensure_ascii=False), turn=turn,
               previous=("Kütüphane hakkında bilgi almak istiyorum",))


def test_context_used_true_with_unsupported_expansion_is_rejected():
    previous = ("Kütüphane hakkında bilgi almak istiyorum",)
    current = "Saatleri nedir?"
    payload = {"intent_count": 1, "intents": [{
        "source_text": current, "normalized_text": current,
        "resolved_text": "Merkez kampüs kütüphane saatleri nedir?",
        "context_used": True, "calendar_relevant": False}]}
    with pytest.raises(ValueError):
        _parse(json.dumps(payload, ensure_ascii=False), turn=current, previous=previous)


def test_only_the_last_two_previous_user_turns_are_supplied():
    payload = _user_payload(previous=("bir", "iki", "üç", "dört"))
    assert payload["previous_user_turns"] == ["üç", "dört"]


def test_bot_messages_are_never_part_of_the_analyzer_input():
    """Only USER turns reach the analyzer; the router filters bot messages."""
    import inspect

    from routers import chat

    source = inspect.getsource(chat._load_recent_context)
    assert 'ConversationMessage.role == "user"' in source

    from services.answer_pipeline import _previous_user_turns

    mixed = ({"role": "user", "content": "soru bir"},
             {"role": "bot", "content": "bot cevabı"},
             {"role": "user", "content": "soru iki"})
    assert _previous_user_turns(mixed) == ("soru bir", "soru iki")


# ── MULTI decision rules (re-acceptance blocker: MULTI never produced) ───────


def test_prompt_decouples_intent_count_from_text_change():
    """§10: goal count decides MULTI, not whether normalized/resolved changed."""
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    assert "INTENT SAYISI KURALI" in system
    assert "HİÇBİR İLGİSİ YOKTUR" in system
    assert "metin hiç değişmese de intent_count 2 olabilir" in system


def test_prompt_lists_the_single_boundaries():
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    for marker in ("aynı hedefin iki kez", "niteleyici", "belirsizse",
                   "Belirsizlikte SINGLE üret", "en fazla 2 intent"):
        assert marker in system


def test_prompt_examples_are_synthetic_not_benchmark_cases():
    """§12: no benchmark text may leak into the prompt."""
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    catalog = json.loads(
        (__import__("services.internal_pilot_freeze", fromlist=["repo_root"]).repo_root()
         / "tests/e2e/internal_pilot/scenarios.json").read_text(encoding="utf-8"))
    for scenario in catalog["scenarios"]:
        for message in scenario["turns"]:
            assert message not in system
    for forbidden in ("471", "472", "IP-E2E"):
        assert forbidden not in system


def test_multi_payload_with_two_independent_goals_is_accepted():
    turn = "Kütüphane saatleri nedir ve spor salonu nerede?"
    payload = {"intent_count": 2, "intents": [
        {"source_text": "Kütüphane saatleri nedir",
         "normalized_text": "Kütüphane saatleri nedir",
         "resolved_text": "Kütüphane saatleri nedir",
         "context_used": False, "calendar_relevant": False},
        {"source_text": "spor salonu nerede",
         "normalized_text": "spor salonu nerede",
         "resolved_text": "spor salonu nerede",
         "context_used": False, "calendar_relevant": False}]}
    analysis = _parse(json.dumps(payload, ensure_ascii=False), turn=turn)
    assert analysis.intent_count == 2


def test_multi_segments_must_come_from_the_current_turn():
    turn = "Kütüphane saatleri nedir ve spor salonu nerede?"
    payload = {"intent_count": 2, "intents": [
        {"source_text": "Kütüphane saatleri nedir",
         "normalized_text": "Kütüphane saatleri nedir",
         "resolved_text": "Kütüphane saatleri nedir",
         "context_used": False, "calendar_relevant": False},
        {"source_text": "yemekhane nerede", "normalized_text": "yemekhane nerede",
         "resolved_text": "yemekhane nerede",
         "context_used": False, "calendar_relevant": False}]}
    with pytest.raises(ValueError, match="multi intent source"):
        _parse(json.dumps(payload, ensure_ascii=False), turn=turn)


def test_more_than_two_intents_is_rejected_by_the_contract():
    item = {"source_text": "x", "normalized_text": "x", "resolved_text": "x",
            "context_used": False, "calendar_relevant": False}
    with pytest.raises(ValidationError):
        IntentAnalysis.model_validate({"intent_count": 3, "intents": [dict(item)] * 3})


def test_analyzer_policy_survives_the_semantic_fix():
    """Frozen policy must be intact after the clarification."""
    from services.intent_analyzer import MAX_PREVIOUS_USER_TURNS

    assert MAX_PREVIOUS_USER_TURNS == 2
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    assert "Belirsizlikte SINGLE üret" in system
    assert "en fazla 2 intent" in system
    assert "calendar_relevant" in system
    assert "salt kayıt/sınav/dönem kelimesi yetmez" in system


# ── Freeze amendment 2 ──────────────────────────────────────────────────────

AMENDMENT_2_PARTS = ("deploy", "internal-pilot", "answer-pipeline-freeze-amendment-2.json")
AMENDMENT_1_FP = "9d221c3cf94ffd5039670e74b6272b078106f793c06f98fe649557ca21ec8485"


def _amendment2():
    from services.internal_pilot_freeze import repo_root

    return json.loads(repo_root().joinpath(*AMENDMENT_2_PARTS).read_text(encoding="utf-8"))


def test_amendment_2_chains_to_immutable_parents():
    a2 = _amendment2()
    assert a2["parent_amendment_fingerprint"] == AMENDMENT_1_FP
    assert a2["historical_parent_freeze_fingerprint"] == PARENT_FP
    assert a2["parents_are_immutable"] is True
    assert _amendment()["amendment_fingerprint"] == AMENDMENT_1_FP


def test_amendment_2_fingerprint_is_deterministic_and_matches_the_file():
    from services.internal_pilot_freeze import amendment_2_fingerprint, build_freeze_amendment_2

    stored = _amendment2()
    assert amendment_2_fingerprint(stored) == stored["amendment_fingerprint"]
    first = build_freeze_amendment_2(git_commit="a" * 40, created_at="2026-01-01T00:00:00Z")
    second = build_freeze_amendment_2(git_commit="b" * 40, created_at="2027-12-31T23:59:59Z",
                                      live_screen={"probes": 14})
    assert first["amendment_fingerprint"] == second["amendment_fingerprint"]
    assert first["amendment_fingerprint"] not in (AMENDMENT_1_FP, PARENT_FP)


def test_amendment_2_is_classified_as_an_analyzer_semantic_bug_fix():
    c = _amendment2()["classification"]
    assert c["intent_analyzer_semantic_bug_fix"] is True
    for forbidden in ("selector_change", "model_change", "retrieval_change",
                      "calendar_policy_change", "parser_contract_change",
                      "analyzer_policy_change"):
        assert c[forbidden] is False


def test_amendment_2_keeps_its_historical_prompt_fingerprint():
    """Amendment 2 is history once amendment 3 changes the prompt again."""
    from services.internal_pilot_freeze import intent_analyzer_prompt_fingerprint

    recorded = _amendment2()["changed"]["intent_analyzer_prompt_fingerprint"]
    assert recorded == (
        "112fa46942d8fdff3edf25a6e02ac3ccfbc020b201ac0acbd964ff91a6bdf493"
    )
    assert recorded != intent_analyzer_prompt_fingerprint()
    assert _amendment2()["changed"]["context_assembly_changed"] is False


def test_amendment_2_preserves_selector_and_contract():
    preserved = _amendment2()["preserved"]
    assert preserved["selector_prompt_fingerprint"] == SELECTOR_FP
    assert preserved["parser_strictness"] == "strict=True, extra=forbid, Literal[1, 2]"
    assert preserved["intent_count_must_be_json_integer"] is True
    assert preserved["context_used_false_implies_verbatim_resolved_text"] is True
    assert preserved["candidate_order"] == "production"


# ── Context resolution final fix (failure classes 1 and 2) ──────────────────


def test_prompt_binds_context_flag_to_the_rewrite():
    """Failure class 1: the flag and the rewrite must be one decision."""
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    assert "tek bir karardır" in system
    assert "ayrı ayrı seçilemez" in system
    assert "context_used = true DEME" in system


def test_prompt_states_the_self_contained_test_for_short_questions():
    """Failure class 2: reference-dependent short questions."""
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    assert "SELF-CONTAINED DEĞİLDİR TESTİ" in system
    for referent in ("belge", "tarih", "ücre", "süre"):
        assert referent in system
    assert "peki" in system.lower()


def test_prompt_keeps_the_anti_over_trigger_rule():
    """§7: short does not automatically mean context-dependent."""
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    assert "AŞIRI TETİKLEME YOK" in system
    assert "Kısa olmak tek başına context-dependent olmak" in system


def test_prompt_resolution_criterion_is_semantic_not_mechanical():
    """§5: no meaningless 'must always differ' rule."""
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    assert "Ölçüt mekanik değil semantiktir" in system
    assert "her zaman farklı olmalıdır" not in system


def test_prompt_never_contains_the_frozen_probe_texts():
    """The screen must not be gamed: probe sentences stay out of the prompt."""
    from services.internal_pilot_freeze import repo_root

    system, _user = build_intent_analyzer_prompt(_TURN, ())
    plan = json.loads(
        (repo_root() / "outputs/internal-pilot-intent-semantic-fix/"
         "prelive-probe-plan.json").read_text(encoding="utf-8"))
    for probe in plan["probes"]:
        assert probe["current"] not in system
        for previous in probe["previous"]:
            assert previous not in system


def test_context_used_true_without_resolution_stays_rejected():
    """Failure class 1, enforced by the unchanged strict parser."""
    turn = "Hangi belgeler gerekiyor?"
    payload = {"intent_count": 1, "intents": [{
        "source_text": turn, "normalized_text": turn, "resolved_text": turn,
        "context_used": True, "calendar_relevant": False}]}
    with pytest.raises(ValueError, match="context_used does not reflect"):
        _parse(json.dumps(payload, ensure_ascii=False), turn=turn,
               previous=("İkinci üniversite kaydı yaptırmak istiyorum",))


@pytest.mark.parametrize("previous,current,resolved", [
    ("Staj başvurusu yapmak istiyorum", "Nereden yapılıyor?",
     "Staj başvurusu nereden yapılıyor?"),
    ("Askerlik erteleme işlemi yapmak istiyorum", "Hangi belgeler gerekiyor?",
     "Askerlik erteleme işlemi hangi belgeler gerekiyor?"),
    ("Bütünleme sınavı hakkında soru soracaktım", "Peki ne zaman?",
     "Peki bütünleme sınavı ne zaman?"),
])
def test_reference_dependent_turn_with_resolution_is_accepted(previous, current, resolved):
    payload = {"intent_count": 1, "intents": [{
        "source_text": current, "normalized_text": current,
        "resolved_text": resolved, "context_used": True,
        "calendar_relevant": False}]}
    analysis = _parse(json.dumps(payload, ensure_ascii=False), turn=current,
                      previous=(previous,))
    assert analysis.intents[0].context_used is True
    assert analysis.intents[0].resolved_text != analysis.intents[0].normalized_text


def test_short_message_without_resolvable_context_must_not_invent_one():
    """No previous turn -> context cannot be claimed at all."""
    turn = "Hangi belgeler gerekiyor?"
    payload = {"intent_count": 1, "intents": [{
        "source_text": turn, "normalized_text": turn,
        "resolved_text": "Yatay geçiş için hangi belgeler gerekiyor?",
        "context_used": True, "calendar_relevant": False}]}
    with pytest.raises(ValueError):
        _parse(json.dumps(payload, ensure_ascii=False), turn=turn, previous=())


def test_self_contained_short_question_keeps_context_false():
    turn = "Harç ne kadar?"
    payload = {"intent_count": 1, "intents": [{
        "source_text": turn, "normalized_text": turn, "resolved_text": turn,
        "context_used": False, "calendar_relevant": False}]}
    analysis = _parse(json.dumps(payload, ensure_ascii=False), turn=turn,
                      previous=("Ders seçimi ne zaman?",))
    assert analysis.intents[0].context_used is False
    assert analysis.intents[0].resolved_text == analysis.intents[0].normalized_text


def test_bot_only_information_cannot_resolve_a_reference():
    """Bot turns never reach the analyzer, so they can never support resolution."""
    from services.answer_pipeline import _previous_user_turns

    mixed = ({"role": "bot", "content": "Yatay geçiş başvuruları AKSİS üzerinden yapılır"},)
    assert _previous_user_turns(mixed) == ()

    current = "Hangi belgeler gerekiyor?"
    payload = {"intent_count": 1, "intents": [{
        "source_text": current, "normalized_text": current,
        "resolved_text": "AKSİS için hangi belgeler gerekiyor?",
        "context_used": True, "calendar_relevant": False}]}
    with pytest.raises(ValueError):
        _parse(json.dumps(payload, ensure_ascii=False), turn=current,
               previous=_previous_user_turns(mixed))


# ── MULTI must survive the context-only change ──────────────────────────────


def test_multi_rules_are_untouched_by_the_context_fix():
    system, _user = build_intent_analyzer_prompt(_TURN, ())
    assert "INTENT SAYISI KURALI" in system
    assert "HİÇBİR İLGİSİ YOKTUR" in system
    assert "metin hiç değişmese de intent_count 2 olabilir" in system
    assert "Belirsizlikte SINGLE üret" in system
    assert "en fazla 2 intent" in system


# ── Freeze amendment 3 ──────────────────────────────────────────────────────

AMENDMENT_3_PARTS = ("deploy", "internal-pilot", "answer-pipeline-freeze-amendment-3.json")
AMENDMENT_2_FP = "2c9f2ea99ae9a44f88ae1623a2bb753abf212c17d5a6bef59ec56f81a99fed50"


def _amendment3():
    from services.internal_pilot_freeze import repo_root

    return json.loads(repo_root().joinpath(*AMENDMENT_3_PARTS).read_text(encoding="utf-8"))


def test_amendment_3_chains_to_immutable_parents():
    a3 = _amendment3()
    assert a3["parent_amendment_fingerprint"] == AMENDMENT_2_FP
    assert a3["amendment_1_fingerprint"] == AMENDMENT_1_FP
    assert a3["historical_parent_freeze_fingerprint"] == PARENT_FP
    assert a3["parents_are_immutable"] is True
    assert _amendment2()["amendment_fingerprint"] == AMENDMENT_2_FP


def test_amendment_3_fingerprint_is_deterministic_and_matches_the_file():
    from services.internal_pilot_freeze import amendment_3_fingerprint, build_freeze_amendment_3

    stored = _amendment3()
    assert amendment_3_fingerprint(stored) == stored["amendment_fingerprint"]
    first = build_freeze_amendment_3(git_commit="a" * 40, created_at="2026-01-01T00:00:00Z")
    second = build_freeze_amendment_3(git_commit="b" * 40, created_at="2027-12-31T23:59:59Z",
                                      live_screen={"probes": 14})
    assert first["amendment_fingerprint"] == second["amendment_fingerprint"]
    assert first["amendment_fingerprint"] not in (AMENDMENT_2_FP, AMENDMENT_1_FP, PARENT_FP)


def test_amendment_3_is_a_context_only_bug_fix():
    c = _amendment3()["classification"]
    assert c["intent_analyzer_context_semantic_bug_fix"] is True
    for forbidden in ("model_change", "selector_change", "retrieval_change",
                      "calendar_change", "parser_contract_change",
                      "context_assembly_change", "multi_rule_change"):
        assert c[forbidden] is False
    assert _amendment3()["changed"]["multi_rules_touched"] is False


def test_amendment_3_tracks_the_live_analyzer_prompt():
    from services.internal_pilot_freeze import intent_analyzer_prompt_fingerprint

    assert _amendment3()["changed"]["intent_analyzer_prompt_fingerprint"] == (
        intent_analyzer_prompt_fingerprint()
    )


def test_amendment_3_preserves_selector_and_contract():
    p = _amendment3()["preserved"]
    assert p["selector_prompt_fingerprint"] == SELECTOR_FP
    assert p["parser_strictness"] == "strict=True, extra=forbid, Literal[1, 2]"
    assert p["context_used_false_implies_verbatim_resolved_text"] is True
    assert p["context_used_true_requires_real_resolution"] is True
    assert p["candidate_order"] == "production"
