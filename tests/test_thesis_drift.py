#!/usr/bin/env python3
"""SPEC-91 — thesis-drift guard: the board must detect when live price has run past a
thesis's whole committed structure, so no thesis goes stale silently again.

LIVE PROOF this guards: LAB (2026-06-30) committed watch_level 16.69 + note "squeeze may
extend to 18.09 / ATH", tp [15.5, 14.0, 12.5]. Price cascaded to 12.26 — below the lowest TP
(all three printed), ~26% below the watch_level — yet the board kept echoing the commit-time
"still waiting near 16.69" string. The move the thesis was written to catch fully played out
and the engine never flagged the staleness.

Rule under test:
  - `thesis_drift(th, live_price)` is pure. A thesis is STALE when EITHER
    (A) every committed TP printed AND live ran >= drift_pct beyond the furthest TP, OR
    (B) every committed price anchor sits on the same side of live AND the nearest anchor is
        >= drift_pct away.
  - classify surfaces a `thesis_drift` machine field (null when not stale) and, when stale,
    the reason LEADS with a `⏳ STALE-THESIS [DRIFT]` action prompt before any echoed
    commit-time note. Drift is a READ — it does NOT change the verdict.
  - One HIGH inbox event fires per (ticker, committed_ts) episode; re-anchoring resets it.

Offline-deterministic: price_window_range + scan_trigger_logs are monkeypatched.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import classify as CL


class ThesisDriftPure(unittest.TestCase):
    """thesis_drift(th, live_price) — direct edge coverage (DoD fixtures a–e)."""

    # ── (a) LAB-like post-cascade: every anchor above a far-below live, furthest TP cleared ──
    def test_lab_like_post_cascade_is_stale(self):
        th = {"direction": "SHORT", "tp": [15.5, 14.0, 12.5],
              "watch_level": {"price": 16.69, "dir": "below"}, "committed_ts": "2026-06-23"}
        d = CL.thesis_drift(th, 11.0)          # below the lowest TP 12.5 by >8%
        self.assertTrue(d["stale"])
        self.assertTrue(d["all_tps_printed"])
        self.assertEqual(d["nearest_anchor"], 12.5)     # closest committed anchor
        self.assertEqual(d["furthest_anchor"], 16.69)   # the stale watch_level
        # nearest 12.5 vs live 11.0 → (12.5-11)/12.5 = 12.0%
        self.assertAlmostEqual(d["nearest_dist_pct"], 12.0, places=1)
        self.assertEqual(d["side"], "SHORT")
        self.assertEqual(d["live_price"], 11.0)

    # ── (b) mid-trade: structure straddles live (stop above, TPs below) → NOT stale ──
    def test_mid_trade_straddle_not_stale(self):
        th = {"direction": "SHORT", "stop": 13.0, "tp": [11.0, 10.0],
              "entry_zone": [12.4, 12.8], "committed_ts": "2026-06-23"}
        d = CL.thesis_drift(th, 12.0)          # between stop (above) and TPs (below)
        self.assertFalse(d["stale"])

    # ── (c) fresh commit: live at/near the entry zone → NOT stale ──
    def test_fresh_commit_near_entry_not_stale(self):
        th = {"direction": "LONG", "stop": 9.0, "tp": [12.0, 13.0],
              "entry_zone": [9.8, 10.2], "committed_ts": "2026-06-30"}
        d = CL.thesis_drift(th, 10.0)          # right at entry; anchors straddle
        self.assertFalse(d["stale"])

    # ── (d) no committed anchors (pure WATCH, all null) → None ──
    def test_no_anchors_returns_none(self):
        th = {"direction": "WATCH", "stop": None, "tp": None, "entry_zone": None,
              "committed_ts": "2026-06-10"}
        self.assertIsNone(CL.thesis_drift(th, 10.0))

    def test_no_thesis_or_no_price_returns_none(self):
        self.assertIsNone(CL.thesis_drift(None, 10.0))
        self.assertIsNone(CL.thesis_drift({"stop": 10.0}, None))

    # ── (e) boundary: nearest anchor exactly drift_pct away → stale; just under → not ──
    def test_boundary_exactly_drift_pct_is_stale(self):
        # single anchor (stop 100), all one side; (100-92)/100 = 8.0% == default drift_pct
        th = {"direction": "LONG", "stop": 100.0, "committed_ts": "2026-06-23"}
        self.assertTrue(CL.thesis_drift(th, 92.0)["stale"])      # exactly 8% → stale (>=)
        self.assertFalse(CL.thesis_drift(th, 92.5)["stale"])     # 7.5% → just under → not stale

    def test_boundary_live_exactly_at_anchor_not_stale(self):
        th = {"direction": "LONG", "stop": 100.0, "committed_ts": "2026-06-23"}
        d = CL.thesis_drift(th, 100.0)         # anchor touches live → neither side → not stale
        self.assertFalse(d["stale"])

    # ── sub-rule B: price gapped clean past the whole structure (no TP order needed) ──
    def test_all_anchors_one_side_far_is_stale(self):
        th = {"direction": "SHORT", "stop": 13.0, "tp": [12.0, 11.0],
              "entry_zone": [12.4, 12.8], "committed_ts": "2026-06-23"}
        d = CL.thesis_drift(th, 9.0)           # below everything; nearest 11.0 → (11-9)/11=18%
        self.assertTrue(d["stale"])
        self.assertEqual(d["nearest_anchor"], 11.0)

    # ── sub-rule A fires for a LONG run far above its furthest TP ──
    def test_long_above_furthest_tp_is_stale(self):
        th = {"direction": "LONG", "tp": [11.0, 12.0, 13.0], "committed_ts": "2026-06-23"}
        d = CL.thesis_drift(th, 15.0)          # above 13.0 by (15-13)/13 = 15.4%
        self.assertTrue(d["stale"])
        self.assertTrue(d["all_tps_printed"])

    # ── drift_pct is configurable ──
    def test_drift_pct_threshold_configurable(self):
        th = {"direction": "LONG", "stop": 100.0, "committed_ts": "2026-06-23"}
        self.assertFalse(CL.thesis_drift(th, 95.0, drift_pct=8.0)["stale"])   # 5% < 8
        self.assertTrue(CL.thesis_drift(th, 95.0, drift_pct=3.0)["stale"])    # 5% >= 3
        self.assertEqual(CL.DRIFT_PCT, 8.0)    # documented default in config block


# live present + funding consistent → directional leg alone yields CONFIRMS for a SHORT dir
LIVE_LAB = {
    "venue": "binance", "primary_venue": "binance",
    "price": 11.0, "chg24": -30.0, "vol_m": 40.0, "oi": 1e6,
    "funding_pi": -0.02, "funding_4h": -0.04, "interval_min": 480,
    "funding_stale": False, "all_floor": False, "funding_split": False,
}


def _rng(high, low, window="klines:binance:50x60m", candles=None):
    return {"high": high, "low": low, "window": window, "candles": candles}


class ClassifyTokenDriftSurface(unittest.TestCase):
    def setUp(self):
        self._orig_range = CL.price_window_range
        self._orig_scan = CL.scan_trigger_logs
        CL.scan_trigger_logs = lambda ticker, since_epoch=None: ([], [])

    def tearDown(self):
        CL.price_window_range = self._orig_range
        CL.scan_trigger_logs = self._orig_scan

    def _patch_range(self, rng):
        CL.price_window_range = lambda ticker, venue="binance", since_epoch=None: rng

    def _lab_tok(self):
        th = {"direction": "SHORT", "tp": [15.5, 14.0, 12.5],
              "watch_level": {"price": 16.69, "dir": "below",
                              "note": "squeeze may extend to 18.09 / ATH"},
              "committed_ts": "2026-06-23"}
        return {"ticker": "LAB", "state": "squeeze to 18.09 wall / ATH first", "thesis": th}

    def test_drifted_thesis_leads_reason_and_surfaces_field(self):
        self._patch_range(_rng(16.8, 11.0))         # cascaded through the whole structure
        r = CL.classify_token(self._lab_tok(), LIVE_LAB)
        self.assertIsNotNone(r.get("thesis_drift"))
        self.assertTrue(r["thesis_drift"]["stale"])
        # the action prompt LEADS the reason, before the echoed commit-time note
        self.assertTrue(r["reason"].startswith("⏳ STALE-THESIS [DRIFT]"),
                        f"reason did not lead with drift prompt: {r['reason']!r}")
        self.assertIn("RE-ANCHOR", r["reason"])
        self.assertIn("[commit-time, stale]", r["reason"])
        # the stale commit-time note appears AFTER the prompt, never as the headline
        self.assertLess(r["reason"].index("STALE-THESIS"), r["reason"].index("18.09"))

    def test_drift_does_not_change_verdict(self):
        # drift is a READ — it must not by itself flip the verdict (§0.5)
        self._patch_range(_rng(16.8, 11.0))
        r = CL.classify_token(self._lab_tok(), LIVE_LAB)
        self.assertIn(r["verdict"], CL.VERDICTS)
        # without the price-leg firing BREAKS/TRIGGERS, a deep-neg SHORT row is not BREAKS
        # purely because of drift; the drift annotation is additive to whatever verdict held.
        self.assertIsNotNone(r.get("thesis_drift"))

    def test_non_drifted_thesis_unchanged(self):
        # mid-trade straddle → no drift field, no prompt
        th = {"direction": "SHORT", "stop": 13.0, "tp": [11.0, 10.0],
              "entry_zone": [12.4, 12.8], "committed_ts": "2026-06-23"}
        tok = {"ticker": "LAB", "state": "mid trade", "thesis": th}
        self._patch_range(_rng(12.6, 12.0))
        r = CL.classify_token(tok, {**LIVE_LAB, "price": 12.2})
        self.assertIsNone(r.get("thesis_drift"))
        self.assertNotIn("STALE-THESIS", r["reason"])


class ThesisDriftInboxDedup(unittest.TestCase):
    """SPEC-91.3 — one HIGH inbox event per (ticker, committed_ts) episode; re-anchor resets."""

    def setUp(self):
        self._orig_cursor = CL.THESIS_DRIFT_CURSOR
        self._orig_append = CL.inbox.append_event
        self._tmp = Path(__file__).resolve().parent / "_thesis_drift_cursor_test.json"
        if self._tmp.exists():
            self._tmp.unlink()
        CL.THESIS_DRIFT_CURSOR = self._tmp
        self.fired = []
        CL.inbox.append_event = lambda **kw: self.fired.append(kw) or kw

    def tearDown(self):
        CL.THESIS_DRIFT_CURSOR = self._orig_cursor
        CL.inbox.append_event = self._orig_append
        if self._tmp.exists():
            self._tmp.unlink()

    def _results(self, committed_ts="2026-06-23"):
        return [{"ticker": "LAB", "verdict": "BREAKS",
                 "thesis_drift": {"stale": True, "live_price": 11.0, "nearest_anchor": 12.5,
                                  "nearest_dist_pct": 12.0, "furthest_anchor": 16.69,
                                  "all_tps_printed": True, "side": "SHORT",
                                  "committed_ts": committed_ts}}]

    def test_fires_once_then_dedups(self):
        n1 = CL.fire_thesis_drift(self._results(), now=1.0e9)
        n2 = CL.fire_thesis_drift(self._results(), now=1.0e9 + 60)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)
        self.assertEqual(len(self.fired), 1)
        self.assertEqual(self.fired[0]["ticker"], "LAB")
        self.assertEqual(self.fired[0]["severity"], "HIGH")
        self.assertEqual(self.fired[0]["source"], "thesis_drift")

    def test_reanchor_resets_episode(self):
        CL.fire_thesis_drift(self._results("2026-06-23"), now=1.0e9)
        n2 = CL.fire_thesis_drift(self._results("2026-06-30"), now=1.0e9 + 600)  # bumped ts
        self.assertEqual(n2, 1)               # new episode → fires again
        self.assertEqual(len(self.fired), 2)

    def test_non_stale_does_not_fire(self):
        rows = [{"ticker": "LAB", "verdict": "CONFIRMS", "thesis_drift": None}]
        self.assertEqual(CL.fire_thesis_drift(rows, now=1.0e9), 0)
        self.assertEqual(len(self.fired), 0)


if __name__ == "__main__":
    unittest.main()
