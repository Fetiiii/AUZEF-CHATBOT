# CPU/thread tuning experiment — controlled local load test

**Date:** 24 September 2026\
**Branch:** `production-readiness`\
**Result:** No configuration demonstrated a consistent end-to-end improvement at both 15 and 20 concurrent virtual users. No production setting was changed.

## Method

Four sequential runs used the same backend image, deterministic PII-free workload, two Uvicorn workers, 6 CPU limit, 8 GiB memory, 12 GiB memory+swap, opt-in telemetry, localhost Nginx front door, and the existing STOP/WARN gate. Each run restarted the backend, passed preflight, then ran 15-user and 20-user warm-up and steady phases. Run order was `OMP/MKL=8 → 4 → 2 → 8`; the final baseline repeat checks time/order drift. All overlays live only under ignored `outputs/performance-readiness/tuning-20260924-threads/`.

PyTorch `get_num_threads()` reported 8, 4 and 2 respectively; `get_num_interop_threads()` remained 16. Worker count and all other pipeline/provider/resource settings were held fixed. Each 15-user steady phase had 105 requests; each 20-user steady phase had 117. Warm-up results are excluded from the table. These sample sizes meet the harness's p95 policy but do not support a reliable p99 estimate.

| OMP/MKL | Run order | Users | Total p95 | Embedding p95 | Throughput | Throttled CPU periods | Throttled time | Max worker threads |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 | 1 | 15 | 5.13 s | 392 ms | 3.38 req/s | 12.3% | 10.0 s | 120 |
| 8 | 1 | 20 | 5.30 s | 809 ms | 4.14 req/s | 23.8% | 24.6 s | 152 |
| 4 | 2 | 15 | 4.78 s | 226 ms | 3.17 req/s | 2.8% | 1.4 s | 64 |
| 4 | 2 | 20 | 5.43 s | 1264 ms | 4.32 req/s | 11.6% | 15.9 s | 76 |
| 2 | 3 | 15 | 5.28 s | 105 ms | 3.56 req/s | 0.7% | 0.1 s | 34 |
| 2 | 3 | 20 | 5.78 s | 1146 ms | 4.16 req/s | 6.1% | 11.1 s | 40 |
| 8 | 4 | 15 | 5.20 s | 742 ms | 3.62 req/s | 16.8% | 18.3 s | 136 |
| 8 | 4 | 20 | 6.22 s | 1598 ms | 4.22 req/s | 26.7% | 34.2 s | 160 |

“Throttled CPU periods” is `Δnr_throttled / Δnr_periods` from cgroup `cpu.stat` within the steady phase; it is not percentage of CPU capacity lost. Throttled time is the cgroup counter delta. Worker thread counts are sampled maxima, not simultaneous sums.

## Observations

- Lower OMP/MKL budgets consistently reduced worker thread counts and CPU throttling. At 15 users, embedding p95 also decreased in both lower-thread runs versus both baseline runs.
- `OMP/MKL=4` gave the lowest **total p95 at 15 users** (4.78 s versus 5.13–5.20 s baseline), but also lower throughput (3.17 versus 3.38–3.62 req/s). At 20 users its total p95 (5.43 s) lay inside the repeated-baseline range (5.30–6.22 s).
- `OMP/MKL=2` gave the lowest throttling and embedding p95 at 15 users, but total p95 was worse than both baseline runs. At 20 users its total p95 was inside the baseline range.
- The two identical baseline runs differed substantially: embedding p95 was 392 versus 742 ms at 15 users and 809 versus 1598 ms at 20. This drift is large enough to overlap the 20-user variant effects. Provider SDK-call p95 also ranged roughly 2.8–3.3 s across runs. The measurements do not establish the cause of the drift.
- All four runs completed with preflight PASS, watcher OK, 1048/1048 HTTP 200, zero 429/5xx/transport timeout and 100% client-to-DecisionTrace join. NONE and degraded counts were identical across variants at each stage. No unplanned worker restart or OOM was observed during a run; the backend was intentionally recreated between variants.

## Recommendation

**Do not select a permanent production thread budget from this experiment.** `OMP/MKL=4` is the most useful candidate for another controlled comparison because it reduced throttling and improved 15-user p95, but the 20-user latency benefit and throughput tradeoff are not settled. `OMP/MKL=2` demonstrably lowers resource pressure without a demonstrated end-to-end latency benefit. Keep the existing configuration until a repeated crossover run on the intended test topology supports a decision. No remediation was applied here; the local test backend was returned to `OMP/MKL=8` with the original test overlay.

The runs are single-node local Compose measurements from a dirty working tree with a localhost self-signed test certificate. They do not prove APP-01/APP-02 or physical-LB capacity. Actual provider SDK retry attempts remain unobservable. Full per-run client and server telemetry is under `outputs/performance-readiness/`; the [matrix manifest](../outputs/performance-readiness/tuning-20260924-threads/matrix.json) and [machine-readable comparison](../outputs/performance-readiness/tuning-20260924-threads/comparison.json) map run IDs to variants.
