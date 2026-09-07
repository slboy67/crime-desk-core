#!/usr/bin/env python3
"""SPEC-193 — `triage`'s `render:"compact"` mode.

Triage rows are already scalar/short-list (no raw depth/kline bulk like brief/classify
carry) — the only cut is `leverage_state` (mostly UNKNOWN placeholders, not a decision
key) and None-valued keys. Pure transform, offline-deterministic.
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


TR = _load("triage")


def _fixture_row(ticker="OP"):
    return {
        "ticker": ticker, "category": "cat-a", "price": 0.0987, "chg24": -2.3,
        "range_pct": 41, "vol_m": 32.5, "funding_pi": -0.012, "funding_venue": "binance",
        "funding_range": [-0.015, -0.009], "funding_suspect": False,
        "funding_raw_pi": -0.012, "funding_interval_min": 240,
        "oi_chg_pct": -5, "oi_chg_pct_24h": -5, "ls_ratio": 1.4,
        "signals": ["OI-flush", "deep-neg-fund"], "direction": "long", "tier": "live",
        "memo": "faded-bounce watch, magnet sweep in progress",
        "oi_mc_ratio": None, "oi_mc_flag": None, "oi_mc_caveat": None,
        "perp_spot_ratio": 1.1, "battlefield": "perp_led",
        "leverage_state": {"leverage_4h": "UNKNOWN", "leverage_48h": "UNKNOWN"},
    }


class TestTriageCompactRow(unittest.TestCase):
    def setUp(self):
        self.full = _fixture_row()
        self.c = TR.compact_row(self.full)

    def test_leverage_state_dropped(self):
        self.assertNotIn("leverage_state", self.c)

    def test_none_values_omitted(self):
        for k in ("oi_mc_ratio", "oi_mc_flag", "oi_mc_caveat"):
            self.assertNotIn(k, self.c)

    def test_decision_fields_equal_to_full(self):
        for k in ("ticker", "signals", "direction", "tier", "memo", "funding_pi",
                  "battlefield", "perp_spot_ratio"):
            self.assertEqual(self.c[k], self.full[k], k)

    def test_false_values_kept(self):
        self.assertIn("funding_suspect", self.c)
        self.assertIs(self.c["funding_suspect"], False)


class TestTriageCompactBoard(unittest.TestCase):
    def test_board_and_meta_shape(self):
        rows = [_fixture_row(f"TOK{i}") for i in range(21)]
        meta = {"agents": {"coder_dispatch": "loaded", "nonce_surveil": "loaded",
                          "board_tick": "loaded"}}
        board = TR.compact_board(rows, meta)
        self.assertEqual(board["meta"], meta)
        self.assertEqual(len(board["board"]), 21)
        for r in board["board"]:
            self.assertNotIn("leverage_state", r)

    def test_compact_smaller_than_full(self):
        rows = [_fixture_row(f"TOK{i}") for i in range(21)]
        meta = {"agents": {}}
        full_size = len(json.dumps({"board": rows, "meta": meta}))
        compact_size = len(json.dumps(TR.compact_board(rows, meta)))
        self.assertLess(compact_size, full_size)


if __name__ == "__main__":
    unittest.main()
