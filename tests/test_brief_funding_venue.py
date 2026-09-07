#!/usr/bin/env python3
"""SPEC-71 — brief: floor-print venue must not win funding selection; per-venue surface.

Run:  python3 tests/test_brief_funding_venue.py

The BILL incident (2026-06-12 14:44Z): one brief payload asserted TWO different live
funding verdicts — the state leg (classify → regime_flip.live_perp) correctly reported
binance +0.023%/4h while the perp leg + headline reported 0.005 (a venue floor/placeholder
print, memory: feedback_funding_0005_is_placeholder_not_flat) with funding_venue null.
On a funding-gated Tier-1 trigger the headline said NOT-met while it WAS met.

Fix under test:
  1. brief resolves live_perp ONCE (shared future) — the state leg and the perp leg read
     the SAME resolution, so one payload cannot disagree with itself (req 3).
  2. The perp leg's funding fields mirror that resolution: a floor print never wins while
     a real print exists (live_perp already guarantees this — req 1), and funding_venue
     is populated with the venue actually used, never null (req 2).
  3. funding_by_venue surfaces per-venue %/4h + raw, including bitget fetched display-only
     when the lazy resolver never consulted it (req 2).
  4. A single-venue floor-only resolution is flagged floor_suspect — a floor print alone
     is a data-failure signal, not a flat verdict (§3).

All layer fns + the surface fetch are mocked — offline-deterministic.
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


BR = _load("brief")


# ── canned fixtures ─────────────────────────────────────────────────────────────
def _watchlist():
    return ([{"ticker": "BILL", "state": "trap-long watch",
              "thesis": {"direction": "LONG", "entry_zone": [0.028, 0.031], "stop": 0.026,
                         "tp": [0.04], "triggers": ["funding flips REAL positive >=+0.02%/4h"],
                         "invalidation": "close < 0.026", "time_stop_h": 96}}], None)


# The shared resolver result for the BILL fixture: binance REAL +0.0225%/4h.
# (regime_flip.live_perp shape — venues carries the per-venue split.)
LIVE_BILL = {
    "venue": "binance", "primary_venue": "binance",
    "funding_pi": 0.0225, "interval_min": 240, "funding_4h": 0.0225,
    "funding_stale": False, "all_floor": False, "funding_suspect": False,
    "funding_split": False,
    "price": 0.0305, "chg24": 3.1, "vol_m": 18.0, "oi": 9.0e5,
    "venues": {"binance": {"funding_4h": 0.0225, "funding_pi": 0.0225,
                           "interval_min": 240, "is_floor": False,
                           "oi": 9.0e5, "vol_m": 18.0}},
}

# Single covered venue sitting at the floor, no secondary to corroborate (SPEC 44).
LIVE_FLOOR_ONLY = {
    "venue": "bybit", "primary_venue": "bybit",
    "funding_pi": 0.005, "interval_min": 240, "funding_4h": 0.005,
    "funding_stale": False, "all_floor": False, "funding_suspect": True,
    "funding_split": False,
    "price": 0.0305, "chg24": None, "vol_m": 2.0, "oi": 1.0e5,
    "venues": {"bybit": {"funding_4h": 0.005, "funding_pi": 0.005,
                         "interval_min": 240, "is_floor": True,
                         "oi": 1.0e5, "vol_m": 2.0}},
}

# Bitget's live print for the BILL fixture — the FLOOR placeholder (surfaced display-only).
BITGET_FLOOR = {"funding_4h": 0.005, "funding_pi": 0.005, "interval_min": 240,
                "is_floor": True, "surfaced_only": True}


def _fake_analyse_floorbug(ticker, days=90):
    """Reproduces the live bug exactly: analyse hands brief the floor print with a
    null venue. The perp leg must NOT echo this while the shared resolver has a
    real print."""
    return {"ticker": ticker, "verdict": "WATCH", "direction": "WATCH", "tier": "WATCH",
            "funding_4h": 0.005, "funding_venue": None, "funding_unavailable": False,
            "oi_chg_pct": 5, "near_ath": False, "cvd_verdict": None, "price": 0.0305,
            "onchain": "OK", "nonce_signal": "QUIET", "notes": []}


def _fake_depth(ticker, venue=None):
    return {"ticker": ticker, "venues": {
        "bitget": {"available": True, "mid": 0.0305, "bid_shelf_below": {"price": 0.0300},
                   "ask_wall_above": None, "truncated": False, "deepest_level_seen": {}},
        "binance": {"available": True, "mid": 0.0305, "bid_shelf_below": {"price": 0.0301},
                    "ask_wall_above": None, "truncated": False, "deepest_level_seen": {}}}}


def _fake_onchain(ticker, depth="fast"):
    return {"ticker": ticker, "bias": None, "score": 0, "signal": "QUIET",
            "nonces": {"tracked": True, "escalation_fired": []},
            "concentration": {"available": False}}


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(BR, k) for k in
                       ("load_watchlist", "classify_token", "live_perp",
                        "build_analyse", "build_depth", "build_onchain",
                        "_surface_bitget", "_surface_hyperliquid", "fetch_aster_symbols")}
        self.live_calls = []
        self.classify_live_seen = []

        def counting_live(t):
            self.live_calls.append(t)
            return LIVE_BILL

        def fake_classify(tok, live=None):
            self.classify_live_seen.append(live)
            return {"ticker": tok["ticker"], "verdict": "TRIGGERS",
                    "reason": "funding flipped real positive (binance +0.023%/4h)",
                    "direction": "LONG", "thesis_present": True, "live": live}

        BR.load_watchlist = _watchlist
        BR.classify_token = fake_classify
        BR.live_perp = counting_live
        BR.build_analyse = _fake_analyse_floorbug
        BR.build_depth = _fake_depth
        BR.build_onchain = _fake_onchain
        BR._surface_bitget = lambda t: BITGET_FLOOR
        BR._surface_hyperliquid = lambda t: None       # SPEC-84: these BILL fixtures aren't on HL
        BR.fetch_aster_symbols = lambda: None   # SPEC-136: unknown — no live network in tests

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(BR, k, v)


class TestBillFixture(_Base):
    """DoD bullet 1 — binance +0.0225%/4h real, bitget +0.005 floor."""

    def test_perp_leg_reports_the_real_binance_rate_not_the_floor(self):
        perp = BR.build_brief("BILL")["perp"]
        self.assertEqual(perp["funding_4h"], 0.0225)
        self.assertEqual(perp["funding_venue"], "binance")     # never null (req 2)
        self.assertFalse(perp["funding_unavailable"])
        self.assertFalse(perp.get("floor_suspect"))

    def test_funding_by_venue_shows_both_with_floor_flagged(self):
        perp = BR.build_brief("BILL")["perp"]
        fbv = perp["funding_by_venue"]
        self.assertIn("binance", fbv)
        self.assertIn("bitget", fbv)                           # surfaced even though lazy
        self.assertEqual(fbv["binance"]["funding_4h"], 0.0225)
        self.assertFalse(fbv["binance"]["is_floor"])
        self.assertEqual(fbv["bitget"]["funding_4h"], 0.005)
        self.assertTrue(fbv["bitget"]["is_floor"])             # the placeholder, marked

    def test_headline_matches_the_state_leg(self):
        b = BR.build_brief("BILL")
        expected = f"funding {0.0225:+.3f}%/4h (binance)"
        self.assertIn(expected, b["headline"])
        self.assertNotIn("0.005", b["headline"])               # the floor never headlines

    def test_one_shared_resolution_for_both_legs(self):
        BR.build_brief("BILL")
        # resolved exactly ONCE …
        self.assertEqual(len(self.live_calls), 1)
        # … and the state leg's classify received that SAME object (req 3)
        self.assertEqual(len(self.classify_live_seen), 1)
        self.assertIs(self.classify_live_seen[0], LIVE_BILL)


class TestFloorOnlySuspect(_Base):
    """DoD bullet 2 — single-venue floor-only still reports the floor but FLAGGED."""

    def setUp(self):
        super().setUp()
        BR.live_perp = lambda t: LIVE_FLOOR_ONLY
        BR._surface_bitget = lambda t: None                    # bitget doesn't list it

    def test_floor_reported_but_flagged_suspect(self):
        perp = BR.build_brief("BILL")["perp"]
        self.assertEqual(perp["funding_4h"], 0.005)            # still reported …
        self.assertEqual(perp["funding_venue"], "bybit")
        self.assertTrue(perp["floor_suspect"])                 # … but a data-failure flag (§3)
        self.assertIn("bybit", perp["funding_by_venue"])
        self.assertTrue(perp["funding_by_venue"]["bybit"]["is_floor"])

    def test_headline_carries_the_floor_warning(self):
        b = BR.build_brief("BILL")
        self.assertIn("floor-suspect", b["headline"])


class TestResolverDegrades(_Base):
    """No resolution → analyse passthrough (degrade-explicit, never a crash)."""

    def test_resolver_none_falls_back_to_analyse_fields(self):
        BR.live_perp = lambda t: None
        perp = BR.build_brief("BILL")["perp"]
        self.assertTrue(perp["available"])
        self.assertEqual(perp["funding_4h"], 0.005)            # analyse's value, unmasked
        self.assertEqual(perp["funding_by_venue"], {})         # nothing fabricated

    def test_resolver_raises_is_contained(self):
        def boom(t):
            raise RuntimeError("venue down")
        BR.live_perp = boom
        b = BR.build_brief("BILL")
        self.assertTrue(b["perp"]["available"])
        self.assertTrue(b["books"]["available"])

    def test_legacy_live_shape_without_venues_is_tolerated(self):
        # a live dict without venues (older shape / partial mock) must not fetch or crash
        BR.live_perp = lambda t: {"price": 0.03, "funding_4h": 0.004}
        surface_calls = []
        BR._surface_bitget = lambda t: surface_calls.append(t)
        perp = BR.build_brief("BILL")["perp"]
        self.assertEqual(perp["funding_4h"], 0.004)
        self.assertEqual(perp["funding_by_venue"], {})
        self.assertEqual(surface_calls, [])                    # display fetch needs a real resolution


if __name__ == "__main__":
    unittest.main(verbosity=2)
