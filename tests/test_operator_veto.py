#!/usr/bin/env python3
"""SPEC-149 — the counterparty read becomes a LIVE veto field: operator_not_done.

The grill (2026-08-19 Q12/Q13) settled the semantics: "distribution confirmed on-chain
=> short it" is close to backwards at entry timescale. An operator who is actively
distributing still needs liquidity to sell into, and squeezing is how they manufacture
it (BEAT +81%, SKYAI +39%, both AFTER distribution fired, both through the stop). So:

  operator_not_done: true   -> VETO the short (the machine is still running)
  operator_not_done: false  -> short unlocks (the structure event fired)
  operator_not_done: "unknown" -> a stale/unreadable read; blocks nothing

Offline-deterministic: pure functions only, no network/state I/O in this file.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))


def _load(name, alias=None):
    spec = importlib.util.spec_from_file_location(alias or name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


OV = _load("operator_veto")

NOW = 1_780_000_000.0
H = 3600.0
D = 86400.0


class TestClassifyOperatorNotDone(unittest.TestCase):
    def test_fresh_distribution_is_veto_true(self):
        r = OV.classify("FRESH", now=NOW, evidence_ts=NOW - 1 * H)
        self.assertIs(r["operator_not_done"], True)
        self.assertIn("FRESH", r["reason"])

    def test_frozen_but_rotated_is_veto_true(self):
        r = OV.classify("ROTATED", now=NOW, evidence_ts=NOW - 1 * H,
                        frozen_since_ts=NOW - 30 * H)
        self.assertIs(r["operator_not_done"], True)
        self.assertIn("ROTATED", r["reason"])

    def test_frozen_under_24h_floor_blocks_unlock_true(self):
        r = OV.classify("FROZEN", now=NOW, evidence_ts=NOW - 1 * H,
                        frozen_since_ts=NOW - 10 * H, breakdown_hold=True)
        self.assertIs(r["operator_not_done"], True)
        self.assertIn("floor", r["reason"].lower())

    def test_frozen_over_24h_no_breakdown_hold_is_unknown_not_false(self):
        r = OV.classify("FROZEN", now=NOW, evidence_ts=NOW - 1 * H,
                        frozen_since_ts=NOW - 40 * H, breakdown_hold=False)
        self.assertEqual(r["operator_not_done"], "unknown")
        self.assertNotEqual(r["operator_not_done"], False)

    def test_frozen_over_24h_with_breakdown_hold_unlocks_false(self):
        r = OV.classify("FROZEN", now=NOW, evidence_ts=NOW - 1 * H,
                        frozen_since_ts=NOW - 40 * H, breakdown_hold=True)
        self.assertIs(r["operator_not_done"], False)
        self.assertIn("breakdown-hold", r["reason"])

    def test_squeeze_leg_within_cadence_still_blocks_true_even_with_breakdown_hold(self):
        r = OV.classify("FROZEN", now=NOW, evidence_ts=NOW - 1 * H,
                        frozen_since_ts=NOW - 40 * H, breakdown_hold=True,
                        squeeze_off=False)
        self.assertIs(r["operator_not_done"], True)
        self.assertIn("cadence", r["reason"])

    def test_stale_evidence_downgrades_to_unknown_no_prefix_age_stated(self):
        # Would otherwise be a clean veto=true (FRESH) but the evidence itself is stale.
        r = OV.classify("FRESH", now=NOW, evidence_ts=NOW - 6 * H)
        self.assertEqual(r["operator_not_done"], "unknown")
        self.assertIn("6", r["reason"])
        self.assertEqual(r["evidence_age_h"], 6.0)

    def test_evidence_within_window_is_not_stale(self):
        r = OV.classify("FRESH", now=NOW, evidence_ts=NOW - 3.9 * H)
        self.assertIs(r["operator_not_done"], True)

    def test_no_rotation_verdict_is_unknown(self):
        r = OV.classify(None, now=NOW)
        self.assertEqual(r["operator_not_done"], "unknown")

    def test_frozen_missing_since_ts_treated_conservatively_as_true(self):
        # Unknown frozen duration must never be silently treated as "long enough" —
        # conservative default is the 24h-floor branch (true), never false/unknown.
        r = OV.classify("FROZEN", now=NOW, evidence_ts=NOW - 1 * H,
                        frozen_since_ts=None, breakdown_hold=True)
        self.assertIs(r["operator_not_done"], True)


class TestSqueezeCadence(unittest.TestCase):
    def test_nine_day_gap_threshold_is_eighteen_days(self):
        legs = ["2026-07-01", "2026-07-10"]  # 9 days apart
        thr = OV.squeeze_cadence_threshold_days(legs)
        self.assertAlmostEqual(thr, 18.0, places=3)

    def test_off_only_after_eighteen_quiet_days(self):
        legs = ["2026-07-01", "2026-07-10"]
        last_epoch = OV._to_epoch("2026-07-10")
        # 17 days quiet: still ON (machine not off yet)
        self.assertFalse(OV.squeeze_machine_off(legs, now=last_epoch + 17 * D))
        # 18 days quiet: OFF
        self.assertTrue(OV.squeeze_machine_off(legs, now=last_epoch + 18 * D))

    def test_four_legs_in_four_days_threshold_is_about_two_days(self):
        legs = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"]
        thr = OV.squeeze_cadence_threshold_days(legs)
        self.assertAlmostEqual(thr, 2.0, places=3)
        last_epoch = OV._to_epoch("2026-07-04")
        self.assertFalse(OV.squeeze_machine_off(legs, now=last_epoch + 1 * D))
        self.assertTrue(OV.squeeze_machine_off(legs, now=last_epoch + 2 * D))

    def test_threshold_is_derived_never_a_constant(self):
        # Two very different cadences must produce two very different thresholds —
        # proves the number is computed from the name's own leg dates, not hardcoded.
        slow = OV.squeeze_cadence_threshold_days(["2026-01-01", "2026-01-10"])
        fast = OV.squeeze_cadence_threshold_days(["2026-01-01", "2026-01-02"])
        self.assertNotEqual(slow, fast)
        self.assertGreater(slow, fast)

    def test_fewer_than_two_legs_has_no_derivable_threshold(self):
        self.assertIsNone(OV.squeeze_cadence_threshold_days([]))
        self.assertIsNone(OV.squeeze_cadence_threshold_days(["2026-01-01"]))

    def test_no_leg_history_machine_off_defaults_true(self):
        # Nothing to block on — a name with no squeeze-leg history at all never blocks
        # the unlock on this signal alone.
        self.assertTrue(OV.squeeze_machine_off([]))


class TestBuildStateAndPaging(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.events = []

    def tearDown(self):
        self._tmp.cleanup()

    def _event_fn(self, ts, ticker, source, severity, msg):
        self.events.append({"ts": ts, "ticker": ticker, "source": source,
                            "severity": severity, "msg": msg})

    def test_true_to_false_transition_fires_exactly_one_page(self):
        OV.build("XYZ", "FRESH", now=NOW, evidence_ts=NOW - 1 * H,
                state_dir=self.state_dir, event_fn=self._event_fn)
        self.assertEqual(len(self.events), 0)   # first read, no prior state -> no transition
        r = OV.build("XYZ", "FROZEN", now=NOW + 100, evidence_ts=NOW + 99,
                     frozen_since_ts=NOW - 40 * H, breakdown_hold=True,
                     state_dir=self.state_dir, event_fn=self._event_fn)
        self.assertIs(r["operator_not_done"], False)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["severity"], "HIGH")
        self.assertIn("XYZ", self.events[0]["msg"])

    def test_false_to_false_fires_no_page(self):
        OV.build("XYZ", "FROZEN", now=NOW, evidence_ts=NOW - 1 * H,
                frozen_since_ts=NOW - 40 * H, breakdown_hold=True,
                state_dir=self.state_dir, event_fn=self._event_fn)
        self.events.clear()
        r = OV.build("XYZ", "FROZEN", now=NOW + 100, evidence_ts=NOW + 99,
                     frozen_since_ts=NOW - 40 * H, breakdown_hold=True,
                     state_dir=self.state_dir, event_fn=self._event_fn)
        self.assertIs(r["operator_not_done"], False)
        self.assertEqual(len(self.events), 0)

    def test_true_to_true_fires_no_page(self):
        OV.build("XYZ", "FRESH", now=NOW, evidence_ts=NOW - 1 * H,
                state_dir=self.state_dir, event_fn=self._event_fn)
        self.events.clear()
        OV.build("XYZ", "FRESH", now=NOW + 100, evidence_ts=NOW + 99,
                state_dir=self.state_dir, event_fn=self._event_fn)
        self.assertEqual(len(self.events), 0)


class TestSweep(unittest.TestCase):
    """Req 6: the freshness stack must run for every board row on the discovery tick,
    not only inside an on-demand `brief` gated on a live SHORT thesis. A minimal sweep
    entry point suffices — this proves it runs headless over a fixture board with no
    live SHORT thesis anywhere."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sweep_runs_headless_with_no_live_short_thesis(self):
        freshness_fn = lambda ticker, now: {"verdict": "FROZEN", "evidence_ts": now - H,  # noqa: E731
                                            "frozen_since_ts": now - 40 * H}
        out = OV.sweep(["AAA", "BBB"], now=NOW, state_dir=self.state_dir,
                       fire=False, freshness_fn=freshness_fn,
                       breakdown_hold_fn=lambda t: False,
                       squeeze_legs_fn=lambda t: [])
        self.assertEqual(set(out.keys()), {"AAA", "BBB"})
        for tk, r in out.items():
            self.assertIn(r["operator_not_done"], (True, False, "unknown"))


if __name__ == "__main__":
    unittest.main()
