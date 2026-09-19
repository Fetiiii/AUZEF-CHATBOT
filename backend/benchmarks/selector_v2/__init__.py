"""Selector V2 benchmark harness (Phase 7A).

Selector-only evaluation over a frozen, versioned candidate snapshot. The
harness reuses the production Selector V2 contract (``SelectorCandidate``,
``build_selector_prompt``, ``parse_selector_output``, ``SelectorDecision``)
and never re-implements it. The default invocation is a dry run with a fake
provider; live provider calls need ``--live`` plus
``--confirm-live-provider-calls``.
"""

BENCHMARK_VERSION = "selector-v2-1"
