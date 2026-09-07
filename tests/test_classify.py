#!/usr/bin/env python3
"""SPEC 39 — classify must evaluate the PRICE leg of the committed thesis.

LIVE PROOF this guards: VELVET (SHORT, stop 0.40, tp [0.30, 0.184]) printed a 24h
high of 0.4749 (stop breached +19%) and a low of 0.2642 (TP1 traded through), yet
classify returned CONFIRMS off the funding leg alone — the worst false verdict,
because §0.5 treats CONFIRMS as "report one line and STOP".

Rule under test: classify compares the high/low range since thesis commit against
the thesis's own stop/tp/entry_zone. A stop printed on a wick IS a break. Stop-breach
beats everything; price-leg BREAKS/TRIGGERS beats the funding/regime leg; the funding
leg can only set the verdict when the price leg is silent.

Offline-deterministic: price_window_range + scan_trigger_logs are monkeypatched.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import classify as CL


def _tok(direction="SHORT", stop=0.40, tp=None, status="ACTIVE", zone=None,
         state="SHORT stage5 — funding -0.10%/4h"):
    th = {
        "direction": direction, "status": status, "setup": "stage5",
        "stop": stop, "tp": [0.30, 0.184] if tp is None else tp,
        "entry_zone": zone, "committed_ts": "2026-06-08",
    }
    return {"ticker": "VELVETX", "state": state, "thesis": th}


# funding neg but NOT deep-neg, memo agrees → rf leg alone says CONFIRM
LIVE_QUIET = {
    "venue": "binance", "primary_venue": "binance",
    "price": 0.36, "chg24": -5.0, "vol_m": 40.0, "oi": 1e6,
    "funding_pi": -0.05, "funding_4h": -0.10, "interval_min": 480,
    "funding_stale": False, "all_floor": False, "funding_split": False,
}


def _rng(high, low, window="24h-ticker:binance", candles=None):
    return {"high": high, "low": low, "window": window, "candles": candles}


class PriceLegTests(unittest.TestCase):
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

    # ── DoD fixture 1: VELVET — stop wicked AND TP1 traded → BREAKS, names the stop ──
    def test_velvet_stop_breach_is_breaks_not_confirms(self):
        self._patch_range(_rng(0.4749, 0.2642))
        r = CL.classify_token(_tok(), LIVE_QUIET)
        self.assertEqual(r["verdict"], "BREAKS")
        self.assertIn("stop", r["reason"].lower())
        self.assertIn("0.4", r["reason"])           # names the breached stop level
        pl = r["price_leg"]
        self.assertTrue(pl["stop_breached"])
        self.assertEqual(pl["high"], 0.4749)
        self.assertEqual(pl["low"], 0.2642)
        self.assertIn(0.30, pl["tps_printed"])
        self.assertTrue(pl["window"])

    # ── DoD fixture 2: stop never printed, TP1 traded → TRIGGERS ──
    def test_tp_printed_without_stop_is_triggers(self):
        self._patch_range(_rng(0.395, 0.264))
        r = CL.classify_token(_tok(), LIVE_QUIET)
        self.assertEqual(r["verdict"], "TRIGGERS")
        pl = r["price_leg"]
        self.assertFalse(pl["stop_breached"])
        self.assertIn(0.30, pl["tps_printed"])

    # ── DoD fixture 3: LONG mirror — low breaches the stop below → BREAKS ──
    def test_long_mirror_low_breaches_stop(self):
        self._patch_range(_rng(1.05, 0.88))
        tok = _tok(direction="LONG", stop=0.90, tp=[1.40],
                   state="LONG squeeze-fuel — funding -0.10%/4h")
        r = CL.classify_token(tok, LIVE_QUIET)
        self.assertEqual(r["verdict"], "BREAKS")
        self.assertTrue(r["price_leg"]["stop_breached"])
        self.assertEqual(r["price_leg"]["tps_printed"], [])

    # ── DoD fixture 4: quiet range + consistent funding → CONFIRMS preserved ──
    def test_quiet_range_consistent_funding_confirms(self):
        self._patch_range(_rng(0.385, 0.31))
        r = CL.classify_token(_tok(), LIVE_QUIET)
        self.assertEqual(r["verdict"], "CONFIRMS")
        self.assertIn("rf=", r["reason"])
        pl = r["price_leg"]
        self.assertFalse(pl["stop_breached"])
        self.assertEqual(pl["tps_printed"], [])

    # ── stop-breach precedence: both stop and TP printed in window → BREAKS ──
    def test_stop_beats_tp_when_both_print(self):
        self._patch_range(_rng(0.4749, 0.17))     # TP1 AND TP2 AND stop all printed
        r = CL.classify_token(_tok(), LIVE_QUIET)
        self.assertEqual(r["verdict"], "BREAKS")

    # ── kline order resolves the sequence when candles are available ──
    def test_sequence_reported_when_klines_resolve_it(self):
        candles = [
            {"ts": 1780300800.0, "high": 0.38, "low": 0.295},   # TP1 0.30 prints first
            {"ts": 1780304400.0, "high": 0.41, "low": 0.36},    # then stop 0.40 breaches
        ]
        self._patch_range(_rng(0.41, 0.295, window="klines:binance:2x1h", candles=candles))
        r = CL.classify_token(_tok(), LIVE_QUIET)
        self.assertEqual(r["verdict"], "BREAKS")
        self.assertIn("then stop", r["reason"])

    # ── price-leg TRIGGERS wins over an rf-leg BREAKS (funding leg is demoted) ──
    def test_price_leg_triggers_wins_over_rf_breaks(self):
        live_deepneg = dict(LIVE_QUIET, funding_pi=-0.25, funding_4h=-0.50)
        # memo SHORT + deep-neg live funding → rf leg alone = REGIME_FLIP → BREAKS
        self._patch_range(_rng(0.395, 0.264))     # TP1 printed, stop intact
        r = CL.classify_token(_tok(), live_deepneg)
        self.assertEqual(r["verdict"], "TRIGGERS")

    # ── ARMED thesis: range enters the entry zone, stop intact → TRIGGERS ──
    def test_armed_entry_zone_print_triggers(self):
        self._patch_range(_rng(0.376, 0.345))
        tok = _tok(stop=0.40, tp=[0.30], status="ARMED", zone=[0.37, 0.39])
        r = CL.classify_token(tok, LIVE_QUIET)
        self.assertEqual(r["verdict"], "TRIGGERS")
        self.assertTrue(r["price_leg"]["entered_zone"])

    # ── dead theses (RETIRED/PASS) never hit the price leg (no fetch, no verdict) ──
    def test_retired_thesis_skips_price_leg(self):
        self._patch_range(_rng(0.4749, 0.2642))
        tok = _tok(status="RETIRED")
        r = CL.classify_token(tok, LIVE_QUIET)
        self.assertEqual(self.range_calls, [])
        self.assertIsNone(r.get("price_leg"))

    # ── range unfetchable → price leg silent, funding leg still classifies ──
    def test_unfetchable_range_falls_back_to_rf_leg(self):
        self._patch_range(None)
        r = CL.classify_token(_tok(), LIVE_QUIET)
        self.assertEqual(r["verdict"], "CONFIRMS")
        self.assertIn("rf=", r["reason"])


class EvalPriceLegPure(unittest.TestCase):
    """eval_price_leg is a pure function — direct edge coverage."""

    def test_short_wick_to_stop_exactly_is_a_break(self):
        th = {"direction": "SHORT", "stop": 0.40, "tp": [0.30], "status": "ACTIVE"}
        leg = CL.eval_price_leg(th, "SHORT", _rng(0.40, 0.35))
        self.assertTrue(leg["stop_breached"])     # touch = printed

    def test_banked_tp_not_re_triggered(self):
        th = {"direction": "SHORT", "stop": 0.40, "tp": [0.30, 0.184],
              "banked": [0.30], "status": "ACTIVE"}
        leg = CL.eval_price_leg(th, "SHORT", _rng(0.38, 0.29))
        self.assertEqual(leg["tps_printed"], [])  # 0.30 already banked
        self.assertIsNone(leg["verdict"])

    def test_long_tp_printed_above(self):
        th = {"direction": "LONG", "stop": 0.90, "tp": [1.40], "status": "ACTIVE"}
        leg = CL.eval_price_leg(th, "LONG", _rng(1.45, 0.95))
        self.assertEqual(leg["verdict"], "TRIGGERS")
        self.assertIn(1.40, leg["tps_printed"])

    # ── SPEC-78: PENDING short whose entry never printed → stop NOT watched ──
    def test_pending_unentered_short_stop_not_evaluated(self):
        # RIVER 06-17: entry [4.15,4.32] below a 4.80 stop, price drifted UP to 5.07
        # and never fell into the zone. The committed stop must sit quiet (no false break).
        th = {"direction": "SHORT", "stop": 4.80, "tp": [4.0],
              "status": "PENDING", "entry_zone": [4.15, 4.32]}
        leg = CL.eval_price_leg(th, "SHORT", _rng(5.07, 5.04))
        self.assertIsNone(leg["stop_breached"])   # not evaluated pre-entry
        self.assertEqual(leg["tps_printed"], [])
        self.assertFalse(leg["entered_zone"])     # 5.04 never fell into [4.15,4.32]
        self.assertIsNone(leg["verdict"])

    # ── SPEC-78: once the zone prints, the position is live and the stop IS watched ──
    def test_pending_short_enters_then_stop_breaks(self):
        th = {"direction": "SHORT", "stop": 4.80, "tp": [4.0],
              "status": "PENDING", "entry_zone": [4.15, 4.32]}
        # range fell into the zone (low 4.18) AND wicked back through the 4.80 stop (high 5.07)
        leg = CL.eval_price_leg(th, "SHORT", _rng(5.07, 4.18))
        self.assertTrue(leg["stop_breached"])     # entry printed → stop watched → breach
        self.assertEqual(leg["verdict"], "BREAKS")

    # ── SPEC-78: an entered_ts latch keeps the stop watched even while status stays PENDING ──
    def test_pending_latched_entered_ts_watches_stop(self):
        th = {"direction": "SHORT", "stop": 4.80, "tp": [4.0], "status": "PENDING",
              "entry_zone": [4.15, 4.32], "entered_ts": "2026-06-17T00:00:00Z"}
        leg = CL.eval_price_leg(th, "SHORT", _rng(5.07, 5.04))
        self.assertTrue(leg["stop_breached"])
        self.assertEqual(leg["verdict"], "BREAKS")

    # ── SPEC-78 regression: an OPEN/live thesis watches its stop exactly like today ──
    def test_open_live_thesis_watches_stop_unconditionally(self):
        th = {"direction": "SHORT", "stop": 4.80, "tp": [4.0], "status": "OPEN",
              "entry_zone": [4.15, 4.32]}
        leg = CL.eval_price_leg(th, "SHORT", _rng(5.07, 5.04))
        self.assertTrue(leg["stop_breached"])
        self.assertEqual(leg["verdict"], "BREAKS")


class Spec78PendingShortBoard(unittest.TestCase):
    """SPEC-78 end-to-end: a PENDING breakdown short must not false-BREAK on its stop
    before entry prints. Funding is held consistent (neutral/mild-neg) so the only leg
    that could fire is the price leg — isolating the fix under test."""

    def setUp(self):
        self._orig_range = CL.price_window_range
        self._orig_scan = CL.scan_trigger_logs
        CL.scan_trigger_logs = lambda ticker, since_epoch=None: ([], [])

    def tearDown(self):
        CL.price_window_range = self._orig_range
        CL.scan_trigger_logs = self._orig_scan

    def _patch_range(self, rng):
        CL.price_window_range = lambda ticker, venue="binance", since_epoch=None: rng

    def _river(self, status="PENDING", entered_ts=None):
        th = {"direction": "SHORT", "status": status, "setup": "stage5",
              "stop": 4.80, "tp": [4.0], "entry_zone": [4.15, 4.32],
              "committed_ts": "2026-06-16"}
        if entered_ts:
            th["entered_ts"] = entered_ts
        return {"ticker": "RIVERX",
                "state": "SHORT breakdown — arm on 90d ATL break, funding -0.10%/4h",
                "thesis": th}

    # DoD: price drifts UP to 5.07, never enters → board stays CONFIRMS (no fake BREAK)
    def test_river_drift_up_stays_confirms(self):
        self._patch_range(_rng(5.07, 5.04))
        r = CL.classify_token(self._river(), LIVE_QUIET)
        self.assertEqual(r["verdict"], "CONFIRMS")
        self.assertNotIn("STOP BREACHED", r["reason"])
        self.assertIn(r["price_leg"]["stop_breached"], (None, False))

    # DoD: enters the zone then reclaims the stop → BREAKS (stop watched post-entry)
    def test_river_enters_then_reclaims_breaks(self):
        self._patch_range(_rng(5.07, 4.18))
        r = CL.classify_token(self._river(), LIVE_QUIET)
        self.assertEqual(r["verdict"], "BREAKS")
        self.assertTrue(r["price_leg"]["stop_breached"])


if __name__ == "__main__":
    unittest.main()
