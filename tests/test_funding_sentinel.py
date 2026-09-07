#!/usr/bin/env python3
"""SPEC 10 (+ superseded by SPEC 13) — floor handling under cross-venue funding.

Run:  python3 tests/test_funding_sentinel.py

SPEC 13 evolved SPEC 10: a single venue's floor must NOT mask another venue's real
deep-neg (use the real one); floor on ALL covered venues is a genuine ~0% flat read
(not UNAVAILABLE). FUNDING_UNAVAILABLE is reserved for no venue returning funding.
Venue fetches are monkeypatched — offline, deterministic.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("regime_flip", ROOT / "capabilities" / "regime_flip.py")
RF = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RF)
FLOOR = RF.FUNDING_FLOOR_RAW   # 0.00005


def venue(name, raw, iv=240, oi=1.0, price=1.0, vol=50.0):
    return {"venue": name, "funding_raw": raw, "interval_min": iv,
            "price": price, "chg24": 1.0, "vol_m": vol, "oi": oi}


class _VenuePatch(unittest.TestCase):
    def patch(self, bybit, binance):
        # SPEC 44: the no-real-read path now lazily consults the secondaries — pin
        # them dark so the all-floor cases stay offline-deterministic
        self._orig = (RF._venue_bybit, RF._venue_binance,
                      RF._venue_bitget, RF._venue_aster)
        RF._venue_bybit = lambda s: bybit
        RF._venue_binance = lambda s: binance
        RF._venue_bitget = lambda s: None
        RF._venue_aster = lambda s: None

    def tearDown(self):
        if hasattr(self, "_orig"):
            (RF._venue_bybit, RF._venue_binance,
             RF._venue_bitget, RF._venue_aster) = self._orig


class TestFloor(_VenuePatch):
    def test_is_floor(self):
        self.assertTrue(RF._is_floor(FLOOR))
        self.assertTrue(RF._is_floor(-FLOOR))
        self.assertFalse(RF._is_floor(0.00012))
        self.assertFalse(RF._is_floor(None))

    def test_all_floor_is_genuine_flat_not_unavailable(self):
        # SPEC 13: floor on every covered venue = real ~0% flat (not null/UNAVAILABLE)
        self.patch(venue("bybit", FLOOR, iv=60), venue("binance", FLOOR, iv=240))
        live = RF.live_perp("BILL")
        self.assertTrue(live["all_floor"])
        self.assertFalse(live["funding_stale"])
        self.assertIsNotNone(live["funding_4h"])           # a value, not None
        sev, tag, note = RF.classify({"ticker": "BILL", "state": "watch"}, live)
        self.assertNotEqual(tag, "FUNDING_UNAVAILABLE")    # flat read, not unavailable
        self.assertIn("floor", note.lower())

    def test_classify_unavailable_only_when_no_funding(self):
        # defensive: a live dict with no funding_4h at all → FUNDING_UNAVAILABLE
        live = {"price": 1.0, "vol_m": 50.0, "funding_4h": None, "funding_pi": None, "interval_min": 240}
        sev, tag, note = RF.classify({"ticker": "X", "state": "short"}, live)
        self.assertEqual(tag, "FUNDING_UNAVAILABLE")

    def test_no_venue_returns_none(self):
        self.patch(None, None)
        self.assertIsNone(RF.live_perp("DELISTED"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
