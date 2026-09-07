#!/usr/bin/env python3
"""SPEC-192 #1 — GoPlus chain-map extension: solana/sui/monad/berachain/bitlayer/
sonic/robinhood/mantle/abstract/world-chain gain keyless holders (top-10 + holder_count)
through onboard._rank_chains -> onchain._concentration_primary. sei/hemi/ton/cardano/
algorand stay explicitly BLIND (named chain + provider), never a silent "unsupported".

Run:  python3 -m unittest tests.test_spec192_chain_coverage -v

All network seams monkeypatched — offline, deterministic. Chain ids / keyless coverage
per reports/RESEARCH-2026-09-02-onchain-chain-coverage.md (live-verified 2026-09-02).
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OC = _load("onchain")
OB = _load("onboard")

MAGMA_CONTRACT = "0x9f854b3ad20f8161ec0886f15f4a1752bf75d22261556f14cc8d3a1c5d50e529::magma::MAGMA"


class TestGoPlusChainMapExtension(unittest.TestCase):
    """Chain-map lookups per provider (the literal DoD ask)."""

    def test_new_chains_present_in_goplus_map(self):
        for chain in ("solana", "sui", "monad", "berachain", "bitlayer", "sonic",
                     "robinhood", "mantle", "abstract", "world-chain"):
            self.assertIn(chain, OC._GOPLUS_CHAIN, chain)

    def test_blind_chains_absent_from_goplus_map(self):
        for chain in ("sei-v2", "hemi", "the-open-network", "cardano", "algorand"):
            self.assertNotIn(chain, OC._GOPLUS_CHAIN, chain)

    def test_solana_and_sui_are_name_segment_chains(self):
        self.assertIn("solana", OC._GOPLUS_NAME_CHAINS)
        self.assertIn("sui", OC._GOPLUS_NAME_CHAINS)
        # every other new chain is a plain numeric chain-id path
        for chain in ("monad", "berachain", "bitlayer", "sonic", "robinhood", "mantle",
                     "abstract", "world-chain"):
            self.assertTrue(OC._GOPLUS_CHAIN[chain].isdigit(), chain)

    def test_cg_platform_map_has_identity_entries(self):
        for chain in ("solana", "sui", "monad", "berachain", "bitlayer", "sonic",
                     "robinhood", "mantle", "abstract", "world-chain"):
            self.assertEqual(OC._CG_PLATFORM_TO_GOPLUS.get(chain), chain)


class TestGoPlusResultDispatch(unittest.TestCase):
    """`_goplus_result` hits the name-segment URL for solana/sui (case-sensitive
    address), the numeric chain_id URL for everything else (lowercased address)."""

    def setUp(self):
        self._orig = OC._get_json
        self.urls = []

    def tearDown(self):
        OC._get_json = self._orig

    def _capture(self, body):
        def fake(url, **kw):
            self.urls.append(url)
            return body
        OC._get_json = fake

    def test_sui_uses_name_segment_and_preserves_case(self):
        key = MAGMA_CONTRACT
        self._capture({"result": {key: {"holder_count": "17854",
                                        "holders": [{"address": "0x8e01", "percent": "0.51"}]}}})
        res = OC._goplus_result(key, "sui")
        self.assertIn("api/v1/sui/token_security", self.urls[0])
        self.assertIn(key, self.urls[0])   # NOT lowercased
        self.assertEqual(res["holder_count"], "17854")

    def test_solana_holders_use_account_key(self):
        mint = "EchesyfXePKdLtoiZSL8pBe8Myagyy8ZRqsACNCFGnvp"
        self._capture({"result": {mint: {"holder_count": "50196",
                                         "holders": [{"account": "6b4aypBh", "percent": "0.2518"}]}}})
        c = OC._goplus_concentration(mint, "solana")
        self.assertTrue(c["available"])
        self.assertEqual(c["holders"][0]["address"], "6b4aypBh")

    def test_evm_chain_still_lowercases_and_uses_numeric_path(self):
        contract = "0xABCDEF0000000000000000000000000000000001"
        self._capture({"result": {contract.lower(): {"holder_count": "100",
                                                       "holders": [{"address": "0xdead", "percent": "0.1"}]}}})
        OC._goplus_concentration(contract, "monad")
        self.assertIn("token_security/143", self.urls[0])
        self.assertIn(contract.lower(), self.urls[0])

    def test_unsupported_chain_named_reason(self):
        c = OC._goplus_concentration("0xabc", "sei-v2")
        self.assertFalse(c["available"])
        self.assertIn("sei-v2", c["reason"])


class TestRankChainsSupportsNewChains(unittest.TestCase):
    """onboard._rank_chains marks the new chains supported:true; sei/ton/cardano/
    algorand/ontology/galachain (BLIND providers) stay supported:false, named."""

    def setUp(self):
        self._orig_chain_stats = OB._chain_stats

    def tearDown(self):
        OB._chain_stats = self._orig_chain_stats

    def test_magma_shaped_sui_only_contract_ranks_supported(self):
        OB._chain_stats = lambda addr, gp: {"holder_count": 17854, "total_supply": 1_000_000_000.0}
        ranked, primary, unreadable = OB._rank_chains({"sui": MAGMA_CONTRACT}, 1_000_000_000.0)
        self.assertEqual(primary, "sui")
        self.assertTrue(ranked[0]["supported"])
        self.assertEqual(ranked[0]["goplus_chain"], "sui")

    def test_blind_chains_marked_unsupported_and_named(self):
        OB._chain_stats = lambda addr, gp: {}
        contracts = {"sei-v2": "0xsei", "the-open-network": "EQtac", "cardano": "policy||hex",
                    "algorand": "3203964481", "galachain": "GALA|Unit|none|none",
                    "ontology": "ong"}
        ranked, primary, unreadable = OB._rank_chains(contracts, 1_000_000.0)
        by_chain = {r["chain"]: r for r in ranked}
        for chain in contracts:
            self.assertFalse(by_chain[chain]["supported"], chain)

    def test_multi_evm_and_solana_new_chains_all_supported(self):
        OB._chain_stats = lambda addr, gp: {"holder_count": 500, "total_supply": 100.0}
        contracts = {"monad": "0x1", "berachain": "0x2", "bitlayer": "0x3", "sonic": "0x4",
                    "robinhood": "0x5", "mantle": "0x6", "abstract": "0x7",
                    "world-chain": "0x8", "solana": "MintAddr111"}
        ranked, primary, unreadable = OB._rank_chains(contracts, 800.0)
        by_chain = {r["chain"]: r for r in ranked}
        for chain in contracts:
            self.assertTrue(by_chain[chain]["supported"], chain)


class TestConcentrationPrimaryOnNewChains(unittest.TestCase):
    """brief-level acceptance: a MAGMA-shaped (sui-primary) token's concentration
    populates with provider goplus / chain sui, never 'no readable chain among ranked'."""

    def test_sui_primary_token_concentration_available(self):
        tok = {
            "contracts": {"sui": MAGMA_CONTRACT},
            "chains_ranked": [{"chain": "sui", "goplus_chain": "sui", "supported": True,
                               "supply_pct": 100.0, "bridge_stub": False, "holder_count": 17854}],
            "primary_chain": "sui",
        }
        orig = OC._goplus_concentration
        OC._goplus_concentration = lambda contract, chain: {
            "available": True, "source": "goplus", "chain": chain, "holder_count": 17854,
            "top1_pct": 51.0, "top10_pct": 80.0, "burn_pct": None, "top1_ex_burn_pct": 51.0,
            "holders": [{"address": "0x8e01", "percent": 51.0, "tag": None,
                        "is_contract": False, "is_locked": False, "is_burn": False}]}
        try:
            r = OC._concentration_primary(tok)
        finally:
            OC._goplus_concentration = orig
        self.assertTrue(r["available"])
        self.assertEqual(r["source"], "goplus")
        self.assertEqual(r["chain"], "sui")
        self.assertEqual(r["holder_count"], 17854)
        self.assertEqual(r["top1_pct"], 51.0)
        self.assertNotIn("no readable chain", str(r.get("reason", "")))

    def test_no_readable_chain_never_fires_for_any_newly_supported_chain(self):
        for chain, contract in (("solana", "Mint1"), ("sui", MAGMA_CONTRACT),
                                ("monad", "0x1"), ("berachain", "0x2"), ("bitlayer", "0x3"),
                                ("sonic", "0x4"), ("robinhood", "0x5"), ("mantle", "0x6"),
                                ("abstract", "0x7"), ("world-chain", "0x8")):
            tok = {"contracts": {chain: contract},
                  "chains_ranked": [{"chain": chain, "goplus_chain": chain, "supported": True,
                                     "supply_pct": 100.0, "bridge_stub": False, "holder_count": 10}],
                  "primary_chain": chain}
            orig = OC._goplus_concentration
            OC._goplus_concentration = lambda c, ch: {
                "available": True, "source": "goplus", "chain": ch, "holder_count": 10,
                "top1_pct": 10.0, "top10_pct": 20.0, "burn_pct": None, "top1_ex_burn_pct": 10.0,
                "holders": []}
            try:
                r = OC._concentration_primary(tok)
            finally:
                OC._goplus_concentration = orig
            self.assertTrue(r["available"], chain)
            self.assertNotIn("no readable chain", str(r.get("reason", "")), chain)


# ── SPEC-192 #2: Etherscan V2 free-tier chain ids + pacing fix ──────────────────────
class TestEtherscanChainIdExtension(unittest.TestCase):
    def test_free_tier_chains_present_with_correct_ids(self):
        expected = {"ethereum": 1, "arbitrum-one": 42161, "polygon-pos": 137,
                   "mantle": 5000, "berachain": 80094, "monad": 143, "sonic": 146,
                   "sei-v2": 1329, "abstract": 2741, "hyperevm": 999, "world-chain": 480}
        for chain, chainid in expected.items():
            self.assertEqual(OC._ETHERSCAN_CHAINID.get(chain), chainid, chain)

    def test_paid_only_chains_absent(self):
        for chain in ("binance-smart-chain", "base", "optimistic-ethereum", "avalanche"):
            self.assertNotIn(chain, OC._ETHERSCAN_CHAINID, chain)

    def test_pacing_is_at_most_3_calls_per_second(self):
        # 3 cps -> minimum 1/3s between calls; the old 0.25s assumed 4 rps
        self.assertGreaterEqual(OC._ETHERSCAN_MIN_INTERVAL, 1.0 / 3.0 - 1e-9)


class TestEtherscanPacingLive(unittest.TestCase):
    """Two consecutive tokentx calls on different (now free-tier) chains must still
    honor the shared throttle — the key is metered across all ten chains, not per-chain."""

    def setUp(self):
        self._orig_call = OC._etherscan_call
        self._orig_key = OC._etherscan_key
        self._orig_last = OC._ETHERSCAN_LAST[0]
        self.times = []
        OC._etherscan_key = lambda: "TESTKEY"

        def fake_call(url, timeout=15):
            self.times.append(__import__("time").time())
            return {"status": "1", "result": []}
        OC._etherscan_call = fake_call
        OC._ETHERSCAN_LAST[0] = 0.0
        OC._ETHERSCAN_CACHE.clear()

    def tearDown(self):
        OC._etherscan_call = self._orig_call
        OC._etherscan_key = self._orig_key
        OC._ETHERSCAN_LAST[0] = self._orig_last

    def test_back_to_back_calls_spaced_by_min_interval(self):
        OC._etherscan_tokentx("0xaaa", None, "monad", days=1)
        OC._etherscan_tokentx("0xbbb", None, "berachain", days=1)
        self.assertEqual(len(self.times), 2)
        self.assertGreaterEqual(self.times[1] - self.times[0], OC._ETHERSCAN_MIN_INTERVAL - 0.02)


# ── SPEC-192 #3: Blockscout flow + holders extension ────────────────────────────────
class TestBlockscoutFlowChainExtension(unittest.TestCase):
    def test_new_flow_instances_present(self):
        instances = OC._BLOCKSCOUT_DEFAULT_INSTANCES
        for chain in ("arbitrum-one", "optimistic-ethereum", "polygon-pos", "hemi"):
            self.assertIn(chain, instances, chain)

    def test_providers_for_include_blockscout_on_new_chains(self):
        for chain in ("arbitrum-one", "optimistic-ethereum", "polygon-pos", "hemi"):
            names = [p["name"] for p in OC.providers_for(chain)]
            self.assertIn("blockscout", names, chain)

    def test_bsc_berachain_monad_sonic_avalanche_have_no_blockscout_instance(self):
        instances = OC._BLOCKSCOUT_DEFAULT_INSTANCES
        for chain in ("binance-smart-chain", "berachain", "monad", "sonic", "avalanche"):
            self.assertNotIn(chain, instances, chain)


class TestBlockscoutHolders(unittest.TestCase):
    def setUp(self):
        self._orig_get_json = OC._get_json
        self._orig_instances = OC._blockscout_instances
        OC._blockscout_instances = lambda: {"hemi": "https://explorer.hemi.xyz/api"}

    def tearDown(self):
        OC._get_json = self._orig_get_json
        OC._blockscout_instances = self._orig_instances

    def test_hemi_holders_computed_from_v2_meta_and_holders(self):
        calls = []

        def fake(url, **kw):
            calls.append(url)
            if url.endswith("/holders"):
                return {"items": [{"address": {"hash": "0xtop1"}, "value": "600"},
                                  {"address": {"hash": "0xtop2"}, "value": "400"}]}
            return {"total_supply": "1000", "holders_count": 250}
        OC._get_json = fake
        r = OC._blockscout_holders("0xHEMI", "hemi")
        self.assertTrue(r["available"])
        self.assertEqual(r["source"], "blockscout")
        self.assertEqual(r["holder_count"], 250)
        self.assertAlmostEqual(r["top1_pct"], 60.0)
        self.assertAlmostEqual(r["top10_pct"], 100.0)
        self.assertTrue(any("/api/v2/tokens/0xhemi/holders" in u for u in calls))

    def test_base_is_not_a_verified_holders_chain(self):
        r = OC._blockscout_holders("0xAERO", "base")
        self.assertFalse(r["available"])
        self.assertIn("base", r["reason"])

    def test_dead_instance_degrades_loudly(self):
        OC._get_json = lambda url, **kw: None
        r = OC._blockscout_holders("0xHEMI", "hemi")
        self.assertFalse(r["available"])
        self.assertIn("unavailable", r["reason"])

    def test_no_instance_configured_named(self):
        OC._blockscout_instances = lambda: {}
        r = OC._blockscout_holders("0xHEMI", "hemi")
        self.assertFalse(r["available"])
        self.assertIn("hemi", r["reason"])


# ── SPEC-192: refresh_chain_ranking — the stale-cache migration path ────────────────
class TestRefreshChainRanking(unittest.TestCase):
    def setUp(self):
        self._orig_load = OB._load_cfg
        self._orig_write = OB._write_cfg
        self._orig_chain_stats = OB._chain_stats
        self._orig_cg_resolve = OB._cg_resolve
        self.written = {}

    def tearDown(self):
        OB._load_cfg = self._orig_load
        OB._write_cfg = self._orig_write
        OB._chain_stats = self._orig_chain_stats
        OB._cg_resolve = self._orig_cg_resolve

    def _install(self, cfg):
        OB._load_cfg = lambda: cfg
        OB._write_cfg = lambda c: self.written.setdefault("cfg", c)

    def test_single_chain_stale_sui_token_becomes_supported(self):
        cfg = {"tokens": {"MAGMA": {
            "contracts": {"sui": MAGMA_CONTRACT}, "cg_id": "magma-finance",
            "primary_chain": "sui",
            "chains_ranked": [{"chain": "sui", "goplus_chain": None, "supported": False,
                               "supply_pct": None, "holder_count": None, "bridge_stub": False}],
        }}}
        self._install(cfg)
        OB._cg_resolve = lambda t, cg_id=None: {"total_supply": 1_000_000_000.0}
        OB._chain_stats = lambda addr, gp: {"holder_count": 17854, "total_supply": 1_000_000_000.0}
        r = OB.refresh_chain_ranking("MAGMA")
        self.assertTrue(r["ok"])
        self.assertTrue(r["changed"])
        self.assertEqual(r["primary_chain"], "sui")
        self.assertTrue(r["chains_ranked"][0]["supported"])
        self.assertEqual(self.written["cfg"]["tokens"]["MAGMA"]["chains_ranked"][0]["supported"], True)

    def test_multichain_reranks_primary_when_exotic_chain_now_wins(self):
        cfg = {"tokens": {"PENGU": {
            "contracts": {"solana": "Mint1", "ethereum": "0xabc"}, "cg_id": "pudgy-penguins",
            "primary_chain": "ethereum",
            "chains_ranked": [
                {"chain": "ethereum", "goplus_chain": "ethereum", "supported": True,
                 "supply_pct": 0.1, "holder_count": 5, "bridge_stub": True},
                {"chain": "solana", "goplus_chain": None, "supported": False,
                 "supply_pct": None, "holder_count": None, "bridge_stub": False}],
        }}}
        self._install(cfg)
        OB._cg_resolve = lambda t, cg_id=None: {"total_supply": 1_000_000_000.0}

        def stats(addr, gp):
            if gp == "solana":
                return {"holder_count": 200_000, "total_supply": 999_000_000.0}
            return {"holder_count": 5, "total_supply": 1_000_000.0}
        OB._chain_stats = stats
        r = OB.refresh_chain_ranking("PENGU")
        self.assertTrue(r["ok"])
        self.assertEqual(r["primary_chain"], "solana")

    def test_not_tracked_is_structured_error(self):
        self._install({"tokens": {}})
        r = OB.refresh_chain_ranking("GHOST")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "not_tracked")

    def test_no_change_reports_changed_false(self):
        row = {"chain": "sui", "goplus_chain": "sui", "supported": True,
              "supply_pct": None, "holder_count": 10, "bridge_stub": False}
        cfg = {"tokens": {"MAGMA": {"contracts": {"sui": MAGMA_CONTRACT}, "cg_id": None,
                                    "primary_chain": "sui", "chains_ranked": [row]}}}
        self._install(cfg)
        OB._chain_stats = lambda addr, gp: {"holder_count": 10}
        r = OB.refresh_chain_ranking("MAGMA")
        self.assertTrue(r["ok"])
        self.assertFalse(r["changed"])


# ── SPEC-192 #7: retire dead RPC URLs, fix the BSC getLogs window ───────────────────
class TestDeadRpcUrlsRetired(unittest.TestCase):
    def test_polygon_rpc_com_removed_from_stake_schedule_pool(self):
        SS = _load("stake_schedule")
        for url in SS.RPC_POOL.get("polygon", []):
            self.assertNotIn("polygon-rpc.com", url)

    def test_bsc_drpc_org_removed_from_local_index_table(self):
        LI = _load("local_index")
        for row in LI.RPC_PROVIDER_TABLE.get("binance-smart-chain", []):
            self.assertNotIn("bsc.drpc.org", row["url"])

    def test_bsc_publicnode_present_at_2000_block_cap(self):
        LI = _load("local_index")
        rows = LI.RPC_PROVIDER_TABLE.get("binance-smart-chain", [])
        pn = next((r for r in rows if "bsc-rpc.publicnode.com" in r["url"]), None)
        self.assertIsNotNone(pn)
        self.assertLessEqual(pn["max_range"], 2000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
