# Answer Pipeline V2 — Phase 7B-Prep Report

## Status and scope

```text
Phase 7B-Prep — Model / Reasoning Evaluation Preparation
STATUS: PASS

Phase 7B-Live — Model / Reasoning Live Evaluation
STATUS: BLOCKED_PENDING_APPROVAL
```

- Branch: `production-readiness`
- Starting HEAD: `e4c8311`
- Source of truth:
  - [`ANSWER_PIPELINE_V2_ARCHITECTURE.md`](../ANSWER_PIPELINE_V2_ARCHITECTURE.md)
    §13.6 and §22.4;
  - [`PHASE_6_REPORT.md`](PHASE_6_REPORT.md);
  - [`PHASE_7A_REPORT.md`](PHASE_7A_REPORT.md).

**ZERO LIVE CALLS.** No OpenAI, OpenRouter or Gemini request was made. Fake
runs executed inside the harness socket/SDK guard. The registry was read
read-only inside that guard, and only Postgres was reachable.

Unchanged in this phase:

- the Phase 7A frozen snapshot (`3e558768…`; file sha256 `348a0219…`
  re-verified on load);
- the Selector prompt and contract fingerprint (`3f49b198…`);
- candidate eligibility;
- the production model/config (registry v1, `af9eb2d0…`).

Not done in this phase: metadata, exact alias, K tuning, bot context. Phase
7C was not started.

## Phase 7A finding, re-verified from the frozen snapshot

Everything below was recomputed from `snapshot.jsonl`, not copied from the 7A
report. It all matches Phase 7A.

| Quantity | Value |
|---|---|
| Primary selector-evaluable cases | 482 |
| Retrieval / eligibility misses | 2 (74, 436) / 0 |
| First-candidate exact (FULL) | **462 / 482 = 0.958506** (95.85%; 0.9585062… rounded to 6 decimals) |
| First-candidate wrong | 20: 88, 89, 104, 153, 160, 189, 216, 245, 366, 381, 415, 440, 460, 470, 471, 472, 473, 474, 503, 516 |
| near_qna / general_specific / kb_overlap_flagged | 77 / 17 / 9 |
| multi_acceptable | 20: 61, 69, 92, 160, 184, 200, 211, 229, 233, 277, 284, 306, 307, 318, 355, 371, 379, 422, 464, 469 |

**Why full accuracy is low-signal:** 481 of the 482 intents are exact KB
aliases (Phase 7A). The first frozen candidate is therefore already right on
462 cases. Any model's full-set accuracy lies within the 20 remaining cases,
and a single accuracy number cannot tell a selector that verifies from one
that merely follows retrieval order. The full set is still kept, as the
**final reference and the corruption/regression check**.

## First-candidate baseline and selector-value metrics

- **Baseline** (`challenge.first_candidate_correct`): select the first
  candidate of the frozen eligible list. It never outputs NONE, so an
  expected-NONE case is wrong for it. Multi-acceptable cases use the normal
  rule: any acceptable ref counts. This is a diagnostic only, not production
  behavior.
- **Per case:** `first_candidate_ref`, `first_candidate_correct` and the
  offline `gold_rank` are stored in `challenge-v1/cases.jsonl`.
- **Value metrics** (`challenge.selector_value`), reported separately from
  accuracy:

  | Baseline | Model | Class |
  |---|---|---|
  | wrong | correct | `RESCUE` |
  | right | wrong | `CORRUPTION` |
  | right | correct | `PRESERVE` |
  | wrong | wrong | `UNRESOLVED` |

  Outputs: `rescue_count`, `rescue_rate` (over baseline-wrong),
  `corruption_count`, `corruption_rate` (over baseline-right),
  `preserve_count`, `unresolved_count` and
  `net_corrections = rescue − corruption`.
- **Paired matrix:** model vs baseline, plus model A vs model B, with
  both / only-A / only-B / both-wrong and the exact McNemar p.
- **No winner:** there is no automatic `BEST_MODEL`, and the report carries
  `"winner": null`.

## Selector challenge set (`challenge-v1`)

**Selection rule.** It is recorded verbatim in the manifest. It is
model-independent: it reads only dataset fields and the frozen retrieval
order, never any model output.

- **Universe:** the primary selector-evaluable cases of snapshot
  `3e558768…` (482).
- **Union of five groups:**
  - A. `first_candidate_correct == false`;
  - B. `near_qna`;
  - C. `general_specific`;
  - D. `kb_overlap_flagged`;
  - E. `multi_acceptable`.

  A case that falls in several groups appears once.
- **Easy control:** drawn from the universe with these conditions:
  - first candidate correct;
  - not near_qna, not general_specific, not kb_overlap_flagged;
  - not already in the union.

  Cases are ordered by `sha256("selector-challenge-v1|" + case_id)` ascending
  and the first 30 are kept. There is no runtime random seed.

**Counts.** Groups overlap, so the union is smaller than the sum.

| Group | Cases |
|---|---:|
| first-candidate-wrong | 20 |
| near_qna | 77 |
| general_specific | 17 |
| kb_overlap_flagged | 9 |
| multi_acceptable | 20 |
| union before control | 107 |
| easy-control pool / selected | 375 / 30 |
| **final unique cases** | **137** |

**Artifact:** `outputs/selector-v2-benchmark/challenge-v1/`, git-ignored.

- **Files:** `cases.jsonl`, `manifest.json` (source snapshot fingerprint,
  definition version, rule, counts, per-slice ids, control ids, all case ids,
  `cases_file_sha256`) and `summary.json` (the first-candidate baseline in
  both layers).
- **Challenge fingerprint:**
  `bed2dad16a83af96a7b427e5e8b1b56b53c9c0033eefaeca6624e8cae2ba760b`.
- **Immutability:** a rebuild reproduces the same bytes. A different
  artifact at the same path is refused, and the fingerprint and sha256 are
  checked again on load.

**Naming.** Every report uses two separately named layers:
`FULL_REFERENCE_EXACT` (the final reference) and `CHALLENGE_EXACT`. The
challenge layer carries the note "deliberately over-samples hard cases; NOT a
global/production accuracy".

## First-candidate baseline in both layers

| Layer | Correct / cases | Exact |
|---|---:|---:|
| FULL_REFERENCE_EXACT | 462 / 482 | 0.958506 |
| CHALLENGE_EXACT | 117 / 137 | 0.854015 |

- **Sanity check:** the challenge layer is clearly lower than the full set,
  as intended.
- **Where its correct cases come from:** the baseline's 117 correct challenge
  cases are its own "right-first" cases. The hard slices plus the control
  keep corruption measurable.

## Diagnostics (offline only; rank never reaches the model)

These are first-candidate baseline figures on the challenge layer, from
`summary.json`:

| Diagnostic | Result |
|---|---|
| gold_at_position_1 / gold_not_at_position_1 | 117 / 20 cases; `accuracy_when_gold_rank1` = 1.0, `accuracy_when_gold_not_rank1` = 0.0 (by construction for this baseline) |
| rank1_wrong slice (20) | primary metric `rescue_rate`; baseline 0 |
| right-first cases (117) | primary metric `corruption_rate` |
| General/specific (17; roles as in Phase 7A) | general 7/11 = 0.636 (4 × chose the specific counterpart), specific 6/6 = 1.0 |
| Near-QnA per pair (case count, baseline accuracy) | 129↔342: 9, 0.556 · 310↔405: 8, 1.0 · 316↔335: 24, 0.958 · 328↔336: 17, 0.941 · 333↔319: 16, 0.938 · 347↔72: 3, 1.0. Each pair also reports rescue/corruption |
| KB overlap flagged | 9 cases, baseline 0/9 |
| Multi-acceptable | 19/20; any acceptable ref counts |

- **KB-overlap caveat:** these nine cases were selected by reviewers
  *because* retrieval collides on an alias. A win there is evidence about
  alias-collision resistance, **not** global model superiority. The
  evaluator writes this caveat into every report.
- **NONE limitation:** reviewed Gold has no expected-NONE cases. **This
  dataset cannot measure NONE precision or recall.** NONE output shows up only
  as `false_none`. The NONE contract stays covered by the Selector V2 contract
  tests.

## Fake-provider validation on the challenge set (HARNESS SELF-TEST, not accuracy)

| Policy | CHALLENGE_EXACT | Rescue | Corruption | Net | Other |
|---|---:|---:|---:|---:|---|
| `first_candidate` | 117/137 = 0.8540 | 0 | 0 | 0 | paired vs baseline: 117/0/0/20, p = 1.0 |
| `oracle` | 137/137 = 1.0 | 20 (rate 1.0) | 0 | +20 | every slice 1.0 |
| `always_none` | 0/137 | 0 | 117 (rate 1.0) | −117 | false NONE 137 |

The run ids are the same as the Phase 7A self-test runs, because identity
does not depend on the case set. This is by design: Stage A results are
reusable by Stage B.

## Reasoning-effort transport

**Inspected contracts:**

- OpenAI SDK 3.16.2: `chat.completions.create(reasoning_effort=…)`.
- OpenRouter: the OpenAI-compatible endpoint with the unified request field
  `reasoning: {"effort": …}`, sent through `extra_body`.
- google-genai 2.24.0: `ThinkingConfig(thinking_budget | thinking_level)`.
  This has no 1:1 low/medium/high mapping, so it is **not** mapped; guessing
  one is forbidden.

**Implementation.** The mapping lives in `services/llm_config.py`, and
`services/llm_provider.py` applies it.

- **Transport map:** `REASONING_TRANSPORT`, an adapter capability map, is
  `{openai: low|medium|high, openrouter: low|medium|high}`.
- **Registry schema unchanged:** whether a model accepts reasoning stays
  registry data (`supports_reasoning_effort`, `allowed_reasoning_efforts`).
- **Request fields:** `reasoning_request_fields(provider, effort)` returns:
  - `{}` when effort is null, so **no parameter is sent**;
  - `{"reasoning_effort": v}` for OpenAI;
  - `{"extra_body": {"reasoning": {"effort": v}}}` for OpenRouter.

  Anything else raises `ReasoningTransportError`.
- **Adapter call sites:** `_OpenAICompatibleProvider._invoke` and
  `GeminiProvider._invoke` call it **before** the request. An unsupported
  level therefore makes 0 outbound calls and raises an explicit error. It is
  never silently ignored and never turned into a model error.
- **Registry:** `validate_assignment` rejects `reasoning_transport_unsupported`
  (e.g. Gemini + any level, OpenRouter + `none`). An admin can no longer
  store a level the adapter would not send.
- **Benchmark:** `LiveSelectorBackend` runs the same preflight. The Phase 7A
  blanket refusal of reasoning runs is replaced by this transport-aware check.

**Runtime regression.** Current production reasoning is null, so the request
payload is unchanged. A test asserts the exact key set
`{model, messages, max_tokens, temperature}` for OpenAI and OpenRouter.

- **Updated test:** one Phase 1 test (`test_llm_outcomes`) encoded "a
  configured reasoning level is **not** transmitted". It now asserts that the
  level **is** transmitted and that native `response_format` is still not
  sent. This is the intended contract change of this phase.
- **Production env:** no `LLM_*_REASONING_EFFORT` is set in the running
  container or `.env`.

**Tests** (mocked SDK, no network):

- low/medium/high → exactly that value, for both providers;
- Gemini + high → `ReasoningTransportError` with 0 SDK calls;
- OpenRouter + `none` → error with 0 calls;
- the live backend refuses Gemini reasoning;
- null / low / medium / high on the same model → 4 distinct config
  fingerprints.

## Registry discovery (read-only, `registry-models.json`)

| Registry id | Provider / model | Qualification | Selector allowed | Reasoning support / values | Provider key | Runnable |
|---:|---|---|---|---|---|---|
| 1 | gemini / gemini-2.5-flash-lite | LEGACY_APPROVED | yes | no / — | missing | **BLOCKED** (`provider_key_missing`) |
| 2 | openai / gpt-4o-mini | LEGACY_APPROVED | yes | no / — | missing | **BLOCKED** (`provider_key_missing`) |
| 3 | openrouter / openai/gpt-4o-mini | LEGACY_APPROVED | yes | no / — | present | **yes (production baseline)** |
| — | openrouter / openai/gpt-5.6-luna (earlier Analiz benchmark `luna-high`) | — | — | — | — | **NOT_REGISTERED** |

- **Key presence:** it comes from production `provider_key_configured`
  (OpenRouter: DB then env; OpenAI/Gemini: env). Only a boolean is recorded.
- **Reasoning-capable runnable models: none.** No registry model declares
  reasoning support. No model id was invented, and nothing was added to the
  registry.

## Proposed live matrix and plan

**Stage A — challenge only**, per `live-plan.json`:

| Config | Role | Config fingerprint | Calls |
|---|---|---|---:|
| `openrouter/openai/gpt-4o-mini@none` | production_baseline | `af9eb2d0…` (= production) | 137 |

- **Totals:** 1 config × 137 = **137 calls**.
- **Alternative-model and reasoning configs:** **BLOCKED**, because no other
  runnable model exists and none is reasoning-capable.
- **What Stage A still measures now:** the baseline's rescue and corruption
  against the first-candidate heuristic.
- **Adding more configs** needs at least one of:
  - registration and qualification of a candidate model, e.g.
    `openai/gpt-5.6-luna` via OpenRouter, with its real reasoning support
    declared;
  - provider keys for the OpenAI/Gemini entries.

  After that, `live-plan` regenerates `<model>@none|low|medium|high`
  automatically.

**Stage B policy — full validation.**

- **Not automatic.** Stage B needs a new plan and a new approval.
- **Configs:** the production baseline plus at most 2 configs picked by human
  review of Stage A.
- **Size:** 482 calls per config, at most 1,446 in total.
- **Reuse:** Stage A results carry over (same run identity), so the provisional
  incremental cost is 345 per config, 1,035 in total.

**Token estimates.** APPROXIMATE: chars/3.0 plus chat framing, on the exact
production serialization of the frozen snapshot.

| | Challenge (per config, 137 cases) | Full (per config, 482 cases) |
|---|---|---|
| Input tokens | 310,115 (band chars/4.0 – chars/2.5: 232,859 – 371,671); mean 2,264 · median 2,202 · p95 3,426 · max 3,846 | 1,122,168 (same as Phase 7A) |
| Output tokens | ≈2,329 (17 per reply); upper bound 137 × 32 = 4,384 | ≈8,194; upper bound 15,424 |

**Pricing.** Only explicit input counts: `--price provider/model=IN:OUT` or
the global `--input-price-per-1m` / `--output-price-per-1m`. None were
supplied, so every config shows `PRICE_REQUIRED` and no dollar estimate
exists.

**Live plan.** `outputs/selector-v2-benchmark/live-plan.json`

- **Contents:** snapshot, challenge and contract fingerprints, the proposed
  configs with calls and tokens, blocked and unregistered models, the Stage A
  and Stage B sections, `live=false` and `approved=false`.
- **Plan fingerprint:**
  `a4dd2c8aa88656220dc4dee711cb8a56fd51974f66441dd78a86b5bc2d8b9993`.

## Live runner approval gate

A live `run` now requires every Phase 7A gate
(`--live --confirm-live-provider-calls --provider --model`) plus
`--live-plan <file> --approve-plan-fingerprint <sha>`. Stage A also requires
`--challenge`, which limits the run to the challenge cases. The run is
refused, before any client is built, when any of these holds (all tested):

- the plan file was edited;
- the approved fingerprint differs;
- the snapshot differs;
- the challenge differs;
- the selector contract differs;
- the requested config is not in the plan.

## Isolation

- **Registry DB:** benchmark configs are ephemeral `EffectiveLLMConfig`
  objects and are never written to `ai_capability_config` or
  `ai_config_version`. A test confirms the row counts do not change.
- **Circuit breaker:** the benchmark calls the adapter's `ask_with_result`
  directly and never goes through `answer_pipeline._select_from_pool`.
  - **Test setup:** a live-backend run whose mocked provider always times
    out, with `LLM_CIRCUIT_BREAKER.acquire` and `record` patched to fail.
  - **Result:** the run completed with 36/36 TIMEOUT results, and the
    breaker state stayed empty.
- **Telemetry:** in the same test, `DecisionTrace.__init__` and
  `emit_decision_trace` are patched to fail, and neither was reached. The
  result JSONL is the only record.

## Tests

```text
test_selector_challenge.py (new, targeted):   20 passed
Full backend suite, repository-root layout:  455 passed, 0 failed  (7A end state 435 → +20)
```

- **Existing tests updated deliberately:**
  - `test_llm_outcomes.py`: reasoning is now transmitted (see above).
  - `test_selector_benchmark.py`: the blanket reasoning refusal became a
    transport refusal for Gemini.
- **Real provider calls:** none. Both benchmark test modules run inside the
  socket and provider-SDK guard.

## Phase 7B-Live blockers

1. **Explicit approval** of `live-plan.json` (fingerprint `a4dd2c8a…`),
   i.e. 137 Stage A calls, is required.
2. **Prices:** needed only if a dollar estimate is wanted.
3. **Model comparison:** needs a registered and qualified candidate model, or
   the missing OpenAI/Gemini keys for the registered entries.
4. **Reasoning experiment:** needs a registry model that declares
   reasoning support on openai/openrouter transport. The adapters are ready.
5. **Stage B:** needs a separate plan and a separate approval after human
   review of Stage A.

## Known limitations

- **Small hard slices.** They are small (20 / 17 / 9) and correlated
  (87 distinct expected ids), so McNemar power is limited.
- **NONE is not measurable.** See the NONE limitation under Diagnostics.
- **Gemini reasoning is not transported.** No low/medium/high mapping exists.
- **OpenRouter `reasoning.effort` is best-effort.** How the upstream model
  honors it depends on the model. The registry declaration is the operator's
  responsibility, and a live qualification should confirm it via
  `usage`/behavior.
- **Token figures are approximate.**
