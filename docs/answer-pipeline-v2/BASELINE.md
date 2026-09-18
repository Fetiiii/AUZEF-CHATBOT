# Answer Pipeline V2 — Phase 0 Baseline

**Phase:** 0 — Baseline Freeze  
**Status:** PASS  
**Recorded:** 2026-09-18  
**Branch:** `production-readiness`  
**Starting HEAD:** `e2985358604bd38b30bf91c84c9309f6c61cd551`

## Source of truth

[`docs/ANSWER_PIPELINE_V2_ARCHITECTURE.md`](../ANSWER_PIPELINE_V2_ARCHITECTURE.md)
is the architecture source of truth for Answer Pipeline V2. Its Phase 0 SHA256 is:

```text
31230299c166387d111174f19dfbfbbcec074dda6b6164e8a324a1a2b2f8230e
```

This document freezes the current V1 implementation. It does not replace the
V2 architecture and does not close any decision marked open, deferred, or
experimental there.

## Current V1 runtime map

| Responsibility | Current implementation |
|---|---|
| Production student endpoint | `POST /widget-chat` → `backend.routers.chat.widget_chat` |
| Secondary public endpoint | `GET /api/search` → `backend.routers.chat.search` |
| Answer entrypoint | `backend/services/answer_pipeline.py::answer_question` |
| Splitter | `backend/services/llm_provider.py::BaseLLMProvider.split_questions` |
| Selector | `backend/services/llm_provider.py::BaseLLMProvider.ask` and `_parse_selection` |
| Candidate construction | `backend/services/answer_pipeline.py::_build_candidate_pool` |
| LLM orchestration | `backend/services/answer_pipeline.py::_llm_answer` |
| Threshold fallback | `backend/services/answer_pipeline.py::_fallback_answer` |
| Calendar routing/matching | `is_date_query`, `search_calendar`, `calendar_utils.match_calendar_entry` |
| Routing guard | `backend/services/routing_guards.py::RoutingGuardPolicy` |
| Context loader | `backend/routers/chat.py::_load_recent_context` |
| Search providers | `backend/services/providers.py` |
| LLM provider/config resolution | `backend/services/llm_provider.py`, `backend/core/deps.py` |
| Admin LLM switch/key | `backend/admin/settings_api.py` |

Both public chat endpoints call the same `answer_question` entrypoint. Only the
widget endpoint can pass persisted conversation context.

## Current V1 behavior contract

### Splitter

- Runs for every request that enters the LLM-enabled path.
- Receives only the current user query, not conversation context.
- Has no maximum split/intent count.
- The prompt asks the model to emit independently understandable questions;
  semantic reformulation is therefore possible.
- Runs in parallel with a speculative selector call for the unsplit query.
- On model/parse exception, falls back to a `?`/newline regex splitter.
- If more than one question is emitted, the speculative full-query result is
  discarded and each sub-question gets new retrieval and selection.

### Selector

- Receives numbered `question` and `answer` pairs as plain text.
- Does not receive `qna_id`, provider, score, rank, matched alias, guard state,
  or general/specific metadata in its prompt.
- Must return one number, or `0` for no suitable candidate.
- The parser uses the first digit sequence in free-form model output.
- `0`, no number, and out-of-range number all become Python `None`.
- A valid selection returns the curated candidate answer verbatim.

### Academic Calendar

- In the LLM path, every Calendar row is added to the beginning of every
  selector candidate pool; there is no calendar-intent gate.
- Calendar rows are sorted and exact duplicate tuples are removed.
- There is no current-year, active-term, or temporal-validity filter.
- In the non-LLM fallback path, `is_date_query` gates Calendar and token overlap
  selects an event.
- `search_calendar(use_llm=True)` exists but has no production caller.

### Context

- Code and production-template default: `CHAT_CONTEXT_ENABLED=false`.
- When enabled, the widget loads bounded prior user/bot messages after ownership
  token validation and before saving the current user turn.
- The context loader defaults to four messages and 1,200 total characters.
- Contextual retrieval uses only the last two previous user messages.
- The selector receives bounded user and bot history as formatted text.
- The splitter and threshold fallback do not receive context.
- `/api/search` does not load or pass conversation context.

### Fallback and result states

- LLM OFF/unavailable: Calendar keyword gate, then Meili `>= 0.90`, then
  Qdrant `> 0.75`, then no answer.
- Selector semantic `None`: Calendar gate stays closed, but the same query is
  retried through Meili/Qdrant threshold fallback.
- Selector/model exception: full fallback runs and Calendar gate is reopened.
- Multi-question selector errors may produce a partial successful response if
  another sub-question produced an answer.
- Multi-answer composition is deterministic: exact answer dedupe followed by
  `"\n\n"` joining.

### Routing guards

- Guards are loaded once per answer request.
- Guard-store read failure returns `(None, "none")` before LLM or fallback.
- Guardless QnAs are selector- and fallback-eligible.
- Active `semantic_selector_only` QnAs are selector-eligible but not
  fallback-eligible.
- Out-of-window and unsupported-mode QnAs are blocked from both paths.
- Calendar candidates and post-answer Meili suggestions are not guard-filtered.

## Current model and configuration

| Setting | Frozen V1 value/behavior |
|---|---|
| Provider selector | `LLM_PROVIDER` environment variable |
| Supported providers | `openrouter`, `openai`, `gemini` |
| Runtime LLM switch | DB `SystemConfig` key `LLM_ENABLED` |
| Default seeded switch | `false` |
| OpenRouter key precedence | DB `OPENROUTER_API_KEY`, then environment |
| OpenAI default model | `gpt-4o-mini` |
| OpenRouter default model | `openai/gpt-4o-mini` |
| Gemini default model | `gemini-2.5-flash-lite` |
| Selector max tokens | `5` |
| Splitter max tokens | `300` |
| Temperature | `0` |
| Reasoning effort support | Not implemented/configured |
| Explicit timeout | Not implemented/configured |
| Explicit retry policy | Not implemented/configured |
| Structured output | Not implemented; plain text parsing |
| Capability-specific models | Not implemented; splitter and selector share a provider/model |

These are historical baseline values, not V2 recommendations.

## Critical source SHA256

| File | SHA256 |
|---|---|
| `backend/services/answer_pipeline.py` | `a1de2c31217e16f0609bd83358a64e17e9a4377cfe7676cf0743d78f51a82186` |
| `backend/services/llm_provider.py` | `bf29f9f4c4c673cba1d4a6f301a77e5781ca266a7b6031e80beb20886abadfcc` |
| `backend/services/providers.py` | `ab76ba04c41d969b33cfe960072064d3cfbfc88639f913c23a0ac32d1e83eb75` |
| `backend/services/routing_guards.py` | `5c30a7d4dc838cd9ccbcc74c1efea0b7ef7ac103efe8efccedde0611c7f477a3` |
| `backend/services/calendar_utils.py` | `a54bbfceda1fbc2f81945e0d86722bf9ace23506bfa193979a482f8de8f566db` |
| `backend/routers/chat.py` | `e257399fafc75ee4fc3318f69130b648f249555ad065d301ba24e2eb947fff1f` |
| `backend/core/deps.py` | `66aa2f797493851b7e93b92a5c713bf19e78b3fe3c5e6ab136742c11ac3332e5` |
| `backend/admin/settings_api.py` | `e66f910b6c3c615b48ea1d1149ec698abba1f73a0811c81e140c64e3992b9010` |
| `backend/core/database.py` | `ac9b3ed09fc94071a0600b605f1718ed243a48a400512f1e00bbbb230bcec6e1` |
| `backend/main.py` | `a90bb937181e2f2985afa3944bdc86b057c9e72d14581d76a67e8a6577dda7b4` |
| `backend/routers/calendar.py` | `27d6930ca1526e1379543a22190738a256fa3284899fbb0d1b24c79aa2a95e35` |
| `backend/routers/qna.py` | `68cb35537fe9bd6b462df3b639023ee7dbcc1af184a9d6c33638c0a6fd0fedd2` |
| `chatbot-web/src/widget.js` | `b84472c98483fc3326c167245e5808941c4c345531e28388c87f182b217a4db2` |

## Migration invariants

The machine-readable forms are in [`baseline.json`](baseline.json).

1. MeiliSearch retrieval implementation remains present.
2. Qdrant retrieval implementation remains present.
3. A valid selector choice returns the curated answer verbatim.
4. Routing-guard storage failure remains fail-closed.
5. Candidate dedupe uses `qna_id` when available.
6. Both `/widget-chat` and `/api/search` use `answer_question`.
7. Admin-controlled LLM ON/OFF state remains available.
8. QnA synchronization maintains both Meili and Qdrant indexes.
9. Conversation ownership token validation remains present.
10. Multi-answer composition remains deterministic unless an explicitly
    approved architecture phase changes that contract.

## Test baseline

Tests were executed without changing application code. The running backend
container's eight required critical runtime files were SHA256-identical to the
host baseline. Test databases were three isolated databases in a temporary
PostgreSQL 15 container; the application databases were not used.

### Relevant answer-pipeline suite

Covered:

- splitter and selector parsing
- candidate construction/order/dedupe
- Calendar matching and routing helpers
- context loading, ownership, and caps
- routing guards and fail-closed behavior
- retrieval thresholds
- widget/search routing and maintenance behavior
- LLM settings/provider-key resolution

Result:

```text
59 passed, 0 failed, 0 skipped, 1 warning in 19.63s
```

Warning: Starlette TestClient uses a deprecated AnyIO `BlockingPortal` alias.

### Full backend suite

Container-visible result:

```text
168 passed, 7 failed, 1 skipped, 1 warning in 75.00s
```

All seven failures are in `tests/test_production_app_runtime.py`. They are
environment/layout failures: the backend image contains `/app` but not the
repository-level `/deploy/production` tree, so the tests resolve paths such as
`/deploy/production/app/auzef-backend.service` and receive `FileNotFoundError`.
The expected files are present in the host repository. They are not counted as
passes and the tests were not changed.

Additional execution limitations recorded during baseline setup:

- Host Python lacks project runtime dependencies such as SQLAlchemy and FastAPI.
- A root-directory pytest invocation cannot resolve the backend `core` package.
- A fresh backend image container does not contain pytest; the currently
  running backend container does.
- The first parallel harness attempts collided on the fixed
  `auzef_pytest_pg:55440` test-container identity. Canonical results above were
  rerun serially with an isolated `auzef_phase0_pg` database container.

## Benchmark provenance

These are migration reference measurements only. No benchmark dataset is
copied into this repository and no runtime behavior is hardcoded from them.

| Measure | Human-reviewed reference |
|---|---:|
| Evaluation targets | 503 |
| Scored targets | 486 |
| Unscored context-required targets | 17 |
| Primary metric | Exact E2E |
| 4o-mini exact | 362/486 ≈ 74.5% |
| Luna-high exact | 364/486 ≈ 74.9% |
| Candidate recall | ≈ 99.6% |
| Recall@3 | ≈ 98.2% |
| Recall@10 | ≈ 99.6% |

## Mutation control

Only documentation artifacts were added in Phase 0. The critical source files
listed above had identical starting and ending SHA256 values. No runtime,
configuration, prompt, schema, threshold, or provider behavior was changed.
