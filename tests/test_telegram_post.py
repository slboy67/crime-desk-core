#!/usr/bin/env python3
"""SPEC-181 — Telegram public signal channel: post() delivery + format_card() redaction.

Run:  python3 tests/test_telegram_post.py

Contract under test:
  - post(): credentials resolved at send time (env first, then one-line files); missing
    creds or CRIMEDESK_NOTIFY=off is a silent no-op; an HTTP failure logs one line to
    state/telegram.err and never raises.
  - format_card(): allowlist-by-construction — a row loaded with signature/wallet/
    funding prose in triggers/invalidation never leaks any of it; a paramless leg
    returns None.
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, sub="ops"):
    spec = importlib.util.spec_from_file_location(name, ROOT / sub / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TG = _load("telegram_post")


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig_paths = (TG.ERR_PATH, TG.TOKEN_FILE, TG.CHAT_ID_FILE)
        TG.ERR_PATH = d / "telegram.err"
        TG.TOKEN_FILE = d / "telegram_bot_token"
        TG.CHAT_ID_FILE = d / "telegram_chat_id"
        self._orig_http = TG._http_post
        self._env_keys = ("CRIMEDESK_TG_BOT_TOKEN", "CRIMEDESK_TG_CHAT_ID", "CRIMEDESK_NOTIFY")
        self._orig_env = {k: os.environ.get(k) for k in self._env_keys}
        for k in self._env_keys:
            os.environ.pop(k, None)

    def tearDown(self):
        TG.ERR_PATH, TG.TOKEN_FILE, TG.CHAT_ID_FILE = self._orig_paths
        TG._http_post = self._orig_http
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.dir.cleanup()


class TestPost(_Tmp):
    def test_missing_credentials_is_silent_noop(self):
        sent = []
        TG._http_post = lambda url, payload: sent.append((url, payload))
        self.assertFalse(TG.post("hello"))
        self.assertEqual(sent, [])
        self.assertFalse(TG.ERR_PATH.exists())

    def test_env_credentials_send(self):
        os.environ["CRIMEDESK_TG_BOT_TOKEN"] = "TOK123"
        os.environ["CRIMEDESK_TG_CHAT_ID"] = "CHAT456"
        sent = []
        TG._http_post = lambda url, payload: sent.append((url, payload))
        ok = TG.post("hello world")
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        url, payload = sent[0]
        self.assertIn("botTOK123", url)
        self.assertIn(b"CHAT456", payload)
        self.assertIn(b"hello world", payload)
        self.assertIn(b"disable_web_page_preview", payload)

    def test_file_credentials_used_when_env_absent(self):
        TG.TOKEN_FILE.write_text("FILETOK\n")
        TG.CHAT_ID_FILE.write_text("FILECHAT\n")
        sent = []
        TG._http_post = lambda url, payload: sent.append((url, payload))
        ok = TG.post("x")
        self.assertTrue(ok)
        self.assertIn("FILETOK", sent[0][0])

    def test_empty_credential_file_is_noop(self):
        TG.TOKEN_FILE.write_text("")
        TG.CHAT_ID_FILE.write_text("CHAT")
        sent = []
        TG._http_post = lambda url, payload: sent.append((url, payload))
        self.assertFalse(TG.post("x"))
        self.assertEqual(sent, [])

    def test_kill_switch_disables_entirely(self):
        os.environ["CRIMEDESK_TG_BOT_TOKEN"] = "TOK"
        os.environ["CRIMEDESK_TG_CHAT_ID"] = "CHAT"
        os.environ["CRIMEDESK_NOTIFY"] = "off"
        sent = []
        TG._http_post = lambda url, payload: sent.append((url, payload))
        self.assertFalse(TG.post("x"))
        self.assertEqual(sent, [])

    def test_http_failure_logs_one_line_never_raises(self):
        os.environ["CRIMEDESK_TG_BOT_TOKEN"] = "TOK"
        os.environ["CRIMEDESK_TG_CHAT_ID"] = "CHAT"

        def boom(url, payload):
            raise RuntimeError("connection refused")
        TG._http_post = boom
        ok = TG.post("x")
        self.assertFalse(ok)
        lines = TG.ERR_PATH.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("connection refused", lines[0])

    def test_empty_text_is_noop(self):
        os.environ["CRIMEDESK_TG_BOT_TOKEN"] = "TOK"
        os.environ["CRIMEDESK_TG_CHAT_ID"] = "CHAT"
        sent = []
        TG._http_post = lambda url, payload: sent.append((url, payload))
        self.assertFalse(TG.post(""))
        self.assertEqual(sent, [])


class TestFormatCard(unittest.TestCase):
    DIRTY_ROW = {
        "ticker": "SKYAI",
        "event": "TRIGGERS",
        "direction": "SHORT",
        "entry_zone": [0.10, 0.12],
        "stop": 0.135,
        "tp": [0.08, 0.06],
        "signature": "stage5_short",
        "triggers": [{"type": "held_break", "note": "seller 0x1234abcd5678 dumping"}],
        "invalidation": {"note": "funding flips positive on the retest, farm unwind"},
        "notes": "operator wallet 0xdeadbeef is the real seller",
        "page_grammar": "🔴 SKYAI BREAKS — invalidation hit",
        "tier": "GO",
        "sizing": "10% equity",
    }

    def test_allowlist_redaction_by_construction(self):
        card = TG.format_card(self.DIRTY_ROW)
        self.assertIsNotNone(card)
        self.assertIn("SKYAI", card)
        self.assertIn("SHORT", card)
        self.assertIn("0.1", card)   # levels present in some form
        for forbidden in ("0x", "stage5_short", "farm unwind", "operator wallet",
                          "page_grammar", "GO", "10% equity"):
            self.assertNotIn(forbidden, card)

    def test_event_labels(self):
        base = {"ticker": "X", "stop": 1.0}
        self.assertIn("ENTRY TRIGGERED", TG.format_card({**base, "event": "TRIGGERS"}))
        self.assertIn("THESIS INVALIDATED", TG.format_card({**base, "event": "BREAKS"}))
        self.assertIn("STOPPED OUT", TG.format_card({**base, "event": "STOP_BREACH"}))

    def test_unknown_event_returns_none(self):
        self.assertIsNone(TG.format_card({"ticker": "X", "event": "TP_PRINT", "stop": 1.0}))

    def test_paramless_leg_returns_none(self):
        row = {"ticker": "X", "event": "TRIGGERS", "direction": "LONG",
               "entry_zone": None, "stop": None, "tp": []}
        self.assertIsNone(TG.format_card(row))

    def test_missing_ticker_returns_none(self):
        self.assertIsNone(TG.format_card({"event": "TRIGGERS", "stop": 1.0}))

    def test_direction_absent_still_produces_card_without_direction_word(self):
        row = {"ticker": "X", "event": "STOP_BREACH", "stop": 1.0}
        card = TG.format_card(row)
        self.assertIsNotNone(card)
        self.assertIn("X — STOPPED OUT", card)

    def test_malformed_entry_zone_ignored_not_crashing(self):
        row = {"ticker": "X", "event": "TRIGGERS", "entry_zone": "not-a-pair", "stop": 1.0}
        card = TG.format_card(row)
        self.assertIsNotNone(card)
        self.assertIn("Stop", card)
        self.assertNotIn("Entry", card)

    def test_leg_field_prefixes_body_with_leg_label(self):
        row = {"ticker": "ACE", "event": "TRIGGERS", "direction": "LONG",
               "entry_zone": [0.181, 0.187], "stop": 0.155, "tp": [0.2205, 0.245], "leg": 1}
        card = TG.format_card(row)
        self.assertIn("Leg 1", card)

    def test_no_leg_field_omits_leg_label(self):
        row = {"ticker": "X", "event": "TRIGGERS", "stop": 1.0}
        card = TG.format_card(row)
        self.assertNotIn("Leg", card)


class TestHasLegGeometry(unittest.TestCase):
    def test_top_level_numeric_field_is_geometry(self):
        self.assertTrue(TG.has_leg_geometry({"stop": 1.0}))
        self.assertTrue(TG.has_leg_geometry({"entry_zone": [1.0, 1.1]}))
        self.assertTrue(TG.has_leg_geometry({"tp": [1.2]}))

    def test_leg_entry_zone_is_geometry(self):
        th = {"legs": [{"name": "1", "entry_zone": [0.18, 0.19]}]}
        self.assertTrue(TG.has_leg_geometry(th))

    def test_leg_entry_ref_is_geometry(self):
        th = {"legs": [{"kind": "momentum", "entry_ref": 0.681}]}
        self.assertTrue(TG.has_leg_geometry(th))

    def test_paramless_watch_has_no_geometry(self):
        th = {"direction": "WATCH", "entry_zone": None, "stop": None, "tp": []}
        self.assertFalse(TG.has_leg_geometry(th))

    def test_empty_or_none_thesis_has_no_geometry(self):
        self.assertFalse(TG.has_leg_geometry(None))
        self.assertFalse(TG.has_leg_geometry({}))


class TestFormatSetupCard(unittest.TestCase):
    ACE_THESIS = {
        "direction": "LONG",
        "status": "LIVE",
        "signature": "trap_formation_long",
        "conviction": "low-medium, funding CVD wallet reasoning lives here",
        "setup": "funding CVD wallet reasoning lives here too",
        "entry_zone": None,
        "stop": None,
        "tp": [],
        "time_stop": "2026-09-04T21:00:00Z",
        "legs": [
            {"name": "1-retest", "side": "long", "entry_zone": [0.181, 0.187],
             "stop": 0.155, "tp": [0.2205, 0.245],
             "gate": "funding CVD wallet pullback into the shelf"},
            {"name": "2-momentum", "side": "long", "entry_zone": [0.221, 0.226],
             "stop": 0.196, "tp": [0.245, 0.256],
             "trigger_timeframe": "1h", "trigger_op": "above", "entry_ref": 0.2210,
             "gate": "Binance 1h close > 0.2210"},
        ],
    }

    def test_two_leg_card_shows_both_legs(self):
        card = TG.format_setup_card("ACE", self.ACE_THESIS)
        self.assertIsNotNone(card)
        self.assertIn("NEW SETUP · ACE · LONG", card)
        self.assertIn("Leg 1", card)
        self.assertIn("Leg 2", card)
        self.assertIn("0.181", card)
        self.assertIn("0.187", card)
        self.assertIn("0.155", card)
        self.assertIn("0.221", card)
        self.assertIn("Valid until 2026-09-04 21:00 UTC", card)

    def test_trigger_line_from_numeric_fields_only(self):
        card = TG.format_setup_card("ACE", self.ACE_THESIS)
        self.assertIn("Trigger: 1h close above 0.221", card)

    def test_trigger_line_omitted_without_structured_fields(self):
        card = TG.format_setup_card("ACE", self.ACE_THESIS)
        # leg 1 has no trigger_timeframe/trigger_op -> no "Trigger:" attached to its line
        leg1_line = next(l for l in card.splitlines() if l.startswith("Leg 1"))
        self.assertNotIn("Trigger", leg1_line)

    def test_negative_word_assertion_gate_prose_never_leaks(self):
        card = TG.format_setup_card("ACE", self.ACE_THESIS)
        for forbidden in ("funding", "wallet", "CVD", "conviction", "trap_formation_long"):
            self.assertNotIn(forbidden, card)

    def test_single_leg_thesis_uses_top_level_fields(self):
        th = {"direction": "SHORT", "entry_zone": [0.5, 0.55], "stop": 0.6, "tp": [0.4]}
        card = TG.format_setup_card("VELVET", th)
        self.assertIsNotNone(card)
        self.assertIn("NEW SETUP · VELVET · SHORT", card)
        self.assertIn("Leg 1", card)
        self.assertIn("0.5", card)

    def test_paramless_thesis_returns_none(self):
        th = {"direction": "WATCH", "entry_zone": None, "stop": None, "tp": []}
        self.assertIsNone(TG.format_setup_card("LAB", th))

    def test_missing_ticker_returns_none(self):
        self.assertIsNone(TG.format_setup_card(None, {"stop": 1.0}))

    def test_no_time_stop_omits_valid_until_line(self):
        th = {"direction": "LONG", "stop": 1.0}
        card = TG.format_setup_card("X", th)
        self.assertNotIn("Valid until", card)


class TestFormatCancelCard(unittest.TestCase):
    def test_cancel_card_shape(self):
        card = TG.format_cancel_card("ACE")
        self.assertIn("CANCELLED", card)
        self.assertIn("ACE", card)
        self.assertIn("setup withdrawn", card)

    def test_missing_ticker_returns_none(self):
        self.assertIsNone(TG.format_cancel_card(None))

    def test_no_reason_text(self):
        card = TG.format_cancel_card("LAB")
        # only the fixed phrase + ticker + timestamp — nothing else
        self.assertEqual(len(card.splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
