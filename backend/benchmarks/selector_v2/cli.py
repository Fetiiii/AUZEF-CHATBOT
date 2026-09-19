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


def cmd_adjudication_lock(args) -> None:
    """Lock the merged human decisions. Never reads audit-view.jsonl."""
    import datetime as _dt

    from benchmarks.selector_v2 import semantic_gold as sg

    with no_live_calls():
        lock = sg.lock_review(decisions_bytes=Path(args.decisions).read_bytes(),
                              expected_sha=args.expected_sha256,
                              review_dir=Path(args.review_dir), followup_dir=Path(args.followup_dir))
        manifest = sg.write_lock(Path(args.review_dir) / sg.LOCK_DIR, lock,
                                 _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
    _dump({k: manifest[k] for k in ("lock_fingerprint", "review_case_count", "decision_counts",
                                    "rounds", "source_decision_sha256")})


def cmd_semantic_apply_rescore(args) -> None:
    """After lock: audit → Semantic Gold V1 (child) → re-score saved outputs."""
    from benchmarks.selector_v2 import prompt_experiment as px
    from benchmarks.selector_v2 import semantic_gold as sg
    from benchmarks.selector_v2.challenge import (
        first_candidate_correct, load_challenge, reference_universe,
    )
    from benchmarks.selector_v2.evaluator import metrics
    from benchmarks.selector_v2.prompt_contract import load_committed_manifest, prompt_manifest
    from benchmarks.selector_v2.runner import RESULTS_FILE, load_results
    from benchmarks.selector_v2.snapshot import load_snapshot

    with no_live_calls():
        review_dir = Path(args.review_dir)
        lock_manifest, records, audit = sg.open_audit_after_lock(review_dir, review_dir / sg.LOCK_DIR)
        manifest, snapshots = load_snapshot(Path(args.snapshot))
        cm, cases = load_challenge(Path(args.challenge), manifest)
        if lock_manifest["snapshot_fingerprint"] != manifest["snapshot_fingerprint"]:
            raise SystemExit("lock belongs to another snapshot")
        if prompt_manifest() != load_committed_manifest():
            raise SystemExit("prompt manifest drift")
        parent_fp = manifest["snapshot_fingerprint"]
        built = sg.build_semantic_gold(snapshots, records, audit,
                                       lock_fp=lock_manifest["lock_fingerprint"], parent_fp=parent_fp)
        acct = sg.accounting(records, audit, built["derived"])
        sem = sg.semantic_snapshots(snapshots, built["cases"])

        def jl(rows):
            return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n"
                           for r in rows)

        prov = built["provenance"]
        gold_dir = Path(args.gold_out)
        files = {
            "semantic-gold.jsonl": "".join(c.model_dump_json() + "\n" for c in built["cases"]),
            "changes.jsonl": jl(prov),
            "excluded-cases.jsonl": jl([p for p in prov if p["derived_outcome"] == "EXCLUDE_AMBIGUOUS"]),
            "content-review-queue.jsonl": jl([p for p in prov
                                              if p["derived_outcome"] == "CONTENT_REVIEW_REQUIRED"]),
            "retrieval-kb-review-queue.jsonl": jl([p for p in prov if p["derived_outcome"]
                                                   == "RETRIEVAL_OR_KB_MAPPING_REVIEW"]),
        }
        hashes = sg.write_immutable(gold_dir, files)
        dens_old = sg.denominators(snapshots)
        dens_new = sg.denominators(sem)
        sg.write_immutable(gold_dir, {"manifest.json": json.dumps({
            "version": sg.SEMANTIC_GOLD_VERSION, "semantic_gold_fingerprint": built["fingerprint"],
            "parent_snapshot_fingerprint": parent_fp,
            "parent_reviewed_gold_all_sha256": manifest["dataset"]["source"]["gold_all_sha256"],
            "review_lock_fingerprint": lock_manifest["lock_fingerprint"],
            "reviewed_cases": len(records), "accounting": acct,
            "denominators_parent": dens_old, "denominators_semantic": dens_new,
            "artifact_sha256": hashes}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"})

        # ── re-score (saved outputs only) ──
        split = px.load_split(challenge_ids={c["case_id"] for c in cases})
        membership = {c["case_id"]: c["membership"] for c in cases}
        side = {**dict.fromkeys(split["dev"], "DEV"), **dict.fromkeys(split["holdout"], "HOLDOUT")}
        runs_raw = {"production": load_results(Path(args.stage_a_run) / RESULTS_FILE).by_case}
        for item in args.run:
            name, _, path = item.partition("=")
            runs_raw[name] = load_results(Path(path) / RESULTS_FILE).by_case
        sem_by = {s.case.case_id: s for s in sem}
        old_by = {s.case.case_id: s for s in snapshots}
        dev_old = [i for i in split["dev"] if old_by[i].selector_evaluable]
        dev_sem = [i for i in split["dev"] if sem_by[i].selector_evaluable]
        runs_sem = {n: sg.rescore_run(sem, r) for n, r in runs_raw.items()}
        empty_slices: dict = {}
        rep_old = {n: px.dev_report(snapshots, dev_old, r, membership, empty_slices, label=n,
                                    split_side=side) for n, r in runs_raw.items()}
        rep_sem = {n: px.dev_report(sem, dev_sem, r, membership, empty_slices, label=n,
                                    split_side=side) for n, r in runs_sem.items()}
        # Like-for-like: OLD Gold on the SAME semantic-evaluable ids (isolates relabelling
        # from the removal of excluded cases).
        rep_old_same = {n: px.dev_report(snapshots, dev_sem, r, membership, empty_slices,
                                         label=n, split_side=side) for n, r in runs_raw.items()}
        gates = {n: px.selection_gate(rep_sem[n], rep_sem["production"])
                 for n in rep_sem if n != "production"}
        paired = px.compare_runs(sem, dev_sem, runs_sem)
        fc_old = px._acc(dev_old, {i: first_candidate_correct(old_by[i]) for i in dev_old})
        fc_sem = px._acc(dev_sem, {i: first_candidate_correct(sem_by[i]) for i in dev_sem})
        full_old = reference_universe(snapshots)
        full_sem = reference_universe(sem)
        challenge_ids = [c["case_id"] for c in cases]
        stage_a_old = metrics([old_by[i] for i in challenge_ids if old_by[i].selector_evaluable],
                              runs_raw["production"])
        stage_a_sem = metrics([sem_by[i] for i in challenge_ids if sem_by[i].selector_evaluable],
                              runs_sem["production"])
        outcomes = {cid: d["outcome"] for cid, d in built["derived"].items()}
        rejected = set(acct["old_gold_rejected_ids"])
        review_slices = {}
        for name, pred in (("KEEP_CURRENT", lambda c: outcomes.get(c) == "KEEP_CURRENT"),
                           ("CHANGE_GOLD", lambda c: outcomes.get(c) == "CHANGE_GOLD"),
                           ("MULTI_ACCEPTABLE", lambda c: outcomes.get(c) == "MULTI_ACCEPTABLE"),
                           ("EXPECT_NONE", lambda c: outcomes.get(c) == "EXPECT_NONE"),
                           ("OLD_GOLD_REJECTED", lambda c: c in rejected)):
            ids = [i for i in dev_sem if pred(i)]
            review_slices[name] = {n: px._acc(ids, {i: runs_sem[n][i].correct for i in ids
                                                    if i in runs_sem[n]}) for n in runs_sem}
        review_slices["EXCLUDED_IN_DEV"] = {
            k: sum(1 for i in split["dev"] if outcomes.get(i) == k)
            for k in ("EXCLUDE_AMBIGUOUS", "CONTENT_REVIEW_REQUIRED", "RETRIEVAL_OR_KB_MAPPING_REVIEW")}
        none_cases = [cid for cid, o in outcomes.items() if o == "EXPECT_NONE"]
        none_diag = {cid: {"split": side.get(cid), **{
            n: (runs_raw[n][cid].decision, runs_sem[n][cid].outcome.value)
            if cid in runs_raw[n] and cid in runs_sem[n] else None for n in runs_raw}}
            for cid in none_cases}
        passing = sorted(n for n, g in gates.items() if g["passed"])
        holdout = ("BLOCKED_NO_VARIANT_PASSED_SEMANTIC_GATE" if not passing else
                   "READY_PENDING_EXPLICIT_APPROVAL" if len(passing) == 1 else
                   "WAITING_FOR_HUMAN_VARIANT_SELECTION")

        def brief(r):
            return {"cases": r["cases"], "exact": r["exact"], "value": r["selector_value"],
                    "false_none": r["false_none"], "none_output": r["none_output"],
                    "general": r["general_expected"], "specific": r["specific_expected"],
                    "near_qna": r["near_qna"], "easy_control": r["easy_control"],
                    "validity": r["validity"], "gs_case_correctness": r["gs_case_correctness"]}

        report = {
            "semantic_gold_fingerprint": built["fingerprint"],
            "review_lock_fingerprint": lock_manifest["lock_fingerprint"],
            "denominators": {"parent": dens_old, "semantic": dens_new,
                             "dev_parent": len(dev_old), "dev_semantic": len(dev_sem)},
            "first_candidate": {
                "full_parent": {"correct": sum(map(first_candidate_correct, full_old)), "cases": len(full_old)},
                "full_semantic": {"correct": sum(map(first_candidate_correct, full_sem)), "cases": len(full_sem)},
                "dev_parent": fc_old, "dev_semantic": fc_sem},
            "stage_a_production_137": {"parent": stage_a_old, "semantic": stage_a_sem},
            "dev_parent": {n: brief(r) for n, r in rep_old.items()},
            "dev_parent_on_semantic_ids": {n: brief(r) for n, r in rep_old_same.items()},
            "first_candidate_parent_on_semantic_ids": px._acc(
                dev_sem, {i: first_candidate_correct(old_by[i]) for i in dev_sem}),
            "dev_semantic": {n: brief(r) for n, r in rep_sem.items()},
            "gates": gates, "passing_variants": passing, "winner": None,
            "paired_semantic": paired["paired"], "diff_counts_semantic": paired["diff_counts"],
            "review_slices_dev": review_slices, "expected_none_diagnostic": none_diag,
            "holdout_status": holdout,
            "notes": ["471/472 (clear selector errors) are HOLDOUT: no live A/B outputs exist",
                      "a single expected-NONE case cannot support NONE precision/recall claims"],
        }
        out = Path(args.rescore_out)
        out.mkdir(parents=True, exist_ok=True)
        _dump(report, out / "semantic-rescore.json")
        _dump(paired["diffs"], out / "semantic-case-diffs.json")
    _dump({"semantic_gold_fingerprint": built["fingerprint"], "outcomes": acct["outcomes"],
           "holdout_status": holdout, "passing": passing})


def _holdout_inputs(args):
    """Verified frozen inputs for the semantic HOLDOUT (offline)."""
    from benchmarks.selector_v2 import prompt_experiment as px
    from benchmarks.selector_v2 import semantic_gold as sg
    from benchmarks.selector_v2 import semantic_holdout as sh
    from benchmarks.selector_v2.challenge import load_challenge
    from benchmarks.selector_v2.prompt_contract import (
        load_committed_manifest, load_prompt, prompt_manifest, serializer_contract_fingerprint,
    )
    from benchmarks.selector_v2.runner import RESULTS_FILE, load_results
    from benchmarks.selector_v2.snapshot import load_snapshot

    manifest, snapshots = load_snapshot(Path(args.snapshot))
    cm, cases = load_challenge(Path(args.challenge), manifest)
    split = px.load_split(challenge_ids={c["case_id"] for c in cases})
    expected = {"snapshot": (manifest["snapshot_fingerprint"], args.expected_snapshot_fp),
                "challenge": (cm["challenge_fingerprint"], args.expected_challenge_fp),
                "split": (split["split_fingerprint"], args.expected_split_fp)}
    wrong = [k for k, (have, want) in expected.items() if have != want]
    if wrong:
        raise SystemExit(f"frozen identity mismatch: {wrong}")
    gold_manifest, gold_cases = sh.load_semantic_gold(Path(args.semantic_gold), args.expected_gold_fp)
    if gold_manifest["parent_snapshot_fingerprint"] != manifest["snapshot_fingerprint"]:
        raise SystemExit("semantic gold belongs to another snapshot")
    if prompt_manifest() != load_committed_manifest():
        raise SystemExit("prompt manifest drift: a prompt or the serializer changed")
    prompt = load_prompt(sh.SELECTED_PROMPT_ID)
    if prompt.fingerprint != args.expected_prompt_fp or prompt.fingerprint != sh.SELECTED_PROMPT_FINGERPRINT:
        raise SystemExit(f"STOP: variant_a_v1 fingerprint {prompt.fingerprint} != expected")
    sem = sg.semantic_snapshots(snapshots, gold_cases)
    stage_a = Path(args.stage_a_run)
    production = load_results(stage_a / RESULTS_FILE).by_case
    return {"manifest": manifest, "snapshots": snapshots, "cm": cm, "cases": cases, "split": split,
            "gold_manifest": gold_manifest, "sem": sem, "prompt": prompt, "production": production,
            "membership": {c["case_id"]: c["membership"] for c in cases},
            "serializer_fp": serializer_contract_fingerprint(),
            "stage_a_results_sha256": _file_sha256(stage_a / RESULTS_FILE),
            "gold_fp": gold_manifest["semantic_gold_fingerprint"]}


def _file_sha256(path: Path) -> str:
    import hashlib

    return hashlib.new("sha256", Path(path).read_bytes()).hexdigest()


def cmd_semantic_holdout_prep(args) -> None:
    """Offline: freeze the production/first-candidate semantic HOLDOUT baseline,
    the promotion gate and the 42-call HOLDOUT plan (no variant output exists)."""
    from benchmarks.selector_v2 import semantic_holdout as sh
    from benchmarks.selector_v2.providers import selector_config
    from benchmarks.selector_v2.runner import RESULTS_FILE, load_results
    from benchmarks.selector_v2.tokens import estimate

    with no_live_calls():
        ctx = _holdout_inputs(args)
        out = Path(args.out)
        if (out / "runs").exists():
            raise SystemExit("variant HOLDOUT outputs already exist: the baseline must precede them")
        identity = {"snapshot_fingerprint": ctx["manifest"]["snapshot_fingerprint"],
                    "challenge_fingerprint": ctx["cm"]["challenge_fingerprint"],
                    "split_fingerprint": ctx["split"]["split_fingerprint"],
                    "semantic_gold_fingerprint": ctx["gold_fp"],
                    "review_lock_fingerprint": ctx["gold_manifest"]["review_lock_fingerprint"],
                    "stage_a_run_id": Path(args.stage_a_run).name,
                    "stage_a_results_sha256": ctx["stage_a_results_sha256"],
                    "serializer_contract_fingerprint": ctx["serializer_fp"],
                    "selected_prompt_fingerprint": ctx["prompt"].fingerprint}
        baseline = sh.build_prelive_baseline(snapshots=ctx["snapshots"], sem=ctx["sem"],
                                             split=ctx["split"], production=ctx["production"],
                                             membership=ctx["membership"], identity=identity)
        baseline["frozen_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        existing = out / sh.BASELINE_FILE
        if existing.exists():
            prior = sh.verify_baseline(existing)
            if prior["baseline_fingerprint"] != baseline["baseline_fingerprint"]:
                raise SystemExit("a different frozen baseline already exists (immutable)")
            baseline = prior
        else:
            _dump(baseline, existing)
        rescore = json.loads(Path(args.rescore).read_text(encoding="utf-8"))
        gate_report = sh.semantic_gate_report(rescore, ctx["prompt"], ctx["split"]["split_fingerprint"],
                                              ctx["gold_fp"])
        if not gate_report["gate"]["passed"]:
            raise SystemExit("variant_a_v1 did not pass the semantic DEV gate: no HOLDOUT")
        _dump(gate_report, out / sh.SEMANTIC_GATE_FILE)
        by_id = {s.case.case_id: s for s in ctx["snapshots"]}
        holdout_snaps = [by_id[i] for i in ctx["split"]["holdout"]]
        config = selector_config(provider="openrouter", model="openai/gpt-4o-mini",
                                 temperature=0.0, max_tokens=32)
        est = estimate(holdout_snaps, max_tokens=config.max_tokens, primary_only=True,
                       system_prompt=ctx["prompt"].text)
        dev_run = load_results(Path(args.dev_run) / RESULTS_FILE).by_case
        dev_ids = ctx["split"]["dev"]
        dev_est = estimate([by_id[i] for i in dev_ids], max_tokens=config.max_tokens,
                           primary_only=True, system_prompt=ctx["prompt"].text)
        actual_dev = sum(dev_run[i].input_tokens or 0 for i in dev_ids if i in dev_run)
        factor = round(actual_dev / dev_est["input_tokens"]["total"], 4)
        estimate_block = {"input_tokens_approx": est["input_tokens"]["total"],
                          "input_tokens_calibrated": round(est["input_tokens"]["total"] * factor),
                          "calibration_factor": factor,
                          "calibration_source": "variant_a_v1 DEV actual OpenRouter input / estimate",
                          "output_tokens_upper_bound": est["output_tokens_upper_bound_total"],
                          "planned_calls": est["calls_per_config"]}
        if est["calls_per_config"] != sh.HOLDOUT_SIZE:
            raise SystemExit(f"{est['calls_per_config']} callable HOLDOUT cases, expected {sh.HOLDOUT_SIZE}")
        plan = sh.build_holdout_plan(
            config=config, prompt=ctx["prompt"], split=ctx["split"], baseline=baseline,
            snapshot_fp=ctx["manifest"]["snapshot_fingerprint"],
            challenge_fp=ctx["cm"]["challenge_fingerprint"], serializer_fp=ctx["serializer_fp"],
            gold_fp=ctx["gold_fp"], estimate_block=estimate_block,
            human_selection="variant_a_v1 selected for HOLDOUT by the human after the semantic "
                            "DEV comparison; variant_b_v1 is not run on HOLDOUT")
        plan_path = out / sh.PLAN_FILE
        if plan_path.exists():
            prior = json.loads(plan_path.read_text(encoding="utf-8"))
            if prior.get("plan_fingerprint") != plan["plan_fingerprint"]:
                raise SystemExit("a different HOLDOUT plan already exists")
        else:
            _dump(plan, plan_path)
    _dump({"baseline_fingerprint": baseline["baseline_fingerprint"],
           "plan_fingerprint": plan["plan_fingerprint"],
           "holdout_total": baseline["holdout_total"],
           "semantic_evaluable": baseline["semantic_evaluable_count"],
           "excluded": baseline["excluded"], "production": baseline["production"],
           "first_candidate": {k: baseline["first_candidate"][k] for k in ("exact", "denominator")},
           "slices_production": baseline["production_slices"],
           "critical_production": baseline["critical_cases_production"],
           "estimate": estimate_block})


def cmd_semantic_holdout_run(args) -> None:
    """Variant A on the 42 HOLDOUT cases: live only under the approved HOLDOUT plan."""
    from benchmarks.selector_v2 import prompt_experiment as px
    from benchmarks.selector_v2 import semantic_holdout as sh
    from benchmarks.selector_v2.providers import (
        FAKE_PROVIDER, FakeSelectorProvider, LiveSelectorBackend, selector_config,
    )
    from benchmarks.selector_v2.runner import RunIdentity, ResultStore, run_benchmark

    check_live_gate(live=args.live, confirmed=args.confirm_live_provider_calls,
                    provider=args.provider, model=args.model)
    with no_live_calls():
        ctx = _holdout_inputs(args)
        out = Path(args.out)
        baseline = sh.verify_baseline(Path(args.baseline_dir) / sh.BASELINE_FILE)
        if baseline["identity"]["semantic_gold_fingerprint"] != ctx["gold_fp"] or \
                baseline["holdout_case_ids"] != ctx["split"]["holdout"]:
            raise SystemExit("frozen baseline belongs to other inputs")
        split, prompt = ctx["split"], ctx["prompt"]
        holdout = set(split["holdout"])
        if args.live:
            config = selector_config(provider=args.provider, model=args.model,
                                     temperature=args.temperature, max_tokens=args.max_tokens)
            plan_path = Path(args.baseline_dir) / sh.PLAN_FILE
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            sh.validate_holdout_plan(plan, args.approve_plan_fingerprint, config=config, prompt=prompt,
                                     split=split, baseline=baseline,
                                     snapshot_fp=ctx["manifest"]["snapshot_fingerprint"],
                                     serializer_fp=ctx["serializer_fp"], gold_fp=ctx["gold_fp"])
            px.holdout_guard(prompt=prompt, selected_winner=args.selected_variant,
                             gate_report_path=Path(args.baseline_dir) / sh.SEMANTIC_GATE_FILE,
                             split_fp=split["split_fingerprint"], live=True, plan_path=plan_path,
                             approved_fp=args.approve_plan_fingerprint)
            inner = LiveSelectorBackend(config, prompt=prompt)
            mode = "LIVE"
        else:
            config = selector_config(provider=FAKE_PROVIDER, model=f"fake-{args.fake_policy}",
                                     max_tokens=args.max_tokens)
            inner = FakeSelectorProvider(args.fake_policy, config, prompt=prompt)
            mode = f"DRY_RUN_FAKE:{args.fake_policy}"
        identity = RunIdentity(
            selector_contract_fingerprint=ctx["serializer_fp"], config=config,
            snapshot_fingerprint=ctx["manifest"]["snapshot_fingerprint"], run_mode=mode,
            prompt_fingerprint=prompt.fingerprint, split_fingerprint=split["split_fingerprint"])
        already = ResultStore(out / "runs" / identity.run_id, identity).load().by_case
        if set(already) - holdout:
            raise SystemExit("run directory holds non-HOLDOUT results")
        # Budget over the whole run (resume included): at most 42 logical calls in total.
        backend = sh.BudgetedBackend(inner, sh.MAX_LOGICAL_CALLS - len(already), holdout)

    def execute():
        return run_benchmark(ctx["snapshots"], backend, identity, out, concurrency=1,
                             max_cases=sh.MAX_LOGICAL_CALLS, retry_errors=False,
                             primary_only=True, case_ids=holdout)

    if args.live:
        with no_live_calls([LIVE_PROVIDERS[config.provider]], block_sdks=False):
            summary = execute()
    else:
        with no_live_calls():
            summary = execute()
    accounting = {"run_id": identity.run_id, "run_mode": mode,
                  "logical_calls_this_invocation": backend.calls,
                  "previously_completed": len(already),
                  "logical_calls_total": backend.calls + len(already),
                  "refused_by_budget_guard": backend.refused,
                  "max_logical_calls": sh.MAX_LOGICAL_CALLS,
                  "production_calls": 0, "variant_b_calls": 0, "stage_b_calls": 0,
                  "summary": summary.__dict__}
    if accounting["logical_calls_total"] > sh.MAX_LOGICAL_CALLS or backend.refused:
        accounting["status"] = "FAIL_CALL_BUDGET"
    _dump(accounting, Path(summary.run_dir) / "call-accounting.json")
    _dump(accounting)


def cmd_semantic_holdout_eval(args) -> None:
    """Offline: re-verify the frozen baseline, score Variant A on Semantic Gold, apply the gate."""
    from benchmarks.selector_v2 import semantic_holdout as sh
    from benchmarks.selector_v2.contract import sha256_text
    from benchmarks.selector_v2.runner import RESULTS_FILE, RUN_MANIFEST, load_results

    with no_live_calls():
        ctx = _holdout_inputs(args)
        out = Path(args.out)
        baseline = sh.verify_baseline(out / sh.BASELINE_FILE)
        plan = json.loads((out / sh.PLAN_FILE).read_text(encoding="utf-8"))
        run_dir = Path(args.run_dir)
        run_identity = json.loads((run_dir / RUN_MANIFEST).read_text(encoding="utf-8"))
        checks = {"prompt": run_identity.get("prompt_fingerprint") == sh.SELECTED_PROMPT_FINGERPRINT,
                  "split": run_identity.get("split_fingerprint") == ctx["split"]["split_fingerprint"],
                  "snapshot": run_identity.get("snapshot_fingerprint") == ctx["manifest"]["snapshot_fingerprint"],
                  "config": run_identity.get("config_fingerprint") == plan["config_fingerprint"]
                  or str(run_identity.get("run_mode", "")).startswith("DRY_RUN")}
        if not all(checks.values()):
            raise SystemExit(f"run identity mismatch: {checks}")
        loaded = load_results(run_dir / RESULTS_FILE)
        changes = sh.load_changes(Path(args.semantic_gold))
        metrics = sh.evaluate_holdout(baseline=baseline, snapshots=ctx["snapshots"], sem=ctx["sem"],
                                      production=ctx["production"], variant=loaded.by_case,
                                      membership=ctx["membership"], changes=changes)
        accounting = json.loads((run_dir / "call-accounting.json").read_text(encoding="utf-8"))
        scored = metrics.pop("scored_rows")
        raw = (run_dir / RESULTS_FILE).read_text(encoding="utf-8")
        files = {
            "responses.jsonl": raw,
            "scored-results.jsonl": "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                                            for r in scored),
            "metrics.json": json.dumps({**metrics, "result_integrity": {
                "unique_cases": len(loaded.by_case), "superseded": loaded.superseded,
                "corrupt_lines": loaded.corrupt_lines}, "call_accounting": accounting},
                ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
            "critical-cases.json": json.dumps(metrics["critical_cases"], ensure_ascii=False,
                                              indent=2, sort_keys=True) + "\n",
        }
        for name, text in files.items():
            (out / name).write_text(text, encoding="utf-8")
        gate = metrics["promotion_gate"]
        manifest = {
            "phase": "7B semantic HOLDOUT — variant_a_v1",
            "baseline_fingerprint": baseline["baseline_fingerprint"],
            "baseline_reverified_after_scoring": True,
            "baseline_frozen_at": baseline["frozen_at"],
            "plan_fingerprint": plan["plan_fingerprint"],
            "run_id": run_identity["run_id"], "run_mode": run_identity["run_mode"],
            "run_identity_checks": checks,
            "semantic_gold_fingerprint": ctx["gold_fp"],
            "prompt_fingerprint": ctx["prompt"].fingerprint,
            "split_fingerprint": ctx["split"]["split_fingerprint"],
            "promotion_gate_version": sh.PROMOTION_GATE["version"],
            "promotion_gate_passed": gate["passed"],
            "outcome": ("VARIANT_A_HOLDOUT = PASS; SELECTOR_PROMPT_CANDIDATE = variant_a_v1"
                        if gate["passed"] else "VARIANT_A_HOLDOUT = FAIL"),
            "artifact_sha256": {name: sha256_text(text) for name, text in files.items()},
            "responses_source": str(run_dir / RESULTS_FILE),
        }
        _dump(manifest, out / "manifest.json")
    _dump({"outcome": manifest["outcome"], "checks": gate["checks"],
           "production": metrics["production"]["exact"], "variant_a": metrics["variant_a"]["exact"],
           "paired": metrics["paired_vs_production"]})


def _tree_sha256(root: Path) -> dict:
    return {str(p.relative_to(root)): _file_sha256(p)
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


def cmd_qualifier_postmortem(args) -> None:
    """Offline: 471/472 blind packet, qualifier inventory, order analysis,
    order-experiment prep and final-validation contamination accounting."""
    from benchmarks.selector_v2 import adjudication as adj
    from benchmarks.selector_v2 import prompt_experiment as px
    from benchmarks.selector_v2 import qualifier_postmortem as qp
    from benchmarks.selector_v2 import semantic_gold as sg
    from benchmarks.selector_v2 import semantic_holdout as sh
    from benchmarks.selector_v2.challenge import load_challenge
    from benchmarks.selector_v2.postmortem import _case_key
    from benchmarks.selector_v2.contract import sha256_text
    from benchmarks.selector_v2.prompt_contract import load_committed_manifest, load_prompt, prompt_manifest
    from benchmarks.selector_v2.runner import RESULTS_FILE, load_results
    from benchmarks.selector_v2.snapshot import load_near_pairs, load_snapshot

    with no_live_calls():
        out = Path(args.out)
        manifest, snapshots = load_snapshot(Path(args.snapshot))
        cm, cases = load_challenge(Path(args.challenge), manifest)
        split = px.load_split(challenge_ids={c["case_id"] for c in cases})
        gold_manifest, gold_cases = sh.load_semantic_gold(Path(args.semantic_gold), args.expected_gold_fp)
        if prompt_manifest() != load_committed_manifest():
            raise SystemExit("prompt manifest drift")
        prompt = load_prompt(sh.SELECTED_PROMPT_ID)
        if prompt.fingerprint != sh.SELECTED_PROMPT_FINGERPRINT:
            raise SystemExit("variant_a_v1 changed")
        holdout_dir = Path(args.holdout_dir)
        holdout_record = {"semantic_holdout_a_sha256": _tree_sha256(holdout_dir),
                          "prompt_fingerprint": prompt.fingerprint,
                          "semantic_gold_fingerprint": gold_manifest["semantic_gold_fingerprint"],
                          "holdout_manifest_outcome": json.loads(
                              (holdout_dir / "manifest.json").read_text(encoding="utf-8"))["outcome"]}
        sh.verify_baseline(holdout_dir / sh.BASELINE_FILE)
        sem = sg.semantic_snapshots(snapshots, gold_cases)
        sem_by = {s.case.case_id: s for s in sem}
        parent_by = {s.case.case_id: s for s in snapshots}
        side = {**dict.fromkeys(split["dev"], "DEV"), **dict.fromkeys(split["holdout"], "HOLDOUT")}
        production = load_results(Path(args.stage_a_run) / RESULTS_FILE).by_case
        variant_a = {**load_results(Path(args.dev_run) / RESULTS_FILE).by_case}
        a_hold = load_results(Path(args.holdout_run) / RESULTS_FILE).by_case
        if set(variant_a) & set(a_hold):
            raise SystemExit("DEV and HOLDOUT Variant A outputs overlap")
        variant_a.update(a_hold)
        runs = {"production": production, "variant_a_v1": variant_a}

        # 1. blind 471/472 packet (no Gold / model / rank information)
        records = qp.blind_records(parent_by, list(qp.CRITICAL_CASES))
        md, csv_text = qp.review_markdown(records), qp.review_template_csv(records)
        cases_jsonl = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
        refs = sorted({c.candidate_ref for cid in qp.CRITICAL_CASES for c in parent_by[cid].candidates})
        problems = qp.review_violations(records, csv_text, md, forbidden_refs=refs)
        if problems:
            raise SystemExit(f"review packet contamination: {problems[:10]}")
        review_dir = out / "review"
        hashes = adj.write_immutable(review_dir, {"review-packet.md": md, "review-template.csv": csv_text,
                                                  "review-cases.jsonl": cases_jsonl})
        review_fp = qp.review_fingerprint(manifest["snapshot_fingerprint"], records)
        adj.write_immutable(review_dir, {"manifest.json": json.dumps({
            "schema": qp.REVIEW_SCHEMA_VERSION, "review_packet_fingerprint": review_fp,
            "case_ids": list(qp.CRITICAL_CASES),
            "candidate_counts": {r["case_id"]: r["candidate_count"] for r in records},
            "decisions_allowed": list(qp.REVIEW_DECISIONS),
            "label_order": "sha256(case_id|candidate_ref) (same blind labelling as the semantic review; "
                           "labels are recomputable from the frozen snapshot)",
            "source_snapshot_fingerprint": manifest["snapshot_fingerprint"],
            "human_review_status": "NOT_STARTED", "artifact_sha256": hashes},
            ensure_ascii=False, indent=2, sort_keys=True) + "\n"})

        # 2. inventory (model-independent)
        inv = qp.inventory(snapshots, sem_by, side)
        inv_ids = {r["case_id"] for r in inv}
        for r in inv:
            r["expectation"] = qp.expectation_class(r, sem_by[r["case_id"]])["class"]
        evaluable_inv = [r for r in inv if r["semantic_evaluable"]]

        def count(rows, key):
            outc: dict = {}
            for r in rows:
                outc[r[key]] = outc.get(r[key], 0) + 1
            return dict(sorted(outc.items()))

        inv_summary = {
            "version": qp.INVENTORY_VERSION,
            "definition": qp.__doc__.split("* A case")[1].split("* The 471")[0].strip(),
            "lexicon": [c for c, _ in qp.QUALIFIER_LEXICON], "lexicon_exclusions": list(qp.LEXICON_EXCLUSIONS),
            "total_qualifier_sensitive": len(inv),
            "by_split": count(inv, "split"), "by_mode": count(inv, "mode"),
            "semantic_evaluable": len(evaluable_inv), "excluded": len(inv) - len(evaluable_inv),
            "excluded_by_reason": count([r for r in inv if not r["semantic_evaluable"]], "semantic_status_reason"),
            "expectation": count(inv, "expectation"),
            "expectation_by_mode": {m: count([r for r in inv if r["mode"] == m], "expectation")
                                    for m in ("STATED", "UNSTATED")},
            "qualifier_concepts": count([{"q": p["qualifier"]} for r in inv for p in r["pairs"]], "q"),
        }

        # 3. Phase 7A general/specific pairs (fixture metadata; membership = both members
        #    present in the frozen candidate set, model-independent)
        gs_rows = []
        for pair in load_near_pairs():
            if pair.relation != "general_specific":
                continue
            g_id = pair.a if pair.roles[0] == "general" else pair.b
            s_id = pair.b if g_id == pair.a else pair.a
            g_q, s_q = pair.questions if g_id == pair.a else pair.questions[::-1]
            g_ref, s_ref = f"qna:{g_id}", f"qna:{s_id}"

            def semantic_class(s):
                if not s.selector_evaluable:
                    return "EXCLUDED"
                if s.case.expected_decision == "NONE":
                    return "NONE"
                acc = set(s.case.acceptable_candidate_refs)
                if len(acc) > 1:
                    return "MULTI_ACCEPTABLE"
                return "GENERAL" if acc == {g_ref} else "SPECIFIC" if acc == {s_ref} else "OTHER"

            members = [s for s in sem if s.case.primary
                       and {g_ref, s_ref} <= {c.candidate_ref for c in s.candidates}]
            rows = [{"case_id": s.case.case_id, "split": side.get(s.case.case_id, "OUTSIDE_CHALLENGE"),
                     "user_text": s.case.intent_text,
                     "user_states_qualifier": bool(qp.qualifiers(s_q) - qp.qualifiers(g_q)
                                                   & qp.qualifiers(s.case.intent_text)),
                     "phase7a_role": next((t.split(":")[1] for t in parent_by[s.case.case_id].derived_tags
                                           if t.startswith("gs_expected:")), None),
                     "semantic": semantic_class(s)}
                    for s in sorted(members, key=lambda s: _case_key(s.case.case_id))]
            counts: dict = {}
            for r in rows:
                counts[r["semantic"]] = counts.get(r["semantic"], 0) + 1
            gs_rows.append({"pair": pair.key, "general": {"ref": g_ref, "question": g_q},
                            "specific": {"ref": s_ref, "question": s_q},
                            "qualifier": sorted(qp.qualifiers(s_q) - qp.qualifiers(g_q)),
                            "cases_with_both_members": len(rows), "semantic_counts": counts,
                            "cases": rows})

        # 4. order analysis on semantic-evaluable qualifier cases
        order_rows = [qp.order_row(r, sem_by[r["case_id"]]) for r in evaluable_inv]
        order_summary = {
            "cases": len(order_rows),
            "position1_accepted": sum(r["position1_accepted"] for r in order_rows),
            "position1_not_accepted": sum(not r["position1_accepted"] for r in order_rows),
            "position1_too_specific": sum(r["position1_too_specific"] for r in order_rows),
            "general_expected": sum(r["expectation"] == "GENERAL" for r in order_rows),
            "specific_expected": sum(r["expectation"] == "SPECIFIC" for r in order_rows),
            "general_expected_but_specific_position1": sum(
                r["general_expected_specific_position1"] for r in order_rows),
            "general_expected_but_specific_position1_ids": [
                r["case_id"] for r in order_rows if r["general_expected_specific_position1"]],
        }

        # 5. first-position association (saved outputs only; no selection by output)
        qual_eval_ids = [r["case_id"] for r in evaluable_inv]
        assoc_qual = qp.position_association(sem_by, qual_eval_ids, runs)
        challenge_eval = [c["case_id"] for c in cases if sem_by[c["case_id"]].selector_evaluable]
        assoc_challenge = qp.position_association(sem_by, challenge_eval, runs)
        verdicts = {"qualifier_set": {n: qp.order_bias_verdict(assoc_qual, n) for n in runs},
                    "challenge_set": {n: qp.order_bias_verdict(assoc_challenge, n) for n in runs}}

        # 6. prompt-rule compliance (saved Variant A / production outputs)
        comp = {n: qp.compliance_table(sem_by, challenge_eval, r) for n, r in runs.items()}
        comp_qual = {n: qp.compliance_table(sem_by, [i for i in qual_eval_ids if i in r], r)
                     for n, r in runs.items()}

        # 7. 471/472 engineering diagnostics (never in the review packet)
        critical = {}
        for cid in qp.CRITICAL_CASES:
            snap = sem_by[cid]
            order = qp.original_order(snap)
            texts = {c.candidate_ref: c.canonical_text for c in snap.candidates}
            row = next((r for r in inv if r["case_id"] == cid), None)
            specific = {x for p in (row or {}).get("pairs", []) for x in p["specific"]}
            general = {x for p in (row or {}).get("pairs", []) for x in p["general"]}
            critical[cid] = {
                "user_text": snap.case.intent_text,
                "user_qualifiers": sorted(qp.qualifiers(snap.case.intent_text)),
                "candidates": [{"retrieval_position": i + 1, "ref": ref, "canonical_question": texts[ref],
                                "qualifiers": sorted(qp.qualifiers(texts[ref])),
                                "qualifier_class": ("SPECIFIC" if ref in specific else
                                                    "GENERAL" if ref in general else "UNRELATED")}
                               for i, ref in enumerate(order)],
                "current_semantic_gold": list(snap.case.acceptable_candidate_refs),
                "gold_provenance": "parent reviewed Gold, not re-adjudicated (pending blind review)",
                "accepted_retrieval_positions": [order.index(r) + 1 for r in snap.case.acceptable_candidate_refs
                                                 if r in order],
                "qna342_retrieval_position": order.index("qna:342") + 1 if "qna:342" in order else None,
                "neutral_order_positions": {"accepted": [qp.neutral_order(snap).index(r) + 1
                                                         for r in snap.case.acceptable_candidate_refs],
                                            "qna:342": qp.neutral_order(snap).index("qna:342") + 1},
                "saved_decisions": {n: {"selected": runs[n][cid].selected_candidate_ref,
                                        "outcome": runs[n][cid].outcome.value} for n in runs},
            }

        # 8. order experiment prep (no call)
        diag_rows = []
        for r in evaluable_inv:
            cid = r["case_id"]
            if side.get(cid) not in ("DEV", "HOLDOUT"):
                continue
            exp = qp.expectation_class(r, sem_by[cid])
            if exp["class"] not in ("GENERAL", "SPECIFIC", "MULTI_ACCEPTABLE"):
                continue
            info = qp.informative({}, r, sem_by[cid])
            if info["informative"] or cid in qp.CRITICAL_CASES:
                diag_rows.append({"case_id": cid, "split": side[cid], "expectation": exp["class"],
                                  "forced": cid in qp.CRITICAL_CASES, **info})
        for cid in qp.CRITICAL_CASES:
            if cid not in {d["case_id"] for d in diag_rows}:
                diag_rows.append({"case_id": cid, "split": side.get(cid), "forced": True,
                                  **qp.informative({}, next(r for r in inv if r["case_id"] == cid), sem_by[cid])})
        diag_rows.sort(key=lambda d: _case_key(d["case_id"]))
        diag_ids = [d["case_id"] for d in diag_rows]
        if not set(diag_ids) <= set(split["dev"]) | set(split["holdout"]):
            raise SystemExit("diagnostic set must stay inside the consumed DEV/HOLDOUT cases")
        conditions = {cid: qp.condition_requests(parent_by[cid], prompt.text) for cid in diag_ids}
        if not all(c["only_order_differs"] for c in conditions.values()):
            raise SystemExit("order conditions differ in more than candidate order")
        diag = {
            "version": qp.DIAGNOSTIC_VERSION,
            "purpose": "Does changing candidate order change qualifier errors? NOT a validation set; "
                       "never used for promotion accuracy.",
            "selection_criteria": [
                "cases 471 and 472 (forced)",
                "qualifier-sensitive (inventory v1), semantic-evaluable",
                "already-consumed challenge cases only (DEV or old HOLDOUT), so the unused pool stays "
                "independent for final validation",
                "Semantic Gold expectation GENERAL, SPECIFIC or MULTI_ACCEPTABLE",
                "ordering informative: the neutral order flips the relative order of the first accepted "
                "candidate and the first non-accepted qualifier-pair rival, OR moves a qualifier-pair "
                "member off position 1",
                "no model output used for selection",
                "asserted: every id is in previous DEV or previous HOLDOUT; none is in the unused pool",
            ],
            "case_ids": diag_ids, "size": len(diag_ids), "rows": diag_rows,
            "fingerprint": fingerprint({"version": qp.DIAGNOSTIC_VERSION, "ids": diag_ids}),
        }
        plan = {
            "plan_kind": "selector-order-diagnostic", "live": False, "approved": False,
            "provider": "openrouter", "model": "openai/gpt-4o-mini", "reasoning": None,
            "temperature": 0.0, "max_tokens": 32, "config_fingerprint": sh.PROMOTION_GATE and
            json.loads((holdout_dir / sh.PLAN_FILE).read_text(encoding="utf-8"))["config_fingerprint"],
            "prompt_id": prompt.prompt_id, "prompt_fingerprint": prompt.fingerprint,
            "conditions": {"ORIGINAL_ORDER": "variant_a_v1 + current retrieval candidate order",
                           "NEUTRAL_ORDER": "variant_a_v1 + sha256('selector-neutral-order-v1|case|ref') order"},
            "single_variable": "candidate order only (prompt, model, config, candidates, content fixed)",
            "diagnostic_set_fingerprint": diag["fingerprint"],
            "diagnostic_case_count": len(diag_ids),
            "calls": {"ORIGINAL_ORDER": len(diag_ids), "NEUTRAL_ORDER": len(diag_ids),
                      "total": 2 * len(diag_ids)},
            "reuse_option": "ORIGINAL_ORDER outputs already exist (Variant A DEV/HOLDOUT runs); rerunning "
                            "them controls for provider drift, reuse would halve the calls",
            "cost": "PRICE_REQUIRED",
            "actual_calls_this_phase": 0,
        }
        plan["plan_fingerprint"] = px.plan_fingerprint(plan)

        # 9. contamination accounting + final validation proposal
        primary_ids = [s.case.case_id for s in sem if s.case.primary]
        evaluable_primary = [s.case.case_id for s in sem if s.case.primary and s.selector_evaluable]
        source_of = {s.case.case_id: s.case.source_case_id for s in sem}
        adj_manifest = json.loads((Path(args.adjudication_dir) / "manifest.json").read_text(encoding="utf-8"))
        pm = Path(args.postmortem)
        inspected = set(json.loads(l)["case_id"] for l in (pm / "failure-cases.jsonl").read_text(
            encoding="utf-8").splitlines() if l.strip())
        inspected |= {json.loads(l)["case_id"] for l in (pm / "specificity-cases.jsonl").read_text(
            encoding="utf-8").splitlines() if l.strip()}

        def queue_ids(items):
            return {x if isinstance(x, str) else x["case_id"] for x in items}

        inspected |= queue_ids(json.loads((pm / "review-queue.json").read_text(encoding="utf-8"))["review_queue"])
        inspected |= queue_ids(json.loads(Path(args.prompt_review_queue).read_text(encoding="utf-8"))["cases"])
        inspected |= set(qp.CRITICAL_CASES)
        groups = {"previous_DEV": split["dev"], "previous_HOLDOUT": split["holdout"],
                  "semantic_adjudication_reviewed": adj_manifest["case_ids"],
                  "postmortem_inspected": sorted(inspected, key=_case_key)}
        contam = qp.contamination(primary_ids, evaluable_primary, source_of, groups)
        core_ids = {r["case_id"] for r in inv if r["expectation"] in ("GENERAL", "SPECIFIC", "MULTI_ACCEPTABLE")}
        proposal = qp.final_validation_proposal(contam["remaining_ids"], sem_by, core_ids, inv_ids,
                                                args.final_size)
        pool = set(contam["remaining_ids"])
        tags_of = lambda i: set(sem_by[i].all_tags)
        contam["remaining_pool_hard_slices"] = {
            "near_qna": sum(1 for i in pool if "near_qna" in tags_of(i)),
            "general_specific": sum(1 for i in pool if "general_specific" in tags_of(i)),
            "kb_overlap_flagged": sum(1 for i in pool if "kb_overlap_flagged" in tags_of(i)),
            "qualifier_sensitive": sum(1 for i in pool if i in inv_ids),
            "qualifier_core": sum(1 for i in pool if i in core_ids),
            "first_candidate_correct": sum(1 for i in pool if qp.original_order(sem_by[i])[0]
                                           in sem_by[i].case.acceptable_candidate_refs),
            "semantically_adjudicated": sum(1 for i in pool if i in set(adj_manifest["case_ids"])),
        }

        # write engineering artifacts
        def jl(rows):
            return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)

        eng = out / "engineering"
        eng.mkdir(parents=True, exist_ok=True)
        (eng / "qualifier-inventory.jsonl").write_text(jl(inv), encoding="utf-8")
        (eng / "order-analysis.jsonl").write_text(jl(order_rows), encoding="utf-8")
        _dump(inv_summary, eng / "inventory-summary.json")
        _dump(gs_rows, eng / "general-specific-pairs.json")
        _dump({"summary": order_summary}, eng / "order-summary.json")
        _dump({"qualifier_set": assoc_qual, "challenge_set": assoc_challenge, "verdicts": verdicts},
              eng / "position-association.json")
        _dump({"challenge_set": {n: {k: v for k, v in c.items() if k != "rows"} for n, c in comp.items()},
               "qualifier_set": {n: {k: v for k, v in c.items() if k != "rows"} for n, c in comp_qual.items()},
               "rows_challenge": {n: c["rows"] for n, c in comp.items()},
               "method": "deterministic lexicon heuristic; no LLM judge; review_queue lists uncertain rows"},
              eng / "prompt-rule-compliance.json")
        _dump(critical, eng / "critical-471-472.json")
        exp_dir = out / "order-experiment"
        _dump(diag, exp_dir / "diagnostic-set.json")
        _dump(plan, exp_dir / "plan.json")
        _dump({cid: {"only_order_differs": c["only_order_differs"], "order_changed": c["order_changed"],
                     "ORIGINAL_ORDER": c["ORIGINAL_ORDER"]["order"], "NEUTRAL_ORDER": c["NEUTRAL_ORDER"]["order"],
                     "user_payload_sha256": {k: sha256_text(c[k]["user"]) for k in ("ORIGINAL_ORDER", "NEUTRAL_ORDER")}}
               for cid, c in conditions.items()}, exp_dir / "conditions.json")
        fv = out / "final-validation"
        _dump({k: v for k, v in contam.items()}, fv / "contamination.json")
        _dump(proposal, fv / "proposal.json")
        _dump(holdout_record, out / "holdout-immutability.json")
        summary = {
            "review_packet": {"path": str(review_dir), "fingerprint": review_fp,
                              "candidate_counts": {r["case_id"]: r["candidate_count"] for r in records}},
            "inventory": {k: inv_summary[k] for k in ("total_qualifier_sensitive", "by_split", "by_mode",
                                                      "semantic_evaluable", "excluded", "expectation")},
            "order": order_summary,
            "association_qualifier": {n: {k: v for k, v in e.items()} for n, e in assoc_qual["by_run"].items()},
            "association_groups": {k: v["cases"] for k, v in assoc_qual["groups"].items()},
            "verdicts": {s: {n: v["verdict"] for n, v in d.items()} for s, d in verdicts.items()},
            "compliance_variant_a_challenge": comp["variant_a_v1"]["counts"],
            "compliance_variant_a_qualifier": comp_qual["variant_a_v1"]["counts"],
            "compliance_production_challenge": comp["production"]["counts"],
            "critical": {cid: {k: c[k] for k in ("accepted_retrieval_positions", "qna342_retrieval_position",
                                                 "neutral_order_positions")} for cid, c in critical.items()},
            "diagnostic": {"size": diag["size"], "ids": diag_ids, "calls": plan["calls"],
                           "plan_fingerprint": plan["plan_fingerprint"]},
            "contamination": {k: v for k, v in contam.items() if k != "remaining_ids"},
            "final_validation": {k: v for k, v in proposal.items() if k != "proposed_ids"},
            "live_calls": 0,
        }
        _dump(summary, out / "summary.json")
    _dump(summary)


def cmd_qualifier_review_lock(args) -> None:
    """Lock the filled 471/472 blind template and derive Semantic Gold V1.1."""
    from benchmarks.selector_v2 import semantic_holdout as sh
    from benchmarks.selector_v2 import variant_c as vc
    from benchmarks.selector_v2.snapshot import load_snapshot

    with no_live_calls():
        manifest, snapshots = load_snapshot(Path(args.snapshot))
        by_id = {s.case.case_id: s for s in snapshots}
        lock = vc.lock_qualifier_review(decisions_bytes=Path(args.decisions).read_bytes(),
                                        review_dir=Path(args.review_dir), snapshots=by_id)
        if args.expect_all_select and any(r["blind_decision"] != "SELECT_ACCEPTABLE" for r in lock["records"]):
            raise SystemExit("expected SELECT_ACCEPTABLE for every row")
        lock_manifest = vc.write_lock(Path(args.lock_dir), lock,
                                      dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        v1_manifest, v1_cases = sh.load_semantic_gold(Path(args.semantic_gold), vc.PARENT_GOLD_FP)
        built = vc.build_gold_v11(v1_cases, lock["records"], parent_fp=vc.PARENT_GOLD_FP,
                                  lock_fp=lock["lock_fingerprint"])
        files = {"semantic-gold.jsonl": "".join(c.model_dump_json() + "\n" for c in built["cases"]),
                 "changes.jsonl": "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                                          for r in built["changes"])}
        from benchmarks.selector_v2 import adjudication as adj

        hashes = adj.write_immutable(Path(args.gold_out), files)
        adj.write_immutable(Path(args.gold_out), {"manifest.json": json.dumps({
            "version": vc.GOLD_VERSION, "semantic_gold_fingerprint": built["fingerprint"],
            "parent_semantic_gold_fingerprint": vc.PARENT_GOLD_FP,
            "parent_snapshot_fingerprint": manifest["snapshot_fingerprint"],
            "qualifier_review_lock_fingerprint": lock["lock_fingerprint"],
            "changed_cases": [c["case_id"] for c in built["changes"] if c["gold_changed"]],
            "outcomes": {c["case_id"]: c["derived_outcome"] for c in built["changes"]},
            "artifact_sha256": hashes}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"})
        sh.load_semantic_gold(Path(args.semantic_gold), vc.PARENT_GOLD_FP)   # parent still intact
    _dump({"lock_fingerprint": lock_manifest["lock_fingerprint"],
           "source_decision_sha256": lock_manifest["source_decision_sha256"],
           "records": [{k: r[k] for k in ("case_id", "blind_decision", "acceptable_labels", "mapped_refs",
                                          "reviewer")} for r in lock["records"]],
           "semantic_gold_v11": built["fingerprint"],
           "outcomes": {c["case_id"]: (c["derived_outcome"], c["gold_changed"]) for c in built["changes"]}})


def _variant_c_inputs(args):
    from benchmarks.selector_v2 import prompt_experiment as px
    from benchmarks.selector_v2 import semantic_gold as sg
    from benchmarks.selector_v2 import variant_c as vc
    from benchmarks.selector_v2.challenge import load_challenge
    from benchmarks.selector_v2.prompt_contract import (
        load_committed_manifest, load_prompt, prompt_manifest, serializer_contract_fingerprint,
    )
    from benchmarks.selector_v2.runner import RESULTS_FILE, load_results
    from benchmarks.selector_v2.snapshot import load_snapshot

    manifest, snapshots = load_snapshot(Path(args.snapshot))
    cm, cases = load_challenge(Path(args.challenge), manifest)
    split = px.load_split(challenge_ids={c["case_id"] for c in cases})
    gold_manifest, gold_cases = vc.load_gold_v11(Path(args.semantic_gold), args.expected_gold_fp)
    if prompt_manifest() != load_committed_manifest():
        raise SystemExit("prompt manifest drift")
    prompt, base = load_prompt(vc.PROMPT_ID), load_prompt(vc.BASE_PROMPT_ID)
    if prompt.fingerprint != args.expected_prompt_fp:
        raise SystemExit("variant_c_v1 fingerprint differs from the frozen value")
    runs = {"production": load_results(Path(args.stage_a_run) / RESULTS_FILE).by_case,
            "variant_a_v1": load_results(Path(args.a_run) / RESULTS_FILE).by_case,
            "variant_b_v1": load_results(Path(args.b_run) / RESULTS_FILE).by_case}
    # Saved Variant A HOLDOUT decisions for the two known-regression cases only (diagnostic context).
    a_hold = load_results(Path(args.a_holdout_run) / RESULTS_FILE).by_case
    runs["variant_a_v1"] = {**runs["variant_a_v1"], **{c: a_hold[c] for c in vc.KNOWN_REGRESSIONS}}
    return {"manifest": manifest, "snapshots": snapshots, "cm": cm, "split": split,
            "gold_manifest": gold_manifest, "gold_fp": gold_manifest["semantic_gold_fingerprint"],
            "sem": sg.semantic_snapshots(snapshots, gold_cases), "prompt": prompt, "base": base,
            "runs": runs, "serializer_fp": serializer_contract_fingerprint()}


def cmd_variant_c_prep(args) -> None:
    """Offline: freeze V1.1 DEV baselines, heuristic, gate and the 97-call plan."""
    from benchmarks.selector_v2 import variant_c as vc
    from benchmarks.selector_v2.providers import selector_config
    from benchmarks.selector_v2.tokens import estimate

    with no_live_calls():
        ctx = _variant_c_inputs(args)
        out = Path(args.out)
        if (out / "runs").exists() or (out / "diagnostics").exists():
            raise SystemExit("Variant C outputs already exist: the baseline must precede them")
        identity = {"snapshot_fingerprint": ctx["manifest"]["snapshot_fingerprint"],
                    "split_fingerprint": ctx["split"]["split_fingerprint"],
                    "semantic_gold_fingerprint": ctx["gold_fp"],
                    "variant_c_prompt_fingerprint": ctx["prompt"].fingerprint,
                    "variant_a_prompt_fingerprint": ctx["base"].fingerprint,
                    "saved_runs": {"production": Path(args.stage_a_run).name,
                                   "variant_a_v1": Path(args.a_run).name,
                                   "variant_b_v1": Path(args.b_run).name},
                    "saved_results_sha256": {n: vc.file_sha256(Path(p) / "results.jsonl") for n, p in
                                             (("production", args.stage_a_run), ("variant_a_v1", args.a_run),
                                              ("variant_b_v1", args.b_run))}}
        baseline = vc.build_prelive_baseline(snapshots=ctx["snapshots"], sem=ctx["sem"], split=ctx["split"],
                                             runs=ctx["runs"], identity=identity)
        baseline["frozen_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        path = out / vc.BASELINE_FILE
        if path.exists():
            prior = vc.verify_baseline(path)
            if prior["baseline_fingerprint"] != baseline["baseline_fingerprint"]:
                raise SystemExit("a different frozen Variant C baseline exists")
            baseline = prior
        else:
            _dump(baseline, path)
        config = selector_config(provider="openrouter", model="openai/gpt-4o-mini", temperature=0.0,
                                 max_tokens=32)
        by_id = {s.case.case_id: s for s in ctx["snapshots"]}
        ids = ctx["split"]["dev"] + list(vc.KNOWN_REGRESSIONS)
        est = estimate([by_id[i] for i in ids], max_tokens=32, primary_only=True,
                       system_prompt=ctx["prompt"].text)
        plan = vc.build_plan(config=config, prompt=ctx["prompt"], base_prompt=ctx["base"], split=ctx["split"],
                             baseline=baseline, snapshot_fp=ctx["manifest"]["snapshot_fingerprint"],
                             serializer_fp=ctx["serializer_fp"], gold_fp=ctx["gold_fp"],
                             estimate_block={"input_tokens_approx": est["input_tokens"]["total"],
                                             "output_tokens_upper_bound": est["output_tokens_upper_bound_total"],
                                             "callable_cases": est["calls_per_config"]})
        if est["calls_per_config"] != vc.MAX_TOTAL_CALLS:
            raise SystemExit(f"{est['calls_per_config']} callable cases, expected {vc.MAX_TOTAL_CALLS}")
        plan_path = out / vc.PLAN_FILE
        if plan_path.exists() and json.loads(plan_path.read_text())["plan_fingerprint"] != plan["plan_fingerprint"]:
            raise SystemExit("a different Variant C plan exists")
        _dump(plan, plan_path)
    b = baseline["runs"]
    _dump({"baseline_fingerprint": baseline["baseline_fingerprint"], "plan_fingerprint": plan["plan_fingerprint"],
           "dev_evaluable": baseline["dev_evaluable"], "qualifier_slice": len(baseline["qualifier_slice_ids"]),
           "first_candidate": baseline["first_candidate"],
           "runs": {n: {k: r[k] for k in ("exact", "false_none", "none_count", "unstated_qualifier_assumed_dev",
                                          "unstated_qualifier_assumed_slice", "slices")} for n, r in b.items()},
           "known": baseline["known_regressions_saved"], "estimate": plan["estimated_tokens"]})


def cmd_variant_c_run(args) -> None:
    """variant_c_v1 on DEV (95) or the two known-regression diagnostics (471, 472)."""
    from benchmarks.selector_v2 import semantic_holdout as sh
    from benchmarks.selector_v2 import variant_c as vc
    from benchmarks.selector_v2.providers import (
        FAKE_PROVIDER, FakeSelectorProvider, LiveSelectorBackend, selector_config,
    )
    from benchmarks.selector_v2.runner import ResultStore, RunIdentity, run_benchmark

    check_live_gate(live=args.live, confirmed=args.confirm_live_provider_calls,
                    provider=args.provider, model=args.model)
    with no_live_calls():
        ctx = _variant_c_inputs(args)
        split, prompt = ctx["split"], ctx["prompt"]
        case_ids = vc.scope_case_ids(args.scope, split)
        baseline = vc.verify_baseline(Path(args.baseline_dir) / vc.BASELINE_FILE)
        if baseline["identity"]["semantic_gold_fingerprint"] != ctx["gold_fp"]:
            raise SystemExit("baseline belongs to another Semantic Gold")
        if args.live:
            config = selector_config(provider=args.provider, model=args.model,
                                     temperature=args.temperature, max_tokens=args.max_tokens)
            plan = json.loads((Path(args.baseline_dir) / vc.PLAN_FILE).read_text(encoding="utf-8"))
            vc.validate_plan(plan, args.approve_plan_fingerprint, config=config, prompt=prompt, split=split,
                             baseline=baseline, snapshot_fp=ctx["manifest"]["snapshot_fingerprint"],
                             serializer_fp=ctx["serializer_fp"], gold_fp=ctx["gold_fp"])
            inner, mode = LiveSelectorBackend(config, prompt=prompt), "LIVE"
        else:
            config = selector_config(provider=FAKE_PROVIDER, model=f"fake-{args.fake_policy}", max_tokens=32)
            inner, mode = FakeSelectorProvider(args.fake_policy, config, prompt=prompt), \
                f"DRY_RUN_FAKE:{args.fake_policy}"
        identity = RunIdentity(selector_contract_fingerprint=ctx["serializer_fp"], config=config,
                               snapshot_fingerprint=ctx["manifest"]["snapshot_fingerprint"], run_mode=mode,
                               prompt_fingerprint=prompt.fingerprint,
                               split_fingerprint=split["split_fingerprint"])
        out = Path(args.out) / ("diagnostics" if args.scope == "diagnostic" else "")
        already = ResultStore(out / "runs" / identity.run_id, identity).load().by_case
        if set(already) - set(case_ids):
            raise SystemExit("run directory holds cases outside this scope")
        budget = (vc.MAX_DIAGNOSTIC_CALLS if args.scope == "diagnostic" else vc.MAX_DEV_CALLS) - len(already)
        backend = sh.BudgetedBackend(inner, budget, set(case_ids))

    def execute():
        return run_benchmark(ctx["snapshots"], backend, identity, out, concurrency=1,
                             max_cases=len(case_ids), retry_errors=False, primary_only=True,
                             case_ids=set(case_ids))

    if args.live:
        with no_live_calls([LIVE_PROVIDERS[config.provider]], block_sdks=False):
            summary = execute()
    else:
        with no_live_calls():
            summary = execute()
    accounting = {"scope": args.scope, "run_id": identity.run_id, "run_mode": mode,
                  "logical_calls_this_invocation": backend.calls, "previously_completed": len(already),
                  "logical_calls_total": backend.calls + len(already),
                  "refused_by_budget_guard": backend.refused, "summary": summary.__dict__,
                  "production_calls": 0, "variant_a_calls": 0, "variant_b_calls": 0,
                  "old_holdout_full_run": False}
    _dump(accounting, Path(summary.run_dir) / "call-accounting.json")
    _dump(accounting)


def cmd_variant_c_eval(args) -> None:
    from benchmarks.selector_v2 import variant_c as vc
    from benchmarks.selector_v2.contract import sha256_text
    from benchmarks.selector_v2.runner import RESULTS_FILE, RUN_MANIFEST, load_results

    with no_live_calls():
        ctx = _variant_c_inputs(args)
        out = Path(args.out)
        baseline = vc.verify_baseline(out / vc.BASELINE_FILE)
        plan = json.loads((out / vc.PLAN_FILE).read_text(encoding="utf-8"))
        loaded, accounts, raw = {}, {}, {}
        for scope, root in (("dev", out / "runs"), ("diagnostic", out / "diagnostics" / "runs")):
            dirs = [d for d in root.iterdir() if d.is_dir()]
            if len(dirs) != 1:
                raise SystemExit(f"{scope}: expected exactly one run dir, found {len(dirs)}")
            ident = json.loads((dirs[0] / RUN_MANIFEST).read_text(encoding="utf-8"))
            if ident["prompt_fingerprint"] != ctx["prompt"].fingerprint or \
                    ident["split_fingerprint"] != ctx["split"]["split_fingerprint"] or \
                    (ident["run_mode"] == "LIVE" and ident["config_fingerprint"] != plan["config_fingerprint"]):
                raise SystemExit(f"{scope}: run identity mismatch")
            loaded[scope] = load_results(dirs[0] / RESULTS_FILE)
            accounts[scope] = json.loads((dirs[0] / "call-accounting.json").read_text(encoding="utf-8"))
            raw[scope] = (dirs[0] / RESULTS_FILE).read_text(encoding="utf-8")
        metrics = vc.evaluate(baseline=baseline, snapshots=ctx["snapshots"], sem=ctx["sem"], runs=ctx["runs"],
                              variant_c=loaded["dev"].by_case, diagnostics=loaded["diagnostic"].by_case)
        scored = metrics.pop("scored_rows")
        integrity = {s: {"unique": len(l.by_case), "superseded": l.superseded, "corrupt": l.corrupt_lines}
                     for s, l in loaded.items()}
        files = {
            "responses.jsonl": raw["dev"] + raw["diagnostic"],
            "semantic-scores.jsonl": "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                                             for r in scored),
            "paired-comparison.json": json.dumps(metrics["paired"], indent=2, sort_keys=True) + "\n",
            "qualifier-analysis.json": json.dumps({n: {k: r[k] for k in (
                "unstated_qualifier_assumed_dev", "unstated_qualifier_assumed_slice", "unstated_ids_dev",
                "compliance_dev", "compliance_slice")} for n, r in metrics["runs"].items()},
                ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "known-regressions.json": json.dumps(metrics["known_regressions"], ensure_ascii=False,
                                                 indent=2, sort_keys=True) + "\n",
            "metrics.json": json.dumps({**metrics, "integrity": integrity, "call_accounting": accounts},
                                       ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        }
        for name, text in files.items():
            (out / name).write_text(text, encoding="utf-8")
        gate = metrics["gate"]
        _dump({"phase": "7B Variant C DEV", "baseline_fingerprint": baseline["baseline_fingerprint"],
               "baseline_reverified_after_scoring": True, "plan_fingerprint": plan["plan_fingerprint"],
               "semantic_gold_fingerprint": ctx["gold_fp"], "prompt_fingerprint": ctx["prompt"].fingerprint,
               "gate_passed": gate["passed"],
               "outcome": vc.DEV_GATE["pass_label"] if gate["passed"] else vc.DEV_GATE["fail_label"],
               "artifact_sha256": {n: sha256_text(t) for n, t in files.items()}}, out / "manifest.json")
    _dump({"gate": gate, "integrity": integrity,
           "exact": {n: r["exact"] for n, r in metrics["runs"].items()},
           "paired": metrics["paired"], "known": {c: (k["selected"], k["correct"]) for c, k in
                                                  metrics["known_regressions"].items()}})


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

    p = sub.add_parser("adjudication-lock", help="lock merged human decisions (no audit access)")
    for name in ("--decisions", "--expected-sha256", "--review-dir", "--followup-dir"):
        p.add_argument(name, required=True)
    p.set_defaults(func=cmd_adjudication_lock)

    p = sub.add_parser("semantic-apply-rescore",
                       help="after lock: Semantic Gold V1 + re-score saved outputs")
    for name in ("--review-dir", "--snapshot", "--challenge", "--stage-a-run", "--gold-out",
                 "--rescore-out"):
        p.add_argument(name, required=True)
    p.add_argument("--run", action="append", required=True, help="name=run_dir")
    p.set_defaults(func=cmd_semantic_apply_rescore)

    def holdout_args(p):
        for name in ("--snapshot", "--challenge", "--stage-a-run", "--semantic-gold", "--out",
                     "--expected-snapshot-fp", "--expected-challenge-fp", "--expected-split-fp",
                     "--expected-gold-fp", "--expected-prompt-fp"):
            p.add_argument(name, required=True)

    p = sub.add_parser("semantic-holdout-prep",
                       help="freeze semantic HOLDOUT baseline + promotion gate + 42-call plan")
    holdout_args(p)
    p.add_argument("--rescore", required=True, help="semantic-rescore.json (DEV semantic gate)")
    p.add_argument("--dev-run", required=True, help="variant_a_v1 DEV run dir (token calibration)")
    p.set_defaults(func=cmd_semantic_holdout_prep)

    p = sub.add_parser("semantic-holdout-run", help="variant_a_v1 on HOLDOUT (gated, 42 calls max)")
    holdout_args(p)
    p.add_argument("--baseline-dir", required=True)
    p.add_argument("--selected-variant", required=True)
    p.add_argument("--live", action="store_true")
    p.add_argument(CONFIRM_FLAG, dest="confirm_live_provider_calls", action="store_true")
    p.add_argument("--provider")
    p.add_argument("--model")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=32)
    p.add_argument("--approve-plan-fingerprint")
    p.add_argument("--fake-policy", default="oracle")
    p.set_defaults(func=cmd_semantic_holdout_run)

    p = sub.add_parser("qualifier-postmortem",
                       help="471/472 blind packet + qualifier/order analysis + order-experiment prep")
    for name in ("--snapshot", "--challenge", "--stage-a-run", "--semantic-gold", "--expected-gold-fp",
                 "--dev-run", "--holdout-run", "--holdout-dir", "--postmortem", "--adjudication-dir",
                 "--prompt-review-queue", "--out"):
        p.add_argument(name, required=True)
    p.add_argument("--final-size", type=int, default=120)
    p.set_defaults(func=cmd_qualifier_postmortem)

    p = sub.add_parser("qualifier-review-lock", help="lock 471/472 review and derive Semantic Gold V1.1")
    for name in ("--decisions", "--review-dir", "--snapshot", "--lock-dir", "--semantic-gold", "--gold-out"):
        p.add_argument(name, required=True)
    p.add_argument("--expect-all-select", action="store_true")
    p.set_defaults(func=cmd_qualifier_review_lock)

    def variant_c_args(p):
        for name in ("--snapshot", "--challenge", "--semantic-gold", "--expected-gold-fp",
                     "--expected-prompt-fp", "--stage-a-run", "--a-run", "--a-holdout-run", "--b-run",
                     "--out"):
            p.add_argument(name, required=True)

    p = sub.add_parser("variant-c-prep", help="freeze Variant C DEV baselines, gate and plan")
    variant_c_args(p)
    p.set_defaults(func=cmd_variant_c_prep)

    p = sub.add_parser("variant-c-run", help="variant_c_v1 on DEV or the 471/472 diagnostics (gated)")
    variant_c_args(p)
    p.add_argument("--scope", required=True)
    p.add_argument("--baseline-dir", required=True)
    p.add_argument("--live", action="store_true")
    p.add_argument(CONFIRM_FLAG, dest="confirm_live_provider_calls", action="store_true")
    p.add_argument("--provider")
    p.add_argument("--model")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=32)
    p.add_argument("--approve-plan-fingerprint")
    p.add_argument("--fake-policy", default="oracle")
    p.set_defaults(func=cmd_variant_c_run)

    p = sub.add_parser("variant-c-eval", help="score Variant C DEV + diagnostics and apply the gate")
    variant_c_args(p)
    p.set_defaults(func=cmd_variant_c_eval)

    p = sub.add_parser("semantic-holdout-eval", help="score variant_a_v1 HOLDOUT on Semantic Gold")
    holdout_args(p)
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=cmd_semantic_holdout_eval)

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
