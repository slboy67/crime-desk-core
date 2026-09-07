#!/usr/bin/env python3
"""SPEC-101 — holder-enumeration provider seam: Bitquery primary, GoPlus fallback.

Run:  python3 -m unittest tests.test_holder_enumeration -v

The ticket's problem statement assumed the current holder-enumeration source is Moralis
("stays on the Moralis free tier"). Live-checking this codebase found no Moralis-based
holder read anywhere — GoPlus (`_goplus_concentration`, SPEC 12/56) is the actual current
enumeration source, free and keyless. This suite tests the seam as actually built: Bitquery
-> GoPlus -> unavailable (see handoffs/REVIEW-REQUEST-SPEC-101.md for the drift note).

Offline-deterministic: Bitquery's HTTP layer and GoPlus's concentration read are both
monkeypatched; no network.
"""
import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

spec = importlib.util.spec_from_file_location("onchain_holders_cap", ROOT / "capabilities" / "onchain.py")
O = importlib.util.module_from_spec(spec)
spec.loader.exec_module(O)


def _bq_response(rows):
    return {"data": {"EVM": {"BalanceUpdates": rows}}}


def _bq_row(addr, balance):
    return {"BalanceUpdate": {"Address": addr}, "balance": str(balance)}


GOPLUS_CONCENTRATION_AVAILABLE = {
    "available": True, "source": "goplus", "chain": "binance-smart-chain",
    "holder_count": 500, "top1_pct": 40.0, "top10_pct": 78.5, "burn_pct": None,
    "top1_ex_burn_pct": 40.0,
    "holders": [{"address": f"0xgp{i}", "percent": 40.0 - i, "tag": None,
                "is_contract": False, "is_locked": False, "is_burn": False}
               for i in range(10)],
}
GOPLUS_CONCENTRATION_UNAVAILABLE = {"available": False, "reason": "GoPlus no result"}


class SeamBase(unittest.TestCase):
    """Patch both providers' live layers; record who was consulted."""

    def setUp(self):
        self.calls = {"bitquery": [], "goplus": [], "quota": []}
        self._orig_bq_call = O._bitquery_call
        self._orig_bq_key = O._bitquery_key
        self._orig_goplus = O._goplus_concentration
        self._orig_holder_providers = list(O._HOLDER_PROVIDERS)
        self._orig_quota_record = O.provider_quota.record_call
        O._BITQUERY_CACHE.clear()
        O._bitquery_key = lambda: "TESTKEY"
        O.provider_quota.record_call = lambda provider, **k: self.calls["quota"].append(provider) or 1

        def bq_call(query, variables, key, timeout=15):
            self.calls["bitquery"].append(variables)
            if isinstance(self.bq_response, Exception):
                raise self.bq_response
            return self.bq_response

        def goplus(contract, chain):
            self.calls["goplus"].append((contract, chain))
            return self.goplus_response

        O._bitquery_call = bq_call
        O._goplus_concentration = goplus
        self.bq_response = _bq_response([_bq_row("0xAAA", 600), _bq_row("0xBBB", 400)])
        self.goplus_response = GOPLUS_CONCENTRATION_AVAILABLE

    def tearDown(self):
        O._bitquery_call = self._orig_bq_call
        O._bitquery_key = self._orig_bq_key
        O._goplus_concentration = self._orig_goplus
        O._HOLDER_PROVIDERS[:] = self._orig_holder_providers
        O.provider_quota.record_call = self._orig_quota_record
        O._BITQUERY_CACHE.clear()


class TestBitqueryPrimary(SeamBase):
    def test_key_present_bitquery_is_source_goplus_not_called(self):
        out = O.enumerate_holders("0xc0ffee", "binance-smart-chain", top_n=10)
        self.assertTrue(out["available"])
        self.assertEqual(out["source"], "bitquery")
        self.assertEqual(self.calls["goplus"], [])
        self.assertEqual(len(self.calls["bitquery"]), 1)

    def test_share_pct_math_against_known_supply(self):
        # 600 + 400 = 1000 total -> 60% / 40%
        out = O.enumerate_holders("0xc0ffee", "binance-smart-chain", top_n=10)
        holders = {h["address"]: h for h in out["holders"]}
        self.assertAlmostEqual(holders["0xaaa"]["share_pct"], 60.0)
        self.assertAlmostEqual(holders["0xbbb"]["share_pct"], 40.0)
        self.assertAlmostEqual(out["top10_share_pct"], 100.0)

    def test_bitquery_call_meters_quota(self):
        O.enumerate_holders("0xc0ffee", "binance-smart-chain")
        self.assertEqual(self.calls["quota"], ["bitquery"])

    def test_cache_hit_within_ttl_does_not_call_again(self):
        O.enumerate_holders("0xc0ffee", "binance-smart-chain", top_n=10)
        O.enumerate_holders("0xc0ffee", "binance-smart-chain", top_n=10)
        self.assertEqual(len(self.calls["bitquery"]), 1)

    def test_eth_chain_supported(self):
        out = O.enumerate_holders("0xc0ffee", "ethereum", top_n=10)
        self.assertEqual(out["source"], "bitquery")

    def test_unsupported_chain_skips_bitquery_to_goplus(self):
        out = O.enumerate_holders("0xc0ffee", "base", top_n=10)
        self.assertEqual(out["source"], "goplus")
        self.assertEqual(self.calls["bitquery"], [])


class TestGoplusFallback(SeamBase):
    def test_bitquery_error_falls_through_to_goplus_envelope_unchanged(self):
        self.bq_response = Exception("boom")
        out = O.enumerate_holders("0xc0ffee", "binance-smart-chain", top_n=10)
        self.assertTrue(out["available"])
        self.assertEqual(out["source"], "goplus")
        self.assertEqual(len(out["holders"]), 10)
        self.assertEqual(out["top10_share_pct"], 78.5)

    def test_bitquery_timeout_falls_through(self):
        self.bq_response = TimeoutError("timed out")
        out = O.enumerate_holders("0xc0ffee", "binance-smart-chain", top_n=10)
        self.assertEqual(out["source"], "goplus")

    def test_bitquery_graphql_errors_fall_through(self):
        self.bq_response = {"errors": [{"message": "quota exhausted"}]}
        out = O.enumerate_holders("0xc0ffee", "binance-smart-chain", top_n=10)
        self.assertEqual(out["source"], "goplus")

    def test_no_key_behaves_byte_identical_to_goplus_only(self):
        O._bitquery_key = lambda: None
        out = O.enumerate_holders("0xc0ffee", "binance-smart-chain", top_n=10)
        self.assertEqual(self.calls["bitquery"], [])
        self.assertEqual(out["source"], "goplus")
        self.assertEqual(out["holders"][0]["address"], "0xgp0")
        self.assertEqual(out["holders"][0]["share_pct"], 40.0)
        self.assertIsNone(out["holders"][0]["balance"])

    def test_goplus_caps_at_10_and_flags_partial_for_larger_top_n(self):
        O._bitquery_key = lambda: None
        out = O.enumerate_holders("0xc0ffee", "binance-smart-chain", top_n=50)
        self.assertEqual(len(out["holders"]), 10)
        self.assertTrue(out["partial"])

    def test_both_providers_fail_returns_unavailable(self):
        self.bq_response = Exception("boom")
        self.goplus_response = GOPLUS_CONCENTRATION_UNAVAILABLE
        out = O.enumerate_holders("0xc0ffee", "binance-smart-chain", top_n=10)
        self.assertFalse(out["available"])
        self.assertIn("reason", out)


class TestHolderCfg(unittest.TestCase):
    def test_default_cache_ttl_and_budget(self):
        cfg = O._holder_cfg()
        self.assertEqual(cfg["cache_ttl_h"], 6)
        self.assertEqual(cfg["daily_budget"], 800)

    def test_config_file_overrides_defaults(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "holder_enumeration.json"
            p.write_text(json.dumps({"cache_ttl_h": 2}))
            orig = O._HOLDER_CFG_PATH
            O._HOLDER_CFG_PATH = p
            try:
                cfg = O._holder_cfg()
                self.assertEqual(cfg["cache_ttl_h"], 2)
                self.assertEqual(cfg["daily_budget"], 800)   # untouched key keeps its default
            finally:
                O._HOLDER_CFG_PATH = orig

    def test_default_daily_budget_registered_in_provider_quota(self):
        self.assertEqual(O.provider_quota.budget_for("bitquery"), 800)

    def test_daily_budget_env_overridable(self):
        os.environ["BITQUERY_DAILY_BUDGET"] = "123"
        try:
            self.assertEqual(O.provider_quota.budget_for("bitquery"), 123)
        finally:
            os.environ.pop("BITQUERY_DAILY_BUDGET", None)


class TestRegistryDrivenSocket(SeamBase):
    def test_a_provider_can_register_ahead_of_bitquery(self):
        calls = []

        def fake(contract, chain_key, top_n):
            calls.append((contract, chain_key, top_n))
            return [{"address": "0xindexed", "balance": 1.0, "share_pct": 100.0}], 100.0, False

        O.register_holder_provider("local-indexer", fake, chains={"binance-smart-chain"}, before="bitquery")
        try:
            out = O.enumerate_holders("0xc0ffee", "binance-smart-chain", top_n=10)
            self.assertEqual(out["source"], "local-indexer")
            self.assertEqual(self.calls["bitquery"], [])
        finally:
            O.unregister_holder_provider("local-indexer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
