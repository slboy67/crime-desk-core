#!/usr/bin/env python3
"""SPEC-186 — `venue_account.open_orders`: surface conditional/strategy orders that
`/fapi/v3/openOrders` is blind to (live-verified 2026-09-01: user's 3-leg OP set —
limit sell 0.1000 + stop 0.1055 + TP 0.0905 — was UI-confirmed resting, but
openOrders returned `[]` while $42.80 was reserved out of equity).

Endpoint hunt (live-fetched from github.com/asterdex/api-docs, V3(Recommended)/EN/
aster-finance-futures-api-v3.md, 2026-09-01): the ONLY conditional/strategy read is
`GET /fapi/v3/strategyOpenOrder` (+ `.../strategyHistoryOrder`) — and BOTH require an
already-known `strategyId` or `clientStrategyId`; there is no bulk-list endpoint for
strategy orders in Aster's documented API (`V1(Legacy)` has no strategy endpoints at
all — the `/fapi/v1/openOrders` null path from the ticket is simply unserved, not a
missing param). So an unknown/standalone conditional set is STRUCTURALLY unlistable
without its ID — the correct behavior is a loud `orders_coverage: partial` +
`hidden_margin_reservation` warning (the margin-reservation cross-check is what
caught the real gap), never a silent "no orders".
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
    def __init__(self, by_path, raise_on=()):
        self.by_path = by_path
        self.raise_on = set(raise_on)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for path in self.raise_on:
            if path in url:
                raise RuntimeError(f"unserved path {path}")
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


# ── fixtures ─────────────────────────────────────────────────────────────────────────
EMPTY_EQUITY = {"totalMarginBalance": "121.84000000", "availableBalance": "121.84000000",
               "totalUnrealizedProfit": "0.00000000"}

GAP_EQUITY = {"totalMarginBalance": "164.65000000", "availableBalance": "121.84000000",
             "totalUnrealizedProfit": "0.00000000"}

NO_POSITIONS = []

GALA_POSITIONS = [
    {"symbol": "GALAUSDT", "positionAmt": "-9910.00", "entryPrice": "0.00198",
     "markPrice": "0.00189", "leverage": "3", "isolatedMargin": "70.17",
     "liquidationPrice": "0.00240", "unRealizedProfit": "13.69"},
]

GALA_OPEN_ORDERS = [
    {"symbol": "GALAUSDT", "type": "STOP_MARKET", "stopPrice": "0.00192", "price": "0",
     "origQty": "9910", "reduceOnly": True},
    {"symbol": "GALAUSDT", "type": "TAKE_PROFIT_MARKET", "stopPrice": "0.00165", "price": "0",
     "origQty": "9910", "reduceOnly": True},
]

# The OP 3-leg OTOCO set from the live-verified problem: leg1 = LIMIT sell entry
# (firstDrivenId=0 -> immediately working), leg2 = STOP_MARKET, leg3 =
# TAKE_PROFIT_MARKET, both driven by leg1's FILLED event (firstDrivenId=1) -> not
# live orders yet, hence "conditional-entry".
OP_STRATEGY_FIXTURE = {
    "strategyId": 555, "clientStrategyId": "op-set-1", "strategyType": "OTOCO",
    "strategyStatus": "WORKING", "bookTime": 1, "updateTime": 2,
    "subOrders": [
        {"strategyId": 555, "orderId": 1, "strategySubId": 1, "firstDrivenId": 0,
         "symbol": "OPUSDT", "side": "SELL", "type": "LIMIT", "status": "NEW",
         "price": "0.1000", "stopPrice": "0", "quantity": "635", "reduceOnly": False},
        {"strategyId": 555, "orderId": 0, "strategySubId": 2, "firstDrivenId": 1,
         "symbol": "OPUSDT", "side": "BUY", "type": "STOP_MARKET", "status": "PENDING",
         "price": "0", "stopPrice": "0.1055", "quantity": "635", "reduceOnly": True},
        {"strategyId": 555, "orderId": 0, "strategySubId": 3, "firstDrivenId": 1,
         "symbol": "OPUSDT", "side": "BUY", "type": "TAKE_PROFIT_MARKET", "status": "PENDING",
         "price": "0", "stopPrice": "0.0905", "quantity": "635", "reduceOnly": True},
    ],
}


class RegularOrdersUniformKind(_WithRealKey):
    def test_stop_and_tp_map_to_lowercase_kinds_attached_to_position(self):
        fx = _FetchFixture({VA.READ_PATHS["open_orders"]: GALA_OPEN_ORDERS,
                           VA.READ_PATHS["positions"]: GALA_POSITIONS,
                           VA.READ_PATHS["equity"]: EMPTY_EQUITY})
        r = VA.open_orders(fetch_fn=fx)
        self.assertTrue(r["ok"], r)
        kinds = sorted(o["kind"] for o in r["data"]["orders"])
        self.assertEqual(kinds, ["stop", "tp"])
        for o in r["data"]["orders"]:
            self.assertTrue(o["attached_to_position"])
            self.assertEqual(o["symbol"], "GALAUSDT")
            self.assertTrue(o["reduce_only"])


class StandaloneConditionalSetVisible(_WithRealKey):
    def test_all_three_op_legs_surface_with_correct_prices_when_strategy_id_known(self):
        fx = _FetchFixture({VA.READ_PATHS["open_orders"]: [],
                           VA.READ_PATHS["positions"]: NO_POSITIONS,
                           VA.READ_PATHS["equity"]: GAP_EQUITY,
                           VA.READ_PATHS["strategy_open_order"]: OP_STRATEGY_FIXTURE})
        r = VA.open_orders(
            strategy_probes=[{"clientStrategyId": "op-set-1", "strategyType": "OTOCO"}],
            fetch_fn=fx)
        self.assertTrue(r["ok"], r)
        orders = r["data"]["orders"]
        self.assertEqual(len(orders), 3)
        by_kind = {o["kind"]: o for o in orders}
        self.assertEqual(by_kind["limit"]["price"], 0.1000)
        self.assertEqual(by_kind["conditional-entry"], by_kind["conditional-entry"])  # sanity
        conditional = [o for o in orders if o["kind"] == "conditional-entry"]
        self.assertEqual(len(conditional), 2)
        stop_prices = sorted(o["stopPrice"] for o in conditional)
        self.assertEqual(stop_prices, [0.0905, 0.1055])
        for o in orders:
            self.assertEqual(o["symbol"], "OPUSDT")
        # equity's reserved margin reconciles: total(164.65) - available(121.84) =
        # 42.81 ~= the $42.80 reservation from the ticket; no position margin to
        # subtract (NO_POSITIONS) -> the full delta is accounted for by these orders.
        self.assertNotIn("warnings", r["data"])
        self.assertEqual(r["data"]["orders_coverage"], "full")


class BlindPathLoudPartialCoverage(_WithRealKey):
    def test_no_known_strategy_id_plus_margin_gap_is_partial_with_reservation_warning(self):
        # exactly the live 2026-09-01 case: openOrders empty, no strategy_probes known
        # (nothing tells the desk an OTOCO exists), equity shows a $42.81 gap.
        fx = _FetchFixture({VA.READ_PATHS["open_orders"]: [],
                           VA.READ_PATHS["positions"]: NO_POSITIONS,
                           VA.READ_PATHS["equity"]: GAP_EQUITY})
        r = VA.open_orders(fetch_fn=fx)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["orders"], [])
        self.assertEqual(r["data"]["orders_coverage"], "partial")
        warnings = r["data"]["warnings"]
        self.assertEqual(len(warnings), 1)
        w = warnings[0]
        self.assertEqual(w["type"], "hidden_margin_reservation")
        self.assertAlmostEqual(w["delta_usd"], 42.81, places=2)

    def test_probed_strategy_path_error_is_loud_partial_with_reason(self):
        fx = _FetchFixture(
            {VA.READ_PATHS["open_orders"]: [], VA.READ_PATHS["positions"]: NO_POSITIONS,
             VA.READ_PATHS["equity"]: EMPTY_EQUITY},
            raise_on={VA.READ_PATHS["strategy_open_order"]})
        r = VA.open_orders(
            strategy_probes=[{"clientStrategyId": "op-set-1", "strategyType": "OTOCO"}],
            fetch_fn=fx)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["orders_coverage"], "partial")
        self.assertIn("coverage_reason", r["data"])
        self.assertIn("strategy", r["data"]["coverage_reason"])

    def test_dead_open_orders_path_is_loud_partial_never_silent_empty(self):
        fx = _FetchFixture({VA.READ_PATHS["positions"]: NO_POSITIONS,
                           VA.READ_PATHS["equity"]: EMPTY_EQUITY},
                           raise_on={VA.READ_PATHS["open_orders"]})
        r = VA.open_orders(fetch_fn=fx)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["orders"], [])
        self.assertEqual(r["data"]["orders_coverage"], "partial")
        self.assertIn("open_orders", r["data"]["coverage_reason"])


class EmptyAccountNoWarning(_WithRealKey):
    def test_empty_account_full_coverage_no_warning(self):
        fx = _FetchFixture({VA.READ_PATHS["open_orders"]: [],
                           VA.READ_PATHS["positions"]: NO_POSITIONS,
                           VA.READ_PATHS["equity"]: EMPTY_EQUITY})
        r = VA.open_orders(fetch_fn=fx)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["orders"], [])
        self.assertEqual(r["data"]["orders_coverage"], "full")
        self.assertNotIn("warnings", r["data"])


class ReadOnlyGuaranteeHoldsForNewEndpoint(unittest.TestCase):
    """The strategy ORDER-PLACEMENT endpoints (`placeStrategyOrder`/`updateStrategyOrder`)
    must stay unreachable — only the GET query endpoint is added to READ_PATHS."""

    def setUp(self):
        self.src = (ROOT / "capabilities" / "venue_account.py").read_text()

    def test_place_and_update_strategy_order_never_literal_call_targets(self):
        import re
        for path in ("/fapi/v3/placeStrategyOrder", "/fapi/v3/updateStrategyOrder"):
            pattern = r'["\']' + re.escape(path) + r'["\']'
            self.assertNotRegex(self.src, pattern)

    def test_strategy_open_order_is_a_get_read_path(self):
        self.assertEqual(VA.READ_PATHS["strategy_open_order"], "/fapi/v3/strategyOpenOrder")


if __name__ == "__main__":
    unittest.main(verbosity=2)
