#!/usr/bin/env python3
"""SPEC-141 — one grammar for every pushed page: TICKER · PRICE · WHAT HAPPENED · DO THIS.

Pure-function tests for ops/page_grammar.py: the 6 page templates (title starts with
emoji+ticker+event-word, body carries live price + fired level + a thesis-derived
action phrase, never fabricated), human-number formatting (no scientific notation,
compact units, signed % deltas).
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, sub="ops"):
    spec = importlib.util.spec_from_file_location(name, ROOT / sub / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PG = _load("page_grammar")


class TestFmtPrice(unittest.TestCase):
    def test_none_is_dash(self):
        self.assertEqual(PG.fmt_price(None), "—")

    def test_large_price_no_scientific_notation(self):
        s = PG.fmt_price(1_903_240)
        self.assertNotIn("e+", s)
        self.assertNotIn("e-", s)

    def test_small_price_no_scientific_notation(self):
        s = PG.fmt_price(0.0000123)
        self.assertNotIn("e-", s)
        self.assertNotIn("e+", s)

    def test_typical_price(self):
        self.assertEqual(PG.fmt_price(0.0412), "0.0412")

    def test_whole_number(self):
        self.assertEqual(PG.fmt_price(100), "100")


class TestFmtCompact(unittest.TestCase):
    def test_millions(self):
        self.assertEqual(PG.fmt_compact(1_903_240), "1.90M")

    def test_thousands(self):
        self.assertEqual(PG.fmt_compact(1_500), "1.50K")

    def test_none_is_dash(self):
        self.assertEqual(PG.fmt_compact(None), "—")

    def test_negative(self):
        self.assertEqual(PG.fmt_compact(-2_000_000), "-2.00M")


class TestFmtBalanceChange(unittest.TestCase):
    def test_1_9e6_fixture_no_scientific_notation_has_M_delta_pct(self):
        s = PG.fmt_balance_change(1_903_240, 1_902_522)
        self.assertNotIn("e+", s)
        self.assertNotIn("e-", s)
        self.assertIn("1.90M", s)
        self.assertIn("-718", s)
        self.assertIn("%", s)

    def test_none_inputs_degrade_to_dash(self):
        self.assertEqual(PG.fmt_balance_change(None, 5), "—")


class TestPageTriggers(unittest.TestCase):
    def test_full_thesis(self):
        thesis = {"direction": "LONG", "entry_zone": [0.040, 0.043], "stop": 0.036, "tp": [0.052, 0.06]}
        title, body = PG.page_triggers("BLESS", 0.0412, thesis)
        self.assertTrue(title.startswith("🟢 BLESS TRIGGERED") or title.startswith("🟢BLESS"))
        self.assertIn("BLESS", title)
        self.assertIn("TRIGGERED", title)
        self.assertIn("0.0412", body)
        self.assertIn("0.04", body)          # entry zone lead
        self.assertIn("LONG", body)
        self.assertIn("stop 0.036", body)
        self.assertIn("tp1 0.052", body)
        self.assertLessEqual(len(body), 110)

    def test_null_thesis_reads_board_never_fabricated(self):
        title, body = PG.page_triggers("NEWNAME", 1.2345, None)
        self.assertTrue(body.endswith("→ read board"))
        self.assertIn("NEWNAME", title)


class TestPageBreaks(unittest.TestCase):
    def test_full_thesis(self):
        title, body = PG.page_breaks("BEAT", 0.0142, {"stop": 0.0155})
        self.assertIn("BEAT", title)
        self.assertIn("BREAKS", title)
        self.assertIn("0.0142", body)
        self.assertIn("0.0155", body)
        self.assertIn("do not chase", body)

    def test_null_thesis_reads_board(self):
        title, body = PG.page_breaks("GHOST", 1.0, None)
        self.assertTrue(body.endswith("→ read board"))


class TestPageStopBreach(unittest.TestCase):
    def test_full_thesis(self):
        title, body = PG.page_stop_breach("BLESS", 0.0358, {"stop": 0.036})
        self.assertIn("BLESS", title)
        self.assertIn("STOP", title)
        self.assertIn("0.0358", body)
        self.assertIn("0.036", body)
        self.assertIn("exit if filled", body)

    def test_null_thesis_reads_board(self):
        title, body = PG.page_stop_breach("GHOST", 1.0, None)
        self.assertTrue(body.endswith("→ read board"))

    def test_spec161_imperative_shape_short_with_entry(self):
        """SPEC-161 req 3: `STOP: GALA 0.00212 ≥ 0.00211 — close short (entry 0.00198)`."""
        thesis = {"direction": "SHORT", "stop": 0.00211, "entry": 0.00198}
        title, body = PG.page_stop_breach("GALA", 0.00212, thesis)
        self.assertTrue(body.startswith("STOP: GALA"))
        self.assertIn("0.00212", body)
        self.assertIn("≥", body)
        self.assertIn("0.00211", body)
        self.assertIn("close short", body)
        self.assertIn("entry", body)
        self.assertIn("0.00198", body)
        self.assertIn("STOP", title)

    def test_spec161_imperative_shape_long(self):
        thesis = {"direction": "LONG", "stop": 0.036}
        _title, body = PG.page_stop_breach("BLESS", 0.0358, thesis)
        self.assertTrue(body.startswith("STOP: BLESS"))
        self.assertIn("≤", body)
        self.assertIn("close long", body)


class TestPageWatchArmed(unittest.TestCase):
    def test_breach_renders_level_and_action(self):
        title, body = PG.page_watch_armed("SKR", 0.00625, {"price": 0.0063, "dir": "below"})
        self.assertIn("SKR", title)
        self.assertIn("ARMED", title)
        self.assertIn("0.00625", body)
        self.assertIn("0.0063", body)
        self.assertIn("convert to entry or dismiss", body)

    def test_spec161_note_never_leaks_even_at_300_chars(self):
        """DoD req 5: a watch_level with a 300-char note -> body contains NO fragment
        of the note, regardless of BODY_MAX truncation."""
        long_note = "desk marginalia " * 20   # > 300 chars, multi-clause prose
        self.assertGreater(len(long_note), 300)
        breach = {"price": 0.0063, "dir": "below", "note": long_note}
        _title, body = PG.page_watch_armed("SKR", 0.00625, breach)
        self.assertNotIn("marginalia", body)
        self.assertNotIn(long_note, body)
        self.assertLessEqual(len(body), 120)

    def test_spec161_page_label_appears_whole(self):
        breach = {"price": 0.0063, "dir": "below", "page_label": "needs HOLD+vol"}
        _title, body = PG.page_watch_armed("CASHCAT", 0.00625, breach)
        self.assertIn("needs HOLD+vol", body)

    def test_spec161_entry_cross_shape_with_thesis_stop(self):
        """SPEC-161 req 3: `ENTRY? CASHCAT 0.00177 < 0.178 — needs HOLD+vol; stop 0.1955`."""
        breach = {"price": 0.178, "dir": "below", "page_label": "needs HOLD+vol"}
        thesis = {"stop": 0.1955}
        title, body = PG.page_watch_armed("CASHCAT", 0.00177, breach, thesis)
        self.assertTrue(body.startswith("ENTRY? CASHCAT"))
        self.assertIn("0.00177", body)
        self.assertIn("<", body)
        self.assertIn("0.178", body)
        self.assertIn("needs HOLD+vol", body)
        self.assertIn("stop 0.1955", body)
        self.assertIn("ARMED", title)

    def test_spec161_tp_zone_shape_derives_tag_from_thesis_tp_list(self):
        """SPEC-161 req 3: `TP1: GALA 0.00165 tagged — <page_label>` — the TP1 tag is
        derived from the breached level's position in thesis.tp (structured field),
        never parsed out of the page_label text; the label (the only allowed prose)
        appears whole in the body."""
        breach = {"price": 0.00165, "dir": "below", "page_label": "TP1 zone: bank 60%, stop→BE"}
        thesis = {"tp": [0.00165, 0.00142]}
        title, body = PG.page_watch_armed("GALA", 0.00165, breach, thesis)
        self.assertEqual(body, "TP1: GALA 0.00165 tagged — TP1 zone: bank 60%, stop→BE")
        self.assertTrue(title.startswith("🟢 GALA TP1"))

    def test_spec161_tp_zone_second_leg_tags_tp2(self):
        breach = {"price": 0.00142, "dir": "below", "page_label": "TP2: bank rest"}
        thesis = {"tp": [0.00165, 0.00142]}
        title, body = PG.page_watch_armed("GALA", 0.00142, breach, thesis)
        self.assertTrue(body.startswith("TP2: GALA"))
        self.assertTrue(title.startswith("🟢 GALA TP2"))


class TestPageDrift(unittest.TestCase):
    def test_drift_renders_pct_and_anchor(self):
        drift = {"live_price": 0.53, "nearest_anchor": 0.60, "nearest_dist_pct": 12}
        title, body = PG.page_drift("TAG", drift)
        self.assertIn("TAG", title)
        self.assertIn("DRIFTED", title)
        self.assertIn("0.53", body)
        self.assertIn("12", body)
        self.assertIn("0.6", body)
        self.assertIn("re-anchor or retire", body)


class TestPageFundingArmed(unittest.TestCase):
    def test_breach_renders_rate_threshold_and_page_label(self):
        breach = {"threshold_4h": 0.02, "op": "lt", "page_label": "cool-arm the short"}
        title, body = PG.page_funding_armed("BLESS", 0.018, breach)
        self.assertIn("BLESS", title)
        self.assertIn("ARMED", title)
        self.assertIn("0.018", body)
        self.assertIn("lt", body)
        self.assertIn("0.020", body)
        self.assertIn("cool-arm the short", body)

    def test_no_page_label_falls_back_to_generic_action(self):
        breach = {"threshold_4h": 0.02, "op": "lt"}
        _title, body = PG.page_funding_armed("BLESS", 0.018, breach)
        self.assertIn("convert to entry or dismiss", body)

    def test_spec161_note_never_leaks_even_when_page_label_absent(self):
        """DoD req 1: `note` is NEVER interpolated, regardless of what it contains —
        only `page_label` (or the generic fallback) may appear."""
        breach = {"threshold_4h": 0.02, "op": "lt", "note": "desk marginalia " * 20}
        _title, body = PG.page_funding_armed("BLESS", 0.018, breach)
        self.assertNotIn("marginalia", body)
        self.assertIn("convert to entry or dismiss", body)


class TestFinalizeBody(unittest.TestCase):
    """SPEC-161 req 4: design-to-length — the hard backstop must never cut a number,
    dropping the trailing prose clause instead of a mid-token [:N] slice."""

    def test_short_body_passes_through(self):
        self.assertEqual(PG._finalize_body("STOP: X 1 >= 2 — close short"),
                         "STOP: X 1 >= 2 — close short")

    def test_over_hard_max_drops_trailing_clause_not_mid_number(self):
        numbers = "STOP: VERYLONGTICKERNAME 0.0000123456789 >= 0.0000198765432"
        body = numbers + " — " + ("close short and do a lot of other prose " * 3)
        self.assertGreater(len(body), 120)
        out = PG._finalize_body(body)
        self.assertLessEqual(len(out), 120)
        self.assertTrue(out.startswith(numbers))
        # never ends mid-number: the last char is not immediately preceded by a lone digit
        # inside a truncated float (crude check: no trailing digit run gets a dangling '.')
        self.assertFalse(out.rstrip().endswith("."))

    def test_never_exceeds_hard_max_even_with_no_clean_separator(self):
        body = "X" * 200
        out = PG._finalize_body(body)
        self.assertLessEqual(len(out), 120)


class TestPageTape(unittest.TestCase):
    def test_strips_prefix_and_terminates(self):
        title, body = PG.page_tape("BIRB", "BIRB tape: operator taking profit")
        self.assertIn("BIRB", title)
        self.assertIn("TAPE", title)
        self.assertNotIn("BIRB tape:", body)
        self.assertIn("check book", body)

    def test_never_carries_raw_detector_token(self):
        _title, body = PG.page_tape("BIRB", "BIRB tape: operator taking profit, shorts still opening")
        self.assertNotIn("operator_profit_take", body)
        self.assertNotIn("tail_of_liquidation", body)


class TestPageOicFlip(unittest.TestCase):
    def test_fuel_evaporating_render(self):
        title, body = PG.page_oic_flip("LAB", "DIRECTIONAL", "ARB_DOMINATED", "fuel_evaporating")
        self.assertIn("LAB", title)
        self.assertIn("OIC FLIP", title)
        self.assertIn("DIRECTIONAL", body)
        self.assertIn("ARB_DOMINATED", body)
        self.assertIn("fuel evaporating", body)

    def test_fuel_arriving_render(self):
        _title, body = PG.page_oic_flip("LAB", "ARB_DOMINATED", "DIRECTIONAL", "fuel_arriving")
        self.assertIn("fuel arriving", body)


class TestPageRoleDrift(unittest.TestCase):
    def test_exit_role_drift_render(self):
        title, body = PG.page_role_drift("LAB", "exit", "bitget", "binance")
        self.assertIn("LAB", title)
        self.assertIn("ROLE DRIFT", title)
        self.assertIn("bitget", body)
        self.assertIn("binance", body)

    def test_mark_engine_drift_render(self):
        _title, body = PG.page_role_drift("LAB", "mark_engine", "43.5", "60.0")
        self.assertIn("mark-constituent", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
