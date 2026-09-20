# Internal Pilot — Final Bare-Referent Context Fix V2

Date: 2026-09-20  
Branch: `production-readiness`  
Start HEAD: `4147eb5488130a7b0e6d30d24502ad5b4a96d543`

## Outcome

`INTENT_ANALYZER_SEMANTIC_SCREEN = FAIL`

`FULL_INTERNAL_PILOT_RE_ACCEPTANCE = BLOCKED`

The prompt-only correction was implemented and all automated regression suites
passed. The one permitted 14-call live screen produced 13 valid outputs and one
strict-parser invalid output on C3. No automatic prompt iteration was performed,
and the 41-scenario full re-acceptance was not run.

## Historical result (immutable)

The preceding screen remains recorded as:

- Overall semantic screen: `FAIL`
- Context-required: `3/4`
- Remaining historical failure: `C3 = CONTEXT_NOT_DETECTED`
- Failure class: `BARE_PLURAL_REFERENT_NOT_RESOLVED`

No preceding report, result artifact, probe text, or amendment was rewritten.

## Generic bare-referent rule

The analyzer now states that grammatical completeness does not prove semantic
self-containment. A generic noun/category set without an explicit subject or
process in the current turn is context-dependent only when previous USER turns
uniquely supply that referent. The prompt describes category families and
generic syntactic shapes; it does not contain the frozen C3 text.

The same instruction preserves both safety guards:

- A current turn that explicitly names its subject/process remains
  `context_used=false` with `resolved_text == normalized_text`.
- Ambiguous previous USER turns do not authorize an invented application,
  program, process, or category.

## Contract and behavior preservation

- Parser: unchanged; strict Pydantic schema and provenance checks remain active.
- Context assembly: unchanged; at most two previous USER turns, BOT excluded.
- Context rewrite: unchanged; `context_used=true` requires a genuinely different,
  self-contained `resolved_text` supported by the current and previous USER text.
- Context control: unchanged; `context_used=false` requires verbatim normalized
  and resolved text equality.
- MULTI: decision wording and `intent_count` rules unchanged.
- Calendar: rules and 2026–27 primary data unchanged; aliases remain intentionally
  empty and owned by authorized AUZEF staff.
- Selector: Variant A, OpenRouter, `openai/gpt-4o-mini`; unchanged fingerprint
  `1aed568885db02f474534224695a45eb6f95835bc31f94e877af9659efe94a1e`.

## Frozen screen identity

The V2 `prelive-probe-plan.json` is byte-identical to the preceding 14-probe
plan (`cmp` exit 0). Probe count, text, order, grouping, expected decisions,
provider, and model are unchanged. Existing tests continue to assert that
probe/scenario texts are absent from the system prompt.

## Live semantic screen

Provider: `openrouter`  
Model: `openai/gpt-4o-mini`  
Logical Analyzer calls: `14/14` (no additional live inference)

| Gate | Result |
|---|---:|
| Valid outputs | 13/14 |
| Invalid outputs | 1 |
| Provider errors | 0 |
| Timeouts | 0 |
| MULTI | 4/4 |
| Context-required | 3/4 |
| Context-required self-contained resolution | 3/4 |
| Context controls | 4/4 |
| Calendar relevant | PASS |
| Calendar irrelevant | PASS |

The operational 14/14-valid gate failed. MULTI, context controls, and calendar
controls remained intact.

## C3 result

- Previous result: `CONTEXT_NOT_DETECTED`
- Current decision: `INVALID_OUTPUT` followed by the safe SINGLE fallback
- `context_used`: `false` (fallback)
- `resolved_text`: `Hangi belgeler gerekiyor?` (fallback, unchanged)
- Correct: `NO`

The strict parser remained unchanged and rejected the model response; only the
safe fallback is retained in the decision artifact. Because the single allowed
screen is exhausted, this task does not speculate from or retry the raw model
response.

## Freeze amendment 4

- ID: `INTERNAL_PILOT_FREEZE_AMENDMENT_4`
- Parent amendment-3 fingerprint:
  `429dc8c0f44f3969a09ef04b6a46363959aa4c37354a3f7394a8896d6511bbce`
- Amendment-4 fingerprint:
  `f92d8eb2a7e809ae15e256280cbe7c707480e2b261553df15aa62707a8cfee98`
- Classification: Intent Analyzer context-detection bug fix only
- Selector/model/retrieval/calendar/parser/context-assembly/MULTI changes: `NO`

The fingerprint excludes measurement and timestamp fields, and deterministic
fingerprint tests pass.

## Automated verification

- Targeted analyzer/context/MULTI/contract/freeze: `158 passed`
- Internal pilot runtime preflight: `PASS`
- Historical manifest verification: `PASS`
- Full backend: `737 passed, 1 skipped`
- Deploy scripts: `37 passed`
- Release build: `9 passed`

## Readiness

Engineering blockers remaining: `1` (C3 live strict-parser invalid output).

The semantic screen is `FAIL`; therefore full internal pilot re-acceptance is
`BLOCKED`. This is not a release-candidate PASS.
