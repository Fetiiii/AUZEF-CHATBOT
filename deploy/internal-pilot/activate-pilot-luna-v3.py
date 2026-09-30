#!/usr/bin/env python3
"""Activate the INTERNAL_PILOT_CANDIDATE (freeze amendment 6) through the admin API.

Idempotent operator tool. It never touches the database directly: every change
goes through ``/api/ai-config`` as a super_admin, and every config mutation
carries ``expected_version`` so a concurrent admin edit stops it (HTTP 409).

Steps (each skipped when already in the target state):

  0. Read the active config version: PRE_ACTIVATION_VERSION is the rollback
     point for both capabilities.
  A. Luna registry entry: create it, or correct drifted fields, so that it
     allows both capabilities, reasoning ["none"] and supports_temperature=false.
  B. Luna UNTESTED -> QUALIFIED with the qualification reference (if needed).
  C. intent_analyzer -> Luna, reasoning "none", max_tokens 600
     (temperature is stored as 0 and never sent).
  D. selector -> Luna, reasoning "none", max_tokens 32.
  E. Verify both config fingerprints against amendment 6.

The selector PROMPT is not an admin setting: set SELECTOR_PROMPT_VERSION=
variant_a_v3_contract in the backend environment and restart (pilot.env.example).

Usage (where the API listens, e.g. inside the backend container on :8000):

  AUZEF_ADMIN_EMAIL=... AUZEF_ADMIN_PASSWORD=... \\
    python deploy/internal-pilot/activate-pilot-luna-v3.py [--base-url URL] [--dry-run]

Rollback (both capabilities back to the pre-activation version):

  POST /api/ai-config/config/rollback/<PRE_ACTIVATION_VERSION>
       {"expected_version": <current version>}

Credentials are read from the environment only and are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Callable, Optional

API = "/api/ai-config"

# Kept equal to backend/services/internal_pilot_amendment6.py by
# backend/tests/test_internal_pilot_amendment6.py (this script runs without the
# backend package on its import path).
LUNA_MODEL = "openai/gpt-6-luna"
PROVIDER = "openrouter"
ANALYZER_MAX_TOKENS = 600
SELECTOR_MAX_TOKENS = 32
REASONING = "none"
ANALYZER_CONFIG_FINGERPRINT = "e74b94a3bf521115c98018e1ec6519a906eba157ded0f47928969c21cc95df23"
SELECTOR_CONFIG_FINGERPRINT = "f6fcb61426cf400fb7f679190480e97e3f2d0b8ea58aecfca94a3440ecb89346"
LUNA_REGISTRY_ENTRY = {
    "display_name": "GPT-6 Luna (OpenRouter)",
    "provider": PROVIDER,
    "model_identifier": LUNA_MODEL,
    "allowed_capabilities": ["intent_analyzer", "selector"],
    "supports_structured_output": True,
    "supports_reasoning_effort": True,
    "allowed_reasoning_efforts": ["none"],
    "supports_temperature": False,
}
LUNA_QUALIFICATION_REFERENCE = (
    "LUNA_QUALIFICATION_REPORT 2026-09-25 + PILOT_CANDIDATE_VALIDATION 2026-09-27; "
    "selector: SELECTOR_V3_MODEL_ABLATION_4OMINI_VS_LUNA 2026-09-28 (INTERNAL_PILOT_CANDIDATE)"
)

Call = Callable[[str, str, Optional[dict]], "tuple[int, dict]"]


class ActivationError(RuntimeError):
    """Stop without further changes; the message says what to resolve."""


def _ok(status: int, data: dict, what: str) -> dict:
    if status >= 400:
        raise ActivationError(f"{what} failed: HTTP {status} {json.dumps(data, ensure_ascii=False)}")
    return data


def _params(model_id, max_tokens: int) -> dict:
    return {"model_registry_id": model_id, "temperature": 0.0, "max_tokens": max_tokens,
            "reasoning_effort": REASONING, "timeout_seconds": None, "max_retries": None,
            "structured_output_enabled": False}


def _fp(config: dict, capability: str) -> Optional[str]:
    return config["capabilities"][capability].get("config_fingerprint")


def activate(call: Call, *, dry_run: bool = False, log=print) -> dict:
    changes: list[str] = []
    config = _ok(*call("GET", f"{API}/config", None), "read config")
    if config.get("status") != "OK" or config.get("version") is None:
        raise ActivationError(f"managed config is not OK: {config.get('status')} {config.get('error')}")
    pre_version = config["version"]
    log(f"PRE_ACTIVATION_VERSION = {pre_version} (rollback point)")
    models = _ok(*call("GET", f"{API}/models", None), "list models")["models"]

    # ── A. registry entry ───────────────────────────────────────────────────
    luna = next((m for m in models if m["provider"] == PROVIDER and m["model_identifier"] == LUNA_MODEL), None)
    if luna is None:
        log(f"[A] register {PROVIDER}/{LUNA_MODEL}")
        if dry_run:
            luna = {"id": "<new id>", **LUNA_REGISTRY_ENTRY, "qualification_status": "UNTESTED", "enabled": True}
        else:
            luna = _ok(*call("POST", f"{API}/models", dict(LUNA_REGISTRY_ENTRY)), "register Luna")
            changes.append("luna_registered")
    else:
        drift = {k: v for k, v in LUNA_REGISTRY_ENTRY.items()
                 if k not in ("provider", "model_identifier")
                 and (sorted(luna.get(k) or []) != sorted(v) if isinstance(v, list) else luna.get(k) != v)}
        if drift:
            log(f"[A] correct Luna registry fields: {sorted(drift)}")
            if not dry_run:
                luna = _ok(*call("PATCH", f"{API}/models/{luna['id']}", drift), "correct Luna fields")
                changes.append("luna_fields_corrected")
        if not luna.get("enabled", True):
            raise ActivationError("Luna registry entry is disabled; enable it explicitly first")

    # ── B. qualification ────────────────────────────────────────────────────
    if luna.get("qualification_status") != "QUALIFIED":
        log("[B] qualify Luna (-> QUALIFIED)")
        if not dry_run:
            luna = _ok(*call("PATCH", f"{API}/models/{luna['id']}", {
                "qualification_status": "QUALIFIED", "qualification_reference": LUNA_QUALIFICATION_REFERENCE,
            }), "qualify Luna")
            changes.append("luna_qualified")

    # ── C/D. assignments ────────────────────────────────────────────────────
    for step, capability, max_tokens, target in (
            ("C", "intent_analyzer", ANALYZER_MAX_TOKENS, ANALYZER_CONFIG_FINGERPRINT),
            ("D", "selector", SELECTOR_MAX_TOKENS, SELECTOR_CONFIG_FINGERPRINT)):
        if not dry_run:
            config = _ok(*call("GET", f"{API}/config", None), "re-read config")
        if _fp(config, capability) == target:
            continue
        log(f"[{step}] {capability} -> {LUNA_MODEL}, reasoning={REASONING}, max_tokens={max_tokens}")
        if not dry_run:
            _ok(*call("PUT", f"{API}/config/{capability}", {
                "expected_version": config["version"], **_params(luna["id"], max_tokens)}),
                f"assign {capability}")
            changes.append(f"{capability}_assigned")

    if dry_run:
        return {"pre_activation_version": pre_version, "active_version": None, "changes": changes,
                "dry_run": True}

    # ── E. verify ───────────────────────────────────────────────────────────
    final = _ok(*call("GET", f"{API}/config", None), "verify config")
    for capability, target in (("intent_analyzer", ANALYZER_CONFIG_FINGERPRINT),
                               ("selector", SELECTOR_CONFIG_FINGERPRINT)):
        if _fp(final, capability) != target:
            raise ActivationError(f"{capability} fingerprint {_fp(final, capability)} != {target}")
    log(f"ACTIVE_VERSION = {final['version']}")
    log(f"intent_analyzer config fingerprint = {ANALYZER_CONFIG_FINGERPRINT}")
    log(f"selector config fingerprint        = {SELECTOR_CONFIG_FINGERPRINT}")
    log(f"rollback -> {pre_version}: POST {API}/config/rollback/{pre_version} "
        f'{{"expected_version": {final["version"]}}}')
    return {"pre_activation_version": pre_version, "active_version": final["version"], "changes": changes,
            "dry_run": False}


class HttpApi:
    COOKIE = "auzef_admin_session"

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session: Optional[str] = None

    def _request(self, method: str, path: str, body: Optional[dict]):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.session:
            request.add_header("Cookie", f"{self.COOKIE}={self.session}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.headers, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b"{}"
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {"detail": raw.decode("utf-8", "replace")[:200]}
            return exc.code, exc.headers, payload

    def login(self, email: str, password: str) -> None:
        status, headers, _data = self._request("POST", "/api/auth/login", {"email": email, "password": password})
        if status != 200:
            raise ActivationError(f"login failed: HTTP {status}")
        for header in headers.get_all("Set-Cookie") or []:
            name, _, rest = header.partition("=")
            if name.strip() == self.COOKIE:
                self.session = rest.split(";", 1)[0]
        if not self.session:
            raise ActivationError("login returned no session cookie")

    def call(self, method: str, path: str, body: Optional[dict]):
        status, _headers, data = self._request(method, path, body)
        return status, data


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default=os.getenv("AUZEF_API_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--dry-run", action="store_true", help="print the planned steps without changing anything")
    args = parser.parse_args(argv)
    email, password = os.getenv("AUZEF_ADMIN_EMAIL"), os.getenv("AUZEF_ADMIN_PASSWORD")
    if not email or not password:
        print("AUZEF_ADMIN_EMAIL and AUZEF_ADMIN_PASSWORD must be set (super_admin)", file=sys.stderr)
        return 2
    api = HttpApi(args.base_url)
    try:
        api.login(email, password)
        result = activate(api.call, dry_run=args.dry_run)
    except ActivationError as exc:
        print(f"ACTIVATION STOPPED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
