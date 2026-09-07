#!/usr/bin/env python3
"""SPEC 58 — multi-chain verify polish + honest balance semantics.

Three live misses from the FOLKS workup, pinned offline:
  1. verify_wallet chain-pick: an ETH-holding EOA was read as `avalanche` (stray bridge
     transfers outranked the chain it actually HOLDS on). Fix: prefer the chain where
     balanceOf > 0, fall back to transfer-count.
  2. `token_balance` was net-flow-in-window mislabeled as a balance. Rename to
     `net_flow_window`; add a real `balance_now` (cross-checked balanceOf, §3).
  3. `onchain` FOLKS must state the supply it CANNOT see — `unsupported_chains` with
     algorand + its supply share — even when the GoPlus concentration read is n/a.

Offline: token_transfers / _live_price / balance_of are monkeypatched on verify_wallet;
the onchain composition layers are stubbed. FOLKS is the multi-chain fixture shape
(eth/avalanche EVM + algorand non-EVM) — TestUnsupportedChainsSurface pins it to an
isolated tracked_wallets.json fixture rather than the real tracked config (SPEC-192:
that config is live-mutable and drifted once already).

Run:  python3 tests/test_verify_multichain_polish.py
"""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_vspec = importlib.util.spec_from_file_location("verify_wallet", ROOT / "capabilities" / "verify_wallet.py")
VW = importlib.util.module_from_spec(_vspec)
_vspec.loader.exec_module(VW)

_ospec = importlib.util.spec_from_file_location("onchain_cap", ROOT / "capabilities" / "onchain.py")
O = importlib.util.module_from_spec(_ospec)
_ospec.loader.exec_module(O)

ADDR = "0xfeed000000000000000000000000000000000058"
SRC = "0xc0ffee0000000000000000000000000000000058"
SINK = "0x51c0000000000000000000000000000000000058"
NOW = datetime.now(timezone.utc)
ETH, AVAX, BSC, BASE = "ethereum", "avalanche", "binance-smart-chain", "base"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def tx(frm, to, val, dt):
    return {"from_address": frm, "to_address": to, "value_decimal": str(val),
            "block_timestamp": _iso(dt), "token_symbol": "FOLKS"}


class _VerifyPatch(unittest.TestCase):
    def patch(self, per_chain, balances=None, price=0.10, fail=()):
        """per_chain: {chain_key: [txs]}; balances: {chain_key: balance_now-dict}."""
        self._orig = (VW.token_transfers, VW._live_price, VW.balance_of)
        balances = balances or {}

        def tt(address, contract, chain_key, days=180, decimals=18):
            if chain_key in fail:
                raise VW.MoralisError(f"provider down on {chain_key}")
            return (per_chain.get(chain_key, []), "moralis", False)

        def bof(address, contract, chain_key, decimals=18):
            return balances.get(chain_key, {"available": False, "chain": chain_key,
                                            "reason": f"no balance on {chain_key}"})

        VW.token_transfers = tt
        VW._live_price = lambda token: (price, "bybit")
        VW.balance_of = bof

    def tearDown(self):
        if hasattr(self, "_orig"):
            VW.token_transfers, VW._live_price, VW.balance_of = self._orig


def _bal(chain, value, pct, decimals=6):
    return {"available": True, "chain": chain, "value": value, "pct_supply": pct,
            "total_supply": (value / pct * 100) if pct else None,
            "cross_checked": True, "agree": True, "sources": ["rpc-a", "rpc-b"]}


class TestChainPick(_VerifyPatch):
    def test_eth_holder_reads_ethereum_without_chain_arg(self):
        # avalanche carries MORE transfers (stray bridge) than ethereum -> old code mispicked
        # avalanche. The EOA HOLDS on ethereum (balanceOf>0 there) -> must read ethereum.
        self.patch(
            per_chain={
                ETH: [tx(SRC, ADDR, 5000, NOW - timedelta(days=20))],
                AVAX: [tx(SRC, ADDR, 10, NOW - timedelta(days=5)),
                       tx(ADDR, SINK, 10, NOW - timedelta(days=4))],
            },
            balances={ETH: _bal(ETH, 5000.0, 0.33)},
        )
        v = VW.build_verify(ADDR, "FOLKS")
        self.assertTrue(v["available"])
        self.assertEqual(v["chain"], ETH)                      # the whole point of SPEC 58
        self.assertEqual(v["balance_now"]["chain"], ETH)
        self.assertIn(ETH, v["holds_on"])
        self.assertIn(AVAX, v["balance_chains_checked"])       # report which chains were checked

    def test_falls_back_to_transfer_count_when_no_balance(self):
        # no balanceOf anywhere -> keep the legacy most-active-chain pick, balance_now honest
        self.patch(per_chain={
            ETH: [tx(SRC, ADDR, 10, NOW - timedelta(days=5))],
            AVAX: [tx(SRC, ADDR, 10, NOW - timedelta(days=5)),
                   tx(SRC, ADDR, 10, NOW - timedelta(days=4)),
                   tx(ADDR, SINK, 5, NOW - timedelta(days=1))],
        })
        v = VW.build_verify(ADDR, "FOLKS")
        self.assertTrue(v["available"])
        self.assertEqual(v["chain"], AVAX)                     # most-active fallback
        self.assertFalse(v["balance_now"]["available"])        # no fabricated 0 (§3)
        self.assertEqual(v["holds_on"], [])


class TestBalanceSemantics(_VerifyPatch):
    def test_balance_now_matches_goplus_pct(self):
        # GoPlus shows this EOA holding 0.33% — balance_now.pct_supply must match within tol.
        self.patch(per_chain={ETH: [tx(SRC, ADDR, 5000, NOW - timedelta(days=20))]},
                   balances={ETH: _bal(ETH, 5000.0, 0.33)})
        v = VW.build_verify(ADDR, "FOLKS")
        self.assertTrue(v["balance_now"]["available"])
        self.assertAlmostEqual(v["balance_now"]["pct_supply"], 0.33, places=2)
        # priced USD on the live perp price
        self.assertAlmostEqual(v["balance_now"]["value_usd"], 5000.0 * 0.10, places=2)

    def test_net_flow_window_renamed_and_distinct_from_balance(self):
        # in 5000, out 1000 -> net_flow_window 4000 (NOT the balance, which is 5000 held)
        self.patch(
            per_chain={ETH: [tx(SRC, ADDR, 5000, NOW - timedelta(days=20)),
                             tx(ADDR, SINK, 1000, NOW - timedelta(days=2))]},
            balances={ETH: _bal(ETH, 4000.0, 0.26)},
        )
        v = VW.build_verify(ADDR, "FOLKS")
        self.assertIn("net_flow_window", v)
        self.assertIn("net_flow_window_usd", v)
        self.assertNotIn("token_balance", v)                   # the mislabeled name is gone
        self.assertNotIn("token_balance_usd", v)
        self.assertAlmostEqual(v["net_flow_window"], 4000.0, places=2)
        self.assertAlmostEqual(v["balance_now"]["value"], 4000.0, places=2)


class TestBalanceOfCrossCheck(unittest.TestCase):
    """onchain.balance_of: cross-checked balanceOf + totalSupply via free-RPC (§3)."""

    def setUp(self):
        self._orig = O._rpc_at

    def tearDown(self):
        O._rpc_at = self._orig

    def test_cross_checked_two_providers_agree(self):
        # FOLKS 6-dec: balanceOf 5000e6, totalSupply 1_500_000e6 -> 0.3333% supply
        bal_hex = hex(5000 * 10**6)
        sup_hex = hex(1_500_000 * 10**6)

        def fake_rpc_at(url, method, params, timeout=12):
            data = params[0]["data"]
            return bal_hex if data.startswith(O.BALANCEOF_SELECTOR) else sup_hex

        O._rpc_at = fake_rpc_at
        # FOLKS eth contract, ethereum pool exists in _PUBLIC_RPCS (>= 2 providers -> cross-checked)
        contract = "0xff7f8f301f7a706e3cfd3d2275f5dc0b9ee8009b"
        r = O.balance_of(ADDR, contract, ETH, decimals=6)
        self.assertTrue(r["available"])
        self.assertAlmostEqual(r["value"], 5000.0, places=2)
        self.assertAlmostEqual(r["pct_supply"], 0.3333, places=3)
        self.assertTrue(r["cross_checked"])
        self.assertTrue(r["agree"])

    def test_unreadable_chain_degrades_not_zero(self):
        O._rpc_at = lambda url, method, params, timeout=12: None
        r = O.balance_of(ADDR, "0xff7f8f301f7a706e3cfd3d2275f5dc0b9ee8009b", ETH, decimals=6)
        self.assertFalse(r["available"])
        self.assertNotIn("value", r)                           # no fabricated 0


class TestUnsupportedChainsSurface(unittest.TestCase):
    """build_onchain FOLKS: state the unseen supply even when concentration is n/a.

    SPEC-192: this used to read the REAL config/tracked_wallets.json FOLKS row directly
    (the file's own docstring called that deliberate — "the multi-chain fixture in
    config"). That coupled the test to live-mutable data: SPEC-192's chain-map
    extension made `monad` GoPlus-supported, and refreshing FOLKS's ranking against
    current live GoPlus/CoinGecko data changed its supply_pct/unreadable_supply_pct
    numbers (94.66% -> ~0%, since more of the supply is now accounted for on-chain) —
    the exact kind of drift a production-config-backed fixture will always be fragile
    to on the next refresh, re-onboard, or organic data change. Pinned to an isolated
    tracked_wallets.json fixture (frozen at the pre-SPEC-192 snapshot) instead, so the
    MECHANISM under test (the sole native-unsupported chain absorbs the unreadable
    remainder) stays pinned regardless of what real FOLKS data does going forward."""

    def setUp(self):
        self._orig = (O.build_nonce_state, O._vc_overlap, O._safe_history_and_flows,
                      O._concentration, O.WALLETS)
        O.build_nonce_state = lambda t, persist=False: {
            "ticker": t, "tracked": True, "signal": "QUIET", "score": 0, "ms": 1,
            "fired_count": 0, "primed_unfired_count": 0, "dormant_count": 0}
        O._vc_overlap = lambda t: {"available": False}
        O._safe_history_and_flows = lambda t: ({"available": False}, {"available": False})
        O._concentration = lambda t: {"available": False, "reason": "mock n/a (the FOLKS live miss)"}
        self._tmp = tempfile.TemporaryDirectory()
        fixture_path = Path(self._tmp.name) / "tracked_wallets.json"
        fixture_path.write_text(json.dumps({"tokens": {"FOLKS": {
            "name": "Folks Finance", "decimals": 6, "cg_id": "folks-finance",
            "contracts": {"ethereum": "0xff7f8f301f7a706e3cfd3d2275f5dc0b9ee8009b",
                         "avalanche": "0xaaa0000000000000000000000000000000000001",
                         "base": "0xbbb0000000000000000000000000000000000002",
                         "polygon-pos": "0xccc0000000000000000000000000000000000003",
                         "binance-smart-chain": "0xddd0000000000000000000000000000000000004",
                         "arbitrum-one": "0xeee0000000000000000000000000000000000005",
                         "monad": "0xfff0000000000000000000000000000000000006",
                         "sei-v2": "0x1110000000000000000000000000000000000007",
                         "algorand": "3203964481"},
            "primary_chain": "ethereum",
            "chains_ranked": [
                {"chain": "ethereum", "goplus_chain": "ethereum", "supported": True,
                 "holder_count": 2104, "supply_pct": 2.88, "bridge_stub": False},
                {"chain": "avalanche", "goplus_chain": "avalanche", "supported": True,
                 "holder_count": 824, "supply_pct": 2.16, "bridge_stub": False},
                {"chain": "base", "goplus_chain": "base", "supported": True,
                 "holder_count": 644, "supply_pct": 0.2, "bridge_stub": True},
                {"chain": "polygon-pos", "goplus_chain": "polygon", "supported": True,
                 "holder_count": 66, "supply_pct": 0.1, "bridge_stub": True},
                {"chain": "binance-smart-chain", "goplus_chain": "binance-smart-chain",
                 "supported": True, "holder_count": 0, "supply_pct": 0.0, "bridge_stub": True},
                {"chain": "arbitrum-one", "goplus_chain": "arbitrum", "supported": True,
                 "holder_count": 120, "supply_pct": 0.0, "bridge_stub": True},
                {"chain": "monad", "supported": False, "supply_pct": None},
                {"chain": "sei-v2", "supported": False, "supply_pct": None},
                {"chain": "algorand", "supported": False, "supply_pct": None},
            ],
            "unreadable_supply_pct": 94.66,
            "wallets": [],
        }}}))
        O.WALLETS = fixture_path

    def tearDown(self):
        (O.build_nonce_state, O._vc_overlap, O._safe_history_and_flows,
         O._concentration, O.WALLETS) = self._orig
        self._tmp.cleanup()

    def test_unsupported_chains_with_algorand_supply_share(self):
        o = O.build_onchain("FOLKS")
        self.assertIn("unsupported_chains", o)
        chains = {u["chain"]: u for u in o["unsupported_chains"]}
        self.assertIn("algorand", chains)                      # the non-EVM native chain
        # the bulk of supply lives on algorand — the engine must state the share it cannot see
        self.assertIsNotNone(chains["algorand"]["supply_pct"])
        self.assertGreater(chains["algorand"]["supply_pct"], 50.0)
        self.assertAlmostEqual(o["unreadable_supply_pct"], 94.66, places=1)
        # surfaces despite concentration being n/a (the actual bug)
        self.assertFalse(o["concentration"]["available"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
