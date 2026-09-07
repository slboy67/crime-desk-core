#!/usr/bin/env python3
"""scan — cross-sectional perp-universe scanner.

Run:  python3 tests/test_scan.py

Hits Bybit public tickers (one fast call set). Asserts clean JSON, the
{universe,longs,shorts} shape, candidate contract + types, the funding-threshold
partition, sort order, the --side filter, and that the bare view stays non-JSON.
SPEC-123: TestScan hits live Bybit — gated behind CRIMEDESK_LIVE_TESTS=1, skipped by
default. TestScoutBoard stays offline (--scout-signals injects signals, no network).
"""
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN = ROOT / "capabilities" / "scan.py"
sys.path.insert(0, str(ROOT / "capabilities"))
import scan  # noqa: E402
import price_structure as PS  # noqa: E402
sys.path.insert(0, str(ROOT / "tests"))
from live_gate import LIVE, SKIP_REASON  # noqa: E402
CAND_KEYS = {"ticker", "funding_4h", "funding_raw", "interval_h", "turnover_m",
             "chg24", "oi", "price", "deep", "side"}


def run(*flags):
    return subprocess.run([sys.executable, str(SCAN), *flags],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=90)


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = run("--json")
        cls.scan = json.loads(cls.proc.stdout)

    def test_clean_json(self):
        self.assertNotIn("\x1b", self.proc.stdout)
        self.assertIsInstance(self.scan, dict)
        self.assertEqual({"universe", "min_vol_m", "thresh", "side", "longs", "shorts"},
                         set(self.scan))

    def test_universe_nonempty(self):
        self.assertGreater(self.scan["universe"], 50)  # the liquid perp universe is large
        self.assertIsInstance(self.scan["longs"], list)
        self.assertIsInstance(self.scan["shorts"], list)

    def test_candidate_contract_and_partition(self):
        th = self.scan["thresh"]
        for c in self.scan["longs"]:
            self.assertTrue(CAND_KEYS.issubset(c), c)
            self.assertIsInstance(c["funding_4h"], float)
            self.assertLessEqual(c["funding_4h"], -th)
            self.assertEqual(c["side"], "long")
        for c in self.scan["shorts"]:
            self.assertTrue(CAND_KEYS.issubset(c), c)
            self.assertGreaterEqual(c["funding_4h"], th)
            self.assertEqual(c["side"], "short")

    def test_sort_order(self):
        longs = [c["funding_4h"] for c in self.scan["longs"]]
        shorts = [c["funding_4h"] for c in self.scan["shorts"]]
        self.assertEqual(longs, sorted(longs))            # most-negative first
        self.assertEqual(shorts, sorted(shorts, reverse=True))  # most-positive first

    def test_side_filter(self):
        scan = json.loads(run("--side", "long", "--json").stdout)
        self.assertEqual(scan["side"], "long")
        self.assertEqual(scan["shorts"], [])
        self.assertTrue(len(scan["longs"]) >= 0)

    def test_top_limit(self):
        scan = json.loads(run("--top", "3", "--json").stdout)
        self.assertLessEqual(len(scan["longs"]), 3)
        self.assertLessEqual(len(scan["shorts"]), 3)

    def test_human_view_not_json(self):
        proc = run("--no-color", "--top", "3")
        self.assertIn("ALL-PERP SCAN", proc.stdout)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(proc.stdout)


class TestScoutBoard(unittest.TestCase):
    """SPEC-82 — scan runs the scorers across a watchlist and surfaces SCOUT candidates
    (offline; injected signals). FORMING-near-armed → scout section; ARMED → armed; rest quiet."""

    # one FORMING-3/4 name (blowoff 4/5 → SCOUT), one ARMED, one quiet (1 leg)
    SIGNALS = {
        "SCOUTER": {  # blowoff 4/5, missing clean_break → SCOUT
            "parabolic_pct": 73.0, "window_high_wick_pct": 3.1, "lower_high": True,
            "intraday_break": {"broke": True, "vol_mult": 1.8, "rebought": True},
            "oi_off_highs": True, "oi_sides_tag": "REAL_DIRECTIONAL",
        },
        "ARMER": {  # catb_top 4/4 → ARMED
            "ath_wick": True, "lower_high": True, "ls_top_drop_pct": 19.0,
            "oi_peaked_rolled": True, "volume_declining": True, "funding_cooling": True,
        },
        "QUIET": {"multi_sigma_neg": True},  # trap_long 1/4 → FORMING (below threshold)
    }

    def test_score_watchlist_partitions_scout_armed_quiet(self):
        board = scan.score_watchlist(self.SIGNALS)
        self.assertEqual(set(board), {"scout", "armed", "quiet"})
        scout_tickers = {r["ticker"] for r in board["scout"]}
        armed_tickers = {r["ticker"] for r in board["armed"]}
        self.assertIn("SCOUTER", scout_tickers)
        self.assertIn("ARMER", armed_tickers)
        self.assertIn("QUIET", board["quiet"])
        # the scout row names its setup + add_triggers (what sizes it up)
        scout_row = next(r for r in board["scout"] if r["ticker"] == "SCOUTER")
        self.assertEqual(scout_row["setup"], "blowoff")
        self.assertEqual(scout_row["tier"], "scout")
        self.assertIn("clean_break", scout_row["add_triggers"])

    def test_cli_scout_board_offline(self):
        out = run("--scout-signals", json.dumps(self.SIGNALS), "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        board = json.loads(out.stdout)
        self.assertIn("scout", board)
        self.assertTrue(any(r["ticker"] == "SCOUTER" for r in board["scout"]))


class TestFadedBounceGates(unittest.TestCase):
    """SPEC-120 — pure gate evaluation + tiering (offline, fixture-driven)."""

    BASE = {
        "off_ath_pct": -55.0, "bounce_pct": 22.0, "low_age_days": 5,
        # SPEC-173: vol24h_m must clear the $10M §7 liquidity floor (9.0 pre-SPEC-173 no
        # longer passes) — 12.0 keeps vol_pct_of_peak a clean 24.0% of the 50.0 peak.
        "vol24h_m": 12.0, "vol_peak24h_m": 50.0,
        "vol_recent_avg_m": 8.0, "vol_prior_avg_m": 15.0,   # declining → faded
        "funding_4h": -0.04, "squeeze_legs_60d": 1,
        "distribution": "FRESH", "on_watchlist": True,
        # SPEC-160 #2: structure already rolled over (not a live uptrend), lower-highs present
        "structure_read": "downtrend", "lower_highs": 2,
        # SPEC-173: Aster execution gate — clears the default $10K min_aster_exit_usd floor.
        "exit_absorbable_usd": 15000.0,
    }

    def _cand(self, **overrides):
        return {**self.BASE, **overrides}

    def test_all_gates_pass_fresh_distribution_is_tier1_ranked_first(self):
        board = scan.build_faded_bounce({"X": self._cand()})
        self.assertEqual(board["excluded"], [])
        self.assertEqual(len(board["candidates"]), 1)
        row = board["candidates"][0]
        self.assertEqual(row["ticker"], "X")
        self.assertEqual(row["tier"], "tier-1")
        self.assertEqual(row["onchain"], "FRESH")
        self.assertEqual(row["vol_pct_of_peak"], 24.0)
        self.assertEqual(row["squeeze_legs_60d"], 1)

    def test_deep_neg_funding_hard_excludes(self):
        board = scan.build_faded_bounce({"X": self._cand(funding_4h=-0.45)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(len(board["excluded"]), 1)
        self.assertEqual(board["excluded"][0]["reason"], "funding_ok")

    def test_frozen_distribution_demotes_to_tier2_with_squeeze_risk_caveat(self):
        board = scan.build_faded_bounce({"X": self._cand(distribution="FROZEN")})
        row = board["candidates"][0]
        self.assertEqual(row["tier"], "tier-2")
        self.assertTrue(any("squeeze-risk" in c for c in row["caveats"]))

    def test_volume_not_yet_faded_excludes(self):
        # recent volume elevated vs the prior window — attention has NOT faded
        board = scan.build_faded_bounce({"X": self._cand(vol24h_m=40.0, vol_recent_avg_m=40.0)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "faded")

    def test_chronic_squeezer_excludes(self):
        # SPEC-138: squeeze_legs_60d subsumes the old squeeze_leg_pct_48h check
        board = scan.build_faded_bounce({"X": self._cand(squeeze_legs_60d=7)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "not_squeezing")
        self.assertEqual(board["excluded"][0]["squeeze_legs_60d"], 7)   # surfaced even when excluded

    def test_onchain_unavailable_is_tier2_never_implies_clean(self):
        board = scan.build_faded_bounce({"X": self._cand(distribution=None, onchain_available=False)})
        row = board["candidates"][0]
        self.assertEqual(row["tier"], "tier-2")
        self.assertEqual(row["onchain"], "unavailable")

    def test_zero_matches_is_a_normal_explicit_result(self):
        board = scan.build_faded_bounce({})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"], [])

    def test_non_watchlist_name_is_tier2_even_if_fresh(self):
        board = scan.build_faded_bounce({"X": self._cand(on_watchlist=False)})
        self.assertEqual(board["candidates"][0]["tier"], "tier-2")

    def test_ranking_tier1_before_tier2_then_by_rank_score(self):
        cands = {
            "T2HIGH": self._cand(on_watchlist=False, bounce_pct=90.0, vol24h_m=1.0, vol_peak24h_m=50.0),
            "T1": self._cand(),
        }
        board = scan.build_faded_bounce(cands)
        tickers = [r["ticker"] for r in board["candidates"]]
        self.assertEqual(tickers[0], "T1")   # tier-1 always leads regardless of rank_score

    def test_cap_at_top_reports_dropped(self):
        cands = {f"T{i}": self._cand(bounce_pct=15.0 + i) for i in range(8)}
        board = scan.build_faded_bounce(cands)
        cfg = {**scan.FADED_BOUNCE_DEFAULTS, "top": 3}
        board = scan.build_faded_bounce(cands, cfg=cfg)
        self.assertEqual(len(board["candidates"]), 3)
        self.assertEqual(len(board["dropped_below_top"]), 5)


class TestFadedBounceVolumeSlope(unittest.TestCase):
    """SPEC-138 — 'faded' gates on recent-volume SLOPE, not %-of-90d-peak; every row
    carries squeeze_legs_60d (int), which subsumes the old squeeze_leg_pct_48h window."""

    BASE = {
        "off_ath_pct": -55.0, "bounce_pct": 22.0, "low_age_days": 5,
        "funding_4h": -0.04, "squeeze_legs_60d": 1,
        "distribution": "FRESH", "on_watchlist": True,
        "structure_read": "downtrend", "lower_highs": 2,
        "exit_absorbable_usd": 15000.0,   # SPEC-173: clears the Aster execution gate
    }

    def _cand(self, **overrides):
        return {**self.BASE, **overrides}

    def test_bico_repro_rising_volume_vs_large_90d_peak_is_rejected(self):
        # BICO: 3 consecutive squeeze days, volume 16 -> 96 -> 132M, ACTIVE squeeze — but a
        # much larger 90d peak makes 132M look tiny by the old %-of-peak measure (2.6% of a
        # 5000M peak would have PASSED the old 30% threshold). The slope gate must reject it.
        board = scan.build_faded_bounce({"BICO": self._cand(
            vol24h_m=132.0, vol_peak24h_m=5000.0,
            vol_recent_avg_m=(96.0 + 132.0) / 2, vol_prior_avg_m=16.0)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "faded")
        self.assertFalse(board["excluded"][0]["gates"]["faded"])

    def test_genuinely_decaying_volume_off_a_dump_still_passes(self):
        # SPEC-173: 8.0 (below the new $10M §7 floor) bumped to 12.0 — the "still passes"
        # intent is about the FADED gate, not the liquidity gate.
        board = scan.build_faded_bounce({"X": self._cand(
            vol24h_m=12.0, vol_peak24h_m=200.0,
            vol_recent_avg_m=8.0, vol_prior_avg_m=40.0)})
        self.assertEqual(board["excluded"], [])
        self.assertEqual(len(board["candidates"]), 1)
        self.assertTrue(board["candidates"][0]["gates"]["faded"])

    def test_every_row_carries_integer_squeeze_legs_60d(self):
        cands = {
            "PASS": self._cand(vol24h_m=12.0, vol_recent_avg_m=8.0, vol_prior_avg_m=40.0,
                               squeeze_legs_60d=6),   # exactly at the chronic threshold
            "EXCL": self._cand(vol24h_m=12.0, vol_recent_avg_m=8.0, vol_prior_avg_m=40.0,
                               off_ath_pct=None),      # excluded on a different gate
        }
        board = scan.build_faded_bounce(cands)
        for row in board["candidates"] + board["excluded"]:
            self.assertIn("squeeze_legs_60d", row)
            self.assertIsInstance(row["squeeze_legs_60d"], int)
        passed = next(r for r in board["candidates"] if r["ticker"] == "PASS")
        self.assertEqual(passed["squeeze_legs_60d"], 6)


class TestFadedBounceRolledOverGate(unittest.TestCase):
    """SPEC-160 #2 — WLD and 1000BONK passed all five mechanical gates while being live
    uptrends (HH/HL sequences, recent squeeze legs). price_structure already computes
    lower_highs/higher_lows (structure.read + structure.lower_highs) — reject when the
    structure read is 'uptrend' or no lower-high exists post-peak."""

    def _cand(self, **overrides):
        return {**TestFadedBounceGates.BASE, **overrides}

    def test_wld_shaped_uptrend_4hh_5hl_is_rejected_not_rolled_over(self):
        board = scan.build_faded_bounce({"WLD": self._cand(
            structure_read="uptrend", lower_highs=2)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(len(board["excluded"]), 1)
        self.assertEqual(board["excluded"][0]["reason"], "not_rolled_over")
        self.assertFalse(board["excluded"][0]["gates"]["rolled_over"])

    def test_gala_shaped_two_lower_highs_post_peak_passes(self):
        board = scan.build_faded_bounce({"GALA": self._cand(
            structure_read="mixed", lower_highs=2)})
        self.assertEqual(board["excluded"], [])
        self.assertEqual(len(board["candidates"]), 1)
        self.assertTrue(board["candidates"][0]["gates"]["rolled_over"])

    def test_no_lower_high_at_all_is_rejected_even_if_not_uptrend(self):
        board = scan.build_faded_bounce({"X": self._cand(
            structure_read="mixed", lower_highs=0)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "not_rolled_over")

    def test_missing_structure_data_fails_closed(self):
        # SPEC-166: missing structure data gets its OWN reason ("structure_unavailable"),
        # distinct from "not_rolled_over" (data present, name just hasn't rolled over) —
        # the sweep must say WHY a name died, not lump "unreadable" with "still going up".
        board = scan.build_faded_bounce({"X": self._cand(
            structure_read=None, lower_highs=None)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "structure_unavailable")
        self.assertFalse(board["excluded"][0]["gates"]["rolled_over"])

    def test_magma_shaped_mixed_read_but_still_making_higher_highs_is_rejected(self):
        # SPEC-166 live bug: MAGMA read structure_read="mixed" with lower_highs=3 — the
        # OLD gate (lower_highs >= 1) passed it. But higher_highs=3 too (tied) AND the most
        # recent swing was ANOTHER higher high — a live squeezer, not a rolled-over bounce.
        board = scan.build_faded_bounce({"MAGMA": self._cand(
            structure_read="mixed", lower_highs=3, higher_highs=3,
            last_swing_higher_high=True)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "not_rolled_over")
        self.assertFalse(board["excluded"][0]["gates"]["rolled_over"])

    def test_mixed_read_with_more_lower_highs_than_higher_highs_still_passes(self):
        # the new higher_highs>=lower_highs check must not false-reject a genuine
        # roll-over just because SOME higher highs exist in the 7-candle window.
        board = scan.build_faded_bounce({"X": self._cand(
            structure_read="mixed", lower_highs=3, higher_highs=1,
            last_swing_higher_high=False)})
        self.assertEqual(board["excluded"], [])
        self.assertTrue(board["candidates"][0]["gates"]["rolled_over"])

    def test_tied_hh_lh_but_last_swing_was_a_lower_high_still_passes(self):
        # tied counts (hh==lh) alone aren't disqualifying — only tied AND the most recent
        # swing being a fresh higher high (the live-squeeze tell) is.
        board = scan.build_faded_bounce({"X": self._cand(
            structure_read="mixed", lower_highs=3, higher_highs=3,
            last_swing_higher_high=False)})
        self.assertEqual(board["excluded"], [])
        self.assertTrue(board["candidates"][0]["gates"]["rolled_over"])


class TestFadedBounceNoRecentLegGate(unittest.TestCase):
    """SPEC-166 #3 — reject a bounce with a squeeze leg still inside the last
    `recent_leg_days` (default 3), or whose bounce peak is younger than
    `min_days_since_peak` (default 4) — the live-bounce rule (MAGMA/ONG/USELESS class)
    applied mechanically, since 'rolled_over' alone doesn't catch a leg from yesterday."""

    def _cand(self, **overrides):
        return {**TestFadedBounceGates.BASE, **overrides}

    def test_leg_one_day_ago_is_rejected(self):
        board = scan.build_faded_bounce({"MAGMA": self._cand(
            recent_squeeze_leg_days_ago=[8, 6, 1], bounce_high_age_days=8)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "recent_leg")
        self.assertFalse(board["excluded"][0]["gates"]["no_recent_leg"])

    def test_leg_exactly_at_the_boundary_is_recent(self):
        board = scan.build_faded_bounce({"X": self._cand(
            recent_squeeze_leg_days_ago=[3], bounce_high_age_days=8)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "recent_leg")

    def test_leg_just_outside_the_window_passes(self):
        board = scan.build_faded_bounce({"X": self._cand(
            recent_squeeze_leg_days_ago=[4], bounce_high_age_days=8)})
        self.assertEqual(board["excluded"], [])
        self.assertTrue(board["candidates"][0]["gates"]["no_recent_leg"])

    def test_bounce_peak_younger_than_min_days_since_peak_is_rejected(self):
        board = scan.build_faded_bounce({"X": self._cand(
            recent_squeeze_leg_days_ago=[], bounce_high_age_days=2)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "recent_leg")

    def test_bounce_peak_at_the_min_days_boundary_passes(self):
        board = scan.build_faded_bounce({"X": self._cand(
            recent_squeeze_leg_days_ago=[], bounce_high_age_days=4)})
        self.assertEqual(board["excluded"], [])

    def test_no_recent_leg_data_defaults_to_pass(self):
        # fields absent entirely (older/injected candidates) must not spuriously exclude —
        # only a POSITIVELY known recent leg/peak rejects.
        board = scan.build_faded_bounce({"X": self._cand()})
        self.assertEqual(board["excluded"], [])
        self.assertTrue(board["candidates"][0]["gates"]["no_recent_leg"])

    def test_custom_recent_leg_days_cfg_honored(self):
        cfg = {**scan.FADED_BOUNCE_DEFAULTS, "recent_leg_days": 10}
        board = scan.build_faded_bounce(
            {"X": self._cand(recent_squeeze_leg_days_ago=[8], bounce_high_age_days=20)}, cfg=cfg)
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "recent_leg")


class TestFadedBounceRejectedSummary(unittest.TestCase):
    """SPEC-166 #4 — the sweep reports WHY a name died: a {ticker: reason} map plus
    per-gate fail counts, not just the raw excluded-row list."""

    def _cand(self, **overrides):
        return {**TestFadedBounceGates.BASE, **overrides}

    def test_rejected_map_and_gate_fail_counts(self):
        cands = {
            "UPTREND": self._cand(structure_read="uptrend", lower_highs=2),
            "SQUEEZY": self._cand(recent_squeeze_leg_days_ago=[1], bounce_high_age_days=8),
            "OK": self._cand(),
        }
        board = scan.build_faded_bounce(cands)
        self.assertEqual(board["rejected"], {"UPTREND": "not_rolled_over", "SQUEEZY": "recent_leg"})
        self.assertEqual(board["gate_fail_counts"]["rolled_over"], 1)
        self.assertEqual(board["gate_fail_counts"]["no_recent_leg"], 1)
        self.assertEqual(board["gate_fail_counts"].get("faded", 0), 0)

    def test_zero_rejections_is_an_empty_map(self):
        board = scan.build_faded_bounce({"OK": self._cand()})
        self.assertEqual(board["rejected"], {})


class TestFadedBounceStructureFieldsHelper(unittest.TestCase):
    """SPEC-166 #1 — structure extraction is its own pure/testable step so a malformed or
    missing price_structure 'structure' block fails LOUD (stderr) and closed (None fields),
    never a silent None the OLD gate happened to treat as 'not rolled over yet'."""

    def test_well_formed_structure_extracts_all_fields(self):
        s90 = {"structure": {"read": "mixed", "lower_highs": 2, "higher_highs": 1,
                             "last_swing_higher_high": False}}
        out = scan._faded_bounce_structure_fields("X", s90)
        self.assertEqual(out, {"structure_read": "mixed", "lower_highs": 2,
                               "higher_highs": 1, "last_swing_higher_high": False})

    def test_missing_structure_key_fails_closed_and_logs(self):
        buf = []
        orig = sys.stderr
        import io
        sys.stderr = io.StringIO()
        try:
            out = scan._faded_bounce_structure_fields("MAGMA", {})
            logged = sys.stderr.getvalue()
        finally:
            sys.stderr = orig
        self.assertEqual(out, {"structure_read": None, "lower_highs": None,
                               "higher_highs": None, "last_swing_higher_high": None})
        self.assertIn("MAGMA", logged)

    def test_malformed_structure_value_fails_closed_and_logs(self):
        import io
        orig = sys.stderr
        sys.stderr = io.StringIO()
        try:
            out = scan._faded_bounce_structure_fields("X", {"structure": "not-a-dict"})
            logged = sys.stderr.getvalue()
        finally:
            sys.stderr = orig
        self.assertIsNone(out["structure_read"])
        self.assertIn("X", logged)


class TestFadedBounceRecentLegFieldsHelper(unittest.TestCase):
    """SPEC-166 #3 — the recency fields (days-ago per squeeze leg, last-3 legs, bounce-peak
    age) computed from an already-fetched s90/s7 pair, pure/offline-testable."""

    def test_days_ago_and_last_three_legs_computed(self):
        import datetime as dt
        today = dt.datetime.now(dt.timezone.utc).date()
        d8 = (today - dt.timedelta(days=8)).isoformat()
        d6 = (today - dt.timedelta(days=6)).isoformat()
        d1 = (today - dt.timedelta(days=1)).isoformat()
        s90 = {"squeezes": [{"day": d8, "pct": 20.0}, {"day": d6, "pct": 18.0},
                            {"day": d1, "pct": 25.0}]}
        s7 = {"days_since_ath": 8}
        out = scan._faded_bounce_recent_leg_fields(s90, s7, scan.FADED_BOUNCE_DEFAULTS)
        self.assertEqual(sorted(out["recent_squeeze_leg_days_ago"]), [1, 6, 8])
        self.assertEqual(len(out["recent_squeeze_legs"]), 3)
        self.assertEqual(out["bounce_high_age_days"], 8)

    def test_no_squeezes_returns_empty_days_ago(self):
        out = scan._faded_bounce_recent_leg_fields({"squeezes": []}, {"days_since_ath": 5},
                                                     scan.FADED_BOUNCE_DEFAULTS)
        self.assertEqual(out["recent_squeeze_leg_days_ago"], [])
        self.assertEqual(out["recent_squeeze_legs"], [])


class TestFadedBounceEnrichTickerHelper(unittest.TestCase):
    """SPEC-183 — the per-ticker enrichment body factored out of
    _faded_bounce_live_candidates into its own function so it can run inside a thread pool
    (concurrency fix) and be injected/mocked in isolation (this test), instead of only being
    reachable through a live network sweep."""

    def test_enrich_ticker_builds_candidate_fields_from_injected_price_structure(self):
        calls = {"days": []}

        class FakePS:
            @staticmethod
            def build_structure(tk, days=90):
                calls["days"].append(days)
                if days == 90:
                    return {"off_ath_pct": -55.0, "current_close": 1.2,
                           "squeezes": [], "vol_daily_m": [], "structure": {"read": "downtrend",
                           "lower_highs": 2, "higher_highs": 0, "last_swing_higher_high": False}}
                return {"atl": 1.0, "days_since_atl": 3, "days_since_ath": 8}

            @staticmethod
            def squeeze_legs_in_window(squeezes, days):
                return 0

        import sys as _sys
        modname = "price_structure"
        real_mod = _sys.modules.get(modname)
        _sys.modules[modname] = FakePS
        orig_gate = scan.aster_execution_gate
        scan.aster_execution_gate = lambda tk, direction="SHORT": {"exit_absorbable_usd": 50000.0}
        try:
            univ_row = {"ticker": "X", "turnover_m": 12.0, "funding_4h": -0.02}
            out = scan._faded_bounce_enrich_ticker("X", univ_row, False, scan.FADED_BOUNCE_DEFAULTS)
        finally:
            if real_mod is not None:
                _sys.modules[modname] = real_mod
            else:
                del _sys.modules[modname]
            scan.aster_execution_gate = orig_gate
        self.assertEqual(out["off_ath_pct"], -55.0)
        self.assertEqual(out["vol24h_m"], 12.0)
        self.assertEqual(out["funding_4h"], -0.02)
        self.assertEqual(out["structure_read"], "downtrend")
        self.assertEqual(calls["days"], [90, 7])

    def test_enrich_ticker_returns_none_on_structure_error(self):
        class FakePS:
            @staticmethod
            def build_structure(tk, days=90):
                return {"error": "not listed"}

        import sys as _sys
        modname = "price_structure"
        real_mod = _sys.modules.get(modname)
        _sys.modules[modname] = FakePS
        try:
            out = scan._faded_bounce_enrich_ticker("X", None, False, scan.FADED_BOUNCE_DEFAULTS)
        finally:
            if real_mod is not None:
                _sys.modules[modname] = real_mod
            else:
                del _sys.modules[modname]
        self.assertIsNone(out)


class TestFadedBounceLiveCandidatesConcurrencyAndTimeout(unittest.TestCase):
    """SPEC-183 — the primary-edge sweep timed out on the serial per-name fetch loop.
    Fixed with a ThreadPoolExecutor fan-out + a per-name timeout: a hanging name costs
    itself (lands in `skipped`, reason 'timeout'), never the whole sweep."""

    def _fake_universe(self, min_vol=10.0, **kw):
        return [{"ticker": "FAST1", "funding_4h": -0.02, "turnover_m": 12.0,
                "funding_raw": -0.02, "interval_h": 4, "chg24": 1.0, "oi": 1.0,
                "price": 1.0, "deep": False},
               {"ticker": "SLOW1", "funding_4h": -0.02, "turnover_m": 12.0,
                "funding_raw": -0.02, "interval_h": 4, "chg24": 1.0, "oi": 1.0,
                "price": 1.0, "deep": False},
               {"ticker": "FAST2", "funding_4h": -0.02, "turnover_m": 12.0,
                "funding_raw": -0.02, "interval_h": 4, "chg24": 1.0, "oi": 1.0,
                "price": 1.0, "deep": False}]

    def test_hanging_name_is_skipped_not_a_sweep_stall(self):
        def fake_enrich(tk, univ_row, on_watchlist, cfg):
            if tk == "SLOW1":
                time.sleep(1.0)
            return {"off_ath_pct": -50.0, "bounce_pct": 20.0, "low_age_days": 1,
                   "vol24h_m": 12.0, "vol_peak24h_m": 20.0, "vol_recent_avg_m": 1.0,
                   "vol_prior_avg_m": 2.0, "funding_4h": -0.02, "squeeze_legs_60d": 0,
                   "distribution": None, "on_watchlist": False, "onchain_available": False,
                   "exit_absorbable_usd": 50000.0, "structure_read": "downtrend",
                   "lower_highs": 2, "higher_highs": 0, "last_swing_higher_high": False,
                   "recent_squeeze_leg_days_ago": [], "recent_squeeze_legs": [],
                   "bounce_high_age_days": 8}

        orig_fetch_universe = scan._fetch_universe
        orig_watchlist_path = None
        scan._fetch_universe = self._fake_universe
        try:
            t0 = time.time()
            cands, skipped = scan._faded_bounce_live_candidates(
                cfg={**scan.FADED_BOUNCE_DEFAULTS, "vol_floor_m": 5.0},
                enrich_fn=fake_enrich, name_timeout_s=0.1, max_workers=4)
            elapsed = time.time() - t0
        finally:
            scan._fetch_universe = orig_fetch_universe
        self.assertLess(elapsed, 5.0)
        self.assertIn("FAST1", cands)
        self.assertIn("FAST2", cands)
        self.assertNotIn("SLOW1", cands)
        skipped_tickers = {s["ticker"] for s in skipped}
        self.assertIn("SLOW1", skipped_tickers)
        slow_entry = next(s for s in skipped if s["ticker"] == "SLOW1")
        self.assertEqual(slow_entry["reason"], "timeout")


class TestFadedBounceUnknownModeFailsLoud(unittest.TestCase):
    """SPEC-120 addendum: an unrecognized scan mode must FAIL LOUDLY (ok:false), never
    silently fall back to the default funding scan — the 8-day false-done masking bug."""

    def test_script_level_unknown_mode_exits_nonzero(self):
        proc = run("--mode", "nonexistent", "--json")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_orchestrator_unknown_mode_is_ok_false(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "orchestrator.py"), "scan", '{"mode":"nonexistent"}'],
            capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        env = json.loads(proc.stdout)
        self.assertFalse(env["ok"])

    def test_known_mode_still_works_through_orchestrator(self):
        payload = json.dumps({"mode": "faded_bounce",
                              "fb_candidates": {"X": TestFadedBounceGates.BASE}})
        proc = subprocess.run(
            [sys.executable, str(ROOT / "orchestrator.py"), "scan", payload],
            capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        env = json.loads(proc.stdout)
        self.assertTrue(env["ok"], env)
        self.assertEqual(env["data"]["mode"], "faded_bounce")
        self.assertEqual(len(env["data"]["candidates"]), 1)


class TestFadedBounceCLIOffline(unittest.TestCase):
    def test_cli_json_offline(self):
        payload = json.dumps({"X": TestFadedBounceGates.BASE})
        proc = run("--mode", "faded_bounce", "--fb-candidates", payload, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        board = json.loads(proc.stdout)
        self.assertEqual(board["mode"], "faded_bounce")
        self.assertEqual(len(board["candidates"]), 1)

    def test_cli_human_view_not_json(self):
        payload = json.dumps({"X": TestFadedBounceGates.BASE})
        proc = run("--mode", "faded_bounce", "--fb-candidates", payload, "--no-color")
        self.assertIn("FADED-BOUNCE SCREEN", proc.stdout)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(proc.stdout)


class TestExistingScanModesUnchanged(unittest.TestCase):
    """SPEC-120 regression: build_scan's output is byte-for-byte identical after the
    _fetch_universe refactor — offline via a monkeypatched fetch() (no network)."""

    TICKERS_RESP = {"retCode": 0, "result": {"list": [
        {"symbol": "AAAUSDT", "fundingRate": "-0.003", "turnover24h": "20000000",
         "price24hPcnt": "0.05", "openInterest": "1000", "lastPrice": "1.5"},
        {"symbol": "BBBUSDT", "fundingRate": "0.004", "turnover24h": "15000000",
         "price24hPcnt": "-0.02", "openInterest": "500", "lastPrice": "0.5"},
        {"symbol": "TINYUSDT", "fundingRate": "-0.01", "turnover24h": "1000000",
         "price24hPcnt": "0.01", "openInterest": "10", "lastPrice": "0.1"},
    ]}}
    INSTR_RESP = {"retCode": 0, "result": {"list": [
        {"symbol": "AAAUSDT", "fundingInterval": 480},
        {"symbol": "BBBUSDT", "fundingInterval": 240},
    ]}}

    def _fake_fetch(self, url):
        if "tickers" in url:
            return self.TICKERS_RESP
        if "instruments-info" in url:
            return self.INSTR_RESP
        return None

    def test_build_scan_output_unchanged_by_the_fetch_universe_refactor(self):
        orig = scan.fetch
        scan.fetch = self._fake_fetch
        try:
            # cross_venue_catalog_rows=[] (SPEC-160 #1): explicit no-op merge, keeps this
            # test offline/deterministic — the live default additionally fans out to the
            # oi_surge catalog venues, covered separately below.
            out = scan.build_scan(min_vol=10.0, side="both", top=25, thresh=0.10,
                                  cross_venue_catalog_rows=[])
        finally:
            scan.fetch = orig
        self.assertEqual(out["universe"], 2)   # TINY dropped by the $10M liquidity gate
        self.assertEqual(len(out["longs"]), 1)
        self.assertEqual(out["longs"][0]["ticker"], "AAA")
        # fundingRate -0.003 -> -0.3%/8h -> normalized *(240/480) -> -0.15%/4h
        self.assertEqual(out["longs"][0]["funding_4h"], -0.15)
        self.assertEqual(len(out["shorts"]), 1)
        self.assertEqual(out["shorts"][0]["ticker"], "BBB")
        # fundingRate 0.004 -> 0.4%/4h -> normalized *(240/240) -> 0.4%/4h
        self.assertEqual(out["shorts"][0]["funding_4h"], 0.4)


class TestFundingScanInheritsOiSurgeUniverse(unittest.TestCase):
    """SPEC-160 #1 — the funding-extremity scan's own single-venue-Bybit liquidity gate made
    it BLIND to names thin on Bybit but liquid cross-venue: ONT at -0.33%/4h on $24M/24h
    cross-venue vol was invisible to the funding scan (n=88, Bybit-only) and only surfaced
    via oi_surge's funding_extreme side-channel (n=1000, cross-venue). Fix: fold in tickers
    from the SAME catalog+aggregate builder oi_surge uses (`_aggregate_oi_catalog`) that
    clear the liquidity floor on SUMMED cross-venue volume, even if absent/thin on Bybit
    alone. Offline via injected `cross_venue_catalog_rows` — no network."""

    def _fake_fetch_empty_bybit(self, url):
        if "tickers" in url:
            return {"retCode": 0, "result": {"list": []}}
        if "instruments-info" in url:
            return {"retCode": 0, "result": {"list": []}}
        return None

    def test_ont_shaped_fixture_thin_on_bybit_liquid_cross_venue_appears_in_funding_scan(self):
        orig = scan.fetch
        scan.fetch = self._fake_fetch_empty_bybit
        try:
            catalog_rows = [
                {"ticker": "ONTUSDT", "venue": "binance", "oi_usd": 5_000_000,
                 "vol24h_usd": 24_000_000, "funding_raw_pct": -0.66, "interval_min": 480,
                 "is_floor": False},
            ]
            out = scan.build_scan(min_vol=10.0, side="both", top=25, thresh=0.10,
                                  cross_venue_catalog_rows=catalog_rows)
        finally:
            scan.fetch = orig
        longs = {c["ticker"]: c for c in out["longs"]}
        self.assertIn("ONT", longs)
        ont = longs["ONT"]
        self.assertEqual(ont["funding_4h"], -0.33)   # -0.66%/8h normalized -> -0.33%/4h
        self.assertEqual(ont["turnover_m"], 24.0)
        self.assertEqual(ont["side"], "long")
        self.assertTrue(ont.get("cross_venue_only"))
        self.assertEqual(out["universe"], 1)

    def test_ticker_below_liquidity_floor_cross_venue_is_excluded(self):
        orig = scan.fetch
        scan.fetch = self._fake_fetch_empty_bybit
        try:
            catalog_rows = [
                {"ticker": "TINYUSDT", "venue": "binance", "oi_usd": 100_000,
                 "vol24h_usd": 2_000_000, "funding_raw_pct": -0.66, "interval_min": 480,
                 "is_floor": False},
            ]
            out = scan.build_scan(min_vol=10.0, side="both", top=25, thresh=0.10,
                                  cross_venue_catalog_rows=catalog_rows)
        finally:
            scan.fetch = orig
        tickers = {c["ticker"] for c in out["longs"] + out["shorts"]}
        self.assertNotIn("TINY", tickers)
        self.assertEqual(out["universe"], 0)

    def test_ticker_already_present_from_bybit_is_not_duplicated(self):
        orig = scan.fetch
        scan.fetch = TestExistingScanModesUnchanged()._fake_fetch
        try:
            catalog_rows = [
                {"ticker": "AAAUSDT", "venue": "binance", "oi_usd": 1_000_000,
                 "vol24h_usd": 50_000_000, "funding_raw_pct": -9.0, "interval_min": 480,
                 "is_floor": False},
            ]
            out = scan.build_scan(min_vol=10.0, side="both", top=25, thresh=0.10,
                                  cross_venue_catalog_rows=catalog_rows)
        finally:
            scan.fetch = orig
        aaa_rows = [c for c in out["longs"] + out["shorts"] if c["ticker"] == "AAA"]
        self.assertEqual(len(aaa_rows), 1)   # not duplicated by the cross-venue merge
        self.assertEqual(out["universe"], 2)   # AAA + BBB, the Bybit-only count


class TestOiSurgeFundingExtreme(unittest.TestCase):
    """SPEC-148 — SPEC-108/112 selection: most-extreme NON-floor cross-venue print, already
    4h-normalized. A floor print must never win regardless of its raw magnitude."""

    def test_floor_excluded_even_if_numerically_larger(self):
        cands = [{"venue": "bybit", "pi_4h": 9.99, "is_floor": True},
                 {"venue": "aster", "pi_4h": -0.15, "is_floor": False}]
        self.assertEqual(scan.select_funding_extreme(cands), {"venue": "aster", "pi_4h": -0.15})

    def test_all_floor_yields_none(self):
        cands = [{"venue": "bybit", "pi_4h": 9.99, "is_floor": True}]
        self.assertIsNone(scan.select_funding_extreme(cands))

    def test_empty_yields_none(self):
        self.assertIsNone(scan.select_funding_extreme([]))


class TestOiSurgeAggregation(unittest.TestCase):
    """SPEC-148 — _aggregate_oi_catalog: per-venue-per-symbol catalog rows -> per-ticker
    cross-venue totals + 4h-normalized funding candidates (SPEC-112 regression: a 1h-interval
    raw print must be normalized to %/4h BEFORE extreme comparison)."""

    def test_1h_interval_normalized_before_aggregation(self):
        rows = [{"ticker": "TICK", "venue": "bitget", "oi_usd": 1_000_000, "vol24h_usd": 500_000,
                 "funding_raw_pct": -0.06, "interval_min": 60, "is_floor": False}]
        agg = scan._aggregate_oi_catalog(rows)
        self.assertEqual(len(agg["TICK"]["funding_candidates"]), 1)
        fc = agg["TICK"]["funding_candidates"][0]
        self.assertEqual(fc["pi_4h"], -0.24)   # -0.06 * 240/60
        self.assertEqual(fc["venue"], "bitget")

    def test_oi_and_vol_summed_across_venues(self):
        rows = [
            {"ticker": "TICK", "venue": "bybit", "oi_usd": 1_000_000, "vol24h_usd": 500_000,
             "funding_raw_pct": None, "interval_min": None, "is_floor": False},
            {"ticker": "TICK", "venue": "bitget", "oi_usd": 500_000, "vol24h_usd": 250_000,
             "funding_raw_pct": None, "interval_min": None, "is_floor": False},
        ]
        agg = scan._aggregate_oi_catalog(rows)
        self.assertEqual(agg["TICK"]["oi_usd_total"], 1_500_000)
        self.assertEqual(agg["TICK"]["vol24h_usd_total"], 750_000)
        self.assertEqual(sorted(agg["TICK"]["venues"]), ["bitget", "bybit"])

    def test_missing_oi_on_some_venues_does_not_poison_the_total(self):
        rows = [
            {"ticker": "TICK", "venue": "binance", "oi_usd": None, "vol24h_usd": 100_000,
             "funding_raw_pct": None, "interval_min": None, "is_floor": False},
            {"ticker": "TICK", "venue": "bybit", "oi_usd": 900_000, "vol24h_usd": 100_000,
             "funding_raw_pct": None, "interval_min": None, "is_floor": False},
        ]
        agg = scan._aggregate_oi_catalog(rows)
        self.assertTrue(agg["TICK"]["oi_known"])
        self.assertEqual(agg["TICK"]["oi_usd_total"], 900_000)


class TestOiSurgeBuildBoard(unittest.TestCase):
    """SPEC-148 — build_oi_surge: pure per-ticker gate/signal evaluation + ranking."""

    NOW = 1_800_000_000.0   # fixed epoch for deterministic baseline-age math

    def _rows(self, ticker, legs):
        """legs: list of (venue, oi_usd, vol24h_usd, funding_raw_pct, interval_min, is_floor)."""
        return [{"ticker": ticker, "venue": v, "oi_usd": oi, "vol24h_usd": vol,
                 "funding_raw_pct": f, "interval_min": iv, "is_floor": fl}
                for (v, oi, vol, f, iv, fl) in legs]

    def test_three_flags_fire_and_rank_first(self):
        # baseline OI 10M -> current 15.5M = +55% (>=40 oi_surge gate)
        # vol total 372M / oi total 15.5M = 24.0 (>=20 vol_oi_brush gate)
        # bitget funding -0.22%/4h non-floor (>=0.20 funding_extreme gate)
        rows = self._rows("TICK", [
            ("bybit", 8_000_000, 200_000_000, None, None, False),
            ("bitget", 7_500_000, 172_000_000, -0.22, 240, False),
            ("hyperliquid", 0.0, 0.0, None, None, False),
        ])
        rows += self._rows("OTHER", [
            ("bybit", 1_000_000, 21_000_000, None, None, False),  # vol/oi=21 -> only 1 flag
        ])
        baseline = {"TICK": {"ts": self.NOW - 3600, "oi_usd_total": 10_000_000,
                             "vol24h_usd_total": 1_000_000, "first_seen_ts": self.NOW - 90000}}
        board = scan.build_oi_surge(rows, baseline=baseline, cfg=None, seeded=False, now_ts=self.NOW)
        self.assertEqual(board["status"], "ok")
        self.assertGreaterEqual(len(board["candidates"]), 1)
        top = board["candidates"][0]
        self.assertEqual(top["ticker"], "TICK")
        self.assertEqual(sorted(top["flags"]), ["funding_extreme", "oi_surge", "vol_oi_brush"])
        self.assertEqual(top["oi_delta_pct"], 55.0)
        self.assertEqual(top["vol_oi_ratio"], 24.0)
        self.assertEqual(top["funding_extreme"], {"venue": "bitget", "pi_4h": -0.22})
        self.assertIn("bybit", top["venues"])
        self.assertIn("bitget", top["venues"])
        self.assertIn("hyperliquid", top["venues"])
        self.assertEqual(top["next_step"], "brief TICK")
        # SPEC-160 minor: flat funding_4h/vol_m fields (triage's naming convention) lifted
        # up from the nested funding_extreme/vol24h_usd — a generic ticker-row consumer
        # shouldn't need to know oi_surge's nested shape.
        self.assertEqual(top["funding_4h"], -0.22)
        self.assertEqual(top["vol_m"], round(372_000_000 / 1e6, 2))

    def test_flat_funding_4h_is_none_when_no_non_floor_print(self):
        rows = self._rows("TICK", [("bybit", 8_000_000, 200_000_000, None, None, False)])
        board = scan.build_oi_surge(rows, baseline={}, cfg=None, seeded=False, now_ts=self.NOW)
        row = next(r for r in board["candidates"] if r["ticker"] == "TICK")
        self.assertIsNone(row["funding_4h"])
        self.assertEqual(row["vol_m"], 200.0)

    def test_stale_baseline_does_not_fire_oi_surge_but_other_flags_still_fire(self):
        rows = self._rows("TICK", [
            ("bybit", 8_000_000, 200_000_000, None, None, False),
            ("bitget", 7_500_000, 172_000_000, -0.22, 240, False),
        ])
        baseline = {"TICK": {"ts": self.NOW - 30 * 3600, "oi_usd_total": 10_000_000,
                             "vol24h_usd_total": 1_000_000, "first_seen_ts": self.NOW - 900000}}
        board = scan.build_oi_surge(rows, baseline=baseline, cfg=None, seeded=False, now_ts=self.NOW)
        row = next(r for r in board["candidates"] if r["ticker"] == "TICK")
        self.assertNotIn("oi_surge", row["flags"])
        self.assertIsNone(row["oi_delta_pct"])
        self.assertAlmostEqual(row["baseline_age_h"], 30.0, places=2)
        self.assertIn("vol_oi_brush", row["flags"])
        self.assertIn("funding_extreme", row["flags"])

    def test_new_listing_flags_the_venue(self):
        rows = self._rows("FRESHY", [("aster", 200_000, 10_000_000, None, None, False)])
        baseline = {"OTHER": {"ts": self.NOW, "oi_usd_total": 1.0, "vol24h_usd_total": 1.0,
                              "first_seen_ts": self.NOW}}
        board = scan.build_oi_surge(rows, baseline=baseline, cfg=None, seeded=False, now_ts=self.NOW)
        row = next(r for r in board["candidates"] if r["ticker"] == "FRESHY")
        self.assertEqual(row["new_listing"], {"venue": "aster"})
        self.assertIn("new_listing", row["flags"])

    def test_liquidity_floor_excludes_and_never_reaches_candidates(self):
        # SPEC-173: the discovery floor is now the shared $10M §7 gate (was a looser $5M).
        rows = self._rows("TINY", [("bybit", 1_000_000, 3_000_000, None, None, False)])
        board = scan.build_oi_surge(rows, baseline={}, cfg=None, seeded=False, now_ts=self.NOW)
        self.assertEqual(board["candidates"], [])
        self.assertEqual(len(board["excluded"]), 1)
        self.assertEqual(board["excluded"][0]["reason"], "liquidity")

    def test_exclude_list_never_reaches_candidates(self):
        rows = self._rows("BTC", [("bybit", 5_000_000_000, 5_000_000_000, -1.0, 240, False)])
        cfg = {**scan.OI_SURGE_DEFAULTS, "exclude": ["BTC"]}
        board = scan.build_oi_surge(rows, baseline={}, cfg=cfg, seeded=False, now_ts=self.NOW)
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "exclude_list")

    def test_zero_matches_is_a_normal_explicit_result(self):
        board = scan.build_oi_surge([], baseline={}, cfg=None, seeded=False, now_ts=self.NOW)
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"], [])

    def test_seeded_first_run_yields_no_candidates_but_writes_baseline(self):
        rows = self._rows("TICK", [("bybit", 8_000_000, 200_000_000, -0.22, 240, False)])
        board = scan.build_oi_surge(rows, baseline={}, cfg=None, seeded=True, now_ts=self.NOW)
        self.assertEqual(board["status"], "seeded")
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"], [])
        self.assertIn("TICK", board["baseline_out"])
        self.assertEqual(board["baseline_out"]["TICK"]["oi_usd_total"], 8_000_000)


class TestOiSurgeBaselineStore(unittest.TestCase):
    """SPEC-148 — baseline file semantics: missing -> seed (never NOTOK); corrupt -> LOUD
    (SPEC-137 convention, never a clean-looking empty sweep)."""

    def test_missing_file_status_missing(self):
        with tempfile.TemporaryDirectory() as d:
            baseline, status = scan.load_oi_surge_baseline(Path(d) / "nope.json")
            self.assertEqual(baseline, {})
            self.assertEqual(status, "missing")

    def test_corrupt_file_status_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "baseline.json"
            p.write_text("{not json")
            baseline, status = scan.load_oi_surge_baseline(p)
            self.assertEqual(status, "corrupt")

    def test_valid_file_roundtrips(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "baseline.json"
            data = {"TICK": {"ts": 123.0, "oi_usd_total": 1.0, "vol24h_usd_total": 2.0,
                             "first_seen_ts": 100.0}}
            scan.save_oi_surge_baseline(data, p)
            baseline, status = scan.load_oi_surge_baseline(p)
            self.assertEqual(status, "ok")
            self.assertEqual(baseline, data)


class TestOiSurgeRunOrchestration(unittest.TestCase):
    """SPEC-148 — run_oi_surge: the file-I/O + live-fetch wrapper around build_oi_surge."""

    def test_missing_baseline_file_is_seeded_run_and_writes_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            bpath = Path(d) / "baseline.json"
            rows = [{"ticker": "TICK", "venue": "bybit", "oi_usd": 1_000_000,
                    "vol24h_usd": 10_000_000, "funding_raw_pct": -0.2, "interval_min": 240,
                    "is_floor": False}]
            board = scan.run_oi_surge(baseline_path=bpath, catalog_rows=rows)
            self.assertEqual(board["status"], "seeded")
            self.assertTrue(bpath.exists())
            saved = json.loads(bpath.read_text())
            self.assertIn("TICK", saved)

    def test_corrupt_baseline_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            bpath = Path(d) / "baseline.json"
            bpath.write_text("not json at all")
            board = scan.run_oi_surge(baseline_path=bpath, catalog_rows=[])
            self.assertIsNone(board)

    def test_one_venue_timing_out_isolated_to_venues_errored(self):
        with tempfile.TemporaryDirectory() as d:
            bpath = Path(d) / "baseline.json"
            existing = {"TICK": {"ts": 1.0, "oi_usd_total": 1.0, "vol24h_usd_total": 1.0,
                                 "first_seen_ts": 1.0}}
            bpath.write_text(json.dumps(existing))

            def slow():
                time.sleep(1)
                return []

            def fast():
                return [{"ticker": "TICK", "venue": "bybit", "oi_usd": 1_000_000,
                        "vol24h_usd": 10_000_000, "funding_raw_pct": None,
                        "interval_min": None, "is_floor": False}]

            venues = {"slowvenue": slow, "bybit": fast}
            board = scan.run_oi_surge(baseline_path=bpath, venues=venues, per_venue_timeout=0.2)
            self.assertEqual(board["meta"]["venues_errored"], ["slowvenue"])
            self.assertTrue(any(r["ticker"] == "TICK" for r in board["candidates"] + board["excluded"]))


class TestOiSurgeUnknownModeFailsLoud(unittest.TestCase):
    """SPEC-148 (SPEC-120 addendum convention): an unrecognized --mode still fails loudly."""

    def test_script_level_unknown_mode_exits_nonzero(self):
        proc = run("--mode", "nonexistent", "--json")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


class TestOiSurgeCorruptBaselineCLIFailsLoud(unittest.TestCase):
    """SPEC-148 / SPEC-137: a corrupt baseline is LOUD-NOTOK at the CLI/orchestrator boundary
    — nonzero exit + empty stdout, never a clean-looking empty candidates:[] result."""

    def test_corrupt_baseline_exits_nonzero_no_stdout(self):
        # No --oi-catalog: the corrupt-baseline check runs BEFORE any catalog fetch
        # (run_oi_surge short-circuits), so this stays network-free and deterministic.
        with tempfile.TemporaryDirectory() as d:
            bpath = Path(d) / "baseline.json"
            bpath.write_text("not json")
            proc = run("--mode", "oi_surge", "--oi-baseline-path", str(bpath), "--json")
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

    def test_orchestrator_surfaces_ok_false(self):
        with tempfile.TemporaryDirectory() as d:
            bpath = Path(d) / "baseline.json"
            bpath.write_text("not json")
            payload = json.dumps({"mode": "oi_surge", "oi_baseline_path": str(bpath)})
            proc = subprocess.run(
                [sys.executable, str(ROOT / "orchestrator.py"), "scan", payload],
                capture_output=True, text=True, cwd=str(ROOT), timeout=60)
            env = json.loads(proc.stdout)
            self.assertFalse(env["ok"])


class TestOiSurgeCLIOffline(unittest.TestCase):
    """SPEC-148 — fully offline CLI path (catalog + baseline both injected, no network,
    no state-file I/O)."""

    def test_cli_json_offline(self):
        rows = [{"ticker": "TICK", "venue": "bybit", "oi_usd": 8_000_000,
                "vol24h_usd": 200_000_000, "funding_raw_pct": -0.22, "interval_min": 240,
                "is_floor": False}]
        payload = json.dumps(rows)
        baseline = json.dumps({"TICK": {"ts": 1.0, "oi_usd_total": 1_000_000,
                                        "vol24h_usd_total": 1.0, "first_seen_ts": 1.0}})
        proc = run("--mode", "oi_surge", "--oi-catalog", payload, "--oi-baseline", baseline, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        board = json.loads(proc.stdout)
        self.assertEqual(board["mode"], "oi_surge")
        self.assertEqual(board["status"], "ok")

    def test_cli_human_view_not_json(self):
        proc = run("--mode", "oi_surge", "--oi-catalog", "[]", "--oi-baseline", "{}", "--no-color")
        self.assertIn("OI-SURGE", proc.stdout)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(proc.stdout)


class TestLiquidityGate(unittest.TestCase):
    """SPEC-173 — the ONE §7 liquidity gate every scan mode shares: <$10M excluded:liquidity,
    [$10M,$25M) passes tagged liquidity_tier:scout, >=$25M passes untagged."""

    def test_below_floor_excludes(self):
        g = scan.liquidity_gate(9_999_999.0)
        self.assertTrue(g["excluded"])
        self.assertEqual(g["reason"], "liquidity")
        self.assertIsNone(g["liquidity_tier"])

    def test_at_floor_passes_scout_tier(self):
        g = scan.liquidity_gate(10_000_000.0)
        self.assertFalse(g["excluded"])
        self.assertEqual(g["liquidity_tier"], "scout")

    def test_just_below_scout_ceiling_still_scout(self):
        g = scan.liquidity_gate(24_999_999.0)
        self.assertFalse(g["excluded"])
        self.assertEqual(g["liquidity_tier"], "scout")

    def test_at_scout_ceiling_is_full(self):
        g = scan.liquidity_gate(25_000_000.0)
        self.assertFalse(g["excluded"])
        self.assertIsNone(g["liquidity_tier"])

    def test_none_never_excludes(self):
        g = scan.liquidity_gate(None)
        self.assertFalse(g["excluded"])
        self.assertIsNone(g["liquidity_tier"])


class TestFadedBounceLiquidityGate(unittest.TestCase):
    """SPEC-173 — the §7 liquidity gate applied inside faded_bounce, as a HARD gate
    independent of the pre-existing 'faded' (volume-decay) gate."""

    def _cand(self, **overrides):
        return {**TestFadedBounceGates.BASE, **overrides}

    def test_below_10m_excludes_liquidity(self):
        board = scan.build_faded_bounce({"X": self._cand(vol24h_m=9.0)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "liquidity")
        self.assertFalse(board["excluded"][0]["gates"]["liquidity"])

    def test_10_to_25m_passes_tagged_scout(self):
        board = scan.build_faded_bounce({"X": self._cand(vol24h_m=15.0)})
        self.assertEqual(board["excluded"], [])
        self.assertEqual(board["candidates"][0]["liquidity_tier"], "scout")

    def test_above_25m_passes_untagged(self):
        board = scan.build_faded_bounce({"X": self._cand(vol24h_m=30.0)})
        self.assertEqual(board["excluded"], [])
        self.assertIsNone(board["candidates"][0]["liquidity_tier"])

    def test_explicit_vol24h_usd_field_wins_over_vol24h_m(self):
        # a caller supplying the $ figure directly (e.g. a cross-venue summed read) must not
        # be second-guessed by the $M field.
        board = scan.build_faded_bounce({"X": self._cand(vol24h_m=30.0, vol24h_usd=1_000_000.0)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "liquidity")


class TestFadedBounceInstrumentExclusion(unittest.TestCase):
    """SPEC-173 — stock/ETF/index perps (KORU et al) are not desk instruments."""

    def _cand(self, **overrides):
        return {**TestFadedBounceGates.BASE, **overrides}

    def test_listed_ticker_excludes_not_crypto(self):
        cfg = {**scan.FADED_BOUNCE_DEFAULTS, "instrument_exclusions": {"KORU"}}
        board = scan.build_faded_bounce({"KORU": self._cand()}, cfg=cfg)
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "not_crypto")

    def test_unlisted_ticker_passes(self):
        cfg = {**scan.FADED_BOUNCE_DEFAULTS, "instrument_exclusions": {"KORU"}}
        board = scan.build_faded_bounce({"X": self._cand()}, cfg=cfg)
        self.assertEqual(board["excluded"], [])


class TestFadedBounceAsterExecutionGate(unittest.TestCase):
    """SPEC-173 — Aster execution gate: below min_aster_exit_usd (config/sizing.json,
    default $10K) excludes aster_book; the row surfaces exit_absorbable_usd either way."""

    def _cand(self, **overrides):
        return {**TestFadedBounceGates.BASE, **overrides}

    def test_thin_aster_book_excludes(self):
        board = scan.build_faded_bounce({"X": self._cand(exit_absorbable_usd=1200.0)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "aster_book")
        self.assertEqual(board["excluded"][0]["exit_absorbable_usd"], 1200.0)

    def test_missing_read_fails_closed(self):
        board = scan.build_faded_bounce({"X": self._cand(exit_absorbable_usd=None)})
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "aster_book")

    def test_deep_book_passes_and_surfaces_figure(self):
        board = scan.build_faded_bounce({"X": self._cand(exit_absorbable_usd=42000.0)})
        self.assertEqual(board["excluded"], [])
        self.assertEqual(board["candidates"][0]["exit_absorbable_usd"], 42000.0)

    def test_custom_min_aster_exit_usd_cfg_honored(self):
        cfg = {**scan.FADED_BOUNCE_DEFAULTS, "min_aster_exit_usd": 50000.0}
        board = scan.build_faded_bounce({"X": self._cand(exit_absorbable_usd=42000.0)}, cfg=cfg)
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "aster_book")


class TestAsterExecutionGateFunction(unittest.TestCase):
    """SPEC-173 — aster_execution_gate(): pure given an injected book_fn (no network)."""

    BOOK = ([(0.0990, 50000.0), (0.0985, 200000.0)],   # bids — 1.0% / 1.5% below mid
            [(0.1010, 50000.0), (0.1015, 200000.0)])   # asks — 1.0% / 1.5% above mid

    def _fn(self, bids=None, asks=None, err=None):
        b, a = self.BOOK
        return lambda sym: (bids if bids is not None else b,
                            asks if asks is not None else a, err)

    def test_short_walks_bid_side(self):
        out = scan.aster_execution_gate("X", direction="SHORT",
                                        cfg={"min_aster_exit_usd": 10000, "aster_exit_band_pct": 2.0},
                                        book_fn=self._fn())
        # mid = (0.0990+0.1010)/2 = 0.1000; both bid levels sit within 2% of mid
        self.assertFalse(out["excluded"])
        self.assertAlmostEqual(out["exit_absorbable_usd"],
                               0.0990 * 50000.0 + 0.0985 * 200000.0, places=2)

    def test_long_walks_ask_side(self):
        out = scan.aster_execution_gate("X", direction="LONG",
                                        cfg={"min_aster_exit_usd": 10000, "aster_exit_band_pct": 2.0},
                                        book_fn=self._fn())
        self.assertAlmostEqual(out["exit_absorbable_usd"],
                               0.1010 * 50000.0 + 0.1015 * 200000.0, places=2)

    def test_thin_book_excludes(self):
        out = scan.aster_execution_gate(
            "X", direction="SHORT",
            cfg={"min_aster_exit_usd": 10000, "aster_exit_band_pct": 2.0},
            book_fn=self._fn(bids=[(0.0999, 10.0)], asks=[(0.1001, 10.0)]))
        self.assertTrue(out["excluded"])
        self.assertEqual(out["reason"], "aster_book")

    def test_empty_book_excludes_closed(self):
        out = scan.aster_execution_gate("X", direction="SHORT",
                                        cfg={"min_aster_exit_usd": 10000, "aster_exit_band_pct": 2.0},
                                        book_fn=self._fn(bids=[], asks=[]))
        self.assertTrue(out["excluded"])
        self.assertEqual(out["reason"], "aster_book")
        self.assertIsNone(out["exit_absorbable_usd"])
        self.assertFalse(out["available"])

    def test_fetch_exception_excludes_closed(self):
        def blows_up(sym):
            raise RuntimeError("timeout")
        out = scan.aster_execution_gate("X", direction="SHORT",
                                        cfg={"min_aster_exit_usd": 10000, "aster_exit_band_pct": 2.0},
                                        book_fn=blows_up)
        self.assertTrue(out["excluded"])
        self.assertEqual(out["reason"], "aster_book")
        self.assertFalse(out["available"])


class TestOiSurgeInstrumentAndLiquidityGates(unittest.TestCase):
    """SPEC-173 — oi_surge carries the same shared instrument-exclusion + liquidity gates."""

    def _rows(self, ticker, legs):
        return [{"ticker": ticker, "venue": v, "oi_usd": oi, "vol24h_usd": vol,
                 "funding_raw_pct": f, "interval_min": iv, "is_floor": fl}
                for (v, oi, vol, f, iv, fl) in legs]

    def test_stock_etf_perp_excludes_not_crypto(self):
        rows = self._rows("KORU", [("bitget", 1_100_000_000, 50_000_000, None, None, False)])
        cfg = {**scan.OI_SURGE_DEFAULTS, "instrument_exclusions": {"KORU"}}
        board = scan.build_oi_surge(rows, baseline={}, cfg=cfg, seeded=False, now_ts=1_800_000_000.0)
        self.assertEqual(board["candidates"], [])
        self.assertEqual(board["excluded"][0]["reason"], "not_crypto")

    def test_10_to_25m_passes_tagged_scout(self):
        rows = self._rows("TICK", [("bybit", 1_000_000, 15_000_000, None, None, False)])
        board = scan.build_oi_surge(rows, baseline={}, cfg=None, seeded=False, now_ts=1_800_000_000.0)
        row = next(r for r in board["candidates"] if r["ticker"] == "TICK")
        self.assertEqual(row["liquidity_tier"], "scout")

    def test_above_25m_untagged(self):
        rows = self._rows("TICK", [("bybit", 1_000_000, 30_000_000, None, None, False)])
        board = scan.build_oi_surge(rows, baseline={}, cfg=None, seeded=False, now_ts=1_800_000_000.0)
        row = next(r for r in board["candidates"] if r["ticker"] == "TICK")
        self.assertIsNone(row["liquidity_tier"])


class TestInstrumentExclusionConfigFile(unittest.TestCase):
    """SPEC-173 — config/instrument_exclusions.json is a real, editable file; a missing/
    malformed file fails OPEN (never blocks the sweep on a config problem)."""

    def test_load_from_a_real_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "excl.json"
            p.write_text(json.dumps({"tickers": ["KORU", "soxl"]}))
            s = scan.load_instrument_exclusions(p)
            self.assertEqual(s, {"KORU", "SOXL"})
            self.assertTrue(scan.is_excluded_instrument("koru", s))
            self.assertFalse(scan.is_excluded_instrument("BTC", s))

    def test_missing_file_fails_open_empty_set(self):
        with tempfile.TemporaryDirectory() as d:
            s = scan.load_instrument_exclusions(Path(d) / "nope.json")
            self.assertEqual(s, set())

    def test_committed_default_file_excludes_koru(self):
        s = scan.load_instrument_exclusions()
        self.assertIn("KORU", s)


class TestSqueezeLegDefinitionParity(unittest.TestCase):
    """SPEC-173 — ONE squeeze-leg definition (build_structure's own `squeezes`, a >=15%
    day-close move) used by every counter. squeeze_legs_in_window() is a pure windowing
    helper OVER that list — never a second, independently-derived detector — so a caller
    (faded_bounce) windowing it can never drift from price_structure's own count, the
    scout sweep 2026-08-28 bug (H 5 vs 10+, UAI 5 vs 7)."""

    DAY = 86_400_000

    def _kline(self, ts_ms, close, qv=1_000_000):
        return [ts_ms, str(close), str(close * 1.02), str(close * 0.98), str(close),
               "1000", 0, str(qv)]

    def _fixture(self, closes, start_ts):
        return [self._kline(start_ts + i * self.DAY, c) for i, c in enumerate(closes)]

    def test_windowed_count_matches_raw_list_when_window_covers_full_range(self):
        # 40 daily closes, three independent >=15% day-close jumps scattered through it —
        # ALL fall inside a window sized to the full fetched range.
        closes = [1.0] * 40
        closes[10] = closes[9] * 1.20     # +20% leg
        closes[11] = closes[10] * 1.05    # follow-through, not itself >=15%
        closes[25] = closes[24] * 1.16    # +16% leg
        closes[38] = closes[37] * 1.30    # +30% leg
        start_ts = 1_700_000_000_000
        fixture = self._fixture(closes, start_ts)

        orig_fetch = PS.fetch
        PS.fetch = lambda url: fixture
        try:
            s = PS.build_structure("PARITY", days=40)
        finally:
            PS.fetch = orig_fetch

        self.assertEqual(len(s["squeezes"]), 3)
        last_ts = start_ts + (len(closes) - 1) * self.DAY
        as_of = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).date()
        windowed = PS.squeeze_legs_in_window(s["squeezes"], window_days=40, as_of=as_of)
        # THE parity assertion: the two counters (price_structure's raw squeezes list, and
        # the shared windowing helper over it) agree exactly when their windows match.
        self.assertEqual(windowed, len(s["squeezes"]))

        # a scan.py-style caller reuses the SAME function for the CLAUDE §6 60d gate —
        # never a hand-rolled cutoff loop.
        squeeze_legs_60d = PS.squeeze_legs_in_window(s["squeezes"], 60, as_of=as_of)
        self.assertEqual(squeeze_legs_60d, len(s["squeezes"]))

    def test_narrower_window_excludes_the_older_leg(self):
        closes = [1.0] * 40
        closes[5] = closes[4] * 1.20     # old leg — outside a 20d trailing window
        closes[35] = closes[34] * 1.20   # recent leg — inside it
        start_ts = 1_700_000_000_000
        fixture = self._fixture(closes, start_ts)
        orig_fetch = PS.fetch
        PS.fetch = lambda url: fixture
        try:
            s = PS.build_structure("PARITY2", days=40)
        finally:
            PS.fetch = orig_fetch
        self.assertEqual(len(s["squeezes"]), 2)
        last_ts = start_ts + (len(closes) - 1) * self.DAY
        as_of = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).date()
        windowed = PS.squeeze_legs_in_window(s["squeezes"], window_days=20, as_of=as_of)
        self.assertEqual(windowed, 1)
        self.assertLess(windowed, len(s["squeezes"]))


class TestSqueezeLegsInWindowPure(unittest.TestCase):
    """SPEC-173 — squeeze_legs_in_window() as a standalone pure function."""

    def test_counts_within_window_only(self):
        today = datetime(2026, 8, 28, tzinfo=timezone.utc).date()
        squeezes = [{"day": "2026-08-27"}, {"day": "2026-06-01"}, {"day": "2026-08-01"}]
        self.assertEqual(PS.squeeze_legs_in_window(squeezes, 60, as_of=today), 2)

    def test_boundary_day_is_inclusive(self):
        today = datetime(2026, 8, 28, tzinfo=timezone.utc).date()
        squeezes = [{"day": "2026-06-29"}]   # exactly 60 days before as_of
        self.assertEqual(PS.squeeze_legs_in_window(squeezes, 60, as_of=today), 1)

    def test_malformed_day_skipped_not_crashed(self):
        today = datetime(2026, 8, 28, tzinfo=timezone.utc).date()
        squeezes = [{"day": "not-a-date"}, {"day": "2026-08-01"}, {}]
        self.assertEqual(PS.squeeze_legs_in_window(squeezes, 60, as_of=today), 1)

    def test_empty_list_is_zero(self):
        self.assertEqual(PS.squeeze_legs_in_window([], 60), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
