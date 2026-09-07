#!/usr/bin/env python3
"""SPEC 81 — counterfactual outcome scoring for committed (live) theses.

A retire records only THAT a thesis closed, never WHAT price did after — so the
desk's real call history is unscoreable (20/23 live rows are retired_unfilled,
pnl_r null). The counterfactual scorer walks forward klines from commit_ts through
the COMMITTED entry_zone/stop/tp geometry and records what would have happened, so
each desk call becomes a scoreable datum tagged source:"counterfactual" — a third
bucket, never silently merged with live (a hand-traded fill) or replay (a scorer
re-derivation).

All synthetic bars, no network — offline-deterministic.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import counterfactual as CF   # noqa: E402
import ledger as L            # noqa: E402


def _bar(ts, o, h, l, c, v=100):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


# A SHORT committed below current price = a sell-the-breakdown (SPEC-79): commit price
# 5.04 sits ABOVE entry_zone [4.15, 4.32]; it fills on the trade DOWN into the zone.
BREAKDOWN_SHORT = {
    "direction": "SHORT",
    "setup": "stage45_short",
    "entry_zone": [4.15, 4.32],
    "stop": 4.55,
    "tp": [3.85, 3.43],
    "time_stop_h": 48,
    "invalidation": {"price_reclaim": 4.55},
}


class TestScorerFilled(unittest.TestCase):
    def test_breakdown_short_fills_then_runs_to_tp(self):
        """Trades down to 4.20 (fills) then 3.80 → a TP outcome, pnl_r>0, source tagged."""
        ts = 1_700_000_000_000
        step = 3_600_000
        bars = [
            _bar(ts, 5.04, 5.06, 5.02, 5.03),              # commit bar, above zone
            _bar(ts + step, 4.90, 4.95, 4.20, 4.30),        # trades down INTO zone → fill @ 4.32
            _bar(ts + 2 * step, 4.25, 4.30, 3.80, 3.85),    # runs to 3.80 → TP
        ]
        res = CF.score_counterfactual(BREAKDOWN_SHORT, bars)
        self.assertTrue(res["scored"])
        self.assertEqual(res["source"], "counterfactual")
        self.assertIn(res["outcome"], ("tp1", "tp2"))
        self.assertGreater(res["pnl_r"], 0)
        cf = res["counterfactual"]
        self.assertTrue(cf["filled"])
        self.assertEqual(cf["entry_mode"], "breakdown")
        self.assertEqual(cf["fill_source"], "inferred")
        # SPEC-147: no explicit entry_mode on BREAKDOWN_SHORT → the mode is INFERRED, and an
        # inferred SHORT charges the WORSE zone edge (4.15, not the flattering top 4.32) —
        # the paper record must never show a better fill than the desk would plausibly get.
        self.assertAlmostEqual(cf["entry_px"], 4.15, places=6)

    def test_breakdown_short_filled_then_stopped(self):
        """Fills then rallies through the stop 4.55 → stopped, pnl_r -1.0 (the chronic-
        squeezer stop-out the desk keeps logging as a bare retired_unfilled)."""
        ts = 1_700_000_000_000
        step = 3_600_000
        bars = [
            _bar(ts, 5.04, 5.06, 5.02, 5.03),
            _bar(ts + step, 4.90, 4.95, 4.30, 4.40),        # fill @ 4.32
            _bar(ts + 2 * step, 4.45, 4.60, 4.40, 4.58),    # high 4.60 >= stop 4.55 → stopped
        ]
        res = CF.score_counterfactual(BREAKDOWN_SHORT, bars)
        self.assertTrue(res["scored"])
        self.assertEqual(res["outcome"], "stopped")
        self.assertEqual(res["pnl_r"], -1.0)
        self.assertEqual(res["source"], "counterfactual")


class TestExplicitEntryMode(unittest.TestCase):
    def test_explicit_entry_mode_overrides_inferred_worse_edge(self):
        """SPEC-147: an explicit thesis.entry_mode latches as committed and takes the
        mode's natural (not worse-edge) price — the desk said where it filled."""
        ts = 1_700_000_000_000
        step = 3_600_000
        th = dict(BREAKDOWN_SHORT, entry_mode="breakdown")
        bars = [
            _bar(ts, 5.04, 5.06, 5.02, 5.03),
            _bar(ts + step, 4.90, 4.95, 4.20, 4.30),
            _bar(ts + 2 * step, 4.25, 4.30, 3.80, 3.85),
        ]
        res = CF.score_counterfactual(th, bars)
        self.assertTrue(res["scored"])
        cf = res["counterfactual"]
        self.assertEqual(cf["entry_mode"], "breakdown")
        self.assertEqual(cf["fill_source"], "committed")
        self.assertAlmostEqual(cf["entry_px"], 4.32, places=6)   # natural edge, not 4.15


class TestScorerUnfilled(unittest.TestCase):
    def test_never_filled_records_counterfactual_mfe(self):
        """Price never trades below 4.40 (never reaches the zone top 4.32) then the
        horizon expires → retired_unfilled with counterfactual.reason 'never_filled' +
        a recorded MFE (the stood-aside-correctly case)."""
        ts = 1_700_000_000_000
        step = 3_600_000
        bars = [_bar(ts + i * step, 4.80, 4.85, 4.40, 4.60) for i in range(6)]
        res = CF.score_counterfactual(BREAKDOWN_SHORT, bars)
        self.assertTrue(res["scored"])
        self.assertEqual(res["outcome"], "retired_unfilled")
        self.assertIsNone(res["pnl_r"])
        cf = res["counterfactual"]
        self.assertFalse(cf["filled"])
        self.assertEqual(cf["reason"], "never_filled")
        self.assertIn("mfe_r", cf)


class TestScorerGeometryGate(unittest.TestCase):
    def test_discretionary_null_zone_is_skipped_no_geometry(self):
        """A discretionary/WATCH row with entry_zone null was never machine-watchable
        (the SPEC-77 gap) → skipped, no_geometry, NOT scored."""
        th = {"direction": "WATCH", "entry_zone": None, "stop": None, "tp": None,
              "invalidation": {"note": "prose"}}
        bars = [_bar(1, 1, 1.1, 0.9, 1.0)]
        res = CF.score_counterfactual(th, bars)
        self.assertFalse(res["scored"])
        self.assertEqual(res["skip"], "no_geometry")

    def test_short_no_tp_is_skipped(self):
        th = {"direction": "SHORT", "entry_zone": [1.0, 1.1], "stop": 1.2, "tp": []}
        res = CF.score_counterfactual(th, [_bar(1, 1, 1.1, 0.9, 1.0)])
        self.assertFalse(res["scored"])
        self.assertEqual(res["skip"], "no_geometry")


class TestFadeRegression(unittest.TestCase):
    def test_fade_the_rally_short_fills_on_trade_up(self):
        """A fade short: zone [5.20, 5.40] ABOVE commit price 5.04 → fills on the trade
        UP into the zone (entry @ 5.20, the lower edge first touched)."""
        ts = 1_700_000_000_000
        step = 3_600_000
        fade = {"direction": "SHORT", "entry_zone": [5.20, 5.40], "stop": 5.60,
                "tp": [5.00, 4.70], "time_stop_h": 48, "invalidation": {"x": 5.6}}
        bars = [
            _bar(ts, 5.04, 5.10, 5.00, 5.06),
            _bar(ts + step, 5.10, 5.30, 5.05, 5.25),        # rallies UP into zone → fill @ 5.20
            _bar(ts + 2 * step, 5.20, 5.22, 4.65, 4.70),    # falls to 4.65 → TP
        ]
        res = CF.score_counterfactual(fade, bars)
        self.assertTrue(res["scored"])
        self.assertEqual(res["counterfactual"]["entry_mode"], "fade")
        self.assertAlmostEqual(res["counterfactual"]["entry_px"], 5.20, places=6)
        self.assertIn(res["outcome"], ("tp1", "tp2"))


class _LedgerTmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig = L.LEDGER_PATH
        L.LEDGER_PATH = d / "ledger.jsonl"
        self.wl_path = d / "watchlist.json"

    def tearDown(self):
        L.LEDGER_PATH = self._orig
        self.dir.cleanup()


# ── SPEC-155: per-leg geometry (the two-leg rule made rule-compliant commits
# invisible to the scorer — top-level entry_zone/stop/tp stay null on a legs[] thesis) ──

CYS_LEGS_THESIS = {
    "direction": "WATCH", "signature": "post_cascade_oi_build",
    "entry_zone": None, "stop": None, "tp": [],
    "legs": [
        {"kind": "reclaim_long", "entry": "hold_above_0.60",
         "entry_zone": [0.6, 0.61], "stop": 0.578, "tp": [0.66, 0.75]},
        {"kind": "momentum_breakdown", "entry": "hold_below_0.559",
         "stop": 0.586, "tp": [0.5, 0.475]},
    ],
    "committed_ts": "2026-08-24T00:11:04Z",
}

KORU_LEG_THESIS = {
    "direction": "WATCH", "signature": "defended_fade",
    "entry_zone": None, "stop": None, "tp": [],
    "legs": [
        {"kind": "retest_fade", "entry": "rejection_at_21.16-21.23",
         "entry_zone": [21.16, 21.23], "stop": 21.45, "tp": [19.0, 16.4]},
    ],
    "committed_ts": "2026-08-24T00:11:04Z",
}

TREE_NO_LEGS_THESIS = {
    "direction": "WATCH", "entry_zone": None, "stop": None, "tp": [],
    "committed_ts": "2026-08-24T00:11:04Z",
}


class TestLegUnits(unittest.TestCase):
    def test_cys_shaped_two_legs_opposite_directions(self):
        units = CF.leg_units(CYS_LEGS_THESIS)
        self.assertEqual(len(units), 2)
        long_leg = next(u for u in units if u["kind"] == "reclaim_long")
        short_leg = next(u for u in units if u["kind"] == "momentum_breakdown")
        self.assertEqual(long_leg["status"], "ok")
        self.assertEqual(long_leg["direction"], "LONG")     # stop 0.578 < entries 0.60-0.61
        self.assertEqual(short_leg["status"], "ok")
        self.assertEqual(short_leg["direction"], "SHORT")   # stop 0.586 > entry 0.559

    def test_koru_shaped_one_fade_leg_short(self):
        units = CF.leg_units(KORU_LEG_THESIS)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["status"], "ok")
        self.assertEqual(units[0]["direction"], "SHORT")
        self.assertEqual(units[0]["family"], "zone_touch")

    def test_bad_leg_geometry_stop_equals_entry_is_skipped(self):
        th = {"direction": "WATCH", "legs": [
            {"kind": "momentum", "entry": "hold_below_0.559", "stop": 0.559, "tp": [0.5]}]}
        units = CF.leg_units(th)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["status"], "bad_leg_geometry")

    def test_tree_shaped_no_legs_yields_no_units(self):
        self.assertEqual(CF.leg_units(TREE_NO_LEGS_THESIS), [])


class TestCloseBeyondFill(unittest.TestCase):
    """momentum/*_hold legs fill on a CLOSE beyond the named level, never a mere wick
    through it (SPEC-79's close-beyond hold requirement, applied per-leg)."""

    def test_wick_through_without_close_does_not_fill(self):
        bars = [
            {"open": 0.60, "high": 0.605, "low": 0.598, "close": 0.602},
            {"open": 0.60, "high": 0.602, "low": 0.550, "close": 0.590},  # wicks below 0.559, closes above
        ]
        fill = CF._close_beyond_fill("SHORT", 0.559, bars)
        self.assertFalse(fill["filled"])
        self.assertIsNone(fill["entry_idx"])

    def test_close_beyond_level_fills(self):
        bars = [
            {"open": 0.60, "high": 0.605, "low": 0.598, "close": 0.602},
            {"open": 0.60, "high": 0.602, "low": 0.550, "close": 0.555},  # closes below 0.559
        ]
        fill = CF._close_beyond_fill("SHORT", 0.559, bars)
        self.assertTrue(fill["filled"])
        self.assertEqual(fill["entry_idx"], 1)
        self.assertEqual(fill["mode"], "breakdown_hold")
        self.assertEqual(fill["entry_px"], 0.559)


class TestBackfillLegs(_LedgerTmp):
    def _seed(self, ticker, thesis):
        L.record({"ticker": ticker, "direction": "WATCH", "signature": "discretionary",
                  "outcome": "retired_unfilled", "pnl_r": None,
                  "commit_ts": thesis["committed_ts"], "close_ts": None, "notes": "watch"})
        wl = {"tokens": [{"ticker": ticker, "thesis": thesis}], "retired": []}
        self.wl_path.write_text(json.dumps(wl))

    def _cys_bars(self, ticker, commit_ts):
        if ticker != "CYS":
            return None
        ts = 1_700_000_000_000
        step = 3_600_000
        return [
            _bar(ts, 0.585, 0.588, 0.582, 0.586),            # commit bar, between the two legs
            _bar(ts + step, 0.590, 0.615, 0.588, 0.610),      # rallies into the reclaim zone [0.6,0.61]
            _bar(ts + 2 * step, 0.612, 0.665, 0.605, 0.660),  # runs to TP1 0.66
        ]

    def test_cys_two_legs_score_independently(self):
        self._seed("CYS", CYS_LEGS_THESIS)
        out = CF.backfill(wl_path=self.wl_path, bars_provider=self._cys_bars)
        self.assertEqual(out["scored"], 2)
        rows = [json.loads(l) for l in L.LEDGER_PATH.read_text().splitlines() if l.strip()]
        cf_rows = [r for r in rows if r.get("source") == "counterfactual"]
        self.assertEqual(len(cf_rows), 2)
        long_row = next(r for r in cf_rows if r["direction"] == "LONG")
        short_row = next(r for r in cf_rows if r["direction"] == "SHORT")
        self.assertIn(long_row["outcome"], ("tp1", "tp2"))
        self.assertGreater(long_row["pnl_r"], 0)
        self.assertEqual(long_row["leg_kind"], "reclaim_long")
        self.assertEqual(short_row["outcome"], "retired_unfilled")   # never closes below 0.559
        self.assertIsNone(short_row["pnl_r"])
        self.assertEqual(short_row["leg_kind"], "momentum_breakdown")
        # both legs of one row share the row's resolved signature bucket (SPEC-155 req 4)
        self.assertEqual(short_row["signature"], long_row["signature"])
        self.assertEqual(long_row["signature"], "discretionary")

    def test_cys_idempotent_second_pass_counts_legs_already_scored(self):
        self._seed("CYS", CYS_LEGS_THESIS)
        CF.backfill(wl_path=self.wl_path, bars_provider=self._cys_bars)
        out2 = CF.backfill(wl_path=self.wl_path, bars_provider=self._cys_bars)
        self.assertEqual(out2["scored"], 0)
        self.assertEqual(out2["skipped"].get("already_scored", 0), 2)
        rows = [json.loads(l) for l in L.LEDGER_PATH.read_text().splitlines() if l.strip()]
        cf_rows = [r for r in rows if r.get("source") == "counterfactual"]
        self.assertEqual(len(cf_rows), 2)

    def _koru_bars(self, ticker, commit_ts):
        if ticker != "KORU":
            return None
        ts = 1_700_000_000_000
        step = 3_600_000
        return [
            _bar(ts, 21.10, 21.12, 21.05, 21.08),             # commit bar, below the fade wall
            _bar(ts + step, 21.12, 21.30, 21.15, 21.20),       # rallies into [21.16,21.23] → fade fill
            _bar(ts + 2 * step, 21.20, 21.20, 18.90, 19.00),   # falls to TP1 19.0
        ]

    def test_koru_fade_leg_fills_on_touch(self):
        self._seed("KORU", KORU_LEG_THESIS)
        out = CF.backfill(wl_path=self.wl_path, bars_provider=self._koru_bars)
        self.assertEqual(out["scored"], 1)
        rows = [json.loads(l) for l in L.LEDGER_PATH.read_text().splitlines() if l.strip()]
        cf_row = next(r for r in rows if r.get("source") == "counterfactual")
        self.assertEqual(cf_row["direction"], "SHORT")
        self.assertEqual(cf_row["counterfactual"]["entry_mode"], "fade")
        self.assertAlmostEqual(cf_row["counterfactual"]["entry_px"], 21.16, places=6)
        self.assertIn(cf_row["outcome"], ("tp1", "tp2"))
        self.assertEqual(cf_row["leg_kind"], "retest_fade")

    def test_tree_shaped_no_legs_no_top_level_skips_no_geometry(self):
        self._seed("TREE", TREE_NO_LEGS_THESIS)
        out = CF.backfill(wl_path=self.wl_path, bars_provider=lambda t, c: None)
        self.assertEqual(out["scored"], 0)
        self.assertEqual(out["skipped"].get("no_geometry", 0), 1)


class TestBackfill(_LedgerTmp):
    def _seed_live_rows(self):
        # two live null-pnl rows (a scoreable geometry thesis + a discretionary one)
        L.record({"ticker": "BEAT", "direction": "SHORT", "signature": "stage5_short",
                  "outcome": "retired_unfilled", "pnl_r": None,
                  "commit_ts": "2026-06-10T23:24:19Z", "close_ts": None,
                  "notes": "retired"})
        L.record({"ticker": "AIOT", "direction": "WATCH", "signature": "discretionary",
                  "outcome": "retired_unfilled", "pnl_r": None,
                  "commit_ts": "2026-06-02T20:15:02+00:00", "close_ts": None,
                  "notes": "watch"})

    def _seed_watchlist(self):
        wl = {"tokens": [{"ticker": "AIOT", "thesis": {
                   "direction": "WATCH", "entry_zone": None, "stop": None, "tp": None,
                   "committed_ts": "2026-06-02T20:15:02+00:00"}}],
              "retired": [{"ticker": "BEAT", "thesis": {
                   "direction": "SHORT", "setup": "stage45_short", "entry_zone": [6.0, 6.6],
                   "stop": 8.21, "tp": [5.85, 4.9], "time_stop_h": 48,
                   "committed_ts": "2026-06-10T23:24:19Z"}}]}
        self.wl_path.write_text(json.dumps(wl))

    def _provider(self, ticker, commit_ts):
        if ticker == "BEAT":
            ts = 1_700_000_000_000
            step = 3_600_000
            # commit ~6.9, trades down into [6.0,6.6] then to 4.8 → a TP
            return [
                _bar(ts, 6.90, 6.95, 6.85, 6.88),
                _bar(ts + step, 6.70, 6.75, 5.90, 6.00),
                _bar(ts + 2 * step, 5.95, 6.00, 4.80, 4.85),
            ]
        return None

    def test_backfill_scores_geometry_skips_discretionary_idempotent(self):
        self._seed_live_rows()
        self._seed_watchlist()
        out = CF.backfill(wl_path=self.wl_path, bars_provider=self._provider)
        self.assertEqual(out["scored"], 1)                       # BEAT scored
        self.assertEqual(out["skipped"]["no_geometry"], 1)       # AIOT skipped
        rows = [json.loads(l) for l in L.LEDGER_PATH.read_text().splitlines() if l.strip()]
        cf_rows = [r for r in rows if r.get("source") == "counterfactual"]
        self.assertEqual(len(cf_rows), 1)
        self.assertEqual(cf_rows[0]["ticker"], "BEAT")
        self.assertIn("counterfactual", cf_rows[0])
        # idempotent: a second run does not duplicate (match on ticker+commit_ts)
        out2 = CF.backfill(wl_path=self.wl_path, bars_provider=self._provider)
        self.assertEqual(out2["scored"], 0)
        self.assertEqual(out2["skipped"].get("already_scored", 0), 1)
        cf_rows2 = [json.loads(l) for l in L.LEDGER_PATH.read_text().splitlines() if l.strip()
                    if json.loads(l).get("source") == "counterfactual"]
        self.assertEqual(len(cf_rows2), 1)

    def test_ledger_stats_splits_counterfactual_bucket(self):
        self._seed_live_rows()
        self._seed_watchlist()
        CF.backfill(wl_path=self.wl_path, bars_provider=self._provider)
        row = L.stats(signature="stage5_short")
        self.assertIn("counterfactual", row["by_source"])
        self.assertIn("live", row["by_source"])
        # the counterfactual bucket is its own population, split from live
        self.assertEqual(row["by_source"]["counterfactual"]["n"], 1)


# ── SPEC-162 req 3: counterfactual reads the ledger row's inline geometry SNAPSHOT
# first — the watchlist join is now the legacy fallback, only used when a row carries
# no snapshot. This is what makes scoring immune to board rewrites/retirements. ──

class TestGeometryFromRow(unittest.TestCase):
    def test_none_when_no_snapshot(self):
        self.assertIsNone(CF._geometry_from_row({"ticker": "X", "commit_ts": "t"}))

    def test_reconstructs_thesis_shape_from_snapshot(self):
        row = {"ticker": "GALA", "direction": "SHORT",
              "geometry": {"direction": "SHORT", "entry_zone": [0.00195, 0.00199],
                          "stop": 0.00211, "tp": [0.00165, 0.00142],
                          "entry_mode": None, "legs": None}}
        th = CF._geometry_from_row(row)
        self.assertEqual(th["entry_zone"], [0.00195, 0.00199])
        self.assertEqual(th["stop"], 0.00211)
        self.assertEqual(th["tp"], [0.00165, 0.00142])


class TestBackfillPrefersSnapshotOverWatchlist(_LedgerTmp):
    def _snapshot_bars(self, ticker, commit_ts):
        if ticker != "GALA":
            return None
        ts = 1_700_000_000_000
        step = 3_600_000
        return [
            _bar(ts, 0.00200, 0.00201, 0.00196, 0.00198),          # commit bar
            _bar(ts + step, 0.00198, 0.00199, 0.00196, 0.00197),    # fills the fade zone
            _bar(ts + 2 * step, 0.00196, 0.00196, 0.00163, 0.00164),  # runs to TP1 0.00165
        ]

    def test_scores_from_snapshot_even_when_watchlist_row_is_gone(self):
        """The board rewrite/retirement case req 3 exists for: the watchlist token for
        GALA is deleted entirely — the exact-ts join would find nothing — but the
        ledger row's own geometry snapshot is enough to score it."""
        L.LEDGER_PATH.write_text(json.dumps({
            "ticker": "GALA", "direction": "SHORT", "signature": "discretionary",
            "outcome": None, "pnl_r": None, "source": "live",
            "commit_ts": "2026-08-25T00:00:00Z", "close_ts": None,
            "geometry": {"direction": "SHORT", "entry_zone": [0.00195, 0.00199],
                        "stop": 0.00211, "tp": [0.00165, 0.00142],
                        "entry_mode": None, "legs": None},
            "unscoreable": False, "notes": "",
        }) + "\n")
        self.wl_path.write_text(json.dumps({"tokens": [], "retired": []}))   # GONE from the board
        out = CF.backfill(wl_path=self.wl_path, bars_provider=self._snapshot_bars)
        self.assertEqual(out["scored"], 1)
        rows = [json.loads(l) for l in L.LEDGER_PATH.read_text().splitlines() if l.strip()]
        cf_row = next(r for r in rows if r.get("source") == "counterfactual")
        self.assertEqual(cf_row["ticker"], "GALA")
        self.assertIn(cf_row["outcome"], ("tp1", "tp2"))

    def test_legacy_row_with_no_snapshot_still_scores_via_watchlist_join(self):
        """A row recorded before SPEC-162 (no `geometry` key at all) must keep working
        exactly as before — the fallback path, never dropped."""
        self._seed_live_rows_no_geometry()
        wl = {"tokens": [], "retired": [{"ticker": "BEAT", "thesis": {
                   "direction": "SHORT", "setup": "stage45_short", "entry_zone": [6.0, 6.6],
                   "stop": 8.21, "tp": [5.85, 4.9], "time_stop_h": 48,
                   "committed_ts": "2026-06-10T23:24:19Z"}}]}
        self.wl_path.write_text(json.dumps(wl))

        def provider(ticker, commit_ts):
            ts = 1_700_000_000_000
            step = 3_600_000
            return [
                _bar(ts, 6.90, 6.95, 6.85, 6.88),
                _bar(ts + step, 6.70, 6.75, 5.90, 6.00),
                _bar(ts + 2 * step, 5.95, 6.00, 4.80, 4.85),
            ]
        out = CF.backfill(wl_path=self.wl_path, bars_provider=provider)
        self.assertEqual(out["scored"], 1)

    def _seed_live_rows_no_geometry(self):
        L.record({"ticker": "BEAT", "direction": "SHORT", "signature": "stage5_short",
                  "outcome": "retired_unfilled", "pnl_r": None,
                  "commit_ts": "2026-06-10T23:24:19Z", "close_ts": None, "notes": "retired"})


if __name__ == "__main__":
    unittest.main()
