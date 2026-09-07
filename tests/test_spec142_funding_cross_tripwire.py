#!/usr/bin/env python3
"""SPEC-142 — per-name funding-cross tripwire (thesis-committed, board-owned).

"Page me when <name>'s funding crosses <level>" (AEON, and the BLESS §6 arming leg —
"funding cools under +0.02%/4h") had no engine home; it was hand-rolled as
ops/interim_funding_watch.sh (deleted by this spec) until now. Covers:
  - capabilities/classify.py: _normalize_funding_watch (malformed entries surfaced in
    caveats, never silently dropped), eval_funding_watch (pure lt/gt crossing),
    classify_token wiring (funding_leg populated, verdict gains WATCH-ARMED semantics,
    already_true_at_commit), fire_funding_armed (once-per-episode inbox dedup, mirrors
    fire_watch_armed).
  - ops/board_tick.py: funding_watch rides the same continuous-key re-arm semantics as
    watch_level — page once, no re-page while still breached, re-arm on leave+return.
  - ops/page_grammar.py: page_funding_armed grammar.
  - interim artifacts removed.

Offline-deterministic: scan_trigger_logs/price_window_range are stubbed so classify_token
never touches the filesystem/network for these fixtures.
"""
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))
import classify as CL


def _load(name, sub="capabilities"):
    spec = importlib.util.spec_from_file_location(name, ROOT / sub / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── capabilities/classify.py — normalize ─────────────────────────────────────────

class TestNormalizeFundingWatch(unittest.TestCase):
    def test_valid_list(self):
        th = {"funding_watch": [{"threshold_4h": 0.02, "op": "lt", "note": "n"}]}
        out, caveats = CL._normalize_funding_watch(th)
        self.assertEqual(out, [{"threshold_4h": 0.02, "op": "lt", "note": "n", "page_label": None}])
        self.assertEqual(caveats, [])

    def test_single_dict_accepted(self):
        th = {"funding_watch": {"threshold_4h": -0.3, "op": "gt", "note": ""}}
        out, caveats = CL._normalize_funding_watch(th)
        self.assertEqual(len(out), 1)
        self.assertEqual(caveats, [])

    def test_absent_is_empty_no_caveat(self):
        out, caveats = CL._normalize_funding_watch({})
        self.assertEqual(out, [])
        self.assertEqual(caveats, [])

    def test_bare_float_entry_surfaced_in_caveats_not_dropped_silently(self):
        th = {"funding_watch": [0.02]}
        out, caveats = CL._normalize_funding_watch(th)
        self.assertEqual(out, [])
        self.assertEqual(len(caveats), 1)
        self.assertIn("malformed", caveats[0])

    def test_missing_op_surfaced_in_caveats(self):
        th = {"funding_watch": [{"threshold_4h": 0.02}]}
        out, caveats = CL._normalize_funding_watch(th)
        self.assertEqual(out, [])
        self.assertEqual(len(caveats), 1)

    def test_bad_op_value_surfaced_in_caveats(self):
        th = {"funding_watch": [{"threshold_4h": 0.02, "op": "eq"}]}
        out, caveats = CL._normalize_funding_watch(th)
        self.assertEqual(out, [])
        self.assertEqual(len(caveats), 1)

    def test_one_good_one_bad_keeps_the_good_flags_the_bad(self):
        th = {"funding_watch": [{"threshold_4h": 0.02, "op": "lt"}, 0.5]}
        out, caveats = CL._normalize_funding_watch(th)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(caveats), 1)

    # ── SPEC-161 req 2: page_label validated/truncated here (commit-read time) ──
    def test_page_label_absent_is_none_no_caveat(self):
        th = {"funding_watch": [{"threshold_4h": 0.02, "op": "lt"}]}
        out, caveats = CL._normalize_funding_watch(th)
        self.assertIsNone(out[0]["page_label"])
        self.assertEqual(caveats, [])

    def test_page_label_over_40_chars_truncated_with_caveat(self):
        th = {"funding_watch": [{"threshold_4h": 0.02, "op": "lt", "page_label": "y" * 60}]}
        out, caveats = CL._normalize_funding_watch(th)
        self.assertEqual(out[0]["page_label"], "y" * 40)
        self.assertEqual(len(caveats), 1)
        self.assertIn("page_label", caveats[0])


# ── capabilities/classify.py — eval (pure) ───────────────────────────────────────

class TestEvalFundingWatch(unittest.TestCase):
    ENTRIES = [{"threshold_4h": 0.02, "op": "lt", "note": "n"}]

    def test_lt_crosses_below_threshold(self):
        out = CL.eval_funding_watch(self.ENTRIES, 0.018)
        self.assertEqual(out, self.ENTRIES)

    def test_lt_does_not_cross_above_threshold(self):
        out = CL.eval_funding_watch(self.ENTRIES, 0.03)
        self.assertEqual(out, [])

    def test_gt_crosses_above_threshold(self):
        entries = [{"threshold_4h": -0.3, "op": "gt", "note": ""}]
        out = CL.eval_funding_watch(entries, -0.1)
        self.assertEqual(out, entries)

    def test_funding_4h_none_never_false_crosses(self):
        self.assertEqual(CL.eval_funding_watch(self.ENTRIES, None), [])

    def test_floor_only_venues_with_one_real_venue_uses_the_resolved_rate(self):
        # regime_flip.live_perp already floor-rejects and resolves the cross-venue rate
        # (§3) BEFORE it ever reaches this function — eval_funding_watch just consumes
        # whatever funding_4h it's handed. A resolved +0.04 (the real venue, floors
        # excluded) must not false-cross a lt 0.02 watch.
        out = CL.eval_funding_watch(self.ENTRIES, 0.04)
        self.assertEqual(out, [])


# ── capabilities/classify.py — classify_token integration ───────────────────────

def _rng(high, low, window="klines:binance:50x60m"):
    return {"high": high, "low": low, "window": window, "candles": None}


def _live(funding_4h, price=1.0):
    """Full live_perp-shaped fixture — regime_flip.classify needs funding_pi/interval_min/
    etc. even though this test only cares about the funding_4h value the funding leg reads."""
    return {"venue": "binance", "primary_venue": "binance", "price": price, "chg24": 0.0,
            "vol_m": 40.0, "oi": 1e6, "funding_pi": funding_4h / 2.0, "funding_4h": funding_4h,
            "interval_min": 480, "funding_stale": False, "all_floor": False, "funding_split": False}


class ClassifyTokenFundingLeg(unittest.TestCase):
    def setUp(self):
        self._orig_scan = CL.scan_trigger_logs
        self._orig_range = CL.price_window_range
        CL.scan_trigger_logs = lambda ticker, since_epoch=None: ([], [])
        CL.price_window_range = lambda *a, **k: None   # no price_gate/watch_levels in these fixtures

    def tearDown(self):
        CL.scan_trigger_logs = self._orig_scan
        CL.price_window_range = self._orig_range

    def _tok(self, funding_watch, committed_ts="2020-01-01"):
        return {"ticker": "AEON", "state": "watch",
                "thesis": {"direction": "WATCH", "status": "WATCH",
                          "stop": None, "tp": None, "entry_zone": None,
                          "committed_ts": committed_ts, "funding_watch": funding_watch}}

    def test_breach_populates_funding_leg_and_verdict_watch_armed(self):
        tok = self._tok([{"threshold_4h": 0.02, "op": "lt", "note": "arming leg"}])
        live = _live(0.018)
        r = CL.classify_token(tok, live)
        self.assertIsNotNone(r["funding_leg"])
        self.assertEqual(r["verdict"], CL.VERDICT_WATCH_ARMED)
        self.assertIn("arming leg", r["reason"])

    def test_no_breach_leaves_funding_leg_none(self):
        tok = self._tok([{"threshold_4h": 0.02, "op": "lt", "note": "n"}])
        live = _live(0.05)
        r = CL.classify_token(tok, live)
        self.assertIsNone(r["funding_leg"])

    def test_already_true_at_commit_tagged(self):
        recent = datetime.now(timezone.utc).isoformat()
        tok = self._tok([{"threshold_4h": 0.02, "op": "lt", "note": "n"}], committed_ts=recent)
        live = _live(0.018)
        r = CL.classify_token(tok, live)
        self.assertTrue(r["funding_leg"]["already_true_at_commit"])
        self.assertIn("already true at commit", r["reason"])

    def test_old_commit_breach_not_tagged_already_true(self):
        tok = self._tok([{"threshold_4h": 0.02, "op": "lt", "note": "n"}], committed_ts="2020-01-01")
        live = _live(0.018)
        r = CL.classify_token(tok, live)
        self.assertFalse(r["funding_leg"]["already_true_at_commit"])

    def test_malformed_entry_surfaced_in_row_caveats(self):
        tok = self._tok([0.02])
        live = _live(0.018)
        r = CL.classify_token(tok, live)
        self.assertEqual(len(r["funding_watch_caveats"]), 1)
        self.assertIsNone(r["funding_leg"])   # the malformed entry never arms anything

    def test_no_funding_watch_field_byte_identical_no_caveats(self):
        tok = self._tok(None)
        live = _live(0.018)
        r = CL.classify_token(tok, live)
        self.assertIsNone(r["funding_leg"])
        self.assertEqual(r["funding_watch_caveats"], [])


# ── capabilities/classify.py — fire_funding_armed (inbox dedup) ─────────────────

class FireFundingArmed(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state_dir = CL.STATE_DIR
        self._orig_cursor = CL.FUNDING_WATCH_CURSOR
        CL.STATE_DIR = Path(self._tmp.name)
        CL.FUNDING_WATCH_CURSOR = CL.STATE_DIR / "funding_watch_cursor.json"
        import inbox
        self._orig_ib = (inbox.LOG_PATH, inbox.EVENTS_PATH, inbox.CURSOR_PATH)
        inbox.LOG_PATH = CL.STATE_DIR / "nonce_alerts.log"
        inbox.EVENTS_PATH = CL.STATE_DIR / "inbox_events.jsonl"
        inbox.CURSOR_PATH = CL.STATE_DIR / "inbox_cursor.json"

    def tearDown(self):
        CL.STATE_DIR = self._orig_state_dir
        CL.FUNDING_WATCH_CURSOR = self._orig_cursor
        import inbox
        inbox.LOG_PATH, inbox.EVENTS_PATH, inbox.CURSOR_PATH = self._orig_ib
        self._tmp.cleanup()

    def _row(self, ticker="AEON", breached=None, funding_4h=0.018, already=False):
        fl = None
        if breached is not None:
            fl = {"breached": breached, "funding_4h": funding_4h, "already_true_at_commit": already}
        return {"ticker": ticker, "funding_leg": fl}

    def test_fires_once_per_key(self):
        w = [{"threshold_4h": 0.02, "op": "lt", "note": "n"}]
        n1 = CL.fire_funding_armed([self._row(breached=w)])
        n2 = CL.fire_funding_armed([self._row(breached=w)])
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)

    def test_no_funding_leg_no_fire(self):
        n = CL.fire_funding_armed([self._row(breached=None)])
        self.assertEqual(n, 0)


# ── ops/board_tick.py — funding_watch rides the watch_level re-arm semantics ─────

BT = _load("board_tick", sub="ops")
IB = BT.inbox


def bt_row(ticker, verdict="WATCH-ARMED", funding_leg=None):
    return {"ticker": ticker, "verdict": verdict,
            "price_leg": {"stop_breached": False, "tps_printed": []},
            "watch_leg": None, "thesis_drift": None, "funding_leg": funding_leg}


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig_bt = (BT.BASELINE_PATH, BT.LOCK_PATH, BT.ERR_PATH, BT.NOTIFY_STATE_PATH)
        BT.BASELINE_PATH = d / "board_last.json"
        BT.LOCK_PATH = d / "board_tick.lock"
        BT.ERR_PATH = d / "board_tick.err"
        BT.NOTIFY_STATE_PATH = d / "notify_sent.json"
        self._orig_ib = (IB.LOG_PATH, IB.EVENTS_PATH, IB.CURSOR_PATH)
        IB.LOG_PATH = d / "nonce_alerts.log"
        IB.EVENTS_PATH = d / "inbox_events.jsonl"
        IB.CURSOR_PATH = d / "inbox_cursor.json"
        self._orig_ob = (BT.onboard.WALLETS, BT.onboard.WL_PATH)
        BT.onboard.WALLETS = d / "tracked_wallets.json"
        BT.onboard.WL_PATH = d / "watchlist.json"
        BT.onboard.WALLETS.write_text('{"tokens": {}}')
        BT.onboard.WL_PATH.write_text('{"tokens": []}')
        self._orig_tw = BT.tape_watch.run_tick
        BT.tape_watch.run_tick = lambda *a, **k: {"checked": 0, "events": 0, "paged": 0, "detail": []}
        self._orig_oic = BT.oic_watch.run_tick
        BT.oic_watch.run_tick = lambda *a, **k: {"checked": 0, "events": 0, "paged": 0, "detail": []}
        self._orig_wl = BT.load_thesis_watchlist
        BT.load_thesis_watchlist = lambda: ([], None)
        import os
        self._orig_env = os.environ.pop("CRIMEDESK_NOTIFY", None)
        self.notes = []
        self.notify = lambda title, msg: self.notes.append((title, msg))

    def tearDown(self):
        BT.BASELINE_PATH, BT.LOCK_PATH, BT.ERR_PATH, BT.NOTIFY_STATE_PATH = self._orig_bt
        IB.LOG_PATH, IB.EVENTS_PATH, IB.CURSOR_PATH = self._orig_ib
        BT.onboard.WALLETS, BT.onboard.WL_PATH = self._orig_ob
        BT.tape_watch.run_tick = self._orig_tw
        BT.oic_watch.run_tick = self._orig_oic
        BT.load_thesis_watchlist = self._orig_wl
        import os
        if self._orig_env is not None:
            os.environ["CRIMEDESK_NOTIFY"] = self._orig_env
        else:
            os.environ.pop("CRIMEDESK_NOTIFY", None)
        self.dir.cleanup()

    def seed(self, *rows):
        BT.BASELINE_PATH.write_text(BT.json.dumps(BT.snapshot(list(rows))))


class TestFundingWatchPushDedup(_Tmp):
    BREACH = [{"threshold_4h": 0.02, "op": "lt", "note": "n"}]

    def _fl(self, funding_4h):
        return {"breached": self.BREACH, "funding_4h": funding_4h, "already_true_at_commit": False}

    def test_page_once_no_repage_same_reading(self):
        self.seed(bt_row("AEON"))
        BT.tick(classify_fn=lambda: [bt_row("AEON", funding_leg=self._fl(0.018))],
               notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("AEON", self.notes[0][0])
        BT.tick(classify_fn=lambda: [bt_row("AEON", funding_leg=self._fl(0.018))],
               notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)   # still armed, same episode — no re-page

    def test_rearm_after_leaving_and_returning(self):
        self.seed(bt_row("AEON"))
        BT.tick(classify_fn=lambda: [bt_row("AEON", funding_leg=self._fl(0.018))],
               notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        # rate cools back above the threshold — condition clears, no funding_leg this tick
        BT.tick(classify_fn=lambda: [bt_row("AEON", funding_leg=None)], notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)   # cleared, no new page
        # rate crosses back under the threshold — fresh episode, re-arms and pages again
        BT.tick(classify_fn=lambda: [bt_row("AEON", funding_leg=self._fl(0.015))],
               notify_fn=self.notify)
        self.assertEqual(len(self.notes), 2)

    def test_grammar_title_and_priority(self):
        self.seed(bt_row("BLESS"))
        BT.tick(classify_fn=lambda: [bt_row("BLESS", funding_leg=self._fl(0.018))],
               notify_fn=self.notify)
        title, body = self.notes[0]
        self.assertTrue(title.startswith("👁 BLESS ARMED"))
        self.assertIn("0.018", body)
        self.assertIn("lt", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
