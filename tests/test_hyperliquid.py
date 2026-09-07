#!/usr/bin/env python3
"""SPEC-84 — Hyperliquid read-only venue (funding / OI / depth / mark / oracle).

Run:  python3 tests/test_hyperliquid.py

HL is a TARGETED add for the Aster/HL overlap names (empirically TNSR + CHIP of the 26
watchlist); absence is the graceful default for the other 24 — HL is simply OMITTED, never
an error, and the other four venues' reads stay byte-identical. HL funding settles HOURLY
(/4h = ×4, interval_min:60) and HL reports a REAL rate every hour — no 0.005% placeholder
sentinel — so the CEX floor detector must NOT misfire on HL's native small per-hour scale.
All network mocked — offline-deterministic.
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


HL = _load("hyperliquid")


def _ctx(funding, oi, mark, oracle, premium, vol):
    return {"funding": str(funding), "openInterest": str(oi), "markPx": str(mark),
            "oraclePx": str(oracle), "premium": str(premium), "dayNtlVlm": str(vol)}


# metaAndAssetCtxs fixture: TNSR + CHIP listed (the overlap), BTC for company. Parallel arrays.
META = {"universe": [{"name": "TNSR"}, {"name": "CHIP"}, {"name": "BTC"}]}
CTXS = [
    _ctx(-0.00125, 1_000_000, 0.50, 0.4995, -0.0008, 12_500_000),   # TNSR: −0.125%/hr → −0.5%/4h
    _ctx(0.0000125, 4_000_000, 0.020, 0.020, 0.0, 3_000_000),       # CHIP: tiny REAL rate (not floor)
    _ctx(0.00001, 100, 65000, 65010, 0.0001, 9e8),                  # BTC
]
META_CTXS = [META, CTXS]


class TestResolveOverlap(unittest.TestCase):
    def test_tnsr_resolves_with_hourly_normalized_to_4h(self):
        v = HL.resolve("TNSR", meta_ctxs=META_CTXS)
        self.assertIsNotNone(v)
        self.assertEqual(v["venue"], "hyperliquid")
        self.assertEqual(v["interval_min"], 60)              # HOURLY
        self.assertAlmostEqual(v["funding_pi"], -0.125, places=6)   # raw ×100 = per-hour %
        self.assertAlmostEqual(v["funding_4h"], -0.5, places=6)     # ×4
        self.assertFalse(v["is_floor"])

    def test_oi_vol_mark_oracle_premium_populated(self):
        v = HL.resolve("TNSR", meta_ctxs=META_CTXS)
        self.assertEqual(v["mark"], 0.50)
        self.assertEqual(v["oracle"], 0.4995)
        self.assertAlmostEqual(v["premium"], -0.0008)
        # OI surfaced in USD (base × mark) so it lines up with the other venues' USD OI
        self.assertAlmostEqual(v["oi"], 1_000_000 * 0.50, places=2)
        self.assertAlmostEqual(v["oi_base"], 1_000_000.0)
        self.assertAlmostEqual(v["vol_m"], 12.5, places=4)   # dayNtlVlm / 1e6

    def test_chip_resolves(self):
        v = HL.resolve("CHIP", meta_ctxs=META_CTXS)
        self.assertIsNotNone(v)
        self.assertEqual(v["coin"], "CHIP")
        self.assertEqual(v["mark"], 0.020)


class TestGracefulAbsence(unittest.TestCase):
    def test_unlisted_name_returns_none(self):
        # BSB is not in the HL universe → omitted (None), never an exception
        self.assertIsNone(HL.resolve("BSB", meta_ctxs=META_CTXS))

    def test_usdt_suffix_stripped(self):
        self.assertIsNotNone(HL.resolve("TNSRUSDT", meta_ctxs=META_CTXS))


class TestDegrade(unittest.TestCase):
    def setUp(self):
        self._post = HL.post

    def tearDown(self):
        HL.post = self._post

    def test_post_failure_degrades_to_none(self):
        # timeout / non-200 / parse-fail → post returns None → resolve None, no crash, no block
        HL.post = lambda payload: None
        self.assertIsNone(HL.resolve("TNSR"))

    def test_malformed_payload_degrades(self):
        self.assertIsNone(HL.resolve("TNSR", meta_ctxs={"not": "a 2-list"}))
        self.assertIsNone(HL.resolve("TNSR", meta_ctxs=[{"universe": []}]))      # wrong length
        self.assertIsNone(HL.resolve("TNSR", meta_ctxs=["bad", "shape"]))


class TestFloorGuard(unittest.TestCase):
    """HL has NO 0.005% placeholder sentinel — only an exact-zero hourly rate is 'no funding'.
    The CEX FLOOR_RAW=0.00005 must NOT misfire on HL's native per-hour scale."""

    def test_exact_zero_is_floor(self):
        self.assertTrue(HL.is_floor(0.0))
        self.assertTrue(HL.is_floor(None))

    def test_small_real_hourly_rate_is_not_floored(self):
        self.assertFalse(HL.is_floor(0.0000125))     # CHIP's real tiny rate
        self.assertFalse(HL.is_floor(-0.00125))      # TNSR

    def test_cex_sentinel_value_is_real_on_hl(self):
        # 0.00005/hr would be the CEX FLOOR sentinel, but on HL it is a REAL ±0.02%/4h rate
        self.assertFalse(HL.is_floor(0.00005))


class TestUniverseAndAlias(unittest.TestCase):
    def test_universe_set(self):
        self.assertEqual(HL.universe(META), {"TNSR", "CHIP", "BTC"})

    def test_universe_malformed_is_empty_set(self):
        self.assertEqual(HL.universe({"bad": 1}), set())

    def test_coin_for_identity_and_alias(self):
        self.assertEqual(HL.coin_for("TNSR"), "TNSR")
        self.assertEqual(HL.coin_for("tnsrusdt"), "TNSR")


class TestL2Book(unittest.TestCase):
    def test_parses_levels(self):
        raw = {"levels": [[{"px": "0.50", "sz": "1000"}, {"px": "0.49", "sz": "2000"}],
                          [{"px": "0.51", "sz": "1500"}, {"px": "0.52", "sz": "900"}]]}
        bids, asks = HL.l2_book("TNSR", raw=raw)
        self.assertEqual(bids[0], (0.50, 1000.0))
        self.assertEqual(asks[0], (0.51, 1500.0))

    def test_empty_book_degrades(self):
        bids, asks = HL.l2_book("X", raw={"levels": [[], []]})
        self.assertIsNone(bids)
        self.assertIsNone(asks)

    def test_malformed_book_degrades(self):
        # SPEC-123: raw=None is l2_book's "fetch live" sentinel, not a malformed-input stand-in
        # — passing it here silently made a real HL POST every run. {} is genuinely malformed
        # (no "levels" key) and exercises the same degrade path offline.
        bids, asks = HL.l2_book("X", raw={})
        self.assertIsNone(bids)
        self.assertIsNone(asks)


DP = _load("depth")
BR = _load("brief")


# Canned HL block as resolve() would return it for an overlap name (TNSR).
HL_TNSR = {
    "venue": "hyperliquid", "coin": "TNSR", "funding_raw": -0.00125, "funding_pi": -0.125,
    "interval_min": 60, "funding_4h": -0.5, "is_floor": False,
    "mark": 0.50, "oracle": 0.4995, "premium": -0.0008, "price": 0.50,
    "oi": 500_000.0, "oi_base": 1_000_000.0, "vol_m": 12.5,
}


class TestDepthBooksVenues(unittest.TestCase):
    """SPEC-84 req 3/DoD: HL in books.venues for overlap names; OMITTED (not unavailable) for
    the 24/26 it doesn't carry; the existing CEX reads stay byte-identical."""

    def setUp(self):
        self._fb = DP._fetch_hl_book
        self._fetch = DP.fetch

    def tearDown(self):
        DP._fetch_hl_book = self._fb
        DP.fetch = self._fetch

    def test_hl_book_surfaced_for_overlap_name(self):
        DP._fetch_hl_book = lambda t: ([(0.50, 1000), (0.49, 2000)], [(0.51, 1500), (0.52, 900)])
        v = DP.build_depth("TNSR", venue="hyperliquid")["venues"]["hyperliquid"]
        self.assertTrue(v["available"])
        self.assertEqual(v["venue"], "hyperliquid")
        self.assertIsNotNone(v["bid_shelf_below"])

    def test_absent_name_omits_hl_entirely(self):
        # BSB not on HL → l2 book empty → HL OMITTED, never present-as-unavailable
        DP._fetch_hl_book = lambda t: (None, None)
        DP.fetch = lambda url: {"data": {"bids": [["0.5", "1"]], "asks": [["0.51", "1"]]}}
        v = DP.build_depth("BSB")["venues"]
        self.assertNotIn("hyperliquid", v)

    def test_cex_venue_request_never_triggers_hl(self):
        # a --venue bitget request must not touch HL at all (byte-identical CEX path)
        called = []
        DP._fetch_hl_book = lambda t: called.append(t) or (None, None)
        DP.fetch = lambda url: {"data": {"bids": [["0.5", "1"]], "asks": [["0.51", "1"]]}}
        d = DP.build_depth("X", venue="bitget")
        self.assertNotIn("hyperliquid", d["venues"])
        self.assertEqual(called, [])

    def test_hl_error_degrades_to_omission(self):
        def boom(t):
            raise RuntimeError("HL down")
        DP._fetch_hl_book = boom
        DP.fetch = lambda url: {"data": {"bids": [["0.5", "1"]], "asks": [["0.51", "1"]]}}
        d = DP.build_depth("TNSR")          # CEX still resolves, HL just omitted
        self.assertNotIn("hyperliquid", d["venues"])


class TestBriefFundingByVenue(unittest.TestCase):
    """SPEC-84 req 1/5/DoD: HL surfaced in perp.funding_by_venue (funding/OI/vol/mark/oracle) for
    overlap names; omitted on absence; the other venues unchanged."""

    def setUp(self):
        self._saved = {k: getattr(BR, k) for k in
                       ("load_watchlist", "classify_token", "live_perp", "build_analyse",
                        "build_depth", "build_onchain", "_surface_bitget", "fetch_aster_symbols")}
        self._resolve = BR.HL.resolve
        BR.load_watchlist = lambda: ([], None)        # discovery name (no thesis) — keeps it simple
        BR.classify_token = lambda tok, live=None: {"verdict": None, "direction": None}
        BR.live_perp = lambda t: {
            "venue": "binance", "funding_4h": -0.5, "price": 0.50,
            "venues": {"binance": {"funding_4h": -0.5, "funding_pi": -0.125,
                                   "interval_min": 240, "is_floor": False, "oi": 9e5, "vol_m": 18.0}}}
        BR.build_analyse = lambda t, days=90: {"ticker": t, "verdict": "WATCH", "funding_4h": -0.5,
                                               "price": 0.50, "notes": []}
        BR.build_depth = lambda t, v=None: {"ticker": t, "venues": {}}
        BR.build_onchain = lambda t, depth="fast": {"ticker": t, "signal": "QUIET",
                                                    "nonces": {"tracked": False}}
        BR._surface_bitget = lambda t: None
        BR.fetch_aster_symbols = lambda: None   # SPEC-136: unknown — no live network in tests

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(BR, k, v)
        BR.HL.resolve = self._resolve

    def test_hl_surfaced_with_funding_oi_mark_oracle(self):
        BR.HL.resolve = lambda t: HL_TNSR
        fbv = BR.build_brief("TNSR")["perp"]["funding_by_venue"]
        self.assertIn("hyperliquid", fbv)
        self.assertIn("binance", fbv)                  # the existing venue still there
        hl = fbv["hyperliquid"]
        self.assertEqual(hl["funding_4h"], -0.5)
        self.assertEqual(hl["interval_min"], 60)
        self.assertFalse(hl["is_floor"])
        self.assertEqual(hl["oi"], 500_000.0)
        self.assertEqual(hl["vol_m"], 12.5)
        self.assertEqual(hl["mark"], 0.50)
        self.assertEqual(hl["oracle"], 0.4995)
        self.assertEqual(hl["premium"], -0.0008)

    def test_absent_name_omits_hl(self):
        BR.HL.resolve = lambda t: None
        fbv = BR.build_brief("BSB")["perp"]["funding_by_venue"]
        self.assertNotIn("hyperliquid", fbv)
        self.assertIn("binance", fbv)                  # byte-identical: the CEX venue still present

    def test_hl_fetch_error_never_blocks_brief(self):
        def boom(t):
            raise RuntimeError("HL down")
        BR.HL.resolve = boom
        b = BR.build_brief("TNSR")
        self.assertTrue(b["perp"]["available"])
        self.assertNotIn("hyperliquid", b["perp"]["funding_by_venue"])


SC = _load("scan")


class TestScanUniverse(unittest.TestCase):
    """SPEC-84 req 1: HL-native perps available to scan's universe (offline; injected ctxs).
    Same row shape as the Bybit path; HOURLY funding → funding_4h ×4; absence-safe."""

    def _meta_ctxs(self):
        return [{"universe": [{"name": "HYPE"}, {"name": "QUIETHL"}]},
                [_ctx(-0.0015, 2_000_000, 30.0, 29.0, -0.001, 50_000_000),    # HYPE: −0.6%/4h, $50M
                 _ctx(0.0002, 1000, 1.0, 1.0, 0.0, 1_000_000)]]              # QUIETHL: $1M < gate

    def test_hl_candidates_shape_and_gate(self):
        cands = SC.hl_candidates(meta_ctxs=self._meta_ctxs(), min_vol=10.0)
        self.assertEqual(len(cands), 1)               # QUIETHL gated out on $1M < $10M
        c = cands[0]
        self.assertEqual(c["ticker"], "HYPE")
        self.assertEqual(c["interval_h"], 1)
        self.assertAlmostEqual(c["funding_4h"], -0.6, places=4)   # −0.15%/hr × 4
        self.assertEqual(c["venue"], "hyperliquid")
        self.assertEqual(c["turnover_m"], 50.0)

    def test_malformed_or_empty_is_empty_list(self):
        self.assertEqual(SC.hl_candidates(meta_ctxs=None.__class__ and {"bad": 1}), [])
        self.assertEqual(SC.hl_candidates(meta_ctxs=["bad"]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
