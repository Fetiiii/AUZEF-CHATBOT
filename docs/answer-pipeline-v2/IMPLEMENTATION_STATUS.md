# Answer Pipeline V2 Implementation Status

The architecture source of truth is
[`docs/ANSWER_PIPELINE_V2_ARCHITECTURE.md`](../ANSWER_PIPELINE_V2_ARCHITECTURE.md).
Open and deferred decisions in that document must not be closed implicitly by
implementation work.

```text
Phase 0 — Baseline Freeze
STATUS: PASS

Phase 1 — Observability + Model Config Foundation
STATUS: PASS

Phase 2 — Intent Analyzer
STATUS: PASS

Phase 3 — Calendar V2
STATUS: PASS

Phase 4 — Candidate Eligibility + Selector V2
STATUS: PASS

Phase 5 — Degraded Mode
STATUS: PASS

Phase 6 — Model Registry + Admin Control
STATUS: PASS

Phase 7A — Selector Benchmark Harness
STATUS: PASS

Phase 7B-Prep — Model / Reasoning Evaluation Preparation
STATUS: PASS

Phase 7B-Live Stage A — Production Selector Baseline on Challenge Set
STATUS: PASS

Phase 7B-Postmortem — Selector Failure Anatomy + Experiment Design
STATUS: PASS

Phase 7B-Prompt Prep — Selector Prompt Experiment Harness
STATUS: PASS

Phase 7B-Prompt DEV Live
STATUS: PASS

Phase 7B-Semantic Adjudication Prep
STATUS: PASS

Phase 7B-Semantic Adjudication First Review
STATUS: PASS

Phase 7B-Semantic Adjudication Full Candidate Follow-up
STATUS: PASS

Phase 7B-Semantic Adjudication Review
STATUS: PASS

Phase 7B-Semantic Gold V1
STATUS: PASS

Phase 7B-Semantic Rescore
STATUS: PASS

Phase 7B-Prompt HOLDOUT Live (variant_a_v1, Semantic Gold V1)
STATUS: FAIL (promotion gate: critical 471/472; run valid, 42/42 calls)

Phase 7B-Live Stage B — Full Reference Validation
STATUS: NOT_STARTED

Phase 7C — Metadata Necessity
STATUS: NOT_STARTED

Phase 7D — Exact Alias Experiment
STATUS: NOT_STARTED

Phase 7E — Candidate Budget Experiment
STATUS: NOT_STARTED

Phase 7F — Bot Context Necessity
STATUS: NOT_STARTED

Phase 7G — Final E2E Freeze
STATUS: NOT_STARTED
```

Phase 7 is tracked per sub-phase. There is no single Phase 7 PASS.

## Phase 0 acceptance record

- [x] Architecture document is versioned and explicitly marked source-of-truth.
- [x] Starting branch and HEAD are recorded.
- [x] Architecture and critical-source SHA256 values are recorded.
- [x] Current V1 behavior and model/config state are documented.
- [x] Relevant test baseline is recorded without reporting blocked tests as pass.
- [x] Full-suite environment/layout failures are recorded.
- [x] Migration invariants are machine-readable.
- [x] Benchmark provenance is documented without copying datasets.
- [x] Runtime-critical source digests are unchanged.
- [x] No Phase 1 implementation had started at the Phase 0 freeze.

## Phase 1 acceptance record

- [x] Capability-based `intent_analyzer` and `selector` config exists.
- [x] Phase 0 effective provider/model and generation defaults are preserved.
- [x] Reasoning, timeout, retry, and structured-output foundation fields exist.
- [x] Stable secret-free config fingerprints exist.
- [x] Success, semantic-none, invalid-output, model-error, and timeout outcomes
      are internally distinguishable.
- [x] Request-scoped, structured, PII-safe decision traces are emitted.
- [x] Candidate IDs/order, selected QnA, fallback reason, provider/config,
      latency, and available usage metadata are traceable.
- [x] Existing admin/DB LLM ON/OFF behavior is preserved.
- [x] Splitter, selector, Calendar, context, guard, fallback, widget, and search
      behavior remain Phase 0-compatible.
- [x] Targeted tests pass and the full suite has no new regression.
- [x] No Phase 2 implementation was started.

Detailed evidence: [`PHASE_1_REPORT.md`](PHASE_1_REPORT.md).

## Phase 2 acceptance record

- [x] The V1 splitter is replaced on the production path by Intent Analyzer V2.
- [x] SINGLE is the ambiguity default; MULTI is limited to two independent
      answer needs.
- [x] Strict typed `source_text`, `normalized_text`, `resolved_text`,
      `context_used`, and `calendar_relevant` output is validated.
- [x] Model/parse/three-plus failures preserve the raw current turn as SINGLE;
      regex splitting is removed.
- [x] The analyzer receives at most two previous owned user turns and no bot
      messages; resolved intent is the only downstream query.
- [x] The speculative/discarded selector call is removed; call counts are one
      analyzer plus one selector per intent.
- [x] Decision trace schema v2 records analyzer config, status, safe per-intent
      facts, latency, and available usage without raw user content.
- [x] Calendar V2, Selector V2, semantic-NONE redesign, and degraded-mode
      redesign were not started.
- [x] Targeted tests pass and the full suite has no new regression.

Detailed evidence: [`PHASE_2_REPORT.md`](PHASE_2_REPORT.md).

## Phase 3 acceptance record

- [x] `calendar_relevant=false` performs no Calendar retrieval/config DB query
      and injects zero Calendar candidates.
- [x] `calendar_relevant=true` retains QnA retrieval and adds only deterministic,
      meaningful, current-year Calendar matches.
- [x] Unconditional all-row injection is removed; default limit is two with a
      hard maximum of three.
- [x] Historical year fails closed; explicit term filters, implicit current term
      ranks, and GENERAL is supported.
- [x] Canonical event and record-owned aliases match without an LLM or random
      nearest-event fallback.
- [x] Current year/term use checked SystemConfig values with env fallback and no
      source-code calendar-state guess.
- [x] New fields remain compatible and maintainable through CRUD, CSV, and the
      admin grid; upgrade/downgrade is tested.
- [x] LLM-off adds no analyzer/LLM call and preserves Calendar → Meili → Qdrant.
- [x] Trace V3 records route, eligibility, IDs, no-match reason, rejection, and
      latency without raw user text.
- [x] Selector V2, semantic-NONE redesign, degraded mode, and Phase 4 work were
      not started.
- [x] Targeted/frontend checks pass; no new backend-suite failure exists, and
      the seven known layout cases pass with repository-root mounting.

Detailed evidence: [`PHASE_3_REPORT.md`](PHASE_3_REPORT.md).

## Phase 4 acceptance record

- [x] An explicit, objective-only eligibility layer precedes the selector
      (structure, content, routing guard, `status=1`); guard/activity errors
      fail closed; no score/rank/keyword/alias/metadata filtering.
- [x] Calendar eligibility relies on Phase 3 routing; only structural checks
      are added.
- [x] Unified typed QnA/Calendar candidates with stable `qna:<id>` /
      `calendar:<id>` refs; QnA dedupe by `qna_id`, Calendar by `calendar_id`.
- [x] Bounded, configurable budget `SELECTOR_MAX_CANDIDATES` (default 32 =
      Phase 3 retrieval ceiling; observed live maximum ≤ 20; no truncation).
- [x] Zero eligible → no selector call (`NO_ELIGIBLE_CANDIDATES`); one eligible
      → selector still runs.
- [x] Semantic-verifier prompt with an explicit unstated-qualifier
      (general/specific) rule; input is resolved intent + candidate content
      only (no context, score, rank, provider, alias).
- [x] Strict Pydantic `SELECT`/`NONE` output; numeric first-digit parser
      removed; out-of-set ref, malformed, and empty → `INVALID_OUTPUT`.
- [x] `SEMANTIC_NONE`, `INVALID_OUTPUT`, `MODEL_ERROR`, `TIMEOUT` are distinct;
      NONE is final with no Meili/Qdrant/Calendar fallback; errors use an
      explicitly traced compatibility fallback until Phase 5.
- [x] No runtime confidence, reason code, or explanation.
- [x] Curated QnA answers verbatim; Calendar answers deterministic.
- [x] Suggestions are guard/validity/activity-safe non-answers.
- [x] Decision trace schema v4 records eligibility, selection, and suggestions
      without raw text.
- [x] Phase 2/3 and LLM-OFF behavior unchanged; no semantic metadata or
      exact-alias bypass added.
- [x] Targeted tests pass; full suite has no new failure (repository-root
      layout 313/313).
- [x] Phase 5 not started.

Detailed evidence: [`PHASE_4_REPORT.md`](PHASE_4_REPORT.md).

## Phase 5 acceptance record

- [x] SEMANTIC_NONE and NO_ELIGIBLE_CANDIDATES never open degraded mode or
      touch the circuit breaker; INVALID_OUTPUT/MODEL_ERROR/TIMEOUT are never
      semantic NONE.
- [x] Admin LLM OFF is the formal `ADMIN_DEGRADED` mode (no analyzer/selector
      call, no breaker mutation); request failures never change `LLM_ENABLED`.
- [x] One deterministic degraded service (`answer_in_degraded_mode`):
      Calendar V2 → Meili ≥0.90 → Qdrant >0.75; thresholds unchanged; guard
      fallback semantics, `status=1`, fail-closed guard/activity errors.
- [x] Analyzer MODEL_ERROR/TIMEOUT/INVALID_OUTPUT/OPEN → raw current turn
      degraded, no selector; only infra errors count against the breaker.
- [x] Selector failures degrade only the affected intent on its
      `resolved_text`; g25 resolved (NONE stays final, error intent degrades,
      raw multi-intent turn never re-answered).
- [x] Capability/config-scoped, thread-safe CLOSED/OPEN/HALF_OPEN breaker with
      configurable threshold (3) and cooldown (60 s), single HALF_OPEN probe,
      success reset, admin OFF → ON reset; node-local by design.
- [x] Decision trace v5: execution mode, per-intent resolution, circuit state,
      degraded provenance; PII-safe.
- [x] Phase 2/3/4 normal paths unchanged; no model/provider/reasoning change;
      no live API call.
- [x] Targeted 33/33; full suite no new failure (repository-root 341/341).
- [x] Phase 6 not started.

Detailed evidence: [`PHASE_5_REPORT.md`](PHASE_5_REPORT.md).

## Phase 6 acceptance record

- [x] DB-backed model registry (typed provider, UNIQUE provider+model,
      allowed capabilities, enabled, structured-output and reasoning support,
      qualification state) — soft-disable only, no secrets.
- [x] Persistent, independently assignable Intent Analyzer / Selector configs
      with backend-authoritative bounds; arbitrary model strings impossible.
- [x] Idempotent bootstrap reproduces the Phase 5 effective config and
      fingerprint exactly (LEGACY_APPROVED); new models start UNTESTED.
- [x] DB primary source with env compatibility only before bootstrap;
      invalid/unavailable config → CONFIG_DEGRADED, bounded stale cache.
- [x] No-redeploy runtime reload; multi-node propagation ≤ 5 s (version poll).
- [x] Atomic versioned writes, optimistic concurrency (409), append-only audit,
      rollback as a new version with current-registry re-validation.
- [x] Reads admin / writes super_admin, enforced in middleware + handlers.
- [x] Angular admin page (eligible-only dropdown, capability-aware reasoning,
      history + rollback); production build PASS.
- [x] Trace v6 config version/source/registry provenance; breaker isolation
      and activation reset.
- [x] Phase 2–5 suites pass; full suite no new failure (repository-root
      392/392); no live provider call; production model unchanged.
- [x] Phase 7 not started.

Detailed evidence: [`PHASE_6_REPORT.md`](PHASE_6_REPORT.md).

## Phase 7A acceptance record

- [x] **No behavior change, no spend.** Production behavior is unchanged: the
      app does not import `backend/benchmarks/`, and the only tracked change
      outside the tooling is `/outputs/` in `.gitignore`. No live
      provider/LLM call was made.
- [x] **Case contract.** There is a typed, versioned selector-only case
      contract (`selector-v2-1`). It covers SELECT, NONE and multi-acceptable
      cases, and `calendar:<id>` refs.
- [x] **Denominators.** Excluded, hold and context-required cases are loaded
      but never counted. Multi-intent cases 71 and 480 become derived,
      non-primary intent cases.
- [x] **Reviewed Gold v2 validated read-only.**
      - **Integrity:** manifest sha256 values and counts match
        (503 / 17 / 486).
      - **Review decisions:** 17 are present.
      - **Expected ids:** 87 expected ids are active in the KB, with
        identical content.
- [x] **Frozen candidate snapshot.**
      - **Builder:** the production pool builder (retrieval + eligibility +
        budget) makes it once, with no selector and no analyzer.
      - **Determinism:** it is deterministic; two generations gave identical
        fingerprint and file hash.
      - **Pinning:** it is fingerprinted and versioned, and every run is
        pinned to it.
- [x] **Miss classes.** Retrieval, eligibility and budget misses are
      separate classes outside the selector denominator. This snapshot has
      2 retrieval misses and 0 of the others.
- [x] **Production contract reuse.** The production prompt builder, parser,
      `SelectorCandidate` and `SelectorDecision` are reused. The contract
      fingerprint tracks prompt, schema and serializer changes.
      Score/rank/provider/alias never reach the model (tested).
- [x] **Current config recorded.** The production selector config is v1,
      openrouter/openai/gpt-4o-mini, fingerprint `af9eb2d0…`. The harness
      baseline has the same fingerprint.
- [x] **Self-tests.** The fake provider modes are oracle, always_none,
      first_candidate, malformed, unknown_ref, empty, timeout and
      model_error. The oracle scores 100% and is labeled HARNESS SELF-TEST,
      not model accuracy.
- [x] **Resume and isolation.**
      - **Resume:** completed cases are skipped. A different config,
        snapshot or contract gets a new namespace.
      - **Bad lines:** foreign results are rejected, and torn lines are
        ignored and rerun.
      - **Retries:** they supersede earlier attempts and are not
        double-counted.
- [x] **Metrics.**
      - **Primary:** Exact Selector Accuracy.
      - **Secondary:** SELECT accuracy, NONE precision/recall, false NONE,
        false SELECT, and invalid/error/timeout rates.
      - **Slices:** general/specific and near-QnA slices, with their
        matrices.
      - **Paired comparison:** it uses the exact McNemar test.
- [x] **Estimate and live gate.**
      - **Token estimate:** APPROXIMATE; about 1.12 M input tokens per
        config over 482 primary cases.
      - **Dollar cost:** only from explicit price input; none was computed.
      - **Live gate:** `--live` + `--confirm-live-provider-calls` +
        provider/model are required.
      - **Reasoning runs:** refused until the adapters transmit reasoning
        effort.
      - **Network guard:** default commands and the test module run inside
        a socket and provider-SDK guard.
- [x] **Tests.** Targeted 43/43. The full suite has no new failure
      (repository-root layout 435/435).
- [x] Phase 7B not started.

Detailed evidence: [`PHASE_7A_REPORT.md`](PHASE_7A_REPORT.md).

## Phase 7B-Prep acceptance record

- [x] **Nothing frozen or production-side moved.**
      - **Phase 7A snapshot:** unchanged (`3e558768…`, file sha256
        re-verified).
      - **Selector:** the prompt/contract (`3f49b198…`) and eligibility are
        unchanged.
      - **Production model/config:** registry v1, `af9eb2d0…`, unchanged.
- [x] **First-candidate baseline reproduced exactly:** 462/482 = 0.958506,
      with the same 20 wrong case ids as Phase 7A.
- [x] **Selector-value metrics:** rescue, corruption, preserve, unresolved
      and net corrections, reported separately from accuracy.
- [x] **Challenge set `challenge-v1`.**
      - **Selection:** model-independent.
      - **Groups:** the union of first-candidate-wrong, near_qna,
        general_specific, kb_overlap_flagged and multi_acceptable.
      - **Control:** a deterministic hash-ordered easy control of 30.
      - **Size:** 137 unique cases.
      - **Fingerprint:** `bed2dad1…`.
      - **Immutability:** the artifact is immutable.
- [x] **Reporting layers.** `FULL_REFERENCE_EXACT` and `CHALLENGE_EXACT` are
      named and reported separately. Reports also include position-1 vs
      non-position-1, rank1_wrong rescue and right-first corruption,
      general vs specific, near-QnA per pair, and KB overlap with its
      selection-bias caveat.
- [x] **Reasoning transport.**
      - **Providers:** only openai and openrouter carry low/medium/high.
      - **Null:** a null level sends nothing, so the production payload is
        unchanged (tested).
      - **Unsupported:** the level is rejected before any request, and the
        registry refuses to store it. It is never silently ignored.
      - **Fingerprint:** the level changes the config fingerprint.
- [x] **Registry discovery.** 3 selector models were found. Only
      openrouter/openai/gpt-4o-mini is runnable (the other providers have no
      key). No model is reasoning-capable. `openai/gpt-5.6-luna` is
      NOT_REGISTERED, and no model id was invented.
- [x] **Live plan (`live-plan.json`, `a4dd2c8a…`).**
      - **Stage A:** 1 × 137 calls.
      - **Stage B policy:** baseline + ≤2 configs; at most 1,446 calls, or
        1,035 incremental.
      - **Challenge token estimate:** ≈310 k input per config
        (APPROXIMATE).
      - **Price:** PRICE_REQUIRED, with no dollar figure.
- [x] **Live run approval.** A live run requires the plan file plus
      `--approve-plan-fingerprint`. Any mismatch in plan, snapshot,
      challenge, contract or config is refused.
- [x] **Isolation.** Benchmark runs do not write registry config, do not
      touch the production circuit breaker and do not create DecisionTrace
      entries (tested).
- [x] **Self-tests on the challenge set** (HARNESS SELF-TEST):
      first_candidate 117/137 (rescue 0, corruption 0); oracle 137/137
      (rescue 20); always_none 0/137 (corruption 117).
- [x] **No automatic winner.**
- [x] **Tests.** Targeted 20/20; full suite 455/455 (repository-root
      layout). Zero real provider calls.
- [x] Phase 7B-Live is BLOCKED_PENDING_APPROVAL, and Phase 7C has not
      started.

Detailed evidence: [`PHASE_7B_PREP_REPORT.md`](PHASE_7B_PREP_REPORT.md).

## Phase 7B-Live Stage A acceptance record

- [x] **Scope as approved.** The user explicitly approved exactly 137
      OpenRouter calls, and 137 unique calls were made.
      - **Provider:** OpenRouter only; no direct OpenAI or Gemini call, and
        no fallback.
      - **Config:** `openai/gpt-4o-mini`, no reasoning, temperature 0,
        max_tokens 32. The config fingerprint `af9eb2d0…` equals production.
- [x] **Frozen inputs verified and unchanged:**
      - plan `a4dd2c8a…`;
      - snapshot `3e558768…`;
      - challenge `bed2dad1…`;
      - contract `3f49b198…`.
- [x] **Production untouched.** Registry config, version and audit rows
      were identical before and after the run. The production breaker and
      telemetry were unaffected: the benchmark ran in a separate process, and
      the backend logged only health probes during the run window.
- [x] **Resume and dedup.** A second pass executed 0 cases with 0 calls.
      The result file holds 137 unique cases with no supersede.
- [x] **Challenge results** (CHALLENGE_EXACT; not a global accuracy):
      - Exact: 51/137 vs first-candidate 117/137.
      - Value: rescue 8/20, corruption 74/117, preserve 43, unresolved 12,
        net −66.
      - Paired: 43/74/8/12, McNemar p = 1.6e−14.
      - NONE: 41 false NONE.
      - Errors: 0 invalid, 0 model error, 0 timeout.
- [x] **Slices reported:** general/specific, near-QnA per pair,
      KB-overlap with its caveat, multi-acceptable, rank1 wrong and correct.
- [x] **Usage from provider metadata** (137/137 coverage): input 256,151
      and output 1,453 tokens; latency median 1.25 s, p95 2.12 s. No dollar
      figure without a price input.
- [x] **Artifacts.** Case-level, error-review and review-queue artifacts
      exist under git-ignored `outputs/`.
- [x] **Stage B not run.** Phase 7C not started.
- [x] **Tests.** Benchmark tests 66/66; full suite 458/458.

Detailed evidence: [`PHASE_7B_STAGE_A_REPORT.md`](PHASE_7B_STAGE_A_REPORT.md).

## Phase 7B-Postmortem acceptance record

- [x] **Inputs verified and unchanged.** The Stage A results and the
      snapshot/challenge artifacts were re-verified on load. None was
      modified, and no live provider call was made.
- [x] **Production unchanged.** The selector prompt/contract (`3f49b198…`)
      and the production model/config are the same.
- [x] **Every informative case accounted for** by a deterministic,
      evidence-based taxonomy (`selector-failure-taxonomy-v1`): 74
      corruptions, 12 unresolved, 8 rescues and 41 false NONE. Undecidable
      cases stay NEEDS_HUMAN_REVIEW (37).
- [x] **General-expected cases (11) analyzed one by one.** The
      unstated-qualifier rule was violated in 2 of them. In 5, the user
      stated the qualifier the model honored.
- [x] **Prompt-vs-Gold mismatch measured.** Clear selector errors 2,
      Gold/alias questionable 18, contract mismatch 29, undetermined 37.
- [x] **Review queue kept separate.** It has recommendations only; Gold was
      not edited.
- [x] **Alias structure of the first-candidate baseline.** The first
      candidate is the intent's exact-alias owner in 481/482 full-set cases.
- [x] **Evidence counts.** Metadata candidates 7/86 (8.1%). An exact-alias
      bypass would get 117 right and 20 wrong on the challenge set.
- [x] **Deterministic stratified DEV/HOLDOUT split.** 95 DEV and 42
      HOLDOUT, fingerprint `0fcb2441…`. Stage A metrics are reported per
      split.
- [x] **Two prompt variants, benchmark-agnostic** (enforced by a test).
      Proposed matrix: 232 new calls if all steps run, OpenRouter only,
      not approved.
- [x] **Stage B:** DO_NOT_RUN_FULL_CURRENT_CONFIG. No model was added, no
      reasoning experiment ran, and Stage B did not run.
- [x] **Tests.** New 6/6, benchmark modules 72/72, full suite 464/464.
      Phase 7C not started.

Detailed evidence: [`PHASE_7B_POSTMORTEM_REPORT.md`](PHASE_7B_POSTMORTEM_REPORT.md).

## Phase 7B-Prompt Prep acceptance record

- [x] **Production unchanged.** Production prompt `2d59cfb6…` and selector
      contract `3f49b198…` are the same, with no `backend/services` diff.
- [x] **Frozen inputs unchanged.** Snapshot, challenge and split were
      re-verified on load.
- [x] **Benchmark-only prompt override.** Only the system prompt varies;
      the production serializer, schema, parser, adapter and model config
      are reused. The production prompt is read at runtime and has no copy.
- [x] **Variants versioned.** `variant_a_v1` and `variant_b_v1` are
      versioned `.md` artifacts with committed fingerprints and a
      whitespace policy. They are postmortem-derived, and B is a pure
      NONE-threshold ablation. No benchmark ids or text appear in them
      (tested).
- [x] **Fingerprints separated.** Prompt fingerprint and serializer
      contract fingerprint (`d50fbee4…`) are distinct.
- [x] **Split frozen and committed.** The DEV/HOLDOUT split is committed as
      ids only: 95/42, fingerprint `0fcb2441…`. The HOLDOUT limitation
      (tuning-separation, not blind) is documented.
- [x] **Production DEV baseline reproduced from Stage A:** 34/95, rescue
      6/13, corruption 54/82, net −48, false NONE 29. No new calls. The
      first-candidate DEV diagnostic is 82/95.
- [x] **DEV evaluation.** DEV report with taxonomy slices and critical
      cases 471/472 (both in HOLDOUT). Selection gate defined; no automatic
      winner.
- [x] **HOLDOUT gate enforced:** it requires a selected DEV winner, a
      passing gate report and, for live runs, its own approved plan.
- [x] **DEV live plan** `c8847848…`: 95 A + 95 B = 190 calls; 0 production
      and 0 HOLDOUT calls.
      - Tokens: approx 436,602 input (calibrated ≈360,808), about 3,230
        output.
      - Cost: PRICE_REQUIRED.
      - Provider: OpenRouter only.
- [x] **Isolation.** Result namespaces are separated by prompt and split,
      with resume. Production DB, breaker and DecisionTrace are untouched
      (tested).
- [x] **Tests.** New 15/15; full suite 479/479. Zero live calls.
      Phase 7C not started.

Detailed evidence: [`PHASE_7B_PROMPT_PREP_REPORT.md`](PHASE_7B_PROMPT_PREP_REPORT.md).

## Phase 7B-Prompt DEV Live acceptance record

- [x] **Scope as approved.** The user explicitly approved exactly 190
      OpenRouter calls, and 190 were made: variant_a_v1 95 and
      variant_b_v1 95.
      - **Provider:** OpenRouter only; no direct OpenAI or Gemini call.
      - **Config:** `openai/gpt-4o-mini`, no reasoning, temperature 0,
        max_tokens 32.
      - **Not run:** production prompt, HOLDOUT, Stage B, alternative
        model and reasoning (0 calls each).
- [x] **Verified before the run:** plan `c8847848…`, snapshot, challenge,
      split, contract, serializer and prompt fingerprints.
- [x] **Frozen after the run.** Prompts were not modified after the
      results.
- [x] **Isolation.** Production config/version/audit rows, the breaker and
      telemetry were unchanged.
- [x] **Result files.** A and B namespaces are separate, with 95 unique
      results each. Resume executed 0 cases with 0 calls.
- [x] **DEV results:**

      | | Exact | Rescue | Corruption | Net | False NONE |
      |---|---:|---:|---:|---:|---:|
      | Production | 34 | 6/13 | 54/82 | −48 | 29 |
      | A | 54 | 6/13 | 34/82 | −28 | 5 |
      | B | 45 | 6/13 | 43/82 | −37 | 18 |

      Paired McNemar p: prod-vs-A 1.9e−6, prod-vs-B 0.0034, A-vs-B 0.0117.
- [x] **Slices and usage reported:** general/specific (no
      production-correct case broken by A or B), near-QnA, KB-overlap,
      multi-acceptable, easy control, taxonomy slices, and actual tokens and
      latency. No dollar figure without prices.
- [x] **Gate outcome.** Both variants FAIL_GATE on net corrections ≥ 0. No
      automatic winner; HOLDOUT is BLOCKED_NO_DEV_VARIANT_PASSED_GATE.
- [x] **Artifacts and tests.** Case-level diffs and a prompt review queue
      exist. Tests 17/17; full suite 481/481. Phase 7C not started.

Detailed evidence: [`PHASE_7B_PROMPT_DEV_REPORT.md`](PHASE_7B_PROMPT_DEV_REPORT.md).

## Phase 7B-Semantic Adjudication Prep acceptance record

- [x] **Nothing existing changed.** Production runtime, parent reviewed
      Gold, Stage A outputs, Variant A/B outputs and prompts, and the prompt
      gate are the same. Zero live calls.
- [x] **Deterministic review scope:**

      | Source | Cases |
      |---|---:|
      | GOLD_ALIAS_QUESTIONABLE | 18 |
      | CONTRACT_MISMATCH | 29 |
      | NEEDS_HUMAN_REVIEW | 37 |
      | KB_OVERLAP (subset) | 5 |
      | Existing queue (+74, 436) | 7 |
      | **Unique scope** | **86** |
      | Hash-selected blind control | 20 |
      | **Total** | **106** |
- [x] **Primary view is model-output blind.** It contains no production/A/B
      decisions, first-candidate result, rescue/corruption, retrieval
      rank/score, refs, current Gold or alias provenance (build-time check +
      tests).
      - **Shown per candidate:** anonymous labels in rank-neutral hash order,
        with canonical question and curated answer.
      - **Secondary audit view:** separate, to be opened only after the
        review.
- [x] **Candidate subset.** Deterministic and model-independent (Gold +
      near-QnA + top-5 retrieved + top-3 lexical), with a completeness flag
      and NEED_FULL_CANDIDATES.
- [x] **Decision schema.** Blind decisions (SELECT_ACCEPTABLE, EXPECT_NONE,
      EXCLUDE_AMBIGUOUS, CONTENT_REVIEW_REQUIRED,
      RETRIEVAL_OR_KB_MAPPING_REVIEW, NEED_FULL_CANDIDATES) map to KEEP /
      CHANGE / MULTI after the review is locked. The general/specific and
      practical-answer rules are documented.
- [x] **Packet locked.** Fingerprint `2c229e4f…`, immutable.
- [x] **Tooling prepared, not executed against the real packet:**
      - apply to a child Gold with provenance (parent immutable);
      - re-score of saved outputs without calls;
      - expected-NONE scoring.
- [x] **Tests.** New 22/22; full suite 503/503. Human review not
      auto-completed. HOLDOUT and Stage B not run; Phase 7C not started.

Detailed evidence: [`PHASE_7B_SEMANTIC_ADJUDICATION_PREP_REPORT.md`](PHASE_7B_SEMANTIC_ADJUDICATION_PREP_REPORT.md).

## Phase 7B-Semantic Adjudication Full-Candidate Follow-up record

- [x] **First pass.** The user's first-pass file was validated against the
      locked template: 87 decided and 19 NEED_FULL_CANDIDATES (19, 74, 112,
      158, 160, 184, 215, 221, 230, 355, 371, 375, 383, 384, 412, 415, 436,
      461, 503).
- [x] **Second blind packet.** It lists every eligible candidate once (8–16
      per case, `candidate_view_complete=true`).
      - **Labels:** the same labels and hash-neutral order as the first pass.
      - **Hidden:** refs, rank, score, source, Gold, alias, model outputs,
        taxonomy and the prior decision.
      - **Check:** field-level contamination check passes.
- [x] **Fingerprints.** Follow-up `078bec9f…`. The parent packet
      `2c229e4f…` is unchanged. Snapshot, challenge and reviewed Gold sha256
      were re-verified.
- [x] **Isolation.** The audit view was not read, the Gold was not
      modified, and no apply, re-score or final lock was run. The 87
      first-pass decisions are preserved. Zero live calls.
- [x] **Tests.** Adjudication module 27/27; full suite 508/508.

Detailed evidence: [`PHASE_7B_SEMANTIC_ADJUDICATION_FOLLOWUP_REPORT.md`](PHASE_7B_SEMANTIC_ADJUDICATION_FOLLOWUP_REPORT.md).

## Phase 7B-Semantic Adjudication + Semantic Gold V1 + Rescore record

- [x] **Human decisions.** The input file was sha256-verified over raw
      bytes (`b88ef9a8…`, 106 rows: 87 first pass + 19 follow-up).
- [x] **Review lock.** `0fbc3bd4…` was written before any audit access.
      - **Validation:** no NEED_FULL_CANDIDATES, no blank rows, labels valid,
        87 first-pass decisions unchanged, follow-up ids exact.
      - **Label mapping:** recomputed from the blind labelling and
        cross-checked against the audit view.
- [x] **Outcomes (106):**

      | Outcome | Cases |
      |---|---:|
      | KEEP_CURRENT | 46 |
      | CHANGE_GOLD | 28 |
      | MULTI_ACCEPTABLE | 8 |
      | EXPECT_NONE | 1 |
      | EXCLUDE_AMBIGUOUS | 10 |
      | CONTENT_REVIEW_REQUIRED | 2 |
      | RETRIEVAL_OR_KB_MAPPING_REVIEW | 11 |

      - 27 old Golds were rejected, 25 of them alias-derived.
      - Blind control: 19 KEEP and 1 EXCLUDE out of 20.
- [x] **Semantic Gold V1** (`36ab1d6f…`) is a child artifact with
      provenance and queues. The parent Gold is unchanged. Evaluable primary
      cases went from 482 to 461, with 1 expected NONE.
- [x] **Re-score** used saved outputs only; zero live calls.
      - **Semantic DEV (77):**

        | | Exact | Net |
        |---|---:|---:|
        | Production | 51 | +5 |
        | A | 64 | +18 |
        | B | 56 | +10 |
        | First candidate | 46 | — |
      - **Stage A (137 → 115 evaluable):** production 75/115.
- [x] **Gate unchanged.** Both variants pass it, so there is no automatic
      winner and HOLDOUT is WAITING_FOR_HUMAN_VARIANT_SELECTION.
- [x] **Nothing else changed.** Production prompt, config, KB and alias
      table are the same, and the prompts are the same. Tests: new 9/9, full
      suite 517/517. Stage B and Phase 7C not started.

Detailed evidence: [`PHASE_7B_SEMANTIC_ADJUDICATION_RESULT.md`](PHASE_7B_SEMANTIC_ADJUDICATION_RESULT.md),
[`PHASE_7B_SEMANTIC_RESCORE_REPORT.md`](PHASE_7B_SEMANTIC_RESCORE_REPORT.md).

## Phase 7B-Variant A Semantic HOLDOUT record

- [x] **Human selection:** variant_a_v1 for HOLDOUT; B not run.
- [x] **Calls:** exactly 42 OpenRouter calls (`openai/gpt-4o-mini`, config
      `af9eb2d0…`, prompt `1aed5688…`). Production, B and Stage B made 0.
- [x] **Frozen before the call** (baseline `0d318f16…`):
      - production and first-candidate semantic HOLDOUT baselines, both 24/38;
      - the 38 evaluable ids (4 excluded, 0 expected NONE);
      - the promotion gate.
      It was re-verified after scoring.
- [x] **Guard:** HOLDOUT plan `7b9b21bc…` with OpenRouter, variant_a_v1,
      HOLDOUT, 42 calls, and the baseline and Gold fingerprints bound.
      `BudgetedBackend` refused 0 calls.
- [x] **Result:** Variant A 30/38 vs production 24/38.
      - Paired: 24 both correct / 0 only production / 6 only A / 8 both
        wrong; net +6, p = 0.031.
      - False NONE: 2 vs 10.
      - General/specific regressions: 0.
      - Errors: 0.
- [ ] **Critical 471/472:** both wrong. Variant A selected "Merkezi yatay
      geçiş…" although the user did not state "merkezi".
      → **VARIANT_A_HOLDOUT = FAIL**. The prompt was not changed and there
      was no re-run on HOLDOUT.
- [x] **Isolation:** production prompt, config, registry, KB and alias table
      unchanged. Tests: new 6/6, full suite 523/523.

Detailed evidence: [`PHASE_7B_VARIANT_A_HOLDOUT_REPORT.md`](PHASE_7B_VARIANT_A_HOLDOUT_REPORT.md).
