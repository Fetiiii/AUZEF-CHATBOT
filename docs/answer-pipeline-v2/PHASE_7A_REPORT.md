# Answer Pipeline V2 — Phase 7A Report

## Status and scope

```text
Phase 7A — Selector Benchmark Harness + Live-Call Approval Gate
STATUS: PASS
```

- Branch: `production-readiness`
- Starting HEAD: `72f60a2b50624c722dd2d703a7e237d8f8ddf382`
- Source of truth:
  [`ANSWER_PIPELINE_V2_ARCHITECTURE.md`](../ANSWER_PIPELINE_V2_ARCHITECTURE.md)
  §10 (Selector V2), §21 Faz 7, §22.4 (selector metrics), §23 (selector
  test classes). Its sha256 `31230299…f8230e` is unchanged from Phase 0
  (`baseline.json`).
- **Production behavior did not change.** The only tracked change outside
  the new tooling and docs is a `/outputs/` line in `.gitignore`. Production
  code does not import `backend/benchmarks/`.
- **No live LLM or provider call was made.** Every harness run used a fake
  provider inside a socket and provider-SDK guard. Snapshot generation read
  only the local Postgres, Meilisearch and Qdrant. The embedding model is
  local (`bge-m3`, HF offline), and the container had no LLM API keys.
- Not started: Phase 7B and later. That includes model or reasoning runs,
  metadata, exact alias, K tuning, bot context, prompt changes and runtime
  config changes.

## Code

| Path | Content |
|---|---|
| `backend/benchmarks/selector_v2/` | The harness package; module map in its [README](../../backend/benchmarks/selector_v2/README.md) |
| `backend/tests/test_selector_benchmark.py` | 43 tests |
| `.gitignore` | `/outputs/` added. Generated artifacts contain real user messages and are never committed |

## Dataset search

The Chatbot repository has no Gold or benchmark artifact: `git ls-files`
and a file search for `gold`, `bench`, `rescore` and `reviewed` found nothing.
The reviewed dataset is in the sibling analysis working tree:

```text
/home/feitcagri/Masaüstü/AUZEF-Chat-Analiz-kb-migration-v31/outputs/
  gold-v2-reviewed-v2-20260918/      gold-reviewed-all.jsonl (516 records) + manifest
  session-gold-v2-reviewed-v2-20260918/  session-targets.jsonl (503) + manifest
  e2e-rescore-reviewed-v2-20260918/  old E2E rescore (reference only, not used)
```

- **Provenance caveat:** these two reviewed directories are **untracked** in
  that repo. Its HEAD is `1057bea`, and the manifests record commit
  `a8a8a15`. The loader therefore does not rely on git. It checks each
  input's sha256 against its own manifest's `outputs_sha256`.
- **Data was read only**, never modified.

Validation (`load-gold`, all PASS unless noted):

| Check | Result |
|---|---|
| sha256 `gold-reviewed-all.jsonl` / `session-targets.jsonl` vs manifests | PASS (`19fdff59…`, `e06f24c9…`) |
| Manifest status counts = actual records | PASS: READY 486, CONTEXT_REQUIRED 17, EXCLUDED_FROM_EVAL 9, CONTENT_REVIEW_HOLD 2, SOURCE_MISSING_HOLD 1, PENDING_CONTENT 1 |
| Evaluation targets / scorable | PASS 503 / 486 |
| Reference denominator 503 / 17 / 486 (documentation check only) | PASS, recomputed from the files |
| Session targets = Gold targets; expected intent groups equal | PASS 503 = 503 |
| Review decisions | 3 CHANGE_GOLD (407, 423, 449), 1 MULTI_ACCEPTABLE_QNA (184), 9 KB_OVERLAP_NEEDS_FIX, 2 NEEDS_CONTENT_REVIEW, 2 EXCLUDE_AMBIGUOUS |
| Expected QnA ids vs local KB | 87/87 present and active; canonical question and answer text identical for all 84 ids whose Gold record carries text |
| PII screen (email, phone, ≥9-digit id) on intent texts | PASS, no pattern hit |
| Expected-NONE targets | **WARN: none exist.** Reviewed Gold has only SELECT targets |

## Benchmark case contract

`BenchmarkCase` (`benchmark_version = "selector-v2-1"`, Pydantic,
`extra=forbid`) is the external dataset contract. Any dataset can be supplied
as JSONL with `--cases`.

```json
{"case_id": "407", "intent_text": "…", "expected_decision": "SELECT",
 "acceptable_candidate_refs": ["qna:336"], "evaluation_status": "SELECTOR_EVALUABLE",
 "primary": true, "review_decision": "CHANGE_GOLD", "calendar_relevant": null,
 "tags": ["single_intent", "single_acceptable", "first_turn", "context_independent", "qna_only"]}
```

- **SELECT:** `expected_decision="SELECT"` with at least one ref. A ref
  matches `^(qna|calendar):[1-9][0-9]*$`, and refs must be unique.
- **NONE:** `expected_decision="NONE"` with `acceptable_candidate_refs=[]`.
  A NONE case carrying refs is rejected.
- **Multi-acceptable:** several refs, e.g. case 184 → `["qna:317", "qna:320"]`.
  Selecting any of them is correct. This is **not** multi-intent.
- **Evaluation status:**
  - `SELECTOR_EVALUABLE`
  - `NOT_SELECTOR_EVALUABLE` (context required, or no intent-level
    expectation)
  - `EXCLUDED`
  - `HOLD`

  Every status other than evaluable needs a `status_reason`.
- **Review decisions:** strings such as `CHANGE_GOLD` or
  `EXCLUDE_AMBIGUOUS` are stored as labels (`review_decision`,
  `review:<decision>` tag). The loader branches on record `status`, never on
  those exact strings.

### Exclusion, hold, context and multi-intent policy

| Dataset state | Case status | In any denominator? |
|---|---|---|
| READY, single intent, `intent_text == user_message` (484) | `SELECTOR_EVALUABLE`, `primary=true` | yes (primary) |
| READY, multi-intent with reviewed intent-level decomposition (71, 480 → 4 intent cases) | `SELECTOR_EVALUABLE`, `primary=false`, tags `multi_intent_derived` and `reformulated_intent_text` | separate slice only |
| CONTEXT_REQUIRED (17) | `NOT_SELECTOR_EVALUABLE` (`context_required_unresolved_intent`) | no |
| EXCLUDED_FROM_EVAL (9) | `EXCLUDED` | no |
| CONTENT_REVIEW_HOLD (2), SOURCE_MISSING_HOLD (1), PENDING_CONTENT (1) | `HOLD` | no |

- **Primary set:** the 484 single-intent READY targets. Each one's
  `intent_text` is the raw user message verbatim, so a selector-only run
  needs no Intent Analyzer and no silent reformulation (§15).
- **Primary denominator:** 484 − 2 = **482**. Both retrieval misses (cases
  74 and 436) are primary cases, and misses are outside the selector
  denominator.
- **Multi-intent cases 71 and 480:** their intent texts are human
  reformulations, not production analyzer output. They stay out of the
  primary metric (§16).
- **Context-required cases:** no context is resolved, and the Intent
  Analyzer is never run to change the dataset.

### Calendar policy

- **Contract support:** `calendar:<id>` refs and `CALENDAR` candidates are
  supported and tested.
- **Calendar route:** retrieval runs only when a case explicitly sets
  `calendar_relevant: true`. Otherwise the route stays closed, which is what
  production does for `calendar_relevant=false`.
- **This dataset:** reviewed Gold carries no Calendar relevance and no
  Calendar expected ids. Its refs are all QnA. So the snapshot contains no
  Calendar candidates, and no Calendar candidate is ever judged right or
  wrong automatically.
- **Consequence:** Calendar selector behavior is **not measurable** on this
  dataset.

## Candidate snapshot

**Generation** (`snapshot`) calls production
`answer_pipeline._build_candidate_pool_result` per case, once:

- Qdrant 24 + Meili 5 retrieval.
- Objective eligibility with the real `RoutingGuardPolicy` (as of
  2026-09-19) and the real `status=1` lookup.
- Production budget 32.

The selector is not called and neither is the Intent Analyzer. The generator
aborts if Meili trips unhealthy or any case gets zero Qdrant hits, so
provider errors that production swallows cannot silently shrink a pool.

**Artifact** (`outputs/selector-v2-benchmark/snapshots/3e558768561814da/`, git-ignored):

- **`snapshot.jsonl`:** per case, the candidates in production order. Each
  candidate carries `candidate_ref`, `kind`, `canonical_text`, `answer_text`,
  plus diagnostic-only `order`, `source`, `retrieval_stage`, `score` and
  `alias_match`. The record also holds `retrieved_refs`, eligibility
  exclusions with reasons, truncated refs, retrieval counts, `pool_status` and
  derived tags.
- **`snapshot-manifest.json`:**

  | Field | Value |
  |---|---|
  | `created_at` | recorded |
  | `git_sha` | `72f60a2…`, dirty (harness uncommitted at generation) |
  | `architecture_doc_sha256` | recorded |
  | selector contract | fingerprint and parts |
  | candidate eligibility version | source sha256 of `candidate_eligibility.py`, `_build_candidate_pool_result` and `_candidate_sort_key` → `4e4168dc…`; budget 32 |
  | KB fingerprint | `ad164d95…` (326 QnA, all active), guard fingerprint `178d268b…` (11 guards) |
  | Calendar | route closed, fingerprint null |
  | Near-pair verification | recorded |
  | Gold↔KB content check | recorded |
  | Dataset provenance and checks | recorded |
  | Case count | 518 |
  | Retrieval diagnostics | recorded |

**Fingerprint:**

- **What it covers:** `snapshot_fingerprint` = sha256 of the model-visible
  content and classification: case id, intent, expected decision and refs,
  status, primary flag, pool status, and ordered
  `[order, ref, kind, canonical, answer]`.
- **What it leaves out:** the creation timestamp and diagnostic floats.
- **Loading:** `load_snapshot` re-verifies the file sha256 and the
  fingerprint, so a tampered file is rejected (tested).
- **This snapshot:**
  `3e558768561814dabe58cd0a8fc7ada71e6c1ad98e6e5bfa460320c21dea75f6`.

**Determinism:** the snapshot was generated twice from scratch in separate
containers. Both runs produced the same fingerprint **and** the same file
sha256, scores included. The copy is kept as `…-regenerated`.

**Reuse for 7B:** runs read the frozen file and never retrieve again. Every
result carries `snapshot_fingerprint`, and `evaluate` and `compare` refuse
results from another snapshot.

### Retrieval and eligibility diagnostics (not selector accuracy)

```text
total candidate cases (selector-evaluable status)  488
expected candidate retrieved                        486
expected candidate eligible                         486
expected candidate in selector pool                 486
RETRIEVAL_MISS                                        2   (cases 74 → qna:179, 436 → qna:325)
ELIGIBILITY_MISS                                      0
BUDGET_MISS                                           0
candidates per case  mean 10.80 · median 11 · p95 17 · max 22 · min 4
truncated cases                                       0   (budget 32 never binds)
eligibility-excluded candidates                       0
```

Miss handling:

- A **retrieval miss** means the expected ref was never retrieved. An
  **eligibility miss** means it was retrieved but excluded, and the reason is
  kept. A **budget miss** means it was eligible but truncated.
- All three are outside the selector denominator. They are reported as
  retrieval diagnostics, and the selector is never expected to pick a
  candidate it did not see.
- `retrieval_inclusive_accuracy` counts misses as wrong. It is a secondary
  diagnostic only.

## Selector contract reuse and leakage

- **Model input:** `contract.build_model_input` is the production
  `build_selector_prompt`, applied to production `SelectorCandidate` objects
  rebuilt from the snapshot.
  - **Parsing:** the production provider's `ask_with_result` →
    `parse_selector_output` path, with strict JSON, Pydantic
    `SelectorDecision` and a candidate-membership check.
  - **Re-check:** the runner repeats the pipeline's allowed-ref check.
  - **Test:** the benchmark request equals the production request for the
    same candidates, byte for byte.
- **Contract fingerprint:**
  `3f49b198d621329022be807beada6d75891449b9722ffd9d7e097c6cf403ceed` =
  sha256 over:
  - `SELECTOR_SYSTEM_PROMPT`;
  - the built system prompt;
  - `SelectorDecision.model_json_schema()`;
  - the `prompt_view()` field set;
  - a serialized probe.

  The probe candidate *carries* score, source and alias, so any future
  serializer leak changes the fingerprint. A test proves a prompt change
  changes it. Results are keyed by it, so a changed contract gets a new
  namespace automatically.
- **Leakage check:** `FrozenCandidate.to_selector_candidate()` drops all
  provenance (score, source, stage and alias set to `None`/`False`).
  - **What the test asserts:** the user payload has exactly the keys
    `resolved_intent` and `candidates`, and each candidate has exactly
    `candidate_ref`, `kind`, `canonical_text` and `answer_text`.
  - **What the payload never contains:** score, provider, stage, alias or
    order.

## Metrics

- **Primary — Exact Selector Accuracy** (= §22.4 "conditional accuracy"): the
  denominator is the selector-evaluable **primary** cases.
  - A SELECT case is correct iff the selected ref is in
    `acceptable_candidate_refs`.
  - A NONE case is correct only on a valid semantic NONE.
  - Invalid output, model error and timeout count as wrong.
- **Secondary:** SELECT accuracy, NONE precision (correct NONE / valid NONE
  predictions), NONE recall, false NONE (count and rate over expected SELECT),
  false SELECT, wrong SELECT, invalid-output, model-error and timeout rates,
  the full outcome histogram, and `retrieval_inclusive_accuracy`.
  - **Denominators:** every rate reports its own; an empty denominator is
    `null`, never 0.
- **Slices:**
  - **Emitted:** a slice is emitted only when at least one evaluable case
    carries its tag, so absent tags get no metric (tested).
  - **Dataset-derived:** `single_intent`, `multi_acceptable`,
    `kb_overlap_flagged`, `context_independent`, `first_turn`,
    `follow_up_context_not_required`, `temporal`, `routing_guarded`,
    `qna_only`, `multi_intent_derived` and `reformulated_intent_text`.
  - **`calendar`:** emitted only when a Calendar ref is expected.
  - **Harness-derived, after the snapshot:** `near_qna` and
    `general_specific`; see below.
- **Matrices:**
  - NONE matrix: expected SELECT/NONE × {correct ref, other ref, NONE,
    invalid, error, timeout};
  - near-QnA matrix per pair: correct, chose sibling, other, NONE or error;
  - general/specific matrix by expected role: correct, chose counterpart,
    other, NONE or error;
  - error ids, invalid reasons and failure categories;
  - token and latency distributions.

  Output formats: `metrics.json` + `REPORT.md` per run.

### Near-QnA and general/specific fixtures

- **Pair definitions:** the six pairs (316↔335, 328↔336, 310↔405, 333↔319,
  129↔342, 347↔72) are stored in `near_qna_pairs.json`. Each entry has the
  real id and canonical question text and **no answers**.
  - **Checked against the live KB:** 12/12 exist, are active and have an
    identical question text.
  - **Checked against Phase 4:** a test asserts the file equals the
    `NEAR_QNA_PAIRS` set that Phase 4 already verified.
- **No synthetic cases:** no case is synthesized from these pairs. A **Gold**
  case joins the `near_qna` slice only when one pair member is acceptable
  **and** the non-acceptable sibling is actually in that case's frozen
  candidate set.
- **Result:** 77 cases.

  | Pair | Cases |
  |---|---:|
  | 316-335 | 24 |
  | 328-336 | 17 |
  | 333-319 | 16 |
  | 129-342 | 9 |
  | 310-405 | 8 |
  | 347-72 | 3 |
- **General/specific** is not a dataset tag. It is a harness classification
  of two pairs: 129↔342 (the prompt's own worked example) and 310↔405, where
  the specific question adds an explicit qualifier.
  - **Cases:** 17 qualify (11 expect the general candidate, 6 the specific
    one).
  - **Evaluable cases are not relabeled:** the other pairs are `sibling` or
    `near_duplicate`.

## Fake provider validation (HARNESS SELF-TEST — not model accuracy)

All fakes run through production `ask_with_result` (prompt builder + parser) on
the real frozen snapshot, 486 selector-evaluable cases:

| Policy | Primary exact acc. (n = 482) | Expected behavior |
|---|---:|---|
| `oracle` | 1.0000 (every slice 1.0; near-QnA and G/S matrices all correct) | evaluator reproduces 100% |
| `always_none` | 0.0000 (482 false NONE) | all FALSE_NONE |
| `first_candidate` | 0.9585 (20 wrong SELECT) | retrieval rank-1 baseline; the 20 are the alias-collision headroom (see limitations) |
| `malformed` / `unknown_ref` | 0.0000, invalid rate 1.0, 0 NONE | INVALID_OUTPUT, never NONE |
| `timeout` | 0.0000, timeout rate 1.0 | TIMEOUT |
| `model_error` | 0.0000, model-error rate 1.0 | MODEL_ERROR |

- **Label:** every report from a fake run carries `"self_test": true` and
  the label `HARNESS SELF-TEST — fake provider output, NOT a model accuracy`,
  in `metrics.json` and in `REPORT.md`.
- **Resume:** re-running the oracle executed 0 cases with 0 provider calls,
  and skipped all 486 completed cases.

## Resume, isolation, retry and concurrency

- **Run identity:** selector contract fingerprint + selector config
  fingerprint + snapshot fingerprint + run mode + candidate order.
  - **Run id:** `run_id` is the first 16 hex characters of the identity
    hash.
  - **Results:** they go to `runs/<run_id>/results.jsonl`, and each line
    carries `result_key` = hash(identity + case_id).
- **Resume:**
  - **Completed keys:** skipped.
  - **A different config, snapshot or contract:** gets a new `run_id`
    and reruns every case.
  - **A result from another identity in the file:** raises
    `ForeignResultError`.
  - **A torn or corrupted line:** ignored, counted and rerun. Appends never
    glue onto a torn line.

  All four behaviors are tested.
- **Retry:** the harness reuses the production adapters, so SDK retries follow
  the config's `max_retries`. The harness records **one final result** per
  case (MODEL_ERROR and TIMEOUT stay explicit). Opt-in `--retry-errors` reruns
  only those cases; the new attempt supersedes the old one (`attempt` is
  incremented), so nothing is double-counted (tested).
- **Concurrency:** 1 by default, hard cap 4.
- **Raw response:** the stored `raw_response` is only the model's final
  structured text, truncated to 256 characters.
  - **Not requested or stored:** chain-of-thought and hidden reasoning.
  - **Also kept out:** results reference `case_id` only and never copy the
    intent text.

## Paired comparison

- **Pairing:** `compare` / `stats.paired_compare` pairs two runs on the same
  snapshot (it refuses a mismatch) over the primary cases both runs
  completed.
- **Output:** both correct, only A, only B, both wrong, the two accuracies,
  and whether both runs used the same contract.
- **Statistics:** an exact two-sided McNemar test (binomial(b + c, ½) via
  `math.comb`) plus the continuity-corrected χ² as a secondary figure.
- **Tests:**
  - (10, 2) → 158/4096;
  - symmetry;
  - (0, 0) → 1;
  - a cross-check against `scipy.stats.binomtest`;
  - a known synthetic pairing, oracle vs always-NONE → (1, 3, 0, 0),
    p = 0.25.
- **No ranking verdict** is produced.

## Token estimate and cost (pre-run, no live call)

The repo and the image have no model-compatible tokenizer (no `tiktoken`), so
the estimate is **APPROXIMATE**: characters / 3.0, plus 11 chat-framing tokens
per call. It is computed on the exact production selector request built from
the frozen snapshot.

```text
                         primary (482 cases)            all selector-evaluable (486)
input tokens / config    1,122,168  (band chars/4.0 … chars/2.5: 842,591 … 1,344,964)   1,131,789
  per case               mean 2,328 · median 2,267 · p95 3,426 · max 4,378 · min 1,118
output tokens / config   ~8,194 estimated (17 per SELECT JSON reply); upper bound 482 × 32 = 15,424
```

- **Price input:** `--input-price-per-1m` and `--output-price-per-1m` (CLI).
  The repo contains no pricing configuration (`grep price|pricing|per_1m`
  → nothing).
- **Dollar cost:** **not calculated.** No explicit price was given, so no
  dollar figure exists (`estimated_cost_per_config: null`).

## Current production selector config (read-only)

These values were read from the active registry (`config-snapshot`, no LLM
call).

| | intent_analyzer | selector |
|---|---|---|
| provider / model | openrouter / openai/gpt-4o-mini | openrouter / openai/gpt-4o-mini |
| reasoning effort | none (not transmitted by adapters) | none |
| temperature / max_tokens | 0.0 / 300 | 0.0 / 32 |
| timeout / retries / native structured output | provider default / provider default / off | same |
| registry model id / qualification | 3 / LEGACY_APPROVED | 3 / LEGACY_APPROVED |
| config version | v1 (BOOTSTRAP) | v1 (BOOTSTRAP) |
| config fingerprint | `2f1b27bc…c2b1a6` | `af9eb2d0767d37cd632799cbae39e7938585b243ceb4c7a1527b8028cd489a6e` |

A harness baseline config (`--provider openrouter --model openai/gpt-4o-mini`,
defaults) has **the same** selector fingerprint `af9eb2d0…`. A baseline run is
therefore keyed exactly to the production config.

## Live-call safety

- **Default:** `run` without flags is a dry run with the `oracle` fake,
  labeled as a self-test.
- **Live mode:** it requires `--live` **and**
  `--confirm-live-provider-calls` **and** an explicit `--provider` and
  `--model`. Otherwise it exits before any provider object is built.
- **Before a live call:** it prints the planned call count and the token
  estimate.
- **Reasoning effort:** live runs with `--reasoning-effort` are **refused**,
  because production adapters do not transmit that value yet (g34). Such a
  run would silently measure the default.
- **Network guard (`safety.no_live_calls`):** every other command runs
  inside it.
  - `getaddrinfo`, `connect` and `connect_ex` are refused except for
    allowlisted infrastructure hosts. `snapshot` and `config-snapshot` may
    reach only Postgres, Meili and Qdrant; the others may reach nothing.
  - `openai` `Completions.create` and `google.genai` `Models.generate_content`
    raise `LiveCallBlocked`.
- **Test suite:** the whole module runs inside this guard, with only the
  test Postgres allowed.
  - **Guard test:** a socket connection and a provider SDK call both raise.
  - **CLI test:** the default command runs with the real `OpenAI` and
    `genai.Client` constructors patched to fail. It completes a full dry
    run (4 fake calls, report written) and **zero** real clients are built.
  - **Result:** network safety test PASS.

## Expected future live call count (Phase 7B)

There is one selector call per case per config, and no Intent Analyzer call.

- **Primary:** 482 calls per config. For the five planned configs (current
  baseline, alternative model, reasoning low, medium and high) that is
  **2,410 calls**.
- **Including the 4 derived multi-intent cases:** 486 per config, 2,430 in
  total.
- **Input tokens:** about 5.6 M for the five primary runs (approximate).
- **Retries:** SDK-internal retries are not double-counted.

## Tests

```text
test_selector_benchmark.py (targeted):           43 passed
Full backend suite, repository-root layout:      435 passed, 0 failed  (Phase 6 end state: 392 → +43 new)
```

- **Harness:** a throwaway `postgres:15` on an isolated network, and the
  backend image with pytest added in a throwaway `auzef-pytest-7a:local`
  image. The application databases were not used for tests.
- **Coverage:**
  - schema: SELECT, NONE, multi-acceptable, exclusion, invalid refs;
  - snapshot: determinism, fingerprint, order preservation,
    retrieval/eligibility/budget miss, tamper rejection, Calendar ref,
    near-pair tags;
  - contract reuse and leakage;
  - evaluator: all outcome classes, miss exclusion, absent slices;
  - the seven fake policies;
  - resume, isolation, torn lines, foreign results, retry;
  - McNemar and paired counts;
  - tokens and cost;
  - live gate, guard, reasoning refusal and default CLI dry run;
  - Gold loader contract on a synthetic fixture (hash/count FAIL, WARN on a
    non-reference denominator).

  The synthetic fixtures are contract tests only.

**No new regression.**

## Phase 7B readiness and blockers

The live selector benchmark is **not blocked by the dataset**: the reviewed
Gold was found and validated, and its snapshot is frozen. Phase 7B still needs
the following before its first call:

1. **Explicit user approval of the live spend.** That covers provider,
   models, call count (482 per config) and, if a dollar figure is wanted,
   the price inputs.
2. **Adapter transmission of `reasoning_effort` (g34)** before any
   reasoning low/medium/high run. The harness refuses those runs until then.
3. **A decision on the alternative model(s)** to compare against the
   `af9eb2d0…` baseline.
4. **An agreed reading plan.** The primary metric has only 20 cases of
   headroom above the retrieval rank-1 baseline. Model comparisons should
   therefore lead with the `kb_overlap_flagged` (9), general/specific (17)
   and near-QnA (77) slices and the paired counts, not with the primary
   accuracy delta alone.

## Known limitations

- **No NONE targets.** Reviewed Gold has 0 expected-NONE cases, so NONE
  precision and recall are `null` on this dataset. Only false NONE on SELECT
  cases is measurable. Scoring `no_match` needs a reviewed NONE set.
- **Gold and retrieval share provenance, so the primary metric has only
  20 cases of headroom.** Measured read-only against `qna_queries`:
  - **Alias overlap:** 481 of the 482 primary in-pool intent texts are exact
    `qna_queries.query_text` aliases in the KB. For 461 of them the alias
    belongs to the expected QnA; for 20 it belongs to a different QnA.
  - **Retrieval rank 1:** it is wrong on **exactly those 20 cases**. The
    offline comparison `compare` oracle vs `first_candidate` gives
    462 / 20 / 0 / 0, so accuracy 1.0 vs 0.9585.
  - **Consequence:** on this set, Exact Selector Accuracy partly measures
    agreement with retrieval, not verification quality. Two real models
    can differ by at most 20 primary cases, so a primary-set McNemar test
    will be dominated by ties.
  - **Where the 7B signal is:** these slices, where retrieval rank 1 is
    wrong:

    | Slice | Cases | Rank-1 correct | Rank-1 wrong (headroom) |
    |---|---:|---:|---:|
    | `kb_overlap_flagged` | 9 | 0.000 | 9 |
    | `general_specific` | 17 | 0.765 | 4 (all expected-general) |
    | `near_qna` | 77 | 0.909 | 7 |
    | `multi_acceptable` | 20 | 0.950 | 1 |
  - **`kb_overlap_flagged` is the key slice.** Reviewers flagged these
    nine cases because an alias collision points retrieval at the wrong
    QnA, and rank 1 is wrong on all nine. It is the cleanest
    "verifies vs follows rank" slice.
  - **Position bias:** the `permute:<seed>` order is the prepared probe. It
    has not been run.
- **The two retrieval misses are the same alias collision.** Case 74 (gold
  179) and case 436 (gold 325) have intent texts that are exact aliases of
  309 and 338. Their targets never enter the pool. Case 74 has the same shape
  as the reviewer note on case 153 ("mesajın 315 altında exact alias olması
  yanlış yönlendiriyor"), but 74 is not in the `KB_OVERLAP_NEEDS_FIX` queue.
  This is a candidate for the KB fix queue; it does not change any metric.
- **The dataset sits outside version control.**
  - **Location:** it is not a Chatbot-repo artifact, and it is untracked in
    the sibling tree.
  - **For 7B:** that path must be supplied again (`--gold-dir` /
    `--session-dir`). The loader verifies it against the recorded manifest
    sha256 values. Those hashes detect drift but cannot recover the data.
  - **Copying:** the data was deliberately not copied into this repo, since
    it holds real user messages.
- **Weak independence.** 486 cases share only 87 distinct expected QnA ids,
  so cases are correlated and accuracy differences look more precise than
  they are.
- **Small slices.** General/specific has only 17 cases (11 + 6) and
  covers only two pairs.
- **Calendar is not measurable.** The dataset has no Calendar relevance or
  Calendar gold.
- **Token counts are approximate.** They are not tokenizer-exact.
- **The snapshot reflects the local dev KB and indexes of 2026-09-19.**
  - **Recorded:** the KB fingerprint `ad164d95…` and guard fingerprint
    `178d268b…`.
  - **Gold equality:** the Gold↔KB content was identical for all 84 checked
    ids.
  - **Comparing KB hashes:** the Gold manifest's `kb_baseline_sha256` hashes
    source JSON files, so it is not directly comparable to our DB hash.
  - **When to regenerate:** if the KB changes. The fingerprint will change,
    and old results will not be mixed in.
- **Qdrant warning.** The Qdrant client (1.19.1) warns about the server
  version (1.13.2). The warning predates this phase, and both generations
  agreed.
- **Packaging.** `backend/benchmarks/` is copied into the backend image by
  the existing `COPY . .`. The app never imports it.

Phase 7B has not been started.
