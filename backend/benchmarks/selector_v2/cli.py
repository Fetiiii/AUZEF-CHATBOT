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
