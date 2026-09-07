#!/usr/bin/env python3
"""SPEC 26 — Bitget added as a funding/OI/price source (primary venue for cluster names).

Run:  python3 tests/test_bitget_venue.py

Bitget OI ≈ Binance for these names and it's the desk's execution venue, but it was absent from
the cross-venue funding/OI/price reads → the §5 veto was blind to a venue carrying ~half the OI.
Now: regime_check funding/oi includes bitget; a cross_venue veto considers ALL venues incl. Bitget
(more-vetoing wins, %/4h-normalized); analyse's price chain falls back to bitget. Network mocked.
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


RC = _load("regime_check")
AN = _load("analyse")


class TestCrossVenueVeto(unittest.TestCase):
    def test_bitget_deepneg_binance_flat_vetoes_and_splits(self):
        cv = RC._cross_venue_veto({"binance": 0.005, "bybit": None, "aster": 0.01, "bitget": -1.2})
        self.assertTrue(cv["veto"])                       # Bitget deep-neg → §5 veto fires
        self.assertEqual(cv["min_venue"], "bitget")
        self.assertAlmostEqual(cv["min_4h"], -1.2, places=3)
        self.assertTrue(cv["funding_split"])              # venues straddle the −0.30 line

    def test_all_flat_no_veto(self):
        cv = RC._cross_venue_veto({"binance": 0.005, "bitget": 0.004})
        self.assertFalse(cv["veto"])
        self.assertFalse(cv["funding_split"])

    def test_all_unavailable_is_not_a_fabricated_veto(self):
        cv = RC._cross_venue_veto({"binance": None, "bitget": None})
        self.assertFalse(cv["veto"])
        self.assertIsNone(cv["min_4h"])


class TestBitgetParsers(unittest.TestCase):
    def test_bg_funding_newest_first(self):
        d = {"funding_hist": {"data": [{"fundingRate": "0.000095"}, {"fundingRate": "0.00005"}]}}
        self.assertEqual(RC._bg_funding(d), [0.000095, 0.00005])

    def test_bg_interval_hours_to_minutes(self):
        d = {"current": {"data": [{"fundingRate": "0.00005", "fundingRateInterval": "4"}]}}
        self.assertEqual(RC._bg_interval_min(d), 240)

    def test_bg_oi_usd_size_times_price(self):
        d = {"oi": {"data": {"openInterestList": [{"size": "126713977"}]}},
             "ticker": {"data": [{"lastPr": "0.16948"}]}}
        self.assertAlmostEqual(RC._bg_oi_usd(d), 126713977 * 0.16948, places=0)

    def test_live_funding_pct_bitget(self):
        self._f = RC.fetch
        RC.fetch = lambda url: {"data": [{"fundingRate": "-0.012"}]} if "current-fund-rate" in url else {}
        try:
            self.assertAlmostEqual(RC._live_funding_pct("bitget", "XUSDT"), -1.2, places=4)
        finally:
            RC.fetch = self._f


class TestBuildRegimeIncludesBitget(unittest.TestCase):
    def test_funding_oi_cross_venue_have_bitget(self):
        self._f = RC.fetch
        self._lf = RC._live_funding_pct

        def fake(url):
            if "history-fund-rate" in url:
                return {"data": [{"fundingRate": "0.00005", "fundingTime": "1780531200000"},
                                 {"fundingRate": "0.00005", "fundingTime": "1780516800000"}]}
            if "open-interest" in url and "bitget" in url:
                return {"data": {"openInterestList": [{"size": "126713977"}]}}
            if "bitget" in url and "ticker" in url:
                return {"data": [{"lastPr": "0.16948"}]}
            return {"_error": "x"}    # other venues empty → degrade, don't crash
        RC.fetch = fake
        RC._live_funding_pct = lambda v, s: -1.2 if v == "bitget" else None
        try:
            r = RC.build_regime("SKYAI")
        finally:
            RC.fetch, RC._live_funding_pct = self._f, self._lf
        self.assertIn("bitget", r["funding"])                 # bitget funding block present
        self.assertIn("bitget", r["oi_24h"])                  # bitget OI present
        self.assertAlmostEqual(r["oi_24h"]["bitget"]["current_usd"], 126713977 * 0.16948, places=0)
        self.assertIn("cross_venue", r)
        self.assertEqual(r["cross_venue"]["min_venue"], "bitget")
        self.assertTrue(r["cross_venue"]["veto"])             # bitget deep-neg vetoes cross-venue


class TestAnalysePriceChainHasBitget(unittest.TestCase):
    def test_price_falls_back_to_bitget(self):
        self._f = AN.RC.fetch

        def f(url):
            if "binance" in url or "bybit" in url:
                return {}                                     # binance + bybit empty
            if "bitget" in url and "ticker" in url:
                return {"data": [{"lastPr": "0.16948"}]}
            return {}
        AN.RC.fetch = f
        try:
            self.assertEqual(AN._cross_venue_price("SKYAI"), (0.16948, "bitget"))
        finally:
            AN.RC.fetch = self._f


if __name__ == "__main__":
    unittest.main(verbosity=2)
