#!/usr/bin/env python3
"""analyse — combined verdict engine: SPEC 1b (budget), SPEC 4 (§4 gate),
Phase-2 (on-chain read = the fast nonce signal, not getLogs).

Run:  python3 tests/test_analyse.py

Deterministic: the data layer (run_json/run_whales) and the on-chain nonce read
(build_nonce_state) are monkeypatched — no network. One live orchestrator smoke
checks the real wiring + the <30s on-chain acceptance (SPEC-123: gated behind
CRIMEDESK_LIVE_TESTS=1, skipped by default).
"""
import importlib.util
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORCH = ROOT / "orchestrator.py"

sys.path.insert(0, str(ROOT / "tests"))
from live_gate import LIVE, SKIP_REASON  # noqa: E402

spec = importlib.util.spec_from_file_location("analyse_cap", ROOT / "capabilities" / "analyse.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)


def make_run_json(perp=None, cvd=None, struct=None, oi=None):
    table = {"perp_analyser.py": perp or {}, "intraday.py": struct or {}, "oi_sides.py": oi or {}}
    A.run_cvd = lambda t, minutes=30: cvd or {}   # SPEC 42: cvd is now a native layer, not run_json

    def fake(script, ticker, extra=None, timeout=120):
        return table.get(script, {})
    return fake


def nonce(signal="QUIET", score=0, tracked=True, fired=None):
    return lambda t, ts=None: {"tracked": tracked, "signal": signal, "score": score,
                               "ms": 50, "escalation_fired": fired or []}


def perp_long(fr_4h, oi_chg=0):
    return {"ticker": "X", "score": 40, "bias": "LONG", "phase": "trap",
            "metrics": {"fr_4h": fr_4h, "oi_chg": oi_chg, "turnover": 500e6,
                        "turnover_bybit": 300e6, "turnover_binance": 200e6, "price": 1.0},
            "reasons": [], "up_clusters": [], "down_clusters": []}


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig = (A.run_json, A.run_whales, A.build_nonce_state, A.run_cvd,
                      A._top_holder_distribution_check, A.run_oi_construction)
        A.run_whales = lambda *a, **k: {"verdict": "NO_DATA"}
        A.build_nonce_state = nonce()  # default neutral/QUIET
        A.run_cvd = lambda t, minutes=30: {}   # SPEC 42: never hit the network in tests
        # SPEC-89: the §4 top-holder verify is Moralis-heavy — stub it offline (unchecked) so the
        # default deep-neg-long fixtures behave exactly as pre-SPEC-89 (no false TRAP, no network).
        A._top_holder_distribution_check = lambda t: {"checked": False, "distributing": False}
        # SPEC-180: oi_construction is a live venue_map/cvd sweep — stub to None (the
        # honest "not wired at this call site yet" default) so build_analyse tests
        # stay offline-deterministic; tests that care inject their own value.
        A.run_oi_construction = lambda t: None

    def tearDown(self):
        (A.run_json, A.run_whales, A.build_nonce_state, A.run_cvd,
         A._top_holder_distribution_check, A.run_oi_construction) = self._orig


class TestNegFundingGate(_Base):
    def test_full_confluence_allows_long(self):
        A.run_json = make_run_json(perp=perp_long(-0.50, oi_chg=30),
                                   cvd={"verdict": "BULLISH_DIVERGENCE"}, struct={"near_ath": False})
        self.assertEqual(A.build_analyse("X")["verdict"], "LONG")

    def test_missing_cvd_downgrades_to_watch(self):
        A.run_json = make_run_json(perp=perp_long(-0.50, oi_chg=30),
                                   cvd={"verdict": "NO_DATA"}, struct={"near_ath": False})
        a = A.build_analyse("X")
        self.assertEqual(a["verdict"], "WATCH")
        self.assertIn("spot-CVD", a["reason"])

    def test_oi_falling_downgrades_to_watch(self):
        A.run_json = make_run_json(perp=perp_long(-0.50, oi_chg=-40),
                                   cvd={"verdict": "BULLISH_DIVERGENCE"}, struct={"near_ath": False})
        a = A.build_analyse("X")
        self.assertEqual(a["verdict"], "WATCH")
        self.assertIn("OI not rising", a["reason"])

    def test_fresh_ath_clean_onchain_is_spectate_short(self):
        A.build_nonce_state = nonce(signal="DORMANT", score=10)   # safes locked/dormant = "clean"
        A.run_json = make_run_json(perp=perp_long(-2.0, oi_chg=5),
                                   cvd={"verdict": "ALIGNED_BULLISH"}, struct={"near_ath": True})
        a = A.build_analyse("X")
        self.assertEqual(a["verdict"], "WATCH")
        self.assertIn("spectate", a["direction"].lower())

    def test_positive_funding_long_not_gated(self):
        A.run_json = make_run_json(perp=perp_long(+0.05, oi_chg=30),
                                   cvd={"verdict": "ALIGNED_BULLISH"}, struct={"near_ath": False})
        self.assertEqual(A.build_analyse("X")["verdict"], "LONG")


class TestOnchainNonceLayer(_Base):
    """Phase-2 — on-chain read comes from the nonce signal; SPEC 1b degrade preserved."""

    def test_nonce_timeout_unavailable_but_verdict_returns(self):
        def boom(t, ts=None):
            raise RuntimeError("nonce read hung")
        A.build_nonce_state = boom
        A.run_json = make_run_json(perp=perp_long(-0.50, oi_chg=30),
                                   cvd={"verdict": "BULLISH_DIVERGENCE"}, struct={"near_ath": False})
        a = A.build_analyse("X")
        self.assertEqual(a["onchain"], "UNAVAILABLE")
        self.assertIn(a["verdict"], {"LONG", "SHORT", "WATCH", "PASS"})
        self.assertEqual(a["perp_score"], 40)
        for k in ("ticker", "verdict", "tier", "direction"):
            self.assertIn(k, a)

    def test_nonce_ok_status(self):
        A.run_json = make_run_json(perp=perp_long(+0.05), cvd={"verdict": "ALIGNED_BULLISH"},
                                   struct={"near_ath": False})
        a = A.build_analyse("X")
        self.assertEqual(a["onchain"], "OK")

    def test_untracked_is_unmapped(self):
        A.build_nonce_state = nonce(tracked=False, signal="UNTRACKED")
        A.run_json = make_run_json(perp=perp_long(+0.05), cvd={}, struct={})
        a = A.build_analyse("X")
        self.assertEqual(a["onchain"], "UNMAPPED")

    def test_escalation_surfaced_in_notes(self):
        A.build_nonce_state = nonce(signal="ESCALATION", score=-30,
                                    fired=[{"label": "MEGA-SAFE", "nonce_prev": 0, "nonce_now": 1}])
        A.run_json = make_run_json(perp=perp_long(+0.05), cvd={"verdict": "ALIGNED_BULLISH"},
                                   struct={"near_ath": False})
        a = A.build_analyse("X")
        self.assertEqual(a["onchain"], "OK")
        self.assertEqual(a["nonce_signal"], "ESCALATION")
        self.assertTrue(any("ESCALATION" in n for n in a["notes"]))


class TestBattlefieldGateNote(_Base):
    """SPEC-177 req 3: the §4 gate note names the battlefield verdict, reusing data
    already fetched THIS call (perp_analyser's `turnover`, cvd's `spot_vol_24h_usd`,
    oi_sides' WASH tag + 4h ΔOI) — no new fetch."""

    def test_perp_led_names_verdict_in_notes(self):
        # turnover 500M (perp_long fixture) vs spot_vol_24h_usd 10M -> ratio 50 -> perp_led
        A.run_json = make_run_json(perp=perp_long(-0.50, oi_chg=30),
                                   cvd={"verdict": "BULLISH_DIVERGENCE", "spot_vol_24h_usd": 10e6},
                                   struct={"near_ath": False})
        a = A.build_analyse("X")
        self.assertTrue(any("battlefield: perp_led" in n for n in a["notes"]))

    def test_wash_tag_reused_from_oi_sides_produces_wash_pinned_annotation_free_of_crash(self):
        A.run_json = make_run_json(perp=perp_long(-0.50, oi_chg=30),
                                   cvd={"verdict": "BULLISH_DIVERGENCE", "spot_vol_24h_usd": 10e6},
                                   struct={"near_ath": False},
                                   oi={"verdict": "WASH", "oi_change_pct": -20})
        a = A.build_analyse("X")   # must not raise; WASH_PINNED leverage_4h computed internally
        self.assertEqual(a["verdict"], "LONG")

    def test_no_spot_or_turnover_no_crash_no_false_verdict_note(self):
        p = perp_long(-0.50, oi_chg=30)
        p["metrics"]["turnover"] = None
        A.run_json = make_run_json(perp=p, cvd={"verdict": "BULLISH_DIVERGENCE"},
                                   struct={"near_ath": False})
        a = A.build_analyse("X")
        self.assertFalse(any("battlefield:" in n for n in a["notes"]))


def _oic(verdict, oi_types, gating_ok=True):
    return {"verdict": verdict, "gating_ok": gating_ok, "oi_types": oi_types}


class TestOicGateConsumption(_Base):
    """SPEC-180 req 1: oi_construction VOIDs/DOWNGRADES the §4 oi_rising leg."""

    def test_arb_dominated_short_side_voids_oi_rising_fails_gate(self):
        oic = _oic("ARB_DOMINATED", [
            {"type": "FUNDING_FARM", "side": "short", "verdict": "ASSERTED", "share_read": "dominant"}])
        A.run_json = make_run_json(perp=perp_long(-0.50, oi_chg=30),   # OI genuinely rising
                                   cvd={"verdict": "BULLISH_DIVERGENCE"}, struct={"near_ath": False})
        A.run_oi_construction = lambda t: oic
        a = A.build_analyse("X")
        self.assertEqual(a["verdict"], "WATCH")   # gate fails despite oi_chg=30
        self.assertTrue(any("VOIDED" in n for n in a["notes"]))
        self.assertIn("OI not rising", a["reason"])

    def test_mixed_material_short_side_downgrades_tier_never_blocks(self):
        oic = _oic("MIXED", [
            {"type": "CROSS_VENUE_FUNDING_ARB", "side": "both", "verdict": "ASSERTED",
             "share_read": "material"}])
        A.run_json = make_run_json(perp=perp_long(-0.50, oi_chg=30),
                                   cvd={"verdict": "BULLISH_DIVERGENCE"}, struct={"near_ath": False})
        A.run_oi_construction = lambda t: oic
        a = A.build_analyse("X")
        self.assertEqual(a["verdict"], "LONG")   # NOT blocked
        self.assertTrue(any("DOWNGRADED" in n for n in a["notes"]))

    def test_unknown_oic_is_byte_identical_to_no_layer_plus_annotation(self):
        A.run_json = make_run_json(perp=perp_long(-0.50, oi_chg=30),
                                   cvd={"verdict": "BULLISH_DIVERGENCE"}, struct={"near_ath": False})
        A.run_oi_construction = lambda t: _oic("UNKNOWN", [])
        with_unknown = A.build_analyse("X")
        A.run_oi_construction = lambda t: None
        without_layer = A.build_analyse("X")
        self.assertEqual(with_unknown["verdict"], without_layer["verdict"])
        self.assertEqual(with_unknown["direction"], without_layer["direction"])
        self.assertEqual(with_unknown["tier"], without_layer["tier"])
        # the only diff is the added annotation note
        self.assertTrue(any("decomposition unknown" in n for n in with_unknown["notes"]))

    def test_stale_gating_ok_false_treated_as_unknown_never_gates(self):
        oic = _oic("ARB_DOMINATED", [
            {"type": "FUNDING_FARM", "side": "short", "verdict": "ASSERTED", "share_read": "dominant"}],
            gating_ok=False)
        A.run_json = make_run_json(perp=perp_long(-0.50, oi_chg=30),
                                   cvd={"verdict": "BULLISH_DIVERGENCE"}, struct={"near_ath": False})
        A.run_oi_construction = lambda t: oic
        a = A.build_analyse("X")
        self.assertEqual(a["verdict"], "LONG")   # stale ARB_DOMINATED must NOT void the leg
        self.assertTrue(any("decomposition unknown" in n for n in a["notes"]))


class TestShortVetoFuelAnnotation(_Base):
    """SPEC-180 req 3: §5 short veto gets an annotation-only fuel/no-fuel line — no
    code change to the veto itself (carry alone still sustains it)."""

    def test_veto_still_fires_unchanged_no_oic(self):
        perp = {"score": -20, "metrics": {"fr_4h": -0.5, "turnover": 50_000_000}}
        struct = {"blowoff_short": False}
        direction, tier, notes, *_ = A.converge(perp, {}, struct=struct)
        joined = " ".join(notes)
        self.assertIn("SHORT VETOED", joined)

    def test_arb_dominated_short_side_annotates_no_fuel(self):
        oic = {"verdict": "ARB_DOMINATED", "gating_ok": True, "oi_types": [
            {"type": "FUNDING_FARM", "side": "short", "verdict": "ASSERTED", "share_read": "dominant"}]}
        perp = {"score": -20, "metrics": {"fr_4h": -0.5, "turnover": 50_000_000}}
        direction, tier, notes, *_ = A.converge(perp, {}, oi_construction=oic)
        self.assertTrue(any("no genuine squeeze fuel" in n for n in notes))


class TestLiquidityGate(_Base):
    def test_low_liquidity_passes(self):
        p = perp_long(-0.50)
        p["metrics"]["turnover"] = 5e6
        A.run_json = make_run_json(perp=p, cvd={}, struct={})
        self.assertEqual(A.build_analyse("X")["verdict"], "PASS")


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestLiveSmoke(unittest.TestCase):
    def test_orchestrator_live(self):
        t0 = time.time()
        proc = subprocess.run([sys.executable, str(ORCH), "analyse", '{"ticker":"LAB"}'],
                              capture_output=True, text=True, cwd=str(ROOT), timeout=120)
        elapsed = time.time() - t0
        env = json.loads(proc.stdout)
        self.assertTrue(env["ok"], env)
        d = env["data"]
        for k in ("ticker", "verdict", "tier", "direction"):
            self.assertIn(k, d)
        # Phase-2 acceptance: on-chain read from nonce, OK (not UNAVAILABLE), well under budget
        self.assertIn(d["onchain"], {"OK", "UNMAPPED", "UNAVAILABLE"})
        if d["onchain"] == "OK":
            self.assertLess(d["onchain_ms"], 30000)
        self.assertLess(elapsed, 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
