"""Command line entry: ``python -m benchmarks.selector_v2 <command>``.

Every command except ``run --live --confirm-live-provider-calls`` executes
inside the network guard. ``snapshot`` and ``config-snapshot`` may reach
only the configured Postgres/Meili/Qdrant hosts (read-only); the others may
reach nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from benchmarks.selector_v2 import BENCHMARK_VERSION
from benchmarks.selector_v2.contract import fingerprint
from benchmarks.selector_v2.safety import (
    CONFIRM_FLAG, LIVE_PROVIDERS, check_live_gate, no_live_calls,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARCH_DOC = REPO_ROOT / "docs" / "ANSWER_PIPELINE_V2_ARCHITECTURE.md"


def _dump(data, path: Path | None = None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if path is None:
        print(text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")


def _infra_hosts() -> list[str]:
    hosts = []
    for name in ("DATABASE_URL", "ADMIN_DATABASE_URL", "CHAT_DATABASE_URL", "MEILI_URL"):
        value = os.getenv(name)
        if value:
            hosts.append(urlparse(value).hostname)
    hosts.append(os.getenv("QDRANT_HOST", "localhost"))
    return [h for h in hosts if h]


def _load_dataset(args):
    from benchmarks.selector_v2.gold_loader import load_cases_jsonl, load_reviewed_gold

    if args.cases:
        return load_cases_jsonl(Path(args.cases))
    if args.gold_dir:
        return load_reviewed_gold(Path(args.gold_dir),
                                  Path(args.session_dir) if args.session_dir else None)
    raise SystemExit("dataset required: --gold-dir [--session-dir] or --cases")


def _fail_on_checks(dataset) -> None:
    for check in dataset.checks:
        print(f"[{check.status}] {check.name} {check.detail}", file=sys.stderr)
    if dataset.failed:
        raise SystemExit(f"dataset validation FAILED: {[c.name for c in dataset.failed]}")


def cmd_load(args) -> None:
    from benchmarks.selector_v2.gold_loader import write_cases_jsonl

    with no_live_calls():
        dataset = _load_dataset(args)
    _fail_on_checks(dataset)
    out = Path(args.out)
    write_cases_jsonl(dataset.cases, out / "cases.jsonl")
    _dump(dataset.report(), out / "dataset-report.json")
    _dump(dataset.counts)


def _git_sha() -> tuple[str, bool | None]:
    if os.getenv("BENCHMARK_GIT_SHA"):
        dirty = os.getenv("BENCHMARK_GIT_DIRTY")
        return os.environ["BENCHMARK_GIT_SHA"], (dirty == "1") if dirty else None
    try:
        sha = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                                    capture_output=True, text=True, check=True).stdout.strip())
        return sha, dirty
    except Exception:
        return "unknown", None


def cmd_snapshot(args) -> None:
    from benchmarks.selector_v2.contract import selector_contract, selector_contract_fingerprint
    from benchmarks.selector_v2.gold_loader import GOLD_ALL, sha256_file
    from benchmarks.selector_v2 import snapshot as snap

    as_of = dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.today()
    with no_live_calls(_infra_hosts()):
        dataset = _load_dataset(args)
        _fail_on_checks(dataset)
        from core.database import SessionLocal
        from core.deps import MEILI_PROVIDER, MEILI_STATUS, QDRANT_PROVIDER
        from services.candidate_eligibility import selector_max_candidates

        MEILI_PROVIDER.healthcheck()
        QDRANT_PROVIDER.healthcheck()
        budget = args.max_candidates or selector_max_candidates()
        pairs = snap.load_near_pairs()
        db = SessionLocal()
        try:
            kb = snap.kb_fingerprint(db)
            near = snap.verify_near_pairs(db, pairs)
            drift = (snap.expected_content_drift(db, Path(args.gold_dir) / GOLD_ALL)
                     if args.gold_dir else None)
            build_pool = snap.production_pool_builder(db, as_of=as_of, max_candidates=budget)
            snapshots = snap.generate_snapshots(dataset.cases, build_pool=build_pool, pairs=pairs)
        finally:
            db.close()
        if not MEILI_STATUS["healthy"]:
            raise SystemExit("Meili became unhealthy during generation; snapshot discarded")
        empty_qdrant = [s.case.case_id for s in snapshots
                        if s.retrieval_counts and not s.retrieval_counts.get("qdrant")]
        if empty_qdrant:
            raise SystemExit(f"Qdrant returned no hits for {empty_qdrant[:10]}; snapshot discarded")

    git_sha, dirty = _git_sha()
    arch = Path(args.architecture_doc)
    manifest = {
        "benchmark_version": BENCHMARK_VERSION,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "git_sha": git_sha,
        "git_dirty": dirty,
        "architecture_doc_sha256": sha256_file(arch) if arch.exists() else None,
        "selector_contract_fingerprint": selector_contract_fingerprint(),
        "selector_contract": selector_contract(),
        "candidate_eligibility": {**snap.eligibility_version(), "candidate_budget": budget},
        "retrieval": {
            "qna": "answer_pipeline._build_candidate_pool_result (Qdrant 24 + Meili 5)",
            "embedding_model": getattr(QDRANT_PROVIDER, "model_name", None)
            or "nezahatkorkmaz/turkce-embedding-bge-m3",
            "routing_guard_as_of": as_of.isoformat(),
        },
        "calendar": {
            "route": "closed: dataset supplies no calendar_relevant; no Calendar candidates",
            "fingerprint": None,
        },
        "kb": kb,
        "near_qna_fixture_verification": near,
        "expected_content_vs_kb": drift,
        "dataset": dataset.report(),
        "case_count": len(snapshots),
        "retrieval_diagnostics": snap.retrieval_diagnostics(snapshots),
    }
    manifest = snap.write_snapshot(Path(args.out), snapshots, manifest)
    _dump({k: manifest[k] for k in ("snapshot_fingerprint", "selector_contract_fingerprint",
                                    "case_count", "retrieval_diagnostics", "kb")})


def cmd_estimate(args) -> None:
    from benchmarks.selector_v2.snapshot import load_snapshot
    from benchmarks.selector_v2.tokens import estimate

    with no_live_calls():
        manifest, snapshots = load_snapshot(Path(args.snapshot))
        result = estimate(
            snapshots, max_tokens=args.max_tokens,
            input_price_per_1m=args.input_price_per_1m,
            output_price_per_1m=args.output_price_per_1m,
            configs_planned=args.configs, primary_only=args.primary_only,
        )
    result["snapshot_fingerprint"] = manifest["snapshot_fingerprint"]
    _dump(result, Path(args.out) if args.out else None)


def _write_report(snapshot_dir: Path, run_dir: Path) -> dict:
    from benchmarks.selector_v2.evaluator import evaluate, render_markdown
    from benchmarks.selector_v2.runner import RESULTS_FILE, RUN_MANIFEST, load_results
    from benchmarks.selector_v2.snapshot import load_snapshot

    manifest, snapshots = load_snapshot(snapshot_dir)
    run_manifest = json.loads((run_dir / RUN_MANIFEST).read_text(encoding="utf-8"))
    loaded = load_results(run_dir / RESULTS_FILE)
    report = evaluate(snapshots, loaded.by_case, snapshot_manifest=manifest,
                      run_manifest=run_manifest)
    report["corrupt_result_lines_ignored"] = loaded.corrupt_lines
    report["superseded_attempts"] = loaded.superseded
    _dump(report, run_dir / "metrics.json")
    (run_dir / "REPORT.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def cmd_run(args) -> None:
    from benchmarks.selector_v2.contract import selector_contract_fingerprint
    from benchmarks.selector_v2.providers import (
        FAKE_PROVIDER, FakeSelectorProvider, LiveSelectorBackend, selector_config,
    )
    from benchmarks.selector_v2.runner import RunIdentity, run_benchmark
    from benchmarks.selector_v2.snapshot import load_snapshot
    from benchmarks.selector_v2.tokens import estimate

    check_live_gate(live=args.live, confirmed=args.confirm_live_provider_calls,
                    provider=args.provider, model=args.model)
    manifest, snapshots = load_snapshot(Path(args.snapshot))
    contract_fp = selector_contract_fingerprint()
    if contract_fp != manifest["selector_contract_fingerprint"]:
        print("WARNING: current selector contract differs from the snapshot's; results go "
              "to a new namespace", file=sys.stderr)
    challenge_manifest, case_ids = None, None
    if args.challenge:
        from benchmarks.selector_v2.challenge import load_challenge

        challenge_manifest, challenge_cases = load_challenge(Path(args.challenge), manifest)
        case_ids = {c["case_id"] for c in challenge_cases}
    if args.live:
        from benchmarks.selector_v2.plan import load_approved_plan

        config = selector_config(
            provider=args.provider, model=args.model, temperature=args.temperature,
            max_tokens=args.max_tokens, reasoning_effort=args.reasoning_effort,
            timeout_seconds=args.timeout_seconds, max_retries=args.max_retries,
        )
        plan = load_approved_plan(
            args.live_plan and Path(args.live_plan), args.approve_plan_fingerprint,
            snapshot_fingerprint=manifest["snapshot_fingerprint"],
            challenge_fingerprint=(challenge_manifest or {}).get("challenge_fingerprint"),
            contract_fingerprint=contract_fp, config_fingerprint=config.fingerprint,
        )
        if plan["stage"] == "A" and case_ids is None:
            raise SystemExit("Stage A plan: --challenge is required (challenge cases only)")
        backend = LiveSelectorBackend(config)
        mode = "LIVE"
        subset = [s for s in snapshots if case_ids is None or s.case.case_id in case_ids]
        est = estimate(subset, max_tokens=config.max_tokens, primary_only=args.primary_only)
        print(f"LIVE RUN (plan {plan['plan_fingerprint'][:12]}): provider={config.provider} "
              f"model={config.model} reasoning={args.reasoning_effort} "
              f"planned_calls<={est['calls_per_config']} (max_cases={args.max_cases}) "
              f"est_input_tokens={est['input_tokens']['total']} ({est['tokenizer']})",
              file=sys.stderr)
    else:
        config = selector_config(provider=FAKE_PROVIDER, model=f"fake-{args.fake_policy}",
                                 max_tokens=args.max_tokens,
                                 reasoning_effort=args.reasoning_effort)
        backend = FakeSelectorProvider(args.fake_policy, config)
        mode = f"DRY_RUN_FAKE:{args.fake_policy}"
    identity = RunIdentity(
        selector_contract_fingerprint=contract_fp,
        config=config,
        snapshot_fingerprint=manifest["snapshot_fingerprint"],
        run_mode=mode,
        candidate_order=args.candidate_order,
    )

    def execute():
        return run_benchmark(snapshots, backend, identity, Path(args.out),
                             concurrency=args.concurrency, max_cases=args.max_cases,
                             retry_errors=args.retry_errors, primary_only=args.primary_only,
                             case_ids=case_ids)

    if args.live:
        # SDK allowed, sockets only to the provider's API host (no fallback).
        with no_live_calls([LIVE_PROVIDERS[config.provider]], block_sdks=False):
            summary = execute()
    else:
        with no_live_calls():
            summary = execute()
    report = _write_report(Path(args.snapshot), Path(summary.run_dir))
    if args.challenge:
        with no_live_calls():
            _write_challenge_report(Path(args.snapshot), Path(args.challenge),
                                    Path(summary.run_dir))
    _dump({"summary": summary.__dict__, "label": report["label"],
           "primary": report["primary"], "denominators": report["denominators"]})


def _write_challenge_report(snapshot_dir: Path, challenge_dir: Path, run_dir: Path) -> dict:
    from benchmarks.selector_v2.challenge import correctness, load_challenge, two_layer_report
    from benchmarks.selector_v2.evaluator import SELF_TEST_LABEL
    from benchmarks.selector_v2.runner import RESULTS_FILE, load_results
    from benchmarks.selector_v2.snapshot import load_snapshot

    manifest, snapshots = load_snapshot(snapshot_dir)
    challenge_manifest, cases = load_challenge(challenge_dir, manifest)
    results = load_results(run_dir / RESULTS_FILE).by_case
    modes = {r.run_mode for r in results.values()}
    label = SELF_TEST_LABEL if any(m.startswith("DRY_RUN_FAKE") for m in modes) else "LIVE MODEL RUN"
    report = two_layer_report(snapshots, [c["case_id"] for c in cases],
                              correctness(snapshots, results), label=label, results=results)
    report["challenge_fingerprint"] = challenge_manifest["challenge_fingerprint"]
    _dump(report, run_dir / "challenge-metrics.json")
    return report


def cmd_challenge(args) -> None:
    from benchmarks.selector_v2.challenge import (
        build_challenge, first_candidate_correctness, two_layer_report, write_challenge,
    )
    from benchmarks.selector_v2.snapshot import load_snapshot

    with no_live_calls():
        manifest, snapshots = load_snapshot(Path(args.snapshot))
        built = build_challenge(snapshots)
        baseline = two_layer_report(snapshots, [c["case_id"] for c in built["cases"]],
                                    first_candidate_correctness(snapshots),
                                    label="FIRST_CANDIDATE_BASELINE (diagnostic, not production)")
        summary = {"counts": built["counts"], "first_candidate_baseline": baseline}
        challenge_manifest = write_challenge(Path(args.out), manifest, built, summary)
    _dump({"challenge_fingerprint": challenge_manifest["challenge_fingerprint"],
           "counts": built["counts"],
           "first_candidate_full": baseline["FULL_REFERENCE_EXACT"]["exact"],
           "first_candidate_challenge": baseline["CHALLENGE_EXACT"]["exact"]})


def cmd_stage_report(args) -> None:
    from benchmarks.selector_v2.challenge import (
        case_records, error_review, load_challenge, run_summary,
    )
    from benchmarks.selector_v2.runner import RESULTS_FILE, load_results
    from benchmarks.selector_v2.snapshot import load_snapshot

    with no_live_calls():
        report = _write_challenge_report(Path(args.snapshot), Path(args.challenge),
                                         Path(args.run_dir))
        manifest, snapshots = load_snapshot(Path(args.snapshot))
        _cm, cases = load_challenge(Path(args.challenge), manifest)
        loaded = load_results(Path(args.run_dir) / RESULTS_FILE)
        records = case_records(snapshots, cases, loaded.by_case)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "case-results.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records),
            encoding="utf-8")
        _dump(error_review(records), out / "error-review.json")
        summary = {
            "run_summary": run_summary(records),
            "result_file": {"superseded_attempts": loaded.superseded,
                            "corrupt_lines_ignored": loaded.corrupt_lines,
                            "unique_cases": len(loaded.by_case)},
            "challenge": report["CHALLENGE_EXACT"],
            "paired_vs_first_candidate": report["paired_vs_first_candidate"]["CHALLENGE_EXACT"],
            "challenge_fingerprint": report["challenge_fingerprint"],
            "winner": None,
        }
        _dump(summary, out / "summary.json")
    _dump(summary["run_summary"])


def cmd_postmortem(args) -> None:
    from benchmarks.selector_v2 import postmortem as pm
    from benchmarks.selector_v2.challenge import load_challenge
    from benchmarks.selector_v2.contract import selector_contract_fingerprint
    from benchmarks.selector_v2.prompt_variants import VARIANTS, variant_contract_fingerprint
    from benchmarks.selector_v2.runner import RESULTS_FILE, load_results
    from benchmarks.selector_v2.snapshot import load_near_pairs, load_snapshot

    with no_live_calls(_infra_hosts()):
        from core.database import QnAQuery, SessionLocal

        db = SessionLocal()
        try:
            alias_rows = [(int(r[0]), r[1]) for r in db.query(QnAQuery.qna_id, QnAQuery.query_text)]
        finally:
            db.close()
    with no_live_calls():
        manifest, snapshots = load_snapshot(Path(args.snapshot))
        cm, cases = load_challenge(Path(args.challenge), manifest)
        run_dir = Path(args.run_dir)
        results = load_results(run_dir / RESULTS_FILE).by_case
        stage_queue = json.loads(Path(args.review_queue).read_text(encoding="utf-8"))
        queue = [c["case_id"] for c in stage_queue["cases"]]
        alias_map = pm.build_alias_map(alias_rows)
        pairs = load_near_pairs()
        built = pm.build_postmortem(snapshots, cases, results, alias_map, pairs, queue)
        acc = built["accounted"]
        expected = {"corruption": 74, "unresolved": 12, "rescue": 8, "false_none": 41}
        if any(acc[k] != v for k, v in expected.items()) or not acc["every_failure_has_primary"]:
            raise SystemExit(f"postmortem accounting mismatch: {acc}")
        split = pm.stratified_split(cases)
        split["production_stage_a"] = {
            "DEV": pm._metrics_for(split["dev"], built["packets"]),
            "HOLDOUT": pm._metrics_for(split["holdout"], built["packets"]),
        }
        dev_failures = [f for f in built["failures"] if f["case_id"] in set(split["dev"])]
        split["dev_failure_primary_distribution"] = pm._counter(
            f["classification"]["primary"] for f in dev_failures)
        split["holdout_policy"] = ("HOLDOUT case texts/ids must not be used to design prompts; "
                                   "prompt variants cite only DEV category distributions")
        summary = built["summary"]
        summary["alias_structure"] = {
            "full_primary": pm.alias_structure(snapshots, alias_map),
            "challenge": pm.alias_structure(snapshots, alias_map, {c["case_id"] for c in cases}),
            "alias_table_fingerprint": fingerprint(sorted(alias_rows)),
        }
        summary["exact_alias_candidate_cases"] = summary["alias_structure"]["challenge"]
        summary["inputs"] = {
            "snapshot_fingerprint": manifest["snapshot_fingerprint"],
            "challenge_fingerprint": cm["challenge_fingerprint"],
            "stage_a_run_id": run_dir.name,
            "production_selector_contract_fingerprint": selector_contract_fingerprint(),
        }
        n_dev, n_hold = len(split["dev"]), len(split["holdout"])
        plan = {
            "plan_kind": "prompt-experiment-proposal", "live": False, "approved": False,
            "provider_policy": "OpenRouter only",
            "model": "openrouter / openai/gpt-4o-mini (current production model)",
            "split_fingerprint": split["split_fingerprint"],
            "snapshot_fingerprint": manifest["snapshot_fingerprint"],
            "challenge_fingerprint": cm["challenge_fingerprint"],
            "variants": {name: {"contract_fingerprint": variant_contract_fingerprint(text),
                                "chars": len(text)} for name, text in VARIANTS.items()},
            "matrix": [
                {"step": 1, "config": "current model + production prompt", "case_set": "DEV+HOLDOUT",
                 "new_calls": 0, "note": "already measured in Stage A (run 9dfc72c140dc7e93)"},
                {"step": 2, "config": "current model + variant_a_practical_qualifier",
                 "case_set": "DEV", "new_calls": n_dev},
                {"step": 3, "config": "current model + variant_b_none_threshold_ablation",
                 "case_set": "DEV", "new_calls": n_dev},
                {"step": 4, "config": "one variant chosen by human review of steps 2-3",
                 "case_set": "HOLDOUT", "new_calls": n_hold,
                 "note": "separate approval; HOLDOUT is scored once"},
            ],
            "total_new_calls_if_all_steps": 2 * n_dev + n_hold,
            "cost": "PRICE_REQUIRED",
            "prerequisite": "harness support for running a variant system prompt "
                            "(contract fingerprint = variant fingerprint); not yet implemented",
            "later": {
                "stronger_model": "best prompt + same HOLDOUT, after registering a model "
                                  "available on OpenRouter (no id proposed here)",
                "reasoning": "only after a reasoning-capable OpenRouter model is registered "
                             "and a prompt baseline exists",
            },
            "stage_b_full_current_config": "DO_NOT_RUN_FULL_CURRENT_CONFIG",
        }
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)

        def jsonl(name, rows):
            (out / name).write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                                            for r in rows), encoding="utf-8")

        jsonl("failure-cases.jsonl", built["failures"])
        jsonl("false-none.jsonl", built["false_none"])
        jsonl("specificity-cases.jsonl", built["specificity"])
        jsonl("rescues.jsonl", built["rescues"])
        _dump({"review_queue": built["review"], "note": "recommendations only; Gold unchanged"},
              out / "review-queue.json")
        _dump(summary, out / "taxonomy-summary.json")
        _dump(split, out / "prompt-experiment-split.json")
        _dump(plan, out / "experiment-plan.json")
    _dump({"accounted": acc, "corruption": summary["corruption_primary"],
           "unresolved": summary["unresolved_primary"], "false_none": summary["false_none_primary"],
           "groups": {k: v["count"] for k, v in summary["mismatch_groups"].items()},
           "split": {"dev": len(split["dev"]), "holdout": len(split["holdout"]),
                     "fp": split["split_fingerprint"]}})


def _prompt_inputs(args):
    from benchmarks.selector_v2 import prompt_experiment as px
    from benchmarks.selector_v2.challenge import load_challenge
    from benchmarks.selector_v2.snapshot import load_snapshot

    manifest, snapshots = load_snapshot(Path(args.snapshot))
    cm, cases = load_challenge(Path(args.challenge), manifest)
    reference = None
    if getattr(args, "postmortem", None):
        reference = json.loads((Path(args.postmortem) / "prompt-experiment-split.json")
                               .read_text(encoding="utf-8"))
    split = px.load_split(challenge_ids={c["case_id"] for c in cases}, reference=reference)
    return manifest, snapshots, cm, cases, split


def cmd_prompt_run(args) -> None:
    """Run ONE benchmark prompt on DEV or HOLDOUT (only the system prompt varies)."""
    from benchmarks.selector_v2 import prompt_experiment as px
    from benchmarks.selector_v2.prompt_contract import (
        PRODUCTION, load_prompt, serializer_contract_fingerprint,
    )
    from benchmarks.selector_v2.providers import (
        FAKE_PROVIDER, FakeSelectorProvider, LiveSelectorBackend, selector_config,
    )
    from benchmarks.selector_v2.runner import RunIdentity, run_benchmark

    check_live_gate(live=args.live, confirmed=args.confirm_live_provider_calls,
                    provider=args.provider, model=args.model)
    prompt = load_prompt(args.prompt)
    if prompt.prompt_id == PRODUCTION:
        raise SystemExit("production prompt is not rerun: reuse the Stage A run as the baseline")
    with no_live_calls():
        manifest, snapshots, _cm, _cases, split = _prompt_inputs(args)
    serializer_fp = serializer_contract_fingerprint()
    if args.case_set == "holdout":
        px.holdout_guard(prompt=prompt, selected_winner=args.selected_dev_winner,
                         gate_report_path=args.dev_gate_report and Path(args.dev_gate_report),
                         split_fp=split["split_fingerprint"], live=args.live,
                         plan_path=args.holdout_plan and Path(args.holdout_plan),
                         approved_fp=args.approve_holdout_plan_fingerprint)
        case_ids = set(split["holdout"])
    else:
        case_ids = set(split["dev"])
    if args.live:
        config = selector_config(provider=args.provider, model=args.model,
                                 temperature=args.temperature, max_tokens=args.max_tokens,
                                 reasoning_effort=args.reasoning_effort)
        if args.case_set == "dev":
            px.validate_dev_plan(args.live_plan and Path(args.live_plan),
                                 args.approve_plan_fingerprint,
                                 snapshot_fp=manifest["snapshot_fingerprint"],
                                 split_fp=split["split_fingerprint"], serializer_fp=serializer_fp,
                                 prompt=prompt, config=config)
        backend = LiveSelectorBackend(config, prompt=prompt)
        mode = "LIVE"
    else:
        config = selector_config(provider=FAKE_PROVIDER, model=f"fake-{args.fake_policy}",
                                 max_tokens=args.max_tokens)
        backend = FakeSelectorProvider(args.fake_policy, config, prompt=prompt)
        mode = f"DRY_RUN_FAKE:{args.fake_policy}"
    identity = RunIdentity(
        selector_contract_fingerprint=serializer_fp, config=config,
        snapshot_fingerprint=manifest["snapshot_fingerprint"], run_mode=mode,
        prompt_fingerprint=prompt.fingerprint, split_fingerprint=split["split_fingerprint"],
    )

    def execute():
        return run_benchmark(snapshots, backend, identity, Path(args.out),
                             concurrency=args.concurrency, max_cases=args.max_cases,
                             retry_errors=args.retry_errors, primary_only=True,
                             case_ids=case_ids)

    if args.live:
        with no_live_calls([LIVE_PROVIDERS[config.provider]], block_sdks=False):
            summary = execute()
    else:
        with no_live_calls():
            summary = execute()
    _dump({"summary": summary.__dict__, "prompt": prompt.to_dict(), "case_set": args.case_set})


def _dev_context(args):
    from benchmarks.selector_v2 import prompt_experiment as px
    from benchmarks.selector_v2.runner import RESULTS_FILE, load_results

    manifest, snapshots, cm, cases, split = _prompt_inputs(args)
    membership = {c["case_id"]: c["membership"] for c in cases}
    failures = [json.loads(line) for line in (Path(args.postmortem) / "failure-cases.jsonl")
                .read_text(encoding="utf-8").splitlines() if line.strip()]
    slices = px.taxonomy_slices(failures)
    side = {**{i: "DEV" for i in split["dev"]}, **{i: "HOLDOUT" for i in split["holdout"]}}
    baseline = load_results(Path(args.stage_a_run) / RESULTS_FILE).by_case
    return manifest, snapshots, cm, cases, split, membership, slices, side, baseline


def cmd_prompt_prep(args) -> None:
    """Offline: verify inputs, production DEV baseline, DEV plan, prompt diff."""
    from benchmarks.selector_v2 import prompt_experiment as px
    from benchmarks.selector_v2.challenge import selector_value
    from benchmarks.selector_v2.contract import selector_contract_fingerprint
    from benchmarks.selector_v2.prompt_contract import (
        PROMPT_IDS, load_committed_manifest, load_prompt, prompt_manifest,
        serializer_contract_fingerprint,
    )
    from benchmarks.selector_v2.providers import selector_config
    from benchmarks.selector_v2.tokens import estimate

    with no_live_calls():
        manifest, snapshots, cm, cases, split, membership, slices, side, baseline = _dev_context(args)
        live_manifest = prompt_manifest()
        if live_manifest != load_committed_manifest():
            raise SystemExit("prompt manifest drift: a prompt or the serializer changed")
        prompts = {pid: load_prompt(pid) for pid in PROMPT_IDS}
        dev = split["dev"]
        prod = px.dev_report(snapshots, dev, baseline, membership, slices,
                             label="production (Stage A reuse)", split_side=side)
        holdout_prod = px.dev_report(snapshots, split["holdout"], baseline, membership, slices,
                                     label="production HOLDOUT (Stage A, reference only)",
                                     split_side=side)
        by_id = {s.case.case_id: s for s in snapshots}
        fc = px.first_candidate_results(snapshots, dev)
        first_candidate = {"exact": px._acc(dev, fc),
                           "note": "diagnostic comparator only; not the prompt-selection standard"}
        dev_snaps = [by_id[i] for i in dev]
        config = selector_config(provider="openrouter", model="openai/gpt-4o-mini")
        prod_est = estimate(dev_snaps, max_tokens=config.max_tokens, primary_only=True)
        actual = sum(baseline[i].input_tokens or 0 for i in dev)
        calibration = {"factor": round(actual / prod_est["input_tokens"]["total"], 4),
                       "source": "Stage A actual OpenRouter input tokens on DEV / "
                                 "approximate estimate of the production prompt on DEV",
                       "stage_a_dev_actual_input_tokens": actual,
                       "production_prompt_dev_estimate": prod_est["input_tokens"]["total"]}
        plan = px.build_dev_plan(
            snapshot_manifest=manifest, challenge_manifest=cm, split=split,
            serializer_fp=serializer_contract_fingerprint(), config=config,
            prompts=[prompts["variant_a_v1"], prompts["variant_b_v1"]],
            production_prompt=prompts["production"], dev_snapshots=dev_snaps,
            baseline_run_id=Path(args.stage_a_run).name, calibration=calibration)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        _dump(plan, out / "live-plan-dev.json")
        _dump({"production_dev": prod, "production_holdout_reference": holdout_prod,
               "first_candidate_dev": first_candidate,
               "taxonomy_slices": {k: {"all": len(v), "dev": sum(side[i] == "DEV" for i in v),
                                       "holdout": sum(side[i] == "HOLDOUT" for i in v)}
                                   for k, v in slices.items()},
               "selector_contract_fingerprint": selector_contract_fingerprint(),
               "prompts": live_manifest, "split_fingerprint": split["split_fingerprint"],
               "selection_gate_definition": list(px.selection_gate(prod, prod)["checks"])},
              out / "prep-summary.json")
        (out / "prompt-diff.md").write_text(px.prompt_diff_markdown(prompts), encoding="utf-8")
    _dump({"plan_fingerprint": plan["plan_fingerprint"], "calls": plan["calls"],
           "tokens": plan["estimated_tokens_total"],
           "production_dev": {"exact": prod["exact"], "value": prod["selector_value"],
                              "false_none": prod["false_none"]},
           "first_candidate_dev": first_candidate["exact"]})


def cmd_prompt_dev_eval(args) -> None:
    """DEV report + selection gate for one variant run (no winner is chosen)."""
    from benchmarks.selector_v2 import prompt_experiment as px
    from benchmarks.selector_v2.runner import RESULTS_FILE, RUN_MANIFEST, load_results

    with no_live_calls():
        manifest, snapshots, cm, cases, split, membership, slices, side, baseline = _dev_context(args)
        run_dir = Path(args.run_dir)
        identity = json.loads((run_dir / RUN_MANIFEST).read_text(encoding="utf-8"))
        if identity.get("split_fingerprint") != split["split_fingerprint"]:
            raise SystemExit("run belongs to another split")
        results = load_results(run_dir / RESULTS_FILE).by_case
        dev = split["dev"]
        prod = px.dev_report(snapshots, dev, baseline, membership, slices,
                             label="production (Stage A reuse)", split_side=side)
        variant = px.dev_report(snapshots, dev, results, membership, slices,
                                label=identity.get("prompt_fingerprint", "?"), split_side=side)
        report = {"prompt_fingerprint": identity.get("prompt_fingerprint"),
                  "split_fingerprint": split["split_fingerprint"],
                  "run_id": identity.get("run_id"), "run_mode": identity.get("run_mode"),
                  "variant_dev": variant, "production_dev": prod,
                  "gate": px.selection_gate(variant, prod), "winner": None}
        _dump(report, run_dir / "dev-gate-report.json")
    _dump({"gate": report["gate"], "exact": variant["exact"], "value": variant["selector_value"]})


def cmd_prompt_dev_compare(args) -> None:
    """Production (Stage A reuse) vs variant runs on DEV: paired stats, diffs, gates."""
    from benchmarks.selector_v2 import prompt_experiment as px
    from benchmarks.selector_v2.runner import RESULTS_FILE, RUN_MANIFEST, load_results

    with no_live_calls():
        manifest, snapshots, cm, cases, split, membership, slices, side, baseline = _dev_context(args)
        dev = split["dev"]
        runs, reports, identities = {"production": baseline}, {}, {}
        for item in args.run:
            name, _, path = item.partition("=")
            run_dir = Path(path)
            identity = json.loads((run_dir / RUN_MANIFEST).read_text(encoding="utf-8"))
            if identity.get("split_fingerprint") != split["split_fingerprint"]:
                raise SystemExit(f"{name}: run belongs to another split")
            loaded = load_results(run_dir / RESULTS_FILE)
            runs[name] = loaded.by_case
            identities[name] = {**identity, "superseded": loaded.superseded,
                                "corrupt_lines": loaded.corrupt_lines,
                                "unique_cases": len(loaded.by_case)}
        for name, results in runs.items():
            reports[name] = px.dev_report(snapshots, dev, results, membership, slices,
                                          label=name, split_side=side)
        prod = reports["production"]
        gates = {n: px.selection_gate(r, prod) for n, r in reports.items() if n != "production"}
        deltas = {n: {"exact": r["exact"]["correct"] - prod["exact"]["correct"],
                      "corruption": r["selector_value"]["corruption_count"]
                      - prod["selector_value"]["corruption_count"],
                      "false_none": r["false_none"] - prod["false_none"],
                      "net_corrections": r["selector_value"]["net_corrections"]
                      - prod["selector_value"]["net_corrections"]}
                  for n, r in reports.items() if n != "production"}
        comparison = px.compare_runs(snapshots, dev, runs)
        fc = px.first_candidate_results(snapshots, dev)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        summary = {"identities": identities, "reports": reports, "deltas_vs_production": deltas,
                   "gates": gates, "paired": comparison["paired"],
                   "diff_counts": comparison["diff_counts"],
                   "first_candidate_dev": px._acc(dev, fc),
                   "passing_variants": sorted(n for n, g in gates.items() if g["passed"]),
                   "winner": None, "cost": "NOT_CALCULATED — no explicit pricing inputs"}
        _dump(summary, out / "dev-comparison.json")
        _dump(comparison["diffs"], out / "case-diffs.json")
    _dump({"deltas": deltas, "gates": {n: g["passed"] for n, g in gates.items()},
           "paired": comparison["paired"], "diff_counts": comparison["diff_counts"]})


def cmd_adjudication_prep(args) -> None:
    """Blind semantic Gold review packet (offline; alias table read-only)."""
    from benchmarks.selector_v2 import adjudication as adj
    from benchmarks.selector_v2 import postmortem as pm
    from benchmarks.selector_v2.challenge import load_challenge
    from benchmarks.selector_v2.contract import sha256_text
    from benchmarks.selector_v2.snapshot import load_near_pairs, load_snapshot

    with no_live_calls(_infra_hosts()):
        from core.database import QnAQuery, SessionLocal

        db = SessionLocal()
        try:
            alias_rows = [(int(r[0]), r[1]) for r in db.query(QnAQuery.qna_id, QnAQuery.query_text)]
        finally:
            db.close()
    with no_live_calls():
        manifest, snapshots = load_snapshot(Path(args.snapshot))
        cm, cases = load_challenge(Path(args.challenge), manifest)
        pdir = Path(args.postmortem)
        failure_text = (pdir / "failure-cases.jsonl").read_text(encoding="utf-8")
        failures = [json.loads(line) for line in failure_text.splitlines() if line.strip()]
        postmortem_fp = fingerprint({
            "failure-cases.jsonl": sha256_text(failure_text),
            "taxonomy-summary.json": sha256_text(
                (pdir / "taxonomy-summary.json").read_text(encoding="utf-8")),
        })
        scope = adj.review_scope(failures, [c["case_id"] for c in cases])
        alias_map = pm.build_alias_map(alias_rows)
        by_id = {s.case.case_id: s for s in snapshots}
        owners = {cid: pm.alias_owners(alias_map, by_id[cid].case.intent_text or "")
                  for cid in scope["case_ids"]}
        packet = adj.build_packet(snapshots, scope, load_near_pairs(), owners)
        problems = adj.primary_view_violations(packet["primary"], ("variant_a_v1", "variant_b_v1"))
        if problems:
            raise SystemExit(f"primary view contaminated: {problems[:10]}")
        packet_fp = adj.packet_fingerprint(scope, packet["content_hashes"],
                                           manifest["snapshot_fingerprint"], postmortem_fp)
        files = {
            "review-cases.jsonl": "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                                          for r in packet["primary"]),
            "review-template.csv": adj.review_template_csv(packet["primary"]),
            "review-packet.md": adj.review_markdown(packet["primary"]),
            "audit-view.jsonl": "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                                        for r in packet["audit"]),
            "README.md": adj.README.format(schema=adj.REVIEW_SCHEMA_VERSION),
        }
        out = Path(args.out)
        hashes = adj.write_immutable(out, files)
        manifest_out = {
            "review_schema_version": adj.REVIEW_SCHEMA_VERSION,
            "review_packet_fingerprint": packet_fp,
            "source_snapshot_fingerprint": manifest["snapshot_fingerprint"],
            "source_challenge_fingerprint": cm["challenge_fingerprint"],
            "postmortem_fingerprint": postmortem_fp,
            "alias_table_fingerprint": fingerprint(sorted(alias_rows)),
            "case_ids": scope["case_ids"],
            "scope_counts": scope["counts"],
            "control_case_ids": scope["control"],
            "clear_selector_error_diagnostic_not_in_scope": scope["clear_selector_error_diagnostic"],
            "candidate_content_sha256": packet["content_hashes"],
            "candidate_policy": {"gold": "all current acceptable refs present",
                                 "near_qna": "all near-pair siblings present",
                                 "top_retrieved": adj.TOP_RETRIEVED,
                                 "top_lexical": adj.TOP_LEXICAL, "max_shown": adj.MAX_SHOWN,
                                 "never_used": "any model decision (production / variants)",
                                 "order": "sha256(case_id|candidate_ref), rank-neutral"},
            "candidate_view_incomplete_cases": [r["case_id"] for r in packet["primary"]
                                                if not r["candidate_view_complete"]],
            "artifact_sha256": hashes,
            "human_review_status": "WAITING_FOR_HUMAN_REVIEW",
        }
        adj.write_immutable(out, {"manifest.json": json.dumps(
            manifest_out, ensure_ascii=False, indent=2, sort_keys=True) + "\n"})
    _dump({"review_packet_fingerprint": packet_fp, "scope": scope["counts"],
           "incomplete_views": len(manifest_out["candidate_view_incomplete_cases"])})


def cmd_adjudication_apply(args) -> None:
    """Locked blind decisions -> CHILD Gold (parent untouched)."""
    from benchmarks.selector_v2 import adjudication as adj
    from benchmarks.selector_v2.snapshot import load_snapshot

    with no_live_calls():
        manifest, snapshots = load_snapshot(Path(args.snapshot))
        review_manifest, audit = adj.load_locked_review(Path(args.review_dir))
        rows = adj.read_decisions(Path(args.decisions).read_text(encoding="utf-8"))
        result = adj.apply_adjudication(
            [s.case for s in snapshots], audit, rows,
            packet_fp=review_manifest["review_packet_fingerprint"],
            expected_packet_fp=args.packet_fingerprint,
            parent_fp=manifest["snapshot_fingerprint"])
        adj.write_immutable(Path(args.out), {
            "cases.jsonl": "".join(c.model_dump_json() + "\n" for c in result["cases"]),
            "provenance.jsonl": "".join(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n"
                                        for p in result["provenance"]),
            "manifest.json": json.dumps({k: v for k, v in result.items()
                                         if k not in ("cases", "provenance")},
                                        ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        })
    _dump({k: v for k, v in result.items() if k not in ("cases", "provenance")})


def cmd_adjudication_rescore(args) -> None:
    """Re-score SAVED outputs against a child Gold. No provider call."""
    from benchmarks.selector_v2 import adjudication as adj
    from benchmarks.selector_v2.evaluator import metrics
    from benchmarks.selector_v2.runner import RESULTS_FILE, load_results
    from benchmarks.selector_v2.schema import BenchmarkCase
    from benchmarks.selector_v2.snapshot import load_snapshot

    with no_live_calls():
        _manifest, snapshots = load_snapshot(Path(args.snapshot))
        child = [BenchmarkCase.model_validate_json(line) for line in
                 (Path(args.child_dir) / "cases.jsonl").read_text(encoding="utf-8").splitlines()
                 if line.strip()]
        report = {}
        for item in args.run:
            name, _, path = item.partition("=")
            results = load_results(Path(path) / RESULTS_FILE).by_case
            new_snaps, new_results = adj.rescore(snapshots, child, results)
            scoped = [s for s in new_snaps if s.case.case_id in new_results and s.selector_evaluable]
            report[name] = {"parent": metrics([s for s in snapshots if s.case.case_id in results
                                               and s.selector_evaluable], results),
                            "child": metrics(scoped, new_results)}
        _dump(report, Path(args.out) if args.out else None)


def cmd_adjudication_followup(args) -> None:
    """Second blind pass: ALL candidates for first-pass NEED_FULL_CANDIDATES cases.

    Offline; never reads audit-view.jsonl; never changes Gold or decisions."""
    from benchmarks.selector_v2 import adjudication as adj
    from benchmarks.selector_v2.challenge import load_challenge
    from benchmarks.selector_v2.contract import sha256_text
    from benchmarks.selector_v2.gold_loader import GOLD_ALL, sha256_file
    from benchmarks.selector_v2.snapshot import load_snapshot

    with no_live_calls():
        review_dir = Path(args.review_dir)
        parent, primary, template = adj.load_primary_review(review_dir)
        filled_text = Path(args.filled).read_text(encoding="utf-8")
        filled = adj.read_decisions(filled_text.lstrip("\ufeff"))
        problems = adj.validate_first_pass(filled, template, primary)
        if problems:
            raise SystemExit(f"first-pass decisions rejected: {problems[:10]}")
        need = [r["case_id"] for r in filled
                if (r.get("blind_decision") or "").strip() == "NEED_FULL_CANDIDATES"]
        completed = [r["case_id"] for r in filled
                     if (r.get("blind_decision") or "").strip() not in ("", "NEED_FULL_CANDIDATES")]
        manifest, snapshots = load_snapshot(Path(args.snapshot))
        cm, _cases = load_challenge(Path(args.challenge), manifest)
        if parent["source_snapshot_fingerprint"] != manifest["snapshot_fingerprint"] or \
                parent["source_challenge_fingerprint"] != cm["challenge_fingerprint"]:
            raise SystemExit("snapshot/challenge differ from the parent review packet")
        gold_sha = manifest["dataset"]["source"]["gold_all_sha256"]
        gold_verified = None
        if args.gold_dir:
            gold_verified = sha256_file(Path(args.gold_dir) / GOLD_ALL) == gold_sha
            if not gold_verified:
                raise SystemExit("reviewed Gold file differs from the snapshot manifest")
        records = adj.build_followup(snapshots, primary, need)
        csv_text = adj.followup_template_csv(records)
        md_text = adj.followup_markdown(records)
        readme = adj.FOLLOWUP_README.format(schema=adj.FOLLOWUP_SCHEMA_VERSION)
        violations = adj.followup_visible_violations(records, csv_text, md_text, readme)
        if violations:
            raise SystemExit(f"follow-up packet contaminated: {violations[:10]}")
        files = {
            "review-cases-full.jsonl": "".join(json.dumps(r, ensure_ascii=False, sort_keys=True)
                                               + "\n" for r in records),
            "review-template-full.csv": csv_text,
            "review-packet-full.md": md_text,
            "README.md": readme,
            "first-pass-decisions.csv": filled_text,
        }
        out = Path(args.out)
        hashes = adj.write_immutable(out, files)
        fp = adj.followup_fingerprint(parent["review_packet_fingerprint"],
                                      manifest["snapshot_fingerprint"], records)
        manifest_out = {
            "followup_schema_version": adj.FOLLOWUP_SCHEMA_VERSION,
            "followup_fingerprint": fp,
            "parent_review_packet_fingerprint": parent["review_packet_fingerprint"],
            "source_snapshot_fingerprint": manifest["snapshot_fingerprint"],
            "source_challenge_fingerprint": cm["challenge_fingerprint"],
            "reviewed_gold_all_sha256": gold_sha,
            "reviewed_gold_file_verified": gold_verified,
            "case_ids": need,
            "case_count": len(need),
            "candidate_counts": {r["case_id"]: r["candidate_count"] for r in records},
            "candidate_content_sha256": {
                r["case_id"]: [sha256_text(json.dumps(c, ensure_ascii=False, sort_keys=True))
                               for c in r["candidates"]] for r in records},
            "neutral_ordering": "sha256(case_id|candidate_ref) ascending; labels identical "
                                "to the first pass",
            "allowed_decisions": list(adj.FOLLOWUP_DECISIONS),
            "first_pass": {"file_sha256": sha256_text(filled_text),
                           "completed_case_ids": completed,
                           "completed_count": len(completed),
                           "needs_full_candidates": len(need)},
            "audit_view_read": False,
            "artifact_sha256": hashes,
            "human_review_status": "READY_FOR_HUMAN_REVIEW",
        }
        adj.write_immutable(out, {"manifest.json": json.dumps(
            manifest_out, ensure_ascii=False, indent=2, sort_keys=True) + "\n"})
    _dump({"followup_fingerprint": fp, "case_count": len(need),
           "first_pass_completed": len(completed),
           "candidate_count_min": min(r["candidate_count"] for r in records),
           "candidate_count_max": max(r["candidate_count"] for r in records),
           "reviewed_gold_file_verified": gold_verified})


def cmd_challenge_eval(args) -> None:
    with no_live_calls():
        report = _write_challenge_report(Path(args.snapshot), Path(args.challenge),
                                         Path(args.run_dir))
    _dump({"label": report["label"],
           "FULL_REFERENCE_EXACT": report["FULL_REFERENCE_EXACT"]["exact"],
           "CHALLENGE_EXACT": report["CHALLENGE_EXACT"]["exact"],
           "challenge_selector_value": report["CHALLENGE_EXACT"]["selector_value"]})


def cmd_registry_models(args) -> None:
    from benchmarks.selector_v2.plan import discover_selector_models

    with no_live_calls(_infra_hosts()):
        from core.database import SessionLocal
        from core.deps import provider_key_configured
        from services.ai_registry import SUPPORTED_PROVIDERS, list_models

        db = SessionLocal()
        try:
            models = [m.to_dict() for m in list_models(db)]
            keys = {p: provider_key_configured(p, db) for p in SUPPORTED_PROVIDERS}
        finally:
            db.close()
    _dump({"provider_key_present": keys,
           "models": discover_selector_models(models, keys)},
          Path(args.out) if args.out else None)


def _parse_prices(args) -> dict:
    prices = {}
    for item in args.price or []:
        name, _, value = item.partition("=")
        inp, _, out = value.partition(":")
        prices[name] = (float(inp), float(out))
    if args.input_price_per_1m is not None and args.output_price_per_1m is not None:
        prices["*"] = (args.input_price_per_1m, args.output_price_per_1m)
    return prices


def cmd_live_plan(args) -> None:
    from benchmarks.selector_v2.challenge import load_challenge
    from benchmarks.selector_v2.contract import selector_contract_fingerprint
    from benchmarks.selector_v2.plan import build_live_plan, write_plan
    from benchmarks.selector_v2.snapshot import load_snapshot
    from benchmarks.selector_v2.tokens import estimate

    with no_live_calls():
        manifest, snapshots = load_snapshot(Path(args.snapshot))
        challenge_manifest, cases = load_challenge(Path(args.challenge), manifest)
        registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
        production = json.loads(Path(args.production_config).read_text(encoding="utf-8"))
        ids = {c["case_id"] for c in cases}
        max_tokens = production["selector"]["max_tokens"]
        challenge_est = estimate([s for s in snapshots if s.case.case_id in ids],
                                 max_tokens=max_tokens, primary_only=True)
        full_est = estimate(snapshots, max_tokens=max_tokens, primary_only=True)
        plan = build_live_plan(
            snapshot_manifest=manifest, challenge_manifest=challenge_manifest,
            contract_fingerprint=selector_contract_fingerprint(),
            production_selector=production["selector"], discovered=registry["models"],
            challenge_estimate=challenge_est, full_estimate=full_est,
            prior_models=args.prior_model or [], prices=_parse_prices(args),
        )
        plan["token_estimates"] = {"challenge_per_config": challenge_est,
                                   "full_per_config": full_est}
        from benchmarks.selector_v2.plan import plan_fingerprint

        plan["plan_fingerprint"] = plan_fingerprint(plan)
        write_plan(Path(args.out), plan)
    _dump({"plan_fingerprint": plan["plan_fingerprint"], "stage_a": plan["stage_a"],
           "configs": [c["config_id"] for c in plan["proposed_configs"]],
           "blocked": plan["blocked_or_unregistered"]})


def cmd_evaluate(args) -> None:
    with no_live_calls():
        report = _write_report(Path(args.snapshot), Path(args.run_dir))
    _dump({"label": report["label"], "primary": report["primary"],
           "denominators": report["denominators"]})


def cmd_compare(args) -> None:
    from benchmarks.selector_v2.runner import RESULTS_FILE, load_results
    from benchmarks.selector_v2.snapshot import load_snapshot
    from benchmarks.selector_v2.stats import paired_compare

    with no_live_calls():
        _manifest, snapshots = load_snapshot(Path(args.snapshot))
        by_case = {s.case.case_id: s for s in snapshots if s.selector_evaluable}
        a = load_results(Path(args.run_a) / RESULTS_FILE).by_case
        b = load_results(Path(args.run_b) / RESULTS_FILE).by_case
        result = paired_compare(by_case, a, b, primary_only=not args.all_cases)
    _dump(result, Path(args.out) if args.out else None)


def cmd_config_snapshot(args) -> None:
    with no_live_calls(_infra_hosts()):
        from core.database import SessionLocal
        from services.ai_registry import load_active_config

        db = SessionLocal()
        try:
            active = load_active_config(db)
        finally:
            db.close()
    if active is None:
        _dump({"status": "NOT_CONFIGURED"})
        return
    out = {"status": "OK", "version_id": active.version_id}
    for cap, assignment in active.assignments.items():
        config = assignment.effective_config()
        out[cap.value] = {**config.to_dict(), "config_fingerprint": config.fingerprint,
                          "registry_model_id": assignment.model.id,
                          "qualification_status": assignment.model.qualification_status.value}
    _dump(out, Path(args.out) if args.out else None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.selector_v2",
                                     description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def dataset_args(p):
        p.add_argument("--gold-dir", help="reviewed Gold layer dir (gold-reviewed-all.jsonl)")
        p.add_argument("--session-dir", help="session Gold dir (session-targets.jsonl)")
        p.add_argument("--cases", help="generic BenchmarkCase JSONL (alternative)")

    p = sub.add_parser("load-gold", help="validate + convert a dataset (offline)")
    dataset_args(p)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("snapshot", help="freeze candidate sets (read-only DB/Meili/Qdrant, no LLM)")
    dataset_args(p)
    p.add_argument("--out", required=True)
    p.add_argument("--as-of", help="routing-guard date (YYYY-MM-DD, default today)")
    p.add_argument("--max-candidates", type=int, help="default: production SELECTOR_MAX_CANDIDATES")
    p.add_argument("--architecture-doc", default=str(DEFAULT_ARCH_DOC))
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("estimate", help="pre-run token (and optional cost) estimate")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--max-tokens", type=int, default=32)
    p.add_argument("--input-price-per-1m", type=float)
    p.add_argument("--output-price-per-1m", type=float)
    p.add_argument("--configs", type=int, default=1, help="number of configs planned")
    p.add_argument("--primary-only", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("run", help="run the selector (default: dry run with a fake provider)")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--out", required=True, help="results root (runs/<run_id>/ is created)")
    p.add_argument("--fake-policy", default="oracle",
                   help="dry-run policy: oracle|always_none|first_candidate|malformed|"
                        "unknown_ref|empty|timeout|model_error")
    p.add_argument("--live", action="store_true", help="real provider calls (costs money)")
    p.add_argument(CONFIRM_FLAG, dest="confirm_live_provider_calls", action="store_true")
    p.add_argument("--provider")
    p.add_argument("--model")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=32)
    p.add_argument("--timeout-seconds", type=float)
    p.add_argument("--max-retries", type=int)
    p.add_argument("--reasoning-effort", choices=["none", "low", "medium", "high"])
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--max-cases", type=int)
    p.add_argument("--retry-errors", action="store_true",
                   help="re-run cases whose final outcome was MODEL_ERROR/TIMEOUT")
    p.add_argument("--candidate-order", default="production",
                   help="production | permute:<seed> (position-bias extension point)")
    p.add_argument("--primary-only", action="store_true")
    p.add_argument("--challenge", help="restrict to a frozen challenge artifact dir")
    p.add_argument("--live-plan", help="approved live plan (required with --live)")
    p.add_argument("--approve-plan-fingerprint", help="explicit plan_fingerprint approval")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("challenge", help="freeze the selector challenge set (offline)")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_challenge)

    p = sub.add_parser("challenge-eval", help="two-layer FULL/CHALLENGE report for a run")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--challenge", required=True)
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=cmd_challenge_eval)

    p = sub.add_parser("stage-report", help="case-level + error-review artifacts for a run")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--challenge", required=True)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_stage_report)

    p = sub.add_parser("prompt-prep", help="offline prompt-experiment prep (DEV plan, baseline, diff)")
    for name in ("--snapshot", "--challenge", "--stage-a-run", "--postmortem", "--out"):
        p.add_argument(name, required=True)
    p.set_defaults(func=cmd_prompt_prep)

    p = sub.add_parser("prompt-run", help="run one benchmark prompt on DEV/HOLDOUT (gated)")
    for name in ("--snapshot", "--challenge", "--out"):
        p.add_argument(name, required=True)
    p.add_argument("--postmortem", help="postmortem dir (split cross-check)")
    p.add_argument("--prompt", required=True, help="variant_a_v1 | variant_b_v1")
    p.add_argument("--case-set", required=True, choices=["dev", "holdout"])
    p.add_argument("--fake-policy", default="oracle")
    p.add_argument("--live", action="store_true")
    p.add_argument(CONFIRM_FLAG, dest="confirm_live_provider_calls", action="store_true")
    p.add_argument("--provider")
    p.add_argument("--model")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=32)
    p.add_argument("--reasoning-effort", choices=["low", "medium", "high"])
    p.add_argument("--live-plan")
    p.add_argument("--approve-plan-fingerprint")
    p.add_argument("--selected-dev-winner")
    p.add_argument("--dev-gate-report")
    p.add_argument("--holdout-plan")
    p.add_argument("--approve-holdout-plan-fingerprint")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--max-cases", type=int)
    p.add_argument("--retry-errors", action="store_true")
    p.set_defaults(func=cmd_prompt_run)

    p = sub.add_parser("prompt-dev-eval", help="DEV report + selection gate for a variant run")
    for name in ("--snapshot", "--challenge", "--stage-a-run", "--postmortem", "--run-dir"):
        p.add_argument(name, required=True)
    p.set_defaults(func=cmd_prompt_dev_eval)

    p = sub.add_parser("prompt-dev-compare", help="production vs variant DEV comparison")
    for name in ("--snapshot", "--challenge", "--stage-a-run", "--postmortem", "--out"):
        p.add_argument(name, required=True)
    p.add_argument("--run", action="append", required=True, help="name=run_dir (repeatable)")
    p.set_defaults(func=cmd_prompt_dev_compare)

    p = sub.add_parser("adjudication-prep", help="blind semantic Gold review packet (offline)")
    for name in ("--snapshot", "--challenge", "--postmortem", "--out"):
        p.add_argument(name, required=True)
    p.set_defaults(func=cmd_adjudication_prep)

    p = sub.add_parser("adjudication-apply", help="locked human decisions -> child Gold")
    for name in ("--snapshot", "--review-dir", "--decisions", "--packet-fingerprint", "--out"):
        p.add_argument(name, required=True)
    p.set_defaults(func=cmd_adjudication_apply)

    p = sub.add_parser("adjudication-rescore", help="re-score saved outputs on a child Gold")
    for name in ("--snapshot", "--child-dir"):
        p.add_argument(name, required=True)
    p.add_argument("--run", action="append", required=True, help="name=run_dir")
    p.add_argument("--out")
    p.set_defaults(func=cmd_adjudication_rescore)

    p = sub.add_parser("adjudication-followup",
                       help="second blind pass with all candidates (offline)")
    for name in ("--review-dir", "--filled", "--snapshot", "--challenge", "--out"):
        p.add_argument(name, required=True)
    p.add_argument("--gold-dir", help="reviewed Gold dir, to verify its sha256 read-only")
    p.set_defaults(func=cmd_adjudication_followup)

    p = sub.add_parser("postmortem", help="deterministic Stage A failure analysis (no LLM)")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--challenge", required=True)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--review-queue", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_postmortem)

    p = sub.add_parser("registry-models", help="selector-eligible registry models (read-only)")
    p.add_argument("--out")
    p.set_defaults(func=cmd_registry_models)

    p = sub.add_parser("live-plan", help="build the fingerprinted live-run approval plan")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--challenge", required=True)
    p.add_argument("--registry", required=True, help="registry-models output JSON")
    p.add_argument("--production-config", required=True, help="config-snapshot output JSON")
    p.add_argument("--prior-model", action="append",
                   help="provider/model used in earlier benchmarks (reported if unregistered)")
    p.add_argument("--price", action="append", help="provider/model=IN_PER_1M:OUT_PER_1M")
    p.add_argument("--input-price-per-1m", type=float)
    p.add_argument("--output-price-per-1m", type=float)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_live_plan)

    p = sub.add_parser("evaluate", help="(re)compute metrics for a run dir")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("compare", help="paired comparison of two runs on one snapshot")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--run-a", required=True)
    p.add_argument("--run-b", required=True)
    p.add_argument("--all-cases", action="store_true", help="include non-primary cases")
    p.add_argument("--out")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("config-snapshot", help="read the active registry config (read-only)")
    p.add_argument("--out")
    p.set_defaults(func=cmd_config_snapshot)
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
