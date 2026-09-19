# Answer Pipeline V2 — Phase 7B Model Capability Experiment

## Status

```text
MODEL CAPABILITY EXPERIMENT: STOP_BEFORE_LIVE
M1 (capability screen): NOT RUN
M2 (full DEV):          NOT RUN
FINAL VALIDATION:       BLOCKED
```

- **Live calls: 0.** Only OpenRouter **catalog metadata** was read
  (`GET /api/v1/models`, `GET /api/v1/models/openai/gpt-5.6-luna/endpoints`).
  No inference call was made.
- **What the stop is:** a config-parity block, required by the experiment
  rule "do not silently pick another config".
- **What the stop is not:** a judgement of the model's capability. Nothing
  was measured.
- Branch `production-readiness`; starting HEAD `6e9838b`.

## Why a model experiment, and why the order experiment stays deferred

**Evidence that the prompt alone is not enough:**

- **Variant A** (gpt-4o-mini) fixed the over-strict NONE. It still assumed
  unstated qualifiers: frozen DEV qualifier slice 3/36, and 471/472 wrong on
  HOLDOUT.
- **Variant C** added an explicit, generic qualifier contract with the same
  model. The slice count did not improve (4/36), and 471/472 still chose the
  "Merkezi" candidate.
- **Remaining question:** is this the model's instruction following? The
  planned test keeps variant_c_v1 fixed and changes only the model.

**Order experiment:** `DEFERRED_ORDER_BIAS_WEAK`.

- The postmortem's pre-declared rule found the order bias weak: 4/21 of A's
  wrong SELECTs were at position 1, p = 0.23.
- The 76-call order control stays prepared and was not run.

## Adjudication provenance correction

Every review row behind Semantic Gold V1 records the same reviewer field,
"ChatGPT GPT-5.6 Sol": all 106 locked rows, and both 471/472 rows of the
V1.1 lock.

**From now on the correct terminology is:**

```text
method             = blind model adjudication
adjudicator_type   = model
adjudicator_model  = GPT-5.6 Sol
```

> Semantic Gold is based on blinded adjudication by GPT-5.6 Sol, not
> independent human annotation.

**Scope of the correction:**

- **Not changed:** historical artifacts and earlier reports (they used
  "human review"), and all Gold decisions.
- **Where the correct wording appears:** this report and the new experiment
  manifest (`adjudication_provenance`).
- **Final-validation requirement kept:** the new production-like
  final-validation set needs **independent human review**. Model
  adjudication must not be presented as human review.

**Consequence for this experiment:**

- The Gold was produced by a GPT-5.6-family model, and the candidate is a
  GPT-5.6-family selector.
- Any future comparison of that selector against this Gold carries a
  same-family agreement risk. Independent human review of the final set is
  the control.

## Capability validation (§4)

**Slug check:** `openai/gpt-5.6-luna` **exists** in the OpenRouter catalog,
which lists 447 models.

**Frozen selector config to match** (Variant C / gpt-4o-mini, config
`af9eb2d0…`):

| Parameter | Value |
|---|---|
| Provider | openrouter |
| Temperature | 0 |
| max_tokens | 32 |
| Reasoning | none |
| Retries | none |
| Candidate order | original |

**Parity check** (`benchmarks/selector_v2/model_capability.py`, recorded in
`capability-validation.json`):

| Parameter | gpt-4o-mini | gpt-5.6-luna | Parity |
|---|---|---|---|
| `temperature` = 0 | listed | **not listed** at the model level or on any of 7 endpoints; the adapter always sends it, so it would be ignored or rejected | **✘** |
| reasoning = none | not a reasoning model (nothing sent) | reasoning-capable (`reasoning` and `reasoning_effort` listed). The adapter's OpenRouter transport can send only `low`, `medium` or `high`; leaving it unset sends no field, so the **provider default reasoning** applies, not "none" | **✘** |
| `max_tokens` = 32 | listed | 4 endpoints list `max_tokens`, 3 (Azure) list only `max_completion_tokens`; with default reasoning, reasoning tokens count against a 32-token cap and may leave no visible JSON | **✘** |

**Endpoint parameters for gpt-5.6-luna:**

- **Every endpoint:** include_reasoning, reasoning, reasoning_effort,
  tool_choice, tools.
- **Some endpoints only:** max_completion_tokens, max_tokens,
  response_format, seed, structured_outputs.
- **Absent everywhere:** `temperature`.

The model catalog entry lists `default_parameters.temperature = null`.

**Verdict: `STOP_BEFORE_LIVE`.** The experiment would have changed three
things at once:

- the model;
- sampling (no temperature 0);
- reasoning (provider default instead of none).

That is the multi-variable comparison this programme forbids. As required,
no second config was tried and no live call was made.

**What would make the experiment possible** (your decision; none applied):

1. **A two-variable experiment:** accept "temperature not applied" plus an
   explicitly declared reasoning effort. For example, add `none` or `minimal`
   to the OpenRouter reasoning transport if the provider supports it; that is
   a benchmark adapter change, verified before any call. The report would
   then attribute differences to "model + its reasoning mode", not the model
   alone.
2. **A different model:** choose one whose catalog entry supports
   temperature 0 and has no mandatory reasoning.
3. **Leave the selector model** and address the other open variables
   separately: the order control, or the retrieval/KB phrasing of the general
   answer qna:129.

## Results

| Item | Status |
|---|---|
| M1 (36 frozen qualifier DEV cases + 471 + 472 = 38 planned calls) | **NOT RUN**, 0 calls |
| M2 (the remaining 59 DEV cases) | **NOT RUN** |
| Accuracy, paired comparisons, NONE behaviour, general/specific, 471/472 decisions, usage, cost, latency | **NOT MEASURED** |
| DEV gates (operational, qualifier ≤ 2, 471/472, exact ≥ 64/77, false NONE ≤ 3, general/specific, ≥ A) | **NOT EVALUATED** |

**Frozen inputs, verified unchanged:**

- Variant C prompt `fc1811443061737b090b388f0b52bb03f68af99a965f2956c4833663847eb9a9`;
- Semantic Gold V1.1 `1d22cac8e20d32e1b32b0433a886dff3bd2c5b658ab0952a93234ce3ed971138`;
- DEV split `0fcb2441…`;
- the 36-case qualifier slice, from the Variant C baseline `392d9a59…`;
- the diagnostics 471 and 472.

Saved baselines (Variant C / gpt-4o-mini DEV, for reference only):

| Metric | Value |
|---|---|
| Exact | 63/77 |
| False NONE | 1 |
| Qualifier slice | 4 |
| 471 / 472 | wrong (qna:342) |

**Pricing, from the catalog, for information only:** gpt-5.6-luna costs
$0.20 per 1M input tokens and $1.20 per 1M output tokens. No cost was
incurred.

## Final validation

**BLOCKED.** The strategy is unchanged:

- about 120 unused frozen cases;
- about 120 new production-like queries with **independent human review**.

The 345-case unused pool alone is insufficient: it is easy by construction
(first candidate 345/345).

## Production isolation

- **Unchanged:** production prompt and config, registry assignments (no
  model row was added), KB, aliases, candidate retrieval and ordering, and
  qna:129 / qna:342.
- **Digests** (`1|4|e17dc611…|df67859d…|66b014e7…|42b94071…`): identical
  before and after.

## Artifacts (git-ignored)

`outputs/selector-v2-benchmark/model-experiment/c-v1-gpt-5.6-luna/`:

| File | Content |
|---|---|
| `openrouter-models-metadata.json` | catalog entries for both models, with the fetch time |
| `openrouter-endpoints-gpt-5.6-luna.json` | endpoint metadata |
| `capability-validation.json` | the parity verdict |
| `manifest.json` | status, 0 calls, frozen identities, provenance |

No run directory and no M1/M2 plan were created.
