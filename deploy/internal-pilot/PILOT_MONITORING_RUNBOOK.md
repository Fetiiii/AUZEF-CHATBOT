# Internal pilot monitoring runbook (A-08)

Use this from the final preflight and smoke through every pilot day. It uses the
always-on `decision_trace` v7 log; it does not require the load-test-only
`AUZEF_LOAD_METRICS` instrumentation. The current pilot candidate is not yet
approved: final KB, Selector, freeze and pilot environment must be recorded
before release. See the [master inventory](../../outputs/performance-readiness/PRODUCTION_READINESS_MASTER_INVENTORY.md)
and [final reacceptance watchlist](../../docs/answer-pipeline-v2/INTERNAL_PILOT_FINAL_REACCEPTANCE_REPORT.md).

## Fill before the first pilot request

| Item | Pilot value |
|---|---|
| Pilot backend host and topology (A-03) | **TBD by release owner** |
| Backend log source: Compose, journald, or institution collector | **TBD by operator** |
| Edge/LB/Nginx access and error log source, including 429 and 5xx | **TBD by operator** |
| Operator who checks twice daily | **TBD by release owner** |
| Backend/AI responder | **TBD by release owner** |
| AUZEF content reviewer and decision channel | **TBD by release owner** |
| Release owner who can pause/resume the pilot | **TBD by release owner** |
| Final KB fingerprint, Selector prompt/model and pilot commit | **TBD after freeze** |

Do not start the pilot with an unfilled log source or responder. Confirm that
the edge access log actually records `/widget-chat` status codes; some Nginx
locations disable access logging. `auzef-logs` shows backend journald only and
cannot establish the edge 429 rate. Keep provider and model unchanged during
the pilot. After any approved AI config or provider change, rerun the final
candidate preflight and short smoke before admitting pilot traffic.

## Start of shift and after each deployment

1. Record UTC time, deployed commit, KB fingerprint and effective AI config.
   On a native APP host with the full checkout and DB access, run the final
   candidate preflight from `backend`:

   ```bash
   python -m scripts.internal_pilot_preflight candidate --json
   ```

   A nonzero exit means **no pilot activation or expansion**. This command
   reads the active DB config; it does not change it. The candidate contract
   currently points to an unsuccessful Selector variant and must be finalized
   after Selector selection. A passing config preflight does **not** override
   a failed Selector gate: the release owner must also check the final gate
   evidence and KB freeze record before activation.
2. Run the short E2E smoke from a machine that can reach the pilot's public
   Nginx/LB endpoint and its backend logs. Compose does not publish backend
   port 8000 to the host. Set `PILOT_BASE_URL` to the actual pilot URL. For
   local Compose logs:

   ```bash
   python tests/e2e/internal_pilot/run.py --base-url "$PILOT_BASE_URL" --suite smoke --preflight candidate --preflight-via compose --trace-source compose
   ```

   Use `--trace-source journal` on the production app host, or `--trace-log`
   with a backend log file. `TECHNICAL_FAIL` blocks rollout. `MANUAL_REQUIRED`
   means transport and trace checks passed; the operator must still review
   semantic targets and Stage G UI behavior. The runner never activates Luna,
   changes AI settings or injects failures.
3. Check backend errors and edge status codes for the same period. Confirm a
   `decision_trace` for every smoke turn. A trace contains an internal request
   ID and conversation ID, not the raw question or answer.

## Twice-daily watch and daily summary

Use one UTC date for collection and reporting. Record the corresponding
Istanbul time interval in the handoff. Select the command for the deployed
topology; protect raw logs as operational records.

```bash
docker compose logs --no-color --since '2026-09-28T00:00:00Z' --until '2026-09-29T00:00:00Z' backend > /tmp/pilot-backend.log
python deploy/internal-pilot/pilot_trace_summary.py --utc-date 2026-09-28 --input /tmp/pilot-backend.log --output /tmp/pilot-summary.md
```

For the production systemd app host, replace the first command with:

```bash
journalctl -u auzef-backend.service --since '2026-09-28 00:00:00 UTC' --until '2026-09-29 00:00:00 UTC' -o cat --no-pager > /tmp/pilot-backend.log
```

The summary counts final outcomes, SELECT/NONE decisions, provider failure
categories, retry counts, degraded reasons, unavailable retrieval sources,
calendar/context/MULTI paths, and latency percentiles. It returns code 2 when
there are no matching traces; check log access, date and deployment before
calling that a quiet day. Track request volume, 429 and 5xx from the edge
log separately: a proxy 429 has no backend trace, while a provider 429 appears
as `failure_category=RATE_LIMIT` in Analyzer or Selector. `retry_count` is an
invocation total, not a per-attempt timeline. Normal production does not emit
load-test `llm_retry` events.

At the end of each day, the operator records:

- UTC window, pilot users/requests, deployment commit, KB/config fingerprint.
- Trace count versus backend/edge requests; missing trace IDs, 5xx, edge 429.
- Provider `RATE_LIMIT` request count, retry requests and total retries.
- p50/p95 total latency; Analyzer, Selector and retrieval p95; source outages,
  circuit/degraded reasons and duration.
- Human-reviewed SELECT/NONE/qualifier/context/MULTI/calendar examples:
  reviewed count, correct/incorrect/uncertain, request IDs, affected QnA IDs,
  owner and resolution. Record adjudication in the approved pilot location,
  not in raw shared logs.
- Incidents, decisions, open items and next shift owner.

## Quality review: wrong QnA and false NONE

The trace explains *which decision happened*, not whether the answer was
correct. Use the summary's review queue plus a daily sample of ordinary
SELECT and NONE turns. Retrieve the question and rendered answer only through
the authorized pilot interface, joining by conversation ID and UTC time.
Do not put student text or conversation tokens in the shared summary.

For each sampled turn, record `correct`, `incorrect` or `uncertain` and a short
reason. Check the selected `final.final_qna_ids` against the student's actual
intent. For false NONE, inspect `selectors[].semantic_none`, candidate QnA
IDs, `source_availability` and retrieval counts: a real absence, retrieval
miss and Selector rejection require different fixes. Check the final answer
too; suggestions are not answers. Give priority to the historical watchlist:
generic versus specific qualifiers, expected NONE, calendar routing, bare
referent context and MULTI. A content disagreement goes to the AUZEF content
owner; a correct candidate rejected or wrong candidate selected goes to the
Selector owner; missing candidates go to the KB/retrieval owner.

## Initial escalation rules

These are conservative pilot operations thresholds, not accuracy claims or
the final Selector benchmark gate. The release owner may adjust them only with
a dated written decision after observing pilot volume.

| Signal | Action |
|---|---|
| Failed candidate preflight, missing smoke trace, smoke 5xx, or wrong critical fee/deadline/eligibility answer | Pause pilot admission; release owner and backend/AI responder investigate immediately. Content owner adjudicates the answer. Resume after a documented fix and preflight/smoke. |
| Any backend/edge 5xx, source `unavailable`, open circuit, or provider `RATE_LIMIT` in normal traffic | Operator checks the affected request IDs and edge/backend logs the same shift. Three affected requests in a day, or any continuing outage, escalates to backend/AI responder and release owner. |
| Any retry, degraded result, Analyzer invalid output, or unexpected NONE | Add to daily review. Three affected requests in a day or repeated cause across two checks escalates to backend/AI responder. |
| One noncritical wrong QnA or false NONE | Content/Selector reviewer assesses the same day and records scope. Repeated pattern or a critical policy error pauses admission until the release owner decides. |
| No traces for a period with known requests, or no usable edge 429/5xx source | Treat monitoring as unavailable; pause expansion and restore evidence collection. |

The operator opens the incident with UTC time, request IDs, deployment/config
fingerprints, trace summary, edge status and affected QnA IDs. The responder
checks provider, DB, Meili and Qdrant health; the content owner decides factual
correctness; the release owner decides pause/resume. Do not change the provider
from the admin UI as an untracked workaround. For a faulty new release, use
the approved prior release/config and rerun preflight plus smoke before
resuming. Preserve the original incident evidence and daily summary.
