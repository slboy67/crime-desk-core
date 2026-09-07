#!/usr/bin/env python3
"""SPEC-93 — extreme-negative-funding monitor (standing squeeze-loading radar).

Run:  python3 tests/test_funding_surveil.py

Offline-deterministic: scan rows + per-ticker cross-venue perp reads are INJECTED (no
network), clock injected, tmp state file. Mirrors the nonce-surveil cadence pattern for
funding. The lesson this guards: IN (deep-neg −2.5%/4h + OI +41% §4 LONG candidate) only
surfaced because the user dropped the ticker — nothing watched the deep-neg band.

What it asserts (from the spec DoD):
  - cross-venue floor rejection: a lone Bybit +0.005 floor / zero / suspect read does NOT
    qualify; the verified rate is the most-negative NON-floor venue.
  - enrichment decode: deep-neg + OI rising → §4 LONG candidate; deep-neg + OI flat/falling
    → hedge/distribution trap (veto-context). BOTH carry the §5 short-veto flag.
  - DUST (sub-gate vol) is marked and NOT paged as tradeable.
  - change-detection: a NEW band name pages; an identical second tick does NOT re-page; a
    material deepen (−1.0 → −2.0) or an OI-context flip to rising re-pages.
"""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("funding_surveil", ROOT / "ops" / "funding_surveil.py")
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)

T0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _cand(ticker, funding_4h=-2.5, turnover_m=14.0, oi=1_000_000.0, chg24=20.0):
    """A scan `longs[]` row (the cross-sectional Bybit print)."""
    return {"ticker": ticker, "funding_4h": funding_4h, "funding_raw": funding_4h / 6.0,
            "interval_h": 4, "turnover_m": turnover_m, "chg24": chg24, "oi": oi,
            "price": 1.0, "deep": True, "side": "long"}


def _lp(funding_4h=-2.5, oi=1_000_000.0, venue="bybit", suspect=False, all_floor=False,
        venues=None):
    """A regime_flip.live_perp() return: cross-venue, floor-resolved."""
    return {"venue": venue, "primary_venue": venue, "funding_4h": funding_4h,
            "funding_pi": funding_4h / 6.0, "interval_min": 240, "funding_stale": False,
            "all_floor": all_floor, "funding_suspect": suspect, "funding_split": False,
            "price": 1.0, "chg24": 20.0, "vol_m": 14.0, "oi": oi,
            "venues": venues or {venue: {"funding_4h": funding_4h, "is_floor": False, "oi": oi}}}


def _perp_fn(table):
    def fn(ticker):
        return table.get(ticker)
    return fn


class TestOiDecode(unittest.TestCase):
    def test_oi_context_buckets(self):
        self.assertEqual(F.oi_context(41.0), "rising")
        self.assertEqual(F.oi_context(-30.0), "falling")
        self.assertEqual(F.oi_context(2.0), "flat")
        self.assertEqual(F.oi_context(None), "unknown")

    def test_decode_rising_is_long_candidate(self):
        label, sev, is_long = F.decode(-2.5, "rising")
        self.assertIn("§4", label)
        self.assertIn("LONG", label.upper())
        self.assertTrue(is_long)
        self.assertEqual(sev, "HIGH")

    def test_decode_falling_is_veto_context_trap(self):
        label, sev, is_long = F.decode(-2.5, "falling")
        self.assertIn("trap", label.lower())
        self.assertIn("veto-context", label.lower())
        self.assertFalse(is_long)

    def test_decode_flat_is_veto_context_trap(self):
        label, _sev, is_long = F.decode(-2.5, "flat")
        self.assertIn("veto-context", label.lower())
        self.assertFalse(is_long)


class TestEnrichShortVeto(unittest.TestCase):
    def test_both_oi_contexts_carry_short_veto(self):
        rising = F.enrich_hit(_cand("IN"), _lp(), oi_chg_pct=41.0)
        falling = F.enrich_hit(_cand("XX"), _lp(), oi_chg_pct=-30.0)
        self.assertTrue(rising["short_veto"])
        self.assertTrue(falling["short_veto"])
        self.assertTrue(rising["is_long_candidate"])
        self.assertFalse(falling["is_long_candidate"])

    def test_enrich_copy_carries_decode_not_bare_number(self):
        hit = F.enrich_hit(_cand("IN"), _lp(), oi_chg_pct=41.0)
        msg = hit["msg"]
        self.assertIn("IN", msg)
        self.assertIn("§4", msg)
        self.assertIn("short-veto", msg.lower())
        self.assertIn("OI", msg)


class TestFloorRejection(unittest.TestCase):
    def test_lone_floor_suspect_is_rejected(self):
        # Bybit surfaced it but cross-venue is a lone floor print → SUSPECT, not a datum.
        hit = F.enrich_hit(_cand("FLOORED"), _lp(suspect=True))
        self.assertIsNone(hit)

    def test_all_floor_flat_is_rejected(self):
        hit = F.enrich_hit(_cand("FLAT"), _lp(funding_4h=0.0, all_floor=True))
        self.assertIsNone(hit)

    def test_no_perp_read_is_rejected(self):
        self.assertIsNone(F.enrich_hit(_cand("NOPERP"), None))

    def test_crossvenue_downgrade_below_band_is_rejected(self):
        # scan saw −2.5 on bybit, but the most-negative NON-floor venue is only −0.4 → not in band
        hit = F.enrich_hit(_cand("DOWN", funding_4h=-2.5), _lp(funding_4h=-0.4))
        self.assertIsNone(hit)

    def test_verified_rate_is_the_crossvenue_value(self):
        # the rate stored is the live_perp (most-negative non-floor) value, not the scan print
        hit = F.enrich_hit(_cand("IN", funding_4h=-2.5), _lp(funding_4h=-2.0), oi_chg_pct=41.0)
        self.assertEqual(hit["funding_4h"], -2.0)


class TestDust(unittest.TestCase):
    def test_dust_marked_and_not_paged(self):
        cands = [_cand("DUSTY", turnover_m=3.0)]
        perp = _perp_fn({"DUSTY": _lp()})
        alerts, _bl = F.assess(cands, perp, baseline={}, now=T0)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["tier"], "DUST")
        self.assertFalse(alerts[0]["page"])

    def test_tradeable_vol_is_not_dust(self):
        self.assertEqual(F.liq_tier(40.0), "tradeable")
        self.assertEqual(F.liq_tier(14.0), "scout")
        self.assertEqual(F.liq_tier(3.0), "DUST")


class TestChangeDetection(unittest.TestCase):
    def test_new_name_pages_then_identical_tick_does_not(self):
        cands = [_cand("IN", funding_4h=-2.5, oi=1_000_000.0)]
        perp = _perp_fn({"IN": _lp(funding_4h=-2.5, oi=1_000_000.0)})

        alerts1, bl1 = F.assess(cands, perp, baseline={}, now=T0)
        paged1 = [a for a in alerts1 if a["page"]]
        self.assertEqual([a["ticker"] for a in paged1], ["IN"])
        self.assertEqual(paged1[0]["reason"], "new")

        # second tick, same set, baseline carried forward → NO re-page
        alerts2, _bl2 = F.assess(cands, perp, baseline=bl1, now=T0)
        self.assertEqual([a for a in alerts2 if a["page"]], [])

    def test_material_deepen_repages(self):
        bl = {"IN": {"funding_4h": -1.2, "oi": 1_000_000.0, "oi_context": "flat",
                     "tier": "scout", "first_seen": "2026-06-30T11:00:00Z"}}
        cands = [_cand("IN", funding_4h=-2.5, oi=1_000_000.0)]
        perp = _perp_fn({"IN": _lp(funding_4h=-2.5, oi=1_000_000.0)})
        alerts, _ = F.assess(cands, perp, baseline=bl, now=T0)
        paged = [a for a in alerts if a["page"]]
        self.assertEqual([a["ticker"] for a in paged], ["IN"])
        self.assertEqual(paged[0]["reason"], "deepened")

    def test_oi_flip_to_rising_repages(self):
        # was tracked, OI was flat; now OI rose >10% → flips to §4 LONG context
        bl = {"IN": {"funding_4h": -2.5, "oi": 1_000_000.0, "oi_context": "flat",
                     "tier": "scout", "first_seen": "2026-06-30T11:00:00Z"}}
        cands = [_cand("IN", funding_4h=-2.5, oi=1_500_000.0)]
        perp = _perp_fn({"IN": _lp(funding_4h=-2.5, oi=1_500_000.0)})
        alerts, _ = F.assess(cands, perp, baseline=bl, now=T0)
        paged = [a for a in alerts if a["page"]]
        self.assertEqual([a["ticker"] for a in paged], ["IN"])
        self.assertEqual(paged[0]["reason"], "oi-flip-rising")
        self.assertEqual(paged[0]["oi_context"], "rising")

    def test_below_band_not_tracked(self):
        cands = [_cand("MEH", funding_4h=-0.5)]
        perp = _perp_fn({"MEH": _lp(funding_4h=-0.5)})
        alerts, bl = F.assess(cands, perp, baseline={}, now=T0)
        self.assertEqual(alerts, [])
        self.assertNotIn("MEH", bl)


class TestRunTickCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "funding_baseline.json"
        self.log = Path(self.tmp.name) / "funding_alerts.log"

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_tick_writes_log_and_dedups(self):
        cands = [_cand("IN", funding_4h=-2.5, oi=1_000_000.0)]
        perp = _perp_fn({"IN": _lp(funding_4h=-2.5, oi=1_000_000.0)})
        events = []

        def emit(ts, ticker, source, severity, msg):
            events.append((ticker, severity, msg))

        r1 = F.run_tick(cands, perp, state_path=self.state, log_path=self.log,
                        now=T0, emit_fn=emit)
        self.assertEqual([t for t in r1["paged"]], ["IN"])
        self.assertEqual(len(events), 1)
        self.assertTrue(self.log.exists())
        self.assertIn("IN", self.log.read_text())

        # second tick: same set, persisted baseline → no new page, no new event
        r2 = F.run_tick(cands, perp, state_path=self.state, log_path=self.log,
                        now=T0, emit_fn=emit)
        self.assertEqual(r2["paged"], [])
        self.assertEqual(len(events), 1)


# ── SPEC-96: price-LOCATION classifier (resolve the §4 deep-neg+OI coin-flip) ─────
def _pc(price=0.070, low=0.0577, high=0.13, range_3d_pct=8.0, new_lows=False):
    """A raw price snapshot for the location classifier (price_structure-shaped)."""
    return {"price": price, "low": low, "high": high, "range_3d_pct": range_3d_pct,
            "new_lows": new_lows}


# canonical fixtures from the spec DoD
_TAIKO = _pc(price=0.070, low=0.0577, high=0.13, range_3d_pct=8.0, new_lows=False)   # held ATL, basing
_IN = _pc(price=0.80, low=0.20, high=0.82, range_3d_pct=30.0, new_lows=False)        # near ATH
_LAB = _pc(price=15.5, low=15.0, high=22.0, range_3d_pct=45.0, new_lows=False)       # near low, fresh cascade
_TAIKO_AFTER = _pc(price=0.13, low=0.0577, high=0.135, range_3d_pct=60.0, new_lows=False)  # post-vertical


class TestPriceLocationHelpers(unittest.TestCase):
    def test_pct_above_low(self):
        self.assertAlmostEqual(F.pct_above_low(0.070, 0.0577), 21.3, places=1)
        self.assertIsNone(F.pct_above_low(0.07, 0.0))
        self.assertIsNone(F.pct_above_low(None, 0.05))

    def test_pct_below_high(self):
        self.assertAlmostEqual(F.pct_below_high(0.80, 0.82), 2.4, places=1)
        self.assertIsNone(F.pct_below_high(1.0, 0.0))

    def test_near_held_low(self):
        # within 25% AND bounced ≥2% off it AND no fresh new lows
        self.assertTrue(F.is_near_held_low(0.070, 0.0577, new_lows=False))
        # sitting on a fresh-breaking low (still cascading) → not held
        self.assertFalse(F.is_near_held_low(0.058, 0.0577, new_lows=True))
        # too far above the low (>25%) → not "near"
        self.assertFalse(F.is_near_held_low(0.13, 0.0577, new_lows=False))

    def test_is_basing(self):
        self.assertTrue(F.is_basing(8.0, new_lows=False))    # tight range, no new lows
        self.assertFalse(F.is_basing(45.0, new_lows=False))  # wide range = still moving
        self.assertFalse(F.is_basing(8.0, new_lows=True))    # new lows = still cascading

    def test_price_context_derives_flags(self):
        pc = F.price_context(_TAIKO)
        self.assertTrue(pc["near_held_low"])
        self.assertTrue(pc["basing"])
        self.assertFalse(pc["near_high"])
        self.assertIsNone(F.price_context(None))


class TestClassifyLocation(unittest.TestCase):
    def test_taiko_like_is_squeeze_long(self):
        tag, sev, is_long = F.classify_location("rising", F.price_context(_TAIKO))
        self.assertIn("SQUEEZE-LONG", tag)
        self.assertIn("TAIKO", tag)
        self.assertEqual(sev, "HIGH")
        self.assertTrue(is_long)

    def test_in_like_near_high_is_ambiguous(self):
        tag, sev, is_long = F.classify_location("rising", F.price_context(_IN))
        self.assertIn("AMBIGUOUS", tag)
        self.assertFalse(is_long)

    def test_lab_like_near_low_not_basing_is_forming(self):
        tag, sev, is_long = F.classify_location("rising", F.price_context(_LAB))
        self.assertIn("FORMING", tag)
        self.assertFalse(is_long)
        self.assertEqual(sev, "WATCH")   # lower-severity watch

    def test_oi_flat_is_no_load(self):
        tag, sev, is_long = F.classify_location("flat", F.price_context(_TAIKO))
        self.assertEqual(tag, "no load")
        self.assertFalse(is_long)

    def test_oi_falling_is_no_load(self):
        tag, _sev, is_long = F.classify_location("falling", F.price_context(_TAIKO))
        self.assertEqual(tag, "no load")
        self.assertFalse(is_long)


class TestEnrichWithLocation(unittest.TestCase):
    def test_taiko_enrich_tags_squeeze_long_high(self):
        hit = F.enrich_hit(_cand("TAIKO", funding_4h=-1.5), _lp(funding_4h=-1.5),
                           oi_chg_pct=71.0, price_raw=_TAIKO)
        self.assertIn("SQUEEZE-LONG", hit["location_tag"])
        self.assertTrue(hit["is_squeeze_long"])
        self.assertTrue(hit["is_long_candidate"])
        self.assertEqual(hit["severity"], "HIGH")
        self.assertTrue(hit["near_held_low"])
        self.assertTrue(hit["basing"])

    def test_in_enrich_tags_ambiguous(self):
        hit = F.enrich_hit(_cand("IN", funding_4h=-2.5), _lp(funding_4h=-2.5),
                           oi_chg_pct=41.0, price_raw=_IN)
        self.assertIn("AMBIGUOUS", hit["location_tag"])
        self.assertFalse(hit["is_squeeze_long"])
        self.assertFalse(hit["is_long_candidate"])

    def test_lab_enrich_tags_forming_lower_severity(self):
        hit = F.enrich_hit(_cand("LAB", funding_4h=-1.0), _lp(funding_4h=-1.0),
                           oi_chg_pct=30.0, price_raw=_LAB)
        self.assertIn("FORMING", hit["location_tag"])
        self.assertEqual(hit["severity"], "WATCH")
        self.assertFalse(hit["is_long_candidate"])

    def test_oi_flat_enrich_is_no_load(self):
        hit = F.enrich_hit(_cand("MEH", funding_4h=-1.2), _lp(funding_4h=-1.2),
                           oi_chg_pct=2.0, price_raw=_TAIKO)
        self.assertEqual(hit["location_tag"], "no load")
        self.assertFalse(hit["is_long_candidate"])

    def test_msg_carries_location_tag(self):
        hit = F.enrich_hit(_cand("TAIKO", funding_4h=-1.5), _lp(funding_4h=-1.5),
                           oi_chg_pct=71.0, price_raw=_TAIKO)
        self.assertIn("SQUEEZE-LONG", hit["msg"])
        self.assertIn("held low", hit["msg"].lower())

    def test_no_price_raw_keeps_spec93_behaviour(self):
        # backward compat: without a price snapshot the SPEC-93 decode still drives the msg
        hit = F.enrich_hit(_cand("IN"), _lp(), oi_chg_pct=41.0)
        self.assertIn("§4", hit["msg"])
        self.assertTrue(hit["is_long_candidate"])
        self.assertIsNone(hit["location_tag"])


class TestBacktestTaiko(unittest.TestCase):
    """DoD back-test: the classifier tags SQUEEZE-LONG DURING the base, not after the vertical."""

    def test_during_base_is_squeeze_long(self):
        tag, _s, is_long = F.classify_location("rising", F.price_context(_TAIKO))
        self.assertIn("SQUEEZE-LONG", tag)
        self.assertTrue(is_long)

    def test_after_vertical_is_not_squeeze_long(self):
        # price ran +126% off the base → no longer near the held low, range wide
        tag, _s, is_long = F.classify_location("rising", F.price_context(_TAIKO_AFTER))
        self.assertNotIn("SQUEEZE-LONG", tag)
        self.assertFalse(is_long)


class TestAssessWiresPriceCtx(unittest.TestCase):
    def test_assess_passes_price_ctx_fn_through(self):
        cands = [_cand("TAIKO", funding_4h=-1.5, turnover_m=40.0, oi=1_000_000.0)]
        perp = _perp_fn({"TAIKO": _lp(funding_4h=-1.5, oi=1_710_000.0)})  # +71% OI vs baseline
        bl = {"TAIKO": {"funding_4h": -1.5, "oi": 1_000_000.0, "oi_context": "flat",
                        "tier": "tradeable", "first_seen": "2026-06-29T00:00:00Z"}}
        alerts, _ = F.assess(cands, perp, baseline=bl, now=T0,
                             price_ctx_fn=lambda tk: _TAIKO)
        self.assertEqual(len(alerts), 1)
        self.assertIn("SQUEEZE-LONG", alerts[0]["location_tag"])
        self.assertTrue(alerts[0]["is_squeeze_long"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
