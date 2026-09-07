#!/usr/bin/env python3
"""SPEC 44 — classify's live-funding leg must not regress the SPEC-10 floor sentinel.

Run:  python3 tests/test_classify_floor_regression.py

The live-predicted funding leg (SPEC 18/23) must route through the floor sentinel
(SPEC 10/13). Three cases:
  1. floor on primary, REAL value on secondary  → secondary wins, not a floor CONFIRM.
  2. floor on EVERY covered venue (>=2 sources)  → corroborated genuine ~0% flat, may CONFIRM,
     tagged `all_floor` in the live payload + reason.
  3. floor on the ONLY covered venue (no secondary) → FUNDING_SUSPECT: a single-source floor
     can't corroborate a flat (CLAUDE.md §3), so it must NOT be the basis of a confident CONFIRM.

Venue fetches monkeypatched — offline, deterministic.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_rf = importlib.util.spec_from_file_location("regime_flip", ROOT / "capabilities" / "regime_flip.py")
RF = importlib.util.module_from_spec(_rf)
_rf.loader.exec_module(RF)
FLOOR = RF.FUNDING_FLOOR_RAW

_cl = importlib.util.spec_from_file_location("classify", ROOT / "capabilities" / "classify.py")
CL = importlib.util.module_from_spec(_cl)
_cl.loader.exec_module(CL)


def venue(name, raw, iv=240, oi=1.0, vol=50.0):
    return {"venue": name, "funding_raw": raw, "interval_min": iv,
            "price": 1.0, "chg24": 1.0, "vol_m": vol, "oi": oi}


class TestFloorRegression(unittest.TestCase):
    def patch(self, bybit, binance, bitget=None, aster=None):
        # SPEC 44 addendum: the no-real-read path lazily consults the Bitget/Aster
        # secondaries — pin them (default dark) so every case stays offline-deterministic
        self._orig = (RF._venue_bybit, RF._venue_binance,
                      RF._venue_bitget, RF._venue_aster)
        RF._venue_bybit = lambda s: bybit
        RF._venue_binance = lambda s: binance
        RF._venue_bitget = lambda s: bitget
        RF._venue_aster = lambda s: aster

    def tearDown(self):
        (RF._venue_bybit, RF._venue_binance,
         RF._venue_bitget, RF._venue_aster) = self._orig

    # ── case 1: floor-primary / real-secondary → secondary wins ──────────────
    def test_real_secondary_wins_over_floor_primary(self):
        # binance (primary by OI) floors, bybit has a real value → use the real venue, no SUSPECT
        self.patch(venue("bybit", 0.00037, iv=240),
                   venue("binance", FLOOR, iv=240, oi=1e9))
        live = RF.live_perp("X")
        self.assertEqual(live["venue"], "bybit")
        self.assertFalse(live["all_floor"])
        self.assertFalse(live.get("funding_suspect"))
        sev, tag, note = RF.classify({"ticker": "X", "state": "watch"}, live)
        self.assertNotIn(tag, ("FUNDING_SUSPECT", "FUNDING_UNAVAILABLE"))

    # ── case 2: floor on every covered venue (>=2) → corroborated flat, may CONFIRM ──
    def test_all_floor_two_venues_is_genuine_flat(self):
        self.patch(venue("bybit", FLOOR, iv=60), venue("binance", FLOOR, iv=240))
        live = RF.live_perp("PIEVERSE")
        self.assertTrue(live["all_floor"])
        self.assertFalse(live.get("funding_suspect"))
        sev, tag, note = RF.classify({"ticker": "PIEVERSE", "state": "watch"}, live)
        self.assertEqual(tag, "CONFIRM")
        # DoD: a flat CONFIRM on a floor print is acceptable ONLY when tagged all_floor.
        self.assertIn("all_floor", note.lower())

    # ── case 3: floor on the ONLY covered venue → FUNDING_SUSPECT ─────────────
    def test_single_venue_floor_is_suspect_not_confident_flat(self):
        # PLAY-style: binance lists the perp at the floor, bybit not listed → no corroboration
        self.patch(None, venue("binance", FLOOR, iv=240, oi=1e9))
        live = RF.live_perp("PLAY")
        self.assertFalse(live["all_floor"])          # NOT a corroborated genuine flat
        self.assertTrue(live["funding_suspect"])
        sev, tag, note = RF.classify({"ticker": "PLAY", "state": "watch"}, live)
        self.assertEqual(tag, "FUNDING_SUSPECT")
        self.assertIn("suspect", note.lower())
        # must NOT masquerade as a confident flat "consistent with memo"
        self.assertNotIn("consistent with memo", note.lower())

    # ── near-floor: the live-predicted rate hovers just off the exact 0.00005 clamp ──
    def test_near_floor_value_treated_as_floor(self):
        # bybit live-predicted 0.000054 (rounds to +0.005%) must NOT evade the sentinel and
        # surface as a confident flat; binance also near-floor → corroborated all_floor.
        self.patch(venue("bybit", 0.000054, iv=240), venue("binance", 0.0000487, iv=240))
        live = RF.live_perp("BEAT")
        self.assertTrue(live["venues"]["bybit"]["is_floor"])
        self.assertTrue(live["all_floor"])
        self.assertFalse(live.get("funding_suspect"))

    def test_real_micro_read_not_floor(self):
        # genuine ~0% reads below the floor magnitude (EDEN/CHIP, raw ~1.5e-5 = +0.001-0.002%/4h)
        # must stay REAL, not get swallowed by the floor band.
        self.patch(venue("bybit", RF.FUNDING_FLOOR_RAW, iv=240), venue("binance", 0.00001451, iv=240, oi=1e9))
        live = RF.live_perp("EDEN")
        self.assertFalse(live["venues"]["binance"]["is_floor"])
        self.assertFalse(live["all_floor"])
        self.assertEqual(live["venue"], "binance")

    # ── classify_token (board verdict): suspect degrades, does not fabricate ──
    def test_classify_token_suspect_degrades_not_confident_confirm(self):
        self.patch(None, venue("binance", FLOOR, iv=240, oi=1e9))
        live = RF.live_perp("PLAY")
        out = CL.classify_token({"ticker": "PLAY", "state": "watch"}, live)
        # not a BREAK/TRIGGER off a fabricated floor — it degrades to CONFIRMS...
        self.assertEqual(out["verdict"], "CONFIRMS")
        # ...but the reason carries the SUSPECT tag, not a confident flat
        self.assertIn("FUNDING_SUSPECT", out["reason"])
        self.assertNotIn("consistent with memo", out["reason"].lower())

    def test_classify_token_all_floor_confirms_with_tag(self):
        self.patch(venue("bybit", FLOOR, iv=60), venue("binance", FLOOR, iv=240))
        live = RF.live_perp("PIEVERSE")
        out = CL.classify_token({"ticker": "PIEVERSE", "state": "watch"}, live)
        self.assertEqual(out["verdict"], "CONFIRMS")
        self.assertIn("all_floor", out["reason"].lower())

    # ── SPEC 44 addendum: Bitget/Aster as lazy floor-resolution secondaries ──
    def test_floor_primary_real_bitget_secondary_wins(self):
        # binance floors, bybit not listed → Bitget's real read is the funding verdict
        # (the SKYAI-class case: the real book is Bitget; a lone Binance floor must not
        # decide the leg when a wired secondary has a real rate)
        self.patch(None, venue("binance", FLOOR, iv=240, oi=1e6),
                   bitget=venue("bitget", -0.0050, iv=240, oi=2e6))   # −0.50%/4h real
        live = RF.live_perp("SKYAI")
        self.assertEqual(live["venue"], "bitget")
        self.assertAlmostEqual(live["funding_4h"], -0.50, places=2)
        self.assertFalse(live["all_floor"])
        self.assertFalse(live.get("funding_suspect"))

    def test_floor_primary_real_aster_secondary_wins(self):
        # BEAT/BSB/SLX-class: binance floors, aster carries the real (tiny) rate
        self.patch(None, venue("binance", FLOOR, iv=240, oi=1e6),
                   aster=venue("aster", 0.0000125, iv=60))            # +0.005%/4h real
        live = RF.live_perp("BEAT")
        self.assertEqual(live["venue"], "aster")
        self.assertFalse(live.get("funding_suspect"))

    def test_secondary_floor_corroborates_all_floor(self):
        # binance floors AND bitget floors → still no real read → 2 sources = all_floor
        self.patch(None, venue("binance", FLOOR, iv=240, oi=1e6),
                   bitget=venue("bitget", FLOOR, iv=240))
        live = RF.live_perp("X")
        self.assertTrue(live["all_floor"])
        self.assertFalse(live.get("funding_suspect"))

    def test_lazy_no_secondary_fetch_on_real_primary(self):
        # a real binance/bybit read must NOT touch the secondaries (latency + quota)
        def boom(s):
            raise AssertionError("secondary venue fetched despite a real primary read")
        self.patch(venue("bybit", 0.00037, iv=240),
                   venue("binance", -0.001, iv=240, oi=1e9))
        RF._venue_bitget = boom
        RF._venue_aster = boom
        live = RF.live_perp("X")
        self.assertEqual(live["venue"], "binance")    # more-vetoing real venue


if __name__ == "__main__":
    unittest.main(verbosity=2)
