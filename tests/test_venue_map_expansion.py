#!/usr/bin/env python3
"""SPEC-176 — venue_map expansion: Gate/MEXC/KuCoin/OKX/HTX (+BingX) + total_vol24h_usd.

All network mocked via VM._get monkeypatch — offline-deterministic. Fixtures are the
live-verified sample responses from `.scratch/oi-construction/research/R1-*.md` /
2026-08-31 curl verification (see venue_map.py docstring for the run notes).
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


VM = _load("venue_map")


class _Patch(unittest.TestCase):
    def setUp(self):
        self._get = VM._get

    def tearDown(self):
        VM._get = self._get


# ---------------------------------------------------------------------------
# DoD — new venues registered, composer sees them, total_vol24h_usd summed.
# ---------------------------------------------------------------------------
class TestNewVenuesRegistered(unittest.TestCase):
    def test_all_five_required_plus_bingx_registered_in_order(self):
        names = list(VM.VENUES.keys())
        for v in ("gate", "mexc", "kucoin", "okx", "htx"):
            self.assertIn(v, VM.VENUES)
        idx = {v: names.index(v) for v in ("gate", "mexc", "kucoin", "okx", "htx")}
        self.assertEqual(sorted(idx, key=idx.get), ["gate", "mexc", "kucoin", "okx", "htx"])


class TestTotalVol24hUsd(unittest.TestCase):
    def test_total_vol24h_usd_sums_ok_venues_reporting_volume(self):
        venues = {
            "binance": lambda t: VM._ok(-0.10, 480, oi_usd=1_000_000, vol24h_usd=2_000_000),
            "gate": lambda t: VM._ok(0.001, 480, oi_usd=500_000),   # no volume reported
            "okx": lambda t: VM._ok(0.005, 480, vol24h_usd=3_000_000),
            "aster": lambda t: {"status": "not_listed"},
        }
        r = VM.build_venue_map("X", venues=venues)
        self.assertEqual(r["total_vol24h_usd"], 5_000_000.0)

    def test_total_vol24h_usd_zero_when_no_venue_reports_volume(self):
        venues = {"binance": lambda t: VM._ok(-0.10, 480, oi_usd=1_000_000)}
        r = VM.build_venue_map("X", venues=venues)
        self.assertEqual(r["total_vol24h_usd"], 0.0)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------
class TestGateAdapter(_Patch):
    def test_ok_shape(self):
        def fake_get(url, timeout=VM.TIMEOUT):
            if "contract_stats" in url:
                return [{"open_interest_usd": 4394819876.75744}], None
            if "contracts/" in url:
                return {"funding_rate": "0.000015", "funding_interval": 28800,
                         "mark_price": "77701.9", "funding_rate_limit": "0.003"}, None
            return None, "error"
        VM._get = fake_get
        r = VM.probe_gate("BTC")
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["funding_raw_pi"], 0.0015)
        self.assertEqual(r["interval_min"], 480)
        self.assertAlmostEqual(r["oi_usd"], 4394819876.75744, places=1)
        self.assertFalse(r["is_floor"])

    def test_not_listed(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: ({"label": "CONTRACT_NOT_FOUND"}, None)
        self.assertEqual(VM.probe_gate("NOPE")["status"], "not_listed")

    def test_fetch_failure_is_error(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (None, "error")
        self.assertEqual(VM.probe_gate("BTC")["status"], "error")

    def test_floor_print_at_cap(self):
        def fake_get(url, timeout=VM.TIMEOUT):
            if "contract_stats" in url:
                return [{"open_interest_usd": 1000.0}], None
            return {"funding_rate": "0.003", "funding_interval": 28800,
                     "mark_price": "1.0", "funding_rate_limit": "0.003"}, None
        VM._get = fake_get
        r = VM.probe_gate("X")
        self.assertTrue(r["is_floor"])


# ---------------------------------------------------------------------------
# MEXC
# ---------------------------------------------------------------------------
class TestMexcAdapter(_Patch):
    def test_ok_shape_converts_holdvol_via_contractsize(self):
        def fake_get(url, timeout=VM.TIMEOUT):
            if "/ticker?" in url:
                return {"success": True, "data": {"fairPrice": 77700.2, "holdVol": 560524761,
                                                    "amount24": 4029288444.5103}}, None
            if "/funding_rate/" in url:
                return {"success": True, "data": {"fundingRate": 0.000077, "collectCycle": 8,
                                                    "maxFundingRate": 0.0018}}, None
            if "/detail" in url:
                return {"success": True, "data": {"contractSize": 0.0001}}, None
            return None, "error"
        VM._get = fake_get
        r = VM.probe_mexc("BTC")
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["funding_raw_pi"], 0.0077)
        self.assertEqual(r["interval_min"], 480)
        self.assertAlmostEqual(r["vol24h_usd"], 4029288444.5103, places=1)
        self.assertAlmostEqual(r["oi_usd"], 560524761 * 0.0001 * 77700.2, places=1)
        self.assertFalse(r["is_floor"])

    def test_not_listed(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (
            {"success": False, "code": 1001, "message": "Contract does not exist"}, None)
        self.assertEqual(VM.probe_mexc("NOPE")["status"], "not_listed")

    def test_fetch_failure_is_error(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (None, "error")
        self.assertEqual(VM.probe_mexc("BTC")["status"], "error")

    def test_missing_contract_detail_degrades_oi_to_none_not_error(self):
        def fake_get(url, timeout=VM.TIMEOUT):
            if "/ticker?" in url:
                return {"success": True, "data": {"fairPrice": 1.0, "holdVol": 100,
                                                    "amount24": 500.0}}, None
            if "/funding_rate/" in url:
                return {"success": True, "data": {"fundingRate": 0.0001, "collectCycle": 8}}, None
            return None, "error"   # detail call fails
        VM._get = fake_get
        r = VM.probe_mexc("X")
        self.assertEqual(r["status"], "ok")
        self.assertNotIn("oi_usd", r)
        self.assertAlmostEqual(r["vol24h_usd"], 500.0)


# ---------------------------------------------------------------------------
# KuCoin
# ---------------------------------------------------------------------------
class TestKucoinAdapter(_Patch):
    def test_ok_shape_converts_oi_via_multiplier(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: ({"code": "200000", "data": {
            "fundingFeeRate": 5.0e-5, "fundingRateGranularity": 28800000,
            "openInterest": "18701781", "multiplier": 0.001, "markPrice": 77700.8,
            "turnoverOf24h": 3.190260454228e8, "fundingRateCap": 0.003,
            "fundingRateFloor": -0.003}}, None)
        r = VM.probe_kucoin("BTC")
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["funding_raw_pi"], 0.005)
        self.assertEqual(r["interval_min"], 480)
        self.assertAlmostEqual(r["oi_usd"], 18701781 * 0.001 * 77700.8, places=1)
        self.assertAlmostEqual(r["vol24h_usd"], 3.190260454228e8, places=1)
        self.assertFalse(r["is_floor"])

    def test_not_listed(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (
            {"code": "404000", "msg": "The contract information you requested does not exist."}, None)
        self.assertEqual(VM.probe_kucoin("NOPE")["status"], "not_listed")

    def test_other_error_code_is_error(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: ({"code": "429000", "msg": "rate limited"}, None)
        self.assertEqual(VM.probe_kucoin("X")["status"], "error")

    def test_fetch_failure_is_error(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (None, "error")
        self.assertEqual(VM.probe_kucoin("BTC")["status"], "error")

    def test_floor_print_at_cap(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: ({"code": "200000", "data": {
            "fundingFeeRate": -0.003, "fundingRateGranularity": 28800000,
            "openInterest": "1", "multiplier": 1.0, "markPrice": 1.0,
            "fundingRateCap": 0.003, "fundingRateFloor": -0.003}}, None)
        r = VM.probe_kucoin("X")
        self.assertTrue(r["is_floor"])


# ---------------------------------------------------------------------------
# OKX
# ---------------------------------------------------------------------------
class TestOkxAdapter(_Patch):
    def test_ok_shape(self):
        def fake_get(url, timeout=VM.TIMEOUT):
            if "funding-rate?" in url:
                return {"code": "0", "data": [{
                    "fundingRate": "0.0000689608952102", "fundingTime": "1788163200000",
                    "prevFundingTime": "1788134400000", "maxFundingRate": "0.00375"}]}, None
            if "market/ticker" in url:
                return {"code": "0", "data": [{"last": "77693.3", "volCcy24h": "55660.0212"}]}, None
            if "open-interest" in url:
                return {"code": "0", "data": [{"oiUsd": "2088939322.4146663333161"}]}, None
            return None, "error"
        VM._get = fake_get
        r = VM.probe_okx("BTC")
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["funding_raw_pi"], 0.00689608952102, places=5)
        self.assertEqual(r["interval_min"], 480)
        self.assertAlmostEqual(r["oi_usd"], 2088939322.4146663333161, places=1)
        self.assertAlmostEqual(r["vol24h_usd"], 55660.0212 * 77693.3, places=1)
        self.assertFalse(r["is_floor"])

    def test_not_listed(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (
            {"code": "51001", "data": [], "msg": "Instrument ID doesn't exist."}, None)
        self.assertEqual(VM.probe_okx("NOPE")["status"], "not_listed")

    def test_fetch_failure_is_error(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (None, "error")
        self.assertEqual(VM.probe_okx("BTC")["status"], "error")


# ---------------------------------------------------------------------------
# HTX
# ---------------------------------------------------------------------------
class TestHtxAdapter(_Patch):
    def test_ok_shape(self):
        def fake_get(url, timeout=VM.TIMEOUT):
            if "swap_funding_rate" in url:
                return {"status": "ok", "data": {"funding_rate": "0.000100000000000000"}}, None
            if "swap_contract_info" in url:
                return {"status": "ok", "data": [{"settlement_period": "8"}]}, None
            if "swap_open_interest" in url:
                return {"status": "ok", "data": [{"value": 2304182898.248,
                                                    "trade_turnover": 338282807.4318}]}, None
            return None, "error"
        VM._get = fake_get
        r = VM.probe_htx("BTC")
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["funding_raw_pi"], 0.01)
        self.assertEqual(r["interval_min"], 480)
        self.assertAlmostEqual(r["oi_usd"], 2304182898.248, places=1)
        self.assertAlmostEqual(r["vol24h_usd"], 338282807.4318, places=1)

    def test_not_listed(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (
            {"status": "error", "err_code": 1332, "err_msg": "The perpetual contract does not exist."}, None)
        self.assertEqual(VM.probe_htx("NOPE")["status"], "not_listed")

    def test_fetch_failure_is_error(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (None, "error")
        self.assertEqual(VM.probe_htx("BTC")["status"], "error")


# ---------------------------------------------------------------------------
# BingX (verify-first bonus venue — endpoints keyless-verified 2026-08-31)
# ---------------------------------------------------------------------------
class TestBingxAdapter(_Patch):
    def test_ok_shape(self):
        def fake_get(url, timeout=VM.TIMEOUT):
            if "premiumIndex" in url:
                return {"code": 0, "data": {"lastFundingRate": "0.00002900",
                                             "fundingIntervalHours": 8,
                                             "maxFundingRate": "0.003000"}}, None
            if "openInterest" in url:
                return {"code": 0, "data": {"openInterest": "624557415.4"}}, None
            if "ticker" in url:
                return {"code": 0, "data": {"quoteVolume": "731471090.68"}}, None
            return None, "error"
        VM._get = fake_get
        r = VM.probe_bingx("BTC")
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["funding_raw_pi"], 0.0029)
        self.assertEqual(r["interval_min"], 480)
        self.assertAlmostEqual(r["oi_usd"], 624557415.4)
        self.assertAlmostEqual(r["vol24h_usd"], 731471090.68)

    def test_not_listed(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (
            {"code": 109425, "msg": "NOPE-USDT not exist", "data": {}}, None)
        self.assertEqual(VM.probe_bingx("NOPE")["status"], "not_listed")

    def test_fetch_failure_is_error(self):
        VM._get = lambda url, timeout=VM.TIMEOUT: (None, "error")
        self.assertEqual(VM.probe_bingx("BTC")["status"], "error")


# ---------------------------------------------------------------------------
# SPEC-178 prep — oi_raw/mark_price additive fields (the sampler's redenomination
# detector needs the raw series untainted by each probe's own USD conversion).
# ---------------------------------------------------------------------------
class TestOiRawMarkPriceFields(unittest.TestCase):
    def test_ok_helper_carries_oi_raw_and_mark_price_when_given(self):
        b = VM._ok(-0.10, 480, oi_usd=1000.0, oi_raw=500.0, mark_price=2.0)
        self.assertEqual(b["oi_raw"], 500.0)
        self.assertEqual(b["mark_price"], 2.0)

    def test_ok_helper_omits_them_when_absent(self):
        b = VM._ok(-0.10, 480, oi_usd=1000.0)
        self.assertNotIn("oi_raw", b)
        self.assertNotIn("mark_price", b)

    def test_binance_probe_carries_oi_raw_and_mark_price(self):
        def fake_get(url, timeout=VM.TIMEOUT):
            if "premiumIndex" in url:
                return {"lastFundingRate": "-0.0020", "markPrice": "10.0"}, None
            if "openInterest" in url:
                return {"openInterest": "1000000"}, None
            return None, "error"
        old_get, old_iv = VM._get, VM.RF.binance_interval_min
        VM._get = fake_get
        VM.RF.binance_interval_min = lambda sym: 480   # stub — else a real fundingInfo fetch
        try:
            r = VM.probe_binance("LAB")
        finally:
            VM._get = old_get
            VM.RF.binance_interval_min = old_iv
        self.assertAlmostEqual(r["oi_raw"], 1_000_000.0)
        self.assertAlmostEqual(r["mark_price"], 10.0)

    def test_kucoin_probe_oi_raw_is_base_asset_units_not_contracts(self):
        old = VM._get
        VM._get = lambda url, timeout=VM.TIMEOUT: ({"code": "200000", "data": {
            "fundingFeeRate": 5.0e-5, "fundingRateGranularity": 28800000,
            "openInterest": "18701781", "multiplier": 0.001, "markPrice": 77700.8,
            "turnoverOf24h": 3.190260454228e8}}, None)
        try:
            r = VM.probe_kucoin("BTC")
        finally:
            VM._get = old
        self.assertAlmostEqual(r["oi_raw"], 18701781 * 0.001)
        self.assertAlmostEqual(r["mark_price"], 77700.8)

    def test_extended_probe_never_fabricates_oi_raw_from_usd_division(self):
        old = VM._get
        VM._get = lambda url, timeout=VM.TIMEOUT: ({"status": "OK", "data": {
            "markPrice": "63800", "fundingRate": "0.000013",
            "openInterest": "62370043.5", "dailyVolume": "140519713.5"}}, None)
        try:
            r = VM.probe_extended("BTC")
        finally:
            VM._get = old
        self.assertNotIn("oi_raw", r)   # openInterest here is already USD, not base-asset
        self.assertAlmostEqual(r["mark_price"], 63800.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
