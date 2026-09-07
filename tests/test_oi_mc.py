#!/usr/bin/env python3
"""SPEC-122 — `oi_mc`: OI/MC ratio as a "perp-casino" flag.

Run:  python3 -m unittest tests.test_oi_mc -v

Corpus rule under test: the ledger's single worst signature is mindshare/blowoff-top
short (n=324, hit 32%, total -26.3R) — vertical perp pumps on low-MC names where the
game is entirely in the derivatives. OI/MC instantly names the profile: EVAA printed
252% OI/MC live 2026-07-10 (DoubleEdge CT intel) — OI 2.5x the ENTIRE market cap.

Offline-deterministic: every fetch (CoinGecko MC) is monkeypatched; no network.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

spec = importlib.util.spec_from_file_location("oi_mc_t", ROOT / "capabilities" / "oi_mc.py")
OM = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OM)


# ── DoD fixtures, verbatim ───────────────────────────────────────────────────
class TestComputeRatio(unittest.TestCase):
    def test_evaa_shaped_fixture_ratio_252pct(self):
        mc_usd = 50_000_000 / 2.52
        ratio = OM.compute_ratio(50_000_000, mc_usd)
        self.assertAlmostEqual(ratio, 2.52, places=2)

    def test_normal_name_oi_0_3x_mc(self):
        ratio = OM.compute_ratio(3_000_000, 10_000_000)
        self.assertAlmostEqual(ratio, 0.3)

    def test_mid_name_oi_0_8x_mc(self):
        ratio = OM.compute_ratio(8_000_000, 10_000_000)
        self.assertAlmostEqual(ratio, 0.8)

    def test_mc_missing_returns_none(self):
        self.assertIsNone(OM.compute_ratio(50_000_000, None))

    def test_oi_missing_returns_none(self):
        self.assertIsNone(OM.compute_ratio(None, 10_000_000))

    def test_mc_zero_returns_none(self):
        self.assertIsNone(OM.compute_ratio(50_000_000, 0))

    def test_negative_oi_returns_none(self):
        self.assertIsNone(OM.compute_ratio(-100, 10_000_000))


class TestFlagForRatio(unittest.TestCase):
    def test_evaa_ratio_is_perp_casino(self):
        self.assertEqual(OM.flag_for_ratio(2.52), "PERP_CASINO")

    def test_normal_ratio_is_normal(self):
        self.assertEqual(OM.flag_for_ratio(0.3), "normal")

    def test_mid_ratio_is_perp_heavy(self):
        self.assertEqual(OM.flag_for_ratio(0.8), "PERP_HEAVY")

    def test_boundary_0_5_is_perp_heavy(self):
        self.assertEqual(OM.flag_for_ratio(0.5), "PERP_HEAVY")

    def test_boundary_1_5_is_perp_heavy_not_casino(self):
        self.assertEqual(OM.flag_for_ratio(1.5), "PERP_HEAVY")

    def test_just_above_1_5_is_perp_casino(self):
        self.assertEqual(OM.flag_for_ratio(1.51), "PERP_CASINO")

    def test_none_ratio_returns_none_flag_never_false_normal(self):
        self.assertIsNone(OM.flag_for_ratio(None))

    def test_thresholds_configurable(self):
        self.assertEqual(OM.flag_for_ratio(0.2, cfg={"perp_heavy_min": 0.1, "perp_casino_min": 0.3}),
                         "PERP_HEAVY")


class TestCaveatFor(unittest.TestCase):
    def test_perp_casino_short_row_gets_caveat(self):
        c = OM.caveat_for(2.52, "PERP_CASINO", "SHORT")
        self.assertIsNotNone(c)
        self.assertIn("PERP_CASINO", c)
        self.assertIn("252%", c)
        self.assertIn("−26R", c)

    def test_perp_casino_long_row_no_caveat(self):
        self.assertIsNone(OM.caveat_for(2.52, "PERP_CASINO", "LONG"))

    def test_perp_heavy_short_row_no_caveat(self):
        self.assertIsNone(OM.caveat_for(0.8, "PERP_HEAVY", "SHORT"))

    def test_normal_short_row_no_caveat(self):
        self.assertIsNone(OM.caveat_for(0.3, "normal", "SHORT"))

    def test_no_direction_no_caveat(self):
        self.assertIsNone(OM.caveat_for(2.52, "PERP_CASINO", None))

    def test_suspect_fake_liq_cross_reference_appended(self):
        c = OM.caveat_for(2.52, "PERP_CASINO", "SHORT", liq_verdict="SUSPECT_FAKE")
        self.assertIn("SUSPECT_FAKE", c)

    def test_confirmed_liq_verdict_not_appended(self):
        c = OM.caveat_for(2.52, "PERP_CASINO", "SHORT", liq_verdict="CONFIRMED")
        self.assertNotIn("SUSPECT_FAKE", c)


class TestBuildOiMc(unittest.TestCase):
    def test_evaa_full_envelope(self):
        mc_usd = 50_000_000 / 2.52
        r = OM.build_oi_mc(50_000_000, mc_usd, direction="SHORT")
        self.assertEqual(r["oi_mc_flag"], "PERP_CASINO")
        self.assertAlmostEqual(r["oi_mc_ratio"], 2.52, places=2)
        self.assertIsNotNone(r["oi_mc_caveat"])

    def test_mc_missing_ratio_and_flag_and_caveat_all_none(self):
        r = OM.build_oi_mc(50_000_000, None, direction="SHORT")
        self.assertIsNone(r["oi_mc_ratio"])
        self.assertIsNone(r["oi_mc_flag"])
        self.assertIsNone(r["oi_mc_caveat"])

    def test_normal_name_no_flag_no_caveat(self):
        r = OM.build_oi_mc(3_000_000, 10_000_000, direction="SHORT")
        self.assertEqual(r["oi_mc_flag"], "normal")
        self.assertIsNone(r["oi_mc_caveat"])


class TestFetchMarketCap(unittest.TestCase):
    def test_uses_coingecko_layer_market_cap(self):
        import pull5
        orig = pull5.coingecko_layer
        pull5.coingecko_layer = lambda ticker, cg_id=None: {"market_cap": 19_841_269.84}
        try:
            mc = OM.fetch_market_cap("EVAA")
        finally:
            pull5.coingecko_layer = orig
        self.assertAlmostEqual(mc, 19_841_269.84)

    def test_error_result_degrades_to_none(self):
        import pull5
        orig = pull5.coingecko_layer
        pull5.coingecko_layer = lambda ticker, cg_id=None: {"_error": "no coingecko id found"}
        try:
            mc = OM.fetch_market_cap("NOTFOUND")
        finally:
            pull5.coingecko_layer = orig
        self.assertIsNone(mc)

    def test_exception_degrades_to_none_never_raises(self):
        import pull5
        orig = pull5.coingecko_layer

        def boom(ticker, cg_id=None):
            raise RuntimeError("network exploded")
        pull5.coingecko_layer = boom
        try:
            mc = OM.fetch_market_cap("EVAA")
        finally:
            pull5.coingecko_layer = orig
        self.assertIsNone(mc)


class TestLoadCfg(unittest.TestCase):
    def test_default_thresholds(self):
        cfg = OM.load_cfg()
        self.assertEqual(cfg["perp_heavy_min"], 0.5)
        self.assertEqual(cfg["perp_casino_min"], 1.5)


# ── integration: triage row carries the field + caveat ──────────────────────
class TestTriageIntegration(unittest.TestCase):
    def setUp(self):
        TR_spec = importlib.util.spec_from_file_location("triage_oimc_t", ROOT / "capabilities" / "triage.py")
        self.TR = importlib.util.module_from_spec(TR_spec)
        TR_spec.loader.exec_module(self.TR)
        self._orig_fetch_mc = self.TR.OM.fetch_market_cap

    def tearDown(self):
        self.TR.OM.fetch_market_cap = self._orig_fetch_mc

    def _base_data(self, oi_usd):
        return {
            "ticker": "EVAA",
            "binance": {"fund_latest": 0.02, "interval_min": 240, "px": 1.0, "ch24": 5.0,
                       "hi24": 1.1, "lo24": 0.9, "qvol24": 20_000_000, "oi_usd": oi_usd,
                       "oi_pct_24h": 3.0},
            "bybit": {"fund_latest": 0.01, "interval_min": 240},
            "aster": {},
            "bitget": {},
        }

    def test_evaa_shaped_row_carries_perp_casino_and_caveat_on_short(self):
        mc_usd = 50_000_000 / 2.52
        self.TR.OM.fetch_market_cap = lambda ticker: mc_usd
        data = self._base_data(50_000_000)
        meta = {"ticker": "EVAA", "category": "B", "state": "SHORT distribution top — blowoff"}
        row = self.TR._build_row(data, meta)
        self.assertAlmostEqual(row["oi_mc_ratio"], 2.52, places=2)
        self.assertEqual(row["oi_mc_flag"], "PERP_CASINO")
        self.assertEqual(row["direction"], "short")
        self.assertIsNotNone(row["oi_mc_caveat"])
        self.assertIn("PERP_CASINO", row["oi_mc_caveat"])

    def test_normal_name_row_unflagged(self):
        self.TR.OM.fetch_market_cap = lambda ticker: 10_000_000
        data = self._base_data(3_000_000)
        meta = {"ticker": "OPN", "category": "B", "state": "watch only"}
        row = self.TR._build_row(data, meta)
        self.assertEqual(row["oi_mc_flag"], "normal")
        self.assertIsNone(row["oi_mc_caveat"])

    def test_mc_unavailable_ratio_null_no_false_flag(self):
        self.TR.OM.fetch_market_cap = lambda ticker: None
        data = self._base_data(50_000_000)
        meta = {"ticker": "EVAA", "category": "B", "state": "SHORT distribution top"}
        row = self.TR._build_row(data, meta)
        self.assertIsNone(row["oi_mc_ratio"])
        self.assertIsNone(row["oi_mc_flag"])
        self.assertIsNone(row["oi_mc_caveat"])

    def test_regression_existing_row_keys_unchanged(self):
        """Existing contract keys survive byte-identical; the new field is additive."""
        self.TR.OM.fetch_market_cap = lambda ticker: None
        data = self._base_data(None)
        meta = {"ticker": "OPN", "category": "B", "state": "watch only"}
        row = self.TR._build_row(data, meta)
        for k in ("ticker", "category", "price", "chg24", "range_pct", "vol_m", "funding_pi",
                  "funding_venue", "funding_range", "funding_suspect", "funding_raw_pi",
                  "funding_interval_min", "oi_chg_pct", "ls_ratio", "signals", "direction",
                  "tier", "memo"):
            self.assertIn(k, row)


# ── integration: classify board annotation carries the field + caveat ───────
class TestClassifyIntegration(unittest.TestCase):
    def setUp(self):
        CL_spec = importlib.util.spec_from_file_location("classify_oimc_t", ROOT / "capabilities" / "classify.py")
        self.CL = importlib.util.module_from_spec(CL_spec)
        CL_spec.loader.exec_module(self.CL)

    def test_annotate_oi_mc_flags_perp_casino_short_row(self):
        mc_usd = 50_000_000 / 2.52
        rows = [{"ticker": "EVAA", "direction": "SHORT",
                "live": {"oi": 50_000_000, "price": 1.0}}]
        self.CL.annotate_oi_mc(rows, fetch_mc=lambda ticker: mc_usd)
        r = rows[0]
        self.assertEqual(r["oi_mc_flag"], "PERP_CASINO")
        self.assertAlmostEqual(r["oi_mc_ratio"], 2.52, places=2)
        self.assertIsNotNone(r["oi_mc_caveat"])

    def test_annotate_oi_mc_normal_name_no_flag(self):
        rows = [{"ticker": "OPN", "direction": "SHORT",
                "live": {"oi": 3_000_000, "price": 1.0}}]
        self.CL.annotate_oi_mc(rows, fetch_mc=lambda ticker: 10_000_000)
        r = rows[0]
        self.assertEqual(r["oi_mc_flag"], "normal")
        self.assertIsNone(r["oi_mc_caveat"])

    def test_annotate_oi_mc_mc_missing_ratio_null(self):
        rows = [{"ticker": "EVAA", "direction": "SHORT",
                "live": {"oi": 50_000_000, "price": 1.0}}]
        self.CL.annotate_oi_mc(rows, fetch_mc=lambda ticker: None)
        r = rows[0]
        self.assertIsNone(r["oi_mc_ratio"])
        self.assertIsNone(r["oi_mc_flag"])
        self.assertIsNone(r["oi_mc_caveat"])

    def test_annotate_oi_mc_no_live_data_degrades_silently(self):
        rows = [{"ticker": "EVAA", "direction": "SHORT", "live": None}]
        self.CL.annotate_oi_mc(rows, fetch_mc=lambda ticker: 10_000_000)
        r = rows[0]
        self.assertIsNone(r["oi_mc_ratio"])
        self.assertIsNone(r.get("oi_mc_flag"))

    def test_annotate_oi_mc_never_raises_on_fetch_exception(self):
        rows = [{"ticker": "EVAA", "direction": "SHORT",
                "live": {"oi": 50_000_000, "price": 1.0}}]

        def boom(ticker):
            raise RuntimeError("boom")
        self.CL.annotate_oi_mc(rows, fetch_mc=boom)  # must not raise
        self.assertIsNone(rows[0]["oi_mc_ratio"])

    def test_classify_json_output_carries_oi_mc_fields(self):
        import inspect
        src = inspect.getsource(self.CL.main)
        self.assertIn("oi_mc_ratio", src)
        self.assertIn("annotate_oi_mc", src)


# ── integration: brief perp layer carries the field ──────────────────────────
class TestBriefIntegration(unittest.TestCase):
    def setUp(self):
        BR_spec = importlib.util.spec_from_file_location("brief_oimc_t", ROOT / "capabilities" / "brief.py")
        self.BR = importlib.util.module_from_spec(BR_spec)
        BR_spec.loader.exec_module(self.BR)

    def test_perp_layer_carries_oi_mc_ratio_and_flag(self):
        mc_usd = 50_000_000 / 2.52
        self.BR.build_analyse = lambda ticker: {
            "verdict": "SHORT", "direction": "SHORT", "tier": 1, "funding_4h": 0.05,
            "oi_chg_pct": 10.0, "near_ath": True, "cvd_verdict": None, "price": 1.0,
        }
        self.BR.OM.fetch_market_cap = lambda ticker: mc_usd
        live_fut = _FakeFuture({"venue": "binance", "primary_venue": "binance", "price": 1.0,
                                "oi": 50_000_000, "funding_4h": 0.05, "venues": {}})
        out = self.BR._perp_layer("EVAA", live_fut)
        self.assertAlmostEqual(out["oi_mc_ratio"], 2.52, places=2)
        self.assertEqual(out["oi_mc_flag"], "PERP_CASINO")
        self.assertIn("PERP_CASINO", out["oi_mc_caveat"])

    def test_perp_layer_mc_unavailable_degrades_to_null(self):
        self.BR.build_analyse = lambda ticker: {
            "verdict": "SHORT", "direction": "SHORT", "tier": 1, "funding_4h": 0.05,
            "oi_chg_pct": 10.0, "near_ath": True, "cvd_verdict": None, "price": 1.0,
        }
        self.BR.OM.fetch_market_cap = lambda ticker: None
        live_fut = _FakeFuture({"venue": "binance", "primary_venue": "binance", "price": 1.0,
                                "oi": 50_000_000, "funding_4h": 0.05, "venues": {}})
        out = self.BR._perp_layer("EVAA", live_fut)
        self.assertIsNone(out["oi_mc_ratio"])
        self.assertIsNone(out["oi_mc_flag"])

    def test_perp_layer_regression_existing_keys_survive(self):
        self.BR.build_analyse = lambda ticker: {
            "verdict": "CONFIRM", "direction": "neutral", "tier": None, "funding_4h": 0.01,
            "oi_chg_pct": None, "near_ath": False, "cvd_verdict": None, "price": 1.0,
        }
        self.BR.OM.fetch_market_cap = lambda ticker: None
        out = self.BR._perp_layer("OPN", None)
        for k in ("available", "verdict", "direction", "tier", "funding_4h",
                  "funding_unavailable", "funding_venue", "floor_suspect", "funding_by_venue",
                  "oi_chg_pct", "near_ath", "cvd_verdict", "price", "onchain_status",
                  "nonce_signal"):
            self.assertIn(k, out)


class _FakeFuture:
    """Minimal stand-in for a concurrent.futures.Future — `.result(timeout=...)` only."""
    def __init__(self, value):
        self._value = value

    def result(self, timeout=None):
        return self._value


if __name__ == "__main__":
    unittest.main(verbosity=2)
