#!/usr/bin/env python3
"""SPEC 32 — VELVET (Velvet / DeFAI) onboarded into the on-chain tracking config.

Run:  python3 tests/test_velvet_onboarding.py

Offline: validates the config shape + tiers and that the capabilities now RESOLVE VELVET
(previously `untracked`). The critical SPEC-20 lesson: the lock/bridge supply and the LP
pools must be tiered so they are NEVER seed-discovered as operator distribution; the team
multisigs (proven Gnosis Safes) ARE watched. Network is mocked. Mirrors test_rave_onboarding.
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
VELVET = CFG.get("VELVET", {})

BRIDGE = "0x6e0bad2c077d699841f1929b45bfb93fafbed395"          # 47.2% BSC lock/bridge — tier bridge
TEAM_A = "0x75e7488ac067f07948739bfb550213b47db094bb"          # 16% BSC Gnosis Safe — tier team
TEAM_B = "0xd19dce537125dfb5e76d7131668c4d5e4172b56c"          # 10.7% BSC Gnosis Safe — tier team
OPERATOR = "0xffa8db7b38579e6a2d14f9b347a9ace4d044cd54"        # cross-cluster SKYAI distributor — tier distribution
POOL_BASE = "0xdc9e387d65d036f0663e9b9ac4a6267f866075ea"       # 62% Base USDC LP — tier pool
RECIP = "0xabc0000000000000000000000000000000000099"
NOW = "2026-06-09T12:00:00.000Z"


def tx(frm, to, val):
    return {"from_address": frm, "to_address": to, "value_decimal": str(val),
            "block_timestamp": NOW, "token_symbol": "VELVET"}


class TestVelvetConfig(unittest.TestCase):
    def test_velvet_present_with_two_contracts_and_decimals(self):
        self.assertTrue(VELVET, "VELVET missing from tracked_wallets.json")
        self.assertEqual(set(VELVET["contracts"]), {"binance-smart-chain", "base"})
        self.assertEqual(VELVET["decimals"], 18)
        self.assertGreaterEqual(len(VELVET["wallets"]), 6)

    def test_bsc_is_default_concentration_chain_and_provider_covered(self):
        # concentration + verify default to BSC (binance-smart-chain present) — must be on both maps
        self.assertIn("binance-smart-chain", OC._MORALIS_CHAIN)
        self.assertIn("binance-smart-chain", OC._GOPLUS_CHAIN)

    def test_lock_pool_team_operator_wallets_are_structurally_valid(self):
        # SPEC 68: don't pin live tiers (a re-tier would break the suite); assert these key wallets
        # are onboarded + structurally valid. Tier *semantics* are the system invariants below +
        # the seeded-staging fixtures in TestSeedTierSemantics.
        OI.assert_unique_within_token(self, VELVET, "VELVET")
        for addr in (BRIDGE, POOL_BASE, TEAM_A, TEAM_B, OPERATOR):
            OI.assert_wallet_structural(self, OI.find_wallet(VELVET, addr))

    def test_seed_tier_system_invariants(self):
        # CRITICAL (SPEC 20/32): locked-supply (bridge) and LP pools (pool) must NEVER be seed
        # tiers → never seed-discovered as operator distribution; team multisigs and distributors
        # ARE seed tiers → watched. These are constants, independent of any live-config tier.
        self.assertNotIn("bridge", VW.SEED_TIERS)
        self.assertNotIn("pool", VW.SEED_TIERS)
        self.assertIn("team", VW.SEED_TIERS)
        self.assertIn("distribution", VW.SEED_TIERS)


class TestVelvetResolves(unittest.TestCase):
    """VELVET must now resolve in verify_wallet (was 'not in tracked config')."""
    def setUp(self):
        self._orig = (VW.token_transfers, VW._live_price, VW.balance_of)
        VW.balance_of = lambda *a, **k: {"available": False}   # SPEC 58: no live RPC in tests
        VW._live_price = lambda token: (0.5, "bybit")

    def tearDown(self):
        VW.token_transfers, VW._live_price, VW.balance_of = self._orig

    def test_verify_wallet_resolves_velvet_multichain(self):
        VW.token_transfers = lambda a, c, ck, days=180, decimals=18: ([tx(OPERATOR, RECIP, 1000)], "moralis", False)
        v = VW.build_verify(RECIP, "VELVET")
        self.assertTrue(v["available"])                       # NOT {available:false, not in tracked config}
        self.assertEqual(set(v["chains_queried"]), {"binance-smart-chain", "base"})


class TestSeedTierSemantics(unittest.TestCase):
    """SPEC 68: seeded-staging semantics via INLINE FIXTURES — a re-tier of any live VELVET wallet
    can't break these. No live-config tier is referenced."""

    FIX_TEAM = "0x" + "a1" * 20
    FIX_BRIDGE = "0x" + "b2" * 20
    FIX_RECIP = "0x" + "c3" * 20

    def setUp(self):
        self._orig = (VW.token_transfers, VW._live_price, VW.balance_of, VW.WALLETS)
        VW.balance_of = lambda *a, **k: {"available": False}
        VW._live_price = lambda token: (0.5, "bybit")
        self._cfg_path = OI.temp_token_config("FIX", [
            {"label": "FIX-TEAM-SAFE", "address": self.FIX_TEAM,
             "chain": "binance-smart-chain", "tier": "team"},
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

    def test_funded_by_operator_safe_is_seeded_staging(self):
        # receiving from tracked operator/team supply = seeded staging (team ∈ SEED_TIERS)
        VW.token_transfers = lambda a, c, ck, days=180, decimals=18: ([self._tx(self.FIX_TEAM)], "moralis", False)
        v = VW.build_verify(self.FIX_RECIP, "FIX")
        self.assertTrue(v.get("seeded_staging"), "funded by a team safe → SEEDED-STAGING")

    def test_funded_by_bridge_is_not_seeded_staging(self):
        # receiving from the bridge/lock vault is NOT operator seeding (bridge ∉ SEED_TIERS)
        VW.token_transfers = lambda a, c, ck, days=180, decimals=18: ([self._tx(self.FIX_BRIDGE)], "moralis", False)
        v = VW.build_verify(self.FIX_RECIP, "FIX")
        self.assertFalse(v.get("seeded_staging"), "bridge-funded must NOT be flagged seeded-staging")


if __name__ == "__main__":
    unittest.main(verbosity=2)
