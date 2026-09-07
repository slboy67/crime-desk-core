#!/usr/bin/env python3
"""SPEC 31 — `tape` capability: per-minute OI/price/taker/liquidation microstructure classifier.

Run:  python3 tests/test_tape.py

Operationalizes the operator-seat OI-reading lens (memory:
feedback_read_oi_price_from_operator_seat): four forces move OI at once, so the net print
is near-meaningless technically — `tape` classifies each candle into the four-force
vocabulary deterministically and flags the staged-play sequences (fake-breakdown short
recruitment, chain-squeeze through a round number, tail-of-liquidation, downtrend slam).
Read-only intel — NO auto-verdict. Pure classifier/detector fns tested directly on
synthetic bars; the envelope test patches the fetch layer — offline-deterministic.
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


# ── 1. per-bar four-force classification (the deterministic core) ──────────────
class TestClassifyForce(unittest.TestCase):
    def test_longs_opening_oi_up_price_up(self):
        force, forced = TP.classify_force(d_oi_pct=0.8, d_price_pct=0.5, taker_imb=0.3)
        self.assertEqual(force, "longs_opening")
        self.assertFalse(forced)

    def test_shorts_opening_oi_up_price_down(self):
        force, _ = TP.classify_force(d_oi_pct=0.8, d_price_pct=-0.4, taker_imb=-0.2)
        self.assertEqual(force, "shorts_opening")

    def test_shorts_opening_oi_up_price_FLAT_is_recruitment(self):
        # the SKYAI lesson: flat tape + OI building = shorts being recruited, not calm
        force, _ = TP.classify_force(d_oi_pct=0.6, d_price_pct=0.0, taker_imb=-0.1)
        self.assertEqual(force, "shorts_opening")

    def test_longs_closing_oi_down_price_down(self):
        force, _ = TP.classify_force(d_oi_pct=-0.7, d_price_pct=-0.5, taker_imb=-0.3)
        self.assertEqual(force, "longs_closing")

    def test_shorts_closing_voluntary_oi_down_price_up_no_liq(self):
        force, forced = TP.classify_force(d_oi_pct=-0.7, d_price_pct=0.6, taker_imb=0.4,
                                          liq_long_usd=0, liq_short_usd=0)
        self.assertEqual(force, "shorts_closing")
        self.assertFalse(forced, "no liq feed → voluntary cover, not forced")

    def test_shorts_closing_FORCED_via_short_liq_feed(self):
        # short positions liquidated = forced BUY-to-close on price↑ (chain-squeeze fuel)
        force, forced = TP.classify_force(d_oi_pct=-0.9, d_price_pct=1.2, taker_imb=0.7,
                                          liq_long_usd=0, liq_short_usd=50000)
        self.assertEqual(force, "shorts_closing")
        self.assertTrue(forced)

    def test_flat_when_both_within_eps(self):
        force, _ = TP.classify_force(d_oi_pct=0.0, d_price_pct=0.0, taker_imb=0.0)
        self.assertEqual(force, "flat")


# ── 2. classify_bars: assembles 1m price/taker + 5m OI mapped on, per bar ───────
def _kline(ts, o, h, l, c, vol, taker_buy):
    # Binance kline array shape (only the fields tape uses need to be right)
    return [ts, str(o), str(h), str(l), str(c), str(vol), ts + 59999,
            "0", 0, str(taker_buy), "0", "0"]


class TestClassifyBars(unittest.TestCase):
    def test_oi_mapped_5m_bucket_and_taker_from_kline(self):
        # 6 one-minute bars; OI sampled every 5m. Bars in the same 5m bucket share ΔOI sign.
        base = 1_780_000_000_000
        klines = [_kline(base + i * 60000, 1.0, 1.0, 1.0, 1.0, 1000, 700) for i in range(6)]
        # OI rises across the first→second 5m bucket
        oi_series = [{"ts": base - 300000, "oi": 100.0},
                     {"ts": base, "oi": 100.0},
                     {"ts": base + 300000, "oi": 110.0}]
        bars = TP.classify_bars(klines, oi_series, liq_series=None, oi_interval="5m")
        self.assertEqual(len(bars), 6)
        # taker imbalance from kline: (2*700-1000)/1000 = 0.4
        self.assertAlmostEqual(bars[0]["taker_imb"], 0.4, places=3)
        # the bar at +5m (index 5) sits in the bucket that rose 100→110 → d_oi positive
        self.assertGreater(bars[5]["d_oi"], 0)
        # every bar carries the contract fields
        for b in bars:
            for k in ("ts", "price", "d_price_pct", "d_oi", "oi_force", "taker_imb",
                      "liq_long_usd", "liq_short_usd"):
                self.assertIn(k, b)

    def test_mixed_bar_carries_dominant_component(self):
        # OI down (5m bucket) but price flat → mixed, with a named dominant component
        base = 1_780_000_000_000
        klines = [_kline(base + i * 60000, 1.0, 1.0, 1.0, 1.0, 1000, 900) for i in range(3)]
        oi_series = [{"ts": base - 300000, "oi": 120.0}, {"ts": base, "oi": 100.0}]  # OI dropped
        bars = TP.classify_bars(klines, oi_series, None, oi_interval="5m")
        mixed = [b for b in bars if b["oi_force"] == "mixed"]
        self.assertTrue(mixed, "expected a mixed bar (OI↓ price-flat)")
        self.assertIn("dominant", mixed[0])


# ── 3. pattern detectors on fixture sequences ──────────────────────────────────
def _bar(i, price, low=None, high=None, close=None, force="flat", forced=False,
         d_price_pct=0.0, d_oi=0.0, liq_long=0.0, liq_short=0.0, taker=0.0):
    return {"ts": 1_780_000_000_000 + i * 60000, "price": price,
            "low": low if low is not None else price,
            "high": high if high is not None else price,
            "close": close if close is not None else price,
            "d_price_pct": d_price_pct, "d_oi": d_oi, "oi_force": force, "forced": forced,
            "taker_imb": taker, "liq_long_usd": liq_long, "liq_short_usd": liq_short}


class TestPatternDetectors(unittest.TestCase):
    def _types(self, patterns):
        return {p["type"] for p in patterns}

    def test_tail_of_liquidation_consecutive_longs_closing(self):
        bars = [
            _bar(0, 1.00, force="longs_opening", d_price_pct=0.5, d_oi=1),
            _bar(1, 0.99, force="longs_closing", d_price_pct=-0.5, d_oi=-1),
            _bar(2, 0.98, force="longs_closing", d_price_pct=-0.6, d_oi=-1),
            _bar(3, 0.97, force="longs_closing", d_price_pct=-0.7, d_oi=-1),
            _bar(4, 0.99, force="shorts_closing", d_price_pct=0.8, d_oi=-1),
        ]
        pats = TP.detect_patterns(bars, level=None, round_numbers=[])
        self.assertIn("tail_of_liquidation", self._types(pats))

    def test_fake_breakdown_wick_below_level_with_shorts_opening(self):
        level = 0.1836
        # price wicks below the support, shorts pile in, then it recovers within K bars
        bars = [
            _bar(0, 0.1870, low=0.1865, close=0.1868, force="shorts_opening", d_oi=1, d_price_pct=-0.1),
            _bar(1, 0.1840, low=0.1825, close=0.1838, force="shorts_opening", d_oi=1, d_price_pct=-0.3),  # break below 0.1836
            _bar(2, 0.1855, low=0.1840, close=0.1854, force="shorts_closing", d_oi=-1, d_price_pct=0.8, forced=True, liq_short=20000),  # wick back above
        ]
        pats = TP.detect_patterns(bars, level=level, round_numbers=[])
        self.assertIn("fake_breakdown_wick", self._types(pats))
        fb = next(p for p in pats if p["type"] == "fake_breakdown_wick")
        self.assertLess(fb["detail"]["break_low"], level)

    def test_no_fake_breakdown_when_it_stays_broken(self):
        level = 0.1836
        bars = [
            _bar(0, 0.1840, low=0.1838, close=0.1839, force="shorts_opening", d_oi=1, d_price_pct=-0.1),
            _bar(1, 0.1820, low=0.1810, close=0.1815, force="shorts_opening", d_oi=1, d_price_pct=-1.3),
            _bar(2, 0.1800, low=0.1790, close=0.1795, force="longs_closing", d_oi=-1, d_price_pct=-1.1),  # stays below — real breakdown
        ]
        pats = TP.detect_patterns(bars, level=level, round_numbers=[])
        self.assertNotIn("fake_breakdown_wick", self._types(pats))

    def test_chain_squeeze_through_round_number(self):
        # rising tape, shorts recruited (shorts_opening) below 0.20, then squeeze up through it
        bars = [
            _bar(0, 0.1900, close=0.1900, force="shorts_opening", d_oi=1, d_price_pct=0.1),
            _bar(1, 0.1950, close=0.1950, force="shorts_opening", d_oi=1, d_price_pct=0.3),
            _bar(2, 0.1990, close=0.1990, force="shorts_opening", d_oi=1, d_price_pct=0.2),
            _bar(3, 0.2030, close=0.2030, force="shorts_closing", forced=True, d_oi=-1, d_price_pct=2.0, liq_short=40000),  # crosses 0.20 up, short liqs
            _bar(4, 0.2085, close=0.2085, force="shorts_closing", forced=True, d_oi=-1, d_price_pct=2.7, liq_short=60000),
        ]
        pats = TP.detect_patterns(bars, level=None, round_numbers=[0.20])
        self.assertIn("chain_squeeze", self._types(pats))
        cs = next(p for p in pats if p["type"] == "chain_squeeze")
        self.assertEqual(cs["detail"]["round_number"], 0.20)

    def test_chain_squeeze_bounded_and_deduped(self):
        # rise through round numbers (the squeeze) then a long flat tail. The episode must be
        # bounded to the rising leg (not run into the flat tail) and emit no duplicate ranges
        # even with several nearby round numbers crossed.
        bars = []
        for i in range(10):                      # rising leg 0.190 → 0.217 through 0.20, 0.21
            px = round(0.190 + i * 0.003, 4)
            bars.append(_bar(i, px, close=px, force="shorts_opening", d_oi=1,
                             d_price_pct=0.3 if i else 0.0))
        for j in range(10, 40):                  # 30-bar flat tail at the top
            bars.append(_bar(j, 0.217, close=0.217, force="flat", d_price_pct=0.0))
        pats = TP.detect_patterns(bars, level=None, round_numbers=[0.20, 0.21])
        cs = [p for p in pats if p["type"] == "chain_squeeze"]
        self.assertTrue(cs, "expected a chain_squeeze on the rising leg")
        ranges = [tuple(p["bar_range"]) for p in cs]
        self.assertEqual(len(ranges), len(set(ranges)), f"duplicate chain_squeeze ranges: {ranges}")
        for p in cs:                              # bounded: ends at the peak (~bar 9), not deep in the flat tail
            self.assertLessEqual(p["bar_range"][1], 12, f"squeeze ran into the flat tail: {p['bar_range']}")

    def test_downtrend_slam_and_bounce_cover(self):
        bars = [
            _bar(0, 1.00, force="longs_closing", d_oi=-1, d_price_pct=-0.6),
            _bar(1, 0.98, force="shorts_opening", d_oi=1, d_price_pct=-0.8),
            _bar(2, 0.96, force="longs_closing", d_oi=-1, d_price_pct=-0.7),
            _bar(3, 0.95, force="shorts_opening", d_oi=1, d_price_pct=-0.5),
            _bar(4, 0.97, force="shorts_closing", d_oi=-1, d_price_pct=1.0),  # counter-trend bounce = covering
            _bar(5, 0.975, force="shorts_closing", d_oi=-1, d_price_pct=0.5),
        ]
        pats = TP.detect_patterns(bars, level=None, round_numbers=[])
        types = self._types(pats)
        self.assertIn("downtrend_slam", types)
        self.assertIn("bounce_cover", types)


# ── 4. round-number helper ─────────────────────────────────────────────────────
class TestRoundNumbers(unittest.TestCase):
    def test_round_numbers_near_includes_the_round_level(self):
        rns = TP.round_numbers_near(0.183, 0.209)
        self.assertIn(0.20, [round(r, 4) for r in rns])


# ── 5. envelope (fetch layer patched) ──────────────────────────────────────────
class TestEnvelope(unittest.TestCase):
    def setUp(self):
        self._k, self._o, self._f = TP.fetch_klines_1m, TP.fetch_oi_hist, TP.fetch_force_orders
        base = 1_780_000_000_000
        TP.fetch_klines_1m = lambda sym, limit, venue="binance": [
            _kline(base + i * 60000, 0.19 + i * 0.001, 0.19 + i * 0.001 + 0.0005,
                   0.19 + i * 0.001 - 0.0005, 0.19 + i * 0.001, 1000, 650) for i in range(10)]
        TP.fetch_oi_hist = lambda sym, period, limit, venue="binance": [
            {"ts": base + i * 300000, "oi": 100.0 + i} for i in range(4)]
        TP.fetch_force_orders = lambda sym, limit, venue="binance": None  # public REST unavailable

    def tearDown(self):
        TP.fetch_klines_1m, TP.fetch_oi_hist, TP.fetch_force_orders = self._k, self._o, self._f

    def test_build_tape_envelope_shape(self):
        t = TP.build_tape("SKYAI", window_min=10)
        for k in ("ticker", "venue", "oi_interval", "window_min", "bars", "patterns",
                  "round_numbers_near", "liq_available"):
            self.assertIn(k, t)
        self.assertEqual(t["ticker"], "SKYAI")
        self.assertEqual(t["oi_interval"], "5m")          # granularity floor surfaced, no pretend-1m
        self.assertFalse(t["liq_available"])              # forceOrders unavailable → degrade-explicit
        self.assertTrue(len(t["bars"]) > 0)
        self.assertIsInstance(t["patterns"], list)

    def test_oi_interval_not_silently_pretend_1m(self):
        t = TP.build_tape("SKYAI", window_min=10)
        # every bar's d_oi derives from 5m OI; the floor is declared, never claimed as 1m
        self.assertNotEqual(t["oi_interval"], "1m")


if __name__ == "__main__":
    unittest.main(verbosity=2)
