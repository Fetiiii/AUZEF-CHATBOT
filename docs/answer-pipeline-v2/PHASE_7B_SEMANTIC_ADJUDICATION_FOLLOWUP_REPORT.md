# Answer Pipeline V2 — Phase 7B Semantic Adjudication Full-Candidate Follow-up

## Status

```text
Phase 7B-Semantic Adjudication First Review — PARTIAL_COMPLETE (87/106 decided)
Phase 7B-Semantic Adjudication Full Candidate Follow-up — READY_FOR_HUMAN_REVIEW (19 cases)
```

- Branch: `production-readiness`
- Starting HEAD: `f1ca91f`
- **AUDIT VIEW NOT OPENED.** The follow-up tooling reads only the primary
  view files (`review-cases.jsonl`, `review-template.csv`) and the snapshot.
  `audit-view.jsonl` is never read; a test builds the packet with no audit
  file present.
- **GOLD NOT MODIFIED.** No apply, no re-score and no final lock were run.
- **ZERO LIVE CALLS.** Everything ran offline inside the harness network
  guard (`--network none` container).

## Why a follow-up

In the first blind pass, the primary view showed a bounded candidate subset
(5–9 of up to 22). The reviewer marked 19 cases `NEED_FULL_CANDIDATES`
because the right answer might be outside the shown subset. These cases need
a second blind pass that shows **every** eligible candidate.

**First-pass input:** `review-template-filled.csv`, the user's file.

- **Rows:** 106, BOM-prefixed UTF-8, sha256 recorded in the manifest.
- **Decisions:**

  | Decision | Rows |
  |---|---:|
  | SELECT_ACCEPTABLE | 78 |
  | EXCLUDE_AMBIGUOUS | 7 |
  | CONTENT_REVIEW_REQUIRED | 2 |
  | NEED_FULL_CANDIDATES | 19 |
- **Validation passed:**
  - case ids and order equal the locked template;
  - no intent text was edited;
  - every decision is in the enum;
  - every SELECT has labels, and no non-SELECT row has labels;
  - every label exists for its case.

**Follow-up cases (19):** 19, 74, 112, 158, 160, 184, 215, 221, 230, 355,
371, 375, 383, 384, 412, 415, 436, 461, 503. This is exactly the set of
first-pass NEED_FULL_CANDIDATES rows.

## Full-candidate guarantee

For each case, `build_followup` lists **every eligible candidate of the
locked snapshot exactly once**, with `candidate_view_complete = true`.

| | Value |
|---|---|
| Candidate count, min / max | **8 / 16** |
| Per case | 19:10, 74:16, 112:12, 158:11, 160:10, 184:10, 215:8, 221:11, 230:15, 355:10, 371:15, 375:12, 383:9, 384:11, 412:8, 415:14, 436:14, 461:10, 503:9 |
| Duplicate candidates | refused |
| Consistency with the first pass | the list must equal the first-pass `all_candidates` for that case, or the build fails |
| Labels | identical to the first pass (a label means the same candidate in both passes) |

**Retrieval misses:** for 74 and 436 the reviewer judges the candidates that
actually exist. No decision is pre-assigned.

## Anonymization and neutral order

- **Shown to the reviewer:** case id, user text, and every candidate as
  `Candidate <label>` with its canonical question and curated answer.
- **Hidden:**
  - `qna:` / `calendar:` refs;
  - retrieval rank, score and source;
  - current Gold and alias owner;
  - production / Variant A / Variant B decisions;
  - the first candidate;
  - taxonomy;
  - the first-pass decision.
- **Order:** `sha256(case_id | candidate_ref)` ascending, which is
  deterministic and independent of retrieval order. It is the same order and
  labels as the first pass.

## Contamination protection (field-level)

`followup_visible_violations` runs at build time and fails the build on any
problem. It checks:

1. **Field names:** only `case_id, intent_text, candidates,
   candidate_view_complete, candidate_count, label, question, answer` are
   allowed.
2. **Content** (user text, questions, answers): only record refs
   (`qna:`, `calendar:`) are forbidden. Words that occur naturally in KB
   text, such as "provider", are not false positives (tested).
3. **Non-content text** (CSV header and non-content cells, Markdown with all
   content removed, README) must not contain: qna:, calendar:, gold,
   expected, production, variant_a, variant_b, first_candidate, rank,
   score, provider, rescue, corruption, taxonomy, alias.

The real packet passes all three checks.

## Decision schema (follow-up)

The allowed values are `SELECT_ACCEPTABLE` (one or more labels; several →
MULTI_ACCEPTABLE at apply), `EXPECT_NONE`, `EXCLUDE_AMBIGUOUS`,
`CONTENT_REVIEW_REQUIRED` and `RETRIEVAL_OR_KB_MAPPING_REVIEW`.

- **`NEED_FULL_CANDIDATES` is not accepted,** because all candidates are
  shown. The later merge refuses it.
- **EXPECT_NONE:** only when none of **all** candidates meets the need.
  Unclear messages are EXCLUDE_AMBIGUOUS.

## First-pass preservation

The 87 completed first-pass decisions are not touched.

- **Stored copy:** the file is copied unchanged as
  `first-pass-decisions.csv`, with its sha256 and the 87 completed case ids
  in the manifest.
- **Later merge:** `merge_reviews` (for the final lock, not run now) replaces
  only the 19 NEED_FULL_CANDIDATES rows with follow-up decisions. The other
  87 stay byte-for-byte the same (tested). A set mismatch or a
  `NEED_FULL_CANDIDATES` in the follow-up is refused.

## Artifacts and fingerprints (git-ignored)

The artifacts live in
`outputs/selector-v2-benchmark/semantic-adjudication-v1/full-candidate-followup/`:

| File | Purpose |
|---|---|
| `review-packet-full.md` | human-readable, all candidates |
| `review-cases-full.jsonl` | machine-readable |
| `review-template-full.csv` | decision sheet: `case_id, intent_text, all_labels, candidate_view_complete, candidate_count, blind_decision, acceptable_labels, review_note, reviewer, reviewed_at` (all empty) |
| `README.md` | reviewer instructions |
| `first-pass-decisions.csv` | copy of the first-pass input |
| `manifest.json` | fingerprints, contents below |

**Manifest contents:**

- parent packet fingerprint;
- snapshot, challenge and reviewed Gold sha256;
- the 19 case ids;
- per-candidate content hashes;
- the ordering algorithm;
- the allowed decisions;
- the first-pass summary;
- `audit_view_read: false`;
- the sha256 of every file;
- status.

| Identity | Value |
|---|---|
| Parent review packet (unchanged) | `2c229e4fb31d998511e54566dea2130bbdb489754070cbc02b13549eff2df269` |
| Follow-up fingerprint | `078bec9f335e117ab95882e36caf983f79cca5310d897bb527a388fe7d00c1a6` |
| Snapshot | `3e558768561814dabe58cd0a8fc7ada71e6c1ad98e6e5bfa460320c21dea75f6` (re-verified on load) |
| Challenge | `bed2dad16a83af96a7b427e5e8b1b56b53c9c0033eefaeca6624e8cae2ba760b` |
| Reviewed Gold `gold-reviewed-all.jsonl` | `19fdff59…` (file re-hashed read-only, matches) |

Artifacts are write-once: a rebuild with different content is refused.

## Network safety

The packet was built in a `--network none` container inside `no_live_calls`.
Tests run under a module-wide guard. **0 provider calls.**

## Tests

```text
test_selector_adjudication.py:                27 passed (22 + 5 new, real follow-up packet checked)
Full backend suite, repository-root layout:   508 passed, 0 failed (503 → +5)
```

**What the new tests cover:**

- every candidate appears once, with first-pass labels, deterministically;
- field-level blindness, including a check that natural words are not false
  positives;
- first-pass validation and merge preservation (87 unchanged);
- NEED_FULL_CANDIDATES rejected;
- the primary loader works without an audit file and rejects tampering;
- the real packet: 19 expected ids, completeness, parent fingerprint, blank
  template, 87 completed first-pass rows.

**No regression.**

## Next step (human)

1. Fill `review-template-full.csv` from `review-packet-full.md`.
2. Then the final lock: merge 87 + 19 → 106, then apply and re-score. Each is
   a separate step.

**Do not open `audit-view.jsonl` until the merge is locked.**
