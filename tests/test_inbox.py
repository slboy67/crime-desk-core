#!/usr/bin/env python3
"""SPEC 45 — `inbox`: alert ingestion so surveillance events actually reach the board.

Run:  python3 tests/test_inbox.py

ops/surveil.sh writes nonce escalations to state/nonce_alerts.log and nothing read it —
the desk's only push channel dead-ended in a log file. Contract under test:
  - inbox '{}' returns unconsumed events [{ts, ticker, source, severity, msg}] parsed
    from nonce_alerts.log plus the normalized inbox_events.jsonl feed (SPEC 46 producer);
  - ack {through_ts} advances an atomic cursor; acked events stop appearing;
  - classify board rows carry alerts:{n, max_severity} + a reason note on HIGH —
    the VERDICT is never overridden (§0.5: the engine surfaces, the Designer judges).
All paths monkeypatched into a tmpdir — offline, deterministic.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


IB = _load("inbox")

# a real surveil.sh ESCALATION line: ISO ts, marker, count, then the orchestrator envelope
_ENVELOPE = json.dumps({
    "ok": True,
    "data": {"scanned": 3,
             "alerts": [{"ticker": "LAB", "signal": "ESCALATION",
                         "escalation_fired": [{"label": "team-safe-2", "tier": "distribution",
                                               "nonce_prev": 4, "nonce_now": 7}],
                         "escalation_kind": "execution"}],
             "loading": [], "board": []},
    "meta": {"capability": "onchain_board", "ms": 1200},
})
ESCALATION_LINE = f"2026-06-10T08:00:00Z ESCALATION x1 {_ENVELOPE}"
SWEEP_ERR_LINE = "2026-06-10T09:00:00Z SWEEP_ERROR (see state/surveil.err)"


class _Tmp(unittest.TestCase):
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

    def write_log(self, *lines):
        IB.LOG_PATH.write_text("\n".join(lines) + "\n")


class TestParse(_Tmp):
    def test_escalation_line_becomes_high_event(self):
        self.write_log(ESCALATION_LINE)
        events = IB.unconsumed()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["ticker"], "LAB")
        self.assertEqual(e["severity"], "HIGH")
        self.assertEqual(e["source"], "nonce_surveil")
        self.assertEqual(e["ts"], "2026-06-10T08:00:00Z")
        self.assertIn("team-safe-2", e["msg"])

    def test_sweep_error_is_low_no_ticker(self):
        self.write_log(SWEEP_ERR_LINE)
        events = IB.unconsumed()
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["ticker"])
        self.assertEqual(events[0]["severity"], "LOW")

    def test_empty_or_missing_log_is_quiet(self):
        self.assertEqual(IB.unconsumed(), [])
        self.write_log("")
        self.assertEqual(IB.unconsumed(), [])

    def test_garbage_lines_skipped_not_fatal(self):
        self.write_log("not a real line", ESCALATION_LINE)
        events = IB.unconsumed()
        self.assertEqual(len(events), 1)


class TestEventsFeed(_Tmp):
    def test_append_event_surfaces(self):
        # SPEC 46 producer path: board_tick appends normalized events
        IB.append_event(ts="2026-06-10T10:00:00Z", ticker="VELVET", source="board_tick",
                        severity="HIGH", msg="verdict CONFIRMS->BREAKS (stop printed)")
        events = IB.unconsumed()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "board_tick")
        self.assertEqual(events[0]["ticker"], "VELVET")

    def test_sources_merge_sorted_by_ts(self):
        self.write_log(ESCALATION_LINE)
        IB.append_event(ts="2026-06-10T07:00:00Z", ticker="SIREN", source="board_tick",
                        severity="MED", msg="TP1 printed")
        events = IB.unconsumed()
        self.assertEqual([e["ticker"] for e in events], ["SIREN", "LAB"])


class TestAck(_Tmp):
    def test_ack_clears_through_ts(self):
        self.write_log(ESCALATION_LINE)
        IB.append_event(ts="2026-06-10T10:00:00Z", ticker="VELVET", source="board_tick",
                        severity="MED", msg="x")
        out = IB.ack("2026-06-10T08:00:00Z")          # inclusive: consumes the 08:00 event
        self.assertEqual(out["remaining"], 1)
        left = IB.unconsumed()
        self.assertEqual([e["ticker"] for e in left], ["VELVET"])

    def test_ack_is_atomic_and_persistent(self):
        self.write_log(ESCALATION_LINE)
        IB.ack("2026-06-10T08:00:00Z")
        self.assertEqual(IB.unconsumed(), [])
        # cursor survives a re-read from disk
        cur = json.loads(IB.CURSOR_PATH.read_text())
        self.assertEqual(cur["through_ts"], "2026-06-10T08:00:00Z")


class TestForTicker(_Tmp):
    def test_alerts_summary_for_ticker(self):
        self.write_log(ESCALATION_LINE)
        IB.append_event(ts="2026-06-10T10:00:00Z", ticker="LAB", source="board_tick",
                        severity="MED", msg="TP printed")
        s = IB.alerts_for("LAB")
        self.assertEqual(s["n"], 2)
        self.assertEqual(s["max_severity"], "HIGH")
        self.assertEqual(IB.alerts_for("SKYAI"), {"n": 0, "max_severity": None, "events": []})


class TestClassifyIntegration(_Tmp):
    def setUp(self):
        super().setUp()
        self.CL = _load("classify")
        # point classify's inbox module at the same tmp paths
        self.CL.inbox.LOG_PATH = IB.LOG_PATH
        self.CL.inbox.EVENTS_PATH = IB.EVENTS_PATH
        self.CL.inbox.CURSOR_PATH = IB.CURSOR_PATH

    def _live(self):
        return {"venue": "binance", "primary_venue": "binance", "funding_pi": 0.02,
                "interval_min": 240, "funding_4h": 0.02, "funding_stale": False,
                "all_floor": False, "funding_suspect": False, "funding_split": False,
                "price": 1.0, "chg24": 1.0, "vol_m": 50.0, "oi": 1e6, "venues": {}}

    def test_row_carries_alerts_and_reason_note_verdict_unchanged(self):
        tok = {"ticker": "LAB", "state": "watch"}
        base = self.CL.classify_token(tok, self._live())   # no alerts yet
        self.write_log(ESCALATION_LINE)                    # inject synthetic HIGH alert
        res = self.CL.classify_token(tok, self._live())
        self.assertEqual(res["alerts"]["n"], 1)
        self.assertEqual(res["alerts"]["max_severity"], "HIGH")
        self.assertIn("alert", res["reason"].lower())
        # §0.5: the verdict is data-for-the-Designer, never overridden by an alert
        self.assertEqual(res["verdict"], base["verdict"])

    def test_no_alerts_row_quiet(self):
        res = self.CL.classify_token({"ticker": "LAB", "state": "watch"}, self._live())
        self.assertEqual(res["alerts"], {"n": 0, "max_severity": None})
        self.assertNotIn("alert", res["reason"].lower())

    def test_med_alert_counts_but_no_reason_note(self):
        IB.append_event(ts="2026-06-10T10:00:00Z", ticker="LAB", source="board_tick",
                        severity="MED", msg="TP printed")
        res = self.CL.classify_token({"ticker": "LAB", "state": "watch"}, self._live())
        self.assertEqual(res["alerts"], {"n": 1, "max_severity": "MED"})
        self.assertNotIn("alert", res["reason"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
