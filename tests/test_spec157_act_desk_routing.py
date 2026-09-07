#!/usr/bin/env python3
"""SPEC-157 — ACT vs DESK page routing: the phone only buzzes when the USER must decide.

Two routing classes decided per page at emit time in ops/board_tick.py:
  - ACT (phone: ntfy POST + osascript): ticker has an open position in
    config/positions.json (any event class), OR its thesis is ARMED and the page is an
    entry-condition event (watch_level/funding_watch crossing, or verdict TRIGGERS) that
    can actually name its action from committed thesis fields.
  - DESK (osascript only, no ntfy POST): everything else — GO_LOOK levels on WATCH rows,
    thesis staleness, tape deterioration on un-armed names, and all discovery-tick /
    funding-surveil radar pages.

Covers:
  - ops/board_tick.py: `_route_for` decision helper + `_load_open_position_tickers`,
    wired into `_push` so the route rides through as a 4th notify() arg.
  - ops/notify.sh: 4th `route` arg (act|desk, default act) — `desk` skips the
    CRIMEDESK_NTFY_URL POST but osascript still fires.
  - ops/discovery_tick.sh / ops/funding_surveil.sh: radar/discovery pages pass `desk`
    explicitly.

Offline-deterministic: classify/notify injected, all paths in a tmpdir; notify.sh tests
stub osascript/curl on PATH exactly like tests/test_spec141_page_grammar.py.
"""
import importlib.util
import json
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


# ── ops/board_tick.py — _route_for pure decision ─────────────────────────────────

class TestRouteFor(unittest.TestCase):
    def test_open_position_any_event_class_is_act(self):
        for cls, level in (("watch_level", None), ("funding_watch", None),
                           ("stop_breach", "stop"), ("verdict", "TRIGGERS"),
                           ("verdict", "BREAKS"), ("tape_watch", "high")):
            self.assertEqual(
                BT._route_for("CYS", cls, level, {"CYS"}, thesis_status=None), "act",
                msg=f"{cls}/{level} on an open position must be ACT")

    def test_watch_only_ticker_no_thesis_is_desk(self):
        self.assertEqual(BT._route_for("BIRB", "watch_level", None, set(), None), "desk")

    def test_armed_watch_level_is_act(self):
        self.assertEqual(
            BT._route_for("GALA", "watch_level", None, set(), "ARMED",
                          body="0.00197 crossed ↓0.00197 → short leg, stop 0.00211"),
            "act")

    def test_armed_funding_watch_is_act(self):
        self.assertEqual(
            BT._route_for("AEON", "funding_watch", None, set(), "ARMED",
                          body="funding -0.30%/4h crossed lt -0.20% → convert to entry or dismiss"),
            "act")

    def test_armed_verdict_triggers_is_act(self):
        self.assertEqual(
            BT._route_for("BLESS", "verdict", "TRIGGERS", set(), "ARMED",
                          body="0.0412 in entry 0.04-0.045 → LONG per thesis, stop 0.036"),
            "act")

    def test_armed_verdict_breaks_is_desk_not_an_entry_condition(self):
        self.assertEqual(BT._route_for("BLESS", "verdict", "BREAKS", set(), "ARMED"), "desk")

    def test_armed_tape_watch_is_desk_not_an_entry_condition(self):
        self.assertEqual(BT._route_for("BLESS", "tape_watch", "high", set(), "ARMED"), "desk")

    def test_watch_status_not_armed_is_desk_even_on_entry_event(self):
        self.assertEqual(BT._route_for("BIRB", "watch_level", None, set(), "WATCH"), "desk")

    def test_armed_but_body_cannot_name_action_is_desk(self):
        # page_grammar's own fallback text when the thesis lacks usable fields
        self.assertEqual(
            BT._route_for("NEWNAME", "watch_level", None, set(), "ARMED", body="→ read board"),
            "desk")

    def test_unreadable_positions_degrades_to_act_always(self):
        # open_tickers=None signals "positions.json unreadable" — never degrade to silence
        self.assertEqual(BT._route_for("ANYTHING", "tape_watch", "high", None, "WATCH"), "act")
        self.assertEqual(BT._route_for("ANYTHING", "watch_level", None, None, None, body="→ read board"),
                          "act")


# ── ops/board_tick.py — _load_open_position_tickers ──────────────────────────────

class TestLoadOpenPositionTickers(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self._orig = BT.POSITIONS_PATH
        BT.POSITIONS_PATH = Path(self.dir.name) / "positions.json"

    def tearDown(self):
        BT.POSITIONS_PATH = self._orig
        self.dir.cleanup()

    def test_missing_file_returns_none(self):
        self.assertIsNone(BT._load_open_position_tickers())

    def test_malformed_json_returns_none(self):
        BT.POSITIONS_PATH.write_text("{not json")
        self.assertIsNone(BT._load_open_position_tickers())

    def test_open_rows_without_closed_ts_returned(self):
        BT.POSITIONS_PATH.write_text(json.dumps({
            "positions": [
                {"ticker": "CYS", "stop": 0.578},
                {"ticker": "ESP"},
            ]
        }))
        self.assertEqual(BT._load_open_position_tickers(), {"CYS", "ESP"})

    def test_closed_ts_row_excluded(self):
        BT.POSITIONS_PATH.write_text(json.dumps({
            "positions": [
                {"ticker": "CYS"},
                {"ticker": "FIDA", "closed_ts": "2026-08-19T00:00:00Z"},
            ]
        }))
        self.assertEqual(BT._load_open_position_tickers(), {"CYS"})


# ── ops/board_tick.py — tick()-level integration ─────────────────────────────────

def row(ticker, verdict, stop_breached=False, tps=(), watch_leg=None, funding_leg=None,
        live_price=None):
    r = {"ticker": ticker, "verdict": verdict,
         "price_leg": {"stop_breached": stop_breached, "tps_printed": list(tps)},
         "watch_leg": watch_leg, "funding_leg": funding_leg}
    if live_price is not None:
        r["live_price"] = live_price
    return r


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig_bt = (BT.BASELINE_PATH, BT.LOCK_PATH, BT.ERR_PATH, BT.NOTIFY_STATE_PATH,
                         BT.POSITIONS_PATH)
        BT.BASELINE_PATH = d / "board_last.json"
        BT.LOCK_PATH = d / "board_tick.lock"
        BT.ERR_PATH = d / "board_tick.err"
        BT.NOTIFY_STATE_PATH = d / "notify_sent.json"
        BT.POSITIONS_PATH = d / "positions.json"
        self._orig_ib = (IB.LOG_PATH, IB.EVENTS_PATH, IB.CURSOR_PATH)
        IB.LOG_PATH = d / "nonce_alerts.log"
        IB.EVENTS_PATH = d / "inbox_events.jsonl"
        IB.CURSOR_PATH = d / "inbox_cursor.json"
        self._orig_ob = (BT.onboard.WALLETS, BT.onboard.WL_PATH)
        BT.onboard.WALLETS = d / "tracked_wallets.json"
        BT.onboard.WL_PATH = d / "watchlist.json"
        BT.onboard.WALLETS.write_text('{"tokens": {}}')
        BT.onboard.WL_PATH.write_text('{"tokens": []}')
        # default: a valid, empty positions.json — "no open positions" (a KNOWN state),
        # distinct from missing/malformed ("can't tell" -> degrade to act, see
        # TestMissingPositionsDegradesToAllAct which overrides this).
        BT.POSITIONS_PATH.write_text(json.dumps({"positions": []}))
        self._orig_tw = BT.tape_watch.run_tick
        BT.tape_watch.run_tick = lambda *a, **k: {"checked": 0, "events": 0, "paged": 0, "detail": []}
        # SPEC-180: oic_watch.run_tick otherwise makes a REAL oi_construction sweep
        # (live network) and writes real config/venue_roles.json whenever this test
        # file's fake ARMED theses give it a non-empty universe — isolate it the same
        # way tape_watch is isolated above.
        self._orig_oic = BT.oic_watch.run_tick
        BT.oic_watch.run_tick = lambda *a, **k: {"checked": 0, "events": 0, "paged": 0, "detail": []}
        self._orig_wl = BT.load_thesis_watchlist
        BT.load_thesis_watchlist = lambda: ([], None)
        self._orig_env = os.environ.pop("CRIMEDESK_NOTIFY", None)
        self.notes = []   # (title, msg, priority, route)

        def notify4(title, msg, priority="default", route="act"):
            self.notes.append((title, msg, priority, route))
        self.notify = notify4

    def tearDown(self):
        (BT.BASELINE_PATH, BT.LOCK_PATH, BT.ERR_PATH, BT.NOTIFY_STATE_PATH,
         BT.POSITIONS_PATH) = self._orig_bt
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
        BT.BASELINE_PATH.write_text(json.dumps(BT.snapshot(list(rows))))

    def set_positions(self, *tickers):
        BT.POSITIONS_PATH.write_text(json.dumps(
            {"positions": [{"ticker": t} for t in tickers]}))


class TestPositionTickerAlwaysAct(_Tmp):
    def test_tape_high_on_position_ticker_routes_act(self):
        self.set_positions("CYS")
        BT.tape_watch.run_tick = lambda **kw: {
            "detail": [{"ticker": "CYS", "severity": "HIGH", "msg": "m1", "paged": True}]}
        BT.tick(classify_fn=lambda: [row("CYS", "CONFIRMS")], notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.notes[0][3], "act")

    def test_stop_breach_on_position_ticker_routes_act(self):
        self.set_positions("CYS")
        self.seed(row("CYS", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("CYS", "CONFIRMS", stop_breached=True, live_price=0.57)],
               notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.notes[0][3], "act")


class TestWatchOnlyTickerRoutesDesk(_Tmp):
    def test_same_tape_high_event_on_watch_only_ticker_routes_desk(self):
        # no positions.json entry for this ticker, no ARMED thesis
        BT.tape_watch.run_tick = lambda **kw: {
            "detail": [{"ticker": "BIRB", "severity": "HIGH", "msg": "m1", "paged": True}]}
        BT.tick(classify_fn=lambda: [row("BIRB", "CONFIRMS")], notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.notes[0][3], "desk")
        # still delivered (osascript leg) and still an inbox event class handled — nothing
        # silenced, just routed differently
        self.assertEqual(self.notes[0][0], "⚠️ BIRB TAPE")

    def test_watch_level_go_look_on_unarmed_watch_ticker_routes_desk(self):
        wl = {"breached": [{"price": 0.09, "dir": "above", "note": "go-look"}]}
        BT.load_thesis_watchlist = lambda: ([{"ticker": "BIRB", "thesis": {"status": "WATCH"}}], None)
        BT.tick(classify_fn=lambda: [row("BIRB", "CONFIRMS", watch_leg=wl, live_price=0.091)],
               notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.notes[0][3], "desk")


class TestArmedEntryConditionRoutesAct(_Tmp):
    def test_armed_watch_level_cross_routes_act_with_imperative_body(self):
        wl = {"breached": [{"price": 0.00197, "dir": "below", "note": "fade zone"}]}
        BT.load_thesis_watchlist = lambda: ([{"ticker": "GALA",
                                              "thesis": {"status": "ARMED", "direction": "SHORT",
                                                        "stop": 0.00211}}], None)
        BT.tick(classify_fn=lambda: [row("GALA", "CONFIRMS", watch_leg=wl, live_price=0.00196)],
               notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        title, body, priority, route = self.notes[0]
        self.assertEqual(route, "act")
        self.assertNotIn("read board", body)

    def test_armed_verdict_triggers_routes_act(self):
        BT.load_thesis_watchlist = lambda: ([{"ticker": "BLESS",
                                              "thesis": {"status": "ARMED", "direction": "LONG",
                                                        "stop": 0.036, "tp": [0.052]}}], None)
        self.seed(row("BLESS", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("BLESS", "TRIGGERS", live_price=0.0412)],
               notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.notes[0][3], "act")

    def test_armed_but_unnameable_action_routes_desk(self):
        # ARMED status, verdict TRIGGERS, but the thesis names no direction — page_grammar's
        # page_triggers falls back to "read board", which is DESK by definition (AC-4)
        BT.load_thesis_watchlist = lambda: ([{"ticker": "NEWNAME",
                                              "thesis": {"status": "ARMED"}}], None)
        self.seed(row("NEWNAME", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("NEWNAME", "TRIGGERS", live_price=1.0)],
               notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        self.assertTrue(self.notes[0][1].endswith("read board"))
        self.assertEqual(self.notes[0][3], "desk")


class TestMissingPositionsDegradesToAllAct(_Tmp):
    def test_no_positions_file_at_all_degrades_to_act(self):
        BT.POSITIONS_PATH.unlink()   # override _Tmp's default empty file -> unreadable
        BT.tape_watch.run_tick = lambda **kw: {
            "detail": [{"ticker": "BIRB", "severity": "HIGH", "msg": "m1", "paged": True}]}
        BT.tick(classify_fn=lambda: [row("BIRB", "CONFIRMS")], notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.notes[0][3], "act")

    def test_malformed_positions_file_degrades_to_act(self):
        BT.POSITIONS_PATH.write_text("{not json")
        BT.tape_watch.run_tick = lambda **kw: {
            "detail": [{"ticker": "BIRB", "severity": "HIGH", "msg": "m1", "paged": True}]}
        BT.tick(classify_fn=lambda: [row("BIRB", "CONFIRMS")], notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.notes[0][3], "act")

    def test_legacy_2arg_notify_fn_still_works_route_never_reaches_it(self):
        # backward compat: a notify_fn that only accepts (title, msg) must not crash
        self.set_positions("CYS")
        BT.tape_watch.run_tick = lambda **kw: {
            "detail": [{"ticker": "CYS", "severity": "HIGH", "msg": "m1", "paged": True}]}
        notes2 = []
        BT.tick(classify_fn=lambda: [row("CYS", "CONFIRMS")],
               notify_fn=lambda title, msg: notes2.append((title, msg)))
        self.assertEqual(len(notes2), 1)


# ── ops/notify.sh — 4th `route` arg ───────────────────────────────────────────────

class _StubbedPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.curl_log = d / "curl.log"
        self.osa_log = d / "osa.log"
        osa = d / "osascript"
        osa.write_text(f'#!/bin/bash\necho "$@" >> "{self.osa_log}"\nexit 0\n')
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


class TestNotifyShRoute(_StubbedPath):
    def test_desk_route_skips_ntfy_post(self):
        self.run_notify("t", "b", "", "desk")
        self.assertFalse(self.curl_log.exists(), "desk route must never POST to ntfy")
        self.assertTrue(self.osa_log.exists(), "osascript leg must still fire on desk route")

    def test_act_route_still_posts(self):
        self.run_notify("t", "b", "", "act")
        self.assertTrue(self.curl_log.exists())
        content = self.curl_log.read_text()
        self.assertIn("ntfy.sh/test", content)

    def test_default_route_omitted_still_posts(self):
        self.run_notify("t", "b")
        self.assertTrue(self.curl_log.exists())

    def test_desk_route_with_priority_still_skips_post(self):
        self.run_notify("t", "b", "urgent", "desk")
        self.assertFalse(self.curl_log.exists())
        self.assertTrue(self.osa_log.exists())


# ── ops/discovery_tick.sh + ops/funding_surveil.sh — pass `desk` explicitly ──────

class TestDiscoveryAndFundingSurveilPassDesk(unittest.TestCase):
    def test_discovery_tick_notify_calls_pass_desk(self):
        src = (ROOT / "ops" / "discovery_tick.sh").read_text()
        calls = [ln for ln in src.splitlines() if "ops/notify.sh" in ln and "bash" in ln]
        self.assertTrue(calls, "expected at least one notify.sh call in discovery_tick.sh")
        for ln in calls:
            self.assertIn('"desk"', ln, f"discovery_tick.sh page must route desk: {ln}")

    def test_funding_surveil_notify_calls_pass_desk(self):
        src = (ROOT / "ops" / "funding_surveil.sh").read_text()
        calls = [ln for ln in src.splitlines() if "ops/notify.sh" in ln and "bash" in ln]
        self.assertTrue(calls, "expected at least one notify.sh call in funding_surveil.sh")
        for ln in calls:
            self.assertIn('"desk"', ln, f"funding_surveil.sh page must route desk: {ln}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
