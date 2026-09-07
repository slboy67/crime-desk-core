#!/usr/bin/env python3
"""SPEC 61 — stop geometry + entry-type guard in `size`, + thesis commit integration.

Two realized lessons as code:
  - ESPORTS -1R: a stop beyond the 2h-tape wick got out-wicked by the true-range leg-9
    wick. FIX: the proposed stop is beyond max(24h extreme, full-history window extreme).
  - BEAT 8.21-under-8.365 near-miss: a stop anchored to a PARTIAL window. FIX: full
    history wins (>8.3654, not the partial-window 7.96).

The pure scorers are fixture-driven (offline); the thesis commit integration is
monkeypatched so it never touches the network.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import size as SIZE          # noqa: E402
import thesis as TH          # noqa: E402


class TestProposeStop(unittest.TestCase):
    def test_esports_short_clears_the_true_0112_wick(self):
        # 24h extreme 0.112 IS the true wick; the 2h tape only saw a lower one.
        r = SIZE.propose_stop("SHORT", entry=0.105, h24=0.112, window_high=0.110)
        self.assertGreater(r["stop_proposed"], 0.112)
        self.assertEqual(r["cleared_wick"], 0.112)        # output NAMES the wick it cleared
        self.assertEqual(r["cleared_source"], "24h_extreme")

    def test_beat_short_uses_full_history_not_partial_window(self):
        # the partial window saw 7.96; full-history window-high is 8.3654 → that wins.
        r = SIZE.propose_stop("SHORT", entry=7.80, h24=7.96, window_high=8.3654)
        self.assertGreater(r["stop_proposed"], 8.3654)
        self.assertEqual(r["cleared_wick"], 8.3654)
        self.assertEqual(r["cleared_source"], "window_full_history")

    def test_long_clears_below_the_true_low(self):
        r = SIZE.propose_stop("LONG", entry=0.10, l24=0.092, window_low=0.090)
        self.assertLess(r["stop_proposed"], 0.090)
        self.assertEqual(r["cleared_wick"], 0.090)

    def test_stop_not_at_a_round_magnet(self):
        # wick 0.0985 + 1.5% buffer ≈ 0.09998 — lands ON the 0.10 round → nudge BEYOND it
        # (§7 — a stop AT the round is donated to the sweep).
        r = SIZE.propose_stop("SHORT", entry=0.097, h24=0.0985, window_high=0.0985,
                              round_magnets=[0.10])
        self.assertTrue(r["magnet_adjusted"])
        self.assertGreater(r["stop_proposed"], 0.10)


class TestEntryTypeGuard(unittest.TestCase):
    def test_chronic_squeezer_requires_post_breakdown(self):
        # ESPORTS: 8 squeeze legs over 60d = 1.33/10d > 1 → chronic.
        g = SIZE.entry_type_guard("SHORT", entry=0.111, squeezes_count=8, window_days=60,
                                  adverse_extreme=0.112)
        self.assertTrue(g["chronic_squeezer"])
        self.assertEqual(g["entry_type_required"], "post_breakdown")

    def test_chronic_retest_entry_warns(self):
        # entry sitting just under the high on a chronic squeezer = a retest-zone entry.
        g = SIZE.entry_type_guard("SHORT", entry=0.111, squeezes_count=8, window_days=60,
                                  adverse_extreme=0.112)
        self.assertIsNotNone(g["warning"])
        self.assertIn("post_breakdown", g["warning"])

    def test_non_chronic_no_requirement(self):
        g = SIZE.entry_type_guard("SHORT", entry=0.111, squeezes_count=2, window_days=60,
                                  adverse_extreme=0.112)
        self.assertFalse(g["chronic_squeezer"])
        self.assertIsNone(g["entry_type_required"])


class TestThesisCommitGeometry(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = (TH.WL_PATH, TH.LOCK_PATH, TH.ledger.LEDGER_PATH,
                      TH._prior_24h_range, TH._propose_stop_geometry)
        TH.WL_PATH = Path(self.tmp.name) / "watchlist.json"
        TH.LOCK_PATH = Path(self.tmp.name) / "wl.lock"
        TH.ledger.LEDGER_PATH = Path(self.tmp.name) / "ledger.jsonl"
        TH.WL_PATH.write_text('{"tokens": []}')
        TH._prior_24h_range = lambda tk: None                  # SPEC 54 check pinned off
        # SPEC 61: a committed SHORT stop 0.114 that sits UNDER the proposed 0.1137 (beyond
        # the true 0.112 wick)... here the committed stop is INSIDE → warning must attach.
        TH._propose_stop_geometry = lambda tk, d, e: {
            "stop_proposed": 0.1137, "cleared_wick": 0.112,
            "cleared_source": "24h_extreme", "direction": "SHORT"}

    def tearDown(self):
        (TH.WL_PATH, TH.LOCK_PATH, TH.ledger.LEDGER_PATH,
         TH._prior_24h_range, TH._propose_stop_geometry) = self._orig
        self.tmp.cleanup()

    def test_commit_attaches_geometry_warning_when_stop_inside_true_wick(self):
        th = {"direction": "SHORT", "entry_zone": [0.104, 0.106], "stop": 0.111,
              "tp": [0.09], "invalidation": {"level": "close above 0.112"}, "time_stop_h": 24}
        r = TH.build_thesis("commit", "ESPORTS", thesis=th)
        self.assertTrue(r["ok"])
        warns = " ".join(r.get("warnings", []))
        self.assertIn("0.112", warns)         # names the true wick it failed to clear
        self.assertIsNotNone(r.get("geometry_warning"))

    def test_commit_no_warning_when_stop_beyond_proposed(self):
        th = {"direction": "SHORT", "entry_zone": [0.104, 0.106], "stop": 0.120,
              "tp": [0.09], "invalidation": {"level": "close above 0.112"}, "time_stop_h": 24}
        r = TH.build_thesis("commit", "ESPORTS", thesis=th)
        self.assertTrue(r["ok"])
        self.assertIsNone(r.get("geometry_warning"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
