# Answer Pipeline V2 — Phase 7B Prompt DEV Live Report

## Status

```text
Phase 7B-Prompt DEV Live — variant_a_v1 and variant_b_v1 on DEV (95 each)
STATUS: PASS (run completed; results are measurements, not a verdict)

Phase 7B-Prompt HOLDOUT Live — BLOCKED_NO_DEV_VARIANT_PASSED_GATE
Phase 7B-Live Stage B — NOT RUN
```

- Branch: `production-readiness`
- Starting HEAD: `744b85f`
- **Approval:** the user explicitly approved exactly 190 OpenRouter calls,
  95 per variant on DEV.
- **Calls made:** **190 unique invocations**, 95 per variant. The production
  prompt made 0 new calls (Stage A reused), HOLDOUT 0, Stage B 0.
- **HOLDOUT NOT RUN.** This includes 471 and 472. **FULL STAGE B NOT RUN.**
  No alternative model, reasoning, metadata, alias or K experiment was run.
- **Prompts frozen:** the prompts were **not modified after the live
  results**; `prompts/` has no diff and the manifest fingerprints are
  unchanged.

## Run identity

| Item | Value |
|---|---|
| Live plan (verified before the run) | `c88478486c12bc07f5650517025d93f779fe9444dcf3c0ab6494ae9c5b8cf3d8` |
| Provider / model | `openrouter` / `openai/gpt-4o-mini`, reasoning none, temperature 0, max_tokens 32 (config `af9eb2d0…`) |
| Snapshot / challenge / split | `3e558768…` / `bed2dad1…` / `0fcb2441…` |
| Selector contract (production) / serializer contract | `3f49b198…` / `d50fbee4…` |
| Production prompt | `2d59cfb6…` (baseline = Stage A run `9dfc72c140dc7e93`, reused) |
| variant_a_v1 | prompt `1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e`, run `f47cf095e441a6e8` |
| variant_b_v1 | prompt `44893b6bb069da5b2849413b3d903fa3ff8dbfd1e1e7ea8242d1302135e093c9`, run `28d38907daa7cd20` |
| Run window (UTC) | 2026-09-19T14:39:41Z – 14:44:12Z |

**Preflight.** All 16 checks passed:

- the plan fingerprint (recomputed, stored and as reported);
- snapshot, challenge, split, contract, serializer and all three prompt
  fingerprints;
- no prompt-manifest drift;
- the plan contains exactly A and B;
- the config;
- DEV has 95 cases and the plan has 95 + 95 calls with 0 HOLDOUT and 0
  production calls;
- the OpenRouter key is present and no direct OpenAI/Gemini key is present.

**Execution checks.**

- **Live gate:** the run used `prompt-run --live --confirm-live-provider-calls
  --live-plan … --approve-plan-fingerprint …`.
- **Network:** live mode is OpenRouter-only, with sockets allowed only to
  `openrouter.ai`. There were no direct OpenAI/Gemini calls and no fallback.
- **Result integrity:** each variant holds 95 unique DEV results, 0
  superseded and 0 corrupt.
- **Resume check:** a pass with a backend that fails if called executed 0
  cases and made 0 calls.
- **Namespaces:** A and B have separate run ids (prompt and split
  fingerprints are part of identity).
- **Isolation:**
  - registry config/version/audit rows and the capability-config digest were
    identical before and after;
  - the production process logged only `/health` in the run window;
  - no breaker or DecisionTrace interaction occurred (the benchmark runs in a
    separate process with a direct adapter call);
  - no key material appears in any output.

## Results on DEV (95 cases; CHALLENGE subset, not a global accuracy)

| Metric | Production (Stage A) | Variant A | Variant B |
|---|---:|---:|---:|
| Exact | 34/95 | **54/95** | 45/95 |
| Rescue (baseline wrong → correct) | 6/13 | 6/13 | 6/13 |
| Corruption (baseline right → wrong) | 54/82 | **34/82** | 43/82 |
| Preserve / Unresolved | 28 / 7 | 48 / 7 | 39 / 7 |
| Net corrections | −48 | **−28** | −37 |
| NONE output = false NONE | 29 | **5** | 18 |
| INVALID_OUTPUT / MODEL_ERROR / TIMEOUT | 0/0/0 | 0/0/0 | 0/0/0 |
| Valid SELECT / valid NONE | 66 / 29 | 90 / 5 | 77 / 18 |

- **First-candidate comparator** (diagnostic only): 82/95.
- **NONE:** there are no expected-NONE cases, so NONE recall and precision
  are not computed.

**Change vs production:**

| | Exact | Corruption | False NONE | Net corrections |
|---|---:|---:|---:|---:|
| Variant A | **+20** | **−20** | **−24** | **+20** |
| Variant B | +11 | −11 | −11 | +11 |

**The key question was whether a prompt change reduces Stage A's
systematic corruption.** It does, partly:

- Variant A removes 20 of the 54 DEV corruptions and 24 of the 29 false
  NONEs.
- Variant A breaks **zero** production-correct cases.
- Variant B, the NONE-threshold-only ablation, recovers about half of A's
  gain. So roughly half of A's effect comes from the NONE threshold, and the
  rest from the need-framing plus explicit-qualifier guidance.
- Neither variant changes rescue: 6/13 in all three.
- Both remain far below the first-candidate comparator (82/95).

### Paired comparisons (exact McNemar, two-sided)

| Pair | Both correct | Only first | Only second | Both wrong | p |
|---|---:|---:|---:|---:|---:|
| production vs A | 34 | 0 | 20 | 41 | 1.9e−6 |
| production vs B | 33 | 1 | 12 | 49 | 0.0034 |
| A vs B | 44 | 10 | 1 | 40 | 0.0117 |

No winner is declared.

### Slices

| Slice (DEV) | Production | A | B |
|---|---:|---:|---:|
| General expected (8) | 2/8 | 2/8 | 2/8 |
| Specific expected (3) | 1/3 | 2/3 | 1/3 |
| Near-QnA (53) | 16/53 | 24/53 | 21/53 |
| kb_overlap_flagged tag (6) | 3/6 | 3/6 | 3/6 |
| Multi-acceptable (14) | 8/14 | 11/14 | 9/14 |
| Easy control (21) | 6/21 (corruption 15) | **16/21 (corruption 5)** | 11/21 (corruption 10) |

**General/specific:**

- The 11 DEV cases are 78, 79, 81, 82, 83, 84, 86, 466, 469, 470 and 473.
- **Production-correct cases broken:** none by A, none by B.
- **Newly fixed:** A fixes 469 (specific). General-expected stays 2/8 for
  all three.
- **Coverage gap:** the remaining general-expected misses are mostly the
  postmortem's "user stated the qualifier" cases (78–85), where the Gold
  expects the general answer. The clear unstated-qualifier errors 471 and
  472 are in **HOLDOUT**. So DEV **cannot verify** that the unstated-qualifier
  rule holds; the gate only blocks regressions observable on DEV.

### Postmortem taxonomy slices (diagnostic; denominators unchanged)

| Slice (DEV) | Production | A | B |
|---|---:|---:|---:|
| CLEAR_SELECTOR_ERROR | 0/0 (not observable on DEV) | 0/0 | 0/0 |
| GOLD_ALIAS_QUESTIONABLE | 0/12 | 3/12 | 1/12 |
| CONTRACT_MISMATCH | 0/23 | 12/23 | 8/23 |
| NEEDS_HUMAN_REVIEW | 0/26 | 5/26 | 3/26 |
| KB_OVERLAP_INTRINSIC | 0/3 | 0/3 | 0/3 |

- **How to read the gains:** gains on GOLD_ALIAS_QUESTIONABLE and
  CONTRACT_MISMATCH mean **closer agreement with the alias-derived Gold**.
  They are not automatically a production semantic improvement. For example,
  A now agrees with the Gold on 19, 35 and 221, which the postmortem flagged
  as questionable.
- **Where A still fails:** 36 of its 41 DEV failures are wrong SELECTs,
  mostly in the GOLD_ALIAS_QUESTIONABLE and NEEDS_HUMAN_REVIEW slices. This
  is where the prompt cannot be expected to agree with an alias-derived
  label.

### Tokens, latency, cost

The usage figures are **actual OpenRouter metadata**, with 95/95 coverage
for each run.

| | Input total | Input mean / median / p95 / max | Output total | Output mean / median / p95 / max |
|---|---:|---|---:|---|
| Variant A | 179,695 | 1,891.5 / 1,834 / 2,870 / 3,203 | 1,195 | 12.6 / 13 / 13 / 13 |
| Variant B | 180,170 | 1,896.5 / 1,839 / 2,875 / 3,208 | 1,091 | 11.5 / 13 / 13 / 13 |
| (Production DEV, Stage A) | 179,220 | 1,886.5 / 1,829 / 2,865 / 3,198 | 1,003 | 10.6 / 13 / 13 / 13 |

- **A vs B:** B uses 475 more input tokens over 95 calls (+5 per call),
  consistent with its longer prompt.
- **Estimate accuracy:** the calibrated plan estimate (≈360.8 k) matched the
  actual total of 359,865 closely.

| Latency ms | Mean | Median | p95 | Max |
|---|---:|---:|---:|---:|
| Variant A | 1,412 | 1,387 | 1,877 | 2,123 |
| Variant B | 1,410 | 1,368 | 1,680 | 2,733 |

**Cost:** **NOT_CALCULATED — no explicit pricing inputs.**

## Prompt selection gate

The gate requires all of the following:

- the run is complete;
- net corrections ≥ 0;
- corruption < 54;
- false NONE < 29;
- no production-correct general/specific case turns wrong.

| Check | Variant A | Variant B |
|---|---|---|
| Complete | ✔ | ✔ |
| Net corrections ≥ 0 | ✘ (−28) | ✘ (−37) |
| Corruption < 54 | ✔ (34) | ✔ (43) |
| False NONE < 29 | ✔ (5) | ✔ (18) |
| No general/specific regression | ✔ (none) | ✔ (none) |
| **Gate** | **FAIL_GATE** | **FAIL_GATE** |

- **Passing variants:** none, and no automatic winner was selected.
- **What the gate cannot see:** DEV contains no CLEAR_SELECTOR_ERROR case,
  so the gate cannot confirm unstated-qualifier behavior. It only blocks
  regressions observable on DEV.

**HOLDOUT recommendation:** **no HOLDOUT run**. The status is
BLOCKED_NO_DEV_VARIANT_PASSED_GATE.

**Observation for human review (not a decision):** the net ≥ 0 condition
compares against the retrieval rank-1 heuristic, which is the alias owner.
Most of A's remaining corruptions sit in Gold/alias-questionable or
unreviewed cases, so "net ≥ 0" may be unreachable for any selector judged
against this alias-derived Gold. Options belong to a human decision:

- a Gold/alias review;
- a revised gate;
- a new `variant_c_v1` experiment;
- a model comparison.

The prompts were not edited.

## Case-level artifacts (git-ignored)

`outputs/selector-v2-benchmark/prompt-experiment/dev-results/`:

- `dev-comparison.json`: reports, deltas, gates, paired stats and
  identities.
- `case-diffs.json`:

  | List | Cases |
  |---|---:|
  | production-correct → A-wrong | 0 |
  | production-correct → B-wrong | 1 (case 422) |
  | production-wrong → A-correct | 20 |
  | production-wrong → B-correct | 12 |
  | A-only-correct | 10 |
  | B-only-correct | 1 (case 355) |
  | NONE changes production→A | 24 |
  | NONE changes production→B | 11 |

  Each entry carries the expected refs, first candidate and each run's
  selection with canonical text.
- `review-queue.json`, prompt-specific:
  - 422: B picks a term-specific "Bahar dönemi" results QnA over the general
    accepted ones;
  - 355: A picks the AKSİS username QnA, a plausible reading outside the
    accepted set;
  - 19, 35, 221: A's agreement with Gold on cases already flagged as
    questionable.

  Gold, taxonomy and prompts are unchanged.
- Per-run `dev-gate-report.json` under
  `prompt-experiment/runs/<run_id>/`.

## Code changes (analysis only; no production or prompt change)

- **`prompt_experiment.py`:**
  - `dev_report` adds NONE/validity counts, token/latency usage and the
    kb_overlap/multi_acceptable slices;
  - new `compare_runs` (paired matrices and human-review diffs; no winner).
- **`cli.py`:** new `prompt-dev-compare` command.
- **Tests:** 2 new; prompt-experiment module 17/17; full suite **481/481**
  (repository-root layout). No regression.
