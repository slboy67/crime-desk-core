#!/usr/bin/env python3
"""SPEC 46 — board tick: standing classify loop with verdict-delta alerts.

Run:  python3 tests/test_board_tick.py

VELVET's stop printed 2026-06-09; the board flagged it when the Designer opened a
session a day later. The tick runs classify on a launchd cadence and pushes deltas
(verdict change / new stop-breach / new TP print) into the SPEC-45 inbox + a macOS
notification. Contract under test:
  - simulated verdict flip vs a fixture baseline → inbox event + notification invoked;
  - no-delta tick leaves the inbox untouched (§0.5 silence);
  - classify failure → tick skipped, baseline NEVER overwritten with a bogus one;
  - lockfile prevents overlapping runs;
  - first run seeds the baseline quietly.
classify + notifier injected; all paths in a tmpdir — offline, deterministic.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, sub="capabilities"):
    spec = importlib.util.spec_from_file_location(name, ROOT / sub / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BT = _load("board_tick", sub="ops")
IB = BT.inbox   # the tick writes through the inbox module it imported


def row(ticker, verdict, stop_breached=False, tps=(), entered_zone=True):
    # entered_zone defaults True: a bare TRIGGERS fixture represents a genuine entry
    # crossing unless a test explicitly says otherwise (SPEC-189 §B gate).
    return {"ticker": ticker, "verdict": verdict,
            "price_leg": {"stop_breached": stop_breached, "tps_printed": list(tps),
                          "entered_zone": entered_zone}}


def wl_row(ticker, price=1.0, d=">", legacy=False, verdict="CONFIRMS"):
    r = row(ticker, verdict)
    r["watch_leg"] = {"legacy": legacy, "breached": [{"price": price, "dir": d, "note": "x"}]}
    return r


def drift_row(ticker, verdict="CONFIRMS"):
    r = row(ticker, verdict)
    r["thesis_drift"] = {"stale": True, "live_price": 1.0, "nearest_anchor": 0.9,
                          "nearest_dist_pct": 11.0, "committed_ts": "2026-01-01T00:00:00Z"}
    return r


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig_bt = (BT.BASELINE_PATH, BT.LOCK_PATH, BT.ERR_PATH, BT.NOTIFY_STATE_PATH,
                         BT.THESIS_SEEN_PATH, BT.POSITIONS_PATH)
        BT.BASELINE_PATH = d / "board_last.json"
        BT.LOCK_PATH = d / "board_tick.lock"
        BT.ERR_PATH = d / "board_tick.err"
        BT.NOTIFY_STATE_PATH = d / "notify_sent.json"   # SPEC-111: delivery dedup, isolate too
        BT.THESIS_SEEN_PATH = d / "thesis_seen.json"    # SPEC-189: commit/retire snapshot
        # Isolate from the REAL config/positions.json — unmocked, this pulled in whatever
        # live positions the desk happens to hold and fed their tickers to _oic_watch_sweep,
        # which makes real network calls (a multi-minute test hang, not a fast offline run).
        BT.POSITIONS_PATH = d / "positions.json"
        self._orig_ib = (IB.LOG_PATH, IB.EVENTS_PATH, IB.CURSOR_PATH)
        IB.LOG_PATH = d / "nonce_alerts.log"
        IB.EVENTS_PATH = d / "inbox_events.jsonl"
        IB.CURSOR_PATH = d / "inbox_cursor.json"
        # SPEC 51: ticks sweep unmapped names — pin the shared onboard module at an
        # empty tmp watchlist so these tests stay offline (sweep short-circuits at 0)
        self._orig_ob = (BT.onboard.WALLETS, BT.onboard.WL_PATH)
        BT.onboard.WALLETS = d / "tracked_wallets.json"
        BT.onboard.WL_PATH = d / "watchlist.json"
        BT.onboard.WALLETS.write_text('{"tokens": {}}')
        BT.onboard.WL_PATH.write_text('{"tokens": []}')
        self.notes = []
        self.notify = lambda title, msg: self.notes.append((title, msg))

    def tearDown(self):
        (BT.BASELINE_PATH, BT.LOCK_PATH, BT.ERR_PATH, BT.NOTIFY_STATE_PATH,
         BT.THESIS_SEEN_PATH, BT.POSITIONS_PATH) = self._orig_bt
        IB.LOG_PATH, IB.EVENTS_PATH, IB.CURSOR_PATH = self._orig_ib
        BT.onboard.WALLETS, BT.onboard.WL_PATH = self._orig_ob
        self.dir.cleanup()

    def seed(self, *rows):
        BT.BASELINE_PATH.write_text(json.dumps(BT.snapshot(list(rows))))


class TestDeltas(_Tmp):
    def test_verdict_flip_fires_high_event_and_notification(self):
        self.seed(row("VELVET", "CONFIRMS"))
        out = BT.tick(classify_fn=lambda: [row("VELVET", "BREAKS", stop_breached=True)],
                      notify_fn=self.notify)
        self.assertEqual(out["events"], 2)            # verdict flip + new stop-breach
        ev = IB.unconsumed()
        self.assertTrue(all(e["source"] == "board_tick" for e in ev))
        self.assertTrue(all(e["severity"] == "HIGH" for e in ev))
        self.assertTrue(any("BREAKS" in e["msg"] for e in ev))
        self.assertEqual(len(self.notes), 2)
        # baseline advanced to the new state
        base = json.loads(BT.BASELINE_PATH.read_text())
        self.assertEqual(base["VELVET"]["verdict"], "BREAKS")

    def test_triggers_flip_is_med(self):
        self.seed(row("LAB", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify)
        ev = IB.unconsumed()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["severity"], "MED")

    def test_new_tp_print_is_med_event(self):
        self.seed(row("SIREN", "TRIGGERS", tps=[0.5]))
        BT.tick(classify_fn=lambda: [row("SIREN", "TRIGGERS", tps=[0.5, 0.45])],
                notify_fn=self.notify)
        ev = IB.unconsumed()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["severity"], "MED")
        self.assertIn("0.45", ev[0]["msg"])

    def test_no_delta_leaves_inbox_untouched(self):
        self.seed(row("LAB", "CONFIRMS"), row("SIREN", "TRIGGERS", tps=[0.5]))
        out = BT.tick(classify_fn=lambda: [row("LAB", "CONFIRMS"),
                                           row("SIREN", "TRIGGERS", tps=[0.5])],
                      notify_fn=self.notify)
        self.assertEqual(out["events"], 0)
        self.assertEqual(IB.unconsumed(), [])
        self.assertEqual(self.notes, [])

    def test_first_run_seeds_baseline_quietly(self):
        out = BT.tick(classify_fn=lambda: [row("LAB", "BREAKS")], notify_fn=self.notify)
        self.assertTrue(out.get("seeded"))
        self.assertEqual(IB.unconsumed(), [])
        self.assertEqual(self.notes, [])
        self.assertTrue(BT.BASELINE_PATH.exists())

    def test_new_ticker_on_board_no_event(self):
        # a token added to the watchlist is not a delta, just baseline growth
        self.seed(row("LAB", "CONFIRMS"))
        out = BT.tick(classify_fn=lambda: [row("LAB", "CONFIRMS"), row("NEW", "CONFIRMS")],
                      notify_fn=self.notify)
        self.assertEqual(out["events"], 0)
        self.assertIn("NEW", json.loads(BT.BASELINE_PATH.read_text()))


class TestSafety(_Tmp):
    def test_classify_failure_skips_and_preserves_baseline(self):
        self.seed(row("LAB", "CONFIRMS"))
        before = BT.BASELINE_PATH.read_text()
        def boom():
            raise RuntimeError("venue 502")
        out = BT.tick(classify_fn=boom, notify_fn=self.notify)
        self.assertTrue(out.get("skipped"))
        self.assertEqual(BT.BASELINE_PATH.read_text(), before)   # never a bogus baseline
        self.assertEqual(IB.unconsumed(), [])
        self.assertIn("venue 502", BT.ERR_PATH.read_text())

    def test_lockfile_prevents_overlap(self):
        self.seed(row("LAB", "CONFIRMS"))
        BT.LOCK_PATH.write_text("12345")
        out = BT.tick(classify_fn=lambda: [row("LAB", "BREAKS")], notify_fn=self.notify)
        self.assertTrue(out.get("skipped"))
        self.assertEqual(IB.unconsumed(), [])

    def test_lock_released_after_tick(self):
        self.seed(row("LAB", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("LAB", "CONFIRMS")], notify_fn=self.notify)
        self.assertFalse(BT.LOCK_PATH.exists())

    def test_lock_released_even_on_failure(self):
        def boom():
            raise RuntimeError("x")
        BT.tick(classify_fn=boom, notify_fn=self.notify)
        self.assertFalse(BT.LOCK_PATH.exists())


class TestOneShotPages(_Tmp):
    """SPEC-154: a page fires once per armed window — re-delivery requires the condition
    to CLEAR and re-fire, never a 6h TTL re-ping."""

    def test_armed_watch_level_delivers_once_not_repaged_7h_later(self):
        rows_fn = lambda: [wl_row("X")]
        BT.tick(classify_fn=rows_fn, notify_fn=self.notify, now_fn=lambda: 1_000_000)
        BT.tick(classify_fn=rows_fn, notify_fn=self.notify, now_fn=lambda: 1_000_000 + 7 * 3600)
        self.assertEqual(len(self.notes), 1)

    def test_condition_clears_then_recross_delivers_immediately(self):
        breached = lambda: [wl_row("X")]
        cleared = lambda: [row("X", "CONFIRMS")]
        BT.tick(classify_fn=breached, notify_fn=self.notify, now_fn=lambda: 1000)
        BT.tick(classify_fn=cleared, notify_fn=self.notify, now_fn=lambda: 1010)   # disarms
        BT.tick(classify_fn=breached, notify_fn=self.notify, now_fn=lambda: 1020)  # re-crosses
        self.assertEqual(len(self.notes), 2)

    def test_tape_watch_high_pages_once_then_reclears_repages(self):
        orig = BT.tape_watch.run_tick
        self.addCleanup(lambda: setattr(BT.tape_watch, "run_tick", orig))
        results = iter([
            {"detail": [{"ticker": "X", "severity": "HIGH", "msg": "m1", "paged": True}]},
            {"detail": [{"ticker": "X", "severity": "HIGH", "msg": "m1", "paged": True}]},
            {"detail": [{"ticker": "X", "severity": "HIGH", "msg": "m1", "paged": True}]},
            {"detail": []},   # HIGH clears
            {"detail": [{"ticker": "X", "severity": "HIGH", "msg": "m2", "paged": True}]},
        ])
        BT.tape_watch.run_tick = lambda **kw: next(results)
        rows_fn = lambda: [row("X", "CONFIRMS")]
        for _ in range(3):   # 3 consecutive HIGH ticks
            BT.tick(classify_fn=rows_fn, notify_fn=self.notify)
        self.assertEqual(len(self.notes), 1)
        BT.tick(classify_fn=rows_fn, notify_fn=self.notify)   # clears -> disarm
        BT.tick(classify_fn=rows_fn, notify_fn=self.notify)   # HIGH returns -> re-pages
        self.assertEqual(len(self.notes), 2)

    def test_thesis_drift_is_inbox_only_never_pushed(self):
        out = BT.tick(classify_fn=lambda: [drift_row("X")], notify_fn=self.notify)
        self.assertEqual(self.notes, [])
        self.assertNotIn("skipped", out)

    def test_oic_watch_sweep_result_rides_along_in_tick_output(self):
        # SPEC-180 req 6: the sweep's result surfaces in tick()'s return dict —
        # oic_watch.run_tick itself is mocked so this stays fully offline.
        orig = BT.oic_watch.run_tick
        self.addCleanup(lambda: setattr(BT.oic_watch, "run_tick", orig))
        BT.oic_watch.run_tick = lambda *a, **kw: {"checked": 1, "events": 1, "paged": 1, "detail": []}
        orig_load = BT.load_thesis_watchlist
        self.addCleanup(lambda: setattr(BT, "load_thesis_watchlist", orig_load))
        BT.load_thesis_watchlist = lambda: (
            [{"ticker": "X", "thesis": {"status": "ARMED"}}], None)
        out = BT.tick(classify_fn=lambda: [row("X", "CONFIRMS")], notify_fn=self.notify)
        self.assertEqual(out["oic_watch"], {"checked": 1, "events": 1, "paged": 1, "detail": []})

    def test_oic_watch_sweep_failure_never_blocks_the_tick(self):
        orig = BT.oic_watch.run_tick
        self.addCleanup(lambda: setattr(BT.oic_watch, "run_tick", orig))

        def boom(*a, **kw):
            raise RuntimeError("boom")
        BT.oic_watch.run_tick = boom
        orig_load = BT.load_thesis_watchlist
        self.addCleanup(lambda: setattr(BT, "load_thesis_watchlist", orig_load))
        BT.load_thesis_watchlist = lambda: (
            [{"ticker": "X", "thesis": {"status": "ARMED"}}], None)
        out = BT.tick(classify_fn=lambda: [row("X", "CONFIRMS")], notify_fn=self.notify)
        self.assertIn("error", out["oic_watch"])
        self.assertNotIn("skipped", out)


class _TelegramTestBase(_Tmp):
    """Shared credential/kill-switch isolation for every telegram-leg test class
    (SPEC-181/189) — NOT itself a test case (no test_ methods), so subclassing it
    (rather than a concrete test class) doesn't re-run another class's tests."""

    def setUp(self):
        super().setUp()
        self._orig_load = BT.load_thesis_watchlist
        self.tg_sent = []

        def _tg_post(text):
            self.tg_sent.append(text)
            return True   # SPEC-189: the key is only consumed when post() returns True
        self.tg_post = _tg_post
        # Isolate the real telegram_post credential resolution so a test that exercises
        # BT.tick()'s DEFAULT (uninjected) tg_post_fn path can never read a real
        # credential file or hit the network — offline-deterministic per GOAL-coder.md.
        d = Path(self.dir.name)
        self._orig_tg_paths = (BT.telegram_post.TOKEN_FILE, BT.telegram_post.CHAT_ID_FILE)
        BT.telegram_post.TOKEN_FILE = d / "telegram_bot_token"
        BT.telegram_post.CHAT_ID_FILE = d / "telegram_chat_id"
        import os
        self._orig_tg_env = {k: os.environ.get(k) for k in
                             ("CRIMEDESK_TG_BOT_TOKEN", "CRIMEDESK_TG_CHAT_ID")}
        os.environ.pop("CRIMEDESK_TG_BOT_TOKEN", None)
        os.environ.pop("CRIMEDESK_TG_CHAT_ID", None)

    def tearDown(self):
        BT.load_thesis_watchlist = self._orig_load
        BT.telegram_post.TOKEN_FILE, BT.telegram_post.CHAT_ID_FILE = self._orig_tg_paths
        import os
        for k, v in self._orig_tg_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def _with_thesis(self, ticker, **thesis_fields):
        BT.load_thesis_watchlist = lambda: (
            [{"ticker": ticker, "thesis": {"status": "ARMED", **thesis_fields}}], None)

    def _with_theses(self, tokens):
        """Multi-token / explicit-status control for the SPEC-189 setup/cancel sweep
        tests — `_with_thesis` above always forces status=ARMED and a single ticker."""
        BT.load_thesis_watchlist = lambda: (tokens, None)


class TestTelegramSignalChannel(_TelegramTestBase):
    """SPEC-181: public Telegram broadcast leg — TRIGGERS/BREAKS/STOP-BREACHED only,
    one-shot under the `tg:` key namespace, kept fully separate from the ntfy leg."""

    def test_triggers_transition_posts_once_public_card(self):
        self._with_thesis("LAB", direction="LONG", entry_zone=[0.5, 0.55], stop=0.45,
                           tp=[0.6, 0.7], signature="trap_long",
                           triggers=[{"note": "seller 0xdeadbeef"}])
        self.seed(row("LAB", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        self.assertEqual(len(self.tg_sent), 1)
        card = self.tg_sent[0]
        self.assertIn("LAB", card)
        self.assertIn("ENTRY TRIGGERED", card)
        self.assertNotIn("0x", card)
        self.assertNotIn("trap_long", card)

    def test_breaks_transition_posts_the_stop_level_only(self):
        self._with_thesis("VELVET", direction="SHORT", stop=0.30,
                           invalidation={"note": "funding flips positive, farm unwind"})
        self.seed(row("VELVET", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("VELVET", "BREAKS", stop_breached=True)],
                notify_fn=self.notify, tg_post_fn=self.tg_post)
        cards = "\n".join(self.tg_sent)
        self.assertIn("THESIS INVALIDATED", cards)
        self.assertIn("STOPPED OUT", cards)   # the same tick also fires stop_breach
        self.assertNotIn("farm unwind", cards)

    def test_stays_silent_while_continuously_armed(self):
        self._with_thesis("LAB", direction="LONG", stop=0.45, tp=[0.6])
        self.seed(row("LAB", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)   # no verdict delta -> diff() emits nothing
        self.assertEqual(len(self.tg_sent), 1)

    def test_reposts_after_key_disarmed(self):
        # SPEC-154 semantics (re-used verbatim for the `tg:` namespace, per the ticket):
        # a key that stays armed never re-delivers; re-delivery requires the condition
        # to clear (`_disarm`) or a human to re-arm the row. This drives the SAME
        # _arm_check/_disarm machinery the ntfy leg uses, just under the `tg:` prefix.
        self._with_thesis("LAB", direction="LONG", stop=0.45, tp=[0.6])
        self.seed(row("LAB", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        self.assertEqual(len(self.tg_sent), 1)
        state = json.loads(BT.NOTIFY_STATE_PATH.read_text())
        for k in list(state):
            if k.startswith("tg:LAB|verdict|"):
                state[k]["armed"] = False
        BT.NOTIFY_STATE_PATH.write_text(json.dumps(state))
        self.seed(row("LAB", "CONFIRMS"))   # fresh baseline so the next tick diffs again
        BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        self.assertEqual(len(self.tg_sent), 2)

    def test_watch_level_crossing_posts_nothing_public(self):
        self._with_thesis("X", direction="LONG", stop=0.9)
        BT.tick(classify_fn=lambda: [wl_row("X")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        self.assertEqual(self.tg_sent, [])

    def test_tp_print_posts_nothing_public(self):
        self._with_thesis("SIREN", direction="LONG", stop=0.4, tp=[0.5])
        self.seed(row("SIREN", "TRIGGERS", tps=[0.5]))
        BT.tick(classify_fn=lambda: [row("SIREN", "TRIGGERS", tps=[0.5, 0.45])],
                notify_fn=self.notify, tg_post_fn=self.tg_post)
        self.assertEqual(self.tg_sent, [])

    def test_paramless_thesis_posts_nothing_public(self):
        self._with_thesis("LAB", direction="LONG")   # no entry_zone/stop/tp at all
        self.seed(row("LAB", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        self.assertEqual(self.tg_sent, [])

    def test_missing_credentials_default_post_is_a_noop_not_a_crash(self):
        # no tg_post_fn injected -> falls through to the real telegram_post.post(),
        # which no-ops without credentials (no env vars set in this test process).
        self._with_thesis("LAB", direction="LONG", stop=0.45, tp=[0.6])
        self.seed(row("LAB", "CONFIRMS"))
        out = BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify)
        self.assertNotIn("skipped", out)

    def test_notify_off_kill_switch_stops_public_posts_too(self):
        import os
        self._with_thesis("LAB", direction="LONG", stop=0.45, tp=[0.6])
        self.seed(row("LAB", "CONFIRMS"))
        os.environ["CRIMEDESK_NOTIFY"] = "off"
        try:
            BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify,
                    tg_post_fn=self.tg_post)
        finally:
            os.environ.pop("CRIMEDESK_NOTIFY", None)
        self.assertEqual(self.tg_sent, [])

    def test_ntfy_leg_unaffected_by_telegram_leg(self):
        self._with_thesis("LAB", direction="LONG", stop=0.45, tp=[0.6])
        self.seed(row("LAB", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(len(self.tg_sent), 1)


class TestTelegramLegAwareCards(_TelegramTestBase):
    """SPEC-189 §A — a two-leg thesis (top-level entry_zone/stop/tp all null, params
    in legs[]) must produce a TRIGGERS card built from the FIRING leg, not the empty
    top-level fields (the ACE bug: `tg:ACE|verdict|TRIGGERS` armed 2026-09-02 with no
    card ever produced)."""

    def _ace_row(self, breach_price, breach_dir="below", entered_zone=False):
        r = row("ACE", "TRIGGERS", entered_zone=entered_zone)
        r["watch_leg"] = {"legacy": False,
                          "breached": [{"price": breach_price, "dir": breach_dir, "note": "x"}]}
        return r

    def test_two_leg_thesis_card_built_from_firing_leg(self):
        self._with_thesis("ACE", direction="LONG", entry_zone=None, stop=None, tp=[],
                           legs=[
                               {"name": "1-retest", "entry_zone": [0.181, 0.187],
                                "stop": 0.155, "tp": [0.2205, 0.245]},
                               {"name": "2-momentum", "entry_zone": [0.221, 0.226],
                                "stop": 0.196, "tp": [0.245, 0.256]},
                           ])
        self.seed(row("ACE", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [self._ace_row(0.187)], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        self.assertEqual(len(self.tg_sent), 1)
        card = self.tg_sent[0]
        self.assertIn("ACE", card)
        self.assertIn("Leg 1", card)
        self.assertIn("0.181", card)
        self.assertIn("0.155", card)
        self.assertNotIn("0.221", card)   # leg 2's numbers must not leak into leg 1's card
        self.assertNotIn("0.196", card)

    def test_leg_2_fires_when_its_own_level_crosses(self):
        self._with_thesis("ACE", direction="LONG", entry_zone=None, stop=None, tp=[],
                           legs=[
                               {"name": "1-retest", "entry_zone": [0.181, 0.187],
                                "stop": 0.155, "tp": [0.2205, 0.245]},
                               {"name": "2-momentum", "entry_zone": [0.221, 0.226],
                                "stop": 0.196, "tp": [0.245, 0.256]},
                           ])
        self.seed(row("ACE", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [self._ace_row(0.221, breach_dir="above")],
                notify_fn=self.notify, tg_post_fn=self.tg_post)
        self.assertEqual(len(self.tg_sent), 1)
        card = self.tg_sent[0]
        self.assertIn("Leg 2", card)
        self.assertIn("0.221", card)
        self.assertNotIn("0.181", card)

    def test_failed_post_leaves_key_armed_for_retry(self):
        self._with_thesis("LAB", direction="LONG", entry_zone=[0.5, 0.55], stop=0.45, tp=[0.6])
        self.seed(row("LAB", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify,
                tg_post_fn=lambda text: False)
        self.assertEqual(self.tg_sent, [])
        state = json.loads(BT.NOTIFY_STATE_PATH.read_text())
        key = next(k for k in state if k.startswith("tg:LAB|verdict|TRIGGERS"))
        self.assertFalse(state[key]["armed"])
        # a fresh transition next tick (armed key cleared) must retry, not stay silent
        self.seed(row("LAB", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        self.assertEqual(len(self.tg_sent), 1)

    def test_none_card_leaves_key_armed_for_retry(self):
        # firing leg matches by entry_ref but carries no entry_zone/stop/tp of its own
        # -> format_card has nothing numeric to render -> None
        self._with_thesis("ACE", direction="LONG", entry_zone=None, stop=None, tp=[],
                           legs=[{"name": "x", "entry_ref": 0.5}])
        self.seed(row("ACE", "CONFIRMS"))
        r = row("ACE", "TRIGGERS")
        r["watch_leg"] = {"legacy": False, "breached": [{"price": 0.5, "dir": "below", "note": "x"}]}
        BT.tick(classify_fn=lambda: [r], notify_fn=self.notify, tg_post_fn=self.tg_post)
        self.assertEqual(self.tg_sent, [])
        state = json.loads(BT.NOTIFY_STATE_PATH.read_text())
        key = next(k for k in state if k.startswith("tg:ACE|verdict|TRIGGERS"))
        self.assertFalse(state[key]["armed"])

    def test_single_leg_thesis_unchanged_regression(self):
        self._with_thesis("LAB", direction="LONG", entry_zone=[0.5, 0.55], stop=0.45, tp=[0.6])
        self.seed(row("LAB", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("LAB", "TRIGGERS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        card = self.tg_sent[0]
        self.assertIn("0.5", card)
        self.assertNotIn("Leg", card)   # no legs[] -> no leg label, unchanged shape


class TestTelegramEntryOnlyGate(_TelegramTestBase):
    """SPEC-189 §B — a TRIGGERS card requires a genuine entry crossing; a TP print /
    zone-blown overrun / stale drift must never read as "ENTRY TRIGGERED" in public
    (the FET bug: verdict flipped TRIGGERS off a TP print 9% below the entry zone)."""

    def test_tp_print_only_transition_posts_no_public_card(self):
        self._with_thesis("FET", direction="SHORT", entry_zone=[0.168, 0.170],
                           stop=0.1745, tp=[0.158, 0.148])
        self.seed(row("FET", "CONFIRMS"))
        fet_row = row("FET", "TRIGGERS", tps=[0.153], entered_zone=False)
        BT.tick(classify_fn=lambda: [fet_row], notify_fn=self.notify, tg_post_fn=self.tg_post)
        self.assertEqual(self.tg_sent, [])
        self.assertEqual(len(self.notes), 1)   # private ntfy leg still fires

    def test_sanity_gate_blocks_price_far_outside_zone(self):
        self._with_thesis("FET", direction="SHORT", entry_zone=[0.168, 0.170],
                           stop=0.1745, tp=[0.158, 0.148])
        self.seed(row("FET", "CONFIRMS"))
        fet_row = row("FET", "TRIGGERS", entered_zone=True)
        fet_row["live_price"] = 0.1545   # ~8% below the zone low
        BT.tick(classify_fn=lambda: [fet_row], notify_fn=self.notify, tg_post_fn=self.tg_post)
        self.assertEqual(self.tg_sent, [])
        self.assertIn("FET", BT.ERR_PATH.read_text())

    def test_sanity_gate_allows_price_inside_zone(self):
        self._with_thesis("LAB", direction="LONG", entry_zone=[0.5, 0.55], stop=0.45, tp=[0.6])
        self.seed(row("LAB", "CONFIRMS"))
        lab_row = row("LAB", "TRIGGERS")
        lab_row["live_price"] = 0.52
        BT.tick(classify_fn=lambda: [lab_row], notify_fn=self.notify, tg_post_fn=self.tg_post)
        self.assertEqual(len(self.tg_sent), 1)

    def test_breaks_and_stop_breach_unaffected_by_entry_gate(self):
        self._with_thesis("VELVET", direction="SHORT", stop=0.30)
        self.seed(row("VELVET", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("VELVET", "BREAKS", stop_breached=True,
                                          entered_zone=False)],
                notify_fn=self.notify, tg_post_fn=self.tg_post)
        cards = "\n".join(self.tg_sent)
        self.assertIn("THESIS INVALIDATED", cards)
        self.assertIn("STOPPED OUT", cards)


class TestTelegramSetupCards(_TelegramTestBase):
    """SPEC-189 §C — thesis commits post NEW SETUP, retires post CANCELLED. Runs the
    same telegram credential/kill-switch isolation as TestTelegramSignalChannel."""

    def _ace_tokens(self, committed_ts="2026-09-01T21:45:00Z", status="WATCH"):
        return [{"ticker": "ACE", "thesis": {
            "status": status, "committed_ts": committed_ts,
            "legs": [{"entry_zone": [0.181, 0.187], "stop": 0.155, "tp": [0.2205, 0.245]}]}}]

    def test_first_tick_seeds_snapshot_quietly(self):
        self._with_theses(self._ace_tokens())
        BT.tick(classify_fn=lambda: [row("ACE", "CONFIRMS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        self.assertEqual(self.tg_sent, [])
        self.assertTrue(BT.THESIS_SEEN_PATH.exists())

    def test_new_commit_posts_new_setup_card(self):
        self._with_theses([])
        BT.tick(classify_fn=lambda: [], notify_fn=self.notify, tg_post_fn=self.tg_post)
        self._with_theses(self._ace_tokens())
        BT.tick(classify_fn=lambda: [row("ACE", "CONFIRMS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        self.assertEqual(len(self.tg_sent), 1)
        self.assertIn("NEW SETUP", self.tg_sent[0])
        self.assertIn("ACE", self.tg_sent[0])

    def test_retire_posts_cancelled_card(self):
        self._with_theses(self._ace_tokens())
        self.seed(row("ACE", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("ACE", "CONFIRMS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)   # first-ever thesis_seen run -> seeds quietly
        self.assertEqual(self.tg_sent, [])
        self._with_theses([])   # ACE retired
        BT.tick(classify_fn=lambda: [row("ACE", "CONFIRMS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        self.assertEqual(len(self.tg_sent), 1)
        self.assertIn("CANCELLED", self.tg_sent[0])
        self.assertIn("ACE", self.tg_sent[0])

    def test_retire_after_stop_breach_card_posts_no_cancelled(self):
        self._with_theses([{"ticker": "ACE", "thesis": {
            "status": "LIVE", "committed_ts": "ts1", "stop": 0.15,
            "entry_zone": [0.18, 0.19], "tp": [0.2]}}])
        self.seed(row("ACE", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("ACE", "CONFIRMS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)   # seeds thesis_seen quietly
        self.assertEqual(self.tg_sent, [])
        BT.tick(classify_fn=lambda: [row("ACE", "BREAKS", stop_breached=True)],
                notify_fn=self.notify, tg_post_fn=self.tg_post)
        self.assertTrue(any("STOPPED OUT" in c for c in self.tg_sent))
        sent_before = len(self.tg_sent)
        self._with_theses([])   # ACE retired after stopping out
        self.seed(row("ACE", "BREAKS", stop_breached=True))
        BT.tick(classify_fn=lambda: [row("ACE", "BREAKS", stop_breached=True)],
                notify_fn=self.notify, tg_post_fn=self.tg_post)
        self.assertEqual(len(self.tg_sent), sent_before)   # no CANCELLED on top

    def test_paramless_commit_posts_no_setup_card(self):
        self._with_theses([{"ticker": "LAB", "thesis": {"status": "WATCH", "committed_ts": "ts1"}}])
        BT.tick(classify_fn=lambda: [row("LAB", "CONFIRMS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)   # seeds quietly (also paramless -> never tracked)
        self._with_theses([{"ticker": "LAB", "thesis": {"status": "WATCH", "committed_ts": "ts2"}}])
        BT.tick(classify_fn=lambda: [row("LAB", "CONFIRMS")], notify_fn=self.notify,
                tg_post_fn=self.tg_post)
        self.assertEqual(self.tg_sent, [])

    def test_notify_off_blocks_new_setup_card(self):
        import os
        self._with_theses([])
        BT.tick(classify_fn=lambda: [], notify_fn=self.notify, tg_post_fn=self.tg_post)
        self._with_theses(self._ace_tokens())
        os.environ["CRIMEDESK_NOTIFY"] = "off"
        try:
            BT.tick(classify_fn=lambda: [], notify_fn=self.notify, tg_post_fn=self.tg_post)
        finally:
            os.environ.pop("CRIMEDESK_NOTIFY", None)
        self.assertEqual(self.tg_sent, [])


class TestOpenCommitSweep(_Tmp):
    """SPEC-162: every tick diffs the watchlist against open ledger rows and
    commit_open()s any (ticker, committed_ts) not yet recorded — best-effort, never
    blocks the tick even when it fails."""

    def setUp(self):
        super().setUp()
        self._orig_ledger_path = BT.ledger.LEDGER_PATH
        BT.ledger.LEDGER_PATH = Path(self.dir.name) / "ledger.jsonl"

    def tearDown(self):
        BT.ledger.LEDGER_PATH = self._orig_ledger_path
        super().tearDown()

    def test_committed_thesis_gets_an_open_ledger_row(self):
        BT.onboard.WL_PATH.write_text(json.dumps({"tokens": [
            {"ticker": "GALA", "thesis": {
                "direction": "SHORT", "entry_zone": [0.00195, 0.00199], "stop": 0.00211,
                "tp": [0.00165, 0.00142], "committed_ts": "2026-08-25T00:00:00Z"}}]}))
        out = BT.tick(classify_fn=lambda: [row("GALA", "CONFIRMS")], notify_fn=self.notify)
        self.assertEqual(out["open_commit_sweep"]["created"],
                         [{"ticker": "GALA", "commit_ts": "2026-08-25T00:00:00Z"}])
        rows = [json.loads(l) for l in BT.ledger.LEDGER_PATH.read_text().splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "GALA")
        self.assertIsNone(rows[0]["outcome"])

    def test_sweep_failure_never_blocks_the_tick(self):
        orig = BT.ledger.sweep_open_commits
        BT.ledger.sweep_open_commits = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        self.addCleanup(lambda: setattr(BT.ledger, "sweep_open_commits", orig))
        out = BT.tick(classify_fn=lambda: [row("X", "CONFIRMS")], notify_fn=self.notify)
        self.assertIn("error", out["open_commit_sweep"])
        self.assertNotIn("skipped", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
