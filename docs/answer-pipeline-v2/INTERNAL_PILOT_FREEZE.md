# Answer Pipeline V2 — Internal Pilot Freeze

**Milestone:** `INTERNAL_PILOT`
**Status:** freeze artifacts complete.
**Runtime status: all three blockers are now CLOSED** —
`INTERNAL_PILOT_RUNTIME_PREFLIGHT = PASS`. See
[`INTERNAL_PILOT_RUNTIME_PREFLIGHT_REPORT.md`](INTERNAL_PILOT_RUNTIME_PREFLIGHT_REPORT.md).
The blocker sections below are retained **as the historical freeze-time
record** and are deliberately not rewritten; the freeze fingerprint is
unchanged.
**Freeze fingerprint:** `c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6`
**Manifest:** `deploy/internal-pilot/answer-pipeline-freeze.json`
**Git commit:** `22a63e87af81960a5f376cb26165149d6d69b70c`

> This is **not** the public production final freeze. Phase 7G
> (`PUBLIC_PRODUCTION_FINAL_FREEZE`) remains
> `DEFERRED_UNTIL_INTERNAL_PILOT_DATA`.

This document is engineering-facing.

---

## 1. Milestone definition

`INTERNAL_PILOT` is a single-server Docker deployment in which AUZEF staff
exercise the chatbot as real users and generate real usage data.

It is explicitly **not** `PUBLIC_PRODUCTION`. Nothing in this freeze claims
public-production readiness, and no accuracy number here is a final accuracy
claim.

---

## 2. Frozen architecture

The normal request path — with the **LLM enabled** — is:

```
User
 → Intent Analyzer (V2)
 → Calendar decision (V2)
 → Retrieval
 → Candidate Eligibility (V2)
 → Selector LLM
 → Curated Answer
```

**Degraded mode is not the pilot's normal behaviour.** It is entered only on
provider/model failure, an open circuit breaker, or an admin emergency
disable. Semantic `NONE` and `NO_ELIGIBLE_CANDIDATES` are final answers and
never open the degraded path.

| Component | Frozen identity |
| --- | --- |
| Intent Analyzer | `services.intent_analyzer` V2 — max 2 intents, max 2 previous *user* turns, bot messages excluded, strict JSON + Pydantic, no regex split fallback, no speculative selector call |
| Intent Analyzer config | Resolved, not hardcoded: DB active config (`ai_registry.load_active_config`), bootstrapped from env via `resolve_llm_config_set`. Env-effective identity: `openrouter` / `openai/gpt-4o-mini`, temperature 0, max_tokens 300, reasoning unset — config fingerprint `2f1b27bc3430…` |
| Calendar | `services.calendar_retrieval` + `services.calendar_utils` V2 — sort key period/event/date then record id (Phase 3 order); calendar candidates precede QnA |
| Candidate Eligibility | `services.candidate_eligibility` V2 — default max 32 candidates (`SELECTOR_MAX_CANDIDATES`), retrieval score rounded to 5 decimals |
| Selector | `services.selector` — strict JSON `SELECT`/`NONE`; malformed output, unknown `candidate_ref` and empty response are `INVALID_OUTPUT`, never semantic `NONE` |
| Degraded mode | `answer_pipeline.answer_in_degraded_mode` — deterministic Calendar → Meili ≥ 0.90 → Qdrant > 0.75, never LLM-generated |
| Circuit breaker | `services.circuit_breaker` — failure threshold 3, cooldown 60 s; `INVALID_OUTPUT` is *neutral* (no availability change outside a probe) |
| Registry / Admin | `services.ai_registry` — snapshot schema 1, immutable `ai_config_version` rows, append-only audit, optimistic concurrency |

No component behaviour was changed by this task.

### Candidate order

The pilot uses the **ORIGINAL retrieval order** (`production`). The neutral
and permuted orders exist only in `benchmarks/selector_v2/contract.py` and
must never reach `services/`. Preflight asserts this.

---

## 3. Selector choice, and the two open pilot blockers

### Frozen selector baseline

| Field | Value |
| --- | --- |
| Prompt version | `variant_a_v1` |
| Prompt fingerprint | `1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e` (verified, 1858 chars) |
| Provider | `openrouter` |
| Model | `openai/gpt-4o-mini` |
| Reasoning | `none` — **unset; no reasoning field transmitted** (see below) |
| Temperature | `0` |
| Max tokens | `32` |
| Capability config fingerprint | `af9eb2d0767d37cd632799cbae39e7938585b243ceb4c7a1527b8028cd489a6e` |
| Selector contract fingerprint | `d50fbee416ca98783e499454fe840c1fdc2ff41b5fb7445cbac20f6a72c7f5a9` |

#### `reasoning = none` means *unset*, not `ReasoningEffort.NONE`

This distinction is load-bearing and easy to get wrong. Every validated
Variant A run (DEV and HOLDOUT) used config fingerprint `af9eb2d0…`, which
has `reasoning_effort = None` (**no reasoning field is sent at all**).

Since commit `88b1970` added `"none"` to the OpenRouter entry of
`REASONING_TRANSPORT`, setting `ReasoningEffort.NONE` would transmit an
explicit `reasoning.effort="none"` field and produce config fingerprint
`caa89baa…` — **a different request that no benchmark has ever measured.**
Preflight and tests pin the unset variant.

### ⚠️ Blocker 1 — the runtime does not serve Variant A

`variant_a_v1` currently exists **only** as a benchmark-only file at
`backend/benchmarks/selector_v2/prompts/variant_a_v1.md`. The runtime still
serves `services.selector.SELECTOR_SYSTEM_PROMPT`, i.e. the `production`
prompt, fingerprint `2d59cfb65f0aaa…`.

This freeze deliberately **did not** change that. Swapping the constant would
alter runtime behaviour with no possibility of validation in this task (no
live benchmark calls were permitted), and it would silently rewrite the
`production` entry of `benchmarks/selector_v2/prompts/manifest.json`, which
six test modules assert against.

**Landing `variant_a_v1` in `services.selector` is a prerequisite for
starting the internal pilot and is a separate, explicitly approved change.**

### ⚠️ Blocker 2 — the LLM is not enabled by default

`backend/scripts/init_system.py` seeds `DEFAULT_SYSTEM_CONFIG["LLM_ENABLED"]
= "false"`. The pilot's normal operating mode requires the LLM enabled.
Preflight reports this as FAIL and **does not change it**: flipping an
operator-facing default is an operator decision.

### Current preflight result

```
[PASS] selector_config_resolvable            [PASS] selector_config_fingerprint_matches
[PASS] selector_provider_matches             [FAIL] selector_prompt_fingerprint_matches
[PASS] selector_model_matches                [FAIL] llm_enabled_seeded_default
[PASS] selector_reasoning_matches            [PASS] candidate_order_expected
[PASS] selector_temperature_matches          [PASS] degraded_mode_available
[PASS] selector_max_tokens_matches           [PASS] provider_policy_openrouter_only

RESULT: FAIL (2 blockers)
```

Run it with:

```bash
cd backend && python -m scripts.internal_pilot_preflight preflight
```

**Run preflight from a host checkout, not inside the backend container.**
`backend/Dockerfile` uses `WORKDIR /app` + `COPY . .`, so the container holds
only the `backend/` subtree. `repo_root()` (and therefore the
`LLM_ENABLED` seed read and the manifest path) resolves above `/app` and the
check would report "unknown" — a FAIL for the wrong reason.

---

## 4. Why Variant A

### Semantic DEV

| Configuration | Score |
| --- | --- |
| Production / 4o-mini | 51/77 |
| **Variant A / 4o-mini** | **64/77** |
| Variant B / 4o-mini | 56/77 |
| Variant C / 4o-mini | 63/77 |
| Variant C / Luna | 64/77 |

### Variant A HOLDOUT

| Metric | Value |
| --- | --- |
| Variant A | 30/38 |
| Production | 24/38 |
| only A correct | 6 |
| only Production correct | 0 |

### ⚠️ Historical HOLDOUT gate status: **FAIL**

The historical Variant A HOLDOUT is recorded as **FAIL** because of the
471/472 hard gate. **This is not softened, hidden or converted to PASS.**
Variant A is selected as the internal-pilot baseline *despite* that
historical gate failure, on these grounds:

- strongest validated low-cost baseline;
- major false-`NONE` reduction;
- no observed HOLDOUT corruption versus Production (6 cases only A gets
  right, 0 cases only Production gets right);
- Luna did not improve aggregate DEV accuracy;
- public-production validation is deferred to pilot data.

### Luna

| Field | Value |
| --- | --- |
| Full DEV | COMPLETE |
| Variant C / Luna | 64/77 |
| M1 | **FAIL** (unchanged) |
| Status | **research candidate** |
| Auto-promoted to pilot selector | **No** |

Luna is not promoted to the pilot selector. M1 stays `FAIL`.

---

## 5. Known limitations

### `IP-KI-1-generic-vs-specific-qualifier`

A generic intent can be routed to a more specific candidate that assumes a
qualifier the user never stated (class: *general vs specific qualifier*).
Representative benchmark case IDs are retained in the Phase 7B engineering
reports, not here and not in any user-facing document.

### `IP-KI-2-expected-none-undervalidated`

Expected-`NONE` behaviour is not validated on a sufficiently large sample.
`NONE` recall remains unmeasured.

### `IP-KI-3-semantic-gold-is-model-adjudicated`

Semantic Gold was produced by **blind model adjudication**, adjudicator
**GPT-5.6 Sol**. It is **not** independent human Gold. Public-production
final validation therefore requires human review independent of the pilot
data.

---

## 6. Provider policy

| Field | Value |
| --- | --- |
| Inference provider | **OpenRouter only** |
| Selector | `openai/gpt-4o-mini` via OpenRouter |
| Intent Analyzer | existing configured OpenRouter policy |
| Direct OpenAI calls | not permitted |
| Direct Gemini calls | not permitted |

---

## 7. Operational note — upstream rate limiting

The Luna experiment hit upstream `429 rate_limit_exceeded`.

**The workarounds are benchmark-only.** Verified by diffing the full
experiment commit range against the runtime packages:

- `benchmarks/selector_v2/model_experiment.PacedBackend` — inter-call sleep
  pacing;
- `benchmarks/selector_v2/model_experiment.FailFastBackend` — stop after N
  consecutive operational errors;
- `benchmarks/selector_v2/cli --fail-fast-after` / `--retry-errors`.

The only shared-runtime retry surface is `services.llm_provider`'s SDK
`max_retries` / `timeout` passthrough from capability config, which the
experiment did **not** touch.

`git diff 3b3d34d~1..22a63e8 -- backend/services backend/core backend/routers
backend/admin` contains exactly one change: `"none"` added to the OpenRouter
entry of `REASONING_TRANSPORT` (a transport capability declaration).

**Internal pilot request semantics were not changed.** The retry policy for
the pilot runtime was deliberately *not* redesigned in this task.

---

## 8. Pilot observability contract

**Verified against `services/decision_trace.py` at this commit, not assumed.**

### Emitted today

| Group | Fields |
| --- | --- |
| Correlation | `request_id` |
| Intent | `execution_mode` (`SINGLE`/`MULTI`), analyzer success/failure, `context_used`, `calendar_relevant` |
| Retrieval | `candidate_count` |
| Selector | selected candidate ref or `NONE`, `selected_candidate_source`, provider, `requested_model`, `actual_model`, success/failure |
| Degraded | degraded run list + request summary, `degraded_reason` |
| Latency | `total_latency_ms`, intent `latency_ms`, selector `latency_ms` |
| Errors | `model_error`, `timeout`, `pipeline_error`, `ai_config_error`, circuit-breaker entries |

### ⚠️ Gaps — required by §13 but NOT currently emitted

| Gap | Detail |
| --- | --- |
| Session correlation | The trace carries `request_id` but **no session/conversation id**. `conversation_id` lives only in `routers/chat.py`, so request→session correlation is not possible from telemetry alone. |
| Wall-clock timestamp | Only elapsed time (`started_at`, `total_latency_ms`) is recorded; there is no wall-clock timestamp field beyond the log record's own. |
| Retrieval latency | No `retrieval_ms`. Intent, selector and total latency exist, so retrieval time is only inferable by subtraction. |
| Source availability | Only proxies exist (`meili_fallback_used`, `qdrant_fallback_used`, `selected_source`); explicit per-source availability is not emitted. |

**Status: `MUST_CLOSE_BEFORE_PILOT`.** These are recorded as gaps rather than
listed as contract, because §13 asked for verification, not declaration.
Stage H of the acceptance plan cannot pass until they are closed.

**Raw sensitive user content is not a telemetry requirement.** The existing
PII-free, request-scoped decision trace is preserved; this freeze adds no new
mandatory raw-content field, and closing the gaps above must not introduce
one.

---

## 9. Pilot conversation data

These two are deliberately distinct:

- **Operational telemetry** — automatic system metrics and the PII-free
  decision trace.
- **Conversation history** — the existing application's permitted
  chat-history behaviour.

This freeze introduces **no new data-collection system**; it documents
existing capability and the requirement.

**No validation artifact may be produced from pilot conversation data**
without all of: PII anonymization/removal, deduplication, sampling, and
**independent human adjudication**.

---

## 10. Backlog and deferred phases

| Phase | Name | Status |
| --- | --- | --- |
| 7C | Metadata Necessity | `BACKLOG_POST_INTERNAL_PILOT` |
| 7D | Exact Alias Experiment | `BACKLOG_POST_INTERNAL_PILOT` |
| 7E | Candidate Budget | `BACKLOG_POST_INTERNAL_PILOT` |
| 7F | Bot Context Necessity | `BACKLOG_POST_INTERNAL_PILOT` |
| **7G** | **PUBLIC_PRODUCTION_FINAL_FREEZE** | **`DEFERRED_UNTIL_INTERNAL_PILOT_DATA`** |

None of 7C–7F blocks the internal pilot. No code or behaviour was changed for
any of them. Phase 7G does not run before the internal pilot.

---

## 11. Validation provenance

| Field | Value |
| --- | --- |
| Semantic Gold method | blind model adjudication |
| Adjudicator | GPT-5.6 Sol |
| Independent human Gold | **No** |
| Variant A HOLDOUT historical gate | **FAIL** (471/472 hard gate) |
| Final accuracy claim | **None made** |

---

## 12. Post-pilot validation plan

1. Collect internal pilot operational telemetry and conversation history.
2. Anonymize / remove PII; deduplicate; sample.
3. Obtain **independent human adjudication** — not model adjudication — for
   the sampled set.
4. Build a new production-like validation set from that human-adjudicated
   data.
5. Re-measure the selector against it, including `NONE` recall
   (`IP-KI-2`) and the generic-vs-specific qualifier class (`IP-KI-1`).
6. Only then run **Phase 7G — `PUBLIC_PRODUCTION_FINAL_FREEZE`**.

---

## 13. Freeze fingerprint

```
INTERNAL_PILOT_FREEZE_FINGERPRINT = c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6
```

Deterministic SHA256 over the canonical JSON of the manifest
(`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`), matching
the `services.llm_config` convention.

**Excluded from the hashed payload:** `created_at` and `freeze_fingerprint`
itself. Everything else in the manifest is covered, so regenerating on a
different day yields the same fingerprint while any change to a frozen value
changes it. Both properties are asserted by
`backend/tests/test_internal_pilot_freeze.py`.

Regenerate / verify:

```bash
cd backend
python -m scripts.internal_pilot_preflight manifest --write
python -m scripts.internal_pilot_preflight verify
```

The manifest contains **no secrets**: only component identity, parameters and
fingerprints. A test asserts this against the actually emitted manifest.

---

## 14. Related artifacts

| Artifact | Path |
| --- | --- |
| Freeze manifest | `deploy/internal-pilot/answer-pipeline-freeze.json` |
| Freeze module + preflight | `backend/services/internal_pilot_freeze.py` |
| Preflight CLI | `backend/scripts/internal_pilot_preflight.py` (`runtime` subcommand) |
| Runtime preflight | `backend/services/internal_pilot_runtime.py` |
| Selector prompt catalog | `backend/services/selector_prompt_catalog.py` |
| Pilot configuration | `deploy/internal-pilot/pilot.env.example` |
| Tests | `backend/tests/test_internal_pilot_freeze.py` |
| Acceptance plan | `docs/answer-pipeline-v2/INTERNAL_PILOT_ACCEPTANCE_PLAN.md` |
| E2E scenario catalog | `tests/e2e/internal_pilot/scenarios.json` |
