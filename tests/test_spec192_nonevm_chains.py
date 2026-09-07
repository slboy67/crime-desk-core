#!/usr/bin/env python3
"""SPEC-192 #4/#5/#6 — Sui GraphQL / Solana RPC / TON / Cardano / Algorand readers.

Run:  python3 -m unittest tests.test_spec192_nonevm_chains -v

Every network seam is injected (`post_fn`/`get_fn`) — offline, deterministic. Fixture
shapes mirror the literal live responses quoted in
reports/RESEARCH-2026-09-02-onchain-chain-coverage.md.
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


NE = _load("nonevm_chains")

MAGMA_COIN = "0x9f854b3ad20f8161ec0886f15f4a1752bf75d22261556f14cc8d3a1c5d50e529::magma::MAGMA"
MY_ADDR = "0x8e01c52e00000000000000000000000000000000000000000000000000f2a"
OTHER_ADDR = "0xdeadbeef00000000000000000000000000000000000000000000000000001"


class TestSuiGraphQLFlow(unittest.TestCase):
    def test_response_fixture_produces_flow_rows(self):
        fixture = {"data": {"address": {"transactions": {"nodes": [
            {"digest": "EQyGoZwD", "effects": {
                "timestamp": "2026-03-30T03:32:38.713Z",
                "balanceChangesJson": [
                    {"address": MY_ADDR, "amount": "-500000000000000000",
                     "coinType": MAGMA_COIN},
                    {"address": OTHER_ADDR, "amount": "500000000000000000",
                     "coinType": MAGMA_COIN},
                ]}},
        ]}}}}
        rows, reason = NE.sui_flow(MY_ADDR, MAGMA_COIN, post_fn=lambda q, v: fixture)
        self.assertIsNone(reason)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["from_address"], MY_ADDR.lower())
        self.assertEqual(r["to_address"], OTHER_ADDR.lower())
        self.assertEqual(r["value"], "500000000000000000")
        self.assertEqual(r["transaction_hash"], "EQyGoZwD")

    def test_unrelated_cointype_change_skipped(self):
        fixture = {"data": {"address": {"transactions": {"nodes": [
            {"digest": "X", "effects": {"timestamp": "2026-01-01T00:00:00.000Z",
                                        "balanceChangesJson": [
                                            {"address": MY_ADDR, "amount": "-1",
                                             "coinType": "0x2::sui::SUI"}]}},
        ]}}}}
        rows, reason = NE.sui_flow(MY_ADDR, MAGMA_COIN, post_fn=lambda q, v: fixture)
        self.assertIsNone(reason)
        self.assertEqual(rows, [])

    def test_graphql_errors_field_is_named_degradation(self):
        rows, reason = NE.sui_flow(MY_ADDR, MAGMA_COIN,
                                   post_fn=lambda q, v: {"errors": [{"message": "bad query"}]})
        self.assertEqual(rows, [])
        self.assertIn("bad query", reason)

    def test_post_fn_raising_is_named_degradation(self):
        def boom(q, v):
            raise RuntimeError("timeout")
        rows, reason = NE.sui_flow(MY_ADDR, MAGMA_COIN, post_fn=boom)
        self.assertEqual(rows, [])
        self.assertIn("timeout", reason)

    def test_old_transaction_outside_days_window_excluded(self):
        fixture = {"data": {"address": {"transactions": {"nodes": [
            {"digest": "OLD", "effects": {"timestamp": "2020-01-01T00:00:00.000Z",
                                          "balanceChangesJson": [
                                              {"address": MY_ADDR, "amount": "-1",
                                               "coinType": MAGMA_COIN},
                                              {"address": OTHER_ADDR, "amount": "1",
                                               "coinType": MAGMA_COIN}]}},
        ]}}}}
        rows, reason = NE.sui_flow(MY_ADDR, MAGMA_COIN, days=30, post_fn=lambda q, v: fixture)
        self.assertEqual(rows, [])


class TestSolanaRpcFlow(unittest.TestCase):
    def test_signature_fixture_produces_flow_rows(self):
        sig = "5gT3q..."

        def fake_get(method, params):
            if method == "getSignaturesForAddress":
                return {"result": [{"signature": sig, "blockTime": int(__import__("time").time())}]}
            if method == "getTransaction":
                return {"result": {
                    "meta": {
                        "preTokenBalances": [{"accountIndex": 0,
                                              "uiTokenAmount": {"uiAmount": 100.0},
                                              "owner": MY_ADDR, "mint": "MintAddr1"}],
                        "postTokenBalances": [{"accountIndex": 0,
                                               "uiTokenAmount": {"uiAmount": 40.0},
                                               "owner": MY_ADDR, "mint": "MintAddr1"},
                                              {"accountIndex": 1,
                                               "uiTokenAmount": {"uiAmount": 60.0},
                                               "owner": OTHER_ADDR, "mint": "MintAddr1"}],
                    },
                    "transaction": {"message": {"accountKeys": []}},
                }}
            raise AssertionError(f"unexpected method {method}")
        rows, reason = NE.solana_flow(MY_ADDR, get_fn=fake_get)
        self.assertIsNone(reason)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["from_address"], MY_ADDR.lower())
        self.assertEqual(r["to_address"], OTHER_ADDR.lower())
        self.assertAlmostEqual(r["value_decimal"], 60.0)
        self.assertEqual(r["transaction_hash"], sig)

    def test_throttled_getsignatures_is_named_degradation(self):
        def fake_get(method, params):
            raise RuntimeError("429 Too many requests")
        rows, reason = NE.solana_flow(MY_ADDR, get_fn=fake_get)
        self.assertEqual(rows, [])
        self.assertIn("429", reason)

    def test_malformed_result_is_named_not_silent_empty(self):
        rows, reason = NE.solana_flow(MY_ADDR, get_fn=lambda m, p: {"result": "not-a-list"})
        self.assertEqual(rows, [])
        self.assertIn("malformed", reason)

    def test_one_bad_tx_read_never_kills_the_sweep(self):
        calls = {"n": 0}

        def fake_get(method, params):
            if method == "getSignaturesForAddress":
                return {"result": [{"signature": "bad", "blockTime": int(__import__("time").time())},
                                   {"signature": "good", "blockTime": int(__import__("time").time())}]}
            calls["n"] += 1
            if params[0] == "bad":
                raise RuntimeError("boom")
            return {"result": {
                "meta": {"preTokenBalances": [{"accountIndex": 0, "uiTokenAmount": {"uiAmount": 5.0},
                                               "owner": MY_ADDR, "mint": "M"}],
                        "postTokenBalances": [{"accountIndex": 0, "uiTokenAmount": {"uiAmount": 0.0},
                                               "owner": MY_ADDR, "mint": "M"},
                                              {"accountIndex": 1, "uiTokenAmount": {"uiAmount": 5.0},
                                               "owner": OTHER_ADDR, "mint": "M"}]},
                "transaction": {"message": {"accountKeys": []}}}}
        rows, reason = NE.solana_flow(MY_ADDR, get_fn=fake_get)
        self.assertIsNone(reason)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["transaction_hash"], "good")


class TestTonCardanoAlgorandMinimalReaders(unittest.TestCase):
    def test_ton_holders_sorted_by_balance_no_nonce(self):
        d = {"jetton_wallets": [{"address": "0:ecc8", "balance": "5246060876000000", "owner": "0:06f9"}]}
        r = NE.ton_holders("EQBK", get_fn=lambda url: d)
        self.assertTrue(r["available"])
        self.assertEqual(r["holders"][0]["nonce"], "n/a")

    def test_ton_holders_dead_instance_named(self):
        r = NE.ton_holders("EQBK", get_fn=lambda url: None)
        self.assertFalse(r["available"])
        self.assertIn("toncenter", r["reason"])

    def test_ton_flow_rows(self):
        d = {"jetton_transfers": [{"source": "0:aaa", "destination": "0:bbb", "amount": "100",
                                   "transaction_hash": "H1"}]}
        rows, reason = NE.ton_flow("EQBK", get_fn=lambda url: d)
        self.assertIsNone(reason)
        self.assertEqual(rows[0]["from_address"], "0:aaa")

    def test_cardano_holders_ranked_by_quantity_no_nonce(self):
        d = [{"payment_address": "Ae2t...", "quantity": "11149547374"},
            {"payment_address": "Ae2s...", "quantity": "999"}]
        r = NE.cardano_holders("policy", "hexname", get_fn=lambda url: d)
        self.assertTrue(r["available"])
        self.assertEqual(r["holders"][0]["address"], "Ae2t...")
        self.assertEqual(r["holders"][0]["nonce"], "n/a")

    def test_cardano_flow_named_degradation_on_bad_shape(self):
        rows, reason = NE.cardano_flow("policy", "hexname", get_fn=lambda url: {"not": "a list"})
        self.assertEqual(rows, [])
        self.assertIn("koios", reason)

    def test_algorand_holders_ranked_no_nonce(self):
        d = {"balances": [{"address": "ABC", "amount": 100}, {"address": "DEF", "amount": 900}]}
        r = NE.algorand_holders(3203964481, get_fn=lambda url: d)
        self.assertTrue(r["available"])
        self.assertEqual(r["holders"][0]["address"], "DEF")
        self.assertEqual(r["holders"][0]["nonce"], "n/a")

    def test_algorand_flow_rows(self):
        d = {"transactions": [{"sender": "S1", "id": "TX1",
                               "asset-transfer-transaction": {"receiver": "R1", "amount": 5}}]}
        rows, reason = NE.algorand_flow(3203964481, get_fn=lambda url: d)
        self.assertIsNone(reason)
        self.assertEqual(rows[0]["from_address"], "S1")
        self.assertEqual(rows[0]["to_address"], "R1")

    def test_algorand_flow_skips_non_transfer_rows(self):
        """SPEC-192 #6 fix: live-verified — an asset's tx list includes non-transfer
        rows (asset-config creation, app calls) with no asset-transfer-transaction key
        at all; these must be skipped, not emitted as a fake to_address=None row."""
        d = {"transactions": [
            {"sender": "CREATOR", "id": "ACFG1", "asset-config-transaction": {"asset-id": 0}},
            {"sender": "S1", "id": "TX1", "asset-transfer-transaction": {"receiver": "R1", "amount": 5}},
        ]}
        rows, reason = NE.algorand_flow(3203964481, get_fn=lambda url: d)
        self.assertIsNone(reason)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["transaction_hash"], "TX1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
