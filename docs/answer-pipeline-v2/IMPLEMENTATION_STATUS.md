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

Phase 7B — Model / Reasoning Evaluation
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
