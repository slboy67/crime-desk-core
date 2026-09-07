#!/usr/bin/env python3
"""SPEC 38 — `tape` tail context keys on OI-FORCE composition + OI-shed, not just price location.

Run:  python3 tests/test_tape_context.py

Supersedes SPEC 35's location-only discriminator (which mislabeled VELVET — a real Stage-5
breakdown at mid-range — as `ambiguous`, and would call any low-range run capitulation). The
operator-seat point is WHO is transacting: a longs_closing run that collapses OI = retail
capitulation REGARDLESS of range; a longs_closing tail with ~flat OI alongside shorts_opening /
chain_squeeze = operator shakeout. Location only corroborates. Pure detector — offline.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TP = _load("tape")
BASE = 1_780_000_000_000


def _bar(i, price, force, oi, d_price_pct=0.0, low=None, high=None):
    return {"ts": BASE + i * 60000, "price": price,
            "low": low if low is not None else price,
            "high": high if high is not None else price, "close": price,
            "d_price_pct": d_price_pct, "d_oi": 0.0, "oi": oi, "oi_force": force,
            "forced": False, "taker_imb": 0.0, "liq_long_usd": 0.0, "liq_short_usd": 0.0}


class TestTailContextSpec38(unittest.TestCase):
    def _tail(self, bars, round_numbers=None):
        pats = TP.detect_patterns(bars, level=None, round_numbers=round_numbers or [])
        tails = [p for p in pats if p["type"] == "tail_of_liquidation"]
        self.assertTrue(tails, "expected a tail_of_liquidation")
        return tails[0]["detail"]

    def test_velvet_midrange_breakdown_is_capitulation_not_ambiguous(self):
        # VELVET: longs_closing dominant + OI collapses ~22% over the run, sitting at MID range (~0.55).
        # SPEC 35 called this `ambiguous` (mid-range); SPEC 38 must read the OI collapse → capitulation.
        bars = [_bar(0, 0.80, "longs_opening", 80, 0.0, low=0.80, high=0.82)]
        for j, (px, oi) in enumerate(zip([0.90, 1.00, 1.08, 1.10], [86, 92, 97, 100])):
            bars.append(_bar(1 + j, px, "longs_opening", oi, 1.0, high=px + 0.01))
        for j, (px, oi) in enumerate(zip([1.05, 1.02, 0.99, 0.97, 0.965], [100, 94, 88, 82, 78])):
            bars.append(_bar(5 + j, px, "longs_closing", oi, -1.2))
        d = self._tail(bars)
        self.assertEqual(d["context"], "retail_capitulation")
        self.assertEqual(d["dominant_force"], "longs_closing")
        self.assertGreaterEqual(d["oi_shed_pct_over_run"], 15.0)
        self.assertTrue(0.45 <= d["range_pos_at_event"] <= 0.70, d["range_pos_at_event"])  # genuinely mid-range

    def test_beat_shakeout_flat_oi_with_chain_squeeze_is_profit_take(self):
        # BEAT: shorts recruited + chain_squeeze, then a longs_closing tail at the highs with FLAT OI
        # (a wash, not a real unwind) → operator_profit_take.
        bars = []
        for j, px in enumerate([0.86, 0.90, 0.94, 0.96, 0.99, 1.00]):     # markup recruiting shorts
            bars.append(_bar(j, px, "shorts_opening", 100, 0.5, low=0.85 if j == 0 else None))
        for j, px in enumerate([0.995, 0.99, 0.985, 0.98]):               # tail at the highs, OI flat
            bars.append(_bar(6 + j, px, "longs_closing", 100, -0.4))
        d = self._tail(bars, round_numbers=[0.90, 0.95, 1.00])
        self.assertEqual(d["context"], "operator_profit_take")
        self.assertTrue(d["chain_squeeze_nearby"])
        self.assertLess(d["oi_shed_pct_over_run"], 8.0)                   # ~flat OI = wash, not unwind

    def test_force_overrides_location_high_range_but_oi_collapsing_is_capitulation(self):
        # the case SPEC 35 gets BACKWARDS: a run at range ~0.90 (near high, tiny drawdown) — but it is
        # longs_closing with OI collapsing 22%. Force/OI must override location → retail_capitulation.
        bars = [_bar(0, 0.85, "longs_opening", 80, 0.0, low=0.85)]
        for j, (px, oi) in enumerate(zip([0.95, 1.05, 1.10], [88, 95, 100])):
            bars.append(_bar(1 + j, px, "longs_opening", oi, 1.0, high=px + 0.01))
        for j, (px, oi) in enumerate(zip([1.09, 1.085, 1.08, 1.078, 1.075], [100, 94, 88, 82, 78])):
            bars.append(_bar(4 + j, px, "longs_closing", oi, -0.3))       # tiny price drop, big OI drop
        d = self._tail(bars)                                              # no round_numbers → no chain_squeeze
        self.assertEqual(d["context"], "retail_capitulation")            # NOT operator_profit_take
        self.assertGreaterEqual(d["range_pos_at_event"], 0.80)           # sitting near the high...
        self.assertLess(d["drawdown_from_local_high_pct"], 8.0)          # ...with a tiny drawdown (SPEC 35 → profit_take)
        self.assertGreaterEqual(d["oi_shed_pct_over_run"], 15.0)         # ...but OI is collapsing → capitulation

    def test_detail_carries_force_and_oi_discriminators(self):
        bars = [_bar(0, 1.00, "longs_opening", 100, 0.0)]
        for j, (px, oi) in enumerate(zip([0.96, 0.92, 0.88], [100, 90, 80])):
            bars.append(_bar(1 + j, px, "longs_closing", oi, -4.0))
        d = self._tail(bars)
        for k in ("context", "dominant_force", "oi_shed_pct_over_run", "chain_squeeze_nearby",
                  "range_pos_at_event", "drawdown_from_local_high_pct", "velocity_pct_per_bar"):
            self.assertIn(k, d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
