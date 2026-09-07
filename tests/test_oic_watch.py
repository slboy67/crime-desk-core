#!/usr/bin/env python3
"""SPEC-180 req 6 (G7) — ops/oic_watch.py: OIC_FLIP / ROLE_DRIFT alerting.

Run:  python3 -m unittest tests.test_oic_watch -v

Offline-deterministic: `oic_fn`/`has_thesis_fn`/`notify_fn` all injected; tmp state
paths per test.
"""
import importlib.util
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("oic_watch", ROOT / "ops" / "oic_watch.py")
OW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OW)


# ---------------------------------------------------------------------------
# classify_flip — the 2-tick debounce, UNKNOWN never participates
# ---------------------------------------------------------------------------
class TestClassifyFlip(unittest.TestCase):
    def test_single_tick_never_fires(self):
        direction, st, _ = OW.classify_flip({"last": "DIRECTIONAL"}, "ARB_DOMINATED")
        self.assertIsNone(direction)
        self.assertEqual(st["pending"], "ARB_DOMINATED")
        self.assertEqual(st["pending_count"], 1)

    def test_two_consecutive_ticks_fires_fuel_evaporating(self):
        st = {"last": "DIRECTIONAL"}
        _, st, _ = OW.classify_flip(st, "ARB_DOMINATED")
        direction, st, prior = OW.classify_flip(st, "ARB_DOMINATED")
        self.assertEqual(direction, "fuel_evaporating")
        self.assertEqual(prior, "DIRECTIONAL")
        self.assertEqual(st["last"], "ARB_DOMINATED")
        self.assertEqual(st["pending_count"], 0)

    def test_reverse_direction_is_fuel_arriving(self):
        st = {"last": "ARB_DOMINATED"}
        _, st, _ = OW.classify_flip(st, "DIRECTIONAL")
        direction, st, prior = OW.classify_flip(st, "DIRECTIONAL")
        self.assertEqual(direction, "fuel_arriving")

    def test_unknown_tick_is_total_noop_x_unknown_x(self):
        st = {"last": "DIRECTIONAL", "pending": "ARB_DOMINATED", "pending_count": 1}
        direction, new_st, _ = OW.classify_flip(st, "UNKNOWN")
        self.assertIsNone(direction)
        self.assertIs(new_st, st)   # literally unchanged, not even copied

    def test_x_unknown_y_still_needs_two_y_ticks(self):
        st = {"last": "DIRECTIONAL"}
        _, st, _ = OW.classify_flip(st, "UNKNOWN")   # no-op
        _, st, _ = OW.classify_flip(st, "ARB_DOMINATED")   # pending=1
        direction, st, _ = OW.classify_flip(st, "UNKNOWN")   # no-op, pending stays 1
        self.assertIsNone(direction)
        self.assertEqual(st["pending_count"], 1)
        direction, st, _ = OW.classify_flip(st, "ARB_DOMINATED")   # pending=2 -> fires
        self.assertEqual(direction, "fuel_evaporating")

    def test_reverting_to_last_before_second_tick_resets_pending_no_fire(self):
        st = {"last": "DIRECTIONAL"}
        _, st, _ = OW.classify_flip(st, "ARB_DOMINATED")   # pending=1
        direction, st, _ = OW.classify_flip(st, "DIRECTIONAL")   # back to last
        self.assertIsNone(direction)
        self.assertEqual(st["pending"], None)
        self.assertEqual(st["pending_count"], 0)

    def test_mixed_counts_as_directional_like(self):
        st = {"last": "MIXED"}
        _, st, _ = OW.classify_flip(st, "ARB_DOMINATED")
        direction, st, _ = OW.classify_flip(st, "ARB_DOMINATED")
        self.assertEqual(direction, "fuel_evaporating")

    def test_no_prior_state_first_ever_read_seeds_no_fire(self):
        direction, st, prior = OW.classify_flip({}, "DIRECTIONAL")
        self.assertIsNone(direction)   # nothing to flip FROM yet
        self.assertIsNone(prior)


# ---------------------------------------------------------------------------
# route_event — the SPEC-180 req 6 table
# ---------------------------------------------------------------------------
class TestRouteEvent(unittest.TestCase):
    def test_fuel_evaporating_live_thesis_is_phone(self):
        self.assertEqual(OW.route_event("oic_flip", "fuel_evaporating", True), "phone")

    def test_fuel_evaporating_no_thesis_is_desk(self):
        self.assertEqual(OW.route_event("oic_flip", "fuel_evaporating", False), "desk")

    def test_fuel_arriving_live_thesis_is_desk(self):
        self.assertEqual(OW.route_event("oic_flip", "fuel_arriving", True), "desk")

    def test_fuel_arriving_no_thesis_is_annotate(self):
        self.assertEqual(OW.route_event("oic_flip", "fuel_arriving", False), "annotate")

    def test_exit_flow_drift_live_thesis_is_phone(self):
        self.assertEqual(OW.route_event("role_drift", "exit_flow", True), "phone")

    def test_mark_constituent_drift_no_thesis_is_annotate(self):
        self.assertEqual(OW.route_event("role_drift", "mark_constituent", False), "annotate")


# ---------------------------------------------------------------------------
# drift detectors
# ---------------------------------------------------------------------------
class TestDetectExitFlowDrift(unittest.TestCase):
    def test_venue_change_between_two_flow_confirmed_snapshots_detected(self):
        prev = {"exit": {"venue": "bitget", "grade": "FLOW_CONFIRMED"}}
        cur = {"exit": {"venue": "binance", "grade": "FLOW_CONFIRMED"}}
        self.assertEqual(OW.detect_exit_flow_drift(prev, cur), ("bitget", "binance"))

    def test_depth_inferred_leg_never_drifts(self):
        prev = {"exit": {"venue": "bitget", "grade": "DEPTH_INFERRED"}}
        cur = {"exit": {"venue": "binance", "grade": "FLOW_CONFIRMED"}}
        self.assertIsNone(OW.detect_exit_flow_drift(prev, cur))

    def test_same_venue_no_drift(self):
        prev = {"exit": {"venue": "bitget", "grade": "FLOW_CONFIRMED"}}
        cur = {"exit": {"venue": "bitget", "grade": "FLOW_CONFIRMED"}}
        self.assertIsNone(OW.detect_exit_flow_drift(prev, cur))


class TestDetectMarkConstituentDrift(unittest.TestCase):
    def test_material_weight_shift_detected(self):
        prev = {"mark_engine": {"weights": {"binance": 43.5, "okx": 17.6}}}
        cur = {"mark_engine": {"weights": {"binance": 60.0, "okx": 10.0}}}
        biggest, pv, cv = OW.detect_mark_constituent_drift(prev, cur, threshold_pct=5.0)
        self.assertEqual(biggest, "binance")

    def test_small_shift_under_threshold_no_drift(self):
        prev = {"mark_engine": {"weights": {"binance": 43.5}}}
        cur = {"mark_engine": {"weights": {"binance": 45.0}}}
        self.assertIsNone(OW.detect_mark_constituent_drift(prev, cur, threshold_pct=5.0))

    def test_missing_weights_no_drift(self):
        self.assertIsNone(OW.detect_mark_constituent_drift(None, {"mark_engine": {}}))


# ---------------------------------------------------------------------------
# process_ticker / run_tick — integration, every event lands in inbox regardless
# ---------------------------------------------------------------------------
class TestRunTick(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.inbox_calls = []
        self._orig_append = OW.inbox.append_event
        OW.inbox.append_event = lambda **kw: self.inbox_calls.append(kw)

    def tearDown(self):
        OW.inbox.append_event = self._orig_append
        self._tmp.cleanup()

    def test_two_ticks_persisting_arb_dominated_pages_phone_for_live_thesis(self):
        state_path = self.tmp / "state.json"
        roles_path = self.tmp / "roles.json"
        page_state_path = self.tmp / "cooldowns.json"
        notified = []

        def oic_fn(t):
            return {"verdict": "ARB_DOMINATED", "gating_ok": True, "venue_roles": {}}

        def notify_fn(title, body, route):
            notified.append((title, body, route))

        # seed state as if the LAST committed verdict was DIRECTIONAL
        OW._write_json(state_path, {"X": {"last": "DIRECTIONAL"}})
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        r1 = OW.run_tick(["X"], oic_fn=oic_fn, has_thesis_fn=lambda t: True, now=now,
                         notify_fn=notify_fn, state_path=state_path, roles_path=roles_path,
                         page_state_path=page_state_path)
        self.assertEqual(r1["paged"], 0)   # first tick only sets pending=1
        r2 = OW.run_tick(["X"], oic_fn=oic_fn, has_thesis_fn=lambda t: True, now=now,
                         notify_fn=notify_fn, state_path=state_path, roles_path=roles_path,
                         page_state_path=page_state_path)
        self.assertEqual(r2["paged"], 1)
        self.assertEqual(notified[0][2], "phone")
        # every event landed in inbox regardless of route
        self.assertTrue(any(c["source"] == "oic_flip" for c in self.inbox_calls))

    def test_annotate_route_never_calls_notify_but_still_inboxed(self):
        state_path = self.tmp / "state.json"
        roles_path = self.tmp / "roles.json"
        page_state_path = self.tmp / "cooldowns.json"
        notified = []
        OW._write_json(state_path, {"X": {"last": "ARB_DOMINATED"}})
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def oic_fn(t):
            return {"verdict": "DIRECTIONAL", "gating_ok": True, "venue_roles": {}}

        OW.run_tick(["X"], oic_fn=oic_fn, has_thesis_fn=lambda t: False, now=now,
                   notify_fn=lambda *a: notified.append(a), state_path=state_path,
                   roles_path=roles_path, page_state_path=page_state_path)
        r2 = OW.run_tick(["X"], oic_fn=oic_fn, has_thesis_fn=lambda t: False, now=now,
                        notify_fn=lambda *a: notified.append(a), state_path=state_path,
                        roles_path=roles_path, page_state_path=page_state_path)
        self.assertEqual(r2["paged"], 0)
        self.assertEqual(notified, [])
        self.assertTrue(any(c["source"] == "oic_flip" for c in self.inbox_calls))

    def test_stale_gating_ok_false_never_pages_treated_as_unknown(self):
        state_path = self.tmp / "state.json"
        roles_path = self.tmp / "roles.json"
        page_state_path = self.tmp / "cooldowns.json"
        OW._write_json(state_path, {"X": {"last": "DIRECTIONAL", "pending": "ARB_DOMINATED",
                                          "pending_count": 1}})
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def oic_fn(t):
            return {"verdict": "ARB_DOMINATED", "gating_ok": False, "venue_roles": {}}

        r = OW.run_tick(["X"], oic_fn=oic_fn, has_thesis_fn=lambda t: True, now=now,
                        state_path=state_path, roles_path=roles_path,
                        page_state_path=page_state_path)
        self.assertEqual(r["paged"], 0)
        st = OW._read_json(state_path, {})
        self.assertEqual(st["X"]["pending_count"], 1)   # untouched — stale is a no-op

    def test_crash_on_one_ticker_never_kills_the_tick(self):
        state_path = self.tmp / "state.json"
        roles_path = self.tmp / "roles.json"
        page_state_path = self.tmp / "cooldowns.json"

        def oic_fn(t):
            if t == "BAD":
                raise RuntimeError("boom")
            return {"verdict": "UNKNOWN", "gating_ok": False, "venue_roles": {}}

        r = OW.run_tick(["BAD", "GOOD"], oic_fn=oic_fn, has_thesis_fn=lambda t: False,
                        state_path=state_path, roles_path=roles_path,
                        page_state_path=page_state_path)
        self.assertEqual(r["checked"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
