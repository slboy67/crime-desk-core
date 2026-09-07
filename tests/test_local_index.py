#!/usr/bin/env python3
"""SPEC-102 — tracked-set incremental local indexer: sunset-proof the surveillance loop.

Run:  python3 -m unittest tests.test_local_index -v

Offline-deterministic: every RPC call goes through an in-memory FakeChain (eth_blockNumber /
eth_getLogs), never real urllib. No live-network calls anywhere in this suite.
"""
import importlib.util
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

LI_spec = importlib.util.spec_from_file_location("local_index_t", ROOT / "capabilities" / "local_index.py")
LI = importlib.util.module_from_spec(LI_spec)
LI_spec.loader.exec_module(LI)

O_spec = importlib.util.spec_from_file_location("onchain_li_t", ROOT / "capabilities" / "onchain.py")
O = importlib.util.module_from_spec(O_spec)
O_spec.loader.exec_module(O)

CONTRACT = "0xC0FFEE0000000000000000000000000000C0FFEE"
ADDR_A = "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
ADDR_B = "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
NOW = 2_000_000_000.0
CHAIN = "testchain"
BLOCK_TIME = 1.0

# small provider table so tests run over a handful of blocks, not hundreds
SMALL_TABLE = [
    {"url": "p1", "max_range": 5, "min_interval_s": 0},
    {"url": "p2", "max_range": 2, "min_interval_s": 0},
]


def _mk_log(block, tx_hash, log_index, frm, to, value):
    return {
        "topics": [LI.TRANSFER_TOPIC,
                  "0x" + "0" * 24 + frm[2:].lower(),
                  "0x" + "0" * 24 + to[2:].lower()],
        "data": hex(value),
        "blockNumber": hex(block),
        "transactionHash": tx_hash,
        "logIndex": hex(log_index),
    }


class FakeChain:
    """In-memory chain: eth_blockNumber -> self.head, eth_getLogs -> logs in [lo,hi].
    `fail_getlogs_urls` fails only eth_getLogs on that url (blockNumber stays healthy) —
    the shape a real "getLogs range rejected / rate-limited" failure takes."""

    def __init__(self, head, logs=None, fail_getlogs_urls=None):
        self.head = head
        self.logs = list(logs or [])
        self.fail_getlogs_urls = set(fail_getlogs_urls or [])
        self.calls = []

    def rpc_call(self, url, method, params):
        self.calls.append((url, method, tuple(params) if params else params))
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getLogs":
            if url in self.fail_getlogs_urls:
                raise ConnectionError(f"simulated getLogs failure: {url}")
            p = params[0]
            lo, hi = int(p["fromBlock"], 16), int(p["toBlock"], 16)
            return [lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi]
        raise ValueError(f"unexpected method {method}")


DEFAULT_TEST_CFG = {"reorg_tail_blocks": 5, "backfill_days": 100, "freshness_max_age_min": 30,
                    "rpc_call_budget": 200}


class LocalIndexBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "local_index.db"
        self._orig_table = dict(LI.RPC_PROVIDER_TABLE)
        self._orig_bt = dict(LI._BLOCK_TIME_S)
        LI.RPC_PROVIDER_TABLE[CHAIN] = SMALL_TABLE
        LI._BLOCK_TIME_S[CHAIN] = BLOCK_TIME

    def tearDown(self):
        self.tmp.cleanup()
        LI.RPC_PROVIDER_TABLE.clear()
        LI.RPC_PROVIDER_TABLE.update(self._orig_table)
        LI._BLOCK_TIME_S.clear()
        LI._BLOCK_TIME_S.update(self._orig_bt)


class TestIngestAndQuery(LocalIndexBase):
    def test_ingest_then_query_returns_envelope_compatible_rows(self):
        chain = FakeChain(head=20, logs=[_mk_log(10, "0xtx1", 0, ADDR_A, ADDR_B, 5 * 10 ** 18)])
        res = LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                                 sleep_fn=lambda s: None, now=NOW, cfg=DEFAULT_TEST_CFG)
        self.assertTrue(res["available"])
        self.assertEqual(res["ingested_rows"], 1)
        self.assertFalse(res["gap"])
        txs = LI.query_transfers(ADDR_A, CONTRACT, CHAIN, db_path=self.db_path, now=NOW)
        self.assertEqual(len(txs), 1)
        row = txs[0]
        self.assertEqual(row["from_address"], ADDR_A.lower())
        self.assertEqual(row["to_address"], ADDR_B.lower())
        self.assertEqual(row["value_decimal"], 5.0)
        self.assertIsInstance(row["block_timestamp"], str)
        self.assertIn("transaction_hash", row)

    def test_query_finds_rows_by_either_leg(self):
        chain = FakeChain(head=20, logs=[_mk_log(10, "0xtx1", 0, ADDR_A, ADDR_B, 1 * 10 ** 18)])
        LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                           sleep_fn=lambda s: None, now=NOW, cfg=DEFAULT_TEST_CFG)
        self.assertEqual(len(LI.query_transfers(ADDR_A, CONTRACT, CHAIN, db_path=self.db_path, now=NOW)), 1)
        self.assertEqual(len(LI.query_transfers(ADDR_B, CONTRACT, CHAIN, db_path=self.db_path, now=NOW)), 1)

    def test_resume_from_head_ingests_only_new_blocks(self):
        chain = FakeChain(head=20, logs=[_mk_log(10, "0xtx1", 0, ADDR_A, ADDR_B, 1 * 10 ** 18)])
        LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                           sleep_fn=lambda s: None, now=NOW, cfg=DEFAULT_TEST_CFG)
        chain.calls.clear()
        chain.head = 30
        chain.logs.append(_mk_log(25, "0xtx2", 0, ADDR_A, ADDR_B, 2 * 10 ** 18))
        res2 = LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                                  sleep_fn=lambda s: None, now=NOW, cfg=DEFAULT_TEST_CFG)
        self.assertTrue(res2["available"])
        # tail (5 blocks) + new range (16..30) = 20 blocks -> a handful of chunks, NOT a
        # full 0..30 rescan (which would need many more getLogs calls)
        get_logs_calls = [c for c in chain.calls if c[1] == "eth_getLogs"]
        self.assertLess(len(get_logs_calls), 15)
        txs = LI.query_transfers(ADDR_A, CONTRACT, CHAIN, db_path=self.db_path, now=NOW)
        self.assertEqual(len(txs), 2)

    def test_duplicate_run_produces_zero_duplicate_rows(self):
        chain = FakeChain(head=20, logs=[_mk_log(10, "0xtx1", 0, ADDR_A, ADDR_B, 1 * 10 ** 18)])
        LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                           sleep_fn=lambda s: None, now=NOW, cfg=DEFAULT_TEST_CFG)
        # re-run with IDENTICAL head/logs (a no-op tick) — the tail re-scan re-fetches the
        # same rows; INSERT OR IGNORE must not duplicate them.
        LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                           sleep_fn=lambda s: None, now=NOW, cfg=DEFAULT_TEST_CFG)
        LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                           sleep_fn=lambda s: None, now=NOW, cfg=DEFAULT_TEST_CFG)
        txs = LI.query_transfers(ADDR_A, CONTRACT, CHAIN, db_path=self.db_path, now=NOW)
        self.assertEqual(len(txs), 1)

    def test_reorg_tail_rescan_replaces_changed_rows(self):
        chain = FakeChain(head=20, logs=[_mk_log(18, "0xtxA", 0, ADDR_A, ADDR_B, 1 * 10 ** 18)])
        LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                           sleep_fn=lambda s: None, now=NOW, cfg=DEFAULT_TEST_CFG)
        self.assertEqual(len(LI.query_transfers(ADDR_A, CONTRACT, CHAIN, db_path=self.db_path, now=NOW)), 1)
        # reorg: the tx at block 18 is replaced by a different tx (different hash/value) —
        # simulates a reorged-away transfer that must not survive in the index.
        chain.logs = [_mk_log(18, "0xtxB", 0, ADDR_A, ADDR_B, 9 * 10 ** 18)]
        LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                           sleep_fn=lambda s: None, now=NOW, cfg=DEFAULT_TEST_CFG)
        txs = LI.query_transfers(ADDR_A, CONTRACT, CHAIN, db_path=self.db_path, now=NOW)
        self.assertEqual(len(txs), 1)
        self.assertEqual(txs[0]["transaction_hash"], "0xtxB")
        self.assertEqual(txs[0]["value_decimal"], 9.0)


class TestFreshnessGate(LocalIndexBase):
    def setUp(self):
        super().setUp()
        self._orig_tracked = LI._tracked_contracts
        LI._tracked_contracts = lambda chain_key: {CONTRACT.lower()} if chain_key == CHAIN else set()

    def tearDown(self):
        LI._tracked_contracts = self._orig_tracked
        super().tearDown()

    def test_never_ingested_returns_unavailable_not_empty_clean(self):
        r = LI.query_or_stale(ADDR_A, CONTRACT, CHAIN, db_path=self.db_path, now=NOW)
        self.assertFalse(r["available"])
        self.assertEqual(r.get("stale_index"), False)
        self.assertIn("un-ingested", r["reason"])

    def test_untracked_contract_bypasses_index_entirely(self):
        r = LI.query_or_stale(ADDR_A, "0xnottracked", CHAIN, db_path=self.db_path, now=NOW)
        self.assertFalse(r["available"])
        self.assertNotIn("stale_index", r)   # never even asked the freshness question

    def test_fresh_index_serves_with_source_local_index(self):
        chain = FakeChain(head=20, logs=[_mk_log(10, "0xtx1", 0, ADDR_A, ADDR_B, 1 * 10 ** 18)])
        LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                           sleep_fn=lambda s: None, now=NOW, cfg=DEFAULT_TEST_CFG)
        r = LI.query_or_stale(ADDR_A, CONTRACT, CHAIN, db_path=self.db_path, now=NOW + 5)
        self.assertTrue(r["available"])
        self.assertEqual(r["source"], "local_index")
        self.assertFalse(r["stale_index"])
        self.assertEqual(len(r["txs"]), 1)

    def test_stale_index_reports_unavailable_and_stale_flag(self):
        chain = FakeChain(head=20, logs=[])
        LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                           sleep_fn=lambda s: None, now=NOW, cfg=DEFAULT_TEST_CFG)
        far_future = NOW + 3600 * 2   # 2h later, way past the 30min freshness window
        r = LI.query_or_stale(ADDR_A, CONTRACT, CHAIN, db_path=self.db_path, now=far_future)
        self.assertFalse(r["available"])
        self.assertTrue(r["stale_index"])
        self.assertIn("stale", r["reason"])


class TestRpcBudget(LocalIndexBase):
    def test_budget_cap_stops_and_resumes(self):
        chain = FakeChain(head=100, logs=[])
        cfg = dict(DEFAULT_TEST_CFG, rpc_call_budget=3)
        res1 = LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                                  sleep_fn=lambda s: None, now=NOW, cfg=cfg)
        self.assertTrue(res1["budget_exhausted"])
        self.assertEqual(res1["calls_used"], 3)
        self.assertLess(res1["new_head"], 100)
        res2 = LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                                  sleep_fn=lambda s: None, now=NOW, cfg=cfg)
        self.assertGreater(res2["new_head"], res1["new_head"])   # resumed, made progress

    def test_gap_stops_ingest_without_recording_empty_success(self):
        # every provider fails a chunk mid-range -> a real gap, head must not advance past it
        chain = FakeChain(head=20, logs=[_mk_log(18, "0xtx1", 0, ADDR_A, ADDR_B, 1 * 10 ** 18)],
                          fail_getlogs_urls={"p1", "p2"})
        res = LI.ingest_contract(CHAIN, CONTRACT, db_path=self.db_path, rpc_call=chain.rpc_call,
                                 sleep_fn=lambda s: None, now=NOW, cfg=DEFAULT_TEST_CFG)
        self.assertTrue(res["gap"])
        self.assertIsNone(res["new_head"])
        self.assertEqual(res["ingested_rows"], 0)


class TestProviderSeamIntegration(unittest.TestCase):
    """SPEC-97 seam wiring: local_index is the FIRST provider; stale/untracked falls
    through unchanged, exactly like every other provider in the registry."""

    def setUp(self):
        self._orig_query = O.local_index.query_or_stale
        self._orig_moralis = O._moralis_tokentx
        self._orig_providers = list(O._PROVIDERS)
        self.moralis_calls = []

        def moralis(address, contract=None, chain="bsc", days=180, max_pages=2, retries=2):
            self.moralis_calls.append((address, contract, chain))
            return [{"from_address": "0xmmm", "to_address": "0xnnn", "value_decimal": 7.0,
                    "block_timestamp": "2026-07-01T00:00:00.000Z", "token_symbol": "TT"}]

        O._moralis_tokentx = moralis

    def tearDown(self):
        O.local_index.query_or_stale = self._orig_query
        O._moralis_tokentx = self._orig_moralis
        O._PROVIDERS[:] = self._orig_providers
        O._LOCAL_INDEX_STALE_WARNED.clear()

    def test_local_index_is_the_first_provider(self):
        self.assertEqual(O._PROVIDERS[0]["name"], "local_index")

    def test_fresh_tracked_contract_serves_from_local_index_moralis_not_called(self):
        O.local_index.query_or_stale = lambda *a, **k: {
            "available": True, "source": "local_index", "partial": False,
            "stale_index": False, "txs": [{"from_address": "0xaaa", "to_address": "0xbbb",
                                           "value_decimal": 1.0, "block_timestamp": None,
                                           "token_symbol": None, "transaction_hash": "0xtx1"}]}
        txs, source, partial = O.token_transfers("0xWb", "0xc0ffee", "binance-smart-chain", days=7)
        self.assertEqual(source, "local_index")
        self.assertEqual(self.moralis_calls, [])
        self.assertEqual(len(txs), 1)

    def test_stale_index_falls_through_to_moralis_envelope_unchanged(self):
        O.local_index.query_or_stale = lambda *a, **k: {
            "available": False, "reason": "index stale (5000s old)", "stale_index": True, "age_s": 5000}
        txs, source, partial = O.token_transfers("0xWb", "0xc0ffee", "binance-smart-chain", days=7)
        self.assertEqual(source, "moralis")
        self.assertEqual(len(self.moralis_calls), 1)
        self.assertEqual(txs[0]["from_address"], "0xmmm")

    def test_untracked_contract_falls_through_silently(self):
        O.local_index.query_or_stale = lambda *a, **k: {
            "available": False, "reason": "not in tracked set — index bypassed"}
        txs, source, partial = O.token_transfers("0xWb", "0xc0ffee", "binance-smart-chain", days=7)
        self.assertEqual(source, "moralis")

    def test_eth_chain_query_never_consults_local_index(self):
        # local_index's RPC_PROVIDER_TABLE only has binance-smart-chain (+ ethereum in the
        # real table) -- but this proves an unscoped chain skips it cleanly either way.
        calls = []
        O.local_index.query_or_stale = lambda *a, **k: calls.append(1) or {"available": False, "reason": "x"}
        O.token_transfers("0xWb", "0xc0ffee", "avalanche", days=7)
        self.assertEqual(calls, [])   # avalanche isn't in local_index's chain scope


if __name__ == "__main__":
    unittest.main(verbosity=2)
