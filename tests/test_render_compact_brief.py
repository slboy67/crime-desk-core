#!/usr/bin/env python3
"""SPEC-193 — `brief`'s `render:"compact"` mode.

Every JSON the orchestrator reads is a prompt-cache write on the top-tier model; the
full `brief` envelope carries raw depth levels, raw OHLC bar arrays, per-venue kline
payloads and per-component `layer_ms` that the desk's decision layer (§0.5/§0.6) never
consumes. `compact_brief()` is a pure transform over an already-built brief dict (no
network) — offline-deterministic by construction.

Pins:
  1. Size ceiling — a realistic full-shape fixture compacts to <= 3 KB.
  2. Every decision key `compact_brief` carries is byte-for-byte EQUAL to the value in
     the full brief it was built from (never re-derived, never re-rounded).
  3. `state.reason` is NEVER truncated, even when long.
  4. `state.thesis` (the restable entry/stop/tp/legs/watch_level geometry) survives
     unchanged — a commit re-read off the compact render must reproduce the same params.
"""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


BR = _load("brief")

LONG_REASON = (
    "[SURVEIL STALE 4h] price within zone, funding flat — magnet sweep in progress; "
    "leg A trigger window opens on the reclaim; venue_agreement FULL 11/14; "
    "rf=CONFIRM: live funding neg (-0.052%/4h (raw -0.052%/4h, binance)) consistent "
    "with memo — this string is deliberately long to prove the compact render never "
    "truncates a decision-relevant reason string, unlike the 65/70-char TTY renders."
)

THESIS = {"direction": "SHORT", "entry_zone": [0.20, 0.21], "stop": 0.225,
          "tp": [0.18, 0.16], "legs": [{"label": "A", "size_pct": 60}],
          "watch_level": [{"price": 0.19, "dir": "below", "note": "shelf"}],
          "triggers": ["LH retest"], "invalidation": "reclaim 0.225",
          "time_stop_h": 72, "committed_ts": "2026-06-05"}


def _full_brief_fixture():
    return {
        "ticker": "OP",
        "headline": "OP — SHORT thesis, price in zone, funding flat, on-chain QUIET",
        "timed_out": [],
        "state": {"available": True, "verdict": "CONFIRMS", "reason": LONG_REASON,
                  "thesis_present": True, "direction": "SHORT", "thesis": THESIS},
        "perp": {
            "available": True, "verdict": "WATCH", "direction": "SHORT-loading-WATCH",
            "tier": "watch", "funding_4h": -0.052, "funding_venue": "binance",
            "floor_suspect": False,
            "funding_by_venue": {
                "binance": {"funding_4h": -0.052, "is_floor": False},
                "bybit": {"funding_4h": -0.048, "is_floor": False},
                "aster": {"funding_4h": 0.005, "is_floor": True},
            },
            "oi_chg_pct": -3, "near_ath": False, "cvd_verdict": "distribution",
            "price": 0.1834, "battlefield": "perp_led",
            "oi_mc_flag": None, "oi_mc_caveat": None,
            "oi_construction": {
                "verdict": "MIXED", "degraded": [],
                "oi_types": [{"type": "vesting_hedge", "verdict": "ASSERTED",
                             "share_read": "majority", "side": "short"}],
                "battlefield": {"battlefield": "perp_led"},
                "venue_roles": {"exit": {"venue": "bitget", "grade": "FLOW_CONFIRMED"}},
            },
        },
        "onchain": {
            "available": True, "signal": "QUIET", "bias": None, "score": 0,
            "newly_fired": [], "staging_vs_execution": None,
            "distribution_freshness": "FROZEN",
            "concentration": {"available": True, "top1_pct": 14.2, "top10_pct": 38.9,
                              "holder_count": 4211},
        },
        "books": {
            "available": True,
            "venues": {
                "aster": {"available": True, "mid": 0.1834,
                         "ask_wall_above": {"price": 0.19, "notional_usd": 42000.0},
                         "bid_shelf_below": {"price": 0.175, "notional_usd": 61000.0},
                         "truncated": False, "deepest_level_seen": 0.21},
                "bitget": {"available": False, "reason": "timeout"},
            },
        },
        "risk_card": {"available": True,
                      "line": "hypothesis · 5x max lev (aster) · risk cap 5% eq = $5.50 at stop"},
        "venue_breadth": {"available": True, "size_book": "bybit", "n_venues": 9,
                          "wired_oi_share_pct": 71.4, "desk_blind_share_pct": 28.6,
                          "venues": [{"venue": "bybit", "oi": 1.2e7}] * 9},
        "venue_bars": {
            "available": True, "n_available": 14, "n_total": 14,
            "dominant_tape": {"venue": "binance", "turnover_24h_usd": 5.1e7},
            "execution_venue": "aster",
            "bars": [{"ts": 1, "median_h": 0.185, "median_l": 0.180, "median_c": 0.183,
                     "high_spread_pct": 0.4, "low_spread_pct": 0.3, "live": True}] * 6,
            "tape_agreement": ["0.19 stop: crossed 3/14 (closed beyond 1/14) · Binance ✗"],
        },
        "meta": {"layer_ms": {"state": 120, "perp": 340, "books": 900, "onchain": 1200}},
    }


class TestCompactBriefSize(unittest.TestCase):
    def test_size_under_3kb(self):
        c = BR.compact_brief(_full_brief_fixture())
        size = len(json.dumps(c))
        self.assertLessEqual(size, 3072, f"compact brief is {size} bytes")


class TestCompactBriefDecisionKeysMatchFull(unittest.TestCase):
    def setUp(self):
        self.full = _full_brief_fixture()
        self.c = BR.compact_brief(self.full)

    def test_state_verdict_reason_thesis_equal_to_full(self):
        self.assertEqual(self.c["state"]["verdict"], self.full["state"]["verdict"])
        self.assertEqual(self.c["state"]["reason"], self.full["state"]["reason"])
        self.assertEqual(self.c["state"]["thesis"], self.full["state"]["thesis"])

    def test_reason_never_truncated(self):
        self.assertEqual(self.c["state"]["reason"], LONG_REASON)
        self.assertGreater(len(self.c["state"]["reason"]), 300)

    def test_thesis_restable_params_survive_byte_for_byte(self):
        th = self.c["state"]["thesis"]
        self.assertEqual(th["entry_zone"], [0.20, 0.21])
        self.assertEqual(th["stop"], 0.225)
        self.assertEqual(th["tp"], [0.18, 0.16])
        self.assertEqual(th["legs"], THESIS["legs"])
        self.assertEqual(th["watch_level"], THESIS["watch_level"])

    def test_headline_equal_to_full(self):
        self.assertEqual(self.c["headline"], self.full["headline"])

    def test_perp_funding_verdict_price_equal_to_full(self):
        p = self.c["perp"]
        self.assertEqual(p["funding_4h"], self.full["perp"]["funding_4h"])
        self.assertEqual(p["verdict"], self.full["perp"]["verdict"])
        self.assertEqual(p["price"], self.full["perp"]["price"])

    def test_oic_line_present_and_matches_module_helper(self):
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("oi_construction", ROOT / "capabilities" / "oi_construction.py")
        OC = ilu.module_from_spec(spec)
        spec.loader.exec_module(OC)
        expected = OC.compact_line(self.full["perp"]["oi_construction"])
        self.assertEqual(self.c["perp"]["oic"], expected)
        self.assertIsNotNone(expected)

    def test_risk_card_line_equal_to_full(self):
        self.assertEqual(self.c["risk_card"]["line"], self.full["risk_card"]["line"])

    def test_venue_breadth_summary_fields_equal_to_full(self):
        vb = self.c["venue_breadth"]
        self.assertEqual(vb["size_book"], self.full["venue_breadth"]["size_book"])
        self.assertEqual(vb["desk_blind_share_pct"], self.full["venue_breadth"]["desk_blind_share_pct"])
        # the raw per-venue OI array is bulk — dropped
        self.assertNotIn("venues", vb)

    def test_venue_bars_drops_raw_bars_keeps_summary_and_tape_agreement(self):
        vbars = self.c["venue_bars"]
        self.assertNotIn("bars", vbars)
        self.assertEqual(vbars["tape_agreement"], self.full["venue_bars"]["tape_agreement"])
        self.assertEqual(vbars["dominant_tape"], self.full["venue_bars"]["dominant_tape"])
        self.assertIn("tape (", vbars["summary"])

    def test_books_reduced_to_one_line_per_venue(self):
        books = self.c["books"]
        self.assertIsInstance(books["aster"], str)
        self.assertIn("mid", books["aster"])
        self.assertIn("unavailable", books["bitget"])

    def test_funding_by_venue_dict_dropped_for_dispersion_string(self):
        self.assertNotIn("funding_by_venue", self.c["perp"])
        self.assertIn("funding_dispersion", self.c["perp"])
        self.assertIn("binance", self.c["perp"]["funding_dispersion"])
        self.assertIn("aster", self.c["perp"]["funding_dispersion"])

    def test_layer_ms_dropped(self):
        self.assertNotIn("meta", self.c)


class TestCompactBriefDegradedLayers(unittest.TestCase):
    def test_unavailable_venue_breadth_carries_reason_not_crash(self):
        full = _full_brief_fixture()
        full["venue_breadth"] = {"available": False, "reason": "not_in_matrix"}
        c = BR.compact_brief(full)
        self.assertEqual(c["venue_breadth"], {"available": False, "reason": "not_in_matrix"})

    def test_unavailable_venue_bars_carries_reason_not_crash(self):
        full = _full_brief_fixture()
        full["venue_bars"] = {"available": False, "reason": "no venue returned bars"}
        c = BR.compact_brief(full)
        self.assertEqual(c["venue_bars"], {"available": False, "reason": "no venue returned bars"})

    def test_no_thesis_no_crash(self):
        full = _full_brief_fixture()
        full["state"] = {"available": True, "verdict": "CONFIRMS", "reason": "no thesis",
                         "thesis_present": False, "direction": None, "thesis": None}
        c = BR.compact_brief(full)
        self.assertEqual(c["state"]["reason"], "no thesis")
        self.assertNotIn("thesis", c["state"])


if __name__ == "__main__":
    unittest.main()
