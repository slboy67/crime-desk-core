#!/usr/bin/env python3
"""SPEC 52 — `onboard`: explicit coingecko_id override + stage-specific failure reasons.

Run:  python3 tests/test_onboard_override.py

Live failure: onboard SLX → {"ok":false,"reason":"fetch failed"} — 4 coingecko symbol
collisions and a single-string reason the Designer can't act on. Contract under test:
  - explicit coingecko_id bypasses symbol search entirely;
  - ambiguous resolution → reason "resolve_ambiguous" + candidates:[{id,symbol,name}]
    and NO config write (the retry is one round-trip with the chosen id);
  - every failure stage returns its named reason: resolve_not_found,
    contract_fetch_failed:<...>, goplus_failed:<...>, baseline_failed:<...> —
    never a bare "fetch failed".
Network seams monkeypatched — offline, deterministic.
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

CG_OK = {
    "cg_id": "solstice", "resolved_id": "solstice", "low_confidence": False,
    "name": "Solstice", "symbol": "SLX",
    "contracts": {"binance-smart-chain": "0xabc0000000000000000000000000000000000001"},
}
SLX_CANDIDATES = [
    {"id": "solstice", "symbol": "SLX", "name": "Solstice", "market_cap_rank": 900},
    {"id": "starslax", "symbol": "SLX", "name": "StarSlax", "market_cap_rank": None},
    {"id": "slimex", "symbol": "SLX", "name": "Slimex", "market_cap_rank": None},
    {"id": "dinari-slx", "symbol": "SLX", "name": "Dinari SLX", "market_cap_rank": None},
]
HOLDERS_OK = {"available": True, "source": "goplus", "chain": "binance-smart-chain",
              "holder_count": 10, "top1_pct": 40.0, "top10_pct": 80.0,
              "holders": [{"address": "0x1111111111111111111111111111111111111111",
                           "percent": 40.0, "tag": None, "is_contract": False,
                           "is_locked": False, "is_burn": False}]}


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig = (OB.WALLETS, OB._cg_resolve, OB._cg_search, OB._holders,
                      OB._decimals, OB._seed_baseline)
        OB.WALLETS = d / "tracked_wallets.json"
        OB.WALLETS.write_text(json.dumps({"rpcs": {}, "tokens": {}}))
        self.resolve_calls = []
        def resolve(tk, cg_id=None):
            self.resolve_calls.append((tk, cg_id))
            return dict(CG_OK)
        OB._cg_resolve = resolve
        OB._cg_search = lambda tk: list(SLX_CANDIDATES)
        OB._holders = lambda contract, chain: dict(HOLDERS_OK)
        OB._decimals = lambda cg_id, platform: 18
        self.seeded = []
        OB._seed_baseline = lambda tk: (self.seeded.append(tk) or {"baseline_seeded": True})

    def tearDown(self):
        (OB.WALLETS, OB._cg_resolve, OB._cg_search, OB._holders,
         OB._decimals, OB._seed_baseline) = self._orig
        self.dir.cleanup()

    def cfg(self):
        return json.loads(OB.WALLETS.read_text())


class TestOverride(_Tmp):
    def test_explicit_id_bypasses_search(self):
        searched = []
        OB._cg_search = lambda tk: searched.append(tk)
        out = OB.build_onboard("SLX", coingecko_id="solstice")
        self.assertTrue(out["ok"])
        self.assertEqual(self.resolve_calls, [("SLX", "solstice")])
        self.assertEqual(searched, [])                      # search never touched
        self.assertIn("SLX", self.cfg()["tokens"])
        self.assertEqual(self.seeded, ["SLX"])

    def test_low_confidence_with_explicit_id_still_onboards(self):
        # the Designer resolved the collision by judgment — their id wins
        OB._cg_resolve = lambda tk, cg_id=None: dict(CG_OK, low_confidence=True)
        out = OB.build_onboard("SLX", coingecko_id="solstice")
        self.assertTrue(out["ok"])


class TestAmbiguous(_Tmp):
    def test_ambiguous_returns_candidates_no_write(self):
        OB._cg_resolve = lambda tk, cg_id=None: dict(CG_OK, low_confidence=True)
        out = OB.build_onboard("SLX")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "resolve_ambiguous")
        self.assertEqual([c["id"] for c in out["candidates"]],
                         ["solstice", "starslax", "slimex", "dinari-slx"])
        self.assertNotIn("SLX", self.cfg()["tokens"])       # no write
        self.assertEqual(self.seeded, [])

    def test_resolve_error_with_collisions_is_ambiguous(self):
        # the live SLX case: detail fetches failed BECAUSE of collisions → give candidates
        OB._cg_resolve = lambda tk, cg_id=None: {"_error": "fetch failed"}
        out = OB.build_onboard("SLX")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "resolve_ambiguous")
        self.assertTrue(len(out["candidates"]) >= 2)

    def test_not_found_when_no_candidates(self):
        OB._cg_resolve = lambda tk, cg_id=None: {"_error": "no coingecko id found for XX"}
        OB._cg_search = lambda tk: []
        out = OB.build_onboard("XX")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "resolve_not_found")


class TestStageReasons(_Tmp):
    def test_contract_fetch_failed_with_explicit_id(self):
        OB._cg_resolve = lambda tk, cg_id=None: {"_error": "fetch failed"}
        out = OB.build_onboard("SLX", coingecko_id="solstice")
        self.assertFalse(out["ok"])
        self.assertTrue(out["reason"].startswith("contract_fetch_failed:"), out["reason"])

    def test_contract_fetch_failed_when_no_platforms(self):
        OB._cg_resolve = lambda tk, cg_id=None: dict(CG_OK, contracts={})
        out = OB.build_onboard("SLX", coingecko_id="solstice")
        self.assertFalse(out["ok"])
        self.assertTrue(out["reason"].startswith("contract_fetch_failed:"), out["reason"])

    def test_goplus_failure_named_but_still_onboards(self):
        OB._holders = lambda contract, chain: {"available": False, "reason": "GoPlus no result"}
        out = OB.build_onboard("SLX", coingecko_id="solstice")
        self.assertTrue(out["ok"])                          # skeleton + baseline still land
        self.assertTrue(out["holders_source"].startswith("goplus_failed:"), out["holders_source"])

    def test_baseline_failure_named(self):
        def boom(tk):
            raise RuntimeError("rpc dead")
        OB._seed_baseline = boom
        out = OB.build_onboard("SLX", coingecko_id="solstice")
        self.assertTrue(out["ok"])
        self.assertFalse(out["baseline_seeded"])
        self.assertTrue(out["baseline_reason"].startswith("baseline_failed:"), out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
