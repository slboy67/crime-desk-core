#!/usr/bin/env python3
"""SPEC 47 — `onboard`: config-driven token onboarding.

Run:  python3 tests/test_onboard.py

Five of 43 SPECs were "onboard token X" — config work routed through full coder
dispatches. Contract under test:
  - onboarding a fixture token writes the tracked_wallets skeleton (contracts,
    decimals) + GoPlus top holders as tier:"unclassified" CANDIDATES + seeds the
    nonce baseline;
  - HARD GUARD: no auto-tiering — unclassified is never in SEED_TIERS, so candidates
    cannot seed radar discovery until the Designer classifies them;
  - needs_judgment carries {address, balance_pct, hints} for a one-read tiering pass;
  - re-run on a tracked token reports current state and never clobbers classified tiers.
Network seams (coingecko, GoPlus, nonce seeding) injected — offline, deterministic.
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
VW = _load("verify_wallet")

CG_FIXTURE = {
    "cg_id": "testtoken", "resolved_id": "testtoken", "low_confidence": False,
    "name": "Test Token", "symbol": "TT",
    "contracts": {"binance-smart-chain": "0xabc0000000000000000000000000000000000001"},
}

HOLDERS_FIXTURE = {
    "available": True, "source": "goplus", "chain": "binance-smart-chain",
    "holder_count": 1234, "top1_pct": 40.0, "top10_pct": 80.0,
    "holders": [
        {"address": "0x1111111111111111111111111111111111111111", "percent": 40.0,
         "tag": None, "is_contract": True, "is_locked": True, "is_burn": False},
        {"address": "0x2222222222222222222222222222222222222222", "percent": 20.0,
         "tag": "Binance 14", "is_contract": False, "is_locked": False, "is_burn": False},
        {"address": "0x000000000000000000000000000000000000dead", "percent": 10.0,
         "tag": None, "is_contract": False, "is_locked": False, "is_burn": True},
        {"address": "0x3333333333333333333333333333333333333333", "percent": 5.0,
         "tag": None, "is_contract": False, "is_locked": False, "is_burn": False},
    ],
}


ERC20_META_FIXTURE = {"available": True, "name": "Test Debit", "decimals": 18,
                      "total_supply": 100_000_000.0}


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig = (OB.WALLETS, OB._cg_resolve, OB._cg_search, OB._holders,
                      OB._decimals, OB._seed_baseline, OB._erc20_meta)
        OB.WALLETS = d / "tracked_wallets.json"
        OB.WALLETS.write_text(json.dumps({"rpcs": {}, "tokens": {}}))
        OB._cg_resolve = lambda tk, cg_id=None: dict(CG_FIXTURE)
        OB._cg_search = lambda tk: []     # SPEC 52: ambiguity path stays offline
        OB._holders = lambda contract, chain: dict(HOLDERS_FIXTURE)
        OB._decimals = lambda cg_id, platform: 18
        OB._erc20_meta = lambda contract, chain: dict(ERC20_META_FIXTURE)
        self.seeded = []
        OB._seed_baseline = lambda tk: (self.seeded.append(tk) or {"baseline_seeded": True})

    def tearDown(self):
        (OB.WALLETS, OB._cg_resolve, OB._cg_search, OB._holders,
         OB._decimals, OB._seed_baseline, OB._erc20_meta) = self._orig
        self.dir.cleanup()

    def cfg(self):
        return json.loads(OB.WALLETS.read_text())


class TestOnboard(_Tmp):
    def test_writes_skeleton_candidates_and_baseline(self):
        out = OB.build_onboard("TT")
        self.assertTrue(out["ok"])
        tok = self.cfg()["tokens"]["TT"]
        self.assertEqual(tok["contracts"]["binance-smart-chain"],
                         "0xabc0000000000000000000000000000000000001")
        self.assertEqual(tok["decimals"], 18)
        # every candidate is unclassified; the burn sink is NOT a candidate
        tiers = {w["tier"] for w in tok["wallets"]}
        self.assertEqual(tiers, {"unclassified"})
        addrs = {w["address"] for w in tok["wallets"]}
        self.assertNotIn("0x000000000000000000000000000000000000dead", addrs)
        self.assertEqual(self.seeded, ["TT"])
        self.assertTrue(out["baseline_seeded"])

    def test_needs_judgment_hints(self):
        out = OB.build_onboard("TT")
        nj = {x["address"]: x for x in out["needs_judgment"]}
        self.assertEqual(nj["0x1111111111111111111111111111111111111111"]["hints"],
                         {"contract": True, "locked": True, "cex_label": None})
        self.assertEqual(nj["0x2222222222222222222222222222222222222222"]["hints"]["cex_label"],
                         "Binance 14")
        self.assertEqual(nj["0x2222222222222222222222222222222222222222"]["balance_pct"], 20.0)

    def test_unclassified_never_in_seed_tiers(self):
        # the HARD GUARD: radars/verify seed-discovery filter on SEED_TIERS membership
        self.assertNotIn("unclassified", VW.SEED_TIERS)
        OB.build_onboard("TT")
        wallets = self.cfg()["tokens"]["TT"]["wallets"]
        seeds = [w for w in wallets if (w.get("tier") or "").lower() in VW.SEED_TIERS]
        self.assertEqual(seeds, [])

    def test_rerun_reports_no_clobber(self):
        OB.build_onboard("TT")
        # the Designer classifies a candidate
        cfg = self.cfg()
        cfg["tokens"]["TT"]["wallets"][0]["tier"] = "team"
        OB.WALLETS.write_text(json.dumps(cfg))
        before = OB.WALLETS.read_text()
        out = OB.build_onboard("TT")
        self.assertTrue(out["already_tracked"])
        self.assertEqual(OB.WALLETS.read_text(), before)   # report-only, no write
        self.assertEqual(out["tiers"].get("team"), 1)

    def test_resolution_failure_degrades(self):
        OB._cg_resolve = lambda tk, cg_id=None: {"_error": "no coingecko id found for XX"}
        out = OB.build_onboard("XX")
        self.assertFalse(out["ok"])
        self.assertNotIn("XX", self.cfg()["tokens"])
        self.assertEqual(self.seeded, [])

    def test_chain_alias_filter(self):
        OB._cg_resolve = lambda tk, cg_id=None: {**CG_FIXTURE,
                                     "contracts": {"binance-smart-chain": "0xabc0000000000000000000000000000000000001",
                                                   "ethereum": "0xdef0000000000000000000000000000000000002"}}
        out = OB.build_onboard("TT", chain="bsc")
        tok = self.cfg()["tokens"]["TT"]
        self.assertEqual(list(tok["contracts"]), ["binance-smart-chain"])
        self.assertTrue(out["ok"])

    def test_goplus_unavailable_still_onboards_skeleton(self):
        # no candidates ≠ no onboarding: contracts + baseline still land; judgment list empty
        OB._holders = lambda contract, chain: {"available": False, "reason": "GoPlus no result"}
        out = OB.build_onboard("TT")
        self.assertTrue(out["ok"])
        self.assertEqual(out["needs_judgment"], [])
        self.assertEqual(self.cfg()["tokens"]["TT"]["wallets"], [])
        self.assertEqual(self.seeded, ["TT"])


DEBIT_CONTRACT = "0x" + "66" * 18 + "ce49"
assert len(DEBIT_CONTRACT) == 42


class TestOnboardByContract(_Tmp):
    """SPEC-165 problem 1 — explicit contract onboarding, no CoinGecko dependency.

    Fresh BSC->Alpha names (DEBIT, KORU) have no CoinGecko entry on day one; onboarding
    must accept {ticker, contract, chain} and skip CoinGecko resolution entirely."""

    def test_onboards_by_contract_offline(self):
        cg_called = []
        OB._cg_resolve = lambda tk, cg_id=None: (cg_called.append(tk) or dict(CG_FIXTURE))
        out = OB.build_onboard("DEBIT", chain="binance-smart-chain", contract=DEBIT_CONTRACT)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["onboarded_by"], "contract")
        self.assertEqual(cg_called, [])          # CoinGecko never touched
        tok = self.cfg()["tokens"]["DEBIT"]
        self.assertEqual(tok["contracts"]["binance-smart-chain"], DEBIT_CONTRACT.lower())
        self.assertEqual(tok["decimals"], 18)
        self.assertEqual(tok["name"], "Test Debit")
        # holders still land as unclassified candidates (GoPlus path unchanged)
        tiers = {w["tier"] for w in tok["wallets"]}
        self.assertEqual(tiers, {"unclassified"})
        self.assertEqual(self.seeded, ["DEBIT"])
        self.assertTrue(out["baseline_seeded"])

    def test_missing_chain_is_structured_error_not_traceback(self):
        out = OB.build_onboard("DEBIT", contract=DEBIT_CONTRACT)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "contract_requires_chain")
        self.assertNotIn("DEBIT", self.cfg()["tokens"])

    def test_bad_contract_format_is_structured_error(self):
        out = OB.build_onboard("DEBIT", chain="bsc", contract="not-an-address")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "bad_contract")
        self.assertNotIn("DEBIT", self.cfg()["tokens"])

    def test_unsupported_chain_is_structured_error(self):
        out = OB.build_onboard("DEBIT", chain="solana", contract=DEBIT_CONTRACT)
        self.assertFalse(out["ok"])
        self.assertIn("unsupported_chain", out["reason"])

    def test_rpc_meta_failure_degrades_not_crashes(self):
        OB._erc20_meta = lambda contract, chain: {"available": False, "name": None,
                                                    "decimals": 18, "total_supply": None}
        out = OB.build_onboard("DEBIT", chain="bsc", contract=DEBIT_CONTRACT)
        self.assertTrue(out["ok"])   # meta failure is non-fatal — contracts still land
        self.assertEqual(self.cfg()["tokens"]["DEBIT"]["decimals"], 18)

    def test_goplus_failure_degrades_not_crashes(self):
        OB._holders = lambda contract, chain: {"available": False, "reason": "GoPlus no result"}
        out = OB.build_onboard("DEBIT", chain="bsc", contract=DEBIT_CONTRACT)
        self.assertTrue(out["ok"])
        self.assertEqual(out["needs_judgment"], [])
        self.assertEqual(self.cfg()["tokens"]["DEBIT"]["wallets"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
