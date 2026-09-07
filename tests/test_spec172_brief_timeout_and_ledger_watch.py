#!/usr/bin/env python3
"""SPEC-172 — two independent defects found live 2026-08-28:

Defect 1: `brief` blew the orchestrator's 180s subprocess budget on GALA/SKR. The root
cause (found while writing this test): every sub-layer LOOKED bounded (`.result(timeout=
...)`), but each `with ThreadPoolExecutor(...) as ex:` block's `__exit__` calls
`ex.shutdown(wait=True)` — which blocks until every submitted thread finishes, INCLUDING
ones already reported as timed-out inside the loop. A single stalled network call (a
live-keyed Aster signed request via `risk_card`'s `venue_account.equity()`, or
`maxsize`'s book walk) silently re-imposed its own wall-clock on the whole `brief`
regardless of any per-`.result()` timeout. Fixed in capabilities/brief.py's `_bounded()`
(no `with`, `shutdown(wait=False)`) + per-component budgets for every layer, main-loop
and post-loop alike. `timed_out`/`meta.layer_ms` make the fix auditable.

Defect 2: `ledger.py record --allow-new` still ran the free-text heuristic
(`_raw_canon_strict`) BEFORE honoring the flag, so "squeeze_exhaust_watch" matched
"squeeze" and silently landed under `trap_formation_long` — a geometry-less WATCH row
corrupting the desk's best LONG signature's base-rate. Fixed: `--allow-new` now means
VERBATIM (no heuristic pass at all), and any `_watch`-suffixed signature (or
`direction=="WATCH"`) is NEVER heuristically canonicalised into a trade signature,
stored verbatim, and excluded from every per-signature trade aggregate (bucketed under
`watch` in `stats` instead). A `migrate_watch_rows` dry-run/--apply scan re-tags
already-corrupted WATCH rows from the watchlist's `thesis.signature`.
"""
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


BR = _load("brief")

sys.path.insert(0, str(ROOT / "capabilities"))
import ledger as LG   # noqa: E402


# ── minimal offline fixtures (mirrors tests/test_brief.py's _patch_all) ────────────────
def _fake_watchlist_no_thesis():
    return ([{"ticker": "ZTEST"}], None)


def _fake_classify_token(tok, live=None):
    return {"ticker": tok["ticker"], "verdict": None, "reason": "not on watchlist",
            "direction": None, "thesis_present": False, "live": live}


def _fake_analyse(ticker, days=90):
    return {"ticker": ticker, "verdict": "WATCH", "direction": "NEUTRAL", "tier": "watch",
            "funding_4h": 0.01, "funding_venue": "binance", "funding_unavailable": False,
            "oi_chg_pct": 0, "near_ath": False, "cvd_verdict": None, "price": 1.0,
            "onchain": "OK", "nonce_signal": "QUIET", "notes": []}


def _fake_depth(ticker, venue=None):
    v = {"available": True, "mid": 1.0, "best_bid": 0.999, "best_ask": 1.001,
        "bid_shelf_below": None, "ask_wall_above": None, "truncated": False,
        "deepest_level_seen": {"bid": 0.9, "ask": 1.1}}
    venues = {"bitget": v, "binance": v}
    if venue:
        venues = {venue.lower(): venues[venue.lower()]}
    return {"ticker": ticker, "venues": venues, "note": "read-only"}


def _fake_onchain(ticker, depth="fast"):
    return {"ticker": ticker, "bias": None, "score": 0, "signal": None,
            "nonces": {"tracked": False, "signal": None, "score": 0, "baseline_seeded": False,
                      "newly_fired": [], "escalation_fired": []},
            "concentration": {"available": False}, "safe_history": {"available": False},
            "recent_flows": {"available": False}, "vc_overlap": {"available": False},
            "coverage": {}}


def _fake_perpfinder_empty(mode, ticker=None, **kw):
    return {"ok": True, "mode": mode, "rows": [], "meta": {}}


def _fake_risk_card_layer(ticker, thesis_display, equity_arg=None):
    return {"available": False, "reason": "stub"}


def _patch_all():
    saved = {k: getattr(BR, k) for k in
             ("load_watchlist", "classify_token", "live_perp",
              "build_analyse", "build_depth", "build_onchain", "fetch_aster_symbols",
              "build_perpfinder", "_risk_card_layer", "LAYER_BUDGET", "POST_LAYER_BUDGET")}
    BR.load_watchlist = _fake_watchlist_no_thesis
    BR.classify_token = _fake_classify_token
    BR.live_perp = lambda t: {"price": 1.0, "funding_4h": 0.01}
    BR.build_analyse = _fake_analyse
    BR.build_depth = _fake_depth
    BR.build_onchain = _fake_onchain
    BR.fetch_aster_symbols = lambda: None
    BR.build_perpfinder = _fake_perpfinder_empty
    BR._risk_card_layer = _fake_risk_card_layer

    def restore():
        for k, v in saved.items():
            setattr(BR, k, v)
    return restore


# ── Defect 1: brief per-component budgets + non-blocking abandonment ───────────────────
class BriefNeverBlocksPastItsBudget(unittest.TestCase):
    def setUp(self):
        self._restore = _patch_all()

    def tearDown(self):
        self._restore()

    def test_a_hung_main_loop_layer_times_out_without_blocking_the_return(self):
        """Sleeps 2s (far past a 0.2s LAYER_BUDGET); with the pre-SPEC-172 `with`-block
        bug this call would take ~2s (the whole brief waits for the abandoned thread on
        executor shutdown). Post-fix it must return in well under 1s."""
        BR.LAYER_BUDGET = 0.2

        def _slow_analyse(ticker, days=90):
            time.sleep(2)
            return _fake_analyse(ticker, days)
        BR.build_analyse = _slow_analyse

        t0 = time.time()
        b = BR.build_brief("ZTEST")
        elapsed = time.time() - t0

        self.assertLess(elapsed, 1.5, f"build_brief blocked on the hung layer: {elapsed:.2f}s")
        self.assertIn("perp", b["timed_out"])
        self.assertFalse(b["perp"]["available"])
        self.assertIn("exceeded", b["perp"]["reason"])
        # partial output: every OTHER main-loop component still completed normally
        self.assertTrue(b["books"]["available"])
        self.assertNotIn("books", b["timed_out"])

    def test_a_hung_post_loop_layer_times_out_without_blocking_the_return(self):
        """Same proof for the post-main-loop batch (defended_fade/unlocks/venue_breadth/
        risk_card) — these run in a SECOND wave and were entirely unbounded pre-SPEC-172
        (risk_card had no timeout at all)."""
        BR.POST_LAYER_BUDGET = 0.2

        def _slow_risk_card(ticker, thesis_display, equity_arg=None):
            time.sleep(2)
            return {"available": True, "line": "should never surface"}
        BR._risk_card_layer = _slow_risk_card

        t0 = time.time()
        b = BR.build_brief("ZTEST")
        elapsed = time.time() - t0

        self.assertLess(elapsed, 1.5, f"build_brief blocked on the hung risk_card layer: {elapsed:.2f}s")
        self.assertIn("risk_card", b["timed_out"])
        self.assertFalse(b["risk_card"]["available"])
        self.assertIn("exceeded", b["risk_card"]["reason"])
        # a sibling post-loop layer (unaffected) still completed
        self.assertNotIn("venue_breadth", b["timed_out"])

    def test_no_timeout_never_returns_an_empty_error_partial_output_intact(self):
        """Requirement (b): brief must return every component that completed plus
        `timed_out: [...]` — never an empty error. Happy path: timed_out is empty and
        every documented top-level section is present."""
        b = BR.build_brief("ZTEST")
        self.assertEqual(b["timed_out"], [])
        for k in ("ticker", "state", "perp", "books", "onchain", "headline",
                  "defended_fade", "unlocks", "venue_breadth", "risk_card"):
            self.assertIn(k, b, f"missing top-level key {k}")

    def test_meta_carries_per_component_ms_for_every_layer(self):
        """Requirement (a): log per-component ms in meta."""
        b = BR.build_brief("ZTEST")
        ms = b["meta"]["layer_ms"]
        for name in ("state", "perp", "books", "onchain", "liqs", "phase",
                    "defended_fade", "unlocks", "venue_breadth", "risk_card"):
            self.assertIn(name, ms, f"meta.layer_ms missing {name}")
            self.assertIsInstance(ms[name], int)
            self.assertGreaterEqual(ms[name], 0)


# ── Defect 2: ledger --allow-new is verbatim; *_watch signatures never heuristic-mapped ─
class LedgerAllowNewIsVerbatim(unittest.TestCase):
    def setUp(self):
        self._orig_ledger = LG.LEDGER_PATH
        self._orig_wl = LG.WL_PATH
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        LG.LEDGER_PATH = Path(self._tmp.name) / "ledger.jsonl"

    def tearDown(self):
        LG.LEDGER_PATH = self._orig_ledger
        LG.WL_PATH = self._orig_wl

    def test_allow_new_stores_verbatim_even_when_it_would_heuristic_match(self):
        # "squeeze_exhaust_watch" contains "squeeze" — _raw_canon_strict would map it to
        # trap_formation_long; --allow-new must bypass the heuristic entirely.
        rec = {"ticker": "STORJ", "direction": "WATCH", "outcome": "retired_unfilled"}
        out = LG.record_cli_signature(rec, "squeeze_exhaust_watch", allow_new=True)
        self.assertEqual(out["record"]["signature"], "squeeze_exhaust_watch")

    def test_watch_direction_signature_never_heuristic_mapped_even_without_allow_new(self):
        # a *_watch signature must never resolve into a trade signature, allow_new or not.
        rec = {"ticker": "STORJ", "direction": "WATCH", "outcome": "retired_unfilled"}
        out = LG.record_cli_signature(rec, "squeeze_exhaust_watch", allow_new=False)
        self.assertEqual(out["record"]["signature"], "squeeze_exhaust_watch")

    def test_watch_rows_excluded_from_trade_signature_aggregate_and_bucketed_under_watch(self):
        rec1 = {"ticker": "STORJ", "direction": "WATCH", "outcome": "retired_unfilled",
               "close_ts": "2026-08-24T00:00:00Z"}
        LG.record_cli_signature(rec1, "squeeze_exhaust_watch", allow_new=True)
        # a real trap_formation_long fill, for contrast — must NOT be polluted by the watch row
        LG.record({"ticker": "BMT", "direction": "LONG", "signature": "trap_formation_long",
                  "outcome": "tp1", "pnl_r": 1.84, "close_ts": "2026-08-25T22:46:18Z"})
        st = LG.stats(signature="trap_formation_long")
        self.assertEqual(st["n_filled"], 1)     # the WATCH row never counts as a trade fill
        watch_bucket = LG.stats().get("watch") or {}
        self.assertIn("squeeze_exhaust_watch", json.dumps(watch_bucket))

    def test_migration_dry_run_lists_mistagged_rows_without_writing(self):
        # simulate the exact incident: a WATCH row whose signature landed under a trade
        # bucket (the pre-fix bug), with a watchlist thesis naming the correct signature.
        LG.record({"ticker": "STORJ", "direction": "WATCH", "signature": "trap_formation_long",
                  "outcome": "retired_unfilled", "close_ts": "2026-08-24T00:00:00Z"})
        wl_tmp = Path(self._tmp.name) / "watchlist.json"
        wl_tmp.write_text(json.dumps({"tokens": [
            {"ticker": "STORJ", "thesis": {"signature": "squeeze_exhaust_watch"}}]}))
        LG.WL_PATH = wl_tmp
        report = LG.migrate_watch_rows(apply=False)
        self.assertTrue(report["dry_run"])
        self.assertEqual(len(report["mistagged"]), 1)
        self.assertEqual(report["mistagged"][0]["ticker"], "STORJ")
        self.assertEqual(report["mistagged"][0]["from"], "trap_formation_long")
        self.assertEqual(report["mistagged"][0]["to"], "squeeze_exhaust_watch")
        # dry-run: the ledger file is untouched
        lines = LG.LEDGER_PATH.read_text().splitlines()
        self.assertEqual(json.loads(lines[0])["signature"], "trap_formation_long")

    def test_migration_apply_rewrites_the_ledger_rows(self):
        LG.record({"ticker": "STORJ", "direction": "WATCH", "signature": "trap_formation_long",
                  "outcome": "retired_unfilled", "close_ts": "2026-08-24T00:00:00Z"})
        wl_tmp = Path(self._tmp.name) / "watchlist.json"
        wl_tmp.write_text(json.dumps({"tokens": [
            {"ticker": "STORJ", "thesis": {"signature": "squeeze_exhaust_watch"}}]}))
        LG.WL_PATH = wl_tmp
        report = LG.migrate_watch_rows(apply=True)
        self.assertFalse(report["dry_run"])
        self.assertEqual(len(report["mistagged"]), 1)
        lines = LG.LEDGER_PATH.read_text().splitlines()
        self.assertEqual(json.loads(lines[0])["signature"], "squeeze_exhaust_watch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
