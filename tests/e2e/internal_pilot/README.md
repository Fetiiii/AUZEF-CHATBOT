# Internal Pilot — E2E scenario catalog

**Status:** `NOT_EXECUTED`. This catalog is authored, versioned and
schema-tested; it is not run by the freeze task. The committed runner is
available for the final pilot candidate. This status describes the catalog's
execution evidence, not runner availability.

- Catalog: [`scenarios.json`](scenarios.json) — 48 scenarios, 16 categories
- Plan: `docs/answer-pipeline-v2/INTERNAL_PILOT_ACCEPTANCE_PLAN.md` (stages E–F)
- Freeze: `docs/answer-pipeline-v2/INTERNAL_PILOT_FREEZE.md`
- Schema/coverage tests: `backend/tests/test_internal_pilot_freeze.py`
- Live runner: [`run.py`](run.py)

## Live runner

From the repo root, with the pilot's public Nginx/LB URL in
`PILOT_BASE_URL` and access to backend logs:

```bash
python tests/e2e/internal_pilot/run.py --base-url "$PILOT_BASE_URL" --suite smoke --preflight candidate --preflight-via compose --trace-source compose
```

Compose does not publish backend port 8000 to the host. Use the public
endpoint that the pilot users will reach.

The same command on an app host can use `--trace-source journal`; use
`--trace-log /path/to/backend.log` when logs are collected elsewhere. The
optional preflight reads the active managed config. For Compose,
`--preflight-via compose` runs it in a temporary backend container with the
checkout mounted read-only; the DB service must already be running. On a
native APP host with the full checkout and DB access, use the default
`--preflight-via host`. `--preflight runtime` checks the older runtime
freeze; `candidate` checks the final candidate contract. The current
candidate is not yet approved for activation.

`smoke` selects seven normal paths. `--suite standard` selects all 41 nominal
scenarios, including multi-turn conversations. `--id IP-E2E-025` and
`--category valid_none` narrow the run. Fault cases 042–048 require the
operator to set up the condition outside the runner and name it explicitly,
for example `--fault-ready openrouter_timeout`; the runner never changes
provider, admin or search settings.

The default report is a timestamped file under `outputs/internal-pilot-e2e/`
(ignored by Git); an existing report is never overwritten. It contains HTTP
status, latency, conversation ID, answer length,
decision trace IDs, selected QnA IDs and behavior differences for human review.
It contains no user question,
answer text or conversation token. Exit code 1 means a preflight, transport or
trace check failed. Exit code 0 with `MANUAL_REQUIRED` means those technical
checks passed; a reviewer must assess `semantic_target`, observed intent,
Selector/NONE, calendar and degraded behavior using the authorized pilot
interface. Stage G widget/UI checks remain manual. The smoke and nominal runs
alone are not a final pilot acceptance or full fault-path run.

## Evaluation rules

**Judge on `semantic_target`, never on exact wording.** A scenario passes
when the delivered answer serves the stated information need *and* the
observable pipeline behaviour (intent mode, calendar relevance, selector
decision, degraded flag) matches the scenario.

Scenarios are written from realistic AUZEF user language and deliberately
contain **no benchmark case IDs and no DEV/HOLDOUT text**, so the suite
cannot be passed by memorising benchmark items. A test asserts this.

## Scenario fields

| Field | Meaning |
| --- | --- |
| `id` | stable identifier, `IP-E2E-NNN` |
| `category` | one of the declared `categories` |
| `turns` | user messages in order; more than one exercises conversation context |
| `expected_intent_mode` | `SINGLE`, `MULTI`, or `N/A` when the LLM path is not reached |
| `expected_selector_decision` | `SELECT`, `NONE`, `ANY` (behaviour matters more than the ref), or `N/A` |
| `calendar_relevant` | whether the turn is a date/period question |
| `degraded_expected` | whether degraded mode is the correct outcome |
| `semantic_target` | what a correct answer must accomplish |
| `fault_injection` | failure to inject before the request (stage F) |
| `known_issue_id` | links to a documented known limitation |
| `critical` | must be covered in every acceptance run |

## Known-issue categories

`generic_vs_specific_qualifier` (`IP-KI-1`) and `valid_none` (`IP-KI-2`) are
**known-open**. Failures there are counted and reported but do not by
themselves block the internal pilot — they are exactly what the pilot is
meant to gather real data on.
