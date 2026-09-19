# Answer Pipeline V2 — Phase 7B Model Capability Experiment

## Status

```text
MODEL_CAPABILITY_SCREEN (M1): FAIL
FULL DEV (M2):                NOT RUN
FINAL VALIDATION:             BLOCKED
```

- **Live calls: 38 logical calls.** They used OpenRouter,
  `openai/gpt-5.6-luna` and variant_c_v1:
  - 36 frozen DEV qualifier-slice cases;
  - 471 and 472.
- **No retries.** The fail-fast guard never tripped, and the budget guard
  refused 0 calls. The remaining 59 DEV calls were **not** made.
- **Branch and commits:** branch `production-readiness`. Starting HEAD
  `6e9838b`; the STOP report was committed as `a4b590a`.

## History of this experiment

1. **First attempt:** `STOP_BEFORE_LIVE`. The capability validation (v1)
   found that the frozen config could not be sent unchanged. Recorded in:
   - `capability-validation.json`;
   - `stop-before-live-manifest.json`.

   **The blocking parameters:**
   - `temperature`: absent on all 7 endpoints;
   - reasoning `none`: not sendable by the adapter;
   - `max_tokens`: at risk under default reasoning.
2. **User decision:** remove the STOP.
   - Add `none` to the OpenRouter reasoning adapter.
   - Send `reasoning = none` explicitly.
   - Omit `temperature` for a model that does not support it.
   - Keep the prompt, candidate set, order and scorer, and keep the gates
     unchanged.
   - Run the same 38-call M1; run the remaining 59 only if M1 passes.
3. **Capability validation v2** (`capability-validation-v2.json`):
   - reasoning and max_tokens are now expressible;
   - only `temperature` is blocked → `PARITY_WITH_DECLARED_OMISSION`.

## Why a model experiment; why the order experiment stays deferred

- **Prompts tried, same model (gpt-4o-mini):**
  - Variant A: slice 3/36, 471/472 wrong;
  - Variant C, with an explicit generic qualifier contract: slice 4/36,
    471/472 still wrong.
- **Remaining question:** is the problem the model's instruction following?
- **Order experiment:** `DEFERRED_ORDER_BIAS_WEAK`, per the postmortem's
  pre-declared rule (4/21 at position 1, p = 0.23).

## Adjudication provenance correction

Semantic Gold V1 and V1.1 come from **blind model adjudication**:

- `adjudicator_type = model`;
- `adjudicator_model = GPT-5.6 Sol`;
- recorded reviewer field: "ChatGPT GPT-5.6 Sol", on all 106 V1 rows and
  both V1.1 rows.

> Semantic Gold is based on blinded adjudication by GPT-5.6 Sol, not
> independent human annotation.

- **Not changed:** historical artifacts and Gold decisions.
- **Final-validation requirement:** the production-like set needs
  **independent human review**.
- **Same-family caution:** the Gold adjudicator (GPT-5.6 Sol) and the
  candidate selector (GPT-5.6 Luna) come from the same model family.
  Agreement between them is not independent evidence.

## Code changes for this run

- **Production — `services/llm_config.py`:**
  - `REASONING_TRANSPORT["openrouter"]` now includes `none`, sent as
    `extra_body.reasoning.effort = "none"`.
  - Unset reasoning (`None`) still sends no field, so the production
    request is unchanged. A test asserts the exact payload keys.
  - **Behaviour change:** the model registry now also *accepts* an
    OpenRouter assignment of reasoning `none` (validation only; no
    assignment was made). The OpenAI and Gemini transports are unchanged,
    and so is the test pinning their refusal.
- **Benchmark only:**
  - `LiveSelectorBackend(omit_request_params=("temperature",))` wraps the
    client and drops the parameter. Only `temperature` may be omitted.
  - The run identity records `omitted_request_params`. Earlier run ids are
    unchanged, which is tested.

**Sent request keys (recorded per call):** `extra_body`, `max_tokens`,
`messages`, `model`.

- `temperature` was absent in all 38 calls, and `reasoning.effort` was
  `none`.
- **Consequence:** sampling uses the provider default, not temperature 0.
  This is a declared **second difference** from the gpt-4o-mini run.

**Evidence that reasoning was off:** every call returned `finish_reason =
stop` with exactly 17 output tokens, well under the 32-token cap. There was
no truncation and no invalid output.

## Frozen before the calls

- **Baseline:** `prelive-baselines.json` (`1fc17b5d…`):
  - saved V1.1 DEV blocks for production, A, B and C/4o-mini;
  - the 36-case slice, identical to the Variant C freeze;
  - the 12 STATED slice cases;
  - the stage case ids (M1 36 + 2, M2 59);
  - the heuristic fingerprint and both gates.
- **Plan:** `m1-plan.json` (`439a90d2…`), config `bec80c36…`.
- **Identities:**
  - Variant C `fc1811443061737b090b388f0b52bb03f68af99a965f2956c4833663847eb9a9`;
  - Semantic Gold V1.1 `1d22cac8e20d32e1b32b0433a886dff3bd2c5b658ab0952a93234ce3ed971138`;
  - DEV split `0fcb2441…`.

## M1 result (36 frozen qualifier-slice cases + 471/472)

| Frozen slice (36) | Production | A | B | C/4o-mini | **C/5.6-luna** |
|---|---:|---:|---:|---:|---:|
| Exact | 25 | 29 | 25 | 28 | **27** |
| UNSTATED_QUALIFIER_ASSUMED | 2 | 3 | 2 | 4 | **3** (412, 466, 473) |

**C/5.6-luna compliance on the slice:**

| Label | Cases |
|---|---:|
| COMPLIANT_GENERAL | 17 |
| COMPLIANT_SPECIFIC | 6 |
| CORRECT_MULTI | 4 |
| OTHER_SELECTOR_ERROR | 6 |
| UNSTATED_QUALIFIER_ASSUMED | 3 |
| NONE | 0 |

**Known development regressions (consumed HOLDOUT; not validation):**

| | 471 "Yatay geçiş nasıl yaparım" | 472 "Yatay geçiş yapmak istiyorum" |
|---|---|---|
| Semantic Gold V1.1 | qna:129 | qna:129 |
| C/4o-mini | qna:342 "Merkezi…", wrong, unstated qualifier | qna:342, wrong, unstated qualifier |
| **C/5.6-luna** | **qna:129, correct, no unstated qualifier** | **qna:129, correct, no unstated qualifier** |

**Explicit-specific cases (12 STATED slice cases):**

| Regression | Cases |
|---|---|
| A-correct → luna-wrong | **320, 422** |
| C/4o-mini-correct → luna-wrong | **320** |

**Slice cases behind the failure:**

| Case | User | Gold | A / C4o | Luna |
|---|---|---|---|---|
| 320 | "Dgs ile tamamlanan çocuk gelişimi bölümü kapandı mı" | qna:336 "YKS ile Çocuk Gelişimi lisans bölümüne tercih…" | qna:336 / qna:336 | qna:328 "Çocuk Gelişimi bölümü kapatıldı mı?" |
| 422 | "Final sonuçları açıklandı mı" | multi: qna:10, 150, 337 | qna:10 / qna:341 | qna:341 "Sınav sonuçları ne zaman açıklanır?" |
| 412 | "Çocuk gelişimi bölümü 4 yıllık olarak başvurulara açılacak mı…" | expected NONE | qna:328 / qna:328 | qna:336 (YKS / lisans), counted as unstated qualifier |
| 466 | "…ortalamam 2.4 … yks puanı ile yapabilir misin" | qna:342 | qna:100 / qna:100 | qna:100 "Açık öğretim sisteminden örgüne…" |
| 473 | "Örgün eğitimden size yata geçiş yapilabilirmi" | qna:129 | qna:100 / qna:100 | qna:100 |

**M1 gate (frozen, unchanged):**

| Check | Result |
|---|---|
| Operational: 38/38; invalid, error and timeout all 0 | ✔ |
| Qualifier: slice count ≤ 2 (3) | **✘** |
| 471 and 472 correct | ✔ |
| Explicit-specific regressions = 0 (320, 422) | **✘** |
| **Overall** | **FAIL → M2 NOT RUN** |

**Reading the result (no gate change):**

- **The stronger model fixed the target failure.** 471 and 472 now pick the
  general `qna:129`, which neither A nor C/4o-mini did. The slice count
  (3) ties A and beats C/4o-mini (4), but misses the ≤ 2 bar.
- **The same "açık öğretim" confusion remains:** 466 and 473 still pick
  qna:100, as A and C/4o-mini did.
- **412 does not decide the outcome.** It is the expected-NONE case, and the
  heuristic counts a qualifier-bearing pick there. Without it the qualifier
  check would be 2 (pass), but the explicit-specific check fails on its own
  (320, 422).
- **320 is a Gold-quality question for the independent human review.**
  Luna's choice "Çocuk Gelişimi bölümü kapatıldı mı?" arguably fits the
  user ("… bölümü kapandı mı") better than the model-adjudicated Gold. The
  gate was applied as frozen and not re-argued.

## Usage, cost, latency (38 calls)

| | C/5.6-luna (M1) | C/4o-mini (same 36 slice cases, saved) |
|---|---:|---:|
| Input tokens (36 slice) | 79,034 | 79,070 |
| Input tokens (38) | 82,806 | — |
| Output tokens (38) | 646 (17 per call) | 460 over 36 (mean 12.8) |
| Latency mean | 1,498.5 ms | 1,496.8 ms (slice) |
| Latency median | 1,357.6 ms | 1,469.3 ms |
| Latency p95 | 2,813.6 ms | 1,936.0 ms |
| Latency max | 3,276.3 ms | 2,181.8 ms |

- **Cost:** about **$0.0173**, computed from the explicit OpenRouter catalog
  prices ($0.20 per 1M input, $1.20 per 1M output). This is not a
  provider-reported cost; the adapter does not request cost metadata.
- **Latency:** comparable on mean and median, with a heavier tail
  (p95 +0.9 s).
- **Model:** `actual_model` was `openai/gpt-5.6-luna` for all calls.

## Full DEV (M2)

**Not run**, because the M1 gate failed. The following are **NOT
MEASURED**:

- full-DEV exact;
- the paired comparisons (C/4o-mini vs luna, A vs luna);
- false NONE and expected-NONE behaviour on 77 cases;
- the general/specific slices;
- the full-DEV gate.

## Final validation, order experiment, production

- **Final validation:** BLOCKED. The strategy is unchanged: about 120
  unused frozen cases plus about 120 new production-like queries **with
  independent human review**.
- **Order experiment:** DEFERRED_ORDER_BIAS_WEAK.
- **Unchanged:** qna:129 and qna:342, aliases, retrieval, Meili settings,
  candidate eligibility and order, the production prompt and config, and
  registry assignments (no model row was added).
- **Digests** (`1|4|e17dc611…|df67859d…|66b014e7…|42b94071…`): identical
  before and after.
- **Production process:** the backend logged nothing in the run window.
- **Secrets:** no key material appears in the outputs.

## Artifacts (git-ignored)

`outputs/selector-v2-benchmark/model-experiment/c-v1-gpt-5.6-luna/`:

- catalog metadata;
- `capability-validation.json` (v1) and `capability-validation-v2.json`;
- `stop-before-live-manifest.json`;
- `prelive-baselines.json` and `m1-plan.json`;
- `runs/` and `diagnostics/runs/` (results, run manifest, call accounting);
- `m1-responses.jsonl`, `m1-metrics.json` and `m1-gate.json`;
- `manifest.json`.

No M2 artifacts exist.
