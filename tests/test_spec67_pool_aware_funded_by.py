#!/usr/bin/env python3
"""SPEC-67 — verify_wallet: pool/contract-aware funded_by.

The live false positive (2026-06-12): verify_wallet called a real ESPORTS swap-buyer
"operator-seeded" because its top funded_by source carried a desk label "TWAP-HUB-6.8M"
— but that address is the PancakeSwap V3 WBNB/ESPORTS 0.01% POOL. A tracked LABEL must
never override the on-chain FACT that the address is a DEX pool.

Run:  python3 tests/test_spec67_pool_aware_funded_by.py

All RPC is monkeypatched (offline-deterministic).
"""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VW = _load("verify_wallet", "capabilities/verify_wallet.py")
OC = _load("onchain", "capabilities/onchain.py")

WALLET = "0x4fec3d360000000000000000000000000000002ceb"   # the ESPORTS whale (swap buyer)
POOL = "0x5bb59bb9000000000000000000000000000004462"      # the V3 WBNB/ESPORTS pool (mislabeled hub)
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
ESPORTS_CONTRACT = "0xdef0000000000000000000000000000000000009"
NOW = datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def tx(frm, to, val, dt):
    return {"from_address": frm, "to_address": to, "value_decimal": str(val),
            "block_timestamp": _iso(dt), "token_symbol": "ESPORTS"}


class _VWBase(unittest.TestCase):
    """A temp tracked config with ONE distribution-tier wallet = the POOL address."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig = (VW.WALLETS, VW.token_transfers, VW._quote_transfers,
                      VW._live_price, VW.balance_of, VW.probe_contract)
        VW.WALLETS = d / "tracked_wallets.json"
        VW.WALLETS.write_text(json.dumps({"tokens": {"ESPORTS": {
            "decimals": 18,
            "contracts": {"binance-smart-chain": ESPORTS_CONTRACT},
            "wallets": [{"label": "TWAP-HUB-6.8M", "address": POOL,
                         "chain": "binance-smart-chain", "tier": "distribution"}]}}}))
        VW._live_price = lambda tk: (1.0, "live")
        VW.balance_of = lambda *a, **k: {"available": False}
        VW._quote_transfers = lambda *a, **k: {}
        self.token_txs = []
        VW.token_transfers = lambda address, contract, chain_key, days=180, decimals=18: (
            list(self.token_txs), "moralis", False)

    def tearDown(self):
        (VW.WALLETS, VW.token_transfers, VW._quote_transfers,
         VW._live_price, VW.balance_of, VW.probe_contract) = self._orig
        self.dir.cleanup()


class TestPoolPoisonsSeed(_VWBase):
    def test_tracked_pool_source_is_not_a_seed(self):
        # the whale's 956 inbound clips are swap fills FROM the pool (a tracked "hub" label)
        self.token_txs = [tx(POOL, WALLET, 1000, NOW - timedelta(days=3)),
                          tx(WALLET, "0xsomesink", 200, NOW - timedelta(days=1))]
        # the probe says: this source is the V3 pool, NOT a safe
        VW.probe_contract = lambda addr, chain, *a, **k: (
            {"available": True, "is_contract": True, "is_pool": True,
             "token0": WBNB, "token1": ESPORTS_CONTRACT, "fee": 100, "kind": "dex-pool"}
            if addr.lower() == POOL else
            {"available": True, "is_contract": False, "is_pool": False})
        v = VW.build_verify(WALLET, "ESPORTS")
        self.assertTrue(v["available"])
        self.assertFalse(v["seeded_staging"])
        self.assertNotEqual(v["verdict"], "SEEDED-STAGING")
        top = v["funded_by"][0]
        self.assertEqual(top["address"], POOL)
        self.assertEqual(top["kind"], "dex-pool")
        self.assertTrue(top["label_conflict"])
        self.assertEqual(len(v["label_conflicts"]), 1)
        self.assertEqual(v["label_conflicts"][0]["address"], POOL)

    def test_eoa_tracked_safe_still_seeds(self):
        # SAME tracked source, but it probes as an EOA → seeding stands (BILL/ESPORTS safe)
        self.token_txs = [tx(POOL, WALLET, 1000, NOW - timedelta(days=2)),
                          tx(WALLET, "0xsink", 100, NOW - timedelta(days=1))]
        VW.probe_contract = lambda *a, **k: {"available": True, "is_contract": False, "is_pool": False}
        v = VW.build_verify(WALLET, "ESPORTS")
        self.assertTrue(v["seeded_staging"])
        self.assertEqual(v["verdict"], "SEEDED-STAGING")
        self.assertEqual(v["funded_by"][0]["kind"], "tracked-safe")
        self.assertEqual(v["label_conflicts"], [])

    def test_probe_failure_does_not_fabricate_pool(self):
        # an unreadable probe must NOT silently drop a real seed (degrade-explicit, §3)
        self.token_txs = [tx(POOL, WALLET, 1000, NOW - timedelta(days=2))]
        VW.probe_contract = lambda *a, **k: {"available": False, "is_contract": None, "is_pool": False}
        v = VW.build_verify(WALLET, "ESPORTS")
        self.assertTrue(v["seeded_staging"])   # unproven-not-a-pool → tier still applies


class TestProbeContract(unittest.TestCase):
    """probe_contract: bytecode + token0()/token1() detection over a mocked RPC."""

    def setUp(self):
        self._rpc = OC._rpc
        self._cache = OC.probe_contract.__defaults__  # noqa: keep ref (unused but explicit)

    def tearDown(self):
        OC._rpc = self._rpc

    def _mock_rpc(self, code, t0=None, t1=None, fee=None):
        def rpc(chain_key, method, params, timeout=12):
            if method == "eth_getCode":
                return code
            if method == "eth_call":
                data = params[0]["data"]
                if data == OC.POOL_TOKEN0_SELECTOR:
                    return ("0x" + "0" * 24 + t0[2:]) if t0 else "0x"
                if data == OC.POOL_TOKEN1_SELECTOR:
                    return ("0x" + "0" * 24 + t1[2:]) if t1 else "0x"
                if data == OC.POOL_FEE_SELECTOR:
                    return hex(fee) if fee is not None else "0x"
            return None
        OC._rpc = rpc

    def test_pool_detected(self):
        self._mock_rpc("0x60806040", t0=WBNB, t1=ESPORTS_CONTRACT, fee=100)
        p = OC.probe_contract(POOL, use_cache=False)
        self.assertTrue(p["available"])
        self.assertTrue(p["is_contract"])
        self.assertTrue(p["is_pool"])
        self.assertEqual(p["kind"], "dex-pool")
        self.assertEqual(p["token0"], WBNB)
        self.assertEqual(p["token1"], ESPORTS_CONTRACT)
        self.assertEqual(p["fee"], 100)

    def test_eoa_not_a_contract(self):
        self._mock_rpc("0x")
        p = OC.probe_contract(WALLET, use_cache=False)
        self.assertTrue(p["available"])
        self.assertFalse(p["is_contract"])
        self.assertFalse(p["is_pool"])
        self.assertEqual(p["kind"], "eoa")

    def test_non_pool_contract(self):
        self._mock_rpc("0x60806040")   # contract, but token0/token1 revert
        p = OC.probe_contract("0xa11ce00000000000000000000000000000000001", use_cache=False)
        self.assertTrue(p["is_contract"])
        self.assertFalse(p["is_pool"])
        self.assertEqual(p["kind"], "contract")

    def test_unreadable_bytecode_degrades(self):
        OC._rpc = lambda *a, **k: None
        p = OC.probe_contract(POOL, use_cache=False)
        self.assertFalse(p["available"])
        self.assertIsNone(p["is_contract"])   # NOT "it's an EOA"


class TestOnboardLint(unittest.TestCase):
    def _pool_probe(self, *a, **k):
        return {"available": True, "is_contract": True, "is_pool": True,
                "token0": WBNB, "token1": ESPORTS_CONTRACT, "fee": 100, "kind": "dex-pool"}

    def _eoa_probe(self, *a, **k):
        return {"available": True, "is_contract": False, "is_pool": False, "kind": "eoa"}

    def test_pool_without_contract_ok_rejected(self):
        r = OC.onboard_lint(POOL, contract_ok=False, probe=self._pool_probe)
        self.assertFalse(r["ok"])
        self.assertIn("pool", r["reason"])
        self.assertTrue(r["probe"]["is_pool"])
        self.assertIn("token0", r["note"])

    def test_pool_with_contract_ok_allowed(self):
        r = OC.onboard_lint(POOL, contract_ok=True, probe=self._pool_probe)
        self.assertTrue(r["ok"])
        self.assertIn("DEX pool", r["note"])

    def test_eoa_allowed(self):
        r = OC.onboard_lint(WALLET, contract_ok=False, probe=self._eoa_probe)
        self.assertTrue(r["ok"])

    def test_staging_sink_rejects_pool(self):
        cfg = {"tokens": {"BILL": {"contracts": {"binance-smart-chain": "0xabc"}, "wallets": []}}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            path = f.name
        added = OC.onboard_staging_sink("BILL", POOL, label="HUB?", wallets_path=path, probe=self._pool_probe)
        d = json.loads(Path(path).read_text())
        Path(path).unlink()
        self.assertFalse(added)                                  # pool never silently tagged
        self.assertEqual(d["tokens"]["BILL"]["wallets"], [])

    def test_staging_sink_pool_with_ack_records_probe_note(self):
        cfg = {"tokens": {"BILL": {"contracts": {"binance-smart-chain": "0xabc"}, "wallets": []}}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            path = f.name
        added = OC.onboard_staging_sink("BILL", POOL, label="ACK", wallets_path=path,
                                        contract_ok=True, probe=self._pool_probe)
        d = json.loads(Path(path).read_text())
        Path(path).unlink()
        self.assertTrue(added)
        self.assertIn("DEX pool", d["tokens"]["BILL"]["wallets"][0]["_note"])


if __name__ == "__main__":
    unittest.main()
