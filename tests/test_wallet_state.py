#!/usr/bin/env python3
"""wallet_state — consolidated on-chain wallet state (Phase 2).

Run:  python3 tests/test_wallet_state.py

snapshot mode is native (reads crime-desk/config + live RPC) — exercised on a
tracked token (LAB) and an untracked one. audit mode (delegated safe_audit, slow
RPC) is not run live here; only its registry wiring is checked.
SPEC-123: the live-RPC snapshot tests are gated behind CRIMEDESK_LIVE_TESTS=1, skipped by
default; test_registry_modes (offline, reads capabilities.json only) stays in TestRegistry.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WS = ROOT / "capabilities" / "wallet_state.py"
ORCH = ROOT / "orchestrator.py"
WALLET_KEYS = {"label", "address", "chain", "tier", "nonce", "native_balance", "fired", "rpc_ok"}

sys.path.insert(0, str(ROOT / "tests"))
from live_gate import LIVE, SKIP_REASON  # noqa: E402

_spec = importlib.util.spec_from_file_location("wallet_state_cap", WS)
WSC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(WSC)


def run(*flags):
    return subprocess.run([sys.executable, str(WS), *flags],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=120)


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestWalletState(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = json.loads(run("LAB", "--json").stdout)

    def test_tracked_snapshot_shape(self):
        s = self.snap
        self.assertEqual(s["ticker"], "LAB")
        self.assertTrue(s["tracked"])
        for k in ("n_wallets", "fired_count", "dormant_count", "primed_unfired_count", "wallets"):
            self.assertIn(k, s)
        self.assertGreater(s["n_wallets"], 0)

    def test_wallet_contract(self):
        for w in self.snap["wallets"]:
            self.assertTrue(WALLET_KEYS.issubset(w), w)
            self.assertIsInstance(w["fired"], bool)
            if w["rpc_ok"]:
                self.assertIsInstance(w["nonce"], int)
                self.assertEqual(w["fired"], w["nonce"] > 0)

    def test_counts_consistent(self):
        s = self.snap
        ok = [w for w in s["wallets"] if w["rpc_ok"]]
        self.assertEqual(s["fired_count"], sum(1 for w in ok if w["fired"]))

    def test_untracked(self):
        s = json.loads(run("ZZZQQ", "--json").stdout)
        self.assertFalse(s["tracked"])
        self.assertEqual(s["n_wallets"], 0)

    def test_human_non_json(self):
        out = run("LAB", "--no-color").stdout
        self.assertIn("WALLET SNAPSHOT", out)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)

    def test_orchestrator_envelope(self):
        proc = subprocess.run([sys.executable, str(ORCH), "wallet_state", '{"ticker":"LAB"}'],
                              capture_output=True, text=True, cwd=str(ROOT), timeout=120)
        env = json.loads(proc.stdout)
        self.assertTrue(env["ok"], env)
        self.assertEqual(env["data"]["ticker"], "LAB")


class TestBuildSnapshotPoolFallback(unittest.TestCase):
    """SPEC-145 req 3: build_snapshot reads through a pool with fallback (reusing
    onchain._rpc_pool/_rpc_at's shape via injectable seams — not a third RPC
    implementation) and distinguishes 'read: nonce=N' from 'unreadable: reason'. Fully
    offline: WALLETS points at a temp config, rpc_pool_fn/rpc_at_fn are fakes."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.wallets_path = Path(self.dir.name) / "tracked_wallets.json"
        self._orig_wallets = WSC.WALLETS
        WSC.WALLETS = self.wallets_path

    def tearDown(self):
        WSC.WALLETS = self._orig_wallets
        self.dir.cleanup()

    def _write_cfg(self, wallets):
        self.wallets_path.write_text(json.dumps({"tokens": {"TST": {"wallets": wallets}}}))

    def test_pool_fallback_when_primary_fails(self):
        self._write_cfg([{"label": "SAFE", "address": "0xabc",
                          "chain": "binance-smart-chain", "tier": "distribution"}])

        def rpc_at_fn(url, method, params, timeout=12):
            if url == "bad-url":
                return None   # primary provider down
            return "0x5" if method == "eth_getTransactionCount" else "0x0"

        snap = WSC.build_snapshot("TST", rpc_pool_fn=lambda c: ["bad-url", "good-url"],
                                  rpc_at_fn=rpc_at_fn)
        w = snap["wallets"][0]
        self.assertTrue(w["rpc_ok"])
        self.assertEqual(w["nonce"], 5)
        self.assertIsNone(w["reason"])
        self.assertEqual(snap["wallets_unreadable"], 0)
        self.assertEqual(snap["wallets_read"], 1)
        self.assertEqual(snap["unreadable"], [])

    def test_all_providers_fail_marks_unreadable_with_reason(self):
        self._write_cfg([{"label": "SAFE", "address": "0xabc",
                          "chain": "binance-smart-chain", "tier": "distribution"}])
        snap = WSC.build_snapshot("TST", rpc_pool_fn=lambda c: ["u1", "u2"],
                                  rpc_at_fn=lambda *a, **k: None)
        w = snap["wallets"][0]
        self.assertFalse(w["rpc_ok"])
        self.assertIsNone(w["nonce"])
        self.assertIn("2", w["reason"])
        self.assertEqual(snap["wallets_total"], 1)
        self.assertEqual(snap["wallets_read"], 0)
        self.assertEqual(snap["wallets_unreadable"], 1)
        self.assertEqual(snap["unreadable"], [{"address": "0xabc", "chain": "binance-smart-chain",
                                               "reason": w["reason"]}])

    def test_no_pool_configured_for_chain(self):
        self._write_cfg([{"label": "SAFE", "address": "0xabc",
                          "chain": "unknownchain", "tier": "distribution"}])
        snap = WSC.build_snapshot("TST", rpc_pool_fn=lambda c: [], rpc_at_fn=lambda *a, **k: "0x1")
        w = snap["wallets"][0]
        self.assertFalse(w["rpc_ok"])
        self.assertIn("no RPC", w["reason"])

    def test_mixed_readable_and_unreadable(self):
        self._write_cfg([
            {"label": "A", "address": "0xa", "chain": "binance-smart-chain", "tier": "op"},
            {"label": "B", "address": "0xb", "chain": "binance-smart-chain", "tier": "op"},
        ])

        def rpc_at_fn(url, method, params, timeout=12):
            addr = params[0]
            if addr == "0xb":
                return None
            return "0x3" if method == "eth_getTransactionCount" else "0x0"

        snap = WSC.build_snapshot("TST", rpc_pool_fn=lambda c: ["only-url"], rpc_at_fn=rpc_at_fn)
        self.assertEqual(snap["wallets_read"], 1)
        self.assertEqual(snap["wallets_unreadable"], 1)
        self.assertEqual(snap["unreadable"][0]["address"], "0xb")

    def test_untracked_reports_zero_coverage(self):
        self._write_cfg([])
        self.wallets_path.write_text(json.dumps({"tokens": {}}))
        snap = WSC.build_snapshot("NOPE", rpc_pool_fn=lambda c: [], rpc_at_fn=lambda *a, **k: None)
        self.assertFalse(snap["tracked"])
        self.assertEqual(snap["wallets_total"], 0)
        self.assertEqual(snap["wallets_unreadable"], 0)
        self.assertEqual(snap["unreadable"], [])


class TestRegistry(unittest.TestCase):
    def test_registry_modes(self):
        caps = json.loads((ROOT / "capabilities.json").read_text())
        self.assertIn("mode", caps["wallet_state"]["invoke"])
        self.assertEqual(caps["wallet_state"]["status"], "native")


if __name__ == "__main__":
    unittest.main(verbosity=2)
