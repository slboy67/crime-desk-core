#!/usr/bin/env python3
"""SPEC 60 — workup: the full §0.6 five-question scan as ONE call.

All offline: the underlying section reads are monkeypatched to fixtures, so the test
exercises the ASSEMBLY (5 sections + flags, per-section degrade, no verdict, meta.ms),
never the network.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import workup  # noqa: E402


# ── fixtures for each underlying read ───────────────────────────────────────────
def _onchain_fx(ticker):
    return {"ticker": ticker, "tracked": True, "signal": "QUIET", "bias": "neutral",
            "score": 1,
            "concentration": {"available": True, "top1_pct": 41.0, "top10_pct": 78.0,
                              "holder_count": 1200},
            "coverage": {"unsupported_chains": [{"chain": "sol", "supply_pct": 12.0}],
                         "unreadable_supply_pct": 12.0},
            "flags": {"is_contract": False, "locked": False, "burn": False},
            "nonces": {"escalation_fired": []}}


def _structure_fx(sym, days=90, squeeze_pct=15.0):
    return {"ticker": sym, "current_close": 0.10, "window_high": 0.13, "window_low": 0.08,
            "range_pos": 40.0, "off_ath_pct": -23.0, "ath_alltime": 0.47,
            "ath_alltime_date": "2025-02-01", "off_ath_alltime_pct": -78.0,
            "prior_cycle": True, "squeezes": [{"day": "2026-06-01", "pct": 22.0}],
            "squeeze_pattern": "diminishing",
            "structure": {"read": "downtrend", "lower_highs": 5}}


def _live_fx(ticker):
    return {"venue": "bitget", "primary_venue": "bitget", "funding_4h": -0.42,
            "funding_pi": -0.07, "all_floor": False, "price": 0.10, "chg24": 8.0,
            "oi": 1_000_000.0, "vol_m": 18.0,
            "venues": {"bitget": {"funding_4h": -0.42, "oi": 1_000_000, "vol_m": 18.0},
                       "binance": {"funding_4h": -0.10, "oi": 600_000, "vol_m": 12.0}}}


def _cvd_fx(ticker, minutes=30):
    return {"ticker": ticker, "spot_venue": "binance", "spot_vol_24h_usd": 5_000_000,
            "spot_coverage": "full", "verdict": "BULLISH_DIVERGENCE", "reliable": True}


def _depth_fx(ticker, venue=None):
    return {"ticker": ticker, "venues": {
        "binance": {"available": True, "mid": 0.100,
                    "bid_shelf_below": {"price": 0.095, "spoof_prone": False, "round_number": None},
                    "ask_wall_above": {"price": 0.108, "spoof_prone": True, "round_number": None},
                    "truncated": False},
        "bitget": {"available": True, "mid": 0.1005,
                   "bid_shelf_below": {"price": 0.096, "spoof_prone": False, "round_number": 0.10},
                   "ask_wall_above": None, "truncated": False}}}


def _magnets_fx(sym, days=14, interval="15m", buckets=100):
    return {"ticker": sym, "hvns": [{"price": 0.105, "pct": 30}],
            "lvns": [{"price": 0.09}], "round_number_magnets": [0.10]}


def _alerts_fx(ticker):
    return {"n": 1, "max_severity": "P2", "events": [{"kind": "nonce", "severity": "P2"}]}


class _OiSides:
    def __init__(self, tag):
        self.tag = tag

    def __call__(self, ticker):
        return {"ticker": ticker, "verdict_tag": self.tag}


class TestWorkup(unittest.TestCase):
    def setUp(self):
        workup.build_onchain = _onchain_fx
        workup.build_structure = _structure_fx
        workup.live_perp = _live_fx
        workup.build_cvd = _cvd_fx
        workup.build_depth = _depth_fx
        workup.build_magnets = _magnets_fx
        workup.oi_sides_read = _OiSides("REAL_DIRECTIONAL")
        workup.alerts_for = _alerts_fx

    def test_all_five_sections_present(self):
        d = workup.build_workup("ESPORTS")
        for section in ("chips", "stage", "oi_construction", "size", "rr", "flags"):
            self.assertIn(section, d, f"missing section {section}")

    def test_no_verdict_field_anywhere(self):
        # §0/§0.6: the dossier informs, the Designer judges. NO verdict field.
        d = workup.build_workup("ESPORTS")
        self.assertNotIn("verdict", d)
        for k, v in d.items():
            if isinstance(v, dict):
                self.assertNotIn("verdict", v, f"section {k} must not carry a verdict")

    def test_meta_ms_present(self):
        d = workup.build_workup("ESPORTS")
        self.assertIn("meta", d)
        self.assertIn("ms", d["meta"])
        self.assertIsInstance(d["meta"]["ms"], (int, float))

    def test_rr_carries_setup_score_and_missing_legs(self):
        d = workup.build_workup("ESPORTS")
        rr = d["rr"]
        self.assertIn("setup_score", rr)
        # score_all returns every setup; each names its missing legs
        self.assertIn("blowoff", rr["setup_score"])
        self.assertIn("missing", rr["setup_score"]["blowoff"])

    def test_chips_surfaces_unsupported_chain_supply(self):
        d = workup.build_workup("ESPORTS")
        self.assertTrue(d["chips"]["available"])
        self.assertEqual(d["chips"]["coverage"]["unreadable_supply_pct"], 12.0)

    def test_oi_construction_has_wash_tag_and_funding_divergence(self):
        d = workup.build_workup("ESPORTS")
        oc = d["oi_construction"]
        self.assertEqual(oc["oi_sides_tag"], "REAL_DIRECTIONAL")
        # cross-venue funding spread surfaced (−0.42 vs −0.10 = 0.32 delta)
        self.assertAlmostEqual(oc["funding_divergence_4h"], 0.32, places=2)

    def test_flags_include_liquidity_tier_and_alerts(self):
        d = workup.build_workup("ESPORTS")
        f = d["flags"]
        self.assertIn("liquidity_tier", f)
        self.assertEqual(f["liquidity_tier"], "SCOUT")    # 18M in the 10-25 band
        self.assertEqual(f["alerts"]["n"], 1)

    def test_flags_venue_mark_divergence(self):
        d = workup.build_workup("ESPORTS")
        # mids 0.100 vs 0.1005 → 0.5% divergence, below the 2% flag threshold
        self.assertIn("venue_mark_divergence_pct", d["flags"])
        self.assertFalse(d["flags"]["venue_mark_divergence_flag"])

    def test_section_degrades_when_a_read_raises(self):
        def _boom(ticker):
            raise RuntimeError("rpc 408")
        workup.build_onchain = _boom
        d = workup.build_workup("ESPORTS")
        self.assertFalse(d["chips"]["available"])
        self.assertIn("rpc 408", d["chips"]["reason"])
        # other sections still present — one dead read never aborts the dossier
        self.assertTrue(d["stage"]["available"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
