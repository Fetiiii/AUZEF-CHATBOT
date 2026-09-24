# AUZEF load-test readiness

**Date:** 24 September 2026\
**Branch:** `production-readiness`\
**Status:** Harness prepared and offline-validated. A controlled local Compose
run completed all five stages after the test-only resource/telemetry overlay
was applied. See the [run analysis](../outputs/performance-readiness/20260924-074237-94obi0ks/analysis.md).
This single-node, dirty-working-tree run does not establish production or
two-node 15-user capacity.

## Implemented test infrastructure

The existing `deploy/load-test/` preflight, generator and watcher were
extended. `auzef-load-plan` is the orchestrator: preflight → environment
snapshot → watcher/resource sampler → stateful warm-up and steady stages →
cooldown → sanitized aggregation. It stops escalation on WARN/STOP or client
failures. No second answer path or production optimization was introduced.

The tracked `resource-contract.env` states the **expected** test limits; an
operator may supply a reviewed alternative with `--contract`. The script
never changes container limits. The render script creates a test-only Compose
overlay from that same contract: approved resource limits, content-free
backend metrics and a minimal Nginx timing log. It does not change workers,
pools, thread settings, rate limits or provider policy. Rendering does not
apply the overlay.

Application/frontend code changes made solely for measurement:

| File | Why needed | Runtime semantics changed? | Activation |
| --- | --- | --- | --- |
| `backend/services/load_metrics.py`, `backend/main.py` | Correlate ASGI arrival, handler start/finish, in-flight count and pool snapshots with a synthetic request ID | No answer/routing change; measurement/log overhead when enabled | Middleware installed only with `AUZEF_LOAD_METRICS=1` |
| `backend/routers/chat.py` | Mark sync-handler start, trace link and DB write timings | No response or persistence contract change | Hooks emit only with the flag; timers are inert otherwise |
| `backend/services/providers.py` | Split embedding, Meili and Qdrant query duration/in-flight | No retrieval order, count or result change | Timers emit only with the flag |
| `backend/services/llm_provider.py` | Measure SDK call duration and in-flight count | No model, prompt, timeout, retry or call-count change | Timer emits only with the flag |
| `chatbot-web/nginx.conf` | Allow a test-only minimal access log include | No rate/proxy/location behavior change | Empty wildcard in normal deployment; generated overlay supplies the file only on test host |

## Measurement coverage

| Question | Measurement |
| --- | --- |
| When does end-to-end latency bend? | Client stage/phase status distribution, RPS, min/mean/p50/p95/p99/max; warm-up separate |
| Did the request wait before handler work? | ASGI arrival → sync handler start upper bound; active chat count, AnyIO borrowed/total token snapshots at arrival and response start, and arrival → response start |
| Is embedding CPU-bound? | Encode timer/count/in-flight, per-worker CPU/RSS/thread, cgroup CPU throttling and host CPU |
| Meili or Qdrant? | Separate Meili, embedding and Qdrant query timers/error classes; existing retrieval trace |
| DB pressure? | `pg_stat_activity` state/wait/xact/query age, connection count; process-local pool size/checked-out/overflow snapshots |
| LLM/circuit? | Opt-in SDK-call timer/in-flight count plus existing DecisionTrace Analyzer/Selector logical calls, latency, outcomes, available retry field and circuit events; joined by synthetic request ID |
| Nginx or upstream? | Test-only request/upstream timing, status 429/502/503/504, upstream address/node; client status |
| Long-term resources? | Per-PID FD/socket/thread/RSS/uptime, cgroup memory/current/peak/events, host available RAM/swap |

All application events are conditional on `AUZEF_LOAD_METRICS=1` and carry
synthetic request IDs, process ID, duration/count/status only. The client
keeps conversation tokens only in memory. Neither question nor response text,
IP, raw SQL, prompt or secret is persisted by the new tools.

## Remaining blind spots

- The arrival→handler interval also includes body parsing and framework work;
  it is not an exact AnyIO token wait metric. AnyIO borrowed/total tokens are
  sampled at two request boundaries, not continuously, so short peaks may be
  missed. Per-worker thread count and active handler count are also recorded.
- SQLAlchemy checkout **wait duration** and exact per-query duration are not
  instrumented. Pool occupancy is sampled at handler boundaries; DB-side
  transaction/query ages are separate, read-only observations.
- The SDK does not expose reliable actual HTTP attempt/retry counts. The
  report records logical Analyzer/Selector calls and `not observable` for
  internal attempts. Provider HTTP 429 cannot always be distinguished from a
  generic SDK error in the existing trace.
- The normal profile has an expected NONE and a suggestion probe. Actual
  NONE/degraded/provider failure is counted only after a DecisionTrace join;
  missing traces produce null, not fabricated zero. Suggestion results may
  vary with QnA/search data. Degraded/fault behavior requires a separate
  isolated fault run.
- Local Compose has one APP node and no physical LB. Real-client-IP and
  cross-node shared-resource behavior require actual LB/APP-01/APP-02 tests.
- Environment snapshot captures image ID and git ref, but cannot prove the
  image was built from that ref without an external build/release record.
  Qdrant/Meili server version may be null if the version API is inaccessible.

## Environment requirements

Use a designated Compose test host with Docker/Compose, Bash, Python 3,
`git`, `awk`, `date`, a writable output directory and the frontend HTTPS
front door. The backend image must include this instrumentation revision.
The default contract requires two running Uvicorn workers, 6 CPUs, 8 GiB
memory and 12 GiB memory+swap. These values are **test requirements**, not
proposed production settings. Before the test overlay, the local backend had
4 GiB memory and 8 GiB memory+swap, and preflight failed as intended. The
overlay recreated the backend with 8/12 GiB limits and opt-in telemetry.
The working tree was dirty; this was recorded as WARN.

`deploy/load-test/auzef-load-render-overlay` writes the generated file under
ignored `outputs/performance-readiness/`. The Nginx include exists only when
that overlay mounts `nginx-metrics.conf`; with no include file the frontend
config's logging behavior remains unchanged. For the local measurement the
test services were rebuilt and recreated; this did not change tracked
production Compose limits or application behavior.

## Preflight contract

Preflight checks explicit expected git commit and image ID, service running
state, configured and actual worker count, CPU/memory/swap, OOMKilled,
HuggingFace cache, live/ready and each DB/search dependency, DB connectivity,
cgroup counters, output writeability and required tools. A full orchestrated
run additionally requires the backend metrics flag and Nginx timing log
active in the running containers. **FAIL is a hard no-load gate.** WARN is
visible and recorded; the operator must resolve unexplained WARN before using
the result as capacity evidence.

The initial local no-load check returned FAIL for memory 4 vs 8 GiB,
memory+swap 8 vs 12 GiB and inactive backend/Nginx metrics. After applying the
test overlay, preflight PASS confirmed the limits and telemetry. The earlier
NO-GO artifact and later staged run are separate ignored output directories.

## Workload composition

`deploy/load-test/workload.json` draws from PII-free internal-pilot fixture
IDs `IP-E2E-001`, `025`, `029`, `036`, `039`, plus one synthetic suggestion
probe. It covers SINGLE SELECT, expected NONE, Calendar, MULTI, a two-turn
context follow-up and a possible suggestion/no-match path. Each virtual user
serializes its requests and uses its own conversation ID/token for the
follow-up. Fresh conversation per scenario prevents cross-scenario context
contamination. The rotation by user ID and round is deterministic; generated
request IDs are unique for correlation.

Fault fixtures `IP-E2E-042`–`048` are explicitly outside normal load. They
require a stubbed isolated provider/dependency environment and are not sent
to real third-party services by this harness.

## Stage design

Default concurrent virtual users: **1, 5, 10, 15, 20**. Each stage warms
every scenario at least once, pauses at least 5 seconds to avoid carrying a
single Nginx burst into steady state, runs a separate steady phase targeting
at least 100 requests, then cools down for at least 5 seconds. Warm-up does not
enter steady percentiles. A stage can complete fewer requests if a failed
first turn prevents a context follow-up; that is a failure signal, not a
silent replacement. p95 below 100 and p99 below 1,000 successful samples
are marked insufficient. Tiny samples must not become capacity claims.

## Collected metrics and output

The full file table and provenance are in
[`deploy/load-test/README.md`](../deploy/load-test/README.md). A run directory
contains `environment.json`, `preflight.json`, `client-results.jsonl`,
`stage-summary.json`, `resource-samples.jsonl`, `db-samples.jsonl`,
`server-metrics.jsonl`, `server-phase-summary.json`, `trace-summary.jsonl`,
`provider-summary.json`, `nginx-metrics.jsonl`, `nginx-summary.json`,
`watcher.txt` and `final-summary.md` when load actually runs. A NO-GO has
only preflight, environment, stage and final summaries. Large outputs remain
under ignored `outputs/` and should not be committed.

## Known limitations

The local run measured 15-user behavior and showed increased embedding encode
time and cgroup CPU throttling; see its analysis for the limits of that result.
`auzef-load-plan` samples host `/proc`, which represents the host where the
orchestrator runs; remote-host tests require running it on that host. Docker
exec sampling and log collection have modest measurement overhead; resource
sample interval is configurable and should be held constant across runs.
Nginx `upstream_response_time` can be absent or compound for retries; invalid
values are excluded from timing aggregation, not replaced with zero. Trace
join coverage is reported; under 95% or missing resource/Nginx samples
changes an otherwise PASS run to `INCOMPLETE_MEASUREMENT`.

## External and manual dependencies

An authorized operator must map image→commit, provision approved test
limits, provide a test host/HTTPS URL and verify the physical LB client-IP
contract. For LB validation, two distinct controlled source IPs, the LB
forwarding record, Nginx `$remote_addr`/XFF, backend-observed client host,
APP node and 429 counts must be compared. No trusted CIDR is invented here.
Two-node APP-01/02 pressure on shared DB, provider quota and search must be
measured separately. An isolated test provider is required for slow/429/
timeout fault scenarios; the normal workload never injects them.

## Exact commands for running the test

From the repository root on the designated test host, after reviewing the
test-only overlay and building the images from the intended commit:

```bash
export AUZEF_COMPOSE_EXTRA_FILE="$(./deploy/load-test/auzef-load-render-overlay)"
docker compose -f docker-compose.yml -f "$AUZEF_COMPOSE_EXTRA_FILE" up -d --build backend frontend
export AUZEF_EXPECT_GIT_COMMIT="$(git rev-parse HEAD)"
export AUZEF_EXPECT_BACKEND_IMAGE_ID="$(docker inspect --format '{{.Image}}' auzef_backend)"
export AUZEF_LOAD_URL='https://TEST-HOST/widget-chat'
AUZEF_REQUIRE_TELEMETRY=1 ./deploy/load-test/auzef-load-preflight
./deploy/load-test/auzef-load-plan --url "$AUZEF_LOAD_URL" --confirm-load-test
```

If the approved target contract differs, set `AUZEF_LOAD_CONTRACT_FILE` before
rendering, pass `--contract /absolute/path/contract.env` to the plan and use
the same file for the manual preflight. Do not adjust expected
values to fit the current host without changing the experiment definition.
The plan repeats preflight before starting any generator process.

Offline validation performed:

```text
bash deploy/load-test/tests/smoke.sh                         PASS
python -m unittest discover -s deploy/load-test/tests ...    18 tests PASS
backend/.venv/bin/python -m pytest tests/test_llm_outcomes.py
  tests/test_load_metrics.py tests/test_health.py
  tests/test_widget_tokens.py -q                             28 tests PASS
Nginx test-overlay syntax (ephemeral test container)          PASS
Local orchestrator preflight gate                              NO-GO; zero load requests
```

## Go / No-Go criteria for starting a load run

**GO:** reviewed ref/image mapping, approved resource contract matched,
preflight PASS, telemetry present, front-door URL verified, enough provider
quota/test isolation, clean or explicitly explained git state, operator ready
to observe STOP/WARN and preserve outputs. **NO-GO:** any preflight FAIL,
unknown image provenance, unexplained WARN, real LB assumptions treated as
validated by local Compose, or insufficiently isolated fault injection.

A local PASS and staged result now exist. The next production-relevant capacity
result requires a clean image provenance record and the intended APP-01/APP-02
topology with physical LB validation. No worker, pool, thread, timeout, retry,
rate-limit or pipeline remediation was included.
