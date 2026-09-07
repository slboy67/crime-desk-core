#!/usr/bin/env python3
"""SPEC 66 — page cooldown: a wallet that already paged within N hours logs but does not
re-notify, UNLESS the fire is a tier upgrade or the wallet's first fire after >24h quiet.

Run:  python3 tests/test_page_cooldown.py

Offline-deterministic: clock injected, tmp state file. The lesson this guards: BILL paged
6x in one afternoon — same apparatus wallets re-firing every sweep trains the human to
ignore pages. The LOG line is always written (the gate only throttles the PAGE).
"""
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("page_gate", ROOT / "ops" / "page_gate.py")
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)

T0 = datetime(2026, 6, 11, 14, 0, 0, tzinfo=timezone.utc)
WALLET = "0xabc0000000000000000000000000000000000001"


def _fire(dest_kind="staging-internal", addr=WALLET, label="BILL-dist-1"):
    return {"label": label, "address": addr, "tier": "distribution", "dest_kind": dest_kind}


class TestPageCooldown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.path = Path(self.tmp.name)
        self.tmp.close()
        self.path.unlink()

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_refires_within_cooldown_page_once(self):
        # six sweeps an hour apart (the BILL afternoon) — same wallet, same kind.
        pages = 0
        for i in range(6):
            now = T0 + timedelta(hours=i)  # 0..5h, all within the 6h cooldown of the first
            if G.should_page(_fire(), now=now, state_path=self.path, cooldown_hours=6):
                pages += 1
        # exactly ONE page across the six refires; the other five are logged-not-paged
        self.assertEqual(pages, 1)

    def test_tier_upgrade_pierces_cooldown(self):
        now = T0
        self.assertTrue(G.should_page(_fire("staging-internal"), now=now,
                                      state_path=self.path, cooldown_hours=6))
        # 1h later (well within cooldown) the SAME wallet now fires token_out to a CEX —
        # a tier upgrade → must pierce the cooldown and page again.
        self.assertTrue(G.should_page(_fire("cex-execution"), now=now + timedelta(hours=1),
                                      state_path=self.path, cooldown_hours=6))
        # a third, non-upgrading fire at the same (now-cex) tier 1h later is suppressed
        self.assertFalse(G.should_page(_fire("cex-execution"), now=now + timedelta(hours=2),
                                       state_path=self.path, cooldown_hours=6))

    def test_first_fire_after_24h_quiet_pages(self):
        now = T0
        self.assertTrue(G.should_page(_fire(), now=now, state_path=self.path, cooldown_hours=6))
        # 25h later the wallet re-emerges after a long quiet → page (notable re-emergence)
        self.assertTrue(G.should_page(_fire(), now=now + timedelta(hours=25),
                                      state_path=self.path, cooldown_hours=6))

    def test_cooldown_elapsed_pages_again(self):
        now = T0
        self.assertTrue(G.should_page(_fire(), now=now, state_path=self.path, cooldown_hours=6))
        # within cooldown: suppressed
        self.assertFalse(G.should_page(_fire(), now=now + timedelta(hours=3),
                                       state_path=self.path, cooldown_hours=6))
        # past the 6h cooldown: pages again
        self.assertTrue(G.should_page(_fire(), now=now + timedelta(hours=7),
                                      state_path=self.path, cooldown_hours=6))

    def test_distinct_wallets_are_independent(self):
        now = T0
        a = "0xaaa0000000000000000000000000000000000001"
        b = "0xbbb0000000000000000000000000000000000002"
        self.assertTrue(G.should_page(_fire(addr=a), now=now, state_path=self.path))
        # different wallet within the same sweep pages on its own first fire
        self.assertTrue(G.should_page(_fire(addr=b), now=now, state_path=self.path))
        # both suppressed on immediate refire
        self.assertFalse(G.should_page(_fire(addr=a), now=now, state_path=self.path))
        self.assertFalse(G.should_page(_fire(addr=b), now=now, state_path=self.path))

    def test_gate_envelope_throttles_but_reports_all(self):
        # the orchestrator onchain_board envelope shape: alerts[].escalation_fired[]
        env = {"data": {"alerts": [
            {"ticker": "BILL", "escalation_fired": [_fire(label="BILL-d1", addr=WALLET)]},
        ]}}
        page, decisions = G.gate_envelope(env, now=T0, state_path=self.path, cooldown_hours=6)
        self.assertTrue(page)
        self.assertEqual(len(decisions), 1)
        # immediate re-sweep: same wallet → no page, but the decision is still reported
        page2, decisions2 = G.gate_envelope(env, now=T0, state_path=self.path, cooldown_hours=6)
        self.assertFalse(page2)
        self.assertEqual(len(decisions2), 1)


def _fund_fire(ticker, band="watch"):
    """funding_surveil.sh's fire shape (ops/funding_surveil.sh): address=FUND:<ticker>,
    dest_kind = the band tier ("watch"/"high") so a WATCH->HIGH deepen is a tier upgrade."""
    return {"address": f"FUND:{ticker}", "label": f"{ticker} deep-neg", "dest_kind": band}


class TestFundingSurveil24hDedup(unittest.TestCase):
    """SPEC-154 req 4: ops/funding_surveil.sh wires page_gate at a 24h cooldown (not the
    6h wallet-activity default) — a deep-neg name pages at most once/24h at the same
    severity; a WATCH->HIGH deepen (tier upgrade) still pierces the window."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.path = Path(self.tmp.name)
        self.tmp.close()
        self.path.unlink()

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_same_name_same_severity_3h_apart_pages_once(self):
        now = T0
        self.assertTrue(G.should_page(_fund_fire("ONG"), now=now,
                                      state_path=self.path, cooldown_hours=24))
        self.assertFalse(G.should_page(_fund_fire("ONG"), now=now + timedelta(hours=3),
                                       state_path=self.path, cooldown_hours=24))
        # still within 24h — still suppressed
        self.assertFalse(G.should_page(_fund_fire("ONG"), now=now + timedelta(hours=20),
                                       state_path=self.path, cooldown_hours=24))
        # past 24h — pages again
        self.assertTrue(G.should_page(_fund_fire("ONG"), now=now + timedelta(hours=25),
                                      state_path=self.path, cooldown_hours=24))

    def test_watch_to_high_escalation_inside_24h_pierces_window(self):
        now = T0
        self.assertTrue(G.should_page(_fund_fire("ONG", "watch"), now=now,
                                      state_path=self.path, cooldown_hours=24))
        # 3h later, well inside the 24h window, but the band DEEPENED watch->high
        self.assertTrue(G.should_page(_fund_fire("ONG", "high"), now=now + timedelta(hours=3),
                                      state_path=self.path, cooldown_hours=24))
        # a third fire at the same (now-high) severity 3h later is suppressed
        self.assertFalse(G.should_page(_fund_fire("ONG", "high"), now=now + timedelta(hours=6),
                                       state_path=self.path, cooldown_hours=24))


class TestCliCooldownHoursForwarding(unittest.TestCase):
    """SPEC-154: `page_gate.py gate --cooldown-hours N` must forward N through to
    gate_envelope — this is what lets funding_surveil.sh (24h) and surveil.sh (6h,
    unspecified -> default) share the same throttle at different tolerances."""

    def test_cli_gate_forwards_cooldown_hours(self):
        import io
        captured = {}

        def fake_gate_envelope(env, now=None, state_path=G.COOLDOWN_PATH,
                               cooldown_hours=G.DEFAULT_COOLDOWN_HOURS):
            captured["cooldown_hours"] = cooldown_hours
            return False, []

        orig_gate_envelope, orig_stdin = G.gate_envelope, sys.stdin
        G.gate_envelope = fake_gate_envelope
        sys.stdin = io.StringIO("{}")
        try:
            G._cli_gate(cooldown_hours=24)
        finally:
            G.gate_envelope = orig_gate_envelope
            sys.stdin = orig_stdin
        self.assertEqual(captured["cooldown_hours"], 24)

    def test_cli_gate_defaults_to_6h_when_unset(self):
        import io
        captured = {}

        def fake_gate_envelope(env, now=None, state_path=G.COOLDOWN_PATH,
                               cooldown_hours=G.DEFAULT_COOLDOWN_HOURS):
            captured["cooldown_hours"] = cooldown_hours
            return False, []

        orig_gate_envelope, orig_stdin = G.gate_envelope, sys.stdin
        G.gate_envelope = fake_gate_envelope
        sys.stdin = io.StringIO("{}")
        try:
            G._cli_gate()
        finally:
            G.gate_envelope = orig_gate_envelope
            sys.stdin = orig_stdin
        self.assertEqual(captured["cooldown_hours"], G.DEFAULT_COOLDOWN_HOURS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
