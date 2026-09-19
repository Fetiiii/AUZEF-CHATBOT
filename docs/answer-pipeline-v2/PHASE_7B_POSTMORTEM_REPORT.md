# Answer Pipeline V2 — Phase 7B Postmortem Report

## Status

```text
Phase 7B-Postmortem — Selector failure anatomy + experiment design
STATUS: PASS
```

- Branch: `production-readiness`
- Starting HEAD: `9d93570`
- **No live provider call** was made. The only data read beyond the frozen
  files was the `qna_queries` alias table (read-only), inside the network
  guard.
- **Production unchanged:** prompt, model and config are the same, and the
  selector contract fingerprint is still `3f49b198…` (tested).
- **Nothing was added or run:** no registry change, no Stage B, no
  reasoning run, no Gold edit, no metadata and no alias bypass.
- **Frozen inputs, re-verified on load:** snapshot `3e558768…`, challenge
  `bed2dad1…`, Stage A run `9dfc72c140dc7e93`. None of these artifacts was
  modified; the postmortem only adds `outputs/selector-v2-benchmark/postmortem-v1/`.

## 1. Stage A recap, recomputed from the frozen results

The numbers match the Stage A report exactly:

- First-candidate baseline **117/137**; production selector **51/137**.
- Rescue **8/20**, corruption **74/117**, preserve 43, unresolved 12,
  false NONE 41.
- Paired counts: both correct 43, only first-candidate 74, only model 8,
  both wrong 12.

**Analysis universe:** 94 informative cases (74 corruption + 12 unresolved
+ 8 rescue). The command refuses to write if these counts do not reconcile.

## 2. Taxonomy method (deterministic, no LLM)

Every model failure gets exactly one primary category plus secondary tags
(`postmortem.py`, `selector-failure-taxonomy-v1`). The evidence is:

- **Distinguishing-term support:** stems that one competing canonical
  question has and the other lacks. The support count is how many of those
  stems occur in the user text. Stems are casefolded, diacritic-folded,
  5-character stems with generic stopwords removed.
- **Coverage:** the share of the user's content stems found in a
  candidate's canonical question plus answer.
- **Dataset and review facts:** the Phase 7A near-pair relation and
  general/specific role, `kb_overlap_flagged`, the Stage A review queue, and
  the historical alias table.

Rules are applied in order, and anything they cannot decide stays
**`NEEDS_HUMAN_REVIEW`**:

| Rule | Primary |
|---|---|
| General expected, specific sibling chosen, user text contains none of its distinguishing terms | `UNSTATED_QUALIFIER_SPECIFICITY` |
| Case in the Stage A human review queue | `GOLD_OR_ALIAS_QUESTIONABLE` |
| Reviewer-flagged KB overlap | `KB_OVERLAP_INTRINSIC` |
| NONE with ≤ 2 content stems | `FALSE_NONE_UNDERSPECIFIED` |
| NONE and the gold candidate covers ≥ 50% of the user's stems | `FALSE_NONE_OVERSTRICT` |
| User text contains more of the *selected* candidate's distinguishing terms than the gold's | `GOLD_OR_ALIAS_QUESTIONABLE` (tag `intent_states_selected_distinction`) |
| Near-pair sibling chosen while the gold has more support or coverage | `WRONG_NEAR_QNA_DISCRIMINATION` |
| Answer text pulls lexically toward the selected candidate while the gold canonical fits better | `ANSWER_TEXT_DISTRACTION` |
| Otherwise | `NEEDS_HUMAN_REVIEW` |

- **Secondary tags:** `POSITION_OVERRIDE` (gold at rank 1, model chose a
  lower rank; 37 cases) and `ANSWER_TEXT_SIGNAL` (8).
- **Unused:** `OTHER` was never needed.

## 3. Root causes

| Primary | Corruption (74) | Unresolved (12) | Of which false NONE (41) |
|---|---:|---:|---:|
| GOLD_OR_ALIAS_QUESTIONABLE | 22 | 1 | 2 |
| FALSE_NONE_OVERSTRICT | 13 | 0 | 13 |
| FALSE_NONE_UNDERSPECIFIED | 5 | 0 | 5 |
| KB_OVERLAP_INTRINSIC | 0 | 5 | 4 |
| UNSTATED_QUALIFIER_SPECIFICITY | 0 | 2 | 0 |
| WRONG_NEAR_QNA_DISCRIMINATION | 1 | 0 | 0 |
| ANSWER_TEXT_DISTRACTION | 0 | 0 | 0 |
| NEEDS_HUMAN_REVIEW | 33 | 4 | 17 |

**Needs human review (37):** 77, 88, 89, 104, 172, 177, 179, 184, 189, 201,
215, 230, 247, 264, 277, 285, 306, 307, 320, 322, 323, 325, 326, 328, 330,
371, 375, 376, 377, 383, 384, 408, 418, 466, 493, 505, 506. Most are
near-QnA sibling choices or NONEs where lexical evidence does not separate
the readings.

### False NONE (41)

- **Candidate set:** the gold candidate was always in the candidate set, at
  gold rank 1 in 37 cases.
- **Alias relation:** in 37 cases the user text is an exact alias of the
  gold, and in 4 it is an alias of another QnA.
- **Slices:** 17 near_qna, 3 general_specific, 4 kb_overlap.
- **Key split for prompt design:**

  | Gold sufficiency | Cases | Meaning |
  |---|---:|---|
  | LEXICALLY_SUFFICIENT | **20** | Gold canonical+answer covers ≥ 50% of the request; NONE is over-strict under a practical reading |
  | ALIAS_MAPPING_ONLY | **20** | Gold is accepted mainly because the text is a historical alias; low lexical support |
  | WEAK_SUPPORT | 1 | Neither |

  Examples of LEXICALLY_SUFFICIENT: 112, 461, 407, 412, 413. Examples of
  ALIAS_MAPPING_ONLY: 306/307 "hesap makinesi", 383/384, 493.

### Specificity (general/specific, 17 cases)

**Expected general (11).** The aggregate is 2/11 correct, broken down
below:

| Case | User text (abridged) | Model | Verdict |
|---|---|---|---|
| 81 | Kayıt tarihleri… | general | CORRECT |
| 84 | Kayıtlar ne zaman başlayacak | general | CORRECT |
| 78, 79, 82, 83, 85 | "İkinci üniversite / sınavsız … kayıt" | specific sibling | **user stated the distinguishing term** (ikinci üniversite, sınavsız): not a rule violation; the Gold general label is questionable |
| 471 | Yatay geçiş nasıl yaparım | specific | **rule violated** (qualifier unstated) |
| 472 | Yatay geçiş yapmak istiyorum | specific | **rule violated**; the prompt's own example |
| 470 | Yatay geçiş | NONE | underspecified NONE (reviewer-flagged KB overlap) |
| 473 | Örgün eğitimden… yatay geçiş | other candidate (reverse direction) | chose a different topic |

- **What this shows:** the "don't assume an unstated qualifier" rule was
  violated in only **2** cases. Five of the nine general-expected failures
  are cases where the user *did* state the qualifier and the model honored
  it, but the alias-derived Gold expects the general answer.

**Expected specific (6):** 3 correct (86, 464, 467), 2 NONE (466, 469), and
1 other candidate (465: "kurumlar arası … tarihleri", selected the general
dates QnA).

### Rescues (8), positive control

- **In every rescue**, the first candidate is the intent's exact-alias
  owner and is wrong.
- **Semantic support for the model's choice:**
  - **Five rescues** have user text that contains terms distinguishing the
    gold (153 "aksis öğretim yönetim", 160 "mobil uygulama şifre", 216
    "kayıt", 381 "af", 516 "uygulama").
  - **Two rescues** (245 and 460) carry no lexical cue, i.e. a semantic
    rescue.
  - **Case 503** is correct *against* lexical cues: the first candidate
    shares the terms.
- **Gold rank:** 2 (four cases), 3 (two) or 9 (two).
- **Requirement for any new prompt:** it must keep these, i.e. keep reading
  the user's specific terms over retrieval order.

## 4. Prompt-vs-Gold contract mismatch

This grouping is for experiment design only; no metric changes.

| Group | Failures (86) |
|---|---:|
| A. Selector clearly wrong | **2** |
| B. Gold/alias expectation questionable | **18** |
| C. Contract mismatch, both readings reasonable (over-strict or underspecified NONE, reviewer-flagged overlap, near-duplicate wording) | **29** |
| U. Undetermined (NEEDS_HUMAN_REVIEW) | **37** |

**Answer:** the Selector V2 standard ("reliably satisfies; no assumption")
and the reviewed Gold standard (alias-derived acceptability) are **not the
same thing**. At least 47 of the 86 failures (B + C) are explained by that
difference or by the Gold rather than by an unambiguous selector error.
Only 2 are unambiguous selector errors under the current contract.

## 5. Why the first-candidate baseline is strong

Measured with the historical alias table (fingerprint `a9322132…`):

| | Full primary (482) | Challenge (137) |
|---|---:|---|
| User text is an exact alias of the expected QnA | 461 | 117 |
| … of another QnA | 20 | 20 |
| Not an alias | 1 | 0 |
| First candidate = intent's alias owner | **481** | 137 |
| First candidate retrieved via its alias point | 481 | 137 |
| First candidate correct | 461 | 117 |

**Descriptively:** the first candidate is right whenever the alias owner is
the gold. It is right because the benchmark texts are the KB's own alias
strings, which retrieval indexes. This measures structural agreement between
the dataset and the index, not independent semantic quality. The benchmark
is still valid for what it measures (agreement with reviewed Gold); it just
cannot credit a selector for matching a label the index already encodes.

## 6. Review queue (recommendations only; Gold unchanged)

| Case | Recommendation | Note |
|---|---|---|
| 404 | REVIEW_RECOMMENDED | "açıköğretim öğrencisiyim": no answerable need |
| 19 | REVIEW_RECOMMENDED | objection vs application gold |
| 35 | KB_MAPPING_REVIEW | gold answer scope ≠ canonical question |
| 221 | REVIEW_RECOMMENDED | selected may be equally acceptable |
| 158 | KB_MAPPING_REVIEW | general vs exam-system login overlap |
| 74, 436 | KB_MAPPING_REVIEW | retrieval misses caused by alias collisions (g37) |

**Additional cases** where the user states the distinguishing term the model
matched (tag `intent_states_selected_distinction`, 18 cases, e.g. 78–85 and
174–176) are strong candidates for a Gold/alias review.

## 7. Metadata (7C) and exact-alias (7D) evidence

- **Metadata-candidate failures** (KB_OVERLAP_INTRINSIC +
  UNSTATED_QUALIFIER_SPECIFICITY): **7 of 86 (8.1%)** — 366, 415, 440, 470,
  471, 472, 474. This is weak evidence that metadata is needed; most failures
  are not candidate-content ambiguities.
- **Exact-alias bypass:**
  - **Challenge set:** a single-owner alias bypass would be right on **117**
    and wrong on **20**.
  - **Full primary:** right on 461, wrong on 20. This is identical to the
    first-candidate baseline.
  - **The 20 wrong cases:** they are exactly the alias collisions.

  7D should measure this against a *non-alias* evaluation set. On this Gold
  it would look artificially strong.

## 8. Canonical vs answer text (diagnostic only)

- **Answer text may pull toward the wrong candidate** (`ANSWER_TEXT_SIGNAL`)
  in 8 cases: 177, 189, 230, 320, 322, 325, 380, 473.
- **Answer text is where the gold's support lies** in 33 informative cases,
  where the gold answer adds coverage beyond its canonical question.
- **Consequence:** a canonical-only payload would lose that support.
- **No counterfactual was run live.**

## 9. DEV / HOLDOUT split (`prompt-experiment-split-v1`)

- **Rule:** stratify by membership in {first-candidate-wrong,
  general_specific, near_qna, kb_overlap_flagged, easy_control}. Within each
  stratum, order by `sha256("prompt-experiment-split-v1|" + case_id)`;
  round(0.3·n) cases go to HOLDOUT.
- **Size:** 95 DEV and 42 HOLDOUT; disjoint, union = 137.
- **Fingerprint:**
  `0fcb244120bdb1144de7f10f853cd2f6fe0c6d13b3f9c08411c041a48efb5777`.
- **The frozen challenge set is unchanged.**

**Production Stage A on each split** (no new calls):

| | DEV (95) | HOLDOUT (42) |
|---|---|---|
| Production exact | 34/95 | 17/42 |
| First-candidate exact | 82/95 | 35/42 |
| Rescue | 6/13 | 2/7 |
| Corruption | 54/82 | 20/35 |
| Net | −48 | −18 |
| False NONE | 29 | 12 |

**DEV failure mix** (the only input used to design the variants):

| Primary | DEV failures |
|---|---:|
| GOLD_OR_ALIAS_QUESTIONABLE | 16 |
| FALSE_NONE_OVERSTRICT | 10 |
| FALSE_NONE_UNDERSPECIFIED | 5 |
| KB_OVERLAP | 3 |
| WRONG_NEAR_QNA | 1 |
| NEEDS_HUMAN_REVIEW | 26 |

**Holdout caveat:**

- **What the analyst saw:** the taxonomy (§2–§8) required reading all 94
  informative cases, so HOLDOUT texts were seen during the analysis. Three
  HOLDOUT cases (77, 404, 472) had also been viewed during Stage A.
- **What protects the holdout:** the prompts contain generic principles
  only, which a test enforces (no ids, no KB question text, no
  benchmark wording).
- **Unstated-qualifier rule:** its two violations (471, 472) fall in
  HOLDOUT. The variants inherit that rule from the production prompt; it was
  not derived from HOLDOUT.

## 10. Proposed prompt variants (not production)

Both variants are defined in `backend/benchmarks/selector_v2/prompt_variants.py`.

- **Shared by both:** the production user payload and strict output
  contract.
- **Removed from both:** the production prompt's concrete example, because
  it overlaps benchmark wording.

**Variant A: `variant_a_practical_qualifier`** (contract `b81f3d6b…`)

- **Short and informal messages are normal.** Select the candidate that
  addresses the topic and practical need. Do not require every word or
  detail to be answered. This targets FALSE_NONE_OVERSTRICT and
  UNDERSPECIFIED.
- **Explicit qualifier → matching candidate.** If the user states a
  qualifier, prefer the candidate that matches it. This targets the
  `intent_states_selected_distinction` failures.
- **Unstated qualifier → general candidate.** If the user states no
  qualifier, prefer the general candidate and do not assume one. This keeps
  UNSTATED_QUALIFIER_SPECIFICITY guarded.
- **Explicit NONE criterion:** NONE only when no candidate addresses the
  topic, or the message states no topic.

**Variant B: `variant_b_none_threshold_ablation`** (contract `885f20a0…`)

- **Rules kept:** the production verification rules 1–6 and the
  general/specific rule (in abstract form).
- **Additions only:** the short-message tolerance and the explicit NONE
  criterion.
- **What the pair isolates:** B vs production isolates the NONE threshold.
  A vs B isolates the bidirectional qualifier guidance. Dropping the example
  is a shared confound.

Neither variant is justified for general/specific *strengthening*: only 2
unambiguous rule violations exist.

## 11. Next live experiment (proposal only; `experiment-plan.json`)

The plan uses OpenRouter only, with the current model
`openrouter / openai/gpt-4o-mini`. `live=false`, `approved=false`.

| Step | Config | Cases | New calls |
|---|---|---|---:|
| 1 | Production prompt | DEV + HOLDOUT | 0 (Stage A run reused) |
| 2 | Variant A | DEV | 95 |
| 3 | Variant B | DEV | 95 |
| 4 | One variant chosen by human review | HOLDOUT (scored once) | 42 |

- **Total:** **232 calls** if all steps run. The Stage A actual usage was
  about 1.9 k input tokens per call, so roughly 0.43 M input tokens
  (approximate).
- **Cost:** PRICE_REQUIRED.
- **Separate approvals:** steps 2–3 and step 4 each need their own approval.
- **Prerequisite:** harness support for running a variant system prompt,
  with the result namespace keyed by the variant's contract fingerprint.
  This is not built yet; it belongs to the prompt-experiment phase.
- **Reading the results:**
  - Primary reading: DEV rescue/corruption and false NONE, plus the
    `intent_states_selected_distinction` and G/S rows.
  - The 8 rescues must not be lost.
  - Improvements on group-B cases should be read as "agrees with the alias
    Gold", not as "more correct".

**Alternative model:** not needed yet. Separate the prompt effect first,
then run the best prompt with a stronger OpenRouter model on the same
HOLDOUT. That model must be registered and qualified first; no model id is
proposed here.

**Reasoning:** not needed yet. It requires a registered reasoning-capable
OpenRouter model and a prompt baseline.

## 12. Stage B full-run recommendation

**`DO_NOT_RUN_FULL_CURRENT_CONFIG`**

- **Low information gain.** On the challenge set the current config
  corrupts 63% of rank-1-correct cases. Most of the 482 full-set cases are
  rank-1-correct alias strings, so a full run would mainly re-measure the
  same over-strict NONE and Gold/contract mismatch at about 3.5× the calls.
- **Which run to prefer.** A full run is informative only for a
  configuration that first shows net corrections ≥ 0 on DEV and HOLDOUT.

## Tests

```text
test_selector_postmortem.py (new):                  6 passed
benchmark modules (7A + 7B-Prep + Stage A + postmortem): 72 passed
Full backend suite, repository-root layout:        464 passed, 0 failed
```

**What the new tests cover:**

- accounting of every informative case: corruption, unresolved, rescue and
  false NONE, with nothing dropped and mismatch groups summing to the total;
- the rule-level taxonomy on a synthetic fixture;
- the false-NONE and specificity tables;
- alias-structure counts;
- the split: disjoint, complete, stratified, deterministic and
  order-independent;
- the prompt variants: at most 2, and no ids, KB question text or benchmark
  wording;
- the production contract fingerprint is unchanged;
- a module-wide network guard.

## Artifacts (git-ignored)

`outputs/selector-v2-benchmark/postmortem-v1/`:

- `failure-cases.jsonl` (86 full case packets with all candidates, offline
  retrieval metadata and classification)
- `false-none.jsonl` (41)
- `specificity-cases.jsonl` (17)
- `rescues.jsonl` (8)
- `review-queue.json`
- `taxonomy-summary.json`
- `prompt-experiment-split.json`
- `experiment-plan.json`

## Known limitations

- **Lexical heuristics.** The taxonomy uses lexical evidence (stems,
  thresholds 2 and 0.5). It is deterministic and conservative, but lexical
  support is not semantic truth. 37 failures remain NEEDS_HUMAN_REVIEW by
  design.
- **Holdout exposure.** The HOLDOUT was seen by the analyst; see the
  holdout caveat in §9.
- **Small samples.** Every sample is small, and the cases share few
  expected QnA ids.
