#!/usr/bin/env python3
"""Activate the internal-pilot Intent Analyzer (Luna) through the admin API.

Idempotent operator tool for freeze amendment 5. It never touches the
database directly: every change goes through ``/api/ai-config`` as a
super_admin, and every mutation carries ``expected_version`` so a concurrent
admin edit makes it stop (HTTP 409) instead of overwriting.

Steps (each skipped when already in the target state):

  A. Rollback-safe intermediate version: intent_analyzer -> the existing
     openrouter/openai/gpt-4o-mini registry model, max_tokens 600,
     temperature 0.0. Its version id is ROLLBACK_VERSION. The registry's
     original version 1 (gpt-4o-mini @300) is NOT a valid rollback target:
     300 tokens truncates the pilot's 500-character inputs.
  B. Luna registry entry (created once), then UNTESTED -> QUALIFIED with the
     qualification reference.
  C. intent_analyzer -> Luna, reasoning_effort "none" (explicit, transmitted),
     max_tokens 600, temperature 0.0, no timeout/retry overrides.

The selector is never modified; the script aborts before any change if the
selector does not already match the frozen pilot selector config.

Usage (inside the backend container, where the API listens on :8000):

  AUZEF_ADMIN_EMAIL=... AUZEF_ADMIN_PASSWORD=... \\
    python deploy/internal-pilot/activate-luna.py [--base-url URL] [--dry-run]

Rollback (analyzer back to gpt-4o-mini @600):

  POST /api/ai-config/config/rollback/<ROLLBACK_VERSION>
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

# Kept equal to services/internal_pilot_candidate.py by
# backend/tests/test_internal_pilot_candidate.py (this script runs without the
# backend package on its import path).
ANALYZER_MODEL = "openai/gpt-6-luna"
ANALYZER_PROVIDER = "openrouter"
ANALYZER_MAX_TOKENS = 600
ANALYZER_TEMPERATURE = 0.0
ANALYZER_REASONING = "none"
ANALYZER_CONFIG_FINGERPRINT = "0181aa63cac4912085dff1934c09674c59d80815cdfdefc8a2f5c2d4a279e835"
ROLLBACK_MODEL = "openai/gpt-4o-mini"
ROLLBACK_CONFIG_FINGERPRINT = "512fe08df368e629a4960ea8f55bfa19874f49cf12f12b58ed8a681b00ab5ee3"
SELECTOR_CONFIG_FINGERPRINT = "af9eb2d0767d37cd632799cbae39e7938585b243ceb4c7a1527b8028cd489a6e"
LUNA_REGISTRY_ENTRY = {
    "display_name": "GPT-6 Luna (OpenRouter)",
    "provider": ANALYZER_PROVIDER,
    "model_identifier": ANALYZER_MODEL,
    "allowed_capabilities": ["intent_analyzer"],
    "supports_structured_output": True,
    "supports_reasoning_effort": True,
    "allowed_reasoning_efforts": ["none"],
}
LUNA_QUALIFICATION_REFERENCE = (
    "LUNA_QUALIFICATION_REPORT 2026-09-25 + PILOT_CANDIDATE_VALIDATION 2026-09-27"
)

Call = Callable[[str, str, Optional[dict]], "tuple[int, dict]"]


class ActivationError(RuntimeError):
    """Stop without further changes; the message says what to resolve."""


def _ok(status: int, data: dict, what: str) -> dict:
    if status >= 400:
        raise ActivationError(f"{what} failed: HTTP {status} {json.dumps(data, ensure_ascii=False)}")
    return data


def _analyzer_params(model_id: int, *, reasoning: Optional[str]) -> dict:
    return {
        "model_registry_id": model_id,
        "temperature": ANALYZER_TEMPERATURE,
        "max_tokens": ANALYZER_MAX_TOKENS,
        "reasoning_effort": reasoning,
        "timeout_seconds": None,
        "max_retries": None,
        "structured_output_enabled": False,
    }


def _find_model(models: list, provider: str, identifier: str) -> Optional[dict]:
    for model in models:
        if model["provider"] == provider and model["model_identifier"] == identifier:
            return model
    return None


def activate(call: Call, *, dry_run: bool = False, log=print) -> dict:
    """Run steps A-C. Returns {"rollback_version", "active_version", "changes"}."""
    changes: list[str] = []

    config = _ok(*call("GET", f"{API}/config", None), "read config")
    if config.get("status") != "OK" or config.get("version") is None:
        raise ActivationError(f"managed config is not OK: {config.get('status')} {config.get('error')}")
    selector_fp = config["capabilities"]["selector"].get("config_fingerprint")
    if selector_fp != SELECTOR_CONFIG_FINGERPRINT:
        raise ActivationError(
            f"selector config {selector_fp} != frozen {SELECTOR_CONFIG_FINGERPRINT}; "
            "this script never edits the selector — resolve it first")
    version = config["version"]
    analyzer_fp = config["capabilities"]["intent_analyzer"].get("config_fingerprint")
    models = _ok(*call("GET", f"{API}/models", None), "list models")["models"]

    # ── A. rollback-safe intermediate version ───────────────────────────────
    rollback_version = None
    if analyzer_fp == ROLLBACK_CONFIG_FINGERPRINT:
        rollback_version = version
    else:
        history = _ok(*call("GET", f"{API}/config/history?limit=500", None),
                      "read history")["versions"]
        for entry in history:  # newest first
            caps = entry["snapshot"]["capabilities"]
            if (caps["intent_analyzer"].get("config_fingerprint") == ROLLBACK_CONFIG_FINGERPRINT
                    and caps["selector"].get("config_fingerprint") == SELECTOR_CONFIG_FINGERPRINT):
                rollback_version = entry["version"]
                break
        if rollback_version is None:
            if analyzer_fp == ANALYZER_CONFIG_FINGERPRINT:
                raise ActivationError(
                    "Luna is active but no gpt-4o-mini @600 version exists to roll back to")
            mini = _find_model(models, "openrouter", ROLLBACK_MODEL)
            if mini is None:
                raise ActivationError(f"registry has no openrouter/{ROLLBACK_MODEL} model")
            log(f"[A] intent_analyzer -> {ROLLBACK_MODEL} @{ANALYZER_MAX_TOKENS} (rollback base)")
            if dry_run:
                rollback_version = "<new version>"
            else:
                result = _ok(*call("PUT", f"{API}/config/intent_analyzer", {
                    "expected_version": version, **_analyzer_params(mini["id"], reasoning=None),
                }), "set rollback-safe analyzer")
                version = rollback_version = result["version"]
                changes.append("rollback_version_created")
    log(f"ROLLBACK_VERSION = {rollback_version}")

    # ── B. Luna registry entry + qualification ──────────────────────────────
    luna = _find_model(models, ANALYZER_PROVIDER, ANALYZER_MODEL)
    if luna is None:
        log(f"[B] register {ANALYZER_PROVIDER}/{ANALYZER_MODEL}")
        if dry_run:
            luna = {"id": "<new id>", **LUNA_REGISTRY_ENTRY, "qualification_status": "UNTESTED"}
        else:
            luna = _ok(*call("POST", f"{API}/models", dict(LUNA_REGISTRY_ENTRY)),
                       "register Luna")
            changes.append("luna_registered")
    else:
        drift = {k: v for k, v in LUNA_REGISTRY_ENTRY.items()
                 if k not in ("provider", "model_identifier") and luna.get(k) != v}
        if drift:
            log(f"[B] correct Luna registry fields: {sorted(drift)}")
            if not dry_run:
                luna = _ok(*call("PATCH", f"{API}/models/{luna['id']}", drift),
                           "correct Luna fields")
                changes.append("luna_fields_corrected")
        if not luna.get("enabled", True):
            raise ActivationError("Luna registry entry is disabled; enable it explicitly first")
    if luna.get("qualification_status") != "QUALIFIED":
        log("[B] qualify Luna (UNTESTED -> QUALIFIED)")
        if not dry_run:
            luna = _ok(*call("PATCH", f"{API}/models/{luna['id']}", {
                "qualification_status": "QUALIFIED",
                "qualification_reference": LUNA_QUALIFICATION_REFERENCE,
            }), "qualify Luna")
            changes.append("luna_qualified")

    # ── C. analyzer -> Luna ─────────────────────────────────────────────────
    if not dry_run:
        config = _ok(*call("GET", f"{API}/config", None), "re-read config")
        version = config["version"]
        analyzer_fp = config["capabilities"]["intent_analyzer"].get("config_fingerprint")
    if analyzer_fp != ANALYZER_CONFIG_FINGERPRINT:
        log(f"[C] intent_analyzer -> {ANALYZER_MODEL}, reasoning={ANALYZER_REASONING}, "
            f"max_tokens={ANALYZER_MAX_TOKENS}")
        if not dry_run:
            _ok(*call("PUT", f"{API}/config/intent_analyzer", {
                "expected_version": version,
                **_analyzer_params(luna["id"], reasoning=ANALYZER_REASONING),
            }), "assign Luna")
            changes.append("luna_assigned")

    if dry_run:
        return {"rollback_version": rollback_version, "active_version": None,
                "changes": changes, "dry_run": True}

    # ── verify ──────────────────────────────────────────────────────────────
    final = _ok(*call("GET", f"{API}/config", None), "verify config")
    got_analyzer = final["capabilities"]["intent_analyzer"].get("config_fingerprint")
    got_selector = final["capabilities"]["selector"].get("config_fingerprint")
    if got_analyzer != ANALYZER_CONFIG_FINGERPRINT:
        raise ActivationError(f"analyzer fingerprint {got_analyzer} != {ANALYZER_CONFIG_FINGERPRINT}")
    if got_selector != SELECTOR_CONFIG_FINGERPRINT:
        raise ActivationError(f"selector fingerprint changed to {got_selector}")
    log(f"ACTIVE_VERSION = {final['version']}")
    log(f"intent_analyzer config fingerprint = {got_analyzer}")
    log(f"selector config fingerprint        = {got_selector} (unchanged)")
    log(f"rollback -> {rollback_version}: POST {API}/config/rollback/{rollback_version} "
        f'{{"expected_version": {final["version"]}}}  (never version 1: gpt-4o-mini @300)')
    return {"rollback_version": rollback_version, "active_version": final["version"],
            "changes": changes, "dry_run": False}


# ── HTTP transport (stdlib) ─────────────────────────────────────────────────


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
        status, headers, data = self._request(
            "POST", "/api/auth/login", {"email": email, "password": password})
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
    parser.add_argument("--dry-run", action="store_true",
                        help="print the planned steps without changing anything")
    args = parser.parse_args(argv)
    email, password = os.getenv("AUZEF_ADMIN_EMAIL"), os.getenv("AUZEF_ADMIN_PASSWORD")
    if not email or not password:
        print("AUZEF_ADMIN_EMAIL and AUZEF_ADMIN_PASSWORD must be set (super_admin)",
              file=sys.stderr)
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
