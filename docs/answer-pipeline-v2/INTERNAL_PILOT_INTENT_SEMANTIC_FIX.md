# Internal Pilot — Intent Analyzer Semantic Blocker Fix

**Historical parent freeze:** `c7081ff54d959d183e7964498720dce7866461e72c3fa407c7346ea3cf32c5c6` — immutable
**Parent amendment 1:** `9d221c3cf94ffd5039670e74b6272b078106f793c06f98fe649557ca21ec8485` — immutable
**Amendment 2:** `INTERNAL_PILOT_FREEZE_AMENDMENT_2` · `2c9f2ea99ae9a44f88ae1623a2bb753abf212c17d5a6bef59ec56f81a99fed50`
**Selector:** `variant_a_v1` · `1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e` — **unchanged**

> ## `INTENT_ANALYZER_SEMANTIC_SCREEN = FAIL`
> ## `FULL_REACCEPTANCE = BLOCKED`
>
> **MULTI is fully restored: 0/3 → 4/4.** Context is partially restored but
> still misses its gate at 1/4, and two probes produced invalid output. Per
> the plan I am reporting the failure taxonomy rather than automatically
> entering a chain of prompt variants.

Historical `ACCEPTANCE = FAIL` and `RE-ACCEPTANCE = FAIL` artifacts were
**not rewritten**.

---

## 1. Historical blocker evidence

From the re-acceptance run (41/41 scenarios, 44 turns):

| Capability | Result |
| --- | --- |
| MULTI | **0/44 turns**, 0/3 `multi_intent` scenarios |
| Context | **`context_used=true` 0/44**, 0/3 context-required follow-ups |

Everything else was healthy: 0 HTTP 5xx, 42/44 `NORMAL_LLM`, 42 selector
invocations, 0 selector errors.

---

## 2. Context input plumbing — proven FIRST

Before touching any prompt, the actual sanitized analyzer input was verified
on two real conversations.

```
CONTEXT_INPUT_PLUMBING = PASS
```

| Check | Result |
| --- | --- |
| Current user turn present | ✅ |
| Previous USER turn(s) present | ✅ 2 supplied |
| Chronological order (oldest first) | ✅ |
| Max previous USER turns ≤ 2 | ✅ |
| BOT messages absent | ✅ stored as `[user, bot, user, bot]`, loader returned `[user, user]` |
| Same conversation/session chain | ✅ |

A live two-turn conversation confirmed it end to end: turn 2 carried
`context_message_count: 1`, `previous_user_context_count: 1` — **the previous
USER turn reached the analyzer, and the analyzer still returned
`context_used=false`.**

**The plumbing was never the problem.** No context-assembly code was changed.

---

## 3. Root cause — prompt instruction behaviour

Two instructions in the analyzer prompt were suppressing the capabilities:

1. **The verbatim rule was unscoped in effect.** Amendment 1 had added, to fix
   the `resolved_text` contract: *"context_used false ise resolved_text …
   BİREBİR AYNI olmalıdır: normalized_text'i aynen kopyala"*, followed by an
   emphatic list of prohibitions. Read as a whole it behaved like an
   unconditional "never change the text" rule, and the context branch — which
   *requires* `resolved_text` to differ — was collapsed into it.
2. **`Belirsizlikte SINGLE üret` led the prompt**, before MULTI was even
   defined, and nothing said that `intent_count` is independent of whether the
   text changed. Combined with (1)'s "change nothing" pressure, MULTI died.

---

## 4. The minimal fix

Only `build_intent_analyzer_prompt` changed. **No parser, no contract, no
policy, no context assembly.**

### Context — explicit decision order, verbatim rule scoped

```
1) Is the current user message reliably understandable on its own?
2) If yes  → context_used = false, resolved_text = normalized_text EXACTLY
3) If no — pronoun, ellipsis, implicit subject, prior-turn reference — and
   previous_user_turns resolve it → context_used = true
4) Then resolved_text adds ONLY the needed information from the previous USER
   turn to make the intent self-contained; no gratuitous paraphrase
```

Stated explicitly: **verbatim equality is mandatory ONLY when
`context_used = false`; when context is genuinely required it is not.** A
counter-instruction was also added so the model does not use context merely
because history is visible.

### MULTI — decided by goal count, not by text change

> `intent_count` is determined **only** by how many genuinely independent,
> separately answerable goals the current turn contains. It has **nothing** to
> do with whether `normalized_text` or `resolved_text` changed — the text may
> be untouched and `intent_count` still be 2.

SINGLE boundaries are enumerated: same goal twice, one goal + qualifier, one
goal with several details, a single request joined by a conjunction, an
ambiguous possible second goal. `Belirsizlikte SINGLE üret` is preserved, now
positioned as the tie-breaker rather than the headline. Max 2 intents, and 3+
apparent goals still collapse to one intent.

### Examples are synthetic

Two minimal non-benchmark examples ("Kütüphane saatleri nedir ve spor salonu
nerede?"; previous "Kütüphane hakkında bilgi almak istiyorum" / current
"Saatleri nedir?"). A test asserts no E2E catalog text and no `471`/`472`/
`IP-E2E` string appears in the prompt.

### Contract preservation — verified

| Guarantee | Status |
| --- | --- |
| `intent_count: Literal[1, 2]`, `strict=True`, `extra=forbid` | unchanged |
| `intent_count` must be a JSON integer; `"1"` rejected | unchanged |
| `context_used=false ⇒ resolved_text == normalized_text` | unchanged |
| Max 2 intents · ambiguity → SINGLE · max 2 previous USER turns · bot excluded | unchanged |
| Selector / model / retrieval / calendar policy | unchanged |

---

## 5. Live semantic screen — 14 probes, plan frozen before results

Provider `openrouter`, model `openai/gpt-4o-mini`, frozen analyzer config.
Distribution exactly as mandated; probe texts were fixed in
`prelive-probe-plan.json` **before** the run and not edited afterwards.

| Gate | Required | Result | Status |
| --- | --- | --- | --- |
| Operational | 14/14 valid, 0 invalid, 0 provider errors, 0 timeouts | 12/14 valid, **2 invalid**, 0 errors, 0 timeouts | **FAIL** |
| MULTI | 4/4 `intent_count = 2` | **4/4** | **PASS** |
| Context-required | 4/4 `context_used = true` | **1/4** | **FAIL** |
| Context controls | 4/4 `context_used = false`, verbatim | **4/4** | **PASS** |
| Calendar relevant | true | true | **PASS** |
| Calendar irrelevant | false | false | **PASS** |

### Failure taxonomy

| Probe | Failure mode | Detail |
| --- | --- | --- |
| C2 | `CONTEXT_CLAIMED_WITHOUT_RESOLUTION` | Set `context_used=true` but left `resolved_text == normalized_text`; the strict parser correctly rejected it |
| C4 | `CONTEXT_CLAIMED_WITHOUT_RESOLUTION` | Same |
| C3 | `CONTEXT_NOT_DETECTED` | Elliptical turn ("Hangi belgeler gerekiyor?") treated as self-contained; `context_used` stayed false |

C1 succeeded cleanly: previous *"Yatay geçiş hakkında bilgi almak istiyorum"*
+ current *"Şartları neler?"* → `context_used=true`, `resolved_text`
*"Yatay geçiş şartları neler?"* — correct, self-contained, using only
supported tokens.

### What this means

The suppression is **partially lifted**: the model now *attempts* context
resolution, which it never did before. It fails in two distinct ways — it
claims context without doing the rewrite (C2, C4), or it misses the ellipsis
entirely (C3). Both are addressable, but per the plan I did **not**
automatically iterate through further prompt variants.

The trade-off is honest: the screen's invalid-output rate went from 0/8 (with
context dead) to 2/14. In production those turns fall back safely to the
deterministic degraded path, but the operational gate requires 0.

---

## 6. Calendar

Not this task's blocker and **not changed**. The controls confirm the prompt
edit did not disturb it: calendar-relevant → `true`, calendar-irrelevant →
`false`.

Calendar aliases remain **0 by design**, owned by authorized AUZEF staff,
**not a blocker**, and nothing was generated or modified.

---

## 7. Freeze amendment 2

| Field | Value |
| --- | --- |
| Id | `INTERNAL_PILOT_FREEZE_AMENDMENT_2` |
| Fingerprint | `2c9f2ea99ae9a44f88ae1623a2bb753abf212c17d5a6bef59ec56f81a99fed50` |
| Parent amendment 1 | `9d221c3cf94ffd…` — immutable |
| Historical parent freeze | `c7081ff54d959d…` — immutable |
| Analyzer prompt fingerprint | `112fa46942d8fdff3edf25a6e02ac3ccfbc020b201ac0acbd964ff91a6bdf493` |
| Classification | intent-analyzer semantic bug fix = **YES**; selector / model / retrieval / calendar policy / parser contract / analyzer policy = **NO** |
| Context assembly changed | **No** |

The fingerprint excludes `created_at`, `git_commit` and the live screen
result, so it identifies the frozen declaration rather than the measurement.
Tests assert determinism, the parent chain, and that amendment 1 keeps its
**historical** prompt fingerprint rather than tracking the live one.

---

## 8. Tests

| Suite | Result |
| --- | --- |
| Targeted analyzer contract / context / MULTI / amendment / preflight | 186 passed |
| Full backend + deploy/release, pilot `.env` present | **758 passed, 0 failed** (was 736; +22 new) |

New coverage: context decision order, verbatim scoping, resolved-text
acceptance and rejection paths, only-last-two-USER-turns, bot exclusion,
MULTI goal-count decoupling, SINGLE boundaries, synthetic-example guard,
amendment-2 determinism/classification/parent chain, selector-unchanged.

---

## 9. Remaining blockers and readiness

**Engineering blockers: 1** — context resolution, with two named failure
modes (`CONTEXT_CLAIMED_WITHOUT_RESOLUTION`, `CONTEXT_NOT_DETECTED`).

**Resolved this task:** `MULTI_NEVER_PRODUCED` — now 4/4.

```
INTENT_ANALYZER_SEMANTIC_SCREEN = FAIL
FULL_REACCEPTANCE = BLOCKED
```

The full 41-scenario Stage E was **not** re-run, as required while the screen
is failing.

**Suggested next step** (a separate, reviewed change): tighten only the
context branch — make the rewrite obligation the same sentence as the flag,
so `context_used=true` and a differing `resolved_text` cannot be chosen
independently, and strengthen ellipsis detection for bare noun-phrase
questions like "Hangi belgeler gerekiyor?". Then re-run this same frozen
14-probe screen before spending the 41-scenario run.

**Not blockers** (unchanged): calendar alias enrichment, Stage G manual UI,
7C–7F, independent final production validation, Phase 7G.

---

## 10. Artifacts

`outputs/internal-pilot-intent-semantic-fix/` — `manifest.json`,
`context-plumbing-evidence.json`, `prelive-probe-plan.json`,
`live-probe-results.jsonl`, `semantic-screen-metrics.json`,
`freeze-amendment-2.json`.

No secret value and no raw PII.
