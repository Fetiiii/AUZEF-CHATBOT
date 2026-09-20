# Answer Pipeline V2 — Phase 7B Luna Full DEV Exploratory Comparison

## Status

```text
LUNA_FULL_DEV_COMPARISON: COMPLETE
HISTORICAL LUNA M1: FAIL (UNCHANGED)
INTERNAL PILOT BASELINE: VARIANT A / OPENAI GPT-4O-MINI (UNCHANGED)
FINAL VALIDATION: DEFERRED TO INTERNAL-PILOT DATA
```

- **No promotion gate was applied.** This is a behavioural comparison only.
- **The M1 screen result stays FAIL.** Its artifacts (`m1-plan.json`,
  `m1-gate.json`, `m1-metrics.json`, `m1-responses.jsonl`, the baseline, the
  capability records and the old manifest) are byte-identical, recorded in
  `m1-artifacts-lock.json` and checked by a test.
- **Branch:** `production-readiness`; starting HEAD `88b1970`.

## What ran

| | Cases |
|---|---:|
| Reused M1 Luna DEV outputs (no new calls) | 36 |
| Reused Luna 471/472 diagnostics (no new calls) | 2 |
| **New Luna DEV logical calls** | **59** |
| **Combined Luna DEV** | **95** (0 missing, 0 duplicate) |
| Production, A, B, C/4o-mini new calls | 0 each |
| Old HOLDOUT, order experiment, final validation | 0 each |

- **Gate bypass is explicit.** The remaining DEV cases ran under a separate
  ungated plan (`m2-plan.json`, `62bae75f…`, `gated: false`,
  `m1_gate_passed: false`) and the `--exploratory` flag, which is recorded in
  every call-accounting row. The gated M2 path still refuses to run.
- **Identities:** Variant C prompt
  `fc1811443061737b090b388f0b52bb03f68af99a965f2956c4833663847eb9a9`;
  Semantic Gold V1.1
  `1d22cac8e20d32e1b32b0433a886dff3bd2c5b658ab0952a93234ce3ed971138`;
  DEV split `0fcb2441…`. Candidate set, content, original retrieval order,
  schema, parser, scorer, qualifier heuristic and slices are the Variant C
  ones.

### Upstream rate limiting, and what a "call" means here

The first attempt at the 59 cases hit OpenRouter's upstream rate limit. The
captured body (one diagnostic call) was:

```text
openai/gpt-5.6-luna is temporarily rate-limited upstream (code 429,
error_type = rate_limit_exceeded)
```

The adapter surfaced these as `MODEL_ERROR` because the body carries no
`choices`.

| | Count |
|---|---:|
| **Logical selector invocations** (one per DEV case) | **59** |
| Transport attempts for those 59 cases | 102 |
| Transport attempts, M1 (36 cases) | 36 |
| Diagnostic probe that captured the 429 body | 1 |

- **Retries changed no logical call count.** They were paced (3 s, then 15 s)
  and repeated until every case had a usable result.
- **Fail-fast:** the first version stopped on the first error. After the
  429 diagnosis it was changed to stop after N consecutive errors
  (15 in the retry rounds). This is an operational parameter; it does not
  touch scoring, the Gold or the gates.
- **Final state:** 95/95 usable, `INVALID_OUTPUT`, `MODEL_ERROR` and
  `TIMEOUT` all 0, `finish_reason = stop` everywhere.

### Parity

```text
STRICT_TRANSPORT_PARITY = NO
BEHAVIORAL_EXPERIMENT_PARITY = YES
```

- **Why:** `temperature` is omitted, since gpt-5.6-luna does not support it;
  sampling uses the provider default. `reasoning = none` is sent explicitly.
- **Everything else** is identical to the Variant C / gpt-4o-mini run.
- **No other parameter was invented** to compensate.

### Semantic Gold provenance

```text
method            = blind model adjudication
adjudicator_type  = model
adjudicator_model = GPT-5.6 Sol
```

> Semantic Gold is based on blinded adjudication by GPT-5.6 Sol, not
> independent human annotation.

The candidate model (GPT-5.6 Luna) and the adjudicator (GPT-5.6 Sol) are from
the same family, so agreement between them is not independent evidence. The
new production-like final-validation set still requires **independent human
review**.

## Results (Semantic Gold V1.1, 77 semantic-evaluable DEV cases)

| | Exact | Accuracy | False NONE | NONE out | Unstated qualifier (77) | Unstated qualifier (frozen 36) |
|---|---:|---:|---:|---:|---:|---:|
| Production / 4o-mini | 51 | 0.662 | 20 | 21 | 2 | 2 |
| Variant A / 4o-mini | 64 | 0.831 | 1 | 1 | 5 | 3 |
| Variant B / 4o-mini | 56 | 0.727 | 12 | 12 | 3 | 2 |
| Variant C / 4o-mini | 63 | 0.818 | 1 | 1 | 4 | 4 |
| **Variant C / 5.6-luna** | **64** | **0.831** | **0** | **0** | **3** | **3** |
| First candidate | 46 | 0.597 | — | — | — | — |

**Paired comparisons (same 77 cases; descriptive, no gate):**

| Pair | Both correct | Only left | Only Luna | Both wrong | Net (Luna − left) | McNemar p |
|---|---:|---:|---:|---:|---:|---:|
| **C/4o-mini vs C/Luna** | 58 | 5 | 6 | 8 | **+1** | 1.0 |
| **A / 4o-mini vs C/Luna** | 58 | 6 | 6 | 7 | **0** | 1.0 |
| B vs C/Luna | 51 | 5 | 13 | 8 | +8 | 0.096 |
| Production vs C/Luna | 44 | 7 | 20 | 6 | +13 | 0.019 |

**Case-level differences:**

| Direction | Cases |
|---|---|
| A-correct → Luna-wrong | 320, 330, 337, 386, 422, 503 |
| C/4o-mini-correct → Luna-wrong | 320, 330, 337, 386, 503 |
| Luna-correct → A-wrong | 89, 104, 166, 205, 371, 470 |
| Luna-correct → C/4o-mini-wrong | 38, 89, 104, 285, 371, 470 |
| Production-correct → Luna-wrong | 320, 330, 337, 386, 412, 422, 503 |

**Reading it:** on aggregate the stronger model with the qualifier-contract
prompt lands exactly where the much cheaper pilot (Variant A / 4o-mini)
already is: 64/77, with 6 wins and 6 losses against it. The differences are a
reshuffle, not a level change.

### NONE behaviour

| | Production | A | B | C/4o-mini | C/Luna |
|---|---:|---:|---:|---:|---:|
| NONE outputs | 21 | 1 | 12 | 1 | **0** |
| False NONE | 20 | 1 | 12 | 1 | **0** |
| Correct NONE | 1 | 0 | 0 | 0 | 0 |
| False SELECT on expected NONE | 0 | 1 | 1 | 1 | 1 |

- **Expected NONE is a single case (412).** Only production answers it
  correctly. Luna, like A, B and C/4o-mini, selects a candidate.
- **Luna never emits NONE** in 95 DEV cases. That removes the false-NONE
  problem and removes the ability to abstain. With one expected-NONE case the
  abstention side remains unmeasurable.

### Qualifier behaviour (frozen heuristic, unchanged)

| Run | Unstated-qualifier cases (77 DEV) |
|---|---|
| Production | 328, 473 |
| Variant A | 205, 210, 466, 470, 473 |
| Variant B | 210, 470, 473 |
| Variant C / 4o-mini | 371, 466, 470, 473 |
| **Variant C / Luna** | **412, 466, 473** |

- **466 and 473 persist across every configuration.** Both pick qna:100
  "Açık öğretim sisteminden örgüne…". This is the one qualifier failure that
  neither a stronger prompt nor a stronger model has moved.
- **Luna's third case is 412**, the expected-NONE case, where it selects a
  candidate carrying an unstated qualifier.

### General / specific and other slices (correct / cases)

| Slice | Cases | Production | A | C/4o-mini | C/Luna |
|---|---:|---:|---:|---:|---:|
| Phase 7A general | 8 | 6 | 6 | 6 | **7** |
| Phase 7A specific | 3 | 1 | 2 | 2 | 2 |
| general_specific tag | 11 | 7 | 8 | 8 | **9** |
| near_qna | 44 | 31 | 36 | 38 | 37 |
| multi_acceptable | 15 | 11 | 13 | 13 | 13 |
| kb_overlap | 5 | 3 | 3 | 3 | 3 |
| easy_control | 15 | 8 | 15 | 15 | 15 |
| Qualifier-sensitive (frozen 36) | 36 | 25 | 29 | 28 | 27 |

### Known diagnostics 471 / 472 (not part of DEV accuracy)

| Case | C/4o-mini | C/Luna |
|---|---|---|
| 471 | qna:342 "Merkezi…", wrong | **qna:129, correct** |
| 472 | qna:342, wrong | **qna:129, correct** |

These come from the M1 run; they were not called again. They remain
consumed-HOLDOUT tuning cases, not validation evidence.

## Cost and latency

| | C/4o-mini (95 DEV) | C/Luna (95 DEV) |
|---|---:|---:|
| Input tokens | 204,654 (97 calls incl. diagnostics) | 200,785 |
| Output tokens | 1,213 | 1,599 (mean 16.8) |
| Latency mean | 1,464.2 ms | **1,440.2 ms** |
| Latency median | 1,436.0 ms | 1,326.0 ms |
| Latency p95 | 1,840.6 ms | 2,000.6 ms |
| Latency max | 2,181.8 ms | 4,426.3 ms |

- **Reported cost:** `NOT_AVAILABLE`. The adapter does not request
  OpenRouter usage accounting.
- **Estimated cost:** about **$0.042** for the 95 DEV calls, computed from
  the catalog prices ($0.20 per 1M input, $1.20 per 1M output). gpt-4o-mini
  is $0.15 / $0.60.
- **Latency caveat:** the Luna numbers are from a rate-limited window;
  successful calls are comparable on mean and median, with a heavier tail.

## What this does and does not decide

- **Decides nothing about promotion.** No gate ran.
- **The pilot baseline is unchanged:** variant_a_v1 on gpt-4o-mini.
- **Aggregate:** Luna equals A (64/77, net 0) and edges C/4o-mini by 1.
- **Where Luna is better:** 471/472, the Phase 7A general slice (7/8), zero
  false NONE, and fewer unstated-qualifier cases than A or C/4o-mini.
- **Where Luna is worse:** 6 cases A gets right, including 320, 330, 337, 386
  and 503; it never abstains; and its price per token is higher.
- **Open Gold question:** case 320 was already flagged in the M1 report. The
  model-adjudicated Gold expects qna:336 "YKS ile Çocuk Gelişimi lisans…"
  while Luna picks qna:328 "Çocuk Gelişimi bölümü kapatıldı mı?" for the user
  text "Dgs ile tamamlanan çocuk gelişimi bölümü kapandı mı". Cases like this
  need the independent human review before any of these numbers carry weight.

## Production and other experiments

- **Unchanged:** production selector prompt, active selector and
  intent-analyzer assignments, registry rows, KB, aliases, candidate
  retrieval and order, qna:129 and qna:342.
- **Digests** (`1|4|e17dc611…|df67859d…|66b014e7…|42b94071…`): identical
  before and after.
- **Adapter capability retained:** OpenRouter can send `reasoning = none`.
  No production assignment uses it.
- **Order experiment:** `DEFERRED_ORDER_BIAS_WEAK`, not run.
- **Final validation:** no calls, no frozen ids. The strategy still needs the
  new production-like set with independent human review.

## Artifacts (git-ignored)

New, alongside the untouched M1 files, in
`outputs/selector-v2-benchmark/model-experiment/c-v1-gpt-5.6-luna/`:
`m2-plan.json`, `m2-responses.jsonl`, `all-dev-responses.jsonl`,
`semantic-scores.jsonl`, `paired-c4o-vs-luna.json`, `paired-a-vs-luna.json`,
`qualifier-analysis-full.json`, `metrics-full.json`,
`manifest-exploratory.json` and `m1-artifacts-lock.json`.
