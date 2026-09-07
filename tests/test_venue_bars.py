#!/usr/bin/env python3
"""SPEC-188 — venue_bars.py: all-venue 1h OHLC sweep + tape-agreement.

Run:  python3 tests/test_venue_bars.py

All network mocked via VB._get monkeypatch (adapter-level, real per-venue parsing
exercised against recorded-shape fixtures) or via build_venue_bars(venues=...) stub
injection (composer-level) — offline-deterministic, matching the venue_map.py pattern.
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


VB = _load("venue_bars")

TICKER = "OP"
BASE = 1788343200          # multiple of 3600, matches a real live-verified sweep (2026-09-02)
TS0, TS1, TS2 = BASE, BASE + 3600, BASE + 7200
NOW = TS2 + 1800            # midway through the TS2 bar -> TS2 is the live bar

# Common OHLC per bar timestamp, shared by every venue's fixture (dispersion tests
# override one venue's high explicitly).
BARS = {
    TS0: (0.0947, 0.0948, 0.0934, 0.0944),
    TS1: (0.0944, 0.0954, 0.0932, 0.0949),
    TS2: (0.0949, 0.0974, 0.0947, 0.0968),
}


def _binance_klines(bars=BARS):
    return [[ts * 1000, o, h, l, c, "0", ts * 1000 + 3599999, str(1000 + ts % 100), 1, "0", "0", "0"]
            for ts, (o, h, l, c) in sorted(bars.items())]


def _bybit_list(bars=BARS):
    # bybit returns DESCENDING (newest first)
    return [[str(ts * 1000), str(o), str(h), str(l), str(c), "1000", str(2000 + ts % 100)]
            for ts, (o, h, l, c) in sorted(bars.items(), reverse=True)]


def _okx_data(bars=BARS):
    return [[str(ts * 1000), str(o), str(h), str(l), str(c), "1000", "1000", str(3000 + ts % 100), "1"]
            for ts, (o, h, l, c) in sorted(bars.items(), reverse=True)]


def _bitget_data(bars=BARS):
    return [[str(ts * 1000), str(o), str(h), str(l), str(c), "1000", str(4000 + ts % 100)]
            for ts, (o, h, l, c) in sorted(bars.items())]


def _kucoin_data(bars=BARS):
    return [[ts * 1000, o, h, l, c, 1000, 5000 + ts % 100] for ts, (o, h, l, c) in sorted(bars.items())]


def _gate_list(bars=BARS):
    return [{"t": ts, "o": str(o), "h": str(h), "l": str(l), "c": str(c), "v": 1000, "sum": str(6000 + ts % 100)}
            for ts, (o, h, l, c) in sorted(bars.items())]


def _mexc_data(bars=BARS):
    items = sorted(bars.items())
    return {"time": [t for t, _ in items], "open": [v[0] for _, v in items],
            "high": [v[1] for _, v in items], "low": [v[2] for _, v in items],
            "close": [v[3] for _, v in items], "amount": [7000 + t % 100 for t, _ in items]}


def _htx_data(bars=BARS):
    return [{"id": ts, "open": o, "high": h, "low": l, "close": c, "trade_turnover": 8000 + ts % 100}
            for ts, (o, h, l, c) in sorted(bars.items(), reverse=True)]


def _bingx_data(bars=BARS):
    return [{"open": str(o), "high": str(h), "low": str(l), "close": str(c), "volume": "1000", "time": ts * 1000}
            for ts, (o, h, l, c) in sorted(bars.items(), reverse=True)]


def _hl_candles(bars=BARS):
    return [{"t": ts * 1000, "T": ts * 1000 + 3599999, "s": TICKER, "i": "1h", "o": o, "c": c, "h": h, "l": l, "v": "1000", "n": 1}
            for ts, (o, h, l, c) in sorted(bars.items())]


def _kraken_candles(bars=BARS):
    return [{"time": ts * 1000, "open": str(o), "high": str(h), "low": str(l), "close": str(c), "volume": "1000"}
            for ts, (o, h, l, c) in sorted(bars.items())]


def _blofin_data(bars=BARS):
    return [[str(ts * 1000), str(o), str(h), str(l), str(c), "1000", "1000", str(9000 + ts % 100), "1"]
            for ts, (o, h, l, c) in sorted(bars.items(), reverse=True)]


def _coinbase_candles(bars=BARS):
    return [{"start": str(ts), "low": str(l), "high": str(h), "open": str(o), "close": str(c), "volume": "1000"}
            for ts, (o, h, l, c) in sorted(bars.items(), reverse=True)]


def make_fake_get(bars=BARS, http500=None):
    """Dispatch by URL substring to the per-venue fixture builders above. `http500`
    (a venue-URL substring) makes that ONE venue's primary call fail with a 500,
    everything else unaffected — the degrade DoD."""
    def fake_get(url, data=None, hdr=None, timeout=VB.TIMEOUT):
        if http500 and http500 in url:
            return None, 500
        if "fapi.binance.com/fapi/v1/klines" in url:
            return _binance_klines(bars), None
        if "fapi.binance.com/fapi/v1/ticker/24hr" in url:
            return {"quoteVolume": "58800000"}, None
        if "api.bybit.com/v5/market/kline" in url:
            return {"retCode": 0, "result": {"list": _bybit_list(bars)}}, None
        if "api.bybit.com/v5/market/tickers" in url:
            return {"retCode": 0, "result": {"list": [{"turnover24h": "23800000"}]}}, None
        if "okx.com/api/v5/market/candles" in url:
            return {"code": "0", "data": _okx_data(bars)}, None
        if "okx.com/api/v5/market/ticker" in url:
            return {"code": "0", "data": [{"last": "0.0968", "volCcy24h": "100000000"}]}, None
        if "bitget.com/api/v2/mix/market/candles" in url:
            return {"code": "00000", "data": _bitget_data(bars)}, None
        if "bitget.com/api/v2/mix/market/ticker" in url:
            return {"code": "00000", "data": [{"usdtVolume": "12000000"}]}, None
        if "api-futures.kucoin.com/api/v1/kline/query" in url:
            return {"code": "200000", "data": _kucoin_data(bars)}, None
        if "api-futures.kucoin.com/api/v1/contracts" in url:
            return {"code": "200000", "data": {"turnoverOf24h": "9000000"}}, None
        if "gateio.ws" in url:
            return _gate_list(bars), None
        if "contract.mexc.com/api/v1/contract/kline" in url:
            return {"success": True, "code": 0, "data": _mexc_data(bars)}, None
        if "contract.mexc.com/api/v1/contract/ticker" in url:
            return {"success": True, "data": {"amount24": "5000000"}}, None
        if "api.hbdm.com/linear-swap-ex/market/history/kline" in url:
            return {"status": "ok", "data": _htx_data(bars)}, None
        if "api.hbdm.com/linear-swap-api/v1/swap_open_interest" in url:
            return {"status": "ok", "data": [{"trade_turnover": "4000000"}]}, None
        if "open-api.bingx.com/openApi/swap/v3/quote/klines" in url:
            return {"code": 0, "data": _bingx_data(bars)}, None
        if "open-api.bingx.com/openApi/swap/v2/quote/ticker" in url:
            return {"code": 0, "data": {"quoteVolume": "3000000"}}, None
        if "api.hyperliquid.xyz/info" in url:
            return _hl_candles(bars), None
        if "fapi.asterdex.com/fapi/v1/klines" in url:
            return _binance_klines(bars), None
        if "fapi.asterdex.com/fapi/v1/ticker/24hr" in url:
            return {"quoteVolume": "1000000"}, None
        if "futures.kraken.com/api/charts/v1/trade" in url:
            return {"candles": _kraken_candles(bars)}, None
        if "openapi.blofin.com/api/v1/market/candles" in url:
            return {"code": "0", "data": _blofin_data(bars)}, None
        if "api.coinbase.com/api/v3/brokerage/market/products" in url:
            return {"candles": _coinbase_candles(bars)}, None
        return None, "error"
    return fake_get


class TestFourteenVenueSweep(unittest.TestCase):
    """DoD #1: all 14 venues parse to the same bar timestamps, h>=c>=l (well-formed
    against a shared low<=open/close<=high fixture)."""

    def setUp(self):
        self._get = VB._get
        VB._get = make_fake_get()

    def tearDown(self):
        VB._get = self._get

    def test_all_venues_parse_same_timestamps_and_sane_ohlc(self):
        try:
            import hyperliquid as HL
            self._hl_resolve = HL.resolve
            HL.resolve = lambda t: None   # keep the turnover leg deterministic/offline
        except Exception:
            self._hl_resolve = None
        try:
            r = VB.build_venue_bars(TICKER, "1h", n=2, now=NOW)
        finally:
            if self._hl_resolve:
                import hyperliquid as HL
                HL.resolve = self._hl_resolve

        self.assertEqual(r["n_total"], 14)
        self.assertEqual(r["n_available"], 14, r["venues"])
        expected_ts = {TS0, TS1, TS2}
        for name, v in r["venues"].items():
            self.assertTrue(v["available"], f"{name}: {v}")
            got_ts = {b["ts"] for b in v["bars"]}
            self.assertEqual(got_ts, expected_ts, f"{name} bar timestamps mismatch: {got_ts}")
            for b in v["bars"]:
                self.assertGreaterEqual(b["h"], b["c"], f"{name}@{b['ts']}: high < close")
                self.assertGreaterEqual(b["c"], b["l"], f"{name}@{b['ts']}: close < low")
                self.assertGreaterEqual(b["h"], b["o"], f"{name}@{b['ts']}: high < open")
                self.assertGreaterEqual(b["o"], b["l"], f"{name}@{b['ts']}: open < low")
        # the live bar (TS2, per NOW) is flagged live on every venue
        for name, v in r["venues"].items():
            live_bar = next(b for b in v["bars"] if b["ts"] == TS2)
            self.assertTrue(live_bar["live"], name)
            self.assertFalse(next(b for b in v["bars"] if b["ts"] == TS0)["live"], name)


class TestDispersion(unittest.TestCase):
    """DoD #2: one venue's high 3% above the rest -> high_spread_pct correct, that
    venue named as max_h_venue."""

    def test_high_spread_names_the_outlier_venue(self):
        hot_bars = dict(BARS)
        o, h, l, c = hot_bars[TS2]
        hot_bars[TS2] = (o, round(h * 1.03, 6), l, c)   # aster wicks 3% above the pack
        venues = {
            "binance": lambda t, i, n: VB._ok([VB._bar(ts, *v) for ts, v in sorted(BARS.items())]),
            "bybit": lambda t, i, n: VB._ok([VB._bar(ts, *v) for ts, v in sorted(BARS.items())]),
            "aster": lambda t, i, n: VB._ok([VB._bar(ts, *v) for ts, v in sorted(hot_bars.items())]),
        }
        r = VB.build_venue_bars(TICKER, "1h", n=2, venues=venues, now=NOW)
        bar2 = next(b for b in r["bars"] if b["ts"] == TS2)
        self.assertEqual(bar2["max_h_venue"], "aster")
        self.assertGreater(bar2["high_spread_pct"], 2.5)
        self.assertLess(bar2["high_spread_pct"], 3.5)
        self.assertIn(bar2["min_h_venue"], ("binance", "bybit"))


class TestLevelAgreement(unittest.TestCase):
    """DoD #3: a level sitting between venues' highs on the live bar splits crossed
    vs not, execution_venue_crossed reflects Aster specifically."""

    def test_level_between_highs_splits_crossed_and_execution_venue(self):
        # TS2 highs: binance/bybit 0.0974, aster 0.0968 (per BARS) — level 0.0970 is
        # crossed by binance/bybit (high >= 0.0970) but NOT by aster.
        venues = {
            "binance": lambda t, i, n: VB._ok([VB._bar(ts, *v) for ts, v in sorted(BARS.items())]),
            "bybit": lambda t, i, n: VB._ok([VB._bar(ts, *v) for ts, v in sorted(BARS.items())]),
            "aster": lambda t, i, n: VB._ok([VB._bar(ts, o, min(h, 0.0968), l, min(c, 0.0968))
                                             for ts, (o, h, l, c) in sorted(BARS.items())]),
        }
        r = VB.build_venue_bars(TICKER, "1h", n=2, venues=venues, now=NOW)
        agr = VB.level_agreement(r, 0.0970, "above", bar_ts=TS2)
        self.assertEqual(agr["n_total"], 3)
        self.assertIn("binance", agr["crossed"])
        self.assertIn("bybit", agr["crossed"])
        self.assertNotIn("aster", agr["crossed"])
        self.assertEqual(agr["n_crossed"], 2)
        self.assertFalse(agr["execution_venue_crossed"])   # aster == execution venue, didn't cross

    def test_execution_venue_crossed_true_when_aster_crosses(self):
        venues = {
            "binance": lambda t, i, n: VB._ok([VB._bar(ts, *v) for ts, v in sorted(BARS.items())]),
            "aster": lambda t, i, n: VB._ok([VB._bar(ts, *v) for ts, v in sorted(BARS.items())]),
        }
        r = VB.build_venue_bars(TICKER, "1h", n=2, venues=venues, now=NOW)
        agr = VB.level_agreement(r, 0.0970, "above", bar_ts=TS2)
        self.assertTrue(agr["execution_venue_crossed"])
        self.assertEqual(agr["closed_beyond"], [])   # close 0.0968 < 0.0970 on both


class TestDegrade(unittest.TestCase):
    """DoD #4: one venue 500s -> available:false + reason, the rest unaffected,
    dispersion computed over survivors with n_total reduced and printed."""

    def setUp(self):
        self._get = VB._get
        VB._get = make_fake_get(http500="bitget.com/api/v2/mix/market/candles")

    def tearDown(self):
        VB._get = self._get

    def test_one_venue_500_isolated_rest_unaffected(self):
        try:
            import hyperliquid as HL
            self._hl_resolve = HL.resolve
            HL.resolve = lambda t: None
        except Exception:
            self._hl_resolve = None
        try:
            r = VB.build_venue_bars(TICKER, "1h", n=2, now=NOW)
        finally:
            if self._hl_resolve:
                import hyperliquid as HL
                HL.resolve = self._hl_resolve
        self.assertFalse(r["venues"]["bitget"]["available"])
        self.assertIn("reason", r["venues"]["bitget"])
        self.assertEqual(r["n_total"], 14)
        self.assertEqual(r["n_available"], 13)
        # dispersion still computed over the 13 survivors
        for b in r["bars"]:
            self.assertEqual(b["n_venues"], 13)
            self.assertNotIn("bitget", (b["max_h_venue"], b["min_h_venue"]))


class TestRenderTapeLine(unittest.TestCase):
    def test_tape_line_prints_live_marker_and_dispersion(self):
        venues = {
            "binance": lambda t, i, n: VB._ok([VB._bar(ts, *v) for ts, v in sorted(BARS.items())]),
            "aster": lambda t, i, n: VB._ok([VB._bar(ts, *v) for ts, v in sorted(BARS.items())]),
        }
        r = VB.build_venue_bars(TICKER, "1h", n=2, venues=venues, now=NOW)
        line = VB.render_tape_line(r)
        self.assertIn("tape (1h, 2 venues)", line)
        self.assertIn("*", line)   # the live bar marker


if __name__ == "__main__":
    unittest.main()
