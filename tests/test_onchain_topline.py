#!/usr/bin/env python3
"""SPEC 55 — the onchain/brief TOPLINE must not ESCALATE on cex-tier or noise fires,
and must not read an untiered candidate batch as a §8 distribution event.

LIVE PROOF this guards: an ESPORTS brief showed `signal: ESCALATION / bias:
"DISTRIBUTION FIRING (mega-safe nonce ticked — bid-pull/top)"` when the ONLY fired
wallet was KRAKEN-HOT (tier `cex`, confidence `noise`, operational). The per-wallet
tiering (SPEC 24) was right; the topline aggregation ignored it. Three same-family
cases: UAI baseline-settling read as ESCALATION, PLAY 2-fired-then-DORMANT between
reads (brief vs onchain drift), ESPORTS cex-noise driving a Stage-5 bias line.

Rules under test:
  - a fire with tier cex / confidence noise / operational still appears in
    `newly_fired` (visibility) but never sets the headline → topline DORMANT/QUIET;
  - a batch where ALL fires are tier `unclassified` (untiered candidates) →
    `BASELINE_SETTLING`, not ESCALATION (the Designer's tiering pass is the gate);
  - a MIXED read (1 real distribution-tier fire + 1 cex fire) → ESCALATION whose
    bias names ONLY the real wallet, never the cex one;
  - `brief` and a direct `onchain` read of the same nonce-state agree on the signal.

Deterministic: live snapshot + baseline I/O + token-out probe are monkeypatched.
"""
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Canonical imports so `brief` and `onchain` share ONE onchain module instance —
# brief does `from onchain import build_onchain`, so patching O.build_nonce_state must
# be the same module brief reaches (the SPEC-55 agreement test depends on this).
sys.path.insert(0, str(ROOT / "capabilities"))
import onchain as O   # noqa: E402
import brief as B     # noqa: E402


def W(label, tier, nonce, bal=0.0, rpc_ok=True):
    return {"label": label, "address": "0x" + label.lower(), "chain": "binance-smart-chain",
            "tier": tier, "nonce": nonce, "native_balance": bal,
            "fired": (nonce is not None and nonce > 0), "rpc_ok": rpc_ok}


def make_snap(wallets, tracked=True):
    ok = [w for w in wallets if w.get("rpc_ok")]
    fired = [w for w in ok if w["fired"]]
    dormant = [w for w in ok if not w["fired"]]
    primed = [w for w in dormant if (w.get("native_balance") or 0) > 0]
    return {"ticker": "TST", "tracked": tracked, "n_wallets": len(wallets),
            "fired_count": len(fired), "dormant_count": len(dormant),
            "primed_unfired_count": len(primed), "wallets": wallets}


class ToplineGate(unittest.TestCase):
    def setUp(self):
        self._orig = (O.build_snapshot, O._load_baseline, O._save_baseline, O._last_token_out)
        O._save_baseline = lambda *a, **k: None
        # low-nonce confirmed path probes the token-out: keep it offline + "recent"
        O._last_token_out = lambda addr, contract, ck, decimals=18, days=7: (
            "2026-06-11T00:00:00Z", None, True)

    def tearDown(self):
        O.build_snapshot, O._load_baseline, O._save_baseline, O._last_token_out = self._orig

    def _run(self, wallets, baseline, tracked=True):
        if baseline is not None and "ts" not in baseline:
            baseline = {**baseline, "ts": time.time() - 60}
        O.build_snapshot = lambda t: make_snap(wallets, tracked=tracked)
        O._load_baseline = lambda t: baseline
        return O.build_nonce_state("TST")

    # ── DoD 1: only a cex/noise fire → topline stays DORMANT/QUIET, still visible ──
    def test_cex_noise_only_fire_stays_quiet(self):
        r = self._run([W("KRAKEN-HOT", "cex", 5571, bal=1.0)],
                      baseline={"nonces": {"0xkraken-hot": 5564}})
        self.assertIn(r["signal"], ("DORMANT", "QUIET"))
        self.assertNotEqual(r["signal"], "ESCALATION")
        # the fire is still LISTED for visibility, tagged noise/operational
        labels = [f["label"] for f in r["newly_fired"]]
        self.assertIn("KRAKEN-HOT", labels)
        f = next(f for f in r["newly_fired"] if f["label"] == "KRAKEN-HOT")
        self.assertEqual(f["confidence"], "noise")
        self.assertTrue(f["operational"])
        self.assertEqual(r["escalation_fired"], [])

    # ── DoD 2: all-unclassified batch → BASELINE_SETTLING (not ESCALATION) ──
    def test_all_unclassified_batch_is_baseline_settling(self):
        r = self._run([W("CAND-A", "unclassified", 3), W("CAND-B", "unclassified", 7)],
                      baseline={"nonces": {"0xcand-a": 1, "0xcand-b": 4}})
        self.assertEqual(r["signal"], "BASELINE_SETTLING")
        self.assertEqual(r["score"], 0)
        self.assertEqual(r["escalation_fired"], [])
        self.assertEqual({f["label"] for f in r["newly_fired"]}, {"CAND-A", "CAND-B"})

    # ── DoD 3: mixed real distribution + cex → ESCALATION names ONLY the real one ──
    def test_mixed_real_and_cex_escalates_naming_real_only(self):
        wallets = [W("MEGA-SAFE", "distribution", 1),      # real, low-nonce → confirmed
                   W("KRAKEN-HOT", "cex", 5571, bal=1.0)]  # cex noise
        full = self._run_full(wallets, baseline={"nonces": {"0xmega-safe": 0, "0xkraken-hot": 5564}})
        self.assertEqual(full["signal"], "ESCALATION")
        self.assertIn("MEGA-SAFE", full["bias"])
        self.assertNotIn("KRAKEN-HOT", full["bias"])
        named = [f["label"] for f in full["nonces"]["escalation_fired"]]
        self.assertEqual(named, ["MEGA-SAFE"])

    def _run_full(self, wallets, baseline):
        """build_onchain over a mocked nonce-state + stubbed heavy layers."""
        if baseline is not None and "ts" not in baseline:
            baseline = {**baseline, "ts": time.time() - 60}
        O.build_snapshot = lambda t: make_snap(wallets, tracked=True)
        O._load_baseline = lambda t: baseline
        self._orig_heavy = (O._vc_overlap, O._safe_history_and_flows, O._concentration)
        O._vc_overlap = lambda t: {"available": False}
        O._safe_history_and_flows = lambda t, **k: ({"available": False}, {"available": False})
        O._concentration = lambda t: {"available": False}
        try:
            return O.build_onchain("TST")
        finally:
            O._vc_overlap, O._safe_history_and_flows, O._concentration = self._orig_heavy


class BriefOnchainAgreement(unittest.TestCase):
    """brief RE-DERIVES NOTHING — it must show the same topline as a direct onchain read."""

    def setUp(self):
        self._orig = (O.build_nonce_state, O._vc_overlap, O._safe_history_and_flows, O._concentration)
        # one fixed nonce-state both readers see → any divergence is aggregation/cache drift
        self.state = {
            "tracked": True, "signal": "ESCALATION", "score": -30, "ms": 5,
            "baseline_seeded": True, "fired_count": 1, "primed_unfired_count": 0,
            "dormant_count": 0, "n_wallets": 2,
            "newly_fired": [{"label": "MEGA-SAFE", "tier": "distribution", "confidence": "high",
                             "operational": False, "dest_kind": "cex-execution"}],
            "escalation_fired": [{"label": "MEGA-SAFE", "tier": "distribution",
                                  "dest_kind": "cex-execution"}],
            "nonce_churn": [], "escalation_unconfirmed": [],
            "escalation_kind": "execution", "execution_escalation": [{"label": "MEGA-SAFE"}],
            "staging_escalation": [], "wallets": [],
        }
        O.build_nonce_state = lambda t, *a, **k: dict(self.state)
        O._vc_overlap = lambda t: {"available": False}
        O._safe_history_and_flows = lambda t, **k: ({"available": False}, {"available": False})
        O._concentration = lambda t: {"available": False}

    def tearDown(self):
        O.build_nonce_state, O._vc_overlap, O._safe_history_and_flows, O._concentration = self._orig

    def test_brief_and_onchain_agree(self):
        direct = O.build_onchain("TST")
        brief_layer = B._onchain_layer("TST")
        self.assertEqual(brief_layer["signal"], direct["signal"])
        self.assertEqual(brief_layer["bias"], direct["bias"])
        self.assertEqual(brief_layer["score"], direct["score"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
