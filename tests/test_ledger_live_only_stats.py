#!/usr/bin/env python3
"""SPEC-139 — ledger stats: live-only headline (replay quarantined) + per-signature
fill_rate.

grill 2026-08-06: `stats` averaged replay records into headline per-signature numbers
(mindshare_top_short showed n=324/-26.3R, 100% replay from an untrusted mechanical
harness — a wrong "proven negative-edge at scale" conclusion) and counted unfilled
commits as evidence (74% of live records never filled; stage5_short read "n=14, 40%
hit" when filled reality was n=3). These tests pin: live-only headline by default,
replay surfaced only under `include_replay` (never summed into headline totals or
`sizeable`), the n_commits/n_filled/fill_rate split, R-math over filled records only,
and the §9 GO gate (filled n>=10, total_r>=+5, avg_r>0 — no hit_pct component) plus
the record()-time near-duplicate warning.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import ledger as LG


def _rec(ticker, signature="stage5_short", outcome="stopped", pnl_r=-1.0,
         source=None, recorded_ts=None, **kw):
    base = {"ticker": ticker, "direction": "SHORT", "signature": signature,
            "outcome": outcome, "pnl_r": pnl_r, "commit_ts": "2026-06-08",
            "close_ts": "2026-06-09", "notes": ""}
    if source is not None:
        base["source"] = source
    if recorded_ts is not None:
        base["recorded_ts"] = recorded_ts
    base.update(kw)
    return base


class _TmpLedger(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = LG.LEDGER_PATH
        LG.LEDGER_PATH = Path(self._tmp.name) / "ledger.jsonl"

    def tearDown(self):
        LG.LEDGER_PATH = self._orig
        self._tmp.cleanup()


class ReplayQuarantine(_TmpLedger):
    def test_default_headline_excludes_replay(self):
        LG.record(_rec("A", outcome="tp1", pnl_r=2.0))                      # live win
        for i in range(20):
            LG.record(_rec(f"R{i}", outcome="stopped", pnl_r=-1.0, source="replay"))
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertEqual(row["n_commits"], 1)
        self.assertEqual(row["n_filled"], 1)
        self.assertEqual(row["total_r"], 2.0)
        self.assertEqual(row["hit_pct"], 100.0)
        # replay stays visible in by_source, but never summed into the headline
        self.assertEqual(row["by_source"]["replay"]["n"], 20)
        self.assertNotIn("replay", row)          # not surfaced without the flag

    def test_include_replay_flag_surfaces_it_without_polluting_headline(self):
        LG.record(_rec("A", outcome="tp1", pnl_r=2.0))
        LG.record(_rec("R0", outcome="stopped", pnl_r=-1.0, source="replay"))
        row = LG.stats(include_replay=True)["signatures"]["stage5_short"]
        self.assertIn("replay", row)
        self.assertEqual(row["replay"]["n"], 1)
        self.assertEqual(row["n_filled"], 1)
        self.assertEqual(row["total_r"], 2.0)

    def test_sizeable_never_flipped_by_replay_volume(self):
        LG.record(_rec("A", outcome="stopped", pnl_r=-1.0))     # one live loss
        for i in range(324):
            LG.record(_rec(f"R{i}", outcome="tp1", pnl_r=2.0, source="replay"))
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertFalse(row["sizeable"])   # 324 replay wins must not flip it
        self.assertEqual(row["n_filled"], 1)


class FillSplit(_TmpLedger):
    def test_commits_vs_filled_vs_fill_rate(self):
        for i in range(3):
            LG.record(_rec(f"F{i}", outcome="stopped", pnl_r=-1.0))
        LG.record(_rec("F3", outcome="tp1", pnl_r=None))            # filled, null-R
        for i in range(5):
            LG.record(_rec(f"U{i}", outcome="retired_unfilled", pnl_r=None))
        LG.record(_rec("Z0", outcome="zone_blown", pnl_r=None))
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertEqual(row["n_commits"], 10)
        self.assertEqual(row["n_filled"], 4)
        self.assertEqual(row["fill_rate"], 0.4)
        # R-math over the filled set only: 3 stopped @ -1.0, 1 tp1 null-R
        self.assertEqual(row["total_r"], -3.0)

    def test_fill_rate_null_when_no_commits(self):
        row = LG.stats(signature="stage5_short")
        self.assertIsNone(row["fill_rate"])
        self.assertEqual(row["n_commits"], 0)
        self.assertEqual(row["n_filled"], 0)


class SizeableGate(_TmpLedger):
    def _fill(self, n, pnl_each):
        outcome = "tp1" if pnl_each > 0 else "stopped"
        for i in range(n):
            LG.record(_rec(f"T{i}", outcome=outcome, pnl_r=pnl_each))

    def test_go_gate_exact_thresholds(self):
        self._fill(10, 0.51)          # total_r = 5.1, avg_r = 0.51
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertEqual(row["n_filled"], 10)
        self.assertAlmostEqual(row["total_r"], 5.1, places=2)
        self.assertTrue(row["sizeable"])

    def test_n_9_not_sizeable(self):
        self._fill(9, 0.6)            # total_r 5.4, avg 0.6, n=9 — below the n floor
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertFalse(row["sizeable"])

    def test_total_r_below_5_not_sizeable(self):
        self._fill(10, 0.49)          # total_r 4.9
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertFalse(row["sizeable"])

    def test_avg_r_not_positive_not_sizeable(self):
        for i in range(5):
            LG.record(_rec(f"H{i}", outcome="tp1", pnl_r=1.0))
        for i in range(5):
            LG.record(_rec(f"M{i}", outcome="stopped", pnl_r=-1.0))
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertEqual(row["n_filled"], 10)
        self.assertEqual(row["total_r"], 0.0)
        self.assertFalse(row["sizeable"])

    def test_hit_pct_never_gates_sizeable(self):
        # hit_pct pinned at exactly 50 (the old gate's own boundary) but total_r/avg_r
        # clear the new §9 GO gate — sizeable must be True; hit_pct is not a factor.
        for i in range(5):
            LG.record(_rec(f"H{i}", outcome="tp1", pnl_r=2.0))
        for i in range(5):
            LG.record(_rec(f"M{i}", outcome="stopped", pnl_r=-0.6))
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertEqual(row["hit_pct"], 50.0)
        self.assertGreaterEqual(row["total_r"], 5.0)
        self.assertTrue(row["sizeable"])


class TopLevelSummary(_TmpLedger):
    def test_cumulative_live_filled_n_and_r_readable_off_one_call(self):
        LG.record(_rec("A", signature="stage5_short", outcome="tp1", pnl_r=2.0))
        LG.record(_rec("B", signature="trap_formation_long", outcome="stopped", pnl_r=-1.0))
        LG.record(_rec("C", signature="stage5_short", outcome="retired_unfilled", pnl_r=None))
        LG.record(_rec("D", signature="trap_formation_long", outcome="tp1", pnl_r=1.5,
                      source="replay"))
        st = LG.stats()
        self.assertEqual(st["summary"]["live_n_filled"], 2)
        self.assertAlmostEqual(st["summary"]["live_total_r"], 1.0, places=2)


class DupGuard(_TmpLedger):
    def test_near_duplicate_within_window_flags_warning(self):
        out1 = LG.record(_rec("TLM", signature="blowoff_top_short", outcome="tp1", pnl_r=1.29,
                              recorded_ts="2026-07-11T10:00:00Z"))
        self.assertNotIn("dup_warning", out1)
        out2 = LG.record(_rec("TLM", signature="blowoff_top_short", outcome="tp1", pnl_r=None,
                              recorded_ts="2026-07-11T10:00:01Z"))
        self.assertIn("dup_warning", out2)

    def test_records_further_apart_are_not_flagged(self):
        LG.record(_rec("X", recorded_ts="2026-07-11T10:00:00Z"))
        out2 = LG.record(_rec("X", recorded_ts="2026-07-11T10:05:00Z"))
        self.assertNotIn("dup_warning", out2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
