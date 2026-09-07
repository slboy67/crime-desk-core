#!/usr/bin/env python3
"""SPEC-169 — signature `tier` + `trailing_10_r` in the ledger (post-GO management).

CLAUDE.md §9 was rewritten 2026-08-28 to define trailing-10 demotion / GO tier
ceilings; the engine only ever computed `sizeable` and nothing consumed it. These
tests pin the tier state machine (hypothesis -> go at fill #10, go -> demoted on a
negative trailing-10, demoted -> go once the window re-clears the GO bars,
discretionary pinned to hypothesis, the decaying flag) plus trailing-10 windowing by
close_ts and the tier_events append log. All offline — LEDGER_PATH (and therefore the
derived tier_events path) redirected to a temp dir per test.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import ledger as LG        # noqa: E402


class _LedgerTmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = LG.LEDGER_PATH
        LG.LEDGER_PATH = Path(self._tmp.name) / "ledger.jsonl"

    def tearDown(self):
        LG.LEDGER_PATH = self._orig
        self._tmp.cleanup()

    def _record(self, ticker, pnl_r, close_ts, signature="trap_formation_long",
               outcome="tp1", direction="LONG"):
        LG.record({"ticker": ticker, "direction": direction, "signature": signature,
                   "outcome": outcome, "pnl_r": pnl_r, "close_ts": close_ts})


class TierEventsPathDerivation(unittest.TestCase):
    def test_tier_events_path_follows_ledger_path(self):
        fake = Path("/tmp/some-dir/ledger.jsonl")
        orig = LG.LEDGER_PATH
        try:
            LG.LEDGER_PATH = fake
            self.assertEqual(LG._tier_events_path(), fake.parent / "tier_events.jsonl")
        finally:
            LG.LEDGER_PATH = orig


class TierStateMachine(_LedgerTmp):
    def _fill_n(self, n, r, sig="trap_formation_long", start_day=1):
        for i in range(n):
            self._record(f"T{i}", r, f"2026-01-{start_day + i:02d}T00:00:00Z", signature=sig)

    def test_below_10_filled_is_hypothesis(self):
        self._fill_n(9, 1.0)
        row = LG.stats()["signatures"]["trap_formation_long"]
        self.assertEqual(row["tier"], "hypothesis")
        self.assertIsNone(row["tier_flag"])

    def test_10th_fill_prints_go_when_bars_clear(self):
        # 10 fills @ +1.0R each -> total_r=10, avg=1.0 -> sizeable -> go
        self._fill_n(10, 1.0)
        row = LG.stats()["signatures"]["trap_formation_long"]
        self.assertEqual(row["tier"], "go")
        self.assertEqual(row["trailing_10_r"], 10.0)

    def test_10_filled_but_bars_not_cleared_stays_hypothesis(self):
        # total_r only 3 (< +5) -> not sizeable -> hypothesis
        self._fill_n(10, 0.3)
        row = LG.stats()["signatures"]["trap_formation_long"]
        self.assertEqual(row["tier"], "hypothesis")

    def test_go_then_negative_trailing_10_demotes(self):
        self._fill_n(10, 1.0)                                   # go: total 10R
        # 10 more losers -> trailing-10 window (last 10) goes deeply negative
        for i in range(10):
            self._record(f"L{i}", -1.0, f"2026-02-{i + 1:02d}T00:00:00Z")
        row = LG.stats()["signatures"]["trap_formation_long"]
        self.assertEqual(row["tier"], "demoted")
        self.assertEqual(row["trailing_10_r"], -10.0)

    def test_demoted_stays_demoted_until_trailing_10_reclears_go_bars(self):
        self._fill_n(10, 1.0)                                    # go
        for i in range(10):
            self._record(f"L{i}", -1.0, f"2026-02-{i + 1:02d}T00:00:00Z")  # demote
        # a lukewarm recovery: trailing-10 back above zero but NOT >=+5/avg>0 over
        # the full window yet — must STAY demoted (test the hysteresis, not a bounce)
        self._record("R1", 0.5, "2026-03-01T00:00:00Z")
        row = LG.stats()["signatures"]["trap_formation_long"]
        self.assertEqual(row["tier"], "demoted")

    def test_demoted_reclears_to_go_when_trailing_10_clears_bars_again(self):
        self._fill_n(10, 1.0)                                    # go
        for i in range(10):
            self._record(f"L{i}", -1.0, f"2026-02-{i + 1:02d}T00:00:00Z")  # demote
        # 10 fresh winners -> trailing-10 window is now entirely these 10 @ +1.0R
        # (total +10, avg +1.0) -> clears the GO bars again -> back to go
        for i in range(10):
            self._record(f"W{i}", 1.0, f"2026-03-{i + 1:02d}T00:00:00Z")
        row = LG.stats()["signatures"]["trap_formation_long"]
        self.assertEqual(row["tier"], "go")

    def test_discretionary_pinned_to_hypothesis_even_if_bars_would_clear(self):
        self._fill_n(10, 1.0, sig="discretionary")
        row = LG.stats()["signatures"]["discretionary"]
        self.assertEqual(row["tier"], "hypothesis")
        # trailing_10_r is still informational even though tier stays pinned
        self.assertEqual(row["trailing_10_r"], 10.0)

    def test_decaying_flag_on_go_tier_at_or_below_threshold(self):
        # 10 fills totalling exactly +1.0R trailing (>=5 total won't happen with n=10
        # unless total>=5; use 10 fills of +0.5 -> total 5.0, avg .5 -> sizeable -> go,
        # trailing_10_r = 5.0 which is > 1.0 -> not decaying yet. Then thin it out with
        # a run that drops the trailing-10 total to <=1.0 while staying non-negative.
        self._fill_n(10, 0.5)                                     # go, trailing 5.0
        for i in range(10):
            self._record(f"F{i}", 0.05, f"2026-02-{i + 1:02d}T00:00:00Z")  # trailing -> 0.5
        row = LG.stats()["signatures"]["trap_formation_long"]
        self.assertEqual(row["tier"], "go")
        self.assertEqual(row["trailing_10_r"], 0.5)
        self.assertEqual(row["tier_flag"], "decaying")

    def test_no_filled_rows_trailing_10_is_null(self):
        row = LG.stats(signature="trap_formation_long")
        self.assertIsNone(row["trailing_10_r"])
        self.assertEqual(row["tier"], "hypothesis")


class TrailingWindowByCloseTs(_LedgerTmp):
    def test_trailing_10_ignores_older_than_last_10_by_close_ts(self):
        # 11 fills, oldest is a big loser OUTSIDE the trailing-10 window
        self._record("OLD", -100.0, "2026-01-01T00:00:00Z")
        for i in range(10):
            self._record(f"N{i}", 1.0, f"2026-02-{i + 1:02d}T00:00:00Z")
        row = LG.stats()["signatures"]["trap_formation_long"]
        self.assertEqual(row["trailing_10_r"], 10.0)          # OLD excluded

    def test_out_of_order_recording_still_windows_by_close_ts_not_insert_order(self):
        # insert the NEWEST row first, oldest last — windowing must still be by close_ts
        for i in reversed(range(10)):
            self._record(f"N{i}", 1.0, f"2026-02-{i + 1:02d}T00:00:00Z")
        self._record("OLD", -100.0, "2026-01-01T00:00:00Z")
        row = LG.stats()["signatures"]["trap_formation_long"]
        self.assertEqual(row["trailing_10_r"], 10.0)

    def test_missing_close_ts_sorts_last_not_first(self):
        # a row missing close_ts must never look like the OLDEST evidence and get
        # silently excluded when a real close_ts exists — it sorts to the END, so
        # with only 10 total fills it's still IN the trailing-10 window.
        for i in range(9):
            self._record(f"N{i}", 1.0, f"2026-02-{i + 1:02d}T00:00:00Z")
        LG.record({"ticker": "NOTS", "direction": "LONG", "signature": "trap_formation_long",
                  "outcome": "tp1", "pnl_r": 2.0, "close_ts": None})
        row = LG.stats()["signatures"]["trap_formation_long"]
        self.assertEqual(row["trailing_10_r"], 11.0)          # 9*1.0 + 2.0


class TierEventsAppend(_LedgerTmp):
    def _events_path(self):
        return LG._tier_events_path()

    def _fill_n(self, n, r, sig="trap_formation_long", start_day=1):
        for i in range(n):
            self._record(f"T{i}", r, f"2026-01-{start_day + i:02d}T00:00:00Z", signature=sig)

    def test_event_appended_on_hypothesis_to_go_transition(self):
        self._fill_n(10, 1.0)
        out = LG.stats()
        appended = out["summary"]["tier_events_appended"]
        self.assertEqual(len(appended), 1)
        ev = appended[0]
        self.assertEqual(ev["signature"], "trap_formation_long")
        self.assertEqual(ev["from"], "hypothesis")
        self.assertEqual(ev["to"], "go")
        self.assertEqual(ev["n_filled"], 10)

        lines = self._events_path().read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["to"], "go")

    def test_no_duplicate_event_on_repeated_stats_call_with_no_change(self):
        self._fill_n(10, 1.0)
        LG.stats()                                            # first call appends
        out2 = LG.stats()                                     # second call: no change
        self.assertEqual(out2["summary"]["tier_events_appended"], [])
        lines = self._events_path().read_text().splitlines()
        self.assertEqual(len(lines), 1)

    def test_second_transition_appends_a_second_event(self):
        self._fill_n(10, 1.0)
        LG.stats()                                            # hypothesis -> go
        for i in range(10):
            self._record(f"L{i}", -1.0, f"2026-02-{i + 1:02d}T00:00:00Z")
        out = LG.stats()                                      # go -> demoted
        appended = out["summary"]["tier_events_appended"]
        self.assertEqual(len(appended), 1)
        self.assertEqual(appended[0]["from"], "go")
        self.assertEqual(appended[0]["to"], "demoted")
        lines = self._events_path().read_text().splitlines()
        self.assertEqual(len(lines), 2)

    def test_hypothesis_signature_never_events(self):
        self._fill_n(3, 1.0)                                  # never reaches n=10
        out = LG.stats()
        self.assertEqual(out["summary"]["tier_events_appended"], [])
        self.assertFalse(self._events_path().exists())


class DiscoveryTickWiresTierEvents(unittest.TestCase):
    """SPEC-169 req 1: the ntfy hook (ops/notify.sh) fires on every tier_events append —
    ledger.py itself makes no network call (house style), so the firing lives in
    ops/discovery_tick.sh. This just pins that the wiring exists and routes `desk`
    (SPEC-157 discipline, same as every other discovery_tick.sh page)."""

    def test_discovery_tick_calls_ledger_stats_and_pages_tier_events(self):
        src = (ROOT / "ops" / "discovery_tick.sh").read_text()
        self.assertIn("ledger.py stats", src)
        self.assertIn("tier_events.jsonl", src)
        tier_notify_lines = [ln for ln in src.splitlines()
                             if "ops/notify.sh" in ln and "TIER change" in ln]
        self.assertTrue(tier_notify_lines, "expected a TIER-change notify.sh call")
        for ln in tier_notify_lines:
            self.assertIn('"desk"', ln)


if __name__ == "__main__":
    unittest.main(verbosity=2)
