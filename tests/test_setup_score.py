#!/usr/bin/env python3
"""SPEC 59 — setup_score: the §6 setup checklists as deterministic box-counters.

The fixtures reproduce this week's hand counts (the desk's own scoring):
  - BEAT 06-11 blowoff      = 4/5, FORMING (missing the un-re-bought break)
  - PLAY trap_long          = VETOED (oi_sides WASH)
  - ID 06-10 neg_funding    = 2/4, FORMING (missing price trigger + spot CVD)
  - FOLKS 06-12 catb_top    = >=4, ARMED (retest fail)

setup_score is a SCORER, not a caller — `score_all`/`score_setup` take a flat
normalized `signals` snapshot (assembled live by gather_signals, injected here).
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import setup_score  # noqa: E402


# ── fixtures: this week's hand counts ───────────────────────────────────────────
BEAT_BLOWOFF = {
    # 4/5: parabolic, ATH wick, lower-high, OI off highs all present; the intraday
    # break got re-bought within a candle → the un-re-bought-break leg fails.
    "parabolic_pct": 73.0,
    "window_high_wick_pct": 3.1,
    "lower_high": True,
    "intraday_break": {"broke": True, "vol_mult": 1.8, "rebought": True},
    "oi_off_highs": True,
    "funding_cooling": True,
    "oi_sides_tag": "REAL_DIRECTIONAL",
    "squeeze_chronic": False,
}

PLAY_TRAP_LONG = {
    # multi-sigma-neg funding + OI spiking present, but oi_sides reads WASH → VETO.
    "multi_sigma_neg": True,
    "oi_spiking": True,
    "price_trigger": True,
    "spot_cvd_up": True,
    "oi_sides_tag": "WASH",
}

ID_NEG_FUNDING = {
    # 2/4: multi-sigma-neg funding + OI spiking (real) present; no magnet
    # sweep-reclaim trigger and spot CVD not diverging up → 2 legs missing.
    "multi_sigma_neg": True,
    "oi_spiking": True,
    "price_trigger": False,
    "spot_cvd_up": False,
    "oi_sides_tag": "REAL_DIRECTIONAL",
}

FOLKS_CATB_TOP = {
    # >=4 of the catb_top legs at the 06-12 retest fail.
    "ath_wick": True,
    "lower_high": True,
    "ls_top_drop_pct": 19.0,        # in the 15-25% band
    "oi_peaked_rolled": True,
    "volume_declining": False,
    "funding_cooling": False,
}


class TestSetupScore(unittest.TestCase):
    def test_beat_blowoff_is_4of5_scout_missing_unrebought_break(self):
        # SPEC-82: 4/5 (one leg short of ARMED) now surfaces as SCOUT, not FORMING —
        # the near-armed ramp is made visible; the missing leg becomes an add-trigger.
        r = setup_score.score_setup("blowoff", BEAT_BLOWOFF)
        self.assertEqual(r["required"], 5)
        self.assertEqual(r["score"], 4)
        self.assertEqual(r["verdict"], "SCOUT")
        self.assertEqual(r["tier"], "scout")
        # the failing leg is the clean (un-re-bought) intraday break, now an add-trigger
        self.assertIn("clean_break", r["missing"])
        self.assertIn("clean_break", r["add_triggers"])
        # every leg names a source + value (auditable in one glance)
        for name, leg in r["legs"].items():
            self.assertIn("pass", leg)
            self.assertIn("value", leg)
            self.assertIn("source", leg)
            self.assertTrue(leg["source"], f"{name} has empty source")

    def test_play_trap_long_vetoed_by_wash(self):
        r = setup_score.score_setup("trap_long", PLAY_TRAP_LONG)
        self.assertEqual(r["verdict"], "VETOED")
        self.assertTrue(any("wash" in v.lower() for v in r["vetoes"]),
                        f"expected a wash veto, got {r['vetoes']}")

    def test_neg_funding_gate_alias_matches_trap_long(self):
        a = setup_score.score_setup("trap_long", ID_NEG_FUNDING)
        b = setup_score.score_setup("neg_funding_gate", ID_NEG_FUNDING)
        self.assertEqual(a["legs"].keys(), b["legs"].keys())
        self.assertEqual(a["score"], b["score"])

    def test_id_neg_funding_is_2of4_missing_trigger_and_cvd(self):
        r = setup_score.score_setup("neg_funding_gate", ID_NEG_FUNDING)
        self.assertEqual(r["required"], 4)
        self.assertEqual(r["score"], 2)
        self.assertEqual(r["verdict"], "FORMING")
        self.assertIn("price_trigger", r["missing"])
        self.assertIn("spot_cvd_up", r["missing"])

    def test_folks_catb_top_armed_at_4plus(self):
        r = setup_score.score_setup("catb_top", FOLKS_CATB_TOP)
        self.assertEqual(r["required"], 4)
        self.assertGreaterEqual(r["score"], 4)
        self.assertEqual(r["verdict"], "ARMED")

    def test_score_all_returns_every_setup(self):
        out = setup_score.score_all(BEAT_BLOWOFF)
        self.assertEqual(set(out.keys()),
                         {"blowoff", "catb_top", "trap_long", "neg_funding_gate", "stage45_short",
                          "defended_fade_short", "defended_fade_long"})

    def test_absent_when_no_legs_pass(self):
        r = setup_score.score_setup("catb_top", {})
        self.assertEqual(r["score"], 0)
        self.assertEqual(r["verdict"], "ABSENT")

    def test_no_trade_recommendation_emitted(self):
        # NO trade calls — scores only. No direction/recommendation/action key anywhere.
        r = setup_score.score_setup("blowoff", BEAT_BLOWOFF)
        for banned in ("recommendation", "action", "call", "trade"):
            self.assertNotIn(banned, r)


class TestScoutTier(unittest.TestCase):
    """SPEC-82 — surface FORMING setups as a graded SCOUT tier; missing legs = add_triggers."""

    def test_grade_verdict_scout_at_required_minus_one(self):
        # pure grader: 3/4, no veto → SCOUT (one leg short of ARMED)
        verdict, tier = setup_score.grade_verdict(3, 4, hard_vetoes=[], swing_vetoes=[])
        self.assertEqual(verdict, "SCOUT")
        self.assertEqual(tier, "scout")

    def test_scout_setup_lists_add_triggers_and_ticked_legs(self):
        # trap_long 3/4 with the funding leg (multi_sigma_neg) missing, no veto → SCOUT,
        # add_triggers = [the missing funding leg]; the ticked legs are listed.
        s = {"multi_sigma_neg": False, "oi_spiking": True, "price_trigger": True,
             "spot_cvd_up": True, "oi_sides_tag": "REAL_DIRECTIONAL"}
        r = setup_score.score_setup("trap_long", s)
        self.assertEqual(r["score"], 3)
        self.assertEqual(r["required"], 4)
        self.assertEqual(r["verdict"], "SCOUT")
        self.assertEqual(r["add_triggers"], ["multi_sigma_neg"])
        # ticked legs are listed and marked pass
        self.assertTrue(r["legs"]["price_trigger"]["pass"])
        self.assertTrue(r["legs"]["spot_cvd_up"]["pass"])

    def test_deepneg_short_veto_suppresses_scout(self):
        # SPEC-82 §3: a §5 deep-neg-funding SHORT stays VETOED even at near-armed score.
        s = {"oi_drop_ls_unfreeze": True, "cex_deposits_firing": True, "lower_high": True,
             "breakdown_not_bought_back": False, "funding_phase_match": False,
             "funding_4h": -0.50}
        r = setup_score.score_setup("stage45_short", s)
        self.assertEqual(r["score"], 3)              # would be SCOUT on score alone
        self.assertEqual(r["verdict"], "VETOED")
        self.assertNotEqual(r["tier"], "scout")
        self.assertTrue(any("§5" in v or "deep-neg" in v.lower() for v in r["vetoes"]),
                        f"expected a §5 deep-neg veto, got {r['vetoes']}")

    def test_below_threshold_stays_quiet(self):
        # 1/4 (below required-1) → FORMING, no SCOUT spam.
        s = {"multi_sigma_neg": True, "oi_spiking": False, "price_trigger": False,
             "spot_cvd_up": False, "oi_sides_tag": "REAL_DIRECTIONAL"}
        r = setup_score.score_setup("trap_long", s)
        self.assertEqual(r["score"], 1)
        self.assertEqual(r["verdict"], "FORMING")
        self.assertIsNone(r["tier"])

    def test_chronic_squeezer_short_is_scout_not_swing_suppressed(self):
        # SPEC-82 §3 + [[feedback_squeezer_scalp_not_no_trade]]: a chronic-squeezer name,
        # near-armed short → SCOUT (scalp-allowed), NOT hard-vetoed; the swing veto is
        # surfaced as a note, not a suppression.
        s = dict(BEAT_BLOWOFF)
        s["squeeze_chronic"] = True
        r = setup_score.score_setup("blowoff", s)
        self.assertEqual(r["score"], 4)
        self.assertEqual(r["verdict"], "SCOUT")
        self.assertEqual(r["vetoes"], [])           # NOT hard-vetoed
        self.assertTrue(r["swing_vetoes"], "swing veto should be surfaced as a note")
        self.assertTrue(any("squeez" in v.lower() for v in r["swing_vetoes"]))

    def test_armed_demotes_to_scout_under_swing_veto(self):
        # A full-confluence ARMED short on a chronic squeezer is a scalp, not a swing →
        # SCOUT (swing-vetoed), not ARMED.
        s = {"parabolic_pct": 73.0, "window_high_wick_pct": 3.1, "lower_high": True,
             "intraday_break": {"broke": True, "vol_mult": 1.8, "rebought": False},
             "oi_off_highs": True, "oi_sides_tag": "REAL_DIRECTIONAL",
             "squeeze_chronic": True}
        r = setup_score.score_setup("blowoff", s)
        self.assertGreaterEqual(r["score"], r["required"])
        self.assertEqual(r["verdict"], "SCOUT")
        self.assertTrue(r["swing_vetoes"])


class TestOiConstructionAnnotation(unittest.TestCase):
    """SPEC-180 req 2 — annotation only, never a gate/score change."""

    def test_no_oi_construction_no_notes(self):
        notes = setup_score.oi_construction_annotation({}, {"oi_peaked_rolled"})
        self.assertEqual(notes, [])

    def test_no_fired_oi_drop_leg_no_notes_even_with_oic(self):
        oic = {"oi_types": [{"type": "FUNDING_FARM", "on_carry_decay": True}]}
        notes = setup_score.oi_construction_annotation({"oi_construction": oic}, set())
        self.assertEqual(notes, [])

    def test_on_carry_decay_fires_farm_unwind_note(self):
        oic = {"oi_types": [{"type": "FUNDING_FARM", "on_carry_decay": True}], "aggregate": {}}
        notes = setup_score.oi_construction_annotation(
            {"oi_construction": oic}, {"oi_peaked_rolled"})
        self.assertTrue(any("FUNDING_FARM unwind" in n for n in notes))

    def test_overstated_by_cross_venue_fires_upper_bound_note(self):
        oic = {"oi_types": [], "aggregate": {"overstated_by_cross_venue": True}}
        notes = setup_score.oi_construction_annotation(
            {"oi_construction": oic}, {"oi_drop_ls_unfreeze"})
        self.assertTrue(any("upper bound" in n for n in notes))

    def test_score_setup_never_gated_by_oic_notes(self):
        s = dict(BEAT_BLOWOFF)
        s["oi_construction"] = {"oi_types": [{"type": "FUNDING_FARM", "on_carry_decay": True}],
                                "aggregate": {"overstated_by_cross_venue": True}}
        r_with = setup_score.score_setup("blowoff", s)
        r_without = setup_score.score_setup("blowoff", BEAT_BLOWOFF)
        self.assertEqual(r_with["score"], r_without["score"])
        self.assertEqual(r_with["verdict"], r_without["verdict"])
        self.assertIn("oi_construction_notes", r_with)
        self.assertNotIn("oi_construction_notes", r_without)


class TestSetupScoreCLI(unittest.TestCase):
    def test_cli_scores_from_signals_blob_offline(self):
        # --signals lets the scorer run with no network (the test path / fixture replay).
        out = subprocess.run(
            [sys.executable, str(ROOT / "capabilities" / "setup_score.py"),
             json.dumps({"setup": "catb_top", "signals": FOLKS_CATB_TOP}), "--json"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        d = json.loads(out.stdout)
        self.assertEqual(d["catb_top"]["verdict"], "ARMED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
