#!/usr/bin/env python3
"""venue_account.py — SPEC-170: READ-ONLY Aster account capability (ADR 0001: the desk
READS the venue, it never executes).

Facts the desk needs from the venue and cannot get any other way: per-symbol max
leverage (exchangeInfo's margin fields do NOT encode it — verified against the UI
slider 2026-08-28), and real positions/fills/equity so config/positions.json stops
drifting from what the venue actually shows.

Aster's V3 API uses an **API-wallet** signing scheme, not a Binance-style HMAC key: an
EVM address (`signer`) + its private key SIGN each request as EIP-712 typed data
(domain `AsterSignTransaction`, chainId 1666) over the urlencoded param string (incl.
`nonce` + `signer`); the hex signature is appended as a `signature` query param. A
keyless HMAC-style key (as `aster_listing.py`'s public exchangeInfo call uses) gets
`{"code":-2014,"msg":"API-key format invalid."}` on any signed endpoint — confirmed
2026-08-28. Source: github.com/asterdex/api-docs, V3(Recommended)/EN/
aster-finance-futures-api-v3.md ("Authentication signature payload" +
"Example of POST /fapi/v3/order").

  python3 capabilities/venue_account.py equity --json
  python3 capabilities/venue_account.py positions --json
  python3 capabilities/venue_account.py open_orders --json   # SPEC-186: regular +
      # (optionally) known conditional/strategy orders, loud partial-coverage +
      # hidden_margin_reservation warning when the read can't see everything
  python3 capabilities/venue_account.py open_orders --client-strategy-id op-set-1 \
      --strategy-type OTOCO --json   # resolve one KNOWN standalone conditional set
  python3 capabilities/venue_account.py fills --since 2026-08-01T00:00:00Z --symbols GALAUSDT --json
  python3 capabilities/venue_account.py fills --since 2026-08-01T00:00:00Z --json   # SPEC-171: no
      # --symbols = resolve from open venue positions + config/positions.json positions[]/closed[]
  python3 capabilities/venue_account.py max_leverage --symbols GALAUSDT,BTRUSDT,CASHCATUSDT --json
  python3 capabilities/venue_account.py max_leverage --write --json
  python3 capabilities/venue_account.py sync --dry-run --json

## HARD CONSTRAINT — read-only by construction, not by key permission
This module calls ONLY GET on the READ_PATHS allowlist below. No `/order`,
`/batchOrders`, `/allOpenOrders` (DELETE), `POST /leverage`, `/marginType`,
`/positionMargin`, transfers, or withdrawals is ever imported, wrapped, or
reachable — `tests/test_spec170_venue_account.py` greps this file for those paths
and for any non-GET HTTP verb and fails the suite if found. `sign_params` (the
signing helper) is scoped to this module — it is not exposed as a general
"signed request" utility.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_typed_data

REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "config"
SECRETS_PATH = CONFIG_DIR / "secrets.json"
POSITIONS_PATH = CONFIG_DIR / "positions.json"
MAX_LEV_PATH = CONFIG_DIR / "aster_max_leverage.json"

BASE_URL = "https://fapi.asterdex.com"
CHAIN_ID = 1666
DOMAIN_NAME = "AsterSignTransaction"

# The ENTIRE set of endpoints this module can ever call — every other capability, incl.
# any order/leverage-change/transfer/withdrawal endpoint, is simply absent from this
# dict and therefore unreachable through `_get`.
READ_PATHS = {
    "equity": "/fapi/v3/accountWithJoinMargin",
    "positions": "/fapi/v3/positionRisk",
    "open_orders": "/fapi/v3/openOrders",
    "user_trades": "/fapi/v3/userTrades",
    "leverage_bracket": "/fapi/v3/leverageBracket",
    "exchange_info": "/fapi/v1/exchangeInfo",
    # SPEC-186: the ONLY conditional/strategy-order READ Aster serves (live-fetched
    # 2026-09-01 from github.com/asterdex/api-docs, V3(Recommended)/EN/aster-finance-
    # futures-api-v3.md "Query Strategy Open Order"). It is ID-scoped — either
    # strategyId or clientStrategyId is mandatory — there is no bulk-list endpoint for
    # strategy orders anywhere in the documented API (V1(Legacy) has no strategy
    # endpoints at all). `/fapi/v3/placeStrategyOrder` and `/fapi/v3/updateStrategyOrder`
    # are the mutating counterparts and must NEVER be added here.
    "strategy_open_order": "/fapi/v3/strategyOpenOrder",
}

# Slider-verified 2026-08-28 (Aster Pro UI) — max_leverage --write refuses to persist
# config/aster_max_leverage.json unless these hold, so a bad/partial leverageBracket
# read can never silently corrupt the size-cap config.
MAX_LEV_SANITY = {"GALAUSDT": 5, "BTRUSDT": 5, "CASHCATUSDT": 10}

VENUE_OWNED_FIELDS = ("notional_usd", "margin_usd", "leverage", "entry", "liq", "sl_set", "tp_set",
                      "resting_stops")
DESK_OWNED_FIELDS = ("signature", "thesis_ref", "stop", "tp", "tp_plan", "desk_disagreed", "note")


# ── secrets / signing ────────────────────────────────────────────────────────────────
def _load_secrets():
    try:
        return json.loads(SECRETS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _signer_and_key():
    """(signer, private_key) from config/secrets.json, or (None, None) when missing OR
    still a template placeholder (`PASTE...`) — never a partially-real credential pair."""
    s = _load_secrets()
    signer = s.get("aster_api_wallet_address")
    priv = s.get("aster_api_private_key")
    if not signer or not priv:
        return None, None
    if str(signer).upper().startswith("PASTE") or str(priv).upper().startswith("PASTE"):
        return None, None
    return signer, priv


def _typed_data(msg):
    """The exact EIP-712 typed-data shape from Aster's own docs (domain
    AsterSignTransaction, chainId 1666, single `Message{msg: string}` field)."""
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Message": [{"name": "msg", "type": "string"}],
        },
        "primaryType": "Message",
        "domain": {"name": DOMAIN_NAME, "version": "1", "chainId": CHAIN_ID,
                   "verifyingContract": "0x0000000000000000000000000000000000000000"},
        "message": {"msg": msg},
    }


def sign_params(params, signer, priv, nonce=None):
    """Pure signing helper — the urlencoded param string (params + nonce + signer),
    EIP-712-signed, returned as the full query string with `&signature=` appended.
    Never touches the network; deterministic given a fixed `nonce` (tests pin one)."""
    p = dict(params)
    p["nonce"] = str(nonce if nonce is not None else int(time.time() * 1_000_000))
    p["signer"] = signer
    qs = urllib.parse.urlencode(p)
    message = encode_typed_data(full_message=_typed_data(qs))
    signed = Account.sign_message(message, private_key=priv)
    sig = signed.signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    return qs + "&signature=" + sig


# ── the one GET path ─────────────────────────────────────────────────────────────────
def _get(read_key, params=None, timeout=15, fetch_fn=None):
    """GET-only call to one of READ_PATHS. `fetch_fn(url) -> bytes` is injectable
    (offline fixtures in tests); the default does a real signed HTTP GET — never a
    POST/DELETE, never any path outside READ_PATHS. Loud failure contract (req 3):
    missing key -> {"ok": false, "error": "aster_key_missing"}; venue auth/signing
    error -> the venue's code+msg verbatim; never an empty list on failure."""
    if read_key not in READ_PATHS:
        raise ValueError(f"unknown read key {read_key!r}")
    signer, priv = _signer_and_key()
    if not signer:
        return {"ok": False, "error": "aster_key_missing"}
    qs = sign_params(params or {}, signer, priv)
    url = BASE_URL + READ_PATHS[read_key] + "?" + qs
    try:
        if fetch_fn is not None:
            body = fetch_fn(url)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "crime-desk"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # GET — no request body
                body = resp.read()
        data = json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
            return {"ok": False, "error": err.get("msg") or str(e), "code": err.get("code"),
                    "http_status": e.code}
        except Exception:  # noqa: BLE001 — a malformed error body still reports SOMETHING
            return {"ok": False, "error": str(e), "http_status": e.code}
    except Exception as e:  # noqa: BLE001 — network/timeout/json-decode all land here
        return {"ok": False, "error": str(e)}
    return {"ok": True, "data": data}


# ── commands ─────────────────────────────────────────────────────────────────────────
def equity(fetch_fn=None):
    """{"perp_total_value_usd", "available_usd", "unrealized_pnl_usd", "ts"} — the
    "Perp Total Value" the Aster UI shows (§9's `equity`)."""
    r = _get("equity", fetch_fn=fetch_fn)
    if not r["ok"]:
        return r
    d = r["data"]
    try:
        out = {"perp_total_value_usd": float(d["totalMarginBalance"]),
               "available_usd": float(d["availableBalance"]),
               "unrealized_pnl_usd": float(d["totalUnrealizedProfit"]),
               "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    except (KeyError, TypeError, ValueError) as e:
        return {"ok": False, "error": f"malformed equity response: {e}"}
    return {"ok": True, "data": out}


def _order_kind(o):
    t = (o.get("type") or "").upper()
    if "TAKE_PROFIT" in t:
        return "TP"
    if "STOP" in t:
        return "STOP"
    return None


def positions(fetch_fn=None, orders_fetch_fn=None):
    """Per open position: symbol, side, notional_usd, margin_usd, leverage, entry,
    mark, liq_price, unrealized_pnl, resting_orders: [{type: STOP|TP, price,
    reduce_only}]. positionRisk failing is fatal to the read; openOrders failing
    degrades resting_orders to [] for every symbol (best-effort — a dead orders leg
    must never hide the positions themselves)."""
    r = _get("positions", fetch_fn=fetch_fn)
    if not r["ok"]:
        return r
    oo = _get("open_orders", fetch_fn=orders_fetch_fn if orders_fetch_fn is not None else fetch_fn)
    orders_by_symbol = {}
    if oo.get("ok"):
        for o in oo["data"]:
            orders_by_symbol.setdefault(o.get("symbol"), []).append(o)
    out = []
    for p in r["data"]:
        try:
            amt = float(p.get("positionAmt", 0) or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if amt == 0:
            continue
        sym = p.get("symbol")
        mark = float(p["markPrice"]) if p.get("markPrice") not in (None, "") else None
        entry = float(p["entryPrice"]) if p.get("entryPrice") not in (None, "") else None
        liq = float(p["liquidationPrice"]) if p.get("liquidationPrice") not in (None, "") else None
        margin = float(p["isolatedMargin"]) if p.get("isolatedMargin") not in (None, "") else None
        lev = float(p["leverage"]) if p.get("leverage") not in (None, "") else None
        resting = []
        for o in orders_by_symbol.get(sym, []):
            kind = _order_kind(o)
            if not kind:
                continue
            try:
                price = float(o.get("stopPrice"))
            except (TypeError, ValueError):
                price = None
            resting.append({"type": kind, "price": price, "reduce_only": bool(o.get("reduceOnly"))})
        out.append({
            "symbol": sym, "side": "LONG" if amt > 0 else "SHORT",
            "notional_usd": round(abs(amt) * mark, 2) if mark is not None else None,
            "margin_usd": margin, "leverage": lev, "entry": entry, "mark": mark,
            "liq_price": liq,
            "unrealized_pnl": (float(p["unRealizedProfit"])
                              if p.get("unRealizedProfit") not in (None, "") else None),
            "resting_orders": resting,
        })
    return {"ok": True, "data": out}


def _regular_order_uniform(o, attached_symbols):
    """Map one `/fapi/v3/openOrders` row to the SPEC-186 uniform order shape."""
    t = (o.get("type") or "").upper()
    if "TAKE_PROFIT" in t:
        kind = "tp"
    elif "STOP" in t:
        kind = "stop"
    else:
        kind = "limit"
    try:
        price = float(o["price"]) if o.get("price") not in (None, "", "0") else None
    except (TypeError, ValueError):
        price = None
    try:
        stop_price = float(o["stopPrice"]) if o.get("stopPrice") not in (None, "", "0") else None
    except (TypeError, ValueError):
        stop_price = None
    try:
        qty = float(o["origQty"]) if o.get("origQty") not in (None, "") else None
    except (TypeError, ValueError):
        qty = None
    return {"symbol": o.get("symbol"), "kind": kind, "price": price, "stopPrice": stop_price,
            "qty": qty, "reduce_only": bool(o.get("reduceOnly")),
            "attached_to_position": o.get("symbol") in attached_symbols}


def _strategy_suborder_uniform(so, attached_symbols):
    """Map one `subOrders[]` entry from `strategyOpenOrder` to the uniform shape. A
    suborder driven by another suborder's event (`firstDrivenId` non-zero) has no
    live venue order yet — it is a PLANNED leg, not a resting order — hence
    `conditional-entry` regardless of its eventual limit/stop/tp type."""
    driven_by = so.get("firstDrivenId")
    if driven_by not in (0, None, "0"):
        kind = "conditional-entry"
    else:
        t = (so.get("type") or "").upper()
        if "TAKE_PROFIT" in t:
            kind = "tp"
        elif "STOP" in t:
            kind = "stop"
        else:
            kind = "limit"
    try:
        price = float(so["price"]) if so.get("price") not in (None, "", "0") else None
    except (TypeError, ValueError):
        price = None
    try:
        stop_price = float(so["stopPrice"]) if so.get("stopPrice") not in (None, "", "0") else None
    except (TypeError, ValueError):
        stop_price = None
    try:
        qty = float(so["quantity"]) if so.get("quantity") not in (None, "") else None
    except (TypeError, ValueError):
        qty = None
    return {"symbol": so.get("symbol"), "kind": kind, "price": price, "stopPrice": stop_price,
            "qty": qty, "reduce_only": bool(so.get("reduceOnly")),
            "attached_to_position": so.get("symbol") in attached_symbols}


def open_orders(strategy_probes=None, fetch_fn=None, positions_fetch_fn=None, equity_fetch_fn=None):
    """SPEC-186: the uniform resting-order read — regular openOrders (limit/stop/tp,
    incl. TP/SL attached to a position) PLUS any known conditional/strategy orders
    (`strategy_probes`: `[{"strategyId"|"clientStrategyId": ..., "strategyType":
    "OTO"|"OCO"|"OTOCO"}]` — Aster's strategyOpenOrder read is ID-scoped, so a
    standalone conditional set is only resolvable when its ID is already known, e.g.
    confirmed by hand against the UI).

    `orders_coverage` is `"partial"` (never a silent `"no orders"`) whenever: (a) a
    probed path (openOrders or a given strategy probe) errors/is unserved, or (b) the
    order list came back EMPTY yet the margin-reservation cross-check (equity total −
    available − Σ position margin) finds > $1 unaccounted for — that gap, with
    nothing visible to explain it, is direct evidence of a resting order this read
    could not see (the real 2026-09-01 case: $42.80 reserved, openOrders `[]`, no
    strategy_probes known). Once ANY order is actually resolved (openOrders or a
    successful strategy probe) the reservation is no longer "hidden" by definition —
    the warning does not attempt per-order margin attribution beyond that. `"full"`
    only when every probed path succeeded AND (orders were found OR the margin
    reconciles)."""
    coverage = "full"
    reasons = []
    orders = []

    pos_r = positions(fetch_fn=positions_fetch_fn if positions_fetch_fn is not None else fetch_fn)
    attached_symbols = {p["symbol"] for p in pos_r["data"]} if pos_r.get("ok") else set()
    position_margins = [p.get("margin_usd") or 0 for p in pos_r["data"]] if pos_r.get("ok") else []
    if not pos_r.get("ok"):
        coverage = "partial"
        reasons.append(f"positions: {pos_r.get('error')}")

    oo = _get("open_orders", fetch_fn=fetch_fn)
    if not oo["ok"]:
        coverage = "partial"
        reasons.append(f"open_orders: {oo.get('error')}")
    else:
        for o in oo["data"]:
            orders.append(_regular_order_uniform(o, attached_symbols))

    for probe in (strategy_probes or []):
        params = {k: v for k, v in probe.items() if k in ("strategyId", "clientStrategyId", "strategyType")}
        r = _get("strategy_open_order", params=params, fetch_fn=fetch_fn)
        if not r["ok"]:
            coverage = "partial"
            reasons.append(f"strategy {probe}: {r.get('error')}")
            continue
        for so in (r["data"].get("subOrders") or []):
            orders.append(_strategy_suborder_uniform(so, attached_symbols))

    eq_r = equity(fetch_fn=equity_fetch_fn if equity_fetch_fn is not None else fetch_fn)
    warnings = []
    if eq_r.get("ok") and pos_r.get("ok"):
        delta = round(eq_r["data"]["perp_total_value_usd"] - eq_r["data"]["available_usd"]
                     - sum(position_margins), 2)
        if delta > 1.0 and not orders:
            coverage = "partial"
            warnings.append({"type": "hidden_margin_reservation", "delta_usd": delta,
                             "detail": f"${delta} reserved by orders this read cannot see "
                                       f"(equity total - available - position margin)"})
    elif not eq_r.get("ok"):
        coverage = "partial"
        reasons.append(f"equity: {eq_r.get('error')}")

    out = {"orders": orders, "orders_coverage": coverage}
    if reasons:
        out["coverage_reason"] = "; ".join(reasons)
    if warnings:
        out["warnings"] = warnings
    return {"ok": True, "data": out}


def _parse_iso8601(ts):
    """UTC-aware datetime from an ISO 8601 (…T…Z) string, or None on any bad/missing
    input — callers that need to fail loudly on a bad `--since` check the string
    themselves; this helper is used where a silently-skipped row is the right behavior
    (a closed[] entry with no/unparseable closed_ts just isn't a --since match)."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _resolve_default_symbols(since_dt, positions_path=None, fetch_fn=None):
    """SPEC-171 req 1: the union of (a) symbols with an OPEN venue position, (b) symbols
    in config/positions.json `positions[]` (unconditional — an open desk row is always
    relevant) + `closed[]` whose `closed_ts` falls inside the `--since` window. `symbols`
    given explicitly on the CLI bypasses this entirely (see `fills`). Best-effort: a dead
    venue-positions read or an unreadable/malformed positions.json degrades to whatever
    the OTHER source resolved, never raises — the caller decides what an empty union
    means (SPEC-171: loud `fills_no_symbols_resolved`, never a silent empty query)."""
    pos_path = Path(positions_path) if positions_path else POSITIONS_PATH
    try:
        desk = json.loads(pos_path.read_text())
    except (OSError, ValueError):
        desk = {}

    symbols = set()
    for p in desk.get("positions") or []:
        tk = (p.get("ticker") or "").upper()
        if tk:
            symbols.add(f"{tk}USDT")
    for c in desk.get("closed") or []:
        ts_dt = _parse_iso8601(c.get("closed_ts"))
        if ts_dt is None:
            continue
        if since_dt is None or ts_dt >= since_dt:
            tk = (c.get("ticker") or "").upper()
            if tk:
                symbols.add(f"{tk}USDT")

    venue_r = positions(fetch_fn=fetch_fn)
    if venue_r.get("ok"):
        for v in venue_r["data"]:
            if v.get("symbol"):
                symbols.add(v["symbol"])
    return symbols


def fills(since=None, symbols=None, fetch_fn=None, positions_path=None):
    """user trades (price, qty, side, realized pnl, ts) — Aster's userTrades endpoint
    requires `symbol` per call (no all-symbols mode). SPEC-171: `--symbols` omitted no
    longer means "no fills" — it means resolve the query set from live venue positions +
    config/positions.json (`_resolve_default_symbols`) and iterate per symbol. The ONLY
    silent-failure risk this closes: `fills` used to return `{"ok": true, "data": []}`
    whenever no symbol was given, which read as "no fills" for a week in which five
    trades actually closed. Now an empty UNION is a loud `fills_no_symbols_resolved`;
    a resolved-but-genuinely-empty result carries `symbols_queried` so it's auditable."""
    start_ms = None
    since_dt = None
    if since:
        since_dt = _parse_iso8601(since)
        if since_dt is None:
            return {"ok": False, "error": f"bad --since {since!r}, expected ISO 8601 (…T…Z)"}
        start_ms = int(since_dt.timestamp() * 1000)

    if symbols:
        symbols_queried = sorted({s.upper() for s in symbols})
    else:
        resolved = _resolve_default_symbols(since_dt, positions_path=positions_path, fetch_fn=fetch_fn)
        if not resolved:
            return {"ok": False, "error": "fills_no_symbols_resolved",
                    "detail": "no --symbols given and none resolvable from an open venue "
                              "position or config/positions.json positions[]/closed[] "
                              "within --since"}
        symbols_queried = sorted(resolved)

    out, errors = [], {}
    for sym in symbols_queried:
        params = {"symbol": sym}
        if start_ms is not None:
            params["startTime"] = str(start_ms)
        r = _get("user_trades", params=params, fetch_fn=fetch_fn)
        if not r["ok"]:
            errors[sym] = r.get("error")
            continue
        for t in r["data"]:
            out.append({"symbol": t.get("symbol"), "price": float(t["price"]), "qty": float(t["qty"]),
                       "side": t.get("side"), "realized_pnl": float(t.get("realizedPnl") or 0),
                       "ts": t.get("time")})
    result = {"ok": True, "data": out, "symbols_queried": symbols_queried}
    if errors:
        result["errors"] = errors
    return result


def max_leverage(symbols=None, write=False, fetch_fn=None, exchange_fetch_fn=None):
    """{symbol: max_lev} from leverageBracket (a symbolless call returns every symbol
    in one shot). `--write` refreshes config/aster_max_leverage.json for every TRADING
    symbol on exchangeInfo — but ONLY after MAX_LEV_SANITY passes; a mismatch is a
    loud error and nothing is written (a bad/partial read must never corrupt the size
    cap silently)."""
    r = _get("leverage_bracket", fetch_fn=fetch_fn)
    if not r["ok"]:
        return r
    by_symbol = {}
    for row in r["data"]:
        sym = row.get("symbol")
        brackets = row.get("brackets") or []
        levs = [b.get("initialLeverage") for b in brackets if b.get("initialLeverage") is not None]
        by_symbol[sym] = {"max_lev": max(levs) if levs else None, "brackets": brackets}

    if not write:
        if symbols:
            data = {s: by_symbol.get(s, {}).get("max_lev") for s in symbols}
        else:
            data = {s: v["max_lev"] for s, v in by_symbol.items()}
        return {"ok": True, "data": data}

    for sym, expected in MAX_LEV_SANITY.items():
        got = by_symbol.get(sym, {}).get("max_lev")
        if got != expected:
            return {"ok": False, "error": "max_lev_sanity_check_failed",
                    "detail": f"{sym}: expected {expected}, got {got} — refusing to write "
                              f"{MAX_LEV_PATH.name}"}

    ex = _get("exchange_info", fetch_fn=exchange_fetch_fn if exchange_fetch_fn is not None else fetch_fn)
    trading = None
    if ex.get("ok"):
        trading = {s["symbol"] for s in (ex["data"].get("symbols") or [])
                  if s.get("status") == "TRADING"}
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_cfg = {}
    for sym, v in by_symbol.items():
        if trading is not None and sym not in trading:
            continue
        out_cfg[sym] = {"max_lev": v["max_lev"], "brackets": v["brackets"], "ts": ts}
    MAX_LEV_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAX_LEV_PATH.write_text(json.dumps(out_cfg, indent=2))
    return {"ok": True, "data": out_cfg, "written": str(MAX_LEV_PATH)}


def _nearest_stop_and_tp(resting_orders, side):
    """SPEC-171: the venue may hold MULTIPLE resting stops (the user's deliberate
    layering — GALA: 0.00206 primary + 0.00211 backstop) — every one must be persisted,
    sorted nearest-first to the side that actually fires FIRST on an adverse move:
    SHORT (stop above mark) → ascending (lowest fires first); LONG (stop below mark) →
    descending (highest fires first). `sl_set` = the nearest one. `tp_set` mirrors the
    same logic on the FAVOURABLE side (nearest TP that would bank first). Returns
    (resting_stops_sorted, sl_set, tp_set)."""
    stops = [o["price"] for o in resting_orders if o["type"] == "STOP" and o.get("price") is not None]
    tps = [o["price"] for o in resting_orders if o["type"] == "TP" and o.get("price") is not None]
    stop_desc = (side == "LONG")          # LONG stop fires nearest-from-above = highest first
    stops_sorted = sorted(stops, reverse=stop_desc)
    tps_sorted = sorted(tps, reverse=not stop_desc)   # TP nearest is on the opposite side
    sl = stops_sorted[0] if stops_sorted else None
    tp = tps_sorted[0] if tps_sorted else None
    return stops_sorted, sl, tp


def sync(apply=False, positions_path=None, fetch_fn=None):
    """Reconciles config/positions.json `positions[]` against the live venue read.
    Venue-owned fields (VENUE_OWNED_FIELDS) are overwritten from the venue on
    --apply; desk-owned fields (DESK_OWNED_FIELDS) are NEVER touched. A desk row
    with no matching venue position is flagged `closed_on_venue` (not auto-moved —
    the orchestrator records the close with its R); a venue position with no desk
    row is flagged `untracked` (off-desk trade, §9: not recorded). --dry-run (the
    default) computes and returns the diff without writing."""
    pos_path = Path(positions_path) if positions_path else POSITIONS_PATH
    try:
        desk = json.loads(pos_path.read_text())
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"positions.json unreadable: {e}"}
    venue_r = positions(fetch_fn=fetch_fn)
    if not venue_r.get("ok"):
        return venue_r
    venue_by_symbol = {v["symbol"]: v for v in venue_r["data"]}

    desk_positions = desk.get("positions") or []
    desk_by_ticker = {}
    for p in desk_positions:
        tk = (p.get("ticker") or "").upper()
        desk_by_ticker.setdefault(tk, []).append(p)

    updates, closed_on_venue = [], []
    for tk, rows in desk_by_ticker.items():
        sym = f"{tk}USDT"
        v = venue_by_symbol.get(sym)
        if v is None:
            closed_on_venue.append(tk)
            continue
        resting_stops, sl, tp = _nearest_stop_and_tp(v["resting_orders"], v["side"])
        venue_map = {"notional_usd": v["notional_usd"], "margin_usd": v["margin_usd"],
                    "leverage": v["leverage"], "entry": v["entry"], "liq": v["liq_price"],
                    "sl_set": sl, "tp_set": tp, "resting_stops": resting_stops}
        row = rows[0]
        diff = {k: {"from": row.get(k), "to": new_v} for k, new_v in venue_map.items()
               if new_v is not None and new_v != row.get(k)}
        if diff:
            updates.append({"ticker": tk, "diff": diff})

    untracked = [{"ticker": sym[:-4] if sym.endswith("USDT") else sym, **v}
                for sym, v in venue_by_symbol.items()
                if (sym[:-4] if sym.endswith("USDT") else sym) not in desk_by_ticker]

    out = {"ok": True, "data": {"updates": updates, "closed_on_venue": closed_on_venue,
                                "untracked": untracked, "applied": False}}
    if apply and updates:
        for u in updates:
            for p in desk_by_ticker[u["ticker"]]:
                for k, chg in u["diff"].items():
                    p[k] = chg["to"]
        pos_path.write_text(json.dumps(desk, indent=1))
        out["data"]["applied"] = True
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="SPEC-170 — READ-ONLY Aster account capability")
    ap.add_argument("cmd", choices=["equity", "positions", "open_orders", "fills", "max_leverage", "sync"])
    ap.add_argument("--since", default=None, help="fills: ISO 8601 (…T…Z)")
    ap.add_argument("--symbols", default=None, help="comma-separated symbols, e.g. GALAUSDT,BTRUSDT")
    ap.add_argument("--write", action="store_true", help="max_leverage: refresh config/aster_max_leverage.json")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true", help="sync: default")
    ap.add_argument("--apply", action="store_true", help="sync: write the diff to config/positions.json")
    ap.add_argument("--strategy-id", default=None, help="open_orders: known numeric strategyId to probe")
    ap.add_argument("--client-strategy-id", default=None,
                    help="open_orders: known clientStrategyId to probe (mutually exclusive with --strategy-id)")
    ap.add_argument("--strategy-type", default=None, choices=["OTO", "OCO", "OTOCO"],
                    help="open_orders: required alongside --strategy-id/--client-strategy-id")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None

    if args.cmd == "equity":
        out = equity()
    elif args.cmd == "positions":
        out = positions()
    elif args.cmd == "open_orders":
        probes = None
        if args.strategy_id or args.client_strategy_id:
            probe = {"strategyType": args.strategy_type}
            if args.strategy_id:
                probe["strategyId"] = args.strategy_id
            if args.client_strategy_id:
                probe["clientStrategyId"] = args.client_strategy_id
            probes = [probe]
        out = open_orders(strategy_probes=probes)
    elif args.cmd == "fills":
        out = fills(since=args.since, symbols=symbols)
    elif args.cmd == "max_leverage":
        out = max_leverage(symbols=symbols, write=args.write)
    else:
        out = sync(apply=args.apply)

    print(json.dumps(out) if args.json else json.dumps(out, indent=2))
    if not out.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
