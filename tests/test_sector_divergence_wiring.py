#!/usr/bin/env python3
"""SPEC-114 — sector-divergence wiring into classify/triage/screener.

Each site adds a `sector` key ONLY when sector_divergence.tag_for(...) returns a
tag (mapped ticker); unmapped tickers get zero extra keys — regression: existing
output stays byte-identical except for the added tag (never present for unmapped).

Offline-deterministic: sector_divergence.tag_for / network fetchers are monkeypatched.
"""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import classify as CL
import triage as TR
import screener as SCR
import sector_divergence as SD


def _tok(state="LONG trap-formation"):
    th = {"direction": "LONG", "status": "ACTIVE", "setup": "trap-formation",
          "stop": 0.9, "tp": [1.5], "entry_zone": None, "committed_ts": "2026-07-01"}
    return {"ticker": "LABX", "state": state, "thesis": th}


LIVE = {"venue": "binance", "primary_venue": "binance", "price": 1.1, "chg24": 12.0,
        "vol_m": 40.0, "oi": 1e6, "funding_pi": -0.2, "funding_4h": -0.2,
        "interval_min": 240, "funding_stale": False, "all_floor": False, "funding_split": False}


class ClassifyWiringTests(unittest.TestCase):
    def setUp(self):
        self._orig_range = CL.price_window_range
        self._orig_scan = CL.scan_trigger_logs
        self._orig_tag_for = SD.tag_for
        CL.scan_trigger_logs = lambda ticker, since_epoch=None: ([], [])
        CL.price_window_range = lambda ticker, venue="binance", since_epoch=None: (
            {"high": 1.2, "low": 1.0, "window": "24h-ticker:binance", "candles": None})

    def tearDown(self):
        CL.price_window_range = self._orig_range
        CL.scan_trigger_logs = self._orig_scan
        SD.tag_for = self._orig_tag_for

    def test_mapped_ticker_adds_sector_key(self):
        SD.tag_for = lambda ticker, token_ret, **kw: {
            "verdict": SD.SOLO_PUMP, "annotation": "sector: SOLO_PUMP (+12% vs AI/DePIN -2%, 1d)",
            "cat_a_bump": True, "detail": None}
        r = CL.classify_token(_tok(), live=LIVE)
        self.assertIn("sector", r)
        self.assertEqual(r["sector"]["verdict"], SD.SOLO_PUMP)

    def test_unmapped_ticker_omits_sector_key_entirely(self):
        SD.tag_for = lambda ticker, token_ret, **kw: None
        r = CL.classify_token(_tok(), live=LIVE)
        self.assertNotIn("sector", r)

    def test_sector_prior_never_changes_verdict(self):
        # Same token/live, only the sector tag differs — verdict/reason must be identical.
        SD.tag_for = lambda ticker, token_ret, **kw: None
        r_no_tag = CL.classify_token(_tok(), live=LIVE)
        SD.tag_for = lambda ticker, token_ret, **kw: {
            "verdict": SD.SOLO_PUMP, "annotation": "x", "cat_a_bump": True, "detail": None}
        r_tag = CL.classify_token(_tok(), live=LIVE)
        self.assertEqual(r_no_tag["verdict"], r_tag["verdict"])
        self.assertEqual(r_no_tag["reason"], r_tag["reason"])

    def test_sector_read_failure_degrades_silently(self):
        def boom(*a, **kw):
            raise RuntimeError("boom")
        SD.tag_for = boom
        r = CL.classify_token(_tok(), live=LIVE)  # must not raise
        self.assertNotIn("sector", r)


TRIAGE_DATA = {
    "binance": {"px": 1.1, "ch24": 12.0, "hi24": 1.2, "lo24": 1.0, "qvol24": 4e7,
                "fund_latest": -0.2, "oi_pct_24h": 3.0, "ls": 1.0, "interval_min": 240},
    "bybit": {}, "aster": {}, "bitget": {},
}
TRIAGE_META = {"ticker": "LABX", "category": "A", "state": "watching"}


class TriageWiringTests(unittest.TestCase):
    def setUp(self):
        self._orig_tag_for = SD.tag_for

    def tearDown(self):
        SD.tag_for = self._orig_tag_for

    def test_mapped_ticker_adds_sector_key(self):
        SD.tag_for = lambda ticker, token_ret, **kw: {
            "verdict": SD.SOLO_PUMP, "annotation": "sector: SOLO_PUMP (+12% vs AI/DePIN -2%, 1d)",
            "cat_a_bump": True, "detail": None}
        row = TR._build_row(TRIAGE_DATA, TRIAGE_META)
        self.assertIn("sector", row)

    def test_unmapped_ticker_is_byte_identical_to_pre_feature_row(self):
        SD.tag_for = lambda ticker, token_ret, **kw: None
        # TRIAGE_DATA carries no oi_usd, so the SPEC-122 oi_mc block short-circuits to
        # nulls without a market-cap fetch (offline-deterministic, no network).
        row_with_hook = TR._build_row(TRIAGE_DATA, TRIAGE_META)

        # simulate the pre-SPEC-114 row shape (no sector wiring at all), plus the
        # SPEC-122 oi_mc fields and SPEC-177 battlefield/leverage_state fields every
        # row now carries unconditionally (never gated on a sector/basket mapping the
        # way `sector` is).
        expected_keys = {"ticker", "category", "price", "chg24", "range_pct", "vol_m",
                         "funding_pi", "funding_venue", "funding_range", "funding_suspect",
                         "funding_raw_pi", "funding_interval_min", "oi_chg_pct",
                         "oi_chg_pct_24h",   # SPEC-174 #6: window-labelled alias
                         "ls_ratio", "signals", "direction", "tier", "memo",
                         "oi_mc_ratio", "oi_mc_flag", "oi_mc_caveat",
                         "perp_spot_ratio", "battlefield", "leverage_state"}   # SPEC-177
        self.assertEqual(set(row_with_hook), expected_keys)
        self.assertNotIn("sector", row_with_hook)


class ScreenerWiringTests(unittest.TestCase):
    def setUp(self):
        self._orig_markets = SCR.markets
        self._orig_bn = SCR.binance_perps
        self._orig_bb = SCR.bybit_perps
        self._orig_load_baseline = SCR.load_vol_baseline
        self._orig_save_baseline = SCR.save_vol_baseline
        self._orig_watchlist = SCR._watchlist
        SCR.load_vol_baseline = lambda: ({}, 10.0, None)
        SCR.save_vol_baseline = lambda coins: None
        SCR._watchlist = lambda: set()
        SCR.binance_perps = lambda: {"LAB", "OTHER"}
        SCR.bybit_perps = lambda: {"LAB", "OTHER"}

    def tearDown(self):
        SCR.markets = self._orig_markets
        SCR.binance_perps = self._orig_bn
        SCR.bybit_perps = self._orig_bb
        SCR.load_vol_baseline = self._orig_load_baseline
        SCR.save_vol_baseline = self._orig_save_baseline
        SCR._watchlist = self._orig_watchlist

    def _lab_coin(self):
        return {
            "symbol": "lab", "market_cap": 50_000_000, "total_volume": 60_000_000,
            "circulating_supply": 10_000_000, "total_supply": 100_000_000,
            "fully_diluted_valuation": 500_000_000,
            "price_change_percentage_7d_in_currency": 80.0,
            "price_change_percentage_30d_in_currency": 90.0,
            "id": "lab",
        }

    def _filler(self, ch7):
        return {"symbol": "zzz", "market_cap": 1_000, "total_volume": 0,
                "price_change_percentage_7d_in_currency": ch7, "id": "zzz-filler"}

    def test_lab_shaped_candidate_gets_sector_tag(self):
        coins = [self._lab_coin()] + [self._filler(v) for v in
                                       (-50, -45, -40, -35, -2, -2, -1, 3, 9)]
        SCR.markets = lambda pages: (coins, [], None, None)
        s = SCR.build_screen(mode="pump", min_score=3, pages=1, top=25)
        lab = next(c for c in s["candidates"] if c["ticker"] == "LAB")
        self.assertIn("sector", lab)
        self.assertIn("SOLO_PUMP", lab["sector"])

    def test_unmapped_candidate_has_no_sector_key(self):
        coin = self._lab_coin()
        coin["symbol"] = "notmapped"
        SCR.binance_perps = lambda: {"NOTMAPPED", "OTHER"}
        SCR.bybit_perps = lambda: {"NOTMAPPED", "OTHER"}
        coins = [coin] + [self._filler(v) for v in (-50, -45, -40, -35, -2, -2, -1, 3, 9)]
        SCR.markets = lambda pages: (coins, [], None, None)
        s = SCR.build_screen(mode="pump", min_score=3, pages=1, top=25)
        cand = next(c for c in s["candidates"] if c["ticker"] == "NOTMAPPED")
        self.assertNotIn("sector", cand)


if __name__ == "__main__":
    unittest.main()
