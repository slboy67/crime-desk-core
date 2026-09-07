#!/usr/bin/env python3
"""SPEC-141 — every phone page reads as TICKER · PRICE · WHAT HAPPENED · DO THIS.

Covers the pieces beyond ops/page_grammar.py's own pure-function tests
(tests/test_page_grammar.py):
  - ops/notify.sh: optional 3rd `priority` arg forwards as the ntfy `Priority:` header;
    absent = today's behavior (no header).
  - ops/board_tick.py: push messages are built through page_grammar (title carries the
    emoji+ticker+event-word grammar), severity maps to ntfy priority (BREAKS/stop_breach
    -> urgent, TRIGGERS/watch_level -> high, else default), aster_listed suppression
    (SPEC-136) still holds.
  - ops/balance_surveil.py: the balance-drop message renders human-compact units with a
    signed delta and %, never scientific notation.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTIFY_SH = ROOT / "ops" / "notify.sh"


def _load(name, sub="capabilities"):
    spec = importlib.util.spec_from_file_location(name, ROOT / sub / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── ops/notify.sh — priority header ──────────────────────────────────────────────

class _StubbedPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.curl_log = d / "curl.log"
        osa = d / "osascript"
        osa.write_text('#!/bin/bash\nexit 0\n')
        osa.chmod(0o755)
        curl = d / "curl"
        curl.write_text(f'#!/bin/bash\necho "$@" >> "{self.curl_log}"\n')
        curl.chmod(0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = f"{d}:{self.env.get('PATH', '')}"
        self.env["CRIMEDESK_NTFY_URL"] = "https://ntfy.sh/test"
        self.env.pop("CRIMEDESK_NOTIFY", None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_notify(self, *args):
        return subprocess.run(["bash", str(NOTIFY_SH), *args],
                              env=self.env, capture_output=True, text=True, timeout=10)


class TestNotifyShPriority(_StubbedPath):
    def test_priority_arg_sends_header(self):
        self.run_notify("t", "b", "urgent")
        content = self.curl_log.read_text()
        self.assertIn("Priority: urgent", content)

    def test_no_priority_arg_sends_no_header(self):
        self.run_notify("t", "b")
        content = self.curl_log.read_text()
        self.assertNotIn("Priority:", content)


# ── ops/board_tick.py — grammar + priority wiring ────────────────────────────────

BT = _load("board_tick", sub="ops")
IB = BT.inbox


def row(ticker, verdict, stop_breached=False, tps=(), watch_leg=None, thesis_drift=None,
        aster_listed="__unset__", live_price=None):
    r = {"ticker": ticker, "verdict": verdict,
         "price_leg": {"stop_breached": stop_breached, "tps_printed": list(tps)},
         "watch_leg": watch_leg, "thesis_drift": thesis_drift}
    if aster_listed != "__unset__":
        r["aster_listed"] = aster_listed
    if live_price is not None:
        r["live_price"] = live_price
    return r


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
        BT.load_thesis_watchlist = lambda: ([], None)   # isolate from the real config/watchlist.json
        self._orig_env = os.environ.pop("CRIMEDESK_NOTIFY", None)
        self.notes = []   # (title, msg, priority)

        def notify3(title, msg, priority="default"):
            self.notes.append((title, msg, priority))
        self.notify = notify3

    def tearDown(self):
        BT.BASELINE_PATH, BT.LOCK_PATH, BT.ERR_PATH, BT.NOTIFY_STATE_PATH = self._orig_bt
        IB.LOG_PATH, IB.EVENTS_PATH, IB.CURSOR_PATH = self._orig_ib
        BT.onboard.WALLETS, BT.onboard.WL_PATH = self._orig_ob
        BT.tape_watch.run_tick = self._orig_tw
        BT.oic_watch.run_tick = self._orig_oic
        BT.load_thesis_watchlist = self._orig_wl
        if self._orig_env is not None:
            os.environ["CRIMEDESK_NOTIFY"] = self._orig_env
        else:
            os.environ.pop("CRIMEDESK_NOTIFY", None)
        self.dir.cleanup()

    def seed(self, *rows):
        BT.BASELINE_PATH.write_text(BT.json.dumps(BT.snapshot(list(rows))))


class TestGrammarWiring(_Tmp):
    def test_breaks_title_and_body_use_grammar(self):
        self.seed(row("BEAT", "CONFIRMS"))
        BT.load_thesis_watchlist = lambda: ([{"ticker": "BEAT", "thesis": {"stop": 0.0155}}], None)
        BT.tick(classify_fn=lambda: [row("BEAT", "BREAKS", live_price=0.0142)],
               notify_fn=self.notify)
        titles = [t for t, _m, _p in self.notes]
        self.assertTrue(any(t.startswith("🔴 BEAT BREAKS") for t in titles))
        bodies = [m for _t, m, _p in self.notes]
        self.assertTrue(any("0.0142" in b and "do not chase" in b for b in bodies))

    def test_triggers_title_uses_grammar_and_thesis_action(self):
        self.seed(row("BLESS", "CONFIRMS"))
        BT.load_thesis_watchlist = lambda: ([{"ticker": "BLESS",
                                              "thesis": {"direction": "LONG", "stop": 0.036,
                                                        "tp": [0.052]}}], None)
        BT.tick(classify_fn=lambda: [row("BLESS", "TRIGGERS", live_price=0.0412)],
               notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        title, body, priority = self.notes[0]
        self.assertTrue(title.startswith("🟢 BLESS TRIGGERED"))
        self.assertIn("LONG per thesis", body)
        self.assertEqual(priority, "high")

    def test_watch_level_uses_grammar(self):
        self.seed(row("BIRB", "CONFIRMS"))
        long_note = "desk marginalia with §-refs and dates 2026-08-25 " * 5
        wl = {"breached": [{"price": 0.0865, "dir": "below", "note": long_note}],
              "high": 0.09, "low": 0.086}
        BT.tick(classify_fn=lambda: [row("BIRB", "CONFIRMS", watch_leg=wl, live_price=0.0863)],
               notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        title, body, priority = self.notes[0]
        self.assertTrue(title.startswith("👁 BIRB ARMED"))
        self.assertIn("convert to entry or dismiss", body)
        # SPEC-161: the analyst `note` must NEVER reach the pushed body, however long
        self.assertNotIn("marginalia", body)
        self.assertEqual(priority, "high")

    def test_watch_level_page_label_composes_entry_cross_with_thesis_stop(self):
        """SPEC-161 req 3: the ENTRY? imperative shape, wired end-to-end through
        board_tick (thesis_by_ticker -> page_watch_armed's new thesis param)."""
        self.seed(row("CASHCAT", "CONFIRMS"))
        BT.load_thesis_watchlist = lambda: (
            [{"ticker": "CASHCAT", "thesis": {"stop": 0.1955}}], None)
        wl = {"breached": [{"price": 0.178, "dir": "below", "page_label": "needs HOLD+vol"}],
              "high": 0.18, "low": 0.177}
        BT.tick(classify_fn=lambda: [row("CASHCAT", "CONFIRMS", watch_leg=wl, live_price=0.177,
                                        aster_listed=True)],
               notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        title, body, _priority = self.notes[0]
        self.assertTrue(body.startswith("ENTRY? CASHCAT"))
        self.assertIn("needs HOLD+vol", body)
        self.assertIn("stop 0.1955", body)

    def test_null_thesis_params_read_board(self):
        self.seed(row("NEWNAME", "CONFIRMS"))
        BT.load_thesis_watchlist = lambda: ([{"ticker": "NEWNAME"}], None)   # no thesis block
        BT.tick(classify_fn=lambda: [row("NEWNAME", "TRIGGERS", live_price=1.0)],
               notify_fn=self.notify)
        _title, body, _priority = self.notes[0]
        self.assertTrue(body.endswith("→ read board"))


class TestPriorityMapping(_Tmp):
    def test_stop_breach_is_urgent(self):
        self.seed(row("VELVET", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("VELVET", "CONFIRMS", stop_breached=True, live_price=0.4)],
               notify_fn=self.notify)
        priorities = [p for _t, _m, p in self.notes]
        self.assertIn("urgent", priorities)

    def test_breaks_is_urgent(self):
        self.seed(row("VELVET", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("VELVET", "BREAKS", live_price=0.4)],
               notify_fn=self.notify)
        priorities = [p for _t, _m, p in self.notes]
        self.assertIn("urgent", priorities)


class TestAsterSuppressionStillHolds(_Tmp):
    def test_false_still_suppresses_with_grammar(self):
        self.seed(row("COTI", "CONFIRMS", aster_listed=False))
        BT.tick(classify_fn=lambda: [row("COTI", "BREAKS", aster_listed=False, live_price=0.4)],
               notify_fn=self.notify)
        self.assertEqual(self.notes, [])


# ── ops/balance_surveil.py — human numbers, no scientific notation ──────────────────

BS = _load("balance_surveil", sub="ops")


class TestBalanceSurveilHumanNumbers(unittest.TestCase):
    def test_balance_drop_msg_has_no_scientific_notation(self):
        wallet_cfg = {"address": "0xabc", "label": "SAFE 0xabc", "chain": "binance-smart-chain",
                     "_contract": "0xtoken", "_decimals": 18}
        balance_fn = lambda addr, contract, chain, decimals: {
            "available": True, "value": 1_902_522.0, "cross_checked": True, "agree": True}
        fire, _entry = BS.assess_wallet(wallet_cfg, {"balance": 1_903_240.0}, balance_fn, dust=1.0)
        self.assertIsNotNone(fire)
        self.assertNotRegex(fire["msg"], r"\d[eE][+-]\d")   # no scientific notation
        self.assertIn("1.90M", fire["msg"])
        self.assertIn("%", fire["msg"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
