# Internal Pilot — Acceptance Execution Report

**Milestone:** `INTERNAL_PILOT`
**Tested commit:** `9c3d893c61849cb94ddf4b632df812bd7a431179`
**Freeze fingerprint:** `c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6` — **unchanged**

> ## `INTERNAL_PILOT_ACCEPTANCE = FAIL`
> ## `INTERNAL_PILOT_RELEASE_CANDIDATE = BLOCKED`
>
> The frozen configuration is served correctly, but **the pipeline does not
> work end to end**: every live request degrades at the Intent Analyzer and
> Selector Variant A is never invoked. The pilot cannot open in this state.

---

## 1. Headline finding — BLOCKER-1 (hard)

**`BLOCKER_INTENT_ANALYZER_INVALID_OUTPUT` — 14 of 14 live requests degraded.**

The Intent Analyzer call *succeeds at transport level* (`call_status: success`,
`finish_reason: stop`, 71 output tokens, real `openai/gpt-4o-mini` response),
but its output fails schema validation, so the request is classified
`INVALID_OUTPUT` and the whole turn falls into `REQUEST_DEGRADED`. The
selector is never reached.

### Root cause — a prompt/schema contract mismatch

The analyzer prompt describes the output schema with **string type labels**:

```json
"output_schema": {
  "intent_count": "1 or 2",
  "intents": [{"source_text": "non-empty string", "context_used": "boolean", ...}]
}
```

Following that shape, the model returns `intent_count` as a **string**:

```json
{"intent_count":"1","intents":[{...}]}
```

But the contract is strict and typed:

```python
class IntentAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent_count: Literal[1, 2]
```

With `strict=True`, Pydantic will not coerce `"1"` → `1`. Validation fails on
every request.

**Measured, not inferred:** four direct raw-output probes — a short fragment
(`harc ne kadar`), a two-intent message, a calendar question and the original
reproduction — **all four returned `"intent_count": "1"` as a string**, and
all 14 live requests failed validation. The failure is systematic across
message shapes, not intermittent.

**Why this was never caught:** the backend test suite mocks the provider, so
no test ever exercised a real `gpt-4o-mini` response against the real schema.
This acceptance run is the first genuine end-to-end execution — which is
exactly what it was for.

### Why it was not fixed here

Closing this needs either the analyzer **prompt** changed or the **strict
validation contract** relaxed. Both alter a component the freeze pins
("Intent Analyzer V2 — strict JSON + Pydantic schema validation"), so both
fall outside the narrow "behaviour-preserving test/config wiring" fix the
acceptance plan permits. It is reported, not silently repaired.

**Suggested fix (for a separate, reviewed change):** make the prompt's
`output_schema` example emit a real integer (`"intent_count": 1`) rather than
the string `"1 or 2"`. That aligns the instruction with the contract without
weakening validation. Requires its own targeted re-validation.

---

## 2. Other blockers

### BLOCKER-2 — `BLOCKER_CALENDAR_DATA` (data currency)

The loaded calendar is the **2025–2026** academic year (19 rows). Today is
**2026-09-20**, i.e. the start of the 2026–27 Güz term, and **no 2026–27
calendar exists**. All 19 rows carry `NULL academic_year`, which Calendar V2
treats as "assume current" — so the mechanism works, but it would answer
date questions with *last year's* dates.

- `migrate_calendar_v2.py` is schema-only; there is **no backfill script and
  no source data** for 2026–27. No date or event was invented.
- Config keys `ACADEMIC_CALENDAR_CURRENT_YEAR=2025-2026` /
  `ACADEMIC_CALENDAR_CURRENT_TERM=GUZ` were set (a configuration change, which
  the plan permits) so the calendar *mechanism* could be exercised honestly.
- **Mechanism: PASS** (2 candidates returned, `calendar_no_match_reason: null`).
  **Data currency: BLOCKED.**

### BLOCKER-3 — `BLOCKER_TEST_ENV_ISOLATION` (medium)

Stage B regressed from **0 failures to 6** purely by enabling the pilot
configuration, because `main.py`, `core/database.py` and
`services/llm_provider.py` all call `load_dotenv()`. The root `.env` therefore
leaks into the pytest process and changes the resolved selector prompt and the
`LLM_ENABLED` seed.

Proof it is an isolation gap and not a runtime defect — same tests, env pinned:

```
SELECTOR_PROMPT_VERSION=production_v2 LLM_ENABLED_DEFAULT=false
  → 110 passed, 0 failed
```

Affected: `test_selector_prompt_experiment` (×2), `test_selector_postmortem`,
`test_selector_qualifier_postmortem`, `test_internal_pilot_runtime`,
`test_initialization`. These tests need to pin the env rather than inherit it.

---

## 3. Environment and effective runtime configuration

**Stage A required a backend image rebuild.** `docker compose up -d backend`
recreated the container from a *stale image* that predated the prompt catalog
(`ModuleNotFoundError: services.selector_prompt_catalog`). Only after
`docker compose build backend` did the runtime carry the pilot code — a real
deployment trap worth noting for the pilot runbook.

Verified **from inside the running container**, not from `.env`:

| Field | Value |
| --- | --- |
| Configured / resolved prompt version | `variant_a_v1` / `variant_a_v1` |
| Resolved prompt fingerprint | `1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e` ✅ |
| Provider / model | `openrouter` / `openai/gpt-4o-mini` |
| Temperature / max_tokens | `0` / `32` |
| Reasoning effort | `null` (unset) |
| Reasoning request fields | `{}` — **no reasoning key emitted** ✅ |
| Selector config fingerprint | `af9eb2d0767d37cd632799cbae39e7938585b243ceb4c7a1527b8028cd489a6e` ✅ |
| `LLM_ENABLED` | `true` (pre-existing persisted value) |

Root `.env` received four non-secret keys (`SELECTOR_PROMPT_VERSION`,
`LLM_ENABLED_DEFAULT`, and the two calendar config keys). Secrets were
preserved untouched; `.env` is gitignored and was not committed. All required
secrets were present — acceptance was **not** blocked on a missing secret.

**`.env` is deliberately left with the pilot overrides in place** (they are
the pilot configuration). Note the direct consequence: on this host the test
suite now fails 6 tests, because those same keys leak into pytest — that is
BLOCKER-3 below, and the two facts belong together. Anyone re-running the
suite here should pin `SELECTOR_PROMPT_VERSION=production_v2` and
`LLM_ENABLED_DEFAULT=false`.

---

## 4. Stage results

| Stage | Status | Summary |
| --- | --- | --- |
| **A** Runtime/config | **PASS** | Frozen config served; verified inside the container (after rebuild) |
| **B** Automated regression | **FAIL** | 6 failed / 698 passed — all six are `.env` leakage; 110 pass when pinned |
| **C** Docker/services | **PASS** | 5/5 services up, db+backend healthy, 0 restart loops |
| **D** Offline regression | **PASS** | Frozen prompt/contract assertions hold; no new LLM benchmark call |
| **E** Live E2E | **FAIL** | 12 scenarios / 13 turns executed, **0 semantic passes**; 14/14 degraded |
| **F** Failure paths | **PASS_PARTIAL** | F1 clean; F2/F3/F4 graceful but telemetry unreachable |
| **G** UI | **MANUAL_REQUIRED** | No test target, no specs, no browser framework |
| **H** Observability | **PASS_PARTIAL** | Correlation/timestamp/schema v7 confirmed; retrieval & selector telemetry unreachable |

### Services

`auzef_db` (healthy), `auzef_backend` (healthy), `auzef_meili`, `auzef_qdrant`,
`auzef_frontend` — all `Up`, restart count 0. Meili index `auzef_qna_index`
holds **326** documents and is searchable; Qdrant collection
`auzef_qna_vectors` is **green** with **3020** points and is searchable.
Tables, registry (3 models), capability config (2) and KB (326 QnA / 2695
aliases) all present.

### Live E2E

All **12 required categories** from the plan were genuinely executed through
the real client path (`POST /widget-chat` via nginx → session → Intent
Analyzer → … → curated answer). The selector was never called directly.

`single_intent`, `short_noisy_language`, `near_qna`, `general_intent`,
`explicit_specific_intent`, `generic_vs_specific_qualifier`,
`explicit_qualifier`, `valid_none`, `calendar_relevant`,
`calendar_irrelevant`, `multi_intent`, `conversation_context`.

**Every one degraded identically.** The run was stopped after 13 of 60
budgeted turns: continuing would have reproduced the same failure ~30 more
times and spent budget for no new information. **0 semantic passes** — not
because answers were wrong, but because the evaluated pipeline was never
exercised.

### Failure paths

| Test | Status | Observed |
| --- | --- | --- |
| F1 admin LLM OFF | **PASS** | `ADMIN_DEGRADED`, `llm_disabled_or_unavailable`, no LLM call, answered from Meili in **13.2 ms**; state restored |
| F2 OpenRouter failure | PASS_PARTIAL | Invalid sentinel key; HTTP 200, graceful no-answer + suggestions, no 5xx. Trace lost on container recreate, so only HTTP behaviour is claimed. Real key restored |
| F3 Meili unavailable | PASS_PARTIAL | Answered from Qdrant, no crash; availability telemetry not reached |
| F4 Qdrant unavailable | PASS_PARTIAL | Curated no-answer + suggestions, no crash, no 5xx; telemetry not reached |

All four restored: `LLM_ENABLED=true`, real key in place, both search
containers healthy, Variant A re-verified. **No degraded test state remains.**

---

## 5. Observability and performance

**Confirmed in 14/14 traces:** `schema_version: 7`, `request_id`,
`conversation_id`, timezone-aware UTC `timestamp`, `execution_mode`,
`degraded_reason`, `final`, and calendar availability recorded as `available`.
No raw PII and no secret appeared in any trace.

**Not verifiable live:** `retrieval_ms`, Meili/Qdrant availability, the
`0 results != unavailable` distinction, and all selector telemetry — every
request degrades *before* retrieval runs, so those code paths never execute.
They are covered by unit tests but remain **unproven in live traffic**.

| Metric | avg | median | p95 | max |
| --- | --- | --- | --- | --- |
| Total | 2468.5 ms | 2349.5 ms | 3202.0 ms | 3344.1 ms |
| Intent Analyzer | 2184.4 ms | 2236.7 ms | 2677.7 ms | 2804.1 ms |
| retrieval_ms | — | — | — | — (never reached) |
| Selector | — | — | — | — (never invoked) |

Sample is 14 traces on a developer host. **These are not production SLA
figures** and no latency gate is asserted.

### Error rates

Logical user turns **18** (13 Stage E catalog + 1 smoke + 4 failure-path) ·
successful **0** · degraded **17** · parser errors **17** · provider errors
**0** · timeouts **0** · 5xx **0** · semantic NONE **0** — zero because the
selector never ran, not because NONE never occurred.

**Inference calls: ~21 of a 120 budget** — 17 Intent Analyzer calls (one per
turn; F1 made none, the LLM being off) plus 4 direct diagnostic probes.
**Selector inference calls: 0**, since the selector was never reached. One
additional F2 call failed authentication by design. Logical turns 18 of 60.

Conversation persistence worked: conversations grew 1692 → 1709, turns were
associated, and multi-turn continuation via `conversation_id` functioned. Raw
conversations were **not** copied into any artifact.

---

## 6. Freeze protection (§37)

Verified by digest before and after the whole run:

| Item | Result |
| --- | --- |
| Freeze fingerprint | unchanged ✅ |
| Variant A bytes (runtime + benchmark copies) | unchanged ✅ |
| Benchmark prompt manifest | unchanged ✅ |
| KB (`qna`) content digest | unchanged ✅ |
| Aliases (`qna_queries`) content digest | unchanged ✅ |
| Calendar content digest | unchanged ✅ |
| `system_config` | unchanged ✅ |

Semantic Gold was not touched. Only `conversations` / `conversation_messages`
grew, as expected from running E2E.

---

## 7. Known limitations (unchanged — not resolved by this run)

- Semantic Gold is **model-adjudicated** (GPT-5.6 Sol), not independent human Gold.
- Independent human final validation is **not done**.
- General-vs-specific qualifier (`IP-KI-1`) remains a known risk class.
- `NONE` coverage (`IP-KI-2`) remains limited and is still unmeasured.

Luna remains a **research candidate**; no model comparison or promotion was
performed. The pilot was tested with **Variant A / gpt-4o-mini** only.

---

## 8. Remaining blockers and what must happen next

1. **`BLOCKER_INTENT_ANALYZER_INVALID_OUTPUT`** — must be fixed and
   re-validated before any pilot traffic. Highest priority.
2. **`BLOCKER_CALENDAR_DATA`** — load a real 2026–27 academic calendar, or
   accept and document that calendar answers are a year stale.
3. **`BLOCKER_TEST_ENV_ISOLATION`** — pin the environment in the six affected
   tests so the pilot config does not break the suite.
4. **UI acceptance is manual** — see the checklist below.
5. Re-run Stages B, E, F and H after the fixes; Stage E has **not** been
   meaningfully executed yet.

### Manual UI checklist (Stage G)

No automation exists (`angular.json` defines only `build` and `serve`; no
`*.spec.ts`; no karma/jasmine/Playwright/Cypress) and installing a browser
framework was out of scope. A human must confirm, at `http://localhost`:

- [ ] Widget loads on the page
- [ ] Message submits; loading state appears and then clears
- [ ] Answer renders correctly (formatting, links)
- [ ] Multi-answer rendering, where applicable
- [ ] Error state shows a friendly message, never a stack trace
- [ ] Session continues across turns (follow-up keeps context)
- [ ] Refresh behaves per the documented history policy
- [ ] Usable at ~375 px mobile viewport

---

## 9. Post-pilot plan (unchanged)

Once the internal pilot actually opens:

```
real conversations → anonymize → deduplicate → representative sampling
→ independent human review → final production-like validation → Phase 7G
```

Phase 7G (`PUBLIC_PRODUCTION_FINAL_FREEZE`) remains
`DEFERRED_UNTIL_INTERNAL_PILOT_DATA`. Phases 7C–7F remain
`BACKLOG_POST_INTERNAL_PILOT`.

---

## 10. Artifacts

`outputs/internal-pilot-acceptance/` — `manifest.json`, `stage-results.json`,
`e2e-results.jsonl`, `failure-path-results.json`,
`observability-results.json`, `performance-summary.json`,
`service-health.json`, `baseline-digest.json`, `after-digest.json`.

Contains no secret value and no raw PII. The directory is gitignored, so it
was force-added deliberately to keep the acceptance evidence with the commit.
