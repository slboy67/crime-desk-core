#!/usr/bin/env python3
"""SPEC-74 — normalized volume-climax confirmation leg for the blowoff/Stage-5 top.

A trader heuristic proposed ABSOLUTE Binance-futures-volume topping bands (4h $300-700M),
but tested live they are ~5-10x too high for the desk's low-float micro-caps. The
RELATIVE climax was unmistakable though: BSB 4h vol ~$3-9M baseline -> $82M at the top
(~10-25x) then collapsed to $4M. This codifies the relative metric (primary, micro-cap)
plus a cap-gated absolute tier (second, never gates a micro-cap), as a CONFIRMATION leg
that never arms a short on its own.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import setup_score  # noqa: E402


# ── fixtures (volumes in USD) ───────────────────────────────────────────────────
# BSB 0.4222 blowoff: baseline ~$6M 4h -> $82M peak (~13.7x) then collapsed to $4M.
BSB_TOP = {
    "parabolic_pct": 110.0,
    "vol_4h_baseline": 6_000_000,
    "vol_4h_peak": 82_000_000,
    "vol_4h_current": 4_000_000,
    "mc_usd": 60_000_000,          # micro-cap -> absolute bands never apply
}

# Steady-volume pump: still parabolic, but volume only ~2x baseline and no collapse.
STEADY_PUMP = {
    "parabolic_pct": 90.0,
    "vol_4h_baseline": 6_000_000,
    "vol_4h_peak": 12_000_000,
    "vol_4h_current": 10_000_000,
    "mc_usd": 60_000_000,
}

# Climax ratio present but NOT rolled over yet (peak == current) -> don't fire mid-pump.
CLIMAX_NO_ROLLOVER = {
    "parabolic_pct": 110.0,
    "vol_4h_baseline": 6_000_000,
    "vol_4h_peak": 82_000_000,
    "vol_4h_current": 80_000_000,
    "mc_usd": 60_000_000,
}

# Big spike but NOT parabolic -> the leg only matters while pumping hard.
NOT_PARABOLIC = {
    "parabolic_pct": 5.0,
    "vol_4h_baseline": 6_000_000,
    "vol_4h_peak": 82_000_000,
    "vol_4h_current": 4_000_000,
    "mc_usd": 60_000_000,
}

# Large-cap perp that DOES reach the absolute band (4h peak >= $300M) on a relative
# spike too small to qualify -> the absolute tier fires.
LARGECAP_ABS = {
    "parabolic_pct": 80.0,
    "vol_4h_baseline": 200_000_000,
    "vol_4h_peak": 400_000_000,    # >= $300M lo band
    "vol_4h_current": 150_000_000,
    "mc_usd": 5_000_000_000,       # large enough to plausibly reach the bands
}

# Same absolute peak but a micro-cap MC -> absolute bands must NOT gate it.
MICROCAP_ABS_PEAK = {
    "parabolic_pct": 80.0,
    "vol_4h_baseline": 200_000_000,
    "vol_4h_peak": 400_000_000,
    "vol_4h_current": 150_000_000,
    "mc_usd": 10_000_000,          # micro-cap
}


class TestVolumeClimax(unittest.TestCase):
    def test_bsb_top_relative_climax_fires(self):
        vc = setup_score.volume_climax(BSB_TOP)
        self.assertTrue(vc["fires"])
        self.assertEqual(vc["tier"], "relative")
        self.assertGreaterEqual(vc["ratio"], 10.0)
        self.assertLessEqual(vc["ratio"], 25.0)
        self.assertTrue(vc["rollover"])

    def test_steady_pump_does_not_fire(self):
        vc = setup_score.volume_climax(STEADY_PUMP)
        self.assertFalse(vc["fires"])

    def test_climax_without_rollover_does_not_fire_midpump(self):
        vc = setup_score.volume_climax(CLIMAX_NO_ROLLOVER)
        self.assertGreaterEqual(vc["ratio"], 10.0)   # ratio is there
        self.assertFalse(vc["rollover"])             # but no collapse yet
        self.assertFalse(vc["fires"])

    def test_not_parabolic_does_not_fire(self):
        vc = setup_score.volume_climax(NOT_PARABOLIC)
        self.assertFalse(vc["fires"])

    def test_largecap_absolute_tier_fires(self):
        vc = setup_score.volume_climax(LARGECAP_ABS)
        self.assertTrue(vc["fires"])
        self.assertEqual(vc["tier"], "absolute")
        self.assertTrue(vc["absolute_eligible"])

    def test_microcap_absolute_band_never_gates(self):
        vc = setup_score.volume_climax(MICROCAP_ABS_PEAK)
        self.assertFalse(vc["absolute_eligible"])
        self.assertFalse(vc["absolute_fires"])
        # relative tier doesn't qualify here either (ratio 2x) -> no fire
        self.assertFalse(vc["fires"])

    def test_attached_to_blowoff_score_as_confirmation(self):
        r = setup_score.score_setup("blowoff", BSB_TOP)
        self.assertIn("volume_climax", r)
        self.assertTrue(r["volume_climax"]["fires"])
        # confirmation only — never arms a short on its own / no trade call key
        for banned in ("recommendation", "action", "call", "trade"):
            self.assertNotIn(banned, r)

    def test_volume_climax_does_not_change_blowoff_score(self):
        # The leg is a confirmation field, NOT a counted/required leg — score & required
        # are unchanged by it (it must not by itself arm or block a short).
        r = setup_score.score_setup("blowoff", BSB_TOP)
        self.assertEqual(r["required"], 5)
        self.assertEqual(len(r["legs"]), 5)
        self.assertNotIn("volume_climax", r["legs"])


class TestVolumeClimaxCLI(unittest.TestCase):
    def test_cli_exposes_volume_climax_offline(self):
        out = subprocess.run(
            [sys.executable, str(ROOT / "capabilities" / "setup_score.py"),
             json.dumps({"setup": "blowoff", "signals": BSB_TOP}), "--json"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        d = json.loads(out.stdout)
        self.assertIn("volume_climax", d["blowoff"])
        self.assertTrue(d["blowoff"]["volume_climax"]["fires"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
