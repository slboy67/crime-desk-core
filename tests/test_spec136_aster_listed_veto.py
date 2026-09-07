#!/usr/bin/env python3
"""SPEC-136 — execution-venue veto: tag every board name `aster_listed`, suppress
un-fillable pages.

COTI printed a clean cross-venue regime-flip signal and got committed with a real
trigger — Aster carries no COTI market, so the trade never existed for this user.
Covers: capabilities/aster_listing.py (fetch+cache+lookup), classify.py wiring
(annotate_aster_listed: SIGNAL-ONLY tag / byte-identical regression / watchlist
override / single-fetch-per-run), ops/board_tick.py paging suppression (false →
notify.sh NOT invoked but inbox event tagged [SIGNAL-ONLY]; true → unchanged;
null → paged WITH a venue-unverified tag, never suppressed).

Offline-deterministic throughout — every network call is monkeypatched.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import aster_listing as AL
import classify as CL


def _load(name, sub="capabilities"):
    spec = importlib.util.spec_from_file_location(name, ROOT / sub / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── capabilities/aster_listing.py ────────────────────────────────────────────

class TestAsterListedLookup(unittest.TestCase):
    def test_listed_symbol_true(self):
        self.assertTrue(AL.aster_listed("BTC", {"BTCUSDT", "ETHUSDT"}))

    def test_unlisted_symbol_false(self):
        self.assertFalse(AL.aster_listed("COTI", {"BTCUSDT", "ETHUSDT"}))

    def test_unknown_symbols_returns_none(self):
        self.assertIsNone(AL.aster_listed("COTI", None))

    def test_no_ticker_returns_none(self):
        self.assertIsNone(AL.aster_listed(None, {"BTCUSDT"}))

    def test_lowercase_ticker_normalized(self):
        self.assertTrue(AL.aster_listed("btc", {"BTCUSDT"}))


class _TmpCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = AL.CACHE_PATH
        AL.CACHE_PATH = Path(self._tmp.name) / "aster_symbols_cache.json"

    def tearDown(self):
        AL.CACHE_PATH = self._orig
        self._tmp.cleanup()


class TestFetchAsterSymbols(_TmpCache):
    def test_live_fetch_used_and_cached(self):
        calls = []
        def fetch_fn():
            calls.append(1)
            return {"BTCUSDT", "ETHUSDT"}
        out = AL.fetch_aster_symbols(fetch_fn=fetch_fn)
        self.assertEqual(out, {"BTCUSDT", "ETHUSDT"})
        self.assertEqual(len(calls), 1)

    def test_fresh_cache_skips_live_fetch(self):
        calls = []
        def fetch_fn():
            calls.append(1)
            return {"BTCUSDT"}
        AL.fetch_aster_symbols(fetch_fn=fetch_fn, now=1_000_000.0)
        self.assertEqual(len(calls), 1)
        out = AL.fetch_aster_symbols(fetch_fn=fetch_fn, now=1_000_000.0 + 10)
        self.assertEqual(len(calls), 1)   # cache hit, no second live call
        self.assertEqual(out, {"BTCUSDT"})

    def test_cache_expires_after_ttl(self):
        calls = []
        def fetch_fn():
            calls.append(1)
            return {"BTCUSDT"}
        AL.fetch_aster_symbols(fetch_fn=fetch_fn, now=1_000_000.0)
        AL.fetch_aster_symbols(fetch_fn=fetch_fn, now=1_000_000.0 + AL.CACHE_TTL_S + 1)
        self.assertEqual(len(calls), 2)

    def test_failed_live_fetch_falls_back_to_stale_cache(self):
        good = lambda: {"BTCUSDT"}
        AL.fetch_aster_symbols(fetch_fn=good, now=1_000_000.0)
        bad = lambda: None
        out = AL.fetch_aster_symbols(fetch_fn=bad, now=1_000_000.0 + AL.CACHE_TTL_S + 1)
        self.assertEqual(out, {"BTCUSDT"})   # stale cache beats nothing

    def test_failed_live_fetch_no_cache_returns_none(self):
        out = AL.fetch_aster_symbols(fetch_fn=lambda: None, now=1_000_000.0)
        self.assertIsNone(out)   # §3: unknown, never an empty listing


# ── classify.py — annotate_aster_listed ──────────────────────────────────────

class TestAnnotateAsterListed(unittest.TestCase):
    def test_false_tags_reason_signal_only(self):
        rows = [{"ticker": "COTI", "reason": "no signals", "verdict": "CONFIRMS"}]
        CL.annotate_aster_listed(rows, tokens=[{"ticker": "COTI"}],
                                 symbols_fn=lambda: {"BTCUSDT"})
        self.assertIs(rows[0]["aster_listed"], False)
        self.assertIn("SIGNAL-ONLY (no Aster market)", rows[0]["reason"])

    def test_true_leaves_reason_byte_identical(self):
        rows = [{"ticker": "BTC", "reason": "no signals", "verdict": "CONFIRMS"}]
        CL.annotate_aster_listed(rows, tokens=[{"ticker": "BTC"}],
                                 symbols_fn=lambda: {"BTCUSDT"})
        self.assertIs(rows[0]["aster_listed"], True)
        self.assertEqual(rows[0]["reason"], "no signals")

    def test_fetch_failure_sets_null_no_reason_tag(self):
        rows = [{"ticker": "BTC", "reason": "no signals", "verdict": "CONFIRMS"}]
        CL.annotate_aster_listed(rows, tokens=[{"ticker": "BTC"}],
                                 symbols_fn=lambda: None)
        self.assertIsNone(rows[0]["aster_listed"])
        self.assertEqual(rows[0]["reason"], "no signals")

    def test_watchlist_manual_override_wins_over_live_probe(self):
        # live probe says listed (BTCUSDT present) but watchlist explicitly pins false
        rows = [{"ticker": "BTC", "reason": "no signals", "verdict": "CONFIRMS"}]
        CL.annotate_aster_listed(rows, tokens=[{"ticker": "BTC", "aster_listed": False}],
                                 symbols_fn=lambda: {"BTCUSDT"})
        self.assertIs(rows[0]["aster_listed"], False)
        self.assertIn("SIGNAL-ONLY", rows[0]["reason"])

    def test_symbols_fetched_once_per_run_not_per_ticker(self):
        calls = []
        def symbols_fn():
            calls.append(1)
            return {"BTCUSDT", "ETHUSDT"}
        rows = [{"ticker": "BTC", "reason": "r1", "verdict": "CONFIRMS"},
                {"ticker": "ETH", "reason": "r2", "verdict": "CONFIRMS"},
                {"ticker": "COTI", "reason": "r3", "verdict": "CONFIRMS"}]
        CL.annotate_aster_listed(rows, tokens=[{"ticker": "BTC"}, {"ticker": "ETH"},
                                               {"ticker": "COTI"}],
                                 symbols_fn=symbols_fn)
        self.assertEqual(len(calls), 1)


# ── ops/board_tick.py — paging suppression ────────────────────────────────────

BT = _load("board_tick", sub="ops")
IB = BT.inbox


def row(ticker, verdict, stop_breached=False, tps=(), watch_leg=None, thesis_drift=None,
        aster_listed="__unset__"):
    r = {"ticker": ticker, "verdict": verdict,
         "price_leg": {"stop_breached": stop_breached, "tps_printed": list(tps)},
         "watch_leg": watch_leg, "thesis_drift": thesis_drift}
    if aster_listed != "__unset__":
        r["aster_listed"] = aster_listed
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
        import os
        if self._orig_env is not None:
            os.environ["CRIMEDESK_NOTIFY"] = self._orig_env
        else:
            os.environ.pop("CRIMEDESK_NOTIFY", None)
        self.dir.cleanup()

    def seed(self, *rows):
        BT.BASELINE_PATH.write_text(BT.json.dumps(BT.snapshot(list(rows))))


class TestAsterVenueSuppression(_Tmp):
    def test_false_suppresses_notify_but_inbox_tagged(self):
        self.seed(row("COTI", "CONFIRMS", aster_listed=False))
        BT.tick(classify_fn=lambda: [row("COTI", "BREAKS", stop_breached=True, aster_listed=False)],
               notify_fn=self.notify)
        self.assertEqual(self.notes, [])   # not fillable — no phone page
        events = IB.unconsumed()
        self.assertTrue(any("[SIGNAL-ONLY]" in e.get("msg", "") for e in events))

    def test_true_pages_as_today(self):
        self.seed(row("LAB", "CONFIRMS", aster_listed=True))
        BT.tick(classify_fn=lambda: [row("LAB", "BREAKS", stop_breached=True, aster_listed=True)],
               notify_fn=self.notify)
        self.assertGreaterEqual(len(self.notes), 1)

    def test_null_pages_with_venue_unverified_tag_not_suppressed(self):
        self.seed(row("XYZ", "CONFIRMS", aster_listed=None))
        BT.tick(classify_fn=lambda: [row("XYZ", "BREAKS", stop_breached=True, aster_listed=None)],
               notify_fn=self.notify)
        self.assertGreaterEqual(len(self.notes), 1)
        self.assertTrue(any("venue-unverified" in msg for _title, msg in self.notes))

    def test_field_absent_treated_as_null_not_suppressed(self):
        # regression: existing rows/tests never set aster_listed at all
        self.seed(row("VELVET", "CONFIRMS"))
        BT.tick(classify_fn=lambda: [row("VELVET", "BREAKS", stop_breached=True)],
               notify_fn=self.notify)
        self.assertGreaterEqual(len(self.notes), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
