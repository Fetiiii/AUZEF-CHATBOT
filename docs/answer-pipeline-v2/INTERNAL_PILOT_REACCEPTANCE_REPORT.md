# Internal Pilot — Re-Acceptance Report

**Parent freeze:** `c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6` — immutable
**Amendment:** `INTERNAL_PILOT_FREEZE_AMENDMENT_1` · `9d221c3cf94ffd5039670e74b6272b078106f793c06f98fe649557ca21ec8485`
**Selector:** `variant_a_v1` · `1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e` — unchanged

> ## `INTERNAL_PILOT_REACCEPTANCE = FAIL`
> ## `INTERNAL_PILOT_RELEASE_CANDIDATE = BLOCKED`
>
> **The infrastructure is healthy and the pipeline now works end to end** —
> 41/41 scenarios ran, 42/44 turns reached Selector Variant A, zero selector
> errors, zero 5xx. But two capabilities the architecture documents are
> **systematically non-functional in live traffic**, and the acceptance plan
> names exactly these patterns as release blockers.

The previous acceptance (`INTERNAL_PILOT_ACCEPTANCE_REPORT.md`,
`ACCEPTANCE = FAIL`) is historical and was **not rewritten**.

---

## 1. The two hard blockers

### `SEMANTIC_ACCEPTANCE_ISSUE_MULTI_NEVER_PRODUCED`

**Frequency: 0 of 44 turns produced `intent_count = 2`. 0 of 3 dedicated
`multi_intent` scenarios.** Not occasional — total.

| Scenario | Message shape | `intent_count` |
| --- | --- | --- |
| IP-E2E-036 | "Harç ne kadar **ve** nasıl ödeyeceğim?" | 1 |
| IP-E2E-037 | "Sınav tarihleri ne zaman, **bir de** öğrenci belgesi nasıl alınır?" | 1 |
| IP-E2E-038 | "Kayıt dondurma nasıl yapılır? **Ayrıca** askerlik erteleme…" | 1 |

IP-E2E-037 in particular pairs a calendar question with an unrelated document
question — two plainly independent needs. Each was answered as a single
intent, so the second need can go unanswered.

### `SEMANTIC_ACCEPTANCE_ISSUE_CONTEXT_NEVER_USED`

**Frequency: 0 of 44 turns set `context_used = true`**, including all three
multi-turn scenarios whose second turn is a deliberate elliptical follow-up
("Peki başvuru için hangi belgeler lazım?", "Peki nereden gireceğim?").

Multi-turn plumbing *works* — `conversation_id` chains correctly and the
second turn is delivered — but the analyzer never resolves the reference, so
a follow-up is interpreted as a standalone question.

### Honest attribution

Both behaviours were already visible as single-probe observations in the
blocker-fix round and were explicitly carried forward for this run to
measure. They are now measured, and the answer is "never".

**The prompt hardening I applied in the previous task is a plausible
contributor.** To satisfy the parser's `context_used=false ⇒ resolved_text ==
normalized_text` rule I added an emphatic instruction to copy
`normalized_text` verbatim. That may bias the model toward the no-op branch,
which also means never claiming context. I did **not** change the prompt in
this task (the plan forbids it), so this is a hypothesis with a clear next
experiment, not a conclusion.

The `ambiguity → SINGLE` policy was deliberately left untouched.

---

## 2. What works — and it is most of the system

| Signal | Result |
| --- | --- |
| Scenarios executed | **41 / 41** (full Stage E scope) |
| Logical user turns | 44 |
| HTTP 5xx | **0** |
| Analyzer valid output | **42 / 44** (95.5 %) |
| Turns reaching `NORMAL_LLM` | **42 / 44** |
| Selector invoked | **42** |
| Selector `SELECT` / `NONE` | 35 / 7 |
| Selector errors (provider, parser, timeout) | **0** |
| Degraded turns | 2 |

The pipeline the previous acceptance could never exercise now runs fully:
client → session → Intent Analyzer → Calendar decision → retrieval →
eligibility → Selector Variant A → curated answer.

**Residual analyzer invalid output: 2/44** (IP-E2E-003, IP-E2E-017). Both
degraded safely and still returned an answer. Down from 14/14 at the original
acceptance, but not zero — a non-blocking follow-up.

### Expected-`NONE` behaves correctly

3 of 4 `valid_none` scenarios returned semantic `NONE`, and `NONE` did **not**
trigger degraded fallback or get classified as a provider error
(`degraded_turns` counts only the 2 analyzer failures). IP-E2E-027
(`"asdfgh"`) returned `SELECT` instead of `NONE` — one isolated quality miss,
explicitly *not* an infrastructure failure.

---

## 3. Calendar

### Primary data — validated

| Field | Value |
| --- | --- |
| Records | 19, **all `2026-2027`** |
| By term | GUZ 10 · BAHAR 8 · GENERAL 1 |
| Configured current period | `2026-2027` / `GUZ` — matches the data |
| Reference date | 2026-09-20 (2026–27 Güz begins 21.09.2026) |
| Calendar V2 retrieval | **verified live** |

Retrieval returns real current records, e.g. *"Güz dönemi final sınavları ne
zaman?"* → 1 candidate (`Bitirme Sınavı (Final)`), `no_match_reason: null`.

### Aliases — intentionally pending, **not a blocker**

```
Calendar primary data = populated
Calendar aliases      = intentionally pending
Alias owner           = authorized AUZEF staff
Alias status          = non-blocking operational follow-up
```

0 of 19 records carry aliases. This is by design: the data owner left them
empty for authorized AUZEF staff to enrich. **Nothing was generated, guessed,
LLM-filled or backfilled**, and this is excluded from the blocker count.

### Two calendar observations (neither a blocker)

1. **`calendar_relevant` rarely fires** — only 1 of 4 `calendar_relevant`
   scenarios set it true. All four were still answered (`SELECT`, source
   `llm`), so users got answers, but the deterministic calendar route was
   mostly bypassed. Given the MULTI/context findings this looks like the same
   conservative-analyzer pattern; recorded as a quality observation.
   `calendar_irrelevant` scenarios correctly did **not** flag calendar, and
   no spurious calendar candidates appeared.
2. **One record has a non-zero-padded day** — id 15, `4.06.2027`. It renders
   correctly and Calendar V2 filters on year/term/event rather than parsed
   dates, so there is **no functional impact**. Reported for the data owner;
   **not modified by this run**.

No calendar data was changed, and no date, event or alias was invented.

---

## 4. Stage results

| Stage | Status | Summary |
| --- | --- | --- |
| **A** Runtime/config | **PASS** | Verified in-container: variant_a_v1 / `1aed5688…`, openrouter, gpt-4o-mini, temp 0, max_tokens 32, reasoning unset (`{}` request fields), LLM enabled, calendar 2026-2027/GUZ |
| **B** Automated regression | **PASS** | Full backend + deploy/release with the pilot `.env` in place |
| **C** Docker/services | **PASS** | 5/5 up, db+backend healthy, no restart loops; Meili 326 docs, Qdrant 3020 points, both searchable |
| **D** Offline regression | **PASS** | Frozen prompt/contract assertions hold; freeze, amendment and Variant A fingerprints unchanged; no new benchmark call |
| **E** Live E2E | **FAIL** | Infrastructure healthy; two systematic semantic failures (§1) |
| **F** Failure paths | **PASS** | All four injections graceful; all state restored |
| **G** UI | **MANUAL_REQUIRED** | No test target, no specs, no browser framework |
| **H** Observability | **PASS** | All required fields in 44/44 joined traces |

### Stage F — failure paths

| Test | Result |
| --- | --- |
| F1 admin LLM OFF | `ADMIN_DEGRADED`, `llm_disabled_or_unavailable`, no LLM call, answered from Meili · restored |
| F2 OpenRouter failure | Invalid sentinel key → `REQUEST_DEGRADED`, `intent_analyzer_model_error`, HTTP 200, no stack trace to the user · real key restored |
| F3 Meili unavailable | `meili: unavailable`, `qdrant: available` — request still answered via the LLM path · restored |
| F4 Qdrant unavailable | Graceful curated no-answer + suggestions, HTTP 200 · restored |

F3 is the clean live proof of the availability distinction: one source down,
the other up, correctly reported side by side.

---

## 5. Observability — fully verified

Every required field was present in **44/44** joined traces (schema
version 7): `conversation_id`, UTC `timestamp`, `retrieval_ms`, per-source
availability, intent mode, `context_used`, `calendar_relevant`, candidate
count, selector provider/model/decision, degraded flag and reason, latencies
and error state.

**Availability semantics confirmed live**, all three states observed:

| Source | States seen |
| --- | --- |
| calendar | `available`, `skipped` |
| meili | `available`, `unavailable` (Stage F3) |
| qdrant | `available`, `unavailable` (Stage F4) |

**`0 results != unavailable` confirmed 5 times** — Meili answered with zero
candidates and stayed `available`.

**Privacy:** no API key, authorization header, identity number, phone number
or verification code appears in any trace or artifact; verified by scanning
the artifacts for every secret in `.env`. Answer previews are truncated and
raw conversations were not copied out.

---

## 6. Performance

| Metric | avg | median | p95 | max |
| --- | --- | --- | --- | --- |
| Total | 3641.9 ms | 3661.0 ms | 4334.0 ms | 6045.2 ms |
| Intent Analyzer | 1931.2 ms | 1949.5 ms | 2337.4 ms | 2445.3 ms |
| `retrieval_ms` | 280.6 ms | 209.0 ms | 504.0 ms | 2045.0 ms |
| Selector | 1484.4 ms | 1465.4 ms | 1771.5 ms | 2053.4 ms |

44 turns on a developer host. **These are not production SLA figures** and no
latency gate is asserted. The two LLM calls dominate (~3.4 s of ~3.6 s).

---

## 7. Manual UI checklist (Stage G)

No automation exists and none was installed. Before the pilot opens, a human
must confirm at `http://localhost`:

- [ ] Widget opens on the page
- [ ] Message sends
- [ ] Loading state appears and clears
- [ ] Answer renders correctly (formatting, links)
- [ ] Conversation continues across turns
- [ ] Refresh behaves per the documented history policy
- [ ] Error state is usable (friendly message, never a stack trace)
- [ ] Basic mobile viewport (~375 px) acceptable

---

## 8. Verdict

Per the gate, Stage E must PASS, and the plan names "MULTI never works" and
"context never works" as release blockers. Both are literally true at 0/44.

**Hard blockers: 2**

1. `SEMANTIC_ACCEPTANCE_ISSUE_MULTI_NEVER_PRODUCED`
2. `SEMANTIC_ACCEPTANCE_ISSUE_CONTEXT_NEVER_USED`

**Non-blocking follow-ups:** calendar alias enrichment (authorized AUZEF
staff), Stage G manual UI, `calendar_relevant` firing 1/4, residual analyzer
invalid output 2/44, the id-15 date formatting, and the known `IP-KI-1` /
`IP-KI-2` limitations.

**Suggested next step** (a separate, reviewed change): revisit the analyzer
prompt's context/MULTI guidance — the verbatim-copy instruction added for the
`resolved_text` contract is the prime suspect — then re-run Stage E. Nothing
else in the stack is blocking.

**Not blockers** (backlog, unchanged): 7C, 7D, 7E, 7F, calendar alias
enrichment, independent final production validation, Phase 7G.

---

## 9. Artifacts

`outputs/internal-pilot-reacceptance/` — `manifest.json`,
`stage-results.json`, `e2e-results.jsonl`, `e2e-trace-join.json`,
`failure-path-results.json`, `observability-results.json`,
`performance-summary.json`, `calendar-validation.json`,
`service-health.json`.

No secret value and no raw PII. The historical acceptance report and its
artifacts were left untouched.
