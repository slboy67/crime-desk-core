#!/usr/bin/env python3
"""SPEC-162 — ledger.commit_open / ledger.sweep_open_commits: close the paper-track
feed gap. `counterfactual.backfill` iterates `source=live, pnl_r=None` ledger rows and
joins watchlist geometry by exact `(ticker, commit_ts)` — but no API ever created such a
row for an ACTIVELY committed thesis (only `ledger.backfill`'s RETIRED/PASS sweep did),
so recent commits (GALA, CASHCAT, …) were invisible to the paper scorer. This pins:
  1. commit_open's geometry-snapshot validation (unscoreable:true, never refused outright)
  2. the auto-sweep's diff/create/idempotent/re-anchor-retire behavior

All offline — ledger + watchlist paths redirected to a temp dir.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import ledger as LG


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig_ledger = LG.LEDGER_PATH
        LG.LEDGER_PATH = d / "ledger.jsonl"
        self.wl_path = d / "watchlist.json"

    def tearDown(self):
        LG.LEDGER_PATH = self._orig_ledger
        self.dir.cleanup()

    def _rows(self):
        if not LG.LEDGER_PATH.exists():
            return []
        return [json.loads(ln) for ln in LG.LEDGER_PATH.read_text().splitlines() if ln.strip()]


GALA_THESIS = {
    "direction": "SHORT", "signature": "faded_bounce", "entry_zone": [0.00195, 0.00199],
    "stop": 0.00211, "tp": [0.00165, 0.00142], "committed_ts": "2026-08-25T00:00:00Z",
}

WATCH_NO_GEOMETRY_THESIS = {
    "direction": "WATCH", "entry_zone": None, "stop": None, "tp": [],
    "committed_ts": "2026-08-25T01:00:00Z",
}

TWO_LEG_THESIS = {
    "direction": "WATCH", "entry_zone": None, "stop": None, "tp": [],
    "legs": [{"kind": "reclaim_long", "entry": "hold_above_0.60",
             "entry_zone": [0.6, 0.61], "stop": 0.578, "tp": [0.66, 0.75]}],
    "committed_ts": "2026-08-25T02:00:00Z",
}


# ── commit_open ──────────────────────────────────────────────────────────────────
class TestCommitOpen(_Tmp):
    def test_writes_open_row_with_geometry_snapshot(self):
        out = LG.commit_open({"ticker": "GALA", "thesis": GALA_THESIS})
        self.assertTrue(out["recorded"])
        rec = out["record"]
        self.assertEqual(rec["ticker"], "GALA")
        self.assertEqual(rec["direction"], "SHORT")
        self.assertIsNone(rec["outcome"])
        self.assertIsNone(rec["pnl_r"])
        self.assertEqual(rec["source"], "live")
        self.assertEqual(rec["commit_ts"], "2026-08-25T00:00:00Z")
        geo = rec["geometry"]
        self.assertEqual(geo["entry_zone"], [0.00195, 0.00199])
        self.assertEqual(geo["stop"], 0.00211)
        self.assertEqual(geo["tp"], [0.00165, 0.00142])
        self.assertFalse(rec["unscoreable"])
        rows = self._rows()
        self.assertEqual(len(rows), 1)

    def test_no_geometry_recorded_anyway_tagged_unscoreable(self):
        """req 1: refuse to SCORE, never refuse to RECORD — the no-geometry cost stays
        visible rather than a silent skip."""
        out = LG.commit_open({"ticker": "TREE", "thesis": WATCH_NO_GEOMETRY_THESIS})
        self.assertTrue(out["recorded"])
        self.assertTrue(out["record"]["unscoreable"])
        self.assertEqual(len(self._rows()), 1)

    def test_leg_geometry_counts_as_scoreable(self):
        out = LG.commit_open({"ticker": "CYS", "thesis": TWO_LEG_THESIS})
        self.assertFalse(out["record"]["unscoreable"])
        self.assertEqual(out["record"]["geometry"]["legs"], TWO_LEG_THESIS["legs"])

    def test_missing_ticker_raises(self):
        with self.assertRaises(ValueError):
            LG.commit_open({"thesis": GALA_THESIS})

    def test_missing_committed_ts_raises(self):
        with self.assertRaises(ValueError):
            LG.commit_open({"ticker": "GALA", "thesis": {"direction": "SHORT"}})

    def test_signature_canonicalized(self):
        th = dict(GALA_THESIS, signature="stage5_short")
        out = LG.commit_open({"ticker": "GALA", "thesis": th})
        self.assertEqual(out["record"]["signature"], "stage5_short")


# ── sweep_open_commits ───────────────────────────────────────────────────────────
class TestSweepOpenCommits(_Tmp):
    def _write_wl(self, tokens):
        self.wl_path.write_text(json.dumps({"tokens": tokens, "retired": []}))

    def test_creates_open_row_for_uncommitted_ticker(self):
        self._write_wl([{"ticker": "GALA", "thesis": GALA_THESIS}])
        out = LG.sweep_open_commits(wl_path=self.wl_path)
        self.assertEqual(out["created"], [{"ticker": "GALA", "commit_ts": "2026-08-25T00:00:00Z"}])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "GALA")
        self.assertIsNone(rows[0]["outcome"])

    def test_idempotent_second_run_creates_nothing(self):
        self._write_wl([{"ticker": "GALA", "thesis": GALA_THESIS}])
        LG.sweep_open_commits(wl_path=self.wl_path)
        out2 = LG.sweep_open_commits(wl_path=self.wl_path)
        self.assertEqual(out2["created"], [])
        self.assertEqual(out2["skipped"], ["GALA"])
        self.assertEqual(len(self._rows()), 1)

    def test_tokens_without_thesis_or_committed_ts_are_ignored(self):
        self._write_wl([{"ticker": "NOTHESIS"}, {"ticker": "NOTS", "thesis": {"direction": "WATCH"}}])
        out = LG.sweep_open_commits(wl_path=self.wl_path)
        self.assertEqual(out["created"], [])
        self.assertEqual(self._rows(), [])

    def test_multiple_tickers_each_get_one_open_row(self):
        self._write_wl([{"ticker": "GALA", "thesis": GALA_THESIS},
                        {"ticker": "TREE", "thesis": WATCH_NO_GEOMETRY_THESIS}])
        out = LG.sweep_open_commits(wl_path=self.wl_path)
        self.assertEqual(len(out["created"]), 2)
        self.assertEqual(len(self._rows()), 2)

    def test_missing_watchlist_is_a_noop_not_a_crash(self):
        out = LG.sweep_open_commits(wl_path=self.wl_path)   # never written
        self.assertEqual(out, {"created": [], "retired": [], "skipped": []})

    def test_reanchored_committed_ts_creates_new_row_and_retires_orphan(self):
        """CLAUDE.md §0.5: re-timestamping committed_ts is standing arm/reconciliation
        policy — the orphaned prior open row must retire (outcome=retired_unfilled,
        notes tagging window_re-anchored), never linger as a phantom open commit that
        can never join again (the exact-ts join is brittle by design)."""
        self._write_wl([{"ticker": "GALA", "thesis": GALA_THESIS}])
        LG.sweep_open_commits(wl_path=self.wl_path)
        reanchored = dict(GALA_THESIS, committed_ts="2026-08-25T12:00:00Z")
        self._write_wl([{"ticker": "GALA", "thesis": reanchored}])
        out = LG.sweep_open_commits(wl_path=self.wl_path)
        self.assertEqual(out["created"], [{"ticker": "GALA", "commit_ts": "2026-08-25T12:00:00Z"}])
        self.assertEqual(out["retired"], [{"ticker": "GALA", "commit_ts": "2026-08-25T00:00:00Z"}])
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        old = next(r for r in rows if r["commit_ts"] == "2026-08-25T00:00:00Z")
        new = next(r for r in rows if r["commit_ts"] == "2026-08-25T12:00:00Z")
        self.assertEqual(old["outcome"], "retired_unfilled")
        self.assertIn("window_re-anchored", old["notes"])
        self.assertIsNone(new["outcome"])
        # a third run (no further watchlist change) creates/retires nothing more
        out3 = LG.sweep_open_commits(wl_path=self.wl_path)
        self.assertEqual(out3["created"], [])
        self.assertEqual(out3["retired"], [])
        self.assertEqual(len(self._rows()), 2)

    def test_reanchor_never_touches_an_already_resolved_row(self):
        """A row some OTHER path already resolved (outcome set, e.g. via a real close)
        must never be mistaken for an orphaned open row and re-tagged."""
        self._write_wl([{"ticker": "GALA", "thesis": GALA_THESIS}])
        LG.sweep_open_commits(wl_path=self.wl_path)
        LG.record({"ticker": "GALA", "direction": "SHORT", "signature": "stage5_short",
                  "outcome": "stopped", "pnl_r": -1.0, "commit_ts": "2026-08-25T00:00:00Z",
                  "close_ts": "2026-08-25T06:00:00Z", "notes": "closed"})
        reanchored = dict(GALA_THESIS, committed_ts="2026-08-25T12:00:00Z")
        self._write_wl([{"ticker": "GALA", "thesis": reanchored}])
        LG.sweep_open_commits(wl_path=self.wl_path)
        rows = self._rows()
        resolved = next(r for r in rows if r.get("outcome") == "stopped")
        self.assertEqual(resolved["pnl_r"], -1.0)   # untouched


if __name__ == "__main__":
    unittest.main(verbosity=2)
