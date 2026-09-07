#!/usr/bin/env python3
"""SPEC 21 — verify_wallet must be MULTI-CHAIN.

Operators distribute across ETH/BSC/Base; a single-chain read returns false DORMANT
for a wallet active on a non-default chain (e.g. EDEN's default-pick is BSC but every
tracked wallet is on ethereum). These tests pin: aggregate across all deployed chains,
accept an explicit `chain` arg (alias-normalized), degrade per-chain explicitly, and
never read a multi-chain-active wallet as DORMANT.

Offline: token_transfers is monkeypatched per chain_key. RAVE is the multi-chain fixture
(eth/bsc/base in config); ESPORTS is the single-chain control.

Run:  python3 tests/test_multichain_verify.py
"""
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("verify_wallet", ROOT / "capabilities" / "verify_wallet.py")
VW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(VW)

ADDR = "0xfeed000000000000000000000000000000000001"
SRC = "0xc0ffee0000000000000000000000000000000001"
SINK = "0x51c0000000000000000000000000000000000099"
NOW = datetime.now(timezone.utc)
ETH, BSC, BASE = "ethereum", "binance-smart-chain", "base"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def tx(frm, to, val, dt):
    return {"from_address": frm, "to_address": to, "value_decimal": str(val),
            "block_timestamp": _iso(dt), "token_symbol": "RAVE"}


class _Patch(unittest.TestCase):
    def patch(self, per_chain, price=0.05, fail=()):
        """per_chain: {chain_key: [txs]} (served as moralis/non-partial). fail: chains that raise."""
        self._orig = (VW.token_transfers, VW._live_price, VW.balance_of)
        VW.balance_of = lambda *a, **k: {"available": False}   # SPEC 58: no live RPC in tests

        def tt(address, contract, chain_key, days=180, decimals=18):
            if chain_key in fail:
                raise VW.MoralisError(f"provider down on {chain_key}")
            return (per_chain.get(chain_key, []), "moralis", False)
        VW.token_transfers = tt
        VW._live_price = lambda token: (price, "bybit")

    def tearDown(self):
        if hasattr(self, "_orig"):
            VW.token_transfers, VW._live_price, VW.balance_of = self._orig


class TestMultiChainVerify(_Patch):
    def test_aggregates_activity_on_nondefault_chain(self):
        # RAVE's old single-chain pick was BSC; the wallet is active ONLY on ethereum.
        # Old inbound + recent outbound = distributing (not recent accumulation).
        self.patch({ETH: [tx(SRC, ADDR, 5000, NOW - timedelta(days=40)),
                           tx(ADDR, SINK, 1000, NOW - timedelta(days=1))]})
        v = VW.build_verify(ADDR, "RAVE")
        self.assertTrue(v["available"])
        self.assertNotEqual(v["verdict"], "DORMANT")          # the whole point of SPEC 21
        self.assertEqual(v["verdict"], "DISTRIBUTING")
        self.assertGreaterEqual(v["out_count"], 1)
        self.assertAlmostEqual(v["net_flow_window"], 4000, places=2)
        self.assertIn(ETH, v["chains_queried"])
        self.assertIn(BSC, v["chains_queried"])
        self.assertIn(BASE, v["chains_queried"])

    def test_explicit_chain_arg_restricts_query(self):
        # eth = an outbound; bsc = a big inbound. chain="eth" must see ONLY the eth leg.
        self.patch({ETH: [tx(ADDR, SINK, 1000, NOW - timedelta(days=1))],
                    BSC: [tx(SRC, ADDR, 9999, NOW - timedelta(days=2))]})
        v = VW.build_verify(ADDR, "RAVE", chain="eth")
        self.assertEqual(v["chains_queried"], [ETH])
        self.assertEqual(v["in_count"], 0)
        self.assertEqual(v["out_count"], 1)
        self.assertAlmostEqual(v["net_flow_window"], -1000, places=2)

    def test_chain_alias_normalization(self):
        self.patch({BSC: [tx(SRC, ADDR, 100, NOW - timedelta(days=1))]})
        v = VW.build_verify(ADDR, "RAVE", chain="bsc")
        self.assertEqual(v["chains_queried"], [BSC])           # "bsc" -> binance-smart-chain
        self.assertTrue(v["available"])

    def test_chain_not_deployed_unavailable(self):
        # ESPORTS is BSC-only; asking for base must fail cleanly, not silently DORMANT.
        self.patch({})
        v = VW.build_verify(ADDR, "ESPORTS", chain="base")
        self.assertFalse(v["available"])
        self.assertIn("base", v["reason"].lower())

    def test_per_chain_status_and_primary_chain(self):
        self.patch({ETH: [tx(SRC, ADDR, 10, NOW - timedelta(days=3))],
                    BSC: [tx(SRC, ADDR, 10, NOW - timedelta(days=3)),
                          tx(SRC, ADDR, 10, NOW - timedelta(days=2)),
                          tx(ADDR, SINK, 5, NOW - timedelta(days=1))]})
        v = VW.build_verify(ADDR, "RAVE")
        self.assertTrue(v["chains"][ETH]["available"])
        self.assertTrue(v["chains"][BSC]["available"])
        self.assertEqual(v["chain"], BSC)                      # primary = most-active chain

    def test_partial_degrade_one_chain_fails(self):
        # eth provider down, bsc serves a distribution leg -> available, NOT dormant, flagged partial
        self.patch({BSC: [tx(SRC, ADDR, 5000, NOW - timedelta(days=40)),
                          tx(ADDR, SINK, 1000, NOW - timedelta(days=1))]}, fail=(ETH,))
        v = VW.build_verify(ADDR, "RAVE")
        self.assertTrue(v["available"])
        self.assertEqual(v["verdict"], "DISTRIBUTING")
        self.assertTrue(v["partial"])
        self.assertIn(ETH, v["degraded_chains"])
        self.assertFalse(v["chains"][ETH]["available"])

    def test_all_chains_fail_degrades_explicitly(self):
        self.patch({}, fail=(ETH, BSC, BASE))
        v = VW.build_verify(ADDR, "RAVE")
        self.assertFalse(v["available"])
        self.assertTrue(v["degraded"])
        self.assertNotIn("token_balance", v)                  # no fabricated 0 (SPEC-1b)
        self.assertNotIn("verdict", v)

    def test_recent_outbounds_carry_chain(self):
        self.patch({ETH: [tx(SRC, ADDR, 5000, NOW - timedelta(days=3)),
                          tx(ADDR, SINK, 1000, NOW - timedelta(days=1))]})
        v = VW.build_verify(ADDR, "RAVE")
        self.assertTrue(v["recent_outbounds"])
        self.assertEqual(v["recent_outbounds"][0]["chain"], ETH)

    def test_single_chain_token_backward_compatible(self):
        # ESPORTS (BSC-only): behaves exactly as the single-chain path did.
        self.patch({BSC: [tx(SRC, ADDR, 100, NOW - timedelta(days=40))]})
        v = VW.build_verify(ADDR, "ESPORTS")
        self.assertTrue(v["available"])
        self.assertEqual(v["chain"], BSC)
        self.assertEqual(v["chains_queried"], [BSC])
        self.assertEqual(v["verdict"], "INDEPENDENT-HOLDER")


if __name__ == "__main__":
    unittest.main(verbosity=2)
