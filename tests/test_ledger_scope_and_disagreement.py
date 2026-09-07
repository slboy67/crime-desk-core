#!/usr/bin/env python3
"""SPEC-150 — scope the ledger to desk-originated calls, add `desk_disagreed`, replace
the kill-switch with the desk-scoped checkpoint.

The user's own words (2026-08-19 grill): "I take trades outside of what the desk tells
me to do" (those are NOT recorded — the ledger is scoped to desk-originated calls only,
never the user's full P&L) and "I would have shorted them anyway and I need the desk to
agree" (the desk is being used as PERMISSION; `desk_disagreed` makes the override
population measurable). Every number `stats()` reports is the DESK's report card, never
the user's performance.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import ledger as LG


def _rec(ticker, signature="stage5_short", outcome="stopped", pnl_r=-1.0,
        scope=None, desk_disagreed=None, **kw):
    base = {"ticker": ticker, "direction": "SHORT", "signature": signature,
           "outcome": outcome, "pnl_r": pnl_r, "commit_ts": "2026-06-08",
           "close_ts": "2026-06-09", "notes": ""}
    if scope is not None:
        base["scope"] = scope
    if desk_disagreed is not None:
        base["desk_disagreed"] = desk_disagreed
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


class ScopeDefaultAndExclusion(_TmpLedger):
    def test_record_defaults_scope_to_desk(self):
        out = LG.record(_rec("A"))
        self.assertEqual(out["record"]["scope"], "desk")

    def test_off_desk_rows_excluded_from_desk_aggregates_by_default(self):
        LG.record(_rec("A", outcome="tp1", pnl_r=2.0))
        LG.record(_rec("B", outcome="tp1", pnl_r=1.5))
        LG.record(_rec("C", outcome="stopped", pnl_r=-1.0))
        LG.record(_rec("D", scope="off_desk", outcome="tp1", pnl_r=99.0))
        LG.record(_rec("E", scope="off_desk", outcome="tp1", pnl_r=99.0))
        s = LG.stats(signature="stage5_short")
        self.assertEqual(s["n_filled"], 3)   # A/B/C only — D/E excluded
        self.assertNotIn(99.0, [r.get("pnl_r") for r in s.get("records", [])])

    def test_off_desk_rows_included_when_explicitly_requested(self):
        LG.record(_rec("A", outcome="tp1", pnl_r=2.0))
        LG.record(_rec("D", scope="off_desk", outcome="tp1", pnl_r=99.0))
        s = LG.stats(signature="stage5_short", include_off_desk=True)
        self.assertEqual(s["n_filled"], 2)

    def test_legacy_rows_with_no_scope_field_count_as_desk(self):
        # Pre-SPEC-150 rows never wrote `scope` — they must not silently vanish from
        # the desk's own report card.
        rec = _rec("A", outcome="tp1", pnl_r=2.0)
        LG.record(rec)   # no scope kwarg -> record() still stamps default "desk"
        s = LG.stats(signature="stage5_short")
        self.assertEqual(s["n_filled"], 1)


class DesignDisagreedSplit(_TmpLedger):
    def test_split_renders_both_populations(self):
        LG.record(_rec("A", outcome="tp1", pnl_r=2.0, desk_disagreed=False))
        LG.record(_rec("B", outcome="tp1", pnl_r=1.0, desk_disagreed=False))
        LG.record(_rec("C", outcome="stopped", pnl_r=-3.0, desk_disagreed=True))
        s = LG.stats()
        split = s["summary"]["desk_disagreed_split"]
        self.assertEqual(split["agreed"]["n_filled"], 2)
        self.assertEqual(split["agreed"]["total_r"], 3.0)
        self.assertEqual(split["disagreed"]["n_filled"], 1)
        self.assertEqual(split["disagreed"]["total_r"], -3.0)

    def test_desk_disagreed_defaults_false(self):
        out = LG.record(_rec("A"))
        self.assertIs(out["record"]["desk_disagreed"], False)

    def test_rules_change_flag_when_overrides_outperform_at_n_ge_10(self):
        for i in range(10):
            LG.record(_rec(f"A{i}", outcome="stopped", pnl_r=-1.0, desk_disagreed=False))
        for i in range(10):
            LG.record(_rec(f"B{i}", outcome="tp1", pnl_r=2.0, desk_disagreed=True))
        s = LG.stats()
        split = s["summary"]["desk_disagreed_split"]
        self.assertTrue(split["rules_change"])

    def test_rules_change_flag_false_below_n10_even_if_outperforming(self):
        for i in range(3):
            LG.record(_rec(f"A{i}", outcome="stopped", pnl_r=-1.0, desk_disagreed=False))
        for i in range(3):
            LG.record(_rec(f"B{i}", outcome="tp1", pnl_r=2.0, desk_disagreed=True))
        s = LG.stats()
        split = s["summary"]["desk_disagreed_split"]
        self.assertFalse(split["rules_change"])

    def test_rules_change_flag_false_when_desk_agreed_outperforms(self):
        for i in range(10):
            LG.record(_rec(f"A{i}", outcome="tp1", pnl_r=2.0, desk_disagreed=False))
        for i in range(10):
            LG.record(_rec(f"B{i}", outcome="stopped", pnl_r=-1.0, desk_disagreed=True))
        s = LG.stats()
        split = s["summary"]["desk_disagreed_split"]
        self.assertFalse(split["rules_change"])


class DeskScopedCheckpoint(_TmpLedger):
    def _fill_n(self, n, pnl_r=-1.0, signature="stage5_short"):
        for i in range(n):
            LG.record(_rec(f"T{i}", signature=signature, outcome="stopped", pnl_r=pnl_r))

    def test_below_30_emits_running_count_no_halt(self):
        self._fill_n(29, pnl_r=-1.0)
        s = LG.stats()
        self.assertIsNone(s["summary"].get("desk_checkpoint"))
        prog = s["summary"]["desk_checkpoint_progress"]
        self.assertEqual(prog["n_filled"], 29)
        self.assertEqual(prog["threshold"], 30)

    def test_at_30_negative_r_no_go_signature_halts_calls(self):
        self._fill_n(30, pnl_r=-1.0)
        s = LG.stats()
        self.assertEqual(s["summary"]["desk_checkpoint"], "HALT_CALLS")

    def test_at_30_no_halt_when_cumulative_r_nonnegative(self):
        self._fill_n(15, pnl_r=-1.0)
        self._fill_n(15, pnl_r=1.0, signature="trap_formation_long")
        s = LG.stats()
        self.assertIsNone(s["summary"].get("desk_checkpoint"))

    def test_off_desk_rows_never_count_toward_the_checkpoint(self):
        self._fill_n(29, pnl_r=-1.0)
        LG.record(_rec("OD", scope="off_desk", outcome="stopped", pnl_r=-1.0))
        s = LG.stats()
        self.assertIsNone(s["summary"].get("desk_checkpoint"))
        self.assertEqual(s["summary"]["desk_checkpoint_progress"]["n_filled"], 29)


class EarnedSignatures(_TmpLedger):
    def test_earned_set_excludes_zero_fill_and_negative_r_signatures(self):
        for i in range(2):
            LG.record(_rec(f"A{i}", signature="trap_formation_long", outcome="tp1", pnl_r=1.5))
        for i in range(2):
            LG.record(_rec(f"B{i}", signature="stage5_short", outcome="tp1", pnl_r=2.0))
        for i in range(2):
            LG.record(_rec(f"C{i}", signature="unlock_cliff_fade", outcome="stopped", pnl_r=-1.0))
        s = LG.stats()
        earned = set(s["summary"]["earned_signatures"])
        self.assertEqual(earned, {"trap_formation_long", "stage5_short"})
        self.assertNotIn("unlock_cliff_fade", earned)
        self.assertNotIn("mindshare_top_short", earned)   # zero fills, never appears


if __name__ == "__main__":
    unittest.main()
