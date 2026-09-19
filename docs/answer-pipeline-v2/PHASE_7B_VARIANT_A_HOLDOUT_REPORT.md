# Answer Pipeline V2 — Phase 7B Variant A Semantic HOLDOUT Report

## Status

```text
Phase 7B-Prompt HOLDOUT Live (variant_a_v1, Semantic Gold V1)
VARIANT_A_HOLDOUT = FAIL
```

- **Run validity:** complete and valid. It made 42/42 calls with no errors.
- **Why it failed:** hard gate 8. Critical cases **471 and 472 are both
  wrong**; the other seven hard checks pass.
- **What this means:** no prompt candidate is promoted, and the prompt was
  **not** changed after the result. HOLDOUT is a one-shot validation set, so
  there is no re-run, no Variant C and no B-on-HOLDOUT.

**Run setup:**

- Branch `production-readiness`; starting HEAD `b7e6c82`.
- **Human decision:** variant_a_v1 was selected for HOLDOUT. variant_b_v1 was
  not run.
- **Live calls, OpenRouter only:**

  | Config | Calls |
  |---|---:|
  | `openai/gpt-4o-mini`, variant_a_v1, HOLDOUT | **42** (approved maximum 42) |
  | Production | 0 |
  | Variant B | 0 |
  | Stage B | 0 |
  | Alternative model, reasoning or metadata experiments | 0 |

- **Config:** `af9eb2d0…`, identical to DEV (reasoning none, temperature 0,
  max_tokens 32).
- **Retries:** none (`retry_errors=False`).

## Frozen identities (verified before the run)

| Artifact | Fingerprint |
|---|---|
| variant_a_v1 prompt | `1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e` (prompt manifest unchanged; file sha256 identical before/after) |
| Snapshot | `3e558768561814dabe58cd0a8fc7ada71e6c1ad98e6e5bfa460320c21dea75f6` |
| Challenge | `bed2dad16a83af96a7b427e5e8b1b56b53c9c0033eefaeca6624e8cae2ba760b` |
| Split | `0fcb244120bdb1144de7f10f853cd2f6fe0c6d13b3f9c08411c041a48efb5777` (HOLDOUT = 42 ids, unchanged) |
| Semantic Gold V1 | `36ab1d6f06b990b247f8fee882acd0aba3d4f235ec0ecafc65504c3ffc9ea713` |
| Serializer contract | `d50fbee4…` |

The Semantic Gold V1 fingerprint was **recomputed** from `semantic-gold.jsonl`,
together with its artifact hashes; it was not just read from the manifest.

## Pre-live freeze (before any Variant A HOLDOUT output existed)

**File:** `prelive-baseline.json`.

- **Frozen:** 2026-09-19T18:49:42Z. The live run started at 18:51:28Z.
- **Baseline fingerprint:** `0d318f169900f7b511e8e13bc5d38ced7485f49fcb9f5669cc8234d0ffb03acb`.
- **Contents:**
  - the production and first-candidate semantic HOLDOUT scores (from the saved
    Stage A run `9dfc72c140dc7e93`; results sha256 recorded);
  - the 38 semantic-evaluable ids and the slice memberships;
  - production decisions on 471 and 472;
  - the **promotion gate definition**
    (`selector-holdout-promotion-gate-v1`).
- **Re-verification after scoring:** the scorer reproduced the frozen
  production values exactly, and the file's fingerprint was re-verified.
  The evaluator refuses to run otherwise.

**HOLDOUT denominators:**

| | Cases |
|---|---:|
| HOLDOUT total | 42 |
| Semantic-evaluable | **38** |
| Excluded | 4: 404, 461 (ambiguous); 325 (content review); 375 (retrieval/KB review) |
| Expected NONE | **0**, so NONE recall = **NOT_MEASURABLE** |

- **Calls vs scoring:** all 42 cases were called against the frozen parent
  snapshot. The exclusions only leave the scoring denominator.

| Pre-live baseline (38) | Exact | False NONE | Correct NONE | Wrong SELECT | NONE count |
|---|---:|---:|---:|---:|---:|
| Production (Stage A reuse) | 24/38 (0.632) | 10 | 0 | 4 | 10 |
| First candidate | 24/38 (0.632) | — | — | — | — |

## Live-run guard

**Plan:** `live-plan.json`, fingerprint
`7b9b21bcf274def65dc9b8b30f3d734684e246d5661e46303d205165d9560588`.

- **Origin:** the plan was generated in this session inside the user-approved
  envelope, which was "OpenRouter / openai/gpt-4o-mini / Variant A / HOLDOUT /
  max 42". Its fingerprint was passed as the approval. It is not a second
  approval.
- **The run is refused unless all of these hold:**

  | Check | Required value |
  |---|---|
  | Plan kind | `selector-prompt-experiment-holdout` |
  | Plan file | unmodified, matching the approved fingerprint |
  | Provider | `openrouter` |
  | Config fingerprint | the DEV config |
  | Prompt | `variant_a_v1` with fingerprint `1aed5688…` |
  | Case ids | exactly the 42 HOLDOUT ids |
  | Calls | `logical_calls == 42`, `max_logical_calls == 42` |
  | Other planned configs | 0 (production, B, Stage B) |
  | Snapshot, serializer, Semantic Gold | same fingerprints |
  | Frozen baseline | same fingerprint |

- **Existing HOLDOUT guard:** `holdout_guard` also runs. It needs a
  human-selected prompt and a passing DEV gate report. It uses a **new**
  `dev-gate-report-semantic.json`: the DEV gate recomputed on Semantic Gold,
  PASS. The old-Gold DEV gate report (`passed=false`) is kept unchanged.
- **`BudgetedBackend`:** refuses any logical call beyond 42 and any case
  outside HOLDOUT. Actual calls were 42; refused calls were 0.
- **Network:** sockets were allowed only to `openrouter.ai`. There were no
  direct OpenAI or Gemini calls and no fallback.
- **Output location:** results were written to
  `prompt-experiment/semantic-holdout-a/`.
  - **Why it is separate:** the run identity does not include the case set,
    so this HOLDOUT run shares run id `f47cf095e441a6e8` with the DEV run.
  - **Effect:** the DEV results file was not touched.

## Results (Semantic Gold V1, 38 semantic-evaluable HOLDOUT cases)

| | Production | Variant A | First candidate |
|---|---:|---:|---:|
| Exact | 24/38 (0.632) | **30/38 (0.789)** | 24/38 (0.632) |
| Correct SELECT | 24 | 30 | — |
| Wrong SELECT | 4 | 6 | — |
| False NONE | 10 | **2** | — |
| Correct NONE | 0 | 0 | — |
| False SELECT on expected NONE | 0 | 0 | — |
| NONE output (38 evaluable) | 10 | 2 | — |
| NONE output (all 42 called) | 12 | 3 | — |

The accuracy delta against production is **+6 cases (+0.158)**.

**Paired vs production (same 38 ids):**

| Both correct | Only production | Only Variant A | Both wrong | Paired net | McNemar exact p |
|---:|---:|---:|---:|---:|---:|
| 24 | **0** | **6** | 8 | **+6** | 0.031 (descriptive) |

- **Variant A-only wins:** 77, 264, 306, 326, 413, 493.
- **Production-correct → A-wrong:** **none**.

**Where the gain comes from:** all 6 Variant A wins are cases where
production answered NONE.

| Production NONE cases (10) | Variant A output | Cases |
|---|---|---:|
| Flipped to SELECT, correct | 77, 264, 306, 326, 413, 493 | 6 |
| Flipped to SELECT, wrong | wrong SELECT goes from 4 to 6 | 2 |
| Kept NONE | 247, 440 | 2 |

The HOLDOUT gain is therefore the NONE-strictness fix. None of it comes from
the unstated-qualifier rule.

### NONE behaviour

| | Production | Variant A |
|---|---:|---:|
| Total NONE (evaluable) | 10 | 2 |
| Expected NONE | 0 | 0 |
| Correct NONE | 0 | 0 |
| False NONE | 10 | 2 |
| False SELECT on expected NONE | 0 | 0 |

- **Measurability:** there are no expected-NONE cases in HOLDOUT, so NONE
  recall is **NOT_MEASURABLE**. No gate was built around NONE.
- **Variant A's NONE outputs:** 247 and 440 (both false), plus 375, which is
  excluded.
- **Consistency with DEV:** the low NONE rate matches DEV (1/77). Whether A
  under-uses NONE when NONE is correct remains untested (g65).

### Historical first-candidate comparator (diagnostic only)

| | Rescue | Corruption | Preserve | Unresolved | Net |
|---|---:|---:|---:|---:|---:|
| Production | 8/14 | 8/24 | 16 | 6 | 0 |
| Variant A | 9/14 | 3/24 | 21 | 5 | **+6** |

**Historical DEV gate recomputed on HOLDOUT:** all five checks pass. It is
reported only and is **not** sufficient for promotion.

### General/specific and other slices

Slice membership comes from Phase 7A and the challenge set. Scores use the
Semantic Gold.

| Slice | Cases | Production | Variant A |
|---|---:|---:|---:|
| General expected (85, 471, 472) | 3 | 1 | 1 |
| Specific expected (464, 465, 467) | 3 | 3 | 3 |
| near_qna | 22 | 18 | 20 |
| kb_overlap (366, 440, 472) | 3 | 0 | 0 |
| multi_acceptable | 7 | 4 | 5 |
| easy_control | 7 | 3 | 6 |

**General/specific regressions** (production-correct → A-wrong): **0**.

## Critical cases 471 / 472

| | 471 | 472 |
|---|---|---|
| User text | "Yatay geçiş nasıl yaparım" | "Yatay geçiş yapmak istiyorum" |
| User states "merkezi"? | no | no |
| Semantic acceptable ref | `qna:129`: "Yatay geçiş işlemleri ile ilgili detaylı bilgiye nereden ulaşabiliriz?" | same |
| Gold provenance | inherited unchanged from the parent reviewed Gold (not re-adjudicated in the semantic round) | same |
| Production | SELECT `qna:342` "Merkezi yatay geçiş başvurusu nasıl yapılır?", **wrong** | same, **wrong** |
| Variant A | SELECT `qna:342` "Merkezi yatay geçiş başvurusu nasıl yapılır?", **wrong** | same, **wrong** |

- **Did Variant A assume the unstated "merkezi" qualifier?** **Yes, in both
  cases.** The user named only the topic ("yatay geçiş"). Variant A chose the
  specific *Merkezi* yatay geçiş candidate over the general `qna:129`.
- **Prompt rule violated:** variant_a_v1's own rule, "unstated qualifier →
  prefer general, never assume".
- **Candidate order:** `qna:342` is also the retrieval rank-1 candidate. The
  first-candidate heuristic makes the same error.
- **Caveat:** this Gold was not re-adjudicated in the semantic round. It is
  the parent reviewed Gold, and Phase 7B postmortem classified these as clear
  selector errors. The two cases were not among the 106, so no alias-ownership
  audit exists for them.
- **Effect on this result:** none. The FAIL stands, and §19 forbids a HOLDOUT
  re-run.
- **Open item:** adjudicate 471/472 semantically, offline, before any future
  validation strategy relies on them.

## Promotion gate (frozen before the run)

| # | Hard check | Result |
|---|---|---|
| 1 | run complete (42/42, no duplicates, no missing ids) | ✔ |
| 2 | INVALID_OUTPUT == 0 | ✔ |
| 3 | MODEL_ERROR == 0 | ✔ |
| 4 | TIMEOUT == 0 | ✔ |
| 5 | Variant A exact > production exact (30 > 24) | ✔ |
| 6 | paired net vs production > 0 (+6) | ✔ |
| 7 | no general/specific regression (3 general + 3 specific cases; 0 regressions) | ✔ |
| 8 | 471 and 472 correct | **✘ (both wrong)** |
| | **Overall** | **FAIL** |

**Reading the result:**

- **What passed:** the aggregate picture favours Variant A on HOLDOUT. It
  gains 6, loses 0 and emits 8 fewer false NONEs. That replicates the DEV
  direction.
- **What failed:** the design target the gate singled out, not inventing an
  unstated "merkezi" qualifier, was not met.
- **Interpretation:** the aggregate gain comes from the NONE-strictness fix,
  not from the unstated-qualifier rule.
- **Next step:** any next iteration needs a new validation strategy. This
  HOLDOUT is spent.

## Usage (actual OpenRouter metadata, 42 calls)

| Metric | Value |
|---|---|
| Input tokens | 77,141 total (mean 1,836.7; plan calibrated estimate 77,257) |
| Output tokens | 522 total (mean 12.4, max 13) |
| Latency | avg 1,446.7 ms · median 1,408.6 ms · p95 1,829.9 ms · max 2,001.6 ms |
| INVALID_OUTPUT / MODEL_ERROR / TIMEOUT | 0 / 0 / 0 |
| `actual_model` | `openai/gpt-4o-mini` |
| Finish reason | `stop` for all 42 |
| Cost | PRICE_REQUIRED (no price inputs; no dollar figure) |

## Production isolation

- **Registry and KB digests:** identical before and after
  (`1|4|e17dc611…|df67859d…|66b014e7…|42b94071…`). This covers:
  - the config version and audit counts;
  - the `ai_capability_config` digest;
  - the KB (`qna`) digest;
  - the alias table (`qna_queries`) digest;
  - the model registry digest.
- **Production process:** the backend container logged nothing in the run
  window. No breaker or DecisionTrace was involved.
- **No changes to:** production prompt, production config, model registry,
  KB, alias table or breaker.
- **Secrets:** no key material appears in the outputs.

## Artifacts (git-ignored)

`outputs/selector-v2-benchmark/prompt-experiment/semantic-holdout-a/` holds:

| File | Content |
|---|---|
| `prelive-baseline.json` | the frozen baseline |
| `live-plan.json` | the HOLDOUT plan |
| `dev-gate-report-semantic.json` | the DEV gate recomputed on Semantic Gold |
| `runs/f47cf095e441a6e8/` | `results.jsonl`, `run-manifest.json`, `call-accounting.json` |
| `responses.jsonl` | the raw results |
| `scored-results.jsonl` | the scored results |
| `metrics.json` | all metrics |
| `critical-cases.json` | the 471/472 diagnostic |
| `manifest.json` | artifact hashes, the baseline re-verification flag and the outcome |

## No tuning after HOLDOUT

No prompt, scoring or gate code was modified after the live results were
produced. Every `semantic_holdout.py` and `cli.py` change was made before the
live call, during the fake self-test. These were **not** done:

- a Variant C;
- a different A version or a model-swapped A;
- a B run on HOLDOUT.

## Tooling and tests

- **New module:** `benchmarks/selector_v2/semantic_holdout.py`.
- **New CLI commands:** `semantic-holdout-prep`, `semantic-holdout-run` and
  `semantic-holdout-eval`.
- **Tests:** `tests/test_selector_semantic_holdout.py` passed 6/6.
  - **Before the run:**
    - prompt, split and Semantic Gold fingerprints;
    - the 42 HOLDOUT ids;
    - the baseline is frozen, reproducible and tamper-detecting;
    - the provider, prompt, split, budget and extra-config plan guards;
    - the call budget, which refuses call 43 and non-HOLDOUT cases;
    - the semantic gate report and `holdout_guard`.
  - **After the run:**
    - 42 outputs, no duplicates, no missing ids, no corrupt lines;
    - LIVE mode on OpenRouter with the DEV config;
    - call accounting at most 42 with 0 refused;
    - deterministic scoring;
    - the production baseline unchanged.
- **Full backend suite:** 523 passed, 0 failed (517 before, plus 6).
