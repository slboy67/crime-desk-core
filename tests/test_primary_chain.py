#!/usr/bin/env python3
"""SPEC 56 — primary-chain resolution: map multi-chain tokens where the supply lives.

Run:  python3 tests/test_primary_chain.py

Live failure: FOLKS deploys on 9 chains; real supply is Avalanche/Algorand-native, the
BSC deployment is a bridge stub. onboard resolved all 9 contracts but concentration/
nonce read the BSC stub → top1:null, holders:0 — the desk traded on-chain-blind.
Contract under test:
  - onboard ranks chains by GoPlus supply/holders: primary_chain = where supply lives;
    a bridge stub (tiny supply/holders) is NEVER primary; per-chain supply_pct written;
  - non-EVM chains → explicit supported:false entries + unreadable_supply_pct (the
    Designer SEES how much supply is invisible — never a silent zero);
  - candidates (needs_judgment) come from the primary chain;
  - concentration follows primary_chain; split supply (top-2 ≥20%) merges reads with
    per-holder chain tags and global (supply-scaled) percentages;
  - a BSC-native token (ESPORTS fixture) takes the legacy path unchanged.
All network seams monkeypatched — offline, deterministic.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OB = _load("onboard")
OC = _load("onchain")

AVAX_C = "0xaaa0000000000000000000000000000000000001"
BSC_C = "0xbbb0000000000000000000000000000000000002"
ALGO_C = "ALGOASSETID123"

CG_FOLKS = {
    "cg_id": "folks", "low_confidence": False, "name": "Folks", "symbol": "FOLKS",
    "total_supply": 100_000_000,
    "contracts": {"binance-smart-chain": BSC_C, "avalanche": AVAX_C, "algorand": ALGO_C},
}
# per-goplus-chain stats: avalanche carries the supply, BSC is a bridge stub
CHAIN_STATS = {
    "avalanche": {"holder_count": 4000, "total_supply": 60_000_000.0},
    "binance-smart-chain": {"holder_count": 10, "total_supply": 500_000.0},
}


def holders_fixture(chain):
    return {"available": True, "source": "goplus", "chain": chain,
            "holder_count": 100, "top1_pct": 30.0, "top10_pct": 60.0,
            "holders": [{"address": f"0x{chain[:4]}{'1' * 36}"[:42], "percent": 30.0,
                         "tag": None, "is_contract": False, "is_locked": False,
                         "is_burn": False}]}


class _OnboardTmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig = (OB.WALLETS, OB.WL_PATH, OB._cg_resolve, OB._cg_search, OB._holders,
                      OB._decimals, OB._seed_baseline, OB._chain_stats)
        OB.WALLETS = d / "tracked_wallets.json"
        OB.WL_PATH = d / "watchlist.json"
        OB.WALLETS.write_text(json.dumps({"rpcs": {}, "tokens": {}}))
        OB.WL_PATH.write_text(json.dumps({"tokens": []}))
        OB._cg_resolve = lambda tk, cg_id=None: dict(CG_FOLKS)
        OB._cg_search = lambda tk: []
        self.holders_calls = []
        OB._holders = lambda contract, chain: (self.holders_calls.append((contract, chain))
                                               or holders_fixture(chain))
        OB._decimals = lambda cg_id, plat: 18
        OB._seed_baseline = lambda tk: {"baseline_seeded": True}
        self.stats_calls = []
        OB._chain_stats = lambda contract, gp: (self.stats_calls.append(gp)
                                                or dict(CHAIN_STATS.get(gp, {})))

    def tearDown(self):
        (OB.WALLETS, OB.WL_PATH, OB._cg_resolve, OB._cg_search, OB._holders,
         OB._decimals, OB._seed_baseline, OB._chain_stats) = self._orig
        self.dir.cleanup()

    def cfg(self, tk="FOLKS"):
        return json.loads(OB.WALLETS.read_text())["tokens"][tk]


class TestOnboardRanking(_OnboardTmp):
    def test_primary_is_where_supply_lives(self):
        out = OB.build_onboard("FOLKS")
        self.assertTrue(out["ok"])
        self.assertEqual(out["primary_chain"], "avalanche")
        tok = self.cfg()
        self.assertEqual(tok["primary_chain"], "avalanche")
        by_chain = {c["chain"]: c for c in tok["chains_ranked"]}
        self.assertAlmostEqual(by_chain["avalanche"]["supply_pct"], 60.0, places=1)
        self.assertTrue(by_chain["binance-smart-chain"]["bridge_stub"])

    def test_bridge_stub_never_primary(self):
        # even with avalanche stats missing, the 0.5% BSC stub must not become primary
        OB._chain_stats = lambda c, gp: dict(CHAIN_STATS.get(gp, {})) if gp != "avalanche" \
            else {"holder_count": 4000, "total_supply": 30_000_000.0}
        out = OB.build_onboard("FOLKS")
        self.assertEqual(out["primary_chain"], "avalanche")

    def test_unsupported_chain_surfaced_with_unreadable_pct(self):
        out = OB.build_onboard("FOLKS")
        tok = self.cfg()
        unsup = [c for c in tok["chains_ranked"] if not c.get("supported")]
        self.assertEqual([c["chain"] for c in unsup], ["algorand"])
        # 100M total − 60.5M readable → ~39.5% invisible, said out loud
        self.assertAlmostEqual(tok["unreadable_supply_pct"], 39.5, places=1)
        self.assertAlmostEqual(out["unreadable_supply_pct"], 39.5, places=1)

    def test_candidates_come_from_primary_chain(self):
        OB.build_onboard("FOLKS")
        self.assertEqual(self.holders_calls, [(AVAX_C, "avalanche")])
        wallets = self.cfg()["wallets"]
        self.assertTrue(all(w["chain"] == "avalanche" for w in wallets))
        self.assertTrue(all(w["tier"] == "unclassified" for w in wallets))

    def test_single_chain_skips_ranking_reads(self):
        OB._cg_resolve = lambda tk, cg_id=None: dict(CG_FOLKS, contracts={"binance-smart-chain": BSC_C})
        out = OB.build_onboard("NATIVE")
        self.assertEqual(out["primary_chain"], "binance-smart-chain")
        self.assertEqual(self.stats_calls, [])          # no extra GoPlus reads


class _ConcTmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig = (OC.WALLETS, OC._goplus_concentration)
        OC.WALLETS = d / "tracked_wallets.json"
        self.gp_calls = []

        def gp(contract, chain):
            self.gp_calls.append((contract, chain))
            return {"available": True, "source": "goplus", "chain": chain,
                    "holder_count": {"avalanche": 4000, "binance-smart-chain": 10,
                                     "ethereum": 900}.get(chain, 1),
                    "top1_pct": 30.0, "top10_pct": 60.0,
                    "holders": [{"address": f"0x{chain[:6]}{'2' * 36}"[:42], "percent": 30.0,
                                 "tag": None, "is_contract": False, "is_locked": False,
                                 "is_burn": False}]}
        OC._goplus_concentration = gp

    def tearDown(self):
        OC.WALLETS, OC._goplus_concentration = self._orig
        self.dir.cleanup()

    def write_tok(self, tok):
        OC.WALLETS.write_text(json.dumps({"rpcs": {}, "tokens": {"X": tok}}))


class TestConcentrationFollowsPrimary(_ConcTmp):
    def test_primary_chain_read_with_coverage(self):
        self.write_tok({
            "contracts": {"binance-smart-chain": BSC_C, "avalanche": AVAX_C, "algorand": ALGO_C},
            "primary_chain": "avalanche",
            "unreadable_supply_pct": 39.5,
            "chains_ranked": [
                {"chain": "avalanche", "goplus_chain": "avalanche", "supported": True,
                 "supply_pct": 60.0, "holder_count": 4000, "bridge_stub": False},
                {"chain": "binance-smart-chain", "goplus_chain": "binance-smart-chain",
                 "supported": True, "supply_pct": 0.5, "holder_count": 10, "bridge_stub": True},
                {"chain": "algorand", "supported": False, "supply_pct": None},
            ],
            "wallets": []})
        r = OC._concentration("X")
        self.assertTrue(r["available"])
        self.assertEqual(self.gp_calls, [(AVAX_C, "avalanche")])   # NOT the BSC stub
        cov = r["coverage"]
        self.assertEqual(cov["primary_chain"], "avalanche")
        self.assertEqual(cov["chains_read"], ["avalanche"])
        self.assertAlmostEqual(cov["unreadable_supply_pct"], 39.5)
        self.assertEqual([u["chain"] for u in cov["unsupported"]], ["algorand"])

    def test_split_supply_merges_top2_with_chain_tags(self):
        eth_c = "0xeee0000000000000000000000000000000000003"
        self.write_tok({
            "contracts": {"avalanche": AVAX_C, "ethereum": eth_c},
            "primary_chain": "avalanche",
            "unreadable_supply_pct": 0.0,
            "chains_ranked": [
                {"chain": "avalanche", "goplus_chain": "avalanche", "supported": True,
                 "supply_pct": 55.0, "holder_count": 4000, "bridge_stub": False},
                {"chain": "ethereum", "goplus_chain": "ethereum", "supported": True,
                 "supply_pct": 45.0, "holder_count": 900, "bridge_stub": False},
            ],
            "wallets": []})
        r = OC._concentration("X")
        self.assertEqual(sorted(c for _, c in self.gp_calls), ["avalanche", "ethereum"])
        self.assertEqual(sorted(r["coverage"]["chains_read"]), ["avalanche", "ethereum"])
        chains_seen = {h["chain"] for h in r["holders"]}
        self.assertEqual(chains_seen, {"avalanche", "ethereum"})
        # global pct = chain-local 30% scaled by the chain's supply share
        top = r["holders"][0]
        self.assertEqual(top["chain"], "avalanche")
        self.assertAlmostEqual(top["global_pct"], 30.0 * 0.55, places=2)
        self.assertAlmostEqual(r["top1_pct"], 16.5, places=2)

    def test_bsc_native_legacy_path_unchanged(self):
        self.write_tok({"contracts": {"binance-smart-chain": BSC_C}, "wallets": []})
        r = OC._concentration("X")
        self.assertEqual(self.gp_calls, [(BSC_C, "binance-smart-chain")])
        self.assertNotIn("coverage", r)                 # byte-for-byte legacy output
        self.assertEqual(r["top1_pct"], 30.0)


class TestChainMaps(unittest.TestCase):
    def test_avalanche_wired_everywhere(self):
        self.assertIn("avalanche", OC._GOPLUS_CHAIN)
        self.assertEqual(OC._CG_PLATFORM_TO_GOPLUS.get("avalanche"), "avalanche")
        self.assertIn("avalanche", OC._MORALIS_CHAIN)
        self.assertTrue(OC._PUBLIC_RPCS.get("avalanche"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
