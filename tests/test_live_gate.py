#!/usr/bin/env python3
"""SPEC-123 — live_gate: the shared live-test switch + the network sentinel behind it.

Run:  python3 tests/test_live_gate.py

Fully offline — every probe here talks to loopback (or nothing at all, since the netblock
raises BEFORE any socket I/O for a non-local host) so this file itself makes zero real
network calls, live flag or not.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# a non-routable, documentation-reserved address (RFC 5737 TEST-NET-3) — the guard raises
# on the hostname check BEFORE attempting any real connect, so this never touches the network.
_NON_LOCAL = "203.0.113.1"


def _run_probe(code, env_extra):
    env = dict(os.environ)
    env.pop("CRIMEDESK_LIVE_TESTS", None)
    env.update(env_extra)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(ROOT), timeout=15, env=env)


_FLAG_PROBE = "import sys; sys.path.insert(0, 'tests'); import live_gate; print(live_gate.LIVE)"
_BLOCK_INSTALLED_PROBE = (
    "import sys; sys.path.insert(0, 'tests'); import live_gate, socket; "
    "print(bool(getattr(socket, '_crimedesk_netblocked', False)))"
)
_CONNECT_NON_LOCAL_PROBE = (
    "import sys; sys.path.insert(0, 'tests'); import live_gate, socket; "
    f"socket.socket().connect(('{_NON_LOCAL}', 80))"
)
_CONNECT_LOOPBACK_CLOSED_PROBE = (
    "import sys; sys.path.insert(0, 'tests'); import live_gate, socket; "
    "socket.socket().connect(('127.0.0.1', 65530))"
)


class TestLiveGateFlag(unittest.TestCase):
    def test_default_unset_is_not_live(self):
        out = _run_probe(_FLAG_PROBE, {})
        self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def test_flag_1_is_live(self):
        out = _run_probe(_FLAG_PROBE, {"CRIMEDESK_LIVE_TESTS": "1"})
        self.assertEqual(out.stdout.strip(), "True", out.stderr)


class TestNetblockInstallation(unittest.TestCase):
    def test_block_installed_when_flag_unset(self):
        out = _run_probe(_BLOCK_INSTALLED_PROBE, {})
        self.assertEqual(out.stdout.strip(), "True", out.stderr)

    def test_block_not_installed_when_flag_set(self):
        out = _run_probe(_BLOCK_INSTALLED_PROBE, {"CRIMEDESK_LIVE_TESTS": "1"})
        self.assertEqual(out.stdout.strip(), "False", out.stderr)


class TestNetblockBehavior(unittest.TestCase):
    def test_non_local_host_blocked_by_default(self):
        proc = _run_probe(_CONNECT_NON_LOCAL_PROBE, {})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("LiveNetworkBlocked", proc.stderr)

    def test_loopback_passes_through_the_guard(self):
        # nothing listens on 65530 — refused, but that's the OS, not our sentinel: proves
        # local traffic is let through rather than swallowed by the block.
        proc = _run_probe(_CONNECT_LOOPBACK_CLOSED_PROBE, {})
        self.assertNotIn("LiveNetworkBlocked", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
