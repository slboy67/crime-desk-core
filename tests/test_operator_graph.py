#!/usr/bin/env python3
"""SPEC-99 — operator-graph auto-clustering: recurring wallets across tokens -> named
clusters (G7).

Local-first graph build: nodes = wallets, edges = a wallet recurring across >=2 tokens'
tracked sets at a "strong" (op/team/distribution) tier. Infra exclusion is load-bearing
(memory: reference_operator_cluster_0x73d8, DISPROVEN) — exchange/CEX-infra, DEX
pools/routers, lockers and bridges never form an edge; a shared-CEX-funder is a WEAK
edge at most (memory: reference_xpin_cat_a_lock_linked_accumulation, "shared-CEX-funder
!= Sybil").

Run:  python3 -m unittest tests.test_operator_graph -v
"""
import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent


def _load(name, alias=None):
    spec = importlib.util.spec_from_file_location(alias or name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


OG = _load("operator_graph")


def _wallet(addr, label, tier):
    return {"address": addr, "label": label, "tier": tier}


class TestBuildEdges(unittest.TestCase):
    def test_op_tier_recurrence_is_a_strong_edge(self):
        tracked = {"tokens": {
            "AAA": {"wallets": [_wallet("0xaaa1", "AGGREGATOR", "op")]},
            "BBB": {"wallets": [_wallet("0xaaa1", "AGGREGATOR", "op")]},
        }}
        strong, weak = OG.build_edges(tracked)
        self.assertEqual(len(strong), 1)
        self.assertEqual(strong[0]["tokens"], ["AAA", "BBB"])
        self.assertEqual(weak, [])

    def test_single_token_occurrence_is_not_an_edge(self):
        tracked = {"tokens": {
            "AAA": {"wallets": [_wallet("0xaaa1", "TEAM", "team")]},
        }}
        strong, weak = OG.build_edges(tracked)
        self.assertEqual(strong, [])
        self.assertEqual(weak, [])

    def test_unclassified_tier_recurrence_forms_no_edge(self):
        # GOPLUS-TOP* noise: unverified top-holder scan hits, not a verified operator link.
        tracked = {"tokens": {
            "AAA": {"wallets": [_wallet("0xnoise", "GOPLUS-TOP1", "unclassified")]},
            "BBB": {"wallets": [_wallet("0xnoise", "GOPLUS-TOP2", "unclassified")]},
        }}
        strong, weak = OG.build_edges(tracked)
        self.assertEqual(strong, [])
        self.assertEqual(weak, [])

    def test_dex_bridge_pool_locker_tiers_form_no_edge_not_even_weak(self):
        for tier in ("dex", "bridge", "staking_lock", "pool", "burn"):
            tracked = {"tokens": {
                "AAA": {"wallets": [_wallet("0xinfra", "INFRA", tier)]},
                "BBB": {"wallets": [_wallet("0xinfra", "INFRA", tier)]},
            }}
            strong, weak = OG.build_edges(tracked)
            self.assertEqual(strong, [], tier)
            self.assertEqual(weak, [], tier)

    def test_shared_cex_funder_is_weak_edge_only(self):
        # SPEC-99 DoD (b)/(c): the 0x73d8-style regression — a synthetic token set sharing
        # ONLY exchange infra (tier=cex) must produce a weak edge, never a strong one.
        tracked = {"tokens": {
            "AAA": {"wallets": [_wallet("0xcex1", "BINANCE-WALLET-PROXY", "cex")]},
            "BBB": {"wallets": [_wallet("0xcex1", "BINANCE-WALLET-PROXY", "cex")]},
            "CCC": {"wallets": [_wallet("0xcex1", "BINANCE-WALLET-PROXY", "cex")]},
        }}
        strong, weak = OG.build_edges(tracked)
        self.assertEqual(strong, [])
        self.assertEqual(len(weak), 1)
        self.assertEqual(weak[0]["tag"], "weak:shared_cex_funder")
        self.assertEqual(weak[0]["tokens"], ["AAA", "BBB", "CCC"])

    def test_router_labeled_wallet_never_forms_an_edge_even_at_op_tier(self):
        # The 0xb300000b landmine: tracked_wallets.json mistiers a shared multi-Cat-A
        # router as tier=op ("Mirror of 0x238a3588 (router-1)" per its own note) — exactly
        # the 0x73d8/0x238a co-occurrence trap the spec calls out, just not yet reclassified.
        # A self-declared ROUTER/MULTI-CAT-A label must never form an edge, strong or weak,
        # regardless of tier.
        tracked = {"tokens": {
            "AAA": {"wallets": [_wallet("0xrouter", "ROUTER-2-MULTI-CAT-A", "op")]},
            "BBB": {"wallets": [_wallet("0xrouter", "ROUTER-2-MULTI-CAT-A", "op")]},
            "CCC": {"wallets": [_wallet("0xrouter", "ROUTER-2-SWAP-EXIT", "op")]},
        }}
        strong, weak = OG.build_edges(tracked)
        self.assertEqual(strong, [])
        self.assertEqual(weak, [])

    def test_cex_labeled_occurrence_demoted_even_at_op_tier(self):
        # A wallet whose own label self-declares CEX-adjacency (e.g. "SKYAI-EOA-CEX-...")
        # must not count toward a strong edge even when tracked_wallets.json tiers it "op" —
        # the label is the desk's own admission the wallet is exchange-adjacent.
        tracked = {"tokens": {
            "AAA": {"wallets": [_wallet("0xcexeoa", "AAA-EOA-CEX-9Mnonce", "op")]},
            "BBB": {"wallets": [_wallet("0xcexeoa", "BBB-EOA-CEX-3Mnonce", "op")]},
        }}
        strong, weak = OG.build_edges(tracked)
        self.assertEqual(strong, [])
        self.assertEqual(len(weak), 1)
        self.assertEqual(weak[0]["tag"], "weak:shared_cex_funder")


class TestConnectedComponents(unittest.TestCase):
    def test_transitive_merge_across_two_edges(self):
        strong = [
            {"address": "0x1", "tokens": ["AAA", "BBB"], "tier": "op", "evidence": []},
            {"address": "0x2", "tokens": ["BBB", "CCC"], "tier": "team", "evidence": []},
        ]
        comps = OG.connected_components(strong)
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0]["tokens"], ["AAA", "BBB", "CCC"])

    def test_disjoint_edges_stay_separate_components(self):
        strong = [
            {"address": "0x1", "tokens": ["AAA", "BBB"], "tier": "op", "evidence": []},
            {"address": "0x2", "tokens": ["CCC", "DDD"], "tier": "op", "evidence": []},
        ]
        comps = OG.connected_components(strong)
        self.assertEqual(sorted(c["tokens"] for c in comps), [["AAA", "BBB"], ["CCC", "DDD"]])


class TestXtokenReproduction(unittest.TestCase):
    """DoD (a): the XTOKEN cluster reproduces from real tracked data."""

    def setUp(self):
        self.tracked = json.loads((ROOT / "config" / "tracked_wallets.json").read_text())
        self.seed = json.loads((ROOT / "config" / "clusters.json").read_text())

    def test_bill_bsb_eden_lab_share_a_cluster(self):
        clusters, _weak = OG.build_clusters(tracked=self.tracked, seed=self.seed, now=1_780_000_000.0)
        name, members = OG.cluster_for_ticker("BILL", clusters)
        self.assertIsNotNone(name)
        for tk in ("BSB", "EDEN", "LAB"):
            self.assertIn(tk, members, f"{tk} missing from BILL's cluster {name}: {members}")

    def test_cluster_name_seeds_from_the_curated_xtoken_mm_entry(self):
        clusters, _weak = OG.build_clusters(tracked=self.tracked, seed=self.seed, now=1_780_000_000.0)
        name, _members = OG.cluster_for_ticker("BILL", clusters)
        self.assertEqual(name, "XTOKEN-MM")

    def test_router_landmine_does_not_merge_unrelated_tokens(self):
        # 0xb300000b (ROUTER-2-MULTI-CAT-A, tier=op) co-occurs on BILL/BSB/BEAT/BLUAI/
        # ESPORTS/GUA — must NOT pull BEAT/ESPORTS/GUA into the XTOKEN cluster.
        clusters, _weak = OG.build_clusters(tracked=self.tracked, seed=self.seed, now=1_780_000_000.0)
        name, members = OG.cluster_for_ticker("BILL", clusters)
        for tk in ("BEAT", "ESPORTS", "GUA"):
            self.assertNotIn(tk, members, f"{tk} falsely merged into {name} via a router edge")


class TestWriteClusters(unittest.TestCase):
    def test_write_is_idempotent_on_ids_and_evidence(self):
        tracked = {"tokens": {
            "AAA": {"wallets": [_wallet("0xaaa1", "AGGREGATOR", "op")]},
            "BBB": {"wallets": [_wallet("0xaaa1", "AGGREGATOR", "op")]},
        }}
        with TemporaryDirectory() as td:
            out_path = Path(td) / "operator_clusters.json"
            first = OG.write_clusters(path=out_path, tracked=tracked, seed={}, now=1_000.0)
            first_ids = sorted(first["clusters"].keys())
            second = OG.write_clusters(path=out_path, tracked=tracked, seed={}, now=2_000.0)
            second_ids = sorted(second["clusters"].keys())
            self.assertEqual(first_ids, second_ids)
            for cid in first_ids:
                self.assertTrue(first["clusters"][cid]["evidence"])
                self.assertTrue(second["clusters"][cid]["evidence"])
                # first_seen_ts is preserved across re-runs; last_verified_ts advances
                self.assertEqual(first["clusters"][cid]["first_seen_ts"],
                                  second["clusters"][cid]["first_seen_ts"])
                self.assertEqual(second["clusters"][cid]["last_verified_ts"], 2_000.0)
            written = json.loads(out_path.read_text())
            self.assertEqual(sorted(written["clusters"].keys()), second_ids)

    def test_write_with_no_strong_edges_produces_empty_clusters(self):
        tracked = {"tokens": {
            "AAA": {"wallets": [_wallet("0xcex1", "BINANCE-WALLET-PROXY", "cex")]},
            "BBB": {"wallets": [_wallet("0xcex1", "BINANCE-WALLET-PROXY", "cex")]},
        }}
        with TemporaryDirectory() as td:
            out_path = Path(td) / "operator_clusters.json"
            out = OG.write_clusters(path=out_path, tracked=tracked, seed={}, now=1_000.0)
            self.assertEqual(out["clusters"], {})
            self.assertEqual(len(out["weak_edges"]), 1)


class TestClusterForTicker(unittest.TestCase):
    def test_returns_none_for_unknown_ticker(self):
        name, members = OG.cluster_for_ticker("ZZZ", {"XTOKEN-MM": {"members": ["BILL", "BSB"]}})
        self.assertIsNone(name)
        self.assertEqual(members, [])

    def test_finds_ticker_case_insensitively(self):
        name, members = OG.cluster_for_ticker("bill", {"XTOKEN-MM": {"members": ["BILL", "BSB"]}})
        self.assertEqual(name, "XTOKEN-MM")
        self.assertEqual(members, ["BILL", "BSB"])


CLUSTERS_FIXTURE = {"XTOKEN": {"members": ["BILL", "BSB", "LAB"]}}


class TestClassifyBoardClusterHeat(unittest.TestCase):
    """DoD (d): the classify board's reason carries the cluster-heat note when 2+
    watchlist names share a cluster."""

    def setUp(self):
        self.CL = _load("classify", alias="classify_og_t")

    def test_two_watchlist_cluster_mates_get_the_base_note(self):
        rows = [{"ticker": "BILL", "reason": "no signals", "direction": "LONG"},
                {"ticker": "BSB", "reason": "no signals", "direction": "SHORT"}]
        tokens = [{"ticker": "BILL", "thesis": {}}, {"ticker": "BSB", "thesis": {}}]
        self.CL.annotate_cluster_heat(rows, clusters=CLUSTERS_FIXTURE, tokens=tokens)
        self.assertIn("⚠ cluster XTOKEN", rows[0]["reason"])
        self.assertIn("BILL+BSB", rows[0]["reason"])
        self.assertIn("§7 one-position rule", rows[0]["reason"])
        self.assertIn("⚠ cluster XTOKEN", rows[1]["reason"])

    def test_solo_watchlist_ticker_in_cluster_gets_no_note(self):
        rows = [{"ticker": "BILL", "reason": "no signals", "direction": "LONG"}]
        tokens = [{"ticker": "BILL", "thesis": {}}]
        self.CL.annotate_cluster_heat(rows, clusters=CLUSTERS_FIXTURE, tokens=tokens)
        self.assertEqual(rows[0]["reason"], "no signals")

    def test_opposing_live_theses_get_not_a_hedge_warning(self):
        rows = [{"ticker": "BILL", "reason": "no signals"},
                {"ticker": "BSB", "reason": "no signals"}]
        tokens = [{"ticker": "BILL", "thesis": {"status": "ACTIVE", "direction": "LONG"}},
                  {"ticker": "BSB", "thesis": {"status": "ACTIVE", "direction": "SHORT"}}]
        self.CL.annotate_cluster_heat(rows, clusters=CLUSTERS_FIXTURE, tokens=tokens)
        self.assertIn("NOT a hedge", rows[0]["reason"])
        self.assertIn("NOT a hedge", rows[1]["reason"])

    def test_same_direction_live_theses_get_combined_risk_not_not_a_hedge(self):
        rows = [{"ticker": "BILL", "reason": "no signals"},
                {"ticker": "BSB", "reason": "no signals"}]
        tokens = [{"ticker": "BILL", "thesis": {"status": "ACTIVE", "direction": "LONG"}},
                  {"ticker": "BSB", "thesis": {"status": "PENDING", "direction": "LONG"}}]
        self.CL.annotate_cluster_heat(rows, clusters=CLUSTERS_FIXTURE, tokens=tokens)
        self.assertNotIn("NOT a hedge", rows[0]["reason"])
        self.assertIn("combined risk", rows[0]["reason"])

    def test_missing_operator_clusters_file_degrades_silently(self):
        rows = [{"ticker": "BILL", "reason": "no signals"}]
        self.CL.annotate_cluster_heat(rows, clusters={}, tokens=[{"ticker": "BILL"}])
        self.assertEqual(rows[0]["reason"], "no signals")

    def test_board_loop_wires_annotate_cluster_heat(self):
        import inspect
        src = inspect.getsource(self.CL)
        self.assertIn("annotate_cluster_heat(results)", src)


class TestBriefClusterHeat(unittest.TestCase):
    """DoD (d): brief's state layer + one-line headline surface the same warning."""

    def setUp(self):
        self.BR = _load("brief", alias="brief_og_t")
        self.CL = _load("classify", alias="classify_og_bt")

    def test_state_layer_is_wired_to_cluster_heat_for(self):
        import inspect
        src = inspect.getsource(self.BR._state_layer)
        self.assertIn("cluster_heat_for", src)
        self.assertIn('"cluster_heat"', src)

    def test_headline_appends_cluster_heat_note(self):
        state = {"verdict": "CONFIRMS", "thesis_present": True,
                 "cluster_heat": {"note": "⚠ cluster XTOKEN: BILL+BSB — §7 one-position rule / combined operator-risk"}}
        h = self.BR._headline("BILL", state, {"available": False}, {"available": False}, {"available": False})
        self.assertIn("⚠ cluster XTOKEN", h)

    def test_headline_without_cluster_heat_is_unaffected(self):
        state = {"verdict": "CONFIRMS", "thesis_present": True, "cluster_heat": None}
        h = self.BR._headline("BILL", state, {"available": False}, {"available": False}, {"available": False})
        self.assertNotIn("cluster", h)


if __name__ == "__main__":
    unittest.main()
