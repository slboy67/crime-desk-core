#!/usr/bin/env python3
"""SPEC-170 — `venue_account.py`: READ-ONLY Aster account capability.

The signing test uses Aster's OWN documented worked example (signer + private key,
`V3(Recommended)/EN/aster-finance-futures-api-v3.md`, "Example of POST /fapi/v3/order"
— explicitly "for demonstration purposes only") as a deterministic offline fixture: it
signs with the demo key and asserts the signature recovers to the documented signer
address, proving the EIP-712 scheme is implemented correctly without any network call.

BLOCKER (see REVIEW-REQUEST-SPEC-170.md): `config/secrets.json`'s `aster_api_wallet_
address`/`aster_api_private_key` are still template placeholders in this worktree — no
live keyed call was possible for this batch. Every other test here uses `fetch_fn`
injection against fixtures built from the venue's documented response shapes.
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import venue_account as VA        # noqa: E402

# ── Aster's own documented demo credentials (explicitly "for demonstration purposes
# only" in api-docs) — used ONLY to prove the signing scheme offline, never a real key.
DEMO_SIGNER = "0x21cF8Ae13Bb72632562c6Fff438652Ba1a151bb0"
DEMO_PRIVKEY = "0x4fd0a42218f3eae43a6ce26d22544e986139a01e5b34a62db53757ffca81bae1"


class SigningIsDeterministicAndCorrect(unittest.TestCase):
    def test_privkey_derives_the_documented_signer_address(self):
        from eth_account import Account
        self.assertEqual(Account.from_key(DEMO_PRIVKEY).address, DEMO_SIGNER)

    def test_sign_params_recovers_to_the_signer_address(self):
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        qs = VA.sign_params({"symbol": "ASTERUSDT"}, DEMO_SIGNER, DEMO_PRIVKEY, nonce=1748310859508867)
        # qs = "...&signature=0x..."; strip the signature to re-derive the signed message
        params_part, sig_part = qs.rsplit("&signature=", 1)
        message = encode_typed_data(full_message=VA._typed_data(params_part))
        recovered = Account.recover_message(message, signature=bytes.fromhex(sig_part[2:]))
        self.assertEqual(recovered, DEMO_SIGNER)

    def test_same_inputs_same_nonce_are_byte_identical(self):
        a = VA.sign_params({"symbol": "ASTERUSDT"}, DEMO_SIGNER, DEMO_PRIVKEY, nonce=42)
        b = VA.sign_params({"symbol": "ASTERUSDT"}, DEMO_SIGNER, DEMO_PRIVKEY, nonce=42)
        self.assertEqual(a, b)

    def test_different_params_different_signature(self):
        a = VA.sign_params({"symbol": "ASTERUSDT"}, DEMO_SIGNER, DEMO_PRIVKEY, nonce=1)
        b = VA.sign_params({"symbol": "BTCUSDT"}, DEMO_SIGNER, DEMO_PRIVKEY, nonce=1)
        self.assertNotEqual(a, b)

    def test_nonce_and_signer_present_in_query_string(self):
        qs = VA.sign_params({"symbol": "ASTERUSDT"}, DEMO_SIGNER, DEMO_PRIVKEY, nonce=7)
        self.assertIn("nonce=7", qs)
        self.assertIn(f"signer={DEMO_SIGNER}", qs)
        self.assertIn("&signature=0x", qs)


class ReadOnlyByConstruction(unittest.TestCase):
    """HARD CONSTRAINT: this module may call ONLY GET account-read endpoints — no
    order/leverage-change/margin-type/transfer/withdrawal path is ever reachable."""

    def setUp(self):
        self.src = (ROOT / "capabilities" / "venue_account.py").read_text()

    def test_no_forbidden_endpoint_paths_as_literal_call_targets(self):
        # quote-bounded: catches an ACTUAL literal path used as a call target
        # ("/fapi/v3/order") without false-positiving on prose/docstring citations
        # (e.g. this module's own doc comment says '"Example of POST /fapi/v3/order"',
        # which has a space — not a quote — immediately before the path).
        forbidden_exact_paths = [
            "/fapi/v3/order", "/fapi/v3/batchOrders", "/fapi/v3/allOpenOrders",
            "/fapi/v3/marginType", "/fapi/v3/positionMargin", "/fapi/v3/leverage",
            "/fapi/v3/subAccountTransfer", "/fapi/v3/migrateUserAssets",
            "/fapi/v3/placeStrategyOrder", "/fapi/v3/updateStrategyOrder",
        ]
        for path in forbidden_exact_paths:
            pattern = r'["\']' + re.escape(path) + r'["\']'
            self.assertNotRegex(self.src, pattern,
                               f"forbidden path {path!r} appears as a literal call target")

    def test_read_paths_allowlist_is_exactly_the_seven_safe_reads(self):
        # explicit allowlist, not a substring heuristic (accountWithJoinMargin
        # legitimately contains "margin" — it's a balance READ, not a margin-type
        # change — so a naive substring ban on "margin" false-positives on it).
        # SPEC-186 added strategyOpenOrder (GET, ID-scoped query) — the mutating
        # placeStrategyOrder/updateStrategyOrder counterparts stay unreachable.
        self.assertEqual(set(VA.READ_PATHS.values()), {
            "/fapi/v3/accountWithJoinMargin", "/fapi/v3/positionRisk",
            "/fapi/v3/openOrders", "/fapi/v3/userTrades",
            "/fapi/v3/leverageBracket", "/fapi/v1/exchangeInfo",
            "/fapi/v3/strategyOpenOrder",
        })

    def test_no_data_kwarg_passed_to_urlopen_or_request(self):
        # a `data=` kwarg on urllib.request.Request/urlopen turns a GET into a POST —
        # none of this module's real network calls may ever pass one.
        self.assertNotIn("data=", self.src)

    def test_no_explicit_non_get_http_method(self):
        for verb in ("method=\"POST\"", "method='POST'", "method=\"DELETE\"", "method='DELETE'",
                     ".delete(", ".post("):
            self.assertNotIn(verb, self.src)

    def test_sign_params_not_exported_as_general_utility_name(self):
        # scoped to this module per the spec — no generic "signed_request"/"sign_and_send"
        # helper that a future caller could point at an order endpoint.
        for bad_name in ("signed_request", "sign_and_send", "signed_post"):
            self.assertNotIn(bad_name, self.src)


class _FetchFixture:
    """Routes fetch_fn(url) -> bytes by matching the READ_PATHS path embedded in the
    URL, so one object can back every command's `_get` calls in a test."""

    def __init__(self, by_path):
        self.by_path = by_path
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for path, body in self.by_path.items():
            if path in url:
                return json.dumps(body).encode()
        raise AssertionError(f"no fixture registered for {url}")


EQUITY_FIXTURE = {
    "totalMarginBalance": "199.00000000",
    "availableBalance": "110.70000000",
    "totalUnrealizedProfit": "13.69000000",
}

POSITIONS_FIXTURE = [
    {"symbol": "GALAUSDT", "positionAmt": "-9910.00", "entryPrice": "0.00198",
     "markPrice": "0.00189", "leverage": "3", "isolatedMargin": "70.17",
     "liquidationPrice": "0.00240", "unRealizedProfit": "13.69"},
    {"symbol": "BTCUSDT", "positionAmt": "0", "entryPrice": "0.00000",
     "markPrice": "60000", "leverage": "10", "isolatedMargin": "0",
     "liquidationPrice": "0", "unRealizedProfit": "0"},
]

OPEN_ORDERS_FIXTURE = [
    {"symbol": "GALAUSDT", "type": "STOP_MARKET", "stopPrice": "0.00192", "reduceOnly": True},
    {"symbol": "GALAUSDT", "type": "TAKE_PROFIT_MARKET", "stopPrice": "0.00165", "reduceOnly": True},
]

USER_TRADES_FIXTURE = [
    {"symbol": "GALAUSDT", "price": "0.00198", "qty": "9910", "side": "SELL",
     "realizedPnl": "0", "time": 1756137600000},
]

LEVERAGE_BRACKET_FIXTURE = [
    {"symbol": "GALAUSDT", "brackets": [{"initialLeverage": 5, "notionalCap": 10000}]},
    {"symbol": "BTRUSDT", "brackets": [{"initialLeverage": 5, "notionalCap": 10000}]},
    {"symbol": "CASHCATUSDT", "brackets": [{"initialLeverage": 10, "notionalCap": 10000}]},
]

EXCHANGE_INFO_FIXTURE = {"symbols": [
    {"symbol": "GALAUSDT", "status": "TRADING"},
    {"symbol": "BTRUSDT", "status": "TRADING"},
    {"symbol": "CASHCATUSDT", "status": "TRADING"},
    {"symbol": "DELISTEDUSDT", "status": "BREAK"},
]}


class _WithRealKey(unittest.TestCase):
    """These commands call `_signer_and_key()` first — inject a fake real-looking key
    pair so the fixture path is exercised (the placeholder-detection path is tested
    separately, in AsterKeyMissingContract)."""

    def setUp(self):
        self._orig = VA._signer_and_key
        VA._signer_and_key = lambda: ("0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A", "0x" + "11" * 32)

    def tearDown(self):
        VA._signer_and_key = self._orig


class EquityParsing(_WithRealKey):
    def test_maps_to_perp_total_value_shape(self):
        fx = _FetchFixture({VA.READ_PATHS["equity"]: EQUITY_FIXTURE})
        r = VA.equity(fetch_fn=fx)
        self.assertTrue(r["ok"])
        d = r["data"]
        self.assertEqual(d["perp_total_value_usd"], 199.0)
        self.assertEqual(d["available_usd"], 110.7)
        self.assertEqual(d["unrealized_pnl_usd"], 13.69)
        self.assertIn("ts", d)

    def test_malformed_response_is_loud_not_a_crash(self):
        fx = _FetchFixture({VA.READ_PATHS["equity"]: {"unexpected": "shape"}})
        r = VA.equity(fetch_fn=fx)
        self.assertFalse(r["ok"])
        self.assertIn("malformed", r["error"])


class PositionsParsing(_WithRealKey):
    def test_zero_amt_positions_excluded(self):
        fx = _FetchFixture({VA.READ_PATHS["positions"]: POSITIONS_FIXTURE,
                           VA.READ_PATHS["open_orders"]: []})
        r = VA.positions(fetch_fn=fx)
        self.assertTrue(r["ok"])
        symbols = [p["symbol"] for p in r["data"]]
        self.assertEqual(symbols, ["GALAUSDT"])           # BTCUSDT (amt=0) excluded

    def test_gala_short_leverage_3_and_resting_orders(self):
        fx = _FetchFixture({VA.READ_PATHS["positions"]: POSITIONS_FIXTURE,
                           VA.READ_PATHS["open_orders"]: OPEN_ORDERS_FIXTURE})
        r = VA.positions(fetch_fn=fx)
        gala = r["data"][0]
        self.assertEqual(gala["side"], "SHORT")
        self.assertEqual(gala["leverage"], 3.0)
        kinds = sorted(o["type"] for o in gala["resting_orders"])
        self.assertEqual(kinds, ["STOP", "TP"])
        stop = next(o for o in gala["resting_orders"] if o["type"] == "STOP")
        self.assertEqual(stop["price"], 0.00192)
        self.assertTrue(stop["reduce_only"])

    def test_dead_open_orders_leg_degrades_resting_to_empty_not_fatal(self):
        def fx(url):
            if VA.READ_PATHS["positions"] in url:
                return json.dumps(POSITIONS_FIXTURE).encode()
            raise RuntimeError("open orders dead")
        r = VA.positions(fetch_fn=fx)
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"][0]["resting_orders"], [])


class FillsParsing(_WithRealKey):
    def test_no_symbols_no_desk_positions_is_loud_fills_no_symbols_resolved(self):
        # SPEC-171: superseded the old flat "symbol_required" contract — no --symbols now
        # means "resolve from the venue + config/positions.json" (see
        # tests/test_spec171_venue_fills_default_symbols_and_nearest_stop.py for the full
        # resolution matrix); an isolated empty positions.json + a dead venue fetch_fn is
        # the one case that's STILL loud, never a silent ok:true/[].
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "positions.json"
            p.write_text(json.dumps({"positions": [], "closed": []}))

            def fx(url):
                raise RuntimeError("no venue in this test")
            r = VA.fills(symbols=None, positions_path=p, fetch_fn=fx)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "fills_no_symbols_resolved")

    def test_maps_trades_shape(self):
        fx = _FetchFixture({VA.READ_PATHS["user_trades"]: USER_TRADES_FIXTURE})
        r = VA.fills(symbols=["GALAUSDT"], fetch_fn=fx)
        self.assertTrue(r["ok"])
        t = r["data"][0]
        self.assertEqual(t["side"], "SELL")
        self.assertEqual(t["price"], 0.00198)
        self.assertEqual(t["ts"], 1756137600000)

    def test_bad_since_is_loud(self):
        r = VA.fills(since="not-a-date", symbols=["GALAUSDT"], fetch_fn=lambda u: b"[]")
        self.assertFalse(r["ok"])

    def test_per_symbol_error_isolated_to_errors_key(self):
        def fx(url):
            if "GALAUSDT" in url:
                return json.dumps(USER_TRADES_FIXTURE).encode()
            raise RuntimeError("dead symbol")
        r = VA.fills(symbols=["GALAUSDT", "DEADUSDT"], fetch_fn=fx)
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["data"]), 1)
        self.assertIn("DEADUSDT", r["errors"])


class MaxLeverageParsing(_WithRealKey):
    def test_symbols_filter(self):
        fx = _FetchFixture({VA.READ_PATHS["leverage_bracket"]: LEVERAGE_BRACKET_FIXTURE})
        r = VA.max_leverage(symbols=["GALAUSDT", "BTRUSDT", "CASHCATUSDT"], fetch_fn=fx)
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"], {"GALAUSDT": 5, "BTRUSDT": 5, "CASHCATUSDT": 10})

    def test_write_refuses_on_sanity_mismatch(self):
        bad = [{"symbol": "GALAUSDT", "brackets": [{"initialLeverage": 25}]},  # wrong!
              {"symbol": "BTRUSDT", "brackets": [{"initialLeverage": 5}]},
              {"symbol": "CASHCATUSDT", "brackets": [{"initialLeverage": 10}]}]
        fx = _FetchFixture({VA.READ_PATHS["leverage_bracket"]: bad,
                           VA.READ_PATHS["exchange_info"]: EXCHANGE_INFO_FIXTURE})
        with tempfile.TemporaryDirectory() as tmp:
            orig = VA.MAX_LEV_PATH
            VA.MAX_LEV_PATH = Path(tmp) / "aster_max_leverage.json"
            try:
                r = VA.max_leverage(write=True, fetch_fn=fx)
                self.assertFalse(r["ok"])
                self.assertEqual(r["error"], "max_lev_sanity_check_failed")
                self.assertFalse(VA.MAX_LEV_PATH.exists())
            finally:
                VA.MAX_LEV_PATH = orig

    def test_write_succeeds_and_filters_to_trading_symbols(self):
        fx = _FetchFixture({VA.READ_PATHS["leverage_bracket"]: LEVERAGE_BRACKET_FIXTURE,
                           VA.READ_PATHS["exchange_info"]: EXCHANGE_INFO_FIXTURE})
        with tempfile.TemporaryDirectory() as tmp:
            orig = VA.MAX_LEV_PATH
            VA.MAX_LEV_PATH = Path(tmp) / "aster_max_leverage.json"
            try:
                r = VA.max_leverage(write=True, fetch_fn=fx)
                self.assertTrue(r["ok"], r)
                written = json.loads(VA.MAX_LEV_PATH.read_text())
                self.assertEqual(written["GALAUSDT"]["max_lev"], 5)
                self.assertEqual(written["CASHCATUSDT"]["max_lev"], 10)
                self.assertIn("ts", written["GALAUSDT"])
            finally:
                VA.MAX_LEV_PATH = orig


class SyncDiffRules(_WithRealKey):
    def _positions_json(self, positions):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "positions.json"
        p.write_text(json.dumps({"positions": positions}))
        return p

    def test_venue_owned_fields_diffed_desk_owned_untouched(self):
        desk_positions = [{"ticker": "GALA", "notional_usd": 500, "margin_usd": 50,
                          "leverage": 10, "entry": 0.002, "liq": 0.003,
                          "signature": "faded_bounce", "note": "do not touch"}]
        p = self._positions_json(desk_positions)
        fx = _FetchFixture({VA.READ_PATHS["positions"]: POSITIONS_FIXTURE,
                           VA.READ_PATHS["open_orders"]: OPEN_ORDERS_FIXTURE})
        r = VA.sync(apply=False, positions_path=p, fetch_fn=fx)
        self.assertTrue(r["ok"], r)
        upd = r["data"]["updates"][0]
        self.assertEqual(upd["ticker"], "GALA")
        for k in ("notional_usd", "margin_usd", "leverage", "entry", "liq", "sl_set", "tp_set"):
            self.assertIn(k, upd["diff"])
        self.assertNotIn("signature", upd["diff"])
        self.assertNotIn("note", upd["diff"])
        self.assertFalse(r["data"]["applied"])          # dry-run: nothing written
        on_disk = json.loads(p.read_text())
        self.assertEqual(on_disk["positions"][0]["notional_usd"], 500)   # unchanged on dry-run

    def test_apply_writes_venue_fields_preserves_desk_fields(self):
        desk_positions = [{"ticker": "GALA", "notional_usd": 500, "leverage": 10,
                          "signature": "faded_bounce", "thesis_ref": "GALA WATCH",
                          "note": "keep me"}]
        p = self._positions_json(desk_positions)
        fx = _FetchFixture({VA.READ_PATHS["positions"]: POSITIONS_FIXTURE,
                           VA.READ_PATHS["open_orders"]: OPEN_ORDERS_FIXTURE})
        r = VA.sync(apply=True, positions_path=p, fetch_fn=fx)
        self.assertTrue(r["data"]["applied"])
        on_disk = json.loads(p.read_text())["positions"][0]
        self.assertEqual(on_disk["leverage"], 3.0)              # venue-owned, overwritten
        self.assertEqual(on_disk["signature"], "faded_bounce")   # desk-owned, untouched
        self.assertEqual(on_disk["note"], "keep me")

    def test_desk_row_with_no_venue_position_flagged_closed_on_venue(self):
        desk_positions = [{"ticker": "DEAD", "notional_usd": 100}]
        p = self._positions_json(desk_positions)
        fx = _FetchFixture({VA.READ_PATHS["positions"]: POSITIONS_FIXTURE,
                           VA.READ_PATHS["open_orders"]: []})
        r = VA.sync(apply=False, positions_path=p, fetch_fn=fx)
        self.assertIn("DEAD", r["data"]["closed_on_venue"])
        self.assertEqual(r["data"]["updates"], [])

    def test_venue_position_with_no_desk_row_flagged_untracked(self):
        p = self._positions_json([])
        fx = _FetchFixture({VA.READ_PATHS["positions"]: POSITIONS_FIXTURE,
                           VA.READ_PATHS["open_orders"]: []})
        r = VA.sync(apply=False, positions_path=p, fetch_fn=fx)
        untracked_tickers = [u["ticker"] for u in r["data"]["untracked"]]
        self.assertIn("GALA", untracked_tickers)


class AsterKeyMissingContract(unittest.TestCase):
    """SPEC-170 req 3: missing/placeholder key -> loud, structured failure — never an
    empty list masquerading as 'no positions'."""

    def setUp(self):
        self._orig = VA.SECRETS_PATH

    def tearDown(self):
        VA.SECRETS_PATH = self._orig

    def _write_secrets(self, tmp_dir, signer, priv):
        p = Path(tmp_dir) / "secrets.json"
        p.write_text(json.dumps({"aster_api_wallet_address": signer, "aster_api_private_key": priv}))
        VA.SECRETS_PATH = p

    def test_placeholder_key_is_aster_key_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_secrets(tmp, "PASTE_ADDRESS_HERE", "PASTE_KEY_HERE")
            self.assertEqual(VA.equity(), {"ok": False, "error": "aster_key_missing"})
            self.assertEqual(VA.positions(), {"ok": False, "error": "aster_key_missing"})

    def test_missing_secrets_file_is_aster_key_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            VA.SECRETS_PATH = Path(tmp) / "does_not_exist.json"
            self.assertEqual(VA.equity(), {"ok": False, "error": "aster_key_missing"})

    def test_never_an_empty_list_on_key_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_secrets(tmp, "PASTE_ADDRESS_HERE", "PASTE_KEY_HERE")
            r = VA.positions()
            self.assertNotEqual(r, {"ok": True, "data": []})
            self.assertFalse(r["ok"])


class VenueHttpErrorPassthrough(_WithRealKey):
    def test_http_error_code_and_msg_passthrough(self):
        def fx(url):
            raise urllib_http_error_stub()
        r = VA.equity(fetch_fn=fx)
        self.assertFalse(r["ok"])


def urllib_http_error_stub():
    import urllib.error
    import io
    e = urllib.error.HTTPError("http://x", 401, "Unauthorized",
                               hdrs=None, fp=io.BytesIO(b'{"code":-2015,"msg":"Invalid signature."}'))
    return e


class DiscoveryTickWiresVenueAccount(unittest.TestCase):
    """SPEC-170 req 4: ops/discovery_tick.sh calls `max_leverage --write` daily and
    `sync --dry-run` per tick, paging desk when a CLOSED_ON_VENUE/UNTRACKED flag
    appears (SPEC-157 desk routing discipline)."""

    def test_discovery_tick_calls_max_leverage_write_and_sync_dry_run(self):
        src = (ROOT / "ops" / "discovery_tick.sh").read_text()
        self.assertIn("venue_account.py max_leverage --write", src)
        self.assertIn("venue_account.py sync --dry-run", src)
        positions_notify_lines = [ln for ln in src.splitlines()
                                  if "ops/notify.sh" in ln and "POSITIONS drift" in ln]
        self.assertTrue(positions_notify_lines, "expected a POSITIONS-drift notify.sh call")
        for ln in positions_notify_lines:
            self.assertIn('"desk"', ln)


if __name__ == "__main__":
    unittest.main(verbosity=2)
