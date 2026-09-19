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


def test_missing_temperature_or_reasoning_none_stops_before_live():
    params = ["max_tokens", "reasoning", "reasoning_effort"]
    out = mc.parity_check(model_meta=_meta(params), endpoints=[{"supported_parameters": params}],
                          frozen=FROZEN)
    assert out["verdict"] == mc.STOP
    assert set(out["blocked_parameters"]) == {"temperature", "reasoning", "max_tokens"}
    # the adapter cannot send reasoning "none"
    assert "none" not in out["checks"]["reasoning"]["adapter_transport_values"]


@pytest.mark.skipif(not (EXP / "capability-validation.json").exists(), reason="private artifacts absent")
def test_recorded_validation_reproduces_and_no_live_run_exists():
    models = json.loads((EXP / "openrouter-models-metadata.json").read_text())["models"]
    luna = next(m for m in models if m["id"] == "openai/gpt-5.6-luna")
    endpoints = json.loads((EXP / "openrouter-endpoints-gpt-5.6-luna.json").read_text())["data"]["endpoints"]
    recorded = json.loads((EXP / "capability-validation.json").read_text())
    assert mc.parity_check(model_meta=luna, endpoints=endpoints, frozen=FROZEN) == recorded["parity"]
    assert recorded["parity"]["verdict"] == mc.STOP
    mini = next(m for m in models if m["id"] == "openai/gpt-4o-mini")
    assert "temperature" in mini["supported_parameters"]
    assert not any((EXP / name).exists() for name in ("runs", "m1-plan.json", "m1-responses.jsonl"))
    manifest = json.loads((EXP / "manifest.json").read_text())
    assert manifest["live_calls"] == 0 and manifest["status"] == "STOP_BEFORE_LIVE"
    assert manifest["adjudication_provenance"]["adjudicator_type"] == "model"
    assert "human" not in manifest["adjudication_provenance"]["method"]


def test_frozen_inputs_unchanged():
    assert load_prompt("variant_c_v1").fingerprint == C_FP
    assert prompt_manifest() == load_committed_manifest()
    assert vc.PROMPT_ID == "variant_c_v1"
