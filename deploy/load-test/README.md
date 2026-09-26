# Controlled AUZEF chatbot load measurement

This directory prepares a **single-server Compose test environment** for a
realistic, stateful `/widget-chat` experiment. It does not tune workers, pools,
timeouts, retrieval, prompts, rate limits or provider policy. Native two-node
production is a separate validation step. The canonical entry point is
`auzef-load-plan`; it runs the existing preflight, generator and watcher.

## Go / No-Go before any traffic

`auzef-load-plan` sends **no requests** if preflight fails. A full run requires:

- An operator-selected expected git commit **and backend image ID**. The image
  must be tied to that source ref in the release record; merely reading the
  current image ID does not prove what source built it. Dirty tree is WARN and
  recorded. Do not treat a dirty tree or unknown image provenance as a capacity
  baseline.
- Running `backend`, `db`, `meilisearch`, `qdrant`, `frontend` services; the
  configured and actual Uvicorn worker count; resource limits exactly matching
  the approved [resource contract](resource-contract.env); no OOMKilled state.
- `/health/live` HTTP 200 `ok`, `/health/ready` HTTP 200 `ready`, each DB/search
  dependency `ok`, DB `SELECT 1`, the configured model's readable HuggingFace
  cache snapshot, cgroup
  counters, required host tools and a writable output directory.
- The generated test-only Compose overlay enabled: backend content-free phase
  metrics, Nginx minimal timing log and **only the resource limits from the
  reviewed contract**. It does not change workers, pools, thread settings,
  rate limits or provider policy. Preflight checks the running containers.

The tracked contract expects 8 GiB memory, 12 GiB memory+swap, 6 CPUs and two
workers. Those numbers belong to the controlled test plan, not a production
tuning recommendation. The repo's Compose file may specify different values;
the running limits are decisive. Supply an approved alternative contract with
`AUZEF_LOAD_CONTRACT_FILE=/absolute/path/contract.env`. Never override the
contract solely to make a low-resource host pass; that would answer a different
capacity question. The scripts never call `docker update` or apply limits
themselves. The render script produces an overlay from the same contract;
an operator chooses when to apply it.

## Exact operator procedure

Run on the designated test host from the repository root. The test environment
must have the **current source built into the backend/frontend images**. To
enable measurement on a controlled Compose test host, an operator may recreate
those two services with the test overlay, after reviewing its changes:

```bash
export AUZEF_COMPOSE_EXTRA_FILE="$(./deploy/load-test/auzef-load-render-overlay)"
docker compose -f docker-compose.yml -f "$AUZEF_COMPOSE_EXTRA_FILE" up -d --build backend frontend
```

This command is preparation for the test host, **not** part of a production
deployment. Verify the image→commit mapping in the build/release record. Then
set the expected identities and the reviewed HTTPS front-door URL:

```bash
export AUZEF_EXPECT_GIT_COMMIT="$(git rev-parse HEAD)"
export AUZEF_EXPECT_BACKEND_IMAGE_ID="$(docker inspect --format '{{.Image}}' auzef_backend)"
export AUZEF_LOAD_URL='https://TEST-HOST/widget-chat'
AUZEF_REQUIRE_TELEMETRY=1 ./deploy/load-test/auzef-load-preflight
```

Only after `RESULT: PASS`, run the staged plan. This repeats preflight itself;
the earlier command is for human review. `--confirm-load-test` is mandatory.

```bash
./deploy/load-test/auzef-load-plan \
  --url "$AUZEF_LOAD_URL" \
  --stages 1,5,10,15,20 \
  --confirm-load-test
```

`--contract /absolute/path/contract.env`, `--workload`, `--output-root`,
`--sample-interval`, `--max-stage-seconds` and `--insecure` are optional. TLS
verification is on by default; `--insecure` is only for an explicitly approved
test certificate. The generator rejects backend-port targets, credentials,
query parameters and redirects. Do not use a production third-party provider
for a fault scenario without an isolated provider/stub arrangement.
For a custom contract, set `AUZEF_LOAD_CONTRACT_FILE` **before rendering** and
pass the same file with `--contract` to the plan.

The plan starts the existing watcher and a process/DB sampler, runs each stage
as warm-up → at least five seconds pause → steady-state → at least five seconds
cooldown, and stops escalation
on STOP, WARN, 429, 5xx, timeout, invalid response or an incomplete measurement.
It never automatically changes limits or retries an HTTP request. For a small
**harness-only** smoke after preflight PASS, the existing generator can run one
round without being used as capacity evidence:

```bash
./deploy/load-test/auzef-load-run --url "$AUZEF_LOAD_URL" \
  --concurrency 1 --workload ./deploy/load-test/workload.json \
  --rounds 1 --phase warmup --stage 1 --confirm-load-test
```

The older stateless `--questions`/`--requests` mode remains available for its
original controlled probe and local fake-server tests. It does not represent
context follow-ups or a full realistic workload.

## Workload and stage design

`workload.json` uses reviewed, PII-free pilot prompts: SINGLE SELECT,
expected NONE, Calendar, MULTI, a true two-turn context follow-up, and a
suggestion/no-match probe. Each virtual user serializes its own requests. A
new conversation starts per scenario; only that user's follow-up reuses the
returned conversation ID and token. The token is held in memory, never written
to results. Scenarios rotate deterministically by user ID and round.

The default stages are **1, 5, 10, 15, 20** users. Warm-up covers every
scenario at least once across users. Steady-state rounds target at least 100
requests per stage; failed prerequisite turns can reduce the completed count
and fail the stage. Warm-up is labeled separately and excluded from steady
statistics. `p95` is marked insufficient below 100 successful requests, `p99`
below 1,000. A computed percentile with too few samples is descriptive only.
The stage summary includes exact count, status distribution, throughput,
min/mean/p50/p95/p99/max and timeout/error counts. Request start skew can be
influenced by host scheduling; this is virtual-user concurrency, not fixed RPS.

Expected NONE, actual degraded mode and provider failures **cannot be inferred
reliably from HTTP text**. The client writes null until a DecisionTrace join
supplies them. Suggestion presence can be observed in the response JSON.

## Measurements and file origins

Each run has an isolated `outputs/performance-readiness/<run-id>/` directory
with mode 0700; generated files use mode 0600. `/outputs/` is gitignored.
No raw question, answer, conversation ID/token, client IP, URL query, prompt,
API key or SQL text is written by these tools.

| File | Source and meaning |
| --- | --- |
| `preflight.json` | Parsed PASS/WARN/FAIL checks; saved even on NO-GO |
| `environment.json` | Host git/ref/dirty, Docker image/limits/start time, Python/package versions, worker count, Meili/Qdrant versions where readable, nonsecret active LLM timeout/retry config |
| `client-results.jsonl` | Synthetic request/user ID, scenario, phase/stage, UTC start/end, latency, status, contract/suggestion and joined NONE/degraded/failure flags; no content/token |
| `stage-summary.json` | Warm-up and steady-stage counts, latency, RPS, percentile sufficiency, measurement coverage and final gate |
| `resource-samples.jsonl` | Per-PID CPU cumulative/% one core, RSS, thread/FD/socket/uptime; cgroup memory/current/peak/events and CPU throttling; host CPU/load/memory/swap if `/proc` accessible |
| `db-samples.jsonl` | Read-only `pg_stat_activity` metadata: state, wait event, transaction/query age and connection count; no query text |
| `server-metrics.jsonl`, `server-phase-summary.json` | Opt-in backend arrival/handler/embedding/Meili/Qdrant/LLM SDK-call/DB-write timers, active counts, AnyIO token snapshots and DB pool occupancy; aggregate timings by stage |
| `trace-summary.jsonl`, `provider-summary.json` | Sanitized DecisionTrace phase/outcome/circuit fields joined by synthetic ID; logical calls and timeouts; SDK internal attempts/retries explicitly unknown when not exposed |
| `nginx-metrics.jsonl`, `nginx-summary.json` | Test-only Nginx status, request/upstream time and upstream node joined by synthetic ID; no client IP or URI |
| `watcher.txt` | Existing watcher OK/WARN/STOP dashboard and reasons |
| `final-summary.md` | Final gate and pointers; not a substitute for stage-level evidence |
| `stages/*` | Existing generator's per-phase summary and client JSONL |

The backend metrics flag `AUZEF_LOAD_METRICS=1` is test-only. With the flag
off, middleware is not installed; provider timers and chat hooks emit nothing.
No prompt/query text enters metric events. The `handler_queue` signal is the
time from ASGI arrival to sync handler start; it includes body parsing and
dependency work, so it is an **upper bound**, not pure AnyIO queue wait.
`db_pool` is instantaneous process-local occupancy; checkout wait is not
measured. Qdrant embedding and Qdrant network calls have separate timers.
DecisionTrace keeps its existing semantics and is reduced to safe fields in
the run artifact. SDK internal retry/attempt count remains not observable.

## STOP / WARN gate

The existing [watcher](auzef-load-watch) is authoritative during the run.
STOP: any widget 5xx/503, QueuePool/connection-timeout log, cgroup OOM or
OOM-kill, live≠200, idle-in-transaction >5 seconds, or backend container
replacement. WARN: `memory.events max` growth, ready not `ready`, idle >1
second, query-log failure, backend memory ≥85% of limit, unreadable probe.
The orchestrator also stops stage escalation on client 429/5xx/timeout,
watcher WARN, missed stage deadline or insufficient server metrics. It does
not interpret a 429 as DB pool failure. Cooldown and safe snapshot follow-up
remain operator review points; `auzef-load-snapshot` can be run after a STOP.

## LB/client-IP and multi-node validation

The Compose test has no physical LB. It **cannot** validate trusted LB CIDR,
`$remote_addr`, backend `request.client.host` or node-local rate-limit keys.
On the actual LB test topology, use two controlled clients with known distinct
source IPs, send one request each with unique synthetic request IDs, and have
an authorized operator compare the LB forwarding record, Nginx `$remote_addr`
and `X-Forwarded-For`, backend observed client host, chosen APP node and
429 counters. Store only equality/difference and node counts in the run
record; keep raw IP diagnostics in restricted short-lived operator logs.
Do not invent a trusted CIDR. Repeat with both APP-01/02 and inspect shared
PostgreSQL/provider/search pressure; Compose results do not certify two nodes.

## Separate fault-test plan

Fault work is **not** part of the normal stages. In an isolated test deployment
with stubbed/test providers and explicit rollback, run one dependency fault
at a time: slow LLM, LLM 429, LLM timeout, Meili unavailable/slow, Qdrant
unavailable/slow, DB wait, Solution Center timeout. Capture proxy status,
handler/in-flight/thread behavior, trace outcomes, circuit state, readiness
and resource recovery after removal. Never inject failure into the real
third-party provider or shared production services for this test. Fault
fixtures `IP-E2E-042`–`048` describe expected degraded paths but are not
silently mixed into normal load.

## Offline verification

```bash
bash deploy/load-test/tests/smoke.sh
python -m unittest discover -s deploy/load-test/tests -p 'test_*.py' -q
```

These tests use fake Docker and a local fake widget server. They test the
preflight STOP gate, watcher/snapshot behavior, state isolation and output
privacy. They are **not** capacity results.

## Intent Analyzer diagnostics and controlled Analyzer load

Two container-side tools exercise the production Intent Analyzer path without
changing configuration, registry, prompt or parser. Both run through
`docker exec -i auzef_backend python - '<json>' < <tool>` and print only
numeric/categorical fields (plus, for the diagnosis tool, the raw model output
for offline scoring under `outputs/`).

- `analyzer-diagnose.py` — per-case diagnosis with optional overrides
  (`model_override`, `reasoning_effort_override`, `max_tokens_override`,
  `system_append`). Records logical latency, final attempt latency,
  `retry_count`, `failure_category`, parse/schema/invariant results.
- `analyzer-load.py` — controlled logical-call load for one model:
  `{"model": "openai/gpt-6-luna", "reasoning_effort": "none", "calls": 60,
  "concurrency": 2, "interval_seconds": 1.0, "cases": [{"current": "...",
  "previous": ["..."]}]}`. Each logical call goes through a fresh
  `CircuitBreaker` (production `BreakerConfig` from env) →
  `analyze_intents_with_result` → `_availability_kind` → `record`. The summary
  reports logical calls, body-level physical attempts, RATE_LIMIT, retries,
  retry-exhausted, circuit open/skip counts, success and degraded rates, and
  logical vs final-attempt latency.

The OpenAI-compatible adapter owns the retry loop (SDK-internal retries are
disabled) under one logical deadline per capability (`LOGICAL_DEADLINE_SECONDS`
in `services/llm_config.py`); HTTP-level and body-level errors (HTTP 200 +
`{"error": {"code": 429}}`) use the SDK's retry decisions and backoff, and each
retry emits an `llm_retry` load-metric event. `retry_count` therefore counts
every adapter retry. This lives in the backend image: the container only runs
it after the test image is rebuilt; `analyzer-load.py` reports
`adapter_body_retry_support` so a run against an old image is recognisable.

`fault-proxy.py` is a TEST-ONLY loopback proxy in front of OpenRouter that
rewrites a seeded fraction of successful answers into body-level 429s and can
hold requests to exercise timeouts. The backend reaches it only through the
gated hook `AUZEF_TEST_OPENROUTER_BASE_URL` (+ `AUZEF_LOAD_METRICS=1`).
