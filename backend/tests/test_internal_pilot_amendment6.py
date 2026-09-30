"""Freeze amendment 6 (INTERNAL_PILOT_CANDIDATE: Luna + variant_a_v3_contract selector)."""
import importlib.util
import json
import shutil
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services import internal_pilot_amendment6 as am6
from services.ai_registry import CapabilityAssignment, CapabilityParams, ModelDefinition, QualificationStatus
from services.decision_trace import DecisionTrace
from services.llm_config import LLMCapability
from services.selector_prompt_catalog import load_selector_prompt

IA, SEL = LLMCapability.INTENT_ANALYZER, LLMCapability.SELECTOR
SCRIPT = am6.REPO_ROOT / "deploy" / "internal-pilot" / "activate-pilot-luna-v3.py"


def _script():
    spec = importlib.util.spec_from_file_location("activate_pilot_luna_v3", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_operator_script_constants_equal_the_amendment():
    s = _script()
    assert s.LUNA_MODEL == am6.LUNA_MODEL and s.PROVIDER == am6.PROVIDER
    assert s.ANALYZER_MAX_TOKENS == am6.ANALYZER_MAX_TOKENS and s.SELECTOR_MAX_TOKENS == am6.SELECTOR_MAX_TOKENS
    assert s.ANALYZER_CONFIG_FINGERPRINT == am6.ANALYZER_CONFIG_FINGERPRINT
    assert s.SELECTOR_CONFIG_FINGERPRINT == am6.SELECTOR_CONFIG_FINGERPRINT
    assert s.LUNA_REGISTRY_ENTRY == am6.LUNA_REGISTRY_ENTRY


def test_expected_configs_hash_to_the_pinned_fingerprints():
    assert am6.expected_config(SEL).fingerprint == am6.SELECTOR_CONFIG_FINGERPRINT
    assert am6.expected_config(IA).fingerprint == am6.ANALYZER_CONFIG_FINGERPRINT
    assert am6.expected_config(SEL).temperature is None and am6.expected_config(IA).temperature is None
    assert load_selector_prompt(am6.SELECTOR_PROMPT_VERSION).fingerprint == am6.SELECTOR_PROMPT_FINGERPRINT


def test_inherited_analyzer_identity_equals_amendment_5():
    ident = am6.amendment_5_identity()
    assert ident["amendment_fingerprint"] == am6.AMENDMENT_5_FINGERPRINT
    assert ident["analyzer_prompt_fingerprint"] == am6.ANALYZER_PROMPT_FINGERPRINT
    assert ident["parser_contract_fingerprint"] == am6.PARSER_CONTRACT_FINGERPRINT


def test_builder_is_deterministic_and_status_is_candidate_only():
    a = am6.build_freeze_amendment_6(git_commit="a", created_at="t1")
    b = am6.build_freeze_amendment_6(git_commit="b", created_at="t2", gate_evidence={"x": 1})
    assert a["amendment_fingerprint"] == b["amendment_fingerprint"] == am6.amendment_6_fingerprint(a)
    assert a["status"] == "INTERNAL_PILOT_CANDIDATE"
    assert set(a["not_qualified_as"]) == {"PRODUCTION_QUALIFIED", "FINAL_SELECTOR_QUALIFIED"}
    assert a["parent_amendment_fingerprint"] == am6.AMENDMENT_5_FINGERPRINT and a["parents_are_immutable"]
    assert a["selector"]["temperature_sent"] is False and a["intent_analyzer"]["temperature_sent"] is False


def _repo_copy(tmp_path, *, write_amendment=True):
    for rel in (am6.AMENDMENT_5_REL, am6.WIDGET_REL, am6.PILOT_ENV_EXAMPLE_REL):
        target = tmp_path.joinpath(*rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(am6.REPO_ROOT.joinpath(*rel), target)
    if write_amendment:
        amendment = am6.build_freeze_amendment_6(git_commit="c", created_at="t", root=tmp_path)
        tmp_path.joinpath(*am6.AMENDMENT_6_REL).write_text(json.dumps(amendment), encoding="utf-8")
    return tmp_path


def test_static_preflight_passes_on_a_consistent_checkout(tmp_path):
    report = am6.run_static_preflight(_repo_copy(tmp_path))
    assert report.passed, [c for c in report.checks if not c.passed]


def test_static_preflight_fails_on_missing_or_tampered_artifact(tmp_path):
    root = _repo_copy(tmp_path, write_amendment=False)
    assert not am6.run_static_preflight(root).passed
    amendment = am6.build_freeze_amendment_6(git_commit="c", created_at="t", root=root)
    amendment["selector"]["max_tokens"] = 64
    root.joinpath(*am6.AMENDMENT_6_REL).write_text(json.dumps(amendment), encoding="utf-8")
    failed = {c.name for c in am6.run_static_preflight(root).checks if not c.passed}
    assert "amendment_6_matches_builder" in failed


def test_builder_refuses_a_changed_amendment_5(tmp_path):
    root = _repo_copy(tmp_path, write_amendment=False)
    path = root.joinpath(*am6.AMENDMENT_5_REL)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["amendment_fingerprint"] = "0" * 64
    path.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(ValueError):
        am6.build_freeze_amendment_6(git_commit="c", created_at="t", root=root)


def _luna(**overrides):
    entry = am6.LUNA_REGISTRY_ENTRY
    base = dict(id=9, display_name=entry["display_name"], provider=entry["provider"],
                model_identifier=entry["model_identifier"], enabled=True,
                allowed_capabilities=tuple(entry["allowed_capabilities"]), supports_structured_output=True,
                supports_reasoning_effort=True, allowed_reasoning_efforts=("none",),
                qualification_status=QualificationStatus.QUALIFIED, supports_temperature=False)
    base.update(overrides)
    return ModelDefinition(**base)


def _state(**overrides):
    luna = _luna()
    base = dict(
        analyzer=CapabilityAssignment(IA, luna, CapabilityParams(temperature=0.0, max_tokens=600, reasoning_effort="none")),
        selector=CapabilityAssignment(SEL, luna, CapabilityParams(temperature=0.0, max_tokens=32, reasoning_effort="none")),
        config_version=7, llm_enabled=True, selector_prompt_version=am6.SELECTOR_PROMPT_VERSION,
        selector_prompt_fingerprint=am6.SELECTOR_PROMPT_FINGERPRINT,
        kb={"fingerprint": am6.KB_FINGERPRINT, "active_qna": 322, "active_queries": 2667},
        meili_documents=322, qdrant_points=2989, analyzer_prompt_fingerprint=am6.ANALYZER_PROMPT_FINGERPRINT,
        parser_fingerprint=am6.PARSER_CONTRACT_FINGERPRINT, backend_max_chars=500, trace_schema=8)
    base.update(overrides)
    return am6.LiveState(**base)


def test_live_preflight_passes_for_the_pinned_runtime():
    report = am6.run_live_preflight(_state())
    assert report.passed, [c for c in report.checks if not c.passed]


@pytest.mark.parametrize("overrides,check", [
    ({"selector_prompt_version": "variant_a_v1"}, "selector_prompt_version_matches"),
    ({"meili_documents": 321}, "meili_documents_match"),
    ({"kb": {"fingerprint": "x", "active_qna": 322, "active_queries": 2667}}, "kb_fingerprint_matches"),
    ({"llm_enabled": False}, "llm_enabled_live_is_true"),
    ({"trace_schema": 7}, "decision_trace_schema_matches"),
])
def test_live_preflight_fails_on_each_drift(overrides, check):
    failed = {c.name for c in am6.run_live_preflight(_state(**overrides)).checks if not c.passed}
    assert check in failed


def test_live_preflight_fails_when_selector_is_still_4o_mini_or_temperature_is_sent():
    mini = _luna(model_identifier="openai/gpt-4o-mini", supports_temperature=True,
                 supports_reasoning_effort=False, allowed_reasoning_efforts=())
    state = _state(selector=CapabilityAssignment(SEL, mini, CapabilityParams(temperature=0.0, max_tokens=32)))
    failed = {c.name for c in am6.run_live_preflight(state).checks if not c.passed}
    assert {"selector_model_matches", "selector_temperature_not_sent", "selector_config_fingerprint_matches"} <= failed
    temp_luna = _luna(supports_temperature=True)
    state = _state(analyzer=CapabilityAssignment(IA, temp_luna, CapabilityParams(temperature=0.0, max_tokens=600,
                                                                                   reasoning_effort="none")))
    failed = {c.name for c in am6.run_live_preflight(state).checks if not c.passed}
    assert "analyzer_temperature_not_sent" in failed


class FakeApi:
    """Minimal in-memory /api/ai-config for the activation script."""

    def __init__(self, analyzer_fp="0181aa63", selector_fp="af9eb2d0", luna=None):
        self.version = 5
        self.fps = {"intent_analyzer": analyzer_fp, "selector": selector_fp}
        self.models = [luna] if luna else []
        self.calls = []

    def call(self, method, path, body):
        self.calls.append((method, path))
        if method == "GET" and path.endswith("/config"):
            return 200, {"status": "OK", "version": self.version,
                         "capabilities": {k: {"config_fingerprint": v} for k, v in self.fps.items()}}
        if method == "GET" and path.endswith("/models"):
            return 200, {"models": self.models}
        if method == "POST" and path.endswith("/models"):
            model = {"id": 42, **body, "qualification_status": "UNTESTED", "enabled": True}
            self.models.append(model)
            return 200, model
        if method == "PATCH":
            self.models[0] = {**self.models[0], **body}
            return 200, self.models[0]
        if method == "PUT":
            assert body["expected_version"] == self.version and body["temperature"] == 0.0
            cap = path.rsplit("/", 1)[1]
            self.fps[cap] = (am6.ANALYZER_CONFIG_FINGERPRINT if cap == "intent_analyzer"
                             else am6.SELECTOR_CONFIG_FINGERPRINT)
            self.version += 1
            return 200, {"version": self.version}
        return 404, {}


def test_activation_script_is_idempotent_and_prints_the_rollback_point():
    s = _script()
    existing = {"id": 7, **{k: v for k, v in am6.LUNA_REGISTRY_ENTRY.items()},
                "allowed_capabilities": ["intent_analyzer"], "supports_temperature": True,
                "qualification_status": "QUALIFIED", "enabled": True}
    api = FakeApi(luna=existing)
    logs = []
    first = s.activate(api.call, log=logs.append)
    assert first["pre_activation_version"] == 5
    assert set(first["changes"]) == {"luna_fields_corrected", "intent_analyzer_assigned", "selector_assigned"}
    assert api.models[0]["supports_temperature"] is False
    second = s.activate(api.call, log=logs.append)
    assert second["changes"] == []
    assert any("rollback -> 5" in line for line in logs)


def test_trace_v8_carries_user_message_id_and_selector_prompt_identity(monkeypatch):
    monkeypatch.setenv("SELECTOR_PROMPT_VERSION", am6.SELECTOR_PROMPT_VERSION)
    trace = DecisionTrace(endpoint="widget_chat", conversation_id=3, user_message_id=11)
    result = SimpleNamespace(invocation=None, decision="NONE", selected_candidate_ref=None, selected_kind=None,
                             selected_qna_id=None, selected_calendar_id=None, selected_candidate_source=None,
                             invalid_reason=None, status=SimpleNamespace(value="semantic_none"),
                             parse_status=SimpleNamespace(value="success"))
    trace.record_selector(result, outcome="semantic_none", config={"provider": "openrouter", "model": am6.LUNA_MODEL},
                          config_fingerprint=am6.SELECTOR_CONFIG_FINGERPRINT, candidate_refs=["qna:1"],
                          candidate_kinds=["QNA"], candidate_qna_ids=[1], purpose="intent_1", used_in_final=False)
    snapshot = trace.to_dict()
    assert snapshot["schema_version"] == 8
    assert snapshot["request"]["user_message_id"] == 11
    entry = snapshot["selectors"][0]
    assert entry["prompt_version"] == am6.SELECTOR_PROMPT_VERSION
    assert entry["prompt_fingerprint"] == am6.SELECTOR_PROMPT_FINGERPRINT
