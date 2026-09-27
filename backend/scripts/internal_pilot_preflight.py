"""INTERNAL_PILOT freeze manifest generator and runtime preflight CLI.

Usage (from ``backend/``)::

    python -m scripts.internal_pilot_preflight manifest [--write]
    python -m scripts.internal_pilot_preflight preflight [--json]
    python -m scripts.internal_pilot_preflight amendment5 [--write] [--gate-evidence FILE]
    python -m scripts.internal_pilot_preflight candidate [--json]

``manifest`` regenerates ``deploy/internal-pilot/answer-pipeline-freeze.json``.
Regeneration is deterministic apart from ``created_at``, which is excluded
from the fingerprint.

``preflight`` compares the live runtime against the frozen baseline and exits
non-zero on any mismatch.

``candidate`` is the authoritative preflight for the pilot candidate (freeze
amendment 5). Unlike ``preflight``/``runtime`` it reads the **DB active
managed config** the runtime actually serves, so it must run where the
backend's database is reachable (inside the backend container). It never modifies configuration: a mismatch is an
operator decision, not something this tool silently repairs.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # direct `python scripts/internal_pilot_preflight.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.internal_pilot_freeze import (  # noqa: E402
    build_freeze_manifest,
    freeze_fingerprint,
    repo_root,
    run_preflight,
)
from services.internal_pilot_candidate import (  # noqa: E402
    AMENDMENT_5_PATH,
    build_freeze_amendment_5,
    collect_live_state,
    run_candidate_preflight,
)
from services.internal_pilot_runtime import run_runtime_preflight  # noqa: E402

MANIFEST_PATH = Path("deploy/internal-pilot/answer-pipeline-freeze.json")
AMENDMENT_5_FILE = Path(*AMENDMENT_5_PATH)


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip()


def _manifest_file() -> Path:
    return repo_root() / MANIFEST_PATH


def cmd_manifest(args: argparse.Namespace) -> int:
    target = _manifest_file()
    # Keep created_at stable across regenerations so that rewriting the file
    # produces no diff when nothing substantive changed.
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if target.exists():
        try:
            created_at = json.loads(target.read_text(encoding="utf-8"))["created_at"]
        except (ValueError, KeyError):
            pass
    manifest = build_freeze_manifest(
        git_commit=args.git_commit or _git_commit(), created_at=created_at
    )
    payload = json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        print(f"wrote {MANIFEST_PATH}")
        print(f"INTERNAL_PILOT_FREEZE_FINGERPRINT = {manifest['freeze_fingerprint']}")
    else:
        print(payload, end="")
    return 0


def _print_report(title: str, report) -> None:
    print(title)
    for check in report.checks:
        mark = "PASS" if check.passed else "FAIL"
        print(f"  [{mark}] {check.name}")
        if not check.passed:
            print(f"         expected: {check.expected}")
            print(f"         actual:   {check.actual}")
            if check.detail:
                print(f"         note:     {check.detail}")


def cmd_runtime(args: argparse.Namespace) -> int:
    """Validate the runtime against the frozen baseline (the 3 blockers)."""
    report = run_runtime_preflight()
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_report("INTERNAL_PILOT runtime preflight", report)
        print(f"\nINTERNAL_PILOT_RUNTIME_PREFLIGHT = {report.status}")
        if report.failures:
            print(
                "Configuration was NOT modified. Resolve each mismatch "
                "explicitly before starting the internal pilot."
            )
    return 0 if report.passed else 1


def cmd_preflight(args: argparse.Namespace) -> int:
    report = run_preflight()
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print("INTERNAL_PILOT preflight")
        for check in report.checks:
            mark = "PASS" if check.passed else "FAIL"
            print(f"  [{mark}] {check.name}")
            if not check.passed:
                print(f"         expected: {check.expected}")
                print(f"         actual:   {check.actual}")
                if check.detail:
                    print(f"         note:     {check.detail}")
        print(f"\nRESULT: {report.status}")
        if report.failures:
            print(
                "Configuration was NOT modified. Resolve each mismatch "
                "explicitly before starting the internal pilot."
            )
    return 0 if report.passed else 1


def cmd_amendment5(args: argparse.Namespace) -> int:
    """Generate the pilot-candidate amendment (never rewrites amendments 1-4)."""
    target = repo_root() / AMENDMENT_5_FILE
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if target.exists():
        try:
            created_at = json.loads(target.read_text(encoding="utf-8"))["created_at"]
        except (ValueError, KeyError):
            pass
    evidence = None
    if args.gate_evidence:
        evidence = json.loads(Path(args.gate_evidence).read_text(encoding="utf-8"))
    amendment = build_freeze_amendment_5(
        git_commit=args.git_commit or _git_commit(), created_at=created_at,
        gate_evidence=evidence,
    )
    payload = json.dumps(amendment, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if args.write:
        target.write_text(payload, encoding="utf-8")
        print(f"wrote {AMENDMENT_5_FILE}")
        print(f"INTERNAL_PILOT_AMENDMENT_5_FINGERPRINT = {amendment['amendment_fingerprint']}")
    else:
        print(payload, end="")
    return 0


def cmd_candidate(args: argparse.Namespace) -> int:
    """Validate the live runtime (DB managed config included) against amendment 5."""
    from core.database import SessionLocal

    with SessionLocal() as db:
        report = run_candidate_preflight(collect_live_state(db))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_report("INTERNAL_PILOT candidate preflight (amendment 5)", report)
        print(f"\nINTERNAL_PILOT_CANDIDATE_PREFLIGHT = {report.status}")
        if report.failures:
            print("Configuration was NOT modified. Resolve each mismatch explicitly.")
    return 0 if report.passed else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Check the committed manifest still matches its own fingerprint."""
    target = _manifest_file()
    if not target.exists():
        print(f"missing {MANIFEST_PATH}", file=sys.stderr)
        return 1
    manifest = json.loads(target.read_text(encoding="utf-8"))
    recomputed = freeze_fingerprint(manifest)
    stored = manifest.get("freeze_fingerprint")
    if recomputed != stored:
        print(f"fingerprint mismatch: stored={stored} recomputed={recomputed}")
        return 1
    print(f"INTERNAL_PILOT_FREEZE_FINGERPRINT = {stored}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    manifest = sub.add_parser("manifest", help="generate the freeze manifest")
    manifest.add_argument("--write", action="store_true")
    manifest.add_argument("--git-commit", default=None)
    manifest.set_defaults(func=cmd_manifest)

    preflight = sub.add_parser("preflight", help="validate runtime against the freeze")
    preflight.add_argument("--json", action="store_true")
    preflight.set_defaults(func=cmd_preflight)

    verify = sub.add_parser("verify", help="verify the committed manifest fingerprint")
    verify.set_defaults(func=cmd_verify)

    runtime = sub.add_parser(
        "runtime", help="validate the runtime against the frozen baseline"
    )
    runtime.add_argument("--json", action="store_true")
    runtime.set_defaults(func=cmd_runtime)

    amendment5 = sub.add_parser("amendment5", help="generate freeze amendment 5")
    amendment5.add_argument("--write", action="store_true")
    amendment5.add_argument("--git-commit", default=None)
    amendment5.add_argument("--gate-evidence", default=None,
                            help="JSON file with analyzer/selector gate results")
    amendment5.set_defaults(func=cmd_amendment5)

    candidate = sub.add_parser(
        "candidate", help="validate DB managed config + runtime against amendment 5"
    )
    candidate.add_argument("--json", action="store_true")
    candidate.set_defaults(func=cmd_candidate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
