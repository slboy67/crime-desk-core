#!/usr/bin/env python3
"""SPEC 40 — `ledger`: per-signature outcome scoreboard.

§9's base-rate gate ("don't size on a signature until n≥10, hit% >50, positive
edge") is unenforceable without a ledger. These tests pin the record→stats
round-trip, the `sizeable` flag thresholds, the zero-record shape, and the
watchlist backfill. All offline — ledger path redirected to a temp dir.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import ledger as LG


def _rec(ticker="VELVET", direction="SHORT", signature="stage5_short",
         outcome="stopped", pnl_r=-1.0, **kw):
    base = {"ticker": ticker, "direction": direction, "signature": signature,
            "entry": 0.37, "stop": 0.40, "tp": [0.30, 0.184], "outcome": outcome,
            "pnl_r": pnl_r, "commit_ts": "2026-06-08", "close_ts": "2026-06-09",
            "notes": ""}
    base.update(kw)
    return base


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = LG.LEDGER_PATH
        LG.LEDGER_PATH = Path(self._tmp.name) / "ledger.jsonl"

    def tearDown(self):
        LG.LEDGER_PATH = self._orig_path
        self._tmp.cleanup()

    # ── record → stats round-trip ──────────────────────────────────────────
    def test_record_stats_roundtrip(self):
        out = LG.record(_rec())
        self.assertTrue(out["recorded"])
        st = LG.stats()
        row = st["signatures"]["stage5_short"]
        self.assertEqual(row["n"], 1)
        self.assertEqual(row["hit_pct"], 0.0)
        self.assertEqual(row["total_r"], -1.0)
        self.assertFalse(row["sizeable"])

    def test_record_appends_jsonl(self):
        LG.record(_rec())
        LG.record(_rec(ticker="SKYAI", outcome="tp1", pnl_r=2.0))
        lines = LG.LEDGER_PATH.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1])["ticker"], "SKYAI")

    def test_invalid_outcome_rejected(self):
        with self.assertRaises(ValueError):
            LG.record(_rec(outcome="mooned"))

    def test_invalid_signature_rejected(self):
        with self.assertRaises(ValueError):
            LG.record(_rec(signature="vibes"))

    # ── the §9 sizeable gate ────────────────────────────────────────────────
    def test_sizeable_flips_only_at_thresholds(self):
        # 9 records, 6 hits, positive R → n<10 → NOT sizeable
        for i in range(6):
            LG.record(_rec(ticker=f"T{i}", outcome="tp1", pnl_r=2.0))
        for i in range(3):
            LG.record(_rec(ticker=f"M{i}", outcome="stopped", pnl_r=-1.0))
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertEqual(row["n"], 9)
        self.assertFalse(row["sizeable"])
        # 10th record (a hit) → n=10, hit 70%, total_r>0 → sizeable
        LG.record(_rec(ticker="T9", outcome="tp2", pnl_r=3.0))
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertEqual(row["n"], 10)
        self.assertTrue(row["sizeable"])

    def test_hit_pct_at_50_no_longer_a_sizeable_gate(self):
        # SPEC-139 (2026-08-07): the §9 GO gate dropped hit_pct entirely (filled n>=10,
        # total_r>=+5, avg_r>0). hit_pct==50 still reports correctly but no longer blocks
        # sizeable on its own — total_r=5 (10-5) clears the gate.
        for i in range(5):
            LG.record(_rec(ticker=f"H{i}", outcome="tp1", pnl_r=2.0))
        for i in range(5):
            LG.record(_rec(ticker=f"M{i}", outcome="stopped", pnl_r=-1.0))
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertEqual(row["hit_pct"], 50.0)
        self.assertTrue(row["sizeable"])

    def test_not_sizeable_when_total_r_negative(self):
        # 7 small hits, 3 big misses → hit% 70 but negative edge
        for i in range(7):
            LG.record(_rec(ticker=f"H{i}", outcome="tp1", pnl_r=0.3))
        for i in range(3):
            LG.record(_rec(ticker=f"M{i}", outcome="stopped", pnl_r=-1.5))
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertGreater(row["hit_pct"], 50)
        self.assertLess(row["total_r"], 0)
        self.assertFalse(row["sizeable"])

    # ── zero records is a shape, not an error ───────────────────────────────
    def test_stats_zero_records(self):
        st = LG.stats()
        self.assertEqual(st["signatures"], {})
        self.assertEqual(st["total_records"], 0)
        one = LG.stats(signature="blowoff_top_short")
        self.assertEqual(one["n"], 0)
        self.assertFalse(one["sizeable"])

    # ── single-signature detail carries the individual records ──────────────
    def test_single_signature_detail(self):
        LG.record(_rec())
        LG.record(_rec(ticker="BSB", signature="blowoff_top_short",
                       outcome="retired_unfilled", pnl_r=None))
        one = LG.stats(signature="stage5_short")
        self.assertEqual(one["n"], 1)
        self.assertEqual(len(one["records"]), 1)
        self.assertEqual(one["records"][0]["ticker"], "VELVET")

    # ── signal-only outcomes count in n but not in hit% ─────────────────────
    def test_signal_only_excluded_from_hit_pct(self):
        LG.record(_rec(ticker="A", outcome="tp1", pnl_r=2.0))
        LG.record(_rec(ticker="B", outcome="retired_unfilled", pnl_r=None))
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertEqual(row["n"], 2)
        self.assertEqual(row["hit_pct"], 100.0)   # 1 decided, 1 hit
        self.assertEqual(row["total_r"], 2.0)

    def test_last_5_most_recent_first(self):
        for i, oc in enumerate(["stopped", "tp1", "tp1", "stopped", "tp2", "tp1"]):
            LG.record(_rec(ticker=f"T{i}", outcome=oc,
                           pnl_r=1.0 if oc != "stopped" else -1.0))
        row = LG.stats()["signatures"]["stage5_short"]
        self.assertEqual(len(row["last_5"]), 5)
        self.assertEqual(row["last_5"][0]["outcome"], "tp1")     # the latest record

    # ── backfill from retired watchlist theses ──────────────────────────────
    def test_backfill_from_watchlist(self):
        wl = {"tokens": [
            {"ticker": "VELVET", "state": "stopped 2026-06-09 — stop 0.40 printed 0.4749",
             "thesis": {"direction": "SHORT", "status": "RETIRED", "setup": "stage5",
                        "stop": 0.40, "tp": [0.30], "committed_ts": "2026-06-08"}},
            {"ticker": "BILL", "state": "signal-only, retired",
             "thesis": {"direction": "WATCH", "status": "RETIRED", "setup": "none"}},
            {"ticker": "SKYAI", "state": "ACTIVE short",
             "thesis": {"direction": "SHORT", "status": "ACTIVE", "setup": "stage5_short",
                        "stop": 0.21}},
        ]}
        wl_path = Path(self._tmp.name) / "watchlist.json"
        wl_path.write_text(json.dumps(wl))
        out = LG.backfill(wl_path)
        self.assertEqual(out["seeded"], 2)                  # ACTIVE thesis NOT seeded
        st = LG.stats()
        self.assertEqual(st["total_records"], 2)
        velvet = [r for r in LG._load() if r["ticker"] == "VELVET"][0]
        self.assertEqual(velvet["outcome"], "stopped")
        self.assertEqual(velvet["signature"], "stage5_short")
        # idempotent: second run seeds nothing
        out2 = LG.backfill(wl_path)
        self.assertEqual(out2["seeded"], 0)


if __name__ == "__main__":
    unittest.main()
