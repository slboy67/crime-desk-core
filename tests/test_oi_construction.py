#!/usr/bin/env python3
"""SPEC-179 — oi_construction: OI-type decomposition + venue roles.

Run:  python3 -m unittest tests.test_oi_construction -v

Offline-deterministic — every live call (`venue_map_fn`, `battlefield_fn`,
`spot_resolver`, `mark_constituents_fn`) is injected at `build_oi_construction`;
`chip_state`/`flow_confirmed`/`lock_info` are the EXISTING-state injection points
per the Boundaries section (never a fresh Moralis call — asserted explicitly below).
"""
import importlib.util
import inspect
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("oi_construction", ROOT / "capabilities" / "oi_construction.py")
OC = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OC)


def _vm_stub(venues=None, total_oi_usd=0.0, total_vol24h_usd=0.0, funding_extreme=None):
    return lambda t: {"ticker": t, "venues": venues or {}, "total_oi_usd": total_oi_usd,
                      "total_vol24h_usd": total_vol24h_usd, "funding_extreme": funding_extreme}


def _no_spot():
    return lambda t: (None, None, None)


def _bf_unknown():
    return lambda **kw: {"battlefield": "UNKNOWN", "leverage_state": {}}


# ---------------------------------------------------------------------------
# OI type mandatory-gate discipline
# ---------------------------------------------------------------------------
class TestFundingFarm(unittest.TestCase):
    def test_viability_alone_no_supporting_signal_is_unknown(self):
        r = OC.evaluate_funding_farm(funding_pi_4h=0.5)   # deep positive, viable APR
        self.assertEqual(r["mandatory_met"], True)
        self.assertEqual(r["verdict"], "UNKNOWN")
        self.assertEqual(r["reason"], "farm_viable_unconfirmed")

    def test_viable_plus_one_supporting_signal_asserts(self):
        r = OC.evaluate_funding_farm(funding_pi_4h=0.5, elasticity_confirmed=True)
        self.assertEqual(r["verdict"], "ASSERTED")
        self.assertEqual(r["side"], "short")

    def test_not_viable_carry_is_not_asserted(self):
        r = OC.evaluate_funding_farm(funding_pi_4h=0.001, elasticity_confirmed=True)
        self.assertEqual(r["verdict"], "NOT_ASSERTED")

    def test_neg_funding_variant_side_is_long(self):
        r = OC.evaluate_funding_farm(funding_pi_4h=-0.5, elasticity_confirmed=True)
        self.assertEqual(r["side"], "long")
        self.assertEqual(r["verdict"], "ASSERTED")

    def test_missing_funding_is_unknown(self):
        r = OC.evaluate_funding_farm(funding_pi_4h=None)
        self.assertEqual(r["verdict"], "UNKNOWN")


class TestVestingHedge(unittest.TestCase):
    def test_missing_lock_not_asserted_even_with_perfect_tape(self):
        # "perfect §4-B tape" = every supporting signal present, but no lock contract
        r = OC.evaluate_vesting_hedge(lock_cliff=None, carry_violation=True,
                                      perp_discount=True, pristine_chain=True)
        self.assertEqual(r["verdict"], "NOT_ASSERTED")
        self.assertEqual(r["mandatory_met"], False)

    def test_lock_present_asserts_with_cliff_date(self):
        r = OC.evaluate_vesting_hedge(lock_cliff="2026-12-01", carry_violation=True,
                                      perp_discount=True)
        self.assertEqual(r["verdict"], "ASSERTED")
        self.assertEqual(r["cliff_date"], "2026-12-01")


class TestOperatorAmm(unittest.TestCase):
    def test_quota_dead_is_unknown_unassertable_never_fabricated(self):
        r = OC.evaluate_operator_amm(chip_control=True, quota_dead=True)
        self.assertEqual(r["verdict"], "UNKNOWN")
        self.assertIn("quota-dead", r["reason"])

    def test_chip_control_none_is_unknown(self):
        r = OC.evaluate_operator_amm(chip_control=None)
        self.assertEqual(r["verdict"], "UNKNOWN")

    def test_chip_control_present_with_signals_asserts(self):
        r = OC.evaluate_operator_amm(chip_control=True, deep_neg_at_highs=True,
                                     no_free_spot=True, no_lock_contract=True)
        self.assertEqual(r["verdict"], "ASSERTED")
        self.assertEqual(r["share_read"], "dominant")   # 3 supporting signals


class TestMmInventory(unittest.TestCase):
    def test_neutral_top_book_asserts(self):
        r = OC.evaluate_mm_inventory(top_book_neutral=True, oi_insensitive_to_funding=True)
        self.assertEqual(r["verdict"], "ASSERTED")

    def test_non_neutral_not_asserted(self):
        r = OC.evaluate_mm_inventory(top_book_neutral=False)
        self.assertEqual(r["verdict"], "NOT_ASSERTED")

    def test_unreadable_is_unknown(self):
        r = OC.evaluate_mm_inventory(top_book_neutral=None)
        self.assertEqual(r["verdict"], "UNKNOWN")


class TestCrossVenueArb(unittest.TestCase):
    def test_dispersion_present_asserts_and_sets_overstated_flag(self):
        r = OC.evaluate_cross_venue_arb(dispersion=True, oi_elevated_both=True, price_inelastic=True)
        self.assertEqual(r["verdict"], "ASSERTED")
        self.assertTrue(r["overstated_by_cross_venue"])

    def test_no_dispersion_not_asserted(self):
        r = OC.evaluate_cross_venue_arb(dispersion=False)
        self.assertEqual(r["verdict"], "NOT_ASSERTED")


# ---------------------------------------------------------------------------
# Roll-up (G3)
# ---------------------------------------------------------------------------
class TestRollUp(unittest.TestCase):
    def test_dominant_nondirectional_is_arb_dominated(self):
        types = [{"type": "FUNDING_FARM", "verdict": "ASSERTED", "share_read": "dominant"},
                {"type": "VESTING_HEDGE", "verdict": "NOT_ASSERTED"},
                {"type": "OPERATOR_AMM", "verdict": "NOT_ASSERTED"},
                {"type": "MM_INVENTORY", "verdict": "NOT_ASSERTED"},
                {"type": "CROSS_VENUE_FUNDING_ARB", "verdict": "NOT_ASSERTED"}]
        self.assertEqual(OC.roll_up_verdict(types), "ARB_DOMINATED")

    def test_operator_amm_asserted_forces_at_least_mixed(self):
        types = [{"type": "FUNDING_FARM", "verdict": "NOT_ASSERTED"},
                {"type": "VESTING_HEDGE", "verdict": "NOT_ASSERTED"},
                {"type": "OPERATOR_AMM", "verdict": "ASSERTED", "share_read": "minor"},
                {"type": "MM_INVENTORY", "verdict": "NOT_ASSERTED"},
                {"type": "CROSS_VENUE_FUNDING_ARB", "verdict": "NOT_ASSERTED"}]
        self.assertEqual(OC.roll_up_verdict(types), "MIXED")

    def test_all_ran_none_asserted_is_directional(self):
        types = [{"type": t, "verdict": "NOT_ASSERTED"} for t in
                ("FUNDING_FARM", "VESTING_HEDGE", "OPERATOR_AMM", "MM_INVENTORY",
                 "CROSS_VENUE_FUNDING_ARB")]
        self.assertEqual(OC.roll_up_verdict(types), "DIRECTIONAL")

    def test_nothing_ran_is_unknown(self):
        types = [{"type": t, "verdict": "UNKNOWN"} for t in
                ("FUNDING_FARM", "VESTING_HEDGE", "OPERATOR_AMM", "MM_INVENTORY",
                 "CROSS_VENUE_FUNDING_ARB")]
        self.assertEqual(OC.roll_up_verdict(types), "UNKNOWN")


# ---------------------------------------------------------------------------
# Venue roles (G2)
# ---------------------------------------------------------------------------
class TestVenueRoles(unittest.TestCase):
    def test_mark_engine_split_on_two_venues_over_threshold(self):
        r = OC.resolve_mark_engine({"binance": 43.5, "okx": 25.0, "gate": 4.3})
        self.assertEqual(r["verdict"], "SPLIT")
        self.assertEqual(r["venues"], ["binance", "okx"])

    def test_mark_engine_unknown_when_unpublished(self):
        r = OC.resolve_mark_engine(None)
        self.assertEqual(r["verdict"], "UNKNOWN")

    def test_size_book_crowns_winner_over_40pct(self):
        r = OC.resolve_size_book({"binance": 60.0, "bybit": 20.0, "aster": 20.0})
        self.assertEqual(r["verdict"], "binance")

    def test_size_book_split_within_15_points(self):
        r = OC.resolve_size_book({"binance": 45.0, "bybit": 40.0})
        self.assertEqual(r["verdict"], "SPLIT")

    def test_size_book_errored_plausible_venue_is_unknown(self):
        r = OC.resolve_size_book({"binance": 60.0}, errored_plausible=True)
        self.assertEqual(r["verdict"], "UNKNOWN")

    def test_exit_flow_confirmed_beats_depth_inferred(self):
        r = OC.resolve_exit(flow_confirmed={"venue": "bitget", "age_days": 3},
                            depth_venue="binance", depth_vol_usd=1e6)
        self.assertEqual(r["grade"], "FLOW_CONFIRMED")
        self.assertEqual(r["venue"], "bitget")

    def test_exit_flow_older_than_14d_falls_back_to_depth(self):
        r = OC.resolve_exit(flow_confirmed={"venue": "bitget", "age_days": 20},
                            depth_venue="binance", depth_vol_usd=1e6)
        self.assertEqual(r["grade"], "DEPTH_INFERRED")

    def test_exit_no_flow_no_spot_is_unknown_perp_only_flag(self):
        r = OC.resolve_exit()
        self.assertEqual(r["grade"], "UNKNOWN")
        self.assertIn("perp-only", r["reason"])

    def test_hedge_outlier_with_evidence_asserts(self):
        r = OC.resolve_hedge(is_outlier=True, elevated_oi_share=True)
        self.assertEqual(r["verdict"], "ASSERTED")

    def test_hedge_no_outlier_is_none_detected_not_unknown(self):
        r = OC.resolve_hedge(is_outlier=False)
        self.assertEqual(r["verdict"], "NONE_DETECTED")

    def test_hedge_unreadable_is_unknown(self):
        r = OC.resolve_hedge(is_outlier=None)
        self.assertEqual(r["verdict"], "UNKNOWN")


class TestGatingOk(unittest.TestCase):
    def test_fresh_roles_gating_ok_true(self):
        self.assertTrue(OC.compute_gating_ok(roles_as_of_ts=1000, now_ts=1000 + 3600))

    def test_stale_roles_over_4h_gating_ok_false(self):
        self.assertFalse(OC.compute_gating_ok(roles_as_of_ts=1000, now_ts=1000 + 4 * 3600 + 1))


# ---------------------------------------------------------------------------
# build_oi_construction — the DoD fixtures, verbatim
# ---------------------------------------------------------------------------
class TestBuildOiConstructionFixtures(unittest.TestCase):
    def test_river_shaped_fixture_vesting_hedge_asserted_with_cliff_verdict_ge_mixed(self):
        r = OC.build_oi_construction(
            "RIVER",
            venue_map_fn=_vm_stub(funding_extreme={"venue": "binance", "pi_4h": -1.5}),
            spot_resolver=_no_spot(), battlefield_fn=_bf_unknown(),
            lock_info={"cliff_date": "2026-12-01", "carry_violation": True,
                      "perp_discount": True, "pristine_chain": True})
        vh = next(t for t in r["oi_types"] if t["type"] == "VESTING_HEDGE")
        self.assertEqual(vh["verdict"], "ASSERTED")
        self.assertEqual(vh["cliff_date"], "2026-12-01")
        self.assertIn(r["verdict"], ("MIXED", "ARB_DOMINATED"))

    def test_lab_shaped_fixture_operator_amm_mixed_ratio_signals_downgraded(self):
        r = OC.build_oi_construction(
            "LAB",
            venue_map_fn=_vm_stub(funding_extreme={"venue": "aster", "pi_4h": -2.0}),
            spot_resolver=_no_spot(), battlefield_fn=_bf_unknown(),
            chip_state={"chip_control": True, "deep_neg_at_highs": True, "no_free_spot": True,
                       "elasticity_inputs": {"account_ratio_divergence": True}},
            lock_info=None)
        oa = next(t for t in r["oi_types"] if t["type"] == "OPERATOR_AMM")
        self.assertEqual(oa["verdict"], "ASSERTED")
        self.assertEqual(r["verdict"], "MIXED")
        # contamination: account-ratio evidence withheld from FUNDING_FARM
        ff = next(t for t in r["oi_types"] if t["type"] == "FUNDING_FARM")
        self.assertNotIn("account_ratio_divergence", [e["signal"] for e in ff["evidence"]])
        self.assertTrue(any("downgraded" in d.get("reason", "") for d in r["degraded"]))

    def test_missing_lock_fixture_with_perfect_tape_vesting_hedge_not_asserted(self):
        r = OC.build_oi_construction(
            "X", venue_map_fn=_vm_stub(), spot_resolver=_no_spot(), battlefield_fn=_bf_unknown(),
            lock_info=None)
        vh = next(t for t in r["oi_types"] if t["type"] == "VESTING_HEDGE")
        self.assertEqual(vh["verdict"], "NOT_ASSERTED")

    def test_quota_dead_fixture_operator_amm_unassertable_and_degraded_row(self):
        r = OC.build_oi_construction(
            "X", venue_map_fn=_vm_stub(), spot_resolver=_no_spot(), battlefield_fn=_bf_unknown(),
            chip_state={"chip_control": True, "quota_dead": True})
        oa = next(t for t in r["oi_types"] if t["type"] == "OPERATOR_AMM")
        self.assertEqual(oa["verdict"], "UNKNOWN")
        self.assertTrue(any("quota-dead" in d.get("reason", "") for d in r["degraded"]))
        # nothing fabricated: no evidence entries invented
        self.assertEqual(oa["evidence"], [])

    def test_stale_roles_fixture_gating_ok_false_roles_still_printed(self):
        r = OC.build_oi_construction(
            "X", venue_map_fn=_vm_stub(), spot_resolver=_no_spot(), battlefield_fn=_bf_unknown(),
            now_ts=100000, roles_as_of_ts=100000 - 5 * 3600)
        self.assertFalse(r["gating_ok"])
        self.assertIn("mark_engine", r["venue_roles"])
        self.assertIn("size_book", r["venue_roles"])

    def test_no_moralis_call_in_the_test_run(self):
        """Assert via the fetch seam: the module never imports anything Moralis-
        touching (verify_wallet/onchain, both Moralis-backed) — chip_state/
        flow_confirmed/lock_info are the ONLY on-chain injection points, so there is
        no code path in this module that could initiate a Moralis call."""
        self.assertFalse(hasattr(OC, "verify_wallet"))
        self.assertFalse(hasattr(OC, "onchain"))
        import_lines = [l for l in inspect.getsource(OC).splitlines()
                        if l.strip().startswith(("import ", "from "))]
        self.assertFalse(any("verify_wallet" in l or "onchain" in l for l in import_lines),
                         import_lines)

    def test_default_call_never_raises_even_with_zero_injection(self):
        # every optional param defaults to a live fetcher; injecting stubs for all of
        # them (the network-touching ones) must still produce a complete, well-typed
        # envelope with no live network in this test.
        r = OC.build_oi_construction(
            "X", venue_map_fn=_vm_stub(), spot_resolver=_no_spot(), battlefield_fn=_bf_unknown(),
            mark_constituents_fn=lambda t: None,
            elasticity_fn=lambda t, **kw: {"elasticity_confirmed": None, "n_settlements": 0,
                                           "coverage_pct": 0.0, "degraded_reason": None},
            lock_info_fn=lambda t, **kw: (None, False))
        for k in ("ticker", "as_of", "battlefield", "venue_roles", "oi_types", "verdict",
                 "aggregate", "degraded", "gating_ok"):
            self.assertIn(k, r)
        self.assertEqual(len(r["oi_types"]), 5)


# ---------------------------------------------------------------------------
# _elasticity_from_store (SPEC-182 req 1) — sampler-store elasticity floor
# ---------------------------------------------------------------------------
def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class TestElasticityFromStore(unittest.TestCase):
    def test_missing_file_is_no_history(self):
        with tempfile.TemporaryDirectory() as td:
            r = OC._elasticity_from_store("NOFILE", store_dir=Path(td), now_ts=100000)
            self.assertIsNone(r["elasticity_confirmed"])
            self.assertEqual(r["n_settlements"], 0)
            self.assertEqual(r["degraded_reason"], "no_history")

    def test_floor_met_with_elastic_response_confirms_true(self):
        # 12x 1h buckets (window=12h, bucket_min=60); hours 0-9 covered (10/12
        # =83.3% >=70%), hours 10-11 gapped. 4 funding-value changes (settlements),
        # each with oi_raw moving the SAME direction as the funding magnitude
        # change (elastic/concordant response) -> elasticity_confirmed=True.
        now_ts = 100000
        start = now_ts - 12 * 3600
        rows = []
        seq = [(0, 0.05, 1000), (1, 0.05, 1005), (2, 0.08, 1050), (3, 0.08, 1060),
              (4, 0.10, 1120), (5, 0.10, 1130), (6, 0.06, 1080), (7, 0.06, 1075),
              (8, 0.09, 1140), (9, 0.09, 1150)]
        for hour, funding, oi in seq:
            rows.append({"ts": start + hour * 3600 + 1, "venue": "binance", "status": "ok",
                        "funding_pi_4h": funding, "oi_raw": oi, "oi_usd": oi * 2})
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "X.jsonl"
            _write_jsonl(p, rows)
            r = OC._elasticity_from_store("X", window=12 * 3600, store_dir=Path(td),
                                          now_ts=now_ts, bucket_min=60)
        self.assertEqual(r["n_settlements"], 4)
        self.assertGreaterEqual(r["coverage_pct"], 70.0)
        self.assertTrue(r["elasticity_confirmed"])
        self.assertIsNone(r["degraded_reason"])

    def test_below_3_settlements_is_no_history_regardless_of_coverage(self):
        now_ts = 100000
        start = now_ts - 12 * 3600
        rows = []
        # full 12/12 coverage but only 2 funding-value changes (settlements)
        seq_fund = [0.05, 0.05, 0.08, 0.08, 0.08, 0.08, 0.08, 0.10, 0.10, 0.10, 0.10, 0.10]
        for hour, funding in enumerate(seq_fund):
            rows.append({"ts": start + hour * 3600 + 1, "venue": "binance", "status": "ok",
                        "funding_pi_4h": funding, "oi_raw": 1000 + hour, "oi_usd": 2000 + hour})
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "X.jsonl"
            _write_jsonl(p, rows)
            r = OC._elasticity_from_store("X", window=12 * 3600, store_dir=Path(td),
                                          now_ts=now_ts, bucket_min=60)
        self.assertEqual(r["coverage_pct"], 100.0)
        self.assertEqual(r["n_settlements"], 2)
        self.assertIsNone(r["elasticity_confirmed"])
        self.assertEqual(r["degraded_reason"], "no_history")

    def test_enough_settlements_but_gappy_coverage(self):
        # 5 settlements packed into 6 of 12 hourly buckets (50% coverage < 70% floor).
        now_ts = 100000
        start = now_ts - 12 * 3600
        seq_fund = [0.05, 0.08, 0.10, 0.06, 0.09, 0.12]
        rows = []
        for hour, funding in enumerate(seq_fund):
            rows.append({"ts": start + hour * 3600 + 1, "venue": "binance", "status": "ok",
                        "funding_pi_4h": funding, "oi_raw": 1000 + hour * 10,
                        "oi_usd": 2000 + hour * 10})
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "X.jsonl"
            _write_jsonl(p, rows)
            r = OC._elasticity_from_store("X", window=12 * 3600, store_dir=Path(td),
                                          now_ts=now_ts, bucket_min=60)
        self.assertEqual(r["n_settlements"], 5)
        self.assertLess(r["coverage_pct"], 70.0)
        self.assertIsNone(r["elasticity_confirmed"])
        self.assertEqual(r["degraded_reason"], "gappy")

    def test_redenomination_marker_splits_series_settlements_post_marker_only(self):
        # pre-marker: 5 funding-value changes across 6 rows (would trivially pass the
        # floor alone) — must be EXCLUDED. post-marker: only 1 settlement (insufficient
        # alone) — proves the split, not just a generous total.
        now_ts = 200000
        start = now_ts - 24 * 3600
        rows = []
        pre_fund = [0.05, 0.08, 0.10, 0.06, 0.09, 0.12]
        for hour, funding in enumerate(pre_fund):
            rows.append({"ts": start + hour * 3600 + 1, "venue": "binance", "status": "ok",
                        "funding_pi_4h": funding, "oi_raw": 1000 + hour * 10,
                        "oi_usd": 2000 + hour * 10})
        rows.append({"ts": start + 6 * 3600 + 30, "venue": "binance", "status": "redenomination",
                    "prev_ratio": 2.0, "cur_ratio": 20.0, "step": 10.0})
        post_fund = [0.20, 0.20, 0.30]
        for i, funding in enumerate(post_fund):
            hour = 7 + i
            rows.append({"ts": start + hour * 3600 + 1, "venue": "binance", "status": "ok",
                        "funding_pi_4h": funding, "oi_raw": 50 + i, "oi_usd": 1000 + i})
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "X.jsonl"
            _write_jsonl(p, rows)
            r = OC._elasticity_from_store("X", window=24 * 3600, store_dir=Path(td),
                                          now_ts=now_ts, bucket_min=60)
        self.assertEqual(r["n_settlements"], 1)
        self.assertEqual(r["degraded_reason"], "no_history")


# ---------------------------------------------------------------------------
# fetch_lock_info (SPEC-182 req 2) — ticker registry -> stake_schedule, cached
# ---------------------------------------------------------------------------
class TestFetchLockInfo(unittest.TestCase):
    def test_no_registry_entry_is_not_attempted(self):
        info, attempted = OC.fetch_lock_info("NOPE", registry={}, now_ts=1000)
        self.assertIsNone(info)
        self.assertFalse(attempted)

    def test_registry_entry_resolves_cliff_from_stake_schedule(self):
        reg = {"RIVER": {"ticker": "RIVER", "contract": "0xabc", "chain": "bsc"}}
        calls = []

        def bs_fn(contract, chain):
            calls.append((contract, chain))
            return {"cliffs": [{"date": "2026-12-01", "amount": 100, "pct": 10}]}

        info, attempted = OC.fetch_lock_info(
            "RIVER", registry=reg, build_schedule_fn=bs_fn, now_ts=1000, cache={})
        self.assertTrue(attempted)
        self.assertEqual(info["cliff_date"], "2026-12-01")
        self.assertTrue(info["has_lock"])
        self.assertEqual(calls, [("0xabc", "bsc")])

    def test_registry_entry_no_cliffs_found_is_none_but_attempted(self):
        reg = {"RIVER": {"ticker": "RIVER", "contract": "0xabc", "chain": "bsc"}}
        info, attempted = OC.fetch_lock_info(
            "RIVER", registry=reg, build_schedule_fn=lambda c, ch: {"cliffs": []},
            now_ts=1000, cache={})
        self.assertIsNone(info)
        self.assertTrue(attempted)

    def test_cache_hit_skips_stake_schedule_while_cliff_is_future(self):
        reg = {"RIVER": {"ticker": "RIVER", "contract": "0xabc", "chain": "bsc"}}
        cache = {"RIVER": {"cliff_date": "2099-01-01",
                           "lock_info": {"has_lock": True, "cliff_date": "2099-01-01"}}}
        calls = []

        def bs_fn(contract, chain):
            calls.append(1)
            return {"cliffs": [{"date": "2099-01-01"}]}

        info, attempted = OC.fetch_lock_info(
            "RIVER", registry=reg, build_schedule_fn=bs_fn, now_ts=1000, cache=cache)
        self.assertEqual(calls, [])
        self.assertTrue(attempted)
        self.assertEqual(info["cliff_date"], "2099-01-01")

    def test_cache_expired_past_cliff_refetches(self):
        reg = {"RIVER": {"ticker": "RIVER", "contract": "0xabc", "chain": "bsc"}}
        cache = {"RIVER": {"cliff_date": "2020-01-01",
                           "lock_info": {"has_lock": True, "cliff_date": "2020-01-01"}}}
        calls = []

        def bs_fn(contract, chain):
            calls.append(1)
            return {"cliffs": [{"date": "2026-06-01"}]}

        info, attempted = OC.fetch_lock_info(
            "RIVER", registry=reg, build_schedule_fn=bs_fn, now_ts=1700000000, cache=cache)
        self.assertEqual(calls, [1])
        self.assertEqual(info["cliff_date"], "2026-06-01")


# ---------------------------------------------------------------------------
# build_oi_construction default-path wiring (SPEC-182 req 2/3)
# ---------------------------------------------------------------------------
class TestDefaultPathLiveWiring(unittest.TestCase):
    def test_lock_info_fn_seam_returning_cliff_fires_vesting_hedge_from_default_path(self):
        r = OC.build_oi_construction(
            "X", venue_map_fn=_vm_stub(), spot_resolver=_no_spot(), battlefield_fn=_bf_unknown(),
            lock_info_fn=lambda t, **kw: ({"has_lock": True, "cliff_date": "2027-01-01"}, True))
        vh = next(t for t in r["oi_types"] if t["type"] == "VESTING_HEDGE")
        self.assertEqual(vh["verdict"], "ASSERTED")
        self.assertEqual(vh["cliff_date"], "2027-01-01")

    def test_lock_info_fn_seam_returning_nothing_is_not_asserted_plus_degraded_row(self):
        r = OC.build_oi_construction(
            "X", venue_map_fn=_vm_stub(), spot_resolver=_no_spot(), battlefield_fn=_bf_unknown(),
            lock_info_fn=lambda t, **kw: (None, True))
        vh = next(t for t in r["oi_types"] if t["type"] == "VESTING_HEDGE")
        self.assertEqual(vh["verdict"], "NOT_ASSERTED")
        self.assertTrue(any(d.get("instrument") == "lock_info" for d in r["degraded"]))

    def test_lock_info_fn_not_attempted_no_degraded_row(self):
        r = OC.build_oi_construction(
            "X", venue_map_fn=_vm_stub(), spot_resolver=_no_spot(), battlefield_fn=_bf_unknown(),
            lock_info_fn=lambda t, **kw: (None, False))
        self.assertFalse(any(d.get("instrument") == "lock_info" for d in r["degraded"]))

    def test_explicit_lock_info_bypasses_the_seam_entirely(self):
        called = []

        def boom(t, **kw):
            called.append(1)
            return (None, True)

        r = OC.build_oi_construction(
            "X", venue_map_fn=_vm_stub(), spot_resolver=_no_spot(), battlefield_fn=_bf_unknown(),
            lock_info={"cliff_date": "2027-06-01"}, lock_info_fn=boom)
        self.assertEqual(called, [])
        vh = next(t for t in r["oi_types"] if t["type"] == "VESTING_HEDGE")
        self.assertEqual(vh["cliff_date"], "2027-06-01")

    def test_elasticity_fn_seam_wires_into_funding_farm(self):
        r = OC.build_oi_construction(
            "X", venue_map_fn=_vm_stub(funding_extreme={"venue": "binance", "pi_4h": 0.5}),
            spot_resolver=_no_spot(), battlefield_fn=_bf_unknown(),
            elasticity_fn=lambda t, **kw: {"elasticity_confirmed": True, "n_settlements": 4,
                                           "coverage_pct": 90.0, "degraded_reason": None})
        ff = next(t for t in r["oi_types"] if t["type"] == "FUNDING_FARM")
        self.assertEqual(ff["verdict"], "ASSERTED")
        self.assertIn("oi_funding_elasticity", [e["signal"] for e in ff["evidence"]])

    def test_elasticity_fn_degraded_reason_lands_in_degraded_list(self):
        r = OC.build_oi_construction(
            "X", venue_map_fn=_vm_stub(), spot_resolver=_no_spot(), battlefield_fn=_bf_unknown(),
            elasticity_fn=lambda t, **kw: {"elasticity_confirmed": None, "n_settlements": 1,
                                           "coverage_pct": 0.0, "degraded_reason": "no_history"})
        self.assertTrue(any(d.get("instrument") == "oi_funding_elasticity"
                            and d.get("reason") == "no_history" for d in r["degraded"]))

    def test_empty_registry_and_empty_store_output_unchanged_bar_degraded_rows(self):
        # req 3 — the layer may only gain the ability to CONFIRM, never lose its
        # conservatism: real default fetchers, ticker with nothing in either the
        # lock registry or the sampler store -> verdict/oi_types identical to the
        # pre-SPEC-182 all-None-injection baseline.
        baseline = OC.build_oi_construction(
            "ZZZNOTHING", venue_map_fn=_vm_stub(), spot_resolver=_no_spot(),
            battlefield_fn=_bf_unknown(),
            elasticity_fn=lambda t, **kw: {"elasticity_confirmed": None, "n_settlements": 0,
                                           "coverage_pct": 0.0, "degraded_reason": None},
            lock_info_fn=lambda t, **kw: (None, False))
        live = OC.build_oi_construction(
            "ZZZNOTHING", venue_map_fn=_vm_stub(), spot_resolver=_no_spot(),
            battlefield_fn=_bf_unknown())
        self.assertEqual(baseline["verdict"], live["verdict"])
        self.assertEqual(baseline["oi_types"], live["oi_types"])


# ---------------------------------------------------------------------------
# save_venue_roles_snapshot — append-friendly history, never overwritten
# ---------------------------------------------------------------------------
class TestSaveVenueRolesSnapshot(unittest.TestCase):
    def test_appends_history_never_overwrites(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "venue_roles.json"
            OC.save_venue_roles_snapshot("X", {"mark_engine": {"verdict": "binance"}}, path=p)
            OC.save_venue_roles_snapshot("X", {"mark_engine": {"verdict": "bybit"}}, path=p)
            OC.save_venue_roles_snapshot("Y", {"mark_engine": {"verdict": "aster"}}, path=p)
            data = json.loads(p.read_text())
            self.assertEqual(len(data["X"]), 2)
            self.assertEqual(len(data["Y"]), 1)
            self.assertEqual(data["X"][0]["mark_engine"]["verdict"], "binance")
            self.assertEqual(data["X"][1]["mark_engine"]["verdict"], "bybit")

    def test_missing_file_starts_fresh(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nope" / "venue_roles.json"
            r = OC.save_venue_roles_snapshot("X", {"a": 1}, path=p)
            self.assertEqual(r["snapshots"], 1)
            self.assertTrue(p.exists())


# ---------------------------------------------------------------------------
# compact_line — SPEC-180 req 5, the ONE board field
# ---------------------------------------------------------------------------
class TestCompactLine(unittest.TestCase):
    def test_unknown_verdict_no_degraded_returns_none(self):
        env = {"verdict": "UNKNOWN", "degraded": [], "oi_types": [],
              "battlefield": {"battlefield": "UNKNOWN"}, "venue_roles": {}}
        self.assertIsNone(OC.compact_line(env))

    def test_none_envelope_returns_none(self):
        self.assertIsNone(OC.compact_line(None))

    def test_mixed_with_asserted_type_and_exit_role_renders(self):
        env = {"verdict": "MIXED", "degraded": [],
              "oi_types": [{"type": "VESTING_HEDGE", "verdict": "ASSERTED",
                            "share_read": "material", "side": "short"}],
              "battlefield": {"battlefield": "perp_led"},
              "venue_roles": {"exit": {"venue": "bitget", "grade": "FLOW_CONFIRMED"}}}
        line = OC.compact_line(env)
        self.assertEqual(line, "oic: MIXED(VH·mat·short) · perp_led · exit:bitget(flow)")

    def test_unknown_with_noteworthy_degraded_still_renders(self):
        env = {"verdict": "UNKNOWN", "degraded": [{"instrument": "x", "reason": "quota-dead"}],
              "oi_types": [], "battlefield": {"battlefield": "UNKNOWN"}, "venue_roles": {}}
        line = OC.compact_line(env)
        self.assertIsNotNone(line)
        self.assertIn("UNKNOWN", line)


# ---------------------------------------------------------------------------
# Call-site wiring — classify.py's oic: compact field (req 5)
# ---------------------------------------------------------------------------
class TestClassifyOicWiring(unittest.TestCase):
    def setUp(self):
        CL_spec = importlib.util.spec_from_file_location(
            "classify_oic_t", ROOT / "capabilities" / "classify.py")
        self.CL = importlib.util.module_from_spec(CL_spec)
        CL_spec.loader.exec_module(self.CL)

    def test_default_board_scope_no_oic_key_ever(self):
        # no oic_envelope injected -> always UNKNOWN/no-degraded -> no oic key at all
        rows = [{"ticker": "X", "battlefield": "perp_led"}]
        self.CL.annotate_oi_construction_compact(rows)
        self.assertNotIn("oic", rows[0])

    def test_injected_envelope_produces_the_key(self):
        rows = [{"ticker": "X", "oic_envelope": {
            "verdict": "MIXED", "degraded": [],
            "oi_types": [{"type": "VESTING_HEDGE", "verdict": "ASSERTED",
                         "share_read": "material", "side": "short"}],
            "battlefield": {"battlefield": "perp_led"},
            "venue_roles": {"exit": {"venue": "bitget", "grade": "FLOW_CONFIRMED"}}}}]
        self.CL.annotate_oi_construction_compact(rows)
        self.assertIn("oic", rows[0])
        self.assertEqual(rows[0]["oic"], "oic: MIXED(VH·mat·short) · perp_led · exit:bitget(flow)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
