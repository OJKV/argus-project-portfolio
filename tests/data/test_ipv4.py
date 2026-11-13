"""Egress must be IPv4 when the exchange key is bound to an IPv4 address (F-060).

Measured on the VPS: the box prefers IPv6, so OKX saw its IPv6 address while the API key
allowlist holds its IPv4 one. Every authenticated call would have failed with 50110.
"""

from __future__ import annotations

import socket

import pytest

from argus.config import Settings
from argus.data import http


def test_ipv4_is_forced_by_default() -> None:
    assert Settings().force_ipv4 is True


def test_the_http_client_binds_to_an_ipv4_local_address() -> None:
    transport = http.build_transport(force_ipv4=True)
    assert transport is not None
    pool = transport._pool  # type: ignore[attr-defined]
    assert pool._local_address == "0.0.0.0"


def test_ipv6_is_left_alone_when_the_setting_is_off() -> None:
    assert http.build_transport(force_ipv4=False) is None


def test_the_ccxt_session_is_pinned_to_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib3.util.connection as urllib3_conn

    monkeypatch.setattr(urllib3_conn, "allowed_gai_family", lambda: socket.AF_UNSPEC)
    from argus.exchange.okx import force_ipv4_for_ccxt

    force_ipv4_for_ccxt()
    assert urllib3_conn.allowed_gai_family() == socket.AF_INET


def test_pinning_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib3.util.connection as urllib3_conn

    monkeypatch.setattr(urllib3_conn, "allowed_gai_family", lambda: socket.AF_UNSPEC)
    from argus.exchange.okx import force_ipv4_for_ccxt

    force_ipv4_for_ccxt()
    force_ipv4_for_ccxt()
    assert urllib3_conn.allowed_gai_family() == socket.AF_INET
