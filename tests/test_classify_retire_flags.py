#!/usr/bin/env python3
"""SPEC-140 — classify board: mechanical auto-retire staleness flags.

grill 2026-08-06: the board hit 65 rows vs a ≤20 target — commit-if-relevant makes
WATCHes cheap to create, but nothing mechanically flagged a dead row (LAB -16% past
trigger and BEAT -75% both went unnoticed on a crowded board). This pins the engine
half of the CLAUDE.md §0.5 standing retire-policy: a `retire_flag` (null when healthy)
per board row, fired by time_stop_elapsed / zone_blown_unfilled / stale_14d /
stale_unresolvable — advisory only, never touching the verdict/state machine. All
offline: STATE_DIR redirected to a temp dir so scan_trigger_logs never reads real logs.
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import classify as CL


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = 1_780_000_000.0  # fixed clock


class _TmpState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state_dir = CL.STATE_DIR
        CL.STATE_DIR = Path(self._tmp.name)

    def tearDown(self):
        CL.STATE_DIR = self._orig_state_dir
        self._tmp.cleanup()


class RetireFlagFor(_TmpState):
    def test_time_stop_elapsed(self):
        tok = {"ticker": "TSX", "thesis": {"status": "ARMED", "direction": "SHORT",
               "committed_ts": _iso(NOW - 100 * 3600), "time_stop_h": 24}}
        row = {"ticker": "TSX", "verdict": "CONFIRMS", "reason": "no signals"}
        self.assertEqual(CL.retire_flag_for(tok, row, now=NOW), "time_stop_elapsed")

    def test_zone_blown_unfilled_short(self):
        # SHORT entry zone [0.10, 0.11]; price_leg never entered/never stopped; live
        # price already ran past the zone (> hi*1.03) — the existing ZONE_BLOWN "ran
        # past entry" read, reused rather than re-derived from a fresh fetch.
        tok = {"ticker": "ZBU", "thesis": {"status": "ARMED", "direction": "SHORT",
               "entry_zone": [0.10, 0.11], "committed_ts": _iso(NOW - 2 * 3600)}}
        row = {"ticker": "ZBU", "verdict": "CONFIRMS", "reason": "no signals",
               "price_leg": {"stop_breached": None, "entered_zone": False},
               "live": {"price": 0.20}}
        self.assertEqual(CL.retire_flag_for(tok, row, now=NOW), "zone_blown_unfilled")

    def test_zone_blown_unfilled_long(self):
        tok = {"ticker": "ZBL", "thesis": {"status": "PENDING", "direction": "LONG",
               "entry_zone": [0.20, 0.22], "committed_ts": _iso(NOW - 2 * 3600)}}
        row = {"ticker": "ZBL", "verdict": "CONFIRMS", "reason": "no signals",
               "price_leg": {"stop_breached": None, "entered_zone": False},
               "live": {"price": 0.05}}
        self.assertEqual(CL.retire_flag_for(tok, row, now=NOW), "zone_blown_unfilled")

    def test_zone_not_flagged_once_entered(self):
        # entered_zone True (a real fill printed) — never zone_blown_unfilled regardless
        # of where price is now.
        tok = {"ticker": "ZBE", "thesis": {"status": "ARMED", "direction": "SHORT",
               "entry_zone": [0.10, 0.11], "committed_ts": _iso(NOW - 2 * 3600)}}
        row = {"ticker": "ZBE", "verdict": "TRIGGERS", "reason": "entered",
               "price_leg": {"stop_breached": None, "entered_zone": True},
               "live": {"price": 0.20}}
        self.assertIsNone(CL.retire_flag_for(tok, row, now=NOW))

    def test_stale_14d_zero_trigger_break_events(self):
        tok = {"ticker": "OLDCOIN14D", "thesis": {"status": "ARMED", "direction": "LONG",
               "committed_ts": _iso(NOW - 15 * 86400)}}
        row = {"ticker": "OLDCOIN14D", "verdict": "CONFIRMS", "reason": "no signals"}
        self.assertEqual(CL.retire_flag_for(tok, row, now=NOW), "stale_14d")

    def test_stale_14d_not_flagged_with_a_trigger_event(self):
        (CL.STATE_DIR / "oldcoin14d_trigger.log").write_text(
            f"{_iso(NOW - 10 * 86400)} 🎯 ARMED entry printed\n")
        tok = {"ticker": "OLDCOIN14D", "thesis": {"status": "ARMED", "direction": "LONG",
               "committed_ts": _iso(NOW - 15 * 86400)}}
        row = {"ticker": "OLDCOIN14D", "verdict": "CONFIRMS", "reason": "no signals"}
        self.assertIsNone(CL.retire_flag_for(tok, row, now=NOW))

    def test_committed_3d_ago_healthy(self):
        tok = {"ticker": "FRESH3D", "thesis": {"status": "ARMED", "direction": "LONG",
               "committed_ts": _iso(NOW - 3 * 86400)}}
        row = {"ticker": "FRESH3D", "verdict": "CONFIRMS", "reason": "no signals"}
        self.assertIsNone(CL.retire_flag_for(tok, row, now=NOW))

    def test_legacy_unresolvable_commit_ts(self):
        tok = {"ticker": "LEGACY", "thesis": {"status": "ARMED", "direction": "SHORT",
               "committed_ts": "not-a-real-timestamp"}}
        row = {"ticker": "LEGACY", "verdict": "CONFIRMS", "reason": "no signals"}
        self.assertEqual(CL.retire_flag_for(tok, row, now=NOW), "stale_unresolvable")

    def test_no_committed_ts_at_all_is_not_unresolvable(self):
        # a thesis with no committed_ts wasn't ever datable in the first place — distinct
        # from SPEC-113's "legacy" case (a commit that IS present but unparseable).
        tok = {"ticker": "NOCOMMIT", "thesis": {"status": "ARMED", "direction": "SHORT"}}
        row = {"ticker": "NOCOMMIT", "verdict": "CONFIRMS", "reason": "no signals"}
        self.assertIsNone(CL.retire_flag_for(tok, row, now=NOW))

    def test_retired_thesis_never_flagged(self):
        tok = {"ticker": "DEAD", "thesis": {"status": "RETIRED", "direction": "LONG",
               "committed_ts": _iso(NOW - 100 * 86400), "time_stop_h": 1}}
        row = {"ticker": "DEAD", "verdict": "CONFIRMS", "reason": "no signals"}
        self.assertIsNone(CL.retire_flag_for(tok, row, now=NOW))

    def test_no_thesis_at_all_never_flagged(self):
        tok = {"ticker": "MEMOONLY", "state": "watching"}
        row = {"ticker": "MEMOONLY", "verdict": "CONFIRMS", "reason": "no signals"}
        self.assertIsNone(CL.retire_flag_for(tok, row, now=NOW))


class AnnotateRetireFlags(_TmpState):
    def test_board_rows_and_summary(self):
        tokens = [
            {"ticker": "TSX", "thesis": {"status": "ARMED", "direction": "SHORT",
             "committed_ts": _iso(NOW - 100 * 3600), "time_stop_h": 24}},
            {"ticker": "ZBU", "thesis": {"status": "ARMED", "direction": "SHORT",
             "entry_zone": [0.10, 0.11], "committed_ts": _iso(NOW - 2 * 3600)}},
            {"ticker": "OLDCOIN14D", "thesis": {"status": "ARMED", "direction": "LONG",
             "committed_ts": _iso(NOW - 15 * 86400)}},
            {"ticker": "FRESH3D", "thesis": {"status": "ARMED", "direction": "LONG",
             "committed_ts": _iso(NOW - 3 * 86400)}},
            {"ticker": "LEGACY", "thesis": {"status": "ARMED", "direction": "SHORT",
             "committed_ts": "not-a-real-timestamp"}},
        ]
        rows = [
            {"ticker": "TSX", "verdict": "CONFIRMS", "reason": "no signals"},
            {"ticker": "ZBU", "verdict": "CONFIRMS", "reason": "no signals",
             "price_leg": {"stop_breached": None, "entered_zone": False},
             "live": {"price": 0.20}},
            {"ticker": "OLDCOIN14D", "verdict": "CONFIRMS", "reason": "no signals"},
            {"ticker": "FRESH3D", "verdict": "CONFIRMS", "reason": "no signals"},
            {"ticker": "LEGACY", "verdict": "CONFIRMS", "reason": "no signals"},
        ]
        # snapshot verdicts/reasons before annotating — must be byte-identical after
        before = [(r["verdict"], r["reason"]) for r in rows]
        CL.annotate_retire_flags(rows, tokens=tokens, now=NOW)
        flags = {r["ticker"]: r["retire_flag"] for r in rows}
        self.assertEqual(flags, {
            "TSX": "time_stop_elapsed",
            "ZBU": "zone_blown_unfilled",
            "OLDCOIN14D": "stale_14d",
            "FRESH3D": None,
            "LEGACY": "stale_unresolvable",
        })
        after = [(r["verdict"], r["reason"]) for r in rows]
        self.assertEqual(before, after)   # advisory only — verdict/reason untouched

        flagged = sorted(r["ticker"] for r in rows if r["retire_flag"])
        self.assertEqual(flagged, ["LEGACY", "OLDCOIN14D", "TSX", "ZBU"])
        self.assertEqual(len(flagged), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
