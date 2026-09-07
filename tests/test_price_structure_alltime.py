#!/usr/bin/env python3
"""SPEC 57 — price_structure: never label a window-capped high "ath".

Run:  python3 tests/test_price_structure_alltime.py

Live near-miss: FOLKS returned `ath: 2.613` from the default 90d window; the contract's
true ATH is $47.00 — a −98% prior cycle the window silently erased, almost published
externally. Contract under test:
  - the full listing history is fetched (one 1d-kline call, limit 1500) regardless of
    the requested window; `listing_date`, `ath_alltime`, `ath_alltime_date`,
    `off_ath_alltime_pct` always present;
  - window-scoped fields are honestly named (`window_high`/`window_low`/`window_days`)
    while ALL existing keys are preserved (additive — no caller breakage);
  - `prior_cycle` true when the all-time high predates the window AND price sits
    < −80% off it (a completed boom-bust the window can't see);
  - a token younger than the window: window == lifetime, prior_cycle false.
fetch monkeypatched — offline, deterministic.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("price_structure", ROOT / "capabilities" / "price_structure.py")
PS = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PS)

DAY = 86_400_000
T0 = 1_762_387_200_000   # 2025-11-06 (FOLKS listing)


def kline(i, h, l=None, c=None):
    o = c if c is not None else h * 0.9
    return [T0 + i * DAY, str(o), str(h), str(l if l is not None else h * 0.8),
            str(c if c is not None else h * 0.9), "1000", 0, "5000000"]


def folks_history(n=400):
    """Listing pump to $47 at day 38 (2025-12-14), bust to ~0.8, window high 2.613."""
    ks = []
    for i in range(n):
        if i == 38:
            ks.append(kline(i, 47.0, 30.0, 40.0))
        elif i < 60:
            ks.append(kline(i, 10.0, 5.0, 8.0))
        elif i == n - 45:                       # inside the 90d window
            ks.append(kline(i, 2.613, 1.5, 2.0))
        else:
            ks.append(kline(i, 1.0, 0.7, 0.83))
    return ks


class TestAlltime(unittest.TestCase):
    def setUp(self):
        self._fetch = PS.fetch
        self.urls = []
        self.fixture = folks_history()
        def fetch(url):
            self.urls.append(url)
            return list(self.fixture)
        PS.fetch = fetch

    def tearDown(self):
        PS.fetch = self._fetch

    def test_window_capped_high_not_called_ath_alltime(self):
        s = PS.build_structure("FOLKS", days=90)
        self.assertEqual(s["ath"], 2.613)               # window key preserved (compat)
        self.assertEqual(s["window_high"], 2.613)       # honest name
        self.assertEqual(s["window_days"], 90)
        self.assertEqual(s["ath_alltime"], 47.0)
        self.assertEqual(s["ath_alltime_date"], "2025-12-14")
        self.assertEqual(s["listing_date"], "2025-11-06")
        self.assertLess(s["off_ath_alltime_pct"], -98.0)

    def test_prior_cycle_flagged(self):
        s = PS.build_structure("FOLKS", days=90)
        self.assertTrue(s["prior_cycle"])               # boom-bust the window can't see

    def test_full_history_fetched_once(self):
        PS.build_structure("FOLKS", days=90)
        self.assertEqual(len(self.urls), 1)
        self.assertIn("limit=1500", self.urls[0])       # full history, not the window

    def test_existing_keys_preserved(self):
        s = PS.build_structure("FOLKS", days=90)
        for k in ("ath", "ath_date", "days_since_ath", "atl", "range_pos", "off_ath_pct",
                  "squeezes", "squeeze_pattern", "vol_profile", "structure", "compression",
                  "age_days", "young_listing", "days_available"):
            self.assertIn(k, s)
        # window stats computed on the WINDOW, not the lifetime
        self.assertEqual(s["days_available"], 90)

    def test_young_token_window_equals_lifetime(self):
        self.fixture = folks_history(20)                # 20-day-old listing
        s = PS.build_structure("YOUNG", days=90)
        self.assertEqual(s["ath"], s["ath_alltime"])
        self.assertFalse(s["prior_cycle"])
        self.assertTrue(s["young_listing"])

    def test_range_3d_pct_present(self):
        # SPEC-96: the recent 3-day high/low range (%) feeds the location/basing classifier.
        s = PS.build_structure("FOLKS", days=90)
        self.assertIn("range_3d_pct", s)
        self.assertIsInstance(s["range_3d_pct"], float)
        self.assertGreaterEqual(s["range_3d_pct"], 0.0)

    def test_vol_daily_m_last_ten_days_oldest_to_newest(self):
        # SPEC-138: recent-volume-slope input for faded_bounce — last up-to-10 days,
        # qv (candle[7]) is already $ quote volume, converted to $M.
        s = PS.build_structure("FOLKS", days=90)
        self.assertEqual(len(s["vol_daily_m"]), 10)
        self.assertEqual(s["vol_daily_m"][-1], 5.0)          # kline()'s fixed qv=5_000_000

    def test_recent_ath_inside_window_no_prior_cycle(self):
        # deep off-ATH but the ATH printed inside the window → not a prior cycle
        ks = folks_history(100)
        ks[95] = kline(95, 47.0, 30.0, 40.0)            # ATH 5 days ago
        ks[38] = kline(38, 10.0, 5.0, 8.0)
        self.fixture = ks
        s = PS.build_structure("X", days=90)
        self.assertFalse(s["prior_cycle"])

    def test_last_swing_higher_high_true_when_last_candle_makes_a_new_high(self):
        # SPEC-166: last two candles of the last-7 window climbing -> still-live squeezer
        ks = folks_history(100)
        ks[-1] = kline(99, 5.0, 4.0, 4.8)               # today's high > yesterday's
        ks[-2] = kline(98, 4.0, 3.0, 3.8)
        self.fixture = ks
        s = PS.build_structure("X", days=90)
        self.assertTrue(s["structure"]["last_swing_higher_high"])

    def test_last_swing_higher_high_false_when_last_candle_rolls_over(self):
        ks = folks_history(100)
        ks[-1] = kline(99, 3.0, 2.0, 2.8)               # today's high < yesterday's
        ks[-2] = kline(98, 4.0, 3.0, 3.8)
        self.fixture = ks
        s = PS.build_structure("X", days=90)
        self.assertFalse(s["structure"]["last_swing_higher_high"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
