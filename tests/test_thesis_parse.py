#!/usr/bin/env python3
"""SPEC-146 — one `thesis` module owns thesis geometry (parse + validate).

Before this, geometry was re-derived independently in four places — classify
(_normalize_watch_levels, _collect_anchors, thesis_direction/entry_zone/committed_epoch/
time_stop), counterfactual (_zone), tape_watch (inline wl.get() reads) — and diverged:
14/27 board rows silently lost their watch_level (SPEC-144), and re-deriving direction
from live price after that repair put 6/29 levels out BACKWARDS because entry_zone was
read unsorted in one place (classify's ZONE_BLOWN branch) and sorted in another
(counterfactual's old _zone, eval_price_leg, retire_flag_for).

`thesis.parse(tok) -> Thesis` is now the ONE typed parse entry point every reader
consumes; `thesis.check_board(tokens)` is the standalone board-level invariant sweep
that catches a hand-written thesis bypassing build_thesis (req 3).

Complements tests/test_thesis.py (SPEC 48/54/144's commit/close/retire lifecycle
coverage, untouched by this spec) with the new read-side geometry parser.

Tests read a FIXTURE watchlist only — never config/watchlist.json (grilling Q8).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
import thesis as TH


def _tok(thesis=None, state="", ticker="X"):
    return {"ticker": ticker, "state": state, "thesis": thesis or {}}


class SchemaTableTest(unittest.TestCase):
    """Every real shape either parses to a typed value or produces a named caveat —
    never a silent vanish. This replaces the three isolated normalizer tests
    (test_classify_watch_level.py's EvalWatchLevelsPure class)."""

    # ── watch_level shapes ──────────────────────────────────────────────────
    def test_watch_level_bare_float_is_dropped_with_caveat(self):
        p = TH.parse(_tok({"watch_level": [0.039, 0.062]}))
        self.assertEqual(p.watch_levels, [])
        self.assertEqual(len(p.caveats), 2)
        self.assertIn("0.039", p.caveats[0])

    def test_watch_level_single_dict_parses(self):
        p = TH.parse(_tok({"watch_level": {"price": 9.2, "dir": "below"}}))
        self.assertEqual(p.watch_levels,
                         [{"price": 9.2, "dir": "below", "note": "", "page_label": None}])
        self.assertEqual(p.caveats, [])

    def test_watch_level_list_of_dicts_parses(self):
        p = TH.parse(_tok({"watch_level": [{"price": 9.2, "dir": "below"},
                                           {"price": 12.0, "dir": "above"}]}))
        self.assertEqual(len(p.watch_levels), 2)
        self.assertEqual(p.caveats, [])

    def test_watch_level_null_is_empty_no_caveat(self):
        p = TH.parse(_tok({"watch_level": None}))
        self.assertEqual(p.watch_levels, [])
        self.assertEqual(p.caveats, [])

    def test_watch_level_absent_is_empty_no_caveat(self):
        p = TH.parse(_tok({}))
        self.assertEqual(p.watch_levels, [])
        self.assertEqual(p.caveats, [])

    def test_watch_level_numeric_string_price_coerces(self):
        p = TH.parse(_tok({"watch_level": [{"price": "9.2", "dir": "below"}]}))
        # a numeric string is coerced (float("9.2") succeeds) — matches pre-146 behavior
        self.assertEqual(p.watch_levels,
                         [{"price": 9.2, "dir": "below", "note": "", "page_label": None}])

    def test_watch_level_non_numeric_price_is_dropped_with_caveat(self):
        p = TH.parse(_tok({"watch_level": [{"price": "nine", "dir": "below"}]}))
        self.assertEqual(p.watch_levels, [])
        self.assertEqual(len(p.caveats), 1)

    def test_watch_level_bad_dir_is_dropped_with_caveat(self):
        p = TH.parse(_tok({"watch_level": [{"price": 5.0, "dir": "sideways"}]}))
        self.assertEqual(p.watch_levels, [])
        self.assertIn("5.0", p.caveats[0])

    def test_watch_level_garbage_element_is_dropped_with_caveat(self):
        p = TH.parse(_tok({"watch_level": [{"price": 9.2, "dir": "below"}, "garbage"]}))
        self.assertEqual(len(p.watch_levels), 1)
        self.assertEqual(len(p.caveats), 1)
        self.assertIn("garbage", p.caveats[0])

    def test_watch_level_mixed_list_keeps_good_drops_bad(self):
        p = TH.parse(_tok({"watch_level": [{"price": 9.2, "dir": "below", "note": "x"},
                                           0.039]}))
        self.assertEqual(len(p.watch_levels), 1)
        self.assertEqual(p.watch_levels[0]["price"], 9.2)
        self.assertEqual(len(p.caveats), 1)
        self.assertIn("0.039", p.caveats[0])
        self.assertIn("price", p.caveats[0])
        self.assertIn("dir", p.caveats[0])

    # ── SPEC-161 req 2: page_label — validated/truncated at COMMIT-READ time ────
    def test_page_label_absent_is_none_no_caveat(self):
        p = TH.parse(_tok({"watch_level": [{"price": 9.2, "dir": "below"}]}))
        self.assertIsNone(p.watch_levels[0]["page_label"])
        self.assertEqual(p.caveats, [])

    def test_page_label_within_limit_passes_through_whole(self):
        p = TH.parse(_tok({"watch_level": [{"price": 9.2, "dir": "below",
                                            "page_label": "TP1 zone: bank 60%"}]}))
        self.assertEqual(p.watch_levels[0]["page_label"], "TP1 zone: bank 60%")
        self.assertEqual(p.caveats, [])

    def test_page_label_over_40_chars_truncated_with_caveat_not_dropped(self):
        long_label = "x" * 60
        p = TH.parse(_tok({"watch_level": [{"price": 9.2, "dir": "below",
                                            "page_label": long_label}]}))
        self.assertEqual(len(p.watch_levels[0]["page_label"]), 40)
        self.assertEqual(p.watch_levels[0]["page_label"], "x" * 40)
        # the element itself is NOT dropped — only the label is clipped
        self.assertEqual(len(p.watch_levels), 1)
        self.assertEqual(len(p.caveats), 1)
        self.assertIn("page_label", p.caveats[0])
        self.assertIn("truncated", p.caveats[0])

    def test_page_label_non_string_dropped_with_caveat(self):
        p = TH.parse(_tok({"watch_level": [{"price": 9.2, "dir": "below", "page_label": 123}]}))
        self.assertIsNone(p.watch_levels[0]["page_label"])
        self.assertEqual(len(p.caveats), 1)

    # ── entry_zone shapes ────────────────────────────────────────────────────
    def test_entry_zone_two_numbers_sorted_regardless_of_commit_order(self):
        p = TH.parse(_tok({"entry_zone": [6.6, 6.0]}))
        self.assertEqual(p.zone, (6.0, 6.6))
        self.assertEqual(p.caveats, [])

    def test_entry_zone_three_elements_is_dropped_with_named_caveat(self):
        p = TH.parse(_tok({"entry_zone": [6.0, 6.3, 6.6]}, state=""))
        self.assertIsNone(p.zone)
        self.assertEqual(len(p.caveats), 1)
        self.assertIn("entry_zone", p.caveats[0])

    def test_entry_zone_null_is_none_no_caveat(self):
        p = TH.parse(_tok({"entry_zone": None}))
        self.assertIsNone(p.zone)
        self.assertEqual(p.caveats, [])

    def test_entry_zone_falls_back_to_memo_when_absent(self):
        p = TH.parse(_tok({}, state="watch $0.115-0.118 for the retest"))
        self.assertEqual(p.zone, (0.115, 0.118))

    # ── direction ─────────────────────────────────────────────────────────────
    def test_direction_sideways_style_junk_is_uppercased_verbatim(self):
        # direction isn't in DIRECTIONS-validated set at read time (validate_thesis
        # gates that at write time) — parse() just reports what's there.
        p = TH.parse(_tok({"direction": "sideways"}))
        self.assertEqual(p.direction, "SIDEWAYS")

    def test_direction_falls_back_to_memo_when_absent(self):
        p = TH.parse(_tok({}, state="short distribution top"))
        self.assertEqual(p.direction, "SHORT")

    # ── stop / tp ────────────────────────────────────────────────────────────
    def test_stop_non_number_is_dropped_with_caveat(self):
        p = TH.parse(_tok({"stop": "eight"}))
        self.assertIsNone(p.stop)
        self.assertEqual(len(p.caveats), 1)

    def test_tp_mixed_list_keeps_good_drops_bad(self):
        p = TH.parse(_tok({"tp": [5.85, "n/a", 4.9]}))
        self.assertEqual(p.tps, [5.85, 4.9])
        self.assertEqual(len(p.caveats), 1)

    def test_legacy_tps_key_still_read(self):
        p = TH.parse(_tok({"tps": [5.85, 4.9]}))
        self.assertEqual(p.tps, [5.85, 4.9])

    # ── time_stop_h / committed_ts ───────────────────────────────────────────
    def test_time_stop_h_zero_reads_as_none(self):
        p = TH.parse(_tok({"time_stop_h": 0, "committed_ts": "2026-06-10"}))
        self.assertIsNone(p.time_stop_h)

    def test_committed_ts_unparseable_drops_with_caveat(self):
        p = TH.parse(_tok({"committed_ts": "not-a-date"}))
        self.assertIsNone(p.committed_epoch)
        self.assertEqual(len(p.caveats), 1)

    def test_committed_ts_iso_and_bare_date_both_parse(self):
        p1 = TH.parse(_tok({"committed_ts": "2026-06-10T23:24:19Z"}))
        p2 = TH.parse(_tok({"committed_ts": "2026-06-10"}))
        self.assertIsNotNone(p1.committed_epoch)
        self.assertIsNotNone(p2.committed_epoch)

    # ── null thesis ──────────────────────────────────────────────────────────
    def test_null_thesis_parses_to_all_empty_defaults(self):
        p = TH.parse(_tok(None))
        self.assertEqual(p.direction, "?")   # memo_direction("") default
        self.assertEqual(p.watch_levels, [])
        self.assertEqual(p.tps, [])
        self.assertEqual(p.anchors, [])
        self.assertEqual(p.caveats, [])


class AnchorsAreCommittedOnly(unittest.TestCase):
    """anchors must reflect ONLY what was actually committed — never the memo-inferred
    zone fallback (that exists for direction/display, not as a thesis_drift anchor)."""

    def test_anchors_do_not_include_memo_inferred_zone(self):
        p = TH.parse(_tok({"tp": [15.5, 14.0, 12.5],
                          "watch_level": {"price": 16.69, "dir": "below"}},
                         state="watch $10.0-11.0 too (memo noise)"))
        self.assertNotIn(10.0, p.anchors)
        self.assertNotIn(11.0, p.anchors)
        self.assertEqual(sorted(p.anchors), [12.5, 14.0, 15.5, 16.69])

    def test_anchors_include_watch_level_zone_stop_tp(self):
        p = TH.parse(_tok({"entry_zone": [6.6, 6.0], "stop": 8.21, "tp": [5.85, 4.9],
                          "watch_level": {"price": 9.2, "dir": "below"}}))
        self.assertEqual(sorted(p.anchors), [4.9, 5.85, 6.0, 6.6, 8.21, 9.2])


class BoardInvariantTest(unittest.TestCase):
    """check_board(tokens) req 3 — every row with a committed watch_level must
    normalize to a NON-EMPTY level list; a bare-float row is reported, naming the
    ticker (BEAT/SPEC-77: 14/27 rows silently unarmed, 2026-08-19)."""

    def test_fixture_watchlist_every_watch_level_row_parses_nonempty(self):
        tokens = [
            {"ticker": "GOOD", "thesis": {"watch_level": [{"price": 1.0, "dir": "below"}]}},
            {"ticker": "CLEAN_GEOMETRY", "thesis": {"entry_zone": [1.0, 1.2], "stop": 0.9,
                                                    "tp": [1.5, 1.8]}},
            {"ticker": "NONE", "thesis": {}},
        ]
        violations = TH.check_board(tokens)
        self.assertEqual(violations, [])
        for tok in tokens:
            wl = (tok.get("thesis") or {}).get("watch_level")
            if wl:
                self.assertTrue(TH.parse(tok).watch_levels, tok["ticker"])

    def test_bare_float_row_is_a_violation_naming_the_ticker(self):
        tokens = [
            {"ticker": "GOOD", "thesis": {"watch_level": [{"price": 1.0, "dir": "below"}]}},
            {"ticker": "BAD", "thesis": {"watch_level": [0.039, 0.062]}},
            {"ticker": "NONE", "thesis": {}},
        ]
        violations = TH.check_board(tokens)
        self.assertEqual([v["ticker"] for v in violations], ["BAD"])
        self.assertEqual(len(violations[0]["caveats"]), 2)

    def test_malformed_entry_zone_is_also_a_violation_not_just_watch_level(self):
        """req 3 generalizes SPEC-144's watch_level-only check to every droppable field."""
        tokens = [{"ticker": "ZONEBAD", "thesis": {"entry_zone": [1.0, 2.0, 3.0]}}]
        violations = TH.check_board(tokens)
        self.assertEqual([v["ticker"] for v in violations], ["ZONEBAD"])

    def test_no_thesis_is_never_a_violation(self):
        tokens = [{"ticker": "BARE"}]
        self.assertEqual(TH.check_board(tokens), [])

    def test_check_board_loads_from_wl_path_when_tokens_omitted(self):
        import json
        import tempfile
        d = Path(tempfile.mkdtemp())
        wl_path = d / "watchlist.json"
        wl_path.write_text(json.dumps({"tokens": [
            {"ticker": "BAD", "thesis": {"watch_level": [0.039]}}]}))
        violations = TH.check_board(wl_path=wl_path)
        self.assertEqual([v["ticker"] for v in violations], ["BAD"])


class AntiDivergenceTest(unittest.TestCase):
    """req 1: classify, counterfactual, tape_watch, brief must produce IDENTICAL geometry
    for the same fixture row. This is the actual live bug SPEC-146 fixes: entry_zone was
    read UNSORTED in classify's ZONE_BLOWN branch (`lo, hi = entry_zone`) and SORTED
    everywhere else (eval_price_leg, retire_flag_for, counterfactual's old _zone) — a
    thesis committed as [hi, lo] read backwards in exactly one of the four places. Proven
    two ways: (a) all four modules import the SAME thesis module object — no fork to
    diverge in the first place — and (b) each module's real geometry-consuming call
    produces the parser's own sorted (lo, hi) for a fixture committed in descending order."""

    FIXTURE_TOK = {
        "ticker": "FIX", "state": "",
        "thesis": {"direction": "SHORT", "entry_zone": [6.6, 6.0], "stop": 8.21,
                  "tp": [5.85, 4.9], "time_stop_h": 48,
                  "committed_ts": "2026-06-10T23:24:19Z",
                  "watch_level": [{"price": 9.2, "dir": "below", "note": "x"}]},
    }

    def test_all_four_modules_import_the_same_thesis_module(self):
        import classify as CL
        import counterfactual as CF
        import tape_watch as TW
        import brief as BR
        self.assertIs(CL.TH, TH)
        self.assertIs(CF.thesis, TH)
        self.assertIs(TW.TH, TH)
        self.assertIs(BR.TH, TH)

    def test_entry_zone_committed_descending_reads_sorted_everywhere(self):
        p = TH.parse(self.FIXTURE_TOK)
        self.assertEqual(p.zone, (6.0, 6.6))   # the canonical read

        import classify as CL
        self.assertEqual(CL.TH.parse(self.FIXTURE_TOK).zone, (6.0, 6.6))

        import counterfactual as CF
        th = self.FIXTURE_TOK["thesis"]
        self.assertEqual(CF.thesis.parse({"thesis": th}).zone, (6.0, 6.6))
        # real call proof: the geometry gate passes (zone/stop/tp all parsed), so an
        # empty bars list fails on no_klines, never no_geometry
        res = CF.score_counterfactual(th, bars=[])
        self.assertEqual(res["skip"], "no_klines")

        import brief as BR
        display = BR._thesis_display(self.FIXTURE_TOK)
        self.assertEqual(tuple(display["entry_zone"]), (6.0, 6.6))
        self.assertEqual(display["direction"], p.direction)
        self.assertEqual(display["stop"], p.stop)
        self.assertEqual(display["tp"], p.tps)
        self.assertEqual(display["time_stop"], p.time_stop_h)
        self.assertEqual(display["watch_level"], p.watch_levels)

    def test_tape_watch_watch_levels_match_the_canonical_parse(self):
        import tape_watch as TW
        watch_th = {"direction": "WATCH",
                   "watch_level": [{"price": 9.2, "dir": "below", "note": "x"}]}
        p = TH.parse({"thesis": watch_th})
        # real call proof: TW.check_tape_deterioration reads watch_levels via the SAME
        # parser — a price that has crossed 9.2-below suppresses the drift reason
        # (test_watch_level_breach_suppresses_the_drift_reason's contract in test_tape_watch.py)
        out = TW.check_tape_deterioration(
            "FIX", watch_th, {"price": 10.0}, price=9.0, funding_pi=None,
            tape_result={"available": True, "patterns": []})
        self.assertIsNone(out)   # -10% drift but breached (9.0<=9.2) suppresses the drift reason
        self.assertEqual(TW.TH.parse({"thesis": watch_th}).watch_levels, p.watch_levels)


class OperatorVetoCaveatTest(unittest.TestCase):
    """SPEC-149 req 3: a thesis committed while classify's live operator_not_done veto
    was true carries a free-text caveat — thesis.py recognizes and validates the field."""

    def test_caveat_string_passes_through(self):
        p = TH.parse(_tok({"direction": "SHORT",
                          "operator_veto_caveat": "committed despite live veto — user override"}))
        self.assertEqual(p.operator_veto_caveat, "committed despite live veto — user override")
        self.assertEqual(p.caveats, [])

    def test_absent_caveat_is_none(self):
        p = TH.parse(_tok({"direction": "SHORT"}))
        self.assertIsNone(p.operator_veto_caveat)

    def test_malformed_caveat_dropped_with_caveat(self):
        p = TH.parse(_tok({"direction": "SHORT", "operator_veto_caveat": 123}))
        self.assertIsNone(p.operator_veto_caveat)
        self.assertTrue(any("operator_veto_caveat" in c for c in p.caveats))

    def test_validate_thesis_rejects_non_string_caveat(self):
        th = {"direction": "SHORT", "invalidation": {"stop": "x"},
             "operator_veto_caveat": 123}
        errors, _warnings = TH.validate_thesis(th)
        self.assertTrue(any("operator_veto_caveat" in e for e in errors))

    def test_validate_thesis_accepts_string_caveat(self):
        th = {"direction": "SHORT", "invalidation": {"stop": "x"},
             "operator_veto_caveat": "override, documented"}
        errors, _warnings = TH.validate_thesis(th)
        self.assertFalse(any("operator_veto_caveat" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
