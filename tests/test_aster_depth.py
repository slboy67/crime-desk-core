#!/usr/bin/env python3
"""SPEC-85 — wire Aster order-book depth (the execution-venue book read).

Run:  python3 tests/test_aster_depth.py

The desk now FILLS on Aster, so the execution-layer book reads (brief TP-shelf, size
exit-liquidity, SPEC-83 defended_fade) must read the Aster DOM FIRST while keeping
Bitget+Binance as the cross-venue (operator-suspect) compare. Aster's REST is Binance-shaped
(`https://fapi.asterdex.com/fapi/v1/depth`). Network mocked — offline-deterministic.
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


DP = _load("depth")


def _binance_shaped(bids, asks):
    """Aster (and Binance) depth shape: top-level bids/asks arrays of [price, qty] strings."""
    return {"bids": [[str(p), str(q)] for p, q in bids],
            "asks": [[str(p), str(q)] for p, q in asks]}


def _bitget_shaped(bids, asks):
    return {"data": {"bids": [[str(p), str(q)] for p, q in bids],
                     "asks": [[str(p), str(q)] for p, q in asks]}}


class TestAsterFetcher(unittest.TestCase):
    def setUp(self):
        self._f = DP.fetch

    def tearDown(self):
        DP.fetch = self._f

    def test_aster_registered_in_venues(self):
        self.assertIn("aster", DP.VENUES)

    def test_aster_book_parses_binance_shape(self):
        bids = [(0.50, 100), (0.49, 200)]
        asks = [(0.51, 80), (0.52, 150)]
        DP.fetch = lambda url: _binance_shaped(bids, asks)
        b, a, err = DP._aster_book("TNSRUSDT")
        self.assertIsNone(err)
        self.assertEqual(b, [(0.50, 100.0), (0.49, 200.0)])
        self.assertEqual(a, [(0.51, 80.0), (0.52, 150.0)])

    def test_aster_book_hits_asterdex_host(self):
        seen = {}
        def cap(url):
            seen["url"] = url
            return _binance_shaped([(1.0, 1)], [(1.1, 1)])
        DP.fetch = cap
        DP._aster_book("TNSRUSDT")
        self.assertIn("fapi.asterdex.com", seen["url"])
        self.assertIn("symbol=TNSRUSDT", seen["url"])

    def test_aster_book_degrade_on_error(self):
        DP.fetch = lambda url: {"_error": "boom"}
        b, a, err = DP._aster_book("XUSDT")
        self.assertIsNone(b)
        self.assertIsNone(a)
        self.assertIsNotNone(err)


class TestDefaultSetAsterFirst(unittest.TestCase):
    def setUp(self):
        self._f = DP.fetch

    def tearDown(self):
        DP.fetch = self._f

    def _route(self, url):
        # aster + binance are Binance-shaped, bitget is wrapped in data{}
        if "asterdex" in url or "binance" in url:
            return _binance_shaped([(1.0, 100), (0.99, 100)], [(1.01, 100), (1.02, 100)])
        return _bitget_shaped([(1.0, 100), (0.99, 100)], [(1.01, 100), (1.02, 100)])

    def test_default_includes_aster_first(self):
        DP.fetch = self._route
        d = DP.build_depth("TNSR")
        names = list(d["venues"].keys())
        self.assertEqual(names[0], "aster")
        self.assertIn("bitget", names)
        self.assertIn("binance", names)

    def test_venue_aster_returns_aster_only(self):
        DP.fetch = self._route
        d = DP.build_depth("TNSR", venue="aster")
        self.assertEqual(list(d["venues"].keys()), ["aster"])
        self.assertTrue(d["venues"]["aster"]["available"])


class TestAsterDegradeIsolation(unittest.TestCase):
    """Aster non-200/timeout → aster omitted/unavailable, the other venues byte-identical."""
    def setUp(self):
        self._f = DP.fetch

    def tearDown(self):
        DP.fetch = self._f

    def _bg_bn_only(self, url):
        if "asterdex" in url:
            return {"_error": "timeout"}
        if "binance" in url:
            return _binance_shaped([(1.0, 100), (0.99, 100)], [(1.01, 100), (1.02, 100)])
        return _bitget_shaped([(1.0, 100), (0.99, 100)], [(1.01, 100), (1.02, 100)])

    def test_aster_down_others_unchanged(self):
        DP.fetch = self._bg_bn_only
        d = DP.build_depth("TNSR")
        self.assertFalse(d["venues"]["aster"]["available"])
        self.assertTrue(d["venues"]["bitget"]["available"])
        self.assertTrue(d["venues"]["binance"]["available"])

        # byte-identical to a bitget-only (pre-SPEC-85 equivalent) read
        d_bitget = DP.build_depth("TNSR", venue="bitget")
        self.assertEqual(d["venues"]["bitget"], d_bitget["venues"]["bitget"])


class TestAsterTruncation(unittest.TestCase):
    def setUp(self):
        self._f = DP.fetch

    def tearDown(self):
        DP.fetch = self._f

    def test_bunched_aster_book_marks_truncated(self):
        mid = 0.50
        bids = [(round(mid * (1 - i * 0.00005), 7), 100) for i in range(1, 60)]
        asks = [(round(mid * (1 + i * 0.00005), 7), 100) for i in range(1, 60)]
        DP.fetch = lambda url: _binance_shaped(bids, asks)
        v = DP.build_depth("X", venue="aster")["venues"]["aster"]
        self.assertTrue(v["truncated"])
        self.assertIsNotNone(v["truncation_note"])
        self.assertIn("bid", v["deepest_level_seen"])
        self.assertLess(v["coverage_below_pct"], DP.WANT_PCT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
