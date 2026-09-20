# Answer Pipeline V2 — Internal Pilot Final Re-Acceptance

Date: 2026-09-20  
Branch: `production-readiness`  
Start HEAD: `72f882965ad42957d8376c547070ea72ac40ac21`

## Verdict

`INTERNAL_PILOT_REACCEPTANCE = PASS_PENDING_MANUAL_UI`

`INTERNAL_PILOT_RELEASE_CANDIDATE = BLOCKED_ON_MANUAL_UI_ONLY`

`ENGINEERING_BLOCKERS = 0`

The complete 41-scenario catalog was executed without changing runtime source,
prompt, parser, model, gold, KB, Calendar data, or aliases. Stage E is
`PASS_WITH_QUALITY_OBSERVATIONS`: every major capability works in live traffic,
while isolated semantic misses remain on the pilot watchlist. Stage G remains
manual and is the only release-candidate gate still open.

## Decision policy fixed before measurement

The product-level policy supplied for this run was applied without revision.
Systemic infrastructure/capability failure is blocking. An isolated wrong QnA,
context miss, qualifier miss, NONE miss, analyzer invalid output, the known
bare-referent limitation, or empty Calendar aliases is a quality observation
unless its frequency shows that the capability is effectively dead.

The historical 14-probe semantic screen remains immutable and remains `FAIL`
for the bare plural / implicit referent case. It was not rewritten to PASS and
was not used as an automatic blocker for this product-level run.

## Freeze and effective runtime

- Parent freeze: `c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6`
- Latest amendment: `f92d8eb2a7e809ae15e256280cbe7c707480e2b261553df15aa62707a8cfee98`
- Selector prompt: Variant A / `variant_a_v1`
- Selector fingerprint: `1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e`
- Provider/model: OpenRouter / `openai/gpt-4o-mini`
- Selector temperature: `0`
- Selector max tokens: `32`
- Reasoning field: unset/omitted
- Admin LLM enabled: `true`

The initially running backend image contained the preceding amendment-3 prompt.
The image was rebuilt/recreated from the unchanged Final HEAD before measurement.
Container-effective prompt fingerprint then matched the Final HEAD:
`0bd2a42977187de11252c716007f3d3b21e229afd0af77f9884c3daffced5a42`.

## Calendar

- Period: `2026-2027`
- Primary live rows: `19`
- Calendar aliases populated: `0`, intentionally
- Alias owner: authorized AUZEF staff
- Alias absence: non-blocking
- Data/aliases modified by this run: `NO`

Live Stage E opened Calendar V2 for 3/4 catalog calendar-relevant scenarios,
using the current 19-row dataset. All three calendar-irrelevant scenarios
correctly kept the route closed. `IP-E2E-032` is the one routing miss.

## Stage results

| Stage | Status | Evidence |
|---|---|---|
| A Runtime/config | PASS | Freeze chain, effective container config and Final HEAD prompt verified |
| B Automated regression | PASS | Backend 737 pass/1 skip; targeted 218; deploy/release 46 |
| C Docker/services | PASS | Backend/DB healthy; frontend/Meili/Qdrant running; readiness all `ok`; no restart loop |
| D Offline regression | PASS | Frozen 518-case snapshot, zero-provider oracle self-test: 486/486 executed; no live benchmark call |
| E Full live E2E | PASS_WITH_QUALITY_OBSERVATIONS | 41/41 scenarios, 44/44 turns, 0 HTTP 5xx; all capabilities demonstrably live |
| F Failure paths | PASS | F1–F4 graceful 200 responses; state restored |
| G UI | MANUAL_REQUIRED | No new UI automation framework introduced |
| H Observability | PASS | 44/44 normal traces plus 4/4 failure traces reconstructed |

Stage D emitted a historical snapshot-contract namespace warning and wrote a
new result namespace. The oracle harness completed 486/486 with zero provider
calls; current production selector prompt/freeze assertions remained unchanged.

## Stage E coverage and operational metrics

Catalog SHA-256:
`1c0acd06475e17c65d3d45af1373cc3f82d94dc99a3d88fb553fe9f35f1021b0`

The catalog was not edited. Scenarios `IP-E2E-001` through `041` were executed
through `/widget-chat`; multi-turn sessions retained their conversation ID and
token. The run stayed within budget: 44 logical user turns and 87 logical model
inferences (44 Analyzer + 43 Selector).

| Metric | Result |
|---|---:|
| Scenarios | 41/41 |
| Logical turns | 44 |
| HTTP 5xx | 0 |
| Analyzer valid | 42 |
| Analyzer invalid | 2 (4.55%) |
| SINGLE | 43 |
| MULTI | 1 |
| `context_used=true` turns | 1 |
| `calendar_relevant=true` turns | 5 |
| Selector invoked | 43 |
| SELECT | 36 |
| NONE | 7 |
| Selector errors | 0 |
| Normal degraded turns | 2 |
| Provider errors | 0 |
| Timeouts | 0 |

The two invalid Analyzer outputs were `IP-E2E-037` and `IP-E2E-039` turn 2.
Both entered `REQUEST_DEGRADED` with reason
`intent_analyzer_invalid_output`, returned HTTP 200, and did not produce a
provider error or timeout.

## Capability acceptance

### MULTI

- Clear-MULTI scenarios: 3
- Correct MULTI: 1 (`IP-E2E-038`)
- Incorrect SINGLE/fallback: 2 (`IP-E2E-036`, `IP-E2E-037`)

MULTI is demonstrably operational and therefore not effectively dead. Its 1/3
recognition rate is a material pilot quality limitation and must be watched.

### Context

- Context-required follow-ups: 2 (`IP-E2E-039`, `IP-E2E-040`)
- Correct context usage and validated rewrite: 1 (`IP-E2E-040`)
- Misses: 1 (`IP-E2E-039`)
- False context usage: 0
- History-present self-contained control: `IP-E2E-041` passed

The miss is consistent with the known bare/generic referent class and degraded
safely. Context is not effectively dead because `IP-E2E-040` resolved and used
the preceding USER topic, while the control did not over-trigger.

### Calendar

- Calendar-relevant scenarios: 4
- Correct route: 3
- Missed route: 1 (`IP-E2E-032`)
- Calendar-irrelevant scenarios: 3
- False-positive routes: 0

### Selector and NONE

- Selector calls: 43
- SELECT: 36
- NONE: 7
- Selector errors: 0
- Expected-NONE scenarios: 4
- Correct NONE: 3
- Semantic miss: `IP-E2E-027` selected an unrelated DGS answer
- NONE never triggered degraded mode

Selector was reachable throughout normal eligible traffic. `IP-E2E-019` also
returned NONE for an in-domain general burs question, recorded as a qualifier
quality observation.

### Qualifiers and semantic answer quality

Known/general-specific observations were recorded for:

- `IP-E2E-012`: general graduation question narrowed to bachelor/240 ECTS
- `IP-E2E-013`: unspecified internship narrowed to one named program
- `IP-E2E-018`: general transfer narrowed to central transfer
- `IP-E2E-019`: general scholarship question returned NONE
- `IP-E2E-023`: explicit summer-school date returned NONE/suggestions

Near-QnA semantic misses were also observed in `IP-E2E-009` and
`IP-E2E-011`. These are pilot quality limitations, not evidence that Selector
is unreachable or the pipeline is systemically dead.

## Failure paths

| Path | Result | Evidence |
|---|---|---|
| F1 Admin LLM OFF | PASS | HTTP 200, `ADMIN_DEGRADED`, one curated answer, no Analyzer call |
| F2 OpenRouter failure | PASS | HTTP 200, `REQUEST_DEGRADED`, `intent_analyzer_model_error`, one curated answer |
| F3 Meili unavailable | PASS | HTTP 200, Meili=`unavailable`, Qdrant=`available`, normal answer |
| F4 Qdrant unavailable | PASS | HTTP 200, Qdrant=`unavailable`, Meili=`available`, normal answer |

After fault injection: LLM enabled was restored, temporary DB key override was
absent, Meili and Qdrant were running, and readiness again reported DB admin,
DB chat, Meili and Qdrant all `ok`.

## Observability and availability semantics

All 44 normal turns exposed:

- conversation ID and UTC timestamp
- Analyzer mode, context and calendar decisions
- Analyzer provider/model and latency
- retrieval latency and candidate counts
- Calendar/Meili/Qdrant availability
- Selector decision, candidate count and latency where invoked
- degraded state/reason and final outcome

Availability values `available`, `unavailable`, and `skipped` were observed in
normal/failure traffic. Zero results were not misclassified as unavailable.
The four controlled failure traces also contained the expected availability or
degraded reason.

## Privacy

Artifacts contain synthetic catalog IDs, aggregate metrics, response hashes and
short curated-answer previews. They contain no API key, Authorization header,
verification code/token, identity number, phone number, or raw secret. `.env`
was neither copied nor committed.

## Performance

Development-host sample; not a production SLA.

| Latency | Avg | Median | p95 | Max |
|---|---:|---:|---:|---:|
| Total pipeline | 3839.83 ms | 3799.95 ms | 4626.93 ms | 6886.60 ms |
| Intent Analyzer | 1978.67 ms | 1882.18 ms | 2689.40 ms | 3167.77 ms |
| Retrieval | 374.49 ms | 341.29 ms | 566.69 ms | 2370.80 ms |
| Selector | 1498.22 ms | 1515.54 ms | 1824.99 ms | 1952.48 ms |

## Stage G manual UI checklist

Stage G remains `MANUAL_REQUIRED`. An authorized tester must confirm:

- widget opens
- message sends
- loading state is visible
- answer renders
- conversation continues across turns
- refresh behavior is acceptable
- error state is usable
- basic mobile viewport is acceptable

## Engineering blockers

None. The run found no systemic infrastructure failure, repeated 5xx behavior,
dead Selector, dead MULTI/context/Calendar/NONE capability, unrecovered fault
path, missing normal-traffic observability, or unhealthy runtime.

## Known quality limitations

- Historical bare-referent semantic screen remains FAIL
- Context required follow-up: 1/2 correct
- Clear MULTI recognition: 1/3 correct
- Calendar relevant routing: 3/4 correct
- Expected NONE: 3/4 correct
- Analyzer invalid output: 2/44 (4.55%), both safely degraded
- General/specific and explicit qualifier errors
- Near-QnA semantic selection misses
- Calendar aliases empty by design

## Pilot quality watchlist

Monitor and independently review:

- bare-referent context misses
- general/specific qualifier errors
- semantic NONE errors
- Calendar routing misses
- Intent Analyzer invalid-output rate
- clear-MULTI recognition
- near-QnA semantic selection

## Post-pilot validation

Real pilot conversations must be anonymized, deduplicated, representatively
sampled, independently human-adjudicated, and used for final production
validation before Phase 7G.

## Artifacts

`outputs/internal-pilot-final-reacceptance/` contains:

- `manifest.json`
- `stage-results.json`
- `e2e-results.jsonl`
- `e2e-trace-join.json`
- `capability-summary.json`
- `failure-path-results.json`
- `observability-results.json`
- `performance-summary.json`
- `service-health.json`
- `quality-watchlist.json`
- `stage-d-offline/`

Historical reports and artifacts were not overwritten.
