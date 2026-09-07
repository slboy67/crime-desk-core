#!/usr/bin/env python3
"""SPEC 25 — `analyse` must not silently return price:null / funding_4h:null.

Run:  python3 tests/test_analyse_perp_backfill.py

perp_analyser sources price+funding from BYBIT ONLY → both null on thin names (SKYAI) while OI
(Binance) populates, silently breaking the verdict (false "funding NEUTRAL → WAIT"). analyse now
backfills from the hardened cross-venue path (binance/bybit/aster, SPEC 18) and degrades EXPLICITLY
(*_unavailable flags) when ALL venues fail — and converge treats unavailable as UNKNOWN, not neutral.
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


AN = _load("analyse")


class TestHardenedFunding(unittest.TestCase):
    def setUp(self):
        self._lf = AN.RC._live_funding_pct
        self._iv = AN._funding_interval_min
        AN._funding_interval_min = lambda v, s: 240          # 4h everywhere

    def tearDown(self):
        AN.RC._live_funding_pct, AN._funding_interval_min = self._lf, self._iv

    def test_bybit_null_falls_back_to_binance(self):
        AN.RC._live_funding_pct = lambda v, s: {"bybit": None, "binance": 0.005, "aster": 0.00171}[v]
        fr4, venue, _ = AN._hardened_funding_4h("SKYAI")
        self.assertIsNotNone(fr4)                            # NOT null — Binance/Aster cover it
        self.assertEqual(venue, "aster")                     # the REAL print (binance 0.005 = floor, SPEC-71)

    def test_more_vetoing_venue_wins(self):
        # a deep-neg on ONE venue must win (SPEC 13 cross-venue veto)
        AN.RC._live_funding_pct = lambda v, s: {"bybit": -1.5, "binance": 0.005, "aster": None}[v]
        fr4, venue, _ = AN._hardened_funding_4h("X")
        self.assertEqual(venue, "bybit")
        self.assertAlmostEqual(fr4, -1.5, places=3)          # 240/240 interval → unchanged

    def test_interval_normalizes_to_4h(self):
        AN._funding_interval_min = lambda v, s: 480          # 8h interval
        AN.RC._live_funding_pct = lambda v, s: -0.2 if v == "binance" else None
        fr4, venue, _ = AN._hardened_funding_4h("X")
        self.assertAlmostEqual(fr4, -0.1, places=4)          # -0.2 %/8h → -0.1 %/4h

    def test_all_venues_fail_returns_none(self):
        AN.RC._live_funding_pct = lambda v, s: None
        self.assertEqual(AN._hardened_funding_4h("X"), (None, None, None))


class TestCrossVenuePrice(unittest.TestCase):
    def setUp(self):
        self._f = AN.RC.fetch

    def tearDown(self):
        AN.RC.fetch = self._f

    def test_binance_first(self):
        AN.RC.fetch = lambda url: {"price": "0.16997"} if "binance" in url and "ticker/price" in url else {}
        self.assertEqual(AN._cross_venue_price("SKYAI"), (0.16997, "binance"))

    def test_falls_back_to_bybit_when_binance_empty(self):
        def f(url):
            if "binance" in url:
                return {}                                    # binance empty
            if "bybit" in url:
                return {"result": {"list": [{"lastPrice": "0.17"}]}}
            return {}
        AN.RC.fetch = f
        self.assertEqual(AN._cross_venue_price("SKYAI"), (0.17, "bybit"))

    def test_all_fail_returns_none(self):
        AN.RC.fetch = lambda url: {}
        self.assertEqual(AN._cross_venue_price("X"), (None, None))


class TestBackfillAndDegrade(unittest.TestCase):
    def test_backfill_patches_null_metrics(self):
        self._o = (AN._hardened_funding_4h, AN._cross_venue_price)
        AN._hardened_funding_4h = lambda t: (0.005, "binance", "all_floor")
        AN._cross_venue_price = lambda t: (0.16997, "binance")
        try:
            perp = {"score": 0, "metrics": {"fr_4h": None, "price": None, "oi_chg": -2}}
            out = AN._backfill_perp(perp, "SKYAI")["metrics"]
        finally:
            AN._hardened_funding_4h, AN._cross_venue_price = self._o
        self.assertEqual(out["fr_4h"], 0.005)
        self.assertEqual(out["price"], 0.16997)
        self.assertTrue(out["funding_backfilled"] and out["price_backfilled"])
        self.assertNotIn("funding_unavailable", out)         # had a value → not flagged unavailable
        self.assertEqual(out["oi_chg"], -2)                  # untouched

    def test_total_failure_sets_explicit_flags_not_null(self):
        self._o = (AN._hardened_funding_4h, AN._cross_venue_price)
        AN._hardened_funding_4h = lambda t: (None, None, None)
        AN._cross_venue_price = lambda t: (None, None)
        try:
            perp = {"score": 0, "metrics": {"fr_4h": None, "price": None}}
            out = AN._backfill_perp(perp, "X")["metrics"]
        finally:
            AN._hardened_funding_4h, AN._cross_venue_price = self._o
        self.assertTrue(out["funding_unavailable"])
        self.assertTrue(out["price_unavailable"])


class TestConvergeUnavailableNotNeutral(unittest.TestCase):
    def _onch_bear(self):
        return {"result": {"score": -30, "phase": "EXECUTING (mega-safe nonce fired)"}}

    def test_funding_unavailable_is_not_false_wait(self):
        perp = {"score": 0, "metrics": {"fr_4h": None, "funding_unavailable": True, "turnover": 50_000_000}}
        direction, tier, notes, *_ = AN.converge(perp, self._onch_bear())
        self.assertEqual(direction, "WATCH (funding unavailable)")
        joined = " ".join(notes)
        self.assertIn("FUNDING UNAVAILABLE", joined)
        self.assertNotIn("funding NEUTRAL", joined)          # the old false claim is gone

    def test_real_flat_funding_still_waits(self):
        # genuinely flat funding (not unavailable) keeps the existing neutral-WAIT behavior
        perp = {"score": 0, "metrics": {"fr_4h": 0.005, "turnover": 50_000_000}}
        direction, tier, notes, *_ = AN.converge(perp, self._onch_bear())
        self.assertEqual(direction, "SHORT (loading)")
        self.assertIn("funding NEUTRAL", " ".join(notes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
