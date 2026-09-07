#!/usr/bin/env python3
"""SPEC 49 — batch the board's venue reads (21 names should not take 30s).

Run:  python3 tests/test_classify_batch.py

classify '{}' took ~30s for 21 names: per-token serial venue calls. Contract:
  - load_venue_snapshot() fetches bulk tickers/premium-index ONCE per venue and
    per-token reads are served from the snapshot (zero further HTTP);
  - per-token output is IDENTICAL to the serial per-symbol path (no contract change);
  - symbols missing from a bulk response fall back to the per-symbol fetch;
  - classify board runs load the snapshot once; single-ticker runs skip it.
fetch monkeypatched with a URL recorder — offline, deterministic.
"""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RF = _load("regime_flip")

# one healthy symbol on both venues
BYBIT_BULK = {"result": {"list": [
    {"symbol": "AAAUSDT", "fundingRate": "-0.0015", "lastPrice": "2.0",
     "price24hPcnt": "0.05", "turnover24h": "60000000", "openInterest": "1000000"},
]}}
BYBIT_INSTR = {"result": {"list": [
    {"symbol": "AAAUSDT", "fundingInterval": 60},
], "nextPageCursor": ""}}
BN_PI_BULK = [
    {"symbol": "AAAUSDT", "lastFundingRate": "-0.0005", "markPrice": "2.01"},
    {"symbol": "BBBUSDT", "lastFundingRate": "0.0001", "markPrice": "5.0"},
]
BN_T24_BULK = [
    {"symbol": "AAAUSDT", "quoteVolume": "80000000", "priceChangePercent": "4.0"},
    {"symbol": "BBBUSDT", "quoteVolume": "20000000", "priceChangePercent": "1.0"},
]


def bulk_fetch_factory(record):
    """Serves the bulk endpoints + per-symbol OI; records every URL."""
    def fetch(url, timeout=12):
        record.append(url)
        if "bybit.com/v5/market/tickers" in url and "symbol=" not in url:
            return BYBIT_BULK
        if "bybit.com/v5/market/instruments-info" in url and "symbol=" not in url:
            return BYBIT_INSTR
        if url.endswith("/fapi/v1/premiumIndex"):
            return BN_PI_BULK
        if url.endswith("/fapi/v1/ticker/24hr"):
            return BN_T24_BULK
        if "openInterest?symbol=AAAUSDT" in url:
            return {"openInterest": "2000000"}
        if "openInterest?symbol=BBBUSDT" in url:
            return {"openInterest": "500000"}
        if "fundingInfo" in url:
            return []
        # per-symbol endpoints (the serial path / fallback)
        if "bybit.com/v5/market/tickers?category=linear&symbol=AAAUSDT" in url:
            return {"result": {"list": [BYBIT_BULK["result"]["list"][0]]}}
        if "instruments-info?category=linear&symbol=AAAUSDT" in url:
            return {"result": {"list": [{"fundingInterval": 60}]}}
        if "premiumIndex?symbol=AAAUSDT" in url:
            return BN_PI_BULK[0]
        if "ticker/24hr?symbol=AAAUSDT" in url:
            return BN_T24_BULK[0]
        if "premiumIndex?symbol=BBBUSDT" in url:
            return BN_PI_BULK[1]
        if "ticker/24hr?symbol=BBBUSDT" in url:
            return BN_T24_BULK[1]
        return None
    return fetch


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.record = []
        self._fetch = RF.fetch
        RF.fetch = bulk_fetch_factory(self.record)
        RF._BN_FUNDING_INFO = None
        RF.clear_venue_snapshot()

    def tearDown(self):
        RF.fetch = self._fetch
        RF.clear_venue_snapshot()

    def test_identical_output_vs_serial_path(self):
        serial = RF.live_perp("AAA")                  # snapshot off → per-symbol path
        RF.load_venue_snapshot(["AAAUSDT"])
        batched = RF.live_perp("AAA")
        self.assertEqual(serial, batched)

    def test_no_per_symbol_http_after_snapshot(self):
        RF.load_venue_snapshot(["AAAUSDT"])
        self.record.clear()
        def deny(url, timeout=12):
            raise AssertionError(f"per-symbol HTTP after snapshot: {url}")
        RF.fetch = deny
        live = RF.live_perp("AAA")
        self.assertEqual(live["venue"], "bybit")      # more-vetoing real venue
        self.assertAlmostEqual(live["funding_4h"], -0.60, places=2)   # -0.15%/1h → %/4h

    def test_missing_symbol_falls_back_to_per_symbol(self):
        RF.load_venue_snapshot(["AAAUSDT"])
        self.record.clear()
        live = RF.live_perp("BBB")                    # not in bybit bulk; binance-only
        self.assertIsNotNone(live)
        self.assertEqual(live["venue"], "binance")
        # the bybit read fell back to a per-symbol call
        self.assertTrue(any("symbol=BBBUSDT" in u and "bybit" in u for u in self.record))

    def test_snapshot_clear_restores_serial(self):
        RF.load_venue_snapshot(["AAAUSDT"])
        RF.clear_venue_snapshot()
        self.record.clear()
        RF.live_perp("AAA")
        self.assertTrue(any("premiumIndex?symbol=AAAUSDT" in u for u in self.record))


class TestClassifyUsesSnapshot(unittest.TestCase):
    def test_board_run_loads_snapshot_once(self):
        CL = _load("classify")
        calls = []
        CL.load_venue_snapshot = lambda syms: calls.append(list(syms))
        CL.live_perp = lambda tk: None
        CL.load_watchlist = lambda: ([{"ticker": "AAA", "state": "watch"},
                                      {"ticker": "BBB", "state": "watch"},
                                      {"ticker": "CCC", "state": "watch"}], None)
        import contextlib, io, sys as _sys
        argv = _sys.argv
        _sys.argv = ["classify.py", "--json"]
        try:
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                CL.main()
        finally:
            _sys.argv = argv
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ["AAAUSDT", "BBBUSDT", "CCCUSDT"])
        json.loads(buf.getvalue())                    # output stays parseable

    def test_single_ticker_skips_snapshot(self):
        CL = _load("classify")
        calls = []
        CL.load_venue_snapshot = lambda syms: calls.append(list(syms))
        CL.live_perp = lambda tk: None
        CL.load_watchlist = lambda: ([{"ticker": "AAA", "state": "watch"}], None)
        import contextlib, io, sys as _sys
        argv = _sys.argv
        _sys.argv = ["classify.py", "AAA", "--json"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                CL.main()
        finally:
            _sys.argv = argv
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
