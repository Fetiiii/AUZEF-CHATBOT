# Answer Pipeline V2 — Phase 7B Semantic Rescore Report

## Status

```text
Phase 7B-Semantic Rescore — PASS
Phase 7B-Prompt HOLDOUT Live — WAITING_FOR_HUMAN_VARIANT_SELECTION
```

- **ZERO LIVE CALLS.** Only saved outputs were re-scored:
  - Stage A production (run `9dfc72c140dc7e93`, 137 cases);
  - Variant A DEV (`f47cf095e441a6e8`);
  - Variant B DEV (`28d38907daa7cd20`);
  - offline first-candidate predictions.
- **Nothing else changed:** model outputs, candidates, candidate order,
  prompts (manifest unchanged) and the prompt gate definition. Only the
  expectations changed, from Semantic Gold V1 `36ab1d6f…`.
- **Artifacts:** `outputs/selector-v2-benchmark/semantic-rescore-v1/`
  (`semantic-rescore.json`, `semantic-case-diffs.json`).

## Headline: the Gold change moved everything, the prompt moved it further

| DEV (net corrections vs first candidate) | Old Gold, 95 cases | Old Gold, same 77 cases | Semantic Gold, 77 cases |
|---|---:|---:|---:|
| Production | −48 | −33 | **+5** |
| Variant A | −28 | −19 | **+18** |
| Variant B | −37 | −26 | **+10** |

**Decomposition:**

- **The Gold cleanup alone** moves production from −33 to +5 on the same 77
  cases. **Production now clears `net_corrections ≥ 0` by itself.**
- **The prompt adds** +13 (A) or +5 (B) on top of that.
- **A's advantage over production shrank:** it was +20 under the old Gold
  and is +13 under the Semantic Gold.
- **Gate strength:** the gate was calibrated against a −48 baseline. Its
  "net ≥ 0" condition is now much less discriminating. This is noted for
  human review only; the gate was **not** changed.

## First-candidate: the alias advantage collapses

| | Old Gold | Semantic Gold |
|---|---:|---:|
| Full primary benchmark | 462/482 (0.959) | **415/461 (0.900)** |
| DEV (95 → 77 evaluable) | 82/95 (0.863) | **46/77 (0.597)** |
| DEV, same 77 cases under the old Gold | 66/77 (0.857) | 46/77 (0.597) |

**The ordering inverts on DEV:** production (0.662) and both variants now
beat the retrieval-rank-1 heuristic. The drop is concentrated where the human
rejected alias-owner Golds (27 old Golds rejected, 25 of them alias-derived).
It quantifies the structural alias advantage flagged in Phase 7A.

## Stage A production (137 challenge cases)

| | Old Gold | Semantic Gold |
|---|---|---|
| Evaluable | 137 | 115 (22 excluded or held) |
| Exact | 51/137 (0.372) | **75/115 (0.652)** |
| SELECT correct | 51 | 74 |
| Expected NONE correct | — (0 cases) | 1/1 |
| False NONE | 41 | 30 |
| Wrong SELECT | 45 | 10 |
| Invalid, error, timeout | 0 | 0 |

## DEV re-score (77 semantic-evaluable of the 95 DEV cases)

**Excluded in DEV:** 8 ambiguous, 1 content review, and 9 retrieval/KB
holds. They are outside every denominator.

| Semantic Gold DEV | Production | Variant A | Variant B | First-candidate |
|---|---:|---:|---:|---:|
| Exact | 51/77 (0.662) | **64/77 (0.831)** | 56/77 (0.727) | 46/77 (0.597) |
| Rescue (baseline wrong → right) | 19/31 | 21/31 | 18/31 | — |
| Corruption (baseline right → wrong) | 14/46 | **3/46** | 8/46 | — |
| Preserve / unresolved | 32 / 12 | 43 / 10 | 38 / 13 | — |
| Net corrections | +5 | **+18** | +10 | — |
| NONE output (= false NONE unless on case 412) | 21 (20 false) | 1 (1 false) | 12 (12 false) | — |
| Near-QnA (Phase 7A slice, 44 evaluable) | 31/44 | 36/44 | 34/44 | |
| Easy control (15 evaluable) | 8/15 (corruption 6) | 15/15 (0) | 11/15 (3) | |

**Old vs semantic exact** (like-for-like column included):

| | Old Gold, 95 | Old Gold, same 77 | Semantic, 77 |
|---|---:|---:|---:|
| Production | 34 | 33 | 51 |
| Variant A | 54 | 47 | 64 |
| Variant B | 45 | 40 | 56 |
| First-candidate | 82 | 66 | 46 |

### General/specific (Phase 7A slice membership; scored under the Semantic Gold)

| | Production | A | B |
|---|---:|---:|---:|
| Phase 7A "expected general" (8) | 6/8 | 6/8 | 6/8 |
| Phase 7A "expected specific" (3) | 1/3 | 2/3 | 1/3 |

- **Production-correct cases broken by a variant:** none, for A or B.
- **Slice membership is from Phase 7A:** the human now expects the
  *specific* candidate for 78–85, where the user stated "ikinci
  üniversite" / "sınavsız". That is why all three score higher on the
  "general" slice.
- **471 and 472** (the clear unstated-qualifier errors) are in HOLDOUT. There
  are still **no live A/B outputs** for them.

### Review-derived slices (DEV, evaluable only)

| Slice | Production | A | B |
|---|---:|---:|---:|
| KEEP_CURRENT (29) | 13 | 23 | 19 |
| CHANGE_GOLD (20) | 13 | 16 | 13 |
| MULTI_ACCEPTABLE (7) | 4 | 5 | 5 |
| Old Gold rejected (19) | 14 | 15 | 13 |
| EXPECT_NONE (1, case 412) | 1 | 0 | 0 |

### Expected NONE (n = 1, diagnostic only)

- **Outcomes on case 412** (DEV): production answered NONE, which is
  correct. Variant A and B both SELECTed (FALSE_SELECT).
- **Limitation:** with one case, NONE precision and recall **cannot be
  estimated**.
- **Warning sign:** Variant A emits NONE only once in 77 cases. It fixed the
  over-strict NONE, but may have overshot toward "never NONE". That matters
  for the Phase 4/5 semantic-NONE contract before any production
  consideration, but this dataset cannot measure it.

### Paired comparisons (semantic DEV, 77, exact McNemar)

| Pair | Both correct | Only left | Only right | Both wrong | p |
|---|---:|---:|---:|---:|---:|
| Production vs A | 49 | 2 | 15 | 11 | 0.0023 |
| Production vs B | 48 | 3 | 8 | 18 | 0.227 |
| A vs B | 56 | 8 | 0 | 13 | 0.0078 |

Production-correct → variant-wrong cases: A 2 (including 412), B 3.

## Prompt gate (definition unchanged)

| Check | Variant A | Variant B |
|---|---|---|
| Complete | ✔ | ✔ |
| Net corrections ≥ 0 | ✔ (+18) | ✔ (+10) |
| Corruption < production (14) | ✔ (3) | ✔ (8) |
| False NONE < production (20) | ✔ (1) | ✔ (12) |
| No production-correct general/specific regression | ✔ | ✔ |
| **Result** | **PASS** | **PASS** |

- **Both variants pass, so no winner is chosen automatically.**
- **HOLDOUT status: `WAITING_FOR_HUMAN_VARIANT_SELECTION`.**

**Evidence for the human choice (no verdict):**

- A: 64/77, corruption 3, false NONE 1, net +18.
- B: 56/77, corruption 8, false NONE 12, net +10.
- Paired A vs B: only-A-correct 8, only-B-correct 0, p = 0.0078.
- Watch-outs for A:
  - its NONE rate is very low (see Expected NONE);
  - it misses the single expected-NONE case;
  - the clear-error cases 471/472 are still unmeasured in HOLDOUT.

Selecting a variant for HOLDOUT (42 calls) and running it require a new,
explicit approval.

## Caveats

- **Scope bias.** The reviewed scope came from production failures. The
  blind control (19/20 KEEP) shows no gratuitous rewriting where production
  was right, but it does not bound the opposite direction. The absolute
  semantic scores are therefore **not unbiased estimates**; the A-vs-B
  comparison is the least confounded.
- **Unreviewed cases.** They keep their alias-derived Gold. The Semantic Gold
  is partial by design.
- **HOLDOUT has not been re-scored for a decision.** No live calls were made.
  Stage B is not started, and Phase 7C is not started.

## Tests

```text
test_selector_semantic_gold.py (new):   9 passed (includes the real-lock / real-gold checks)
Full backend suite (repository-root):   517 passed, 0 failed (508 → +9)
```

**Coverage:**

- lock: validation and immutability;
- audit inaccessible before the lock;
- deterministic label mapping, cross-checked;
- parent unchanged, deterministic child;
- outcomes and provenance;
- EXPECT_NONE scored correctly;
- MULTI_ACCEPTABLE scored correctly;
- retrieval/KB review excluded and never scored as NONE;
- content review and ambiguous cases excluded from denominators;
- saved outputs reused and unmodified;
- prompt and gate definitions unchanged;
- production registry config untouched;
- a network guard.
