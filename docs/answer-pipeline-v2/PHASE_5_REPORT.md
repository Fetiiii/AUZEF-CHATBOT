# Answer Pipeline V2 — Phase 5 Report

## Status and scope

```text
Phase 5 — Degraded Mode
STATUS: PASS
```

- Branch: `production-readiness`
- Starting HEAD: `ccce39d`
- Source of truth:
  [`ANSWER_PIPELINE_V2_ARCHITECTURE.md`](../ANSWER_PIPELINE_V2_ARCHITECTURE.md),
  especially §3.7/§3.8 and §11.
- These were **not** changed: the model registry and admin model UI
  (Phase 6), the production provider/model, reasoning effort, semantic
  metadata, exact alias, bot context, candidate K, the selector prompt, and
  the Meili/Qdrant thresholds. No live provider calls were made.

## Previous behavior (verified at `ccce39d`)

- **Admin LLM OFF:** `is_llm_enabled(db)` is false when the DB flag
  `LLM_ENABLED` is not `true` **or** when no provider/key is available. In
  that case `_fallback_answer(query, use_calendar=True)` ran on the raw turn:
  the `is_date_query` gate opens Calendar V2 (limit 1), then Meili (limit 3,
  score ≥ 0.90), then Qdrant (limit 5, score > 0.75). This path had no
  activity check. Guard evaluation errors were not caught and could escape
  as a 500.
- **Intent Analyzer MODEL_ERROR / TIMEOUT / INVALID_OUTPUT:** the provider
  returned a safe raw SINGLE and the pipeline **ignored the status**. The
  selector still ran on the raw turn.
- **Selector errors:** once no intent had produced an answer, the whole
  **raw** turn went to the compatibility fallback:
  - INVALID_OUTPUT → Calendar gate closed;
  - MODEL_ERROR / TIMEOUT → Calendar gate open.
- **g25:** if any intent returned SEMANTIC_NONE or NO_ELIGIBLE, the error
  fallback was suppressed completely, so an errored intent lost its fallback.
- **Guard semantics:** `GuardDecision` has separate `selector_allowed` and
  `fallback_allowed` flags. `semantic_selector_only` inside its validity
  window is selector-only; outside the window both flags are false.
- **Thresholds:** `MEILI_THERESHOLD` = 0.90 (≥), `QDRANT_THERESHOLD` = 0.75
  (>). Neither was changed.

## Degraded mode and execution modes

Degraded mode means: an LLM capability is unavailable, so the deterministic
path looks for a curated answer. `ExecutionMode` has four values:

| Mode | When |
|---|---|
| `NORMAL_LLM` | Analyzer and selector ran (SELECT, NONE, or NO_ELIGIBLE) |
| `ADMIN_DEGRADED` | Admin `LLM_ENABLED=false`, or no provider/key is configured. No LLM call and no breaker mutation. Reason: `llm_disabled_or_unavailable` |
| `REQUEST_DEGRADED` | A request-level analyzer/selector MODEL_ERROR, TIMEOUT or INVALID_OUTPUT, or a pipeline exception |
| `CIRCUIT_DEGRADED` | A capability circuit was OPEN, so the call was skipped |

Each intent carries a typed resolution: `SELECTED`, `SEMANTIC_NONE`,
`NO_ELIGIBLE_CANDIDATES`, `DEGRADED_SELECTED` or `DEGRADED_NONE`. It also
carries its own execution mode and degraded reason. The request-level mode is
`CIRCUIT_DEGRADED` if any intent is circuit-degraded, otherwise
`REQUEST_DEGRADED` if any intent degraded, otherwise `NORMAL_LLM`.

A single request failure never writes to the DB `LLM_ENABLED` flag.

## Deterministic degraded service

`answer_in_degraded_mode(query, db, routing_policy, calendar_gate, reason,
trace, purpose, query_kind)` is the single source of truth. It returns
`DegradedAnswer(answer, source, qna_id, calendar_id)` and never calls an LLM.
`_fallback_answer` remains a thin compatibility wrapper around it.

The order is unchanged:

1. **Calendar**, through Calendar V2 (`retrieve_calendar_candidates`, limit 1)
   with its current year/term, explicit year/term, meaningful-event and
   no-random-event rules. There is no all-rows behavior. The gate is one of:
   - `date_query`: the deterministic `is_date_query` gate. Used for admin OFF
     and for analyzer failures, where no analysis is available.
   - `intent_relevant`: opened by the analyzer's `calendar_relevant` flag.
     Used when a selector fails after a successful analyzer.
   - `closed`.
2. **Meili** (limit 3), taking the top hit **after** safety filtering; it must
   score ≥ 0.90.
3. **Qdrant** (limit 5), top safe hit; it must score > 0.75.
4. Otherwise, no answer.

Degraded QnA safety applies to every source:

- guard **fallback** semantics (`fallback_allowed`): `semantic_selector_only`,
  expired and not-yet-valid guarded QnA can never be a degraded answer;
- a guard evaluation error, a missing `qna_id` or a failed activity lookup
  **fails closed**;
- the QnA must exist with `status=1` (one bounded query).

The `status=1` check is new in this phase. It makes the degraded path
symmetric with Phase 4 eligibility, and it guards against a stale index.

Suggestions are unchanged: they use the Phase 4 `guard_safe_suggestions` and
are still non-answers.

## Failure policies

**Intent Analyzer**

| Outcome | Breaker | Behavior |
|---|---|---|
| MODEL_ERROR / TIMEOUT | +1 failure | Raw SINGLE is kept. The whole raw current turn goes to degraded (`date_query` gate). **No selector.** Reason `intent_analyzer_model_error` / `_timeout` |
| INVALID_OUTPUT | no change | Same degraded path. Reason `intent_analyzer_invalid_output` |
| Circuit OPEN | skipped | No analyzer or selector call. Raw turn goes to degraded. Reason `intent_analyzer_circuit_open` |

**Selector**, per intent, after a successful analyzer:

| Outcome | Breaker | Behavior |
|---|---|---|
| SELECT | success (reset) | Curated answer |
| SEMANTIC_NONE | success (reset) | **Final for this intent.** No degraded path, no Meili/Qdrant/Calendar |
| NO_ELIGIBLE_CANDIDATES | untouched (no call) | Final for this intent. No degraded path |
| INVALID_OUTPUT | neutral | **Only this intent** degrades, using its `resolved_text` |
| MODEL_ERROR / TIMEOUT | +1 failure | Only this intent degrades, using its `resolved_text` |
| Circuit OPEN | skipped | Deliberate choice: retrieval and eligibility still run, so Intent Analyzer semantics are preserved and NO_ELIGIBLE stays final without spending a probe slot. The LLM call is skipped and only this intent degrades on its `resolved_text`. Cost: one extra retrieval round per affected intent |

When a selector fails, the degraded Calendar gate is the intent's
`calendar_relevant` flag. Degraded retrieval never uses the raw multi-intent
turn or the conversation history.

**Composition:** intent order, exact-duplicate dedupe, joined with `\n\n`.
The existing source vocabulary is kept: if any intent was selector-answered
the source is `llm`; otherwise it is the first degraded source.

**g25 resolution:**

- intent A NONE + intent B TIMEOUT → A is final, and only
  `resolved_text(B)` is degraded;
- A SELECT + B MODEL_ERROR → A's answer is kept and B degrades on its own
  text;
- both intents error → each intent degrades on its own resolved text;
- the combined raw turn is never re-answered.

The Phase 4 "suppress the error fallback when any NONE is present" rule was
removed. Tests cover all four cases.

## Circuit breaker

The breaker is in
[`circuit_breaker.py`](../../backend/services/circuit_breaker.py).

- **Scope and key:** `capability:provider:model:fingerprint[:16]`, for
  example `selector:openai:gpt-4o-mini:…`. The Intent Analyzer and the
  selector have independent breakers. A new model or config gets a new key,
  so it starts CLOSED and an old OPEN circuit never blocks it. This is the
  foundation Phase 6 needs.
- **Configuration:**
  - `LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD`, default **3** consecutive
    infrastructure failures;
  - `LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS`, default **60**;
  - invalid values fail at startup.
- **CLOSED:** calls go through. When the failure count reaches the threshold,
  the circuit opens.
- **OPEN:** calls are skipped (`llm_call_skipped=true`) and the deterministic
  path runs.
- **HALF_OPEN:** after cooldown, exactly **one** atomic probe permit is
  granted and concurrent requests are skipped (tested with 16 threads).
  - A successful probe moves the circuit to CLOSED and resets the counter.
  - A failed probe moves it back to OPEN and restarts the cooldown.
  - A probe that never reports back is re-leased after another cooldown, so a
    crashed request cannot wedge the circuit.
  - A probe that gets INVALID_OUTPUT proves the provider is reachable, so it
    also closes the circuit.
- **What counts:**
  - Counted as failures: MODEL_ERROR and TIMEOUT, plus unexpected exceptions
    from the provider call.
  - Counted as success (resets the counter): schema-valid SELECT, NONE, or
    analyzer output.
  - Neutral: INVALID_OUTPUT does not count and does not reset.
  - Never touch the breaker: NO_ELIGIBLE_CANDIDATES and admin OFF.
- **Retries:** exactly one record per logical invocation. SDK-internal
  retries are already collapsed inside `_invoke`, and no retry count is
  invented.
- **Concurrency:** one `threading.Lock` guards every state transition. The
  clock is `time.monotonic`, and tests inject a fake clock. The app runs sync
  endpoints in a threadpool, so this matches the execution model.
- **Admin OFF → ON:** this is an explicit operator action and resets the
  breakers. There are two triggers:
  - `PUT /api/settings/llm {"enabled": true}` resets the node that handled
    the request;
  - every node observes its own effective OFF → ON transition on its next
    request and resets itself.

  While admin OFF is active the breaker is never mutated or probed.
- **Distributed state:** none. The breaker is process- and node-local, with
  no Redis or DB coordination.
- **Failure category:** MODEL_ERROR carries a safe `failure_category`
  (`RATE_LIMIT`, `AUTH`, `NETWORK`, `PROVIDER_5XX`, `SDK_ERROR`, `TIMEOUT`,
  `UNKNOWN`), derived only from the exception class and status code. No error
  text or secrets are recorded. Missing or invalid keys are also classified
  (usually `AUTH`); they degrade the request and can open the circuit if
  repeated.

## Observability (decision trace schema v5)

The trace schema moves from 4 to **5**:

- **`execution`:** request `execution_mode`, `degraded_reason`, and one entry
  per intent: position, resolution, execution mode, selection outcome,
  degraded reason, answer source, `qna_id`, `calendar_id`.
- **`circuit[]`:** one entry per capability call or skip:
  - capability, purpose, `circuit_key`, provider, model, `config_fingerprint`;
  - `circuit_state_before` and `circuit_state_after`;
  - `consecutive_failures_before` and `consecutive_failures_after`;
  - `llm_call_skipped`, `probe_attempted`;
  - `call_status`, `availability_outcome`, `failure_kind`,
    `failure_category`;
  - `transition` (`opened` / `recovered` / `reopened`), `degraded_reason`.
- **`degraded[]`:** one entry per deterministic run:
  - purpose, reason, `query_kind` (`raw_current_turn` / `resolved_intent`),
    `calendar_gate`;
  - `degraded_calendar_attempted`, `degraded_meili_attempted`,
    `degraded_qdrant_attempted`;
  - `degraded_selected_source`, `degraded_selected_qna_id`,
    `degraded_selected_calendar_id`, `degraded_no_answer`;
  - `exclusion_reasons`.
- **`fallback`:** kept as the request-level summary and derived from
  `degraded[]`. `record_fallback` and the Phase 4 `selection` block were
  removed.
- **`final`:** the pipeline now records `final_qna_ids` and `answer_count` for
  every answered request, including degraded multi-intent answers. The
  router's `answer_count=1` override was removed.
- **Transition logs:** each OPEN or recovery also emits one structured
  `llm_circuit_transition={...}` log line.

Metrics derivable from the structured logs, and the field each one comes
from:

| Metric | Source field |
|---|---|
| Normal / admin-degraded / request-degraded / circuit-open request rate | `execution.execution_mode` (one trace per request) |
| Capability error rate | `circuit[].failure_kind` ≠ null, grouped by `capability` |
| Timeout rate | `circuit[].failure_kind == "timeout"` |
| Circuit open count | `circuit[].transition == "opened"`, or `llm_circuit_transition` log lines |
| Circuit recovery count | `circuit[].transition == "recovered"` |
| Degraded answer success rate | `degraded[].degraded_no_answer == false` over all `degraded[]` entries |

No raw user, resolved or answer text, provider body, or exception message is
logged.

## Tests

The new file [`test_degraded_mode.py`](../../backend/tests/test_degraded_mode.py)
has 33 tests. They use real Selector V2 parsing, scripted analyzer and network
calls, and a real test Postgres.

- **Breaker unit tests:**
  - it opens on the 3rd consecutive failure;
  - a success resets the counter;
  - INVALID_OUTPUT is neutral;
  - HALF_OPEN recovers on probe success;
  - a failed probe reopens the circuit and restarts the cooldown;
  - 16 concurrent threads get exactly one probe;
  - a lost probe is re-leased;
  - env configuration is validated;
  - keys are scoped by capability and config.
- **Semantic outcomes:** semantic NONE and NO_ELIGIBLE never touch the breaker
  or the degraded path.
- **Selector timeout:** a single timeout sets the count to 1, keeps the circuit
  CLOSED, degrades only the affected intent with full provenance, and leaves
  the DB flag unchanged.
- **Opening and recovery:** 3 failures open the circuit, and the next request
  skips the provider (`CIRCUIT_DEGRADED`). A pipeline-level HALF_OPEN probe
  recovers the circuit.
- **Invalid output:** 3 invalid outputs degrade the request each time but
  never open the circuit.
- **Admin:**
  - admin OFF: no LLM call, empty breaker state, Calendar → Meili → Qdrant
    order;
  - admin OFF → ON resets the breaker through the pipeline and through the
    settings API;
  - a new selector config is isolated from an old OPEN circuit.
- **Analyzer failures:** MODEL_ERROR, TIMEOUT and INVALID_OUTPUT degrade the
  raw turn, never call the selector, and count 1/1/0 breaker failures
  respectively. An open analyzer circuit skips both analyzer and selector.
- **Per-intent isolation:**
  - a selector failure degrades on `resolved_text`, not the raw turn or the
    conversation context;
  - g25 NONE + TIMEOUT;
  - SELECT + MODEL_ERROR;
  - both intents erroring.
- **Degraded guard safety:** protected, expired, inactive and deleted QnA
  never become degraded answers; guard evaluation errors fail closed; the
  thresholds are pinned at 0.90 (≥) and 0.75 (>).
- **Degraded Calendar:** explicit Güz, the current-term default, historical
  year rejection and no random event all hold. After a selector failure,
  `calendar_relevant` opens the Calendar gate.
  A Calendar V2 retrieval exception stays inside its intent
  (`failed_calendar_result`) and does not escape as a raw-turn degraded
  answer.

Existing tests updated deliberately:

- `test_answer_pipeline_phase1.py`: the Phase 4 routing tests became
  execution-mode tests (unexpected exception → REQUEST_DEGRADED on the raw
  turn; OFF → ADMIN_DEGRADED; success).
- `test_selector_v2.py`: the fallback spies now target
  `answer_in_degraded_mode`. The Phase 4 mixed NONE + error test now asserts
  the g25 resolution. Error and invalid-output cases now expect per-intent
  degradation.
- `test_decision_trace.py`, `test_answer_pipeline_phase2.py`: schema v5.
- `test_calendar_v2.py`: the Calendar route purpose is now `degraded_request`.
- `test_widget_tokens.py`, `test_maintenance.py`, `test_routing_guards.py`:
  the fake LLM-off hits now carry `qna_id` and an active QnA row, because of
  the new degraded `status=1` rule.
- `conftest.py`: an autouse reset of the breaker and admin-mode tracker, so
  process-global state cannot leak between tests.

**Results**, in an isolated `postgres:15` container plus a backend-image
pytest container:

```text
test_degraded_mode.py:                     33 passed
Full suite, backend-only /app layout:      334 passed, 7 failed (known layout cases)
Full suite, repository-root layout:        341 passed, 0 failed
Phase 4 end state (ccce39d), re-measured in this harness before any
Phase 5 change (backend-only layout):      306 passed, 7 failed
```

The seven known failures are the unchanged `/deploy/production` layout cases.
They pass with the repository root mounted.

Regressions:

- **Phase 2:** the analyzer suite and orchestration tests are unchanged and
  passing.
- **Phase 3:** the Calendar V2 suite passes.
- **Phase 4:** the eligibility, typed candidate, structured output, leakage,
  semantic-NONE-final and suggestion tests pass.

The healthy `NORMAL_LLM` path is behaviorally unchanged.

## Architecture alignment

§3.7 (NONE final), §3.8 (a model error is not NONE), §11.2 (distinct outcome
types) and §11.3 (degraded mode is not triggered by NONE; a single request
timeout is not a global OFF; the Meili → Qdrant → no-answer order) are
implemented as written. Calendar stays at the front of the degraded chain, as
the product decision and Phase 0 behavior require. No conflict with the
source of truth.

## Known limitations

- The breaker is node-local. Each app node learns provider health
  independently, and one node's settings-API reset is seen by other nodes
  only when they observe the OFF → ON transition themselves.
- An OFF → ON flip that happens entirely between two requests on some node is
  not observed by that node, so its breakers are not reset. They still
  recover through HALF_OPEN within one cooldown.
- A missing provider/key is labeled `ADMIN_DEGRADED`, like admin OFF, because
  both are operator configuration.
- An INVALID_OUTPUT probe closes the circuit. This treats "the provider
  answered" as availability evidence. Quality is measured separately.
- Degraded answers for selector-failed or circuit-open intents still pay for
  their earlier retrieval. Meili and Qdrant are queried again at fallback
  limits (the deliberate choice described above).
- There is no live-provider validation of failure categories (no API calls
  were made).

Phase 6 has not been started.
