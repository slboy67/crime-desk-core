#!/usr/bin/env python3
"""SPEC-193 — `scan`'s `render:"compact"` mode.

scan's boards (default funding scan, scout/faded_bounce/oi_surge) are already
candidate-lean — the one unbounded tail is `excluded`/`dropped_below_top`, which can
run to the size of the scanned universe. `compact_scan_output` caps those to a
count + 3-item sample; everything else passes through unchanged.
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


SC = _load("scan")


class TestCompactScanOutput(unittest.TestCase):
    def test_long_excluded_list_capped_with_count_and_sample(self):
        board = {"mode": "faded_bounce", "candidates": [{"ticker": "OP"}],
                "excluded": [{"ticker": f"T{i}", "reason": "no bounce"} for i in range(50)]}
        c = SC.compact_scan_output(board)
        self.assertEqual(c["excluded"]["n"], 50)
        self.assertEqual(len(c["excluded"]["sample"]), 3)
        self.assertEqual(c["candidates"], board["candidates"])

    def test_short_excluded_list_passes_through_unchanged(self):
        board = {"excluded": [{"ticker": "A"}, {"ticker": "B"}]}
        c = SC.compact_scan_output(board)
        self.assertEqual(c["excluded"], board["excluded"])

    def test_dropped_below_top_capped(self):
        board = {"dropped_below_top": [f"T{i}" for i in range(30)]}
        c = SC.compact_scan_output(board)
        self.assertEqual(c["dropped_below_top"]["n"], 30)

    def test_no_excluded_key_no_crash(self):
        board = {"universe": 40, "longs": [], "shorts": []}
        c = SC.compact_scan_output(board)
        self.assertEqual(c, board)

    def test_default_scan_board_unchanged_shape(self):
        scan = {"universe": 40, "min_vol_m": 10.0, "thresh": 0.1, "side": "both",
               "longs": [{"ticker": "X", "funding_4h": -0.5}], "shorts": []}
        c = SC.compact_scan_output(scan)
        self.assertEqual(c, scan)
        self.assertLessEqual(len(json.dumps(c)), len(json.dumps(scan)) + 5)


if __name__ == "__main__":
    unittest.main()
