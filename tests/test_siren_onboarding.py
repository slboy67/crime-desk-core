#!/usr/bin/env python3
"""SPEC 37 — SIREN (siren-2, BNB meme, cluster-connected) onboarded to on-chain tracking.

Run:  python3 tests/test_siren_onboarding.py

Offline: config shape + tiers, the BURN address excluded (NOT distribution — mis-tagging it
would wreck concentration/distribution math), the cross-cluster escrow linked to RAVE/SKYAI/
SLX/KITE, and `_concentration` flagging burned supply (`is_burn`, `burn_pct`, `top1_ex_burn_pct`)
so a 27% burn doesn't read like a 27% operator whale. Network mocked.
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
TOKENS = OI.load_tokens()
SIREN = TOKENS.get("SIREN", {})

BURN = "0x000000000000000000000000000000000000dead"
ESCROW = "0x73d8bd54f7cf5fab43fe4ef40a62d390644946db"
WHALE = "0xfe5bcc32063ced8384507d41c334ead5c70fbb56"
POOL = "0xb2af49dbf526054faf19602860a5e298a79f3d05"
RECIP = "0xabc0000000000000000000000000000000000037"
NOW = "2026-06-09T12:00:00.000Z"


def tx(frm, to, val):
    return {"from_address": frm, "to_address": to, "value_decimal": str(val),
            "block_timestamp": NOW, "token_symbol": "SIREN"}


class TestSirenConfig(unittest.TestCase):
    def test_present_bsc_decimals(self):
        self.assertTrue(SIREN, "SIREN missing from tracked_wallets.json")
        self.assertEqual(set(SIREN["contracts"]), {"binance-smart-chain"})
        self.assertEqual(SIREN["contracts"]["binance-smart-chain"],
                         "0x997a58129890bbda032231a52ed1ddc845fc18e1")
        self.assertEqual(SIREN["decimals"], 18)
        self.assertGreaterEqual(len(SIREN["wallets"]), 5)

    def test_onboarded_wallets_are_structurally_valid(self):
        OI.assert_unique_within_token(self, SIREN, "SIREN")
        for addr in (BURN, ESCROW, WHALE, POOL):
            OI.assert_wallet_structural(self, OI.find_wallet(SIREN, addr))

    def test_burn_tier_is_never_seed_discovered_nor_operational(self):
        # SPEC 68: don't pin the burn wallet's live tier; assert the *system* invariant that the
        # burn tier (whatever address carries it) is excluded from seeding AND operational noise —
        # mis-reading a burn as distribution would wreck the concentration math. Behaviour that the
        # burn address itself is flagged is covered by TestConcentrationBurnFlag below.
        self.assertNotIn("burn", VW.SEED_TIERS)
        self.assertNotIn("burn", OC.OP_TIERS)

    def test_cluster_escrow_linked_to_other_cluster_names(self):
        # the cluster linkage: the SAME escrow address is already tracked on RAVE et al.
        OI.assert_wallet_structural(self, OI.find_wallet(SIREN, ESCROW))
        others = [t for t, v in TOKENS.items()
                  if t != "SIREN" and any(w["address"].lower() == ESCROW for w in v.get("wallets", []))]
        self.assertIn("RAVE", others, "escrow must cross-reference the existing cluster (RAVE)")


class TestConcentrationBurnFlag(unittest.TestCase):
    def setUp(self):
        self._get = OC._get_json

    def tearDown(self):
        OC._get_json = self._get

    def test_burn_flagged_and_excluded_from_operator_concentration(self):
        contract = SIREN["contracts"]["binance-smart-chain"].lower()
        OC._get_json = lambda url, **k: {"result": {contract: {"holder_count": "54142", "holders": [
            {"address": BURN, "percent": "0.2741", "is_contract": "0", "is_locked": "1", "tag": ""},
            {"address": WHALE, "percent": "0.0718", "is_contract": "0", "is_locked": "0", "tag": ""},
            {"address": ESCROW, "percent": "0.0253", "is_contract": "1", "is_locked": "0", "tag": ""},
        ]}}}
        c = OC._concentration("SIREN")
        self.assertTrue(c["available"])
        self.assertAlmostEqual(c["top1_pct"], 27.41, places=2)          # raw top1 = the burn
        self.assertAlmostEqual(c["burn_pct"], 27.41, places=2)          # flagged as burned
        self.assertAlmostEqual(c["top1_ex_burn_pct"], 7.18, places=2)   # the REAL operator top1
        burn_holder = next(h for h in c["holders"] if h["address"].lower() == BURN)
        self.assertTrue(burn_holder["is_burn"])
        whale_holder = next(h for h in c["holders"] if h["address"].lower() == WHALE)
        self.assertFalse(whale_holder["is_burn"])

    def test_is_burn_helper(self):
        self.assertTrue(OC._is_burn("0x000000000000000000000000000000000000dEaD"))
        self.assertTrue(OC._is_burn("0x0000000000000000000000000000000000000000"))
        self.assertFalse(OC._is_burn(WHALE))


class TestSirenResolves(unittest.TestCase):
    def setUp(self):
        self._orig = (VW.token_transfers, VW._live_price, VW.balance_of)
        VW.balance_of = lambda *a, **k: {"available": False}   # SPEC 58: no live RPC in tests
        VW._live_price = lambda token: (1.1, "bybit")

    def tearDown(self):
        VW.token_transfers, VW._live_price, VW.balance_of = self._orig

    def test_verify_wallet_resolves_siren(self):
        VW.token_transfers = lambda a, c, ck, days=180, decimals=18: ([tx(WHALE, RECIP, 1000)], "moralis", False)
        v = VW.build_verify(RECIP, "SIREN")
        self.assertTrue(v["available"])
        self.assertIn("binance-smart-chain", v["chains_queried"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
