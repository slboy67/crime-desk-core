#!/usr/bin/env python3
"""SPEC-106 — page on tape-pattern deterioration for WATCH names.

Run: python3 tests/test_tape_watch.py

BIRB moved -12% intraday off its pump high with operator_profit_take + chain_squeeze +
downtrend_slam printing on the tape, and the desk emitted ZERO alerts because the committed
watch levels (0.070/0.095) sat outside the move. Contract under test:
  - a BIRB-shaped tick (pattern + >=10% drift, no watch-level breach) -> a MED/HIGH event
    whose message composes the read (drift + pattern + retire-level), not just a bare fact;
  - a quiet tick (small drift, no patterns, funding unchanged) -> no event;
  - funding decaying past half its committed magnitude (even before the +/- flip) -> event;
  - run_tick pages once then respects the page_gate cooldown on the immediate repeat, but
    still counts the name as checked;
  - a DUST-gated name (vol_m < the $10M/24h gate) is skipped before any tape fetch.
All network/venue calls are injected — fully offline.
"""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, sub="capabilities"):
    spec = importlib.util.spec_from_file_location(name, ROOT / sub / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TW = _load("tape_watch", sub="ops")
IB = TW.inbox

BIRB_THESIS = {
    "direction": "WATCH",
    "watch_level": [
        {"price": 0.095, "dir": "above", "note": "reclaim through the Binance wall zone"},
        {"price": 0.07, "dir": "below", "note": "pump fully faded back into the old grind range"},
    ],
}
BIRB_REGIME = {"funding_sign": "neg", "funding_pi": -0.898, "price": 0.0883}


class TestCheckTapeDeterioration(unittest.TestCase):
    def test_birb_shaped_drift_and_pattern_composes_read(self):
        price = 0.0883 * 0.88   # -12% off commit, still inside 0.07/0.095
        tape_result = {
            "available": True,
            "patterns": [
                {"type": "tail_of_liquidation", "detail": {"context": "operator_profit_take"}},
                {"type": "downtrend_slam", "detail": {"shorts_opening": 3}},
            ],
        }
        out = TW.check_tape_deterioration(
            "BIRB", BIRB_THESIS, BIRB_REGIME, price=price, funding_pi=-0.898, tape_result=tape_result)
        self.assertIsNotNone(out)
        severity, msg = out
        self.assertIn(severity, ("MED", "HIGH"))
        self.assertIn("-12%", msg)
        self.assertIn("operator taking profit", msg)   # SPEC-141: human phrase, not the raw token
        self.assertIn("shorts still opening", msg)
        self.assertIn("retire-level 0.07", msg)
        self.assertIn("below", msg)

    def test_quiet_tick_no_event(self):
        price = 0.0883 * 1.02   # +2% drift, well under the 10% gate
        tape_result = {"available": True, "patterns": []}
        out = TW.check_tape_deterioration(
            "BIRB", BIRB_THESIS, BIRB_REGIME, price=price, funding_pi=-0.898, tape_result=tape_result)
        self.assertIsNone(out)

    def test_funding_decay_past_half_fires(self):
        # committed -0.898 -> half magnitude is -0.449; -0.30 has decayed past it, still neg
        price = BIRB_REGIME["price"]   # no drift in this fixture
        tape_result = {"available": True, "patterns": []}
        out = TW.check_tape_deterioration(
            "BIRB", BIRB_THESIS, BIRB_REGIME, price=price, funding_pi=-0.30, tape_result=tape_result)
        self.assertIsNotNone(out)
        severity, msg = out
        self.assertIn("funding", msg)
        self.assertIn("-0.30", msg)
        self.assertIn("-0.90", msg.replace("-0.898", "-0.90"))  # tolerate rounding either way

    def test_funding_still_above_half_does_not_fire_alone(self):
        price = BIRB_REGIME["price"]
        tape_result = {"available": True, "patterns": []}
        out = TW.check_tape_deterioration(
            "BIRB", BIRB_THESIS, BIRB_REGIME, price=price, funding_pi=-0.62, tape_result=tape_result)
        self.assertIsNone(out)

    def test_watch_level_breach_suppresses_the_drift_reason(self):
        # price already broke the 0.07 watch level -> that's watch_level's job, not tape_watch's
        price = 0.065
        tape_result = {"available": True, "patterns": []}
        out = TW.check_tape_deterioration(
            "BIRB", BIRB_THESIS, BIRB_REGIME, price=price, funding_pi=-0.898, tape_result=tape_result)
        self.assertIsNone(out)

    def test_non_watch_thesis_returns_none(self):
        thesis = dict(BIRB_THESIS, direction="LONG")
        out = TW.check_tape_deterioration(
            "BIRB", thesis, BIRB_REGIME, price=0.05, funding_pi=-0.898,
            tape_result={"available": True, "patterns": []})
        self.assertIsNone(out)

    def test_chain_squeeze_cluster_fires_high(self):
        tape_result = {
            "available": True,
            "patterns": [
                {"type": "chain_squeeze", "detail": {}},
                {"type": "chain_squeeze", "detail": {}},
            ],
        }
        price = BIRB_REGIME["price"] * 1.11   # clear the 10% gate too
        out = TW.check_tape_deterioration(
            "BIRB", BIRB_THESIS, BIRB_REGIME, price=price, funding_pi=-0.898, tape_result=tape_result)
        self.assertIsNotNone(out)
        severity, msg = out
        self.assertEqual(severity, "HIGH")
        self.assertIn("chain-squeeze cluster", msg)   # SPEC-141: human phrase, not the raw token


class TestDust(unittest.TestCase):
    def test_dust_predicate(self):
        self.assertTrue(TW._dust(5.0))
        self.assertFalse(TW._dust(25.0))
        self.assertFalse(TW._dust(None))


class TestLoadWatchEntries(unittest.TestCase):
    def test_loads_only_watch_direction_tokens(self):
        d = Path(tempfile.mkdtemp())
        wl = d / "watchlist.json"
        wl.write_text(json.dumps({"tokens": [
            {"ticker": "BIRB", "thesis": BIRB_THESIS, "regime": BIRB_REGIME},
            {"ticker": "LONGTOK", "thesis": {"direction": "LONG"}, "regime": {}},
        ]}))
        entries = TW.load_watch_entries(wl)
        self.assertEqual([e["ticker"] for e in entries], ["BIRB"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(TW.load_watch_entries(Path("/nonexistent/watchlist.json")), [])


class TestRunTick(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self.wl_path = d / "watchlist.json"
        self.wl_path.write_text(json.dumps({"tokens": [
            {"ticker": "BIRB", "thesis": BIRB_THESIS, "regime": BIRB_REGIME},
        ]}))
        self.page_state_path = d / "page_cooldowns.json"
        self._orig_ib = (IB.LOG_PATH, IB.EVENTS_PATH, IB.CURSOR_PATH)
        IB.LOG_PATH = d / "nonce_alerts.log"
        IB.EVENTS_PATH = d / "inbox_events.jsonl"
        IB.CURSOR_PATH = d / "inbox_cursor.json"
        self.notes = []
        self.now = datetime(2026, 7, 2, 13, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        IB.LOG_PATH, IB.EVENTS_PATH, IB.CURSOR_PATH = self._orig_ib
        self.dir.cleanup()

    def _live_fn(self, ticker):
        return {"price": 0.0883 * 0.88, "funding_pi": -0.898, "vol_m": 50.0}

    def _tape_fn(self, ticker, window_min=60, level=None):
        return {"available": True, "patterns": [
            {"type": "tail_of_liquidation", "detail": {"context": "operator_profit_take"}},
        ]}

    def test_pages_once_then_cooldown_suppresses_repeat(self):
        out1 = TW.run_tick(wl_path=self.wl_path, live_fn=self._live_fn, tape_fn=self._tape_fn,
                            now=self.now, page_state_path=self.page_state_path,
                            notify_fn=lambda t, m: self.notes.append((t, m)))
        self.assertEqual(out1["checked"], 1)
        self.assertEqual(out1["paged"], 1)
        self.assertEqual(out1["events"], 1)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(len(IB.unconsumed()), 1)

        out2 = TW.run_tick(wl_path=self.wl_path, live_fn=self._live_fn, tape_fn=self._tape_fn,
                            now=self.now, page_state_path=self.page_state_path,
                            notify_fn=lambda t, m: self.notes.append((t, m)))
        self.assertEqual(out2["checked"], 1)
        self.assertEqual(out2["paged"], 0)     # cooldown active
        self.assertEqual(len(self.notes), 1)   # no second notification
        self.assertEqual(len(IB.unconsumed()), 1)   # no second inbox write either

    def test_dust_gated_name_skipped_before_tape_fetch(self):
        called = []

        def tape_fn(ticker, window_min=60, level=None):
            called.append(ticker)
            return {"available": True, "patterns": []}

        out = TW.run_tick(wl_path=self.wl_path,
                           live_fn=lambda t: {"price": 0.05, "funding_pi": -0.898, "vol_m": 2.0},
                           tape_fn=tape_fn, now=self.now, page_state_path=self.page_state_path,
                           notify_fn=lambda t, m: self.notes.append((t, m)))
        self.assertEqual(out["checked"], 0)
        self.assertEqual(called, [])
        self.assertEqual(IB.unconsumed(), [])

    def test_quiet_tick_no_page_no_event(self):
        out = TW.run_tick(wl_path=self.wl_path,
                           live_fn=lambda t: {"price": 0.0883, "funding_pi": -0.898, "vol_m": 50.0},
                           tape_fn=lambda t, window_min=60, level=None: {"available": True, "patterns": []},
                           now=self.now, page_state_path=self.page_state_path,
                           notify_fn=lambda t, m: self.notes.append((t, m)))
        self.assertEqual(out["checked"], 1)
        self.assertEqual(out["events"], 0)
        self.assertEqual(out["paged"], 0)
        self.assertEqual(self.notes, [])
        self.assertEqual(IB.unconsumed(), [])

    def test_malformed_watch_level_element_degrades_not_crashes(self):
        # SPEC-144 (pre-146): watch_level containing a bare float raised AttributeError
        # inside run_tick's level-extraction (`wl.get("dir")` on a float) — caught only
        # by a blanket except-and-skip. SPEC-146: thesis.parse() now drops the malformed
        # element (+ surfaces a caveat at the board level via thesis.check_board) instead
        # of raising, so the tick never crashes AND the row is still checked normally —
        # it no longer needs to be skipped at all.
        self.wl_path.write_text(json.dumps({"tokens": [
            {"ticker": "BROKEN",
             "thesis": {"direction": "WATCH", "watch_level": [0.039, 0.062]},
             "regime": {}},
            {"ticker": "BIRB", "thesis": BIRB_THESIS, "regime": BIRB_REGIME},
        ]}))

        def live_fn(ticker):
            if ticker == "BROKEN":
                return {"price": 1.0, "funding_pi": 0.0, "vol_m": 50.0}   # quiet — no page
            return self._live_fn(ticker)

        def tape_fn(ticker, window_min=60, level=None):
            if ticker == "BROKEN":
                return {"available": True, "patterns": []}
            return self._tape_fn(ticker, window_min=window_min, level=level)

        out = TW.run_tick(wl_path=self.wl_path, live_fn=live_fn, tape_fn=tape_fn,
                           now=self.now, page_state_path=self.page_state_path,
                           notify_fn=lambda t, m: self.notes.append((t, m)))
        self.assertEqual(out["checked"], 2)   # neither row crashed the tick
        self.assertEqual(out["paged"], 1)     # only BIRB produced an event

    def test_empty_watchlist_is_a_cheap_noop(self):
        empty = Path(self.dir.name) / "empty.json"
        empty.write_text(json.dumps({"tokens": []}))
        calls = []
        out = TW.run_tick(wl_path=empty,
                           live_fn=lambda t: calls.append(t) or {},
                           tape_fn=lambda *a, **k: calls.append("tape") or {"available": False},
                           now=self.now, page_state_path=self.page_state_path)
        self.assertEqual(out, {"checked": 0, "events": 0, "paged": 0, "detail": []})
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
