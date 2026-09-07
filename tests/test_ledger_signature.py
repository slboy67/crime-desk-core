#!/usr/bin/env python3
"""SPEC 63 — ledger rows must carry the setup signature and realized R.

Live FOLKS closed tp1 landed as signature:"discretionary", pnl_r:null — telling the §9
gate nothing. This wires: a commit-time `signature` (SPEC-59 vocabulary), close passing it
through, mechanical pnl_r from exit_px (or an estimate flagged from the committed TP), and a
`ledger stats` source split (live vs replay vs backfill).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import thesis as TH        # noqa: E402
import ledger as L         # noqa: E402

BASE = {"direction": "SHORT", "entry_zone": [0.104, 0.106], "stop": 0.112,
        "tp": [0.095, 0.085], "triggers": ["breakdown"],
        "invalidation": {"level": "close above 0.112"}, "time_stop_h": 24}


class _ThesisFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = (TH.WL_PATH, TH.LOCK_PATH, TH.ledger.LEDGER_PATH,
                      TH._prior_24h_range, TH._propose_stop_geometry)
        TH.WL_PATH = Path(self.tmp.name) / "watchlist.json"
        TH.LOCK_PATH = Path(self.tmp.name) / "wl.lock"
        TH.ledger.LEDGER_PATH = Path(self.tmp.name) / "ledger.jsonl"
        TH.WL_PATH.write_text('{"tokens": []}')
        TH._prior_24h_range = lambda tk: None
        TH._propose_stop_geometry = lambda tk, d, e: None

    def tearDown(self):
        (TH.WL_PATH, TH.LOCK_PATH, TH.ledger.LEDGER_PATH,
         TH._prior_24h_range, TH._propose_stop_geometry) = self._orig
        self.tmp.cleanup()


class TestCommitSignature(_ThesisFixture):
    def test_commit_with_signature_persists_and_validates(self):
        r = TH.build_thesis("commit", "FOLKS", thesis=dict(BASE, signature="catb_top"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["thesis"]["signature"], "catb_top")

    def test_commit_bad_signature_rejected(self):
        r = TH.build_thesis("commit", "FOLKS", thesis=dict(BASE, signature="moonshot"))
        self.assertFalse(r["ok"])
        self.assertIn("signature", r["error"])


class TestCloseRealizedR(_ThesisFixture):
    def test_close_with_exit_px_computes_pnl_r(self):
        TH.build_thesis("commit", "FOLKS", thesis=dict(BASE, signature="catb_top"))
        r = TH.build_thesis("close", "FOLKS", outcome="tp1", exit_px=0.095,
                            reason="tp1 banked")
        self.assertTrue(r["ok"])
        row = r["ledger_row"]
        self.assertEqual(row["signature"], "mindshare_top_short")   # catb_top canonicalized
        # SHORT: entry mid 0.105, stop 0.112 → risk 0.007; (0.105-0.095)/0.007 = 1.429R
        self.assertAlmostEqual(row["pnl_r"], 1.429, places=2)
        self.assertFalse(r.get("pnl_r_estimated"))

    def test_close_tp_without_exit_px_estimates_and_flags(self):
        TH.build_thesis("commit", "FOLKS", thesis=dict(BASE, signature="catb_top"))
        r = TH.build_thesis("close", "FOLKS", outcome="tp2", reason="tp2")
        self.assertTrue(r["ok"])
        self.assertTrue(r["pnl_r_estimated"])
        # tp2 committed = 0.085 → (0.105-0.085)/0.007 = 2.857R estimate
        self.assertAlmostEqual(r["ledger_row"]["pnl_r"], 2.857, places=2)

    def test_explicit_pnl_r_is_authoritative(self):
        TH.build_thesis("commit", "FOLKS", thesis=dict(BASE, signature="catb_top"))
        r = TH.build_thesis("close", "FOLKS", outcome="tp1", pnl_r=1.8, reason="x")
        self.assertEqual(r["ledger_row"]["pnl_r"], 1.8)
        self.assertFalse(r.get("pnl_r_estimated"))


class TestCloseSignatureBackfill(_ThesisFixture):
    def test_close_no_signature_defaults_discretionary_not_a_guess(self):
        # commit WITHOUT a signature (and no setup) → close must not silently guess.
        th = {k: v for k, v in BASE.items()}
        TH.build_thesis("commit", "FOLKS", thesis=th)
        r = TH.build_thesis("close", "FOLKS", outcome="tp1", exit_px=0.095,
                            reason="cat-b mindshare top fade")
        self.assertEqual(r["ledger_row"]["signature"], "discretionary")
        self.assertEqual(r["signature_source"], "defaulted")
        self.assertIsNotNone(r.get("suggested_signature"))   # a suggestion is offered

    def test_close_confirm_inferred_writes_the_suggestion(self):
        TH.build_thesis("commit", "FOLKS", thesis={k: v for k, v in BASE.items()})
        r = TH.build_thesis("close", "FOLKS", outcome="tp1", exit_px=0.095,
                            reason="cat-b mindshare top fade", confirm_signature=True)
        self.assertEqual(r["signature_source"], "inferred-confirmed")
        # "mindshare"/"cat-b" → mindshare_top_short
        self.assertEqual(r["ledger_row"]["signature"], "mindshare_top_short")


class TestStatsSourceSplit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = L.LEDGER_PATH
        L.LEDGER_PATH = Path(self.tmp.name) / "ledger.jsonl"

    def tearDown(self):
        L.LEDGER_PATH = self._orig
        self.tmp.cleanup()

    def test_stats_split_live_replay_backfill(self):
        L.record({"ticker": "A", "signature": "mindshare_top_short", "outcome": "tp1",
                  "pnl_r": 1.5})                                          # live
        L.record({"ticker": "B", "signature": "mindshare_top_short", "outcome": "stopped",
                  "pnl_r": -1.0, "source": "replay"})
        L.record({"ticker": "C", "signature": "mindshare_top_short", "outcome": "tp2",
                  "pnl_r": 2.0, "source": "backfill"})
        row = L.stats()["signatures"]["mindshare_top_short"]
        self.assertIn("by_source", row)
        self.assertEqual(row["by_source"]["live"]["n"], 1)
        self.assertEqual(row["by_source"]["replay"]["n"], 1)
        self.assertEqual(row["by_source"]["backfill"]["n"], 1)
        self.assertEqual(row["by_source"]["live"]["hit_pct"], 100.0)
        self.assertEqual(row["by_source"]["replay"]["hit_pct"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
