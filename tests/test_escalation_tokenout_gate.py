#!/usr/bin/env python3
"""SPEC 24 — onchain ESCALATION must be gated by tracked-token-out recency, not nonce-delta alone.

Run:  python3 tests/test_escalation_tokenout_gate.py

A nonce increment proves the WALLET transacted, not that it moved the TRACKED TOKEN. On
ultra-high-frequency CEX-MM EOAs the nonce churns regardless of token → a nonce-only escalation
is noise (the live SKYAI false-fire). HIGH-NONCE fired wallets require a recent tracked-token OUT
to confirm; stale token-out → NONCE-CHURN (neutral), not ESCALATION. Low-nonce dormant safes still
escalate on the nonce alone (the §8 early-warning edge). Network mocked — offline-deterministic.
"""
import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


OC = _load("onchain")
NOW = 1780600000.0
FRESH = datetime.fromtimestamp(NOW - 3600, tz=timezone.utc).isoformat()      # 1h ago (within 6h window)
STALE = datetime.fromtimestamp(NOW - 48 * 3600, tz=timezone.utc).isoformat()  # 48h ago (overnight-stale)

HI = "0x4982085c9e2f89f2ecb8131eca71afad896e89cb"   # high-nonce CEX-MM EOA
LO = "0x00000000000000000000000000000000deadbeef"   # low-nonce dormant safe


def _wallet(addr, label, tier, nonce):
    return {"address": addr, "label": label, "tier": tier, "nonce": nonce,
            "rpc_ok": True, "chain": "binance-smart-chain"}


def _out(frm, ts):
    return {"from_address": frm, "to_address": "0xdead", "value_decimal": "1000",
            "block_timestamp": ts, "token_symbol": "BILL"}


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig = (OC.build_snapshot, OC._load_baseline, OC._save_baseline, OC.token_transfers)
        OC._save_baseline = lambda *a, **k: None

    def tearDown(self):
        OC.build_snapshot, OC._load_baseline, OC._save_baseline, OC.token_transfers = self._orig

    def _run(self, wallets, base_nonces, txs_for):
        OC.build_snapshot = lambda ticker: {"tracked": True, "wallets": wallets,
                                            "fired_count": len(wallets), "primed_unfired_count": 0,
                                            "dormant_count": 0, "n_wallets": len(wallets)}
        OC._load_baseline = lambda ticker: {"ts": NOW - 100, "seeded_ts": NOW - 1000, "nonces": base_nonces}
        OC.token_transfers = txs_for
        return OC.build_nonce_state("BILL", ts=NOW)


class TestHighNonceGate(_Base):
    def test_high_nonce_stale_tokenout_is_churn_not_escalation(self):
        w = _wallet(HI, "BILL-EOA-CEX-20Mnonce", "distribution", 20_700_000)
        r = self._run([w], {HI: 20_699_000},
                      lambda a, c, ck, days=7, decimals=18: ([_out(HI, STALE)], "moralis", False))
        self.assertEqual(r["signal"], "NONCE-CHURN")              # NOT ESCALATION
        self.assertEqual(r["score"], 0)
        self.assertEqual(r["escalation_fired"], [])
        self.assertEqual(len(r["nonce_churn"]), 1)
        ch = r["nonce_churn"][0]
        self.assertFalse(ch["token_out_recent"])
        self.assertEqual(ch["last_token_out_ts"], STALE)         # stale ts surfaced for the orchestrator
        self.assertTrue(ch["high_nonce"])

    def test_high_nonce_fresh_tokenout_still_escalates(self):
        w = _wallet(HI, "BILL-AGGREGATOR", "team", 11_000_244)
        r = self._run([w], {HI: 11_000_000},
                      lambda a, c, ck, days=7, decimals=18: ([_out(HI, FRESH)], "moralis", False))
        self.assertEqual(r["signal"], "ESCALATION")              # token-out today → real distribution
        self.assertEqual(r["score"], -30)
        self.assertEqual(len(r["escalation_fired"]), 1)
        self.assertTrue(r["escalation_fired"][0]["token_out_recent"])

    def test_provider_down_is_unconfirmed_not_silent(self):
        def boom(a, c, ck, days=7, decimals=18):
            raise OC.MoralisError("all providers failed")
        w = _wallet(HI, "BILL-EOA-CEX-20Mnonce", "distribution", 20_700_000)
        r = self._run([w], {HI: 20_699_000}, boom)
        self.assertEqual(r["signal"], "ESCALATION-UNCONFIRMED")   # neither silently fired nor suppressed
        self.assertEqual(len(r["escalation_unconfirmed"]), 1)
        self.assertIsNone(r["escalation_unconfirmed"][0]["token_out_recent"])


class TestLowNonceUnaffected(_Base):
    def test_low_nonce_dormant_safe_escalates_on_nonce_alone(self):
        # a genuine dormant operator safe firing is the §8 early-warning — must NOT be gated out
        # (the cascade outruns the visible token-out). No false-negative.
        w = _wallet(LO, "MEGA-SAFE-1", "mega", 55)
        r = self._run([w], {LO: 50},
                      lambda a, c, ck, days=7, decimals=18: ([], "moralis", False))   # no out yet
        self.assertEqual(r["signal"], "ESCALATION")
        self.assertEqual(len(r["escalation_fired"]), 1)
        self.assertFalse(r["escalation_fired"][0]["high_nonce"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
