#!/usr/bin/env python3
"""SPEC 54 — price-leg must evaluate from committed_ts forward, not a trailing 24h window.

Run:  python3 tests/test_price_leg_committed_ts.py

Live double false-fire on a fresh ESPORTS commit: BREAKS off a stop-wick that predated
the commit by hours, then TRIGGERS off pre-commit TP/zone prints — SPEC 39's price leg
read the trailing 24h range with no committed_ts cut (the cut was coupled to
time_stop_h and vanished without it). Contract under test:
  - stop wicked BEFORE commit → CONFIRMS (no BREAK); wicked AFTER → BREAKS;
  - TP printed pre-commit only → no TRIGGERS;
  - the committed_ts cut applies even when the thesis has NO time_stop_h (the live bug);
  - fresh commit with no closed bars → price leg SILENT (never the 24h fallback);
  - commit older than the kline floor → trailing_24h_fallback, tagged;
  - thesis commit emits commit_warning when stop/TP/zone sit inside the prior-24h range.
Kline/ticker fetches monkeypatched — offline, deterministic.
"""
import importlib.util
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CL = _load("classify")
NOW = time.time()


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def bars(spec_list):
    """[(offset_h_from_now, high, low)] → kline rows (ts ascending)."""
    return [{"ts": NOW + off * 3600, "high": h, "low": lo}
            for off, h, lo in sorted(spec_list)]


def thesis_tok(committed_h_ago=3, stop=0.105, tps=(0.08,), zone=None, time_stop_h=None):
    th = {"direction": "SHORT", "setup": "scalp", "status": "OPEN",
          "stop": stop, "tps": list(tps),
          "invalidation": {"price_reclaim": 0.112},
          "committed_ts": iso(NOW - committed_h_ago * 3600)}
    if zone:
        th["entry_zone"] = list(zone)
    if time_stop_h:
        th["time_stop_h"] = time_stop_h
    return {"ticker": "ZZZQ", "state": "short scalp", "thesis": th}


class _Patch(unittest.TestCase):
    def setUp(self):
        self._orig = (CL._klines_binance, CL._klines_bybit, CL._ticker_hl)
        self.kline_calls = []
        self.fixture = []

        def kl(sym, start_ms, iv_min=60):
            # deliberately returns EVERYTHING — the implementation must apply the
            # committed_ts cut itself (a venue returning the straddling bar included)
            self.kline_calls.append((sym, start_ms, iv_min))
            return list(self.fixture) or None
        CL._klines_binance = kl
        CL._klines_bybit = kl

        def deny_hl(sym, venue):
            raise AssertionError("trailing 24h fallback used despite a fresh committed_ts")
        CL._ticker_hl = deny_hl

    def tearDown(self):
        CL._klines_binance, CL._klines_bybit, CL._ticker_hl = self._orig


class TestCommittedCut(_Patch):
    def test_pre_commit_stop_wick_is_not_a_break(self):
        # the 0.112 wick printed 2h BEFORE the commit (T-3h); post-commit bars stay under
        self.fixture = bars([(-5, 0.112, 0.088), (-1, 0.0955, 0.09), (0, 0.096, 0.091)])
        tok = thesis_tok(committed_h_ago=3)
        res = CL.classify_token(tok, None)
        self.assertEqual(res["verdict"], "CONFIRMS")
        self.assertFalse(res["price_leg"]["stop_breached"])
        # and the fetch was cut at the commit, not a trailing day
        sym, start_ms, iv = self.kline_calls[0]
        self.assertAlmostEqual(start_ms / 1000, NOW - 3 * 3600, delta=5)

    def test_post_commit_stop_wick_breaks(self):
        # committed 3h ago; the -2.5h bar is 0.5h AFTER commit and wicks through the stop
        self.fixture = bars([(-5, 0.095, 0.09), (-2.5, 0.112, 0.09), (-1, 0.095, 0.09)])
        tok = thesis_tok(committed_h_ago=3)
        res = CL.classify_token(tok, None)
        self.assertEqual(res["verdict"], "BREAKS")
        self.assertTrue(res["price_leg"]["stop_breached"])

    def test_pre_commit_tp_print_is_not_a_trigger(self):
        # TP 0.09 traded only before the commit; after it the tape never reached it
        self.fixture = bars([(-5, 0.096, 0.085), (-1, 0.0955, 0.0945), (0, 0.096, 0.0942)])
        tok = thesis_tok(committed_h_ago=3, tps=(0.09,))
        res = CL.classify_token(tok, None)
        self.assertEqual(res["verdict"], "CONFIRMS")
        self.assertEqual(res["price_leg"]["tps_printed"], [])

    def test_cut_applies_without_time_stop_h(self):
        # the live regression: no time_stop_h must NOT mean no committed_ts cut
        self.fixture = bars([(-1, 0.0955, 0.09)])
        tok = thesis_tok(committed_h_ago=3, time_stop_h=None)
        res = CL.classify_token(tok, None)
        self.assertTrue(self.kline_calls, "klines never fetched — fell to the 24h window")
        self.assertAlmostEqual(self.kline_calls[0][1] / 1000, NOW - 3 * 3600, delta=5)
        self.assertEqual(res["verdict"], "CONFIRMS")

    def test_intraday_interval_for_fresh_commits(self):
        self.fixture = bars([(-1, 0.0955, 0.09)])
        CL.classify_token(thesis_tok(committed_h_ago=3), None)
        self.assertEqual(self.kline_calls[0][2], 15)        # 15m bars inside the first 24h
        self.kline_calls.clear()
        CL.classify_token(thesis_tok(committed_h_ago=48), None)
        self.assertEqual(self.kline_calls[0][2], 60)        # 1h beyond a day

    def test_fresh_commit_no_bars_is_silent_not_fallback(self):
        self.fixture = []                                   # no closed bars yet
        res = CL.classify_token(thesis_tok(committed_h_ago=0.01), None)
        self.assertIsNone(res["price_leg"])                 # silent; deny_hl proves no fallback
        self.assertEqual(res["verdict"], "CONFIRMS")

    def test_ancient_commit_falls_back_tagged(self):
        CL._ticker_hl = lambda sym, venue: (0.2, 0.05)      # allow the fallback here
        tok = thesis_tok(committed_h_ago=24 * 70)           # beyond the 1h-kline reach
        res = CL.classify_token(tok, None)
        self.assertIn("trailing_24h_fallback", res["price_leg"]["window"])


class TestCommitWarning(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self.TH = _load("thesis")
        self._orig = (self.TH.WL_PATH, self.TH.LOCK_PATH,
                      self.TH.ledger.LEDGER_PATH, self.TH._prior_24h_range)
        self.TH.WL_PATH = d / "watchlist.json"
        self.TH.LOCK_PATH = d / "watchlist.lock"
        self.TH.ledger.LEDGER_PATH = d / "ledger.jsonl"
        self.TH.WL_PATH.write_text(json.dumps({"tokens": [], "retired": []}))
        self.TH._prior_24h_range = lambda tk: (0.112, 0.0552)   # ESPORTS-style wide day

    def tearDown(self):
        (self.TH.WL_PATH, self.TH.LOCK_PATH,
         self.TH.ledger.LEDGER_PATH, self.TH._prior_24h_range) = self._orig
        self.dir.cleanup()

    def _thesis(self, stop):
        return {"direction": "SHORT", "setup": "scalp", "stop": stop, "tp": [0.04],
                "invalidation": {"price_reclaim": 0.125}, "time_stop_h": 72}

    def test_stop_inside_prior_24h_range_warns(self):
        out = self.TH.build_thesis("commit", "ESPORTS", thesis=self._thesis(stop=0.105))
        self.assertTrue(out["ok"])
        self.assertIn("inside", out["commit_warning"])
        self.assertIn("0.105", out["commit_warning"])

    def test_stop_beyond_range_no_warning(self):
        out = self.TH.build_thesis("commit", "ESPORTS", thesis=self._thesis(stop=0.13))
        self.assertTrue(out["ok"])
        self.assertIsNone(out.get("commit_warning"))

    def test_range_fetch_failure_never_blocks_commit(self):
        def boom(tk):
            raise RuntimeError("venue down")
        self.TH._prior_24h_range = boom
        out = self.TH.build_thesis("commit", "ESPORTS", thesis=self._thesis(stop=0.105))
        self.assertTrue(out["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
