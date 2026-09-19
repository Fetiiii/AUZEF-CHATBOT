# Answer Pipeline V2 — Phase 4 Report

## Status and scope

```text
Phase 4 — Candidate Eligibility + Selector V2
STATUS: PASS
```

- Branch: `production-readiness`
- Starting HEAD: `f597ee2a48d2ff1f9f3570eb8cca66ea379bad8c`
- Source of truth:
  [`ANSWER_PIPELINE_V2_ARCHITECTURE.md`](../ANSWER_PIPELINE_V2_ARCHITECTURE.md)
  §9 (eligibility), §10 (Selector V2), §11 (NONE/error separation).
- Not started or changed: degraded-mode redesign (Phase 5), model registry or
  admin model switching (Phase 6), semantic metadata, exact-alias bypass, bot
  context, and reasoning-effort changes (Phase 7). The LLM-OFF path, Intent
  Analyzer, Calendar V2 routing, QnA retrieval (Qdrant 24 + Meili 5) and the
  thresholds are also unchanged.

## Old selector (verified in code at the starting HEAD)

- The selector prompt asked the model to pick "the most suitable candidate" and
  to reply with only a number (`1..N`, `0` = none). Candidates were plain dicts
  (`question`, `answer`, optional `qna_id`), and Calendar rows were synthesized
  as `"{period} {event} ne zaman?"`.
- Parsing took the **first digit sequence** from the reply (`re.search(r"\d+")`).
  `0` meant semantic NONE, and an out-of-range number meant invalid output.
- Semantic NONE and invalid output both went to the Meili ≥0.90 → Qdrant >0.75
  threshold fallback, with the Calendar gate closed.
- An intent with zero candidates was never counted as reaching the LLM. When
  every intent was like that, the pipeline raised `LLMPipelineError(MODEL_ERROR)`,
  which ran the full fallback **with** the Calendar gate open.
- Routing-guard `decision()` errors were not caught per candidate. The whole
  intent was silently skipped and counted as a model error.
- Suggestions (`MEILI_PROVIDER.get_suggestions`) returned bare Meili titles
  without routing-guard or activity checks. Titles of `semantic_selector_only`
  and expired guarded QnA could therefore leak.

## Candidate eligibility

New module:
[`candidate_eligibility.py`](../../backend/services/candidate_eligibility.py).
It answers only one question: *may the selector evaluate this candidate at
all?*

QnA hard rules, applied in order:

| Check | Exclusion reason |
|---|---|
| Integer `qna_id` present (structural identity) | `missing_qna_id` |
| Canonical question present | `missing_question` |
| Curated answer present | `missing_answer` |
| Routing guard allows the selector (existing contract: no guard → allowed; `semantic_selector_only` inside validity → allowed) | `not_yet_valid`, `expired`, `unsupported_selector_mode` |
| Guard evaluation raised | `guard_evaluation_error` (fail closed, per candidate) |
| Exists with `status=1` (one bounded `SELECT id … WHERE id IN (…) AND status=1` per intent) | `inactive_or_missing` |
| Activity lookup failed or unavailable | `activity_lookup_failed` (fail closed for all QnA) |

The existing guard-store load failure still makes the whole request fail
closed. `RoutingGuardPolicy` is reused unchanged. The activity check is new at
runtime. Inactive QnA were already removed from the indexes at sync time, so
this check guards against a stale index. It is not a behavior change for a
healthy index.

Calendar rules: a candidate exists only when Phase 3 routing admitted it
(`calendar_relevant=true`, current or historical year policy, term policy,
meaningful event match). Eligibility adds only structural checks: an integer
`id` (`calendar_missing_id`), and non-empty event plus both dates
(`calendar_incomplete`). None of Calendar V2's safety decisions are
re-evaluated, and none are delegated to the LLM.

Eligibility deliberately does **not** filter on:

- retrieval score or thresholds;
- Qdrant or Meili rank;
- general-vs-specific or keyword rules;
- exact alias;
- semantic metadata.

A test shows that a low-score specific candidate (`qna:342`, score 0.10) and a
high-score general candidate (`qna:129`, score 0.95) both reach the selector.

## Unified typed candidate representation

`SelectorCandidate` is a frozen dataclass:

```text
candidate_ref   "qna:<qna_id>" | "calendar:<calendar_id>"  (stable, unique, internal)
kind            QNA | CALENDAR
canonical_text  QnA canonical question | "<period> <event>"
answer_text     curated QnA answer | deterministic Calendar answer
qna_id / calendar_id                       provenance
source, retrieval_stage, score, alias_match   trace-only, never in the prompt
```

`prompt_view()` exposes only `candidate_ref`, `kind`, `canonical_text` and
`answer_text`. `trace_view()` exposes provenance without any text.

The Calendar answer is `format_calendar_answer(period, event, start_date,
end_date)`. This is the existing AUZEF formatter, and it keeps both the start
and end date. No generative Calendar composer was added.

## Merge, dedupe, ordering, budget

- **Merge and order:** Calendar candidates come first, in the Phase 3 order
  (period / event / dates / id). QnA follow in the existing deterministic
  retrieval order (stage, provider, 5-decimal score bucket, qna_id). This order
  exists only to be deterministic. It is not semantic truth, and it is not sent
  to the model as a rank or score.
- **QnA dedupe:** by `qna_id`. The first occurrence keeps its position. If that
  occurrence has no usable text, a later point of the same record supplies it.
- **Calendar dedupe:** by `calendar_id`, and also by identical
  period/event/dates, keeping the lowest id. The content rule preserves Phase 3
  behavior.
- **No cross-kind duplicates:** `qna:17` and `calendar:17` are separate
  namespaces. A test covers this.
- **Budget:** `SELECTOR_MAX_CANDIDATES` (env; default **32**; a positive
  integer, otherwise startup fails). Truncation runs after dedupe and
  eligibility, keeps the deterministic order, and records
  `candidate_truncated` and `truncated_candidate_refs` in the trace.
- **Why the default is 32:** it equals the Phase 3 retrieval ceiling of
  Qdrant 24 + Meili 5 + Calendar hard maximum 3. Phase 4 therefore cannot
  lower recall.
- **Observed on live data:** read-only retrieval against the local
  Qdrant/Meili indexes, no LLM, 20 representative queries:

  ```text
  unique QnA candidates per query: min 5, median 9, mean 9.7, max 17
  + Calendar ≤ 3  →  observed maximum ≤ 20 < 32
  truncation: none
  ```

  Tighter K is a Phase 7 experiment.
- **Answer length:** active answers are at most 1644 characters long (p95 about
  827). No answer truncation was introduced, because the model must see the
  curated answer it would return.

## Zero and single candidate

- **Zero eligible candidates:** the selector is not called. The intent outcome
  is `NO_ELIGIBLE_CANDIDATES`, which is separate from `SEMANTIC_NONE`. The
  trace entry has `selector_called=false`. The final result is no answer, with
  **no** threshold or Calendar fallback. This replaces the old accidental
  `MODEL_ERROR` → full-fallback path.
- **One eligible candidate:** the selector is still called and may answer
  `NONE`. There is no auto-bypass and no exact-alias bypass.

## Selector V2 contract

The prompt and parser are in [`selector.py`](../../backend/services/selector.py).
The system prompt defines the role as *verification, not similarity*. A
candidate may be selected only if all of these hold:

1. The topic is the same.
2. The answer covers the user's real need.
3. No unstated condition has to be assumed.
4. It does not contradict any explicit qualifier.
5. Shared words alone are not enough.
6. The answer addresses the center of the intent.

The general/specific rule is stated explicitly: *an unstated qualifier is never
assumed to justify a more specific candidate*. It gives a few qualifier types
(application or transfer type, program or student group, lisans/önlisans,
exam type, term) and the `Yatay geçiş` / `Merkezi yatay geçiş` pair as an
example. It says this is semantic judgment, not keyword matching.

Calendar candidates satisfy an intent only when the user asks for that event's
date or term. When several candidates are acceptable, the model still picks
exactly one; this is not MULTI. Candidate order carries no meaning, and nothing
outside the candidate set may be produced.

User message (JSON): `{"resolved_intent": …, "candidates": [prompt_view…]}`.
There is no conversation history, no score, no rank, no provider name and no
alias flag. Tests assert this.

### Structured output

`SelectorDecision` (Pydantic, `extra="forbid"`, `strict=True`):

```json
{"decision": "SELECT", "candidate_ref": "qna:342"}
{"decision": "NONE"}
```

`candidate_ref: null` is accepted with NONE, because strict schema producers
emit every declared key. SELECT without a ref, NONE with a ref, extra fields
(`confidence`, `reason`, …), other decision values, markdown fences, empty
output and non-JSON are all rejected.

The runtime never requests confidence, score, reason, reason code,
explanation, or chain of thought.

Validation is strict JSON plus Pydantic, then a candidate-set membership check.
Native provider `response_format` is **not** used, and the Phase 1
`structured_output_enabled` flag stays unused (default `false`). This avoids
changing provider request bodies in this phase, and §30 allows strict JSON plus
Pydantic.

The numeric first-digit parser was **removed** from production:
`_build_prompt`, `_parse_selection` and `_parse_selection_result` no longer
exist, and neither does the `re` import. The only test that used it
(`test_parse_selection_accepts_noisy_numbers`) was deleted with it. `ask()`
remains as a thin compatibility wrapper over `ask_with_result()` that returns
the selected answer text. It no longer parses numbers.

### Outcome separation

| Condition | Intent outcome | Semantic NONE? |
|---|---|---|
| Valid `{"decision":"NONE"}` | `SEMANTIC_NONE` | yes |
| Zero eligible candidates (no call) | `NO_ELIGIBLE_CANDIDATES` | no |
| Malformed JSON, schema violation, empty output, `"3"`, `{"decision":"MAYBE"}` | `INVALID_OUTPUT` | no |
| SELECT with an out-of-set ref (e.g. `qna:999999`) | `INVALID_OUTPUT` (`unknown_candidate_ref`) | no |
| Provider exception | `MODEL_ERROR` | no |
| Timeout | `TIMEOUT` | no |
| Unexpected retrieval or eligibility exception | `MODEL_ERROR` (`pipeline_error`) | no |

The pipeline also re-checks that any SUCCESS result's ref belongs to the
candidate set it sent, so a custom provider cannot select outside it.

## NONE and error behavior

`answer_question` after the LLM path:

| Aggregate outcome | Behavior |
|---|---|
| SELECT (any intent) | Curated answers verbatim, deterministic `\n\n` composition, exact-duplicate dedupe |
| `SEMANTIC_NONE` / `NO_ELIGIBLE_CANDIDATES` | **Final no answer.** No Meili, Qdrant or Calendar fallback |
| `INVALID_OUTPUT` | Compatibility fallback, Calendar gate closed (as in Phase 0), reason `selector_invalid_output` |
| `MODEL_ERROR` / `TIMEOUT` | Compatibility fallback, Calendar gate open (as in Phase 0), reason `selector_model_error` / `selector_timeout` |
| Pipeline exception before selection (e.g. no provider) | Compatibility fallback, reason `llm_model_error` |
| LLM OFF | Unchanged deterministic Calendar → Meili → Qdrant, reason `llm_disabled_or_unavailable` |

**Multi-intent aggregation.** Intents are independent. A NONE in one intent
never removes another intent's answer. When no intent produced an answer and
any intent produced a semantic decision (`SEMANTIC_NONE` or
`NO_ELIGIBLE_CANDIDATES`), the compatibility fallback is **suppressed**.
Otherwise the fallback would re-answer the raw turn and could override that
NONE. The compatibility path runs only when every intent ended in a selector
error. The Phase 5 degraded-mode redesign will formalize this.

**Suggestions** are non-answers ("Bunu mu demek istediniz?"). They never
override a selector decision and never return a curated answer.
`guard_safe_suggestions()` requests Meili suggestion hits *with ids*
(`get_suggestion_hits`) and offers a title only when all of these hold:

- the hit has a QnA id;
- the guard's **fallback** semantics allow it (guardless QnA pass;
  `semantic_selector_only`, expired and not-yet-valid guarded QnA are hidden);
- the QnA exists with `status=1`.

Failure to load the guard store or to check activity yields no suggestions
(fail closed). The trace records counts, offered ids and exclusion reasons,
never titles. Both `/widget-chat` and `/api/search` use this path, and the
response shape (a list of strings) is unchanged.

## Configuration change

| Setting | Before | After | Reason |
|---|---|---|---|
| Selector `max_tokens` (default) | 5 | 32 | `{"decision":"SELECT","candidate_ref":"calendar:12345"}` is about 15–20 tokens; 32 is the minimum with safe headroom |
| Temperature | 0 | 0 | unchanged |
| Reasoning effort | unset | unset | unchanged |
| Provider / model | as before | as before | unchanged |

The selector config fingerprint changes because `max_tokens` is part of it.
The new value appears in `effective_configs.selector`, in the per-selector
`config_fingerprint`, and in this report.

## Decision trace schema v4

`schema_version` 3 → **4**, because fallback-reason semantics, selector fields
and retrieval fields changed.

- **`retrieval[]`:**
  - `retrieved_candidate_count`
  - `candidate_count_before_eligibility`
  - `candidate_count_after_eligibility`
  - `excluded_candidate_count`
  - `excluded_candidate_refs`
  - `eligibility_exclusion_reasons`
  - `selector_candidate_refs`
  - `selector_candidate_kinds`
  - `candidate_budget`
  - `candidate_truncated`
  - `truncated_candidate_refs`
  - QnA and Calendar counts
  - `candidate_order` (internal ref, kind, ids, source, score and
    `alias_match` — never text)

  Removed: `eligible_after_guard_count`, `guard_rejections` (now part of
  `eligibility_exclusion_reasons`), and the always-zero context counters.
- **`selectors[]`:**
  - `selector_called`
  - `selector_status`
  - `selector_decision`
  - `selected_candidate_ref`
  - `selected_kind`
  - `selected_qna_id`
  - `selected_calendar_id`
  - `invalid_reason` (a safe code)
  - `semantic_none`, `invalid_output`, `model_error`, `timeout`
  - `candidate_count`, `candidate_refs`, `candidate_kinds`
  - `config_fingerprint`
  - `requested_model`, `actual_model`
  - `latency_ms`, `usage`, `retry_count`

  `raw_selector_value` was removed, because there is no numeric output
  anymore.
- **`selection`:** `aggregate_outcome`, `intent_outcomes`,
  `error_fallback_allowed`.
- **`fallback`:** as before, with the new explicit `selector_*` reasons.
- **`suggestions`:** evaluated flag, retrieved and offered counts, offered QnA
  ids, exclusion reasons.

No raw user text, resolved text, answer text, suggestion title or provider
output is written to the trace.

## Tests

The new file [`test_selector_v2.py`](../../backend/tests/test_selector_v2.py)
has 54 tests. It uses the real Selector V2 prompt and parser. Only the network
call is scripted, and it runs against a real test Postgres. It covers:

- **Eligibility:** inactive, guard outside validity, `semantic_selector_only`,
  guard-evaluation error, activity-lookup failure, structural exclusions, valid
  Calendar, closed Calendar route, and general and specific candidates both
  surviving.
- **Candidate counts:** zero eligible (0 selector calls, no fallback, trace);
  one eligible (1 call; NONE allowed).
- **Structured SELECT:** QnA 342 returned verbatim; Calendar 17 returns the
  deterministic stored answer, with exactly one LLM call.
- **Semantic NONE:** final, proven by call counts. Meili is called only at its
  retrieval limit 5, Qdrant only at its retrieval limit 24. No threshold or
  Calendar fallback runs even with 0.99 scores, and only guard-safe
  suggestions are shown.
- **Invalid output:** 13 variants, all `INVALID_OUTPUT` and never
  `SEMANTIC_NONE`. The error-compatibility path runs with explicit reasons.
- **Model error and timeout:** separated, each with its explicit fallback
  reason.
- **Prompt contract:** the verifier role and the unstated-qualifier rule are
  present. The prompt contains no score, rank, provider or alias. The selector
  sees only the resolved intent, not conversation context.
- **Near-QnA fixtures:** the six reviewed pairs use their real ids and question
  texts, which I verified read-only in the local DB (316↔335, 328↔336, 310↔405,
  333↔319, 129↔342, 347↔72). The answers in the fixtures are synthetic. The
  tests check that both candidates survive eligibility, that refs are stable,
  and that the order does not depend on provider order.
- **Multi-intent:** independence, and suppression of the error fallback when a
  NONE is present.
- **Budget:** parse, default ceiling, deterministic truncation plus trace,
  stable ordering.
- **Other:** an exact-alias hit still goes through the selector; suggestions
  apply the guard, validity and activity rules and fail closed; LLM OFF calls
  neither the analyzer nor the selector.

These existing tests were **intentionally** updated to the Phase 4 contract:

- `test_answer_pipeline_phase1.py`: semantic NONE previously asserted that
  fallback **is** entered. It now asserts **no** fallback (plus the
  no-eligible case). The error reasons are now the explicit `selector_*` names.
- `test_llm_outcomes.py`: the numeric outputs (`"[2]"`, `"0"`, `"7"`) became
  JSON decisions; `max_tokens` 5 → 32. The `response_format`-absent assertion
  is kept.
- `test_llm_config.py`, `test_answer_pipeline_phase2.py`,
  `test_decision_trace.py`: `max_tokens` 32 and trace schema 4. The selector
  test doubles now use typed candidates and refs.
- `test_calendar_v2.py`: the test doubles now use typed candidates. The
  Calendar SELECT assertion is stronger: it expects the deterministic stored
  answer instead of a fake string.
- `test_pure_functions.py`: the numeric-parser test was removed. The dedupe
  and Calendar-order assertions now check typed refs.
- `test_routing_guards.py`: the pool builder now receives an explicit activity
  lookup.
- `test_widget_tokens.py`: suggestions now carry QnA ids and existing active
  rows.
- `test_maintenance.py`: it now patches the new suggestion entry point.

### Results

Harness: an isolated `postgres:15` container plus a backend-image container
with pytest. The live application databases were not used for tests.

```text
Phase 4 file (test_selector_v2.py):              54 passed
Full backend suite, backend-only /app layout:    306 passed, 7 failed (known layout cases only)
Full backend suite, repository-root layout:      313 passed, 0 failed
Baseline at f597ee2 in the same harness:         250 passed, 7 failed
```

The seven known failures are the unchanged `test_production_app_runtime.py`
layout cases: there is no `/deploy/production` tree inside a backend-only
container. They pass when the repository root is mounted. (Phase 3 reported
`249 passed, 1 skipped`; in this harness the subprocess compatibility test also
runs because `TEST_COMPAT_DATABASE_URL` is set, which gives 250 at baseline.)

Phase 2 regressions: the Intent Analyzer suite and the Phase 2 orchestration
tests still pass, with no change. SINGLE default, maximum two intents, the
previous two user turns only, no bot context, the expansion guards, no
speculative selector call, and analyzer error → raw SINGLE are all still
covered.

Phase 3 regressions: all Calendar V2 tests pass. They cover irrelevant intent →
0 Calendar, filtered relevant Calendar, historical rejection, term preference,
the limit, and no random event.

LLM OFF: unchanged. It is covered by the tests above.

## Evaluation

- **Reviewed Gold / Exact E2E:** not available in this repository (open task
  g02). Exact E2E before/after was therefore **not measured**, and no accuracy
  number is claimed.
- **Live-model selector evaluation:** **not run.** It needs API spend and a
  live provider call, which require the user's approval. The near-QnA and
  general/specific fixtures are ready for a live run.
- **What was verified deterministically:**
  - Contract correctness: SELECT and NONE parsing, the invalid-output rate on
    malformed inputs (13 of 13 → `INVALID_OUTPUT`, 0 → NONE), and
    out-of-set refs.
  - Selector call count: 0 for zero eligible, 1 per intent otherwise.
  - General/specific is proven only as a **prompt-contract** property: the
    rule and example are present, both candidates reach the selector, and there
    is no score or rank bias in the input. Whether the live model follows the
    rule is not proven here.
  - Candidate-count distribution on live retrieval: see the budget section.
- **Latency and token usage:** trace fields exist (`latency_ms`, `usage`,
  `requested_model`, `actual_model`), but no live measurement was taken.
  Expected prompt size: about 9–20 candidates × ~300 characters of answer, with
  JSON output ≤32 tokens.

## Architecture alignment

- §10.5 shows `SELECT(qna_id)`. Phase 4 uses `candidate_ref` (`qna:<id>` or
  `calendar:<id>`), because Calendar candidates are now part of the same
  heterogeneous set. `qna_id` provenance is kept in the result and the trace.
  This refines the frozen contract for mixed candidate kinds and does not
  conflict with it.
- §11.1 (NONE final), §11.2 (distinct outcomes), §9 (objective eligibility)
  and §12 (deterministic composition) are implemented as written.
- §11.3 degraded mode was not redesigned. The error-compatibility fallback
  remains until Phase 5.

## Phase 0 migration invariants

All eleven invariants in [`baseline.json`](baseline.json) still hold. That file
is a frozen Phase 0 artifact and was not edited. Two of its evidence symbols
have moved:

- `curated_answer_returned_verbatim` is now enforced by
  `selector.parse_selector_output`, which returns the selected candidate's
  `answer_text`. `_parse_selection` was removed.
- `qna_candidate_dedupe_uses_qna_id` is now enforced by
  `candidate_eligibility.build_candidate_set` (the `qna:<id>` ref).
  `_candidate_identity` was removed.

Meili and Qdrant retrieval, the guard-store fail-closed behavior, both
endpoints using `answer_question`, admin LLM ON/OFF, QnA index synchronization,
ownership tokens, and deterministic multi-answer composition are unchanged.

The widget (`chatbot-web/src/widget.js`) renders `suggestions` as string chips.
Clicking one sends it as a new question, which goes through the selector
again. The response shape is unchanged, so no frontend change was needed.
`MeiliSearchProvider.get_suggestions` remains as an unused legacy method; the
production path uses `get_suggestion_hits`.

## Known limitations

- Selector accuracy, including on general/specific and near-duplicate cases,
  has not been measured with a live model or on the reviewed Gold set.
- If one intent returns NONE and another ends in a selector error with no
  answer, the result is no answer: the error intent loses its compatibility
  fallback. Phase 5 should define per-intent degraded behavior.
- The QnA activity check adds one bounded indexed DB query per intent.
- Stricter JSON parsing means a model that wraps output in markdown fences is
  `INVALID_OUTPUT` and falls back. Phase 5 or 6 model qualification should
  measure this rate.
- Native provider structured output (`response_format`) remains unused.
- Operational prerequisites carried over from Phase 3: Calendar year, term and
  alias backfill, and current-year and current-term configuration.

Phase 5 has not been started.
