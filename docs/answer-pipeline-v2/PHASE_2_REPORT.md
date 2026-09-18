# Answer Pipeline V2 — Phase 2 Report

## Status

```text
Phase 2 — Intent Analyzer
STATUS: PASS
```

- Branch: `production-readiness`
- Starting HEAD: `8dc09640d4b6fcf02af8d944f3d1bb15d6e11904`
- Architecture source of truth:
  [`docs/ANSWER_PIPELINE_V2_ARCHITECTURE.md`](../ANSWER_PIPELINE_V2_ARCHITECTURE.md)
- Scope: Intent Analyzer V2, user-only context resolution, and associated
  decision tracing. Calendar V2, Selector V2, semantic-NONE redesign,
  degraded-mode redesign, metadata, model registry/UI, exact-alias changes,
  and candidate tuning were not started.

## Removed V1 splitter behavior

The production path no longer contains:

- free-form line-based splitter output;
- bullet/number cleanup and unbounded line deduplication;
- `?`/newline regex splitting after model failure;
- analyzer/splitter and full-query selector calls started concurrently;
- the discarded full-query selector call on MULTI requests;
- full conversation history in the selector prompt; or
- a second context-concatenated retrieval pass.

Analyzer execution now completes before any retrieval or selector work begins.

## Intent Analyzer V2 contract

The typed contract is `IntentAnalysis` containing exactly one or two
`IntentItem` values. Each item has:

```text
source_text
normalized_text
resolved_text
context_used
calendar_relevant
```

`IntentAnalysis` and `IntentItem` use strict Pydantic validation:

- extra fields are rejected;
- `intent_count` is only `1` or `2` and must equal list length;
- empty and over-2,000-character text fields are rejected;
- empty, malformed, fenced, or prose-wrapped JSON is rejected;
- three-or-more intents are rejected rather than truncated;
- SINGLE must preserve the full current turn as `source_text`;
- MULTI sources must be real current-turn spans;
- `context_used=false` requires `resolved_text == normalized_text`;
- `context_used=true` requires prior user context and a real context-derived
  resolution; and
- normalized/resolved tokens must be lexically attributable to the source or,
  for real follow-ups, prior user turns.

The lexical provenance check is deliberately conservative. If it rejects a
valid but aggressive rewrite, the consequence is the safe raw-SINGLE path,
not a fabricated intent.

## SINGLE, MULTI, and three-plus policy

- SINGLE is the prompt-level default and the ambiguity policy.
- Multiple clauses do not imply MULTI.
- MULTI is permitted only for two independent answer needs.
- The runtime schema has no three-plus state.
- If the model returns three-plus intents, or a three-plus current turn cannot
  be represented safely, the deterministic policy is to preserve the complete
  current turn as one raw intent. This avoids arbitrarily dropping a goal. The
  separate three-plus UX remains an open architecture decision.

## Context behavior

The widget loader now provides at most the previous two owned user messages,
in chronological order. Bot messages are excluded at the database query and
again by the pipeline boundary. `/api/search` still provides no conversation
context.

The code and production configuration template now default
`CHAT_CONTEXT_ENABLED=true`, making the frozen Context V2 policy active after
deployment unless explicitly disabled. Ownership-token validation, bounded
history, and the 1,200-character cap are preserved.

Context is consumed only by the analyzer. Retrieval and selector receive each
intent's `resolved_text`; full history is not propagated downstream.

## Calendar relevance

`calendar_relevant` is produced and traced per intent. The prompt defines it
as a semantic need for Academic Calendar date/term/event information rather
than a keyword match.

Phase 3 behavior was not implemented: the flag does not gate retrieval, every
Calendar row is still injected into every selector candidate pool, and the
existing non-LLM Calendar fallback is unchanged.

## Structured output and fail-safe

All providers use the same compatibility contract:

1. request strict JSON in the analyzer prompt;
2. parse the complete response as JSON (no substring, fence, digit, or regex
   recovery); and
3. validate it locally against the typed schema and provenance invariants.

The Phase 1 native-structured-output flag remains available, but its default
is unchanged (`false`) and no provider/model behavior is changed in this
phase. Local validation remains mandatory and provider-independent.

Model error, timeout, empty output, invalid JSON, invalid schema, semantic
expansion, and three-plus output all produce the same lossless fallback:

```text
intent_count = 1
source_text = current user turn
normalized_text = current user turn
resolved_text = current user turn
context_used = false
calendar_relevant = false
```

The typed cause (`MODEL_ERROR`, `TIMEOUT`, or `INVALID_OUTPUT`) and parse status
remain available in the trace. Analyzer failure does not turn the whole
request into LLM-OFF mode; the selector still receives the safe SINGLE intent.

## LLM call count and composition

Normal paths are now:

```text
SINGLE: 1 Intent Analyzer + 1 Selector
MULTI:  1 Intent Analyzer + 2 Selectors
```

There is no speculative selector. Existing deterministic composition is
unchanged: selected curated answers remain verbatim, duplicate answer strings
are removed in intent order, and remaining answers are joined with a blank
line.

## Model/config integration

The analyzer uses the Phase 1 `intent_analyzer` capability config and selector
uses `selector`. Effective production defaults remain unchanged:

- OpenAI: `gpt-4o-mini`
- OpenRouter: `openai/gpt-4o-mini`
- Gemini: `gemini-2.5-flash-lite`
- analyzer: temperature `0`, max tokens `300`
- selector: temperature `0`, max tokens `5`
- reasoning effort, timeout, and retries: unset/provider default unless
  explicitly configured

## Decision trace V2

The trace schema is now version `2`. The V1 `splitter` field was removed and
was not silently reused. `intent_analyzer` records:

- provider, requested model, actual model when returned;
- effective config and capability config fingerprint;
- current input length and previous-user context count;
- intent count;
- per-intent source/normalized/resolved lengths, `context_used`, and
  `calendar_relevant`;
- call and outcome status, parse status, and `fallback_to_single`;
- latency, provider response metadata, token usage, and observable retry count.

Raw current/context text, normalized/resolved text, prompts, provider output,
answers, identity numbers, phone numbers, email addresses, verification data,
and student-specific content are not persisted in the trace.

## Regression boundaries

The following Phase 3+ behavior remains unchanged:

- all Calendar records are unconditionally injected in selector pools;
- selector prompt, numeric parser, and semantic contract are V1;
- semantic NONE and invalid selector output still enter the existing
  threshold fallback without reopening the Calendar keyword gate;
- model error/timeout still enters the existing full fallback;
- routing guards, retrieval limits/thresholds, candidate ordering/deduplication,
  LLM ON/OFF administration, curated answers, and endpoint response contracts
  are unchanged.

## Verification

Focused analyzer/orchestration contract evaluation:

```text
33 passed, 0 failed, 0 skipped
```

This covers SINGLE/MULTI examples, two-clause false-split cases, genuine
follow-up, topic switch, no context, max-two user turns, bot exclusion,
semantic-expansion rejection, Calendar relevance examples, strict schema,
malformed/empty/three-plus/error/timeout fallbacks, call order, exact LLM call
counts, resolved-text-only downstream flow, composition, and trace safety.

Final targeted Phase 2 plus V1/Phase 1 regression suite:

```text
109 passed, 0 failed, 0 skipped, 1 warning in 28.36s
```

Full backend suite in the same backend-only layout used for the Phase 1
baseline:

```text
228 passed, 7 failed, 1 skipped, 1 warning in 87.46s
```

The seven failures are exactly the Phase 0/1 infrastructure/layout baseline in
`tests/test_production_app_runtime.py`: the backend-only container maps
`/app`, but not repository-level `/deploy/production`. There are no new full
suite regressions. With the repository root mounted at its expected layout,
the same file passes separately:

```text
7 passed, 0 failed in 1.25s
```

## Benchmark provenance and limitation

This repository intentionally contains neither the human-reviewed Gold
dataset nor an analyzer accuracy harness. The available scripts measure
retrieval/coverage, not Intent Analyzer accuracy. Therefore no fabricated
false-split, missed-MULTI, context precision/recall, Calendar precision/recall,
latency, or token metric is reported.

The 33-case focused result above is a deterministic contract/regression
evaluation with scripted provider outputs; it is not a live-model accuracy
benchmark. Live capability comparison remains dependent on the external
reviewed dataset/harness.

## Known limitations and Phase 3 readiness

- Real-model false-split/missed-MULTI and Calendar-relevance quality still need
  the external reviewed benchmark.
- Three-plus intent UX remains deliberately unresolved; raw SINGLE preserves
  information until that product decision is made.
- Conservative semantic-expansion validation can reject a legitimate broad
  paraphrase, but fails safely without changing user meaning.
- `calendar_relevant` is observability-only until Phase 3.

No conflict with the architecture source of truth was found. No Phase 3 code
was started, and there is no implementation blocker for beginning Phase 3.
