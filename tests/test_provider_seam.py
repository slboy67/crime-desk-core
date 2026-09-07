#!/usr/bin/env python3
"""SPEC-97 — chain-scoped token-flow provider seam: Etherscan-V2 (ETH-only) → Moralis →
RPC/getlogs, with a data-driven registry SPEC-101/102 can plug BSC providers into.

Run:  python3 tests/test_provider_seam.py

Offline-deterministic: every provider's HTTP layer is monkeypatched; no network.
The v3 premise this guards (live-verified 2026-07-02): Etherscan V2 free tier is
ETH-ONLY — chainid=56 is paywalled — so the seam must be CHAIN-SCOPED and a BSC
query must never touch Etherscan at all.
"""
import importlib.util
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

spec = importlib.util.spec_from_file_location("onchain_cap", ROOT / "capabilities" / "onchain.py")
O = importlib.util.module_from_spec(spec)
spec.loader.exec_module(O)

qspec = importlib.util.spec_from_file_location("provider_quota_t", ROOT / "capabilities" / "provider_quota.py")
PQ = importlib.util.module_from_spec(qspec)
qspec.loader.exec_module(PQ)


def _esc_row(ts=None, frm="0xaaa", to="0xbbb", value="2500000000000000000", dec="18"):
    """An Etherscan-V2 tokentx result row (their wire shape)."""
    return {"blockNumber": "19000001", "timeStamp": str(int(ts if ts is not None else time.time() - 3600)),
            "hash": "0xhash1", "from": frm, "to": to, "value": value,
            "tokenDecimal": dec, "tokenSymbol": "TT", "contractAddress": "0xc0ffee"}


MORALIS_TXS = [{"from_address": "0xmmm", "to_address": "0xnnn", "value_decimal": 7.0,
                "block_timestamp": "2026-07-01T00:00:00.000Z", "token_symbol": "TT"}]
GETLOGS_TXS = [{"from_address": "0xggg", "to_address": "0xhhh", "value_decimal": 1.0,
                "block_timestamp": "2026-07-01T00:00:00.000Z", "token_symbol": None}]


class SeamBase(unittest.TestCase):
    """Patch every provider's live layer; record who was consulted."""

    def setUp(self):
        self.calls = {"etherscan": [], "moralis": [], "getlogs": [], "quota": []}
        self._orig = (O._etherscan_call, O._etherscan_key, O._moralis_tokentx,
                      O._getlogs_transfers, list(O._PROVIDERS))
        self._orig_quota = O.provider_quota.record_call
        O._ETHERSCAN_CACHE.clear()
        O._etherscan_key = lambda: "TESTKEY"
        O.provider_quota.record_call = lambda provider, **k: self.calls["quota"].append(provider) or 1

        def esc_call(url, timeout=15):
            self.calls["etherscan"].append(url)
            return self.esc_response

        def moralis(address, contract=None, chain="bsc", days=180, max_pages=2, retries=2):
            self.calls["moralis"].append((address, contract, chain))
            if isinstance(self.moralis_response, Exception):
                raise self.moralis_response
            return self.moralis_response

        def getlogs(address, contract, chain_key, decimals=18, **k):
            self.calls["getlogs"].append((address, contract, chain_key))
            if isinstance(self.getlogs_response, Exception):
                raise self.getlogs_response
            return self.getlogs_response

        O._etherscan_call = esc_call
        O._moralis_tokentx = moralis
        O._getlogs_transfers = getlogs
        self.esc_response = {"status": "1", "message": "OK", "result": [_esc_row()]}
        self.moralis_response = MORALIS_TXS
        self.getlogs_response = GETLOGS_TXS

    def tearDown(self):
        (O._etherscan_call, O._etherscan_key, O._moralis_tokentx,
         O._getlogs_transfers, providers) = self._orig
        O.provider_quota.record_call = self._orig_quota
        O._PROVIDERS[:] = providers
        O._ETHERSCAN_CACHE.clear()


class TestChainScopedSeam(SeamBase):
    def test_eth_query_uses_etherscan_moralis_not_called(self):
        # DoD (a): ETH + key present + 200 → Etherscan is the source, Moralis untouched
        txs, source, partial = O.token_transfers("0xW1", "0xc0ffee", "ethereum")
        self.assertEqual(source, "etherscan")
        self.assertFalse(partial)
        self.assertEqual(len(self.calls["etherscan"]), 1)
        self.assertEqual(self.calls["moralis"], [])
        # Moralis-shaped envelope: the consumer contract fields are present
        self.assertEqual(txs[0]["from_address"], "0xaaa")
        self.assertEqual(txs[0]["to_address"], "0xbbb")
        self.assertAlmostEqual(txs[0]["value_decimal"], 2.5)
        self.assertEqual(txs[0]["token_symbol"], "TT")
        self.assertTrue(txs[0]["block_timestamp"].endswith("Z"))
        # request is chain-scoped to chainid=1 and carries the key
        self.assertIn("chainid=1", self.calls["etherscan"][0])
        self.assertIn("apikey=TESTKEY", self.calls["etherscan"][0])

    def test_bsc_query_never_touches_etherscan(self):
        # DoD (b): chain scope respected — BSC goes straight to Moralis, byte-identical envelope
        txs, source, partial = O.token_transfers("0xW2", "0xc0ffee", "binance-smart-chain")
        self.assertEqual(self.calls["etherscan"], [])
        self.assertEqual(source, "moralis")
        self.assertFalse(partial)
        self.assertEqual(txs, MORALIS_TXS)
        self.assertEqual(self.calls["moralis"][0], ("0xW2", "0xc0ffee", "bsc"))

    def test_etherscan_error_falls_through_to_moralis(self):
        # DoD (c): NOTOK error → fallthrough to Moralis, envelope unchanged
        self.esc_response = {"status": "0", "message": "NOTOK",
                             "result": "Free API access is not supported for this chain"}
        txs, source, partial = O.token_transfers("0xW3", "0xc0ffee", "ethereum")
        self.assertEqual(source, "moralis")
        self.assertEqual(txs, MORALIS_TXS)
        self.assertFalse(partial)

    def test_etherscan_and_moralis_dead_falls_to_getlogs(self):
        # DoD (c): both rich providers dead → RPC/getlogs, partial=True (today's contract)
        self.esc_response = {"status": "0", "message": "NOTOK", "result": "Max rate limit reached"}
        self.moralis_response = RuntimeError("moralis down")
        txs, source, partial = O.token_transfers("0xW4", "0xc0ffee", "ethereum")
        self.assertEqual(source, "getlogs")
        self.assertTrue(partial)
        self.assertEqual(txs, GETLOGS_TXS)

    def test_all_providers_dead_raises_moralis_error(self):
        self.esc_response = {"status": "0", "message": "NOTOK", "result": "boom"}
        self.moralis_response = RuntimeError("moralis down")
        self.getlogs_response = RuntimeError("rpc down")
        with self.assertRaises(O.MoralisError):
            O.token_transfers("0xW5", "0xc0ffee", "ethereum")

    def test_no_transactions_found_is_empty_result_not_error(self):
        # DoD (d): status:"0" "No transactions found" = EMPTY, no fallback, no error (§3)
        self.esc_response = {"status": "0", "message": "No transactions found", "result": []}
        txs, source, partial = O.token_transfers("0xW6", "0xc0ffee", "ethereum")
        self.assertEqual(txs, [])
        self.assertEqual(source, "etherscan")
        self.assertFalse(partial)
        self.assertEqual(self.calls["moralis"], [])   # an empty book is a datum, not a failure

    def test_no_key_behaves_like_today(self):
        # DoD (e): key absent → Etherscan skipped silently, Moralis path byte-identical
        O._etherscan_key = lambda: None
        txs, source, partial = O.token_transfers("0xW7", "0xc0ffee", "ethereum")
        self.assertEqual(self.calls["etherscan"], [])
        self.assertEqual(source, "moralis")
        self.assertEqual(txs, MORALIS_TXS)

    def test_fake_bsc_provider_registers_ahead_of_moralis(self):
        # DoD: the socket SPEC-101/102 will use — a registered BSC provider is consulted first
        fake_calls = []

        def fake_fetch(address, contract, chain_key, days, decimals):
            fake_calls.append((address, chain_key))
            return [{"from_address": "0xfff", "to_address": "0x111", "value_decimal": 9.0,
                     "block_timestamp": "2026-07-02T00:00:00.000Z", "token_symbol": "TT"}], False

        O.register_provider("local_index", fake_fetch,
                            chains=["binance-smart-chain"], before="moralis")
        txs, source, partial = O.token_transfers("0xW8", "0xc0ffee", "binance-smart-chain")
        self.assertEqual(source, "local_index")
        self.assertEqual(fake_calls, [("0xW8", "binance-smart-chain")])
        self.assertEqual(self.calls["moralis"], [])
        # and its chain scope is respected too: an ETH read never consults it
        txs, source, _ = O.token_transfers("0xW9", "0xc0ffee", "ethereum")
        self.assertEqual(source, "etherscan")
        self.assertEqual(len(fake_calls), 1)
        O.unregister_provider("local_index")

    def test_etherscan_call_meters_quota(self):
        # DoD: the meter increments per LIVE Etherscan call (cache hits don't count)
        O.token_transfers("0xWa", "0xc0ffee", "ethereum")
        self.assertEqual(self.calls["quota"], ["etherscan"])
        O.token_transfers("0xWa", "0xc0ffee", "ethereum")   # cached → no new live call
        self.assertEqual(self.calls["quota"], ["etherscan"])
        self.assertEqual(len(self.calls["etherscan"]), 1)

    def test_days_window_filters_old_rows(self):
        old = _esc_row(ts=time.time() - 10 * 86400, frm="0xold")
        fresh = _esc_row(ts=time.time() - 3600, frm="0xnew")
        self.esc_response = {"status": "1", "message": "OK", "result": [fresh, old]}
        txs, source, _ = O.token_transfers("0xWb", "0xc0ffee", "ethereum", days=7)
        self.assertEqual(source, "etherscan")
        self.assertEqual([t["from_address"] for t in txs], ["0xnew"])


def _bs_row(ts=None, frm="0xccc", to="0xddd", value="1000000000000000000", dec="18"):
    """A Blockscout tokentx result row — Etherscan-compatible wire shape (SPEC-152)."""
    return {"blockNumber": "19000002", "timeStamp": str(int(ts if ts is not None else time.time() - 1800)),
            "hash": "0xhash2", "from": frm, "to": to, "value": value,
            "tokenDecimal": dec, "tokenSymbol": "TT", "contractAddress": "0xc0ffee"}


FAKE_BLOCKSCOUT_INSTANCES = {"ethereum": "https://fake-eth.blockscout/api",
                             "base": "https://fake-base.blockscout/api"}


class BlockscoutSeamBase(SeamBase):
    """SeamBase + a monkeypatched Blockscout wire layer (own instance map, own call stub —
    same isolation discipline as the etherscan/moralis/getlogs mocks above)."""

    def setUp(self):
        super().setUp()
        self.calls["blockscout"] = []
        self._orig_bs_call = O._blockscout_call
        self._orig_bs_instances = O._blockscout_instances

        def bs_call(url, timeout=15):
            self.calls["blockscout"].append(url)
            if isinstance(self.bs_response, Exception):
                raise self.bs_response
            return self.bs_response

        O._blockscout_call = bs_call
        O._blockscout_instances = lambda: dict(FAKE_BLOCKSCOUT_INSTANCES)
        self.bs_response = {"status": "1", "message": "OK", "result": [_bs_row()]}

    def tearDown(self):
        O._blockscout_call = self._orig_bs_call
        O._blockscout_instances = self._orig_bs_instances
        super().tearDown()


class TestBlockscoutProvider(BlockscoutSeamBase):
    def test_registry_scoped_to_ethereum_and_base_only(self):
        # DoD: providers_for("base") includes blockscout; providers_for("bsc") does not
        base_names = [p["name"] for p in O.providers_for("base")]
        self.assertIn("blockscout", base_names)
        bsc_names = [p["name"] for p in O.providers_for("binance-smart-chain")]
        self.assertNotIn("blockscout", bsc_names)
        eth_names = [p["name"] for p in O.providers_for("ethereum")]
        self.assertIn("blockscout", eth_names)

    def test_fixture_txlist_matches_etherscan_row_shape(self):
        # DoD: identical fields to the Etherscan adapter's output for the same fixture
        row = _bs_row()
        self.bs_response = {"status": "1", "message": "OK", "result": [row]}
        # etherscan is scoped off (no key) so the seam falls straight to blockscout
        O._etherscan_key = lambda: None
        txs, source, partial = O.token_transfers("0xW1", "0xc0ffee", "ethereum")
        self.assertEqual(source, "blockscout")
        self.assertFalse(partial)
        expected = O._etherscan_map_row(row, 18)
        self.assertEqual(txs[0], expected)

    def test_no_transactions_found_is_empty_not_a_failure(self):
        self.bs_response = {"status": "0", "message": "No transactions found", "result": []}
        O._etherscan_key = lambda: None
        txs, source, partial = O.token_transfers("0xW2", "0xc0ffee", "ethereum")
        self.assertEqual(txs, [])
        self.assertEqual(source, "blockscout")
        self.assertFalse(partial)
        self.assertEqual(self.calls["moralis"], [])   # empty book, no fallthrough

    def test_500_falls_through_to_next_provider_consult_order(self):
        # DoD: a real blockscout failure falls through; assert the consult order
        O._etherscan_key = lambda: None                 # etherscan skipped (no key)
        self.bs_response = RuntimeError("blockscout 500")
        txs, source, partial = O.token_transfers("0xW3", "0xc0ffee", "ethereum")
        self.assertGreaterEqual(len(self.calls["blockscout"]), 1)   # blockscout WAS consulted
        self.assertEqual(source, "moralis")                         # then fell through to moralis
        self.assertEqual(txs, MORALIS_TXS)

    def test_base_chain_uses_blockscout_when_etherscan_not_scoped(self):
        # the SPEC-145 scenario, restated as a provider-count assertion: base previously had
        # only moralis+getlogs in the seam; with blockscout registered the count goes up by
        # one and a base wallet resolves through it instead of failing straight to moralis.
        before = len(O.providers_for("base"))
        O.unregister_provider("blockscout")
        after_removed = len(O.providers_for("base"))
        self.assertEqual(after_removed, before - 1)
        O.register_provider("blockscout", O._p_blockscout, chains={"ethereum", "base"}, before="moralis")
        self.assertEqual(len(O.providers_for("base")), before)
        txs, source, partial = O.token_transfers("0xW4", "0xc0ffee", "base")
        self.assertEqual(source, "blockscout")
        self.assertEqual(self.calls["moralis"], [])

    def test_divergence_emits_caveat_naming_both_providers(self):
        # DoD: blockscout 5 rows vs etherscan 7 rows -> both served, caveat naming both
        etherscan_rows = [_esc_row(ts=time.time() - i * 60, frm=f"0xe{i}") for i in range(7)]
        self.esc_response = {"status": "1", "message": "OK", "result": etherscan_rows}
        blockscout_rows = [_bs_row(ts=time.time() - i * 60, frm=f"0xb{i}") for i in range(5)]
        self.bs_response = {"status": "1", "message": "OK", "result": blockscout_rows}
        txs, source, caveat = O.blockscout_cross_check("0xW5", "0xc0ffee", "ethereum")
        self.assertEqual(source, "etherscan")
        self.assertEqual(len(txs), 7)
        self.assertEqual(len(self.calls["etherscan"]), 1)
        self.assertEqual(len(self.calls["blockscout"]), 1)   # both were served
        self.assertIsNotNone(caveat)
        self.assertIn("blockscout", caveat)
        self.assertIn("etherscan", caveat)
        self.assertIn("5", caveat)
        self.assertIn("7", caveat)

    def test_agreement_emits_no_caveat(self):
        etherscan_rows = [_esc_row(ts=time.time() - i * 60, frm=f"0xe{i}") for i in range(6)]
        self.esc_response = {"status": "1", "message": "OK", "result": etherscan_rows}
        blockscout_rows = [_bs_row(ts=time.time() - i * 60, frm=f"0xb{i}") for i in range(6)]
        self.bs_response = {"status": "1", "message": "OK", "result": blockscout_rows}
        txs, source, caveat = O.blockscout_cross_check("0xW6", "0xc0ffee", "ethereum")
        self.assertIsNone(caveat)

    def test_cross_check_skips_chain_blockscout_does_not_cover(self):
        # bsc isn't in the verified instance set — cross-check is a no-op there
        self.moralis_response = MORALIS_TXS
        txs, source, caveat = O.blockscout_cross_check("0xW7", "0xc0ffee", "binance-smart-chain")
        self.assertEqual(source, "moralis")
        self.assertIsNone(caveat)
        self.assertEqual(self.calls["blockscout"], [])


class TestProviderQuota(unittest.TestCase):
    """SPEC-97 — per-provider generalization of the SPEC-66 meter."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "etherscan_quota.json"
        self.now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("FAKEPROV_DAILY_BUDGET", None)

    def test_counter_and_warn_at_80pct(self):
        for _ in range(7):
            PQ.record_call("etherscan", now=self.now, state_path=self.path)
        st = PQ.status("etherscan", budget=10, now=self.now, state_path=self.path)
        self.assertEqual(st["calls"], 7)
        self.assertFalse(st["warn"])
        PQ.record_call("etherscan", now=self.now, state_path=self.path)
        st = PQ.status("etherscan", budget=10, now=self.now, state_path=self.path)
        self.assertEqual(st["pct"], 80)
        self.assertTrue(st["warn"])
        self.assertEqual(st["prefix"], "[QUOTA 80%]")

    def test_budget_env_overridable_per_provider(self):
        os.environ["FAKEPROV_DAILY_BUDGET"] = "10"
        self.assertEqual(PQ.budget_for("fakeprov"), 10)

    def test_default_budgets_per_provider(self):
        # moralis keeps the SPEC-66 default; etherscan gets its own conservative budget
        self.assertGreater(PQ.budget_for("etherscan"), 0)
        self.assertGreater(PQ.budget_for("moralis"), 0)

    def test_providers_have_distinct_state(self):
        p2 = Path(self.tmp.name) / "other_quota.json"
        PQ.record_call("etherscan", now=self.now, state_path=self.path)
        st = PQ.status("etherscan", budget=10, now=self.now, state_path=p2)
        self.assertEqual(st["calls"], 0)

    def test_with_prefix_contract(self):
        for _ in range(9):
            PQ.record_call("etherscan", now=self.now, state_path=self.path)
        self.assertEqual(
            PQ.with_prefix("etherscan", "clean read", budget=10, now=self.now, state_path=self.path),
            "[QUOTA 90%] clean read")


if __name__ == "__main__":
    unittest.main(verbosity=2)
