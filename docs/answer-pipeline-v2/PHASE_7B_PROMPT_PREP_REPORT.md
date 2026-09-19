# Answer Pipeline V2 — Phase 7B Prompt Prep Report

## Status

```text
Phase 7B-Prompt Prep — Selector prompt experiment harness
STATUS: PASS

Phase 7B-Prompt DEV Live — BLOCKED_PENDING_EXPLICIT_APPROVAL
```

- Branch: `production-readiness`
- Starting HEAD: `af1acca`
- **ZERO LIVE CALLS.** Every command and test ran inside the harness
  network/SDK guard.
- **Production unchanged:**
  - selector contract `3f49b198d621329022be807beada6d75891449b9722ffd9d7e097c6cf403ceed`;
  - production prompt fingerprint
    `2d59cfb65f0aaa08ffcc481d81fed5a89f29803251cdc8a5438da673cb867f3c`;
  - no diff under `backend/services`.

  The production runtime does not import any of the new code.
- **Frozen inputs unchanged** and re-verified on load:
  - snapshot `3e558768561814dabe58cd0a8fc7ada71e6c1ad98e6e5bfa460320c21dea75f6`;
  - challenge `bed2dad16a83af96a7b427e5e8b1b56b53c9c0033eefaeca6624e8cae2ba760b`;
  - split `0fcb244120bdb1144de7f10f853cd2f6fe0c6d13b3f9c08411c041a48efb5777`.

## Rationale (from the postmortem)

The postmortem traced Stage A's poor result to the difference between the
strict Selector V2 verification standard and the alias-derived Gold. The
main symptoms were over-strict and underspecified false NONE, together with
cases where the user explicitly stated the qualifier. Only 2 failures were
clear selector errors. So the next measurement changes **only the system
prompt**. Everything else stays fixed:

- provider `openrouter`, model `openai/gpt-4o-mini`, reasoning none,
  temperature 0, max_tokens 32 (config fingerprint `af9eb2d0…`);
- the frozen candidates and their order;
- the serializer, schema and parser;
- eligibility, Gold, acceptable refs and the split.

## Prompt contract (benchmark-only)

The contract lives in `backend/benchmarks/selector_v2/prompt_contract.py`
and `prompts/`.

- **Production prompt:** read at runtime from
  `services.selector.SELECTOR_SYSTEM_PROMPT`. There is deliberately **no**
  `production.md` copy (a test enforces this).
- **Variants:** stored as `prompts/variant_a_v1.md` and
  `prompts/variant_b_v1.md`, with committed fingerprints in
  `prompts/manifest.json`. `prompt-prep` refuses to run on drift.
- **Normalization:**
  - CRLF → LF;
  - trailing whitespace stripped from each line;
  - leading and trailing blank lines removed.

  Nothing else changes, and the normalized text is exactly what the model
  receives.
- **Fingerprints are separated:**
  - `prompt_fingerprint` = sha256 of the normalized system prompt;
  - `serializer_contract_fingerprint`
    (`d50fbee416ca98783e499454fe840c1fdc2ff41b5fb7445cbac20f6a72c7f5a9`) =
    output schema + candidate view fields + serialized probe payload + parser.
    It is the same for every prompt.
- **Override path:** `select_with_prompt`.
  - For `production` it calls the unmodified `ask_with_result`.
  - For a variant it calls production `build_selector_prompt` (for the user
    payload), then the production adapter `_invoke` (same config and
    transport), then production `parse_selector_output`. Only the system
    string differs.
  - Tests verify that user payloads are byte-identical across prompts, that
    the production request equals the `ask_with_result` request, that
    invalid-output handling is identical, and that a live-backend variant
    request carries exactly `{model, messages, max_tokens, temperature}` with
    the variant system prompt.

| Prompt | Fingerprint | Chars | Benchmark-only |
|---|---|---:|---|
| production | `2d59cfb65f0aaa08ffcc481d81fed5a89f29803251cdc8a5438da673cb867f3c` | 1846 | no (runtime) |
| variant_a_v1 | `1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e` | 1858 | yes |
| variant_b_v1 | `44893b6bb069da5b2849413b3d903fa3ff8dbfd1e1e7ea8242d1302135e093c9` | 1926 | yes |

The variant texts are byte-identical to the postmortem design; their
postmortem-era combined fingerprints (`b81f3d6b…`, `885f20a0…`) still
reproduce.

### Semantic differences (full raw diff in `prompt-experiment/prompt-diff.md`)

**production → variant_a_v1**

- The role changes from "verify, not find the most similar" to "select the
  candidate that meets the user's need".
- **Added:** short, informal or misspelled messages are normal, and no
  word-by-word or detail-by-detail match is required.
- **Replaced:** rules 1–6 (all must hold) become four selection rules:
  1. same topic;
  2. a stated qualifier selects the matching candidate and is never
     contradicted;
  3. an unstated qualifier means prefer the general candidate and never
     assume one;
  4. if several fit, take the most direct one.
- **Narrowed NONE:** NONE only if no candidate addresses the topic, every
  candidate answers a clearly different need, or the message states no
  topic.
- **Removed:** the concrete "yatay geçiş / merkezi" example (benchmark
  wording).
- **Unchanged:** the Calendar rule, order-is-meaningless, single choice, and
  the output contract.

**production → variant_b_v1** (NONE-threshold ablation)

- **Unchanged:** the verifier role, rules 1–6, and the general/specific rule
  (in abstract form).
- **Added:** the same short-message tolerance and the same explicit, narrow
  NONE criterion as in A.
- **Removed:** the concrete example. This is a confound shared with A.

**variant_b_v1 → variant_a_v1**

- A drops the strict all-conditions checklist ("answer must cover the real
  need / centre").
- A adds the "user-stated qualifier → matching candidate" direction.
- A frames the task as need-satisfaction.

**What each comparison measures:**

- B vs production measures how much of Stage A comes from the NONE
  threshold alone.
- A vs B measures the extra qualifier/need-framing guidance.

Neither variant contains case ids, QnA ids, KB question text, near-pair
wording or the benchmark phrases (a test enforces this).

## DEV / HOLDOUT policy

- **Split:** `prompt-experiment-split-v1`, committed as
  `benchmarks/selector_v2/splits/prompt-experiment-split-v1.json`. It
  contains ids only, no case text.
- **Size:** DEV **95**, HOLDOUT **42**; disjoint, and the union is the frozen
  137.
- **Verification:** `load_split` checks the fingerprint, ordering,
  disjointness, coverage of the challenge set and equality with the
  postmortem artifact. A test regenerates the split from the challenge set
  and gets identical ids.
- **HOLDOUT limitation:** the postmortem read all 137 cases, including
  HOLDOUT, to build the taxonomy. HOLDOUT is therefore a **tuning-separation
  set, not a blind academic test**.
  - What is recorded: the variants were not hand-optimized on HOLDOUT case
    text, case ids or QnA content (test-enforced).
  - Truly independent validation belongs to Phase 7G.
- **Where the clear errors sit:** both clear selector errors (471, 472) fall
  in **HOLDOUT**. The DEV run cannot observe them directly. The evaluator
  reports them separately wherever they are evaluated.

## Production DEV baseline (reused from Stage A, recomputed; zero new calls)

The source is Stage A run `9dfc72c140dc7e93`. These are CHALLENGE-subset
numbers, not a global accuracy.

| DEV (95) | Production | First-candidate (diagnostic only) |
|---|---:|---:|
| Exact | 34/95 = 0.358 | 82/95 = 0.863 |
| Rescue | 6/13 | — |
| Corruption | 54/82 (0.659) | — |
| Net corrections | −48 | — |
| False NONE | 29 | — |
| General expected | 2/8 | |
| Specific expected | 1/3 | |
| Near-QnA | 16/53 | |
| Easy control (21 in DEV) | 6/21, corruption 15 | |

- **Production HOLDOUT** (reference only; not used for selection): 17/42.
  Both 471 and 472 are WRONG_SELECT.
- **First-candidate as a comparator:** it is a diagnostic only. It measures
  agreement with the alias-owning retrieval order, not the selector's
  contract.

### Taxonomy slices (offline, from the postmortem; denominators unchanged)

| Slice | All | DEV | HOLDOUT | Production DEV correct |
|---|---:|---:|---:|---:|
| CLEAR_SELECTOR_ERROR | 2 | 0 | 2 | — |
| GOLD_ALIAS_QUESTIONABLE | 18 | 12 | 6 | 0/12 |
| CONTRACT_MISMATCH | 29 | 23 | 6 | 0/23 |
| NEEDS_HUMAN_REVIEW | 37 | 26 | 11 | 0/26 |
| KB_OVERLAP_INTRINSIC | 5 | 3 | 2 | 0/3 |

- **Scoring is unchanged:** a variant that picks a different candidate on a
  GOLD_ALIAS_QUESTIONABLE case is still **benchmark-incorrect**.
- **How to read gains:** gains on GOLD_ALIAS_QUESTIONABLE or
  CONTRACT_MISMATCH mean closer agreement with the alias-derived Gold. They
  are **not** automatically a production semantic improvement, and every
  report carries this note.

## DEV live matrix and plan

| Config | Case set | Calls |
|---|---|---:|
| openrouter / openai/gpt-4o-mini + **variant_a_v1** | DEV | **95** |
| openrouter / openai/gpt-4o-mini + **variant_b_v1** | DEV | **95** |
| production prompt | DEV | 0 (Stage A reused) |
| any prompt | HOLDOUT | 0 |
| **Total** | | **190** |

**Token estimates.** They use the production serializer and are
APPROXIMATE (chars/3). A calibrated figure applies the Stage A factor
0.8264: actual DEV input 179,220 over the production-prompt estimate
216,876.

| | Approx. input | Calibrated input | Est. output | Output upper bound |
|---|---:|---:|---:|---:|
| Variant A (95) | 217,256 (mean 2,287) | ≈179,540 | 1,615 | 3,040 |
| Variant B (95) | 219,346 (mean 2,309) | ≈181,268 | 1,615 | 3,040 |
| Total (190) | 436,602 | ≈360,808 | 3,230 | 6,080 |

- **Cost:** **PRICE_REQUIRED**. No dollar figure was computed.
- **Live plan:** `outputs/selector-v2-benchmark/prompt-experiment/live-plan-dev.json`
  (git-ignored).
  - **Contents:** provider, model, reasoning = null, temperature 0,
    max_tokens 32, the config fingerprint, dataset DEV with 95 case ids, the
    snapshot, challenge, split, serializer and production prompt
    fingerprints, and per-variant prompt fingerprints, calls and tokens.
  - **Flags:** `production_baseline_calls = 0`, `holdout_calls = 0`,
    `approved = false`.
  - **Plan fingerprint:**
    **`c88478486c12bc07f5650517025d93f779fe9444dcf3c0ab6494ae9c5b8cf3d8`**.

## Gates

**DEV live run** (`prompt-run --case-set dev`) requires all of the
following:

- the Phase 7A/7B gates: `--live --confirm-live-provider-calls`, and
  `--provider openrouter` (any other provider is refused);
- `--live-plan` plus `--approve-plan-fingerprint`.

It is refused on any of these mismatches: plan kind, file edit, approved
fingerprint, snapshot, split, serializer contract, model config, or a
prompt that is not in the plan.

- **Production prompt:** refused outright, because its baseline is reused
  from Stage A.
- **Result isolation:** each run's identity is snapshot + split +
  serializer contract + prompt + model config fingerprint. A, B and
  production therefore have separate namespaces. Resume within a variant
  works, and a different prompt never reuses results. The Stage A run id is
  unchanged.

**Prompt selection gate** (`prompt-dev-eval`, experiment gate, not a
production criterion). All of these must hold:

- the DEV run is complete;
- net corrections ≥ 0;
- corruption < production DEV (54);
- false NONE < production DEV (29);
- no DEV general/specific case that production got right turns wrong.

**No winner is ever chosen automatically:**

- several variants pass → human review;
- none passes → **no HOLDOUT run**.

**HOLDOUT gate** (`prompt-run --case-set holdout`) is refused unless all of
these hold:

- the prompt is not production;
- `--selected-dev-winner` equals `--prompt`;
- `--dev-gate-report` belongs to the same prompt and split fingerprints and
  shows `gate.passed = true`;
- for live runs, a separate `selector-prompt-experiment-holdout` plan exists
  with its own approved fingerprint.

The HOLDOUT gate is enforced even for fake runs, and no HOLDOUT plan exists
yet.

**DEV report contents:**

- DEV exact, rescue, corruption and net;
- false NONE and invalid/error;
- general and specific accuracy;
- near-QnA;
- easy-control corruption;
- the taxonomy slices;
- the critical cases 471/472 (split side and outcome);
- per-case general/specific correctness for the regression check.

## Isolation

- **Production state:** a variant run through the live backend, with the
  SDK mocked to always time out, did not touch the breaker (acquire/record
  patched to fail) or DecisionTrace, and changed no
  `ai_config_version` / `ai_capability_config` rows (tested).
- **Live policy:** live mode stays OpenRouter-only, with the socket
  allowlist `openrouter.ai`.

## Tests

```text
test_selector_prompt_experiment.py (new):  15 passed (includes private-artifact checks, none skipped)
Full backend suite, repository-root layout: 479 passed, 0 failed  (464 → +15)
```

**Test coverage:**

- the production prompt and contract are unchanged, and no production copy
  exists;
- the variant fingerprints are stable and match the committed manifest;
- the whitespace normalization policy;
- no benchmark-specific content in the variants;
- the override changes only the system prompt; serialization, schema and
  parser behave identically;
- the split is frozen (95/42, fingerprint, disjoint, regenerates from the
  challenge set);
- the production DEV baseline is reproduced from Stage A;
- A/B/production namespaces and resume;
- the selection gate;
- the HOLDOUT gate;
- the DEV plan (190/0/0, approval validation, tamper rejection) plus the
  real plan file;
- OpenRouter-only, and production prompt-run refused;
- breaker/trace/registry isolation;
- a module-wide network guard.

## Next explicit approval needed

Phase 7B-Prompt DEV Live needs approval of **190 OpenRouter calls**:
`openai/gpt-4o-mini` with variant_a_v1 and variant_b_v1, 95 DEV cases each,
under live plan `c88478486c12bc07f5650517025d93f779fe9444dcf3c0ab6494ae9c5b8cf3d8`.
Prices are optional; without them there is no dollar figure.

HOLDOUT (42 calls for one selected prompt) and Stage B are separate, later
approvals. Phase 7C has not started.
