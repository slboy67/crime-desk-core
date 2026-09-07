#!/usr/bin/env python3
"""SPEC-188 §3 — classify price-leg/watch-leg events carry venue_agreement.

Offline-deterministic: price_window_range + scan_trigger_logs + venue_agreement_for
are monkeypatched (mirrors tests/test_classify.py's pattern) for the classify_token
integration tests; venue_agreement_for's own FULL/PARTIAL/SINGLE logic is tested
directly against a stubbed venue_bars.build_venue_bars/venue_map.build_venue_map.
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import classify as CL
import venue_bars as VB

LIVE_TS = int(time.time() // 3600 * 3600)   # level_agreement defaults bar_ts to the live 1h bucket


def _tok(direction="SHORT", stop=0.40, tp=None, status="ACTIVE", zone=None,
         state="SHORT stage5 — funding -0.10%/4h"):
    th = {
        "direction": direction, "status": status, "setup": "stage5",
        "stop": stop, "tp": [0.30, 0.184] if tp is None else tp,
        "entry_zone": zone, "committed_ts": "2026-06-08",
    }
    return {"ticker": "VELVETX", "state": state, "thesis": th}


def _watch_tok(watch_levels, status="ACTIVE"):
    th = {"direction": "SHORT", "status": status, "setup": "watch",
         "watch_level": watch_levels, "committed_ts": "2026-06-08"}
    return {"ticker": "VELVETX", "state": "watch", "thesis": th}


LIVE_QUIET = {
    "venue": "binance", "primary_venue": "binance",
    "price": 0.36, "chg24": -5.0, "vol_m": 40.0, "oi": 1e6,
    "funding_pi": -0.05, "funding_4h": -0.10, "interval_min": 480,
    "funding_stale": False, "all_floor": False, "funding_split": False,
}


def _rng(high, low, window="24h-ticker:binance", candles=None):
    return {"high": high, "low": low, "window": window, "candles": candles}


class TestClassifyTokenVenueAgreementWiring(unittest.TestCase):
    """price_leg / watch_leg events carry venue_agreement; the reason string carries
    the (tape: LABEL n/total) tag; the verdict itself is UNCHANGED by the label."""

    def setUp(self):
        self._orig_range = CL.price_window_range
        self._orig_scan = CL.scan_trigger_logs
        self._orig_va = CL.venue_agreement_for
        CL.scan_trigger_logs = lambda ticker, since_epoch=None: ([], [])
        self.va_calls = []

    def tearDown(self):
        CL.price_window_range = self._orig_range
        CL.scan_trigger_logs = self._orig_scan
        CL.venue_agreement_for = self._orig_va

    def _patch_range(self, rng):
        CL.price_window_range = lambda ticker, venue="binance", since_epoch=None: rng

    def _patch_va(self, result):
        def fake(ticker, level, direction):
            self.va_calls.append((ticker, level, direction))
            return result
        CL.venue_agreement_for = fake

    def test_stop_breach_carries_venue_agreement_and_tape_tag(self):
        self._patch_range(_rng(0.4749, 0.2642))
        va = {"label": "PARTIAL", "n_crossed": 6, "n_total": 14, "crossed": ["binance"],
              "closed_beyond": [], "size_book_venue": "bybit"}
        self._patch_va(va)
        r = CL.classify_token(_tok(), LIVE_QUIET)
        self.assertEqual(r["verdict"], "BREAKS")   # verdict UNCHANGED by the label (§0.5)
        self.assertEqual(r["price_leg"]["venue_agreement"], va)
        self.assertIn("(tape: PARTIAL 6/14)", r["reason"])
        # stop breach on a SHORT -> crossing direction "above" (price rose through it)
        self.assertEqual(self.va_calls, [("VELVETX", 0.40, "above")])

    def test_tp_trigger_carries_venue_agreement(self):
        self._patch_range(_rng(0.395, 0.264))
        va = {"label": "FULL", "n_crossed": 14, "n_total": 14, "crossed": ["binance", "aster", "bybit"],
              "closed_beyond": ["binance"], "size_book_venue": "bybit"}
        self._patch_va(va)
        r = CL.classify_token(_tok(), LIVE_QUIET)
        self.assertEqual(r["verdict"], "TRIGGERS")
        self.assertEqual(r["price_leg"]["venue_agreement"]["label"], "FULL")
        self.assertIn("(tape: FULL 14/14)", r["reason"])
        # tp on a SHORT -> crossing direction "below"
        self.assertEqual(self.va_calls, [("VELVETX", 0.30, "below")])

    def test_long_mirror_stop_breach_direction_below(self):
        self._patch_range(_rng(1.05, 0.88))
        self._patch_va({"label": "SINGLE", "n_crossed": 1, "n_total": 14, "crossed": ["binance"],
                        "closed_beyond": [], "size_book_venue": None})
        tok = _tok(direction="LONG", stop=0.90, tp=[1.40],
                  state="LONG squeeze-fuel — funding -0.10%/4h")
        r = CL.classify_token(tok, LIVE_QUIET)
        self.assertEqual(r["verdict"], "BREAKS")
        self.assertEqual(self.va_calls, [("VELVETX", 0.90, "below")])
        self.assertIn("(tape: SINGLE 1/14)", r["reason"])

    def test_venue_agreement_none_never_breaks_the_verdict(self):
        """venue_agreement_for failing (network dead) must not touch the verdict or
        crash classify_token — it's a live cross-venue call layered on TOP of the
        already-final Binance-primary read."""
        self._patch_range(_rng(0.4749, 0.2642))
        self._patch_va(None)
        r = CL.classify_token(_tok(), LIVE_QUIET)
        self.assertEqual(r["verdict"], "BREAKS")
        self.assertIsNone(r["price_leg"]["venue_agreement"])
        self.assertNotIn("(tape:", r["reason"])

    def test_watch_armed_carries_venue_agreement(self):
        wl = [{"price": 0.50, "dir": "above", "note": "ATH watch"}]
        self._patch_range(_rng(0.55, 0.30))
        va = {"label": "PARTIAL", "n_crossed": 9, "n_total": 14, "crossed": ["binance"],
              "closed_beyond": [], "size_book_venue": "bitget"}
        self._patch_va(va)
        r = CL.classify_token(_watch_tok(wl), LIVE_QUIET)
        self.assertEqual(r["verdict"], "WATCH-ARMED")
        self.assertEqual(r["watch_leg"]["venue_agreement"], va)
        self.assertIn("(tape: PARTIAL 9/14)", r["reason"])
        self.assertEqual(self.va_calls, [("VELVETX", 0.50, "above")])


class TestVenueAgreementForLogic(unittest.TestCase):
    """venue_agreement_for's own FULL/PARTIAL/SINGLE derivation, against a stubbed
    venue_bars.build_venue_bars + venue_map.build_venue_map (no network)."""

    def setUp(self):
        self._orig_bvb = VB.build_venue_bars
        try:
            import venue_map as VM
            self._VM = VM
            self._orig_bvm = VM.build_venue_map
        except Exception:
            self._VM = None

    def tearDown(self):
        VB.build_venue_bars = self._orig_bvb
        if self._VM:
            self._VM.build_venue_map = self._orig_bvm

    def _stub(self, crossed_venues, all_venues=("binance", "bybit", "aster", "okx")):
        def fake(ticker, interval="1h", n=6):
            venues = {}
            for v in all_venues:
                bar = {"ts": LIVE_TS, "o": 1, "h": (2 if v in crossed_venues else 0.5),
                      "l": 0.1, "c": 1, "live": True}
                venues[v] = {"venue": v, "available": True, "bars": [bar]}
            return {"ticker": ticker, "interval": interval, "n": n, "venues": venues,
                    "n_total": len(all_venues), "n_available": len(all_venues),
                    "bars": [], "dominant_tape": None, "execution_venue": "aster"}
        VB.build_venue_bars = fake

    def test_full_requires_binance_size_book_and_aster(self):
        self._stub(crossed_venues={"binance", "bybit", "aster"})
        if self._VM:
            self._VM.build_venue_map = lambda t: {"top_oi_venue": "bybit"}
        va = CL.venue_agreement_for("OP", 1.0, "above")
        self.assertEqual(va["label"], "FULL")
        self.assertEqual(va["n_crossed"], 3)

    def test_partial_when_size_book_venue_missing(self):
        self._stub(crossed_venues={"binance", "aster"})   # bybit (size book) did NOT cross
        if self._VM:
            self._VM.build_venue_map = lambda t: {"top_oi_venue": "bybit"}
        va = CL.venue_agreement_for("OP", 1.0, "above")
        self.assertEqual(va["label"], "PARTIAL")

    def test_single_when_exactly_one_venue_crosses(self):
        self._stub(crossed_venues={"binance"})
        if self._VM:
            self._VM.build_venue_map = lambda t: {"top_oi_venue": "bybit"}
        va = CL.venue_agreement_for("OP", 1.0, "above")
        self.assertEqual(va["label"], "SINGLE")
        self.assertEqual(va["n_crossed"], 1)

    def test_none_when_no_venue_crosses(self):
        self._stub(crossed_venues=set())
        va = CL.venue_agreement_for("OP", 1.0, "above")
        self.assertIsNone(va)

    def test_none_on_missing_level_or_direction(self):
        self.assertIsNone(CL.venue_agreement_for("OP", None, "above"))
        self.assertIsNone(CL.venue_agreement_for("OP", 1.0, None))


if __name__ == "__main__":
    unittest.main()
