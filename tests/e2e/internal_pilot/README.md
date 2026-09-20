# Internal Pilot — E2E scenario catalog

**Status:** `NOT_EXECUTED`. This catalog is authored, versioned and
schema-tested; it is not run by the freeze task.

- Catalog: [`scenarios.json`](scenarios.json) — 48 scenarios, 16 categories
- Plan: `docs/answer-pipeline-v2/INTERNAL_PILOT_ACCEPTANCE_PLAN.md` (stages E–F)
- Freeze: `docs/answer-pipeline-v2/INTERNAL_PILOT_FREEZE.md`
- Schema/coverage tests: `backend/tests/test_internal_pilot_freeze.py`

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
