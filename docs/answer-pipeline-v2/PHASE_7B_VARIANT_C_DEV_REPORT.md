# Answer Pipeline V2 — Phase 7B Variant C (Qualifier Contract) DEV Report

## Status

```text
VARIANT_C_DEV = FAIL
FINAL_VALIDATION = BLOCKED
```

- Branch `production-readiness`; starting HEAD `3b3d34d`.
- **Live calls: 97 logical, OpenRouter only.**

  | Scope | Calls |
  |---|---:|
  | `openai/gpt-4o-mini`, variant_c_v1, DEV | 95 |
  | Known-regression diagnostics (471, 472) | 2 |
  | Production, A, B, old-HOLDOUT full run, neutral order, final validation | 0 each |

- **Result:** the run is valid. The gate fails on two behavioural checks:
  - qualifier improvement;
  - the 471/472 known regressions.

## Why Variant C exists

- **Variant A on the old HOLDOUT (FAIL):**
  - aggregate gain: 30/38 vs production 24/38, with 0 cases lost;
  - failure: both critical cases wrong. Variant A chose "Merkezi yatay
    geçiş…", assuming a qualifier the user never stated.
- **Qualifier postmortem:**
  - verdict **ORDER_BIAS_WEAK**: only 4/21 of A's wrong SELECTs were at
    position 1;
  - A's unstated-qualifier count (heuristic) was higher than production's.
- **Next controlled variable:** the prompt's qualifier contract. Everything
  else stays fixed.

## Part A–D: 471/472 re-adjudication

- **Input:** `review-template-qualifier-filled.csv`, sha256
  `83143689c30627dcf60e88783a91deb8184a5ddf2163a81af5d6157a066dd6b2`.
- **Validation:**
  - 2 rows, ids {471, 472}, both SELECT_ACCEPTABLE, no blank labels;
  - the packet reproduces from the frozen snapshot, fingerprint
    `bf73542d9bb86d8813bb14fac9dfc344cc2d44c777cf7714eedfae9a24ab3537`.
- **Label mapping** (recomputed with the blind hash labelling):
  - 471 label **I** → `qna:129`;
  - 472 label **D** → `qna:129`.
- **Lock:** `qualifier-postmortem/adjudicated/`, fingerprint `a68fb258…`,
  write-once.
- **Reviewer field.** Every row's `reviewer` says **"ChatGPT GPT-5.6 Sol"**.
  The same value appears on all 106 rows of the earlier semantic lock that
  produced Semantic Gold V1. The recorded reviewer is therefore a language
  model, not a person. The label "human review" in this programme should be
  read with that in mind. The decisions are recorded verbatim; this phase did
  not re-review them.

**Semantic Gold V1.1**

- **Location:** `semantic-gold-v1.1/`.
- **Fingerprint:**
  `1d22cac8e20d32e1b32b0433a886dff3bd2c5b658ab0952a93234ce3ed971138`.
- **Parent:** V1 `36ab1d6f…`, verified intact after the build.
- **Outcome:** 471 and 472 are both **KEEP_CURRENT**; the reviewed choice
  equals the existing Gold `qna:129`.
  - No case changed; V1.1 cases are identical to V1.
  - Only the provenance (lock) and the fingerprint are new.
  - The DEV denominator stays 77.

**Historical HOLDOUT:**

- **`VARIANT_A_HOLDOUT = FAIL` is unchanged.** The `semantic-holdout-a/` tree
  sha256 matches the recorded values, and the report was not touched.
- **Post-adjudication note:** the review confirmed the Gold that A failed
  against, so the historical FAIL stands on a reviewed expectation.

## Variant C design

**Prompt:** `prompts/variant_c_v1.md`, fingerprint
**`fc1811443061737b090b388f0b52bb03f68af99a965f2956c4833663847eb9a9`**.

- **Registered:** in the prompt manifest. The A (`1aed5688…`) and B
  (`44893b6b…`) entries are unchanged.
- **Built from A:** C keeps A's role and the short-message tolerance. It
  also keeps the NONE criterion, adding "uncertainty alone is not a NONE
  reason", plus the calendar and output rules.
- **Replaces** A's four selection rules with a six-step qualifier contract:
  1. identify the qualifiers the user explicitly stated;
  2. eliminate candidates on a different topic;
  3. drop a candidate that needs an unstated qualifier when a more general
     candidate meets the same need;
  4. prefer the general candidate when the user speaks generally;
  5. keep the matching specific candidate when the user states the
     qualifier;
  6. judge practical satisfaction, not word overlap.
- **Adds** an explicit rule: specificity, detail, apparent completeness or
  list position is not evidence of intent.
- **No benchmark content:** no case ids, no "yatay geçiş" or "merkezi", no
  qna refs and no digits (test-enforced).

**Fixed variables** (test-verified):

- **Same as A:** model config `af9eb2d0…` (openrouter, gpt-4o-mini,
  reasoning none, temperature 0, max_tokens 32, no retries); original
  retrieval candidate order; serializer and parser; DEV split `0fcb2441…`.
- **Request equality:** the user payloads of C and A are byte-identical;
  only the system prompt differs.

## Pre-live freeze

**Baseline:** `prelive-baseline.json`, fingerprint `392d9a59…`, frozen before
any C call and re-verified after scoring. It holds:

- the V1.1 DEV evaluable ids (77) and the qualifier slice (36);
- the general/specific slices;
- the saved production/A/B blocks and first-candidate;
- the heuristic fingerprint, as the `qualifier_postmortem.py` sha256 plus
  the lexicon fingerprint;
- the DEV gate.

**Plan:** `58116ee6…`, with the call budget below. A full old-HOLDOUT scope
is refused by code.

| Scope | Calls |
|---|---:|
| DEV | 95 |
| Diagnostics (471, 472) | 2 |
| **Maximum total** | **97** |

## Results (Semantic Gold V1.1, 77 semantic-evaluable DEV cases)

| | Exact | Accuracy | False NONE | NONE | Wrong SELECT | Unstated qualifier (DEV 77) | Unstated qualifier (frozen slice 36) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Production | 51 | 0.662 | 20 | 21 | 6 | 2 | 2 |
| Variant A | 64 | 0.831 | 1 | 1 | 11 | 5 | **3** |
| Variant B | 56 | 0.727 | 12 | 12 | 8 | 3 | 2 |
| **Variant C** | **63** | **0.818** | **1** | 1 | 12 | 4 | **4** |
| First candidate | 46 | 0.597 | — | — | — | — | — |

**Expected NONE** (n = 1, case 412): production is correct. A, B and C all
SELECT, so false SELECT on expected NONE is 1 each.

**Unstated-qualifier cases (heuristic):**

| Run | Cases |
|---|---|
| A | 205, 210, 466, 470, 473 |
| C | **371**, 466, 470, 473 |

C fixed 205 and 210 but added 371. 466, 470 and 473 are unchanged: 470 is
still qna:342, and 466 and 473 are still qna:100 "Açık öğretim…".

**Paired comparisons (77):**

| Pair | Both correct | Only left | Only C | Both wrong | Net (C − left) | McNemar p |
|---|---:|---:|---:|---:|---:|---:|
| A vs C | 61 | 3 | 2 | 11 | **−1** | 1.0 |
| Production vs C | 49 | 2 | 14 | 12 | **+12** | 0.0042 |
| B vs C | 55 | 1 | 8 | 13 | +7 | 0.039 |

**Case-level differences:**

| Direction | Cases |
|---|---|
| A-correct → C-wrong | 38, 285, 422 |
| C-correct → A-wrong | 166, 205 |
| Production-correct → C-wrong | 412 (the expected-NONE case), 422 |
| Production-correct → C-wrong, general/specific slices | none |

**General / specific:**

| Slice | Production | A | B | C |
|---|---:|---:|---:|---:|
| Phase 7A general (8) | 6 | 6 | 6 | 6 |
| Phase 7A specific (3) | 1 | 2 | 1 | 2 |
| Inventory general (15) | 9 | 13 | 10 | 13 |
| Inventory specific (12) | 9 | 10 | 10 | 10 |

## Known development regressions 471 / 472 (not validation)

| | 471 "Yatay geçiş nasıl yaparım" | 472 "Yatay geçiş yapmak istiyorum" |
|---|---|---|
| Semantic Gold V1.1 | qna:129 | qna:129 |
| Variant C | SELECT **qna:342** "Merkezi yatay geçiş başvurusu nasıl yapılır?" | SELECT **qna:342** |
| Correct | **no** | **no** |
| Unstated qualifier assumed | **yes** | **yes** |
| Saved production / A | qna:342 / qna:342 | qna:342 / qna:342 |

Even with an explicit, generic "do not assume unstated qualifiers" contract,
gpt-4o-mini still picks the position-1 "Merkezi" candidate in both cases.
These are tuning cases, and passing them would not have been validation
evidence.

## DEV gate (frozen before the run)

| Check | Result |
|---|---|
| Operational: 95 + 2 complete; invalid, error and timeout all 0 | ✔ |
| Qualifier improvement: C < A on the frozen slice (4 vs 3) | **✘** |
| False NONE ≤ 5 (1) | ✔ |
| Exact ≥ 62/77 (63) | ✔ |
| 0 production-correct → C-wrong on the general/specific slices | ✔ |
| 471 and 472 correct | **✘** |
| **Overall** | **FAIL** |

**About the qualifier check:** on all 77 DEV cases C has 4 unstated-qualifier
cases against A's 5. The frozen gate slice (36) is the one that counts, and
there C is higher. The gate was not re-litigated.

**Reading the result:**

- **What C kept:** A's aggregate behaviour — 63 vs 64, net −1, p = 1.0 — and
  A's false-NONE level (1).
- **What C did not change:** the targeted behaviour. The same
  qualifier-assuming choices remain on 466, 470 and 473 in DEV, and on 471
  and 472.
- **Interpretation:** prompt wording alone did not move gpt-4o-mini off
  these candidates.
- **Options:** order effects and retrieval/KB phrasing remain untested
  separate variables. The neutral-order control (76 calls) is prepared, and
  the weak `qna:129` canonical question is noted in the qualifier postmortem.
  They are options only; nothing was run.

## Old HOLDOUT and final validation

- **Old HOLDOUT:** it was **NOT rerun**. Only the two approved
  known-regression diagnostics (471 and 472) were called.
- **Final validation:** **BLOCKED**, because the gate failed. No
  final-validation calls were made and no IDs were frozen.
- **Strategy unchanged** (`PHASE_7B_FINAL_VALIDATION_STRATEGY.md`): 120 cases
  from the unused frozen pool plus about 120 new production-like reviewed
  queries.
- **The unused pool alone is insufficient.** It has 345 cases, easy by
  construction, and the first candidate is correct in 345/345.

## Usage (97 calls)

| Metric | Value |
|---|---|
| Input tokens | 204,654 total (mean 2,110) |
| Output tokens | 1,213 total |
| Latency | avg 1,463.7 ms · median 1,436.0 ms · p95 1,840.6 ms · max 2,181.8 ms |
| Errors | 0 invalid, 0 model error, 0 timeout; 0 retries |
| Cost | PRICE_REQUIRED |

## Isolation

- **Registry, capability config, KB, alias and model-registry digests:**
  identical before and after (`1|4|e17dc611…|df67859d…|66b014e7…|42b94071…`).
- **Unchanged:** production selector prompt and contract, config, candidate
  retrieval and order.
- **Production process:** the backend logged nothing in the run window.
- **Secrets:** no key material appears in the outputs.
- **Artifacts** (git-ignored): `prompt-experiment/variant-c-dev/`.

## Tests

- **New:** `tests/test_selector_variant_c.py`, 8/8.
  - **Review lock:** deterministic; labels map deterministically; unknown
    labels are rejected.
  - **Semantic Gold:** V1 parent unchanged; V1.1 deterministic.
  - **Prompts:** A and B frozen; C generic.
  - **Isolation:** C and A differ only in the system prompt; config, order
    and split are identical.
  - **Plan guard:** OpenRouter only, variant_c_v1 only, 97 calls, no other
    calls.
  - **Scope:** an old-HOLDOUT scope is refused; only 471/472 may run outside
    DEV.
  - **Freeze:** baseline, heuristic and gate are frozen and tamper-detecting.
  - **Post-run:** 95 + 2 unique LIVE results, no errors, budget respected,
    baseline and Gold re-verified.
- **Full backend suite:** **539 passed**, 0 failed (531 before, plus 8).
