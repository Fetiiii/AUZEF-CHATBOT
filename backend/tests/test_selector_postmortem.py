"""Phase 7B postmortem — deterministic taxonomy, split and prompt variants.
Offline: no provider call (module-wide network guard)."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from benchmarks.selector_v2 import postmortem as pm
from benchmarks.selector_v2.challenge import build_challenge
from benchmarks.selector_v2.contract import selector_contract_fingerprint
from benchmarks.selector_v2.prompt_variants import VARIANTS, variant_contract_fingerprint
from benchmarks.selector_v2.providers import FakeSelectorProvider, selector_config
from benchmarks.selector_v2.runner import RESULTS_FILE, RunIdentity, load_results, run_benchmark
from benchmarks.selector_v2.safety import no_live_calls
from benchmarks.selector_v2.snapshot import generate_snapshots, load_near_pairs, snapshot_fingerprint
from tests.test_selector_benchmark import _builder, _case, _hit

PRODUCTION_CONTRACT = "3f49b198d621329022be807beada6d75891449b9722ffd9d7e097c6cf403ceed"


@pytest.fixture(autouse=True)
def _no_network():
    db_hosts = [urlparse(os.environ[name]).hostname
                for name in ("DATABASE_URL", "ADMIN_DATABASE_URL", "CHAT_DATABASE_URL")
                if os.environ.get(name)]
    with no_live_calls(allow_hosts=db_hosts):
        yield


def _q(qid, question, answer="genel bilgi"):
    return _hit(qid, question=question, answer=answer)


def _world():
    """Synthetic KB/cases covering every value class and the main rules."""
    hits, cases, script = {}, [], {}

    def add(cid, refs, text, hit_list, reply, tags=()):
        cases.append(_case(cid, refs, text=text, tags=list(tags)))
        hits[text] = hit_list
        script[str(cid)] = reply

    sel = lambda ref: json.dumps({"decision": "SELECT", "candidate_ref": ref})  # noqa: E731
    none = json.dumps({"decision": "NONE"})
    general = _q(129, "Yatay geçiş işlemleri ile ilgili detaylı bilgi")
    specific = _q(342, "Merkezi yatay geçiş başvurusu nasıl yapılır?")
    # 1 corruption: specific chosen, qualifier unstated -> UNSTATED_QUALIFIER_SPECIFICITY
    add(1, ["qna:129"], "yatay geçiş yapmak istiyorum", [general, specific], sel("qna:342"))
    # 2 corruption: user states the distinguishing term -> GOLD_OR_ALIAS_QUESTIONABLE
    add(2, ["qna:129"], "merkezi yatay geçiş", [general, specific], sel("qna:342"))
    # 3 corruption: short message NONE -> FALSE_NONE_UNDERSPECIFIED
    add(3, ["qna:10"], "kitapçık", [_q(10, "Sınav kitapçıklarına nereden ulaşırım?"), _q(11, "x")], none)
    # 4 corruption: well-covered NONE -> FALSE_NONE_OVERSTRICT
    add(4, ["qna:20"], "sınav sonucu itiraz süresi kaç gün sürer",
        [_q(20, "Sınav sonucu itiraz süresi kaç gündür?", "itiraz süresi sonucu"), _q(21, "y")], none)
    # 5 rescue: first wrong, model right
    add(5, ["qna:31"], "mobil uygulama şifre", [_q(30, "Sisteme giriş"), _q(31, "Mobil uygulama şifre")],
        sel("qna:31"))
    # 6 unresolved: first wrong, model follows first
    add(6, ["qna:41"], "pasif ne", [_q(40, "Pasif öğrenci"), _q(41, "Bölüm pasif")], sel("qna:40"))
    # 7 preserve
    add(7, ["qna:50"], "harç ödeme", [_q(50, "Harç ödeme"), _q(51, "z")], sel("qna:50"))
    # 8 corruption, in the human review queue
    add(8, ["qna:60"], "öğrenciyim", [_q(60, "Talep oluşturamıyorum"), _q(61, "Canlı ders")],
        sel("qna:61"))
    snaps = generate_snapshots(cases, build_pool=_builder(hits), pairs=load_near_pairs())
    return snaps, script


def _results(snaps, script, tmp_path):
    fp = snapshot_fingerprint(snaps)
    config = selector_config(provider="fake", model="fake-script")
    identity = RunIdentity(selector_contract_fingerprint(), config, fp, "DRY_RUN_FAKE:script")
    backend = FakeSelectorProvider("oracle", config)
    backend._policy = lambda s, _c: script[s.case.case_id]
    run_benchmark(snaps, backend, identity, tmp_path)
    return load_results(tmp_path / "runs" / identity.run_id / RESULTS_FILE, identity).by_case


@pytest.fixture()
def world(tmp_path):
    snaps, script = _world()
    results = _results(snaps, script, tmp_path)
    groups = build_challenge(snaps, control_size=0)["groups"]
    cases = [{"case_id": s.case.case_id,
              "membership": [g for g, ids in groups.items() if s.case.case_id in ids]}
             for s in snaps]
    alias = pm.build_alias_map([(129, "yatay geçiş yapmak istiyorum"), (30, "mobil uygulama şifre")])
    built = pm.build_postmortem(snaps, cases, results, alias, load_near_pairs(), ["8"])
    return built, snaps, cases


def _primary(built, cid):
    return next(f for f in built["failures"] if f["case_id"] == cid)["classification"]


def test_every_informative_case_is_accounted(world):
    built, _snaps, cases = world
    acc = built["accounted"]
    assert (acc["corruption"], acc["unresolved"], acc["rescue"]) == (5, 1, 1)
    assert acc["false_none"] == 2 and acc["every_failure_has_primary"]
    ids = {c["case_id"] for c in cases}
    classes = {p["case_id"]: p["value_class"] for p in built["packets"].values()}
    assert set(classes) == ids  # nothing silently dropped
    failures = {f["case_id"] for f in built["failures"]}
    assert failures == {cid for cid, v in classes.items() if v in ("CORRUPTION", "UNRESOLVED")}
    assert {r["case_id"] for r in built["rescues"]} == {"5"}
    groups = built["summary"]["mismatch_groups"]
    assert sum(g["count"] for g in groups.values()) == len(failures)


def test_taxonomy_rules_are_evidence_based(world):
    built, _s, _c = world
    assert _primary(built, "1")["primary"] == "UNSTATED_QUALIFIER_SPECIFICITY"
    assert _primary(built, "1")["mismatch_group"] == "A"
    assert _primary(built, "2")["primary"] == "GOLD_OR_ALIAS_QUESTIONABLE"
    assert "intent_states_selected_distinction" in _primary(built, "2")["secondary"]
    assert _primary(built, "3")["primary"] == "FALSE_NONE_UNDERSPECIFIED"
    assert _primary(built, "4")["primary"] == "FALSE_NONE_OVERSTRICT"
    assert _primary(built, "8")["primary"] == "GOLD_OR_ALIAS_QUESTIONABLE"
    assert "review_queue" in _primary(built, "8")["secondary"]
    unresolved = _primary(built, "6")
    assert unresolved["primary"] in pm.CATEGORIES


def test_false_none_rows_and_specificity_table(world):
    built, _s, _c = world
    rows = {r["case_id"]: r for r in built["false_none"]}
    assert set(rows) == {"3", "4"}
    assert rows["4"]["gold_sufficiency"] == "LEXICALLY_SUFFICIENT"
    spec = {r["case_id"]: r["verdict"] for r in built["specificity"]}
    assert spec["1"] == "CHOSE_SPECIFIC_UNSTATED_QUALIFIER_RULE_VIOLATED"
    assert spec["2"] == "CHOSE_SPECIFIC_USER_STATED_DISTINGUISHING_TERM"


def test_alias_structure_counts(world):
    _built, snaps, _c = world
    alias = pm.build_alias_map([(129, "yatay geçiş yapmak istiyorum"), (40, "pasif ne")])
    stats = pm.alias_structure(snaps, alias)
    assert stats["intent_alias_relation"]["alias_of_expected"] == 1
    assert stats["intent_alias_relation"]["alias_of_other_qna"] == 1
    assert stats["exact_alias_bypass_would_be_wrong"] == 1


def _challenge_like(n=137):
    kinds = ["A_first_candidate_wrong", "B_near_qna", "C_general_specific",
             "D_kb_overlap_flagged", "easy_control", "E_multi_acceptable"]
    return [{"case_id": str(i), "membership": [kinds[i % len(kinds)]] + (
        ["B_near_qna"] if i % 7 == 0 else [])} for i in range(1, n + 1)]


def test_split_is_disjoint_complete_stratified_and_stable():
    cases = _challenge_like()
    split = pm.stratified_split(cases)
    dev, hold = set(split["dev"]), set(split["holdout"])
    assert not dev & hold and dev | hold == {c["case_id"] for c in cases}
    assert pm.stratified_split(cases) == split
    assert pm.stratified_split(list(reversed(cases)))["split_fingerprint"] == split["split_fingerprint"]
    for key, n in split["strata"].items():
        members = [c["case_id"] for c in cases
                   if ("|".join(s for s in pm.STRATA if s in c["membership"]) or "other") == key]
        assert sum(1 for m in members if m in hold) == int(n * pm.HOLDOUT_FRACTION + 0.5)
    assert 0.25 <= len(hold) / len(cases) <= 0.35


def test_prompt_variants_are_generic_and_production_prompt_unchanged():
    from services.selector import SELECTOR_SYSTEM_PROMPT

    assert selector_contract_fingerprint() == PRODUCTION_CONTRACT
    near_questions = [q for p in load_near_pairs() for q in p.questions]
    assert len(VARIANTS) <= 2
    for name, text in VARIANTS.items():
        assert text != SELECTOR_SYSTEM_PROMPT
        assert not re.search(r"(qna|calendar):\d", text), name
        assert not re.search(r"\d{2,}", text), name  # no case ids / record ids
        for question in near_questions:
            assert question.casefold() not in text.casefold()
        for phrase in ("yatay geçiş", "merkezi", "çözüm merkezi", "öğrenci affı"):
            assert not re.search(rf"\b{phrase}\b", text.casefold()), (name, phrase)
        assert '{"decision":"SELECT","candidate_ref":' in text and '{"decision":"NONE"}' in text
        assert variant_contract_fingerprint(text) != PRODUCTION_CONTRACT
    assert variant_contract_fingerprint(VARIANTS["variant_a_practical_qualifier"]) != \
        variant_contract_fingerprint(VARIANTS["variant_b_none_threshold_ablation"])
