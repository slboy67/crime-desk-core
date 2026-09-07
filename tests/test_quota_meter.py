#!/usr/bin/env python3
"""SPEC 66 — Moralis quota meter: per-UTC-day call counter + warning/degrade thresholds.

Run:  python3 tests/test_quota_meter.py

Offline-deterministic: the clock is injected (now=...) and the state file is a tmp path,
so no wall-clock or real Moralis call is touched. The memory lesson this guards is
`reference_moralis_free_daily_quota_gates_onchain` — the quota death is silent mid-session;
this meter is what lets the desk SEE the wall before it hits it.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("moralis_quota", ROOT / "capabilities" / "moralis_quota.py")
Q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(Q)


def _dt(y, mo, d, h=12):
    return datetime(y, mo, d, h, 0, 0, tzinfo=timezone.utc)


class TestQuotaMeter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.path = Path(self.tmp.name)
        self.tmp.close()
        self.path.unlink()  # start with no file (cold)

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_counter_increments(self):
        now = _dt(2026, 6, 11)
        self.assertEqual(Q.calls_today(now=now, state_path=self.path), 0)
        self.assertEqual(Q.record_call(now=now, state_path=self.path), 1)
        self.assertEqual(Q.record_call(now=now, state_path=self.path), 2)
        self.assertEqual(Q.record_call(now=now, state_path=self.path), 3)
        self.assertEqual(Q.calls_today(now=now, state_path=self.path), 3)
        # persisted on disk
        d = json.loads(self.path.read_text())
        self.assertEqual(d["day"], "2026-06-11")
        self.assertEqual(d["calls"], 3)

    def test_resets_on_utc_day_change(self):
        d1 = _dt(2026, 6, 11)
        Q.record_call(now=d1, state_path=self.path)
        Q.record_call(now=d1, state_path=self.path)
        self.assertEqual(Q.calls_today(now=d1, state_path=self.path), 2)
        # next UTC day → counter starts fresh
        d2 = _dt(2026, 6, 12)
        self.assertEqual(Q.calls_today(now=d2, state_path=self.path), 0)
        self.assertEqual(Q.record_call(now=d2, state_path=self.path), 1)

    def test_warning_threshold_fires_above_80pct(self):
        now = _dt(2026, 6, 11)
        budget = 10
        # 7/10 = 70% → no warning
        for _ in range(7):
            Q.record_call(now=now, state_path=self.path)
        st = Q.status(budget=budget, now=now, state_path=self.path)
        self.assertEqual(st["calls"], 7)
        self.assertEqual(st["pct"], 70)
        self.assertFalse(st["warn"])
        self.assertFalse(st["exhausted"])
        self.assertEqual(st["prefix"], "")
        # 9/10 = 90% → warning fires, prefix present
        Q.record_call(now=now, state_path=self.path)
        Q.record_call(now=now, state_path=self.path)
        st = Q.status(budget=budget, now=now, state_path=self.path)
        self.assertEqual(st["pct"], 90)
        self.assertTrue(st["warn"])
        self.assertFalse(st["exhausted"])
        self.assertEqual(st["prefix"], "[QUOTA 90%]")

    def test_exhausted_at_100pct(self):
        now = _dt(2026, 6, 11)
        budget = 5
        for _ in range(5):
            Q.record_call(now=now, state_path=self.path)
        st = Q.status(budget=budget, now=now, state_path=self.path)
        self.assertEqual(st["pct"], 100)
        self.assertTrue(st["warn"])
        self.assertTrue(st["exhausted"])
        self.assertEqual(st["prefix"], "[QUOTA 100%]")

    def test_prefix_helper_prepends_only_when_warning(self):
        now = _dt(2026, 6, 11)
        budget = 10
        # below threshold: note unchanged
        self.assertEqual(Q.with_prefix("clean read", budget=budget, now=now, state_path=self.path),
                         "clean read")
        for _ in range(9):
            Q.record_call(now=now, state_path=self.path)
        # above threshold: prefix prepended
        self.assertEqual(Q.with_prefix("clean read", budget=budget, now=now, state_path=self.path),
                         "[QUOTA 90%] clean read")

    def test_corrupt_state_file_is_treated_as_cold(self):
        self.path.write_text("{not valid json")
        now = _dt(2026, 6, 11)
        self.assertEqual(Q.calls_today(now=now, state_path=self.path), 0)
        self.assertEqual(Q.record_call(now=now, state_path=self.path), 1)


class TestCallerAttribution(unittest.TestCase):
    """SPEC-123: state/moralis_quota.json must answer 'what spent the quota today' from the
    file alone — per-caller counts + a CU estimate, not just a flat total."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.path = Path(self.tmp.name)
        self.tmp.close()
        self.path.unlink()

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_by_caller_breakdown_accumulates_per_caller(self):
        now = _dt(2026, 7, 14)
        Q.record_call(now=now, state_path=self.path, caller="verify_wallet")
        Q.record_call(now=now, state_path=self.path, caller="verify_wallet")
        Q.record_call(now=now, state_path=self.path, caller="onchain_radar")
        d = json.loads(self.path.read_text())
        self.assertEqual(d["by_caller"]["verify_wallet"]["calls"], 2)
        self.assertEqual(d["by_caller"]["onchain_radar"]["calls"], 1)
        self.assertEqual(d["calls"], 3)   # the flat total stays intact

    def test_cu_estimate_recorded_per_caller_and_total(self):
        now = _dt(2026, 7, 14)
        Q.record_call(now=now, state_path=self.path, caller="verify_wallet")
        d = json.loads(self.path.read_text())
        self.assertGreater(d["by_caller"]["verify_wallet"]["cu_estimate"], 0)
        self.assertEqual(d["cu_estimate"], d["by_caller"]["verify_wallet"]["cu_estimate"])

    def test_explicit_cu_weight_overrides_default(self):
        now = _dt(2026, 7, 14)
        Q.record_call(now=now, state_path=self.path, caller="board_tick", cu=40)
        d = json.loads(self.path.read_text())
        self.assertEqual(d["by_caller"]["board_tick"]["cu_estimate"], 40)
        self.assertEqual(d["cu_estimate"], 40)

    def test_by_caller_resets_on_utc_day_change(self):
        d1 = _dt(2026, 7, 14)
        Q.record_call(now=d1, state_path=self.path, caller="verify_wallet")
        d2 = _dt(2026, 7, 15)
        Q.record_call(now=d2, state_path=self.path, caller="onchain_radar")
        d = json.loads(self.path.read_text())
        self.assertNotIn("verify_wallet", d["by_caller"])
        self.assertEqual(d["by_caller"]["onchain_radar"]["calls"], 1)
        self.assertEqual(d["calls"], 1)

    def test_caller_defaults_to_the_running_script_name(self):
        # no explicit caller — falls back to Path(sys.argv[0]).stem so every existing
        # moralis_quota.record_call() call site attributes itself with zero plumbing.
        now = _dt(2026, 7, 14)
        Q.record_call(now=now, state_path=self.path)
        d = json.loads(self.path.read_text())
        expected = Path(sys.argv[0]).stem
        self.assertIn(expected, d["by_caller"])


class TestProviderQuotaCallerPassthrough(unittest.TestCase):
    """provider_quota (etherscan/bitquery) shares moralis_quota's mechanics (SPEC-97) — the
    caller/CU attribution must cover it too (SPEC-123 notes: 'if the attribution change is a
    shared meter, cover both')."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "provider_quota", ROOT / "capabilities" / "provider_quota.py")
        self.PQ = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.PQ)
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.path = Path(self.tmp.name)
        self.tmp.close()
        self.path.unlink()

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_etherscan_call_attributes_to_caller(self):
        now = _dt(2026, 7, 14)
        self.PQ.record_call("etherscan", now=now, state_path=self.path, caller="verify_wallet")
        d = json.loads(self.path.read_text())
        self.assertEqual(d["by_caller"]["verify_wallet"]["calls"], 1)
        self.assertGreater(d["by_caller"]["verify_wallet"]["cu_estimate"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
