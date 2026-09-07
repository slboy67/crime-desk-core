#!/usr/bin/env python3
"""SPEC 34 — coingecko/pull5 contract fallback so UNTRACKED discovery reads surface concentration.

Run:  python3 tests/test_concentration_fallback.py

Root cause: `onchain._concentration` sourced the token contract ONLY from tracked_wallets.json,
so every UNTRACKED ticker was concentration-blind — the single most important §0.6 chip read —
even though the coingecko contract + GoPlus chain support were one API call away. This adds the
fallback (concentration only; nonce/distribution still needs onboarding) flagged discovery:true.
Network mocked — offline-deterministic.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


OC = _load("onchain")


def _goplus_payload(contract, holders):
    # holders = list of (address, percent) — GoPlus percent is a 0..1 fraction
    return {"result": {contract.lower(): {
        "holder_count": "390",
        "holders": [{"address": a, "percent": str(p / 100.0), "is_contract": 0, "is_locked": 0, "tag": ""}
                    for a, p in holders]}}}


class TestCgPlatformMapping(unittest.TestCase):
    def test_platform_names_map_to_goplus_chain_keys(self):
        plats = {"ethereum": "0xaaa", "arbitrum-one": "0xbbb", "base": "0xccc",
                 "polygon-pos": "0xddd", "solana": "SoLxxx", "unknown-chain": "0xeee"}
        out = OC._cg_platforms_to_goplus(plats)
        self.assertEqual(out.get("ethereum"), "0xaaa")
        self.assertEqual(out.get("arbitrum"), "0xbbb")     # arbitrum-one → arbitrum
        self.assertEqual(out.get("base"), "0xccc")
        self.assertEqual(out.get("polygon"), "0xddd")      # polygon-pos → polygon
        # SPEC-192 #1: solana IS now GoPlus-supported (identity-mapped)
        self.assertEqual(out.get("solana"), "SoLxxx")
        self.assertNotIn("unknown-chain", out)             # unmapped dropped


class TestUntrackedFallback(unittest.TestCase):
    def setUp(self):
        self._cg, self._get = OC._coingecko_contracts, OC._get_json

    def tearDown(self):
        OC._coingecko_contracts, OC._get_json = self._cg, self._get

    def test_untracked_ticker_gets_fallback_concentration_flagged_discovery(self):
        CONTRACT = "0x0c1c1c109fe34733fca54b82d7b46b75cfb71f6e"
        OC._coingecko_contracts = lambda t: {"ethereum": CONTRACT}
        OC._get_json = lambda url, **k: _goplus_payload(CONTRACT, [("0xe7761a", 70.89), ("0xlp", 17.0), ("0xh3", 3.0)])
        c = OC._concentration("CHIPZZ")                    # a name guaranteed NOT in tracked config
        self.assertTrue(c["available"])
        self.assertAlmostEqual(c["top1_pct"], 70.89, places=2)
        self.assertEqual(c["holder_count"], 390)
        self.assertEqual(c["source"], "goplus")
        self.assertEqual(c["chain"], "ethereum")
        self.assertTrue(c["discovery"])                    # flagged: untracked read
        self.assertFalse(c["tracked"])

    def test_untracked_with_no_coingecko_contract_is_unavailable(self):
        OC._coingecko_contracts = lambda t: {}
        c = OC._concentration("NOSUCHTOKENXYZ")
        self.assertFalse(c["available"])
        self.assertIn("untracked", c["reason"].lower())

    def test_untracked_with_no_goplus_supported_chain_is_unavailable(self):
        OC._coingecko_contracts = lambda t: {}              # solana-only mapped out → empty
        c = OC._concentration("SOLONLY")
        self.assertFalse(c["available"])


class TestTrackedStillWins(unittest.TestCase):
    """An onboarded ticker must still use its config contract — fallback NOT consulted."""
    def setUp(self):
        self._cg, self._get = OC._coingecko_contracts, OC._get_json

    def tearDown(self):
        OC._coingecko_contracts, OC._get_json = self._cg, self._get

    def test_tracked_ticker_uses_config_not_coingecko(self):
        import json
        tracked = json.loads((ROOT / "config" / "tracked_wallets.json").read_text())["tokens"]
        # pick a tracked BSC name
        tk = next(t for t, v in tracked.items()
                  if "binance-smart-chain" in (v.get("contracts") or {}))
        cfg_contract = tracked[tk]["contracts"]["binance-smart-chain"]
        called = {"cg": False}

        def _no_cg(t):
            called["cg"] = True
            return {"ethereum": "0xWRONG"}
        OC._coingecko_contracts = _no_cg
        OC._get_json = lambda url, **k: _goplus_payload(cfg_contract, [("0xt1", 20.0), ("0xt2", 10.0)])
        c = OC._concentration(tk)
        self.assertTrue(c["available"])
        self.assertFalse(called["cg"], "coingecko fallback must NOT run for a tracked ticker")
        self.assertTrue(c.get("tracked"))
        self.assertFalse(c.get("discovery"))
        self.assertEqual(c["chain"], "binance-smart-chain")


if __name__ == "__main__":
    unittest.main(verbosity=2)
