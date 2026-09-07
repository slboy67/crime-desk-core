#!/usr/bin/env python3
"""SPEC 19 — regime_check funding HISTORY must drop the 0.005% venue-floor placeholder.

Run:  python3 tests/test_funding_history_floor.py

SPEC 18 made each venue's funding LATEST the live predicted rate, but the history
series (rendered `history`, the z-score SAMPLE, and `regime_hint`'s trend windows)
still came from the settled endpoint, which returns the 0.005% floor for low-activity
intervals. On EDEN that read as "flat then spiked negative" when the real trajectory
was deep-neg COOLING toward flat. These tests are pure/offline (no network): they
exercise `_venue_funding_block` / `regime_hint` directly.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("regime_check", ROOT / "capabilities" / "regime_check.py")
RC = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RC)


class TestFloorHistory(unittest.TestCase):
    def test_eden_cooling_not_spike(self):
        # latest-first; the 0.005s are floor placeholders, the reals are a deep-neg uptrend (cooling)
        block = RC._venue_funding_block([-0.39, 0.005, 0.005, 0.005, -0.8, -1.2, -1.7])
        # floors rendered null in history — no bare 0.005 surfaced as if real (mirror SPEC 10)
        self.assertIsNone(block["history"][1])
        self.assertIsNone(block["history"][2])
        self.assertIsNone(block["history"][3])
        self.assertNotIn(0.005, [h for h in block["history"] if h is not None])
        # regime = deep-neg COOLING toward flat, NOT a false "flat then spiked"/flip
        self.assertIn("cooling toward flat", block["regime"].lower())
        self.assertNotIn("flip", block["regime"].lower())

    def test_zscore_ignores_floors(self):
        negs = [-0.6 - 0.1 * i for i in range(10)]           # 10 real settled points
        block = RC._venue_funding_block([-0.5, 0.005, 0.005] + negs)
        self.assertEqual(block["z"]["n"], 10)                # 2 floors excluded from the sample
        self.assertIsNotNone(block["z"]["z"])

    def test_all_floor_is_genuine_flat(self):
        block = RC._venue_funding_block([0.005] * 12)
        self.assertTrue(block["all_floor"])                  # SPEC 13/10 distinction kept
        self.assertIn("flat", block["regime"].lower())       # genuine ~0%, not "unknown"
        self.assertEqual(block["latest"], 0.005)
        self.assertTrue(all(h is None for h in block["history"]))

    def test_too_few_real_points_insufficient(self):
        block = RC._venue_funding_block([-0.39, 0.005, 0.005, 0.005, 0.005, 0.005])
        self.assertIn("insufficient real history", block["regime"].lower())  # no false trend call

    def test_regime_hint_negative_cooling_branch(self):
        msg = RC.regime_hint(-0.39, [-0.39, -0.8, -1.2, -1.7])
        self.assertIn("cooling toward flat", msg.lower())

    def test_real_deepening_still_detected(self):
        # floors removed must NOT turn a genuine deepening into cooling
        block = RC._venue_funding_block([-1.7, -1.2, -0.8, -0.4, -0.39])
        self.assertIn("deepening", block["regime"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
