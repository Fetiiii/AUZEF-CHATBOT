# Answer Pipeline V2 — Phase 7B Final Validation Strategy

## Status

The strategy is proposed; no IDs are frozen and no model calls were made.

The old HOLDOUT (42 cases) is **consumed**: Variant A was validated on it
once, and it failed. It must never again be used for promotion.

| Set | Allowed use |
|---|---|
| Old DEV (95) | Tuning and development only |
| Old HOLDOUT (42) | Known-regression checks only (471/472, the qna:342 family) |
| Final validation | A NEW set whose case IDs are frozen, blind-adjudicated and fingerprinted **before any prompt or model output on it exists** |

## Contamination accounting (frozen benchmark)

| | Cases |
|---|---:|
| Full frozen primary cases | 514 |
| Semantic-evaluable primary cases | 461 |
| Previous DEV | 95 |
| Previous HOLDOUT | 42 |
| Semantic-adjudication reviewed | 106 |
| Postmortem-inspected (Stage A failures, specificity cases, review queues, 471/472) | 94 |
| **Union of used cases (deduplicated by case and by source query)** | **139** |
| **Remaining candidate pool** (semantic-evaluable, unused) | **345** |

The remaining pool is written to `final-validation/contamination.json`.

**What the pool is:**

- **Easy by construction.** The challenge set was defined as the union of
  these case types plus an easy control:
  - first candidate wrong;
  - near-QnA;
  - general/specific;
  - KB overlap;
  - multi-acceptable.

  So every hard-slice case was consumed.
- **Consequence:** the pool contains **0** near-QnA, **0** general/specific,
  **0** KB-overlap and **0** multi-acceptable cases. The first candidate is
  correct in **345/345** under the current (unreviewed) Gold.
- **Unreviewed:** none of the 345 cases has been semantically adjudicated.
  Their Gold is the alias-derived parent Gold. In the reviewed scope that Gold
  was rejected in 27/106 cases, so adjudication may lower the first-candidate
  rate here too.
- **Qualifier-sensitive cases:** 140 by the inventory v1 detector, of which
  100 are "qualifier_core" (the Gold is a qualifier-pair member).

**Answer to "is there enough independent material?"**

- **For a regression and qualifier check on common queries: yes.** The pool
  has 345 unused cases, including 100 qualifier-core cases.
- **For measuring hard-case selector quality: no.** The frozen benchmark has
  no unused hard-slice cases left. A final validation built only from it
  would reward a "pick position 1" selector with a perfect score (under the
  current Gold).

## Proposed final validation (two parts, both required for promotion)

### Part 1 — frozen-benchmark remainder (proposal V1)

Stratified sample of the unused pool (`final-validation/proposal.json`):

- **Size:** target 120, proposed **120**.
- **Composition:** all 100 qualifier_core cases, 4 qualifier_other and 16
  other. Cases are ordered by `sha256('selector-final-validation-proposal-v1|case_id')`.
- **Proposal fingerprint:** `c5f710ab…`.
- **Status:** PROPOSED. The case IDs are not yet frozen.

### Part 2 — new production-like reviewed query set

- **Source:** real user queries not already in the frozen benchmark, for
  example recent query logs. They go through the same frozen retrieval to
  get candidate sets, with no model involved.
- **Selection:** model-independent. Stratify on retrieval-side features only,
  and oversample hard slices:
  - qualifier-sensitive cases (inventory detector);
  - near-QnA pairs;
  - multi-candidate topics.
- **Target:** about 120 cases, so the final set can measure what Part 1
  cannot.

### Freeze protocol (for both parts, before any model output)

1. Freeze the case IDs and candidate sets, then fingerprint them.
2. Run a blind semantic human adjudication:
   - show all candidates;
   - use anonymous labels and hash order;
   - hide Gold, ranks and model outputs;
   - use the same decision schema as the semantic review.
3. Lock the review, derive the child Gold and fingerprint it.
4. Freeze the promotion gate and the comparator (production). Freeze the
   production baseline if it needs new calls, under its own approval.
5. Only then run the single candidate under an explicitly approved call
   budget. Run it once; there is no tuning afterwards.

**Promotion requirements:**

- Pass on both parts.
- No regression on the old-HOLDOUT known-regression cases.
- Old-HOLDOUT results are reported **as regressions only**, never as
  accuracy.

## Not done in this phase

- No final-validation IDs were frozen.
- No adjudication was performed.
- No model calls were made.
- Part 2 was not sampled.
