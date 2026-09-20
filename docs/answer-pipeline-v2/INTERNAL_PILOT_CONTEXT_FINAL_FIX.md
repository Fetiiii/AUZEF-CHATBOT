# Internal Pilot — Context Resolution Final Fix

**Historical freeze:** `c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6` — immutable
**Amendment 1:** `9d221c3cf94ffd5039670e74b6272b078106f793c06f98fe649557ca21ec8485` — immutable
**Amendment 2:** `2c9f2ea99ae9a44f88ae1623a2bb753abf212c17d5a6bef59ec56f81a99fed50` — immutable
**Amendment 3:** `INTERNAL_PILOT_FREEZE_AMENDMENT_3` · `429dc8c0f44f3969a09ef04b6a46363959aa4c37354a3f7394a8896d6511bbce`
**Selector:** `variant_a_v1` · `1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e` — **unchanged**

> ## `INTENT_ANALYZER_SEMANTIC_SCREEN = FAIL`
> ## `FULL_REACCEPTANCE = BLOCKED`
>
> **Failure class 1 is fully fixed.** The operational gate is clean for the
> first time — 14/14 valid, 0 invalid — and every probe that claims context
> now produces a genuinely self-contained rewrite. **Failure class 2 remains
> on one probe**, so the context-required gate misses at 3/4.

---

## 1. Previous screen vs. this screen

Same frozen 14 probes, same distribution, **same texts** — verified
byte-identical to the previous plan. No case added, removed or edited.

| Gate | Previous | Now | Status |
| --- | --- | --- | --- |
| Operational (valid / invalid) | 12/14 · **2 invalid** | **14/14 · 0 invalid** | **PASS** ✅ |
| MULTI | 4/4 | **4/4** | **PASS** ✅ |
| Context-required | 1/4 | **3/4** | **FAIL** ❌ |
| Context-required self-contained | 1/4 | **3/4** | **FAIL** ❌ |
| Context controls | 4/4 | **4/4** | **PASS** ✅ |
| Calendar relevant / irrelevant | true / false | **true / false** | **PASS** ✅ |

Provider errors 0, timeouts 0.

---

## 2. Failure classes

### Class 1 — `CONTEXT_CLAIMED_WITHOUT_RESOLUTION` → **FIXED (2 → 0)**

Previously the model set `context_used=true` while leaving `resolved_text`
referential, which the strict parser correctly rejected — that was the entire
source of the 2 invalid outputs. Both probes now resolve properly:

| Probe | Previous turn | Current | Resolved |
| --- | --- | --- | --- |
| C1 | Yatay geçiş… | Şartları neler? | *Yatay geçiş şartları neler?* |
| C2 | Bütünleme sınavı… | Ne zaman yapılıyor? | *Bütünleme sınavı ne zaman yapılıyor?* |
| C4 | Mezuniyet işlemleri… | Ne kadar sürüyor? | *Mezuniyet işlemleri ne kadar sürüyor?* |

All three are readable standalone — the semantic criterion, not a mechanical
"must differ" rule.

### Class 2 — `CONTEXT_NOT_DETECTED` → **PARTIALLY FIXED (3 → 1)**

**C3 still fails.** Previous turn *"İkinci üniversite kaydı yaptırmak
istiyorum"*, current *"Hangi belgeler gerekiyor?"* → `context_used=false`,
`resolved_text` unchanged. The document-set referent is only identifiable
from the previous USER turn, so this should have been `true`.

This is the sole remaining engineering blocker.

---

## 3. The minimal prompt change

**Context branch only.** No parser, no contract, no policy, no context
assembly, and the MULTI wording was not touched.

### Class 1 — the flag and the rewrite are one decision

> Setting `context_used = true` *means* rewriting `resolved_text` so the
> request is understandable without the previous turns. **These are one
> decision and cannot be chosen separately.** Never set `context_used = true`
> while leaving `resolved_text` unresolved or referential.

The criterion is stated as semantic, not mechanical: *someone reading
`resolved_text` alone must be able to tell what is being asked.* No
meaningless "must always differ" rule was introduced.

### Class 2 — a generic self-contained test

> A question is **not** self-contained when its referent — noun, subject,
> object, process, application, document set, date, fee, duration or place —
> can only be determined from the previous USER turns. If a qualifier such as
> *şartı / belgesi / ücreti / süresi / tarihi / yeri* does not say **which
> topic** it belongs to, the referent is missing. The same applies to
> pronouns, ellipsis, and short "peki …?" follow-ups.

One synthetic example (previous *"Staj başvurusu yapmak istiyorum"*, current
*"Nereden yapılıyor?"*). **No frozen probe text was copied into the prompt** —
a test asserts every probe sentence, previous and current, is absent.

### Anti-over-trigger — protecting the 4/4 controls

> Being short does not by itself mean context-dependent. If the current
> message carries its own referent, `context_used` stays `false` and
> `resolved_text = normalized_text` even when history is visible.

That guard held: context controls stayed **4/4**.

### Decision order

```
1. Is the current message self-contained without looking at previous turns?
2. If yes  → context_used = false, resolved_text = normalized_text
3. If no   → inspect previous USER turns; if they uniquely resolve the
             referent → context_used = true and rewrite resolved_text
4. If they do not resolve it → context_used = false; never invent context
```

MULTI remains an independent decision.

---

## 4. Contract and policy preservation

| Guarantee | Status |
| --- | --- |
| `Literal[1, 2]`, `strict=True`, `extra=forbid` | unchanged |
| `context_used=false ⇒ resolved_text == normalized_text` | unchanged |
| `context_used=true ⇒ resolved_text actually resolves the reference` | unchanged |
| No silent coercion or normalization | none added |
| ambiguity → SINGLE · MULTI max 2 · calendar semantics · normalization · bot exclusion · max 2 previous USER turns | unchanged |
| Context assembly code | untouched |
| Selector / model / retrieval / calendar | untouched |

A useful detail confirmed by test: `resolved_text` tokens must come from
`normalized_text` or the previous turns, so resolutions are near-concatenations
and cannot smuggle in new connectors. The live resolutions above satisfy this.

---

## 5. Freeze amendment 3

| Field | Value |
| --- | --- |
| Id | `INTERNAL_PILOT_FREEZE_AMENDMENT_3` |
| Fingerprint | `429dc8c0f44f3969a09ef04b6a46363959aa4c37354a3f7394a8896d6511bbce` |
| Parent (amendment 2) | `2c9f2ea99ae9a4…` — verified from the artifact, immutable |
| Amendment 1 / historical freeze | `9d221c3cf94ffd…` / `c7081ff54d959d…` — immutable |
| Analyzer prompt fingerprint | `4e1583f8c06c8369eeda08dd1025bbb0db8f50977e4b902873fd07fcd174cecf` |
| Classification | context semantic bug fix = **YES**; model / selector / retrieval / calendar / parser contract / context assembly / MULTI rule = **NO** |

The fingerprint excludes `created_at`, `git_commit` and the live screen, so it
identifies the declaration rather than the measurement. Amendment 2 now keeps
its **historical** prompt fingerprint; a test asserts it no longer tracks live.

---

## 6. Calendar

Unchanged and unaffected — controls confirm relevant → `true`, irrelevant →
`false`. Calendar data untouched. Aliases remain **0 by design**, owned by
authorized AUZEF staff, **not a blocker**, and nothing was generated.

---

## 7. Remaining blockers and readiness

**Engineering blockers: 1** — `CONTEXT_NOT_DETECTED` on C3.

Resolved across this task and the previous one: `MULTI_NEVER_PRODUCED`
(4/4), `CONTEXT_CLAIMED_WITHOUT_RESOLUTION` (0 occurrences), and the
operational invalid-output regression (now 0).

```
INTENT_ANALYZER_SEMANTIC_SCREEN = FAIL
FULL_REACCEPTANCE = BLOCKED
```

The 41-scenario re-acceptance was **not** run, as required while the screen
fails. No further prompt iteration was attempted.

**Suggested next step** (separate, reviewed): C3's pattern is a *plural
document-set* referent (*"Hangi belgeler gerekiyor?"*) after an intent-stated
previous turn. C1/C2/C4 each attach a qualifier to a **singular** topic noun;
C3 asks for a set with no possessive marker, which is likely why the referent
test did not fire. Strengthening the rule for bare plural noun-phrase
questions — and re-running this same frozen screen — is the narrow next
change.

**Not blockers** (unchanged): calendar alias enrichment, Stage G manual UI,
7C–7F, independent final production validation, Phase 7G.

---

## 7b. Tests

| Suite | Result |
| --- | --- |
| Targeted analyzer / context / MULTI / amendment / preflight | 94 passed |
| Full backend + deploy/release, pilot `.env` present | **776 passed, 0 failed** (was 758; +18) |

New coverage: flag-bound-to-rewrite, self-contained test wording,
anti-over-trigger, semantic-not-mechanical criterion, probe texts absent from
the prompt, reference-dependent resolutions accepted, context invention
rejected, bot-only information cannot resolve, MULTI regression, and
amendment-3 determinism / classification / parent chain.

---

## 8. Artifacts

`outputs/internal-pilot-context-final-fix/` — `manifest.json`,
`prelive-probe-plan.json`, `live-probe-results.jsonl`,
`semantic-screen-metrics.json`, `freeze-amendment-3.json`.

No secret value and no raw PII. Earlier acceptance, re-acceptance and
semantic-fix artifacts were left untouched.
