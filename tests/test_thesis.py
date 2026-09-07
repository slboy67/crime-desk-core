#!/usr/bin/env python3
"""SPEC 48 — `thesis`: schema-validated thesis lifecycle wired to `ledger`.

Run:  python3 tests/test_thesis.py

The §0.5 state machine's core state was the only desk state mutated by hand-edited
JSON (inline Python against config/watchlist.json, twice in one day). Contract:
  - commit validates the §0.5 contract and REJECTS a thesis with no named invalidation
    ("if you can't name the broken invalidation field, there is no BREAK");
  - close/retire moves tokens → retired and appends the outcome row to the SPEC-40
    ledger so the §9 base-rate gate accumulates without anyone remembering to log;
  - read-modify-write on watchlist.json is atomic + lock-guarded (concurrent safety);
  - orchestrator `fill` passes dict args as shell-quoted JSON (the thesis block must
    survive the shell=True invoke path).
All paths in a tmpdir — offline, deterministic.
"""
import importlib.util
import json
import shlex
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path=None):
    spec = importlib.util.spec_from_file_location(name, path or ROOT / "capabilities" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TH = _load("thesis")

GOOD_THESIS = {
    "direction": "SHORT",
    "setup": "blowoff_top",
    "entry_zone": [0.118, 0.122],
    "stop": 0.131,
    "tp": [0.105, 0.098],
    "triggers": ["held break of 0.115 on >=1.5x vol"],
    "invalidation": {"price_reclaim": 0.125, "note": "reclaim of the breakdown level = bear trap"},
    "time_stop_h": 72,
}


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig = (TH.WL_PATH, TH.LOCK_PATH, TH.ledger.LEDGER_PATH, TH._prior_24h_range)
        TH._prior_24h_range = lambda tk: None   # SPEC 54: geometry check offline-pinned
        TH.WL_PATH = d / "watchlist.json"
        TH.LOCK_PATH = d / "watchlist.lock"
        TH.ledger.LEDGER_PATH = d / "ledger.jsonl"
        TH.WL_PATH.write_text(json.dumps(
            {"_doc": "test", "tokens": [{"ticker": "LAB", "state": "watch"}], "retired": []}))

    def tearDown(self):
        TH.WL_PATH, TH.LOCK_PATH, TH.ledger.LEDGER_PATH, TH._prior_24h_range = self._orig
        self.dir.cleanup()

    def wl(self):
        return json.loads(TH.WL_PATH.read_text())


class TestCommit(_Tmp):
    def test_commit_stamps_and_writes(self):
        out = TH.build_thesis("commit", "SKYAI", thesis=dict(GOOD_THESIS))
        self.assertTrue(out["ok"])
        wl = self.wl()
        tok = next(t for t in wl["tokens"] if t["ticker"] == "SKYAI")
        th = tok["thesis"]
        self.assertEqual(th["direction"], "SHORT")
        self.assertTrue(th["committed_ts"])
        self.assertEqual(th["committed_by"], "thesis-capability")
        self.assertEqual(th["status"], "PENDING")

    def test_commit_existing_token_keeps_its_fields(self):
        TH.build_thesis("commit", "LAB", thesis=dict(GOOD_THESIS))
        tok = next(t for t in self.wl()["tokens"] if t["ticker"] == "LAB")
        self.assertEqual(tok["state"], "watch")
        self.assertIn("thesis", tok)

    def test_commit_without_invalidation_rejected(self):
        bad = {k: v for k, v in GOOD_THESIS.items() if k != "invalidation"}
        out = TH.build_thesis("commit", "SKYAI", thesis=bad)
        self.assertFalse(out["ok"])
        self.assertIn("invalidation", out["error"])
        self.assertNotIn("SKYAI", [t["ticker"] for t in self.wl()["tokens"]])

    def test_commit_empty_invalidation_rejected(self):
        bad = dict(GOOD_THESIS, invalidation={})
        out = TH.build_thesis("commit", "SKYAI", thesis=bad)
        self.assertFalse(out["ok"])

    def test_commit_bad_direction_rejected(self):
        out = TH.build_thesis("commit", "SKYAI", thesis=dict(GOOD_THESIS, direction="SIDEWAYS"))
        self.assertFalse(out["ok"])
        self.assertIn("direction", out["error"])

    def test_commit_over_active_thesis_rejected(self):
        TH.build_thesis("commit", "LAB", thesis=dict(GOOD_THESIS))
        out = TH.build_thesis("commit", "LAB", thesis=dict(GOOD_THESIS))
        self.assertFalse(out["ok"])
        self.assertIn("close", out["error"].lower())   # tells the caller to close/retire first

    # ── SPEC-144: watch_level must be gated at commit like every other geometry field ──
    def test_watch_level_bare_floats_rejected(self):
        errors, _ = TH.validate_thesis(dict(GOOD_THESIS, watch_level=[0.039, 0.062]))
        self.assertTrue(any("watch_level" in e for e in errors), errors)

    def test_watch_level_bad_dir_rejected(self):
        errors, _ = TH.validate_thesis(
            dict(GOOD_THESIS, watch_level={"price": 0.039, "dir": "sideways"}))
        self.assertTrue(any("watch_level" in e for e in errors), errors)

    def test_watch_level_proper_shape_accepted(self):
        errors, _ = TH.validate_thesis(
            dict(GOOD_THESIS, watch_level=[{"price": 0.039, "dir": "below"}]))
        self.assertEqual(errors, [])

    def test_watch_level_none_is_unchanged(self):
        errors, _ = TH.validate_thesis(dict(GOOD_THESIS))
        self.assertEqual(errors, [])

    def test_watch_level_mixed_list_one_bad_rejected(self):
        errors, _ = TH.validate_thesis(
            dict(GOOD_THESIS, watch_level=[{"price": 0.039, "dir": "below"}, 0.062]))
        self.assertTrue(any("watch_level" in e for e in errors), errors)

    def test_commit_rejects_watch_level_naming_ticker_and_shape(self):
        bad = dict(GOOD_THESIS, watch_level=[0.039, 0.062])
        out = TH.build_thesis("commit", "SKYAI", thesis=bad)
        self.assertFalse(out["ok"])
        self.assertIn("SKYAI", out["error"])
        self.assertIn("watch_level", out["error"])
        self.assertIn("price", out["error"])
        self.assertIn("dir", out["error"])
        self.assertNotIn("SKYAI", [t["ticker"] for t in self.wl()["tokens"]])


class TestCloseRetire(_Tmp):
    def test_close_roundtrip_moves_and_ledgers(self):
        TH.build_thesis("commit", "LAB", thesis=dict(GOOD_THESIS, banked=[0.105]))
        out = TH.build_thesis("close", "LAB", reason="TP1 banked, runner stopped breakeven",
                              outcome="tp1", pnl_r=1.8)
        self.assertTrue(out["ok"])
        wl = self.wl()
        self.assertNotIn("LAB", [t["ticker"] for t in wl["tokens"]])
        ret = next(t for t in wl["retired"] if t["ticker"] == "LAB")
        self.assertEqual(ret["thesis"]["status"], "CLOSED")
        self.assertTrue(ret["retired_date"])
        self.assertIn("TP1", ret["retired_reason"])
        # the §9 ledger row landed without anyone remembering to log
        row = out["ledger_row"]
        self.assertEqual(row["ticker"], "LAB")
        self.assertEqual(row["outcome"], "tp1")
        self.assertEqual(row["signature"], "blowoff_top_short")
        recs = [json.loads(x) for x in TH.ledger.LEDGER_PATH.read_text().splitlines()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["pnl_r"], 1.8)

    def test_retire_defaults_unfilled(self):
        TH.build_thesis("commit", "LAB", thesis=dict(GOOD_THESIS))
        out = TH.build_thesis("retire", "LAB", reason="zone blown without trigger")
        self.assertTrue(out["ok"])
        self.assertEqual(out["ledger_row"]["outcome"], "retired_unfilled")
        ret = next(t for t in self.wl()["retired"] if t["ticker"] == "LAB")
        self.assertEqual(ret["thesis"]["status"], "RETIRED")

    def test_close_without_thesis_rejected(self):
        out = TH.build_thesis("close", "LAB", reason="x", outcome="tp1")
        self.assertFalse(out["ok"])

    def test_unknown_ticker_rejected(self):
        out = TH.build_thesis("retire", "NOPE", reason="x")
        self.assertFalse(out["ok"])

    def test_watchlist_stays_valid_json_after_roundtrip(self):
        TH.build_thesis("commit", "SKYAI", thesis=dict(GOOD_THESIS))
        TH.build_thesis("retire", "SKYAI", reason="r")
        wl = self.wl()   # raises if torn
        self.assertEqual({t["ticker"] for t in wl["tokens"]}, {"LAB"})


class TestConcurrency(_Tmp):
    def test_parallel_commits_all_land(self):
        tickers = [f"T{i:02d}" for i in range(20)]
        with ThreadPoolExecutor(max_workers=8) as ex:
            outs = list(ex.map(
                lambda tk: TH.build_thesis("commit", tk, thesis=dict(GOOD_THESIS)), tickers))
        self.assertTrue(all(o["ok"] for o in outs))
        wl = self.wl()   # valid JSON
        present = {t["ticker"] for t in wl["tokens"]}
        self.assertTrue(set(tickers) <= present)


class TestOrchestratorFill(unittest.TestCase):
    def test_fill_passes_dict_args_as_quoted_json(self):
        ORC = _load("orchestrator", path=ROOT / "orchestrator.py")
        cmd = ORC.fill("python3 capabilities/thesis.py --op {op} --ticker {ticker} [--thesis {thesis}] --json",
                       {"op": "commit", "ticker": "LAB", "thesis": GOOD_THESIS})
        toks = shlex.split(cmd)
        blob = toks[toks.index("--thesis") + 1]
        self.assertEqual(json.loads(blob), GOOD_THESIS)   # survives the shell round-trip


if __name__ == "__main__":
    unittest.main(verbosity=2)
