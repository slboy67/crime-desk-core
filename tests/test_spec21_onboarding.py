#!/usr/bin/env python3
"""SPEC 21 — onboard the reported multi-chain operator wallets into tracked config.

The user observed live distribution on wallets the desk read as DORMANT (multi-chain
blindspot). Beyond the multi-chain fix, these specific operator wallets must be tracked
with the correct chain so they're never read as independent/dormant.

SPEC 68: these tests assert *structural* invariants (present, valid address, known chain,
tier in the allowed enum, no duplicates) — NOT the specific tier of a named wallet. Wallet
tiers are routine desk judgment (e.g. ESPORTS CEX-DEPOSIT-SECONDARY-2 was deliberately
re-tiered distribution->op on 2026-06-11). A test that pins a wallet's tier turns every
re-tier into a suite failure; tier *semantics* are covered by test_retier_resilience and the
seeded-staging fixtures in verify_wallet's own tests.

Run:  python3 tests/test_spec21_onboarding.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import onboarding_invariants as OI  # noqa: E402

CFG = OI.load_tokens()


def _find(token, addr):
    return OI.find_wallet(CFG[token], addr)


class TestSpec21Onboarding(unittest.TestCase):
    def test_esports_cex_deposit_secondaries(self):
        # Onboarded per the ticket; on-chain verify (§8) showed they're active on ETHEREUM
        # (BSC nonce 0 / no ESPORTS-token movement) — chain tagged accordingly.
        for addr in ("0xc2F8C63d6D7c8C6eD2FEB92DBb1e8119193524C6",
                     "0x2a500f79c651759F12e67FeDb7748eF2449590CF"):
            w = _find("ESPORTS", addr)
            self.assertIsNotNone(w, f"ESPORTS secondary {addr} not onboarded")
            OI.assert_wallet_structural(self, w, expected_chain="ethereum")

    def test_eden_gate_deposit_wallet(self):
        w = _find("EDEN", "0xad11e97f5044db890a75a7d3e51eaa7099d7e7ff")
        self.assertIsNotNone(w, "EDEN Gate-deposit wallet missing")
        OI.assert_wallet_structural(self, w, expected_chain="ethereum")

    def test_play_dex_dumpers(self):
        for addr in ("0x24d0315bf3a0b9f176095bf2b57a848446af5fad",
                     "0x68a3067aaa379ad92bc2f1f9acd9e502245208fc"):
            w = _find("PLAY", addr)
            self.assertIsNotNone(w, f"PLAY dumper {addr} not onboarded")
            OI.assert_wallet_structural(self, w)

    def test_no_duplicate_addresses_within_token(self):
        for tk in ("ESPORTS", "EDEN", "PLAY"):
            OI.assert_unique_within_token(self, CFG[tk], tk)

    def test_onboarded_wallets_are_valid_hex(self):
        for tk, addr in (("ESPORTS", "0xc2F8C63d6D7c8C6eD2FEB92DBb1e8119193524C6"),
                         ("ESPORTS", "0x2a500f79c651759F12e67FeDb7748eF2449590CF"),
                         ("PLAY", "0x24d0315bf3a0b9f176095bf2b57a848446af5fad"),
                         ("PLAY", "0x68a3067aaa379ad92bc2f1f9acd9e502245208fc")):
            w = _find(tk, addr)
            self.assertTrue(OI.is_evm_address(w["address"]), f"{addr} not a valid EVM address")


if __name__ == "__main__":
    unittest.main(verbosity=2)
