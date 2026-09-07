#!/usr/bin/env python3
"""SPEC-92 — 0x73d8bd54 + 0x6aba0315 are EXCHANGE/MM wallet infra (Binance proxy),
NOT operator escrow. Reclassifying them must:

  1. tag both as cex/exchange infra in known_entities (so a deposit TO them = a CEX
     deposit, the real §8 distribution signal — NOT staging-internal);
  2. re-tier every per-token occurrence off team/op/escrow/GOPLUS-TOP onto a cex tier
     (∈ OP_TIERS / noise, ∉ SEED_TIERS) so they never read as operator distribution,
     never seed-discover, never escalate;
  3. drop them from the vc_entities critical nonce_any monitor.

Run:  python3 tests/test_spec92_exchange_infra_reclassify.py
"""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


O = _load("onchain_cap92", "capabilities/onchain.py")
VW = _load("verify_wallet92", "capabilities/verify_wallet.py")

INFRA = ("0x73d8bd54f7cf5fab43fe4ef40a62d390644946db",
         "0x6aba0315493b7e6989041c91181337b662fb1b90")
CEX_TIERS = {"cex", "exchange", "cex-hot", "cex-execution"}
BANNED_TIERS = {"team", "op", "safe", "treasury", "mega", "mega-safe",
                "distribution", "passthrough", "unclassified"}


class TestKnownEntities(unittest.TestCase):
    def test_both_addresses_labelled_cex_infra(self):
        labels = O._entity_labels()
        for a in INFRA:
            self.assertIn(a, labels, f"{a} missing from entity labels")
            name = labels[a].lower()
            self.assertTrue(any(h in name for h in VW.CEX_HINTS),
                            f"{a} label {labels[a]!r} has no CEX hint → won't classify as cex")

    def test_classify_addr_returns_cex(self):
        labels = O._entity_labels()
        for a in INFRA:
            kind, _ = VW._classify_addr(a, {}, labels)  # not tracked here → label-driven
            self.assertEqual(kind, "cex", f"{a} should classify as cex via label, got {kind}")

    def test_no_escrow_label_anywhere_in_config(self):
        # the disproven "CROSS-CAT-A-ESCROW" / GOPLUS-TOP labels must be gone for these addrs
        for fn in ("known_entities.json", "vc_entities.json", "tracked_wallets.json"):
            raw = (ROOT / "config" / fn).read_text()
            data = json.loads(raw)
            self._scan(data, fn)

    def _scan(self, node, fn, ctx=None):
        # walk JSON; any object whose address is an INFRA addr must not carry an
        # escrow/operator label or tier
        if isinstance(node, dict):
            addr = (node.get("address") or "").lower()
            if addr in INFRA:
                lab = (node.get("label") or node.get("name") or "").upper()
                self.assertNotIn("ESCROW", lab, f"{fn}: {addr} still labelled ESCROW ({lab})")
                self.assertNotIn("GOPLUS-TOP", lab, f"{fn}: {addr} still labelled GOPLUS-TOP")
                tier = (node.get("tier") or "").lower()
                if tier:
                    self.assertIn(tier, CEX_TIERS,
                                  f"{fn}: {addr} tier {tier!r} not a cex tier")
                    self.assertNotIn(tier, BANNED_TIERS)
            for v in node.values():
                self._scan(v, fn)
        elif isinstance(node, list):
            for v in node:
                self._scan(v, fn)


class TestTrackedWalletTiers(unittest.TestCase):
    def test_every_occurrence_is_cex_tier_not_seed(self):
        d = json.loads((ROOT / "config" / "tracked_wallets.json").read_text())
        seen = 0
        for tok, info in d.get("tokens", {}).items():
            for w in info.get("wallets", []):
                if (w.get("address") or "").lower() in INFRA:
                    seen += 1
                    tier = (w.get("tier") or "").lower()
                    self.assertIn(tier, CEX_TIERS, f"{tok}: infra tier {tier!r} not cex")
                    self.assertNotIn(tier, VW.SEED_TIERS, f"{tok}: infra tier is a SEED tier")
                    self.assertIn(tier, O.OP_TIERS, f"{tok}: infra tier not in OP_TIERS (will escalate)")
        self.assertGreater(seen, 0, "expected the infra address tracked on several tokens")


class TestDepositReadsAsCex(unittest.TestCase):
    """Regression for the COLLECT 'staging-internal' bug: a team safe DEPOSITING TO
    0x73d8 is a real CEX deposit, not internal staging."""

    def _tracked_with_infra(self):
        d = json.loads((ROOT / "config" / "tracked_wallets.json").read_text())
        for tok, info in d.get("tokens", {}).items():
            tracked = {w["address"].lower(): {"label": w.get("label"),
                                              "tier": (w.get("tier") or "").lower()}
                       for w in info.get("wallets", [])}
            if any(a in tracked for a in INFRA):
                return tok, tracked
        return None, {}

    def test_dest_kind_is_cex_not_staging(self):
        tok, tracked = self._tracked_with_infra()
        self.assertIsNotNone(tok, "no token tracks the infra address")
        labels = O._entity_labels()
        infra = next(a for a in INFRA if a in tracked)
        kind, _ = VW._classify_addr(infra, tracked, labels)
        dest_kind = O.classify_destination(kind, tracked[infra]["tier"])
        self.assertEqual(dest_kind, "cex-execution",
                         f"deposit to infra on {tok} read as {dest_kind}, expected cex-execution")
        self.assertNotEqual(dest_kind, "staging-internal")


class TestNoFalseEscalation(unittest.TestCase):
    def setUp(self):
        self._orig = (O.build_snapshot, O._load_baseline, O._save_baseline)
        O._save_baseline = lambda *a, **k: None

    def tearDown(self):
        O.build_snapshot, O._load_baseline, O._save_baseline = self._orig

    def test_infra_fire_is_noise_not_escalation(self):
        import time
        addr = INFRA[0]
        wallets = [{"label": "BINANCE-WALLET-PROXY", "address": addr,
                    "chain": "binance-smart-chain", "tier": "cex", "nonce": 9_900_000,
                    "native_balance": 1.0, "fired": True, "rpc_ok": True},
                   {"label": "MEGA-SAFE", "address": "0xdead", "chain": "binance-smart-chain",
                    "tier": "distribution", "nonce": 0, "native_balance": 1.0,
                    "fired": False, "rpc_ok": True}]
        snap = {"ticker": "TST", "tracked": True, "n_wallets": 2, "fired_count": 1,
                "dormant_count": 1, "primed_unfired_count": 1, "wallets": wallets}
        O.build_snapshot = lambda t: snap
        O._load_baseline = lambda t: {"ts": time.time() - 60, "nonces": {addr: 9_800_000}}
        r = O.build_nonce_state("TST")
        # the infra proxy ticking its nonce is noise, never a §8 escalation
        self.assertNotEqual(r["signal"], "ESCALATION")
        fired = {f["address"]: f for f in r["newly_fired"]}
        self.assertIn(addr, fired)
        self.assertEqual(fired[addr]["confidence"], "noise")
        self.assertEqual(r["escalation_fired"], [])


class TestVcEntitiesMonitor(unittest.TestCase):
    def test_infra_not_a_critical_nonce_any_monitor(self):
        vc = json.loads((ROOT / "config" / "vc_entities.json").read_text())
        ents = vc.get("entities", [])
        ents = ents if isinstance(ents, list) else list(ents.values())
        for e in ents:
            if not isinstance(e, dict):
                continue
            addrs = {(e.get("address") or "").lower()}
            for w in e.get("wallets", []) or []:
                if isinstance(w, dict):
                    addrs.add((w.get("address") or "").lower())
            if addrs & set(INFRA):
                self.assertNotEqual(e.get("alert_strategy"), "nonce_any",
                                    "infra proxy still a nonce_any monitor")
                self.assertNotEqual(e.get("_priority"), "critical",
                                    "infra proxy still a critical monitor")


if __name__ == "__main__":
    unittest.main(verbosity=2)
