#!/usr/bin/env python3
"""SPEC 29 — radar sweeps return PARTIAL on time-budget (never all-or-nothing) + emit progress.

Run:  python3 tests/test_radar_partial_progress.py

The orchestrator hard-kills at the capability timeout and emits a bare error, discarding ALL
completed work. The sweeps now self-budget below that timeout: accumulate per-token results and,
on hitting budget, return what finished with partial:true + tokens_done/tokens_remaining, plus a
sidecar progress file the orchestrator can read mid-run. A `tickers` subset arg runs just those.
Network mocked / not hit — offline-deterministic. SPEC-123: TestOnchainRadarBudget and
TestDistributionRadarPartial are still gated behind CRIMEDESK_LIVE_TESTS=1 — the
progress emitter (_write_progress) prints unconditional "radar-progress ..." stderr lines
that read as a live sweep to anyone watching premerge output (the 2026-07-14 false alarm
that prompted this spec), even though neither class makes a network call.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "tests"))
from live_gate import LIVE, SKIP_REASON  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


DR = _load("distribution_radar")
OCR = _load("onchain_radar")


class TestParseTickers(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(DR._parse_tickers(["BILL", "skyai"]), ["BILL", "SKYAI"])
        self.assertEqual(DR._parse_tickers("BILL,SKYAI"), ["BILL", "SKYAI"])
        self.assertEqual(DR._parse_tickers("['BILL', 'SKYAI']"), ["BILL", "SKYAI"])   # python-list str
        self.assertEqual(DR._parse_tickers("BILL SKYAI"), ["BILL", "SKYAI"])
        self.assertIsNone(DR._parse_tickers(None))
        self.assertIsNone(DR._parse_tickers(""))


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.mkdtemp()
        # _write_progress/_progress_path resolve STATE from their module globals; onchain_radar may
        # use a distinct distribution_radar instance, so patch the actual function globals (covers both).
        globs, seen = [], set()
        for g in (DR._write_progress.__globals__, OCR._write_progress.__globals__):
            if id(g) not in seen:
                seen.add(id(g)); globs.append(g)
        self._saved = [(g, g["STATE"]) for g in globs]
        for g in globs:
            g["STATE"] = Path(self._td)

    def tearDown(self):
        for g, st in self._saved:
            g["STATE"] = st


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestOnchainRadarBudget(_Tmp):
    """Mid-sweep budget break is deterministic via a fake clock that only advances per token."""
    def setUp(self):
        super().setUp()
        self._time, self._pass, self._pl = OCR.time, OCR._token_pass, OCR._perp_liqs
        OCR._perp_liqs = lambda *a, **k: {"available": False, "reason": "stubbed"}  # no network

        class Clock:
            t = 1000.0
            def time(self):
                return Clock.t
        self.clock = Clock()
        OCR.time = self.clock

        def slow_pass(tk, meta, labels):     # each token "takes" 200s of budget
            Clock.t += 200
            return {"available": True, "distributors": [], "accumulators": [], "alerts": []}
        OCR._token_pass = slow_pass

    def tearDown(self):
        OCR.time, OCR._token_pass, OCR._perp_liqs = self._time, self._pass, self._pl
        super().tearDown()

    def test_partial_on_budget_keeps_finished_work(self):
        # 3 real config tokens; budget 350s, 200s/token → breaks at the 3rd (2 done)
        r = OCR.build_onchain_radar(tickers="BILL,SKYAI,LAB", budget_sec=350)
        self.assertTrue(r["partial"])
        self.assertEqual(r["tokens_done"], 2)            # finished work PRESERVED, not discarded
        self.assertEqual(r["tokens_remaining"], ["LAB"])
        self.assertEqual(set(r["tokens"]), {"BILL", "SKYAI"})
        self.assertEqual(r["tokens_total"], 3)

    def test_completes_within_budget(self):
        r = OCR.build_onchain_radar(tickers="BILL,SKYAI,LAB", budget_sec=100000)
        self.assertFalse(r["partial"])
        self.assertEqual(r["tokens_done"], 3)
        self.assertEqual(r["tokens_remaining"], [])

    def test_progress_sidecar_written(self):
        OCR.build_onchain_radar(tickers="BILL,SKYAI,LAB", budget_sec=100000)
        prog = json.loads((Path(self._td) / "radar_progress_onchain_radar.json").read_text())
        self.assertEqual(prog["tokens_total"], 3)
        self.assertEqual(prog["tokens_done"], 3)
        self.assertIn("elapsed_sec", prog)


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestDistributionRadarPartial(_Tmp):
    """distribution_radar gets the same treatment (untracked tickers exercise the budget/partial
    path without any network)."""
    def test_partial_immediately_when_over_budget(self):
        r = DR.build_radar(tickers="FAKEA,FAKEB,FAKEC", budget_sec=-1)
        self.assertTrue(r["partial"])
        self.assertEqual(r["tokens_done"], 0)
        self.assertEqual(r["tokens_remaining"], ["FAKEA", "FAKEB", "FAKEC"])

    def test_completes_untracked_subset(self):
        r = DR.build_radar(tickers="FAKEA,FAKEB", budget_sec=100000)
        self.assertFalse(r["partial"])
        self.assertEqual(r["tokens_done"], 2)
        self.assertEqual(set(r["scanned"]), {"FAKEA", "FAKEB"})
        # untracked → explicit unavailable, never a crash
        self.assertFalse(r["tokens"]["FAKEA"]["available"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
