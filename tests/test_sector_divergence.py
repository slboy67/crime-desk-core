#!/usr/bin/env python3
"""SPEC-114 — sector-divergence pre-classifier: SOLO_PUMP vs SECTOR_MOVE.

Onchain-Analysis-Workshop-CrimeDesk.md Lesson 10's cheapest Cat A first-pass test:
one coin pumping vertically while its sector basket is flat = manipulation prior;
a sector-wide rise = liquidity rotation, not operator action.

Offline-deterministic throughout: classify_divergence/basket_stats are pure (no
network); evaluate_token/tag_for are exercised with an injected http_fetch stub.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import sector_divergence as SD


CFG = {
    "thresholds": dict(SD.DEFAULT_THRESHOLDS),
    "baskets": {
        "ai-depin": {"cg_category": "ai-agents", "label": "AI/DePIN"},
        "small": {"cg_category": "tiny-basket", "label": "Tiny"},
    },
    "token_baskets": {"LAB": ["ai-depin"], "TINYTOK": ["small"]},
}


class BasketStatsTests(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertIsNone(SD.basket_stats([]))
        self.assertIsNone(SD.basket_stats(None))

    def test_median_odd_and_breadth(self):
        # LAB-shaped: median -2, breadth 2/9 up
        rets = [-50, -45, -40, -35, -2, -2, -1, 3, 9]
        s = SD.basket_stats(rets)
        self.assertEqual(s["n"], 9)
        self.assertEqual(s["median"], -2)
        self.assertAlmostEqual(s["breadth"], 2 / 9)

    def test_median_even(self):
        s = SD.basket_stats([10, 20, 30, 40])
        self.assertEqual(s["median"], 25)


class ClassifyDivergenceTests(unittest.TestCase):
    def test_lab_shaped_solo_pump(self):
        stats = SD.basket_stats([-50, -45, -40, -35, -2, -2, -1, 3, 9])
        r = SD.classify_divergence(80, stats)
        self.assertEqual(r["verdict"], SD.SOLO_PUMP)
        self.assertEqual(r["basket_median_ret"], -2)
        self.assertEqual(r["divergence"], 82)
        self.assertAlmostEqual(r["breadth"], 2 / 9)
        self.assertIsNone(r["caveat"])

    def test_sector_rotation_move(self):
        stats = SD.basket_stats([5, 10, 15, 22, 30, 35, 40])
        r = SD.classify_divergence(30, stats)
        self.assertEqual(r["verdict"], SD.SECTOR_MOVE)
        self.assertEqual(r["basket_median_ret"], 22)

    def test_in_between_is_mixed(self):
        stats = SD.basket_stats([6, 9, 12, 15, 18])  # median 12
        r = SD.classify_divergence(90, stats)
        self.assertEqual(r["verdict"], SD.MIXED)

    def test_boundary_solo_inclusive(self):
        # token_ret == solo_min (40), |median| == solo_max (10) -> exactly qualifies SOLO
        stats = SD.basket_stats([10, 10, 10, 10, 10])
        r = SD.classify_divergence(40.0, stats)
        self.assertEqual(r["verdict"], SD.SOLO_PUMP)

    def test_boundary_just_below_solo_min_is_not_solo(self):
        stats = SD.basket_stats([10, 10, 10, 10, 10])
        r = SD.classify_divergence(39.999, stats)
        self.assertNotEqual(r["verdict"], SD.SOLO_PUMP)

    def test_boundary_just_above_solo_max_basket_voids_solo(self):
        stats = SD.basket_stats([10.001, 10.001, 10.001, 10.001, 10.001])
        r = SD.classify_divergence(50.0, stats)
        self.assertNotEqual(r["verdict"], SD.SOLO_PUMP)

    def test_boundary_sector_inclusive(self):
        # median == sector_min (15), token_ret == 2x median (30) -> exactly qualifies SECTOR_MOVE
        stats = SD.basket_stats([15, 15, 15, 15, 15])
        r = SD.classify_divergence(30.0, stats)
        self.assertEqual(r["verdict"], SD.SECTOR_MOVE)

    def test_boundary_just_above_inline_multiple_is_mixed(self):
        stats = SD.basket_stats([15, 15, 15, 15, 15])
        r = SD.classify_divergence(30.001, stats)
        self.assertEqual(r["verdict"], SD.MIXED)

    def test_small_basket_caveat_forces_mixed(self):
        stats = SD.basket_stats([-50, -45, -40])  # n=3, would otherwise be SOLO territory
        r = SD.classify_divergence(80, stats)
        self.assertEqual(r["verdict"], SD.MIXED)
        self.assertIn("small basket", r["caveat"])
        self.assertEqual(r["n"], 3)

    def test_unavailable_stats_is_unavailable_never_flat(self):
        r = SD.classify_divergence(80, None)
        self.assertEqual(r["verdict"], SD.UNAVAILABLE)
        self.assertIsNotNone(r["caveat"])
        self.assertIsNone(r["basket_median_ret"])


class FormatTagTests(unittest.TestCase):
    def test_solo_pump_tag(self):
        entry = {"verdict": SD.SOLO_PUMP, "token_ret": 62, "basket_median_ret": -3,
                 "label": "AI/DePIN", "window_days": 7, "caveat": None}
        tag = SD.format_tag(entry)
        self.assertIn("SOLO_PUMP", tag)
        self.assertIn("+62%", tag)
        self.assertIn("AI/DePIN", tag)
        self.assertIn("-3%", tag)
        self.assertIn("7d", tag)

    def test_sector_move_tag_has_beta_annotation(self):
        entry = {"verdict": SD.SECTOR_MOVE, "token_ret": 30, "basket_median_ret": 22,
                 "label": "AI/DePIN", "window_days": 7, "caveat": None}
        tag = SD.format_tag(entry)
        self.assertIn("sector beta", tag)

    def test_unmapped_has_no_tag(self):
        self.assertIsNone(SD.format_tag({"verdict": SD.UNMAPPED}))
        self.assertIsNone(SD.format_tag(None))

    def test_unavailable_tag_never_says_flat(self):
        entry = {"verdict": SD.UNAVAILABLE, "caveat": "basket fetch failed: boom",
                 "label": "AI/DePIN", "window_days": 7}
        tag = SD.format_tag(entry)
        self.assertIn("UNAVAILABLE", tag)
        self.assertNotIn("flat", tag.lower())


def _cg_rows(rets, field="price_change_percentage_7d_in_currency"):
    return [{field: r} for r in rets]


class EvaluateTokenTests(unittest.TestCase):
    def test_unmapped_token(self):
        r = SD.evaluate_token("NOPE", 50, config=CFG)
        self.assertEqual(r["verdict"], SD.UNMAPPED)
        self.assertEqual(r["baskets"], [])

    def test_mapped_token_solo_pump_via_fetch_seam(self):
        rows = _cg_rows([-50, -45, -40, -35, -2, -2, -1, 3, 9])
        r = SD.evaluate_token("LAB", 80, config=CFG, http_fetch=lambda url: rows)
        self.assertEqual(r["verdict"], SD.SOLO_PUMP)
        self.assertEqual(r["strongest"]["basket"], "ai-depin")
        self.assertEqual(len(r["baskets"]), 1)

    def test_fetch_failure_is_unavailable_not_flat(self):
        def boom(url):
            raise RuntimeError("CG 429")
        r = SD.evaluate_token("LAB", 80, config=CFG, http_fetch=boom)
        self.assertEqual(r["verdict"], SD.UNAVAILABLE)
        self.assertIn("fetch failed", r["strongest"]["caveat"])

    def test_small_basket_via_fetch_seam(self):
        rows = _cg_rows([-50, -45, -40])
        r = SD.evaluate_token("TINYTOK", 80, config=CFG, http_fetch=lambda url: rows)
        self.assertEqual(r["verdict"], SD.MIXED)
        self.assertIn("small basket", r["strongest"]["caveat"])


class TagForTests(unittest.TestCase):
    def test_unmapped_returns_none(self):
        self.assertIsNone(SD.tag_for("NOPE", 50, config=CFG))

    def test_mapped_solo_pump_bumps_cat_a(self):
        rows = _cg_rows([-50, -45, -40, -35, -2, -2, -1, 3, 9])
        tag = SD.tag_for("LAB", 80, config=CFG, http_fetch=lambda url: rows)
        self.assertIsNotNone(tag)
        self.assertEqual(tag["verdict"], SD.SOLO_PUMP)
        self.assertTrue(tag["cat_a_bump"])
        self.assertIn("SOLO_PUMP", tag["annotation"])

    def test_sector_move_does_not_bump_cat_a(self):
        rows = _cg_rows([5, 10, 15, 22, 30, 35, 40])
        tag = SD.tag_for("LAB", 30, config=CFG, http_fetch=lambda url: rows)
        self.assertFalse(tag["cat_a_bump"])
        self.assertIn("sector beta", tag["annotation"])

    def test_total_failure_degrades_to_unavailable_never_raises(self):
        def boom(url):
            raise RuntimeError("network down")
        tag = SD.tag_for("LAB", 80, config=CFG, http_fetch=boom)
        self.assertEqual(tag["verdict"], SD.UNAVAILABLE)
        self.assertFalse(tag["cat_a_bump"])


class LoadConfigTests(unittest.TestCase):
    def test_loads_shipped_config(self):
        cfg = SD.load_config()
        self.assertIn("baskets", cfg)
        self.assertIn("token_baskets", cfg)
        self.assertIn("LAB", cfg["token_baskets"])

    def test_missing_file_degrades_to_defaults(self):
        cfg = SD.load_config(path="/nonexistent/path/x.json")
        self.assertEqual(cfg["thresholds"], SD.DEFAULT_THRESHOLDS)
        self.assertEqual(cfg["baskets"], {})


if __name__ == "__main__":
    unittest.main()
