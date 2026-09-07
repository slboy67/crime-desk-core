#!/usr/bin/env python3
"""SPEC-111 — push watch-level crossings and HIGH alerts to the user (silent-breach fix).

board_tick already produces the events (verdict deltas, SPEC-106 tape pages, SPEC-77/91
watch_level/thesis_drift reads); the gap was DELIVERY between sessions. Covers:
  - ops/notify.sh: osascript + optional ntfy.sh POST, kill switch (mocked binaries on PATH).
  - board_tick wiring: exactly the 4 named event classes push (watch_level crossing, verdict
    ->TRIGGERS/BREAKS, STOP-BREACHED, tape_watch/thesis_drift at HIGH) — TP prints and other
    MED noise do not; 6h dedup per (ticker, event_class, level); CRIMEDESK_NOTIFY=off silences
    board_tick's own delivery calls too (not just notify.sh's).
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


BT = _load("board_tick", sub="ops")
IB = BT.inbox


# ── ops/notify.sh — direct shell tests, mocked osascript/curl on PATH ────────────

class _StubbedPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.osascript_log = d / "osascript.log"
        self.curl_log = d / "curl.log"
        osa = d / "osascript"
        osa.write_text(f'#!/bin/bash\necho "$@" >> "{self.osascript_log}"\n')
        osa.chmod(0o755)
        curl = d / "curl"
        curl.write_text(f'#!/bin/bash\necho "$@" >> "{self.curl_log}"\n')
        curl.chmod(0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = f"{d}:{self.env.get('PATH', '')}"
        self.env.pop("CRIMEDESK_NOTIFY", None)
        self.env.pop("CRIMEDESK_NTFY_URL", None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_notify(self, title, body, extra_env=None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["bash", str(NOTIFY_SH), title, body],
                              env=env, capture_output=True, text=True, timeout=10)


class TestNotifyShDelivery(_StubbedPath):
    def test_invokes_osascript_with_composed_title_body(self):
        self.run_notify("crime-desk M", "TRIGGERS - re-check now $0.031")
        self.assertTrue(self.osascript_log.exists())
        content = self.osascript_log.read_text()
        self.assertIn("crime-desk M", content)
        self.assertIn("TRIGGERS - re-check now", content)

    def test_ntfy_url_set_also_posts(self):
        self.run_notify("crime-desk M", "BREAKS", extra_env={"CRIMEDESK_NTFY_URL": "https://ntfy.sh/test"})
        self.assertTrue(self.curl_log.exists())
        self.assertIn("https://ntfy.sh/test", self.curl_log.read_text())

    def test_no_ntfy_url_no_curl_call(self):
        self.run_notify("crime-desk M", "BREAKS")
        self.assertFalse(self.curl_log.exists())

    def test_kill_switch_zero_invocations(self):
        self.run_notify("t", "b", extra_env={"CRIMEDESK_NOTIFY": "off"})
        self.assertFalse(self.osascript_log.exists())
        self.assertFalse(self.curl_log.exists())

    def test_never_raises_never_blocks(self):
        # bad URL / no network still exits 0 — best-effort
        proc = self.run_notify("t", "b", extra_env={"CRIMEDESK_NTFY_URL": "http://127.0.0.1:1/nope"})
        self.assertEqual(proc.returncode, 0)


# ── board_tick wiring ─────────────────────────────────────────────────────────────

def row(ticker, verdict, stop_breached=False, tps=(), watch_leg=None, thesis_drift=None):
    return {"ticker": ticker, "verdict": verdict,
            "price_leg": {"stop_breached": stop_breached, "tps_printed": list(tps)},
            "watch_leg": watch_leg, "thesis_drift": thesis_drift}


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
        self._orig_env = os.environ.pop("CRIMEDESK_NOTIFY", None)
        self.notes = []
        self.notify = lambda title, msg: self.notes.append((title, msg))

    def tearDown(self):
        BT.BASELINE_PATH, BT.LOCK_PATH, BT.ERR_PATH, BT.NOTIFY_STATE_PATH = self._orig_bt
        IB.LOG_PATH, IB.EVENTS_PATH, IB.CURSOR_PATH = self._orig_ib
        BT.onboard.WALLETS, BT.onboard.WL_PATH = self._orig_ob
        BT.tape_watch.run_tick = self._orig_tw
        BT.oic_watch.run_tick = self._orig_oic
        if self._orig_env is not None:
            os.environ["CRIMEDESK_NOTIFY"] = self._orig_env
        else:
            os.environ.pop("CRIMEDESK_NOTIFY", None)
        self.dir.cleanup()

    def seed(self, *rows):
        BT.BASELINE_PATH.write_text(BT.json.dumps(BT.snapshot(list(rows))))


class TestEventClassFiltering(_Tmp):
    def test_breaks_transition_delivers(self):
        self.seed(row("VELVET", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("VELVET", "BREAKS", stop_breached=True)],
               notify_fn=self.notify)
        # verdict->BREAKS AND stop-breach are both allowed classes
        self.assertEqual(len(self.notes), 2)

    def test_triggers_transition_delivers(self):
        self.seed(row("LAB", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)

    def test_tp_print_does_not_deliver(self):
        self.seed(row("SIREN", "TRIGGERS", tps=[0.5]))
        BT.tick(classify_fn=lambda: [row("SIREN", "TRIGGERS", tps=[0.5, 0.45])],
               notify_fn=self.notify)
        self.assertEqual(self.notes, [])   # TP print is inbox-only, never pushed (SPEC-111)

    def test_watch_level_crossing_delivers_even_though_med(self):
        self.seed(row("BIRB", "CONFIRMS"))
        wl = {"breached": [{"price": 0.0865, "dir": "below", "note": "short-convert"}],
              "high": 0.09, "low": 0.086}
        BT.tick(classify_fn=lambda: [row("BIRB", "CONFIRMS", watch_leg=wl)],
               notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("BIRB", self.notes[0][0])

    def test_thesis_drift_stale_delivers_no_push(self):
        """SPEC-154: thesis_drift is inbox-only — the phone leg is dropped entirely
        (12 pushes/7d, none actionable at page time). The inbox record itself still
        writes, but that's classify.fire_thesis_drift's job (tests/test_thesis_drift.py),
        unchanged by this spec and never routed through board_tick's notify_fn."""
        self.seed(row("LAB", "CONFIRMS"))
        drift = {"stale": True, "live_price": 12.26, "nearest_anchor": 16.69,
                 "nearest_dist_pct": 26.5, "furthest_anchor": 17.7, "all_tps_printed": True,
                 "side": "SHORT"}
        BT.tick(classify_fn=lambda: [row("LAB", "CONFIRMS", thesis_drift=drift)],
               notify_fn=self.notify)
        self.assertEqual(self.notes, [])

    def test_thesis_drift_not_stale_does_not_deliver(self):
        self.seed(row("LAB", "CONFIRMS"))
        drift = {"stale": False, "live_price": 16.0, "nearest_anchor": 16.69,
                 "nearest_dist_pct": 4.0, "furthest_anchor": 17.7, "all_tps_printed": False,
                 "side": "SHORT"}
        BT.tick(classify_fn=lambda: [row("LAB", "CONFIRMS", thesis_drift=drift)],
               notify_fn=self.notify)
        self.assertEqual(self.notes, [])

    def test_tape_watch_high_delivers_med_does_not(self):
        BT.tape_watch.run_tick = lambda *a, **k: {
            "checked": 1, "events": 2, "paged": 2,
            "detail": [{"ticker": "BIRB", "severity": "HIGH", "msg": "BIRB tape: operator_profit_take", "paged": True},
                      {"ticker": "OTHER", "severity": "MED", "msg": "OTHER tape: drift", "paged": True}]}
        self.seed(row("LAB", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("LAB", "CONFIRMS")], notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("BIRB", self.notes[0][0])


class TestDedup(_Tmp):
    def test_same_event_twice_within_6h_delivers_once(self):
        self.seed(row("BIRB", "CONFIRMS"))
        wl = {"breached": [{"price": 0.0865, "dir": "below", "note": ""}], "high": 0.09, "low": 0.086}
        now = [1_000_000.0]
        BT.tick(classify_fn=lambda: [row("BIRB", "CONFIRMS", watch_leg=wl)],
               notify_fn=self.notify, now_fn=lambda: now[0])
        now[0] += 60      # 1 minute later — still breached, still within 6h
        BT.tick(classify_fn=lambda: [row("BIRB", "CONFIRMS", watch_leg=wl)],
               notify_fn=self.notify, now_fn=lambda: now[0])
        self.assertEqual(len(self.notes), 1)

    def test_continuously_armed_does_not_redeliver_after_6h(self):
        """SPEC-154: the 6h TTL re-ping is gone — a key delivers exactly on transition
        to armed and never again while continuously armed. Redelivery requires the
        condition to CLEAR (disarm) and re-fire, not the passage of any window."""
        self.seed(row("BIRB", "CONFIRMS"))
        wl = {"breached": [{"price": 0.0865, "dir": "below", "note": ""}], "high": 0.09, "low": 0.086}
        now = [1_000_000.0]
        BT.tick(classify_fn=lambda: [row("BIRB", "CONFIRMS", watch_leg=wl)],
               notify_fn=self.notify, now_fn=lambda: now[0])
        now[0] += 6 * 3600 + 1
        BT.tick(classify_fn=lambda: [row("BIRB", "CONFIRMS", watch_leg=wl)],
               notify_fn=self.notify, now_fn=lambda: now[0])
        self.assertEqual(len(self.notes), 1)

    def test_recross_after_leaving_zone_redelivers_within_window(self):
        self.seed(row("BIRB", "CONFIRMS"))
        wl = {"breached": [{"price": 0.0865, "dir": "below", "note": ""}], "high": 0.09, "low": 0.086}
        now = [1_000_000.0]
        BT.tick(classify_fn=lambda: [row("BIRB", "CONFIRMS", watch_leg=wl)],
               notify_fn=self.notify, now_fn=lambda: now[0])
        now[0] += 60
        # price left the zone this tick (watch_leg breached list empty / None)
        BT.tick(classify_fn=lambda: [row("BIRB", "CONFIRMS", watch_leg=None)],
               notify_fn=self.notify, now_fn=lambda: now[0])
        now[0] += 60
        # re-crossed — well within the 6h window, but a fresh episode re-arms
        BT.tick(classify_fn=lambda: [row("BIRB", "CONFIRMS", watch_leg=wl)],
               notify_fn=self.notify, now_fn=lambda: now[0])
        self.assertEqual(len(self.notes), 2)


class TestKillSwitch(_Tmp):
    def test_crimedesk_notify_off_zero_invocations(self):
        os.environ["CRIMEDESK_NOTIFY"] = "off"
        self.seed(row("VELVET", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("VELVET", "BREAKS", stop_breached=True)],
               notify_fn=self.notify)
        self.assertEqual(self.notes, [])
        # inbox events still recorded — only DELIVERY is silenced
        self.assertGreater(len(IB.unconsumed()), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
