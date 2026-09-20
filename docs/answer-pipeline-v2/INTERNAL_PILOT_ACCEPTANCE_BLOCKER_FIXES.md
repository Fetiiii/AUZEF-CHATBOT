# Internal Pilot — Acceptance Blocker Fixes

**Parent freeze:** `c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6` — **immutable, not rewritten**
**Amendment:** `INTERNAL_PILOT_FREEZE_AMENDMENT_1`
**Amendment fingerprint:** `9d221c3cf94ffd5039670e74b6272b078106f793c06f98fe649557ca21ec8485`

> ## `INTERNAL PILOT BLOCKER FIXES: PARTIAL`
> ## `INTERNAL_PILOT_REACCEPTANCE = BLOCKED_ON_CALENDAR_DATA`
>
> Two of three blockers are closed and verified against live OpenRouter. The
> third cannot be closed here: **the 2026–27 academic calendar does not exist
> anywhere in the repository**, and no date or event was invented.

---

## 1. Acceptance failure summary (historical — unchanged)

The previous run is recorded in `INTERNAL_PILOT_ACCEPTANCE_REPORT.md` and
stays `INTERNAL PILOT ACCEPTANCE = FAIL`. That artifact was **not rewritten**.
It found three blockers:

1. Intent Analyzer live output contract mismatch — 14/14 requests degraded.
2. Calendar data stale — 2025–26 loaded, pilot period is 2026–27.
3. Test environment isolation — root `.env` changed six test outcomes.

---

## 2. BLOCKER-1 — Intent Analyzer contract — **CLOSED**

### Root cause

The analyzer prompt declared its output schema with **string type labels**:

```json
"output_schema": {"intent_count": "1 or 2", "context_used": "boolean", ...}
```

`gpt-4o-mini` mirrored the shape and returned `"intent_count": "1"` — a
string. The runtime contract is strict and typed:

```python
model_config = ConfigDict(extra="forbid", strict=True)
intent_count: Literal[1, 2]
```

Strict Pydantic will not coerce `"1"` → `1`, so every turn failed validation,
the request fell to `REQUEST_DEGRADED`, and the selector was never invoked.

### Fix — prompt aligned to the contract, contract untouched

Per the fix direction, the contract was **not** weakened. No `"1"` → `1`
coercion, no `strict=False`, no `Union[str, int]`, no silent normalization.

Two changes, both in `build_intent_analyzer_prompt`:

1. `output_schema` is now a **typed example** — every value is a real instance
   of the JSON type the field must carry (`"intent_count": 1`, booleans as
   `false`), never a type label.
2. The system prompt states the requirement explicitly: *"intent_count bir
   JSON integer olmalıdır: 1 ya da 2. Tırnak içinde string gönderme
   (`"1"` YANLIŞ, 1 DOĞRU)."*

### A second instance of the same class, found by live probing

The first live probe round returned **integer `intent_count` in 8/8** — the
original bug was fixed — but **6/8 still failed** on a different rule:

```
ValueError: resolved intent changed without context
```

The parser requires `resolved_text == normalized_text` whenever
`context_used` is false, and the prompt never said so; the model was
rephrasing (e.g. *"Kayıt yenileme işlemi hakkında bilgi almak istiyorsunuz."*).
The same fix pattern applied: the prompt now states both branches of that
rule explicitly. **This second defect would not have been found without the
mandated live validation.**

### Live validation — 8 probes, frozen analyzer config

Provider `openrouter`, model `openai/gpt-4o-mini`, config fingerprint
`2f1b27bc3430…`, reasoning unset. No selector benchmark call.

| Probe | Result |
| --- | --- |
| simple SINGLE | ✅ valid |
| short/noisy SINGLE | ✅ valid |
| calendar SINGLE | ✅ valid |
| clear MULTI | ✅ valid |
| long but SINGLE | ✅ valid |
| context follow-up | ✅ valid |
| valid-NONE shape | ✅ valid |
| explicit qualifier | ✅ valid |

```
invalid_output              = 0   (was 6/8, then 14/14 live)
integer intent_count        = 8/8
quoted-string intent_count  = 0/8
```

### End-to-end proof

A real request through the client path (`nginx → /widget-chat`) now completes
the full pipeline:

```
execution_mode        NORMAL_LLM          (was REQUEST_DEGRADED)
intent_analyzer       success             (was invalid_output)
selectors called      1 → SELECT          (was never invoked)
selector model        openai/gpt-4o-mini
candidate_count       11
retrieval_ms          414.2               (was never reached)
source_availability   calendar: skipped, meili: available, qdrant: available
final                 answer from llm, qna_id 32
```

This also demonstrates the observability fields the acceptance run could not
reach: `retrieval_ms` and all three availability states are now populated in
live traffic, including the `skipped` vs `available` distinction.

### ⚠️ Residual observations for re-acceptance (not blockers)

All eight probes are now **valid output**, which is the criterion. But the
analyzer is behaving conservatively, and re-acceptance Stage E must measure
this rather than assume it:

- the *clear MULTI* probe (`"Harç ne kadar ve nasıl ödeyeceğim?"`) returned
  `intent_count = 1`, not 2;
- the *calendar* probe returned `calendar_relevant = false`;
- the *context follow-up* probe returned `context_used = false`.

These are consistent with the documented policy (*ambiguity → SINGLE*), and
policy was deliberately **not** changed here. But they are semantic-quality
questions that only a full Stage E run with the selector can answer. They are
flagged, not resolved.

---

## 3. BLOCKER-3 — Test environment isolation — **CLOSED**

### Root cause

`main.py`, `core/database.py` and `services/llm_provider.py` all call
`load_dotenv()` **at import time**. `pytest_configure` runs *before* those
imports, so any variable it clears is re-**set** from the project `.env` when
the application is imported (`load_dotenv` sets keys absent from the
environment). Test outcomes therefore depended on the developer's `.env`.

Measured cost: adding the pilot settings to `.env` broke **six** unrelated
tests with no code change at all.

### Fix — at the test fixture level, not the command line

`backend/tests/conftest.py` now disables `dotenv.load_dotenv` for the test
process. Runtime semantics are untouched: Docker and production still use
`env_file: .env` and the real `load_dotenv()`. Local development is unchanged.

### Verified in both modes

| Mode | Condition | Result |
| --- | --- | --- |
| **A** | Project `.env` carries `SELECTOR_PROMPT_VERSION=variant_a_v1`, `LLM_ENABLED_DEFAULT=true` | **PASS** |
| **B** | No pilot overrides in the process environment | **PASS** |

Four guard tests assert the isolation directly, including one that reads the
machine's real `.env` and fails if its values ever reach the test process.

---

## 4. BLOCKER-2 — Calendar source — **OPEN**

Searched `data/`, `backend/data/` (empty), `outputs/`, both import scripts,
and the whole repository for `2026-2027` / `2026-27` / `2026/2027`.

**No 2026–27 calendar exists.** Every match is a test fixture, a UI
placeholder (`placeholder="ör: 2026-2027"`) or a docs example. The only real
calendar file is:

```
data/auzef_akademik_takvim_2025_2026_guncel.csv
  header: Donem,Etkinlik,Baslangic_Tarihi,Bitis_Tarihi
  19 rows, academic year 2025-2026
```

`migrate_calendar_v2.py` is schema-only. Neither `importer.py` (QnA) nor
`import_etiya_history.py` (chat history) touches `academic_calendar`.

**Nothing was invented.** No date, event or row was fabricated, and no
calendar was guessed from the web.

### `BLOCKER_CALENDAR_SOURCE = OPEN` — exactly what is needed

| Item | Value |
| --- | --- |
| **What** | AUZEF **2026–2027** academic calendar |
| **Format** | CSV, UTF-8, comma or semicolon delimited, ≤ 5 MB |
| **Required columns** | `Donem`, `Etkinlik`, `Baslangic_Tarihi`, `Bitis_Tarihi` |
| **Optional columns** | `Akademik_Yil`, `Term`, `Aliases` (pipe-separated) |
| **English aliases** | `period`, `event`, `start_date`, `end_date`, `academic_year`, `term`, `aliases` |
| **Date format** | `DD.MM.YYYY` (as in the existing 2025–26 file) |
| **Term values** | `GUZ`, `BAHAR`, `GENERAL` |
| **Import via** | `POST /api/academic-calendar/import` (multipart CSV, authenticated) |
| **Writes to** | `academic_calendar` table |
| **Then set** | `PUT /api/settings/calendar` → `ACADEMIC_CALENDAR_CURRENT_YEAR=2026-2027`, `ACADEMIC_CALENDAR_CURRENT_TERM=GUZ` |

**The internal pilot must not open until a current calendar is loaded.**

---

## 5. Freeze amendment

The parent freeze is immutable and was not rewritten. Amendment 1 records the
fix with its own fingerprint:

| Field | Value |
| --- | --- |
| Amendment id | `INTERNAL_PILOT_FREEZE_AMENDMENT_1` |
| Amendment fingerprint | `9d221c3cf94ffd5039670e74b6272b078106f793c06f98fe649557ca21ec8485` |
| Parent fingerprint | `c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6` |
| Analyzer prompt fingerprint | `b5f3f8b3da978042f9e6cee13c0f204a1d502100849d3127cfaf99c325a2d25f` |
| Classification | **bug_fix = YES**; research / selector / model / policy change = **NO** |

The amendment fingerprint excludes `created_at` and `git_commit`, so it is
reproducible; a test asserts that and that the parent still fingerprints to
`c7081ff5…`.

### Selector — byte-identical, verified

| Field | Value |
| --- | --- |
| Prompt | `variant_a_v1`, `1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e` ✅ |
| Provider / model | `openrouter` / `openai/gpt-4o-mini` ✅ |
| Temperature / max_tokens | `0` / `32` ✅ |
| Reasoning | UNSET ✅ |
| Candidate order | ORIGINAL (`production`) ✅ |
| Contract fingerprint | `d50fbee416ca…` ✅ |

Parser strictness is unchanged (`strict=True`, `extra="forbid"`), and the
analyzer behaviour policy is unchanged (SINGLE default, MULTI max 2,
ambiguity → SINGLE, max 2 previous USER turns, bot messages excluded,
`calendar_relevant` semantics, failure fallback).

---

## 6. Observability

No observability semantics were changed in this task. The analyzer fix made
the previously unreachable fields verifiable live for the first time:
`conversation_id`, UTC `timestamp`, `retrieval_ms` (414.2 ms), per-source
availability with a real `skipped` value, and full selector telemetry. Full
verification remains part of re-acceptance.

---

## 6b. Tests

Run on the final HEAD **with the pilot `.env` in place** — the exact
configuration that produced 6 failures before this fix:

| Suite | Result |
| --- | --- |
| Backend + deploy/release, pilot `.env` present | **736 passed, 0 failed** |
| — of which new contract/isolation/amendment tests | 32 |
| Previous run, same `.env` | 6 failed, 698 passed |

Coverage added: analyzer contract (integer accepted, string rejected, 0/3/None
rejected, count/length invariant), raw-provider-payload → real-parser path,
prompt/contract alignment, analyzer policy unchanged, freeze amendment
determinism and classification, selector-unchanged, and four `.env`-isolation
guards.

---

## 7. Remaining blockers and re-acceptance readiness

| Blocker | Status |
| --- | --- |
| Intent Analyzer contract | **CLOSED** — 8/8 live probes valid, 0 invalid output |
| Test environment isolation | **CLOSED** — Mode A and Mode B both pass |
| **Calendar source** | **OPEN** — `BLOCKER_CALENDAR_SOURCE`, needs the 2026–27 CSV |

`INTERNAL_PILOT_REACCEPTANCE = BLOCKED_ON_CALENDAR_DATA`

Once the calendar is supplied and imported, re-acceptance can run the full
plan. Carry into it: the three residual analyzer observations in §2, and the
Stage G UI gap (still manual — no test target, no specs, no browser
framework), which the previous report already documents.

---

## 8. Artifacts

`outputs/internal-pilot-blocker-fixes/` — `manifest.json`,
`intent-live-probes.json`, `test-isolation-results.json`,
`calendar-status.json`, `freeze-amendment.json`.

No secret value and no raw PII. The previous acceptance artifacts and report
were left untouched.
