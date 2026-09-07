#!/usr/bin/env python3
"""SPEC 33 — CHIP (USD.AI, multi-chain non-BSC) onboarded into on-chain tracking.

Run:  python3 tests/test_chip_onboarding.py

Offline: validates the config — multi-chain non-BSC contracts, ETH chosen as the
concentration primary (reproduces the verified 390-holder / top1-71% read), tiers, and the
EVM-only invariant (Solana is documented but NOT a nonce-watch wallet). Network mocked.
"""
import importlib.util
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
CHIP = CFG.get("CHIP", {})

ETH_TREASURY = "0xe7761a103c85bbdf25957606b20a32cb6be2cf78"   # 70.89% ETH — tier team
ARB_SAFE = "0xe23796fbda930646e903c2c94a6ed1312409ca05"       # 8% Arbitrum Gnosis Safe — tier team
ETH_LP = "0x155738f4da7d7b55876f5398bb0195d027400127"         # UniV3 LP — tier pool
RECIP = "0xabc0000000000000000000000000000000000033"
NOW = "2026-06-09T12:00:00.000Z"


def tx(frm, to, val):
    return {"from_address": frm, "to_address": to, "value_decimal": str(val),
            "block_timestamp": NOW, "token_symbol": "CHIP"}


class TestChipConfig(unittest.TestCase):
    def test_present_multichain_non_bsc_decimals(self):
        self.assertTrue(CHIP, "CHIP missing from tracked_wallets.json")
        self.assertEqual(set(CHIP["contracts"]), {"ethereum", "arbitrum", "base"})
        self.assertNotIn("binance-smart-chain", CHIP["contracts"])     # the non-BSC blind spot
        self.assertEqual(CHIP["decimals"], 18)
        self.assertGreaterEqual(len(CHIP["wallets"]), 6)

    def test_ethereum_is_the_concentration_primary(self):
        # _concentration picks BSC-if-present else next(iter(contracts)); CHIP has no BSC, and the
        # contracts are ordered ethereum-first so concentration reproduces the verified ETH read.
        self.assertEqual(next(iter(CHIP["contracts"])), "ethereum")
        self.assertIn("ethereum", OC._GOPLUS_CHAIN)

    def test_solana_documented_but_excluded_from_nonce(self):
        # EVM-only framework: Solana is recorded separately, NEVER as a nonce-watch wallet/contract
        self.assertIn("_solana_contract", CHIP)
        self.assertNotIn("solana", CHIP["contracts"])
        chains = {(w.get("chain") or "").lower() for w in CHIP["wallets"]}
        self.assertNotIn("solana", chains)
        self.assertTrue(chains <= {"ethereum", "arbitrum", "base"})

    def test_key_wallets_are_structurally_valid(self):
        # SPEC 68: don't pin the treasury/safe/LP live tiers (a re-tier would break the suite);
        # assert they are onboarded + structurally valid. Tier *semantics* are the system invariants
        # in test_team_and_distribution_are_seed_tiers_pool_is_not.
        OI.assert_unique_within_token(self, CHIP, "CHIP")
        OI.assert_wallet_structural(self, OI.find_wallet(CHIP, ETH_TREASURY), expected_chain="ethereum")
        OI.assert_wallet_structural(self, OI.find_wallet(CHIP, ARB_SAFE), expected_chain="arbitrum")
        OI.assert_wallet_structural(self, OI.find_wallet(CHIP, ETH_LP), expected_chain="ethereum")

    def test_team_and_distribution_are_seed_tiers_pool_is_not(self):
        self.assertIn("team", VW.SEED_TIERS)
        self.assertIn("distribution", VW.SEED_TIERS)
        self.assertNotIn("pool", VW.SEED_TIERS)                   # LPs never seed-discovered


class TestChipResolves(unittest.TestCase):
    def setUp(self):
        self._orig = (VW.token_transfers, VW._live_price, VW.balance_of)
        VW.balance_of = lambda *a, **k: {"available": False}   # SPEC 58: no live RPC in tests
        VW._live_price = lambda token: (1.0, "bybit")

    def tearDown(self):
        VW.token_transfers, VW._live_price, VW.balance_of = self._orig

    def test_verify_wallet_resolves_chip_multichain_evm(self):
        VW.token_transfers = lambda a, c, ck, days=180, decimals=18: ([tx(ETH_TREASURY, RECIP, 1000)], "moralis", False)
        v = VW.build_verify(RECIP, "CHIP")
        self.assertTrue(v["available"])                          # was 'not in tracked config'
        self.assertEqual(set(v["chains_queried"]), {"ethereum", "arbitrum", "base"})
        self.assertNotIn("solana", v["chains_queried"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
