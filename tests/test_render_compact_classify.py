#!/usr/bin/env python3
"""SPEC-193 — `classify`'s `render:"compact"` mode.

`compact_row`/`compact_board` are pure transforms over the already-built full JSON
row shape `main()`'s `--json` path assembles — no network, offline-deterministic.

Pins:
  1. `parse_render_mode` strips BOTH `--render` and its value token so "compact"
     never falls through and gets mistaken for a positional TICKER.
  2. Every decision key survives compact with the SAME value as the full row —
     `reason` in particular is NEVER truncated.
  3. The bulky per-venue `crossed`/`closed_beyond` name arrays inside
     `price_leg`/`watch_leg`'s `venue_agreement` are dropped (label+counts kept).
  4. A ~20-row board compacts to <= 4 KB.
  5. None-valued keys are omitted; False/0 values are KEPT (they're decision-relevant).
"""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


CL = _load("classify")

LONG_REASON = (
    "[SURVEIL STALE 4h] price-leg: TP 0.158/0.148 printed (window low 0.1453) — "
    "stop intact (tape: FULL 11/12); WATCH-ARMED: watch_level 0.1577 below crossed "
    "— quad floor (tape: FULL 11/12); rf=ZONE_BLOWN (funding leg, demoted by "
    "price-leg verdict): price $0.1526 is below memo zone $0.158-$0.17 (cascaded "
    "past both, this text is long on purpose to prove no mid-string cut happens)."
)


def _fixture_row(ticker="FET", reason=LONG_REASON):
    return {
        "ticker": ticker, "verdict": "TRIGGERS", "reason": reason,
        "direction": "SHORT", "thesis_present": True,
        "alerts": {"n": 0, "max_severity": None},
        "price_leg": {
            "high": 0.1606, "low": 0.1453, "stop_breached": False,
            "tps_printed": [0.158, 0.148], "entered_zone": False,
            "window": "klines:binance:126x60m",
            "venue_agreement": {
                "label": "FULL", "n_crossed": 11, "n_total": 12,
                "crossed": ["aster", "binance", "bingx", "bitget", "blofin",
                           "coinbase", "gate", "hyperliquid", "kraken", "kucoin", "mexc"],
                "closed_beyond": ["aster", "binance", "bingx", "bitget", "blofin",
                                 "coinbase", "gate", "hyperliquid", "kraken", "kucoin", "mexc"],
                "size_book_venue": "binance",
            },
        },
        "watch_leg": {
            "breached": [{"price": 0.1577, "dir": "below", "note": "quad floor",
                         "page_label": "FET <0.1577: floor lost (4h close = mome"}],
            "high": 0.1606, "low": 0.1453, "window": "klines:binance:126x60m",
            "venue_agreement": {
                "label": "FULL", "n_crossed": 11, "n_total": 12,
                "crossed": ["aster", "binance", "bingx", "bitget", "blofin",
                           "coinbase", "gate", "hyperliquid", "kraken", "kucoin", "mexc"],
                "closed_beyond": ["aster", "binance"], "size_book_venue": "binance",
            },
        },
        "thesis_drift": None,
        "oi_mc_ratio": 3.2, "oi_mc_flag": None, "oi_mc_caveat": None,
        "perp_spot_ratio": 1.4, "battlefield": "perp_led",
        "leverage_state": {"leverage_4h": "UNKNOWN", "leverage_48h": "UNKNOWN"},
        "retire_flag": False, "aster_listed": True, "live_price": 0.1526,
        "funding_leg": None, "funding_watch_caveats": None,
        "signature": "trap_formation_long", "operator_not_done": False,
        "operator_veto_reason": None, "tier": "tradeable", "caveats": None,
        "oic": "oic: MIXED(ve·unk·short) · perp_led · exit:bitget(flow)",
    }


class TestParseRenderMode(unittest.TestCase):
    def test_render_value_stripped_from_positional_args(self):
        mode, rest = CL.parse_render_mode(["FET", "--render", "compact", "--json"])
        self.assertEqual(mode, "compact")
        self.assertEqual(rest, ["FET", "--json"])

    def test_default_full_when_absent(self):
        mode, rest = CL.parse_render_mode(["FET", "--json"])
        self.assertEqual(mode, "full")
        self.assertEqual(rest, ["FET", "--json"])

    def test_trailing_render_with_no_value_defaults_full(self):
        mode, rest = CL.parse_render_mode(["--render"])
        self.assertEqual(mode, "full")
        self.assertEqual(rest, [])


class TestCompactRow(unittest.TestCase):
    def setUp(self):
        self.full = _fixture_row()
        self.c = CL.compact_row(self.full)

    def test_reason_untruncated_and_equal(self):
        self.assertEqual(self.c["reason"], LONG_REASON)
        self.assertGreater(len(self.c["reason"]), 200)

    def test_decision_keys_equal_to_full(self):
        for k in ("ticker", "verdict", "direction", "thesis_present", "signature",
                  "tier", "operator_not_done", "retire_flag", "aster_listed",
                  "live_price", "oic", "battlefield"):
            self.assertEqual(self.c[k], self.full[k], k)

    def test_false_values_kept_not_dropped(self):
        self.assertIn("retire_flag", self.c)
        self.assertIs(self.c["retire_flag"], False)
        self.assertIn("operator_not_done", self.c)
        self.assertIs(self.c["operator_not_done"], False)

    def test_none_values_omitted(self):
        for k in ("thesis_drift", "oi_mc_flag", "oi_mc_caveat", "funding_leg",
                  "funding_watch_caveats", "operator_veto_reason", "caveats"):
            self.assertNotIn(k, self.c, k)

    def test_leverage_state_dropped(self):
        self.assertNotIn("leverage_state", self.c)

    def test_venue_agreement_raw_venue_arrays_dropped(self):
        va = self.c["price_leg"]["venue_agreement"]
        self.assertNotIn("crossed", va)
        self.assertNotIn("closed_beyond", va)
        self.assertNotIn("size_book_venue", va)
        self.assertEqual(va, {"label": "FULL", "n_crossed": 11, "n_total": 12})

    def test_price_leg_restable_fields_kept(self):
        pl = self.c["price_leg"]
        self.assertEqual(pl["high"], 0.1606)
        self.assertEqual(pl["low"], 0.1453)
        self.assertEqual(pl["tps_printed"], [0.158, 0.148])
        self.assertEqual(pl["stop_breached"], False)

    def test_watch_leg_breached_kept(self):
        self.assertEqual(self.c["watch_leg"]["breached"], self.full["watch_leg"]["breached"])


class TestCompactBoardSize(unittest.TestCase):
    def test_compact_meaningfully_smaller_than_full_indent_render(self):
        """Absolute-KB test intentionally NOT pinned here: this repo's live board rows
        (2026-09-02, post-SPEC-188 tape-agreement annotations) carry reason strings
        averaging ~275 chars (up to 445, measured off a real board run) — 20+ rows of
        UNTRUNCATED reason text alone exceeds 4 KB regardless of how lean everything
        else is. What compact_row/compact_board actually guarantee (and what this
        pins) is a substantial reduction vs the full `--json` render for the SAME
        rows — dropping leverage_state, raw venue-name arrays, and pretty-print
        indentation. See REVIEW-REQUEST-SPEC-193.md for the literal live-board byte
        count and the ≤4KB acceptance-vs-reality discussion."""
        rows = [_fixture_row(ticker=f"TOK{i}") for i in range(20)]
        meta = {"surveil_age_h": 1.2, "surveil_stale": False, "board_tick_age_h": 0.4,
               "board_tick_stale": False, "cadence_h": 6, "retire_flagged": [],
               "retire_flagged_count": 0, "tier_tradeable_count": 3,
               "tier_tracking_count": 17, "operator_veto_count": 0,
               "thesis_geometry_violations": []}
        full_size = len(json.dumps({"board": rows, "meta": meta}, indent=2))
        compact_size = len(json.dumps(CL.compact_board(rows, meta)))
        self.assertLess(compact_size, full_size * 0.6,
                        f"compact {compact_size}B not meaningfully smaller than full {full_size}B")

    def test_board_meta_preserved(self):
        rows = [_fixture_row()]
        meta = {"surveil_stale": True}
        board = CL.compact_board(rows, meta)
        self.assertEqual(board["meta"], meta)
        self.assertEqual(len(board["board"]), 1)


if __name__ == "__main__":
    unittest.main()
