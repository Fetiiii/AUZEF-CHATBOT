"""TEST-ONLY Intent Analyzer model override for controlled local load tests.

Lets a local, instrumented test stack route the Intent Analyzer to another
OpenRouter model WITHOUT touching the model registry or the managed AI config.
It is inert unless BOTH are set in the container environment:

  AUZEF_TEST_ANALYZER_MODEL=<openrouter model id>     (e.g. openai/gpt-6-luna)
  AUZEF_LOAD_METRICS=1                                 (instrumented test stack)

Optional: AUZEF_TEST_ANALYZER_REASONING=none|low|medium|high.

A second, independent test hook (same gate) points the OpenRouter client at a
local fault-injection proxy:

  AUZEF_TEST_OPENROUTER_BASE_URL=http://127.0.0.1:18080/api/v1

Only ``intent_analyzer.model`` / ``reasoning_effort`` change; the selector and
every other field keep the managed values. A non-OpenRouter analyzer is left
untouched. The changed config has a different fingerprint, so decision traces
and circuit-breaker keys identify overridden traffic.
"""
from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Mapping, Optional

from services.llm_config import EffectiveLLMConfigSet, ReasoningEffort

logger = logging.getLogger("auzef")

MODEL_ENV = "AUZEF_TEST_ANALYZER_MODEL"
BASE_URL_ENV = "AUZEF_TEST_OPENROUTER_BASE_URL"
REASONING_ENV = "AUZEF_TEST_ANALYZER_REASONING"
GATE_ENV = "AUZEF_LOAD_METRICS"
_warned = False


def apply_test_analyzer_override(
    configs: EffectiveLLMConfigSet, environ: Optional[Mapping[str, str]] = None
) -> EffectiveLLMConfigSet:
    global _warned
    env = os.environ if environ is None else environ
    model = (env.get(MODEL_ENV) or "").strip()
    if not model or env.get(GATE_ENV) != "1":
        return configs
    analyzer = configs.intent_analyzer
    if analyzer.provider != "openrouter":
        return configs
    reasoning = (env.get(REASONING_ENV) or "").strip()
    overridden = replace(
        analyzer, model=model,
        reasoning_effort=ReasoningEffort(reasoning) if reasoning else None,
    )
    if not _warned:
        logger.warning(
            "TEST-ONLY analyzer override active: intent_analyzer model=%s reasoning=%s "
            "(registry/managed config unchanged)", model, reasoning or None,
        )
        _warned = True
    return replace(configs, intent_analyzer=overridden)


def openrouter_base_url(default: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """Test-only OpenRouter base URL (fault-injection proxy); inert by default."""
    env = os.environ if environ is None else environ
    override = (env.get(BASE_URL_ENV) or "").strip()
    if not override or env.get(GATE_ENV) != "1":
        return default
    if not override.startswith(("http://127.0.0.1:", "http://localhost:")):
        # Only a loopback test proxy is accepted; never an arbitrary endpoint.
        return default
    logger.warning("TEST-ONLY OpenRouter base URL override active: %s", override)
    return override
