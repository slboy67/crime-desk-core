#!/usr/bin/env python3
"""SPEC-147 — unify the fill rule: the board and the paper record must agree what filled,
and at what price.

Before this, classify.eval_price_leg decided WHETHER a thesis filled (range-intersection)
and counterfactual._infer_mode separately decided AT WHAT PRICE (breakdown -> zone top,
fade -> zone bottom, defaulting to zone bottom whenever there was no commit-price
reference) -- so the two could agree a thesis filled and still disagree about its R, the
exact number the desk's paper track record is made of.

`thesis.fill_of(th, direction, window) -> Fill | None` is now the ONE rule both callers
consume. This file tests it directly (pure, offline) and proves classify.eval_price_leg
and counterfactual.score_counterfactual agree on filled/mode/entry_px given the same
thesis + bars.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import thesis as TH   # noqa: E402
import classify as CL  # noqa: E402
import counterfactual as CF  # noqa: E402


def _bar(ts, o, h, l, c):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


def _window(candles):
    return {"high": max(c["high"] for c in candles), "low": min(c["low"] for c in candles),
            "candles": candles, "window": "test"}


class FillOfGeometryGate(unittest.TestCase):
    def test_no_entry_zone_returns_none(self):
        th = {"direction": "SHORT", "stop": 1.0, "tp": [0.5]}
        self.assertIsNone(TH.fill_of(th, "SHORT", _window([_bar(1, 1, 1.1, 0.9, 1.0)])))

    def test_empty_window_returns_none(self):
        th = {"direction": "SHORT", "entry_zone": [0.9, 1.0]}
        self.assertIsNone(TH.fill_of(th, "SHORT", {}))
        self.assertIsNone(TH.fill_of(th, "SHORT", None))


class FillOfExplicitEntryMode(unittest.TestCase):
    """An explicit thesis.entry_mode latches as committed and takes the mode's natural
    edge at face value, regardless of the commit-price geometry."""

    def test_explicit_breakdown_overrides_geometry_source_committed(self):
        # commit_px (bar0 open) sits INSIDE the zone (would geometrically read "immediate"),
        # but entry_mode="breakdown" is explicit -> trust it, fill at the zone TOP.
        th = {"direction": "SHORT", "entry_zone": [4.15, 4.32], "entry_mode": "breakdown"}
        c = [_bar(1, 4.20, 4.25, 4.10, 4.18), _bar(2, 4.18, 4.20, 4.10, 4.15)]
        fill = TH.fill_of(th, "SHORT", _window(c))
        self.assertEqual(fill["mode"], "breakdown")
        self.assertEqual(fill["source"], "committed")
        self.assertAlmostEqual(fill["entry_px"], 4.32)

    def test_explicit_fade_overrides_geometry_source_committed(self):
        th = {"direction": "LONG", "entry_zone": [4.15, 4.32], "entry_mode": "fade"}
        c = [_bar(1, 4.20, 4.25, 4.10, 4.18)]
        fill = TH.fill_of(th, "LONG", _window(c))
        self.assertEqual(fill["mode"], "fade")
        self.assertEqual(fill["source"], "committed")
        self.assertAlmostEqual(fill["entry_px"], 4.15)

    def test_explicit_mode_aliases_recognized(self):
        th = {"direction": "SHORT", "entry_zone": [1.0, 2.0], "entry_mode": "dip"}
        fill = TH.fill_of(th, "SHORT", _window([_bar(1, 3, 3, 0.5, 1.5)]))
        self.assertEqual(fill["mode"], "breakdown")
        self.assertEqual(fill["source"], "committed")


class FillOfInferredConservativeEdge(unittest.TestCase):
    """No explicit entry_mode -> mode is inferred from the commit reference, and the
    ambiguous top/bottom pick charges the WORSE edge for the position's direction."""

    def test_inferred_breakdown_short_charges_worse_lower_edge(self):
        # commit (bar0 open 5.04) sits ABOVE the zone -> breakdown geometry. Natural edge
        # is zhi (4.32, flattering for a SHORT); inferred must charge the worse zlo (4.15).
        th = {"direction": "SHORT", "entry_zone": [4.15, 4.32]}
        c = [_bar(1, 5.04, 5.06, 5.02, 5.03), _bar(2, 4.90, 4.95, 4.20, 4.30)]
        fill = TH.fill_of(th, "SHORT", _window(c))
        self.assertEqual(fill["mode"], "breakdown")
        self.assertEqual(fill["source"], "inferred")
        self.assertAlmostEqual(fill["entry_px"], 4.15)

    def test_inferred_breakdown_long_charges_worse_upper_edge_matches_natural(self):
        # a LONG buying the dip: commit ABOVE zone -> breakdown. Worse edge for LONG is
        # zhi, which also happens to be breakdown's natural (first-touch) edge -> unchanged.
        th = {"direction": "LONG", "entry_zone": [4.15, 4.32]}
        c = [_bar(1, 5.04, 5.06, 5.02, 5.03), _bar(2, 4.90, 4.95, 4.20, 4.30)]
        fill = TH.fill_of(th, "LONG", _window(c))
        self.assertEqual(fill["mode"], "breakdown")
        self.assertAlmostEqual(fill["entry_px"], 4.32)

    def test_inferred_fade_short_charges_worse_lower_edge_matches_natural(self):
        # a SHORT fading a rally: commit BELOW zone -> fade. Worse edge for SHORT is zlo,
        # which also happens to be fade's natural (first-touch) edge -> unchanged.
        th = {"direction": "SHORT", "entry_zone": [5.20, 5.40]}
        c = [_bar(1, 5.04, 5.10, 5.00, 5.06), _bar(2, 5.10, 5.30, 5.05, 5.25)]
        fill = TH.fill_of(th, "SHORT", _window(c))
        self.assertEqual(fill["mode"], "fade")
        self.assertAlmostEqual(fill["entry_px"], 5.20)

    def test_inferred_fade_long_charges_worse_upper_edge_the_flattering_bug_fixed(self):
        # a LONG buying a breakout: commit BELOW zone -> fade. Natural edge is zlo (5.20,
        # the cheap/flattering buy for a LONG); the pre-147 bug always defaulted here.
        # Inferred must now charge the worse zhi (5.40).
        th = {"direction": "LONG", "entry_zone": [5.20, 5.40]}
        c = [_bar(1, 5.04, 5.10, 5.00, 5.06), _bar(2, 5.10, 5.30, 5.05, 5.25)]
        fill = TH.fill_of(th, "LONG", _window(c))
        self.assertEqual(fill["mode"], "fade")
        self.assertEqual(fill["source"], "inferred")
        self.assertAlmostEqual(fill["entry_px"], 5.40)

    def test_inferred_immediate_uses_exact_commit_price_no_edge_hedge(self):
        # commit price sits INSIDE the zone -> immediate, no ambiguity, no worse-edge hedge.
        th = {"direction": "SHORT", "entry_zone": [4.15, 4.32]}
        c = [_bar(1, 4.20, 4.25, 4.10, 4.18)]
        fill = TH.fill_of(th, "SHORT", _window(c))
        self.assertEqual(fill["mode"], "immediate")
        self.assertEqual(fill["source"], "inferred")
        self.assertAlmostEqual(fill["entry_px"], 4.20)
        self.assertTrue(fill["filled"])
        self.assertEqual(fill["entry_idx"], 0)

    def test_no_commit_reference_defaults_fade_worse_edge_per_direction(self):
        # no candle open/close at all (classify's real kline fetch shape) -> no commit
        # reference. A SHORT still charges zlo (worse); a LONG charges zhi (worse) --
        # NOT the old single always-zlo default, which flattered every inferred LONG.
        th_short = {"direction": "SHORT", "entry_zone": [4.15, 4.32]}
        th_long = {"direction": "LONG", "entry_zone": [4.15, 4.32]}
        c = [{"ts": 1, "high": 4.30, "low": 4.10}, {"ts": 2, "high": 4.25, "low": 4.05}]
        fs = TH.fill_of(th_short, "SHORT", _window(c))
        fl = TH.fill_of(th_long, "LONG", _window(c))
        self.assertEqual(fs["source"], "inferred")
        self.assertAlmostEqual(fs["entry_px"], 4.15)
        self.assertEqual(fl["source"], "inferred")
        self.assertAlmostEqual(fl["entry_px"], 4.32)


class FillOfFilledAndSequencing(unittest.TestCase):
    def test_never_intersects_zone_is_unfilled(self):
        th = {"direction": "SHORT", "entry_zone": [4.15, 4.32]}
        c = [_bar(1, 5.04, 5.06, 5.02, 5.03), _bar(2, 5.05, 5.08, 5.00, 5.02)]
        fill = TH.fill_of(th, "SHORT", _window(c))
        self.assertFalse(fill["filled"])
        self.assertIsNone(fill["entry_idx"])
        self.assertIsNotNone(fill["entry_px"])   # reference price still populated

    def test_entry_idx_is_first_intersecting_candle(self):
        th = {"direction": "SHORT", "entry_zone": [4.15, 4.32]}
        c = [_bar(1, 5.06, 5.00, 5.02, 5.00),     # no overlap
             _bar(2, 5.00, 5.00, 4.60, 4.65),     # no overlap (low 4.60 > 4.32)
             _bar(3, 4.55, 4.55, 4.30, 4.40),     # overlaps [4.15,4.32]: low 4.30<=4.32
             _bar(4, 4.30, 4.31, 4.10, 4.20)]     # also overlaps -- must not be picked
        fill = TH.fill_of(th, "SHORT", _window(c))
        self.assertTrue(fill["filled"])
        self.assertEqual(fill["entry_idx"], 2)

    def test_aggregate_window_no_candles_reports_filled_only(self):
        th = {"direction": "SHORT", "entry_zone": [4.15, 4.32]}
        fill = TH.fill_of(th, "SHORT", {"high": 5.07, "low": 4.18, "candles": None})
        self.assertTrue(fill["filled"])
        self.assertIsNone(fill["entry_idx"])   # unsequenceable without candle detail

    def test_aggregate_window_no_intersection_is_unfilled(self):
        th = {"direction": "SHORT", "entry_zone": [4.15, 4.32]}
        fill = TH.fill_of(th, "SHORT", {"high": 5.07, "low": 5.00, "candles": None})
        self.assertFalse(fill["filled"])


class ClassifyCounterfactualAgree(unittest.TestCase):
    """DoD: same thesis + same bars through classify.eval_price_leg and
    counterfactual.score_counterfactual -> identical filled/mode/entry_px."""

    def _fixtures(self):
        # breakdown / fade / immediate / no-commit-reference, one thesis + bar set each.
        # Bar highs are kept below `stop` throughout so no fixture ALSO trips a stop-breach
        # (that's classify's separate BREAKS/TRIGGERS sequencing, not the fill rule under
        # test here — and a same-bar entry+stop-breach exercises classify's SPEC-79 seq
        # formatting, which expects second-epoch `ts`, not something these fixtures need).
        base = {"direction": "SHORT", "status": "PENDING", "entry_zone": [4.15, 4.32],
               "stop": 4.55, "tp": [3.85, 3.43], "time_stop_h": 48}
        return {
            "breakdown": (
                dict(base),
                [_bar(1, 5.04, 5.06, 5.02, 5.03),      # commit, above zone, no overlap
                 _bar(2, 4.30, 4.33, 4.20, 4.25),      # falls INTO zone -> fill @ worse edge
                 _bar(3, 4.25, 4.30, 3.80, 3.85)],     # runs to 3.80 -> TP1
            ),
            "fade": (
                dict(base, entry_zone=[5.20, 5.40], stop=5.60, tp=[5.00, 4.70]),
                [_bar(1, 5.04, 5.10, 5.00, 5.06),      # commit, below zone
                 _bar(2, 5.10, 5.30, 5.05, 5.25),      # rallies INTO zone -> fill @ worse edge
                 _bar(3, 5.20, 5.22, 4.65, 4.70)],     # falls to 4.65 -> TP1
            ),
            "immediate": (
                dict(base),
                [_bar(1, 4.20, 4.25, 4.10, 4.18),      # commit price INSIDE the zone
                 _bar(2, 4.10, 4.15, 3.80, 3.85)],     # runs to 3.80 -> TP1
            ),
            "no_commit_reference": (
                dict(base),
                # classify's real kline shape: ts/high/low only, no open/close
                [{"ts": 1, "high": 4.30, "low": 4.10},
                 {"ts": 2, "high": 4.20, "low": 3.80}],
            ),
        }

    def test_all_fixture_modes_agree(self):
        for name, (th, bars) in self._fixtures().items():
            with self.subTest(fixture=name):
                direction = th["direction"]
                window = _window(bars)
                leg = CL.eval_price_leg(th, direction, window)
                cf_res = CF.score_counterfactual(th, bars)
                self.assertTrue(cf_res["scored"], name)
                cf = cf_res["counterfactual"]
                leg_fill = leg["fill"]
                self.assertEqual(leg_fill["filled"], cf["filled"], f"{name}: filled mismatch")
                self.assertEqual(leg_fill["mode"], cf["entry_mode"], f"{name}: mode mismatch")
                cf_entry = cf.get("entry_px", cf.get("entry_ref"))
                self.assertAlmostEqual(leg_fill["entry_px"], cf_entry, places=6,
                                       msg=f"{name}: entry_px mismatch")


if __name__ == "__main__":
    unittest.main()
