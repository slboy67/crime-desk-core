#!/usr/bin/env python3
"""SPEC-94 — 0x238a3588 is a shared PancakeSwap-Infinity DEX router/hook (plumbing),
NOT an operator. Reclassifying it must:

  1. re-tier every per-token occurrence off op/operator onto a DEX-router tier
     (∈ OP_TIERS / noise, ∉ SEED_TIERS) so its throughput NEVER reads as operator
     distribution, never seed-discovers, never escalates;
  2. label it DEX-router infra in known_entities (so an untracked-context read
     classifies it as `dex`, not an operator entity);
  3. resolve a flow TO it as a routing pass-through (`dex-router`), NOT
     staging-internal (operator loading) and NOT cex/operator distribution.

0xffa8 is a CONFIRMED real CEX-execution channel (EOA → Bitget 87%) — it must be
LEFT UNCHANGED in behavior (still surfaces its outbounds as distribution); only a
provenance `_note` is added.

Run:  python3 tests/test_spec94_dex_router_reclassify.py
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


O = _load("onchain_cap94", "capabilities/onchain.py")
VW = _load("verify_wallet94", "capabilities/verify_wallet.py")

ROUTER = "0x238a358808379702088667322f80ac48bad5e6c4"
FFA8 = "0xffa8db7b38579e6a2d14f9b347a9ace4d044cd54"
# noise tiers a DEX-router may carry (∈ OP_TIERS, ∉ SEED_TIERS)
ROUTER_TIERS = {"dex", "router"}
# tiers that would (wrongly) make it an operator/distribution signal
BANNED_TIERS = {"team", "op", "safe", "treasury", "mega", "mega-safe",
                "distribution", "passthrough", "staking_lock", "unclassified"}


class TestClassifyDestination(unittest.TestCase):
    """A flow to a DEX-router-tier tracked contract is a routing pass-through, NOT
    operator staging (the misread that tier `op` produced: staging-internal)."""

    def test_dex_router_tier_is_passthrough_not_staging(self):
        for tier in ("dex", "router"):
            dk = O.classify_destination("tracked-safe", tier)
            self.assertEqual(dk, "dex-router",
                             f"tracked-safe/{tier} → {dk}, expected dex-router pass-through")
            self.assertNotEqual(dk, "staging-internal")
            self.assertNotEqual(dk, "cex-execution")

    def test_dex_router_kind_is_passthrough(self):
        self.assertEqual(O.classify_destination("dex-router", None), "dex-router")

    def test_dex_router_never_counts_as_escalation_or_staging(self):
        # the board's §8 severity buckets must ignore a pass-through router flow
        dk = O.classify_destination("tracked-safe", "dex")
        self.assertNotIn(dk, ("cex-execution", "staging-internal"))


class TestTrackedWalletTiers(unittest.TestCase):
    def test_every_router_occurrence_is_noise_not_seed(self):
        d = json.loads((ROOT / "config" / "tracked_wallets.json").read_text())
        seen = 0
        for tok, info in d.get("tokens", {}).items():
            for w in info.get("wallets", []):
                if (w.get("address") or "").lower() == ROUTER:
                    seen += 1
                    tier = (w.get("tier") or "").lower()
                    self.assertIn(tier, ROUTER_TIERS, f"{tok}: router tier {tier!r} not a dex tier")
                    self.assertNotIn(tier, VW.SEED_TIERS, f"{tok}: router tier is a SEED tier")
                    self.assertIn(tier, O.OP_TIERS, f"{tok}: router tier not in OP_TIERS (will escalate)")
                    self.assertNotIn(tier, BANNED_TIERS)
                    lab = (w.get("label") or "").upper()
                    self.assertNotIn("MULTI-CAT-A-ROUTER", lab,
                                     f"{tok}: router still carries the operator label {lab}")
                    self.assertNotIn("GOPLUS-TOP", lab, f"{tok}: router still labelled GOPLUS-TOP")
        self.assertGreater(seen, 0, "expected the router tracked on several tokens")


class TestKnownEntities(unittest.TestCase):
    def test_router_labelled_dex(self):
        labels = O._entity_labels()
        self.assertIn(ROUTER, labels, f"{ROUTER} missing from entity labels")
        name = labels[ROUTER].lower()
        self.assertTrue(any(h in name for h in VW.DEX_HINTS),
                        f"{ROUTER} label {labels[ROUTER]!r} has no DEX hint → won't classify as dex")

    def test_classify_addr_returns_dex(self):
        labels = O._entity_labels()
        kind, _ = VW._classify_addr(ROUTER, {}, labels)  # not tracked here → label-driven
        self.assertEqual(kind, "dex", f"{ROUTER} should classify as dex via label, got {kind}")


class TestFlowToRouterIsPassThrough(unittest.TestCase):
    """Regression for the misread: an OUT to 0x238a is DEX-router plumbing, not
    operator staging/distribution."""

    def _tracked_with_router(self):
        d = json.loads((ROOT / "config" / "tracked_wallets.json").read_text())
        for tok, info in d.get("tokens", {}).items():
            tracked = {w["address"].lower(): {"label": w.get("label"),
                                              "tier": (w.get("tier") or "").lower()}
                       for w in info.get("wallets", [])}
            if ROUTER in tracked:
                return tok, tracked
        return None, {}

    def test_dest_kind_is_dex_router_not_staging(self):
        tok, tracked = self._tracked_with_router()
        self.assertIsNotNone(tok, "no token tracks the router address")
        labels = O._entity_labels()
        kind, _ = VW._classify_addr(ROUTER, tracked, labels)
        dest_kind = O.classify_destination(kind, tracked[ROUTER]["tier"])
        self.assertEqual(dest_kind, "dex-router",
                         f"flow to router on {tok} read as {dest_kind}, expected dex-router")
        self.assertNotEqual(dest_kind, "staging-internal")


class TestNoFalseEscalation(unittest.TestCase):
    def setUp(self):
        self._orig = (O.build_snapshot, O._load_baseline, O._save_baseline)
        O._save_baseline = lambda *a, **k: None

    def tearDown(self):
        O.build_snapshot, O._load_baseline, O._save_baseline = self._orig

    def test_router_fire_is_noise_not_escalation(self):
        import time
        wallets = [{"label": "DEX-ROUTER-PANCAKE-INFINITY", "address": ROUTER,
                    "chain": "binance-smart-chain", "tier": "dex", "nonce": 260,
                    "native_balance": 1.0, "fired": True, "rpc_ok": True},
                   {"label": "MEGA-SAFE", "address": "0xdead", "chain": "binance-smart-chain",
                    "tier": "distribution", "nonce": 0, "native_balance": 1.0,
                    "fired": False, "rpc_ok": True}]
        snap = {"ticker": "TST", "tracked": True, "n_wallets": 2, "fired_count": 1,
                "dormant_count": 1, "primed_unfired_count": 1, "wallets": wallets}
        O.build_snapshot = lambda t: snap
        O._load_baseline = lambda t: {"ts": time.time() - 60, "nonces": {ROUTER: 240}}
        r = O.build_nonce_state("TST")
        self.assertNotEqual(r["signal"], "ESCALATION")
        fired = {f["address"]: f for f in r["newly_fired"]}
        self.assertIn(ROUTER, fired)
        self.assertEqual(fired[ROUTER]["confidence"], "noise")
        self.assertEqual(r["escalation_fired"], [])


class TestFfa8Unchanged(unittest.TestCase):
    """0xffa8 is a confirmed CEX-execution channel — behavior preserved, only a note added."""

    def _ffa8_entries(self):
        d = json.loads((ROOT / "config" / "tracked_wallets.json").read_text())
        out = []
        for tok, info in d.get("tokens", {}).items():
            for w in info.get("wallets", []):
                if (w.get("address") or "").lower() == FFA8:
                    out.append((tok, w))
        return out

    def test_ffa8_not_reclassified_as_infra_and_still_distribution(self):
        entries = self._ffa8_entries()
        self.assertGreater(len(entries), 0)
        tiers = {(w.get("tier") or "").lower() for _, w in entries}
        # must NOT be downgraded to dex/router noise infra
        self.assertNotIn("dex", tiers)
        self.assertNotIn("router", tiers)
        # its distribution-tier occurrences survive (surfaces outbounds as distribution)
        self.assertIn("distribution", tiers)

    def test_ffa8_has_spec94_probe_note(self):
        entries = self._ffa8_entries()
        with_note = [w for _, w in entries if "spec94" in json.dumps(w).lower()
                     or "spec-94" in json.dumps(w).lower()]
        self.assertTrue(with_note, "expected a SPEC-94 probe note on 0xffa8 entries")


if __name__ == "__main__":
    unittest.main(verbosity=2)
