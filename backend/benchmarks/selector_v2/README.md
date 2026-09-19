# Selector V2 benchmark harness (Phase 7A)

Selector-only evaluation of Selector V2 on a **frozen candidate snapshot**.
Every model or config sees exactly the same candidates, built once by the
production retrieval + eligibility code. The harness never calls the Intent
Analyzer and never re-implements the selector prompt, schema or parser.

Report: [`docs/answer-pipeline-v2/PHASE_7A_REPORT.md`](../../../docs/answer-pipeline-v2/PHASE_7A_REPORT.md).

## Modules

| Module | Role |
|---|---|
| `schema.py` | Versioned contracts: `BenchmarkCase` (dataset input), `FrozenCandidate`, `CaseSnapshot`, `BenchmarkResult`, `Outcome` |
| `gold_loader.py` | Generic `BenchmarkCase` JSONL loader plus the reviewed Gold v2 adapter (sha256 + count validation) |
| `snapshot.py` | Candidate snapshot generation, miss classification, fingerprint, diagnostics, persistence |
| `contract.py` | Production contract reuse (`build_selector_prompt`) and the selector contract fingerprint |
| `providers.py` | Fake policies (dry run) and the gated live backend. Both use production `ask_with_result` |
| `runner.py` | Case-by-case execution, resume, identity-scoped result files |
| `evaluator.py` | Denominators, metrics, slices, matrices, Markdown report |
| `stats.py` | Paired comparison and the exact McNemar test |
| `tokens.py` | Pre-run token estimate (APPROXIMATE without `tiktoken`), optional cost from user-supplied prices |
| `safety.py` | Network/provider-SDK guard and the live gate |
| `near_qna_pairs.json` | Near-QnA / general-specific pair definitions (ids + real canonical questions) |

## Commands

All commands run from `backend/` with `python -m benchmarks.selector_v2 …`.
Generated artifacts belong under `outputs/` at the repo root. That directory
is git-ignored because it contains real user messages from the dataset.

The backend image has no `git` or `pytest`. The examples mount the repository
into it:

```bash
IMG=auzefchatbot-backend
RUN="docker run --rm -v $PWD:/repo -w /repo/backend --entrypoint python"
GOLD=/path/to/AUZEF-Chat-Analiz-kb-migration-v31/outputs

# 1) validate + convert the dataset (offline, no network)
$RUN --network none -v $GOLD:/gold:ro $IMG -m benchmarks.selector_v2 load-gold \
  --gold-dir /gold/gold-v2-reviewed-v2-20260918 \
  --session-dir /gold/session-gold-v2-reviewed-v2-20260918 \
  --out /repo/outputs/selector-v2-benchmark/dataset

# 2) freeze candidates (read-only Postgres/Meili/Qdrant; env file WITHOUT LLM keys)
$RUN --network auzefchatbot_default --env-file infra-only.env \
  -e HF_HOME=/hf -e HF_HUB_OFFLINE=1 -v auzefchatbot_hf_cache:/hf:ro \
  -e BENCHMARK_GIT_SHA=$(git rev-parse HEAD) -v $GOLD:/gold:ro $IMG \
  -m benchmarks.selector_v2 snapshot --gold-dir … --session-dir … \
  --as-of 2026-09-19 --out /repo/outputs/selector-v2-benchmark/snapshots/<name>

# 3) pre-run estimate (no $ unless prices are passed explicitly)
… estimate --snapshot <dir> --configs 5 [--input-price-per-1m X --output-price-per-1m Y]

# 4) dry run (DEFAULT; fake provider, zero provider calls) → HARNESS SELF-TEST
… run --snapshot <dir> --out <root> [--fake-policy oracle|always_none|first_candidate|
      malformed|unknown_ref|empty|timeout|model_error]

# 5) live run: needs BOTH flags and an explicit provider/model (costs money)
… run --snapshot <dir> --out <root> --live --confirm-live-provider-calls \
      --provider openrouter --model openai/gpt-4o-mini --max-tokens 32

# 6) metrics / paired comparison / production config (read-only)
… evaluate --snapshot <dir> --run-dir <root>/runs/<run_id>
… compare  --snapshot <dir> --run-a <run_dir> --run-b <run_dir>
… config-snapshot
```

## Contracts in one paragraph

A case is one resolved intent: `SELECT` with at least one acceptable ref
(`qna:<id>` / `calendar:<id>`; any of them is correct), or `NONE` with none.
Excluded, hold and context-required cases are loaded but never enter a
denominator. A snapshot records, per case, the selector candidate set in
production order and the pool status: `IN_POOL`, `RETRIEVAL_MISS`,
`ELIGIBILITY_MISS` or `BUDGET_MISS`. Only `IN_POOL` (or `NOT_APPLICABLE` for
NONE) cases are selector-evaluable.

The model sees only `resolved_intent` and `candidate_ref`, `kind`,
`canonical_text`, `answer_text`. Score, rank, source and alias flags stay in
the snapshot for diagnostics.

Each run has its own identity: selector contract fingerprint, config
fingerprint, snapshot fingerprint, run mode and candidate order. It writes to
`runs/<run_id>/results.jsonl`. Resume skips completed keys, rejects foreign
results and ignores torn lines.

## Phase 7B-Prep additions

| Module | Role |
|---|---|
| `challenge.py` | First-candidate baseline, frozen challenge set (`challenge-v1`), rescue/corruption metrics, FULL vs CHALLENGE report |
| `plan.py` | Registry discovery, fingerprinted live plan, approval validation |

```bash
… challenge --snapshot <snap> --out <root>/challenge-v1          # freeze (immutable)
… run --snapshot <snap> --challenge <root>/challenge-v1 --out <r> --fake-policy first_candidate
… challenge-eval --snapshot <snap> --challenge <root>/challenge-v1 --run-dir <run_dir>
… registry-models --out <root>/registry-models.json              # read-only, guarded
… live-plan --snapshot <snap> --challenge <root>/challenge-v1 \
    --registry <root>/registry-models.json --production-config <root>/production-config.json \
    [--prior-model provider/model] [--price provider/model=IN:OUT] --out <root>/live-plan.json
# live (Stage A): all 7A gates + the plan and its fingerprint
… run … --challenge <root>/challenge-v1 --live --confirm-live-provider-calls \
    --provider P --model M [--reasoning-effort low|medium|high] \
    --live-plan <root>/live-plan.json --approve-plan-fingerprint <plan_fingerprint>
```

Reasoning levels travel only through the adapter transport
(`services.llm_config.REASONING_TRANSPORT`: openai/openrouter
low|medium|high). Any other level is refused before a request.
