#!/usr/bin/env python3
"""SPEC 62 — replay engine logic on SYNTHETIC bars (no network).

Tests the deterministic core: leg derivation, trade simulation, per-symbol replay,
report aggregation, and ledger-import tagging. The live data layer (`_fetch_live`) is
network and intentionally not exercised here.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import replay as R          # noqa: E402
import ledger as L          # noqa: E402


def _bar(ts, o, h, l, c, v, oi=None, funding=None):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v,
            "oi": oi, "funding": funding}


def blowoff_series():
    """A synthetic blowoff top: flat base → parabola → wick rejection → lower highs →
    a clean high-volume breakdown that holds. Built to fire the blowoff checklist."""
    bars = []
    ts = 1_700_000_000_000
    step = 3_600_000
    px = 1.00
    oi = 1_000_000.0
    # 0..11 flat base
    for i in range(12):
        bars.append(_bar(ts, px, px * 1.005, px * 0.995, px, 100, oi, 0.05))
        ts += step
    # 12..40 parabola up to ~1.70, OI climbing, funding hot (positive)
    for i in range(29):
        px *= 1.018
        oi *= 1.03
        bars.append(_bar(ts, px * 0.99, px * 1.01, px * 0.985, px, 140, oi, 0.08))
        ts += step
    # 41 the blowoff wick: high pokes well above, closes lower (rejection)
    peak = px
    bars.append(_bar(ts, peak, peak * 1.06, peak * 0.99, peak * 0.999, 300, oi, 0.07)); ts += step
    # 42..50 lower highs, OI rolling off, funding cooling
    hp = peak
    for i in range(9):
        hp *= 0.985
        oi *= 0.97
        bars.append(_bar(ts, hp, hp * 1.002, hp * 0.985, hp * 0.99, 120, oi, 0.03 - i * 0.004))
        ts += step
    # 51 clean breakdown: close below the recent support on >1.5x vol
    support = min(b["low"] for b in bars[-12:])
    brk = support * 0.97
    oi *= 0.97
    bars.append(_bar(ts, hp * 0.99, hp * 0.99, brk, brk, 400, oi, -0.01)); ts += step
    # 52..70 the break holds and drifts lower (let TP/stop resolve)
    dp = brk
    for i in range(19):
        dp *= 0.99
        oi *= 0.98
        bars.append(_bar(ts, dp * 1.005, dp * 1.008, dp * 0.99, dp, 150, oi, -0.02))
        ts += step
    return bars


def flat_series():
    bars = []
    ts = 1_700_000_000_000
    for i in range(90):
        bars.append(_bar(ts, 1.0, 1.002, 0.998, 1.0, 100, 1_000_000.0, 0.01))
        ts += 3_600_000
    return bars


class TestSimulateTrade(unittest.TestCase):
    def test_short_stop_out(self):
        fwd = [_bar(0, 1.0, 1.2, 0.99, 1.1, 1)]      # high 1.2 >= stop 1.1
        o, r, n, mae = R.simulate_trade(fwd, entry=1.0, stop=1.1, tp1=0.9, tp2=0.8)
        self.assertEqual(o, "stopped"); self.assertEqual(r, -1.0)

    def test_short_tp1(self):
        fwd = [_bar(0, 1.0, 1.02, 0.895, 0.91, 1)]   # low 0.895 <= tp1 0.90, not tp2 0.80
        o, r, n, mae = R.simulate_trade(fwd, entry=1.0, stop=1.1, tp1=0.9, tp2=0.8)
        self.assertEqual(o, "tp1"); self.assertEqual(r, 1.0)

    def test_short_tp2(self):
        fwd = [_bar(0, 1.0, 1.02, 0.79, 0.80, 1)]    # low 0.79 <= tp2 0.80
        o, r, n, mae = R.simulate_trade(fwd, entry=1.0, stop=1.1, tp1=0.9, tp2=0.8)
        self.assertEqual(o, "tp2"); self.assertEqual(r, 2.0)

    def test_time_stop_marks_to_close(self):
        fwd = [_bar(0, 1.0, 1.01, 0.99, 0.95, 1)]    # never hits stop/tp within budget
        o, r, n, mae = R.simulate_trade(fwd, entry=1.0, stop=1.1, tp1=0.9, tp2=0.8,
                                        time_stop_bars=1)
        self.assertEqual(o, "time_stop")
        self.assertAlmostEqual(r, 0.5, places=3)     # (1.0-0.95)/0.1 = 0.5R


class TestReplayEngine(unittest.TestCase):
    def test_blowoff_fires_and_records_outcome(self):
        bars = blowoff_series()
        outs = R.replay_symbol("BEAT", bars, "blowoff")
        self.assertGreaterEqual(len(outs), 1, "blowoff series should fire at least once")
        o = outs[0]
        self.assertEqual(o["signature"], "blowoff_top_short")
        self.assertEqual(o["source"], "replay")
        self.assertEqual(o["direction"], "SHORT")
        self.assertIn(o["outcome"], ("stopped", "tp1", "tp2", "time_stop"))
        self.assertIn("clean_break", o["evaluable_legs"])

    def test_reproducible_same_inputs_same_outputs(self):
        bars = blowoff_series()
        a = R.replay_symbol("BEAT", bars, "blowoff")
        b = R.replay_symbol("BEAT", bars, "blowoff")
        self.assertEqual(a, b)

    def test_flat_series_never_fires(self):
        outs = R.replay_symbol("FLAT", flat_series(), "blowoff")
        self.assertEqual(outs, [])

    def test_no_overlapping_positions(self):
        bars = blowoff_series()
        outs = R.replay_symbol("BEAT", bars, "blowoff")
        # entries must be strictly increasing in ts (a new entry only after the prior closed)
        ts = [o["entry_ts"] for o in outs]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(len(ts), len(set(ts)))


class TestReport(unittest.TestCase):
    def _rows(self):
        return [
            {"symbol": "BEAT", "setup": "blowoff", "signature": "blowoff_top_short",
             "outcome": "tp1", "pnl_r": 1.0, "mae_r": 0.4},
            {"symbol": "BEAT", "setup": "blowoff", "signature": "blowoff_top_short",
             "outcome": "stopped", "pnl_r": -1.0, "mae_r": 1.0},
            {"symbol": "FOLKS", "setup": "blowoff", "signature": "blowoff_top_short",
             "outcome": "tp2", "pnl_r": 2.0, "mae_r": 0.6},
        ]

    def test_report_aggregates_per_signature(self):
        rep = R.report(self._rows())
        sig = rep["signatures"]["blowoff_top_short"]
        self.assertEqual(sig["n"], 3)
        self.assertEqual(sig["hit_pct"], round(100 * 2 / 3, 1))   # 2 hits of 3 decided
        self.assertAlmostEqual(sig["avg_r"], round((1 - 1 + 2) / 3, 3))
        self.assertEqual(sig["max_adverse_excursion_r"], 1.0)
        self.assertIn("BEAT", sig["by_symbol"])
        self.assertEqual(sig["source"], "replay")


class TestLedgerImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = L.LEDGER_PATH
        L.LEDGER_PATH = Path(self.tmp.name) / "ledger.jsonl"

    def tearDown(self):
        L.LEDGER_PATH = self._orig
        self.tmp.cleanup()

    def test_import_tags_rows_source_replay(self):
        rows = [
            {"symbol": "BEAT", "setup": "blowoff", "signature": "blowoff_top_short",
             "outcome": "tp1", "pnl_r": 1.0, "entry_px": 1.0, "stop": 1.1, "tp": [0.9, 0.8],
             "score": 5, "required": 5},
            {"symbol": "FOLKS", "setup": "blowoff", "signature": "blowoff_top_short",
             "outcome": "time_stop", "pnl_r": 0.3, "entry_px": 1.0, "stop": 1.1, "tp": [0.9, 0.8],
             "score": 5, "required": 5},
        ]
        res = R.ledger_import(rows, ledger_path=str(L.LEDGER_PATH))
        self.assertEqual(res["imported"], 2)
        recs = [__import__("json").loads(x) for x in L.LEDGER_PATH.read_text().splitlines() if x.strip()]
        self.assertTrue(all(r["source"] == "replay" for r in recs))
        # time_stop maps to the signal-only retired_unfilled
        self.assertIn("retired_unfilled", [r["outcome"] for r in recs])


if __name__ == "__main__":
    unittest.main(verbosity=2)
