#!/usr/bin/env python3
"""SPEC-171 — found on the SPEC-170 live acceptance (2026-08-28 12:24Z, read-only key):

1. `fills --since ... --json` (no `--symbols`) returned `{"ok": true, "data": []}` even
   though five trades had closed in the window — Aster's userTrades endpoint is
   per-symbol, and the capability silently returned nothing when no symbol was given.
   That's exactly the "empty list on failure" the SPEC-170 loud-failure contract forbids.
   Fixed: default-resolve the query set from (a) symbols with an open venue position,
   (b) config/positions.json positions[]/closed[] (closed[] filtered to closed_ts inside
   --since), (c) any --symbols given; an empty UNION is a loud `fills_no_symbols_resolved`,
   never a silent `[]`; a real (possibly empty) result carries `symbols_queried`.

2. `sync --dry-run` proposed `sl_set: 0.00206 -> 0.00211` on GALA, which holds TWO
   resting stops (0.00206 primary, 0.00211 backstop — the user's deliberate layering).
   `sl_set` must be the stop that fires FIRST on the adverse side (SHORT: lowest stop
   above mark; LONG: highest below), and EVERY resting stop must be persisted, not just
   whichever the venue listed first. Fixed via `_nearest_stop_and_tp` + a new
   `resting_stops` field (sorted nearest-first) on the synced position row.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import venue_account as VA        # noqa: E402


class _FetchFixture:
    """Same routing helper as tests/test_spec170_venue_account.py."""

    def __init__(self, by_path):
        self.by_path = by_path

    def __call__(self, url):
        for path, body in self.by_path.items():
            if path in url:
                return json.dumps(body).encode()
        raise AssertionError(f"no fixture registered for {url}")


class _WithRealKey(unittest.TestCase):
    def setUp(self):
        self._orig = VA._signer_and_key
        VA._signer_and_key = lambda: ("0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A", "0x" + "11" * 32)

    def tearDown(self):
        VA._signer_and_key = self._orig


# ── req 1: fills default-symbol resolution + loud failure ──────────────────────────────
GALA_POSITION_FIXTURE = [
    {"symbol": "GALAUSDT", "positionAmt": "-9910.00", "entryPrice": "0.00198",
     "markPrice": "0.00189", "leverage": "3", "isolatedMargin": "70.17",
     "liquidationPrice": "0.00240", "unRealizedProfit": "13.69"},
]

GALA_TRADES = [{"symbol": "GALAUSDT", "price": "0.00198", "qty": "9910", "side": "SELL",
               "realizedPnl": "0", "time": 1756137600000}]
BMT_TRADES = [{"symbol": "BMTUSDT", "price": "0.024", "qty": "100", "side": "SELL",
              "realizedPnl": "30.16", "time": 1756158378000}]


def _positions_json(tmp_dir, positions=None, closed=None):
    p = Path(tmp_dir) / "positions.json"
    p.write_text(json.dumps({"positions": positions or [], "closed": closed or []}))
    return p


class FillsDefaultSymbolResolution(_WithRealKey):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_no_symbols_no_positions_is_loud_never_empty_ok(self):
        p = _positions_json(self._tmp.name)
        fx = _FetchFixture({VA.READ_PATHS["positions"]: [], VA.READ_PATHS["open_orders"]: []})
        r = VA.fills(symbols=None, positions_path=p, fetch_fn=fx)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "fills_no_symbols_resolved")

    def test_open_venue_position_resolves_its_symbol(self):
        p = _positions_json(self._tmp.name)   # empty desk file — GALA only known via the VENUE
        fx = _FetchFixture({VA.READ_PATHS["positions"]: GALA_POSITION_FIXTURE,
                           VA.READ_PATHS["open_orders"]: [],
                           VA.READ_PATHS["user_trades"]: GALA_TRADES})
        r = VA.fills(symbols=None, positions_path=p, fetch_fn=fx)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["symbols_queried"], ["GALAUSDT"])
        self.assertEqual(len(r["data"]), 1)

    def test_desk_open_position_resolves_even_when_venue_read_fails(self):
        p = _positions_json(self._tmp.name, positions=[{"ticker": "GALA"}])

        def fx(url):
            if VA.READ_PATHS["user_trades"] in url:
                return json.dumps(GALA_TRADES).encode()
            raise RuntimeError("venue positions read is down")
        r = VA.fills(symbols=None, positions_path=p, fetch_fn=fx)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["symbols_queried"], ["GALAUSDT"])

    def test_closed_row_inside_since_window_resolved(self):
        p = _positions_json(self._tmp.name, closed=[
            {"ticker": "BMT", "closed_ts": "2026-08-25T22:46:18Z"},
            {"ticker": "FIDA", "closed_ts": "2026-08-19T00:00:00Z"},   # outside window
        ])
        fx = _FetchFixture({VA.READ_PATHS["positions"]: [], VA.READ_PATHS["open_orders"]: [],
                           VA.READ_PATHS["user_trades"]: BMT_TRADES})
        r = VA.fills(since="2026-08-20T00:00:00Z", symbols=None, positions_path=p, fetch_fn=fx)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["symbols_queried"], ["BMTUSDT"])   # FIDA excluded (closed before --since)

    def test_closed_row_missing_closed_ts_never_resolved(self):
        p = _positions_json(self._tmp.name, closed=[{"ticker": "ESP"}])   # no closed_ts at all
        fx = _FetchFixture({VA.READ_PATHS["positions"]: [], VA.READ_PATHS["open_orders"]: []})
        r = VA.fills(symbols=None, positions_path=p, fetch_fn=fx)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "fills_no_symbols_resolved")

    def test_explicit_symbols_bypasses_resolution_entirely(self):
        p = _positions_json(self._tmp.name)   # empty — irrelevant, --symbols given
        fx = _FetchFixture({VA.READ_PATHS["user_trades"]: GALA_TRADES})
        r = VA.fills(symbols=["GALAUSDT"], positions_path=p, fetch_fn=fx)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["symbols_queried"], ["GALAUSDT"])

    def test_resolved_but_zero_rows_is_ok_true_with_symbols_queried(self):
        p = _positions_json(self._tmp.name, positions=[{"ticker": "GALA"}])
        fx = _FetchFixture({VA.READ_PATHS["user_trades"]: []})
        r = VA.fills(symbols=None, positions_path=p, fetch_fn=fx)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"], [])
        self.assertEqual(r["symbols_queried"], ["GALAUSDT"])

    def test_bad_since_still_fails_loud_before_any_resolution(self):
        r = VA.fills(since="not-a-date", symbols=None)
        self.assertFalse(r["ok"])
        self.assertIn("bad --since", r["error"])


# ── req 2: sync persists ALL resting stops, sl_set = nearest adverse ────────────────────
class NearestStopSelection(unittest.TestCase):
    def test_short_two_stops_nearest_is_lowest_above_mark(self):
        orders = [{"type": "STOP", "price": 0.00211, "reduce_only": True},
                 {"type": "STOP", "price": 0.00206, "reduce_only": True},
                 {"type": "TP", "price": 0.00165, "reduce_only": True}]
        stops, sl, tp = VA._nearest_stop_and_tp(orders, "SHORT")
        self.assertEqual(stops, [0.00206, 0.00211])
        self.assertEqual(sl, 0.00206)
        self.assertEqual(tp, 0.00165)

    def test_long_two_stops_nearest_is_highest_below_mark(self):
        orders = [{"type": "STOP", "price": 0.0178, "reduce_only": True},
                 {"type": "STOP", "price": 0.0185, "reduce_only": True},
                 {"type": "TP", "price": 0.0225, "reduce_only": True},
                 {"type": "TP", "price": 0.027, "reduce_only": True}]
        stops, sl, tp = VA._nearest_stop_and_tp(orders, "LONG")
        self.assertEqual(stops, [0.0185, 0.0178])
        self.assertEqual(sl, 0.0185)
        self.assertEqual(tp, 0.0225)          # nearest TP above mark, not the far one

    def test_no_stops_is_none_not_a_crash(self):
        stops, sl, tp = VA._nearest_stop_and_tp([], "SHORT")
        self.assertEqual(stops, [])
        self.assertIsNone(sl)
        self.assertIsNone(tp)


GALA_TWO_STOPS_ORDERS = [
    {"symbol": "GALAUSDT", "type": "STOP_MARKET", "stopPrice": "0.00211", "reduceOnly": True},
    {"symbol": "GALAUSDT", "type": "STOP_MARKET", "stopPrice": "0.00206", "reduceOnly": True},
    {"symbol": "GALAUSDT", "type": "TAKE_PROFIT_MARKET", "stopPrice": "0.00165", "reduceOnly": True},
]


class SyncNearestStopIntegration(_WithRealKey):
    def _positions_json(self, positions):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "positions.json"
        p.write_text(json.dumps({"positions": positions}))
        return p

    def test_dry_run_proposes_no_change_to_sl_set_and_adds_resting_stops(self):
        desk_positions = [{"ticker": "GALA", "sl_set": 0.00206}]
        p = self._positions_json(desk_positions)
        fx = _FetchFixture({VA.READ_PATHS["positions"]: GALA_POSITION_FIXTURE,
                           VA.READ_PATHS["open_orders"]: GALA_TWO_STOPS_ORDERS})
        r = VA.sync(apply=False, positions_path=p, fetch_fn=fx)
        self.assertTrue(r["ok"], r)
        upd = next((u for u in r["data"]["updates"] if u["ticker"] == "GALA"), None)
        self.assertIsNotNone(upd)
        self.assertNotIn("sl_set", upd["diff"])                 # unchanged: still 0.00206
        self.assertIn("resting_stops", upd["diff"])
        self.assertEqual(upd["diff"]["resting_stops"]["to"], [0.00206, 0.00211])

    def test_apply_persists_resting_stops_list(self):
        desk_positions = [{"ticker": "GALA", "sl_set": 0.00206}]
        p = self._positions_json(desk_positions)
        fx = _FetchFixture({VA.READ_PATHS["positions"]: GALA_POSITION_FIXTURE,
                           VA.READ_PATHS["open_orders"]: GALA_TWO_STOPS_ORDERS})
        r = VA.sync(apply=True, positions_path=p, fetch_fn=fx)
        self.assertTrue(r["data"]["applied"])
        on_disk = json.loads(p.read_text())["positions"][0]
        self.assertEqual(on_disk["resting_stops"], [0.00206, 0.00211])
        self.assertEqual(on_disk["sl_set"], 0.00206)


if __name__ == "__main__":
    unittest.main(verbosity=2)
