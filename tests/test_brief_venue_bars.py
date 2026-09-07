#!/usr/bin/env python3
"""SPEC-188 §2 — brief's venue_bars layer: the all-venue tape block + tape-agreement
lines. Offline-deterministic: build_venue_bars is monkeypatched (mirrors
test_brief.py's _patch_all pattern for every other network layer)."""
import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
# Reuse test_brief's ALREADY-LOADED brief module instance (BR) and its _patch_all —
# `importlib.util.spec_from_file_location` creates a fresh module object per load, so
# loading a second copy here would leave IT unpatched (classify_token/build_analyse/etc
# would hit real network/watchlist state and hang the suite).
from test_brief import BR, _patch_all


TS0, TS1 = 1788343200, 1788346800


def _venue_bars_fixture(binance_high=0.42, aster_high=0.40, kraken_available=False):
    venues = {
        "binance": {"venue": "binance", "available": True,
                   "bars": [{"ts": TS0, "o": 0.39, "h": binance_high, "l": 0.38, "c": 0.41, "live": False},
                           {"ts": TS1, "o": 0.41, "h": binance_high, "l": 0.40, "c": 0.415, "live": True}],
                   "turnover_24h_usd": 58800000.0},
        "aster": {"venue": "aster", "available": True,
                 "bars": [{"ts": TS0, "o": 0.39, "h": aster_high, "l": 0.38, "c": 0.40, "live": False},
                         {"ts": TS1, "o": 0.40, "h": aster_high, "l": 0.39, "c": 0.405, "live": True}],
                 "turnover_24h_usd": 1000000.0},
    }
    if kraken_available:
        venues["kraken"] = {"venue": "kraken", "available": True,
                            "bars": [{"ts": TS0, "o": 0.39, "h": 0.395, "l": 0.38, "c": 0.39, "live": False},
                                    {"ts": TS1, "o": 0.39, "h": 0.395, "l": 0.385, "c": 0.39, "live": True}]}
    else:
        venues["kraken"] = {"venue": "kraken", "available": False, "reason": "no candles returned"}
    return {
        "ticker": "SKYAI", "interval": "1h", "n": 2, "venues": venues,
        "n_total": len(venues), "n_available": sum(1 for v in venues.values() if v["available"]),
        "bars": [
            {"ts": TS0, "live": False, "n_venues": 2, "median_h": (binance_high + aster_high) / 2,
             "median_l": 0.38, "median_c": 0.405,
             "high_spread_pct": abs(binance_high - aster_high) / ((binance_high + aster_high) / 2) * 100,
             "low_spread_pct": 0.0, "close_spread_pct": 1.2,
             "max_h_venue": "binance" if binance_high >= aster_high else "aster",
             "min_h_venue": "aster" if binance_high >= aster_high else "binance",
             "max_l_venue": "binance", "min_l_venue": "aster"},
            {"ts": TS1, "live": True, "n_venues": 2, "median_h": (binance_high + aster_high) / 2,
             "median_l": 0.395, "median_c": 0.41,
             "high_spread_pct": abs(binance_high - aster_high) / ((binance_high + aster_high) / 2) * 100,
             "low_spread_pct": 0.5, "close_spread_pct": 1.0,
             "max_h_venue": "binance" if binance_high >= aster_high else "aster",
             "min_h_venue": "aster" if binance_high >= aster_high else "binance",
             "max_l_venue": "binance", "min_l_venue": "aster"},
        ],
        "dominant_tape": {"venue": "binance", "turnover_24h_usd": 58800000.0},
        "execution_venue": "aster",
    }


class TestVenueBarsLayerAvailable(unittest.TestCase):
    def setUp(self):
        self._restore = _patch_all()
        # SKYAI's thesis (from test_brief._fake_watchlist_with_thesis): SHORT stop 0.225 —
        # too far from this fixture's range to cross, so use a level that DOES: the fixture
        # highs (0.40-0.42) straddle a 0.41 stop for the crossing test below.

    def tearDown(self):
        self._restore()

    def test_venue_bars_available_true_and_shape(self):
        BR.build_venue_bars = lambda ticker, interval="1h", n=6: _venue_bars_fixture()
        b = BR.build_brief("SKYAI")
        vb = b["venue_bars"]
        self.assertTrue(vb["available"])
        self.assertEqual(vb["n_available"], 2)
        self.assertIn("tape_agreement", vb)

    def test_render_human_prints_tape_block_when_available(self):
        BR.build_venue_bars = lambda ticker, interval="1h", n=6: _venue_bars_fixture()
        b = BR.build_brief("SKYAI")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            BR.render_human(b)
        out = buf.getvalue()
        self.assertIn("tape (1h,", out)
        self.assertIn("dominant tape binance", out)
        self.assertIn("execution aster", out)

    def test_render_human_prints_unavailable_never_silent(self):
        BR.build_venue_bars = lambda ticker, interval="1h", n=6: (_ for _ in ()).throw(RuntimeError("dead"))
        b = BR.build_brief("SKYAI")
        self.assertFalse(b["venue_bars"]["available"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            BR.render_human(b)
        out = buf.getvalue()
        self.assertIn("tape: UNAVAILABLE", out)

    def test_tape_agreement_line_when_stop_level_crossed(self):
        """SKYAI's committed thesis stop is 0.225 (test_brief fixture) — too far below
        this fixture's 0.38-0.42 range to cross, so override the thesis stop via
        classify_token's returned thesis-adjacent state to exercise a level that DOES
        sit inside the fixture's range."""
        def fake_classify(tok, live=None):
            tok = dict(tok)
            tok["thesis"] = dict(tok["thesis"])
            tok["thesis"]["stop"] = 0.41   # sits inside [0.38, 0.42] — binance/aster both touch it
            return {"ticker": tok["ticker"], "verdict": "CONFIRMS",
                   "reason": "price within zone, funding flat", "direction": "SHORT",
                   "thesis_present": True, "live": live}
        BR.classify_token = fake_classify
        BR.build_venue_bars = lambda ticker, interval="1h", n=6: _venue_bars_fixture(binance_high=0.42, aster_high=0.40)
        b = BR.build_brief("SKYAI")
        vb = b["venue_bars"]
        # the stop (0.41) also needs to be on the STATE thesis display, which reads off
        # the ORIGINAL watchlist (0.225) — this test only exercises the tape line's
        # crossing logic directly (unit-level), not the full round trip through state.
        lines = BR._tape_agreement_lines(vb, {"direction": "SHORT", "stop": 0.41, "tp": [], "entry_zone": None, "watch_level": []})
        self.assertEqual(len(lines), 1)
        self.assertIn("crossed", lines[0])
        self.assertIn("binance", lines[0].lower())

    def test_tape_agreement_omitted_when_level_never_crossed(self):
        vb = _venue_bars_fixture()
        lines = BR._tape_agreement_lines(vb, {"direction": "SHORT", "stop": 999.0, "tp": [], "entry_zone": None, "watch_level": []})
        self.assertEqual(lines, [])

    def test_tape_agreement_names_execution_venue_check(self):
        vb = _venue_bars_fixture(binance_high=0.42, aster_high=0.40)
        lines = BR._tape_agreement_lines(vb, {"direction": "SHORT", "stop": 0.41, "tp": [], "entry_zone": None, "watch_level": []})
        self.assertEqual(len(lines), 1)
        self.assertIn("Aster ✗", lines[0])   # aster's high (0.40) never reached 0.41
        self.assertIn("Binance ✓", lines[0])


if __name__ == "__main__":
    unittest.main()
