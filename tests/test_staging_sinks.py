#!/usr/bin/env python3
"""SPEC 28 — staging-sink auto-tracking + destination classification (cex-execution vs staging-internal).

Run:  python3 tests/test_staging_sinks.py

The desk's edge is catching the bid-pull at the staged-wallet nonce — but the staging SINKS (where
the dump originates) were untracked, and the board couldn't tell CONSOLIDATION (tokens → operator-
controlled sink = loading) from EXECUTION (tokens → a CEX = the dump firing). Network mocked.
"""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


OC = _load("onchain")
NOW = 1780600000.0
FRESH = datetime.fromtimestamp(NOW - 3600, tz=timezone.utc).isoformat()

CEX = "0xf977814e90da44bfa03b6295a0616a897441acec"   # a CEX hot wallet (entity-labeled)
SINK = "0x26209d9f0dc3ac0129c3fb1badabfeb9ee728c66"  # an operator-controlled internal sink
DISTRIB = "0x1ab4973a48dc892cd9971ece8e01dcc7688f8f23"


class TestClassifyDestination(unittest.TestCase):
    def test_cex_entity_is_execution(self):
        self.assertEqual(OC.classify_destination("cex", None), "cex-execution")

    def test_cex_tier_tracked_wallet_is_execution(self):
        self.assertEqual(OC.classify_destination("tracked-safe", "cex"), "cex-execution")

    def test_operator_tracked_sink_is_staging(self):
        self.assertEqual(OC.classify_destination("tracked-safe", "distribution"), "staging-internal")

    def test_dex_is_execution_and_unknown_is_unknown(self):
        self.assertEqual(OC.classify_destination("dex", None), "dex-execution")
        self.assertEqual(OC.classify_destination("unknown", None), "unknown")


class TestEscalationExecutionVsStaging(unittest.TestCase):
    """SPEC 24+28: a confirmed escalation routing to a CEX = EXECUTION; to an internal sink = STAGING."""
    def setUp(self):
        self._orig = (OC.build_snapshot, OC._load_baseline, OC._save_baseline, OC.token_transfers,
                      OC._entity_labels)
        OC._save_baseline = lambda *a, **k: None
        OC._entity_labels = lambda: {CEX: "Binance hot wallet"}

    def tearDown(self):
        (OC.build_snapshot, OC._load_baseline, OC._save_baseline, OC.token_transfers,
         OC._entity_labels) = self._orig

    def _run(self, dest):
        w = {"address": DISTRIB, "label": "AGGREGATOR", "tier": "team", "nonce": 11_000_244,
             "rpc_ok": True, "chain": "binance-smart-chain"}
        OC.build_snapshot = lambda ticker: {"tracked": True, "wallets": [w], "fired_count": 1,
                                            "primed_unfired_count": 0, "dormant_count": 0, "n_wallets": 1}
        OC._load_baseline = lambda ticker: {"ts": NOW - 100, "seeded_ts": NOW - 1000,
                                            "nonces": {DISTRIB: 11_000_000}}
        OC.token_transfers = lambda a, c, ck, days=7, decimals=18: (
            [{"from_address": DISTRIB, "to_address": dest, "block_timestamp": FRESH, "value_decimal": "1"}],
            "moralis", False)
        return OC.build_nonce_state("BILL", ts=NOW)

    def test_cex_bound_out_is_execution_escalation(self):
        r = self._run(CEX)
        self.assertEqual(r["signal"], "ESCALATION")
        self.assertEqual(r["escalation_kind"], "execution")          # the dump firing
        self.assertEqual(len(r["execution_escalation"]), 1)
        self.assertEqual(r["escalation_fired"][0]["dest_kind"], "cex-execution")

    def test_internal_sink_out_is_staging_escalation(self):
        # the destination is itself a tracked operator wallet (BILL's AGGREGATOR) → internal
        r = self._run(DISTRIB)
        self.assertEqual(r["escalation_kind"], "staging")            # apparatus loading, NOT the dump
        self.assertEqual(len(r["staging_escalation"]), 1)
        self.assertEqual(r["escalation_fired"][0]["dest_kind"], "staging-internal")


class TestQualifyAndOnboard(unittest.TestCase):
    def test_qualifies_seeded_accumulating(self):
        self.assertTrue(OC._qualifies_as_staging_sink(
            {"available": True, "seeded_staging": True, "out_count": 0, "distribution_mode": None}))
        self.assertTrue(OC._qualifies_as_staging_sink(
            {"available": True, "seeded_staging": True, "out_count": 5, "distribution_mode": "staging-internal"}))

    def test_does_not_qualify_if_executing_to_cex(self):
        self.assertFalse(OC._qualifies_as_staging_sink(
            {"available": True, "seeded_staging": True, "out_count": 40, "distribution_mode": "cex-execution"}))
        self.assertFalse(OC._qualifies_as_staging_sink({"available": True, "seeded_staging": False}))

    def test_onboard_writes_dedup(self):
        cfg = {"tokens": {"BILL": {"contracts": {"binance-smart-chain": "0xabc"}, "wallets": []}}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            path = f.name
        # SPEC-67: inject an offline EOA probe so the lint stays deterministic (no live RPC)
        eoa = lambda *a, **k: {"available": True, "is_contract": False, "is_pool": False}
        added1 = OC.onboard_staging_sink("BILL", SINK, label="STAGING-SINK-TEST", wallets_path=path, probe=eoa)
        added2 = OC.onboard_staging_sink("BILL", SINK, label="STAGING-SINK-TEST", wallets_path=path, probe=eoa)
        d = json.loads(Path(path).read_text())
        Path(path).unlink()
        self.assertTrue(added1)
        self.assertFalse(added2)                                     # dedup
        w = d["tokens"]["BILL"]["wallets"]
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["subtag"], "staging-sink")
        self.assertEqual(w[0]["tier"], "distribution")


class TestSinksOnboardedInConfig(unittest.TestCase):
    def test_two_bill_sinks_tracked(self):
        bill = json.loads((ROOT / "config" / "tracked_wallets.json").read_text())["tokens"]["BILL"]
        addrs = {w["address"].lower() for w in bill["wallets"]}
        self.assertIn("0xdd276dc5223d0120f9bf1776f38957cc8da23cb0", addrs)
        self.assertIn("0x26209d9f0dc3ac0129c3fb1badabfeb9ee728c66", addrs)
        sinks = [w for w in bill["wallets"] if w.get("subtag") == "staging-sink"]
        self.assertGreaterEqual(len(sinks), 2)
        # SPEC 68: don't pin the live sink tiers (a re-tier would break the suite). That a freshly
        # onboarded sink enters nonce-watch as tier 'distribution' is the onboard_staging_sink
        # write-contract, covered by test_onboard_writes_dedup on a temp fixture config.


if __name__ == "__main__":
    unittest.main(verbosity=2)
