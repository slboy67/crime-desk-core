#!/usr/bin/env python3
"""SPEC 68 — a config-only re-tier of any production wallet must not fail the suite.

Wallet tiers are routine desk judgment (re-tiered 4x in two days). The onboarding tests must
validate wallet *structure*, never the live tier of a named production wallet. This locks that
in two ways:
  1. STATIC — no onboarding test contains an `assertEqual(<...tier...>, "<tier-literal>")`
     against a live-config wallet (the exact pattern that blocked SPEC-67's premerge).
  2. BEHAVIOURAL — flipping a production wallet's tier in config leaves the structural
     invariants intact, proving the converted assertions are tier-agnostic.

Run:  python3 tests/test_retier_resilience.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import onboarding_invariants as OI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# The onboarding tests that load live config/tracked_wallets.json and historically pinned tiers.
ONBOARDING_TESTS = (
    "test_spec21_onboarding.py",
    "test_siren_onboarding.py",
    "test_opn_onboarding.py",
    "test_velvet_onboarding.py",
    "test_chip_onboarding.py",
    "test_rave_onboarding.py",
)


class TestNoHardcodedProductionTiers(unittest.TestCase):
    def test_onboarding_tests_do_not_pin_live_wallet_tiers(self):
        for fname in ONBOARDING_TESTS:
            path = ROOT / "tests" / fname
            offenders = OI.live_tier_assertions(path)
            self.assertEqual(
                offenders, [],
                f"{fname} pins a live wallet tier to a literal (re-tier would break the suite):\n"
                + "\n".join(f"  L{ln}: {txt}" for ln, txt in offenders),
            )


class TestRetierIsHarmless(unittest.TestCase):
    def test_structural_invariants_survive_a_retier(self):
        # Take a real production wallet and re-tier it to every allowed value; the structural
        # invariants must hold regardless — that is exactly what makes a re-tier config-safe.
        tokens = OI.load_tokens()
        sample = None
        for tk, cfg in tokens.items():
            for w in cfg.get("wallets", []):
                if (w.get("chain") or "").lower() != "solana":
                    sample = dict(w)
                    break
            if sample:
                break
        self.assertIsNotNone(sample, "no EVM production wallet found")
        for tier in sorted(OI.ALLOWED_TIERS):
            retiered = dict(sample, tier=tier)
            OI.assert_wallet_structural(self, retiered)  # must not raise for any tier


if __name__ == "__main__":
    unittest.main(verbosity=2)
