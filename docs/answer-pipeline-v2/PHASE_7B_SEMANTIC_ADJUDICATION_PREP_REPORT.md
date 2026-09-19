# Answer Pipeline V2 — Phase 7B Semantic Adjudication Prep Report

## Status

```text
Phase 7B-Semantic Adjudication Prep — blind human review packet + apply/re-score tooling
STATUS: PASS

Phase 7B-Semantic Adjudication Review — WAITING_FOR_HUMAN_REVIEW
```

- Branch: `production-readiness`
- Starting HEAD: `1187fa1`
- **ZERO LIVE CALLS.** The whole phase ran offline inside the harness
  network/SDK guard. The only external read was the `qna_queries` alias
  table, read-only, for the secondary audit view.
- **Nothing existing changed:**
  - production runtime (no `backend/services` diff);
  - the reviewed Gold (parent);
  - the Stage A outputs;
  - the Variant A/B outputs and prompts;
  - the prompt gate.
- **Not run:** no HOLDOUT, no Stage B, no model, metadata or alias
  change. Phase 7C was not started.

## Why adjudication

Stage A, the postmortem and the prompt DEV experiment all point to the same
gap between the benchmark Gold, which is derived from the KB alias history,
and the user's actual semantic need:

- **Prompt DEV results:** production 34/95, Variant A 54/95, Variant B
  45/95.
  - **What A improved:** it removed 20 corruptions and 24 false NONEs and
    broke no production-correct case.
  - **What still fails:** net corrections stay negative against the
    alias-owner baseline.
- **Where the failures sit:** the postmortem found only 2 clear selector
  errors. The rest are 18 Gold/alias-questionable, 29 contract-mismatch and
  37 undetermined failures.

The next question is therefore not "does the model hit the Gold" but
**"which candidate(s) actually satisfy this user message?"**. That has to be
answered by humans, independently of any model output.

## Review scope

The scope is deterministic (`adjudication.review_scope`). It reads only case
ids and taxonomy groups from the postmortem.

| Source | Cases |
|---|---:|
| GOLD_ALIAS_QUESTIONABLE (postmortem group B) | 18 |
| CONTRACT_MISMATCH (group C) | 29 |
| NEEDS_HUMAN_REVIEW (group U) | 37 |
| KB_OVERLAP_INTRINSIC (subset of the above) | 5 |
| Postmortem-recommended Gold/alias review (`intent_states_selected_distinction`, subset of the above) | 18 |
| Existing review queue (404, 19, 221, 35, 158, 74, 436) | 7 (adds 74 and 436; the others were already in scope) |
| **Unique scope** | **86** |
| Blind control (see below) | 20 |
| **Packet total** | **106** |

- **Clear selector errors:** CLEAR_SELECTOR_ERROR cases (471, 472) are not
  Gold problems. They are recorded as a diagnostic and kept out of scope.
- **Retrieval misses:** 74 and 436 are Phase 7A retrieval misses. Their
  current Gold is not in the candidate pool, and the reviewer can decide
  from the pool that is there.

**Blind control.** The scope comes from cases the *production* selector got
wrong. Correcting the Gold only there would bias every later re-score toward
whatever model disagreed.

- **Selection:** 20 challenge cases outside the scope, excluding the clear
  errors, ordered by `sha256(schema|control|case_id)`.
- **Blindness:** they are mixed into the packet, and the reviewer cannot
  tell scope from control.
- **Where the reasons live:** scope reasons appear only in the secondary
  audit view.

## Anti-model-bias design

**Primary view** (`review-cases.jsonl`, `review-packet.md`,
`review-template.csv`). It contains only:

- `case_id` and the user text (`intent_text`);
- the candidates under anonymous labels **A, B, C, …**, each with its
  **canonical question and curated answer**;
- `candidate_view_complete` and `all_candidate_count`;
- `all_candidates` (every candidate in the same blind format, for
  NEED_FULL_CANDIDATES).

**Hidden from the reviewer** (enforced by `primary_view_violations` at build
time and by tests):

- every model decision (production, Variant A, Variant B) and prompt names;
- the first-candidate choice and correctness;
- rescue, corruption and value class;
- retrieval rank, score, source and stage;
- candidate refs (`qna:<id>`);
- taxonomy and scope reason;
- **the current Gold**;
- **alias provenance**.

**Candidate order:** `sha256(case_id | candidate_ref)`, which is neutral to
retrieval rank. Labels are stable over the full candidate list. This does not
change the benchmark runtime order.

**Secondary audit view** (`audit-view.jsonl`, to be opened only **after** all
blind decisions are recorded) holds:

- the label → ref map and the retrieval rank per label;
- the current Gold (refs, labels, and whether it is in the pool);
- alias owners and the alias-collision flag (17 cases);
- prior review decision and tags;
- scope reasons and pool status.

**Model outputs are in neither view.** A production/A/B comparison is allowed
only after the adjudication is locked, as a separate diagnostic.

## Candidate subset (deterministic, model-independent)

A case can have up to 22 candidates, so the primary view shows a bounded
subset (`candidate_subset`). It is the union of:

1. all current acceptable refs present in the pool, always shown;
2. all near-QnA siblings present;
3. the top 5 in retrieval order (rank hidden);
4. the top 3 by lexical overlap between the user text and the canonical
   question.

**Model selections are never used.**

| Property | Value |
|---|---|
| Shown per case | 5–9 |
| Complete view | 1 of 106 cases |
| Incomplete view | 105 of 106 cases, flagged `candidate_view_complete=false` |

When the view is incomplete, the reviewer can answer `NEED_FULL_CANDIDATES`
and use `all_candidates`. The Gold is never changed because of a truncated
subset.

## Decision schema

**Blind decision**, recorded by the reviewer without seeing the Gold:

| Blind decision | Meaning |
|---|---|
| `SELECT_ACCEPTABLE` + `acceptable_labels` | The label(s) meet the practical need. Several labels = several acceptable answers for **one** intent (not multi-intent) |
| `EXPECT_NONE` | None of the shown candidates reasonably meets the need. **Not** for "unsure which" (use several labels) and not for unclear messages |
| `EXCLUDE_AMBIGUOUS` | The message is too ambiguous to benchmark |
| `CONTENT_REVIEW_REQUIRED` | The intent is clear, but the KB content is deficient |
| `RETRIEVAL_OR_KB_MAPPING_REVIEW` | The right answer is absent, or the mapping is clearly wrong |
| `NEED_FULL_CANDIDATES` | The shown subset is insufficient |

**Final decision**, derived after the review is locked by comparing labels
with the current Gold, so the reviewer never needs to know the Gold:

- `KEEP_CURRENT`: the labels equal the current Gold.
- `CHANGE_GOLD`: a single, different label.
- `MULTI_ACCEPTABLE`: several labels different from the Gold.
- The other blind decisions carry over unchanged (EXPECT_NONE,
  EXCLUDE_AMBIGUOUS, CONTENT_REVIEW_REQUIRED, RETRIEVAL_OR_KB_MAPPING_REVIEW,
  NEED_FULL_CANDIDATES). An empty row is UNREVIEWED.

Invalid rows are rejected: unknown labels, SELECT without labels, labels on
a non-SELECT decision, or an unknown decision.

**Review rules** (in the packet README):

- **Practical answer.** Judge the question **and** the answer. Exact wording
  is not required, but the same topic alone is not enough.
- **General vs specific.**
  - Never assume a qualifier the user did not state. For example, "Yatay
    geçiş yapmak istiyorum" does not make the "merkezi" candidate expected.
  - When the user explicitly states a qualifier ("ikinci üniversite",
    "sınavsız", "merkezi"), the specific candidate is natural.
  - The historical alias mapping does not make the current Gold right.

## Artifacts, fingerprint, immutability

**Location:** `outputs/selector-v2-benchmark/semantic-adjudication-v1/`
(git-ignored; it contains real user messages).

| File | Purpose |
|---|---|
| `README.md` | Reviewer instructions and rules |
| `review-packet.md` | Human-readable blind packet |
| `review-cases.jsonl` | Primary view, machine-readable |
| `review-template.csv` | Decision sheet: `case_id, intent_text, shown_labels, candidate_view_complete, all_candidate_count, blind_decision, acceptable_labels, review_note, reviewer, reviewed_at`, all decisions empty |
| `audit-view.jsonl` | Secondary view |
| `manifest.json` | Fingerprints and metadata (below) |

XLSX was not produced; the CSV is sufficient.

**`manifest.json` contents:**

- the review packet fingerprint;
- source snapshot `3e558768…`, challenge `bed2dad1…`, and postmortem
  fingerprint (hashes of `failure-cases.jsonl` and `taxonomy-summary.json`);
- the alias-table fingerprint;
- case ids, scope counts and control ids;
- per-case candidate content sha256;
- the candidate policy;
- the incomplete-view cases;
- the sha256 of every file;
- `human_review_status: WAITING_FOR_HUMAN_REVIEW`.

**Review packet fingerprint:**
**`2c229e4fb31d998511e54566dea2130bbdb489754070cbc02b13549eff2df269`**.

**Immutability:** artifacts are written once. A rebuild with different
content is refused, and `load_locked_review` re-verifies every file hash
before decisions are applied. The packet cannot silently change after review
starts.

## Apply and re-score flow (prepared; not executed)

**1. Apply** (`adjudication-apply`, `apply_adjudication`) takes the parent
cases, the locked audit view, the decisions CSV and the explicit packet
fingerprint. It refuses a packet mismatch and rows for cases outside the
packet. It produces a **child** case set; the **parent is never mutated**
(tested).

| Final decision | Child case |
|---|---|
| CHANGE_GOLD / MULTI_ACCEPTABLE | SELECT with the new refs |
| EXPECT_NONE | expected NONE with `[]` |
| EXCLUDE_AMBIGUOUS | EXCLUDED |
| CONTENT_REVIEW_REQUIRED / RETRIEVAL_OR_KB_MAPPING_REVIEW | HOLD |
| KEEP_CURRENT / UNREVIEWED / NEED_FULL_CANDIDATES | parent unchanged |

- **Provenance per changed case:** previous and new expectations, the
  decision, review note, reviewer, timestamp, and the source packet and
  parent fingerprints.
- **Child set:** it gets its own fingerprint, so a future
  `selector-semantic-gold-v1` is a separate artifact.

**2. Re-score** (`adjudication-rescore`, `rescore`/`reclassify`) takes the
**saved** outputs of Stage A production, Variant A DEV and Variant B DEV and
re-scores them against the child expectations.

- **No provider call.**
- **What stays fixed:** the model outputs, candidates and order are
  unchanged.
- **What changes:** only the expected decision, acceptable refs and
  exclusion state. This isolates the metric effect of the Gold change.

**3. Expected NONE support.** EXPECT_NONE cases become the first empirical
NONE cases.

- **Scoring:** a NONE output scores CORRECT_NONE and a selection scores
  FALSE_SELECT.
- **Metrics:** the Phase 7A evaluator already computes NONE precision and
  recall, false NONE, correct NONE, wrong SELECT and multi-acceptable.
- **Test:** the re-score test covers expected NONE, a changed Gold,
  exclusion, and the fact that saved outputs are left unchanged.

**4. Prompt gate:** unchanged, with a test pinning the check list. It will be
re-applied to the child Gold after review, and revised only by human
decision.

## Tests

```text
test_selector_adjudication.py (new):          22 passed (includes the real-packet check)
Full backend suite, repository-root layout:   503 passed, 0 failed (481 → +22)
```

**Coverage:**

- deterministic scope and counts;
- the primary view is model-output blind: model fields injected into the
  input are absent from the output;
- refs, rank, score and Gold are hidden;
- the violation detector catches contamination;
- the Gold is always shown when it is in the pool;
- rank-neutral ordering independent of retrieval order;
- audit-view content;
- fingerprint determinism, content sensitivity and immutability;
- all blind → final decision mappings, plus invalid-row rejection;
- apply: child and provenance, parent immutable, unreviewed = no-op, packet
  mismatch and foreign-row refusal;
- re-score with expected NONE, changed Gold and exclusion;
- the prompt gate is unchanged;
- the real packet is blind, locked, unreviewed and shows the Gold;
- a module-wide network guard.

## Next step (human)

1. **Blind review.** Fill `review-template.csv` using only `review-packet.md`
   or `review-cases.jsonl`. Do not open `audit-view.jsonl` until every row
   has a decision.
2. **Apply.** Run
   `adjudication-apply --packet-fingerprint 2c229e4f…` to produce the child
   Gold.
3. **Re-score.** Run `adjudication-rescore` for production, Variant A and
   Variant B, with no new calls, then re-apply the unchanged prompt gate.

**HOLDOUT stays blocked pending adjudication, and Stage B is not started.**
