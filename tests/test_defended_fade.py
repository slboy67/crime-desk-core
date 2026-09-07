#!/usr/bin/env python3
"""SPEC-83 — defended_fade: the wall-fade entry (growing wall + empty beyond + stalled
lower-high) scored WITHOUT requiring the cascade. The size-able squeezer entry is the
defended extreme with a stop above empty liquidity, not the breakdown chase.

Pure scorer + wall-dynamics derivation (offline; no network).
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import setup_score  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────────
SHORT_LEVEL = {  # the LEVEL read only — no cascade legs yet
    "wall_growing": True,
    "empty_beyond": True,
    "stall_lower_high": True,
    "wall_price": 18.0,
    "funding_4h": +0.02,   # not deep-neg
}


class TestDefendedFadeScorer(unittest.TestCase):
    def test_short_arms_on_the_level_read_alone(self):
        r = setup_score.score_setup("defended_fade_short", SHORT_LEVEL)
        self.assertEqual(r["verdict"], "ARMED")
        self.assertEqual(r["score"], r["required"])
        # the cascade legs are NOT required — they ride as add_triggers (the size-up)
        self.assertIn("multi_sigma_neg", r["add_triggers"])
        self.assertIn("breakdown_not_bought_back", r["add_triggers"])
        self.assertIn("cex_deposits_firing", r["add_triggers"])
        # entry=level, stop=beyond the wall (into the vacuum above)
        geo = r["geometry"]
        self.assertEqual(geo["entry"], 18.0)
        self.assertGreater(geo["stop"], 18.0)
        self.assertEqual(geo["side"], "short")

    def test_cluster_above_wall_does_not_arm(self):
        # empty_beyond false → a cluster sits above the wall, the fade stop would be IN the
        # fuel → NOT armed (the squeeze location isn't safe). [[feedback_safe_short_is_stop_above_empty_liquidity]]
        s = dict(SHORT_LEVEL, empty_beyond=False)
        r = setup_score.score_setup("defended_fade_short", s)
        self.assertNotEqual(r["verdict"], "ARMED")

    def test_flat_or_shrinking_wall_does_not_arm(self):
        s = dict(SHORT_LEVEL, wall_growing=False)
        r = setup_score.score_setup("defended_fade_short", s)
        self.assertNotEqual(r["verdict"], "ARMED")

    def test_deepneg_funding_vetoes_regardless_of_level(self):
        s = dict(SHORT_LEVEL, funding_4h=-0.80)
        r = setup_score.score_setup("defended_fade_short", s)
        self.assertEqual(r["verdict"], "VETOED")
        self.assertTrue(any("§5" in v or "deep-neg" in v.lower() for v in r["vetoes"]))

    def test_cascade_add_present_moves_out_of_add_triggers(self):
        # when a cascade leg DOES fire it's a confirmed add, not an absent add_trigger
        s = dict(SHORT_LEVEL, multi_sigma_neg=True)
        r = setup_score.score_setup("defended_fade_short", s)
        self.assertEqual(r["verdict"], "ARMED")
        self.assertNotIn("multi_sigma_neg", r["add_triggers"])
        self.assertTrue(r["add_legs"]["multi_sigma_neg"]["pass"])

    def test_long_mirror_arms_on_growing_floor_with_vacuum_below(self):
        s = {"wall_growing": True, "empty_beyond": True, "stall_higher_low": True,
             "wall_price": 12.0, "funding_4h": -0.80}   # long is never funding-vetoed (§5)
        r = setup_score.score_setup("defended_fade_long", s)
        self.assertEqual(r["verdict"], "ARMED")
        geo = r["geometry"]
        self.assertEqual(geo["entry"], 12.0)
        self.assertLess(geo["stop"], 12.0)             # stop BELOW the floor into the vacuum
        self.assertEqual(geo["side"], "long")

    def test_chronic_squeezer_short_is_scout_not_swing_suppressed(self):
        # this setup IS the scout instrument for squeezers — the squeezer veto must NOT
        # silence it; an ARMED level read demotes to SCOUT (scalp/scout-allowed).
        s = dict(SHORT_LEVEL, squeeze_chronic=True)
        r = setup_score.score_setup("defended_fade_short", s)
        self.assertEqual(r["verdict"], "SCOUT")
        self.assertEqual(r["vetoes"], [])
        self.assertTrue(r["swing_vetoes"])

    def test_defended_fade_in_score_all(self):
        out = setup_score.score_all(SHORT_LEVEL)
        self.assertIn("defended_fade_short", out)
        self.assertIn("defended_fade_long", out)


class TestWallDynamics(unittest.TestCase):
    def test_growing_wall_at_held_price_is_true(self):
        snaps = [{"price": 18.00, "notional": 295_000, "ts": 1},
                 {"price": 18.05, "notional": 320_000, "ts": 2},
                 {"price": 17.98, "notional": 352_000, "ts": 3}]
        self.assertTrue(setup_score.derive_wall_growing(snaps))

    def test_rising_notional_while_price_walks_away_is_false(self):
        # notional rises but price drifts off the level → the wall isn't being TESTED
        snaps = [{"price": 18.0, "notional": 295_000, "ts": 1},
                 {"price": 17.0, "notional": 320_000, "ts": 2},
                 {"price": 15.0, "notional": 352_000, "ts": 3}]
        self.assertFalse(setup_score.derive_wall_growing(snaps))

    def test_shrinking_wall_is_false(self):
        snaps = [{"price": 18.0, "notional": 352_000, "ts": 1},
                 {"price": 18.0, "notional": 320_000, "ts": 2},
                 {"price": 18.0, "notional": 295_000, "ts": 3}]
        self.assertFalse(setup_score.derive_wall_growing(snaps))

    def test_single_snapshot_cannot_grow(self):
        self.assertFalse(setup_score.derive_wall_growing(
            [{"price": 18.0, "notional": 295_000, "ts": 1}]))

    def test_record_snapshot_persists_and_trims(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "wall_history.json"
            last = None
            for i, notl in enumerate((295_000, 320_000, 352_000)):
                last = setup_score.record_wall_snapshot(
                    "LAB", "ask", price=18.0, notional=notl, ts=i, path=path)
            self.assertEqual([s["notional"] for s in last], [295_000, 320_000, 352_000])
            self.assertTrue(setup_score.derive_wall_growing(last))
            # bounded: never exceeds the cap
            for i in range(50):
                last = setup_score.record_wall_snapshot(
                    "LAB", "ask", price=18.0, notional=400_000 + i, ts=100 + i, path=path)
            self.assertLessEqual(len(last), setup_score.WALL_HISTORY_MAX)


if __name__ == "__main__":
    unittest.main(verbosity=2)
