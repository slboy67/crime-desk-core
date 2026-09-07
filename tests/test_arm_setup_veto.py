#!/usr/bin/env python3
"""SPEC 8 — arm_setup hard pre-ENTRY funding veto, in every trigger mode + units.

Run:  python3 tests/test_arm_setup_veto.py

arm_setup.main() is an infinite poll loop; the ENTRY decision is extracted into pure
functions (funding_veto / decide_entry / as_4h_threshold) which the loop calls in
EVERY mode. Tested directly — offline, deterministic.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("arm_setup", ROOT / "capabilities" / "arm_setup.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)
to_4h = A.to_4h


class TestFundingVeto(unittest.TestCase):
    def test_heldbreak_short_deepneg_does_not_enter(self):
        # LAB case: held-break SHORT, funding −1.985%/int on a 1h name = −7.94%/4h ≤ veto
        f4 = to_4h(-1.985, 60)
        self.assertLess(f4, A.VETO_4H)
        fire, msg = A.decide_entry(trig=True, short=True, funding_4h=f4)
        self.assertFalse(fire)                       # does NOT enter
        self.assertIn("FUNDING-VETO", msg)

    def test_short_enters_when_funding_cooled(self):
        # funding cooled to −0.10%/4h (above the −0.30 line) → DOES enter
        fire, msg = A.decide_entry(trig=True, short=True, funding_4h=-0.10)
        self.assertTrue(fire)
        self.assertIsNone(msg)

    def test_veto_applies_regardless_of_how_trig_reached(self):
        # the gate is on (trig, short, funding) — same for held-break/break/self-arming/now
        for f4 in (to_4h(-1.985, 60), -0.30, -0.31):
            self.assertFalse(A.decide_entry(True, True, f4)[0])

    def test_long_never_funding_vetoed(self):
        fire, msg = A.decide_entry(trig=True, short=False, funding_4h=to_4h(-1.985, 60))
        self.assertTrue(fire)
        self.assertIsNone(msg)

    def test_no_trig_no_entry_no_veto(self):
        self.assertEqual(A.decide_entry(trig=False, short=True, funding_4h=-9.0), (False, None))

    def test_boundary_exactly_at_line_vetoes(self):
        self.assertTrue(A.funding_veto(True, -0.30))     # ≤ −0.30 = vetoed
        self.assertFalse(A.funding_veto(True, -0.29))


class TestUnits(unittest.TestCase):
    def test_funding_guard_minus_030_is_4h_not_raw(self):
        self.assertEqual(A.as_4h_threshold(-0.30), -0.30)   # already %/4h

    def test_obviously_raw_decimal_autoscaled(self):
        self.assertAlmostEqual(A.as_4h_threshold(-0.003), -0.30, places=3)  # raw → %/4h

    def test_none_passes_through(self):
        self.assertIsNone(A.as_4h_threshold(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
