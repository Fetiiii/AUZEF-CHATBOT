# Answer Pipeline V2 — Phase 3 Report

## Status and scope

```text
Phase 3 — Academic Calendar V2
STATUS: PASS
```

- Branch: `production-readiness`
- Starting HEAD: `a76d07d92d1bc4ef6b07e973b39e29368fe471bd`
- Source of truth:
  [`ANSWER_PIPELINE_V2_ARCHITECTURE.md`](../ANSWER_PIPELINE_V2_ARCHITECTURE.md)
- Selector V1 prompt/parser, semantic-NONE fallback, routing guards, QnA
  thresholds/K, degraded mode, model registry, exact-alias QnA behavior, and
  multi-answer composition were not changed.

## Previous and new routing

Previously every LLM-enabled request loaded every `AcademicCalendar` row and
placed the complete list at the start of every selector pool. Phase 2's
`calendar_relevant` value was trace-only.

Phase 3 routes each resolved intent as follows:

```text
calendar_relevant=false -> QnA retrieval; Calendar closed; 0 Calendar rows
calendar_relevant=true  -> QnA retrieval + filtered Calendar retrieval
```

The closed route performs no Calendar or Calendar-config DB query. The open
route supplies at most two filtered rows to the existing candidate shape.
Meili and Qdrant still run in both cases. Procedure-plus-date MULTI output uses
the existing per-intent loop: procedure goes to QnA only; date goes to QnA plus
Calendar. No new answer composer was added.

## Schema and migration

The existing model was reused. Its prior fields were:

```text
id, period, event, start_date, end_date, updated_by, created_at, updated_at
```

`event` remains the canonical event name and the string date range is retained.
The backward-compatible additions are:

```text
academic_year  VARCHAR(9), nullable
term           VARCHAR(16), nullable; GUZ | BAHAR | GENERAL at service boundary
aliases        TEXT, non-null JSON string list, default []
```

The project has no Alembic framework. Startup uses idempotent `ADMIN_DDL`
statements, while
[`migrate_calendar_v2.py`](../../backend/scripts/migrate_calendar_v2.py)
provides tested upgrade and downgrade operations.

No production row is deleted or assigned a guessed year/term. A legacy NULL
year is treated as part of the configured current set for rollout compatibility
and counted as `legacy_year_assumed_current_count`. Operators must review and
backfill such rows. No `active` field was invented because the prior model had
none and current academic year is the authoritative validity boundary.

List/create/update/bulk-update/import/export APIs and the existing admin grid
expose all new fields. Old payloads remain valid. When configured, current year
is assigned to new rows. Term is checked or conservatively derived from
`period`, otherwise `GENERAL`. CSV round-trip now includes `Akademik_Yil`,
`Term`, and pipe-separated `Aliases`.

## Current year and term policy

Resolution order is existing `SystemConfig`, then environment fallback:

```text
ACADEMIC_CALENDAR_CURRENT_YEAR
ACADEMIC_CALENDAR_CURRENT_TERM
```

`GET/PUT /api/settings/calendar` exposes the settings with checks. Both env
templates declare blank keys; no year or term is hardcoded. Year must be a
consecutive `YYYY-YYYY`; term must be `GUZ`, `BAHAR`, or `GENERAL`.

Missing/invalid current year fails closed. An implicit-term query also fails
closed without configured current term. Explicit-term queries do not require
current term for eligibility. No month-based guess is made.

Conservative year parsing accepts `2026-2027`, `2026/2027`, and `2026 2027`.
Non-consecutive or ambiguous evidence is rejected. A year different from the
configured current year returns zero Calendar candidates before Calendar rows
are loaded and records `historical_year_rejected=true`; QnA retrieval remains.

`güz`, `guz`, and `bahar` are parsed locally. Explicit term admits that term
plus `GENERAL`. Without explicit term, current term is a preference rather than
a hard opposite-term exclusion, so stronger event evidence can still win.

## Matching, aliases, ordering and limit

[`calendar_retrieval.py`](../../backend/services/calendar_retrieval.py) is a
deterministic boundary with no LLM or QnA-retrieval call. It normalizes Turkish
spelling and selected start-event inflections, applies year/term policy, scores
canonical event text plus record-owned aliases, rejects zero-evidence rows, and
uses a stable evidence/term/ID/name/date order.

Generic words such as `ne zaman`, `tarih`, `gün`, `dönem`, `sınav`, and `kayıt`
are not event evidence alone. Curated aliases live on each record; no AUZEF
event dictionary is embedded in source. Specific `büt` and reviewed multiword
aliases can match. Unrelated abbreviations cannot select a nearest row.

The default limit is `2`, hard-capped at `3`. `calendar_relevant=true` does not
guarantee a match; zero candidates is a normal result.

## LLM-off behavior

Fallback order remains:

```text
deterministic Calendar gate/retrieval -> Meili threshold -> Qdrant threshold
```

The Calendar gate now requires date-question evidence; an event keyword alone
does not open it. It uses the same year/term/match rules without Intent Analyzer
or a new LLM call. The legacy `search_calendar(use_llm=...)` signature remains,
but Calendar V2 always performs deterministic retrieval.

Selector semantic NONE still enters the existing Meili/Qdrant threshold
fallback with Calendar closed. Model-error/timeout and LLM-disabled handling
otherwise keep their prior contract.

## Decision trace V3

Schema version `3` adds one `calendar_routes` entry per intent or LLM-off
Calendar fallback. It records:

- relevance and route-opened state;
- current and explicit year/term;
- total, year-eligible, term-eligible, match, and returned counts;
- returned Calendar IDs, terms, and curated event names;
- typed no-match reason and historical rejection;
- legacy NULL-year count, latency, and intent/fallback purpose.

Raw user/resolved text, aliases, prompts, identity/contact/verification values,
and student-specific content are not stored.

## Verification

Final targeted Calendar/analyzer/trace/pipeline/export/audit regression suite:

```text
52 passed, 0 failed, 0 skipped, 1 warning
```

It covers year/term parsing, DB-over-env precedence, historical rejection,
GENERAL, term preference/override, aliases, generic-word and unrelated-event
safety, stable bounds, reversible migration, CRUD/settings, false/true routing,
QnA coexistence, procedure-plus-date MULTI, LLM-off order, trace safety,
Selector V1 compatibility, endpoints, CSV, and audit behavior.

Full backend suite in the same backend-only layout used by Phases 0–2:

```text
249 passed, 7 failed, 1 skipped, 1 warning
```

The seven failures are the unchanged `test_production_app_runtime.py` layout
case: `/app` is mounted without repository-level `/deploy/production`. With the
repository root mounted at the expected layout, those tests pass `7/7`.
Frontend production build also passes.

## Focused evaluation and candidate flood

No reviewed live-model Calendar benchmark is present, so no route precision,
recall, or live-model accuracy number is invented. Scripted analyzer contract
checks are not presented as model metrics.

The controlled eight-query retriever matrix covers implicit/explicit terms,
GENERAL, alias, no-match, historical rejection, and semester start:

```text
candidate counts: [2, 1, 1, 1, 2, 0, 0, 1]
average: 1.0
maximum: 2
```

Flooding changed from every Calendar row on every LLM request to exactly zero
when irrelevant and `0..2` with the current default when relevant (hard maximum
`3`). All deterministic route/match/year/term cases pass. Real analyzer route
accuracy still depends on the external reviewed corpus.

## Known limitations and Phase 4 readiness

- Existing production Calendar rows require review/backfill for year, term, and
  useful aliases. Until current year/term config is set, Calendar fails closed.
- Legacy dates remain strings to avoid an unrelated migration expansion.
- Historical Calendar questions intentionally receive no current-year event;
  QnA retrieval may still answer independently.
- Live-model Calendar route accuracy needs the external reviewed harness.

The architecture source-of-truth was not changed and no conflict was found.
Phase 4 implementation has not started. Production row review/config is an
operational rollout prerequisite, not a code blocker for starting Phase 4.
