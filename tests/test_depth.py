#!/usr/bin/env python3
"""SPEC 27 — `depth` capability: live order-book shelf read.

Run:  python3 tests/test_depth.py

Surfaces the largest resting bid shelf below + ask wall above per venue (price + $size); on a thin
name where ~100 levels bunch near mid it marks `truncated:true` (NOT a false 'empty book below') and
exposes `deepest_level_seen`. Read-only intel. Network mocked — offline-deterministic.
"""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


DP = _load("depth")


def _bitget(bids, asks):
    return {"data": {"bids": [[str(p), str(q)] for p, q in bids],
                     "asks": [[str(p), str(q)] for p, q in asks]}}


class TestShelfDetection(unittest.TestCase):
    def setUp(self):
        self._f = DP.fetch

    def tearDown(self):
        DP.fetch = self._f

    def test_bid_shelf_and_ask_wall_surfaced(self):
        # a big bid block ~2% below mid, a big ask block ~2% above
        bids = [(0.171, 50)] + [(0.1675, 100000)] + [(0.167, 200), (0.166, 150)]
        asks = [(0.1711, 50)] + [(0.1745, 80000)] + [(0.175, 200)]
        DP.fetch = lambda url: _bitget(bids, asks)
        d = DP.build_depth("SKYAI", venue="bitget")
        v = d["venues"]["bitget"]
        self.assertTrue(v["available"])
        self.assertIsNotNone(v["bid_shelf_below"])
        self.assertIsNotNone(v["ask_wall_above"])
        # the dominant bid shelf is the 100k block near 0.1675
        self.assertAlmostEqual(v["bid_shelf_below"]["price"], 0.1675, places=2)
        self.assertGreater(v["bid_shelf_below"]["notional_usd"], v["ask_wall_above"]["notional_usd"] * 0.0)
        self.assertLess(v["bid_shelf_below"]["dist_pct"], 0)     # below mid
        self.assertGreater(v["ask_wall_above"]["dist_pct"], 0)   # above mid

    def test_truncated_when_levels_bunch_near_mid(self):
        # all 100 levels within ~0.5% of mid → can't see the requested ±5% band → truncated
        mid = 0.171
        bids = [(round(mid * (1 - i * 0.00005), 6), 100) for i in range(1, 60)]
        asks = [(round(mid * (1 + i * 0.00005), 6), 100) for i in range(1, 60)]
        DP.fetch = lambda url: _bitget(bids, asks)
        v = DP.build_depth("X", venue="bitget")["venues"]["bitget"]
        self.assertTrue(v["truncated"])
        self.assertIsNotNone(v["truncation_note"])
        self.assertIn("bid", v["deepest_level_seen"])
        self.assertLess(v["coverage_below_pct"], DP.WANT_PCT)

    def test_not_truncated_when_book_reaches_requested_band(self):
        # a book that spans ±6% of mid → reaches the ±5% want → not truncated
        mid = 0.171
        bids = [(round(mid * (1 - i * 0.001), 6), 100) for i in range(1, 65)]   # down to ~-6.4%
        asks = [(round(mid * (1 + i * 0.001), 6), 100) for i in range(1, 65)]
        DP.fetch = lambda url: _bitget(bids, asks)
        v = DP.build_depth("X", venue="bitget")["venues"]["bitget"]
        self.assertFalse(v["truncated"])

    def test_unavailable_venue_degrades_not_crashes(self):
        DP.fetch = lambda url: {"_error": "boom"}
        v = DP.build_depth("X", venue="bitget")["venues"]["bitget"]
        self.assertFalse(v["available"])
        self.assertIsNotNone(v["reason"])


class TestReadOnlyContract(unittest.TestCase):
    def test_output_has_no_verdict_fields(self):
        DP.fetch = lambda url: _bitget([(0.171, 100), (0.170, 100)], [(0.1711, 100), (0.172, 100)])
        d = DP.build_depth("X", venue="bitget")
        DP.fetch = DP.fetch  # noqa
        # read-only intel: no direction/verdict/score keys
        self.assertNotIn("verdict", d)
        self.assertNotIn("direction", d)
        self.assertIn("note", d)
        self.assertIn("read-only", d["note"])


class TestCapabilityRegistered(unittest.TestCase):
    def test_depth_in_capabilities_json(self):
        c = json.loads((ROOT / "capabilities.json").read_text())
        self.assertIn("depth", c)
        self.assertIn("depth.py", c["depth"]["invoke"])
        self.assertEqual(c["depth"]["status"], "native")


if __name__ == "__main__":
    unittest.main(verbosity=2)
