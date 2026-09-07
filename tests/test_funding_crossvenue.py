#!/usr/bin/env python3
"""SPEC 13 — funding read CROSS-VENUE (Binance + Bybit), not single-source.

Run:  python3 tests/test_funding_crossvenue.py

A single venue's floor must not mask another venue's real deep-neg; the more-vetoing
real venue wins; venues straddling the −0.30%/4h line flag funding_split. Venue fetches
monkeypatched — offline, deterministic.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("regime_flip", ROOT / "capabilities" / "regime_flip.py")
RF = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RF)
FLOOR = RF.FUNDING_FLOOR_RAW


def venue(name, raw, iv=240, oi=1.0, vol=50.0):
    return {"venue": name, "funding_raw": raw, "interval_min": iv,
            "price": 1.0, "chg24": 1.0, "vol_m": vol, "oi": oi}


class TestCrossVenue(unittest.TestCase):
    def patch(self, bybit, binance):
        self._orig = (RF._venue_bybit, RF._venue_binance)
        RF._venue_bybit = lambda s: bybit
        RF._venue_binance = lambda s: binance

    def tearDown(self):
        RF._venue_bybit, RF._venue_binance = self._orig

    def test_real_venue_wins_over_floor(self):
        # Bybit floors, Binance has a real value (H/PIEVERSE case) → use Binance, not UNAVAILABLE
        self.patch(venue("bybit", FLOOR, iv=60),
                   venue("binance", 0.00037, iv=240, oi=1e9))   # +0.037%/4h
        live = RF.live_perp("H")
        self.assertEqual(live["venue"], "binance")
        self.assertFalse(live["all_floor"])
        self.assertAlmostEqual(live["funding_4h"], 0.037, places=3)
        sev, tag, note = RF.classify({"ticker": "H", "state": "watch"}, live)
        self.assertNotEqual(tag, "FUNDING_UNAVAILABLE")

    def test_floor_does_not_mask_deep_neg(self):
        # LAB case: Bybit flat (floor), Binance deep-neg dominant → veto reflects Binance
        self.patch(venue("bybit", FLOOR, iv=60, oi=1.0),
                   venue("binance", -0.003820, iv=60, oi=6.4e6))   # ≈ −1.53%/4h
        live = RF.live_perp("LAB")
        self.assertEqual(live["venue"], "binance")
        self.assertLessEqual(live["funding_4h"], RF.DEEP_NEG)       # vetoed on the real venue
        self.assertTrue(live["funding_split"])                      # venues straddle the line
        self.assertIn("SHORT vetoed", RF.short_veto_flag(live))

    def test_more_vetoing_wins_when_both_real(self):
        self.patch(venue("bybit", -0.001, iv=240),    # −0.10%/4h
                   venue("binance", -0.015, iv=240, oi=1e9))   # −1.50%/4h
        live = RF.live_perp("X")
        self.assertAlmostEqual(live["funding_4h"], -1.50, places=2)
        self.assertTrue(live["funding_split"])         # -1.5 ≤ -0.30 < -0.10

    def test_no_split_when_both_same_side(self):
        self.patch(venue("bybit", 0.0010, iv=240), venue("binance", 0.0020, iv=240, oi=1e9))
        live = RF.live_perp("X")
        self.assertFalse(live["funding_split"])
        self.assertAlmostEqual(live["funding_4h"], 0.10, places=2)  # more-vetoing = the smaller positive

    def test_per_venue_breakdown_present(self):
        self.patch(venue("bybit", FLOOR, iv=60), venue("binance", -0.003, iv=60, oi=1e9))
        live = RF.live_perp("LAB")
        self.assertIn("bybit", live["venues"])
        self.assertIn("binance", live["venues"])
        self.assertTrue(live["venues"]["bybit"]["is_floor"])
        self.assertFalse(live["venues"]["binance"]["is_floor"])

    def test_classify_veto_note_flags_split(self):
        self.patch(venue("bybit", FLOOR, iv=60), venue("binance", -0.004, iv=60, oi=1e9))
        live = RF.live_perp("LAB")
        sev, tag, note = RF.classify({"ticker": "LAB", "state": "SHORT distribution top"}, live)
        self.assertIn("VETOED", note)
        self.assertIn("VENUE-SPLIT", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
