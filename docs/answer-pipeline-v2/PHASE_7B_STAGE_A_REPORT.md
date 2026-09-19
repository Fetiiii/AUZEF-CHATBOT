# Answer Pipeline V2 — Phase 7B-Live Stage A Report

## Status

```text
Phase 7B-Live Stage A — Production selector baseline on the challenge set
STATUS: PASS (run completed; results below are measurements, not a verdict)

Phase 7B-Live Stage B — NOT RUN
```

- Branch: `production-readiness`
- Starting HEAD: `045bf8f`
- **Approval:** the user explicitly approved exactly 137 OpenRouter calls for
  this run.
- **Calls:** 137 unique provider invocations were made, and no other live
  call.

## Live plan identity

| Item | Value |
|---|---|
| Live plan | `outputs/selector-v2-benchmark/live-plan.json` |
| Plan fingerprint (approved, re-verified) | `a4dd2c8aa88656220dc4dee711cb8a56fd51974f66441dd78a86b5bc2d8b9993` |
| Snapshot fingerprint (from manifest) | `3e558768561814dabe58cd0a8fc7ada71e6c1ad98e6e5bfa460320c21dea75f6` |
| Challenge fingerprint | `bed2dad16a83af96a7b427e5e8b1b56b53c9c0033eefaeca6624e8cae2ba760b` (137 cases, cases file sha256 re-verified) |
| Selector contract fingerprint | `3f49b198d621329022be807beada6d75891449b9722ffd9d7e097c6cf403ceed` |
| Config fingerprint | `af9eb2d0767d37cd632799cbae39e7938585b243ceb4c7a1527b8028cd489a6e` (= production selector v1) |
| Run id | `9dfc72c140dc7e93` |
| Run window (UTC) | 2026-09-19T13:57:25Z – 14:00:32Z |

## Provider policy

**The project inference policy for this evaluation is OpenRouter-only. The
direct OpenAI and Gemini registry entries were not used.**

- **Provider and model:** `openrouter` / `openai/gpt-4o-mini`. OpenRouter
  reported `actual_model = openai/gpt-4o-mini` on 137/137 responses.
- **Parameters:** reasoning_effort none (no reasoning field sent),
  temperature 0, max_tokens 32. Timeout and retries were left at the SDK
  defaults, exactly as in the production config.
- **Enforced in code:** live mode refuses any provider other than
  OpenRouter. During the live run, sockets could reach only `openrouter.ai`.
  There was no provider fallback.
- **Key handling:** only the OpenRouter key was present in the run
  container's environment. The OpenAI and Gemini keys were absent, which the
  preflight checked. The key was never printed or logged, and a scan of the
  run output and artifacts found no key material.

## Preflight

All 14 checks passed before the first request:

- provider, model, reasoning = null, temperature 0, max_tokens 32;
- 137 challenge cases;
- plan, snapshot, challenge and contract fingerprints;
- config fingerprint = production selector = plan baseline;
- OpenRouter key present, direct OpenAI/Gemini keys absent.

The live runner used the full approval gate:
`--live --confirm-live-provider-calls --live-plan … --approve-plan-fingerprint a4dd2c8a…`.

## Execution, resume and isolation

- **Execution:** 137 planned and 137 executed; 0 skipped, 0 superseded,
  0 corrupt lines. The result file holds 137 unique cases.
- **Resume check** (offline, after the run): re-running the same identity
  with a backend that fails if called executed 0 cases (137 skipped) and made
  0 calls.
- **Production DB,** before vs after:
  - version count 1, max version 1, audit rows 4, capability rows 2;
  - `ai_capability_config` digest `e17dc611…`;
  - query_logs 1,697 and conversation_messages 3,384.

  All of these were identical after the run. No config version or admin
  assignment was created.
- **Circuit breaker and telemetry:**
  - **Separate process:** the benchmark ran in its own container process
    and called the adapter directly, so it never went through
    `_select_from_pool`.
  - **Production breaker:** it lives in the `auzef_backend` process, which
    the benchmark cannot reach. The in-process isolation is also covered by a
    test, where breaker and DecisionTrace calls fail if touched.
  - **Backend logs:** during the run window, `auzef_backend` logged only
    `/health` probes, with no chat request and no decision trace.

## Results — CHALLENGE_EXACT (137 cases; NOT a global accuracy)

The challenge set deliberately over-samples hard cases. These figures are not
production accuracy and cannot be compared with the full-set 95.85%.

| Metric | First-candidate baseline | Model |
|---|---:|---:|
| Exact | 117/137 = 0.8540 | **51/137 = 0.3723** |
| Exact delta | — | **−66 cases (−0.4818)** |

**Selector value** (definitions as in Phase 7B-Prep):

| | Count | Rate |
|---|---:|---:|
| RESCUE (baseline wrong → model right) | **8 / 20** | 0.400 |
| CORRUPTION (baseline right → model wrong) | **74 / 117** | 0.632 |
| PRESERVE | 43 | |
| UNRESOLVED | 12 | |
| **Net corrections** | **−66** | |

**Paired test, model vs first-candidate:**

| Both correct | Only first-candidate | Only model | Both wrong |
|---:|---:|---:|---:|
| 43 | 74 | 8 | 12 |

The exact McNemar test gives p = 1.6 × 10⁻¹⁴ (two-sided). The harness does
not declare a winner.

**Outcomes:**

| Outcome | Count |
|---|---:|
| Valid SELECT (correct) | 96 (51) |
| Wrong SELECT | 45 |
| Valid NONE (all false NONE) | 41 |
| INVALID_OUTPUT | 0 |
| MODEL_ERROR | 0 |
| TIMEOUT | 0 |

- **Response validity:** all 137 responses were valid strict JSON with
  `finish_reason=stop`, parsed by the production parser. None was corrected
  by hand.
- **NONE:** reviewed Gold has no expected-NONE cases, so NONE precision and
  recall are not computed. The model produced NONE 41 times, and every one is
  a false NONE.

### Slices

| Slice | Cases | Model exact | Rescue | Corruption |
|---|---:|---:|---:|---:|
| rank1_wrong (gold not at position 1) | 20 | 8/20 = 0.400 | **8/20 = 0.40** | — |
| rank1_correct (gold at position 1) | 117 | 43/117 = 0.368 | — | **74/117 = 0.63** |
| general_specific: expected general | 11 | 2/11 = 0.182 | | |
| general_specific: expected specific | 6 | 3/6 = 0.500 | | |
| general_specific total | 17 | 5/17 | 0 | 8 |
| near_qna total | 77 | 27/77 = 0.351 | | |
| near 129↔342 | 9 | 0.222 | 0 | 3 |
| near 310↔405 | 8 | 0.375 | 0 | 5 |
| near 316↔335 | 24 | 0.667 | 1 | 8 |
| near 328↔336 | 17 | 0.235 | 0 | 12 |
| near 333↔319 | 16 | 0.125 | 1 | 14 |
| near 347↔72 | 3 | 0.000 | 0 | 3 |
| kb_overlap_flagged (see caveat) | 9 | 3/9 = 0.333 | 3/9 | 0 |
| multi_acceptable (any acceptable ref counts) | 20 | 11/20 = 0.550 | | |
| easy_control | 30 | 9/30 = 0.300 (14 false NONE, 7 wrong SELECT) | | |

- **General/specific: the key behavior.** On expected-general cases the
  model mostly chose the more specific sibling or NONE. For example, case
  472 "Yatay geçiş yapmak istiyorum" → `qna:342` *Merkezi* yatay geçiş, which
  is exactly the prompt's own counter-example. The model is assuming
  qualifiers the user did not state.
- **KB-overlap caveat:** these 9 cases were selected by reviewers
  *because* retrieval collides on an alias. The 3 rescues are evidence about
  alias-collision resistance only, not about global model quality.
- **Easy control:** on 30 cases where retrieval rank 1 was correct and no
  hard slice applied, the model was right only 9 times. Most failures are
  NONE on short, underspecified user messages (e.g. 19 "Muafiyet itirazi",
  112 "kac dönemim kaldı", 141 "Sınav kitapçığı"). This is consistent with
  the strict Phase 4 verifier rules ("the answer must cover the real need",
  "no unstated condition").

### Tokens, latency, cost

The usage figures are **actual OpenRouter metadata**, with 137/137
coverage.

| | Total | Mean | Median | p95 | Max |
|---|---:|---:|---:|---:|---:|
| Input tokens | 256,151 | 1,869.7 | 1,819 | 2,887 | 3,225 |
| Output tokens | 1,453 | 10.6 | 13 | 13 | 13 |
| Total tokens | 257,604 | 1,880.3 | 1,827 | 2,900 | 3,230 |
| Latency ms | 185,308 | 1,352.6 | 1,245.6 | 2,119.3 | 5,241.7 (case 217) |

- **Approximation error:** the pre-run approximate estimate was 310,115 input
  tokens, 21% above the actual figure.
- **Cost:** **NOT_CALCULATED — no explicit price inputs.**

## Artifacts (git-ignored, contain real user messages)

`outputs/selector-v2-benchmark/live-stage-a/`:

- `runs/9dfc72c140dc7e93/results.jsonl`: the authoritative per-call
  results, plus `run-manifest.json`, `metrics.json`, `REPORT.md` and
  `challenge-metrics.json`.
- `stage-a-report/case-results.jsonl`: per case, the expected refs, the
  first-candidate ref and correctness, gold rank, model decision and
  selection, model correctness, value class, tags, status, tokens and
  latency, and canonical questions for inspection.
- `stage-a-report/error-review.json`:
  - `model_wrong_cases` (86)
  - `rescued_cases` (8)
  - `corrupted_cases` (74)
  - `unresolved_cases` (12)
  - `invalid_or_error_cases` (0)
- `stage-a-report/summary.json`: the run summary, challenge layer, paired
  counts and `winner: null`.
- `stage-a-report/review-queue.json`: see below.

## Review queue (human review; metrics unchanged)

Gold and the snapshot were not modified, and no model answer was reinterpreted.
These cases looked suspicious on inspection:

| Case | Observation |
|---|---|
| 404 | "açıköğretim öğrencisiyim" carries no answerable need, but gold is `qna:335` (Çözüm Merkezi talep). Likely context loss |
| 19 | "Muafiyet itirazi" (an objection) vs gold `qna:306` (muafiyet başvurusu) |
| 35 | "Hangi senemdeyim" vs gold `qna:307`, whose answer text is about yatay geçiş intibak |
| 221 | Gold `qna:320` vs selected `qna:317`; possibly both acceptable |
| 158 | Gold `qna:315` (general system login) vs selected `qna:397` (sınav tercih sistemi); KB overlap |

Carried over from Phase 7A: the retrieval-miss alias collisions 74 and 436
(task g37).

## Inputs for a Stage B decision (no decision taken)

- **Headline result.** On this frozen challenge set, the production
  selector (gpt-4o-mini, no reasoning) scores far below the retrieval-order
  heuristic:
  - net corrections −66;
  - corruption rate 0.63 on cases where retrieval was already right;
  - 41 false NONE;
  - general/specific accuracy 5/17.

  It does rescue 8 of the 20 rank-1-wrong cases.
- **Why a full-set run would be low-signal.** Running Stage B for this same
  config would mostly measure how often it corrupts the 462 rank-1-correct
  full-set cases. The challenge corruption rate already suggests the answer
  is substantial.
- **Questions for human review before any further spend:**
  1. Is the strict verifier prompt, together with loose/alias-derived Gold
     acceptability, the dominant cause? This would need prompt work, which
     is out of scope for 7B.
  2. Would a stronger or reasoning model via OpenRouter behave differently?
     That requires registering and qualifying such a model first.
  3. Do the review-queue Gold cases need correction?
- **Readiness:** Stage B requires a new plan and new explicit approval.

**Stage B was NOT RUN.** No full-482 run, no second model, no reasoning
levels, no permutation, and no metadata, alias or K experiment was performed.

## Code changes in this stage (no production runtime change)

- **`benchmarks/selector_v2/safety.py`:**
  - live mode is OpenRouter-only;
  - the new live-network guard lets the SDK run but restricts sockets to
    `openrouter.ai`;
  - DNS results for allowed hosts are added to the allowlist dynamically.
- **`benchmarks/selector_v2/challenge.py`, `cli.py`:** a `stage-report`
  command that writes the case-level, error-review and summary artifacts.
- **Tests:** 3 new tests in `test_selector_challenge.py` (OpenRouter-only
  gate, live guard, stage artifacts).
  - **Benchmark modules:** 66/66.
  - **Full backend suite:** 458/458, repository-root layout; no regression.
