"""Versioned, production-side selector system-prompt catalog.

Runtime owns its prompts. This module deliberately has **no dependency on
``benchmarks``**: the internal pilot must be able to serve ``variant_a_v1``
without the benchmark package being importable or even present.

Two prompt identities exist:

``production_v2``
    The historical production prompt, resolved *dynamically* from
    ``services.selector.SELECTOR_SYSTEM_PROMPT`` on every call. It is never
    copied or cached here, so the historical benchmark identity
    (``2d59cfb6…``) stays exactly as it was, and tests that monkeypatch that
    module attribute still observe the change.

``variant_a_v1``
    The internal-pilot prompt, stored as data at
    ``services/prompts/selector/variant_a_v1.md`` and fingerprinting to
    ``1aed5688…``.

Why the Variant A text exists twice
-----------------------------------
``benchmarks/selector_v2/prompts/variant_a_v1.md`` is a *historical*
artifact: its path is recorded in the committed benchmark prompt manifest,
and six test modules assert that manifest byte-for-byte. Rewriting it to
point at the runtime copy would mutate that historical record, which the
pilot task forbids. So the runtime keeps its own copy and
``tests/test_internal_pilot_runtime.py`` asserts the two are identical and
fingerprint the same. If they ever diverge, that test fails.

Selection is configuration, not code: ``SELECTOR_PROMPT_VERSION`` selects the
version and defaults to ``production_v2``, so no existing deployment changes
behaviour by upgrading.
"""
from __future__ import annotations

import hashlib
import importlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

PROMPT_VERSION_ENV = "SELECTOR_PROMPT_VERSION"

PRODUCTION_V2 = "production_v2"
VARIANT_A_V1 = "variant_a_v1"

#: The version served when nothing is configured. Changing this would change
#: public production behaviour, so the internal pilot overrides it by config
#: instead.
DEFAULT_PROMPT_VERSION = PRODUCTION_V2

PROMPT_DIR = Path(__file__).with_name("prompts") / "selector"

#: File-backed versions. ``production_v2`` is intentionally absent: it is read
#: from the runtime module so there is exactly one copy of that text.
_FILE_PROMPTS = {VARIANT_A_V1: "variant_a_v1.md"}

PROMPT_VERSIONS = (PRODUCTION_V2, VARIANT_A_V1)


class UnknownSelectorPromptVersion(ValueError):
    """A selector prompt version was requested that the catalog does not have.

    Raised rather than silently falling back: serving a different prompt than
    the operator configured is exactly the drift the pilot freeze exists to
    prevent.
    """


def normalize_prompt(text: str) -> str:
    """CRLF->LF, rstrip each line, strip leading/trailing blank lines.

    Identical to the benchmark prompt-contract normalization, so a prompt
    fingerprints the same on both sides. The normalized text is what the
    model receives.
    """
    lines = [
        line.rstrip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    return "\n".join(lines).strip("\n")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SelectorPromptSpec:
    version: str
    text: str
    source: str

    @property
    def fingerprint(self) -> str:
        return _sha256(self.text)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "source": self.source,
            "prompt_fingerprint": self.fingerprint,
            "chars": len(self.text),
        }


def _production_text() -> str:
    """Read the production prompt from the runtime module at call time.

    Module-attribute access (not a ``from ... import``) is required: tests
    monkeypatch ``services.selector.SELECTOR_SYSTEM_PROMPT`` and expect the
    resolved prompt — and therefore the benchmark contract fingerprint — to
    follow. Caching the text here would silently break that.
    """
    selector = importlib.import_module("services.selector")
    return selector.SELECTOR_SYSTEM_PROMPT


def load_selector_prompt(version: str) -> SelectorPromptSpec:
    if version == PRODUCTION_V2:
        return SelectorPromptSpec(
            version=PRODUCTION_V2,
            text=normalize_prompt(_production_text()),
            source="services.selector.SELECTOR_SYSTEM_PROMPT (runtime)",
        )
    if version not in _FILE_PROMPTS:
        known = ", ".join(PROMPT_VERSIONS)
        raise UnknownSelectorPromptVersion(
            f"unknown selector prompt version {version!r}; known: {known}"
        )
    path = PROMPT_DIR / _FILE_PROMPTS[version]
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UnknownSelectorPromptVersion(
            f"selector prompt {version!r} is registered but unreadable at {path}: {exc}"
        ) from None
    return SelectorPromptSpec(
        version=version,
        text=normalize_prompt(raw),
        source=f"services/prompts/selector/{path.name}",
    )


def configured_prompt_version(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    raw = (env.get(PROMPT_VERSION_ENV) or "").strip()
    return raw or DEFAULT_PROMPT_VERSION


def resolve_selector_prompt(
    environ: Optional[Mapping[str, str]] = None,
) -> SelectorPromptSpec:
    """The prompt this runtime actually serves, right now.

    Preflight calls this rather than trusting the configured string, so a
    version that is configured but unservable fails loudly.
    """
    return load_selector_prompt(configured_prompt_version(environ))


def catalog_manifest() -> dict:
    """Every runtime prompt identity, for preflight and reporting."""
    return {
        "default_version": DEFAULT_PROMPT_VERSION,
        "version_env": PROMPT_VERSION_ENV,
        "prompts": {
            version: load_selector_prompt(version).to_dict()
            for version in PROMPT_VERSIONS
        },
    }
