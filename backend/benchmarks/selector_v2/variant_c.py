"""Phase 7B Variant C: 471/472 review lock → Semantic Gold V1.1 → Variant C DEV.

Order is enforced:

1. ``lock_qualifier_review`` validates the filled 471/472 blind template
   against the frozen review packet and maps anonymous labels to refs by the
   same hash labelling (recomputed from the snapshot).
2. ``build_gold_v11`` derives a CHILD of Semantic Gold V1; a human selection
   equal to the current Gold is KEEP_CURRENT (no case change).
3. ``build_prelive_baseline`` freezes, before any Variant C call, the V1.1
   DEV baselines of the saved production/A/B runs and first-candidate, the
   qualifier heuristic fingerprint and the DEV gate.
4. ``build_plan`` / ``validate_plan`` bind the live run to OpenRouter,
   variant_c_v1, the 95 DEV ids plus the two known-regression diagnostics
   (471, 472), 97 logical calls at most. A full old-HOLDOUT run is refused.
5. ``evaluate`` scores the saved Variant C outputs; diagnostics never enter
   the DEV denominator.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Mapping, Optional, Sequence

from benchmarks.selector_v2 import adjudication as adj
from benchmarks.selector_v2 import prompt_experiment as px
from benchmarks.selector_v2 import qualifier_postmortem as qp
from benchmarks.selector_v2 import semantic_gold as sg
from benchmarks.selector_v2.challenge import first_candidate_correct, paired_bool
from benchmarks.selector_v2.contract import fingerprint, sha256_text
from benchmarks.selector_v2.plan import PRICE_REQUIRED, PlanApprovalError
from benchmarks.selector_v2.postmortem import _case_key
from benchmarks.selector_v2.schema import BenchmarkCase, BenchmarkResult, CaseSnapshot, EvaluationStatus
from benchmarks.selector_v2.snapshot import describe

LOCK_SCHEMA = "selector-qualifier-review-lock-v1"
GOLD_VERSION = "selector-semantic-gold-v1.1"
PARENT_GOLD_FP = "36ab1d6f06b990b247f8fee882acd0aba3d4f235ec0ecafc65504c3ffc9ea713"
PROMPT_ID = "variant_c_v1"
BASE_PROMPT_ID = "variant_a_v1"
KNOWN_REGRESSIONS = ("471", "472")
DEV_SIZE = 95
MAX_DEV_CALLS = 95
MAX_DIAGNOSTIC_CALLS = 2
MAX_TOTAL_CALLS = MAX_DEV_CALLS + MAX_DIAGNOSTIC_CALLS
PLAN_KIND = "selector-prompt-experiment-dev-variant-c"
BASELINE_VERSION = "selector-variant-c-dev-baseline-v1"
BASELINE_FILE = "prelive-baseline.json"
PLAN_FILE = "live-plan.json"
SCOPES = ("dev", "diagnostic")

DEV_GATE = {
    "version": "selector-variant-c-dev-gate-v1",
    "purpose": "Is Variant C worth carrying to a NEW independent validation set? NOT final validation.",
    "operational": ["run complete (95 DEV + 2 diagnostics)", "invalid_output == 0", "model_error == 0",
                    "timeout == 0"],
    "behavioral": {
        "qualifier_improvement": "C UNSTATED_QUALIFIER_ASSUMED < A UNSTATED_QUALIFIER_ASSUMED on the "
                                 "frozen DEV qualifier slice (same heuristic, frozen before the run)",
        "false_none": "C false NONE <= 5 on semantic-evaluable DEV",
        "exact": "C semantic exact >= 62/77 (>= 0.80 if the denominator differs)",
        "production_regression": "0 production-correct -> C-wrong cases on the DEV general/specific slices",
        "known_regressions": "471 and 472 correct under Semantic Gold V1.1 (tuning cases, not validation)",
    },
    "exact_threshold": {"numerator": 62, "denominator": 77, "ratio": 0.80},
    "false_none_max": 5,
    "pass_label": "VARIANT_C_DEV = PASS; FINAL_VALIDATION = READY_FOR_PREPARATION",
    "fail_label": "VARIANT_C_DEV = FAIL; FINAL_VALIDATION = BLOCKED",
}


def file_sha256(path: Path) -> str:
    return hashlib.new("sha256", Path(path).read_bytes()).hexdigest()


def heuristic_fingerprint() -> dict:
    """Frozen identity of the qualifier taxonomy/compliance heuristic."""
    source = Path(qp.__file__)
    return {"module_sha256": file_sha256(source),
            "lexicon_fingerprint": fingerprint({"lexicon": list(qp.QUALIFIER_LEXICON),
                                                "exclusions": list(qp.LEXICON_EXCLUSIONS)})}


# ── 1. review lock ─────────────────────────────────────────────────────────

def lock_qualifier_review(*, decisions_bytes: bytes, review_dir: Path,
                          snapshots: Mapping[str, CaseSnapshot]) -> dict:
    review_dir = Path(review_dir)
    manifest = json.loads((review_dir / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["artifact_sha256"].items():
        if sha256_text(sg._read_exact(review_dir / name)) != digest:
            raise SystemExit(f"review packet artifact {name} changed")
    packet = [json.loads(l) for l in sg._read_exact(review_dir / "review-cases.jsonl").splitlines() if l.strip()]
    rebuilt = qp.blind_records(snapshots, manifest["case_ids"])
    if rebuilt != packet or qp.review_fingerprint(manifest["source_snapshot_fingerprint"], rebuilt) \
            != manifest["review_packet_fingerprint"]:
        raise SystemExit("review packet does not reproduce from the frozen snapshot")
    sha = hashlib.new("sha256", decisions_bytes).hexdigest()
    rows = sg.read_csv_rows(decisions_bytes.decode("utf-8"))
    problems = []
    ids = [r["case_id"] for r in rows]
    if len(rows) != len(manifest["case_ids"]) or set(ids) != set(manifest["case_ids"]) \
            or len(set(ids)) != len(ids):
        problems.append(f"rows {ids} != packet cases {manifest['case_ids']}")
    labels = {rec["case_id"]: [c["label"] for c in rec["candidates"]] for rec in packet}
    records = []
    for row in rows:
        cid, decision = row["case_id"], (row.get("blind_decision") or "").strip()
        chosen = sg._labels_of(row.get("acceptable_labels"))
        if decision not in qp.REVIEW_DECISIONS:
            problems.append(f"{cid}: decision {decision!r} not allowed")
        if decision == "SELECT_ACCEPTABLE" and not chosen:
            problems.append(f"{cid}: SELECT_ACCEPTABLE without labels")
        if decision != "SELECT_ACCEPTABLE" and chosen:
            problems.append(f"{cid}: labels on {decision}")
        if [c for c in chosen if c not in labels.get(cid, [])]:
            problems.append(f"{cid}: unknown labels {chosen}")
        if row.get("intent_text") and row["intent_text"] != snapshots[cid].case.intent_text:
            problems.append(f"{cid}: intent text differs from the packet")
        label_map = sg.blind_label_map(snapshots[cid])
        records.append({"case_id": cid, "blind_decision": decision, "acceptable_labels": chosen,
                        "mapped_refs": sorted(label_map[c] for c in chosen if c in label_map),
                        "review_note": row.get("review_note", ""), "reviewer": row.get("reviewer", ""),
                        "reviewed_at": row.get("reviewed_at", "")})
    if problems:
        raise SystemExit(f"qualifier review lock refused: {problems}")
    records.sort(key=lambda r: _case_key(r["case_id"]))
    identity = {"schema": LOCK_SCHEMA, "source_decision_sha256": sha,
                "parent_review_packet_fingerprint": manifest["review_packet_fingerprint"],
                "snapshot_fingerprint": manifest["source_snapshot_fingerprint"], "records": records}
    return {"records": records, "identity": identity, "lock_fingerprint": fingerprint(identity)}


def write_lock(lock_dir: Path, lock: dict, locked_at: str) -> dict:
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in lock["records"])
    hashes = adj.write_immutable(lock_dir, {"locked-review.jsonl": body})
    existing = Path(lock_dir) / "manifest.json"
    if existing.exists():
        prior = json.loads(existing.read_text(encoding="utf-8"))
        if prior["lock_fingerprint"] != lock["lock_fingerprint"]:
            raise SystemExit("a different qualifier review lock already exists (immutable)")
        return prior
    manifest = {**{k: v for k, v in lock["identity"].items() if k != "records"},
                "lock_fingerprint": lock["lock_fingerprint"], "case_count": len(lock["records"]),
                "artifact_sha256": hashes, "locked_at": locked_at}
    adj.write_immutable(lock_dir, {"manifest.json": json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"})
    return manifest


def load_lock(lock_dir: Path) -> tuple[dict, list[dict]]:
    manifest = json.loads((Path(lock_dir) / "manifest.json").read_text(encoding="utf-8"))
    body = sg._read_exact(Path(lock_dir) / "locked-review.jsonl")
    if sha256_text(body) != manifest["artifact_sha256"]["locked-review.jsonl"]:
        raise SystemExit("qualifier review lock changed")
    records = [json.loads(l) for l in body.splitlines() if l.strip()]
    identity = {k: manifest[k] for k in ("schema", "source_decision_sha256",
                                         "parent_review_packet_fingerprint", "snapshot_fingerprint")}
    if fingerprint({**identity, "records": records}) != manifest["lock_fingerprint"]:
        raise SystemExit("qualifier review lock fingerprint mismatch")
    return manifest, records


# ── 2. Semantic Gold V1.1 (child of V1) ────────────────────────────────────

def build_gold_v11(parent_cases: Sequence[BenchmarkCase], records: Sequence[dict], *,
                   parent_fp: str, lock_fp: str) -> dict:
    by_rec = {r["case_id"]: r for r in records}
    cases, changes = [], []
    for case in parent_cases:
        rec = by_rec.get(case.case_id)
        new = case
        if rec is not None:
            decision = rec["blind_decision"]
            old = sorted(case.acceptable_candidate_refs)
            if decision == "SELECT_ACCEPTABLE":
                refs = rec["mapped_refs"]
                outcome = ("KEEP_CURRENT" if refs == old and case.expected_decision == "SELECT"
                           and case.evaluation_status is EvaluationStatus.SELECTOR_EVALUABLE
                           else "CHANGE_GOLD" if len(refs) == 1 else "MULTI_ACCEPTABLE")
                update = {} if outcome == "KEEP_CURRENT" else {
                    "expected_decision": "SELECT", "acceptable_candidate_refs": refs,
                    "evaluation_status": EvaluationStatus.SELECTOR_EVALUABLE, "status_reason": None}
            elif decision == "EXPECT_NONE":
                outcome = "EXPECT_NONE"
                update = {"expected_decision": "NONE", "acceptable_candidate_refs": [],
                          "evaluation_status": EvaluationStatus.SELECTOR_EVALUABLE, "status_reason": None}
            else:
                outcome = decision
                status, reason = sg.EXCLUSION_REASON[decision]
                update = {"evaluation_status": status, "status_reason": reason}
            if update:
                new = BenchmarkCase.model_validate({**case.model_dump(), **update,
                                                    "review_decision": f"QUALIFIER_{outcome}"})
            changes.append({"case_id": case.case_id, "derived_outcome": outcome,
                            "gold_changed": new != case,
                            "previous": {"expected_decision": case.expected_decision,
                                         "acceptable_candidate_refs": case.acceptable_candidate_refs,
                                         "evaluation_status": case.evaluation_status.value},
                            "new": {"expected_decision": new.expected_decision,
                                    "acceptable_candidate_refs": new.acceptable_candidate_refs,
                                    "evaluation_status": new.evaluation_status.value},
                            "review_decision": decision, "labels": rec["acceptable_labels"],
                            "mapped_refs": rec["mapped_refs"], "reviewer": rec["reviewer"],
                            "reviewed_at": rec["reviewed_at"], "review_note": rec["review_note"],
                            "source_lock_fingerprint": lock_fp, "parent_gold_fingerprint": parent_fp})
        cases.append(new)
    fp = fingerprint({"version": GOLD_VERSION, "parent": parent_fp, "lock": lock_fp,
                      "cases": [c.model_dump(mode="json") for c in cases]})
    return {"cases": cases, "changes": changes, "fingerprint": fp}


def load_gold_v11(gold_dir: Path, expected_fp: Optional[str] = None) -> tuple[dict, list[BenchmarkCase]]:
    gold_dir = Path(gold_dir)
    manifest = json.loads((gold_dir / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["artifact_sha256"].items():
        if sha256_text(sg._read_exact(gold_dir / name)) != digest:
            raise SystemExit(f"semantic gold v1.1 artifact {name} changed")
    body = sg._read_exact(gold_dir / "semantic-gold.jsonl")
    cases = [BenchmarkCase.model_validate_json(l) for l in body.splitlines() if l.strip()]
    fp = fingerprint({"version": GOLD_VERSION, "parent": manifest["parent_semantic_gold_fingerprint"],
                      "lock": manifest["qualifier_review_lock_fingerprint"],
                      "cases": [c.model_dump(mode="json") for c in cases]})
    if fp != manifest["semantic_gold_fingerprint"] or (expected_fp and fp != expected_fp):
        raise SystemExit("semantic gold v1.1 fingerprint mismatch")
    return manifest, cases


# ── 3. baselines, qualifier slice, gate ────────────────────────────────────

def dev_ids(sem_by: Mapping[str, CaseSnapshot], split: dict) -> list[str]:
    return [i for i in split["dev"] if sem_by[i].selector_evaluable]


def qualifier_slice(snapshots: Sequence[CaseSnapshot], sem_by: Mapping[str, CaseSnapshot],
                    ids: Sequence[str]) -> list[str]:
    """Model-independent DEV qualifier slice: inventory v1 members among ``ids``."""
    idset = set(ids)
    inv = qp.inventory([s for s in snapshots if s.case.case_id in idset], sem_by, {})
    return sorted((r["case_id"] for r in inv if r["semantic_evaluable"]), key=_case_key)


def run_block(name: str, ids: Sequence[str], results: Mapping[str, BenchmarkResult],
              sem_by: Mapping[str, CaseSnapshot], qslice: Sequence[str], gs: Mapping[str, list]) -> dict:
    correct = {i: results[i].correct for i in ids if i in results}
    outcomes: dict[str, int] = {}
    for i in ids:
        if i in results:
            outcomes[results[i].outcome.value] = outcomes.get(results[i].outcome.value, 0) + 1
    comp_all = qp.compliance_table(sem_by, [i for i in ids if i in results], results)
    comp_q = qp.compliance_table(sem_by, [i for i in qslice if i in results], results)
    expected_none = [i for i in ids if sem_by[i].case.expected_decision == "NONE"]
    from benchmarks.selector_v2.challenge import selector_value

    return {
        "name": name, "denominator": len(ids), "completed": len(correct),
        "exact": sum(correct.values()),
        "accuracy": round(sum(correct.values()) / len(ids), 6) if ids else None,
        "false_none": outcomes.get("FALSE_NONE", 0), "correct_none": outcomes.get("CORRECT_NONE", 0),
        "none_count": outcomes.get("FALSE_NONE", 0) + outcomes.get("CORRECT_NONE", 0),
        "false_select_on_expected_none": outcomes.get("FALSE_SELECT", 0),
        "wrong_select": outcomes.get("WRONG_SELECT", 0),
        "expected_none_count": len(expected_none),
        "invalid_output": outcomes.get("INVALID_OUTPUT", 0), "model_error": outcomes.get("MODEL_ERROR", 0),
        "timeout": outcomes.get("TIMEOUT", 0), "outcomes": outcomes,
        "unstated_qualifier_assumed_dev": comp_all["counts"]["UNSTATED_QUALIFIER_ASSUMED"],
        "unstated_qualifier_assumed_slice": comp_q["counts"]["UNSTATED_QUALIFIER_ASSUMED"],
        "unstated_ids_dev": comp_all["unstated_qualifier_assumed_ids"],
        "compliance_dev": comp_all["counts"], "compliance_slice": comp_q["counts"],
        "selector_value": selector_value([sem_by[i] for i in ids], correct),
        "slices": {k: {"cases": len(v), "correct": sum(1 for i in v if correct.get(i))}
                   for k, v in gs.items()},
        "correct_by_case": correct,
    }


def gs_slices(sem_by: Mapping[str, CaseSnapshot], parent_by: Mapping[str, CaseSnapshot],
              snapshots: Sequence[CaseSnapshot], ids: Sequence[str]) -> dict[str, list[str]]:
    """Phase 7A tags (historical DEV gate slice) and the inventory expectation."""
    idset = set(ids)
    inv = {r["case_id"]: r for r in qp.inventory([s for s in snapshots if s.case.case_id in idset],
                                                  sem_by, {})}
    exp = {i: qp.expectation_class(inv[i], sem_by[i])["class"] for i in inv if i in idset}
    return {
        "phase7a_general": [i for i in ids if "gs_expected:general" in parent_by[i].derived_tags],
        "phase7a_specific": [i for i in ids if "gs_expected:specific" in parent_by[i].derived_tags],
        "inventory_general": [i for i in ids if exp.get(i) == "GENERAL"],
        "inventory_specific": [i for i in ids if exp.get(i) == "SPECIFIC"],
    }


def build_prelive_baseline(*, snapshots, sem, split, runs: Mapping[str, Mapping[str, BenchmarkResult]],
                           identity: dict) -> dict:
    sem_by = {s.case.case_id: s for s in sem}
    parent_by = {s.case.case_id: s for s in snapshots}
    if len(split["dev"]) != DEV_SIZE:
        raise SystemExit("DEV split size changed")
    ids = dev_ids(sem_by, split)
    qslice = qualifier_slice(snapshots, sem_by, ids)
    gs = gs_slices(sem_by, parent_by, snapshots, ids)
    rescored = {n: sg.rescore_run(sem, r) for n, r in runs.items()}
    blocks = {n: run_block(n, ids, r, sem_by, qslice, gs) for n, r in rescored.items()}
    fc = {i: first_candidate_correct(sem_by[i]) for i in ids}
    known = {cid: {"semantic_gold_v11": sem_by[cid].case.acceptable_candidate_refs,
                   **{n: ({"selected": runs[n][cid].selected_candidate_ref,
                           "decision": runs[n][cid].decision} if cid in runs[n] else None)
                      for n in runs}} for cid in KNOWN_REGRESSIONS}
    body = {"version": BASELINE_VERSION, "identity": identity,
            "dev_case_ids": list(split["dev"]), "dev_evaluable_ids": ids, "dev_evaluable": len(ids),
            "dev_excluded": {i: sem_by[i].case.status_reason or sem_by[i].pool_status.value
                             for i in split["dev"] if i not in ids},
            "qualifier_slice_ids": qslice, "gs_slices": gs,
            "runs": blocks,
            "first_candidate": {"exact": sum(fc.values()), "denominator": len(ids),
                                "accuracy": round(sum(fc.values()) / len(ids), 6)},
            "known_regressions_saved": known,
            "heuristic": heuristic_fingerprint(), "dev_gate": DEV_GATE,
            "variant_c_outputs_read": False}
    return {**body, "baseline_fingerprint": fingerprint(body)}


def verify_baseline(path: Path) -> dict:
    baseline = json.loads(Path(path).read_text(encoding="utf-8"))
    body = {k: v for k, v in baseline.items() if k not in ("baseline_fingerprint", "frozen_at")}
    if fingerprint(body) != baseline.get("baseline_fingerprint"):
        raise SystemExit("Variant C pre-live baseline changed after freeze")
    if baseline["heuristic"] != heuristic_fingerprint():
        raise SystemExit("qualifier heuristic changed after freeze")
    return baseline


# ── 4. plan and guard ──────────────────────────────────────────────────────

def build_plan(*, config, prompt, base_prompt, split, baseline, snapshot_fp, serializer_fp,
               gold_fp, estimate_block) -> dict:
    plan = {
        "plan_kind": PLAN_KIND, "live": True,
        "provider": config.provider, "model": config.model,
        "reasoning": None if config.reasoning_effort is None else config.reasoning_effort.value,
        "temperature": config.temperature, "max_tokens": config.max_tokens,
        "config_fingerprint": config.fingerprint,
        "prompt_id": prompt.prompt_id, "prompt_fingerprint": prompt.fingerprint,
        "base_prompt_id": base_prompt.prompt_id, "base_prompt_fingerprint": base_prompt.fingerprint,
        "candidate_order": "production (original retrieval order)",
        "dev_case_ids": list(split["dev"]), "diagnostic_case_ids": list(KNOWN_REGRESSIONS),
        "calls": {"dev": len(split["dev"]), "diagnostic": len(KNOWN_REGRESSIONS),
                  "total": len(split["dev"]) + len(KNOWN_REGRESSIONS)},
        "max_logical_calls": MAX_TOTAL_CALLS,
        "other_calls": {"production": 0, "variant_a_v1": 0, "variant_b_v1": 0,
                        "old_holdout_full": 0, "neutral_order": 0, "final_validation": 0},
        "retries": "none (errors are reported, never retried)",
        "snapshot_fingerprint": snapshot_fp, "split_fingerprint": split["split_fingerprint"],
        "serializer_contract_fingerprint": serializer_fp, "semantic_gold_fingerprint": gold_fp,
        "prelive_baseline_fingerprint": baseline["baseline_fingerprint"],
        "dev_gate_version": DEV_GATE["version"], "heuristic": baseline["heuristic"],
        "estimated_tokens": estimate_block, "cost": PRICE_REQUIRED, "provider_policy": "OpenRouter only",
    }
    plan["plan_fingerprint"] = px.plan_fingerprint(plan)
    return plan


def scope_case_ids(scope: str, split: dict) -> list[str]:
    """Only DEV, or exactly the two known-regression diagnostics. Never the old HOLDOUT."""
    if scope == "dev":
        return list(split["dev"])
    if scope == "diagnostic":
        if not set(KNOWN_REGRESSIONS) <= set(split["holdout"]):
            raise SystemExit("known-regression ids are not the consumed HOLDOUT cases")
        return list(KNOWN_REGRESSIONS)
    raise SystemExit(f"scope {scope!r} refused: only 'dev' or 'diagnostic' (old HOLDOUT full run forbidden)")


def validate_plan(plan: dict, approved_fp: Optional[str], *, config, prompt, split, baseline,
                  snapshot_fp, serializer_fp, gold_fp) -> None:
    if not approved_fp:
        raise PlanApprovalError("Variant C live run requires --approve-plan-fingerprint")
    checks = {
        "not a Variant C DEV plan": plan.get("plan_kind") != PLAN_KIND,
        "plan file modified": px.plan_fingerprint(plan) != plan.get("plan_fingerprint"),
        "approved fingerprint differs": approved_fp != plan.get("plan_fingerprint"),
        "provider is not openrouter": config.provider != "openrouter" or plan.get("provider") != "openrouter",
        "config differs": plan.get("config_fingerprint") != config.fingerprint,
        "prompt is not variant_c_v1": prompt.prompt_id != PROMPT_ID or plan.get("prompt_id") != PROMPT_ID
        or plan.get("prompt_fingerprint") != prompt.fingerprint,
        "DEV ids differ": plan.get("dev_case_ids") != list(split["dev"])
        or plan.get("split_fingerprint") != split["split_fingerprint"],
        "diagnostics differ": plan.get("diagnostic_case_ids") != list(KNOWN_REGRESSIONS),
        "call budget differs": plan.get("max_logical_calls") != MAX_TOTAL_CALLS
        or plan.get("calls", {}).get("total") != MAX_TOTAL_CALLS,
        "other calls planned": any(plan.get("other_calls", {}).values()),
        "snapshot differs": plan.get("snapshot_fingerprint") != snapshot_fp,
        "serializer differs": plan.get("serializer_contract_fingerprint") != serializer_fp,
        "semantic gold differs": plan.get("semantic_gold_fingerprint") != gold_fp,
        "baseline differs": plan.get("prelive_baseline_fingerprint") != baseline.get("baseline_fingerprint"),
        "heuristic differs": plan.get("heuristic") != heuristic_fingerprint(),
    }
    failed = [k for k, bad in checks.items() if bad]
    if failed:
        raise PlanApprovalError(f"Variant C plan refused: {', '.join(failed)}")


# ── 5. evaluation ──────────────────────────────────────────────────────────

def evaluate(*, baseline, snapshots, sem, runs: Mapping[str, Mapping[str, BenchmarkResult]],
             variant_c: Mapping[str, BenchmarkResult], diagnostics: Mapping[str, BenchmarkResult]) -> dict:
    sem_by = {s.case.case_id: s for s in sem}
    parent_by = {s.case.case_id: s for s in snapshots}
    ids = baseline["dev_evaluable_ids"]
    qslice, gs = baseline["qualifier_slice_ids"], baseline["gs_slices"]
    rescored = {n: sg.rescore_run(sem, r) for n, r in runs.items()}
    for n, r in rescored.items():
        if run_block(n, ids, r, sem_by, qslice, gs) != baseline["runs"][n]:
            raise SystemExit(f"saved {n} baseline no longer reproduces the frozen values")
    c_sem = sg.rescore_run(sem, variant_c)
    c = run_block("variant_c_v1", ids, c_sem, sem_by, qslice, gs)
    blocks = {**baseline["runs"], "variant_c_v1": c}
    ok = {n: b["correct_by_case"] for n, b in blocks.items()}

    def pair(a, b):
        p = paired_bool(ok[a], ok[b], ids)
        return {"both_correct": p["both_correct"], f"only_{a}": p["only_a_correct"],
                f"only_{b}": p["only_b_correct"], "both_wrong": p["both_wrong"],
                "paired_net": p["only_b_correct"] - p["only_a_correct"],
                "mcnemar_exact_p": p["mcnemar_exact_p_two_sided"]}

    def regressions(a):
        return sorted((i for i in ids if ok[a].get(i) and ok["variant_c_v1"].get(i) is False), key=_case_key)

    gs_ids = sorted(set(gs["phase7a_general"] + gs["phase7a_specific"]
                        + gs["inventory_general"] + gs["inventory_specific"]), key=_case_key)
    prod_gs_regressions = [i for i in regressions("production") if i in gs_ids]
    texts = lambda cid: {x.candidate_ref: x.canonical_text for x in sem_by[cid].candidates}
    known = {}
    for cid in KNOWN_REGRESSIONS:
        r = diagnostics.get(cid)
        scored = sg.rescore_run([sem_by[cid]], {cid: r}).get(cid) if r else None
        comp = qp.compliance(sem_by[cid], scored) if scored else None
        known[cid] = {"label": "KNOWN DEVELOPMENT REGRESSION CASE (consumed HOLDOUT; not validation)",
                      "user_text": sem_by[cid].case.intent_text,
                      "semantic_gold_v11": sem_by[cid].case.acceptable_candidate_refs,
                      "decision": r.decision if r else None,
                      "selected": r.selected_candidate_ref if r else None,
                      "selected_text": texts(cid).get(r.selected_candidate_ref) if r else None,
                      "correct": bool(scored and scored.correct),
                      "unstated_qualifier_assumed": bool(comp and comp["label"] == "UNSTATED_QUALIFIER_ASSUMED"),
                      "compliance_label": comp["label"] if comp else None,
                      "saved_production": baseline["known_regressions_saved"][cid].get("production"),
                      "saved_variant_a": baseline["known_regressions_saved"][cid].get("variant_a_v1")}
    all_c = [variant_c[i] for i in baseline["dev_case_ids"] if i in variant_c] + list(diagnostics.values())
    ops = {"dev_complete": sorted(variant_c) == sorted(baseline["dev_case_ids"]),
           "diagnostics_complete": sorted(diagnostics) == sorted(KNOWN_REGRESSIONS),
           "invalid_output": sum(r.outcome.value == "INVALID_OUTPUT" for r in all_c),
           "model_error": sum(r.outcome.value == "MODEL_ERROR" for r in all_c),
           "timeout": sum(r.outcome.value == "TIMEOUT" for r in all_c)}
    thr = DEV_GATE["exact_threshold"]
    exact_ok = (c["exact"] >= thr["numerator"] if len(ids) == thr["denominator"]
                else c["accuracy"] >= thr["ratio"])
    a = blocks["variant_a_v1"]
    gate = {
        "operational": ops["dev_complete"] and ops["diagnostics_complete"]
        and not (ops["invalid_output"] or ops["model_error"] or ops["timeout"]),
        "qualifier_improvement": c["unstated_qualifier_assumed_slice"] < a["unstated_qualifier_assumed_slice"],
        "false_none": c["false_none"] <= DEV_GATE["false_none_max"],
        "exact": exact_ok,
        "production_regression": not prod_gs_regressions,
        "known_regressions": all(k["correct"] for k in known.values()),
    }

    def values(attr):
        return [getattr(r, attr) for r in all_c if getattr(r, attr) is not None]

    return {
        "dev_evaluable": len(ids), "qualifier_slice_size": len(qslice),
        "runs": {n: {k: v for k, v in b.items() if k != "correct_by_case"} for n, b in blocks.items()},
        "first_candidate": baseline["first_candidate"],
        "paired": {"variant_a_v1 vs variant_c_v1": pair("variant_a_v1", "variant_c_v1"),
                   "production vs variant_c_v1": pair("production", "variant_c_v1"),
                   "variant_b_v1 vs variant_c_v1": pair("variant_b_v1", "variant_c_v1")},
        "regressions": {"production_correct_c_wrong": regressions("production"),
                        "a_correct_c_wrong": regressions("variant_a_v1"),
                        "production_correct_c_wrong_general_specific": prod_gs_regressions,
                        "c_correct_a_wrong": sorted((i for i in ids if ok["variant_c_v1"].get(i)
                                                     and ok["variant_a_v1"].get(i) is False), key=_case_key)},
        "known_regressions": known, "operational": ops,
        "gate": {"version": DEV_GATE["version"], "checks": gate, "passed": all(gate.values())},
        "usage": {"input_tokens": describe(values("input_tokens")),
                  "output_tokens": describe(values("output_tokens")),
                  "latency_ms": describe(values("latency_ms")), "calls": len(all_c), "cost": PRICE_REQUIRED},
        "scored_rows": [{"case_id": i, "set": "DEV", "semantic_evaluable": i in ids,
                         "decision": variant_c[i].decision, "selected": variant_c[i].selected_candidate_ref,
                         "outcome": c_sem[i].outcome.value if i in c_sem else None,
                         "correct": c_sem[i].correct if i in c_sem else None,
                         "compliance": qp.compliance(sem_by[i], c_sem[i])["label"] if i in c_sem else None}
                        for i in baseline["dev_case_ids"] if i in variant_c],
    }


def csv_text_rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))
