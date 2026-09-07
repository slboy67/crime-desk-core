#!/usr/bin/env python3
"""SPEC-147 req 4/5 — re-score existing `source: counterfactual` ledger rows under the
unified fill rule, and `ledger.stats` must say when a counterfactual population is only
PARTIALLY rescored (never silently blend the two populations — they can disagree on
pnl_r for the same call).

Offline-deterministic: a temp ledger.jsonl, a synthetic bars_provider, no network/watchlist.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import counterfactual as CF   # noqa: E402
import ledger as L            # noqa: E402


def _bar(ts, o, h, l, c):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


# The pre-147 flattering fill: entry_px 4.32 (zone TOP) on an inferred SHORT breakdown —
# what the old _infer_mode default would have charged. Under the unified rule this
# re-scores to the worse edge (4.15) and a smaller pnl_r.
_STALE_ROW = {
    "ticker": "RIVERX", "direction": "SHORT", "signature": "stage5_short",
    "entry_zone": [4.15, 4.32], "stop": 4.55, "tp": [3.85, 3.43],
    "outcome": "tp1", "pnl_r": 2.913,
    "commit_ts": "2026-06-01T00:00:00Z", "close_ts": None,
    "counterfactual": {"filled": True, "entry_mode": "breakdown", "entry_px": 4.32},
    "notes": "backfill counterfactual of live call", "source": "counterfactual",
}

_LIVE_ROW = {
    "ticker": "BEAT", "direction": "SHORT", "signature": "stage5_short",
    "outcome": "retired_unfilled", "pnl_r": None,
    "commit_ts": "2026-06-10T23:24:19Z", "close_ts": None, "notes": "retired",
}

_BARS = {
    "RIVERX": [
        _bar(1_700_000_000_000, 5.04, 5.06, 5.02, 5.03),
        _bar(1_700_003_600_000, 4.90, 4.95, 4.20, 4.30),
        _bar(1_700_007_200_000, 4.25, 4.30, 3.80, 3.85),
    ],
}


def _provider(ticker, commit_ts):
    return _BARS.get(ticker)


class _LedgerTmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ledger_path = Path(self.dir.name) / "ledger.jsonl"
        self._orig = L.LEDGER_PATH
        L.LEDGER_PATH = self.ledger_path

    def tearDown(self):
        L.LEDGER_PATH = self._orig
        self.dir.cleanup()

    def _write(self, rows):
        self.ledger_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def _read(self):
        return [json.loads(l) for l in self.ledger_path.read_text().splitlines() if l.strip()]


class RescoreCounterfactualRows(_LedgerTmp):
    def test_stale_counterfactual_row_rescored_to_worse_edge(self):
        self._write([_LIVE_ROW, _STALE_ROW])
        out = CF.rescore(ledger_path=self.ledger_path, bars_provider=_provider)
        self.assertEqual(out["rescored"], 1)
        rows = self._read()
        cf_row = next(r for r in rows if r.get("source") == "counterfactual")
        self.assertAlmostEqual(cf_row["counterfactual"]["entry_px"], 4.15, places=6)
        self.assertLess(cf_row["pnl_r"], _STALE_ROW["pnl_r"])   # the flattering fill is gone
        self.assertEqual(cf_row["rescored_from"], _STALE_ROW["pnl_r"])
        self.assertIn("rescored_ts", cf_row)

    def test_live_row_never_touched(self):
        self._write([_LIVE_ROW, _STALE_ROW])
        CF.rescore(ledger_path=self.ledger_path, bars_provider=_provider)
        rows = self._read()
        live_row = next(r for r in rows if r.get("source") != "counterfactual")
        self.assertEqual(live_row, _LIVE_ROW)   # byte-for-byte, never touched

    def test_no_cached_bars_skips_and_leaves_row_untouched(self):
        row = dict(_STALE_ROW, ticker="NOBARS")
        self._write([row])
        out = CF.rescore(ledger_path=self.ledger_path, bars_provider=_provider)
        self.assertEqual(out["rescored"], 0)
        self.assertEqual(out["skipped"].get("no_klines"), 1)
        rows = self._read()
        self.assertNotIn("rescored_ts", rows[0])

    def test_no_ledger_file_is_a_noop(self):
        out = CF.rescore(ledger_path=self.ledger_path, bars_provider=_provider)
        self.assertEqual(out["rescored"], 0)

    def test_rerunning_rescore_is_stable(self):
        self._write([_STALE_ROW])
        CF.rescore(ledger_path=self.ledger_path, bars_provider=_provider)
        first = self._read()[0]
        out2 = CF.rescore(ledger_path=self.ledger_path, bars_provider=_provider)
        self.assertEqual(out2["rescored"], 1)   # re-derives every pass, not add-only
        second = self._read()[0]
        self.assertEqual(first["pnl_r"], second["pnl_r"])
        self.assertEqual(second["rescored_from"], first["pnl_r"])


class LedgerStatsRescoreGapWarning(_LedgerTmp):
    def test_unrescored_counterfactual_row_flagged_in_signature_row(self):
        self._write([_STALE_ROW])
        row = L.stats(signature="stage5_short")
        self.assertFalse(row["counterfactual_fully_rescored"])
        self.assertEqual(row["counterfactual_unrescored_n"], 1)

    def test_fully_rescored_after_rescore_pass(self):
        self._write([_STALE_ROW])
        CF.rescore(ledger_path=self.ledger_path, bars_provider=_provider)
        row = L.stats(signature="stage5_short")
        self.assertTrue(row["counterfactual_fully_rescored"])
        self.assertEqual(row["counterfactual_unrescored_n"], 0)

    def test_full_stats_envelope_flags_partial_rescore(self):
        self._write([_STALE_ROW])
        out = L.stats()
        self.assertFalse(out["summary"]["counterfactual_fully_rescored"])
        self.assertEqual(out["summary"]["counterfactual_unrescored_n"], 1)

    def test_no_counterfactual_rows_reads_fully_rescored_vacuously_true(self):
        self._write([_LIVE_ROW])
        out = L.stats()
        self.assertTrue(out["summary"]["counterfactual_fully_rescored"])
        self.assertEqual(out["summary"]["counterfactual_unrescored_n"], 0)


if __name__ == "__main__":
    unittest.main()
