#!/usr/bin/env python3
"""onchain — the native on-chain analyser (Phase 2: nonce spine + composed picture).

Run:  python3 tests/test_onchain.py

build_nonce_state is the spine (the §8 live-top signal). Its baseline-diff logic is
tested DETERMINISTICALLY by monkeypatching the live snapshot + baseline I/O — no
network, no disk. One live orchestrator smoke checks the <30s no-hang acceptance
(SPEC-123: gated behind CRIMEDESK_LIVE_TESTS=1, skipped by default).
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORCH = ROOT / "orchestrator.py"

sys.path.insert(0, str(ROOT / "tests"))
from live_gate import LIVE, SKIP_REASON  # noqa: E402

spec = importlib.util.spec_from_file_location("onchain_cap", ROOT / "capabilities" / "onchain.py")
O = importlib.util.module_from_spec(spec)
spec.loader.exec_module(O)


def make_snap(wallets, tracked=True):
    ok = [w for w in wallets if w.get("rpc_ok")]
    fired = [w for w in ok if w["fired"]]
    dormant = [w for w in ok if not w["fired"]]
    primed = [w for w in dormant if (w.get("native_balance") or 0) > 0]
    return {"ticker": "TST", "tracked": tracked, "n_wallets": len(wallets),
            "fired_count": len(fired), "dormant_count": len(dormant),
            "primed_unfired_count": len(primed), "wallets": wallets}


def W(label, tier, nonce, bal=0.0, rpc_ok=True, chain="binance-smart-chain", reason=None):
    return {"label": label, "address": "0x" + label.lower(), "chain": chain,
            "tier": tier, "nonce": nonce, "native_balance": bal,
            "fired": (nonce is not None and nonce > 0), "rpc_ok": rpc_ok, "reason": reason}


class TestNonceState(unittest.TestCase):
    def setUp(self):
        self._orig = (O.build_snapshot, O._load_baseline, O._save_baseline)
        O._save_baseline = lambda *a, **k: None     # no disk writes in tests

    def tearDown(self):
        O.build_snapshot, O._load_baseline, O._save_baseline = self._orig

    def _run(self, wallets, baseline, tracked=True):
        if baseline is not None and "ts" not in baseline:
            baseline = {**baseline, "ts": time.time() - 60}   # recent by default
        O.build_snapshot = lambda t: make_snap(wallets, tracked=tracked)
        O._load_baseline = lambda t: baseline
        return O.build_nonce_state("TST")

    def test_escalation_when_dormant_safe_fires(self):
        # a mapped non-op (distribution) safe ticked 0→1 vs a fresh baseline = the §8 signal
        r = self._run([W("MEGA-SAFE", "distribution", 1), W("HOT", "op", 99999)],
                      baseline={"nonces": {"0xmega-safe": 0, "0xhot": 99998}})
        self.assertEqual(r["signal"], "ESCALATION")
        self.assertEqual(r["score"], -30)
        self.assertEqual(len(r["escalation_fired"]), 1)
        self.assertEqual(r["escalation_fired"][0]["label"], "MEGA-SAFE")
        self.assertTrue(r["baseline_seeded"])
        self.assertFalse(r["baseline_stale"])

    def test_unmapped_feeder_is_low_conf_not_escalation(self):
        # an UNMAPPED feeder ticking is low-confidence churn, NOT a Stage-5 ESCALATION
        r = self._run([W("UNMAPPED-FEEDER-68M", "distribution", 5)],
                      baseline={"nonces": {"0xunmapped-feeder-68m": 3}})
        self.assertEqual(r["signal"], "LOADING")
        self.assertEqual(r["escalation_fired"], [])
        self.assertEqual(len(r["low_conf_fired"]), 1)

    def test_stale_baseline_degrades_escalation(self):
        # mapped safe fired but vs a 7h-old baseline → catch-up, degrade (-15) not silent -30
        r = self._run([W("MEGA-SAFE", "distribution", 1)],
                      baseline={"nonces": {"0xmega-safe": 0}, "ts": time.time() - 7 * 3600})
        self.assertEqual(r["signal"], "ESCALATION")
        self.assertEqual(r["score"], -15)
        self.assertTrue(r["baseline_stale"])

    def test_no_activity_second_run_clears(self):
        # baseline already at current nonce, no chain activity → empty + seeded (DoD)
        r = self._run([W("MEGA-SAFE", "distribution", 5)],
                      baseline={"nonces": {"0xmega-safe": 5}})
        self.assertEqual(r["newly_fired"], [])
        self.assertTrue(r["baseline_seeded"])
        self.assertNotEqual(r["signal"], "ESCALATION")

    def test_op_wallet_tick_is_not_escalation(self):
        # operational/hot wallet ticking is noise, not the signal
        r = self._run([W("HOT", "op", 100000, bal=1.0), W("MEGA-SAFE", "distribution", 0, bal=1.0)],
                      baseline={"nonces": {"0xhot": 99990, "0xmega-safe": 0}})
        self.assertNotEqual(r["signal"], "ESCALATION")

    def test_cex_hot_wallet_tick_is_not_escalation(self):
        # a CEX/exchange hot wallet (e.g. KRAKEN-HOT) ticks constantly — exchange-side noise
        r = self._run([W("KRAKEN-HOT", "cex", 5571, bal=1.0)],
                      baseline={"nonces": {"0xkraken-hot": 5564}})
        self.assertNotEqual(r["signal"], "ESCALATION")

    def test_loading_when_primed_unfired(self):
        r = self._run([W("SAFE-A", "distribution", 0, bal=2.0)],   # gas-primed, nonce 0
                      baseline={"nonces": {"0xsafe-a": 0}})
        self.assertEqual(r["signal"], "LOADING")
        self.assertEqual(r["score"], -10)

    def test_dormant_when_no_gas_no_fire(self):
        r = self._run([W("SAFE-A", "distribution", 0, bal=0.0)],
                      baseline={"nonces": {"0xsafe-a": 0}})
        self.assertEqual(r["signal"], "DORMANT")
        self.assertEqual(r["score"], 10)

    def test_seeded_first_run_no_false_escalation(self):
        # no prior baseline → seed it, no diff possible, baseline_seeded False this run
        r = self._run([W("MEGA-SAFE", "distribution", 1)], baseline=None)
        self.assertFalse(r["baseline_seeded"])
        self.assertEqual(r["signal"], "QUIET")
        self.assertEqual(r["score"], 0)

    def test_untracked(self):
        r = self._run([], baseline=None, tracked=False)
        self.assertFalse(r["tracked"])
        self.assertEqual(r["signal"], "UNTRACKED")

    # ── SPEC-145: unreadable wallets must be counted and never mistaken for clean silence ──
    def test_unreadable_wallets_reported_and_signal_not_clean_quiet(self):
        wallets = [W(f"W{i}", "distribution", 0, bal=0.0) for i in range(7)]
        wallets += [W(f"BAD{i}", "distribution", None, rpc_ok=False,
                      reason="unreadable: all 2 provider(s) failed") for i in range(3)]
        r = self._run(wallets, baseline={"nonces": {}})
        self.assertEqual(r["wallets_total"], 10)
        self.assertEqual(r["wallets_read"], 7)
        self.assertEqual(r["wallets_unreadable"], 3)
        self.assertEqual(len(r["unreadable"]), 3)
        self.assertTrue(all(u["reason"] for u in r["unreadable"]))
        self.assertNotEqual(r["signal"], "QUIET")
        self.assertNotEqual(r["signal"], "DORMANT")

    def test_all_readable_no_movement_is_clean_quiet_zero_unreadable(self):
        # the valid silent case: every wallet read fine, nothing fired
        r = self._run([W("A", "op", 5)], baseline={"nonces": {"0xa": 5}})
        self.assertEqual(r["wallets_unreadable"], 0)
        self.assertEqual(r["unreadable"], [])
        self.assertNotEqual(r["signal"], "PARTIAL")

    def test_escalation_unaffected_by_unreadable_peers(self):
        # a mapped safe fired AND a sibling wallet is unreadable — the escalation must still
        # be the headline (unreadable is additive info, never masks a real fire)
        wallets = [W("MEGA-SAFE", "distribution", 1),
                   W("SIDE", "distribution", None, rpc_ok=False)]
        r = self._run(wallets, baseline={"nonces": {"0xmega-safe": 0}})
        self.assertEqual(r["signal"], "ESCALATION")
        self.assertEqual(r["wallets_unreadable"], 1)

    def test_untracked_reports_zero_coverage(self):
        r = self._run([], baseline=None, tracked=False)
        self.assertEqual(r["wallets_total"], 0)
        self.assertEqual(r["wallets_unreadable"], 0)
        self.assertEqual(r["unreadable"], [])


class TestCoverageAlerts(unittest.TestCase):
    """SPEC-145 req 4 — a surveillance outage is itself news: pure decision over aggregated
    chain read-totals, consumed by build_board / ops/surveil.sh."""

    def test_chain_fully_unreadable_is_high(self):
        totals = {"binance-smart-chain": {"total": 4, "unreadable": 4},
                  "ethereum": {"total": 3, "unreadable": 0}}
        events = O.coverage_alerts(totals, wallets_total=7, wallets_unreadable=4)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["severity"], "HIGH")
        self.assertEqual(events[0]["chain"], "binance-smart-chain")

    def test_fraction_over_quarter_is_high_when_no_chain_fully_dark(self):
        totals = {"binance-smart-chain": {"total": 6, "unreadable": 2},
                  "ethereum": {"total": 6, "unreadable": 2}}
        events = O.coverage_alerts(totals, wallets_total=12, wallets_unreadable=4)
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["chain"])

    def test_under_threshold_and_no_chain_dark_is_clean(self):
        totals = {"binance-smart-chain": {"total": 10, "unreadable": 1}}
        events = O.coverage_alerts(totals, wallets_total=10, wallets_unreadable=1)
        self.assertEqual(events, [])

    def test_zero_tracked_wallets_is_clean(self):
        self.assertEqual(O.coverage_alerts({}, wallets_total=0, wallets_unreadable=0), [])


class TestComposedPicture(unittest.TestCase):
    def setUp(self):
        self._orig = (O.build_snapshot, O._load_baseline, O._save_baseline,
                      O._safe_history_and_flows, O._concentration)
        O._save_baseline = lambda *a, **k: None
        O._load_baseline = lambda t: {"nonces": {}}
        O.build_snapshot = lambda t: make_snap([W("SAFE-A", "distribution", 0, bal=2.0)])
        # safe_history powered by Moralis (covers BSC) — canned available read
        O._safe_history_and_flows = lambda t, days=90: (
            {"available": True, "source": "moralis",
             "wallets": [{"label": "BITGET-DEP", "tier": "distribution", "out_count": 5, "tx_count": 11}]},
            {"available": True, "source": "moralis", "recent": [{"from_label": "BITGET-DEP", "to": "0xabc"}]})
        O._concentration = lambda t: {"available": True, "source": "goplus", "chain": "binance-smart-chain",
                                      "holder_count": 25430, "top1_pct": 20.0, "top10_pct": 55.0, "holders": []}

    def tearDown(self):
        (O.build_snapshot, O._load_baseline, O._save_baseline,
         O._safe_history_and_flows, O._concentration) = self._orig

    def test_full_picture_shape_and_coverage(self):
        o = O.build_onchain("TST")
        for k in ("ticker", "bias", "score", "signal", "nonces", "safe_history",
                  "recent_flows", "vc_overlap", "concentration", "coverage"):
            self.assertIn(k, o)
        self.assertTrue(o["coverage"]["nonces"])              # spine always covered
        self.assertTrue(o["coverage"]["safe_history"])        # Moralis-backed BSC read
        self.assertEqual(o["safe_history"]["source"], "moralis")
        self.assertTrue(o["coverage"]["concentration"])       # SPEC 12 — GoPlus-backed
        self.assertEqual(o["concentration"]["top1_pct"], 20.0)
        self.assertIn("available", o["vc_overlap"])


class TestSpec72FiredSafeDistribution(unittest.TestCase):
    """SPEC-72: a fired tracked safe feeding an operator distribution aggregator must NOT read
    DORMANT / NEUTRAL-CONSTRUCTIVE once the surveillance sweep has consumed the live fire
    (newly_fired:[]). build_onchain resolves the fired safe's flow (verify_wallet path) and one
    bounded chain of hops into the aggregator's dex_swap_sell USDT settlement leg (the 2026-06-15
    BSB QUIET-14.75M → staging → XTOKEN-BILL-AGGREGATOR $2.87M miss)."""

    def setUp(self):
        self._orig = (O.build_snapshot, O._load_baseline, O._save_baseline,
                      O._safe_history_and_flows, O._concentration,
                      O._fired_safe_addresses, O._verify_wallet)
        O._save_baseline = lambda *a, **k: None
        # the live diff is EMPTY (baseline already at the safe's nonce — the sweep consumed it):
        # a dormant, no-gas safe → the spine signal is DORMANT, escalation_fired [].
        O.build_snapshot = lambda t: make_snap([W("SAFE", "distribution", 0, bal=0.0)])
        O._load_baseline = lambda t: {"nonces": {"0xsafe": 0}}
        O._safe_history_and_flows = lambda t, days=90: ({"available": True, "wallets": []},
                                                        {"available": True, "recent": []})
        O._concentration = lambda t: {"available": True, "source": "goplus",
                                      "chain": "binance-smart-chain", "holder_count": 1000,
                                      "top1_pct": 43.0, "top10_pct": 70.0, "holders": []}

    def tearDown(self):
        (O.build_snapshot, O._load_baseline, O._save_baseline,
         O._safe_history_and_flows, O._concentration,
         O._fired_safe_addresses, O._verify_wallet) = self._orig

    @staticmethod
    def _fake_verify(address, token, days=14):
        a = (address or "").lower()
        if a == "0xcbd3c6":   # the fired safe → feeds a staging-internal forwarder
            return {"available": True, "verdict": "DISTRIBUTING",
                    "net_flow_window": -750000, "net_flow_window_usd": -214000,
                    "distribution_mode": "staging-internal",
                    "sell_destinations": [{"address": "0xa8bdd6", "label": None,
                                           "dest_kind": "staging-internal",
                                           "amount": 750000, "count": 1}],
                    "settlement": {"available": False, "mode": None}}
        if a == "0xa8bdd6":   # hop 1: internal forwarder → the operator aggregator
            return {"available": True, "verdict": "DISTRIBUTING",
                    "distribution_mode": "staging-internal",
                    "sell_destinations": [{"address": "0x1ab497", "label": "XTOKEN-BILL-AGGREGATOR",
                                           "dest_kind": "staging-internal",
                                           "amount": 740000, "count": 1}],
                    "settlement": {"available": False, "mode": None}}
        if a == "0x1ab497":   # hop 2: the aggregator dex_swap_sells to USDT
            return {"available": True, "verdict": "DISTRIBUTING",
                    "distribution_mode": "dex_swap_sell",
                    "sell_destinations": [{"address": "0xdex", "label": "PancakeRouter",
                                           "dest_kind": "dex-execution", "amount": 730000, "count": 104}],
                    "settlement": {"available": True, "mode": "dex_swap_sell", "asset": "USDT",
                                   "usd_in": 2870000, "amount_in": 2870000, "n_settlements": 104}}
        return {"available": False}

    def test_fired_safe_to_aggregator_is_not_dormant(self):
        O._verify_wallet = self._fake_verify
        O._fired_safe_addresses = lambda ticker, nonce_state: [
            {"address": "0xcbd3c6", "label": "QUIET-14.75M", "tier": "distribution",
             "chain": "binance-smart-chain"}]
        o = O.build_onchain("BSB")
        self.assertNotEqual(o["signal"], "DORMANT")
        self.assertEqual(o["signal"], "DISTRIBUTING")
        self.assertNotIn("NEUTRAL-CONSTRUCTIVE", o["bias"])
        d = o["distribution"]
        self.assertTrue(d["resolved"])
        self.assertTrue(d["staging"])
        self.assertTrue(d["distributing"])
        self.assertEqual(d["aggregator"]["mode"], "dex_swap_sell")
        self.assertEqual(d["aggregator"]["usd_in"], 2870000)
        self.assertEqual(d["aggregator"]["label"], "XTOKEN-BILL-AGGREGATOR")
        # req 2: the fired safe is surfaced in escalation_fired so the brief newly_fired is non-empty
        labels = [f.get("label") for f in o["nonces"]["escalation_fired"]]
        self.assertIn("QUIET-14.75M", labels)

    def test_no_fire_stays_dormant_no_quota_burn(self):
        calls = []
        O._verify_wallet = lambda *a, **k: calls.append(a) or {"available": False}
        O._fired_safe_addresses = lambda ticker, nonce_state: []
        o = O.build_onchain("BSB")
        self.assertEqual(o["signal"], "DORMANT")
        self.assertIn("NEUTRAL-CONSTRUCTIVE", o["bias"])
        self.assertEqual(calls, [])                 # no fire → no verify_wallet call → no quota burn
        self.assertIsNone(o.get("distribution"))


class TestSpec72FiredSafeSources(unittest.TestCase):
    """SPEC-72: _fired_safe_addresses gates the resolution — it must union the LIVE diff
    (escalation_fired) with the CONSUMED nonce_surveil HIGH alert (the sweep advanced the
    baseline → a later read sees newly_fired:[]; the address is recovered by mapping the
    alert's fired LABEL back to the tracked wallet)."""

    def test_live_escalation_fired_is_a_source(self):
        ns = {"escalation_fired": [{"address": "0xDEAD", "label": "MEGA", "tier": "distribution",
                                    "chain": "binance-smart-chain"}]}
        got = O._fired_safe_addresses("TST", ns, alerts_fn=lambda t: {"events": []})
        self.assertEqual([s["address"] for s in got], ["0xDEAD"])

    def test_consumed_alert_maps_label_to_tracked_wallet(self):
        # LAB is in tracked config; pick a real wallet label/address to map the alert against.
        import json as _json
        tok = _json.loads((ROOT / "config" / "tracked_wallets.json").read_text())["tokens"].get("LAB", {})
        wallets = tok.get("wallets", [])
        if not wallets:
            self.skipTest("LAB has no tracked wallets to map")
        w = wallets[0]
        alert = {"events": [{"source": "nonce_surveil", "severity": "HIGH",
                             "msg": f"dormant safe FIRED [{w['label']}(3→4)] kind=staging — §8 bid-pull/top"}]}
        got = O._fired_safe_addresses("LAB", {"escalation_fired": []}, alerts_fn=lambda t: alert)
        self.assertIn(w["address"].lower(), [s["address"].lower() for s in got])

    def test_no_alert_no_live_fire_is_empty(self):
        got = O._fired_safe_addresses("TST", {"escalation_fired": []}, alerts_fn=lambda t: {"events": []})
        self.assertEqual(got, [])


def _iso_ago(secs):
    """ISO-8601 (…Z) timestamp `secs` seconds before now (the alert ts shape)."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time() - secs, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class TestSpec73RecentDistribution(unittest.TestCase):
    """SPEC-73: a tracked safe that distributed within 24h but >6h ago must still surface a
    24h distribution-memory line (`recent_distribution`) + a non-NEUTRAL bias, while the LIVE
    `signal` reverts to DORMANT (the fire aged past the 6h live-escalation window). The
    2026-06-15 BSB $5.1M/24h case read at T+8h — two safes, one drained to zero."""

    def setUp(self):
        self._orig = (O.build_snapshot, O._load_baseline, O._save_baseline,
                      O._safe_history_and_flows, O._concentration,
                      O._fired_safe_addresses, O._verify_wallet)
        O._save_baseline = lambda *a, **k: None
        # live diff EMPTY (sweep consumed the fire), dormant no-gas safe → spine signal DORMANT
        O.build_snapshot = lambda t: make_snap([W("SAFE", "distribution", 0, bal=0.0)])
        O._load_baseline = lambda t: {"nonces": {"0xsafe": 0}}
        O._safe_history_and_flows = lambda t, days=90: ({"available": True, "wallets": []},
                                                        {"available": True, "recent": []})
        O._concentration = lambda t: {"available": True, "source": "goplus",
                                      "chain": "binance-smart-chain", "holder_count": 1000,
                                      "top1_pct": 43.0, "top10_pct": 70.0, "holders": []}

    def tearDown(self):
        (O.build_snapshot, O._load_baseline, O._save_baseline,
         O._safe_history_and_flows, O._concentration,
         O._fired_safe_addresses, O._verify_wallet) = self._orig

    @staticmethod
    def _fake_verify(address, token, days=14):
        a = (address or "").lower()
        if a == "0xcbd3c6":   # safe A — feeds a staging forwarder → aggregator (still holds float)
            return {"available": True, "verdict": "DISTRIBUTING",
                    "net_flow_window": -750000, "net_flow_window_usd": -214000,
                    "distribution_mode": "staging-internal",
                    "balance_now": {"available": True, "value": 9_000_000.0},
                    "sell_destinations": [{"address": "0xa8bdd6", "label": None,
                                           "dest_kind": "staging-internal", "amount": 750000, "count": 1}],
                    "settlement": {"available": False, "mode": None}}
        if a == "0xa8bdd6":   # hop 1: internal forwarder → the operator aggregator
            return {"available": True, "verdict": "DISTRIBUTING",
                    "distribution_mode": "staging-internal",
                    "balance_now": {"available": True, "value": 100.0},
                    "sell_destinations": [{"address": "0x1ab497", "label": "XTOKEN-BILL-AGGREGATOR",
                                           "dest_kind": "staging-internal", "amount": 740000, "count": 1}],
                    "settlement": {"available": False, "mode": None}}
        if a == "0x1ab497":   # hop 2: the aggregator dex_swap_sells $2.87M to USDT
            return {"available": True, "verdict": "DISTRIBUTING",
                    "distribution_mode": "dex_swap_sell",
                    "balance_now": {"available": True, "value": 50.0},
                    "sell_destinations": [{"address": "0xdex", "label": "PancakeRouter",
                                           "dest_kind": "dex-execution", "amount": 730000, "count": 104}],
                    "settlement": {"available": True, "mode": "dex_swap_sell", "asset": "USDT",
                                   "usd_in": 2870000, "amount_in": 2870000, "n_settlements": 104}}
        if a == "0xq29m":     # safe B — drained to zero, own dex_swap_sell $2.27M
            return {"available": True, "verdict": "DISTRIBUTING",
                    "net_flow_window": -29000000, "net_flow_window_usd": -2270000,
                    "distribution_mode": "dex_swap_sell",
                    "balance_now": {"available": True, "value": 0.0},
                    "sell_destinations": [{"address": "0xdex", "label": "PancakeRouter",
                                           "dest_kind": "dex-execution", "amount": 29000000, "count": 80}],
                    "settlement": {"available": True, "mode": "dex_swap_sell", "asset": "USDT",
                                   "usd_in": 2270000, "amount_in": 2270000, "n_settlements": 80}}
        return {"available": False}

    def test_recent_distribution_surfaces_while_signal_dormant(self):
        O._verify_wallet = self._fake_verify
        # both safes fired 8h ago — past the 6h LIVE window, inside the 24h memory window
        O._fired_safe_addresses = lambda ticker, nonce_state: [
            {"address": "0xcbd3c6", "label": "QUIET-14.75M", "tier": "distribution",
             "chain": "binance-smart-chain", "fired_ts": _iso_ago(8 * 3600)},
            {"address": "0xq29m", "label": "QUIET-29M", "tier": "distribution",
             "chain": "binance-smart-chain", "fired_ts": _iso_ago(8 * 3600)}]
        o = O.build_onchain("BSB")
        # live signal aged out of the 6h window → DORMANT, NOT DISTRIBUTING
        self.assertEqual(o["signal"], "DORMANT")
        rd = o["recent_distribution"]
        self.assertIsNotNone(rd)
        self.assertEqual(rd["window_h"], 24)
        self.assertEqual(rd["n_safes"], 2)
        self.assertEqual(rd["total_usd"], 5140000)          # $2.87M (agg) + $2.27M (direct)
        self.assertEqual(rd["drained_to_zero"], ["QUIET-29M"])
        self.assertIsNotNone(rd["last_fire_ts"])
        # req 2: bias reflects it — RECENT-DISTRIBUTION, never plain NEUTRAL-CONSTRUCTIVE
        self.assertIn("RECENT-DISTRIBUTION", o["bias"])
        self.assertNotIn("NEUTRAL-CONSTRUCTIVE", o["bias"])

    def test_live_fire_still_flips_distributing_and_carries_recent(self):
        O._verify_wallet = self._fake_verify
        # a fire within the 6h LIVE window → SPEC-72 behaviour preserved: signal flips DISTRIBUTING
        O._fired_safe_addresses = lambda ticker, nonce_state: [
            {"address": "0xcbd3c6", "label": "QUIET-14.75M", "tier": "distribution",
             "chain": "binance-smart-chain", "fired_ts": _iso_ago(2 * 3600)}]
        o = O.build_onchain("BSB")
        self.assertEqual(o["signal"], "DISTRIBUTING")
        self.assertIsNotNone(o["recent_distribution"])
        self.assertEqual(o["recent_distribution"]["n_safes"], 1)

    def test_no_distribution_in_24h_reads_clean_no_quota(self):
        calls = []
        O._verify_wallet = lambda *a, **k: calls.append(a) or {"available": False}
        O._fired_safe_addresses = lambda ticker, nonce_state: []
        o = O.build_onchain("BSB")
        self.assertEqual(o["signal"], "DORMANT")
        self.assertIn("NEUTRAL-CONSTRUCTIVE", o["bias"])
        self.assertIsNone(o.get("recent_distribution"))
        self.assertEqual(calls, [])          # no fire → no verify_wallet → no quota burn

    def test_fires_older_than_24h_are_not_recent(self):
        O._verify_wallet = self._fake_verify
        # fired 30h ago — past the 24h memory window → no recent_distribution
        O._fired_safe_addresses = lambda ticker, nonce_state: [
            {"address": "0xq29m", "label": "QUIET-29M", "tier": "distribution",
             "chain": "binance-smart-chain", "fired_ts": _iso_ago(30 * 3600)}]
        o = O.build_onchain("BSB")
        self.assertEqual(o["signal"], "DORMANT")
        self.assertIsNone(o.get("recent_distribution"))
        self.assertNotIn("RECENT-DISTRIBUTION", o["bias"])


class TestSpec75TokenOutAttribution(unittest.TestCase):
    """SPEC-75: the DISTRIBUTION-FIRING per-wallet attribution must be gated on a CONFIRMED
    tracked-token OUT this window. The 2026-06-15 BILL false fire: MM-PROG-60K jumped 48 nonces
    (6295→6343) but moved 0 BILL (n_out=0) — yet it was named the lead distributor. The real
    distributor was TERMINAL-DRAINED (dex_swap_sell, −1.16M BILL, ~$17.8M settled). A nonce-only
    fire (moved nothing) must be demoted to a nonce_only bucket, never named the lead; the headline
    must rank by confirmed token-out magnitude. If NO fired safe confirms a token-out, the label
    downgrades off DISTRIBUTION-FIRING to a nonce-only watch."""

    def setUp(self):
        self._orig = (O.build_snapshot, O._load_baseline, O._save_baseline,
                      O._safe_history_and_flows, O._concentration,
                      O._fired_safe_addresses, O._verify_wallet, O._last_token_out)
        O._save_baseline = lambda *a, **k: None
        O._last_token_out = lambda *a, **k: (None, None, False)   # spine: no network for the token-out read
        O._safe_history_and_flows = lambda t, days=90: ({"available": True, "wallets": []},
                                                        {"available": True, "recent": []})
        O._concentration = lambda t: {"available": True, "source": "goplus",
                                      "chain": "binance-smart-chain", "holder_count": 1000,
                                      "top1_pct": 43.0, "top10_pct": 70.0, "holders": []}
        # two low-nonce distribution safes FIRE this window (live diff) → spine ESCALATION
        O.build_snapshot = lambda t: make_snap([W("MM-PROG-60K", "distribution", 48),
                                                W("TERMINAL-DRAINED", "distribution", 7)])
        O._load_baseline = lambda t: {"nonces": {"0xmm-prog-60k": 0, "0xterminal-drained": 0},
                                      "ts": time.time() - 60}

    def tearDown(self):
        (O.build_snapshot, O._load_baseline, O._save_baseline,
         O._safe_history_and_flows, O._concentration,
         O._fired_safe_addresses, O._verify_wallet, O._last_token_out) = self._orig

    @staticmethod
    def _fired_two(ticker, nonce_state):
        return [{"address": "0xmm-prog-60k", "label": "MM-PROG-60K", "tier": "distribution",
                 "chain": "binance-smart-chain", "fired_ts": None},
                {"address": "0xterminal-drained", "label": "TERMINAL-DRAINED", "tier": "distribution",
                 "chain": "binance-smart-chain", "fired_ts": None}]

    @staticmethod
    def _verify_bill(address, token, days=14):
        a = (address or "").lower()
        if a == "0xmm-prog-60k":   # 48-nonce jump on NON-BILL activity → moved 0 BILL this window
            return {"available": True, "verdict": "DORMANT", "out_count": 0, "in_count": 0,
                    "net_flow_window": 0, "net_flow_window_usd": None,
                    "balance_now": {"available": True, "value": 0.0},
                    "sell_destinations": [], "settlement": {"available": False, "mode": None}}
        if a == "0xterminal-drained":   # the REAL distributor — dex_swap_sell, −1.16M BILL, $17.8M
            return {"available": True, "verdict": "DISTRIBUTING", "out_count": 92, "in_count": 1,
                    "net_flow_window": -1160000, "net_flow_window_usd": -17800000,
                    "distribution_mode": "dex_swap_sell",
                    "balance_now": {"available": True, "value": 0.0},
                    "sell_destinations": [{"address": "0xdex", "label": "PancakeRouter",
                                           "dest_kind": "dex-execution", "amount": 1160000, "count": 92}],
                    "settlement": {"available": True, "mode": "dex_swap_sell", "asset": "USDT",
                                   "usd_in": 17800000, "amount_in": 17800000, "n_settlements": 92}}
        return {"available": False}

    def test_lead_is_confirmed_distributor_not_nonce_jump(self):
        O._fired_safe_addresses = self._fired_two
        O._verify_wallet = self._verify_bill
        o = O.build_onchain("BILL")
        fired = o["nonces"]["escalation_fired"]
        labels = [f.get("label") for f in fired]
        # lead = the wallet that actually moved size, NOT the biggest nonce jump
        self.assertEqual(labels[0], "TERMINAL-DRAINED")
        # the nonce-only fire is EXCLUDED from the FIRING list
        self.assertNotIn("MM-PROG-60K", labels)
        # …and demoted into a separate nonce-only (unconfirmed) bucket
        nonce_only = [f.get("label") for f in o["nonces"].get("nonce_only_fired", [])]
        self.assertIn("MM-PROG-60K", nonce_only)
        # token-level signal stays honest: ≥1 confirmed out → DISTRIBUTION FIRING is still correct
        self.assertIn("TERMINAL-DRAINED", o["bias"])
        self.assertNotIn("MM-PROG-60K", o["bias"])

    def test_all_nonce_only_does_not_read_distribution_firing(self):
        # both fired safes moved 0 of the tracked token this window
        O.build_snapshot = lambda t: make_snap([W("MM-PROG-60K", "distribution", 48),
                                                W("OTHER-DORM", "distribution", 3)])
        O._load_baseline = lambda t: {"nonces": {"0xmm-prog-60k": 0, "0xother-dorm": 0},
                                      "ts": time.time() - 60}
        O._fired_safe_addresses = lambda ticker, ns: [
            {"address": "0xmm-prog-60k", "label": "MM-PROG-60K", "tier": "distribution",
             "chain": "binance-smart-chain", "fired_ts": None},
            {"address": "0xother-dorm", "label": "OTHER-DORM", "tier": "distribution",
             "chain": "binance-smart-chain", "fired_ts": None}]
        O._verify_wallet = lambda address, token, days=14: {
            "available": True, "verdict": "DORMANT", "out_count": 0, "in_count": 0,
            "net_flow_window": 0, "net_flow_window_usd": None,
            "balance_now": {"available": True, "value": 0.0},
            "sell_destinations": [], "settlement": {"available": False, "mode": None}}
        o = O.build_onchain("BILL")
        self.assertNotIn("DISTRIBUTION FIRING", o["bias"])
        self.assertEqual(o["nonces"].get("escalation_fired", []), [])
        nonce_only = [f.get("label") for f in o["nonces"].get("nonce_only_fired", [])]
        self.assertIn("MM-PROG-60K", nonce_only)


class TestSafeHistoryMoralis(unittest.TestCase):
    """_safe_history_and_flows reads BSC via Moralis (bscscan-free can't) — deterministic."""

    def setUp(self):
        self._orig = O._moralis

        class FakeMoralis:
            def tokentx(self, address, contract=None, chain="bsc", days=90, max_pages=2):
                return [{"from_address": address, "to_address": "0xCEXdeadbeef",
                         "value_decimal": "300000", "token_symbol": "LAB",
                         "block_timestamp": "2026-05-14T17:34:13.000Z"}]
        O._moralis = lambda: FakeMoralis()

    def tearDown(self):
        O._moralis = self._orig

    def test_reads_outbound_and_tags(self):
        hist, flows = O._safe_history_and_flows("LAB", days=90)
        self.assertTrue(hist["available"])
        self.assertEqual(hist["source"], "moralis")
        self.assertTrue(any(w["out_count"] >= 1 for w in hist["wallets"]))
        self.assertTrue(flows["available"])
        self.assertEqual(flows["recent"][0]["token"], "LAB")


class TestConcentration(unittest.TestCase):
    """SPEC 12 — concentration via GoPlus (deterministic: monkeypatch the HTTP layer)."""

    def setUp(self):
        self._orig = O._get_json
        O._get_json = lambda url, timeout=15: {"result": {
            "0x7ec43cf65f1663f820427c62a5780b8f2e25593a": {
                "holder_count": "25430",
                "holders": [{"address": "0xaaa", "percent": "0.20", "tag": "", "is_contract": "1", "is_locked": "0"},
                            {"address": "0xbbb", "percent": "0.10", "tag": "Binance", "is_contract": "1", "is_locked": "0"}],
            }}}

    def tearDown(self):
        O._get_json = self._orig

    def test_concentration_populated(self):
        c = O._concentration("LAB")   # LAB has a BSC contract in config
        self.assertTrue(c["available"])
        self.assertEqual(c["source"], "goplus")
        self.assertEqual(c["holder_count"], 25430)
        self.assertEqual(c["top1_pct"], 20.0)
        self.assertEqual(c["top10_pct"], 30.0)
        self.assertEqual(len(c["holders"]), 2)

    def test_untracked_unavailable(self):
        c = O._concentration("ZZZQQ")
        self.assertFalse(c["available"])


class TestBoardSweep(unittest.TestCase):
    """Surveillance sweep across tracked watchlist names; ESCALATION → alerts (deterministic)."""

    def setUp(self):
        self._orig = O.build_nonce_state

        def fake(ticker, ts=None, persist=False):   # SPEC 55: board sweep passes persist=True
            if ticker == "LAB":
                return {"ticker": "LAB", "signal": "ESCALATION", "score": -30, "fired_count": 1,
                        "primed_unfired_count": 0, "escalation_fired": [
                            {"label": "MEGA", "nonce_prev": 0, "nonce_now": 1}], "baseline_seeded": False,
                        "wallets_total": 2, "wallets_read": 2, "wallets_unreadable": 0, "unreadable": [],
                        "wallets": [W("MEGA", "distribution", 1), W("SIDE", "op", 5)]}
            if ticker == "BLIND":
                return {"ticker": "BLIND", "signal": "PARTIAL", "score": 0, "fired_count": 0,
                        "primed_unfired_count": 0, "escalation_fired": [], "baseline_seeded": False,
                        "wallets_total": 2, "wallets_read": 0, "wallets_unreadable": 2,
                        "unreadable": [{"address": "0xa", "chain": "ethereum", "reason": "x"},
                                       {"address": "0xb", "chain": "ethereum", "reason": "x"}],
                        "wallets": [W("A", "op", None, rpc_ok=False, chain="ethereum"),
                                    W("B", "op", None, rpc_ok=False, chain="ethereum")]}
            return {"ticker": ticker, "signal": "QUIET", "score": 0, "fired_count": 0,
                    "primed_unfired_count": 0, "escalation_fired": [], "baseline_seeded": False,
                    "wallets_total": 1, "wallets_read": 1, "wallets_unreadable": 0, "unreadable": [],
                    "wallets": [W("C", "op", 1)]}
        O.build_nonce_state = fake

    def tearDown(self):
        O.build_nonce_state = self._orig

    def test_escalation_surfaces_as_alert(self):
        """build_board()'s no-arg path intersects config/watchlist.json's live tickers
        against tracked_wallets.json — LAB has since retired off the live board (it's
        still in tracked_wallets.json, just no longer in watchlist.json's `tokens`), so
        against the ambient repo state `names` silently drops LAB and no alert fires.
        Inject a temp ROOT carrying its own watchlist.json so this test doesn't drift
        with the live board (a test-isolation bug, not a regression in onchain.py)."""
        orig_root = O.ROOT
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "config").mkdir()
            (root / "config" / "watchlist.json").write_text(
                json.dumps({"tokens": [{"ticker": "LAB"}, {"ticker": "AIOT"}]}))
            O.ROOT = root
            try:
                b = O.build_board()
            finally:
                O.ROOT = orig_root
        self.assertGreater(b["scanned"], 1)
        self.assertEqual([a["ticker"] for a in b["alerts"]], ["LAB"])
        self.assertEqual(b["alerts"][0]["escalation_fired"][0]["label"], "MEGA")

    def test_explicit_ticker_subset(self):
        b = O.build_board(["LAB"])
        self.assertEqual(b["scanned"], 1)
        self.assertEqual(len(b["alerts"]), 1)

    def _with_blind_tracked(self, fn):
        """BLIND isn't a real tracked_wallets.json token — point O.WALLETS at a temp config
        that maps LAB/BLIND/OTHER so build_board's tracked-set filter admits it."""
        orig_wallets = O.WALLETS
        tmpdir = tempfile.TemporaryDirectory()
        wallets_path = Path(tmpdir.name) / "tracked_wallets.json"
        wallets_path.write_text(json.dumps({"tokens": {"LAB": {}, "BLIND": {}, "OTHER": {}}}))
        O.WALLETS = wallets_path
        try:
            return fn()
        finally:
            O.WALLETS = orig_wallets
            tmpdir.cleanup()

    def test_coverage_aggregated_to_top_level(self):
        # SPEC-145 req 2: build_board propagates the per-token coverage fields to the top level
        b = self._with_blind_tracked(lambda: O.build_board(["LAB", "BLIND"]))
        self.assertEqual(b["wallets_total"], 4)         # 2 (LAB) + 2 (BLIND)
        self.assertEqual(b["wallets_unreadable"], 2)
        self.assertEqual(b["wallets_read"], 2)
        self.assertEqual(len(b["unreadable"]), 2)
        self.assertTrue(all(u["ticker"] == "BLIND" for u in b["unreadable"]))

    def test_coverage_collapse_on_chain_fully_dark_is_high(self):
        # BLIND contributes a fully-unreadable ethereum chain across its 2 wallets
        b = self._with_blind_tracked(lambda: O.build_board(["BLIND"]))
        self.assertEqual(len(b["coverage_alerts"]), 1)
        self.assertEqual(b["coverage_alerts"][0]["severity"], "HIGH")
        self.assertEqual(b["coverage_alerts"][0]["chain"], "ethereum")

    def test_clean_sweep_has_no_coverage_alerts(self):
        b = O.build_board(["LAB"])
        self.assertEqual(b["coverage_alerts"], [])


class TestSpec17RateLimitProofing(unittest.TestCase):
    """SPEC 17 — Moralis cache (≤1 call), provider fallback pool, free-RPC nonce."""

    def test_moralis_cache_collapses_repeat_calls(self):
        calls = {"n": 0}

        class Fake:
            def tokentx(self, address, contract=None, chain="bsc", days=180, max_pages=2):
                calls["n"] += 1
                return [{"from_address": address, "to_address": "0xb", "value_decimal": "1"}]
        orig = O._moralis
        O._moralis = lambda: Fake()
        O._MORALIS_CACHE.clear()
        try:
            for _ in range(10):
                O._moralis_tokentx("0xCACHE000000000000000000000000000000000001", contract="0xc", chain="bsc", days=30)
            self.assertEqual(calls["n"], 1)   # 10 calls in <90s TTL → ONE provider hit
        finally:
            O._moralis = orig
            O._MORALIS_CACHE.clear()

    def test_token_transfers_falls_back_to_getlogs(self):
        orig = (O._moralis_tokentx, O._getlogs_transfers)
        def moralis_dead(*a, **k):
            raise O.MoralisError("quota exceeded")
        O._moralis_tokentx = moralis_dead
        O._getlogs_transfers = lambda address, contract, chain_key, decimals=18, **k: \
            [{"from_address": address, "to_address": "0xb", "value_decimal": 5.0, "block_timestamp": None}]
        try:
            txs, source, partial = O.token_transfers("0xA", "0xc", "binance-smart-chain", days=30)
            self.assertEqual(source, "getlogs")
            self.assertTrue(partial)             # getlogs fallback = recent-window only
            self.assertEqual(len(txs), 1)
        finally:
            O._moralis_tokentx, O._getlogs_transfers = orig

    def test_token_transfers_raises_only_when_all_fail(self):
        orig = (O._moralis_tokentx, O._getlogs_transfers)
        def dead(*a, **k):
            raise O.MoralisError("down")
        O._moralis_tokentx = dead
        O._getlogs_transfers = dead
        try:
            with self.assertRaises(O.MoralisError):
                O.token_transfers("0xA", "0xc", "binance-smart-chain")
        finally:
            O._moralis_tokentx, O._getlogs_transfers = orig

    def test_moralis_401_quota_exhausted_named_not_generic_timeout(self):
        # SPEC-123: the 2026-07-14 incident — a Moralis 401 plan-consumed response was
        # retried (2 retries, backoff) until the whole capability blew the orchestrator's
        # subprocess timeout, so the operator only ever saw "timeout running verify_wallet".
        # The 401 must be recognized immediately (no retry) and named.
        calls = {"n": 0}
        err_body = ('{"_err_code": 401, "_err_body": "Validation service blocked: Your '
                    'plan: free-plan-daily total included usage has been consumed..."}')

        class Fake:
            def tokentx(self, address, contract=None, chain="bsc", days=180, max_pages=2):
                calls["n"] += 1
                raise RuntimeError(f"Moralis err: {err_body}")
        orig = O._moralis
        O._moralis = lambda: Fake()
        O._MORALIS_CACHE.clear()
        try:
            with self.assertRaises(O.MoralisQuotaExhausted) as ctx:
                O._moralis_tokentx("0xQUOTA00000000000000000000000000000001",
                                   contract="0xc", chain="bsc", days=30)
            self.assertIn("QUOTA_EXHAUSTED", str(ctx.exception))
            self.assertIn("moralis", str(ctx.exception))
            self.assertEqual(calls["n"], 1)   # no retry — retrying an exhausted quota is futile
        finally:
            O._moralis = orig
            O._MORALIS_CACHE.clear()

    def test_transient_error_still_retries_unlike_quota_exhaustion(self):
        # a non-quota failure (rate-limit blip, transient 5xx) keeps the existing retry path.
        calls = {"n": 0}

        class Fake:
            def tokentx(self, address, contract=None, chain="bsc", days=180, max_pages=2):
                calls["n"] += 1
                raise RuntimeError("Moralis err: {'_err': 'connection reset'}")
        orig = O._moralis
        O._moralis = lambda: Fake()
        O._MORALIS_CACHE.clear()
        try:
            with self.assertRaises(O.MoralisError):
                O._moralis_tokentx("0xTRANSIENT0000000000000000000000000000001",
                                   contract="0xc", chain="bsc", days=30, retries=1)
            self.assertEqual(calls["n"], 2)   # 1 retry, as before
        finally:
            O._moralis = orig
            O._MORALIS_CACHE.clear()

    def test_token_transfers_surfaces_quota_exhausted_over_a_later_generic_failure(self):
        orig = (O._moralis_tokentx, O._getlogs_transfers)

        def quota_dead(*a, **k):
            raise O.MoralisQuotaExhausted("QUOTA_EXHAUSTED (moralis, resets 00:00 UTC): 401")
        O._moralis_tokentx = quota_dead
        O._getlogs_transfers = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("getlogs down"))
        try:
            with self.assertRaises(O.MoralisQuotaExhausted) as ctx:
                O.token_transfers("0xA", "0xc", "binance-smart-chain")
            self.assertIn("QUOTA_EXHAUSTED", str(ctx.exception))
        finally:
            O._moralis_tokentx, O._getlogs_transfers = orig

    def test_nonce_of_parses_free_rpc(self):
        orig = O._rpc
        O._rpc = lambda chain_key, method, params, timeout=12: "0x6d"   # 109
        try:
            self.assertEqual(O.nonce_of("0xabc"), 109)
        finally:
            O._rpc = orig

    def test_nonce_of_none_on_rpc_fail(self):
        orig = O._rpc
        O._rpc = lambda *a, **k: None
        try:
            self.assertIsNone(O.nonce_of("0xabc"))
        finally:
            O._rpc = orig


class TestErc20MetaSpec165(unittest.TestCase):
    """SPEC-165 — RPC-only identity read (no CoinGecko dependency)."""

    def _name_hex(self, s):
        raw = s.encode()
        pad = (-len(raw)) % 32
        return "0x" + format(32, "064x") + format(len(raw), "064x") + (raw + b"\x00" * pad).hex()

    def test_reads_name_decimals_supply_offline(self):
        orig = O._rpc

        def fake_rpc(chain_key, method, params, timeout=12):
            data = params[0]["data"]
            if data == O.NAME_SELECTOR:
                return self._name_hex("Test Debit")
            if data == O.DECIMALS_SELECTOR:
                return hex(18)
            if data == O.TOTALSUPPLY_SELECTOR:
                return hex(1_000_000 * 10**18)
            raise AssertionError(f"unexpected selector {data}")
        O._rpc = fake_rpc
        try:
            m = O.erc20_meta("0x" + "66" * 20, "binance-smart-chain")
        finally:
            O._rpc = orig
        self.assertTrue(m["available"])
        self.assertEqual(m["name"], "Test Debit")
        self.assertEqual(m["decimals"], 18)
        self.assertAlmostEqual(m["total_supply"], 1_000_000.0, places=2)

    def test_degrades_when_all_calls_fail(self):
        orig = O._rpc
        O._rpc = lambda *a, **k: None
        try:
            m = O.erc20_meta("0x" + "66" * 20, "binance-smart-chain")
        finally:
            O._rpc = orig
        self.assertFalse(m["available"])
        self.assertIsNone(m["name"])
        self.assertEqual(m["decimals"], 18)          # EVM default fallback
        self.assertIsNone(m["total_supply"])


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestLiveSmoke(unittest.TestCase):
    def test_onchain_lab_no_hang(self):
        # ACCEPTANCE: orchestrator.py onchain '{"ticker":"LAB"}' returns without hanging.
        t0 = time.time()
        proc = subprocess.run([sys.executable, str(ORCH), "onchain", '{"ticker":"LAB"}'],
                              capture_output=True, text=True, cwd=str(ROOT), timeout=90)
        elapsed = time.time() - t0
        env = json.loads(proc.stdout)
        self.assertTrue(env["ok"], env)
        self.assertTrue(env["data"]["nonces"]["tracked"])
        self.assertLess(elapsed, 30, "onchain must not hang (<30s)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
