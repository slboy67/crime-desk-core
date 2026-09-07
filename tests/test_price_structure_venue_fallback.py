#!/usr/bin/env python3
"""SPEC-175 — price_structure: multi-venue kline fallback (Binance -> Bybit -> Aster).

Run:  python3 tests/test_price_structure_venue_fallback.py

Live gap: BTR/HNT/ZKC/TUT are Bybit-listed with no Binance perp (or Binance IP-banned,
418/429/-1003) — price_structure returned "no kline data" and the §6 squeeze-history
pre-check / faded_bounce structure gates couldn't run mechanically. Contract under test:
  - `fetch()` swallows a 418/429 HTTPError (never raises) — the existing behavior a
    Binance ban relies on to fall through instead of dying;
  - Binance succeeds -> exactly one fetch call, venue tagged "binance";
  - Binance fails (banned/empty/too-short) -> falls through to Bybit;
  - Bybit's newest-first kline rows are reversed to oldest-first before use;
  - Bybit also fails -> falls through to Aster;
  - all three fail -> {"error": ...}, never a raise;
  - `meta.kline_venue` names the winning venue on a successful build_structure() call.
Network seams monkeypatched — offline, deterministic.
"""
import importlib.util
import unittest
import unittest.mock
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("price_structure", ROOT / "capabilities" / "price_structure.py")
PS = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PS)

DAY = 86_400_000
T0 = 1_700_000_000_000


def binance_rows(n=40, price=1.0):
    # [openTime, open, high, low, close, volume, closeTime, quoteVolume, ...]
    return [[T0 + i * DAY, str(price), str(price * 1.05), str(price * 0.95),
             str(price), "1000", 0, "500000"] for i in range(n)]


def bybit_payload(n=40, price=2.0):
    # bybit v5 kline: [start, open, high, low, close, volume, turnover] — NEWEST FIRST
    rows = [[str(T0 + i * DAY), str(price), str(price * 1.05), str(price * 0.95),
             str(price), "1000", "500000"] for i in range(n)]
    rows.reverse()
    return {"result": {"list": rows}}


class TestFetchSwallowsBanErrors(unittest.TestCase):
    def test_fetch_returns_none_on_429(self):
        with unittest.mock.patch.object(PS.urllib.request, "urlopen",
                                         side_effect=HTTPError("u", 429, "Too Many Requests", None, None)):
            self.assertIsNone(PS.fetch("https://fapi.binance.com/fapi/v1/klines?symbol=BTRUSDT"))

    def test_fetch_returns_none_on_418(self):
        with unittest.mock.patch.object(PS.urllib.request, "urlopen",
                                         side_effect=HTTPError("u", 418, "I'm a teapot", None, None)):
            self.assertIsNone(PS.fetch("https://fapi.binance.com/fapi/v1/klines?symbol=BTRUSDT"))


class TestFallbackChain(unittest.TestCase):
    def setUp(self):
        self._fetch = PS.fetch

    def tearDown(self):
        PS.fetch = self._fetch

    def test_binance_success_single_call_venue_tagged(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return binance_rows()
        PS.fetch = fetch
        s = PS.build_structure("AAA", days=30)
        self.assertNotIn("error", s)
        self.assertEqual(len(calls), 1)
        self.assertEqual(s["meta"]["kline_venue"], "binance")

    def test_binance_banned_falls_through_to_bybit(self):
        # a 418/429 is already None-by-the-time-it-reaches-here (fetch() swallows it,
        # see TestFetchSwallowsBanErrors) — this is what _binance_klines then sees.
        def fetch(url):
            if "binance.com" in url:
                return None
            if "bybit.com" in url:
                return bybit_payload()
            return None
        PS.fetch = fetch
        s = PS.build_structure("BTR", days=30)
        self.assertNotIn("error", s)
        self.assertEqual(s["meta"]["kline_venue"], "bybit")

    def test_binance_empty_list_falls_through_to_bybit(self):
        # unlisted symbol: Binance 200s an empty/garbage payload rather than erroring
        def fetch(url):
            if "binance.com" in url:
                return []
            if "bybit.com" in url:
                return bybit_payload()
            return None
        PS.fetch = fetch
        s = PS.build_structure("BTR", days=30)
        self.assertEqual(s["meta"]["kline_venue"], "bybit")

    def test_bybit_rows_reversed_to_oldest_first(self):
        # distinct closes per day so ordering is checkable via ATH date
        def fetch(url):
            if "binance.com" in url:
                return None
            if "bybit.com" in url:
                rows = [[str(T0 + i * DAY), "1", str(1 + i), "1", str(1 + i), "10", "10"]
                        for i in range(40)]
                rows.reverse()   # bybit newest-first wire format
                return {"result": {"list": rows}}
            return None
        PS.fetch = fetch
        s = PS.build_structure("BTR", days=30)
        # highest close (day 39, latest) must be the ATH/current close, not day 0
        self.assertEqual(s["current_close"], 40)
        self.assertEqual(s["days_since_ath"], 0)

    def test_binance_and_bybit_fail_falls_through_to_aster(self):
        def fetch(url):
            if "asterdex.com" in url:
                return binance_rows(price=3.0)
            return None
        PS.fetch = fetch
        s = PS.build_structure("CASHCAT", days=30)
        self.assertNotIn("error", s)
        self.assertEqual(s["meta"]["kline_venue"], "aster")

    def test_all_venues_fail_returns_error_never_raises(self):
        PS.fetch = lambda url: None
        s = PS.build_structure("ZZZ", days=30)
        self.assertIn("error", s)

    def test_below_min_bars_does_not_count_as_success(self):
        # Binance returns a too-short history (e.g. a fresh listing on Binance but the
        # real depth is on Bybit) -> must still fall through
        def fetch(url):
            if "binance.com" in url:
                return binance_rows(n=5)
            if "bybit.com" in url:
                return bybit_payload(n=40)
            return None
        PS.fetch = fetch
        s = PS.build_structure("BTR", days=30)
        self.assertEqual(s["meta"]["kline_venue"], "bybit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
