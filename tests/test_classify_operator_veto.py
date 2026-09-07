#!/usr/bin/env python3
"""SPEC-149 — classify.py board-row wiring: operator_not_done veto prefix + tier.

Offline-deterministic: operator_veto.sweep is driven through injected freshness_fn /
event_fn so nothing here touches the network or real state/ files.
"""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import classify as CL
import ledger as LG


class AnnotateOperatorVetoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_veto_true_prefixes_reason_never_changes_verdict(self):
        rows = [{"ticker": "ABC", "verdict": "CONFIRMS", "reason": "no signals"}]
        fresh = lambda ticker, now: {"verdict": "FRESH", "evidence_ts": now - 3600,  # noqa: E731
                                     "frozen_since_ts": None}
        CL.annotate_operator_veto(rows, state_dir=self.state_dir, fire=False,
                                  freshness_fn=fresh)
        self.assertIs(rows[0]["operator_not_done"], True)
        self.assertTrue(rows[0]["reason"].startswith("⛔ VETO —"))
        self.assertEqual(rows[0]["verdict"], "CONFIRMS")   # advisory only — never touches verdict

    def test_veto_unknown_leaves_reason_untouched(self):
        rows = [{"ticker": "ABC", "verdict": "CONFIRMS", "reason": "no signals"}]
        fresh = lambda ticker, now: {"verdict": None, "evidence_ts": None, "frozen_since_ts": None}  # noqa: E731
        CL.annotate_operator_veto(rows, state_dir=self.state_dir, fire=False,
                                  freshness_fn=fresh)
        self.assertEqual(rows[0]["operator_not_done"], "unknown")
        self.assertEqual(rows[0]["reason"], "no signals")

    def test_transition_fires_one_event_via_injected_event_fn(self):
        events = []
        def event_fn(ts, ticker, source, severity, msg):
            events.append((ticker, severity, msg))
        fresh_first = lambda ticker, now: {"verdict": "FRESH", "evidence_ts": now - 3600,  # noqa: E731
                                           "frozen_since_ts": None}
        rows = [{"ticker": "XYZ", "verdict": "CONFIRMS", "reason": "r"}]
        CL.annotate_operator_veto(rows, state_dir=self.state_dir, fire=True,
                                  freshness_fn=fresh_first, event_fn=event_fn)
        self.assertEqual(events, [])

        fresh_unlock = lambda ticker, now: {"verdict": "FROZEN", "evidence_ts": now - 60,  # noqa: E731
                                            "frozen_since_ts": now - 40 * 3600}
        rows2 = [{"ticker": "XYZ", "verdict": "CONFIRMS", "reason": "r"}]
        CL.annotate_operator_veto(rows2, state_dir=self.state_dir, fire=True,
                                  freshness_fn=fresh_unlock,
                                  breakdown_hold_fn=lambda t: True,
                                  event_fn=event_fn)
        self.assertIs(rows2[0]["operator_not_done"], False)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "XYZ")


class AnnotateTierTests(unittest.TestCase):
    def test_tradeable_requires_earned_signature_and_aster_and_liquidity(self):
        rows = [{"ticker": "GOOD", "signature": "trap_formation_long",
                 "aster_listed": True, "live": {"vol_m": 50.0}},
                {"ticker": "DISCRETIONARY", "signature": "discretionary",
                 "aster_listed": True, "live": {"vol_m": 50.0}},
                {"ticker": "NOTASTER", "signature": "trap_formation_long",
                 "aster_listed": False, "live": {"vol_m": 50.0}},
                {"ticker": "DUST", "signature": "trap_formation_long",
                 "aster_listed": True, "live": {"vol_m": 1.0}}]
        earned = {"trap_formation_long", "stage5_short"}
        CL.annotate_tier(rows, earned=earned)
        by = {r["ticker"]: r["tier"] for r in rows}
        self.assertEqual(by["GOOD"], "tradeable")
        # discretionary-signature row is tracking even though everything else passes
        self.assertEqual(by["DISCRETIONARY"], "tracking")
        # trap_formation_long on a non-Aster name is also tracking
        self.assertEqual(by["NOTASTER"], "tracking")
        self.assertEqual(by["DUST"], "tracking")

    def test_earned_signatures_reads_from_ledger_stats(self):
        # SPEC-150 req 4: ledger.stats() itself now derives the earned set (desk-scoped);
        # classify.earned_signatures() just reads summary.earned_signatures straight off it.
        stats = {"signatures": {
            "trap_formation_long": {"n_filled": 3, "total_r": 2.6},
            "stage5_short": {"n_filled": 3, "total_r": 8.5},
            "blowoff_top_short": {"n_filled": 6, "total_r": 1.29},
            "mindshare_top_short": {"n_filled": 0, "total_r": 0.0},
            "unlock_cliff_fade": {"n_filled": 2, "total_r": -0.5},
        }, "summary": {"earned_signatures": ["blowoff_top_short", "stage5_short",
                                             "trap_formation_long"]}}
        orig = LG.stats
        LG.stats = lambda: stats
        try:
            earned = CL.earned_signatures()
        finally:
            LG.stats = orig
        self.assertEqual(earned, {"trap_formation_long", "stage5_short", "blowoff_top_short"})
        self.assertNotIn("mindshare_top_short", earned)   # n_filled 0
        self.assertNotIn("unlock_cliff_fade", earned)      # negative total_r


if __name__ == "__main__":
    unittest.main()
