# Answer Pipeline V2 — Phase 7B Semantic Adjudication Result

## Status

```text
Phase 7B-Semantic Adjudication Review — PASS (106/106 locked)
Phase 7B-Semantic Gold V1 — PASS (child artifact; parent unchanged)
```

- Branch: `production-readiness`
- Starting HEAD: `8807272`
- **ZERO LIVE CALLS.** Everything ran offline inside the harness network
  guard.
- **No production change:** production prompt, model, registry config, KB
  (`qna` digest `df67859d…`) and alias table (`qna_queries` digest
  `66b014e7…`) were all identical before and after.

## Review lock (before any audit access)

- **Human decision input:** `semantic-adjudication-decisions-106.csv`, sha256
  over the raw bytes
  **`b88ef9a8e02f232d1a48f9b9e5c3ce8824955488d86507ec1ca93e2f3d7fdab8`**
  (verified). A first attempt hashed newline-translated text and was refused
  by the sha check; the hash is now taken over raw bytes.
- **Checks passed before the lock:**
  - 106 rows with 106 unique ids, equal to the blind packet;
  - no blank and no `NEED_FULL_CANDIDATES`;
  - every SELECT has at least one label and non-SELECT rows have none;
  - every label exists in its case's blind packet;
  - the FULL_CANDIDATE_FOLLOWUP rows are exactly the 19 follow-up ids, and
    each was NEED_FULL_CANDIDATES in the first pass;
  - the 87 FIRST_PASS rows are unchanged (decision and labels) against the
    stored first-pass copy.
- **Lock:** `outputs/selector-v2-benchmark/semantic-adjudication-v1/locked/`
  (`locked-review.jsonl`, `lock-manifest.json`).
  - **Lock fingerprint:**
    **`0fbc3bd47f157d4ddc7951720fa7c7abe84b86424a06c6601cde82f211456d60`**.
  - **Bound to:** parent packet `2c229e4f…`, follow-up `078bec9f…` and
    snapshot `3e558768…`. The challenge `bed2dad1…` was also verified.
  - **Write-once:** re-verified on every load.
- **Audit access:** `audit-view.jsonl` was opened for the first time **after**
  the lock, through `open_audit_after_lock`. That function refuses to open
  it without a valid lock, which is tested.
- **Label → ref mapping:**
  - **How:** recomputed from the snapshot with the blind labelling rule
    (`sha256(case_id|candidate_ref)`) and cross-checked against the audit
    mapping; a mismatch aborts.
  - **Never used:** model outputs and retrieval order.
  - **Result:** all 106 mappings agreed.

| Human blind decision | Count |
|---|---:|
| SELECT_ACCEPTABLE (15 with several labels) | 82 |
| RETRIEVAL_OR_KB_MAPPING_REVIEW | 11 |
| EXCLUDE_AMBIGUOUS | 10 |
| CONTENT_REVIEW_REQUIRED | 2 |
| EXPECT_NONE | 1 |

The review ran in two rounds: FIRST_PASS 87 and FULL_CANDIDATE_FOLLOWUP 19.

## Derived adjudication outcomes

The outcomes compare the human's refs with the current Gold, after the lock.

| Outcome | Cases | Child Semantic Gold |
|---|---:|---|
| KEEP_CURRENT | 46 | unchanged (includes 7 multi-label selections equal to an already multi-acceptable Gold) |
| CHANGE_GOLD | 28 | single new acceptable ref |
| MULTI_ACCEPTABLE | 8 | the human's acceptable set |
| EXPECT_NONE | 1 (case 412) | expected NONE; the first reviewed NONE case |
| EXCLUDE_AMBIGUOUS | 10 | `EXCLUDED`, reason `ambiguous_user_intent` |
| CONTENT_REVIEW_REQUIRED | 2 | `HOLD`, reason `content_review_required` |
| RETRIEVAL_OR_KB_MAPPING_REVIEW | 11 | `HOLD`, reason `retrieval_or_kb_mapping_review` (never turned into NONE) |

**Old Gold accounting:**

| Old Gold status | Cases |
|---|---:|
| Still acceptable | 56 |
| **Rejected** | **27** |
| Not assessed (excluded, content, KB) | 23 |
| Absent from the candidate set | 2 (74, 436) |

The 27 rejected cases are 78, 79, 82, 83, 85, 92, 118, 166, 174, 175, 176,
177, 189, 201, 254, 320, 322, 323, 326, 330, 337, 412, 436, 465, 505, 506 and
507.

**Queues** (written as artifacts; the KB was not touched):

| Queue | Cases |
|---|---|
| Ambiguous-excluded | 141, 160, 172, 179, 355, 376, 404, 410, 418, 461 |
| Content review | 325, 408 |
| Retrieval/KB mapping | 19, 74, 158, 184, 215, 221, 230, 375, 383, 384, 415 |

## Blind-control analysis

The 20 hash-selected controls came from cases the production selector got
right, and were revealed only after the lock:

| Outcome | Controls |
|---|---:|
| KEEP_CURRENT | **19** |
| EXCLUDE_AMBIGUOUS | 1 |
| CHANGE_GOLD / MULTI / NONE / content / KB | 0 |

This is strong evidence that the reviewer did **not** rewrite the Gold where
it was already fine.

**Limitation:** the control bounds only that direction. The 86 scope cases
were chosen from production's failures, so absolute re-scored numbers are
**not** an unbiased estimate of true accuracy. The **A-vs-B** comparison is
the least confounded: neither variant influenced the scope, the packet or the
review, and model-blindness is test-enforced.

## Alias impact (descriptive; alias table unchanged)

| | Cases |
|---|---:|
| Old Gold came from alias ownership (intent text is an alias of the old Gold) | 89 of 106 |
| Alias collisions (alias owner ≠ old Gold) | 17 |
| Human judged the candidates and rejected an alias-derived old Gold | **25** |
| Alias-derived old Gold where the human did not judge (excluded, content, KB) | 20 |

## Semantic Gold V1

- **Location:** `outputs/selector-v2-benchmark/semantic-gold-v1/` (git-ignored).
- **Files:** `semantic-gold.jsonl`, `manifest.json`, `changes.jsonl` (106
  provenance rows), `excluded-cases.jsonl`, `content-review-queue.jsonl`,
  `retrieval-kb-review-queue.jsonl`.
- **Child fingerprint:**
  **`36ab1d6f06b990b247f8fee882acd0aba3d4f235ec0ecafc65504c3ffc9ea713`**
  (deterministic).
- **Parent:** snapshot `3e558768…`; reviewed Gold `gold-reviewed-all.jsonl`
  sha256 `19fdff59…`. Both are unchanged.
- **Scope of change:** only the 106 adjudicated cases change; every other
  case is inherited from the parent unchanged.
- **Provenance per reviewed case:**
  - previous and new expectations;
  - the human blind decision and labels;
  - the derived outcome;
  - note, reviewer, timestamp and round;
  - the review lock and parent fingerprints.
- **Regeneration note:** the directory was regenerated once, before commit,
  after an accounting-only fix. The alias-disagreement count now covers only
  cases the human actually judged: 45 → 25 disagreements plus 20 not
  assessed. The Semantic Gold fingerprint was identical (`36ab1d6f…`), and
  the child cases did not change.

**Denominators (primary cases):**

| | Parent | Semantic |
|---|---:|---:|
| Evaluable | 482 | **461** |
| Expected SELECT | 482 | 460 |
| Expected NONE | 0 | **1** |
| Multi-acceptable | 20 | 22 |
| Ambiguous-excluded | — | 10 |
| Content-review hold | — | 2 |
| Retrieval/KB hold | — | 11 |
| Former retrieval misses | 2 | 436 now evaluable (the human chose a pool candidate); 74 moved to the retrieval/KB hold |

Existing exclusions are unchanged: 17 context-required, 9 excluded from
evaluation, and 4 holds or pending. The reconciliation is
482 − 22 (new exclusions among the parent-evaluable cases; 74 was not one of
them) + 1 (436) = 461.
