# Internal Pilot — Runtime Preflight Report

**Milestone:** `INTERNAL_PILOT`
**Freeze fingerprint:** `c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6` — **unchanged**
**Runtime preflight:** `INTERNAL_PILOT_RUNTIME_PREFLIGHT = PASS` (18/18)
**Remaining blockers:** 0
**Live LLM calls:** 0

Run it:

```bash
cd backend && python -m scripts.internal_pilot_preflight runtime
```

---

## 0. Two separate axes

The freeze and the runtime are deliberately *not* the same claim:

| Axis | Question | Status |
| --- | --- | --- |
| **Internal Pilot Freeze** | Is the frozen decision recorded correctly, deterministically, reproducibly? | **PASS** (unchanged) |
| **Internal Pilot Runtime Preflight** | Does the runtime, configured as the pilot will configure it, actually behave that way? | **PASS** |
| **Internal Pilot Acceptance Execution** | Has the Docker/E2E acceptance actually been run? | **NOT_STARTED** |

**The freeze artifact was not modified.** `answer-pipeline-freeze.json` still
records the observability gaps with `gap_status = MUST_CLOSE_BEFORE_PILOT`,
because that is what was true *at freeze time*. Closing a gap is reported
here, on the runtime axis — rewriting the freeze would destroy the record it
exists to keep. A test pins the fingerprint so this cannot drift.

---

## 1. The three original blockers

### BLOCKER-1 — runtime served the production prompt

**Was:** `services.selector` served the historical production prompt
(`2d59cfb6…`); `variant_a_v1` existed only as a benchmark-only file.

**Closed by** a production-side versioned prompt catalog,
`services/selector_prompt_catalog.py`:

| Version | Source | Fingerprint |
| --- | --- | --- |
| `production_v2` | `services.selector.SELECTOR_SYSTEM_PROMPT`, read **dynamically** | `2d59cfb65f0aaa…` |
| `variant_a_v1` | `services/prompts/selector/variant_a_v1.md` | `1aed568885db02…` |

- `build_selector_prompt` now resolves the system prompt through the catalog.
  Only the system prompt varies; the candidate serializer is untouched, so
  the selector contract fingerprint (`d50fbee416ca…`) is unchanged.
- **The historical production prompt was not mutated.** Its text, its
  fingerprint and the committed benchmark prompt manifest are all byte-identical.
- `production_v2` is resolved by *module attribute access at call time*, never
  cached — so tests that monkeypatch `SELECTOR_SYSTEM_PROMPT` still observe
  the change, and the benchmark contract fingerprint still tracks it.
- An unknown version raises `UnknownSelectorPromptVersion` rather than
  silently falling back; serving a prompt other than the configured one is
  precisely the drift the freeze exists to prevent.

**No `services → benchmarks` dependency exists.** A preflight check
AST-scans `services/`, `core/`, `routers/`, `admin/` and `scripts/` for any
`import benchmarks`; the result is empty.

#### Why the Variant A text exists in two files

`benchmarks/selector_v2/prompts/variant_a_v1.md` is a **historical artifact**:
its path is recorded in the committed benchmark prompt manifest, which six
test modules assert byte-for-byte, and which this task must not rewrite. The
runtime needs its own copy so it never imports the benchmark package. The two
copies are therefore both required, and
`test_runtime_and_benchmark_variant_a_copies_are_identical` asserts they stay
byte-identical and fingerprint the same. If they ever diverge, that test fails.

### BLOCKER-2 — `LLM_ENABLED` seeded to `false`

**Was:** `scripts/init_system.py` hardcoded `LLM_ENABLED = "false"` on a
fresh install.

**Closed by** making the *seeded value* configurable via
`LLM_ENABLED_DEFAULT`, without touching the seeding semantics:

- **Public default is unchanged.** With nothing configured,
  `llm_enabled_default()` returns `"false"` exactly as before.
- **Internal pilot** sets `LLM_ENABLED_DEFAULT=true`.
- **Seed-if-missing is preserved verbatim.** The `if row is None:` branch is
  untouched: an admin's persisted value — including an emergency `false` —
  is never overwritten. Tests cover existing-`false`-stays-`false`,
  existing-`true`-stays-`true`, and fresh-pilot-seeds-`true`.
- An unparseable value raises rather than silently defaulting.

**No database was mutated by this task.** The seeding branch is exercised
against an in-memory fake, and preflight validates the *desired* state the
pilot stack will resolve. The actual persisted state is checked later, during
Docker acceptance (Stage A8).

### BLOCKER-3 — four missing observability fields

**Correction to the freeze report.** The freeze recorded
"session/conversation correlation" as missing. That was **inaccurate**:
`DecisionTrace.conversation_id` already existed, was populated at
`routers/chat.py:205`, and was already emitted as
`request.conversation_id`. The original grep did not cover that field name.
Only three fields were genuinely absent. The freeze artifact keeps the
original wording because it is a historical record; this report is the
correction.

| Field | Before | Now |
| --- | --- | --- |
| Conversation correlation | **already present** (verified) | unchanged; `request.conversation_id`, an opaque integer id |
| Wall-clock timestamp | missing | `request.timestamp`, timezone-aware UTC ISO-8601 with `Z`, ms precision |
| `retrieval_ms` | missing | on every retrieval snapshot, monotonic clock |
| Per-source availability | proxies only | `source_availability` with typed states |

All four are **additive**; no existing field was removed or re-interpreted.
Trace `schema_version` is bumped **6 → 7**.

`/api/search` constructs its trace without a `conversation_id` because that
endpoint has no conversation. The pilot request path is `/widget-chat`, which
does populate it; correlation tests are scoped accordingly.

---

## 2. Reasoning omission — confirmed at payload level

The frozen baseline's `reasoning = none` means the reasoning field is
**UNSET**, i.e. no reasoning key is transmitted at all.

- Pilot selector config resolves `reasoning_effort = None`, capability config
  fingerprint `af9eb2d0767d…` — the exact config every validated Variant A
  run used.
- The check is made **on the built request payload**, not by comparing config
  fingerprints: `reasoning_request_fields("openrouter", None)` returns `{}`.
- A test proves the counterfactual: `ReasoningEffort.NONE` would emit
  `{"extra_body": {"reasoning": {"effort": "none"}}}` — a different request
  (config `caa89baa…`) that no benchmark has measured.
- `deploy/internal-pilot/pilot.env.example` deliberately does **not** set
  `LLM_SELECTOR_REASONING_EFFORT`, and a test asserts its absence.

OpenRouter's `ReasoningEffort.NONE` transport support (added in `88b1970`)
remains in place; the pilot simply does not use it.

---

## 3. Observability schema (trace v7)

```
request.request_id          opaque UUID4
request.conversation_id     opaque integer id (null on /api/search)
request.timestamp           "2026-09-20T08:15:31.123Z"  (UTC, tz-aware)
source_availability         {calendar, meili, qdrant} -> state
retrieval[].retrieval_ms    float, milliseconds, monotonic
retrieval[].qdrant_availability / .meili_availability
```

### `retrieval_ms` scope — documented from code behaviour

`retrieval_ms` measures the **QnA retrieval leg only**: entry to completion
of Qdrant + Meili inside `_build_candidate_pool_result`.

- **Calendar is excluded.** Calendar entries are resolved upstream by
  `search_calendar` / `retrieve_calendar_candidates` and arrive already built;
  calendar has its own `calendar_retrieval_latency_ms`.
- **Selector/LLM latency is excluded** — recorded separately per selector entry.
- It is recorded **per retrieval entry**, not once per request, because
  `record_retrieval` fires once per intent (MULTI produces several).

`test_retrieval_ms_scope_is_the_qna_leg_only` pins this by inspecting the
timed window in the source, so the definition cannot drift silently.

### Source availability semantics

| State | Meaning |
| --- | --- |
| `available` | The source was called and answered — **including with zero results** |
| `unavailable` | The source failed, or is inside its circuit-breaker cooldown after a failure |
| `skipped` | The source was deliberately not consulted by pipeline policy |

**`0 results != unavailable`** is the load-bearing distinction: a successful
empty answer stays `available` with `candidate_count = 0`. Collapsing the two
would make an empty knowledge base look like an outage.

- **Calendar** — `skipped` when the calendar gate judged the query irrelevant
  (no DB/config lookup occurs on the closed route); `unavailable` on a
  retrieval error; otherwise `available`.
- **Meili** — an open circuit breaker is treated as **`unavailable`**, not
  `skipped`: it is a known outage, not a policy decision.
  `meili_search_with_availability` returns the state; `meili_search_safe`
  keeps its exact previous behaviour on top of it.
- **Qdrant** — an exception is `unavailable`; otherwise `available`.
- `unavailable` is **sticky** within a request: if a source failed even once,
  the request did not get the full candidate pool and the trace says so.

Availability is derived centrally inside `record_retrieval` and
`record_calendar_route`, so every call site feeds it — including the degraded
path — without each one having to remember.

---

## 4. Privacy

No raw user content, identifier or secret was added to telemetry. Correlation
uses opaque ids only (UUID4 request id, integer conversation id) — never an
identity number, phone, email, student id or message text. The existing
context handling still retains only aggregate counts.

A regression test serializes a trace built from a request carrying a raw
message, an identity number, a phone number, a verification code, an API key
and an authorization header, and asserts none appear in the payload.

---

## 5. Luna benchmark isolation (re-verified)

The pacing, retry and fail-fast mechanisms from the Luna experiment remain
**benchmark-only**: `PacedBackend`, `FailFastBackend` and the
`--fail-fast-after` / `--retry-errors` CLI flags all live under
`backend/benchmarks/`. Shared production runtime retry/backoff/timeout
behaviour is unchanged.

This task edited shared runtime (`selector.py`, `decision_trace.py`,
`answer_pipeline.py`, `candidate_eligibility.py`, `core/deps.py`,
`scripts/init_system.py`), so the check was re-run over *this* change as
well: grepping the diff for added `retry` / `backoff` / `fail_fast` / `sleep`
/ `throttle` / `max_retries` lines in `services/`, `core/`, `routers/`,
`admin/` and `scripts/` returns nothing. **No separate blocker was raised.**

---

## 6. Configuration and Docker wiring

| Artifact | Purpose |
| --- | --- |
| `deploy/internal-pilot/pilot.env.example` | Desired non-secret pilot settings. **Preflight reads this file** as the desired configuration rather than ambient shell env. No real key present; a test asserts credential-named keys carry no value. |
| `docker-compose.yml` | Backend service now receives `SELECTOR_PROMPT_VERSION: ${SELECTOR_PROMPT_VERSION:-production_v2}` and `LLM_ENABLED_DEFAULT: ${LLM_ENABLED_DEFAULT:-false}`. Both defaults reproduce the historical public behaviour exactly. |
| `docker-compose.dev.yml` | No change needed — it is an override layered on the base file and inherits both variables. |
| `deploy/production/config/backend.env.example` | Documents both settings at their public defaults. |

Static tests assert the override reaches the backend service. **No container
was started, built or restarted.**

### ⚠️ Deployment prerequisite — the operator must set the variables

`pilot.env.example` documents the *desired* values and is what preflight
validates, but **the backend service does not read it**: the compose backend
uses `env_file: .env`, and compose resolves `${SELECTOR_PROMPT_VERSION:-…}`
from the shell environment or the project-root `.env`.

So before starting the pilot stack the operator must put both variables in the
project-root `.env` (or export them):

```
SELECTOR_PROMPT_VERSION=variant_a_v1
LLM_ENABLED_DEFAULT=true
```

Without this the compose defaults win and the container would serve
`production_v2` with `LLM_ENABLED` seeded `false` — **while this preflight
still reports PASS**, because preflight validates the declared pilot config,
not the deployed container's environment.

This is deliberate: wiring `env_file` to the pilot file is a deployment-shape
decision, and this task performs static validation only. The gap is closed by
**acceptance Stage A**, which checks the *live resolved* selector prompt and
LLM state inside the running stack before the pilot begins.

---

## 7. Preflight result

```
[PASS] selector_prompt_version_is_variant_a      [PASS] selector_config_fingerprint_matches
[PASS] selector_prompt_fingerprint_matches       [PASS] selector_reasoning_is_unset
[PASS] runtime_does_not_import_benchmarks        [PASS] selector_request_omits_reasoning_key
[PASS] selector_config_resolvable                [PASS] pilot_fresh_llm_enabled_is_true
[PASS] selector_provider_matches                 [PASS] trace_has_correlation_and_timestamp
[PASS] selector_model_matches                    [PASS] trace_timestamp_is_utc_iso8601
[PASS] selector_temperature_matches              [PASS] trace_has_source_availability
[PASS] selector_max_tokens_matches               [PASS] source_availability_states_are_typed
                                                 [PASS] trace_has_retrieval_ms
                                                 [PASS] trace_schema_version_bumped

INTERNAL_PILOT_RUNTIME_PREFLIGHT = PASS
```

The preflight is discriminating, not unconditionally green: a test asserts it
**FAILs** on the default (non-pilot) configuration, on explicit reasoning, and
on model drift.

---

## 8. Tests

| Suite | Result |
| --- | --- |
| Backend full suite | **658 passed, 0 failed** (598 at the freeze commit + 60 new) |
| Runtime preflight + freeze (targeted) | 105 passed |
| Deployment lifecycle + release build | 46 passed |
| Frontend (`ng test`) | not run — no change was made to `chatbot-web/`; the compose/env wiring is covered by static Python tests |

**Regression: none.** One regression *was* introduced and fixed during this
task: switching the retrieval call from `meili_search_safe` to a new helper
broke 4 pipeline tests that monkeypatch that exact symbol, and a missing
`import time` broke 38 more. Both were fixed by keeping `meili_search_safe`
as the call seam and adding the import; the final run is clean.

---

## 9. Remaining blockers

**None.** All three are closed.

What is still outstanding is *execution*, not readiness:

- Internal Pilot Acceptance Execution: `NOT_STARTED` — no Docker stack, E2E
  run or live OpenRouter call happened in this task.
- Phase 7G `PUBLIC_PRODUCTION_FINAL_FREEZE`: `DEFERRED_UNTIL_INTERNAL_PILOT_DATA`.
- Phases 7C–7F: `BACKLOG_POST_INTERNAL_PILOT`.
- The known selector limitations (`IP-KI-1` generic-vs-specific qualifier,
  `IP-KI-2` expected-NONE undervalidated) are unchanged — the pilot exists to
  gather real data on them.
- Semantic Gold remains model-adjudicated (`IP-KI-3`); independent human
  validation is still required before public production.
