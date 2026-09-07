#!/usr/bin/env python3
"""SPEC 42 — spot-CVD leg of the §4 confluence gate reads the token's REAL spot venue.

Run:  python3 -m unittest tests.test_cvd_venue

The old _oldrepo cvd_spot_perp leg was Binance-centric: Bitget-primary / DEX-primary
names got a structurally blind spot-CVD read that still reported a verdict. Native
capabilities/cvd.py resolves the primary spot venue by 24h volume (Binance → Bitget →
GeckoTerminal DEX via tracked contract) and degrades EXPLICIT (spot_coverage "none" +
verdict "UNAVAILABLE" → analyse treats the §4 leg as UNKNOWN, never a silent fail).

Offline-deterministic: cvd.fetch and analyse's layer fetchers are monkeypatched.
"""
import importlib.util
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, alias=None):
    spec = importlib.util.spec_from_file_location(alias or name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


CVD = _load("cvd", "cvd_cap")
A = _load("analyse", "analyse_cap_spec42")

NOW_MS = int(time.time() * 1000)


def _klines(n, quote_vol, taker_buy_quote):
    """Binance kline rows: idx 0 openTime, 7 quoteAssetVolume, 10 takerBuyQuoteVolume."""
    t0 = NOW_MS - n * 60000
    return [[t0 + i * 60000, "1", "1", "1", "1", "100",
             t0 + (i + 1) * 60000 - 1, str(quote_vol), 50, "0", str(taker_buy_quote), "0"]
            for i in range(n)]


PERP_SELLING = _klines(2, 2_000_000, 500_000)     # perp buy 1M / sell 3M → CVD −2M


class _Routed(unittest.TestCase):
    """Route CVD.fetch by URL substring; record every URL hit."""

    def setUp(self):
        self._fetch = CVD.fetch
        self._resolve = CVD.resolve_contract
        self.urls = []

    def tearDown(self):
        CVD.fetch = self._fetch
        CVD.resolve_contract = self._resolve

    def route(self, table):
        def fake(url, timeout=15):
            self.urls.append(url)
            for frag, resp in table.items():
                if frag in url:
                    return resp
            return None
        CVD.fetch = fake


class TestBitgetOnlyVenue(_Routed):
    """SPEC-191 #1: Bitget is the only CEX venue with data → the aggregate is computed
    from Bitget alone (still named in `cvd_detail.venues_used`), clears $1M at the
    first (30min) ladder rung so the window never has to extend."""

    def test_bitget_only_and_cvd_from_bitget_fills(self):
        self.route({
            "api.bitget.com/api/v2/spot/market/fills": {"data": [
                {"ts": str(NOW_MS), "side": "buy", "price": "1", "size": "3000000"},
                {"ts": str(NOW_MS - 1000), "side": "sell", "price": "1", "size": "1000000"}]},
            "fapi.binance.com/fapi/v1/klines": PERP_SELLING,
        })
        r = CVD.build_cvd("SKYAI", 30)
        self.assertEqual(r["spot_venue"], "bitget")
        self.assertEqual(r["spot_coverage"], "full")
        self.assertEqual(r["cvd_detail"]["venues_used"], ["bitget"])
        self.assertEqual(r["cvd_detail"]["window_min"], 30)
        self.assertEqual(r["verdict"], "BULLISH_DIVERGENCE")   # spot +2M vs perp −2M
        self.assertGreater(r["spot_cvd"], 0)
        self.assertLess(r["perp_cvd"], 0)


class TestBinanceOnlyVenue(_Routed):
    """SPEC-191 #1: Binance is the only CEX venue with data → aggregate off Binance
    alone, no other venue's absence is treated as a failure worth surfacing loudly
    (a venue with genuinely no listing just contributes nothing)."""

    def test_binance_only_and_uses_binance_spot_klines(self):
        self.route({
            "api.binance.com/api/v3/klines": _klines(2, 2_000_000, 1_500_000),   # spot buying
            "fapi.binance.com/fapi/v1/klines": PERP_SELLING,
        })
        r = CVD.build_cvd("X", 30)
        self.assertEqual(r["spot_venue"], "binance")
        self.assertEqual(r["spot_coverage"], "full")
        self.assertEqual(r["cvd_detail"]["venues_used"], ["binance"])
        self.assertEqual(r["verdict"], "BULLISH_DIVERGENCE")


class TestSpec191AggregatedMultiVenue(unittest.TestCase):
    """SPEC-191 #1: aggregated spot across venues + the adaptive window ladder."""

    def setUp(self):
        self._orig_map = dict(CVD._VENUE_SPOT_FN)
        self._orig_perp = CVD.perp_cvd_binance

    def tearDown(self):
        CVD._VENUE_SPOT_FN = self._orig_map
        CVD.perp_cvd_binance = self._orig_perp

    def test_each_venue_alone_thin_but_aggregate_clears_1m_at_60m(self):
        # 30min: each venue reports only $300k/$300k (thin alone) -> aggregate still
        # thin at 30m ($600k); window extends to 60m where each doubles past $600k,
        # clearing the $1M target combined.
        def venue_a(sym, minutes):
            scale = 2 if minutes >= 60 else 1
            return dict(buy=200_000.0 * scale, sell=100_000.0 * scale, cvd=100_000.0 * scale,
                       n=10, window_min=minutes)

        def venue_b(sym, minutes):
            scale = 2 if minutes >= 60 else 1
            return dict(buy=150_000.0 * scale, sell=150_000.0 * scale, cvd=0.0, n=10,
                       window_min=minutes)
        CVD._VENUE_SPOT_FN = {"binance": venue_a, "bybit": venue_b}
        CVD.perp_cvd_binance = lambda sym, minutes: dict(buy=100_000.0, sell=900_000.0,
                                                          cvd=-800_000.0, n=5, window_min=minutes)
        r = CVD.build_cvd("XYZ", 30)
        self.assertEqual(r["cvd_detail"]["window_min"], 60)
        self.assertEqual(r["cvd_detail"]["venues_used"], ["binance", "bybit"])
        self.assertEqual(r["cvd_detail"]["spot_notional_usd"], 1_200_000)
        self.assertTrue(r["reliable"])
        self.assertNotEqual(r["verdict"], "UNRELIABLE_THIN_SPOT")

    def test_one_venue_failing_is_loud_verdict_computed_on_the_rest(self):
        def venue_ok(sym, minutes):
            return dict(buy=800_000.0, sell=400_000.0, cvd=400_000.0, n=10, window_min=minutes)

        def venue_boom(sym, minutes):
            raise RuntimeError("http 500")
        CVD._VENUE_SPOT_FN = {"binance": venue_ok, "bybit": venue_boom}
        CVD.perp_cvd_binance = lambda sym, minutes: dict(buy=100_000.0, sell=1_000_000.0,
                                                          cvd=-900_000.0, n=5, window_min=minutes)
        r = CVD.build_cvd("XYZ", 30)
        self.assertEqual(r["cvd_detail"]["venues_used"], ["binance"])
        self.assertEqual(len(r["venues_failed"]), 1)
        self.assertEqual(r["venues_failed"][0]["venue"], "bybit")
        self.assertIn("http 500", r["venues_failed"][0]["reason"])
        self.assertTrue(r["reliable"])   # verdict computed off binance alone (1.2M clears $1M @ 30m)

    def test_all_venues_thin_even_at_4h_cap_names_the_notional(self):
        def tiny(sym, minutes):
            return dict(buy=1000.0, sell=500.0, cvd=500.0, n=2, window_min=minutes)
        CVD._VENUE_SPOT_FN = {"binance": tiny}
        CVD.perp_cvd_binance = lambda sym, minutes: dict(buy=100_000.0, sell=100_000.0, cvd=0.0,
                                                          n=5, window_min=minutes)
        r = CVD.build_cvd("XYZ", 30)
        self.assertEqual(r["cvd_detail"]["window_min"], 240)
        self.assertEqual(r["verdict"], "UNRELIABLE_THIN_SPOT")
        self.assertIn("1,500", r["note"])   # names the aggregated notional
        self.assertIn("240min", r["note"])

    def test_no_venue_at_all_falls_back_to_single_venue_resolution(self):
        CVD._VENUE_SPOT_FN = {}   # every default venue absent from the aggregate map
        called = {}

        def fake_resolve(sym):
            called["hit"] = True
            return None, None, None
        orig_resolve = CVD.resolve_spot_venue
        CVD.resolve_spot_venue = fake_resolve
        try:
            r = CVD.build_cvd("GHOST", 30)
        finally:
            CVD.resolve_spot_venue = orig_resolve
        self.assertTrue(called.get("hit"))
        self.assertEqual(r["spot_coverage"], "none")
        self.assertEqual(r["verdict"], "UNAVAILABLE")


class TestDexPrimary(_Routed):
    """No CEX spot anywhere but a resolvable contract → GeckoTerminal DEX pool CVD."""

    def test_dex_pool_cvd_with_declared_heuristic(self):
        CVD.resolve_contract = lambda s: ("bsc", "0xabc")
        gt_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.route({
            "api.binance.com/api/v3/ticker/24hr": {"code": -1121, "msg": "Invalid symbol."},
            "api.bitget.com/api/v2/spot/market/tickers": {"data": []},
            "/tokens/0xabc/pools": {"data": [{"id": "bsc_0xpool1",
                                              "attributes": {"volume_usd": {"h24": "3000000"}}}]},
            "/pools/0xpool1/trades": {"data": [
                {"attributes": {"block_timestamp": gt_ts, "kind": "buy", "volume_in_usd": "2500000"}},
                {"attributes": {"block_timestamp": gt_ts, "kind": "sell", "volume_in_usd": "500000"}}]},
            "fapi.binance.com/fapi/v1/klines": PERP_SELLING,
        })
        r = CVD.build_cvd("X", 30)
        self.assertEqual(r["spot_venue"], "dex:bsc")
        self.assertEqual(r["spot_coverage"], "full")
        self.assertEqual(r["verdict"], "BULLISH_DIVERGENCE")
        self.assertIn("taker", r["taker_side_heuristic"].lower())   # DECLARED heuristic


class TestNoSpotAnywhere(_Routed):
    """DoD: no spot read anywhere → spot_coverage 'none' + verdict 'UNAVAILABLE'."""

    def test_coverage_none_verdict_unavailable(self):
        CVD.resolve_contract = lambda s: None
        self.route({
            "api.binance.com/api/v3/ticker/24hr": {"code": -1121, "msg": "Invalid symbol."},
            "api.bitget.com/api/v2/spot/market/tickers": {"data": []},
            "fapi.binance.com/fapi/v1/klines": PERP_SELLING,
        })
        r = CVD.build_cvd("GHOST", 30)
        self.assertIsNone(r["spot_venue"])
        self.assertEqual(r["spot_coverage"], "none")
        self.assertEqual(r["verdict"], "UNAVAILABLE")

    def test_venue_resolved_but_window_read_fails_is_partial(self):
        self.route({
            "api.binance.com/api/v3/ticker/24hr": {"code": -1121, "msg": "Invalid symbol."},
            "api.bitget.com/api/v2/spot/market/tickers": {"data": [{"quoteVolume": "21000000"}]},
            "api.bitget.com/api/v2/spot/market/fills": {"data": []},      # fills read fails
            "fapi.binance.com/fapi/v1/klines": PERP_SELLING,
        })
        r = CVD.build_cvd("X", 30)
        self.assertEqual(r["spot_venue"], "bitget")
        self.assertEqual(r["spot_coverage"], "partial")
        self.assertEqual(r["verdict"], "NO_DATA")


# ---- analyse wiring (house style of tests/test_analyse.py) ----

def make_run_json(perp=None, struct=None, oi=None):
    table = {"perp_analyser.py": perp or {}, "intraday.py": struct or {}, "oi_sides.py": oi or {}}

    def fake(script, ticker, extra=None, timeout=120):
        return table.get(script, {})
    return fake


def perp_long(fr_4h, oi_chg=0):
    return {"ticker": "X", "score": 40, "bias": "LONG", "phase": "trap",
            "metrics": {"fr_4h": fr_4h, "oi_chg": oi_chg, "turnover": 500e6,
                        "turnover_bybit": 300e6, "turnover_binance": 200e6, "price": 1.0},
            "reasons": [], "up_clusters": [], "down_clusters": []}


class TestAnalyseWiring(unittest.TestCase):
    def setUp(self):
        self._orig = (A.run_json, A.run_whales, A.build_nonce_state, A.run_cvd)
        A.run_whales = lambda *a, **k: {"verdict": "NO_DATA"}
        A.build_nonce_state = lambda t, ts=None: {"tracked": True, "signal": "QUIET", "score": 0,
                                                  "ms": 50, "escalation_fired": []}

    def tearDown(self):
        A.run_json, A.run_whales, A.build_nonce_state, A.run_cvd = self._orig

    def test_bitget_cvd_venue_named_in_notes_and_output(self):
        A.run_cvd = lambda t, minutes=30: {"verdict": "BULLISH_DIVERGENCE", "spot_venue": "bitget",
                                           "spot_vol_24h_usd": 21_000_000, "spot_coverage": "full"}
        A.run_json = make_run_json(perp=perp_long(-0.50, oi_chg=30), struct={"near_ath": False})
        a = A.build_analyse("X")
        self.assertEqual(a["verdict"], "LONG")                       # full §4 confluence intact
        self.assertEqual(a["cvd_venue"], "bitget")
        self.assertTrue(any("bitget" in n.lower() for n in a["notes"]))

    def test_binance_primary_parity_unchanged(self):
        A.run_cvd = lambda t, minutes=30: {"verdict": "BULLISH_DIVERGENCE", "spot_venue": "binance",
                                           "spot_vol_24h_usd": 50_000_000, "spot_coverage": "full"}
        A.run_json = make_run_json(perp=perp_long(-0.50, oi_chg=30), struct={"near_ath": False})
        a = A.build_analyse("X")
        self.assertEqual(a["verdict"], "LONG")
        self.assertEqual(a["cvd_venue"], "binance")
        self.assertTrue(any("binance" in n.lower() for n in a["notes"]))

    def test_unavailable_is_unknown_leg_not_false_negative(self):
        """DoD: coverage 'none' → analyse reports cvd_verdict UNAVAILABLE + the leg is
        UNKNOWN (gate still blocks the neg-funding LONG, naming UNAVAILABLE) — and the
        BEARISH hedge-trap veto must NOT fire on it."""
        A.run_cvd = lambda t, minutes=30: {"verdict": "UNAVAILABLE", "spot_venue": None,
                                           "spot_vol_24h_usd": None, "spot_coverage": "none"}
        A.run_json = make_run_json(perp=perp_long(-0.50, oi_chg=30), struct={"near_ath": False})
        a = A.build_analyse("X")
        self.assertEqual(a["cvd_verdict"], "UNAVAILABLE")
        self.assertEqual(a["spot_coverage"], "none")
        self.assertEqual(a["verdict"], "WATCH")                      # can't CONFIRM a §4 long
        self.assertNotIn("hedge-trap", a["direction"])               # not a false negative
        blob = (a.get("reason") or "") + " ".join(a["notes"])
        self.assertIn("UNAVAILABLE", blob)
        self.assertIn("UNKNOWN", blob)

    def test_unavailable_does_not_veto_positive_funding_long(self):
        A.run_cvd = lambda t, minutes=30: {"verdict": "UNAVAILABLE", "spot_venue": None,
                                           "spot_vol_24h_usd": None, "spot_coverage": "none"}
        A.run_json = make_run_json(perp=perp_long(+0.05, oi_chg=10), struct={"near_ath": False})
        a = A.build_analyse("X")
        self.assertEqual(a["verdict"], "LONG")                       # UNKNOWN ≠ bearish veto
        self.assertNotIn("hedge-trap", a["direction"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
