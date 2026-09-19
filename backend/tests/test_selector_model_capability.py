"""Phase 7B model-capability experiment: offline config-parity validation.
No provider call: the check reads saved OpenRouter catalog metadata only."""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

from benchmarks.selector_v2 import model_capability as mc
from benchmarks.selector_v2 import variant_c as vc
from benchmarks.selector_v2.prompt_contract import load_committed_manifest, load_prompt, prompt_manifest
from benchmarks.selector_v2.safety import no_live_calls

REPO = Path(__file__).resolve().parents[2]
EXP = REPO / "outputs" / "selector-v2-benchmark" / "model-experiment" / "c-v1-gpt-5.6-luna"
FROZEN = {"temperature": 0.0, "max_tokens": 32, "reasoning": None}
C_FP = "fc1811443061737b090b388f0b52bb03f68af99a965f2956c4833663847eb9a9"


@pytest.fixture(autouse=True)
def _no_network():
    db_hosts = [urlparse(os.environ[name]).hostname
                for name in ("DATABASE_URL", "ADMIN_DATABASE_URL", "CHAT_DATABASE_URL")
                if os.environ.get(name)]
    with no_live_calls(allow_hosts=db_hosts):
        yield


def _meta(params):
    return {"id": "x/model", "supported_parameters": params}


def test_parity_ok_for_plain_model():
    params = ["max_tokens", "temperature", "response_format"]
    out = mc.parity_check(model_meta=_meta(params), endpoints=[{"supported_parameters": params}],
                          frozen=FROZEN)
    assert out["verdict"] == mc.READY and out["blocked_parameters"] == []


def test_missing_temperature_is_omitted_never_substituted():
    params = ["max_tokens", "reasoning", "reasoning_effort"]
    out = mc.parity_check(model_meta=_meta(params), endpoints=[{"supported_parameters": params}],
                          frozen=FROZEN)
    assert out["verdict"] == mc.STOP and out["blocked_parameters"] == ["temperature"]
    assert "none" in out["checks"]["reasoning"]["adapter_transport_values"]
    plan = mc.request_plan(out)
    assert plan["omitted_request_params"] == ["temperature"] and plan["still_blocked"] == []
    # anything other than temperature still stops
    other = mc.parity_check(model_meta=_meta(["reasoning"]), endpoints=[{"supported_parameters": ["reasoning"]}],
                            frozen=FROZEN)
    assert mc.request_plan(other)["verdict"] == mc.STOP


@pytest.mark.skipif(not (EXP / "capability-validation.json").exists(), reason="private artifacts absent")
def test_recorded_validation_reproduces_and_no_live_run_exists():
    models = json.loads((EXP / "openrouter-models-metadata.json").read_text())["models"]
    luna = next(m for m in models if m["id"] == "openai/gpt-5.6-luna")
    endpoints = json.loads((EXP / "openrouter-endpoints-gpt-5.6-luna.json").read_text())["data"]["endpoints"]
    # v1 (historical STOP, before the user's decision) is kept unchanged
    v1 = json.loads((EXP / "capability-validation.json").read_text())
    assert v1["parity"]["verdict"] == mc.STOP
    assert set(v1["parity"]["blocked_parameters"]) == {"temperature", "reasoning", "max_tokens"}
    stop_manifest = json.loads((EXP / "stop-before-live-manifest.json").read_text())
    assert stop_manifest["live_calls"] == 0 and stop_manifest["status"] == "STOP_BEFORE_LIVE"
    assert stop_manifest["adjudication_provenance"]["adjudicator_type"] == "model"
    assert "human" not in stop_manifest["adjudication_provenance"]["method"]
    # v2 reproduces with the current transport: only temperature blocked -> declared omission
    v2 = json.loads((EXP / "capability-validation-v2.json").read_text())
    parity = mc.parity_check(model_meta=luna, endpoints=endpoints, frozen={**FROZEN, "reasoning": "none"})
    assert parity == v2["parity"] and parity["blocked_parameters"] == ["temperature"]
    assert mc.request_plan(parity) == v2["request_plan"]
    assert v2["request_plan"]["omitted_request_params"] == ["temperature"]
    mini = next(m for m in models if m["id"] == "openai/gpt-4o-mini")
    assert "temperature" in mini["supported_parameters"]


def test_frozen_inputs_unchanged():
    assert load_prompt("variant_c_v1").fingerprint == C_FP
    assert prompt_manifest() == load_committed_manifest()
    assert vc.PROMPT_ID == "variant_c_v1"
