#!/usr/bin/env python3
"""SPEC-113 — watch-level crossings must only count AFTER thesis commit time.

Live false-fires this guards against:
  - SLX 2026-07-07: thesis committed @ 0.2077 with exit line 0.1846; classify immediately
    reported "0.1846 below crossed" off the 07-06 PRE-commit low while live price was 0.2094
    — a false EXIT-line page against a healthy position.
  - M 2026-07-04: "1.83 above crossed" off the pre-commit reflate high, read as "void
    condition met" when it never happened post-commit.

Contract under test:
  1. A watch_level counts as crossed ONLY if price traded through it at/after committed_ts
     (reuses the SPEC-54 committed_ts kline cut already wired for the price leg).
  2. A level breached both before AND after commit still reads as ONE crossing (post-commit
     event wins, not double-counted).
  3. A legacy entry with no resolvable committed_ts (or one beyond the kline reach) falls to
     the trailing_24h_fallback window — that crossing is tagged `(pre-commit history — verify
     live price)` in the reason, and `watch_leg["legacy"]` is True.
  4. board_tick.py's SPEC-111 push-notification path never delivers a legacy-tagged crossing
     (the inbox record, via classify.fire_watch_armed, still fires — only the phone push is
     gated).

Kline/ticker fetches monkeypatched — offline, deterministic.
"""
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, sub="capabilities"):
    spec = importlib.util.spec_from_file_location(name, ROOT / sub / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CL = _load("classify")
NOW = time.time()


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def bars(spec_list):
    """[(offset_h_from_now, high, low)] -> kline rows (ts ascending)."""
    return [{"ts": NOW + off * 3600, "high": h, "low": lo}
            for off, h, lo in sorted(spec_list)]


def _watch_tok(watch_level, committed_ts=None, status="WATCH"):
    th = {"direction": "WATCH", "status": status, "stop": None, "tp": None,
          "entry_zone": None, "watch_level": watch_level}
    if committed_ts is not None:
        th["committed_ts"] = committed_ts
    return {"ticker": "SLX", "state": "watch", "thesis": th}


class _KlinePatch(unittest.TestCase):
    def setUp(self):
        self._orig = (CL._klines_binance, CL._klines_bybit, CL._ticker_hl, CL.scan_trigger_logs)
        self.kline_calls = []
        self.fixture = []
        self.hl_calls = []

        def kl(sym, start_ms, iv_min=60):
            self.kline_calls.append((sym, start_ms, iv_min))
            return list(self.fixture) or None
        CL._klines_binance = kl
        CL._klines_bybit = kl

        def deny_hl(sym, venue):
            self.hl_calls.append((sym, venue))
            raise AssertionError("trailing 24h fallback used despite a fresh committed_ts")
        CL._ticker_hl = deny_hl
        CL.scan_trigger_logs = lambda ticker, since_epoch=None: ([], [])

    def tearDown(self):
        CL._klines_binance, CL._klines_bybit, CL._ticker_hl, CL.scan_trigger_logs = self._orig


class TestWatchLevelCommitCut(_KlinePatch):
    def test_breached_only_before_commit_is_not_crossed(self):
        # SLX-style: exit line 0.1846 wicked pre-commit (07-06); post-commit bars hold above it
        wl = {"price": 0.1846, "dir": "below", "note": "EXIT (thesis break)"}
        self.fixture = bars([(-5, 0.21, 0.180), (-1, 0.21, 0.205), (0, 0.212, 0.207)])
        tok = _watch_tok(wl, committed_ts=iso(NOW - 3 * 3600))
        res = CL.classify_token(tok, None)
        self.assertIsNone(res.get("watch_leg"))
        self.assertNotEqual(res["verdict"], "WATCH-ARMED")

    def test_breached_only_after_commit_is_crossed(self):
        wl = {"price": 0.1846, "dir": "below", "note": "EXIT (thesis break)"}
        # committed 3h ago; the -2.5h bar (0.5h after commit) wicks through the level
        self.fixture = bars([(-5, 0.21, 0.21), (-2.5, 0.21, 0.180), (-1, 0.21, 0.205)])
        tok = _watch_tok(wl, committed_ts=iso(NOW - 3 * 3600))
        res = CL.classify_token(tok, None)
        self.assertEqual(res["verdict"], "WATCH-ARMED")
        self.assertIsNotNone(res.get("watch_leg"))
        self.assertEqual(len(res["watch_leg"]["breached"]), 1)
        self.assertFalse(res["watch_leg"]["legacy"])
        self.assertNotIn("pre-commit history", res["reason"])

    def test_breached_before_and_after_is_crossed_once(self):
        wl = {"price": 0.1846, "dir": "below", "note": "EXIT (thesis break)"}
        self.fixture = bars([(-5, 0.21, 0.180),      # pre-commit breach (dropped)
                             (-2, 0.21, 0.181),       # post-commit breach (counts)
                             (-1, 0.21, 0.205)])
        tok = _watch_tok(wl, committed_ts=iso(NOW - 3 * 3600))
        res = CL.classify_token(tok, None)
        self.assertEqual(res["verdict"], "WATCH-ARMED")
        self.assertEqual(len(res["watch_leg"]["breached"]), 1)   # single crossing, not doubled

    def test_legacy_no_committed_ts_falls_back_tagged_no_raise(self):
        wl = {"price": 0.1846, "dir": "below", "note": "EXIT (thesis break)"}
        CL._ticker_hl = lambda sym, venue: (0.212, 0.180)   # trailing 24h fallback allowed here
        tok = _watch_tok(wl, committed_ts=None)             # no commit_ts at all (the SLX bug)
        res = CL.classify_token(tok, None)
        self.assertEqual(res["verdict"], "WATCH-ARMED")
        self.assertTrue(res["watch_leg"]["legacy"])
        self.assertIn("pre-commit history — verify live price", res["reason"])

    def test_legacy_commit_beyond_kline_reach_falls_back_tagged(self):
        wl = {"price": 0.1846, "dir": "below", "note": "EXIT (thesis break)"}
        CL._ticker_hl = lambda sym, venue: (0.212, 0.180)
        tok = _watch_tok(wl, committed_ts=iso(NOW - 24 * 70 * 3600))   # beyond the 1h-kline reach
        res = CL.classify_token(tok, None)
        self.assertEqual(res["verdict"], "WATCH-ARMED")
        self.assertTrue(res["watch_leg"]["legacy"])
        self.assertIn("pre-commit history — verify live price", res["reason"])

    def test_verified_crossing_not_tagged_legacy(self):
        wl = {"price": 0.1846, "dir": "below", "note": "x"}
        self.fixture = bars([(-2, 0.21, 0.180)])
        tok = _watch_tok(wl, committed_ts=iso(NOW - 3 * 3600))
        res = CL.classify_token(tok, None)
        self.assertFalse(res["watch_leg"]["legacy"])
        self.assertNotIn("pre-commit history", res["reason"])


class InboxStillFiresForLegacy(unittest.TestCase):
    """SPEC-113 requirement 3: legacy crossings keep firing the SPEC-77 inbox event (tagged);
    only the SPEC-111 phone push (board_tick.py) is gated — see BoardTickLegacyPushGate below."""

    def setUp(self):
        self._orig_cursor = CL.WATCH_ARMED_CURSOR
        self._orig_append = CL.inbox.append_event
        self._tmp = Path(__file__).resolve().parent / "_spec113_watch_armed_cursor.json"
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

    def test_legacy_watch_leg_still_fires_inbox_event_tagged(self):
        results = [{"ticker": "SLX", "verdict": "WATCH-ARMED",
                   "watch_leg": {"breached": [{"price": 0.1846, "dir": "below", "note": "x"}],
                                 "high": 0.212, "low": 0.180, "window": "trailing_24h_fallback:binance",
                                 "legacy": True}}]
        n = CL.fire_watch_armed(results, now=1.0e9)
        self.assertEqual(n, 1)
        self.assertIn("pre-commit history — verify live price", self.fired[0]["msg"])


BT = _load("board_tick", sub="ops")


class BoardTickLegacyPushGate(unittest.TestCase):
    """SPEC-113 requirement 4: board_tick's SPEC-111 push-delivery path never fires for a
    legacy-tagged watch_leg crossing (unresolvable/beyond-reach committed_ts)."""

    def setUp(self):
        self.notes = []
        self.notify = lambda title, msg: self.notes.append((title, msg))

    def _row(self, ticker, watch_leg):
        return {"ticker": ticker, "verdict": "CONFIRMS",
               "price_leg": {"stop_breached": False, "tps_printed": []},
               "watch_leg": watch_leg, "thesis_drift": None}

    def test_legacy_crossing_never_pushed(self):
        wl = {"breached": [{"price": 0.1846, "dir": "below", "note": "x"}],
             "high": 0.212, "low": 0.180, "window": "trailing_24h_fallback:binance", "legacy": True}
        rows = [self._row("SLX", wl)]
        keys = BT._current_continuous_keys(rows)
        self.assertEqual(keys, set())   # never armed — legacy crossings don't enter push state

    def test_verified_crossing_still_pushes(self):
        wl = {"breached": [{"price": 0.1846, "dir": "below", "note": "x"}],
             "high": 0.212, "low": 0.180, "window": "klines:binance:5x15m", "legacy": False}
        rows = [self._row("SLX", wl)]
        keys = BT._current_continuous_keys(rows)
        self.assertEqual(len(keys), 1)


IB = BT.inbox


class BoardTickTickLegacyGate(unittest.TestCase):
    """Full tick() end-to-end: a legacy-tagged watch_leg crossing never reaches notify_fn."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig_bt = (BT.BASELINE_PATH, BT.LOCK_PATH, BT.ERR_PATH, BT.NOTIFY_STATE_PATH)
        BT.BASELINE_PATH = d / "board_last.json"
        BT.LOCK_PATH = d / "board_tick.lock"
        BT.ERR_PATH = d / "board_tick.err"
        BT.NOTIFY_STATE_PATH = d / "notify_sent.json"
        self._orig_ib = (IB.LOG_PATH, IB.EVENTS_PATH, IB.CURSOR_PATH)
        IB.LOG_PATH = d / "nonce_alerts.log"
        IB.EVENTS_PATH = d / "inbox_events.jsonl"
        IB.CURSOR_PATH = d / "inbox_cursor.json"
        self._orig_ob = (BT.onboard.WALLETS, BT.onboard.WL_PATH)
        BT.onboard.WALLETS = d / "tracked_wallets.json"
        BT.onboard.WL_PATH = d / "watchlist.json"
        BT.onboard.WALLETS.write_text('{"tokens": {}}')
        BT.onboard.WL_PATH.write_text('{"tokens": []}')
        self._orig_tw = BT.tape_watch.run_tick
        BT.tape_watch.run_tick = lambda *a, **k: {"checked": 0, "events": 0, "paged": 0, "detail": []}
        self._orig_oic = BT.oic_watch.run_tick
        BT.oic_watch.run_tick = lambda *a, **k: {"checked": 0, "events": 0, "paged": 0, "detail": []}
        self._orig_env = os.environ.pop("CRIMEDESK_NOTIFY", None)
        self.notes = []
        self.notify = lambda title, msg: self.notes.append((title, msg))

    def tearDown(self):
        BT.BASELINE_PATH, BT.LOCK_PATH, BT.ERR_PATH, BT.NOTIFY_STATE_PATH = self._orig_bt
        IB.LOG_PATH, IB.EVENTS_PATH, IB.CURSOR_PATH = self._orig_ib
        BT.onboard.WALLETS, BT.onboard.WL_PATH = self._orig_ob
        BT.tape_watch.run_tick = self._orig_tw
        BT.oic_watch.run_tick = self._orig_oic
        if self._orig_env is not None:
            os.environ["CRIMEDESK_NOTIFY"] = self._orig_env
        else:
            os.environ.pop("CRIMEDESK_NOTIFY", None)
        self.dir.cleanup()

    def _row(self, ticker, watch_leg):
        return {"ticker": ticker, "verdict": "CONFIRMS",
               "price_leg": {"stop_breached": False, "tps_printed": []},
               "watch_leg": watch_leg}

    def seed(self, *rows):
        BT.BASELINE_PATH.write_text(BT.json.dumps(BT.snapshot(list(rows))))

    def test_legacy_crossing_no_push_verified_crossing_pushes(self):
        self.seed(self._row("SLX", None))
        legacy_wl = {"breached": [{"price": 0.1846, "dir": "below", "note": "x"}],
                    "high": 0.212, "low": 0.180, "window": "trailing_24h_fallback:binance",
                    "legacy": True}
        BT.tick(classify_fn=lambda: [self._row("SLX", legacy_wl)], notify_fn=self.notify)
        self.assertEqual(self.notes, [])

        verified_wl = {"breached": [{"price": 0.1846, "dir": "below", "note": "x"}],
                      "high": 0.212, "low": 0.180, "window": "klines:binance:5x15m",
                      "legacy": False}
        BT.tick(classify_fn=lambda: [self._row("SLX", verified_wl)], notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("SLX", self.notes[0][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
