#!/usr/bin/env python3
"""SPEC 15 — distribution_radar: DISCOVERS distributing wallets + change-detection.

Run:  python3 tests/test_distribution_radar.py

Fully offline: monkeypatch the data layer (Moralis safe-outbound discovery, GoPlus
holders, per-candidate verify, perp, live price, state persistence). Proves the radar
DISCOVERS a seeded distributor without being given the address (the SPEC-15 point) and
that a repeat sweep with no new activity raises no new alerts.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("distribution_radar", ROOT / "capabilities" / "distribution_radar.py")
DR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DR)

SEED_SAFE = "0xsafe000000000000000000000000000000000001"
SEEDED = "0xbb58b69b686149627e4d205d9493b59ad98ef275"     # discovered, not given
INDEP = "0xindependent00000000000000000000000000beef"


def fake_verify(addr, token, days=180, price=None):
    if addr == SEEDED:
        return {"available": True, "seeded_staging": True,
                "seeded_sources": [{"label": "FRESH-STAGED-1 (distribution)"}],
                "distributing": True, "recent_outbounds": [{"to_kind": "cex"}],
                "net_flow_window": 7e6, "net_flow_window_usd": 324068.0,
                "last_out_ts": "2026-06-03T12:00:00.000Z", "verdict": "SEEDED-STAGING"}
    if addr == INDEP:
        return {"available": True, "seeded_staging": False, "seeded_sources": [],
                "distributing": True, "recent_outbounds": [{"to_kind": "dex"}],
                "net_flow_window": 1e6, "net_flow_window_usd": 50000.0,
                "last_out_ts": "2026-06-03T11:00:00.000Z", "verdict": "DISTRIBUTING"}
    return {"available": True, "seeded_staging": False, "seeded_sources": [], "distributing": False,
            "recent_outbounds": [], "net_flow_window": 0, "net_flow_window_usd": 0,
            "last_out_ts": None, "verdict": "DORMANT"}


class TestRadar(unittest.TestCase):
    def setUp(self):
        self._orig = (DR._cat_a_tokens, DR._seeded_recipients, DR._concentration,
                      DR.build_verify, DR.live_perp, DR._live_price, DR._load_known, DR._save_known, DR.nonce_of)
        DR.nonce_of = lambda addr, chain_key="binance-smart-chain": 1   # stable nonce (offline)
        DR._cat_a_tokens = lambda: ["ESPORTS"]
        DR._seeded_recipients = lambda contract, chain, safes, days=4: ({SEEDED}, 0)
        DR._concentration = lambda tk: {"available": True, "holders": [
            {"address": INDEP, "percent": 5.0, "is_contract": False, "is_locked": False},
            {"address": "0xpool0000000000000000000000000000000000aa", "percent": 9.0, "is_contract": True, "is_locked": False}]}
        DR.build_verify = fake_verify
        DR.live_perp = lambda tk: {"funding_4h": 0.05, "funding_split": False}
        DR._live_price = lambda tk: (0.05, "bybit")
        self._known = {}
        DR._load_known = lambda tk: dict(self._known)
        DR._save_known = lambda tk, k: self._known.update(k)
        # make a tracked-config entry exist for ESPORTS with a BSC contract + a seed safe
        import json
        cfg = json.loads((ROOT / "config" / "tracked_wallets.json").read_text())
        self.assertIn("ESPORTS", cfg["tokens"])   # relies on real config having ESPORTS

    def tearDown(self):
        (DR._cat_a_tokens, DR._seeded_recipients, DR._concentration,
         DR.build_verify, DR.live_perp, DR._live_price, DR._load_known, DR._save_known, DR.nonce_of) = self._orig

    def test_discovers_seeded_distributor_as_high(self):
        r = DR.build_radar("ESPORTS")
        t = r["tokens"]["ESPORTS"]
        self.assertTrue(t["available"])
        addrs = {c["address"]: c for c in t["distributors"]}
        self.assertIn(SEEDED, addrs)                              # discovered without being given
        self.assertEqual(addrs[SEEDED]["confidence"], "HIGH")     # seeded + distributing
        self.assertEqual(addrs[INDEP]["confidence"], "MED")       # independent + distributing

    def test_perp_fused(self):
        t = DR.build_radar("ESPORTS")["tokens"]["ESPORTS"]
        self.assertIn("perp", t)
        self.assertEqual(t["perp"]["funding_4h"], 0.05)
        self.assertFalse(t["perp"]["short_vetoed"])      # +funding → not deep-neg-vetoed
        self.assertIn(t["perp"]["sign"], {"pos", "flat"})

    def test_dormant_dropped(self):
        DR._seeded_recipients = lambda *a, **k: ({"0xdormant00000000000000000000000000000001"}, 0)
        DR._concentration = lambda tk: {"available": True, "holders": []}
        t = DR.build_radar("ESPORTS")["tokens"]["ESPORTS"]
        self.assertEqual(t["distributors"], [])                   # non-seller dropped

    def test_degraded_candidate_flagged_not_dropped(self):
        # a candidate the provider couldn't read → token.degraded true + counted, not silently gone
        DR._seeded_recipients = lambda *a, **k: ({SEEDED}, 0)
        DR._concentration = lambda tk: {"available": True, "holders": []}
        DR.build_verify = lambda addr, token, days=180, price=None: {"available": False, "degraded": True}
        t = DR.build_radar("ESPORTS")["tokens"]["ESPORTS"]
        self.assertTrue(t["degraded"])
        self.assertEqual(t["coverage"]["candidates_unread"], 1)

    def test_seed_discovery_error_marks_degraded(self):
        DR._seeded_recipients = lambda *a, **k: (set(), 2)        # 2 safes failed to read
        DR._concentration = lambda tk: {"available": True, "holders": []}
        t = DR.build_radar("ESPORTS")["tokens"]["ESPORTS"]
        self.assertTrue(t["degraded"])
        self.assertEqual(t["coverage"]["seeded_discovery_errors"], 2)

    def test_change_detection_no_new_on_repeat(self):
        r1 = DR.build_radar("ESPORTS")
        self.assertGreater(r1["alerts_total"], 0)                 # first sweep = all NEW
        r2 = DR.build_radar("ESPORTS")                            # identical data, no chain change
        self.assertEqual(r2["alerts_total"], 0)                   # no re-alerting

    def test_acceleration_realerts(self):
        DR.build_radar("ESPORTS")                                 # seed known set (nonce 1)
        # a real new sell ADVANCES the nonce → nonce-gate re-fetches → sees newer last_out → ACCEL
        DR.nonce_of = lambda addr, chain_key="binance-smart-chain": 2
        orig = DR.build_verify
        def newer(addr, token, days=180, price=None):
            v = orig(addr, token, days, price)
            if addr == SEEDED:
                v["last_out_ts"] = "2026-06-03T18:00:00.000Z"
            return v
        DR.build_verify = newer
        r = DR.build_radar("ESPORTS")
        self.assertTrue(any(a["alert"] == "ACCELERATED" and a["address"] == SEEDED for a in r["tokens"]["ESPORTS"]["alerts"]))

    def test_nonce_gate_skips_moralis_when_unchanged(self):
        # seed, then re-run with SAME nonce + a build_verify that would explode if called
        DR.build_radar("ESPORTS")
        def boom(*a, **k):
            raise AssertionError("build_verify called despite unchanged nonce")
        DR.build_verify = boom
        r = DR.build_radar("ESPORTS")                             # must NOT call build_verify
        t = r["tokens"]["ESPORTS"]
        self.assertEqual(len(t["alerts"]), 0)                    # cached → no re-alert
        self.assertGreaterEqual(t["n_nonce_cached"], 1)         # served from nonce cache


    def test_shared_destination_promotes_unknown_to_high(self):
        # SPEC 15 refinement: a funded_by:unknown wallet that feeds the SAME sell-hub as a
        # seeded operator wallet is the SAME actor → promote MED→HIGH on shared-destination,
        # not funding lineage alone. (ESPORTS 0x2609 ⇄ 0xbb58 via hub 0x5bb5.)
        HUB = "0x5bb59bb9371cbec158ed602d5f3cf1ad1c9b4462"
        UNKNOWN_OP = "0x2609000000000000000000000000000000000abc"
        DR._seeded_recipients = lambda contract, chain, safes, days=4: ({SEEDED}, 0)
        DR._concentration = lambda tk: {"available": True, "holders": [
            {"address": UNKNOWN_OP, "percent": 4.0, "is_contract": False, "is_locked": False}]}

        def vf(addr, token, days=180, price=None):
            if addr == SEEDED:
                return {"available": True, "seeded_staging": True,
                        "seeded_sources": [{"label": "FRESH-STAGED-1 (distribution)"}],
                        "distributing": True, "recent_outbounds": [{"to_kind": "dex"}],
                        "net_flow_window": 7e6, "net_flow_window_usd": 324068.0,
                        "last_out_ts": "2026-06-03T12:00:00.000Z", "verdict": "SEEDED-STAGING",
                        "sell_destinations": [{"address": HUB, "kind": "unknown", "amount": 1000.0, "count": 3}]}
            if addr == UNKNOWN_OP:
                return {"available": True, "seeded_staging": False, "seeded_sources": [],
                        "distributing": True, "recent_outbounds": [{"to_kind": "dex"}],
                        "net_flow_window": 2e6, "net_flow_window_usd": 99000.0,
                        "last_out_ts": "2026-06-03T11:00:00.000Z", "verdict": "DISTRIBUTING",
                        "sell_destinations": [{"address": HUB, "kind": "unknown", "amount": 800.0, "count": 2}]}
            return {"available": True, "seeded_staging": False, "seeded_sources": [], "distributing": False,
                    "recent_outbounds": [], "net_flow_window": 0, "net_flow_window_usd": 0,
                    "last_out_ts": None, "verdict": "DORMANT", "sell_destinations": []}
        DR.build_verify = vf
        t = DR.build_radar("ESPORTS")["tokens"]["ESPORTS"]
        addrs = {c["address"]: c for c in t["distributors"]}
        self.assertEqual(addrs[SEEDED]["confidence"], "HIGH")
        self.assertEqual(addrs[UNKNOWN_OP]["confidence"], "HIGH")            # promoted, not MED
        self.assertEqual(addrs[UNKNOWN_OP]["confidence_reason"], "shared-dest")
        self.assertTrue(addrs[UNKNOWN_OP]["cluster"])
        hubs = {h["address"]: h for h in t["sell_hubs"]}
        self.assertIn(HUB, hubs)                                             # hub discovered
        self.assertEqual(hubs[HUB]["n_wallets"], 2)                          # both feed it


class TestDistributionRadarInvokeWiring(unittest.TestCase):
    """SPEC-174 #1 — distribution_radar's positional CLI arg is `token`, but the desk names
    everything `ticker` everywhere else; a `{"ticker": X}` call silently dropped to the
    all-Cat-A sweep instead of scoping to X. Fixed the SPEC-89 way: an alias, not a code
    change to the capability itself."""

    def setUp(self):
        import orchestrator as ORC
        self.ORC = ORC
        self.caps = ORC.load_caps()

    def test_registry_has_ticker_to_token_alias(self):
        dr_cap = self.caps["distribution_radar"]
        self.assertEqual((dr_cap.get("aliases") or {}).get("ticker"), "token")

    def test_ticker_arg_fills_the_token_positional(self):
        dr_cap = self.caps["distribution_radar"]
        args = self.ORC.apply_aliases(dr_cap, {"ticker": "ESPORTS"})
        cmd = self.ORC.fill(dr_cap["invoke"], args)
        self.assertIn("distribution_radar.py ESPORTS", cmd)

    def test_omitted_ticker_still_sweeps_everything(self):
        dr_cap = self.caps["distribution_radar"]
        args = self.ORC.apply_aliases(dr_cap, {})
        cmd = self.ORC.fill(dr_cap["invoke"], args)
        self.assertIn("distribution_radar.py --json", cmd)   # no bare token token in the middle


if __name__ == "__main__":
    unittest.main(verbosity=2)
