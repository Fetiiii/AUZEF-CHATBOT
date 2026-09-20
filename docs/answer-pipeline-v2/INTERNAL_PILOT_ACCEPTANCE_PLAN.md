# Internal Pilot — Acceptance Test Plan

**Milestone:** `INTERNAL_PILOT`
**Status:** `READY_TO_EXECUTE` — **not executed by the freeze task**
**Freeze:** `docs/answer-pipeline-v2/INTERNAL_PILOT_FREEZE.md`
**Freeze fingerprint:** `c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6`

This is a **test plan**. Executing it is a separate, explicitly approved
activity: stages C, E, F and G require a running Docker stack and (for E)
live OpenRouter calls.

> ### Execute stage A first — it currently FAILs
>
> The freeze preflight reports two open blockers against the current runtime:
> the selector prompt is still `production` (not `variant_a_v1`), and
> `LLM_ENABLED` seeds to `false`. **Both must be resolved before stages D–G
> are meaningful.** See §3 of the freeze document.

**Never write secret values into any acceptance report.** Record only
*presence*/*absence* and the variable name.

---

## Stage A — Static / config preflight

No services required.

**Run stage A from a host checkout, not inside the backend container.** The
image carries only the `backend/` subtree (`WORKDIR /app`, `COPY . .`), so
the repo-root-relative reads in preflight would resolve above `/app`.

| # | Check | How | Expected |
| --- | --- | --- | --- |
| A1 | Freeze preflight | `cd backend && python -m scripts.internal_pilot_preflight preflight` | exit 0, `RESULT: PASS` |
| A2 | Manifest fingerprint intact | `cd backend && python -m scripts.internal_pilot_preflight verify` | fingerprint matches |
| A3 | Env file present | `deploy/production/config/backend.env` derived from `backend.env.example` | present |
| A4 | Secrets presence | `OPENROUTER_API_KEY`, DB credentials, `MEILI_MASTER_KEY`, admin secrets | **presence only** — never print values |
| A5 | Provider policy | `LLM_PROVIDER=openrouter` | openrouter; no direct OpenAI/Gemini key in use |
| A6 | Selector assignment | preflight A1 checks provider/model/reasoning/temperature/max_tokens/config fingerprint | matches freeze |
| A7 | Intent Analyzer assignment | registry active config version vs manifest `intent_analyzer.config_fingerprint` | `2f1b27bc3430…` or an explicitly approved newer version |
| A8 | LLM enabled — **live value** | `SystemConfig.LLM_ENABLED` row in the DB | `true` |
| A8b | LLM enabled — **seeded default** | preflight A1 `llm_enabled_seeded_default`, which AST-reads `DEFAULT_SYSTEM_CONFIG` in `scripts/init_system.py` | `true` |
| A9 | Service definitions | `docker-compose.yml` services `db`, `meilisearch`, `qdrant`, `backend`, `frontend` | all defined |
| A10 | DB connectivity config | `DATABASE_URL` / admin + chat DB URLs resolve | reachable host/port |
| A11 | Meili config | host, index name, master key present | configured |
| A12 | Qdrant config | host, collection name | configured |
| A13 | Calendar settings | active academic term / calendar source configured | configured |
| A14 | Candidate order | preflight A1 `candidate_order_expected` | `production` |

**Gate:** A1 must exit 0. Any FAIL stops the pilot.

---

## Stage B — Full automated suite

Commands detected in this repo:

| Target | Command | Notes |
| --- | --- | --- |
| Backend | `cd backend && PYTHONUTF8=1 python -m pytest tests/ -v` | Requires Docker (spins up a throwaway Postgres) **or** `TEST_DATABASE_URL` |
| Backend (freeze only) | `cd backend && python -m pytest tests/test_internal_pilot_freeze.py -q` | No infrastructure needed |
| Frontend / widget | `cd chatbot-web && npm test` (`ng test`) | Angular |
| Deployment lifecycle | `cd backend && python -m pytest ../deploy/production/scripts/tests -q` | Admin/ops scripts |
| Release build | `cd backend && python -m pytest ../deploy/production/build/tests -q` | Build lock |

Admin functionality is covered inside the backend suite
(`test_admin_login_lockout.py`, `test_model_registry.py`, `test_settings.py`,
`test_roles.py`, `test_audit.py`).

**Reference baseline (this freeze commit): 553 passed before the freeze, 598
after it — the delta is exactly the new freeze tests.** Report the
delta against that number, not an absolute pass count.

**Gate:** no new failures versus the recorded baseline.

---

## Stage C — Docker stack acceptance

Single-server Docker environment.

| # | Service | Health check | Expected |
| --- | --- | --- | --- |
| C1 | `db` (PostgreSQL) | `pg_isready`; admin + chat DBs exist | healthy |
| C2 | `meilisearch` | `GET /health` | `available` |
| C3 | `qdrant` | `GET /healthz`; collection present | healthy |
| C4 | `backend` | `GET /health` | 200, dependencies reported healthy |
| C5 | `frontend` / widget | widget asset served | 200 |
| C6 | Required workers | embedding model loaded; any init service completed | ready |
| C7 | Restart resilience | `docker compose restart backend` | recovers without manual action |
| C8 | Image/project isolation | `COMPOSE_PROJECT_NAME` set distinctly | does not collide with `auzef-chat-analiz`, which shares the `auzef-backend:latest` tag |

**Gate:** C1–C6 healthy.

---

## Stage D — Offline Answer Pipeline regression

Uses the existing frozen benchmark/test fixtures. **No live calls.**

**Purpose: regression detection only. This stage produces NO final accuracy
claim** — the Gold set is model-adjudicated (`IP-KI-3`), not human Gold.

| # | Check | Expected |
| --- | --- | --- |
| D1 | `benchmarks/selector_v2/prompts/manifest.json` vs live prompts | identical |
| D2 | Selector contract fingerprint | `d50fbee416ca…` |
| D3 | Serializer probe payload | unchanged |
| D4 | Stored fixture replays | decisions unchanged versus recorded run |
| D5 | Candidate eligibility ordering on fixtures | unchanged |

**Gate:** no unexplained change in stored-fixture decisions.

---

## Stage E — Live E2E acceptance

**Requires live OpenRouter.** Run the catalog at
`tests/e2e/internal_pilot/scenarios.json` (48 scenarios).

Evaluate against each scenario's `semantic_target` and observable behaviour.
**Never compare exact answer wording.**

| Category | Scenarios | Focus |
| --- | --- | --- |
| `single_intent` | 4 | baseline correctness |
| `short_noisy_language` | 4 | typos, lowercase, fragments |
| `near_qna` | 3 | sibling-topic discrimination |
| `general_intent` | 3 | no invented qualifier |
| `explicit_specific_intent` | 3 | stated qualifier honoured |
| `generic_vs_specific_qualifier` | 4 | **`IP-KI-1`** — known open issue |
| `explicit_qualifier` | 3 | stated qualifier not contradicted |
| `valid_none` | 4 | **`IP-KI-2`** — `NONE` must be a real answer |
| `calendar_relevant` | 4 | date questions reach calendar |
| `calendar_irrelevant` | 3 | event named but no date asked |
| `multi_intent` | 3 | max 2 intents |
| `conversation_context` | 3 | follow-up resolution |

Record per scenario: intent mode, `context_used`, `calendar_relevant`,
candidate count, selector decision, degraded yes/no, latencies, errors.

**Gate:** no *new* failure class beyond the documented `IP-KI-1` /
`IP-KI-2`. Because those are known-open, the pilot is not blocked by
individual failures inside them — but they must be counted and reported.

---

## Stage F — Failure-path acceptance

Scenarios `IP-E2E-042` … `IP-E2E-048`.

| # | Injected failure | Expected behaviour |
| --- | --- | --- |
| F1 | OpenRouter unavailable | Degraded mode (Calendar → Meili ≥ 0.90 → Qdrant > 0.75); curated response; **never** a raw error or hallucinated answer; `degraded_reason` recorded |
| F2 | OpenRouter timeout | Classified `TIMEOUT` (not `INVALID_OUTPUT`); degraded mode serves the request |
| F3 | Admin LLM OFF | `ADMIN_DEGRADED`; **no LLM call made**; deterministic path answers |
| F4 | Circuit breaker open | Selector **not called**; `degraded_reason = selector_circuit_open`; closes after 60 s cooldown |
| F5 | Meili unavailable | Request still completes on the remaining source; outage visible in `source_availability`; no stack trace to the user |
| F6 | Qdrant unavailable | Request still completes; reduced candidate count visible in telemetry |
| F7 | Invalid selector output | `INVALID_OUTPUT`, **never** semantic `NONE`; breaker treats it as *neutral* |

**Gate:** every path degrades gracefully; no user-visible stack trace; no
fabricated answer.

---

## Stage G — UI acceptance

| # | Check | Expected |
| --- | --- | --- |
| G1 | Widget message flow | send → response rendered |
| G2 | Loading state | visible during the request, cleared afterwards |
| G3 | Answer rendering | formatting/links render correctly |
| G4 | Error state | provider failure shows a friendly message, not a trace |
| G5 | Session continuation | follow-up turn keeps context (`IP-E2E-039`…`041`) |
| G6 | Refresh | session behaves per the application's documented history policy |
| G7 | Mobile viewport | usable at ~375 px width |

---

## Stage H — Observability acceptance

> **Four contract fields are not emitted today** — see §8 of the freeze
> document. They are recorded as gaps with status `MUST_CLOSE_BEFORE_PILOT`,
> so H1, H3 and H6 cannot pass as written until they are closed. Close them
> first, or run stage H knowing it is partial and record which.

For **every** request issued in stages E–G, confirm:

| # | Check | Expected | Today |
| --- | --- | --- | --- |
| H1 | Correlation | `request_id` + session/conversation id + wall-clock timestamp | ⚠️ `request_id` only — session id and timestamp are **gaps** |
| H2 | Intent | `execution_mode`, analyzer success/failure, `context_used`, `calendar_relevant` | ✅ emitted |
| H3 | Retrieval | `candidate_count` + per-source availability | ⚠️ count only; availability is a **gap** (proxies exist) |
| H4 | Selector | decision (ref or `NONE`), provider, requested/actual model, success/failure | ✅ emitted |
| H5 | Degraded | yes/no + reason | ✅ emitted |
| H6 | Latency | total, intent, retrieval, selector | ⚠️ retrieval latency is a **gap** |
| H7 | Errors | provider / parser / timeout / circuit breaker distinguishable | ✅ emitted |
| H8 | Privacy | **no raw user content is a mandatory telemetry field** | ✅ preserved — closing the gaps must not change this |

**Gate:** every stage-E scenario reconstructable from telemetry alone. This
gate is currently **unreachable** without closing the H1/H3/H6 gaps.

---

## Exit criteria

| Stage | Gate |
| --- | --- |
| A | preflight exit 0 |
| B | no new failures vs. the recorded baseline |
| C | C1–C6 healthy |
| D | no unexplained fixture-decision change |
| E | no new failure class beyond `IP-KI-1` / `IP-KI-2` |
| F | all failure paths degrade gracefully |
| G | G1–G5 pass |
| H | full telemetry reconstruction — **blocked on the four observability gaps** |

Passing this plan authorises the **internal pilot only**. It does not
authorise public production, and it produces no final accuracy claim —
Phase 7G remains `DEFERRED_UNTIL_INTERNAL_PILOT_DATA`.
