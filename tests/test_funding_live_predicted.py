#!/usr/bin/env python3
"""SPEC 18 — funding LATEST must be the LIVE predicted rate, not the last SETTLED print.

Run:  python3 tests/test_funding_live_predicted.py

Fully offline: monkeypatch each module's `fetch` with a URL router that serves a
settled history head of +0.005% (flat) while the LIVE predicted endpoints
(bybit /v5/market/tickers, binance|aster /fapi/v1/premiumIndex) serve deep-neg.
The settled print hid a §5-vetoed short (the EDEN case); these prove the live
value wins, that cross-venue divergence is computed on the LIVE latests, that a
fetch failure degrades to the settled head, and that a genuinely-flat 0.005
(live == settled) is left unchanged.

SPEC-123: triage.venue_pull also calls regime_flip.binance_interval_min, which used its
OWN module-level `fetch` (capabilities/regime_flip.py) — un-mocked, this was a real live
Binance call on every TestTriageLiveFunding run (silently succeeding, so it never showed
up as a failure; SPEC-123's network sentinel is what surfaced it). TR.RF is the same
regime_flip module object triage.py imported, so it's patched alongside TR.fetch.
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


TR = _load("triage")
RC = _load("regime_check")

FLAT = "0.00005"        # +0.005% settled (the flat sentinel that genuinely-flat names sit at)
DEEPNEG = "-0.0172"     # −1.72% LIVE predicted (deep-neg, fires the §5 short-veto)


# ---------- triage ----------
def triage_router(live_by=DEEPNEG, live_bn=DEEPNEG, by_tickers_ok=True):
    def fetch(url, timeout=8):
        if "fapi.binance.com/fapi/v1/fundingRate" in url:
            return [{"fundingRate": FLAT} for _ in range(7)]          # settled history (series)
        if "fapi.binance.com/fapi/v1/premiumIndex" in url:
            return {"lastFundingRate": live_bn}                       # LIVE
        if "fapi.binance.com/fapi/v1/ticker/24hr" in url:
            return {"lastPrice": "1.0", "priceChangePercent": "-3.0",
                    "highPrice": "1.2", "lowPrice": "0.8", "quoteVolume": "50000000"}
        if "openInterestHist" in url:
            return [{"sumOpenInterest": "100"}, {"sumOpenInterest": "110"}]
        if "topLongShortAccountRatio" in url:
            return [{"longShortRatio": "1.0"}]
        if "api.bybit.com/v5/market/tickers" in url:
            if not by_tickers_ok:
                return None                                          # live fetch fails → degrade
            return {"retCode": 0, "result": {"list": [{"fundingRate": live_by}]}}  # LIVE
        if "api.bybit.com/v5/market/funding/history" in url:
            return {"retCode": 0, "result": {"list": [{"fundingRate": FLAT}]}}     # settled
        if "api.bybit.com/v5/market/open-interest" in url:
            return {"retCode": 0, "result": {"list": [{"openInterest": "110"}, {"openInterest": "100"}]}}
        if "asterdex" in url:
            return {"lastFundingRate": live_bn}
        return None
    return fetch


class TestTriageLiveFunding(unittest.TestCase):
    def tearDown(self):
        if hasattr(self, "_orig"):
            TR.fetch, TR.RF.fetch = self._orig
            TR.RF._BN_FUNDING_INFO = self._orig_bn_cache

    def patch(self, **kw):
        self._orig = (TR.fetch, TR.RF.fetch)
        self._orig_bn_cache = TR.RF._BN_FUNDING_INFO
        router = triage_router(**kw)
        TR.fetch = router
        TR.RF.fetch = router          # regime_flip.binance_interval_min uses its own fetch
        TR.RF._BN_FUNDING_INFO = None  # drop any cached interval from a prior test's router

    def test_bybit_latest_is_live_not_settled(self):
        self.patch()
        out = TR.venue_pull("EDEN")
        self.assertAlmostEqual(out["bybit"]["fund_latest"], -1.72, places=3)    # live, not +0.005
        self.assertAlmostEqual(out["binance"]["fund_latest"], -1.72, places=3)

    def test_funding_pi_reflects_live_deepneg(self):
        self.patch()
        row = TR._build_row(TR.venue_pull("EDEN"), {"ticker": "EDEN", "category": "A", "state": ""})
        self.assertAlmostEqual(row["funding_pi"], -1.72, places=3)              # board funding = live
        self.assertIn("deep-neg-fund", row["signals"])                          # §5 veto signal surfaces

    def test_genuinely_flat_unchanged(self):
        self.patch(live_by=FLAT, live_bn=FLAT)                                  # live == settled == 0.005
        out = TR.venue_pull("KITE")
        self.assertAlmostEqual(out["bybit"]["fund_latest"], 0.005, places=4)
        self.assertAlmostEqual(out["binance"]["fund_latest"], 0.005, places=4)

    def test_bybit_live_fetch_fails_degrades_to_settled(self):
        self.patch(by_tickers_ok=False)
        out = TR.venue_pull("EDEN")
        self.assertAlmostEqual(out["bybit"]["fund_latest"], 0.005, places=4)    # settled fallback, no crash


# ---------- regime_check ----------
def regime_router(live_by=DEEPNEG, live_bn=DEEPNEG, by_tickers_ok=True):
    def fetch(url, timeout=8):
        if "fapi.binance.com/fapi/v1/fundingRate" in url:
            return [{"fundingRate": FLAT} for _ in range(120)]        # settled series (z sample)
        if "fapi.binance.com/fapi/v1/premiumIndex" in url:
            return {"lastFundingRate": live_bn}
        if "api.bybit.com/v5/market/tickers" in url:
            if not by_tickers_ok:
                return None
            return {"retCode": 0, "result": {"list": [{"fundingRate": live_by}]}}
        if "api.bybit.com/v5/market/funding/history" in url:
            return {"retCode": 0, "result": {"list": [{"fundingRate": FLAT} for _ in range(120)]}}
        if "asterdex.com/fapi/v1/fundingRate" in url:
            return [{"fundingRate": FLAT} for _ in range(120)]
        if "asterdex.com/fapi/v1/premiumIndex" in url:
            return {"lastFundingRate": live_bn}
        return None                                                  # OI endpoints → handled gracefully
    return fetch


class TestRegimeCheckLiveFunding(unittest.TestCase):
    def tearDown(self):
        if hasattr(self, "_orig"):
            RC.fetch = self._orig

    def patch(self, **kw):
        self._orig = RC.fetch
        RC.fetch = regime_router(**kw)

    def test_latest_is_live_per_venue(self):
        self.patch()
        r = RC.build_regime("EDEN")
        self.assertAlmostEqual(r["funding"]["bybit"]["latest"], -1.72, places=3)
        self.assertAlmostEqual(r["funding"]["binance"]["latest"], -1.72, places=3)
        self.assertIn("NEG", r["funding"]["bybit"]["regime"].upper())          # deep-neg surfaces

    def test_divergence_uses_live_latests(self):
        # settled is flat on BOTH (delta 0 → no divergence); live diverges → divergence must reflect live
        self.patch(live_bn=DEEPNEG, live_by="-0.0010")                          # bn −1.72%, by −0.10%
        r = RC.build_regime("EDEN")
        self.assertIsNotNone(r["divergence"])
        self.assertAlmostEqual(r["divergence"]["binance"], -1.72, places=3)
        self.assertAlmostEqual(r["divergence"]["bybit"], -0.10, places=3)

    def test_genuinely_flat_no_false_divergence(self):
        self.patch(live_by=FLAT, live_bn=FLAT)
        r = RC.build_regime("KITE")
        self.assertAlmostEqual(r["funding"]["bybit"]["latest"], 0.005, places=4)
        self.assertIsNone(r["divergence"])                                     # flat on all → no divergence

    def test_live_fetch_fails_degrades_to_settled(self):
        self.patch(by_tickers_ok=False)
        r = RC.build_regime("EDEN")
        self.assertAlmostEqual(r["funding"]["bybit"]["latest"], 0.005, places=4)  # settled head fallback


if __name__ == "__main__":
    unittest.main(verbosity=2)
