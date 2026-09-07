#!/usr/bin/env python3
"""SPEC-109 — trace_tree.py: recursive hop tracer with CEX-termination verdict.

Run:  python3 tests/test_trace_tree.py

Reuses verify_wallet's build_verify (fully monkeypatched — offline/deterministic, same
pattern as tests/test_verify_wallet.py's _Patch). A synthetic per-address fixture stands
in for the wallet-scoped token-transfer fetch.
"""
import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, relpath):
    """Registers into sys.modules by `name` BEFORE exec — trace_tree.py does a plain
    `import verify_wallet as VW`, which must resolve to THIS (patchable) module instance,
    not a second freshly-executed copy."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


VW = _load("verify_wallet", "capabilities/verify_wallet.py")
TT = _load("trace_tree", "capabilities/trace_tree.py")

TOKEN = "ESPORTS"   # real tracked-config token (has a BSC contract) — network fully stubbed
NOW = datetime.now(timezone.utc)

ROOT_ADDR = "0x1000000000000000000000000000000000000a"
A1 = "0x1000000000000000000000000000000000000b"
A2 = "0x1000000000000000000000000000000000000c"
A3 = "0x1000000000000000000000000000000000000d"
CEX_ADDR = "0x2000000000000000000000000000000000000e"
ROUTER_ADDR = "0x2000000000000000000000000000000000000f"
BRIDGE_ADDR = "0x3000000000000000000000000000000000000a"
CEX_ADDR_2 = "0x2000000000000000000000000000000000000c"
CONTRACT_ADDR = "0x4000000000000000000000000000000000000b"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def tx(frm, to, val, dt=NOW):
    return {"from_address": frm, "to_address": to, "value_decimal": str(val),
            "block_timestamp": _iso(dt), "token_symbol": TOKEN}


class _Patch(unittest.TestCase):
    """Same monkeypatch shape as test_verify_wallet.py, but token_transfers is keyed by
    the QUERIED ADDRESS (a fixture dict), since trace_tree fans out across many wallets."""

    def patch(self, fixture, price=50.0, labels=None, bridges=None, chains=None,
             contract_addrs=None, fail_chains=None):
        self._orig = (VW.token_transfers, VW._live_price, VW._quote_transfers,
                      VW.balance_of, VW.probe_contract, VW._entity_labels, VW.queryable_chains,
                      TT._load_bridges)
        VW._quote_transfers = lambda *a, **k: {}
        contract_addrs = {a.lower() for a in (contract_addrs or set())}
        fail_chains = fail_chains or set()

        def _token_transfers(address, contract, chain_key, days=180, decimals=18):
            if chain_key in fail_chains:
                raise Exception(f"provider down on {chain_key}")
            entry = fixture.get(address.lower(), [])
            if isinstance(entry, dict):
                return entry.get(chain_key, []), "moralis", False
            return entry, "moralis", False

        VW.token_transfers = _token_transfers
        VW._live_price = lambda token: (price, "bybit")
        VW.balance_of = lambda *a, **k: {"available": False, "reason": "stubbed offline"}
        VW.probe_contract = lambda addr, *a, **k: (
            {"available": True, "is_contract": True, "is_pool": False}
            if addr.lower() in contract_addrs else
            {"available": True, "is_contract": False, "is_pool": False})
        VW._entity_labels = lambda: (labels or {})
        if chains is not None:
            def _qc(tok, chain=None):
                if chain:
                    ck = VW.CHAIN_ALIASES.get(chain.lower().strip(), chain.lower().strip())
                    return {ck: chains[ck]} if ck in chains else {}
                return dict(chains)
            VW.queryable_chains = _qc
        if bridges is not None:
            TT._load_bridges = lambda: {a.lower(): b for a, b in bridges.items()}
        # trace_tree imports these names directly from verify_wallet at module load time
        # via `import verify_wallet as VW`, so patching VW's attributes is visible to it.

    def tearDown(self):
        if hasattr(self, "_orig"):
            (VW.token_transfers, VW._live_price, VW._quote_transfers,
             VW.balance_of, VW.probe_contract, VW._entity_labels, VW.queryable_chains,
             TT._load_bridges) = self._orig


class TestCexTermination(_Patch):
    def test_cex_terminated_path_and_total(self):
        # root --100@$50=$5000--> A1 --80@$50=$4000--> CEX(labeled)
        fixture = {ROOT_ADDR: [tx(ROOT_ADDR, A1, 100)], A1: [tx(A1, CEX_ADDR, 80)]}
        self.patch(fixture, labels={CEX_ADDR: "Binance Hot Wallet 6"})
        r = TT.trace_tree(ROOT_ADDR, TOKEN, depth=3, min_usd=1000, days=30, max_fetches=25)
        self.assertTrue(r["available"])
        self.assertEqual(r["nodes"][CEX_ADDR]["terminal"], "cex")
        self.assertIsNone(r["nodes"][A1]["terminal"])   # internal node, was recursed
        self.assertTrue(r["cex_terminated"])
        self.assertTrue(r["verdict"].startswith("CEX_TERMINATED"))
        self.assertIn("4,000", r["verdict"].replace("4000", "4,000"))  # total across the branch
        branch = next(b for b in r["branches"] if b["path"][-1] == CEX_ADDR)
        self.assertEqual(branch["path"], [ROOT_ADDR, A1, CEX_ADDR])
        self.assertAlmostEqual(branch["amount_usd"], 4000.0, places=0)


class TestNoCexTermination(_Patch):
    def test_fresh_wallets_zero_outs_are_resting(self):
        fixture = {ROOT_ADDR: [tx(ROOT_ADDR, A1, 100)], A1: []}
        self.patch(fixture)
        r = TT.trace_tree(ROOT_ADDR, TOKEN, depth=3, min_usd=1000, days=30, max_fetches=25)
        self.assertFalse(r["cex_terminated"])
        self.assertEqual(r["nodes"][A1]["terminal"], "resting")
        self.assertTrue(r["verdict"].startswith("NO_CEX_TERMINATION"))
        self.assertIn("resting", r["verdict"])


class TestRouterTerminal(_Patch):
    def test_router_node_classified_dex_not_recursed(self):
        # ROUTER_ADDR has its own outs in the fixture — proves trace_tree never fetches them
        fixture = {ROOT_ADDR: [tx(ROOT_ADDR, ROUTER_ADDR, 100)],
                   ROUTER_ADDR: [tx(ROUTER_ADDR, A2, 999999)]}
        self.patch(fixture, labels={ROUTER_ADDR: "PancakeSwap V3 Router"})
        r = TT.trace_tree(ROOT_ADDR, TOKEN, depth=3, min_usd=1000, days=30, max_fetches=25)
        self.assertEqual(r["nodes"][ROUTER_ADDR]["terminal"], "dex")
        self.assertNotIn(A2, r["nodes"])           # never recursed through the router
        self.assertEqual(r["fetched"], 1)          # only root was fetched


class TestPruneAndBudget(_Patch):
    def test_edge_below_min_usd_pruned(self):
        # root -> A1 $5000 (kept), root -> A2 $10 (pruned, below --min-usd 1000)
        fixture = {ROOT_ADDR: [tx(ROOT_ADDR, A1, 100), tx(ROOT_ADDR, A2, 0.2)], A1: [], A2: []}
        self.patch(fixture)
        r = TT.trace_tree(ROOT_ADDR, TOKEN, depth=3, min_usd=1000, days=30, max_fetches=25)
        children = {e["child"] for e in r["edges"]}
        self.assertIn(A1, children)
        self.assertNotIn(A2, children)
        self.assertNotIn(A2, r["nodes"])

    def test_cap_exceeded_truncates_and_marks_partial(self):
        # root -> A1 -> A2 -> A3, each a real edge; max_fetches=2 caps after root+A1
        fixture = {ROOT_ADDR: [tx(ROOT_ADDR, A1, 100)], A1: [tx(A1, A2, 100)],
                   A2: [tx(A2, A3, 100)], A3: []}
        self.patch(fixture)
        r = TT.trace_tree(ROOT_ADDR, TOKEN, depth=5, min_usd=1000, days=30, max_fetches=2)
        self.assertEqual(r["fetched"], 2)
        self.assertTrue(r["truncated"])
        self.assertIn(A2, r["skipped"])
        self.assertEqual(r["nodes"][A2]["terminal"], "open")
        self.assertIn("OPEN_BRANCHES", r["verdict"])


class TestBridgeTerminal(_Patch):
    def test_bridge_leaf_never_recursed_and_sized(self):
        # root --80@$50=$4000--> BRIDGE (seeded bridge, its own outs must never be fetched)
        fixture = {ROOT_ADDR: [tx(ROOT_ADDR, BRIDGE_ADDR, 80)], BRIDGE_ADDR: [tx(BRIDGE_ADDR, A2, 999999)]}
        bridges = {BRIDGE_ADDR: {"name": "Wormhole Portal Token Bridge (BSC)", "trail_continues": ["eth"]}}
        self.patch(fixture, bridges=bridges)
        r = TT.trace_tree(ROOT_ADDR, TOKEN, depth=3, min_usd=1000, days=30, max_fetches=25)
        self.assertTrue(r["available"])
        self.assertEqual(r["nodes"][BRIDGE_ADDR]["terminal"], "bridge")
        self.assertEqual(r["nodes"][BRIDGE_ADDR]["trail_continues"], ["eth"])
        self.assertNotIn(A2, r["nodes"])                # never recursed through the bridge
        self.assertEqual(r["fetched"], 1)                # only root fetched
        self.assertTrue(r["bridge_terminated"])
        self.assertAlmostEqual(r["terminated"]["bridge"]["usd"], 4000.0, places=0)
        self.assertIn("BRIDGE_TERMINATED", r["verdict"])
        self.assertIn("4,000", r["verdict"])

    def test_xpin_shaped_fresh_wallet_tree_with_one_bridge_hop(self):
        # A1 fresh/quiet (re-staging), A2 bridges out — must NOT read as plain "all quiet"
        fixture = {ROOT_ADDR: [tx(ROOT_ADDR, A1, 50), tx(ROOT_ADDR, A2, 30)],
                   A1: [], A2: [tx(A2, BRIDGE_ADDR, 25)]}
        bridges = {BRIDGE_ADDR: {"name": "Wormhole Portal Token Bridge (BSC)", "trail_continues": ["eth"]}}
        self.patch(fixture, bridges=bridges)
        r = TT.trace_tree(ROOT_ADDR, TOKEN, depth=3, min_usd=100, days=30, max_fetches=25)
        self.assertFalse(r["cex_terminated"])
        self.assertTrue(r["bridge_terminated"])
        self.assertEqual(r["nodes"][A1]["terminal"], "resting")
        self.assertEqual(r["nodes"][BRIDGE_ADDR]["terminal"], "bridge")
        self.assertIn("BRIDGE_TERMINATED", r["verdict"])
        # distinct from the plain all-quiet re-staging verdict (no bridge mention there)
        quiet_fixture = {ROOT_ADDR: [tx(ROOT_ADDR, A1, 100)], A1: []}
        self.patch(quiet_fixture)
        quiet = TT.trace_tree(ROOT_ADDR, TOKEN, depth=3, min_usd=1000, days=30, max_fetches=25)
        self.assertNotIn("BRIDGE_TERMINATED", quiet["verdict"])
        self.assertNotEqual(r["verdict"], quiet["verdict"])


class TestBridgeCrossChainContinuation(_Patch):
    def test_eoa_bridges_then_deposits_to_cex_on_other_chain(self):
        fixture = {
            ROOT_ADDR: [tx(ROOT_ADDR, A1, 100)],
            A1: {
                "binance-smart-chain": [tx(A1, BRIDGE_ADDR, 80)],
                "ethereum": [tx(A1, CEX_ADDR_2, 50)],
            },
        }
        bridges = {BRIDGE_ADDR: {"name": "Wormhole Portal Token Bridge (BSC)", "trail_continues": ["eth"]}}
        chains = {"binance-smart-chain": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                 "ethereum": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
        self.patch(fixture, bridges=bridges, chains=chains, labels={CEX_ADDR_2: "Binance Hot Wallet 6"})
        r = TT.trace_tree(ROOT_ADDR, TOKEN, chain="bsc", depth=3, min_usd=1000, days=30, max_fetches=25)
        cont = r["nodes"][BRIDGE_ADDR]["continuation"]
        self.assertTrue(cont["available"])
        self.assertEqual(cont["chain_checked"], "ethereum")
        self.assertTrue(cont["cex_terminated"])
        self.assertAlmostEqual(cont["amount_usd"], 2500.0, places=0)

    def test_provider_absent_on_continuation_never_reports_clean(self):
        fixture = {ROOT_ADDR: [tx(ROOT_ADDR, A1, 100)],
                   A1: {"binance-smart-chain": [tx(A1, BRIDGE_ADDR, 80)]}}
        bridges = {BRIDGE_ADDR: {"name": "Wormhole Portal Token Bridge (BSC)", "trail_continues": ["eth"]}}
        chains = {"binance-smart-chain": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                 "ethereum": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
        self.patch(fixture, bridges=bridges, chains=chains, fail_chains={"ethereum"})
        r = TT.trace_tree(ROOT_ADDR, TOKEN, chain="bsc", depth=3, min_usd=1000, days=30, max_fetches=25)
        cont = r["nodes"][BRIDGE_ADDR]["continuation"]
        self.assertFalse(cont["available"])
        self.assertNotIn("cex_terminated", cont)          # never a clean bill on a failed read


class TestUnresolvedContract(_Patch):
    def test_unlabeled_contract_midtree_is_leaf_not_recursed(self):
        fixture = {ROOT_ADDR: [tx(ROOT_ADDR, CONTRACT_ADDR, 100)],
                   CONTRACT_ADDR: [tx(CONTRACT_ADDR, A2, 999999)]}
        self.patch(fixture, contract_addrs={CONTRACT_ADDR})
        r = TT.trace_tree(ROOT_ADDR, TOKEN, depth=3, min_usd=1000, days=30, max_fetches=25)
        self.assertEqual(r["nodes"][CONTRACT_ADDR]["terminal"], "unresolved_contract")
        self.assertNotIn(A2, r["nodes"])
        self.assertEqual(r["fetched"], 1)   # only root fetched — the contract probe isn't a fetch


class TestDecompositionRegression(_Patch):
    def test_no_bridge_hops_verdict_unchanged_plus_zero_fields(self):
        fixture = {ROOT_ADDR: [tx(ROOT_ADDR, A1, 100)], A1: [tx(A1, CEX_ADDR, 80)]}
        self.patch(fixture, labels={CEX_ADDR: "Binance Hot Wallet 6"})
        r = TT.trace_tree(ROOT_ADDR, TOKEN, depth=3, min_usd=1000, days=30, max_fetches=25)
        self.assertTrue(r["verdict"].startswith("CEX_TERMINATED"))
        self.assertNotIn("BRIDGE_TERMINATED", r["verdict"])
        self.assertFalse(r["bridge_terminated"])
        self.assertEqual(r["terminated"]["bridge"]["usd"], 0)
        self.assertAlmostEqual(r["terminated"]["cex"]["usd"], 4000.0, places=0)

    def test_dex_terminal_sized_in_decomposition(self):
        fixture = {ROOT_ADDR: [tx(ROOT_ADDR, ROUTER_ADDR, 100)],
                   ROUTER_ADDR: [tx(ROUTER_ADDR, A2, 999999)]}
        self.patch(fixture, labels={ROUTER_ADDR: "PancakeSwap V3 Router"})
        r = TT.trace_tree(ROOT_ADDR, TOKEN, depth=3, min_usd=1000, days=30, max_fetches=25)
        self.assertAlmostEqual(r["terminated"]["dex"]["usd"], 5000.0, places=0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
