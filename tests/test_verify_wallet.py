#!/usr/bin/env python3
"""SPEC 14 — verify_wallet: ad-hoc address lookup, §8 seeded-staging detection.

Run:  python3 tests/test_verify_wallet.py

Moralis is monkeypatched (offline). Uses a REAL tracked ESPORTS distribution wallet
from config as the seeding source so the tracked-safe match is exercised against the
live config, deterministically.
"""
import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("verify_wallet", ROOT / "capabilities" / "verify_wallet.py")
VW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(VW)

ADDR = "0xbb58b69b686149627e4d205d9493b59ad98ef275"   # arbitrary (not a tracked wallet)
NOW = datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _tracked_distribution_addr():
    tok = json.loads((ROOT / "config" / "tracked_wallets.json").read_text())["tokens"]["ESPORTS"]
    for w in tok["wallets"]:
        if (w.get("tier") or "").lower() == "distribution":
            return w["address"].lower()
    return None


def tx(frm, to, val, dt):
    return {"from_address": frm, "to_address": to, "value_decimal": str(val),
            "block_timestamp": _iso(dt), "token_symbol": "ESPORTS"}


class _Patch(unittest.TestCase):
    def patch(self, txs, price=0.05):
        self._orig = (VW.token_transfers, VW._live_price, VW._quote_transfers, VW.balance_of, VW.probe_contract)
        VW._quote_transfers = lambda *a, **k: {}
        VW.token_transfers = lambda address, contract, chain_key, days=180, decimals=18: (txs, "moralis", False)
        VW._live_price = lambda token: (price, "bybit")   # live price, offline-stubbed
        VW.balance_of = lambda *a, **k: {"available": False, "reason": "stubbed offline"}   # SPEC 58: no live RPC
        # SPEC-67: probe offline-stubbed to "EOA" by default (a tracked-safe seed stays a seed)
        VW.probe_contract = lambda *a, **k: {"available": True, "is_contract": False, "is_pool": False}

    def tearDown(self):
        if hasattr(self, "_orig"):
            VW.token_transfers, VW._live_price, VW._quote_transfers, VW.balance_of, VW.probe_contract = self._orig


class TestVerifyWallet(_Patch):
    def test_seeded_by_tracked_safe(self):
        safe = _tracked_distribution_addr()
        self.assertIsNotNone(safe, "need a distribution-tier ESPORTS wallet in config")
        self.patch([tx(safe, ADDR, 1000, NOW - timedelta(days=2)),
                    tx(ADDR, "0xdeadbeef", 100, NOW - timedelta(days=1))])
        v = VW.build_verify(ADDR, "ESPORTS")
        self.assertTrue(v["available"])
        self.assertEqual(v["verdict"], "SEEDED-STAGING")
        self.assertTrue(v["seeded_staging"])
        self.assertEqual(v["funded_by"][0]["kind"], "tracked-safe")

    def test_independent_holder(self):
        self.patch([tx("0xc0ffee0000000000000000000000000000000001", ADDR, 5000, NOW - timedelta(days=30))])
        v = VW.build_verify(ADDR, "ESPORTS")
        self.assertEqual(v["verdict"], "INDEPENDENT-HOLDER")   # held, not seeded, no recent out
        self.assertFalse(v["seeded_staging"])
        self.assertGreater(v["net_flow_window"], 0)

    def test_distributing(self):
        self.patch([tx("0xc0ffee0000000000000000000000000000000001", ADDR, 5000, NOW - timedelta(days=20)),
                    tx(ADDR, "0xrouter", 1000, NOW - timedelta(days=1))])
        v = VW.build_verify(ADDR, "ESPORTS")
        self.assertEqual(v["verdict"], "DISTRIBUTING")
        self.assertTrue(v["distributing"])

    def test_dormant(self):
        self.patch([tx("0xc0ffee0000000000000000000000000000000001", ADDR, 1000, NOW - timedelta(days=200)),
                    tx(ADDR, "0xsink", 1000, NOW - timedelta(days=180))])
        v = VW.build_verify(ADDR, "ESPORTS")
        self.assertEqual(v["net_flow_window"], 0)
        self.assertEqual(v["verdict"], "DORMANT")

    def test_balance_usd_uses_live_price_not_stale_config(self):
        # SPEC 14 bug fix: USD must come from the LIVE price, not the stale config price_usd.
        self.patch([tx("0xc0ffee0000000000000000000000000000000001", ADDR, 1000, NOW - timedelta(days=5))],
                   price=0.05)
        v = VW.build_verify(ADDR, "ESPORTS")
        self.assertEqual(v["price_usd"], 0.05)
        self.assertEqual(v["price_source"], "bybit")
        self.assertAlmostEqual(v["net_flow_window_usd"], 1000 * 0.05, places=2)   # 50, not 1000×0.6576

    def test_usd_none_when_no_live_price(self):
        self.patch([tx("0xc0ffee0000000000000000000000000000000001", ADDR, 1000, NOW - timedelta(days=5))],
                   price=None)
        v = VW.build_verify(ADDR, "ESPORTS")
        self.assertIsNone(v["net_flow_window_usd"])            # no stale-config fallback

    def test_fallback_source_surfaced(self):
        # SPEC 17: when Moralis is down and getLogs serves, surface source+partial
        self._orig = (VW.token_transfers, VW._live_price, VW._quote_transfers, VW.balance_of, VW.probe_contract)
        VW._quote_transfers = lambda *a, **k: {}
        VW.token_transfers = lambda address, contract, chain_key, days=180, decimals=18: (
            [tx("0xc0ffee0000000000000000000000000000000001", ADDR, 1000, NOW)], "getlogs", True)
        VW._live_price = lambda t: (0.05, "bybit")
        VW.balance_of = lambda *a, **k: {"available": False, "reason": "stubbed offline"}
        v = VW.build_verify(ADDR, "ESPORTS")
        self.assertTrue(v["available"])
        self.assertEqual(v["source"], "getlogs")
        self.assertTrue(v["partial"])

    def test_arbitrary_address_works(self):
        # an address in no tracked map still resolves (the whole point of §8 ad-hoc lookup)
        self.patch([tx("0xc0ffee0000000000000000000000000000000001", ADDR, 10, NOW)])
        v = VW.build_verify(ADDR, "ESPORTS")
        self.assertTrue(v["available"])

    def test_untracked_token_unavailable(self):
        self.patch([])
        v = VW.build_verify(ADDR, "NOPECOIN")
        self.assertFalse(v["available"])
        self.assertIn("not in tracked config", v["reason"])

    def test_cex_sourced_funding_categorized(self):
        # SPEC 15 refinement: a wallet funded by a CEX hot wallet must be tagged
        # `cex-withdrawal` (named), NOT dropped to "unknown" — it's a distinct category.
        # GATE is a REAL Gate.io withdrawal addr in config/known_entities.json.
        GATE = "0x0d0707963952f2fba59dd06f2b425ace40b492fe"
        self.patch([tx(GATE, ADDR, 9_810_000, NOW - timedelta(hours=8)),
                    tx(ADDR, "0xrouter00000000000000000000000000000000aa", 5_000_000, NOW - timedelta(hours=4))])
        v = VW.build_verify(ADDR, "ESPORTS")
        self.assertEqual(v["funded_by"][0]["kind"], "cex")          # not "unknown"
        self.assertEqual(v["funded_by_kind"], "cex-withdrawal")
        self.assertTrue(v["cex_sourced"])
        self.assertIsNotNone(v["funded_by_cex"])
        self.assertIn("gate", v["funded_by_cex"].lower())
        # CEX-withdrawal → fresh wallet → DEX dump = a distribution pattern (operator
        # sourcing supply off-exchange to obscure lineage), distinct from seeded-staging.
        self.assertTrue(v["distributing"])
        self.assertTrue(v["cex_sourced_distribution"])
        self.assertFalse(v["seeded_staging"])

    def test_funded_by_kind_seeded_takes_priority(self):
        safe = _tracked_distribution_addr()
        GATE = "0x0d0707963952f2fba59dd06f2b425ace40b492fe"
        self.patch([tx(safe, ADDR, 1000, NOW - timedelta(days=2)),
                    tx(GATE, ADDR, 50, NOW - timedelta(days=2))])
        v = VW.build_verify(ADDR, "ESPORTS")
        self.assertEqual(v["funded_by_kind"], "seeded")            # tracked-safe seed dominates
        self.assertTrue(v["seeded_staging"])

    def test_sell_destinations_exposed_for_clustering(self):
        # SPEC 15 refinement: expose aggregated sell destinations (hubs) so the radar can
        # cluster wallets feeding the SAME downstream address as one operator.
        HUB = "0x5bb59bb9371cbec158ed602d5f3cf1ad1c9b4462"
        self.patch([tx("0xc0ffee0000000000000000000000000000000001", ADDR, 5000, NOW - timedelta(days=10)),
                    tx(ADDR, HUB, 1000, NOW - timedelta(days=2)),
                    tx(ADDR, HUB, 800, NOW - timedelta(days=1))])
        v = VW.build_verify(ADDR, "ESPORTS")
        dests = {d["address"] for d in v["sell_destinations"]}
        self.assertIn(HUB, dests)
        hub = next(d for d in v["sell_destinations"] if d["address"] == HUB)
        self.assertEqual(hub["count"], 2)
        self.assertAlmostEqual(hub["amount"], 1800, places=2)

    def test_quota_exhausted_degrades_with_named_reason_not_generic(self):
        # SPEC-123: a Moralis 401 must surface as QUOTA_EXHAUSTED in the reason field, not a
        # bare "timeout"/generic degrade — the 2026-07-14 incident this spec exists to prevent.
        self._orig = (VW.token_transfers, VW._live_price, VW._quote_transfers, VW.balance_of, VW.probe_contract)
        VW._quote_transfers = lambda *a, **k: {}

        def boom(*a, **k):
            raise VW.MoralisQuotaExhausted(
                "QUOTA_EXHAUSTED (moralis, resets 00:00 UTC): moralis err 401")
        VW.token_transfers = boom
        VW._live_price = lambda t: (0.05, "bybit")
        VW.balance_of = lambda *a, **k: {"available": False, "reason": "stubbed offline"}
        v = VW.build_verify(ADDR, "ESPORTS")
        self.assertFalse(v["available"])
        self.assertTrue(v["degraded"])
        self.assertIn("QUOTA_EXHAUSTED", v["reason"])

    def test_provider_error_degrades_explicitly(self):
        # ALL providers fail → available:false + degraded:true, NOT null/zero fields (SPEC-1b/17)
        self._orig = (VW.token_transfers, VW._live_price, VW._quote_transfers, VW.balance_of, VW.probe_contract)
        VW._quote_transfers = lambda *a, **k: {}
        def boom(*a, **k):
            raise VW.MoralisError("all providers failed")
        VW.token_transfers = boom
        VW._live_price = lambda t: (0.05, "bybit")
        VW.balance_of = lambda *a, **k: {"available": False, "reason": "stubbed offline"}
        v = VW.build_verify(ADDR, "ESPORTS")
        self.assertFalse(v["available"])
        self.assertTrue(v["degraded"])
        self.assertNotIn("net_flow_window", v)        # no fabricated 0 — caller can't misread as "0 sells"
        self.assertNotIn("verdict", v)


if __name__ == "__main__":
    unittest.main(verbosity=2)
