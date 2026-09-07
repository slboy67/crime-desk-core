#!/usr/bin/env python3
"""SPEC 77 — WATCH theses carry a machine-checkable watch_level.

LIVE PROOF this guards: BEAT (2026-06-16) parked WATCH with entry_zone/stop/tp = null and
the re-arm condition written as prose. classify computes a price_leg — and so can only emit
BREAKS/TRIGGERS — when the thesis has a committed stop/tp/entry_zone, so a WATCH thesis with
those null returns nothing but CONFIRMS. BEAT then ran 11.57 ATH → −75% to 2.83 in 4 days and
the board printed `BEAT CONFIRMS` the entire time. No level, no alert.

Rule under test: a `watch_level {price, dir, note}` on the thesis is a monitor-only trip-wire.
classify reads it against the same klines even with null committed levels and WATCH direction,
and emits a distinct WATCH-ARMED event (NOT BREAKS/TRIGGERS — those mean a committed position
moved) carrying the note. Null watch_level → today's behavior, byte-identical.

Offline-deterministic: price_window_range + scan_trigger_logs are monkeypatched.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import classify as CL
import thesis as TH


# WATCH thesis: parked, no committed levels, re-arm condition was prose-only until SPEC 77.
def _watch_tok(watch_level=None, status="WATCH", state="WATCH blowoff-short retired-unfilled"):
    th = {
        "direction": "WATCH", "status": status,
        "stop": None, "tp": None, "entry_zone": None,
        "committed_ts": "2026-06-10",
    }
    if watch_level is not None:
        th["watch_level"] = watch_level
    return {"ticker": "BEAT", "state": state, "thesis": th}


# live present + funding consistent → the rf/directional leg alone yields CONFIRMS for a WATCH dir
LIVE_QUIET = {
    "venue": "binance", "primary_venue": "binance",
    "price": 3.0, "chg24": -20.0, "vol_m": 40.0, "oi": 1e6,
    "funding_pi": -0.02, "funding_4h": -0.04, "interval_min": 480,
    "funding_stale": False, "all_floor": False, "funding_split": False,
}


def _rng(high, low, window="klines:binance:50x60m", candles=None):
    return {"high": high, "low": low, "window": window, "candles": candles}


class WatchLevelTests(unittest.TestCase):
    def setUp(self):
        self._orig_range = CL.price_window_range
        self._orig_scan = CL.scan_trigger_logs
        CL.scan_trigger_logs = lambda ticker, since_epoch=None: ([], [])
        self.range_calls = []

    def tearDown(self):
        CL.price_window_range = self._orig_range
        CL.scan_trigger_logs = self._orig_scan

    def _patch_range(self, rng):
        def fake(ticker, venue="binance", since_epoch=None):
            self.range_calls.append((ticker, venue, since_epoch))
            return rng
        CL.price_window_range = fake

    # ── DoD 1a: window low crosses a `below` watch_level → WATCH-ARMED, carries the note ──
    def test_watch_level_below_breach_is_watch_armed(self):
        wl = {"price": 9.20, "dir": "below",
              "note": "lower-high rebuild below 9.20 + funding cooling → post-breakdown short"}
        self._patch_range(_rng(11.57, 2.83))     # ran to ATH then collapsed through 9.20
        r = CL.classify_token(_watch_tok(wl), LIVE_QUIET)
        self.assertEqual(r["verdict"], "WATCH-ARMED")
        self.assertIn("9.2", r["reason"])
        self.assertIn("post-breakdown short", r["reason"])   # the note rides in the reason
        self.assertIsNotNone(r.get("watch_leg"))
        self.assertEqual(len(r["watch_leg"]["breached"]), 1)
        self.assertIsNone(r.get("price_leg"))    # no committed levels → price leg stays None

    # ── DoD 1b: SAME thesis, window never crosses → plain CONFIRMS, no event ──
    def test_watch_level_not_crossed_is_confirms(self):
        wl = {"price": 9.20, "dir": "below", "note": "post-breakdown short"}
        self._patch_range(_rng(11.0, 9.50))      # low 9.50 never reaches 9.20
        r = CL.classify_token(_watch_tok(wl), LIVE_QUIET)
        self.assertEqual(r["verdict"], "CONFIRMS")
        self.assertIsNone(r.get("watch_leg"))

    # ── DoD 1c: null watch_level → byte-identical to today (regression guard) ──
    def test_null_watch_level_is_unchanged(self):
        self._patch_range(_rng(11.57, 2.83))
        r = CL.classify_token(_watch_tok(None), LIVE_QUIET)
        self.assertEqual(r["verdict"], "CONFIRMS")
        self.assertIsNone(r.get("watch_leg"))
        # with no committed levels AND no watch_level, the price window is never fetched
        self.assertEqual(self.range_calls, [])

    # ── 'above' direction: window high crosses → WATCH-ARMED ──
    def test_watch_level_above_breach(self):
        wl = {"price": 12.0, "dir": "above", "note": "breakout reclaim"}
        self._patch_range(_rng(12.5, 8.0))
        r = CL.classify_token(_watch_tok(wl), LIVE_QUIET)
        self.assertEqual(r["verdict"], "WATCH-ARMED")
        self.assertIn("breakout reclaim", r["reason"])

    # ── list of watch_levels: any one breached arms ──
    def test_watch_level_list_any_breached(self):
        wl = [{"price": 9.20, "dir": "below", "note": "breakdown"},
              {"price": 13.0, "dir": "above", "note": "reclaim"}]
        self._patch_range(_rng(11.0, 8.5))       # only the 9.20-below trips
        r = CL.classify_token(_watch_tok(wl), LIVE_QUIET)
        self.assertEqual(r["verdict"], "WATCH-ARMED")
        self.assertEqual(len(r["watch_leg"]["breached"]), 1)
        self.assertIn("breakdown", r["reason"])

    # ── RETIRED/PASS WATCH thesis never reads the watch leg (no fetch, no event) ──
    def test_retired_watch_thesis_skips_watch_leg(self):
        wl = {"price": 9.20, "dir": "below", "note": "x"}
        self._patch_range(_rng(11.57, 2.83))
        r = CL.classify_token(_watch_tok(wl, status="RETIRED"), LIVE_QUIET)
        self.assertEqual(self.range_calls, [])
        self.assertIsNone(r.get("watch_leg"))

    # ── watch leg works with no live data too (board still surfaces it) ──
    def test_watch_armed_without_live(self):
        wl = {"price": 9.20, "dir": "below", "note": "post-breakdown short"}
        self._patch_range(_rng(11.57, 2.83))
        r = CL.classify_token(_watch_tok(wl), None)
        self.assertEqual(r["verdict"], "WATCH-ARMED")

    # ── WATCH-ARMED outranks CONFIRMS in board precedence ──
    def test_watch_armed_beats_confirms_precedence(self):
        self.assertLess(CL.VERDICT_ORDER["WATCH-ARMED"], CL.VERDICT_ORDER["CONFIRMS"])
        self.assertGreater(CL.VERDICT_ORDER["WATCH-ARMED"], CL.VERDICT_ORDER["TRIGGERS"])


class EvalWatchLevelsPure(unittest.TestCase):
    """eval_watch_levels is pure — direct edge coverage. Normalizer-level edge cases
    (malformed watch_level shapes, caveat emission) moved to tests/test_thesis.py's
    SchemaTableTest (SPEC-146: thesis.parse() is now the one normalizer)."""

    def test_below_touch_is_breach(self):
        wls = TH.parse({"thesis": {"watch_level": {"price": 9.20, "dir": "below"}}}).watch_levels
        self.assertEqual(CL.eval_watch_levels(wls, _rng(11.0, 9.20)), wls)  # touch = crossed

    def test_above_not_reached(self):
        wls = TH.parse({"thesis": {"watch_level": {"price": 12.0, "dir": "above"}}}).watch_levels
        self.assertEqual(CL.eval_watch_levels(wls, _rng(11.99, 8.0)), [])


class WatchLevelCaveatOnBoardRow(unittest.TestCase):
    """SPEC-144 req 3: a dropped watch_level element surfaces on the classify row, not
    just internally — a reader that discards committed intent must say so."""

    def setUp(self):
        self._orig_range = CL.price_window_range
        self._orig_scan = CL.scan_trigger_logs
        CL.scan_trigger_logs = lambda ticker, since_epoch=None: ([], [])
        CL.price_window_range = lambda ticker, venue="binance", since_epoch=None: _rng(11.0, 9.5)

    def tearDown(self):
        CL.price_window_range = self._orig_range
        CL.scan_trigger_logs = self._orig_scan

    def test_row_carries_caveat_for_mixed_watch_level(self):
        tok = _watch_tok([{"price": 9.20, "dir": "below", "note": "x"}, 0.039])
        r = CL.classify_token(tok, LIVE_QUIET)
        self.assertTrue(any("0.039" in c for c in (r.get("caveats") or [])), r.get("caveats"))

    def test_row_no_caveats_for_clean_watch_level(self):
        tok = _watch_tok([{"price": 9.20, "dir": "below", "note": "x"}])
        r = CL.classify_token(tok, LIVE_QUIET)
        self.assertFalse(r.get("caveats"))


# SPEC-146: the board-level invariant sweep (formerly check_watch_level_invariant, SPEC-144
# req 5) moved to thesis.check_board and now covers every droppable geometry field, not just
# watch_level — see tests/test_thesis.py's BoardInvariantTest.


class WatchArmedInboxDedup(unittest.TestCase):
    """SPEC 77.4 — a breached watch_level reads as armed every tick but alerts ONCE."""

    def setUp(self):
        self._orig_cursor = CL.WATCH_ARMED_CURSOR
        self._orig_append = CL.inbox.append_event
        # redirect the cursor to a temp path so we never touch real state
        self._tmp = Path(__file__).resolve().parent / "_watch_armed_cursor_test.json"
        if self._tmp.exists():
            self._tmp.unlink()
        CL.WATCH_ARMED_CURSOR = self._tmp
        self.fired = []
        CL.inbox.append_event = lambda **kw: self.fired.append(kw) or kw

    def tearDown(self):
        CL.WATCH_ARMED_CURSOR = self._orig_cursor
        CL.inbox.append_event = self._orig_append
        if self._tmp.exists():
            self._tmp.unlink()

    def _results(self):
        return [{"ticker": "BEAT", "verdict": "WATCH-ARMED",
                 "watch_leg": {"breached": [{"price": 9.20, "dir": "below", "note": "x"}],
                               "high": 11.57, "low": 2.83, "window": "klines"}}]

    def test_fires_once_then_dedups(self):
        n1 = CL.fire_watch_armed(self._results(), now=1.0e9)
        n2 = CL.fire_watch_armed(self._results(), now=1.0e9 + 60)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)               # second tick does not re-nag
        self.assertEqual(len(self.fired), 1)
        self.assertEqual(self.fired[0]["ticker"], "BEAT")
        self.assertEqual(self.fired[0]["source"], "watch_level")


if __name__ == "__main__":
    unittest.main()
