#!/usr/bin/env python3
"""SPEC-79 — price-leg must model SELL-THE-BREAKDOWN short entries, not just fade-the-rally.

SPEC-78 made entry detection a range-intersection (a breakdown SHORT whose price drifts UP
no longer reads "entered"), but it still evaluates the stop/TP against the AGGREGATE window
high/low. A breakdown SHORT falls from ABOVE its stop DOWN into the entry zone — so the
window's pre-entry high sits above the stop, and the moment the zone actually prints the leg
read `stop_breached` off that pre-entry high → false BREAKS instead of TRIGGERS.

Live: RIVER (2026-06-17) SHORT, entry [4.15,4.32] (arm = break of the 90d ATL), stop 4.80,
price 5.04. BSB SHORT, entry [0.44,0.475], price 0.537. Both are breakdown shorts the board
could not represent.

Fix under test: when the thesis goes live THIS window (PENDING→entered, no prior entered_ts)
and candles resolve the entry bar, the stop/TP are evaluated only from the entry bar forward.
The pre-entry drift no longer counts as a breach. Without candles the order is unresolvable →
SPEC-78's conservative full-window read is kept.

Offline-deterministic: eval_price_leg is pure; the board tests monkeypatch the range fetch.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import classify as CL


def candle(ts, high, low):
    return {"ts": ts, "high": high, "low": low}


def rng(candles, window="klines:binance:test"):
    """Aggregate the candle list into the {high, low, candles} window the leg consumes."""
    return {"high": max(c["high"] for c in candles),
            "low": min(c["low"] for c in candles),
            "window": window, "candles": candles}


def _short(zone, stop=4.80, tps=(4.0,), status="PENDING", entered_ts=None):
    th = {"direction": "SHORT", "status": status, "setup": "stage5",
          "stop": stop, "tps": list(tps), "entry_zone": list(zone)}
    if entered_ts:
        th["entered_ts"] = entered_ts
    return th


# ── live perp blocks that hold the funding leg quiet so only the price leg can fire ──
LIVE_QUIET = {
    "venue": "binance", "primary_venue": "binance",
    "price": 5.04, "chg24": -3.0, "vol_m": 40.0, "oi": 1e6,
    "funding_pi": -0.05, "funding_4h": -0.10, "interval_min": 480,
    "funding_stale": False, "all_floor": False, "funding_split": False,
}


class BreakdownShortEntry(unittest.TestCase):
    """Pure eval_price_leg — the SHORT entry must fire on the correct side."""

    # DoD: never trades down → CONFIRMS (verdict None), entered_zone False
    def test_breakdown_never_enters_is_silent(self):
        c = [candle(1, 5.06, 5.04), candle(2, 5.05, 5.04), candle(3, 5.07, 5.05)]
        leg = CL.eval_price_leg(_short([4.15, 4.32]), "SHORT", rng(c))
        self.assertFalse(leg["entered_zone"])
        self.assertIsNone(leg["verdict"])
        self.assertIsNone(leg["stop_breached"])      # not a live position → stop not watched

    # DoD: price falls THROUGH into the zone (low 4.30) → TRIGGERS / entered, NOT BREAKS.
    # The pre-entry high 5.06 (above the 4.80 stop) must not read as a stop breach.
    def test_breakdown_trades_through_triggers_not_breaks(self):
        c = [candle(1, 5.06, 5.00),   # pre-entry, above the stop
             candle(2, 5.00, 4.60),   # pre-entry, crossing the stop on the way DOWN
             candle(3, 4.55, 4.30)]   # ENTRY bar: low 4.30 <= 4.32
        leg = CL.eval_price_leg(_short([4.15, 4.32]), "SHORT", rng(c))
        self.assertTrue(leg["entered_zone"])
        self.assertFalse(leg["stop_breached"])        # post-entry high 4.55 < 4.80
        self.assertEqual(leg["verdict"], "TRIGGERS")

    # A genuine post-entry reclaim of the stop IS a break (sequence respected).
    def test_breakdown_reclaims_stop_post_entry_breaks(self):
        c = [candle(1, 5.06, 5.00),
             candle(2, 4.55, 4.30),   # ENTRY bar
             candle(3, 4.85, 4.40)]   # post-entry rally back THROUGH the 4.80 stop
        leg = CL.eval_price_leg(_short([4.15, 4.32]), "SHORT", rng(c))
        self.assertTrue(leg["stop_breached"])
        self.assertEqual(leg["verdict"], "BREAKS")

    # Regression: fade-the-rally short (zone ABOVE spot) still TRIGGERS exactly as today.
    def test_fade_rally_short_still_triggers(self):
        c = [candle(1, 5.10, 5.04), candle(2, 5.30, 5.18)]   # rises INTO [5.20,5.40]
        leg = CL.eval_price_leg(_short([5.20, 5.40], stop=5.60, tps=(4.8,)), "SHORT", rng(c))
        self.assertTrue(leg["entered_zone"])
        self.assertFalse(leg["stop_breached"])               # 5.30 < 5.60
        self.assertEqual(leg["verdict"], "TRIGGERS")

    # SPEC-78 guard: with NO candles the sequence is unresolvable → conservative full-window
    # read is kept (enter + reclaim-the-stop in one aggregate window → BREAKS).
    def test_no_candles_keeps_conservative_full_window(self):
        r = {"high": 5.07, "low": 4.18, "window": "24h-ticker:binance", "candles": None}
        leg = CL.eval_price_leg(_short([4.15, 4.32]), "SHORT", r)
        self.assertTrue(leg["stop_breached"])
        self.assertEqual(leg["verdict"], "BREAKS")

    # A position already live (entered_ts latched / OPEN) watches the FULL window, unchanged.
    def test_already_live_watches_full_window(self):
        c = [candle(1, 5.06, 5.00), candle(2, 5.00, 4.60), candle(3, 4.55, 4.30)]
        leg = CL.eval_price_leg(
            _short([4.15, 4.32], status="PENDING", entered_ts="2026-06-17T00:00:00Z"),
            "SHORT", rng(c))
        self.assertTrue(leg["stop_breached"])     # latched live → pre-entry high counts
        self.assertEqual(leg["verdict"], "BREAKS")


class BreakoutLongMirror(unittest.TestCase):
    """SPEC-79 req #2 mirror: a buy-the-breakout LONG rises from BELOW its stop up into the
    zone — the pre-entry dip below the stop must not false-BREAK either."""

    def _long(self, zone, stop, status="PENDING"):
        return {"direction": "LONG", "status": status, "setup": "squeeze_fuel",
                "stop": stop, "tps": [5.8], "entry_zone": list(zone)}

    def test_breakout_long_preentry_dip_below_stop_triggers(self):
        c = [candle(1, 5.04, 4.90),   # pre-entry dip BELOW the 5.00 stop
             candle(2, 5.30, 5.10)]   # ENTRY: breaks up into [5.20,5.40]
        leg = CL.eval_price_leg(self._long([5.20, 5.40], stop=5.00), "LONG", rng(c))
        self.assertTrue(leg["entered_zone"])
        self.assertFalse(leg["stop_breached"])     # post-entry low 5.10 > 5.00
        self.assertEqual(leg["verdict"], "TRIGGERS")

    def test_breakout_long_post_entry_loses_stop_breaks(self):
        c = [candle(1, 5.04, 5.02),
             candle(2, 5.30, 5.10),   # ENTRY
             candle(3, 5.15, 4.95)]   # post-entry loses the 5.00 stop
        leg = CL.eval_price_leg(self._long([5.20, 5.40], stop=5.00), "LONG", rng(c))
        self.assertTrue(leg["stop_breached"])
        self.assertEqual(leg["verdict"], "BREAKS")


class BoardReplay(unittest.TestCase):
    """End-to-end: RIVER + BSB breakdown shorts read CONFIRMS while price sits above the zone,
    and TRIGGERS (never BREAKS) once price trades down through it. Funding held neutral so the
    price leg is the only one that can fire."""

    def setUp(self):
        self._orig_range = CL.price_window_range
        self._orig_scan = CL.scan_trigger_logs
        CL.scan_trigger_logs = lambda ticker, since_epoch=None: ([], [])

    def tearDown(self):
        CL.price_window_range = self._orig_range
        CL.scan_trigger_logs = self._orig_scan

    def _patch(self, r):
        CL.price_window_range = lambda ticker, venue="binance", since_epoch=None: r

    def _tok(self, ticker, zone, stop):
        return {"ticker": ticker,
                "state": f"SHORT breakdown — arm on break of zone, funding -0.10%/4h",
                "thesis": {"direction": "SHORT", "status": "PENDING", "setup": "stage5",
                           "stop": stop, "tp": [zone[0] * 0.9], "entry_zone": list(zone),
                           "committed_ts": "2026-06-16"}}

    def test_river_above_zone_confirms(self):
        self._patch(rng([candle(1, 5.06, 5.02), candle(2, 5.08, 5.04)]))
        r = CL.classify_token(self._tok("RIVERX", [4.15, 4.32], 4.80), LIVE_QUIET)
        self.assertEqual(r["verdict"], "CONFIRMS")
        self.assertNotIn("STOP BREACHED", r["reason"])

    def test_bsb_above_zone_confirms(self):
        self._patch(rng([candle(1, 0.537, 0.52), candle(2, 0.54, 0.50)]))
        live = dict(LIVE_QUIET, price=0.537)
        r = CL.classify_token(self._tok("BSBX", [0.44, 0.475], 0.55), live)
        self.assertEqual(r["verdict"], "CONFIRMS")

    def test_river_trades_through_triggers_not_breaks(self):
        self._patch(rng([candle(1, 5.06, 5.00), candle(2, 5.00, 4.60),
                         candle(3, 4.55, 4.30)]))
        r = CL.classify_token(self._tok("RIVERX", [4.15, 4.32], 4.80), LIVE_QUIET)
        self.assertEqual(r["verdict"], "TRIGGERS")
        self.assertTrue(r["price_leg"]["entered_zone"])
        self.assertFalse(r["price_leg"]["stop_breached"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
