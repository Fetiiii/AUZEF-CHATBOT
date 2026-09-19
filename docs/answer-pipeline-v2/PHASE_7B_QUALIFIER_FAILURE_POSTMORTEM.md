# Answer Pipeline V2 — Phase 7B Qualifier Failure Postmortem & Order-Bias Prep

## Status

```text
Phase 7B-Qualifier Failure Postmortem — PASS (offline; 0 live calls)
```

- **Branch:** `production-readiness`; starting HEAD `7818ae0`.
- **Live calls: 0.** Every analysis reads the frozen snapshot, Semantic Gold V1
  (`36ab1d6f…`, recomputed on load) and the SAVED outputs listed below. No
  provider was constructed; a test enforces this.

| Saved outputs used | Run |
|---|---|
| Stage A production | `9dfc72c140dc7e93` |
| Variant A DEV | `f47cf095e441a6e8` |
| Variant A HOLDOUT | `semantic-holdout-a` |

- **Unchanged:**
  - the Variant A HOLDOUT result (FAIL) and every file under
    `semantic-holdout-a/` (sha256 recorded in `holdout-immutability.json`,
    re-checked by test);
  - the variant_a_v1 prompt and the prompt manifest;
  - the Semantic Gold;
  - the production selector contract (`3f49b198…`).
- **No Gold change and no rescore in this phase.**
- **Artifacts:** `outputs/selector-v2-benchmark/qualifier-postmortem/` (git-ignored).

## 1. 471 / 472 blind re-adjudication packet

**Location:** `qualifier-postmortem/review/`, with `review-packet.md`,
`review-template.csv`, `review-cases.jsonl` and `manifest.json`. The packet
fingerprint is `bf73542d…`.

| Case | User text | Candidates shown |
|---|---|---:|
| 471 | "Yatay geçiş nasıl yaparım" | **9 (all eligible)** |
| 472 | "Yatay geçiş yapmak istiyorum" | **6 (all eligible)** |

**What the reviewer sees:**

- the case id and the user text;
- per candidate: an anonymous label, the canonical question and the curated
  answer.

**Candidate order** is `sha256(case_id|candidate_ref)`, the same neutral
labelling as the semantic review. It is recomputable from the snapshot and is
not the retrieval order; a test asserts both.

**Hidden from the reviewer:**

- model decisions (production and Variant A);
- first candidate, retrieval rank and score;
- current Gold and qna refs;
- taxonomy and the HOLDOUT result.

These are enforced by:

- a field whitelist;
- a record-ref check for every candidate ref of both cases;
- a forbidden-word scan (the semantic review's list plus `holdout`,
  `rank`, `score`);
- a test.

**Decision schema:** SELECT_ACCEPTABLE (with labels), EXPECT_NONE,
EXCLUDE_AMBIGUOUS, CONTENT_REVIEW_REQUIRED, RETRIEVAL_OR_KB_MAPPING_REVIEW.
The review status is NOT_STARTED; nothing was decided or changed.

**After review:**

- the decisions are locked;
- a child Gold is derived from them;
- a separate `post_adjudication_rescore` of the saved HOLDOUT responses is
  written to a new directory.

The historical `PHASE_7B_VARIANT_A_HOLDOUT_REPORT.md` and
`semantic-holdout-a/` are never overwritten.

## 2. Qualifier-sensitive inventory (model-independent)

**Definition.** A case is qualifier-sensitive when both of these hold:

- its frozen candidate set holds a candidate **S** whose canonical question
  carries a qualifier concept *q*;
- it also holds a sibling **G** without *q*, where S and G share a topic stem
  (qualifier spans removed) that the user text also contains.

The case is **UNSTATED** if the user text lacks *q*, and **STATED** otherwise.

**Scope of the rules:**

- only canonical questions and user text are read, never answers, Gold,
  ranks or model outputs;
- the lexicon is a **taxonomy of generic academic qualifier concepts**:
  merkezi, sınavsız, ikinci üniversite, DGS, YKS, uzaktan, açıköğretim,
  yüksek lisans, önlisans, lisans, final, bütünleme;
- the lexicon is not a case list; a test asserts that no case id appears in
  it;
- compound nouns are excluded ("sınav merkezi", "Çözüm Merkezi", …).

**Inventory size:** 206 cases. The first draft had 215; excluding the
compound nouns removed 9 "sınav merkezi"-type false positives.

| | Cases |
|---|---:|
| **Total qualifier-sensitive** (primary) | **206** |
| DEV | 46 |
| Old HOLDOUT | 18 |
| Outside the challenge set | 142 |
| UNSTATED / STATED | 155 / 51 |
| Semantic-evaluable | 192 |
| Excluded | 14 (4 ambiguous, 2 content review, 8 retrieval/KB review) |

**Semantic Gold expectation relative to the qualifier pairs:**

| Mode | GENERAL | SPECIFIC | MULTI | NONE | OTHER | EXCLUDED |
|---|---:|---:|---:|---:|---:|---:|
| UNSTATED | 90 | 18 | 3 | 1 | 31 | 12 |
| STATED | 10 | 20 | 5 | 0 | 14 | 2 |
| **Total** | **100** | **38** | 8 | 1 | 45 | 14 |

**OTHER** means the accepted answer is not a member of a detected pair. This
is a heuristic, so it is broad by design.

**Phase 7A general/specific pairs.** Pair membership here means both members
are in the frozen candidate set.

| Pair | General | Specific | Qualifier | Cases | Semantic Gold |
|---|---|---|---|---:|---|
| 310-405 | qna:310 "Kayıt tarihleri ne zamandır?" | qna:405 "İkinci Üniversite sınavsız kayıt tarihleri ne zaman?" | ikinci üniversite, sınavsız | 10 | GENERAL 2, SPECIFIC 6, OTHER 2 |
| 129-342 | qna:129 "Yatay geçiş işlemleri ile ilgili detaylı bilgiye nereden ulaşabiliriz?" | qna:342 "Merkezi yatay geçiş başvurusu nasıl yapılır?" | merkezi | 14 | GENERAL 4, SPECIFIC 2, MULTI 1, OTHER 6, EXCLUDED 1 |

## 3. Candidate order (retrieval order; model-independent)

These counts cover the 192 semantic-evaluable qualifier cases.

| | Cases |
|---|---:|
| Position 1 accepted | **166** |
| Position 1 not accepted | **26** |
| Position 1 too specific (carries an unstated qualifier and is not accepted) | 9 |
| General expected | 100 |
| Specific expected | 38 |
| **General expected, but position 1 is the more specific candidate** | **6: 465, 469, 470, 471, 472, 473** |

**In all six cases, position 1 is the same candidate:** `qna:342` "Merkezi
yatay geçiş…". Retrieval ranks it above the general yatay geçiş answer
`qna:129` for every unqualified yatay geçiş query in this set.

| Case | Variant A | Production |
|---|---|---|
| 470 | qna:342 | NONE |
| 471 | qna:342 | qna:342 |
| 472 | qna:342 | qna:342 |
| 473 | qna:100 (açıköğretim), not position 1 | qna:100 (açıköğretim), not position 1 |
| 469 | qna:131, correct | NONE |
| 465 | qna:131, wrong | qna:131, wrong |

In 473 both selectors assumed a different unstated qualifier, açıköğretim,
off position 1.

## 4. 471 / 472 engineering diagnostics (not in the review packet)

| Position | 471 candidate | Class | 472 candidate | Class |
|---:|---|---|---|---|
| 1 | qna:342 Merkezi yatay geçiş başvurusu… | SPECIFIC (merkezi) | qna:342 | SPECIFIC |
| 2 | **qna:129** Yatay geçiş işlemleri… detaylı bilgi | GENERAL (current Gold) | **qna:129** | GENERAL (current Gold) |
| 3 | qna:326 Bölüm değişikliği | unrelated | qna:326 | unrelated |
| 4 | qna:130 Hangi yarıyıllarda yatay geçiş | GENERAL | qna:130 | GENERAL |
| 5 | qna:135 Yatay geçişte ders muafiyeti | GENERAL | qna:100 Açık öğretimden örgüne | SPECIFIC (açıköğretim) |
| 6 | qna:131 Yatay geçiş başvuru tarihleri | GENERAL | qna:131 | GENERAL |
| 7 | qna:100 Açık öğretimden örgüne | SPECIFIC (açıköğretim) | — | |
| 8 | qna:307 Kaçıncı sınıftayım | unrelated | — | |
| 9 | qna:235 Yatay geçişe engel olmadığına dair belge | GENERAL | — | |

**Current Semantic Gold and positions:**

- The current Semantic Gold is `qna:129`, inherited from the parent reviewed
  Gold and not re-adjudicated.
- `qna:129` is at retrieval position **2** in both cases, and `qna:342` at
  position **1**.
- **Under the neutral order**, qna:342 moves to position 2 (471) or 5 (472)
  and qna:129 to position 3 or 6. qna:342 still precedes qna:129 in both, so
  the neutral condition tests **leaving position 1**, not a reversal.
- **Other general candidates:** several other general yatay-geçiş candidates
  exist (130, 131, 135, 235). The blind review may therefore produce a
  multi-acceptable set.

## 5. First-position association (saved outputs only)

**Groups** (qualifier set, semantic-evaluable):

- Group A (position 1 accepted): 166 cases;
- Group B (position 1 not accepted): 26 cases.

Saved outputs exist for 51 of these cases, the challenge members. Every
outside-challenge qualifier case is in Group A.

| Qualifier set | Production | Variant A |
|---|---:|---:|
| Chose position 1, overall (51) | 0.294 | **0.431** |
| Chose position 1, Group A (26) | 0.500 | 0.692 |
| Chose position 1, Group B (25) | 0.080 | 0.160 |
| Group B wrong SELECT | 15 | 21 |
| **Group B wrong SELECT at position 1** | **2** (471, 472) | **4** (366, 470, 471, 472) |
| Share of wrong SELECTs at position 1 vs uniform chance | 0.13 vs 0.12 (×1.13, p = 0.54) | 0.19 vs 0.12 (×1.63, p = 0.23) |
| Group B NONE outputs | 8 | 1 |

On the full challenge set (115 semantic-evaluable), wrong SELECTs at
position 1 were: production 4/25 (×1.23, p = 0.41); Variant A 6/33
(×1.46, p = 0.22).

**Pre-declared rule** (in `order_bias_verdict`):

- **STRONG:** a share of at least 0.5, a ratio of at least 2 and p < 0.05;
- **WEAK:** a ratio below 1.5 or p ≥ 0.2;
- **MIXED:** anything else.

**Verdict: `ORDER_BIAS_WEAK`** for both runs, on both sets.

**Reading the numbers:**

- **Most errors are not at position 1.** 81% of Variant A's wrong SELECTs in
  Group B pick a non-position-1 candidate.
- **The qualifier failure survives off position 1.** In 473 the accepted
  answer is at position 4 and both selectors pick qna:100, an unstated
  "açıköğretim" candidate that is not at position 1. The unstated-qualifier
  error is a content/prompt behaviour, not only a slot effect.
- **The rate gap is not an anchoring finding.** A's higher position-1 rate is
  confounded with its near-absence of NONE (Group B NONE: 1 vs 8).
- **Confound:** position 1 is also the retrieval top hit. Offline data cannot
  separate anchoring from a strong distractor; only a neutral-order run can.

## 6. Prompt-rule compliance (deterministic heuristic, no LLM judge)

These counts use the saved outputs on the 115 semantic-evaluable challenge
cases.

| Label | Production | Variant A |
|---|---:|---:|
| COMPLIANT_GENERAL | 36 | 51 |
| COMPLIANT_SPECIFIC | 4 | 5 |
| CORRECT_MULTI_ACCEPTABLE | 10 | 13 |
| **UNSTATED_QUALIFIER_ASSUMED** | **4** (328, 471, 472, 473) | **8** (205, 210, 211, 466, 470, 471, 472, 473) |
| OTHER_SELECTOR_ERROR | 30 | 35 |
| NONE | 31 | 3 |

On the qualifier set only, Variant A has 5 UNSTATED_QUALIFIER_ASSUMED.

**Variant A's "do not assume unstated qualifiers" rule did not reduce this
error. The count doubled (4 → 8).** Its aggregate gain came from answering
instead of NONE, and some of those answers assume a qualifier.

**Review queue:** 10 rows where the heuristic is uncertain, listed in
`prompt-rule-compliance.json`. Examples:

- a wrong pick carries an unstated qualifier that the accepted answer also
  carries;
- a correct pick carries an unstated qualifier.

## 7. Candidate-order experiment — prepared, NOT run

| Condition | Definition |
|---|---|
| **ORIGINAL_ORDER** | variant_a_v1 with the current retrieval order |
| **NEUTRAL_ORDER** | the same prompt, candidates, candidate content and model config, with the order set to `sha256('selector-neutral-order-v1\|case\|ref')` |

- **Single variable:** candidate order. The new `order="neutral"` in
  `contract.order_candidates` is benchmark-only. Production order and the
  selector contract fingerprint are unchanged.
- **Verification:** for every diagnostic case, the two requests have the
  same system prompt, the same resolved intent and the same multiset of
  candidate objects, and a different order. This is checked at build time
  and in tests.
- **Retrieval data in the prompt:** retrieval rank and score are never
  serialized.

**Diagnostic set** (`diagnostic-set.json`, fingerprint in the plan):

- **Size:** 38 cases. **Not a validation set**, and never used for
  promotion.
- **Selection** (model-independent):
  - 471 and 472 (forced);
  - semantic-evaluable qualifier-sensitive cases;
  - **only already-consumed DEV/old-HOLDOUT cases** (asserted), so the unused
    pool stays independent;
  - Gold GENERAL, SPECIFIC or MULTI;
  - ordering informative: the neutral order flips the accepted-vs-rival order
    or moves a pair member off position 1.

**Planned budget** (plan `333353e4…`, not approved): ORIGINAL_ORDER 38 +
NEUTRAL_ORDER 38 = **76 calls**. Saved ORIGINAL_ORDER outputs exist, so
reusing them would halve this, at the cost of provider-drift control.

## 8. Recommendation

**Verdict by the pre-declared rule: `ORDER_BIAS_WEAK`.**

**Recommended next experiment:** a **Variant C qualifier-contract prompt**.

- **Test design:** the prompt is the only variable. Keep the original order,
  gpt-4o-mini and the same config.
- **Where:** develop on DEV. Check 471/472 and the qna:342 family only as
  known regressions.
- **Prerequisite:** the 471/472 blind review, so the regression targets are
  reviewed.

**The order experiment is a separate control.** It is prepared at 76 calls
and should run only as its own experiment, never combined with a prompt or
model change.

**Observation outside the selector:** retrieval puts qna:342 first for every
unqualified yatay geçiş query in the set. A KB or retrieval review of the
general answer qna:129 (its canonical question is weak lexically) is an
independent remedy. It must not be mixed into a selector experiment.

## Tests

- **New:** `tests/test_selector_qualifier_postmortem.py`, **8/8** passed.
  - **471/472 packet:** all candidates present; deterministic rebuild; no
    model-output, Gold or retrieval metadata contamination; labels in hash
    order, not retrieval order; immutable hashes.
  - **Neutral order:** deterministic and independent of input order;
    production order untouched.
  - **Order conditions:** they differ only in order, and the check is
    sensitive to content changes. The plan records 0 actual calls.
  - **Diagnostic set:** inside DEV ∪ HOLDOUT and disjoint from the unused
    pool.
  - **Lexicon:** a taxonomy, with compound-noun exclusions.
  - **End-to-end offline run:** reads saved outputs only; no provider can be
    constructed; the network is guarded; artifacts are byte-identical.
  - **Unchanged:** HOLDOUT artifacts (tree sha256), Variant A prompt, prompt
    manifest, Semantic Gold and the production selector contract.
- **Full backend suite:** 531 passed, 0 failed (523 → +8). Registry, KB and alias digests unchanged.
