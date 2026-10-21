"""代理地址校验：只允许显式信任的虚拟 DNS 段，不放开普通内网。"""

from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from coder.sandbox.base import SandboxError
from coder.sandbox.network_proxy import ProxyHandler
from coder.sandbox.policy import SandboxPolicy


@pytest.mark.parametrize(
    ("resolved", "trust_fake_ip_dns", "accepted"),
    [
        ("198.18.0.63", False, False),
        ("198.18.0.63", True, True),
        ("192.168.1.10", True, False),
        ("127.0.0.1", True, False),
        ("169.254.1.2", True, False),
        ("8.8.8.8", False, True),
    ],
)
def test_proxy_restricts_fake_ip_exception(
    monkeypatch: pytest.MonkeyPatch, resolved: str,
    trust_fake_ip_dns: bool, accepted: bool,
) -> None:
    handler = object.__new__(ProxyHandler)
    setattr(handler, "server", SimpleNamespace(
        allowed=lambda host: host == "registry.npmjs.org",
        trust_fake_ip_dns=trust_fake_ip_dns,
    ))
    monkeypatch.setattr(
        "coder.sandbox.network_proxy.socket.getaddrinfo",
        lambda host, port, *, type: [(socket.AF_INET, type, 6, "", (resolved, port))],
    )
    connected: list[tuple[str, int]] = []

    class FakeSocket:
        def __init__(self, family: int, kind: int) -> None:
            assert family == socket.AF_INET and kind == socket.SOCK_STREAM

        def settimeout(self, timeout: int) -> None:
            assert timeout == 15

        def connect(self, address: tuple[str, int]) -> None:
            connected.append(address)

    monkeypatch.setattr("coder.sandbox.network_proxy.socket.socket", FakeSocket)
    if accepted:
        result = handler._connect("registry.npmjs.org", 443)
        assert isinstance(result, FakeSocket)
        assert connected == [(resolved, 443)]
    else:
        with pytest.raises(ValueError, match="non-public address"):
            handler._connect("registry.npmjs.org", 443)
        assert connected == []


def test_fake_ip_policy_requires_allowlist(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="requires network=allowlist"):
        SandboxPolicy(state_dir=tmp_path, trust_fake_ip_dns=True)
