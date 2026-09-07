#!/usr/bin/env python3
"""SPEC-123 — the single switch for every test that reaches a live external API
(Moralis, Etherscan, CoinGecko, venue REST, RPC — anything off-machine).

Unset (the default — ops/premerge.sh and the coder TDD loop never set it): LIVE is False,
and importing this module blocks outbound sockets process-wide so a test that forgets to
check LIVE fails loudly (LiveNetworkBlocked) instead of silently spending real Moralis/
Etherscan/venue quota — the 2026-07-14 incident (premerge ran 3x + a coder TDD loop and by
20:50 UTC Moralis returned a 401 plan-consumed error; the desk's on-chain layer went blind
for the rest of the day) this spec exists to prevent.

Set CRIMEDESK_LIVE_TESTS=1 to opt into real calls for a local run.

Usage — gate a live TestCase/method:
    from live_gate import LIVE, SKIP_REASON
    @unittest.skipUnless(LIVE, SKIP_REASON)
    class TestLiveThing(unittest.TestCase): ...
"""
import os
import socket

LIVE = os.environ.get("CRIMEDESK_LIVE_TESTS") == "1"
SKIP_REASON = "live network test — set CRIMEDESK_LIVE_TESTS=1 to run"

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


class LiveNetworkBlocked(RuntimeError):
    """Raised when a test tries to reach the real network without CRIMEDESK_LIVE_TESTS=1."""


def _install_netblock():
    if getattr(socket, "_crimedesk_netblocked", False):
        return   # idempotent — multiple test files import this module
    orig_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, (tuple, list)) else address
        if isinstance(host, str) and host not in _LOCAL_HOSTS:
            raise LiveNetworkBlocked(
                f"outbound connection to {address!r} blocked (CRIMEDESK_LIVE_TESTS unset) — "
                f"gate this test behind tests.live_gate.LIVE, or set CRIMEDESK_LIVE_TESTS=1 "
                f"to run it live"
            )
        return orig_connect(self, address)

    socket.socket.connect = guarded_connect
    socket._crimedesk_netblocked = True


if not LIVE:
    _install_netblock()
