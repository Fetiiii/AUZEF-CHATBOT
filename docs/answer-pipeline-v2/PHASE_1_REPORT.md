# Answer Pipeline V2 — Phase 1 Report

**Phase:** 1 — Observability + Model Config Foundation
**Status:** PASS
**Recorded:** 2026-09-19
**Branch:** `production-readiness`
**Starting HEAD:** `746fb1eb10f873e22d7f4bdd8f614dba56c97bef`

The architecture source of truth remains
[`docs/ANSWER_PIPELINE_V2_ARCHITECTURE.md`](../ANSWER_PIPELINE_V2_ARCHITECTURE.md).
Its content and Phase 0 digest were not changed in Phase 1.

## Scope and behavior compatibility

Phase 1 adds internal configuration, typed results, and structured tracing. It
does not change the V1 splitter prompt/activation, selector prompt/index parser
compatibility, unconditional Calendar candidate injection, context rules,
routing-guard policy, fallback thresholds, or final answer composition.

The compatibility wrappers remain:

- `BaseLLMProvider.split_questions()` returns the same list contract.
- `BaseLLMProvider.ask()` returns the same curated answer or `None` contract.
- `_build_candidate_pool()` returns the same candidate-list shape and order.
- `answer_question()` returns the same `(answer, source)` tuple.
- Widget and `/api/search` response payloads are unchanged.

## Capability configuration

`backend/services/llm_config.py` is the internal source for effective LLM
configuration. It defines `intent_analyzer` and `selector` capabilities, each
with these fields:

- provider and model
- reasoning effort
- temperature and max tokens
- optional timeout and max retries
- structured-output enablement

The effective defaults preserve Phase 0:

| Provider | Model |
|---|---|
| OpenAI | `gpt-4o-mini` |
| OpenRouter | `openai/gpt-4o-mini` |
| Gemini | `gemini-2.5-flash-lite` |

The selector remains `temperature=0`, `max_tokens=5`; the current splitter,
represented by the future-compatible `intent_analyzer` capability, remains
`temperature=0`, `max_tokens=300`. Optional timeout, retry, reasoning, and
structured-output settings default to unset/disabled.

Capability-specific environment overrides use the prefixes
`LLM_INTENT_ANALYZER_*` and `LLM_SELECTOR_*`. The legacy `LLM_PROVIDER` and
the existing DB/admin `LLM_ENABLED` plus OpenRouter key precedence remain in
effect. No DB model registry or admin model-switch UI was added.

Each effective capability config and the combined config set have a stable
fingerprint produced from sorted, compact JSON and SHA-256. Secrets are not
part of the config model or fingerprint input.

## Provider behavior

OpenAI/OpenRouter requests now consume the effective capability model,
temperature, and token limit. Explicit timeout and retry values are applied
only when configured. When unset, the arguments are omitted so the installed
SDK defaults remain unchanged. The Phase 0 OpenAI SDK behavior is therefore
preserved (SDK-managed timeout and retry defaults).

Gemini also omits unset timeout/retry values and retains its Phase 0 SDK
behavior. When explicitly configured, timeout seconds are mapped to client
`HttpOptions` milliseconds and `max_retries=N` is mapped to `attempts=N+1`;
configured clients are cached by the secret-free effective-config fingerprint.

Reasoning effort is validated and retained in the effective snapshot, but it
is not sent to the current models without a verified provider/model capability
mapping. Structured-output enablement is likewise represented but defaults to
false; the V1 plain-text index contract remains active. The Selector V2 schema
is not implemented in this phase.

Provider adapters retain available response metadata without retaining raw
responses:

- requested and actual/resolved model
- provider response ID
- input and output token counts
- finish reason
- latency

Actual retry count remains `null` when the SDK does not expose it; the system
does not report configured maximum retries as retries actually performed.

## Typed LLM outcomes

`backend/services/llm_types.py` introduces explicit statuses:

- `SUCCESS`
- `SEMANTIC_NONE`
- `INVALID_OUTPUT`
- `MODEL_ERROR`
- `TIMEOUT`

Selector `0` is retained as semantic none; an absent number or out-of-range
index is invalid output. The legacy `ask()` wrapper still maps both none-like
parse results to Python `None`, so Phase 0 routing remains unchanged.
Provider errors and timeouts retain their separate internal causes. Splitter
errors still use the same regex fallback but now expose the error/fallback
status to observability.

## Decision trace

`backend/services/decision_trace.py` provides one mutable, lock-protected trace
per endpoint request. The router owns correlation and finalization; the same
trace is passed through the answer pipeline and emitted once as a structured
JSON application-log payload.

Recorded fields include:

- request ID, optional conversation ID, endpoint, LLM enabled state, nullable
  deployment/git version, effective configs, and combined fingerprint
- context enabled state, message count, and character count
- splitter provider/model/config, input length, subquestion count, call/parse
  status, fallback usage, latency, usage, retries, and provider metadata
- Calendar/Qdrant/Meili/current-context candidate counts, guard rejections,
  deduped/eligible counts, safe candidate IDs, source, score, stage, and order
- selector purpose, candidate IDs, normalized selected index, selected QnA ID,
  selected source, explicit result status, latency, usage, retries, and provider
  metadata
- fallback reason, branch/source flags, and selected QnA ID when present
- final outcome, source, QnA IDs, answer count, and total latency

The trace does not contain raw query, context, prompt, answer, provider output,
conversation token, IP address, API key, identity number, phone, e-mail, or
verification token. Invalid model output is represented by status and a safe
normalized integer only.

Application logs were selected over a new durable event table because the
repository has no existing decision-event store and Phase 1 does not require a
new persistence schema. Existing `QueryLog` behavior is unchanged.

## Runtime source changes against Phase 0

| File | Reason | User-visible behavior change |
|---|---|---|
| `backend/services/answer_pipeline.py` | Carry typed outcomes, candidate provenance, fallback reason, and trace | No |
| `backend/services/llm_provider.py` | Resolve capability configs and retain typed provider results/metadata | No; default requests remain equivalent |
| `backend/routers/chat.py` | Create/finalize/emit request-scoped trace | No response-contract change |
| `backend/services/llm_config.py` | New capability config and fingerprint foundation | No |
| `backend/services/llm_types.py` | New typed internal outcomes | No |
| `backend/services/decision_trace.py` | New PII-safe structured trace | No |

`providers.py`, routing guards, Calendar utilities, `core/deps.py`, admin
settings runtime, DB models/schema, and the architecture document were not
changed.

## Verification

Tests used three isolated databases in a temporary PostgreSQL 15 container;
the application databases were not used.

Targeted Phase 1 and V1 regression suite:

```text
89 passed, 0 failed, 0 skipped, 1 warning in 25.13s
```

Full backend suite:

```text
198 passed, 7 failed, 1 skipped, 1 warning in 83.35s
```

The same seven Phase 0 environment/layout failures remain in
`tests/test_production_app_runtime.py`: the backend-only test container exposes
`/app` but not the repository-level `/deploy/production` tree. Failure count,
test names, and cause are unchanged. There is no new full-suite regression.

New tests cover default/effective configs, deterministic fingerprints, typed
none/invalid/error/timeout results, provider metadata, optional timeout/retry,
PII-safe trace serialization, correlation, concurrent trace writes, candidate
and selected QnA IDs, fallback reason, endpoint integration, and Phase 0
routing compatibility.

## Deferred by design

- Intent Analyzer V2 semantics and max-two behavior (Phase 2)
- Calendar gating/temporal routing (Phase 3)
- Selector V2 schema and semantic contract (Phase 4)
- semantic `NONE` and degraded-mode redesign (Phase 5)
- DB-backed model registry, cross-provider capability dispatch, and admin model
  controls (Phase 6)
- reasoning-level/model necessity experiments (Phase 7)

No open or deferred architecture decision was closed implicitly.
