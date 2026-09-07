#!/usr/bin/env python3
"""SPEC-129 — venue_map.py: full cross-venue coverage map.

Run:  python3 tests/test_venue_map.py

All network mocked — offline-deterministic. Composer-level tests (`build_venue_map`)
inject a `venues` dict of stub probe functions, bypassing the real HTTP adapters
entirely; adapter-level tests monkeypatch `venue_map._get` to drive one adapter's
parsing/not-listed/error logic against canned venue responses.
"""
import importlib.util
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


VM = _load("venue_map")


# ---------------------------------------------------------------------------
# DoD #1 — 6-venue fixture (2 CEX + HL + a DEX + not_listed + error): oi_share/top
# math correct; funding_extreme picks the most-extreme non-floor print across ALL
# venues including a DEX.
# ---------------------------------------------------------------------------
class TestSixVenueSweep(unittest.TestCase):
    def test_oi_share_top_venue_and_cross_venue_extreme(self):
        venues = {
            "binance": lambda t: VM._ok(-0.20, 480, oi_usd=40_000_000, vol24h_usd=5_000_000),
            "bybit": lambda t: VM._ok(-0.10, 480, oi_usd=30_000_000, vol24h_usd=3_000_000),
            "hyperliquid": lambda t: VM._ok(-0.50, 60, oi_usd=20_000_000, vol24h_usd=1_000_000),
            "lighter": lambda t: VM._ok(-0.30, 60, oi_usd=10_000_000, vol24h_usd=500_000),
            "aster": lambda t: {"status": "not_listed"},
            "paradex": lambda t: {"status": "error", "reason": "timeout >6s"},
        }
        r = VM.build_venue_map("LAB", venues=venues)
        self.assertEqual(r["ticker"], "LAB")
        self.assertEqual(r["n_listed"], 4)
        self.assertEqual(r["total_oi_usd"], 100_000_000)
        self.assertEqual(r["top_oi_venue"], "binance")
        self.assertAlmostEqual(r["oi_top_share_pct"], 40.0)
        self.assertAlmostEqual(r["venues"]["lighter"]["oi_share_pct"], 10.0)
        self.assertAlmostEqual(r["venues"]["hyperliquid"]["oi_share_pct"], 20.0)
        # hyperliquid's 1h print normalizes to -2.00%/4h — the most extreme of the set,
        # beating binance's native -0.20%/4h (cross-venue, includes a DEX-style venue)
        self.assertEqual(r["funding_extreme"], {"venue": "hyperliquid", "pi_4h": -2.0})
        self.assertEqual(r["venues"]["aster"], {"status": "not_listed"})
        self.assertEqual(r["venues"]["paradex"]["status"], "error")
        self.assertNotIn("oi_usd", r["venues"]["aster"])
        self.assertNotIn("oi_usd", r["venues"]["paradex"])


# ---------------------------------------------------------------------------
# DoD #2 — 1h-interval venue (aster/HL-style) raw print normalized to 4h BEFORE the
# extreme comparison (SPEC-112 regression): a 1h -0.20%/hr venue (-0.80%/4h) must beat
# a native 4h -0.50%/4h venue at face value, never compare raw prints directly.
# ---------------------------------------------------------------------------
class TestOkNormalization(unittest.TestCase):
    def test_1h_interval_normalizes_to_4h(self):
        b = VM._ok(-0.15, 60)
        self.assertAlmostEqual(b["funding_pi_4h"], -0.60)
        self.assertEqual(b["funding_raw_pi"], -0.15)
        self.assertEqual(b["interval_min"], 60)

    def test_4h_native_passthrough(self):
        b = VM._ok(-0.30, 240)
        self.assertAlmostEqual(b["funding_pi_4h"], -0.30)


class TestExtremeUsesNormalizedValue(unittest.TestCase):
    def test_1h_venue_raw_print_beats_larger_native_4h_print_after_normalization(self):
        venues = {
            "hyperliquid": lambda t: VM._ok(-0.20, 60),   # -0.20%/hr -> -0.80%/4h
            "binance": lambda t: VM._ok(-0.50, 240),      # -0.50%/4h native, larger RAW value
        }
        r = VM.build_venue_map("X", venues=venues)
        self.assertEqual(r["funding_extreme"]["venue"], "hyperliquid")
        self.assertAlmostEqual(r["funding_extreme"]["pi_4h"], -0.80)


# ---------------------------------------------------------------------------
# DoD #3 — Bybit floored +0.005 excluded from funding_extreme; still shown w/ is_floor.
# ---------------------------------------------------------------------------
class TestFloorExclusion(unittest.TestCase):
    def test_floored_venue_excluded_from_extreme_but_still_shown(self):
        venues = {
            "bybit": lambda t: VM._ok(0.005, 480, is_floor=True),
            "binance": lambda t: VM._ok(-0.40, 480),
        }
        r = VM.build_venue_map("X", venues=venues)
        self.assertEqual(r["funding_extreme"]["venue"], "binance")
        self.assertIn("bybit", r["venues"])
        self.assertTrue(r["venues"]["bybit"]["is_floor"])
        self.assertEqual(r["venues"]["bybit"]["status"], "ok")


# ---------------------------------------------------------------------------
# DoD #4 — one venue adapter raising / timing out -> that venue status:"error", sweep
# completes, totals computed over ok venues only.
# ---------------------------------------------------------------------------
class TestAdapterFailureIsolated(unittest.TestCase):
    def test_raising_adapter_isolated_as_error_sweep_completes(self):
        def boom(t):
            raise RuntimeError("venue down")
        venues = {
            "binance": lambda t: VM._ok(-0.40, 480, oi_usd=10_000_000),
            "brokenvenue": boom,
        }
        r = VM.build_venue_map("X", venues=venues)
        self.assertEqual(r["venues"]["brokenvenue"]["status"], "error")
        self.assertEqual(r["n_listed"], 1)
        self.assertEqual(r["total_oi_usd"], 10_000_000)

    def test_timeout_isolated_as_error_sweep_completes(self):
        def slow(t):
            time.sleep(2)
            return VM._ok(-0.40, 480)
        venues = {
            "binance": lambda t: VM._ok(-0.20, 480, oi_usd=5_000_000),
            "slowvenue": slow,
        }
        r = VM.build_venue_map("X", venues=venues, per_venue_timeout=0.3)
        self.assertEqual(r["venues"]["slowvenue"]["status"], "error")
        self.assertIn("timeout", r["venues"]["slowvenue"]["reason"])
        self.assertEqual(r["n_listed"], 1)
        self.assertEqual(r["total_oi_usd"], 5_000_000)


# ---------------------------------------------------------------------------
# DoD #5 — venue answering with no market -> not_listed, never error.
# ---------------------------------------------------------------------------
class TestNotListedDistinctFromError(unittest.TestCase):
    def test_not_listed_excluded_from_totals_but_distinct_status(self):
        venues = {
            "binance": lambda t: VM._ok(-0.10, 480, oi_usd=1_000_000),
            "vest": lambda t: {"status": "not_listed"},
        }
        r = VM.build_venue_map("X", venues=venues)
        self.assertEqual(r["venues"]["vest"]["status"], "not_listed")
        self.assertNotIn("oi_usd", r["venues"]["vest"])
        self.assertEqual(r["n_listed"], 1)


# ---------------------------------------------------------------------------
# DoD #6 — concurrency envelope: stubbed 1s-latency adapters x8 -> wall-clock < 4s.
# ---------------------------------------------------------------------------
class TestConcurrencyEnvelope(unittest.TestCase):
    def test_eight_slow_venues_run_concurrently_not_serially(self):
        def slow_ok(t):
            time.sleep(1)
            return VM._ok(-0.10, 480, oi_usd=1_000_000)
        venues = {f"v{i}": slow_ok for i in range(8)}
        start = time.time()
        r = VM.build_venue_map("X", venues=venues, per_venue_timeout=6)
        elapsed = time.time() - start
        self.assertLess(elapsed, 4.0)
        self.assertEqual(r["n_listed"], 8)


# ---------------------------------------------------------------------------
# Adapter-level parsing coverage (beyond the composer-level DoD tests) — verifies the
# not_listed/error distinction each live adapter actually implements, not just the
# generic composer plumbing.
# ---------------------------------------------------------------------------
class _AdapterPatch(unittest.TestCase):
    def setUp(self):
        self._get = VM._get
        self._bn_iv = VM.RF.binance_interval_min
        self._by_iv = VM.RF.bybit_interval_min

    def tearDown(self):
        VM._get = self._get
        VM.RF.binance_interval_min = self._bn_iv
        VM.RF.bybit_interval_min = self._by_iv


class TestBinanceAdapter(_AdapterPatch):
    def test_ok_shape(self):
        def fake_get(url, timeout=VM.TIMEOUT):
            if "premiumIndex" in url:
                return {"lastFundingRate": "-0.0020", "markPrice": "10.0"}, None
            if "openInterest" in url:
                return {"openInterest": "1000000"}, None
            if "24hr" in url:
                return {"quoteVolume": "5000000"}, None
            return None, "error"
        VM._get = fake_get
        VM.RF.binance_interval_min = lambda sym: 480
        r = VM.probe_binance("LAB")
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["funding_raw_pi"], -0.20)
        self.assertAlmostEqual(r["funding_pi_4h"], -0.10)   # -0.20%/8h -> %/4h (interval_min=480)
        self.assertAlmostEqual(r["oi_usd"], 10_000_000.0)
        self.assertAlmostEqual(r["vol24h_usd"], 5_000_000.0)

    def test_invalid_symbol_is_not_listed(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: ({"code": -1121, "msg": "Invalid symbol."}, 400)
        self.assertEqual(VM.probe_binance("NOPE")["status"], "not_listed")

    def test_fetch_failure_is_error_not_not_listed(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (None, "error")
        self.assertEqual(VM.probe_binance("LAB")["status"], "error")


class TestBybitAdapter(_AdapterPatch):
    def test_invalid_symbol_retcode_is_not_listed(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (
            {"retCode": 10001, "retMsg": "params error: symbol invalid", "result": {}}, None)
        self.assertEqual(VM.probe_bybit("NOPE")["status"], "not_listed")

    def test_other_retcode_is_error_not_not_listed(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (
            {"retCode": 10006, "retMsg": "rate limit exceeded", "result": {}}, None)
        self.assertEqual(VM.probe_bybit("LAB")["status"], "error")

    def test_ok_shape_uses_openinterestvalue_directly_as_usd(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: ({
            "retCode": 0, "retMsg": "OK",
            "result": {"list": [{"fundingRate": "0.0001", "openInterestValue": "2000000",
                                 "turnover24h": "300000"}]},
        }, None)
        VM.RF.bybit_interval_min = lambda sym: 480
        r = VM.probe_bybit("LAB")
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["oi_usd"], 2_000_000.0)
        self.assertAlmostEqual(r["vol24h_usd"], 300_000.0)


class TestExtendedAdapter(unittest.TestCase):
    def setUp(self):
        self._get = VM._get

    def tearDown(self):
        VM._get = self._get

    def test_all_zero_response_is_not_listed_not_error(self):
        # SPEC-129 §3: Extended answers 200/OK with all-zero fields for an unlisted market —
        # a genuinely listed perp never marks at exactly 0, so this is not_listed, not a
        # fabricated zero datum.
        VM._get = lambda url, timeout=VM.TIMEOUT: ({"status": "OK", "data": {
            "markPrice": "0", "fundingRate": "0", "openInterest": "0", "dailyVolume": "0"}}, None)
        self.assertEqual(VM.probe_extended("NOPE")["status"], "not_listed")

    def test_ok_shape(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: ({"status": "OK", "data": {
            "markPrice": "63800", "fundingRate": "0.000013",
            "openInterest": "62370043.5", "dailyVolume": "140519713.5"}}, None)
        r = VM.probe_extended("BTC")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["interval_min"], 60)
        self.assertAlmostEqual(r["oi_usd"], 62370043.5)


class TestLighterAdapter(unittest.TestCase):
    def setUp(self):
        self._get = VM._get

    def tearDown(self):
        VM._get = self._get

    def test_symbol_not_in_orderbook_details_is_not_listed(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: ({"order_book_details": [
            {"symbol": "ETH", "mark_price": "1900", "open_interest": "1000",
             "daily_quote_token_volume": "1000000"}]}, None)
        self.assertEqual(VM.probe_lighter("NOPE")["status"], "not_listed")

    def test_ok_uses_lighter_native_rate_not_reference_rates(self):
        def fake_get(url, timeout=VM.TIMEOUT):
            if "orderBookDetails" in url:
                return {"order_book_details": [
                    {"symbol": "BTC", "mark_price": "64000", "open_interest": "2000",
                     "daily_quote_token_volume": "700000000"}]}, None
            if "funding-rates" in url:
                return {"funding_rates": [
                    {"symbol": "BTC", "exchange": "binance", "rate": 0.0001},
                    {"symbol": "BTC", "exchange": "lighter", "rate": 0.000056}]}, None
            return None, "error"
        VM._get = fake_get
        r = VM.probe_lighter("BTC")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["interval_min"], 60)
        self.assertAlmostEqual(r["funding_raw_pi"], 0.0056)
        self.assertAlmostEqual(r["oi_usd"], 2000 * 64000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
