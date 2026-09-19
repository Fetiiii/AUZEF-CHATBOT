"""Phase 7B model experiment (variant_c_v1 × openai/gpt-5.6-luna): pre-live
freeze, guards, request shape; post-run integrity once the live run exists."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from benchmarks.selector_v2 import model_experiment as me
from benchmarks.selector_v2 import prompt_experiment as px
from benchmarks.selector_v2 import variant_c as vc
from benchmarks.selector_v2.plan import PlanApprovalError
from benchmarks.selector_v2.prompt_contract import load_prompt, serializer_contract_fingerprint
from benchmarks.selector_v2.providers import LiveSelectorBackend, ParamOmittingClient, selector_config
from benchmarks.selector_v2.runner import RunIdentity
from benchmarks.selector_v2.safety import check_live_gate, no_live_calls

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "outputs" / "selector-v2-benchmark"
EXP = OUT / "model-experiment" / "c-v1-gpt-5.6-luna"
C_FP = "fc1811443061737b090b388f0b52bb03f68af99a965f2956c4833663847eb9a9"
GOLD11_FP = "1d22cac8e20d32e1b32b0433a886dff3bd2c5b658ab0952a93234ce3ed971138"

pytestmark = pytest.mark.skipif(not (EXP / "prelive-baselines.json").exists(),
                                reason="private benchmark outputs not present")


@pytest.fixture(autouse=True)
def _no_network():
    db_hosts = [urlparse(os.environ[name]).hostname
                for name in ("DATABASE_URL", "ADMIN_DATABASE_URL", "CHAT_DATABASE_URL")
                if os.environ.get(name)]
    with no_live_calls(allow_hosts=db_hosts):
        yield


@pytest.fixture(scope="module")
def world():
    from benchmarks.selector_v2.snapshot import load_snapshot

    with no_live_calls():
        manifest, snaps = load_snapshot(OUT / "snapshots" / "3e558768561814da")
    return {"manifest": manifest, "by_id": {s.case.case_id: s for s in snaps},
            "baseline": me.verify_baseline(EXP / "prelive-baselines.json"),
            "plan": json.loads((EXP / "m1-plan.json").read_text()), "split": px.load_split(),
            "c_baseline": vc.verify_baseline(OUT / "prompt-experiment/variant-c-dev" / vc.BASELINE_FILE)}


def _config():
    return selector_config(provider="openrouter", model=me.MODEL, temperature=0.0, max_tokens=32,
                           reasoning_effort="none")


def test_frozen_identities_and_stage_ids(world):
    b, plan = world["baseline"], world["plan"]
    assert load_prompt("variant_c_v1").fingerprint == C_FP == plan["prompt_fingerprint"]
    assert b["identity"]["semantic_gold_fingerprint"] == GOLD11_FP == plan["semantic_gold_fingerprint"]
    assert plan["split_fingerprint"] == world["split"]["split_fingerprint"]
    assert b["qualifier_slice_ids"] == world["c_baseline"]["qualifier_slice_ids"] and len(b["qualifier_slice_ids"]) == 36
    assert me.stage_ids(b, "m1", "dev") == b["qualifier_slice_ids"]
    assert me.stage_ids(b, "m1", "diagnostic") == ["471", "472"]
    m2 = me.stage_ids(b, "m2", "dev")
    assert len(m2) == 59 and not set(m2) & set(b["qualifier_slice_ids"])
    assert set(m2) | set(b["qualifier_slice_ids"]) == set(world["split"]["dev"])
    for bad in (("m2", "diagnostic"), ("m1", "holdout")):
        with pytest.raises(SystemExit):
            me.stage_ids(b, *bad)


def test_gates_frozen_and_tamper_detected(world, tmp_path):
    b = world["baseline"]
    assert b["m1_gate"] == me.M1_GATE and b["full_gate"] == me.FULL_GATE
    assert me.M1_GATE["qualifier_max"] == 2 and me.FULL_GATE["false_none_max"] == 3
    assert b["new_model_outputs_read"] is False
    bad = {**b, "m1_gate": {**b["m1_gate"], "qualifier_max": 9}}
    (tmp_path / "b.json").write_text(json.dumps(bad))
    with pytest.raises(SystemExit, match="changed after freeze"):
        me.verify_baseline(tmp_path / "b.json")


def test_plan_guard_openrouter_model_and_budget(world):
    plan, b = world["plan"], world["baseline"]
    assert plan["provider"] == "openrouter" and plan["model"] == "openai/gpt-5.6-luna"
    assert plan["reasoning"] == "none" and plan["omitted_request_params"] == ["temperature"]
    assert plan["max_logical_calls"] == 97 and plan["stages"]["m1"]["calls"] == 38
    assert plan["stages"]["m2"]["calls"] == 59 and not any(plan["other_calls"].values())
    assert plan["candidate_order"].startswith("production")
    kwargs = dict(omitted=("temperature",), prompt=load_prompt("variant_c_v1"), split=world["split"],
                  baseline=b, snapshot_fp=world["manifest"]["snapshot_fingerprint"],
                  serializer_fp=serializer_contract_fingerprint(), gold_fp=GOLD11_FP)
    me.validate_plan(plan, plan["plan_fingerprint"], config=_config(), **kwargs)
    wrong_model = selector_config(provider="openrouter", model="openai/gpt-5.6-luna-pro", reasoning_effort="none")
    with pytest.raises(PlanApprovalError, match="model differs"):
        me.validate_plan(plan, plan["plan_fingerprint"], config=wrong_model, **kwargs)
    implicit = selector_config(provider="openrouter", model=me.MODEL)
    with pytest.raises(PlanApprovalError, match="explicit none"):
        me.validate_plan(plan, plan["plan_fingerprint"], config=implicit, **kwargs)
    with pytest.raises(PlanApprovalError, match="omissions"):
        me.validate_plan(plan, plan["plan_fingerprint"], config=_config(), **{**kwargs, "omitted": ()})
    with pytest.raises(SystemExit, match="not allowed"):
        check_live_gate(live=True, confirmed=True, provider="openai", model="gpt-5.6-luna")


def test_request_omits_temperature_and_sends_reasoning_none():
    calls = []

    class Client:
        chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **k: calls.append(k) or SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"decision":"NONE"}'),
                                     finish_reason="stop")], usage=None, model=me.MODEL, id="x")))

        def with_options(self, **_o):
            return self

    backend = LiveSelectorBackend(_config(), client_factory=lambda model: _openrouter_stub(model, Client()),
                                  prompt=load_prompt("variant_c_v1"), omit_request_params=("temperature",))
    assert isinstance(backend.provider.client, ParamOmittingClient)
    from services.candidate_eligibility import CandidateKind, SelectorCandidate

    backend.select(SimpleNamespace(case=SimpleNamespace(intent_text="soru")),
                   [SelectorCandidate("qna:1", CandidateKind.QNA, "q", "a", qna_id=1)])
    assert "temperature" not in calls[0]
    assert calls[0]["extra_body"] == {"reasoning": {"effort": "none"}}
    assert calls[0]["max_tokens"] == 32 and calls[0]["model"] == me.MODEL
    with pytest.raises(SystemExit):
        LiveSelectorBackend(_config(), client_factory=lambda model: _openrouter_stub(model, Client()),
                            omit_request_params=("max_tokens",))


def _openrouter_stub(model, client):
    from services.llm_provider import OpenRouterProvider

    provider = OpenRouterProvider.__new__(OpenRouterProvider)
    provider.model, provider.provider_name, provider.client = model, "openrouter", client
    provider._configured_clients = {}
    return provider


def test_old_run_ids_unchanged_and_omission_in_identity():
    config = selector_config(provider="openrouter", model="openai/gpt-4o-mini")
    base = dict(selector_contract_fingerprint="s", config=config, snapshot_fingerprint="x", run_mode="LIVE")
    assert "omitted_request_params" not in RunIdentity(**base).to_dict()
    assert RunIdentity(**base, omitted_request_params=("temperature",)).run_id != RunIdentity(**base).run_id


def test_candidate_order_and_contents_unchanged(world):
    cid = world["baseline"]["qualifier_slice_ids"][0]
    snap = world["by_id"][cid]
    from benchmarks.selector_v2.contract import order_candidates

    assert [c.candidate_ref for c in order_candidates(snap.selector_candidates(), case_id=cid)] == \
        [c.candidate_ref for c in sorted(snap.candidates, key=lambda c: c.order)]


# ── post-run (skipped until live outputs exist) ────────────────────────────

def test_post_run_integrity(world):
    from benchmarks.selector_v2.runner import RESULTS_FILE, RUN_MANIFEST, load_results

    if not (EXP / "m1-gate.json").exists():
        pytest.skip("live M1 not run")
    loaded, accounts = {}, []
    for scope, root in (("dev", EXP / "runs"), ("diagnostic", EXP / "diagnostics" / "runs")):
        (run_dir,) = [d for d in root.iterdir() if d.is_dir()]
        ident = json.loads((run_dir / RUN_MANIFEST).read_text())
        assert ident["run_mode"] == "LIVE" and ident["config"]["model"] == me.MODEL
        assert ident["omitted_request_params"] == ["temperature"]
        assert ident["config"]["reasoning_effort"] == "none"
        loaded[scope] = load_results(run_dir / RESULTS_FILE)
        assert loaded[scope].superseded == 0 and loaded[scope].corrupt_lines == 0
        accounts += [json.loads(l) for l in (run_dir / "call-accounting.jsonl").read_text().splitlines() if l.strip()]
    assert sorted(loaded["diagnostic"].by_case) == ["471", "472"]
    dev = set(loaded["dev"].by_case)
    m1 = set(world["baseline"]["qualifier_slice_ids"])
    assert dev == m1 or dev == set(world["split"]["dev"])
    total = sum(a["logical_calls"] for a in accounts)
    assert total == len(dev) + 2 <= 97 and all(a["refused_by_budget_guard"] == 0 for a in accounts)
    for a in accounts:
        for keys in a["request_param_keys_sent"]:
            assert "temperature" not in keys and "extra_body" in keys
    gate = json.loads((EXP / "m1-gate.json").read_text())
    m1_metrics = me.evaluate_m1(baseline=world["baseline"], sem=_sem(),
                                dev=loaded["dev"].by_case, diagnostics=loaded["diagnostic"].by_case)
    assert m1_metrics["gate"] == gate["gate"]                          # deterministic
    if dev != m1:
        assert gate["gate"]["passed"]                                   # M2 only after an M1 pass


def _sem():
    from benchmarks.selector_v2 import semantic_gold as sg
    from benchmarks.selector_v2.snapshot import load_snapshot

    _m, snaps = load_snapshot(OUT / "snapshots" / "3e558768561814da")
    _gm, cases = vc.load_gold_v11(OUT / "semantic-gold-v1.1", GOLD11_FP)
    return sg.semantic_snapshots(snaps, cases)
