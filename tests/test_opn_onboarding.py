#!/usr/bin/env python3
"""SPEC 22 — OPN (Opinion) onboarded into the on-chain tracking config.

Run:  python3 tests/test_opn_onboarding.py

OPN is the live pump leg of the XTOKEN-MM cluster (SKYAI/BILL/BSB/EDEN/LAB). This
validates the config shape and that the capabilities now RESOLVE OPN (previously
`available:false / untracked`), with:
  - the 705M-locked vault contract tagged `bridge` (∉ SEED_TIERS → never read as
    operator distribution, the RAVE/SPEC-20 lesson),
  - the 35% Binance hot wallet tagged `cex` (∈ OP_TIERS → never fires a false
    dormant-safe escalation),
  - the cross-cluster operator wallets (aggregator 0x1ab4973a, router 0x238a…,
    feeder 0xf89d…) present = the operator-linkage confirmation,
  - the fresh distribution-suspect EOAs tier `distribution` with mapped labels so a
    dormant→nonce-advance flip fires the §8 rug-catch ESCALATION.
Network is mocked — offline-deterministic.
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
OPN = CFG.get("OPN", {})

LOCK = "0xb21748e2aa38b3ae5fba9ea40ee6a2dab1bdf8dc"     # 39.6% contract — tier bridge (lock vault)
BINANCE = "0xf977814e90da44bfa03b6295a0616a897441acec"  # 35% — Binance hot — tier cex
AGG = "0x1ab4973a48dc892cd9971ece8e01dcc7688f8f23"      # 6% — XTOKEN-MM aggregator (cluster linkage)
DIST = "0x43684d03d81d3a4c70da68febdd61029d426f042"     # 3.2% fresh EOA — tier distribution (suspect)
RECIP = "0xabc0000000000000000000000000000000000001"    # arbitrary recipient under test
NOW = "2026-06-04T12:00:00.000Z"


def tx(frm, to, val):
    return {"from_address": frm, "to_address": to, "value_decimal": str(val),
            "block_timestamp": NOW, "token_symbol": "OPN"}


class TestOpnConfig(unittest.TestCase):
    def test_opn_present_with_both_contracts(self):
        self.assertTrue(OPN, "OPN missing from tracked_wallets.json")
        self.assertEqual(set(OPN["contracts"]), {"ethereum", "binance-smart-chain"})
        # disambiguation: same address both chains (Opinion / prediction-market OPN)
        self.assertEqual(OPN["contracts"]["ethereum"].lower(),
                         OPN["contracts"]["binance-smart-chain"].lower())
        self.assertEqual(OPN["decimals"], 18)
        self.assertGreaterEqual(len(OPN["wallets"]), 9)

    def test_bsc_contract_is_moralis_and_goplus_covered(self):
        # concentration + verify default to BSC (the live trading chain) → must be on both maps
        self.assertIn("binance-smart-chain", OC._MORALIS_CHAIN)
        self.assertIn("binance-smart-chain", OC._GOPLUS_CHAIN)

    def test_lock_vault_and_cex_wallets_are_structurally_valid(self):
        # SPEC 68: don't pin LOCK/BINANCE live tiers (re-tier would break the suite); assert they
        # are onboarded + structurally valid. The behaviour that matters is the *system* invariant
        # below + the seeded-staging fixtures in TestSeedTierSemantics.
        for addr in (LOCK, BINANCE):
            OI.assert_wallet_structural(self, OI.find_wallet(OPN, addr))

    def test_bridge_excluded_from_seeding_and_cex_is_operational(self):
        # CRITICAL (RAVE/SPEC-20 lesson): a locked-supply vault (tier bridge) must NEVER be a
        # SEED_TIER, and CEX-held float (tier cex) must be operational noise (∈ OP_TIERS → never
        # fires a false §8 dormant-safe escalation). These are constants, not live-config tiers.
        self.assertNotIn("bridge", VW.SEED_TIERS)
        self.assertIn("cex", OC.OP_TIERS)

    def test_cluster_operator_wallets_present_linkage_confirmed(self):
        # the operator-linkage confirmation: cross-cluster wallets hold OPN → same XTOKEN-MM crew.
        tracked = {w["address"].lower() for w in OPN["wallets"]}
        self.assertIn(AGG, tracked)                                  # SKYAI/EDEN dump aggregator
        self.assertIn("0x238a358808379702088667322f80ac48bad5e6c4", tracked)  # MULTI-CAT-A-ROUTER
        OI.assert_wallet_structural(self, OI.find_wallet(OPN, AGG))


class TestSeedTierSemantics(unittest.TestCase):
    """SPEC 68: tier *semantics* (§8 rug-catch / seeded-staging) covered with INLINE FIXTURES so a
    re-tier of any live production wallet can't break them. No live-config tier is referenced."""

    # fixture wallets — synthetic, never re-tiered by the desk
    FIX_DIST = "0x" + "d1" * 20
    FIX_BRIDGE = "0x" + "b2" * 20
    FIX_RECIP = "0x" + "e3" * 20

    def setUp(self):
        self._orig = (VW.token_transfers, VW._live_price, VW.balance_of, VW.WALLETS)
        VW.balance_of = lambda *a, **k: {"available": False}
        VW._live_price = lambda token: (0.22, "bybit")
        self._cfg_path = OI.temp_token_config("FIX", [
            {"label": "FIX-DISTRIBUTION-SUSPECT", "address": self.FIX_DIST,
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

    def test_distribution_suspect_can_fire_the_rugcatch(self):
        # a distribution-tier EOA with a MAPPED (non-UNMAPPED) label → _confidence == 'high' on a
        # nonce advance → ESCALATION; the distribution tier is a seed tier and NOT operational noise.
        self.assertIn("distribution", VW.SEED_TIERS)
        self.assertNotIn("distribution", OC.OP_TIERS)
        self.assertEqual(OC._confidence("OPN-DISTRIBUTION-SUSPECT-1"), "high")
        self.assertNotEqual(OC._confidence("UNMAPPED-FEEDER"), "high")

    def test_funded_by_distribution_eoa_is_seeded_staging(self):
        VW.token_transfers = lambda a, c, ck, days=180, decimals=18: ([self._tx(self.FIX_DIST)], "moralis", False)
        v = VW.build_verify(self.FIX_RECIP, "FIX")
        self.assertTrue(v["seeded_staging"])                  # distribution tier IS a seed tier

    def test_funded_by_lock_vault_is_not_seeded_staging(self):
        # receiving from the bridge/lock vault is NOT operator seeding (bridge ∉ SEED_TIERS)
        VW.token_transfers = lambda a, c, ck, days=180, decimals=18: ([self._tx(self.FIX_BRIDGE)], "moralis", False)
        v = VW.build_verify(self.FIX_RECIP, "FIX")
        self.assertFalse(v["seeded_staging"])
        self.assertEqual(v["funded_by"][0]["kind"], "tracked-safe")   # still recognized as tracked


class TestOpnResolves(unittest.TestCase):
    """OPN must now resolve in verify_wallet (was 'not in tracked config')."""
    def setUp(self):
        self._orig = (VW.token_transfers, VW._live_price, VW.balance_of)
        VW.balance_of = lambda *a, **k: {"available": False}   # SPEC 58: no live RPC in tests
        VW._live_price = lambda token: (0.22, "bybit")

    def tearDown(self):
        VW.token_transfers, VW._live_price, VW.balance_of = self._orig

    def test_verify_wallet_resolves_opn_on_bsc(self):
        VW.token_transfers = lambda a, c, ck, days=180, decimals=18: ([tx(DIST, RECIP, 1000)], "moralis", False)
        v = VW.build_verify(RECIP, "OPN")
        self.assertTrue(v["available"])                          # NOT {available:false, not in tracked config}
        # SPEC 21 multi-chain: OPN is deployed on eth+bsc (same address) so BOTH are queried.
        # Assert BSC (the live trading chain) is covered — the tie-broken `primary` chain is a
        # mock artifact (both chains given identical activity); reality has the volume on BSC.
        self.assertIn("binance-smart-chain", v["chains_queried"])


class TestOpnConcentration(unittest.TestCase):
    def test_concentration_uses_bsc_contract(self):
        bsc = OPN["contracts"]["binance-smart-chain"].lower()
        self._orig = OC._get_json
        OC._get_json = lambda url, **k: {"result": {bsc: {"holder_count": "45138", "holders": [
            {"address": LOCK, "percent": "0.396", "is_contract": "1", "is_locked": "0"},
            {"address": BINANCE, "percent": "0.35", "is_contract": "0", "is_locked": "0"}]}}}
        try:
            c = OC._concentration("OPN")
        finally:
            OC._get_json = self._orig
        self.assertTrue(c["available"])
        self.assertEqual(c["chain"], "binance-smart-chain")
        self.assertEqual(c["holder_count"], 45138)
        self.assertIsNotNone(c["top1_pct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
