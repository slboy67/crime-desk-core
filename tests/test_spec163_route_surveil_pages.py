#!/usr/bin/env python3
"""SPEC-163 — route ops/surveil.sh pages (safe FIRED / contract safe DRAINED) through
ACT/DESK, the sender SPEC-157 missed.

ops/surveil.sh calls ops/notify.sh three times with NO 4th `route` arg (safe FIRED,
surveillance BLIND, contract safe DRAINED), so every one defaulted to `act` -> phone,
regardless of whether the fired/drained ticker was ever held or committed to.

Covers:
  - ops/route_for.py: `qualifies`/`qualifying_tickers`/`route_for_tickers` — a ticker
    with an open position (config/positions.json) or a thesis status ARMED/LIVE is ACT;
    everything else is DESK. Reuses board_tick's own loaders (`_load_open_position_
    tickers`, `load_thesis_watchlist`) as the one source of truth, so this can never
    drift from board_tick._route_for's (SPEC-157) idea of "held or committed".
  - ops/page_grammar.page_safe_event: the composed body
    `SAFE FIRED: TICKER label -$usd | ... -> read board / exit?`.
  - ops/safe_page.py: parses the onchain_board envelope / balance_surveil tick output
    into {ticker, label, usd} entries, decides route (ACT if ANY listed ticker
    qualifies), and reorders qualifying tickers first.
  - ops/notify.sh: route wiring already proven by SPEC-157 — re-exercised here end to
    end with a safe_page-composed body so the whole pipeline is covered, not just its
    pieces in isolation.
  - ops/surveil.sh: source-level wiring — FIRED/DRAINED calls route dynamically via
    `"$route"`; the BLIND call is hardcoded `"desk"` (req 2 — an ops-health message,
    never a per-ticker decision).

Offline-deterministic: board_tick's POSITIONS_PATH / load_thesis_watchlist are
monkeypatched to tmp fixtures; notify.sh tests stub osascript/curl on PATH exactly like
tests/test_spec157_act_desk_routing.py.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTIFY_SH = ROOT / "ops" / "notify.sh"
SURVEIL_SH = ROOT / "ops" / "surveil.sh"


def _load(name, sub="ops"):
    spec = importlib.util.spec_from_file_location(name, ROOT / sub / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Loading safe_page (isolated instance) triggers ITS OWN plain `import route_for`,
# which in turn plain-`import board_tick`s — both real imports that register under
# sys.modules["route_for"] / sys.modules["board_tick"] as a side effect. Grabbing them
# by plain import afterward returns those SAME cached instances, so patching
# board_tick.POSITIONS_PATH here is visible inside safe_page's routing calls too.
SP = _load("safe_page", sub="ops")
import route_for   # noqa: E402
import board_tick  # noqa: E402

PG = _load("page_grammar", sub="ops")


# ── ops/route_for.py — pure qualification rule ────────────────────────────────────

class TestQualifies(unittest.TestCase):
    def test_open_position_qualifies(self):
        self.assertTrue(route_for.qualifies("CYS", {"CYS"}, {}))

    def test_no_position_no_thesis_does_not_qualify(self):
        self.assertFalse(route_for.qualifies("BIRB", set(), {}))

    def test_armed_thesis_qualifies(self):
        self.assertTrue(route_for.qualifies("GALA", set(), {"GALA": "ARMED"}))

    def test_live_thesis_qualifies(self):
        self.assertTrue(route_for.qualifies("GALA", set(), {"GALA": "LIVE"}))

    def test_watch_thesis_does_not_qualify(self):
        self.assertFalse(route_for.qualifies("GALA", set(), {"GALA": "WATCH"}))

    def test_lowercase_status_still_qualifies(self):
        self.assertTrue(route_for.qualifies("GALA", set(), {"GALA": "armed"}))

    def test_unreadable_positions_degrades_to_qualifies(self):
        # open_tickers=None -> positions.json unreadable -> never risk silencing a page
        self.assertTrue(route_for.qualifies("ANYTHING", None, {}))


class _RouteForTmp(unittest.TestCase):
    """Monkeypatches the shared board_tick singleton route_for/safe_page call into."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self._orig_pos = board_tick.POSITIONS_PATH
        self._orig_wl = board_tick.load_thesis_watchlist
        board_tick.POSITIONS_PATH = Path(self.dir.name) / "positions.json"
        board_tick.POSITIONS_PATH.write_text(json.dumps({"positions": []}))
        board_tick.load_thesis_watchlist = lambda: ([], None)

    def tearDown(self):
        board_tick.POSITIONS_PATH = self._orig_pos
        board_tick.load_thesis_watchlist = self._orig_wl
        self.dir.cleanup()

    def set_positions(self, *tickers):
        board_tick.POSITIONS_PATH.write_text(json.dumps(
            {"positions": [{"ticker": t} for t in tickers]}))

    def set_thesis(self, ticker, status):
        board_tick.load_thesis_watchlist = lambda: (
            [{"ticker": ticker, "thesis": {"status": status}}], None)


class TestQualifyingTickers(_RouteForTmp):
    def test_held_ticker_routes_act(self):
        self.set_positions("CASHCAT")
        self.assertEqual(route_for.route_for_tickers(["CASHCAT"]), "act")

    def test_unheld_untracked_ticker_routes_desk(self):
        self.assertEqual(route_for.route_for_tickers(["RANDOM"]), "desk")

    def test_armed_thesis_ticker_routes_act(self):
        self.set_thesis("GALA", "ARMED")
        self.assertEqual(route_for.route_for_tickers(["GALA"]), "act")

    def test_mixed_list_any_qualifying_routes_act(self):
        self.set_positions("CASHCAT")
        self.assertEqual(route_for.route_for_tickers(["RANDOM", "CASHCAT"]), "act")

    def test_sort_qualifying_first_reorders(self):
        self.set_positions("CASHCAT")
        self.assertEqual(route_for.sort_qualifying_first(["RANDOM", "CASHCAT", "OTHER"]),
                         ["CASHCAT", "RANDOM", "OTHER"])

    def test_sort_qualifying_first_no_qualifiers_preserves_order(self):
        self.assertEqual(route_for.sort_qualifying_first(["RANDOM", "OTHER"]),
                         ["RANDOM", "OTHER"])


# ── ops/page_grammar.page_safe_event — body composition ───────────────────────────

class TestPageSafeEvent(unittest.TestCase):
    def test_single_entry_fired_format(self):
        body = PG.page_safe_event("FIRED", [{"ticker": "CASHCAT", "label": "team safe", "usd": 12345}])
        self.assertEqual(body, "SAFE FIRED: CASHCAT team safe -$12.35K → read board / exit?")

    def test_drained_event_word(self):
        body = PG.page_safe_event("DRAINED", [{"ticker": "GALA", "label": "vault", "usd": 500}])
        self.assertTrue(body.startswith("SAFE DRAINED: GALA vault -$500"))

    def test_no_usd_omits_dollar_segment(self):
        body = PG.page_safe_event("FIRED", [{"ticker": "GALA", "label": "vault", "usd": None}])
        self.assertEqual(body, "SAFE FIRED: GALA vault → read board / exit?")

    def test_multi_entry_order_preserved_as_given(self):
        body = PG.page_safe_event("FIRED", [
            {"ticker": "CASHCAT", "label": "team safe", "usd": 100},
            {"ticker": "RANDOM", "label": "x", "usd": 1},
        ])
        self.assertLess(body.index("CASHCAT"), body.index("RANDOM"))
        self.assertTrue(body.endswith("→ read board / exit?"))


# ── ops/safe_page.py — entry extraction ────────────────────────────────────────────

class TestEntriesFromFiredEnvelope(unittest.TestCase):
    def test_lead_wallet_plus_more_count(self):
        env = {"data": {"alerts": [
            {"ticker": "CASHCAT", "escalation_fired": [
                {"label": "team safe", "distributed_usd": 12345},
                {"label": "staging", "distributed_usd": 10},
            ]},
        ]}}
        entries = SP.entries_from_fired_envelope(env)
        self.assertEqual(entries, [{"ticker": "CASHCAT", "label": "team safe +1 more", "usd": 12345}])

    def test_single_wallet_no_more_suffix(self):
        env = {"data": {"alerts": [
            {"ticker": "GALA", "escalation_fired": [{"label": "vault", "distributed_usd": 5}]},
        ]}}
        entries = SP.entries_from_fired_envelope(env)
        self.assertEqual(entries, [{"ticker": "GALA", "label": "vault", "usd": 5}])

    def test_no_alerts_empty(self):
        self.assertEqual(SP.entries_from_fired_envelope({"data": {"alerts": []}}), [])

    def test_malformed_envelope_never_raises(self):
        self.assertEqual(SP.entries_from_fired_envelope({}), [])


class TestEntriesFromDrainedPayload(unittest.TestCase):
    def test_per_wallet_entries(self):
        payload = {"GALA": {"fired": [{"label": "vault", "delta": -718135}]}}
        entries = SP.entries_from_drained_payload(payload)
        self.assertEqual(entries, [{"ticker": "GALA", "label": "vault", "usd": -718135}])

    def test_empty_payload(self):
        self.assertEqual(SP.entries_from_drained_payload({}), [])


# ── ops/safe_page.py — route_and_compose (the DoD scenarios) ──────────────────────

class TestRouteAndCompose(_RouteForTmp):
    def test_fired_safe_on_held_ticker_routes_act_with_safe_fired_body(self):
        self.set_positions("CASHCAT")
        entries = [{"ticker": "CASHCAT", "label": "team safe", "usd": 12345}]
        route, body = SP.route_and_compose("FIRED", entries)
        self.assertEqual(route, "act")
        self.assertTrue(body.startswith("SAFE FIRED: CASHCAT team safe -$12.35K"))

    def test_fired_safe_on_unheld_ticker_routes_desk(self):
        entries = [{"ticker": "RANDOM", "label": "x safe", "usd": 500}]
        route, body = SP.route_and_compose("FIRED", entries)
        self.assertEqual(route, "desk")
        self.assertIn("RANDOM", body)

    def test_mixed_summary_routes_act_held_ticker_first(self):
        self.set_positions("CASHCAT")
        entries = [{"ticker": "RANDOM", "label": "x", "usd": 1},
                   {"ticker": "CASHCAT", "label": "team safe", "usd": 99}]
        route, body = SP.route_and_compose("FIRED", entries)
        self.assertEqual(route, "act")
        self.assertLess(body.index("CASHCAT"), body.index("RANDOM"))

    def test_no_entries_returns_none(self):
        self.assertEqual(SP.route_and_compose("FIRED", []), (None, None))

    def test_drained_on_armed_thesis_ticker_routes_act(self):
        self.set_thesis("GALA", "LIVE")
        entries = [{"ticker": "GALA", "label": "vault", "usd": -718135}]
        route, body = SP.route_and_compose("DRAINED", entries)
        self.assertEqual(route, "act")
        self.assertTrue(body.startswith("SAFE DRAINED: GALA vault"))


# ── end-to-end through ops/notify.sh (stubbed osascript/curl) ─────────────────────

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
        import os
        self.env = dict(os.environ)
        self.env["PATH"] = f"{d}:{self.env.get('PATH', '')}"
        self.env["CRIMEDESK_NTFY_URL"] = "https://ntfy.sh/test"
        self.env.pop("CRIMEDESK_NOTIFY", None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_notify(self, *args):
        return subprocess.run(["bash", str(NOTIFY_SH), *args],
                              env=self.env, capture_output=True, text=True, timeout=10)


class TestEndToEndPageDelivery(_StubbedPath):
    def test_held_ticker_fired_page_posts_to_ntfy(self):
        body = PG.page_safe_event("FIRED", [{"ticker": "CASHCAT", "label": "team safe", "usd": 12345}])
        self.run_notify("crime-desk — safe FIRED", body, "urgent", "act")
        self.assertTrue(self.curl_log.exists(), "held-ticker FIRED page must POST to ntfy")
        self.assertIn("SAFE FIRED: CASHCAT", self.curl_log.read_text())

    def test_unheld_ticker_fired_page_never_posts_but_still_desktop(self):
        body = PG.page_safe_event("FIRED", [{"ticker": "RANDOM", "label": "x safe", "usd": 500}])
        self.run_notify("crime-desk — safe FIRED", body, "urgent", "desk")
        self.assertFalse(self.curl_log.exists(), "unheld-ticker FIRED page must never POST")
        self.assertTrue(self.osa_log.exists(), "desktop leg must still fire on desk route")

    def test_blind_page_never_posts(self):
        self.run_notify("crime-desk — surveillance BLIND", "coverage collapse", "", "desk")
        self.assertFalse(self.curl_log.exists(), "surveillance BLIND must never reach the phone")
        self.assertTrue(self.osa_log.exists())

    def test_mixed_summary_posts_with_held_ticker_leading(self):
        body = PG.page_safe_event("FIRED", [
            {"ticker": "RANDOM", "label": "x", "usd": 1},
            {"ticker": "CASHCAT", "label": "team safe", "usd": 99},
        ])
        self.run_notify("crime-desk — safe FIRED", body, "urgent", "act")
        self.assertTrue(self.curl_log.exists())


# ── ops/surveil.sh — source-level wiring ───────────────────────────────────────────

class TestSurveilShWiring(unittest.TestCase):
    def setUp(self):
        self.src = SURVEIL_SH.read_text()

    def test_fired_page_uses_safe_page_and_dynamic_route(self):
        self.assertIn("ops/safe_page.py fired", self.src)
        fired_calls = [ln for ln in self.src.splitlines()
                       if "ops/notify.sh" in ln and "safe FIRED" in ln]
        self.assertTrue(fired_calls)
        for ln in fired_calls:
            self.assertIn('"$route"', ln, f"FIRED page must route dynamically: {ln}")

    def test_drained_page_uses_safe_page_and_dynamic_route(self):
        self.assertIn("ops/safe_page.py drained", self.src)
        # SPEC-164 made the balance-page title dynamic ("$bal_title": DRAINED / OUTBOUND /
        # OUTBOUND-DRIP by kind) — match the balance page by its composed summary arg.
        drained_calls = [ln for ln in self.src.splitlines()
                         if "ops/notify.sh" in ln and ("DRAINED" in ln or '"$bal_summary"' in ln)]
        self.assertTrue(drained_calls)
        for ln in drained_calls:
            self.assertIn('"$route"', ln, f"DRAINED page must route dynamically: {ln}")

    def test_blind_page_hardcoded_desk(self):
        blind_calls = [ln for ln in self.src.splitlines()
                       if "ops/notify.sh" in ln and "BLIND" in ln]
        self.assertTrue(blind_calls)
        for ln in blind_calls:
            self.assertIn('"desk"', ln, f"BLIND page must always route desk: {ln}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
