#!/usr/bin/env python3
"""Phase 1a — triage native --json suite.

Run:  python3 tests/test_triage_json.py   (or)  python3 -m unittest -v tests.test_triage_json

Asserts: (1) --json is clean JSON, no ANSI/leading text; (2) it's the watchlist
board incl. LAB; (3) every row matches the contract (keys, types, enums);
(4) the bare invocation still prints the human cards (NON-JSON). Hits live venue
APIs, so it's slow — the orchestrator suite does the same for classify.
SPEC-123: gated behind CRIMEDESK_LIVE_TESTS=1, skipped by default.
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRIAGE = ROOT / "capabilities" / "triage.py"
sys.path.insert(0, str(ROOT / "tests"))
from live_gate import LIVE, SKIP_REASON  # noqa: E402
ANSI = re.compile(r"\x1b\[[0-9;]*m")
CONTRACT_KEYS = {"ticker", "category", "price", "chg24", "range_pct", "vol_m",
                 "funding_pi", "oi_chg_pct", "ls_ratio", "signals", "direction",
                 "tier", "memo"}
TIERS = {"live", "watch", "dust"}
DIRECTIONS = {"short", "long", "neutral"}


def run(*flags):
    return subprocess.run([sys.executable, str(TRIAGE), *flags],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=180)


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestTriageJson(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = run("--json")
        cls.env = json.loads(cls.proc.stdout)  # (1) parses cleanly or this raises
        cls.board = cls.env["board"]           # SPEC 64: {board, meta} envelope

    def test_clean_json_no_ansi(self):
        out = self.proc.stdout
        self.assertNotIn("\x1b", out, "ANSI escape codes leaked into --json")
        self.assertEqual(out.strip(), out.strip().rstrip("\n").strip())
        self.assertIsInstance(self.board, list)

    def test_meta_reports_agent_health(self):
        # SPEC 64: triage meta surfaces launchd watcher health (one glance, every scan)
        agents = self.env["meta"]["agents"]
        self.assertEqual(set(agents), {"coder_dispatch", "nonce_surveil", "board_tick"})
        for state in agents.values():
            self.assertIn(state, {"loaded", "MISSING", "unknown"})

    def test_board_has_tokens_incl_lab(self):
        self.assertGreaterEqual(len(self.board), 15)
        tickers = {r["ticker"] for r in self.board}
        self.assertIn("LAB", tickers)

    def test_each_row_matches_contract(self):
        for r in self.board:
            self.assertTrue(CONTRACT_KEYS.issubset(r), f"{r.get('ticker')} missing keys")
            self.assertIsInstance(r["signals"], list)
            self.assertIn(r["tier"], TIERS)
            self.assertIn(r["direction"], DIRECTIONS)
            self.assertIsInstance(r["vol_m"], (int, float))
            if r["funding_pi"] is not None:
                self.assertIsInstance(r["funding_pi"], float)

    def test_lab_has_live_data(self):
        lab = next(r for r in self.board if r["ticker"] == "LAB")
        self.assertIsInstance(lab["price"], float)
        self.assertIsInstance(lab["funding_pi"], float)

    def test_sorted_signals_then_vol(self):
        keys = [(-len(r["signals"]), -(r["vol_m"] or 0)) for r in self.board]
        self.assertEqual(keys, sorted(keys))

    def test_human_view_is_not_json(self):
        proc = run("--no-color")  # bare/human path
        self.assertIn("CRIME TRIAGE", proc.stdout)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
