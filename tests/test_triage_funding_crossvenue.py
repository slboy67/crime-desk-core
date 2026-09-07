#!/usr/bin/env python3
"""SPEC-108 — triage board funding_pi must be cross-venue (most-extreme non-floor print),
not single-venue (Bybit-preferred/Binance-fallback). Offline/deterministic: builds synthetic
per-venue data dicts, no network.

Run:  python3 tests/test_triage_funding_crossvenue.py
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("triage", ROOT / "capabilities" / "triage.py")
triage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(triage)

FLOOR = 0.005   # pct-space floor sentinel (0.00005 raw * 100)


def venue_data(**kw):
    """Build a synthetic venue_pull()-shaped dict; kw maps venue -> fund_latest pct."""
    return {v: {"fund_latest": val} for v, val in kw.items()}


class TestSelectFunding(unittest.TestCase):
    def test_most_extreme_nonfloor_wins(self):
        data = venue_data(bybit=-0.10, binance=-0.501, bitget=-0.452)
        f_pi, venue, rng, suspect = triage._select_funding(data)
        self.assertAlmostEqual(f_pi, -0.501)
        self.assertEqual(venue, "binance")
        self.assertEqual(rng, [-0.501, -0.10])
        self.assertFalse(suspect)

    def test_floor_never_wins(self):
        data = venue_data(bybit=FLOOR, binance=-0.706)
        f_pi, venue, rng, suspect = triage._select_funding(data)
        self.assertAlmostEqual(f_pi, -0.706)
        self.assertEqual(venue, "binance")
        self.assertFalse(suspect)

    def test_all_floored_is_suspect(self):
        data = venue_data(bybit=FLOOR, binance=FLOOR, bitget=-FLOOR)
        f_pi, venue, rng, suspect = triage._select_funding(data)
        self.assertIsNone(f_pi)
        self.assertIsNone(venue)
        self.assertIsNone(rng)
        self.assertTrue(suspect)

    def test_genuinely_flat_prints_near_zero_no_false_extreme(self):
        data = venue_data(bybit=-0.01, binance=0.008, bitget=-0.006)
        f_pi, venue, rng, suspect = triage._select_funding(data)
        self.assertAlmostEqual(f_pi, -0.01)
        self.assertFalse(suspect)
        self.assertEqual(rng, [-0.01, 0.008])

    def test_no_venue_data_at_all(self):
        f_pi, venue, rng, suspect = triage._select_funding({})
        self.assertIsNone(f_pi)
        self.assertIsNone(venue)
        self.assertIsNone(rng)
        self.assertFalse(suspect)


class TestSelectFundingIntervalNormalization(unittest.TestCase):
    """SPEC-112: a 1h-interval venue must be normalized to %/4h-equivalent before the
    most-extreme comparison and before render — never compared/rendered raw next to a
    4h venue's print (SLX: aster -0.0852/1h read in the same column as bitget -0.6405/4h)."""

    def test_1h_venue_normalized_before_comparison(self):
        data = {
            "aster": {"fund_latest": -0.085, "interval_min": 60},
            "bitget": {"fund_latest": -0.29, "interval_min": 240},
        }
        f_pi, venue, rng, suspect = triage._select_funding(data)
        self.assertAlmostEqual(f_pi, -0.34)         # -0.085 * 240/60
        self.assertEqual(venue, "aster")
        self.assertEqual(rng, [-0.34, -0.29])
        self.assertFalse(suspect)

    def test_all_4h_venue_set_unchanged_behavior(self):
        # No interval_min supplied at all -> defaults to 240 (4h), identical to pre-SPEC-112.
        data = venue_data(bybit=-0.10, binance=-0.501, bitget=-0.452)
        f_pi, venue, rng, suspect = triage._select_funding(data)
        self.assertAlmostEqual(f_pi, -0.501)
        self.assertEqual(venue, "binance")
        self.assertEqual(rng, [-0.501, -0.10])
        self.assertFalse(suspect)

    def test_floor_dropped_before_normalization_on_1h_venue(self):
        # A raw floor (0.005) on a 1h venue would scale to 0.02 if normalized first, clearing
        # the floor band and reading as a false real datum. Floor check must run pre-normalize.
        data = {
            "aster": {"fund_latest": FLOOR, "interval_min": 60},
            "binance": {"fund_latest": -0.706, "interval_min": 240},
        }
        f_pi, venue, rng, suspect = triage._select_funding(data)
        self.assertAlmostEqual(f_pi, -0.706)
        self.assertEqual(venue, "binance")
        self.assertFalse(suspect)


class TestBuildRowContract(unittest.TestCase):
    def test_build_row_carries_funding_venue_and_range(self):
        data = {
            "binance": {"px": 1.0, "ch24": 1.0, "qvol24": 5e7, "lo24": 0.9, "hi24": 1.1,
                        "fund_latest": -0.501},
            "bybit": {"fund_latest": -0.10},
            "bitget": {"fund_latest": -0.452},
        }
        row = triage._build_row(data, {"ticker": "SLX", "category": "A", "state": "watch"})
        self.assertEqual(row["funding_pi"], -0.501)
        self.assertEqual(row["funding_venue"], "binance")
        self.assertEqual(row["funding_range"], [-0.501, -0.10])
        self.assertFalse(row["funding_suspect"])

    def test_build_row_all_floor_sets_suspect_null_pi(self):
        data = {
            "binance": {"px": 1.0, "ch24": 1.0, "qvol24": 5e7, "lo24": 0.9, "hi24": 1.1,
                        "fund_latest": FLOOR},
            "bybit": {"fund_latest": FLOOR},
        }
        row = triage._build_row(data, {"ticker": "X", "category": "?", "state": ""})
        self.assertIsNone(row["funding_pi"])
        self.assertTrue(row["funding_suspect"])

    def test_build_row_exposes_raw_pi_and_interval_for_winning_venue(self):
        # SPEC-112: funding_pi is 4h-normalized; funding_raw_pi/funding_interval_min carry
        # the un-normalized winning-venue print, same convention as brief.
        data = {
            "binance": {"px": 1.0, "ch24": 1.0, "qvol24": 5e7, "lo24": 0.9, "hi24": 1.1,
                        "fund_latest": -0.10, "interval_min": 240},
            "aster": {"fund_latest": -0.085, "interval_min": 60},
            "bitget": {"fund_latest": -0.29, "interval_min": 240},
        }
        row = triage._build_row(data, {"ticker": "SLX", "category": "A", "state": "watch"})
        self.assertAlmostEqual(row["funding_pi"], -0.34)
        self.assertEqual(row["funding_venue"], "aster")
        self.assertAlmostEqual(row["funding_raw_pi"], -0.085)
        self.assertEqual(row["funding_interval_min"], 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
