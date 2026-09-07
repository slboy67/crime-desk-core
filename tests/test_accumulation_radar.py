#!/usr/bin/env python3
"""SPEC-76 — operator accumulation radar (wallet-centric early-long signal).

The LONG-side mirror of distribution_radar: watch the mapped operator cluster wallets
ACCUMULATE a NEW untracked token, classify active-load vs parked-allocation, and cross with
perp construction to tier IMMINENT / EARLY / PARK. All network is mocked — deterministic.
"""
import importlib.util
import json
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("accumulation_radar", ROOT / "capabilities" / "accumulation_radar.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)


def _iso_ago(secs):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time() - secs, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class TestClusterEnumeration(unittest.TestCase):
    """Req 1: wallet-centric enumeration of the mapped operator cluster — dedup an address
    that holds positions across many tokens (the 0x73d8 cross-Cat-A escrow) and flag it."""

    def test_cross_cluster_escrow_is_deduped_and_flagged(self):
        cfg = {"tokens": {
            "RAVE": {"wallets": [{"label": "ESCROW", "address": "0x73d8", "tier": "distribution",
                                  "chain": "binance-smart-chain"}]},
            "SKYAI": {"wallets": [{"label": "ESCROW", "address": "0x73d8", "tier": "distribution",
                                   "chain": "binance-smart-chain"},
                                  {"label": "TEAM-A", "address": "0xaaa", "tier": "team",
                                   "chain": "binance-smart-chain"}]},
            "AIOT": {"wallets": [{"label": "ESCROW", "address": "0x73d8", "tier": "distribution",
                                  "chain": "binance-smart-chain"}]},
        }}
        wallets = A.operator_cluster_wallets(cfg)
        by_addr = {w["address"]: w for w in wallets}
        self.assertIn("0x73d8", by_addr)                       # deduped to ONE entry
        self.assertEqual(len([w for w in wallets if w["address"] == "0x73d8"]), 1)
        self.assertGreaterEqual(len(by_addr["0x73d8"]["tokens"]), 3)   # holds across 3 tokens
        self.assertTrue(by_addr["0x73d8"]["is_escrow"])        # multi-token → cross-cluster escrow
        self.assertIn("0xaaa", by_addr)                        # a team wallet is in the cluster too
        self.assertFalse(by_addr["0xaaa"]["is_escrow"])

    def test_cex_and_burn_tiers_excluded(self):
        cfg = {"tokens": {"X": {"wallets": [
            {"label": "CEX", "address": "0xcex", "tier": "cex", "chain": "binance-smart-chain"},
            {"label": "BURN", "address": "0xburn", "tier": "burn", "chain": "binance-smart-chain"},
            {"label": "TEAM", "address": "0xteam", "tier": "team", "chain": "binance-smart-chain"}]}}}
        addrs = {w["address"] for w in A.operator_cluster_wallets(cfg)}
        self.assertEqual(addrs, {"0xteam"})


class TestActiveVsParked(unittest.TestCase):
    """Req 3: distinguish ACTIVE accumulation (DEX swap / CEX withdrawal / open-market buy =
    tradeable load) from PASSIVE receipt (inbound from a vesting/team/escrow contract =
    allocation parked, NOT a pre-pump signal). Reuses verify_wallet funded_by/source logic."""

    def test_dex_swap_acquisition_is_active(self):
        v = {"available": True, "seeded_staging": False, "funded_by_kind": "dex",
             "accumulating": True, "recent_net": 500000,
             "funded_by": [{"kind": "dex", "label": "PancakeRouter"}]}
        mode, _reason = A.classify_accumulation(v)
        self.assertEqual(mode, "active")

    def test_cex_withdrawal_is_active(self):
        v = {"available": True, "seeded_staging": False, "funded_by_kind": "cex-withdrawal",
             "accumulating": False, "funded_by": [{"kind": "cex", "label": "Binance"}]}
        mode, _ = A.classify_accumulation(v)
        self.assertEqual(mode, "active")

    def test_vesting_escrow_receipt_is_parked(self):
        # inbound from a tracked team/escrow safe → seeded_staging True → parked allocation
        v = {"available": True, "seeded_staging": True, "funded_by_kind": "seeded",
             "accumulating": False, "funded_by": [{"kind": "tracked-safe", "label": "VESTING-ESCROW"}]}
        mode, _ = A.classify_accumulation(v)
        self.assertEqual(mode, "parked")


class TestConfluenceTier(unittest.TestCase):
    """Req 4: cross on-chain accumulation with PERP construction (the timing key)."""

    def test_active_plus_perp_firing_is_imminent(self):
        perp = {"has_perp": True, "deep_neg": True, "oi_building": True, "firing": True}
        self.assertEqual(A.confluence_tier("active", perp), "IMMINENT")

    def test_active_but_perp_dormant_is_early(self):
        perp = {"has_perp": True, "deep_neg": False, "oi_building": False, "firing": False}
        self.assertEqual(A.confluence_tier("active", perp), "EARLY")

    def test_active_but_no_perp_is_early(self):
        perp = {"has_perp": False, "firing": False}
        self.assertEqual(A.confluence_tier("active", perp), "EARLY")

    def test_parked_and_no_perp_is_park(self):
        perp = {"has_perp": False, "firing": False}
        self.assertEqual(A.confluence_tier("parked", perp), "PARK")


class TestPerpState(unittest.TestCase):
    """Perp construction firing = a live perp + deep-neg trap-formation funding (§4) + OI
    building (vs an OI baseline when known, else OI present)."""

    def test_deep_neg_with_oi_building_fires(self):
        perp_fn = lambda t: {"funding_4h": -1.2, "oi": 2_000_000}
        st = A.perp_state("LOADX", perp_fn=perp_fn, oi_baseline=1_000_000)
        self.assertTrue(st["has_perp"])
        self.assertTrue(st["deep_neg"])
        self.assertTrue(st["oi_building"])
        self.assertTrue(st["firing"])

    def test_flat_funding_does_not_fire(self):
        perp_fn = lambda t: {"funding_4h": 0.01, "oi": 2_000_000}
        st = A.perp_state("X", perp_fn=perp_fn, oi_baseline=1_000_000)
        self.assertFalse(st["deep_neg"])
        self.assertFalse(st["firing"])

    def test_no_perp_does_not_fire(self):
        st = A.perp_state("X", perp_fn=lambda t: None)
        self.assertFalse(st["has_perp"])
        self.assertFalse(st["firing"])


class TestWalletInbounds(unittest.TestCase):
    """Req 1/2: enumerate a wallet's ERC20 inbounds (snapshot), grouped per token, so the diff
    can flag a NEW untracked position."""

    def test_groups_inbound_by_token(self):
        wallet = "0xwallet"
        txs = [
            {"address": "0xtokA", "token_symbol": "AAA", "from_address": "0xdex", "to_address": wallet,
             "value_decimal": "1000", "block_timestamp": _iso_ago(3600), "_chain": "binance-smart-chain"},
            {"address": "0xtokA", "token_symbol": "AAA", "from_address": "0xdex", "to_address": wallet,
             "value_decimal": "500", "block_timestamp": _iso_ago(7200), "_chain": "binance-smart-chain"},
            {"address": "0xtokB", "token_symbol": "BBB", "from_address": "0xvest", "to_address": wallet,
             "value_decimal": "9", "block_timestamp": _iso_ago(60), "_chain": "binance-smart-chain"},
            # an OUTbound transfer (from the wallet) must NOT count as accumulation
            {"address": "0xtokA", "token_symbol": "AAA", "from_address": wallet, "to_address": "0xother",
             "value_decimal": "200", "block_timestamp": _iso_ago(30), "_chain": "binance-smart-chain"},
        ]
        holds = A.wallet_token_inbounds(wallet, ["binance-smart-chain"], 30,
                                        tokentx_fn=lambda a, chains, days: txs)
        self.assertEqual(holds["0xtoka"]["in_amount"], 1500)
        self.assertEqual(holds["0xtoka"]["n_in"], 2)
        self.assertEqual(holds["0xtokb"]["in_amount"], 9)
        self.assertEqual(holds["0xtoka"]["last_in_from"], "0xdex")


class TestUntrackedFilter(unittest.TestCase):
    def test_tracked_token_contract_excluded(self):
        tracked = {"0xtoka"}        # AAA is already a tracked/watchlist token
        self.assertFalse(A.is_accumulation_candidate("0xtoka", tracked))
        self.assertTrue(A.is_accumulation_candidate("0xnew", tracked))


class TestBaselineDiff(unittest.TestCase):
    """Req 2: snapshot + diff vs a stored baseline — flag a NEW position or one growing >N%."""

    def test_new_and_grown_positions_flagged(self):
        baseline = {"0xold": {"in_amount": 1000}}
        holds = {"0xold": {"in_amount": 1600},   # +60% > GROWTH_PCT → GROWN
                 "0xnew": {"in_amount": 50}}      # not in baseline → NEW
        diff = A.diff_holdings(baseline, holds)
        kinds = {d["contract"]: d["change"] for d in diff}
        self.assertEqual(kinds["0xnew"], "NEW")
        self.assertEqual(kinds["0xold"], "GROWN")

    def test_flat_position_not_flagged(self):
        baseline = {"0xold": {"in_amount": 1000}}
        holds = {"0xold": {"in_amount": 1010}}    # +1% < GROWTH_PCT → not flagged
        self.assertEqual(A.diff_holdings(baseline, holds), [])


class TestBacktestLeadTime(unittest.TestCase):
    """DoD backtest gate (the go/no-go): for the cluster tokens, did the wallets accumulate
    BEFORE each pump, and with what lead-time? Honest either way — this pure function computes
    the lead-time given the (accumulation, pump) event pairs the live harness gathers."""

    def test_lead_time_and_honest_summary(self):
        # token P1: accumulated 5 days before the pump (predictive); P2: accumulation LAGGED the
        # pump (not predictive); P3: no accumulation found at all.
        accum = {"P1": _iso_ago(10 * 86400), "P2": _iso_ago(1 * 86400)}
        pump = {"P1": _iso_ago(5 * 86400), "P2": _iso_ago(3 * 86400), "P3": _iso_ago(2 * 86400)}
        bt = A.backtest_lead_time(["P1", "P2", "P3"], accum, pump)
        rows = {r["token"]: r for r in bt["rows"]}
        self.assertTrue(rows["P1"]["accumulated_before"])
        self.assertAlmostEqual(rows["P1"]["lead_days"], 5, delta=0.1)
        self.assertFalse(rows["P2"]["accumulated_before"])      # accumulation lagged the pump
        self.assertIsNone(rows["P3"]["lead_days"])              # no accumulation found
        # summary is honest: 1 of 3 had a tradeable accumulation lead
        self.assertEqual(bt["n_tokens"], 3)
        self.assertEqual(bt["n_accumulated_before"], 1)


class TestBuildRadarEndToEnd(unittest.TestCase):
    """The composed radar: enumerate cluster wallets → inbounds → untracked candidates →
    active/parked → perp cross → ranked IMMINENT/EARLY/PARK candidates."""

    def setUp(self):
        self._orig = (A._load_cfg, A._tracked_contracts, A._load_baseline, A._save_baseline)
        A._save_baseline = lambda *a, **k: None
        A._load_baseline = lambda addr: {}        # cold baseline → all inbounds are NEW
        A._load_cfg = lambda: {"tokens": {
            "RAVE": {"wallets": [{"label": "ESCROW", "address": "0x73d8", "tier": "distribution",
                                  "chain": "binance-smart-chain"}]}}}
        A._tracked_contracts = lambda cfg: {"0xrave"}      # RAVE itself is tracked

    def tearDown(self):
        (A._load_cfg, A._tracked_contracts, A._load_baseline, A._save_baseline) = self._orig

    def test_imminent_candidate_surfaces_with_fields(self):
        # the escrow buys a NEW untracked token LOADX via a DEX swap, and LOADX perp is firing
        def tokentx_fn(addr, chains, days):
            return [{"address": "0xloadx", "token_symbol": "LOADX", "from_address": "0xdex",
                     "to_address": "0x73d8", "value_decimal": "1000000",
                     "block_timestamp": _iso_ago(2 * 3600), "_chain": "binance-smart-chain"}]

        def verify_fn(addr, token, contract=None, chain=None):
            return {"available": True, "seeded_staging": False, "funded_by_kind": "dex",
                    "accumulating": True, "recent_net": 1000000,
                    "funded_by": [{"kind": "dex", "label": "PancakeRouter"}],
                    "balance_now": {"available": True, "value": 1000000, "pct_supply": 4.2}}

        perp_fn = lambda t: {"funding_4h": -1.5, "oi": 3_000_000} if t == "LOADX" else None

        r = A.build_accumulation_radar(tokentx_fn=tokentx_fn, verify_fn=verify_fn,
                                       perp_fn=perp_fn, oi_baseline_fn=lambda t: 1_000_000)
        cands = r["candidates"]
        self.assertTrue(any(c["tier"] == "IMMINENT" for c in cands), cands)
        c = next(c for c in cands if c["tier"] == "IMMINENT")
        self.assertEqual(c["token"], "LOADX")
        self.assertEqual(c["wallet"], "0x73d8")
        self.assertEqual(c["mode"], "active")
        self.assertEqual(c["pct_supply"], 4.2)
        self.assertTrue(c["perp"]["firing"])
        self.assertEqual(c["change"], "NEW")

    def test_parked_allocation_is_not_imminent(self):
        # the escrow RECEIVES a new token from a vesting contract, no perp → PARK, not a long
        def tokentx_fn(addr, chains, days):
            return [{"address": "0xparkx", "token_symbol": "PARKX", "from_address": "0xvesting",
                     "to_address": "0x73d8", "value_decimal": "5000000",
                     "block_timestamp": _iso_ago(3600), "_chain": "binance-smart-chain"}]

        def verify_fn(addr, token, contract=None, chain=None):
            return {"available": True, "seeded_staging": True, "funded_by_kind": "seeded",
                    "accumulating": False, "funded_by": [{"kind": "tracked-safe", "label": "VESTING"}],
                    "balance_now": {"available": True, "value": 5000000, "pct_supply": 10.0}}

        r = A.build_accumulation_radar(tokentx_fn=tokentx_fn, verify_fn=verify_fn,
                                       perp_fn=lambda t: None, oi_baseline_fn=lambda t: None)
        cands = r["candidates"]
        c = next(c for c in cands if c["token"] == "PARKX")
        self.assertEqual(c["mode"], "parked")
        self.assertEqual(c["tier"], "PARK")
        self.assertNotEqual(c["tier"], "IMMINENT")

    def test_ticker_scope_filters_to_matching_token_same_shape(self):
        """SPEC-174 #1: an off-board Cat-A name has no on-chain radar leg without this — the
        cluster is still swept in full (there's no way to know which wallet holds it up
        front), but the RESULT is scoped to just that token, same output shape."""
        def tokentx_fn(addr, chains, days):
            return [
                {"address": "0xloadx", "token_symbol": "LOADX", "from_address": "0xdex",
                 "to_address": "0x73d8", "value_decimal": "1000000",
                 "block_timestamp": _iso_ago(2 * 3600), "_chain": "binance-smart-chain"},
                {"address": "0xparkx", "token_symbol": "PARKX", "from_address": "0xvesting",
                 "to_address": "0x73d8", "value_decimal": "5000000",
                 "block_timestamp": _iso_ago(3600), "_chain": "binance-smart-chain"},
            ]

        def verify_fn(addr, token, contract=None, chain=None):
            if token == "LOADX":
                return {"available": True, "seeded_staging": False, "funded_by_kind": "dex",
                        "accumulating": True, "recent_net": 1000000,
                        "funded_by": [{"kind": "dex", "label": "PancakeRouter"}],
                        "balance_now": {"available": True, "value": 1000000, "pct_supply": 4.2}}
            return {"available": True, "seeded_staging": True, "funded_by_kind": "seeded",
                    "accumulating": False, "funded_by": [{"kind": "tracked-safe", "label": "VESTING"}],
                    "balance_now": {"available": True, "value": 5000000, "pct_supply": 10.0}}

        perp_fn = lambda t: {"funding_4h": -1.5, "oi": 3_000_000} if t == "LOADX" else None

        r = A.build_accumulation_radar(ticker="loadx", tokentx_fn=tokentx_fn, verify_fn=verify_fn,
                                       perp_fn=perp_fn, oi_baseline_fn=lambda t: 1_000_000)
        self.assertEqual(r["ticker"], "loadx")
        self.assertNotIn("not_found", r)
        self.assertEqual({c["token"] for c in r["candidates"]}, {"LOADX"})
        self.assertEqual(set(r), {"scanned_wallets", "n_wallets", "partial", "candidates",
                                  "imminent", "early", "n_candidates", "caveats", "ticker"})
        # the cluster is still fully scanned — scoping happens on the RESULT, not the sweep
        self.assertEqual(r["n_wallets"], 1)

    def test_ticker_scope_no_match_is_not_found_not_empty_sweep(self):
        def tokentx_fn(addr, chains, days):
            return [{"address": "0xloadx", "token_symbol": "LOADX", "from_address": "0xdex",
                     "to_address": "0x73d8", "value_decimal": "1000000",
                     "block_timestamp": _iso_ago(2 * 3600), "_chain": "binance-smart-chain"}]

        def verify_fn(addr, token, contract=None, chain=None):
            return {"available": True, "seeded_staging": False, "funded_by_kind": "dex",
                    "accumulating": True, "recent_net": 1000000,
                    "funded_by": [{"kind": "dex", "label": "PancakeRouter"}],
                    "balance_now": {"available": True, "value": 1000000, "pct_supply": 4.2}}

        r = A.build_accumulation_radar(ticker="NOPE", tokentx_fn=tokentx_fn, verify_fn=verify_fn,
                                       perp_fn=lambda t: None, oi_baseline_fn=lambda t: None)
        self.assertEqual(r["candidates"], [])
        self.assertTrue(r["not_found"])
        self.assertEqual(r["n_wallets"], 1)   # the sweep itself still ran


if __name__ == "__main__":
    unittest.main(verbosity=2)
