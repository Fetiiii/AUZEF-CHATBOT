"""Pilot-candidate Intent Analyzer prompt contract (freeze amendment 5).

The prompt was tightened so the target pilot model produces output the
UNCHANGED strict parser accepts. These tests pin the three new rules and prove
each synthetic example the prompt teaches is itself accepted (or, for the
forbidden example, rejected) by the real parser. No live LLM call.
"""
from __future__ import annotations

import json

import pytest

from services.intent_analyzer import build_intent_analyzer_prompt, parse_intent_analysis
from services.internal_pilot_freeze import repo_root


def _system() -> str:
    return build_intent_analyzer_prompt("Soru?", ())[0]


def _single(turn, normalized=None, resolved=None, context=False):
    normalized = normalized or turn
    return json.dumps({"intent_count": 1, "intents": [{
        "source_text": turn, "normalized_text": normalized,
        "resolved_text": resolved or normalized, "context_used": context,
        "calendar_relevant": False}]}, ensure_ascii=False)


# ── Rule 1: SINGLE source_text is the whole current turn ───────────────────


def test_prompt_states_single_source_is_the_whole_turn():
    system = _system()
    assert "SOURCE_TEXT KURALI (SINGLE)" in system
    assert "current turn'ün TAMAMIDIR" in system
    assert "yalnız son soru cümlesini almak YASAKTIR" in system


def test_single_example_whole_turn_is_accepted_and_trimmed_is_rejected():
    turn = "İyi günler, ben önlisans öğrencisiyim. Kayıt yenilemem gerekiyor mu?"
    assert turn in _system()
    parse_intent_analysis(_single(turn), current_user_turn=turn, previous_user_turns=())
    trimmed = "Kayıt yenilemem gerekiyor mu?"
    with pytest.raises(ValueError, match="whole current turn"):
        parse_intent_analysis(_single(trimmed), current_user_turn=turn,
                              previous_user_turns=())


# ── Rule 2: MULTI segments are verbatim substrings ──────────────────────────


def test_prompt_states_multi_segments_are_verbatim_and_falls_back_to_single():
    system = _system()
    assert "SOURCE_TEXT KURALI (MULTI)" in system
    assert "BİREBİR alt dizgisidir" in system
    assert "sonuna '?' EKLENMEZ" in system
    assert "MULTI üretme;" in system and "SINGLE üret" in system


def test_multi_example_verbatim_segments_are_accepted():
    turn = "Kütüphane saatleri nedir ve spor salonu nerede?"
    payload = json.dumps({"intent_count": 2, "intents": [
        {"source_text": "Kütüphane saatleri nedir",
         "normalized_text": "Kütüphane saatleri nedir?",
         "resolved_text": "Kütüphane saatleri nedir?",
         "context_used": False, "calendar_relevant": False},
        {"source_text": "spor salonu nerede?",
         "normalized_text": "Spor salonu nerede?",
         "resolved_text": "Spor salonu nerede?",
         "context_used": False, "calendar_relevant": False},
    ]}, ensure_ascii=False)
    analysis = parse_intent_analysis(payload, current_user_turn=turn, previous_user_turns=())
    assert analysis.intent_count == 2


def test_added_question_mark_in_a_multi_source_is_rejected():
    """The exact failure the rule targets; the parser stays strict."""
    turn = "Kütüphane saatleri nedir ve spor salonu nerede?"
    payload = json.dumps({"intent_count": 2, "intents": [
        {"source_text": "Kütüphane saatleri nedir?",
         "normalized_text": "Kütüphane saatleri nedir?",
         "resolved_text": "Kütüphane saatleri nedir?",
         "context_used": False, "calendar_relevant": False},
        {"source_text": "spor salonu nerede?",
         "normalized_text": "Spor salonu nerede?",
         "resolved_text": "Spor salonu nerede?",
         "context_used": False, "calendar_relevant": False},
    ]}, ensure_ascii=False)
    with pytest.raises(ValueError, match="not a current-turn segment"):
        parse_intent_analysis(payload, current_user_turn=turn, previous_user_turns=())


# ── Rule 3: resolved_text words come only from the user turns ───────────────


def test_prompt_states_resolved_text_word_provenance():
    system = _system()
    assert "RESOLVED_TEXT KELİME KAYNAĞI" in system
    assert "HER ZAMAN tercih edilir" in system
    assert "'için'" in system


PREVIOUS = ("Askerlik tecili yaptırmak istiyorum.",)
CURRENT = "Hangi evraklar isteniyor?"


@pytest.mark.parametrize("resolved", [
    "Askerlik tecili yaptırmak istiyorum. Hangi evraklar isteniyor?",
    "Askerlik tecili hangi evraklar isteniyor?",
])
def test_provenance_safe_resolutions_taught_by_the_prompt_are_accepted(resolved):
    assert resolved in _system()
    analysis = parse_intent_analysis(
        _single(CURRENT, resolved=resolved, context=True),
        current_user_turn=CURRENT, previous_user_turns=PREVIOUS)
    assert analysis.intents[0].context_used is True


def test_filler_word_resolution_named_wrong_by_the_prompt_is_rejected():
    resolved = "Askerlik tecili için hangi evraklar isteniyor?"
    assert resolved in _system()
    with pytest.raises(ValueError, match="unsupported semantic expansion"):
        parse_intent_analysis(
            _single(CURRENT, resolved=resolved, context=True),
            current_user_turn=CURRENT, previous_user_turns=PREVIOUS)


# ── Anti-gaming: gate inputs never appear in the prompt ─────────────────────


def test_prompt_never_contains_the_pilot_length_gate_cases():
    cases_path = (repo_root() / "outputs" / "performance-readiness"
                  / "20260927-pilot-candidate-validation" / "length-matrix"
                  / "base_cases.json")
    if not cases_path.exists():  # git-ignored artifact; absent on clean checkouts
        pytest.skip("pilot length-gate case file not present")
    system = _system()
    for case in json.loads(cases_path.read_text(encoding="utf-8")):
        assert case["question"] not in system
        for previous in case["previous"]:
            assert previous not in system


def test_existing_contract_rules_are_still_present():
    system = _system()
    for marker in ("INTENT SAYISI KURALI", "CONTEXT KARAR SIRASI",
                   "SELF-CONTAINED DEĞİLDİR TESTİ", "GRAMER YETERLİ DEĞİLDİR",
                   "AŞIRI TETİKLEME YOK", "en fazla 2 intent"):
        assert marker in system


# ── Iteration 2: long turns keep context detection ──────────────────────────


def test_prompt_states_long_turns_still_need_context_detection():
    system = _system()
    assert "UZUN MESAJDA CONTEXT" in system
    assert "context kararını DEĞİŞTİRMEZ" in system


def test_long_turn_example_resolution_is_accepted_by_the_strict_parser():
    previous = ("Staj yapmak istiyorum.",)
    current = ("Bir şirkette tam zamanlı çalışıyorum, mesaim çok yoğun. "
               "Başvuruyu nereden yapıyorum?")
    resolved = "Staj yapmak istiyorum. " + current
    assert current in _system() and resolved in _system()
    analysis = parse_intent_analysis(
        _single(current, resolved=resolved, context=True),
        current_user_turn=current, previous_user_turns=previous)
    assert analysis.intents[0].context_used is True


# ── Possessive / deictic qualifiers (H-Q1 class) ────────────────────────────


def test_prompt_states_possessive_deictic_qualifier_rule_with_negative_guard():
    system = _system()
    assert "İYELİK VE İŞARET İFADELERİ" in system
    assert "'benim programım'" in system and "'bu bölüm'" in system
    assert "Current turn niteleyiciyi zaten açıkça söylüyorsa" in system
    assert "geçmişten ilgisiz bilgi" in system


def test_possessive_example_resolution_is_accepted_by_the_strict_parser():
    previous = ("Turizm rehberliği önlisans programına kayıtlıyım.",)
    current = "Bu programda yabancı dil dersi zorunlu mu?"
    resolved = previous[0] + " " + current
    assert current in _system() and resolved in _system()
    analysis = parse_intent_analysis(
        _single(current, resolved=resolved, context=True),
        current_user_turn=current, previous_user_turns=previous)
    assert analysis.intents[0].context_used is True
    assert "önlisans" in analysis.intents[0].resolved_text


def test_prompt_does_not_contain_the_used_holdout_cases():
    holdout = (repo_root() / "outputs" / "performance-readiness" / "20260927-selector-v21"
               / "analyzer-holdout" / "holdout-cases.json")
    if not holdout.exists():
        pytest.skip("used analyzer holdout (git-ignored) not present")
    system = _system()
    for case in json.loads(holdout.read_text(encoding="utf-8"))["cases"]:
        assert case["question"] not in system
        for previous in case["previous"]:
            assert previous not in system
