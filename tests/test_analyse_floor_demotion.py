#!/usr/bin/env python3
"""SPEC-71 — analyse (the perp capability): a floor print must not win funding selection.

Run:  python3 tests/test_analyse_floor_demotion.py

Two floor leaks in analyse's funding path, both fixed here:

  1. `_hardened_funding_4h` picked the most-negative venue with min() — a +0.005 floor
     placeholder beat a real +0.02 print (less positive = "more vetoing"). Floor prints
     are now DEMOTED: the most-vetoing REAL (non-floor) venue wins; only when every
     covered venue is floored does a floor print return, flagged.
  2. perp_analyser's own Bybit live print fed `fr_4h` straight through with NO floor
     check and NO venue attribution (`funding_venue` null — the BILL incident).
     `_backfill_perp` now demotes a floor-sentinel print to the cross-venue real rate
     when one exists, flags `floor_suspect`/`all_floor` when none does, and always
     attributes the venue.

Floor sentinel = the ±0.005%/interval placeholder (memory:
feedback_funding_0005_is_placeholder_not_flat — it once masked −1.65%/4h).
Network mocked — offline-deterministic.
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


AN = _load("analyse")


class TestHardenedFundingFloorDemotion(unittest.TestCase):
    def setUp(self):
        self._lf = AN.RC._live_funding_pct
        self._iv = AN._funding_interval_min
        AN._funding_interval_min = lambda v, s: 240

    def tearDown(self):
        AN.RC._live_funding_pct, AN._funding_interval_min = self._lf, self._iv

    def test_floor_print_demoted_when_a_real_print_exists(self):
        # the BILL shape: one venue at the +0.005 floor, another with a REAL +0.02
        AN.RC._live_funding_pct = lambda v, s: {"bybit": 0.005, "binance": 0.02, "aster": None}[v]
        fr4, venue, floor_state = AN._hardened_funding_4h("BILL")
        self.assertEqual(venue, "binance")                     # real beats floor
        self.assertAlmostEqual(fr4, 0.02, places=4)
        self.assertIsNone(floor_state)

    def test_negative_floor_does_not_mask_real_negative(self):
        AN.RC._live_funding_pct = lambda v, s: {"bybit": -0.005, "binance": -1.2, "aster": None}[v]
        fr4, venue, floor_state = AN._hardened_funding_4h("X")
        self.assertEqual(venue, "binance")
        self.assertAlmostEqual(fr4, -1.2, places=4)
        self.assertIsNone(floor_state)

    def test_all_floor_two_venues_is_corroborated_flat(self):
        AN.RC._live_funding_pct = lambda v, s: {"bybit": 0.005, "binance": -0.005, "aster": None}[v]
        fr4, venue, floor_state = AN._hardened_funding_4h("X")
        self.assertEqual(floor_state, "all_floor")             # ≥2 floored = genuine ~0% (SPEC 19/44)
        self.assertEqual(venue, "binance")                     # still the most-vetoing of them
        self.assertAlmostEqual(fr4, -0.005, places=4)

    def test_lone_floor_venue_is_suspect(self):
        AN.RC._live_funding_pct = lambda v, s: 0.005 if v == "bybit" else None
        fr4, venue, floor_state = AN._hardened_funding_4h("X")
        self.assertEqual(floor_state, "suspect")               # single source, no corroboration (§3)
        self.assertEqual(venue, "bybit")
        self.assertAlmostEqual(fr4, 0.005, places=4)

    def test_all_fail_returns_none_triple(self):
        AN.RC._live_funding_pct = lambda v, s: None
        self.assertEqual(AN._hardened_funding_4h("X"), (None, None, None))


class TestBackfillFloorDemotion(unittest.TestCase):
    def setUp(self):
        self._o = (AN._hardened_funding_4h, AN._cross_venue_price)
        AN._cross_venue_price = lambda t: (0.0305, "binance")

    def tearDown(self):
        AN._hardened_funding_4h, AN._cross_venue_price = self._o

    def _metrics(self, perp):
        return AN._backfill_perp(perp, "BILL")["metrics"]

    def test_floor_fr4h_demoted_to_the_real_venue(self):
        # perp_analyser's Bybit live print is the floor sentinel; binance has the real rate
        AN._hardened_funding_4h = lambda t: (0.0225, "binance", None)
        m = self._metrics({"score": 0, "metrics": {"fr_4h": 0.005, "funding": 0.005,
                                                   "interval": 240, "price": 0.0305}})
        self.assertEqual(m["fr_4h"], 0.0225)
        self.assertEqual(m["funding_venue"], "binance")
        self.assertTrue(m["funding_floor_demoted"])
        self.assertNotIn("floor_suspect", m)

    def test_floor_everywhere_keeps_floor_but_flags_suspect(self):
        AN._hardened_funding_4h = lambda t: (0.005, "bybit", "suspect")
        m = self._metrics({"score": 0, "metrics": {"fr_4h": 0.005, "funding": 0.005,
                                                   "interval": 240, "price": 0.0305}})
        self.assertEqual(m["fr_4h"], 0.005)                    # still reported …
        self.assertEqual(m["funding_venue"], "bybit")
        self.assertTrue(m["floor_suspect"])                    # … but never a confident flat

    def test_corroborated_all_floor_is_flat_not_suspect(self):
        AN._hardened_funding_4h = lambda t: (0.005, "bybit", "all_floor")
        m = self._metrics({"score": 0, "metrics": {"fr_4h": 0.005, "funding": 0.005,
                                                   "interval": 240, "price": 0.0305}})
        self.assertEqual(m["fr_4h"], 0.005)
        self.assertTrue(m["all_floor"])                        # genuine ~0% (SPEC 44 semantics)
        self.assertNotIn("floor_suspect", m)

    def test_demotion_fetch_failure_keeps_floor_flagged(self):
        AN._hardened_funding_4h = lambda t: (None, None, None)
        m = self._metrics({"score": 0, "metrics": {"fr_4h": 0.005, "funding": 0.005,
                                                   "interval": 240, "price": 0.0305}})
        self.assertEqual(m["fr_4h"], 0.005)
        self.assertEqual(m["funding_venue"], "bybit")
        self.assertTrue(m["floor_suspect"])

    def test_real_bybit_print_passes_through_with_venue(self):
        called = []
        AN._hardened_funding_4h = lambda t: called.append(t)   # must NOT be consulted
        m = self._metrics({"score": 0, "metrics": {"fr_4h": -0.40, "funding": -0.40,
                                                   "interval": 240, "price": 0.0305}})
        self.assertEqual(m["fr_4h"], -0.40)                    # untouched
        self.assertEqual(m["funding_venue"], "bybit")          # attributed, never null (req 2)
        self.assertEqual(called, [])

    def test_normalized_floor_is_caught_via_raw_print(self):
        # 8h-interval floor: raw 0.005%/8h normalizes to fr_4h 0.0025 — the RAW per-interval
        # print is the sentinel; demotion must still fire
        AN._hardened_funding_4h = lambda t: (0.018, "binance", None)
        m = self._metrics({"score": 0, "metrics": {"fr_4h": 0.0025, "funding": 0.005,
                                                   "interval": 480, "price": 0.0305}})
        self.assertEqual(m["fr_4h"], 0.018)
        self.assertEqual(m["funding_venue"], "binance")


class TestBuildAnalyseSurfacesFlags(unittest.TestCase):
    """The output envelope carries floor_suspect + a populated funding_venue."""

    def setUp(self):
        self._orig = (AN.run_json, AN.run_whales, AN.build_nonce_state, AN.run_cvd,
                      AN._hardened_funding_4h, AN._cross_venue_price, AN.run_oi_construction)
        AN.run_whales = lambda *a, **k: {"verdict": "NO_DATA"}
        AN.build_nonce_state = lambda t, ts=None: {"tracked": True, "signal": "QUIET",
                                                   "score": 0, "ms": 5, "escalation_fired": []}
        AN.run_cvd = lambda t, minutes=30: {}
        AN._cross_venue_price = lambda t: (0.0305, "binance")
        AN.run_oi_construction = lambda t: None   # SPEC-180: keep offline-deterministic

    def tearDown(self):
        (AN.run_json, AN.run_whales, AN.build_nonce_state, AN.run_cvd,
         AN._hardened_funding_4h, AN._cross_venue_price, AN.run_oi_construction) = self._orig

    def _run(self, fr_4h, funding, hardened):
        AN._hardened_funding_4h = lambda t: hardened
        perp = {"ticker": "BILL", "score": 0, "bias": "WATCH", "phase": "chop",
                "metrics": {"fr_4h": fr_4h, "funding": funding, "interval": 240,
                            "oi_chg": 2, "turnover": 50e6, "price": 0.0305},
                "reasons": [], "up_clusters": [], "down_clusters": []}
        table = {"perp_analyser.py": perp, "intraday.py": {}, "oi_sides.py": {}}
        AN.run_json = lambda script, ticker, extra=None, timeout=120: table.get(script, {})
        return AN.build_analyse("BILL")

    def test_demoted_envelope(self):
        a = self._run(0.005, 0.005, (0.0225, "binance", None))
        self.assertEqual(a["funding_4h"], 0.0225)
        self.assertEqual(a["funding_venue"], "binance")
        self.assertFalse(a["floor_suspect"])
        self.assertTrue(any("floor" in n for n in a["notes"]))   # the demotion is named

    def test_suspect_envelope(self):
        a = self._run(0.005, 0.005, (0.005, "bybit", "suspect"))
        self.assertEqual(a["funding_4h"], 0.005)
        self.assertEqual(a["funding_venue"], "bybit")
        self.assertTrue(a["floor_suspect"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
