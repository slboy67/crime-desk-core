#!/usr/bin/env python3
"""SPEC-177 — battlefield verdict + leverage_state (oi_mc.py).

Run: python3 -m unittest tests.test_oi_mc_battlefield -v

Offline-deterministic — `build_battlefield`/`battlefield_verdict`/
`leverage_state_for_window`/`_range_held` are pure compute (no I/O); the one I/O
helper (`fetch_leverage_window`) is tested via a monkeypatched fetch seam.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

spec = importlib.util.spec_from_file_location("oi_mc_bf_t", ROOT / "capabilities" / "oi_mc.py")
OM = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OM)


# ---------------------------------------------------------------------------
# battlefield_verdict — asymmetric perp_led/spot_led/mixed/UNKNOWN (req 1)
# ---------------------------------------------------------------------------
class TestBattlefieldVerdict(unittest.TestCase):
    def test_bluai_shaped_perp_vol_7_6x_spot_oi_mc_0_57_is_perp_led(self):
        # perp vol 7.6x spot -> ratio 7.6 >= perp_led_min(4.0); OI/MC 0.57 -> PERP_HEAVY too
        ratio, verdict = OM.battlefield_verdict(76_000_000, 10_000_000, "PERP_HEAVY")
        self.assertAlmostEqual(ratio, 7.6)
        self.assertEqual(verdict, "perp_led")

    def test_null_spot_leg_with_oi_mc_0_6_is_perp_led(self):
        # no real spot venue (ratio null) but OI/MC >= 0.5 (PERP_HEAVY) IS the condition
        ratio, verdict = OM.battlefield_verdict(50_000_000, None, "PERP_HEAVY")
        self.assertIsNone(ratio)
        self.assertEqual(verdict, "perp_led")

    def test_both_legs_present_and_low_is_spot_led(self):
        # ratio <= spot_led_max(2.0) AND oi_mc flag "normal" (< 0.5)
        ratio, verdict = OM.battlefield_verdict(4_000_000, 3_000_000, "normal")
        self.assertAlmostEqual(ratio, 4_000_000 / 3_000_000)
        self.assertEqual(verdict, "spot_led")

    def test_ratio_3_0_is_mixed(self):
        ratio, verdict = OM.battlefield_verdict(9_000_000, 3_000_000, "normal")
        self.assertAlmostEqual(ratio, 3.0)
        self.assertEqual(verdict, "mixed")

    def test_both_null_is_unknown(self):
        ratio, verdict = OM.battlefield_verdict(None, None, None)
        self.assertIsNone(ratio)
        self.assertEqual(verdict, "UNKNOWN")

    def test_perp_casino_flag_also_forces_perp_led_even_at_low_ratio(self):
        ratio, verdict = OM.battlefield_verdict(1_000_000, 900_000, "PERP_CASINO")
        self.assertEqual(verdict, "perp_led")

    def test_spot_led_requires_both_legs_present_null_spot_never_spot_led(self):
        ratio, verdict = OM.battlefield_verdict(1_000_000, None, "normal")
        self.assertIsNone(ratio)
        self.assertNotEqual(verdict, "spot_led")

    def test_thresholds_read_from_injected_cfg(self):
        ratio, verdict = OM.battlefield_verdict(5_000_000, 2_000_000, "normal",
                                                 cfg={"perp_led_min": 10.0, "spot_led_max": 1.0})
        # ratio 2.5 -> below the raised perp_led_min(10), above the lowered spot_led_max(1.0)
        self.assertAlmostEqual(ratio, 2.5)
        self.assertEqual(verdict, "mixed")


# ---------------------------------------------------------------------------
# _range_held — closes-only, wick-throughs never break hold (req 2)
# ---------------------------------------------------------------------------
class TestRangeHeld(unittest.TestCase):
    def test_long_side_close_below_swing_low_breaks_hold(self):
        self.assertFalse(OM._range_held([10, 9.5, 9.8], swing_point=9.6, direction="long"))

    def test_long_side_all_closes_above_swing_low_holds(self):
        self.assertTrue(OM._range_held([10, 9.7, 9.9], swing_point=9.6, direction="long"))

    def test_short_side_close_above_swing_high_breaks_hold(self):
        self.assertFalse(OM._range_held([10, 10.5, 10.1], swing_point=10.4, direction="short"))

    def test_short_side_all_closes_below_swing_high_holds(self):
        self.assertTrue(OM._range_held([10, 10.2, 10.3], swing_point=10.4, direction="short"))

    def test_none_when_no_closes_or_no_swing_point(self):
        self.assertIsNone(OM._range_held([], swing_point=9.6, direction="long"))
        self.assertIsNone(OM._range_held([10, 9.5], swing_point=None, direction="long"))


# ---------------------------------------------------------------------------
# leverage_state_for_window — dual-window enum incl. WASH_PINNED override (req 2)
# ---------------------------------------------------------------------------
class TestLeverageStateForWindow(unittest.TestCase):
    def test_wash_tag_overrides_to_wash_pinned_regardless_of_delta_oi(self):
        r = OM.leverage_state_for_window(delta_oi_pct=-40, range_held=True,
                                         oi_sides_tag="WASH", threshold_pct=8.0, window_label="4h")
        self.assertEqual(r["state"], "WASH_PINNED")

    def test_drain_and_held_closes_is_reset_constructive(self):
        # range_held computed upstream as True even with a wick through the swing (req 2) —
        # this function only consumes the boolean, doesn't recompute it.
        r = OM.leverage_state_for_window(delta_oi_pct=-20, range_held=True,
                                         oi_sides_tag=None, threshold_pct=8.0, window_label="4h")
        self.assertEqual(r["state"], "RESET_CONSTRUCTIVE")

    def test_drain_and_closed_below_is_move_done(self):
        r = OM.leverage_state_for_window(delta_oi_pct=-20, range_held=False,
                                         oi_sides_tag=None, threshold_pct=8.0, window_label="4h")
        self.assertEqual(r["state"], "MOVE_DONE")

    def test_build_and_rangebound_is_loading(self):
        r = OM.leverage_state_for_window(delta_oi_pct=20, range_held=True,
                                         oi_sides_tag=None, threshold_pct=8.0, window_label="48h")
        self.assertEqual(r["state"], "LOADING")

    def test_build_and_trending_is_trend_feeding(self):
        r = OM.leverage_state_for_window(delta_oi_pct=20, range_held=False,
                                         oi_sides_tag=None, threshold_pct=8.0, window_label="48h")
        self.assertEqual(r["state"], "TREND_FEEDING")

    def test_sub_threshold_delta_oi_is_null_verdict(self):
        r = OM.leverage_state_for_window(delta_oi_pct=3, range_held=True,
                                         oi_sides_tag=None, threshold_pct=8.0, window_label="4h")
        self.assertIsNone(r["state"])

    def test_missing_series_is_unknown_with_reason(self):
        r = OM.leverage_state_for_window(delta_oi_pct=None, range_held=None,
                                         oi_sides_tag=None, threshold_pct=8.0, window_label="4h")
        self.assertEqual(r["state"], "UNKNOWN")
        self.assertTrue(r["reason"])


# ---------------------------------------------------------------------------
# build_battlefield — full envelope composition (pure)
# ---------------------------------------------------------------------------
class TestBuildBattlefield(unittest.TestCase):
    def test_full_envelope_shape_and_leverage_windows_both_present(self):
        r = OM.build_battlefield(
            oi_usd=50_000_000, mc_usd=87_719_298,       # ratio 0.57 -> PERP_HEAVY
            perp_vol_24h=76_000_000, spot_vol_24h=10_000_000,
            leverage_4h={"delta_oi_pct": -20, "range_held": True, "oi_sides_tag": None},
            leverage_48h=None,
        )
        self.assertEqual(r["battlefield"], "perp_led")
        self.assertAlmostEqual(r["perp_spot_ratio"], 7.6)
        self.assertEqual(r["leverage_state"]["leverage_4h"]["state"], "RESET_CONSTRUCTIVE")
        self.assertEqual(r["leverage_state"]["leverage_48h"]["state"], "UNKNOWN")
        self.assertIn("oi_mc", r)
        self.assertEqual(r["oi_mc"]["oi_mc_flag"], "PERP_HEAVY")

    def test_trend_feeding_annotation_fires(self):
        r = OM.build_battlefield(
            perp_vol_24h=None, spot_vol_24h=None,
            leverage_4h={"delta_oi_pct": 25, "range_held": False, "oi_sides_tag": None},
        )
        self.assertTrue(any("reset+next-OI-expansion" in a for a in r["annotations"]))

    def test_reset_constructive_on_perp_led_annotation_fires(self):
        r = OM.build_battlefield(
            oi_usd=50_000_000, mc_usd=87_719_298,
            leverage_48h={"delta_oi_pct": -20, "range_held": True, "oi_sides_tag": None},
        )
        self.assertEqual(r["battlefield"], "perp_led")
        self.assertTrue(any("next-OI-expansion is the entry" in a for a in r["annotations"]))

    def test_no_new_fetches_pure_function_never_imports_urllib(self):
        import inspect
        src = inspect.getsource(OM.build_battlefield)
        self.assertNotIn("urllib", src)
        self.assertNotIn("_get(", src)


# ---------------------------------------------------------------------------
# commit_time_annotation — spot-natured-entry-on-perp-led-name flag (req 4 bullet 1)
# ---------------------------------------------------------------------------
class TestCommitTimeAnnotation(unittest.TestCase):
    def test_spot_retest_entry_on_perp_led_name_flags(self):
        note = OM.commit_time_annotation("perp_led", "spot_level_retest")
        self.assertIn("spot-natured entry on perp-led name", note)

    def test_perp_structure_entry_on_perp_led_name_no_flag(self):
        self.assertIsNone(OM.commit_time_annotation("perp_led", "perp_structure_trigger"))

    def test_spot_led_name_never_flags(self):
        self.assertIsNone(OM.commit_time_annotation("spot_led", "spot_level_retest"))


# ---------------------------------------------------------------------------
# fetch_leverage_window — best-effort I/O helper, mocked fetch seam
# ---------------------------------------------------------------------------
class TestFetchLeverageWindow(unittest.TestCase):
    def setUp(self):
        self._http_get = OM._http_get

    def tearDown(self):
        OM._http_get = self._http_get

    def test_ok_shape_computes_delta_oi_and_range_held(self):
        def fake(url):
            if "openInterestHist" in url:
                return [{"sumOpenInterest": "1000"}, {"sumOpenInterest": "1200"}]
            if "klines" in url:
                return [[0, 0, 0, 0, "9.8"], [0, 0, 0, 0, "9.9"]]
            return None
        OM._http_get = fake
        r = OM.fetch_leverage_window("X", period="15m", kline_interval="15m", limit=16,
                                     swing_point=9.5, direction="long")
        self.assertAlmostEqual(r["delta_oi_pct"], 20.0)
        self.assertTrue(r["range_held"])

    def test_fetch_failure_degrades_to_none_never_raises(self):
        OM._http_get = lambda url: None
        r = OM.fetch_leverage_window("X", period="15m", kline_interval="15m", limit=16,
                                     swing_point=9.5, direction="long")
        self.assertIsNone(r["delta_oi_pct"])
        self.assertIsNone(r["range_held"])


# ---------------------------------------------------------------------------
# Call-site wiring — classify.py annotate_oi_mc gains battlefield/leverage_state
# ---------------------------------------------------------------------------
class TestClassifyBattlefieldWiring(unittest.TestCase):
    def setUp(self):
        CL_spec = importlib.util.spec_from_file_location(
            "classify_bf_t", ROOT / "capabilities" / "classify.py")
        self.CL = importlib.util.module_from_spec(CL_spec)
        CL_spec.loader.exec_module(self.CL)

    def test_perp_led_from_vol_m_reuse_no_new_fetch(self):
        # vol_m 76 ($M) -> perp_vol_24h 76,000,000; oi_mc PERP_HEAVY also forces perp_led
        rows = [{"ticker": "BLUAI", "direction": "SHORT",
                "live": {"oi": 50_000_000, "price": 1.0, "vol_m": 76.0}}]
        self.CL.annotate_oi_mc(rows, fetch_mc=lambda t: 50_000_000 / 0.57)
        r = rows[0]
        self.assertEqual(r["battlefield"], "perp_led")
        # board scope has no spot leg (no per-row spot fetch) -> ratio null, verdict
        # still correctly perp_led via the heavy-OI/MC-flag branch (req 1)
        self.assertIsNone(r["perp_spot_ratio"])
        self.assertEqual(r["leverage_state"]["leverage_4h"]["state"], "UNKNOWN")
        self.assertEqual(r["leverage_state"]["leverage_48h"]["state"], "UNKNOWN")

    def test_no_live_data_still_carries_null_battlefield_fields(self):
        rows = [{"ticker": "X", "direction": "SHORT", "live": None}]
        self.CL.annotate_oi_mc(rows, fetch_mc=lambda t: 10_000_000)
        r = rows[0]
        self.assertIn("battlefield", r)
        self.assertIn("leverage_state", r)
        self.assertEqual(r["battlefield"], "UNKNOWN")

    def test_existing_oi_mc_keys_unaffected_by_battlefield_addition(self):
        rows = [{"ticker": "EVAA", "direction": "SHORT",
                "live": {"oi": 50_000_000, "price": 1.0}}]
        self.CL.annotate_oi_mc(rows, fetch_mc=lambda t: 50_000_000 / 2.52)
        r = rows[0]
        self.assertEqual(r["oi_mc_flag"], "PERP_CASINO")
        self.assertAlmostEqual(r["oi_mc_ratio"], 2.52, places=2)


# ---------------------------------------------------------------------------
# Call-site wiring — brief.py _perp_layer gains battlefield/leverage_state
# ---------------------------------------------------------------------------
class _FakeFuture:
    def __init__(self, value):
        self._value = value

    def result(self, timeout=None):
        return self._value


class TestBriefBattlefieldWiring(unittest.TestCase):
    def setUp(self):
        BR_spec = importlib.util.spec_from_file_location(
            "brief_bf_t", ROOT / "capabilities" / "brief.py")
        self.BR = importlib.util.module_from_spec(BR_spec)
        BR_spec.loader.exec_module(self.BR)
        self._saved = {k: getattr(self.BR, k) for k in ("build_analyse",)}
        self._saved_fetch_mc = self.BR.OM.fetch_market_cap
        self.BR.OM.fetch_market_cap = lambda t, cg_id=None: None

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.BR, k, v)
        self.BR.OM.fetch_market_cap = self._saved_fetch_mc

    def test_perp_led_reuses_analyse_spot_vol_and_live_vol_m_zero_new_fetch(self):
        self.BR.build_analyse = lambda ticker, days=90: {
            "ticker": ticker, "verdict": "WATCH", "direction": "WATCH", "tier": "watch",
            "funding_4h": 0.0, "funding_venue": "binance", "funding_unavailable": False,
            "oi_chg_pct": None, "oi_chg_pct_48h": None, "near_ath": False,
            "cvd_verdict": None, "price": 1.0, "onchain": "OK", "nonce_signal": "QUIET",
            "spot_vol_24h_usd": 10_000_000, "notes": [],
        }
        live_fut = _FakeFuture({"price": 1.0, "funding_4h": 0.0, "vol_m": 76.0,
                                "oi": 50_000_000})
        perp = self.BR._perp_layer("X", live_fut=live_fut)
        self.assertAlmostEqual(perp["perp_spot_ratio"], 7.6)
        self.assertEqual(perp["battlefield"], "perp_led")
        self.assertEqual(perp["leverage_state"]["leverage_4h"]["state"], "UNKNOWN")

    def test_oi_construction_block_passed_through_from_build_analyse(self):
        # SPEC-180 req 5: brief renders the FULL oi_construction block, zero extra
        # fetch — reuses build_analyse's own oi_construction output.
        oic = {"verdict": "MIXED", "gating_ok": True}
        self.BR.build_analyse = lambda ticker, days=90: {
            "ticker": ticker, "verdict": "WATCH", "direction": "WATCH", "tier": "watch",
            "funding_4h": 0.0, "funding_venue": "binance", "funding_unavailable": False,
            "oi_chg_pct": None, "oi_chg_pct_48h": None, "near_ath": False,
            "cvd_verdict": None, "price": 1.0, "onchain": "OK", "nonce_signal": "QUIET",
            "notes": [], "oi_construction": oic,
        }
        perp = self.BR._perp_layer("X", live_fut=None)
        self.assertEqual(perp["oi_construction"], oic)

    def test_no_live_no_crash_battlefield_unknown(self):
        self.BR.build_analyse = lambda ticker, days=90: {
            "ticker": ticker, "verdict": "PASS", "direction": "PASS", "tier": "PASS",
            "funding_4h": None, "funding_venue": None, "funding_unavailable": True,
            "oi_chg_pct": None, "oi_chg_pct_48h": None, "near_ath": False,
            "cvd_verdict": None, "price": None, "onchain": "OK", "nonce_signal": "QUIET",
            "notes": [],
        }
        perp = self.BR._perp_layer("X", live_fut=None)
        self.assertEqual(perp["battlefield"], "UNKNOWN")
        self.assertIsNone(perp["perp_spot_ratio"])


# ---------------------------------------------------------------------------
# Call-site wiring — triage.py _build_row gains battlefield/leverage_state
# ---------------------------------------------------------------------------
class TestTriageBattlefieldWiring(unittest.TestCase):
    def setUp(self):
        TR_spec = importlib.util.spec_from_file_location(
            "triage_bf_t", ROOT / "capabilities" / "triage.py")
        self.TR = importlib.util.module_from_spec(TR_spec)
        TR_spec.loader.exec_module(self.TR)
        self._orig_fetch_mc = self.TR.OM.fetch_market_cap

    def tearDown(self):
        self.TR.OM.fetch_market_cap = self._orig_fetch_mc

    def _base_data(self, oi_usd, spot_vol_24h_usd=None, qvol24=76_000_000):
        return {
            "ticker": "X",
            "binance": {"fund_latest": 0.02, "interval_min": 240, "px": 1.0, "ch24": 5.0,
                       "hi24": 1.1, "lo24": 0.9, "qvol24": qvol24, "oi_usd": oi_usd,
                       "oi_pct_24h": 3.0},
            "bybit": {"fund_latest": 0.01, "interval_min": 240},
            "aster": {}, "bitget": {},
            "spot_vol_24h_usd": spot_vol_24h_usd,
        }

    def test_perp_led_from_qvol24_reuse_and_spot_leg(self):
        self.TR.OM.fetch_market_cap = lambda ticker: 50_000_000 / 0.57
        data = self._base_data(50_000_000, spot_vol_24h_usd=10_000_000, qvol24=76_000_000)
        meta = {"ticker": "X", "category": "A", "state": "watch"}
        row = self.TR._build_row(data, meta)
        self.assertAlmostEqual(row["perp_spot_ratio"], 7.6)
        self.assertEqual(row["battlefield"], "perp_led")
        self.assertEqual(row["leverage_state"]["leverage_4h"]["state"], "UNKNOWN")

    def test_no_spot_leg_still_carries_battlefield_fields(self):
        self.TR.OM.fetch_market_cap = lambda ticker: None
        data = self._base_data(None, spot_vol_24h_usd=None)
        meta = {"ticker": "X", "category": "A", "state": "watch"}
        row = self.TR._build_row(data, meta)
        self.assertIn("battlefield", row)
        self.assertIn("leverage_state", row)


# ---------------------------------------------------------------------------
# Call-site wiring — tape.py prints leverage_state (req 3), reusing its own
# already-fetched bars/oi_series — zero new fetch.
# ---------------------------------------------------------------------------
class TestTapeLeverageStateWiring(unittest.TestCase):
    def setUp(self):
        TP_spec = importlib.util.spec_from_file_location(
            "tape_bf_t", ROOT / "capabilities" / "tape.py")
        self.TP = importlib.util.module_from_spec(TP_spec)
        TP_spec.loader.exec_module(self.TP)

    def test_drain_and_held_closes_is_reset_constructive(self):
        bars = [{"close": 10.0}, {"close": 9.8}, {"close": 9.9}]
        oi_series = [{"oi": 1000.0}, {"oi": 800.0}]   # -20% ΔOI
        r = self.TP._leverage_state_from_tape(bars, oi_series, level=9.5, window_min=60)
        self.assertEqual(r["state"], "RESET_CONSTRUCTIVE")
        self.assertEqual(r["window"], "60m")

    def test_no_level_no_crash_range_held_none_degrades_to_unknown(self):
        bars = [{"close": 10.0}, {"close": 9.8}]
        oi_series = [{"oi": 1000.0}, {"oi": 800.0}]
        r = self.TP._leverage_state_from_tape(bars, oi_series, level=None, window_min=60)
        self.assertEqual(r["state"], "UNKNOWN")

    def test_no_oi_series_degrades_to_unknown_never_raises(self):
        r = self.TP._leverage_state_from_tape([], [], level=None, window_min=60)
        self.assertEqual(r["state"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
