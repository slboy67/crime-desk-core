#!/usr/bin/env python3
"""SPEC-168 — `faded_bounce` is a first-class ledger signature.

`SIGNATURES`/`_raw_canon_strict` had no branch for it, so a thesis committed with
`signature: "faded_bounce"` (the user's PRIMARY edge — CLAUDE.md §0 item 1) collapsed
into `discretionary`. The live GALA SHORT (committed 2026-08-25T16:51:48Z) sits in the
real ledger under `discretionary` and needs the one-shot retag. All offline — ledger/
watchlist paths redirected to temp files.
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import ledger as LG        # noqa: E402


class CanonMapping(unittest.TestCase):
    def test_three_spellings_map_to_faded_bounce(self):
        for spelling in ("faded_bounce", "faded-bounce", "faded bounce"):
            self.assertEqual(LG.canon_signature(spelling), "faded_bounce")

    def test_faded_bounce_wins_over_catb_substring(self):
        # a faded_bounce setup string may ALSO contain "cat_b" (it IS a Cat-B setup) —
        # faded_bounce must match first, never fall through to mindshare_top_short.
        self.assertEqual(
            LG.canon_signature("faded_bounce cat_b top checklist 5/5"), "faded_bounce")

    def test_direction_guard_never_lets_faded_bounce_be_long(self):
        # SPEC-90 direction-guard semantics: same behavior as every other short sig —
        # a LONG-tagged record never keeps a SHORT signature name.
        result = LG.canon_signature("faded_bounce", direction="LONG")
        self.assertNotEqual(result, "faded_bounce")
        self.assertEqual(result, "trap_formation_long")

    def test_short_direction_unaffected(self):
        self.assertEqual(LG.canon_signature("faded_bounce", direction="SHORT"), "faded_bounce")

    def test_faded_bounce_is_a_short_sig(self):
        self.assertIn("faded_bounce", LG._SHORT_SIGS)
        self.assertEqual(LG._sig_side("faded_bounce"), "SHORT")

    def test_faded_bounce_in_signatures_tuple(self):
        self.assertIn("faded_bounce", LG.SIGNATURES)


class _LedgerWlTmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_ledger = LG.LEDGER_PATH
        self.ledger_path = Path(self._tmp.name) / "ledger.jsonl"
        LG.LEDGER_PATH = self.ledger_path
        self.wl_path = Path(self._tmp.name) / "watchlist.json"

    def tearDown(self):
        LG.LEDGER_PATH = self._orig_ledger
        self._tmp.cleanup()

    def _write_ledger(self, rows):
        self.ledger_path.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n")

    def _write_wl(self, tokens):
        self.wl_path.write_text(json.dumps({"tokens": tokens}))


class RetagMigration(_LedgerWlTmp):
    def setUp(self):
        super().setUp()
        self._write_wl([
            {"ticker": "GALA", "thesis": {"signature": "faded_bounce",
                                          "committed_ts": "2026-08-25T16:51:48Z"}},
            {"ticker": "ZORA", "thesis": {"signature": "faded_bounce",
                                          "committed_ts": "2026-08-20T00:00:00Z"}},
        ])
        self._write_ledger([
            # 1 — GALA live row, discretionary, matching wl key -> retagged
            {"ticker": "GALA", "signature": "discretionary", "outcome": None,
             "pnl_r": None, "commit_ts": "2026-08-25T16:51:48Z", "source": "live"},
            # 2 — GALA counterfactual row, discretionary, matching wl key -> retagged
            {"ticker": "GALA", "signature": "discretionary", "outcome": "stopped",
             "pnl_r": -1.0, "commit_ts": "2026-08-25T16:51:48Z", "source": "counterfactual"},
            # 3 — discretionary but commit_ts does NOT match any faded_bounce wl thesis
            {"ticker": "SOME", "signature": "discretionary", "outcome": "tp1",
             "pnl_r": 1.0, "commit_ts": "2026-01-01T00:00:00Z", "source": "live"},
            # 4 — different ticker's counterfactual row, untouched
            {"ticker": "OTHER", "signature": "discretionary", "outcome": None,
             "pnl_r": None, "commit_ts": "2026-08-20T00:00:00Z", "source": "counterfactual"},
        ])

    def test_dry_run_reports_two_without_writing(self):
        out = LG.retag_from_watchlist(wl_path=self.wl_path, apply=False)
        self.assertEqual(len(out["retagged"]), 2)
        self.assertFalse(out["applied"])
        tickers = sorted(r["ticker"] for r in out["retagged"])
        self.assertEqual(tickers, ["GALA", "GALA"])
        # file untouched on dry-run
        rows = [json.loads(ln) for ln in self.ledger_path.read_text().splitlines()]
        self.assertTrue(all(r["signature"] == "discretionary" for r in rows))

    def test_apply_writes_and_backs_up(self):
        out = LG.retag_from_watchlist(wl_path=self.wl_path, apply=True)
        self.assertEqual(len(out["retagged"]), 2)
        self.assertTrue(out["applied"])
        bak = self.ledger_path.parent / (self.ledger_path.name + ".bak-spec168")
        self.assertTrue(bak.exists())

        rows = [json.loads(ln) for ln in self.ledger_path.read_text().splitlines()]
        by_key = {(r["ticker"], r.get("source") or "live"): r for r in rows}
        self.assertEqual(by_key[("GALA", "live")]["signature"], "faded_bounce")
        self.assertEqual(by_key[("GALA", "counterfactual")]["signature"], "faded_bounce")
        self.assertEqual(by_key[("SOME", "live")]["signature"], "discretionary")
        self.assertEqual(by_key[("OTHER", "counterfactual")]["signature"], "discretionary")

        st = LG.stats()
        self.assertGreaterEqual(st["signatures"]["faded_bounce"]["n_commits"], 1)

    def test_idempotent(self):
        LG.retag_from_watchlist(wl_path=self.wl_path, apply=True)
        second = LG.retag_from_watchlist(wl_path=self.wl_path, apply=True)
        self.assertEqual(second["retagged"], [])
        self.assertFalse(second["applied"])


class RegressionKnownSignatureTags(unittest.TestCase):
    """Every canonical signature must round-trip through canon_signature (a future
    SIGNATURES addition that forgets a _raw_canon_strict branch fails this test
    immediately, rather than silently landing in `discretionary`)."""

    def test_every_canonical_signature_round_trips(self):
        for sig in LG.SIGNATURES:
            if sig == "discretionary":
                continue
            self.assertEqual(LG.canon_signature(sig), sig)

    def test_scan_mode_choices_that_are_signature_names_are_canonical(self):
        # SPEC-120/148: orchestrator's scan.py --mode set. A mode string that is ALSO a
        # real trading-signature name (today: faded_bounce) must map to itself, never
        # discretionary — guards the next new signature/mode pairing.
        scan_src = (ROOT / "capabilities" / "scan.py").read_text()
        m = re.search(r'"--mode",\s*choices=\[([^\]]*)\]', scan_src)
        self.assertIsNotNone(m, "scan.py --mode choices list not found")
        modes = [tok.strip().strip('"').strip("'") for tok in m.group(1).split(",")]
        self.assertIn("faded_bounce", modes)
        for mode in modes:
            if mode in LG.SIGNATURES:
                self.assertEqual(LG.canon_signature(mode), mode)


if __name__ == "__main__":
    unittest.main(verbosity=2)
