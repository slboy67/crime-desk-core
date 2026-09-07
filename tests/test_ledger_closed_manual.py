#!/usr/bin/env python3
"""SPEC-69 — ledger: `closed_manual` outcome (user discretionary close ≠ stopped).

Run:  python3 tests/test_ledger_closed_manual.py

A position the user closes manually before any kill/TP prints (the H LONG 2026-06-12:
~75min hold, scratch exit, every kill line intact) had no honest category and was recorded
as `stopped` pnl_r 0.0 — polluting the stopped-bucket / kill-discipline stats that the §9
base-rate gate reads (SPEC-63 made those rows load-bearing).

Pins:
  1. `closed_manual` is a valid outcome — record() and the thesis close op round-trip it.
  2. Stats semantics: closed_manual counts toward n WITH its realized pnl_r, but is
     excluded from hit% (it says nothing about whether the committed stops work — same
     n-but-not-decided treatment as retired_unfilled).
  3. The one-off migration recategorizes exactly the H row (ticker H, close 2026-06-12,
     notes contain "SPEC-69") from stopped → closed_manual, idempotently.

All offline — ledger/watchlist paths redirected to temp dirs.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))
import ledger as LG


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TH = _load("thesis")

# the live row, verbatim shape (what migrate must find)
H_ROW = {"ticker": "H", "direction": "LONG", "signature": "trap_formation_long",
         "outcome": "stopped", "pnl_r": 0.0, "banked": [],
         "commit_ts": "2026-06-12T14:07:11Z", "close_ts": "2026-06-12T14:32:56Z",
         "notes": "USER CLOSED ~14:45Z ... NOT a stop-out — manual close; recorded as "
                  "'stopped' pnl_r 0.0 pending SPEC-69 closed_manual outcome; recategorize then.",
         "recorded_ts": "2026-06-12T14:32:56Z"}


def _rec(ticker="X", outcome="stopped", pnl_r=-1.0, **kw):
    base = {"ticker": ticker, "direction": "LONG", "signature": "trap_formation_long",
            "entry": 0.23, "stop": 0.2095, "tp": [0.30], "outcome": outcome,
            "pnl_r": pnl_r, "commit_ts": "2026-06-12", "close_ts": "2026-06-12", "notes": ""}
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


class TestOutcomeEnum(_TmpLedger):
    def test_closed_manual_is_a_valid_outcome(self):
        self.assertIn("closed_manual", LG.OUTCOMES)
        out = LG.record(_rec(outcome="closed_manual", pnl_r=0.0))
        self.assertTrue(out["recorded"])
        self.assertEqual(json.loads(LG.LEDGER_PATH.read_text())["outcome"], "closed_manual")

    def test_counts_in_n_and_r_but_not_hit_pct(self):
        LG.record(_rec(ticker="A", outcome="tp1", pnl_r=2.0))
        LG.record(_rec(ticker="B", outcome="stopped", pnl_r=-1.0))
        LG.record(_rec(ticker="H", outcome="closed_manual", pnl_r=0.4))
        row = LG.stats()["signatures"]["trap_formation_long"]
        self.assertEqual(row["n"], 3)                          # counts toward n …
        self.assertEqual(row["total_r"], 1.4)                  # … with its realized R …
        self.assertEqual(row["hit_pct"], 50.0)                 # … but 1/2 DECIDED only:
        #                                                        kill-discipline unpolluted

    def test_not_a_hit_and_not_a_miss(self):
        self.assertNotIn("closed_manual", LG._HITS)
        self.assertNotIn("closed_manual", LG._MISSES)


class TestThesisCloseRoundtrip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig = (TH.WL_PATH, TH.LOCK_PATH, TH.ledger.LEDGER_PATH,
                      TH._prior_24h_range, TH._commit_stop_geometry_warning)
        TH._prior_24h_range = lambda tk: None
        TH._commit_stop_geometry_warning = lambda tk, th: None
        TH.WL_PATH = d / "watchlist.json"
        TH.LOCK_PATH = d / "watchlist.lock"
        TH.ledger.LEDGER_PATH = d / "ledger.jsonl"
        TH.WL_PATH.write_text(json.dumps({"_doc": "test", "tokens": [
            {"ticker": "H", "state": "long active",
             "thesis": {"direction": "LONG", "setup": "trap_long", "status": "ACTIVE",
                        "entry_zone": [0.226, 0.240], "stop": 0.2095, "tp": [0.30],
                        "triggers": ["x"], "invalidation": {"price": 0.2095},
                        "time_stop_h": 96, "committed_ts": "2026-06-12T14:07:11Z"}}],
            "retired": []}))

    def tearDown(self):
        (TH.WL_PATH, TH.LOCK_PATH, TH.ledger.LEDGER_PATH,
         TH._prior_24h_range, TH._commit_stop_geometry_warning) = self._orig
        self.dir.cleanup()

    def test_close_with_closed_manual_round_trips(self):
        out = TH.build_thesis("close", "H", outcome="closed_manual", pnl_r=0.0,
                              reason="user closed manually, kills intact")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["ledger_row"]["outcome"], "closed_manual")
        self.assertEqual(out["pnl_r"], 0.0)
        # ledger row landed + thesis retired off the active list
        row = json.loads(TH.ledger.LEDGER_PATH.read_text())
        self.assertEqual((row["ticker"], row["outcome"]), ("H", "closed_manual"))
        wl = json.loads(TH.WL_PATH.read_text())
        self.assertEqual(wl["tokens"], [])
        self.assertEqual(wl["retired"][0]["thesis"]["status"], "CLOSED")
        # … and report sees it (DoD: round-trips through ledger + report)
        st = LG_stats_at(TH.ledger.LEDGER_PATH)
        row = st["signatures"]["trap_formation_long"]
        self.assertEqual(row["n"], 1)
        self.assertEqual(row["hit_pct"], 0.0)                  # zero DECIDED rows, not a miss


def LG_stats_at(path):
    orig, LG.LEDGER_PATH = LG.LEDGER_PATH, path
    try:
        return LG.stats()
    finally:
        LG.LEDGER_PATH = orig


class TestMigrateSpec69(_TmpLedger):
    def _seed(self):
        rows = [_rec(ticker="SKYAI", outcome="stopped", pnl_r=-1.2,
                     close_ts="2026-06-10", signature="stage5_short"),
                dict(H_ROW),
                _rec(ticker="ESPORTS", outcome="tp1", pnl_r=1.1, close_ts="2026-06-12")]
        with LG.LEDGER_PATH.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_migrates_exactly_the_h_row(self):
        self._seed()
        out = LG.migrate_spec69()
        self.assertEqual(out["migrated"], 1)
        rows = [json.loads(ln) for ln in LG.LEDGER_PATH.read_text().splitlines()]
        h = next(r for r in rows if r["ticker"] == "H")
        self.assertEqual(h["outcome"], "closed_manual")
        self.assertIn("SPEC-69 migration", h["notes"])         # the rewrite is named
        # bystander rows byte-identical in outcome
        self.assertEqual(next(r for r in rows if r["ticker"] == "SKYAI")["outcome"], "stopped")
        self.assertEqual(next(r for r in rows if r["ticker"] == "ESPORTS")["outcome"], "tp1")
        # stopped bucket no longer contains H (DoD)
        st = LG.stats()["signatures"]["trap_formation_long"]
        self.assertEqual(st["hit_pct"], 100.0)                 # ESPORTS tp1 is the only decided row

    def test_idempotent(self):
        self._seed()
        LG.migrate_spec69()
        out2 = LG.migrate_spec69()
        self.assertEqual(out2["migrated"], 0)
        rows = [json.loads(ln) for ln in LG.LEDGER_PATH.read_text().splitlines()]
        self.assertEqual(sum(1 for r in rows if r["outcome"] == "closed_manual"), 1)

    def test_no_h_row_is_a_noop(self):
        with LG.LEDGER_PATH.open("w") as f:
            f.write(json.dumps(_rec(ticker="SKYAI")) + "\n")
        out = LG.migrate_spec69()
        self.assertEqual(out["migrated"], 0)

    def test_missing_ledger_is_a_noop(self):
        out = LG.migrate_spec69()
        self.assertEqual(out["migrated"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
