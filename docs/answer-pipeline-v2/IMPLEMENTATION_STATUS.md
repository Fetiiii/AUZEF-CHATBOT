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
STATUS: NOT_STARTED

Phase 5 — Degraded Mode
STATUS: NOT_STARTED

Phase 6 — Model Registry + Admin Control
STATUS: NOT_STARTED

Phase 7 — Necessity Experiments
STATUS: NOT_STARTED
```

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
