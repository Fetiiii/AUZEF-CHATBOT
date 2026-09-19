"""Phase 7B qualifier-failure postmortem + order-bias prep. Offline: the
module-wide guard refuses any network connection and provider SDK call."""
import hashlib
import json
import os
import random
from pathlib import Path
from urllib.parse import urlparse

import pytest

from benchmarks.selector_v2 import qualifier_postmortem as qp
from benchmarks.selector_v2 import semantic_gold as sg
from benchmarks.selector_v2 import semantic_holdout as sh
from benchmarks.selector_v2.contract import (
    NEUTRAL_ORDER, PRODUCTION_ORDER, order_candidates, selector_contract_fingerprint,
)
from benchmarks.selector_v2.prompt_contract import load_committed_manifest, load_prompt, prompt_manifest
from benchmarks.selector_v2.safety import no_live_calls

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "outputs" / "selector-v2-benchmark"
QP = OUT / "qualifier-postmortem"
GOLD_FP = "36ab1d6f06b990b247f8fee882acd0aba3d4f235ec0ecafc65504c3ffc9ea713"
PRODUCTION_CONTRACT_FP = "3f49b198d621329022be807beada6d75891449b9722ffd9d7e097c6cf403ceed"

pytestmark = pytest.mark.skipif(not (QP / "summary.json").exists(),
                                reason="private benchmark outputs not present")


@pytest.fixture(autouse=True)
def _no_network():
    db_hosts = [urlparse(os.environ[name]).hostname
                for name in ("DATABASE_URL", "ADMIN_DATABASE_URL", "CHAT_DATABASE_URL")
                if os.environ.get(name)]
    with no_live_calls(allow_hosts=db_hosts):
        yield


@pytest.fixture(scope="module")
def snaps():
    from benchmarks.selector_v2.snapshot import load_snapshot

    with no_live_calls():
        _m, snapshots = load_snapshot(OUT / "snapshots" / "3e558768561814da")
    return {s.case.case_id: s for s in snapshots}


def _review():
    rdir = QP / "review"
    records = [json.loads(l) for l in (rdir / "review-cases.jsonl").read_text().splitlines() if l.strip()]
    return rdir, records, (rdir / "review-template.csv").read_text(), (rdir / "review-packet.md").read_text()


def test_blind_packet_contains_all_candidates(snaps):
    _rdir, records, csv_text, md = _review()
    assert [r["case_id"] for r in records] == ["471", "472"]
    for rec in records:
        snap = snaps[rec["case_id"]]
        assert rec["candidate_count"] == len(snap.candidates) == len(rec["candidates"])
        assert sorted(c["question"] for c in rec["candidates"]) == \
            sorted(c.canonical_text for c in snap.candidates)
        assert {c["answer"] for c in rec["candidates"]} == {c.answer_text for c in snap.candidates}
        assert rec["candidate_view_complete"] is True
    assert [r["candidate_count"] for r in records] == [9, 6]
    assert records == qp.blind_records(snaps, ["471", "472"])            # deterministic rebuild


def test_blind_packet_has_no_model_gold_or_retrieval_contamination(snaps):
    rdir, records, csv_text, md = _review()
    refs = sorted({c.candidate_ref for cid in ("471", "472") for c in snaps[cid].candidates})
    assert qp.review_violations(records, csv_text, md, forbidden_refs=refs) == []
    allowed = {"case_id", "intent_text", "candidates", "candidate_view_complete", "candidate_count"}
    for rec in records:
        assert set(rec) <= allowed
        for cand in rec["candidates"]:
            assert set(cand) == {"label", "question", "answer"}     # no ref/order/score/source
        # label order is the neutral hash order, not retrieval order
        by_q = {c.canonical_text: c for c in snaps[rec["case_id"]].candidates}
        shown = [by_q[c["question"]].candidate_ref for c in rec["candidates"]]
        assert shown == sorted(shown, key=lambda r: qp.adj._hash(rec["case_id"], r))
        assert shown != [c.candidate_ref for c in sorted(snaps[rec["case_id"]].candidates,
                                                         key=lambda c: c.order)]
    manifest = (rdir / "manifest.json").read_text()
    for text in ("qna:", "gold", "expected", "production", "variant", "rank", "score", "holdout",
                 "WRONG_SELECT", "CORRECT"):
        assert text not in manifest.replace("GOLD", "")
        assert text.lower() not in csv_text.lower()
    # the review files are the immutable ones recorded in the manifest
    for name, digest in json.loads(manifest)["artifact_sha256"].items():
        assert sg.sha256_text((rdir / name).read_text()) == digest


def test_neutral_order_is_deterministic_and_input_order_independent(snaps):
    snap = snaps["471"]
    cands = snap.selector_candidates()
    first = [c.candidate_ref for c in order_candidates(cands, case_id="471", order=NEUTRAL_ORDER)]
    shuffled = list(cands)
    random.Random(7).shuffle(shuffled)
    again = [c.candidate_ref for c in order_candidates(shuffled, case_id="471", order=NEUTRAL_ORDER)]
    assert first == again and sorted(first) == sorted(c.candidate_ref for c in cands)
    assert [c.candidate_ref for c in order_candidates(cands, case_id="471", order=PRODUCTION_ORDER)] == \
        [c.candidate_ref for c in cands]                    # production order untouched


def test_conditions_differ_only_in_candidate_order(snaps):
    diag = json.loads((QP / "order-experiment" / "diagnostic-set.json").read_text())
    prompt = load_prompt("variant_a_v1")
    assert {"471", "472"} <= set(diag["case_ids"]) and diag["size"] == len(diag["case_ids"])
    for cid in diag["case_ids"]:
        cond = qp.condition_requests(snaps[cid], prompt.text)
        assert cond["only_order_differs"] and cond["order_changed"]
        assert cond["ORIGINAL_ORDER"]["system"] == cond["NEUTRAL_ORDER"]["system"] == prompt.text
    # the equality check really detects a content change
    tampered = snaps["471"].model_copy(deep=True)
    cond = qp.condition_requests(tampered, prompt.text)
    o = json.loads(cond["NEUTRAL_ORDER"]["user"])
    o["candidates"][0]["canonical_text"] += "!"
    assert sorted(map(json.dumps, o["candidates"])) != sorted(
        map(json.dumps, json.loads(cond["ORIGINAL_ORDER"]["user"])["candidates"]))
    plan = json.loads((QP / "order-experiment" / "plan.json").read_text())
    assert plan["calls"] == {"ORIGINAL_ORDER": diag["size"], "NEUTRAL_ORDER": diag["size"],
                             "total": 2 * diag["size"]}
    assert plan["actual_calls_this_phase"] == 0 and plan["live"] is False and plan["approved"] is False


def test_diagnostic_set_stays_in_consumed_cases():
    from benchmarks.selector_v2 import prompt_experiment as px

    split = px.load_split()
    diag = json.loads((QP / "order-experiment" / "diagnostic-set.json").read_text())
    pool = set(json.loads((QP / "final-validation" / "contamination.json").read_text())["remaining_ids"])
    assert set(diag["case_ids"]) <= set(split["dev"]) | set(split["holdout"])
    assert not set(diag["case_ids"]) & pool


def test_qualifier_lexicon_is_a_taxonomy_not_a_case_list():
    assert qp.qualifiers("Merkezi yatay geçiş başvurusu nasıl yapılır?") == {"merkezi"}
    assert qp.qualifiers("Yatay geçiş nasıl yaparım") == set()
    assert qp.qualifiers("AUZEF sınav merkezi tercihi nereden yapılır?") == set()
    assert qp.qualifiers("Çözüm Merkezi'ne giriş yapamıyorum") == set()
    assert qp.qualifiers("İkinci Üniversite sınavsız kayıt") == {"ikinci_universite", "sinavsiz"}
    assert qp.qualifiers("Ön lisans mezunuyum, lisans tamamlama") == {"onlisans", "lisans"}
    assert "471" not in json.dumps(qp.QUALIFIER_LEXICON)


def test_offline_run_uses_saved_outputs_only_and_is_deterministic(tmp_path, monkeypatch):
    """End-to-end: no provider construction, no network, identical artifacts."""
    from benchmarks.selector_v2 import cli, providers

    def refuse(*_a, **_k):
        raise AssertionError("provider constructed in an offline command")

    monkeypatch.setattr(providers.LiveSelectorBackend, "__init__", refuse)
    monkeypatch.setattr(providers.FakeSelectorProvider, "__init__", refuse)
    b = OUT
    cli.main(["qualifier-postmortem", "--snapshot", str(b / "snapshots/3e558768561814da"),
              "--challenge", str(b / "challenge-v1"),
              "--stage-a-run", str(b / "live-stage-a/runs/9dfc72c140dc7e93"),
              "--semantic-gold", str(b / "semantic-gold-v1"), "--expected-gold-fp", GOLD_FP,
              "--dev-run", str(b / "prompt-experiment/runs/f47cf095e441a6e8"),
              "--holdout-run", str(b / "prompt-experiment/semantic-holdout-a/runs/f47cf095e441a6e8"),
              "--holdout-dir", str(b / "prompt-experiment/semantic-holdout-a"),
              "--postmortem", str(b / "postmortem-v1"),
              "--adjudication-dir", str(b / "semantic-adjudication-v1"),
              "--prompt-review-queue", str(b / "prompt-experiment/dev-results/review-queue.json"),
              "--out", str(tmp_path)])
    for rel in ("review/review-cases.jsonl", "review/review-template.csv", "review/review-packet.md",
                "engineering/qualifier-inventory.jsonl", "engineering/order-analysis.jsonl",
                "order-experiment/diagnostic-set.json", "order-experiment/plan.json",
                "final-validation/proposal.json"):
        assert (tmp_path / rel).read_bytes() == (QP / rel).read_bytes(), rel
    assert json.loads((tmp_path / "summary.json").read_text())["live_calls"] == 0


def test_holdout_prompt_gold_and_production_unchanged():
    record = json.loads((QP / "holdout-immutability.json").read_text())
    root = OUT / "prompt-experiment" / "semantic-holdout-a"
    now = {str(p.relative_to(root)): hashlib.new("sha256", p.read_bytes()).hexdigest()
           for p in sorted(root.rglob("*")) if p.is_file()}
    assert now == record["semantic_holdout_a_sha256"]
    assert record["holdout_manifest_outcome"] == "VARIANT_A_HOLDOUT = FAIL"
    sh.verify_baseline(root / sh.BASELINE_FILE)
    assert load_prompt("variant_a_v1").fingerprint == sh.SELECTED_PROMPT_FINGERPRINT
    assert prompt_manifest() == load_committed_manifest()
    manifest, _cases = sh.load_semantic_gold(OUT / "semantic-gold-v1", GOLD_FP)
    assert manifest["semantic_gold_fingerprint"] == GOLD_FP
    assert selector_contract_fingerprint() == PRODUCTION_CONTRACT_FP
