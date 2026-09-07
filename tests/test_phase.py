#!/usr/bin/env python3
"""SPEC-104 — `phase`: Wyckoff cycle/event classifier (cycle-first breakout gate).

Run:  python3 -m unittest tests.test_phase -v

Corpus rule under test (memory: feedback_wyckoff_cycle_first_breakout_gate): the same
breakout/breakdown candle is REAL inside accumulation/re-accumulation and BAIT inside
distribution — the cycle, not the candle, decides tradability.

Offline-deterministic: every fixture is a hand-constructed bar sequence; no network.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

spec = importlib.util.spec_from_file_location("phase_t", ROOT / "capabilities" / "phase.py")
PH = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PH)


def bar(o, h, l, c, v):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}


def _flat_bars(n, price=100, vol=100):
    return [bar(price, price + 1, price - 1, price, vol) for _ in range(n)]


def _accumulation_spring_markup_bars():
    """SC(10) -> AR(11) -> ST(14) -> SPRING(20) -> MARKUP_BREAK(21)."""
    bars = _flat_bars(10)
    bars.append(bar(100, 100, 80, 85, 1000))      # SC
    bars.append(bar(85, 96, 85, 95, 300))         # AR (+18.75% off SC low)
    bars.append(bar(95, 95, 90, 90, 200))
    bars.append(bar(90, 90, 86, 87, 150))
    bars.append(bar(87, 87, 83, 84, 140))         # ST (retest near SC low, low vol)
    bars.append(bar(84, 85, 81, 83, 150))
    bars.append(bar(83, 88, 82, 86, 120))
    bars.append(bar(86, 89, 84, 88, 110))
    bars.append(bar(88, 90, 85, 89, 100))
    bars.append(bar(89, 91, 86, 90, 100))
    bars.append(bar(90, 91, 78, 88, 200))         # SPRING (undercuts 80, reclaims same bar)
    bars.append(bar(90, 108, 89, 106, 500))       # MARKUP_BREAK (impulse above range_high=100)
    return bars


def _distribution_utad_breakdown_bars():
    """BC(10) -> AR(11) -> ST(13) -> UTAD(20) -> MARKDOWN_BREAK(21)."""
    bars = _flat_bars(10)
    bars.append(bar(100, 120, 100, 118, 1000))    # BC
    bars.append(bar(118, 118, 105, 108, 300))     # AR (-10% off BC high)
    bars.append(bar(108, 112, 107, 111, 200))
    bars.append(bar(111, 115, 110, 114, 150))     # ST (retest near BC high, low vol)
    bars.append(bar(114, 117, 112, 116, 140))
    bars.append(bar(116, 119, 114, 117, 150))
    bars.append(bar(117, 118, 112, 114, 120))
    bars.append(bar(114, 116, 110, 112, 110))
    bars.append(bar(112, 114, 108, 110, 100))
    bars.append(bar(110, 112, 106, 108, 100))
    bars.append(bar(108, 122, 107, 110, 200))     # UTAD (sweeps 120, fails back below same bar)
    bars.append(bar(108, 109, 95, 97, 500))       # MARKDOWN_BREAK (impulse below range_low=108)
    return bars


class TestClimaxArStDetection(unittest.TestCase):
    def test_sc_detected_on_climactic_down_bar(self):
        bars = _flat_bars(10) + [bar(100, 100, 80, 85, 1000)]
        sc = PH._find_climax(bars, PH.load_cfg(), 0, "down")
        self.assertIsNotNone(sc)
        self.assertEqual(sc["bar_index"], 10)

    def test_no_sc_on_quiet_bars(self):
        bars = _flat_bars(20)
        sc = PH._find_climax(bars, PH.load_cfg(), 0, "down")
        self.assertIsNone(sc)

    def test_bc_requires_up_close_not_down(self):
        bars = _flat_bars(10) + [bar(100, 100, 80, 82, 1000)]   # huge down bar
        bc = PH._find_climax(bars, PH.load_cfg(), 0, "up")
        self.assertIsNone(bc)   # a down climax must never be read as a BC

    def test_ar_requires_minimum_bounce(self):
        cfg = PH.load_cfg()
        sc = {"bar_index": 0, "low": 80, "high": 100, "close": 85, "volume": 1000}
        bars = [None, bar(85, 82, 81, 82, 100)]   # only +2.5% off the SC low — too small
        ar = PH._find_ar(bars, cfg, sc, "down")
        self.assertIsNone(ar)


class TestOiCorroborates(unittest.TestCase):
    def test_none_when_no_series_supplied(self):
        self.assertIsNone(PH._oi_corroborates(None, 5))

    def test_true_on_material_oi_drop(self):
        series = [100, 100, 100, 100, 100, 90]   # -10% at index 5
        self.assertTrue(PH._oi_corroborates(series, 5))

    def test_false_when_oi_flat_or_rising(self):
        series = [100, 100, 100, 100, 100, 105]
        self.assertFalse(PH._oi_corroborates(series, 5))


class TestDodFixtures(unittest.TestCase):
    """The five literal SPEC-104 DoD fixtures."""

    def test_a_sc_ar_st_spring_markup_in_order(self):
        bars = _accumulation_spring_markup_bars()
        events, cycle = PH.classify_events(bars)
        seq = [e["type"] for e in events if e["type"] in
              ("SC", "AR", "ST", "SPRING", "MARKUP_BREAK")]
        self.assertEqual(seq, ["SC", "AR", "ST", "SPRING", "MARKUP_BREAK"])
        # events list overall must be chronologically ordered (bar_index non-decreasing)
        idxs = [e["bar_index"] for e in events]
        self.assertEqual(idxs, sorted(idxs))
        self.assertEqual(cycle, "MARKUP")

    def test_b_distribution_range_utad_breakdown(self):
        bars = _distribution_utad_breakdown_bars()
        events, cycle = PH.classify_events(bars)
        seq = [e["type"] for e in events if e["type"] in ("BC", "AR", "ST", "UTAD", "MARKDOWN_BREAK")]
        self.assertEqual(seq, ["BC", "AR", "ST", "UTAD", "MARKDOWN_BREAK"])
        self.assertEqual(cycle, "DISTRIBUTION")
        # breakdown pass-through: a SHORT signal on this cycle must NOT conflict
        gate = PH.cycle_gate_for("short", cycle)
        self.assertTrue(gate["agree"])
        self.assertFalse(gate["conflict"])

    def test_c_breakdown_inside_accumulation_is_cycle_conflict(self):
        gate = PH.cycle_gate_for("short", "ACCUMULATION")
        self.assertTrue(gate["conflict"])
        self.assertIn("CYCLE_CONFLICT", gate["note"])
        # the mirror: a breakout LONG inside DISTRIBUTION also conflicts
        gate2 = PH.cycle_gate_for("long", "DISTRIBUTION")
        self.assertTrue(gate2["conflict"])

    def test_d_falling_knife_no_held_range_no_spring_label(self):
        # a genuine climax fires, but the very next bar undercuts further and reclaims —
        # no AR/ST ever completes, so no held range exists.
        bars = _flat_bars(10)
        bars.append(bar(100, 100, 80, 85, 1000))     # SC
        bars.append(bar(85, 86, 60, 82, 1200))       # immediate further plunge + partial recover
        bars.append(bar(82, 83, 81, 82.5, 150))
        events, cycle = PH.classify_events(bars)
        self.assertFalse(any(e["type"] == "SPRING" for e in events))
        self.assertIn(cycle, ("UNCLEAR",))   # UNCLEAR is acceptable/expected here

    def test_d_plain_falling_knife_with_no_climax_at_all(self):
        bars = _flat_bars(10) + [bar(100, 100, 90, 95, 150), bar(95, 96, 94, 95, 100)]
        events, cycle = PH.classify_events(bars)
        self.assertEqual(events, [])
        self.assertEqual(cycle, "UNCLEAR")

    def test_e_grind_vs_impulse_breakout_flags_only_the_grind(self):
        level = 100
        grind_bars = [bar(100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100 + (i % 2) * 0.5, 100)
                     for i in range(8)]
        clean_bars = [bar(96, 97, 95, 96, 100), bar(96, 110, 96, 109, 500)]
        r_grind = PH.classify_breakout_leg(grind_bars, level)
        r_clean = PH.classify_breakout_leg(clean_bars, level)
        self.assertTrue(r_grind["grind_trap_suspect"])
        self.assertFalse(r_clean["grind_trap_suspect"])


class TestCycleGate(unittest.TestCase):
    def test_unclear_never_conflicts(self):
        for side in ("short", "long"):
            g = PH.cycle_gate_for(side, "UNCLEAR")
            self.assertFalse(g["conflict"])
            self.assertIsNone(g["agree"])

    def test_unclear_default_when_cycle_missing(self):
        g = PH.cycle_gate_for("short", None)
        self.assertEqual(g["cycle"], "UNCLEAR")
        self.assertFalse(g["conflict"])

    def test_long_agrees_with_accumulation_and_markup(self):
        for c in ("ACCUMULATION", "RE-ACCUMULATION", "MARKUP"):
            self.assertTrue(PH.cycle_gate_for("long", c)["agree"])

    def test_short_agrees_with_distribution_and_markdown(self):
        for c in ("DISTRIBUTION", "RE-DISTRIBUTION", "MARKDOWN"):
            self.assertTrue(PH.cycle_gate_for("short", c)["agree"])

    def test_case_insensitive_side_and_cycle(self):
        g = PH.cycle_gate_for("SHORT", "accumulation")
        self.assertTrue(g["conflict"])


class TestOneLiner(unittest.TestCase):
    def test_available_result_names_cycle_and_last_event(self):
        r = {"available": True, "cycle": "ACCUMULATION",
            "events": [{"type": "SC", "bar_index": 10}, {"type": "ST", "bar_index": 14}]}
        line = PH.one_liner(r)
        self.assertIn("ACCUMULATION", line)
        self.assertIn("ST", line)
        self.assertIn("@14", line)

    def test_unavailable_result_says_unavailable_not_blocking(self):
        r = {"available": False, "reason": "thin history (5 bars < 25 minimum)"}
        line = PH.one_liner(r)
        self.assertIn("unavailable", line)
        self.assertIn("thin history", line)

    def test_none_result_degrades_gracefully(self):
        self.assertIn("unavailable", PH.one_liner(None))


class TestBuildPhase(unittest.TestCase):
    def test_injected_bars_classify_without_network(self):
        bars = _accumulation_spring_markup_bars()
        r = PH.build_phase("OPN", bars=bars, min_bars=5)
        self.assertTrue(r["available"])
        self.assertEqual(r["cycle"], "MARKUP")
        self.assertEqual(r["n_bars"], len(bars))

    def test_thin_history_degrades_without_blocking(self):
        r = PH.build_phase("OPN", bars=_flat_bars(3), min_bars=25)
        self.assertFalse(r["available"])
        self.assertIn("thin history", r["reason"])
        self.assertIsNone(r["cycle"])

    def test_fetch_exception_degrades_not_a_crash(self):
        # bars=None forces the live-fetch path; monkeypatch tape to raise
        orig_spec = importlib.util.spec_from_file_location("tape_phase_t", ROOT / "capabilities" / "tape.py")
        fake_tape = importlib.util.module_from_spec(orig_spec)
        orig_spec.loader.exec_module(fake_tape)

        def boom(*a, **k):
            raise RuntimeError("network exploded")

        fake_tape.fetch_klines_1m = boom
        sys.modules["tape"] = fake_tape
        try:
            r = PH.build_phase("NOPE", bars=None)
        finally:
            sys.modules.pop("tape", None)
        self.assertFalse(r["available"])
        self.assertIn("error", r["reason"].lower())


class TestBriefIntegration(unittest.TestCase):
    def setUp(self):
        BR_spec = importlib.util.spec_from_file_location("brief_phase_t", ROOT / "capabilities" / "brief.py")
        self.BR = importlib.util.module_from_spec(BR_spec)
        BR_spec.loader.exec_module(self.BR)

    def test_headline_leads_with_the_phase_one_liner(self):
        state = {"verdict": "CONFIRMS", "thesis_present": True}
        phase_result = {"available": True, "cycle": "ACCUMULATION",
                        "events": [{"type": "SPRING", "bar_index": 20}]}
        h = self.BR._headline("OPN", state, {"available": False}, {"available": False},
                              {"available": False}, phase=phase_result)
        self.assertIn("phase: ACCUMULATION", h)
        self.assertTrue(h.startswith("phase:"))   # leads the headline (req: cycle-first)

    def test_headline_shows_unavailable_without_blocking(self):
        state = {"verdict": "CONFIRMS", "thesis_present": True}
        phase_result = {"available": False, "reason": "thin history (5 bars < 25 minimum)"}
        h = self.BR._headline("OPN", state, {"available": False}, {"available": False},
                              {"available": False}, phase=phase_result)
        self.assertIn("phase: unavailable", h)

    def test_phase_layer_is_wired_into_build_brief_jobs(self):
        import inspect
        src = inspect.getsource(self.BR.build_brief)
        self.assertIn('"phase"', src)
        self.assertIn("_phase_layer", src)


class TestSetupScoreIntegration(unittest.TestCase):
    def setUp(self):
        SS_spec = importlib.util.spec_from_file_location("setup_score_phase_t", ROOT / "capabilities" / "setup_score.py")
        self.SS = importlib.util.module_from_spec(SS_spec)
        SS_spec.loader.exec_module(self.SS)

    def _breakdown_signals(self, cycle):
        return {
            "funding_phase_match": True, "oi_drop_ls_unfreeze": True,
            "cex_deposits_firing": True, "lower_high": True,
            "breakdown_not_bought_back": True, "cycle": cycle,
        }

    def test_breakdown_triggered_setup_gains_cycle_gate_conflict(self):
        out = self.SS.score_setup("stage45_short", self._breakdown_signals("ACCUMULATION"))
        self.assertIn("cycle_gate", out)
        self.assertTrue(out["cycle_gate"]["conflict"])
        # a warning, never a hard veto: score/verdict unaffected by the conflict
        self.assertEqual(out["score"], 5)

    def test_breakdown_triggered_setup_gains_cycle_gate_agree(self):
        out = self.SS.score_setup("stage45_short", self._breakdown_signals("DISTRIBUTION"))
        self.assertTrue(out["cycle_gate"]["agree"])
        self.assertFalse(out["cycle_gate"]["conflict"])

    def test_no_cycle_gate_field_when_cycle_absent(self):
        signals = self._breakdown_signals("DISTRIBUTION")
        del signals["cycle"]
        out = self.SS.score_setup("stage45_short", signals)
        self.assertNotIn("cycle_gate", out)

    def test_non_breakout_setup_never_gains_cycle_gate(self):
        # defended_fade_short's REQUIRED legs don't include breakdown_not_bought_back/
        # price_trigger (those are add_legs there) — no gate on the required-leg score.
        signals = {"wall_growing": True, "empty_beyond": True, "stall_lower_high": True,
                  "cycle": "ACCUMULATION"}
        out = self.SS.score_setup("defended_fade_short", signals)
        self.assertNotIn("cycle_gate", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
