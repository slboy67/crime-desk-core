#!/usr/bin/env python3
"""SPEC 23 — cross-venue LONG/SHORT positioning leg in `regime_check`.

Run:  python3 tests/test_ls_positioning.py

Binance is the authoritative source (topLongShortPositionRatio / topLongShortAccountRatio /
globalLongShortAccountRatio); Bybit is best-effort (empty list for thin crime-coins →
`unavailable`, NEVER a fabricated 0/flat — the SPEC 10/18 floor-sentinel lesson); Bitget/Aster
always `unavailable`. The §6 short-fire flag fires when top-trader L/S drops ≥15% over the
window and stays QUIET on the live SKYAI slow-drift case. Network is mocked — offline-deterministic.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


RC = _load("regime_check")


def bn_series(ratios, long_pcts=None):
    """Binance L/S endpoint shape: ascending list (oldest→newest)."""
    out = []
    for i, r in enumerate(ratios):
        lp = (long_pcts[i] if long_pcts else r / (1 + r))  # long fraction from ratio if not given
        out.append({"symbol": "XUSDT", "longShortRatio": f"{r}",
                    "longAccount": f"{lp}", "shortAccount": f"{1 - lp}",
                    "timestamp": 1780516800000 + i * 3600000})
    return out


class TestCohortParse(unittest.TestCase):
    def test_latest_is_last_element_and_trend_computed(self):
        # ascending series 2.8 → 2.3 = (2.3-2.8)/2.8 = -17.86%
        c = RC._ls_cohort(bn_series([2.8, 2.6, 2.4, 2.3]))
        self.assertTrue(c["available"])
        self.assertEqual(c["venue"], "binance")
        self.assertAlmostEqual(c["ratio"], 2.3, places=3)
        self.assertAlmostEqual(c["trend_pct"], -17.86, places=1)
        self.assertEqual(c["n"], 4)
        # long%/short% surfaced and sum ~100
        self.assertAlmostEqual(c["long_pct"] + c["short_pct"], 100.0, places=1)

    def test_empty_series_is_unavailable_not_zero(self):
        c = RC._ls_cohort([])
        self.assertFalse(c["available"])
        self.assertIsNone(c.get("ratio"))               # never a fabricated 0/flat


class TestSignals(unittest.TestCase):
    def test_short_fire_on_15pct_top_trader_drop(self):
        cohorts = {
            "top_position": RC._ls_cohort(bn_series([2.8, 2.6, 2.4, 2.3])),   # -17.9%
            "top_account": RC._ls_cohort(bn_series([1.5, 1.45, 1.4, 1.38])),  # -8%
            "global": RC._ls_cohort(bn_series([1.2, 1.18, 1.15, 1.14])),
        }
        sig = RC._ls_signals(cohorts)
        self.assertTrue(sig["short_fire"])                  # §6: top-trader L/S dropped ≥15%
        self.assertEqual(sig["short_fire_cohort"], "top_position")
        self.assertLessEqual(sig["worst_top_trend_pct"], -15.0)

    def test_quiet_on_slow_drift_live_skyai_case(self):
        # the live SKYAI read: top-position 2.41 → 2.39 (~-0.8%), must NOT false-fire (DoD)
        cohorts = {
            "top_position": RC._ls_cohort(bn_series([2.41, 2.40, 2.40, 2.39], [0.707, 0.706, 0.706, 0.7065])),
            "top_account": RC._ls_cohort(bn_series([1.29, 1.29, 1.28, 1.2784], [0.563, 0.562, 0.561, 0.5611])),
            "global": RC._ls_cohort(bn_series([1.15, 1.15, 1.145, 1.145], [0.534, 0.534, 0.533, 0.5338])),
        }
        sig = RC._ls_signals(cohorts)
        self.assertFalse(sig["short_fire"])                 # slow <5% drift → quiet
        self.assertEqual(sig["crowdedness"], "long-crowded")  # 70% long = bags, short-side uncrowded (§7)


class TestLsBlockVenueFallback(unittest.TestCase):
    """Binance-primary; Bybit empty → unavailable (not 0); Bitget/Aster always unavailable."""
    def setUp(self):
        self._orig = RC.fetch

        def fake(url):
            if "topLongShortPositionRatio" in url:
                return bn_series([2.41, 2.40, 2.40, 2.4077], [0.706, 0.706, 0.706, 0.7065])
            if "topLongShortAccountRatio" in url:
                return bn_series([1.29, 1.28, 1.28, 1.2784], [0.562, 0.561, 0.561, 0.5611])
            if "globalLongShortAccountRatio" in url:
                return bn_series([1.15, 1.15, 1.145, 1.1450], [0.534, 0.533, 0.533, 0.5338])
            if "account-ratio" in url:                      # bybit — empty list for thin symbol
                return {"retCode": 0, "result": {"list": []}}
            return {"_error": "unexpected"}
        RC.fetch = fake

    def tearDown(self):
        RC.fetch = self._orig

    def test_binance_primary_bybit_unavailable_bitget_unavailable(self):
        ls = RC.build_ls("SKYAI")
        self.assertEqual(ls["source"], "binance")
        # reproduces the hand-curl: top-position ≈2.41 / 70% long, global ≈1.145
        tp = ls["cohorts"]["top_position"]
        self.assertAlmostEqual(tp["ratio"], 2.4077, places=2)
        self.assertAlmostEqual(tp["long_pct"], 70.65, places=1)
        self.assertAlmostEqual(ls["cohorts"]["global"]["ratio"], 1.1450, places=2)
        # Bybit unavailable (empty list) — NOT a fabricated number
        self.assertFalse(ls["venues"]["bybit"]["available"])
        self.assertIsNotNone(ls["venues"]["bybit"]["reason"])
        # Bitget + Aster always unavailable, with a reason, no synthesized ratio
        self.assertFalse(ls["venues"]["bitget"]["available"])
        self.assertFalse(ls["venues"]["aster"]["available"])
        # slow drift → no false short-fire
        self.assertFalse(ls["signals"]["short_fire"])


class TestBuildRegimeWiresLs(unittest.TestCase):
    def test_build_regime_includes_ls_block(self):
        self._orig = RC.build_ls
        RC.build_ls = lambda sym: {"_sentinel": True, "source": "binance"}
        try:
            # stub all network so build_regime doesn't hit the wire
            self._of = RC.fetch
            RC.fetch = lambda url: {"_error": "stubbed"}
            RC._live_funding_pct = lambda v, s: None
            r = RC.build_regime("SKYAI")
        finally:
            RC.build_ls = self._orig
            RC.fetch = self._of
        self.assertIn("ls", r)
        self.assertTrue(r["ls"].get("_sentinel"))           # leg is wired into the contract


if __name__ == "__main__":
    unittest.main(verbosity=2)
