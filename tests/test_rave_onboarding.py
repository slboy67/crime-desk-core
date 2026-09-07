#!/usr/bin/env python3
"""SPEC 20 — RAVE (RaveDAO) onboarded into the on-chain tracking config.

Run:  python3 tests/test_rave_onboarding.py

Offline: validates the config shape and that the capabilities now RESOLVE RAVE
(previously `available:false / untracked`), with the critical bridge/lock vaults
tagged so they're never read as operator distribution. Network is mocked.
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import onboarding_invariants as OI  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


VW = _load("verify_wallet")
OC = _load("onchain")
CFG = OI.load_tokens()
RAVE = CFG.get("RAVE", {})

BRIDGE = "0x9831156f1a6e506fca41503590b42f07c2e80f54"      # ETH lock vault — tier bridge
DIST_EOA = "0xf07327d01857e62c31ce29bd8a7870e769406fa3"   # 27.6% BSC EOA — tier distribution
RECIP = "0xabc0000000000000000000000000000000000001"      # arbitrary recipient under test
NOW = "2026-06-03T12:00:00.000Z"


def tx(frm, to, val):
    return {"from_address": frm, "to_address": to, "value_decimal": str(val),
            "block_timestamp": NOW, "token_symbol": "RAVE"}


class TestRaveConfig(unittest.TestCase):
    def test_rave_present_with_three_contracts(self):
        self.assertTrue(RAVE, "RAVE missing from tracked_wallets.json")
        self.assertEqual(set(RAVE["contracts"]), {"ethereum", "binance-smart-chain", "base"})
        self.assertEqual(RAVE["decimals"], 18)
        self.assertGreaterEqual(len(RAVE["wallets"]), 6)

    def test_bsc_contract_is_moralis_and_goplus_covered(self):
        # concentration + verify default to BSC; it must be on both provider maps
        self.assertIn("binance-smart-chain", OC._MORALIS_CHAIN)
        self.assertIn("binance-smart-chain", OC._GOPLUS_CHAIN)

    def test_bridge_and_distribution_wallets_tracked_and_structurally_valid(self):
        # CRITICAL (SPEC 20): bridge/lock vaults must NOT be a SEED_TIER → never seed-discovered as
        # distributors. SPEC 68: don't pin the vault's live tier (a re-tier would break the suite);
        # assert the *system* invariant + that the key wallets are onboarded + structurally valid.
        self.assertNotIn("bridge", VW.SEED_TIERS)
        for addr in (BRIDGE, DIST_EOA):
            OI.assert_wallet_structural(self, OI.find_wallet(RAVE, addr))


class TestRaveResolves(unittest.TestCase):
    """RAVE must now resolve in verify_wallet (was 'not in tracked config')."""
    def setUp(self):
        self._orig = (VW.token_transfers, VW._live_price, VW.balance_of)
        VW.balance_of = lambda *a, **k: {"available": False}   # SPEC 58: no live RPC in tests
        VW._live_price = lambda token: (0.001, "bybit")

    def tearDown(self):
        VW.token_transfers, VW._live_price, VW.balance_of = self._orig

    def test_verify_wallet_resolves_rave(self):
        VW.token_transfers = lambda a, c, ck, days=180, decimals=18: ([tx(DIST_EOA, RECIP, 1000)], "moralis", False)
        v = VW.build_verify(RECIP, "RAVE")
        self.assertTrue(v["available"])                       # NOT {available:false, not in tracked config}
        # SPEC 21: RAVE is multi-chain (eth/bsc/base) → all deployed chains are queried.
        self.assertEqual(set(v["chains_queried"]), {"ethereum", "binance-smart-chain", "base"})


class TestSeedTierSemantics(unittest.TestCase):
    """SPEC 68: seeded-staging semantics via INLINE FIXTURES — a re-tier of any live RAVE wallet
    can't break these. No live-config tier is referenced."""

    FIX_DIST = "0x" + "d1" * 20
    FIX_BRIDGE = "0x" + "b2" * 20
    FIX_RECIP = "0x" + "e3" * 20

    def setUp(self):
        self._orig = (VW.token_transfers, VW._live_price, VW.balance_of, VW.WALLETS)
        VW.balance_of = lambda *a, **k: {"available": False}
        VW._live_price = lambda token: (0.001, "bybit")
        self._cfg_path = OI.temp_token_config("FIX", [
            {"label": "FIX-DISTRIBUTION-EOA", "address": self.FIX_DIST,
             "chain": "binance-smart-chain", "tier": "distribution"},
            {"label": "FIX-LOCK-VAULT", "address": self.FIX_BRIDGE,
             "chain": "binance-smart-chain", "tier": "bridge"},
        ])
        VW.WALLETS = Path(self._cfg_path)

    def tearDown(self):
        VW.token_transfers, VW._live_price, VW.balance_of, VW.WALLETS = self._orig
        os.unlink(self._cfg_path)

    def _tx(self, frm):
        return {"from_address": frm, "to_address": self.FIX_RECIP, "value_decimal": "5000",
                "block_timestamp": NOW, "token_symbol": "FIX"}

    def test_funded_by_bridge_is_not_seeded_staging(self):
        # receiving from a bridge/lock vault is NOT operator seeding (bridge ∉ SEED_TIERS)
        VW.token_transfers = lambda a, c, ck, days=180, decimals=18: ([self._tx(self.FIX_BRIDGE)], "moralis", False)
        v = VW.build_verify(self.FIX_RECIP, "FIX")
        self.assertFalse(v["seeded_staging"])
        self.assertEqual(v["funded_by"][0]["kind"], "tracked-safe")   # still recognized as tracked

    def test_funded_by_distribution_eoa_is_seeded_staging(self):
        VW.token_transfers = lambda a, c, ck, days=180, decimals=18: ([self._tx(self.FIX_DIST)], "moralis", False)
        v = VW.build_verify(self.FIX_RECIP, "FIX")
        self.assertTrue(v["seeded_staging"])                  # distribution tier IS a seed tier


class TestRaveConcentration(unittest.TestCase):
    def test_concentration_uses_bsc_contract(self):
        bsc = RAVE["contracts"]["binance-smart-chain"].lower()
        self._orig = OC._get_json
        OC._get_json = lambda url, **k: {"result": {bsc: {"holder_count": "31708", "holders": [
            {"address": "0x73d8bd54f7cf5fab43fe4ef40a62d390644946db", "percent": "0.27", "is_contract": "0", "is_locked": "0"},
            {"address": BRIDGE, "percent": "0.10", "is_contract": "1", "is_locked": "1"}]}}}
        try:
            c = OC._concentration("RAVE")
        finally:
            OC._get_json = self._orig
        self.assertTrue(c["available"])
        self.assertEqual(c["chain"], "binance-smart-chain")
        self.assertEqual(c["holder_count"], 31708)
        self.assertIsNotNone(c["top1_pct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
