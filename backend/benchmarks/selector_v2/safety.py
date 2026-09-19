"""Live-call safety: network guard and the explicit live-run gate.

Every non-live harness command runs inside ``no_live_calls``: outbound
sockets are refused except to explicitly allowlisted infrastructure hosts
(the local Postgres/Meili/Qdrant used for snapshot generation), and the
provider SDK entry points raise. A provider call therefore fails loudly
instead of costing money.
"""
from __future__ import annotations

import socket
from contextlib import contextmanager, ExitStack
from typing import Iterable
from unittest import mock


class LiveCallBlocked(RuntimeError):
    """Raised when non-live harness code attempts a network/provider call."""


def _host_of(address) -> str | None:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return None  # AF_UNIX path: local IPC, not network


@contextmanager
def no_live_calls(allow_hosts: Iterable[str] = (), *, block_sdks: bool = True):
    """Refuse outbound connections except to ``allow_hosts`` (names or IPs).

    With ``block_sdks=False`` (approved live runs only) the provider SDKs may
    run, but sockets can still reach only the allowlisted hosts.
    """
    real_getaddrinfo = socket.getaddrinfo
    allowed_names = {h for h in allow_hosts if h}
    allowed_ips: set[str] = set()
    for host in allowed_names:
        try:
            allowed_ips.update(info[4][0] for info in real_getaddrinfo(host, None))
        except (OSError, LiveCallBlocked):  # unresolvable now; resolved lazily later
            pass
        allowed_ips.add(host)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host is not None and str(host) not in allowed_names | allowed_ips:
            raise LiveCallBlocked(f"network lookup blocked by benchmark guard: {host}")
        infos = real_getaddrinfo(host, *args, **kwargs)
        if host is not None and str(host) in allowed_names:
            allowed_ips.update(info[4][0] for info in infos)  # DNS may rotate
        return infos

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address):
        host = _host_of(address)
        if host is not None and host not in allowed_ips:
            raise LiveCallBlocked(f"network connection blocked by benchmark guard: {host}")

    def guarded_connect(self, address):
        _check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    def sdk_blocked(*_args, **_kwargs):
        raise LiveCallBlocked("provider SDK call blocked: live mode not enabled")

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(socket, "getaddrinfo", guarded_getaddrinfo))
        stack.enter_context(mock.patch.object(socket.socket, "connect", guarded_connect))
        stack.enter_context(mock.patch.object(socket.socket, "connect_ex", guarded_connect_ex))
        if not block_sdks:
            yield
            return
        try:
            from openai.resources.chat.completions import Completions

            stack.enter_context(mock.patch.object(Completions, "create", sdk_blocked))
        except ImportError:  # pragma: no cover - SDK always installed in the image
            pass
        try:
            from google.genai.models import Models

            stack.enter_context(mock.patch.object(Models, "generate_content", sdk_blocked))
        except ImportError:  # pragma: no cover
            pass
        yield


LIVE_FLAG = "--live"
# Project inference policy: live model inference goes through OpenRouter only.
LIVE_PROVIDERS = {"openrouter": "openrouter.ai"}
CONFIRM_FLAG = "--confirm-live-provider-calls"


def check_live_gate(*, live: bool, confirmed: bool, provider: str | None, model: str | None) -> None:
    """Both flags plus an explicit provider/model are required for live mode."""
    if not live:
        return
    missing = []
    if not confirmed:
        missing.append(CONFIRM_FLAG)
    if not provider:
        missing.append("--provider")
    if not model:
        missing.append("--model")
    if provider and provider not in LIVE_PROVIDERS:
        raise SystemExit(
            f"live mode refused: provider {provider!r} not allowed "
            f"(inference policy: {', '.join(LIVE_PROVIDERS)} only)"
        )
    if missing:
        raise SystemExit(
            f"live mode refused: {LIVE_FLAG} also requires {', '.join(missing)}"
        )
