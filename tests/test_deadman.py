#!/usr/bin/env python3
"""SPEC 64 — surveillance dead-man switch + inbox multi-alert envelope + agent health.

Run:  python3 tests/test_deadman.py   (or)  python3 -m unittest -v tests.test_deadman

The nonce-surveil launchd agent was unloaded for NINE DAYS (last alert 06-03,
restored 06-12) and nothing surfaced it — the Designer kept reading "surveillance
armed" off a board whose watcher was dead. Contract under test:

  1. Dead-man (classify): when the surveil layer's newest tick is older than 2× the
     launchd cadence, EVERY board row's reason is prefixed `[SURVEIL STALE Nh]` and a
     single HIGH inbox event fires per stale-episode (deduped, never per-row spam). A
     fresh heartbeat is silent. No state at all → stale-by-absence prefix, no event.
  2. Inbox envelope: the restored sweep's multi-alert line (`ESCALATION xN` + the raw
     onchain_board JSON, NOT orchestrator-wrapped) parses to one HIGH event per ticker
     — the wrapped-only read returned 0 events for 8 alerts (the silent death).
  3. Agent health (triage): launchctl probe → {coder_dispatch, nonce_surveil,
     board_tick} ∈ loaded|MISSING|unknown — one glance, every scan.

All paths monkeypatched into a tmpdir; launchctl mocked — offline, deterministic.
"""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CL = _load("classify")
IB = _load("inbox")
TR = _load("triage")

NOW = 1_750_000_000.0  # fixed clock so age math is deterministic


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(*tickers):
    return [{"ticker": t, "verdict": "CONFIRMS", "reason": "rf=CONFIRM: quiet"} for t in tickers]


# ── 1. dead-man switch ──────────────────────────────────────────────────────────
class TestSurveilDeadman(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        # classify reads/writes these
        self._cl = (CL.NONCE_LOG, CL.SURVEIL_HEARTBEAT, CL.BOARD_BASELINE, CL.DEADMAN_CURSOR)
        CL.NONCE_LOG = d / "nonce_alerts.log"
        CL.SURVEIL_HEARTBEAT = d / "surveil.heartbeat"
        CL.BOARD_BASELINE = d / "board_last.json"
        CL.DEADMAN_CURSOR = d / "surveil_deadman.json"
        # the dead-man HIGH event flows through classify's inbox module
        self._ib = (CL.inbox.LOG_PATH, CL.inbox.EVENTS_PATH, CL.inbox.CURSOR_PATH)
        CL.inbox.LOG_PATH = d / "nonce_alerts.log"
        CL.inbox.EVENTS_PATH = d / "inbox_events.jsonl"
        CL.inbox.CURSOR_PATH = d / "inbox_cursor.json"

    def tearDown(self):
        CL.NONCE_LOG, CL.SURVEIL_HEARTBEAT, CL.BOARD_BASELINE, CL.DEADMAN_CURSOR = self._cl
        CL.inbox.LOG_PATH, CL.inbox.EVENTS_PATH, CL.inbox.CURSOR_PATH = self._ib
        self.dir.cleanup()

    def _high_events(self):
        return [e for e in CL.inbox.unconsumed() if e["severity"] == "HIGH"]

    def test_stale_log_prefixes_every_row_and_fires_one_event(self):
        old = NOW - 9 * 86400  # nine days dead, like the real incident
        CL.NONCE_LOG.write_text(f"{_iso(old)} ESCALATION x1 {{}}\n")
        rows = _rows("LAB", "VELVET", "SKYAI")
        meta = CL.annotate_deadman(rows, now=NOW)

        self.assertTrue(meta["surveil_stale"])
        self.assertGreater(meta["surveil_age_h"], 200)
        for r in rows:
            self.assertTrue(r["reason"].startswith("[SURVEIL STALE "),
                            f"row {r['ticker']} not prefixed: {r['reason']}")
        ev = self._high_events()
        self.assertEqual(len(ev), 1, "exactly one HIGH dead-man event per stale-episode")
        self.assertEqual(ev[0]["source"], "nonce_surveil")
        self.assertIsNone(ev[0]["ticker"])

    def test_event_fires_once_per_episode_not_per_read(self):
        old = NOW - 9 * 86400
        CL.NONCE_LOG.write_text(f"{_iso(old)} ESCALATION x1 {{}}\n")
        CL.annotate_deadman(_rows("LAB"), now=NOW)
        CL.annotate_deadman(_rows("LAB"), now=NOW + 60)   # second board read, same episode
        self.assertEqual(len(self._high_events()), 1)

    def test_fresh_heartbeat_is_silent(self):
        CL.SURVEIL_HEARTBEAT.write_text(f"{_iso(NOW - 300)} quiet\n")   # 5 min ago
        rows = _rows("LAB", "VELVET")
        meta = CL.annotate_deadman(rows, now=NOW)
        self.assertFalse(meta["surveil_stale"])
        self.assertLess(meta["surveil_age_h"], 0.5)
        for r in rows:
            self.assertFalse(r["reason"].startswith("[SURVEIL STALE"))
        self.assertEqual(self._high_events(), [])

    def test_no_state_is_stale_by_absence_but_fires_no_event(self):
        rows = _rows("LAB")
        meta = CL.annotate_deadman(rows, now=NOW)
        self.assertTrue(meta["surveil_stale"])
        self.assertIsNone(meta["surveil_age_h"])
        self.assertTrue(rows[0]["reason"].startswith("[SURVEIL STALE "))
        self.assertEqual(self._high_events(), [], "no last_epoch → no episode → no event")

    def test_board_baseline_age_in_meta(self):
        CL.SURVEIL_HEARTBEAT.write_text(f"{_iso(NOW - 60)} quiet\n")  # surveil fresh
        CL.BOARD_BASELINE.write_text("{}")                            # exists, just written
        import os
        os.utime(CL.BOARD_BASELINE, (NOW - 9 * 3600, NOW - 9 * 3600))  # stale baseline
        rows = _rows("LAB")
        meta = CL.annotate_deadman(rows, now=NOW)
        self.assertTrue(meta["board_tick_stale"])
        self.assertGreater(meta["board_tick_age_h"], 8)
        self.assertIn("BOARD-TICK STALE", rows[0]["reason"])


# ── 2. inbox multi-alert envelope ───────────────────────────────────────────────
def _raw_onchain_board(tickers):
    """The RAW onchain_board JSON (build_board return value) — NOT orchestrator-wrapped.
    This is the 'full onchain_board JSON' the restored sweep writes; the wrapped-only
    parser read env['data']['alerts'] and found nothing here (the silent death)."""
    alerts = [{"ticker": t, "signal": "ESCALATION",
               "escalation_fired": [{"label": f"{t.lower()}-team-safe", "tier": "distribution",
                                     "nonce_prev": 3, "nonce_now": 4}],
               "escalation_kind": "staging"} for t in tickers]
    return {"scanned": len(tickers), "alerts": alerts, "loading": [],
            "nonce_churn": [], "board": []}


EIGHT = ["VELVET", "LAB", "SKYAI", "ESPORTS", "BILL", "RAVE", "SIREN", "BEAT"]
# the live 06-12 12:08:32Z line: ESCALATION x8 + the raw onchain_board JSON
LIVE_LINE = f"2026-06-12T12:08:32Z ESCALATION x8 {json.dumps(_raw_onchain_board(EIGHT))}"


class TestInboxMultiAlertEnvelope(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig = (IB.LOG_PATH, IB.EVENTS_PATH, IB.CURSOR_PATH)
        IB.LOG_PATH = d / "nonce_alerts.log"
        IB.EVENTS_PATH = d / "inbox_events.jsonl"
        IB.CURSOR_PATH = d / "inbox_cursor.json"

    def tearDown(self):
        IB.LOG_PATH, IB.EVENTS_PATH, IB.CURSOR_PATH = self._orig
        self.dir.cleanup()

    def test_raw_multi_alert_line_parses_to_per_ticker_events(self):
        IB.LOG_PATH.write_text(LIVE_LINE + "\n")
        events = IB.unconsumed()
        self.assertEqual(len(events), 8, "one event per ticker-escalation")
        self.assertEqual([e["ticker"] for e in events], EIGHT)
        for e in events:
            self.assertEqual(e["severity"], "HIGH")
            self.assertEqual(e["source"], "nonce_surveil")
            self.assertEqual(e["ts"], "2026-06-12T12:08:32Z")
            self.assertIn("team-safe", e["msg"])

    def test_dedup_against_cursor(self):
        IB.LOG_PATH.write_text(LIVE_LINE + "\n")
        out = IB.ack("2026-06-12T12:08:32Z")   # inclusive → consumes all 8
        self.assertEqual(out["remaining"], 0)
        self.assertEqual(IB.unconsumed(), [])

    def test_wrapped_envelope_still_parses(self):
        # regression: the orchestrator-wrapped form must keep working
        wrapped = {"ok": True, "data": _raw_onchain_board(["LAB"]), "meta": {}}
        IB.LOG_PATH.write_text(f"2026-06-12T12:08:32Z ESCALATION x1 {json.dumps(wrapped)}\n")
        events = IB.unconsumed()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["ticker"], "LAB")


# ── 3. agent health (triage) ────────────────────────────────────────────────────
class TestAgentHealth(unittest.TestCase):
    def test_probe_maps_to_loaded_missing_unknown(self):
        states = {
            "com.crimedesk.coder-dispatch": True,
            "com.crimedesk.nonce-surveil": False,
            "com.crimedesk.board-tick": None,
        }
        health = TR.agent_health(probe=lambda label: states[label])
        self.assertEqual(health, {"coder_dispatch": "loaded",
                                  "nonce_surveil": "MISSING",
                                  "board_tick": "unknown"})

    def test_missing_launchctl_is_unknown_not_false_missing(self):
        # launchctl absent (non-macOS) must NOT report a false MISSING
        health = TR.agent_health(probe=lambda label: None)
        self.assertEqual(set(health.values()), {"unknown"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
