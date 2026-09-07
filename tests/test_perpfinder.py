#!/usr/bin/env python3
"""SPEC-151 — perpfinder: keyless multi-venue breadth API, second-source only.

Offline-deterministic: every HTTP call is monkeypatched (P._http_get); no network. The
cache is redirected into a TemporaryDirectory per test so runs never touch the real
state/perpfinder_cache.json.

Run:  python3 -m unittest tests.test_perpfinder -v
"""
import importlib.util
import sys
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

spec = importlib.util.spec_from_file_location("perpfinder_cap", ROOT / "capabilities" / "perpfinder.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


def _http_error(code, headers=None):
    hdrs = Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError("https://perpfinder.com/api/data/x", code, "err", hdrs, None)


# Live-shape fixture (captured 2026-08-25, SPEC-158 — the schema drifted from SPEC-151's
# `symbols`/`asset`/`venues` to `rows`/`symbol`/`exchanges`; fieldSupport moved from
# per-symbol-venue to top-level meta.fieldSupport keyed by venue, oi/price only, never
# rate1h). Trimmed to 5 symbols incl. one multi-venue row (CASHCAT, 3 of its live 9 venues).
FUNDING_FIXTURE = {
    "schemaVersion": 1, "dataStatus": "live", "updatedAt": "2026-08-24T23:56:38.284Z",
    "coverage": {"requested": 27, "succeeded": ["Bybit", "MEXC", "Hyperliquid"],
                "failed": [], "retired": []},
    "meta": {
        "fieldSupport": {
            "Bybit": {"oi": "observed", "price": "observed"},
            "MEXC": {"oi": "unsupported", "price": "observed"},
            "Hyperliquid": {"oi": "observed", "price": "observed"},
        },
        "nullSemantics": "null = not carried by the venue feed; 0 = true numeric zero",
    },
    "rows": [
        {"symbol": "CASHCAT", "exchanges": {
            "Bybit": {"rate1h": 0.00055334, "oi": 14104626.97, "price": 0.20059},
            "MEXC": {"rate1h": 0.00027575, "oi": None, "price": 0.2012},
            "Hyperliquid": {"rate1h": 0.0004901732, "oi": 30385822.02, "price": 0.20085},
        }, "maxRate": 0.00055334, "minRate": 0.00027575, "exchangeCount": 3},
        {"symbol": "HEMI", "exchanges": {
            "Bybit": {"rate1h": -0.001, "oi": 500000.0, "price": 1.2},
        }, "exchangeCount": 1},
        {"symbol": "SKYAI", "exchanges": {
            "Bybit": {"rate1h": 0.0005, "oi": 100000.0, "price": 0.05},
        }, "exchangeCount": 1},
        {"symbol": "BTC", "exchanges": {
            "Bybit": {"rate1h": 4.15875e-06, "oi": 3755065036.1, "price": 78848.9},
        }, "exchangeCount": 1},
        {"symbol": "ETH", "exchanges": {
            "Bybit": {"rate1h": 1.2e-05, "oi": 900000000.0, "price": 2481.5},
        }, "exchangeCount": 1},
    ],
}

OI_FIXTURE = {
    "schemaVersion": 1, "dataStatus": "live", "updatedAt": "2026-08-20T00:00:00Z",
    "totalOI": 123456789.0,
    "byExchange": [
        {"name": "binance", "oi": 50000000.0},
        {"name": "aster", "oi": None},
    ],
}

SLIPPAGE_FIXTURE = {
    "schemaVersion": 1, "dataStatus": "live", "updatedAt": "2026-08-20T00:00:00Z",
    "rows": [
        {"venue": "binance", "totalBps": 12.3, "vwap": 60000.1, "spread": 1.2},
        {"venue": "okx", "totalBps": 5.1, "vwap": 60000.0, "spread": 0.8},
        {"venue": "bybit", "totalBps": 8.4, "vwap": 60000.2, "spread": 1.0},
    ],
}


class PerpfinderBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self._orig_http = P._http_get
        self._orig_cache = P.CACHE_PATH
        self._orig_state = P.STATE
        P.STATE = Path(self.tmp.name)
        P.CACHE_PATH = Path(self.tmp.name) / "perpfinder_cache.json"
        self.calls = []
        self.responses = []  # list of dicts or Exception instances, consumed in order

        def fake_http(url, timeout=15):
            self.calls.append(url)
            resp = self.responses[len(self.calls) - 1] if len(self.calls) <= len(self.responses) else self.responses[-1]
            if isinstance(resp, Exception):
                raise resp
            return resp

        P._http_get = fake_http

    def tearDown(self):
        P._http_get = self._orig_http
        P.CACHE_PATH = self._orig_cache
        P.STATE = self._orig_state
        self.tmp.cleanup()


class TestFundingMode(PerpfinderBase):
    def test_4h_conversion_and_normalized_no_raw_pi(self):
        self.responses = [FUNDING_FIXTURE]
        r = P.build_perpfinder("funding")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["rows"]), 7)   # 3 CASHCAT venues + 1 each HEMI/SKYAI/BTC/ETH
        for row in r["rows"]:
            self.assertTrue(row["normalized"])
            self.assertNotIn("raw_pi", row)   # SPEC-187: renamed rate_raw_pi, never "raw_pi"
        bybit_hemi = next(x for x in r["rows"] if x["asset"] == "HEMI" and x["venue"] == "Bybit")
        # SPEC-187: funding_pi_4h now goes through the fraction->percent step (x100) before
        # the x4 interval scale — was `-0.001 * 4` (missing x100), 100x too small.
        self.assertAlmostEqual(bybit_hemi["funding_pi_4h"], -0.001 * 100 * 4)
        self.assertEqual(bybit_hemi["rate_raw_pi"], -0.001)
        self.assertEqual(bybit_hemi["interval_min"], 60)
        self.assertEqual(bybit_hemi["oi"], 500000.0)
        self.assertEqual(bybit_hemi["price"], 1.2)

    def test_cashcat_multivenue_row_yields_per_venue_rate1h_oi_price(self):
        """DoD req 3: the CASHCAT-style `exchanges` map must yield per-venue
        rate1h(->funding_pi_4h)/oi/price for every venue, including a null OI staying null."""
        self.responses = [FUNDING_FIXTURE]
        r = P.build_perpfinder("funding", ticker="CASHCAT")
        self.assertTrue(r["ok"])
        self.assertEqual({row["venue"] for row in r["rows"]}, {"Bybit", "MEXC", "Hyperliquid"})
        bybit = next(x for x in r["rows"] if x["venue"] == "Bybit")
        # RF.to_4h rounds to 4 places (percent-scale convention) — SPEC-112, same as
        # every other funding_4h field in the codebase.
        self.assertAlmostEqual(bybit["funding_pi_4h"], round(0.00055334 * 100 * 4, 4))
        self.assertEqual(bybit["rate_raw_pi"], 0.00055334)
        self.assertEqual(bybit["oi"], 14104626.97)
        self.assertEqual(bybit["price"], 0.20059)
        mexc = next(x for x in r["rows"] if x["venue"] == "MEXC")
        self.assertAlmostEqual(mexc["funding_pi_4h"], round(0.00027575 * 100 * 4, 4))
        self.assertIsNone(mexc["oi"])                    # null stays null, never 0
        self.assertEqual(mexc["field_support"], {"oi": "unsupported", "price": "observed"})

    def test_reconciles_with_regime_flip_to_4h_same_transform(self):
        """SPEC-187 — venue_breadth's funding_pi_4h must be the SAME number
        regime_flip.to_4h would produce for the same raw fraction/interval, not a
        parallel bespoke transform that can silently drift out of the SPEC-112
        convention again."""
        import regime_flip as RF
        self.responses = [FUNDING_FIXTURE]
        r = P.build_perpfinder("funding", ticker="SKYAI")
        row = r["rows"][0]
        expected = RF.to_4h(round(row["rate_raw_pi"] * 100, 6), row["interval_min"])
        self.assertAlmostEqual(row["funding_pi_4h"], expected)

    def test_ticker_filter_returns_only_that_asset(self):
        self.responses = [FUNDING_FIXTURE]
        r = P.build_perpfinder("funding", ticker="SKYAI")
        self.assertTrue(r["ok"])
        self.assertEqual({row["asset"] for row in r["rows"]}, {"SKYAI"})

    def test_unknown_ticker_returns_empty_rows_ok_true(self):
        """A ticker that legitimately matches nothing in a non-trivial universe is NOT
        shape drift — raw_count (pre-filter) stays nonzero so the guard never fires here."""
        self.responses = [FUNDING_FIXTURE]
        r = P.build_perpfinder("funding", ticker="NOPETOKEN")
        self.assertTrue(r["ok"])
        self.assertEqual(r["rows"], [])

    def test_attribution_present(self):
        self.responses = [FUNDING_FIXTURE]
        r = P.build_perpfinder("funding")
        self.assertEqual(r["meta"]["attribution"], P.ATTRIBUTION)


class TestErrorHandling(PerpfinderBase):
    def test_429_with_retry_after_retries_once_then_succeeds(self):
        self.responses = [_http_error(429, {"Retry-After": "0"}), FUNDING_FIXTURE]
        r = P.build_perpfinder("funding", now=1000.0)
        self.assertTrue(r["ok"])
        self.assertEqual(len(self.calls), 2)

    def test_429_persists_past_retry_is_loud_error(self):
        self.responses = [_http_error(429, {"Retry-After": "0"}), _http_error(429, {"Retry-After": "0"})]
        r = P.build_perpfinder("funding", now=1000.0)
        self.assertFalse(r["ok"])
        self.assertIn("429", r["reason"])
        self.assertEqual(len(self.calls), 2)   # exactly one retry, never a loop

    def test_500_is_ok_false_no_partial_render(self):
        self.responses = [_http_error(500)]
        r = P.build_perpfinder("funding", now=1000.0)
        self.assertFalse(r["ok"])
        self.assertNotIn("rows", r)
        self.assertEqual(len(self.calls), 1)   # 500 is not retried, only 429 is


class TestNullPreservation(PerpfinderBase):
    def test_null_oi_stays_null_never_zero(self):
        self.responses = [OI_FIXTURE]
        r = P.build_perpfinder("oi")
        self.assertTrue(r["ok"])
        aster_row = next(x for x in r["byExchange"] if x["name"] == "aster")
        self.assertIsNone(aster_row["oi"])
        binance_row = next(x for x in r["byExchange"] if x["name"] == "binance")
        self.assertEqual(binance_row["oi"], 50000000.0)


class TestCache(PerpfinderBase):
    def test_second_call_within_ttl_serves_from_cache(self):
        self.responses = [FUNDING_FIXTURE]
        r1 = P.build_perpfinder("funding", now=1000.0)
        r2 = P.build_perpfinder("funding", now=1000.0 + 10)   # well within 120s TTL
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertEqual(len(self.calls), 1)   # no second fetch

    def test_expired_ttl_refetches(self):
        self.responses = [FUNDING_FIXTURE, FUNDING_FIXTURE]
        P.build_perpfinder("funding", now=1000.0)
        P.build_perpfinder("funding", now=1000.0 + 121)   # past the 120s TTL
        self.assertEqual(len(self.calls), 2)


class TestSchemaDrift(PerpfinderBase):
    def test_schema_version_mismatch_renders_with_warning(self):
        drifted = dict(FUNDING_FIXTURE, schemaVersion=2)
        self.responses = [drifted]
        r = P.build_perpfinder("funding")
        self.assertTrue(r["ok"])
        self.assertIn("rows", r)
        self.assertIn("warning", r["meta"])
        self.assertIn("schemaVersion", r["meta"]["warning"])


class TestModeDispatch(PerpfinderBase):
    def test_unknown_mode_is_ok_false(self):
        r = P.build_perpfinder("nonexistent")
        self.assertFalse(r["ok"])
        self.assertEqual(self.calls, [])   # never touches the wire for a bad mode

    def test_cli_omitted_mode_defaults_to_funding(self):
        """SPEC-174 #2: `perpfinder.py --json` (no positional mode) used to argparse-error
        (`mode` was a bare required positional) — now defaults to `funding`, the flagship
        2205x27 matrix."""
        import contextlib
        import io
        import json as _json
        self.responses = [FUNDING_FIXTURE]
        orig_argv = sys.argv
        sys.argv = ["perpfinder.py", "--json"]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                P.main()
        finally:
            sys.argv = orig_argv
        out = _json.loads(buf.getvalue())
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["mode"], "funding")


class TestSlippage(PerpfinderBase):
    def test_ladder_sorted_by_total_bps(self):
        self.responses = [SLIPPAGE_FIXTURE]
        r = P.build_perpfinder("slippage", ticker="BTC", size=1000, side="buy")
        self.assertTrue(r["ok"])
        self.assertEqual([row["venue"] for row in r["rows"]], ["okx", "bybit", "binance"])
        self.assertEqual(r["meta"]["attribution"], P.ATTRIBUTION)

    def test_missing_required_args_is_loud_fail(self):
        r = P.build_perpfinder("slippage")
        self.assertFalse(r["ok"])
        self.assertEqual(self.calls, [])


class TestOiLongShort(PerpfinderBase):
    def test_empty_protocols_renders_ok_true_with_empty_reason(self):
        self.responses = [{"schemaVersion": 1, "dataStatus": "live",
                           "protocols": [], "totalLong": 0, "totalShort": 0}]
        r = P.build_perpfinder("oi_long_short")
        self.assertTrue(r["ok"])
        self.assertEqual(r["rows"], [])
        self.assertIn("empty_reason", r)

    def test_populated_protocols_parses_live_shape(self):
        """SPEC-158: live-verified 2026-08-25 the endpoint is now populated under
        `protocols` (not `rows`, contra the SPEC-151 doc) — parse it, don't render empty."""
        self.responses = [{"schemaVersion": 1, "dataStatus": "live",
                           "totalLong": 205696628, "totalShort": 103796609,
                           "protocols": [
                               {"slug": "gmtrade", "name": "GMTrade", "longOI": 101900019,
                                "shortOI": 103796609, "totalOI": 205696628, "longPct": 49.5},
                           ]}]
        r = P.build_perpfinder("oi_long_short")
        self.assertTrue(r["ok"])
        self.assertNotIn("empty_reason", r)
        self.assertEqual(len(r["rows"]), 1)
        self.assertEqual(r["rows"][0]["name"], "GMTrade")
        self.assertEqual(r["rows"][0]["long_oi"], 101900019)
        self.assertEqual(r["rows"][0]["short_oi"], 103796609)

    PROTOCOLS_FIXTURE = {
        "schemaVersion": 1, "dataStatus": "live",
        "totalLong": 205696628, "totalShort": 103796609,
        "protocols": [
            {"slug": "gmtrade", "name": "GMTrade", "longOI": 101900019,
             "shortOI": 103796609, "totalOI": 205696628, "longPct": 49.5},
            {"slug": "gmx", "name": "GMX", "longOI": 50000000,
             "shortOI": 40000000, "totalOI": 90000000, "longPct": 55.6},
        ],
    }

    def test_ticker_filters_to_the_matching_protocol_by_slug_or_name(self):
        """SPEC-174 #2: before this fix `ticker` was silently discarded — every call got the
        full unfiltered DEX-protocol table regardless of what was asked for."""
        self.responses = [self.PROTOCOLS_FIXTURE]
        r = P.build_perpfinder("oi_long_short", ticker="gmx")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["rows"]), 1)
        self.assertEqual(r["rows"][0]["slug"], "gmx")
        self.assertNotIn("not_listed", r)

    def test_ticker_with_no_matching_protocol_is_not_listed_never_the_full_table(self):
        self.responses = [self.PROTOCOLS_FIXTURE]
        r = P.build_perpfinder("oi_long_short", ticker="HEMI")
        self.assertTrue(r["ok"])                 # a legitimate "not listed" result, not a failure
        self.assertEqual(r["rows"], [])
        self.assertTrue(r.get("not_listed"))
        self.assertIn("empty_reason", r)

    def test_unfiltered_call_still_returns_the_full_table(self):
        self.responses = [self.PROTOCOLS_FIXTURE]
        r = P.build_perpfinder("oi_long_short")
        self.assertEqual(len(r["rows"]), 2)


class TestShapeDrift(PerpfinderBase):
    def test_renamed_top_level_key_on_substantial_body_is_loud_shape_drift(self):
        """The SPEC-158 regression itself: a real ~KB+ body whose top-level container got
        renamed out from under the parser must fail loud, never render rows:[] silently."""
        drifted = dict(FUNDING_FIXTURE)
        drifted["symbols"] = drifted.pop("rows")          # simulate the old/wrong key name
        drifted["padding"] = "x" * 1200                    # push body past the byte floor
        self.responses = [drifted]
        r = P.build_perpfinder("funding")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "shape_drift")
        self.assertEqual(r["shape_drift"]["body_type"], "dict")
        self.assertIn("symbols", r["shape_drift"]["first_keys"])

    def test_small_legitimately_empty_body_is_not_shape_drift(self):
        self.responses = [{"schemaVersion": 1, "dataStatus": "live",
                           "protocols": [], "totalLong": 0, "totalShort": 0}]
        r = P.build_perpfinder("oi_long_short")
        self.assertTrue(r["ok"])
        self.assertNotIn("shape_drift", r)


class TestVolumeAndLiqsAttribution(PerpfinderBase):
    def test_volume_venue_level_shape_and_attribution(self):
        self.responses = [{"schemaVersion": 1, "dataStatus": "live",
                           "exchanges": [{"name": "binance", "volume24h": 1e9,
                                         "symbolCount": 400, "topSymbols": ["BTC", "ETH"]}]}]
        r = P.build_perpfinder("volume")
        self.assertTrue(r["ok"])
        self.assertEqual(r["exchanges"][0]["name"], "binance")
        self.assertEqual(r["meta"]["attribution"], P.ATTRIBUTION)

    def test_liqs_event_stream_and_attribution(self):
        self.responses = [{"schemaVersion": 1, "dataStatus": "live",
                           "events": [{"exchange": "okx", "symbol": "HEMI", "side": "long",
                                      "sizeUsd": 5000, "price": 1.2, "timestamp": 123}]}]
        r = P.build_perpfinder("liqs", ticker="HEMI")
        self.assertTrue(r["ok"])
        self.assertEqual(r["events"][0]["exchange"], "okx")
        self.assertEqual(r["meta"]["attribution"], P.ATTRIBUTION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
