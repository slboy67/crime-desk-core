#!/usr/bin/env python3
"""perpfinder.py — SPEC-151: PerpFinder keyless breadth API as a SECOND-SOURCE capability.

Doctrine (§3, non-negotiable — CLAUDE.md §0.6.3 venue-role read, §3 funding-phase rule):
PerpFinder is an aggregator — breadth and second source ONLY. Its funding prints are
1h-NORMALIZED, so placeholder-floor detection (SPEC-19/108) is IMPOSSIBLE on them: they
may NEVER feed verdict-gating funding selection. Venue-native prints (and Velo) stay
authoritative. Every funding row this capability emits carries `normalized:true`.

Live-verified 2026-08-25 (SPEC-158 — the schema drifted from the SPEC-151 build; build
against THIS, not the SPEC-151 addenda where they conflict):
  - Keyless, HTTP 200, no auth headers. Base: https://perpfinder.com/api/data/
  - `funding-rates`: 2253 symbols x 27 venues in one call — the desk's real discovery
    universe. Top-level container is `rows` (NOT `symbols`), each row keyed `symbol` (NOT
    `asset`) with a per-venue map at `exchanges` (NOT `venues`), each venue carrying
    `rate1h`/`oi`/`price` together. `fieldSupport` moved to top-level `meta.fieldSupport`,
    keyed by venue name, covering only `oi`/`price` support (observed|unsupported|
    temporarily_unavailable) — it does NOT cover `rate1h`, so it is surfaced per-row as a
    venue-level lookup, not a per-symbol fact. `nullSemantics` also lives under `meta` now.
  - `open-interest` / `volume` are VENUE-LEVEL aggregates (byExchange / exchanges), not
    per-asset — the funding-rates matrix is the only per-asset x per-venue read.
  - `oi-long-short` is NOW POPULATED (top-level `protocols` list, NOT `rows` — SPEC-158) —
    each protocol row carries longOI/shortOI/totalOI/longPct. Still rendered
    ok:true/rows:[]/meta.empty_reason on the rare call where `protocols` comes back empty,
    but that is no longer the steady state.
  - Any mode whose response body parses but yields zero built rows from a body that isn't
    itself trivially small is treated as SHAPE DRIFT (SPEC-158) — an explicit ok:false with
    reason `shape_drift` and the body's top-level type/first-keys, never a silent empty
    render. This is what should have caught the SPEC-151->SPEC-158 key rename on day one.
  - `slippage` and `funding-history` are MAJORS-ONLY (BTC/ETH/... ) — useless for the
    desk's micro-cap book; kept for completeness, never fed into §7 sizing for desk names.
  - `liquidations` is a real multi-venue liq event stream (sourced from Coinalyze per their
    own docs) — breadth/corroboration next to the OKX-native ground truth (liqs.py,
    SPEC-103), never a replacement for it.
  - No `openapi.json` / `api-manifest` (404) — the endpoint contract is UNVERSIONED but
    self-describing (schemaVersion in the body IS the version signal).
  - No rate-limit headers are exposed; the documented 40/min (funding/oi/volume) and
    30/min (slippage) caps are respected conservatively via the TTL cache below, never by
    polling in a loop.

Attribution ("Data by PerpFinder (perpfinder.com)") is PerpFinder's stated condition for
free use and is stamped on every response's meta.

  python3 capabilities/perpfinder.py funding --ticker HEMI --json
  python3 capabilities/perpfinder.py liqs --json
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import regime_flip as RF   # SPEC-187: the ONE canonical raw->%/4h transform (to_4h)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE = ROOT / "state"
CACHE_PATH = STATE / "perpfinder_cache.json"
CONFIG_PATH = ROOT / "config" / "perpfinder.json"

BASE_URL = "https://perpfinder.com/api/data/"
UA = {"User-Agent": "crime-desk-perpfinder/1.0"}
EXPECTED_SCHEMA_VERSION = 1
ATTRIBUTION = "Data by PerpFinder (perpfinder.com)"

# mode -> endpoint path (under BASE_URL)
MODES = {
    "funding": "funding-rates",
    "oi": "open-interest",
    "oi_long_short": "oi-long-short",
    "volume": "volume",
    "liqs": "liquidations",
    "slippage": "slippage",
    "funding_history": "funding-history",
}

DEFAULT_CFG = {"cache_ttl_s": 120}

# SPEC-158: a body whose serialized size is at/under this is plausibly a genuine small/empty
# payload (e.g. oi-long-short's occasional zero-protocols call); above it, zero built rows
# from a real ~KB+ body is a parser/key mismatch, never "no data" — see shape-drift guard.
SHAPE_DRIFT_BODY_BYTES = 1000


class PerpFinderError(Exception):
    """A real PerpFinder failure (HTTP/timeout/malformed body/429-after-retry) — the
    caller renders ok:false with the reason, never a partial render (§3)."""


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        d = json.loads(CONFIG_PATH.read_text())
        cfg.update({k: v for k, v in d.items() if k in DEFAULT_CFG})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def _cache_load():
    try:
        return json.loads(CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _cache_save(cache):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache))
        tmp.replace(CACHE_PATH)
    except OSError:  # noqa: BLE001 — a cache-write failure must never block a read
        pass


def _cache_key(endpoint, params):
    return endpoint + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))


def _http_get(url, timeout=15):
    """One live GET -> parsed JSON body (separate so tests patch the wire, not the retry/
    cache logic, same pattern as onchain.py's _etherscan_call)."""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get_with_retry(url, retries=1):
    """§3 + req 3: a 429 gets exactly ONE retry honoring Retry-After (capped, so a bad/huge
    header can never hang a run), then errors out loudly. Any other HTTP error, timeout, or
    malformed JSON body is a loud, immediate failure — never a silent retry loop."""
    attempt = 0
    while True:
        try:
            return _http_get(url)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                wait = 0.0
                try:
                    wait = float(e.headers.get("Retry-After", 0)) if e.headers else 0.0
                except (TypeError, ValueError):
                    wait = 0.0
                time.sleep(min(max(wait, 0.0), 5.0))
                attempt += 1
                continue
            raise PerpFinderError(f"perpfinder HTTP {e.code}: {str(e)[:160]}") from e
        except Exception as e:  # noqa: BLE001 — URLError/timeout/bad JSON
            raise PerpFinderError(f"perpfinder unreachable: {str(e)[:160]}") from e


def fetch(mode, params=None, use_cache=True, now=None, cfg=None):
    """mode -> raw PerpFinder JSON body. Cached (state/perpfinder_cache.json) keyed on
    mode+params with a `cache_ttl_s` TTL (config/perpfinder.json, default 120s) — rate-
    limit citizenship (req 3): a repeat call inside the TTL never touches the wire."""
    if mode not in MODES:
        raise PerpFinderError(f"unknown perpfinder mode {mode!r}")
    cfg = cfg or load_cfg()
    now = now if now is not None else time.time()
    params = params or {}
    endpoint = MODES[mode]
    ckey = _cache_key(endpoint, params)
    cache = _cache_load() if use_cache else {}
    hit = cache.get(ckey)
    if use_cache and hit and (now - hit.get("ts", 0)) < cfg["cache_ttl_s"]:
        return hit["body"]
    url = BASE_URL + endpoint
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    body = _get_with_retry(url)
    if use_cache:
        cache[ckey] = {"ts": now, "body": body}
        _cache_save(cache)
    return body


# ── per-mode normalizers — the wire shape genuinely differs per endpoint (ADDENDUM 2), a
# uniform `rows: [...]` envelope does NOT hold across all seven. Each returns
# (shape_dict, venues_covered_or_None, raw_universe_count) — the third element is the size
# of the correctly-keyed top-level container BEFORE any ticker/venue filtering, used by
# build_perpfinder's shape-drift guard (SPEC-158) so a ticker that legitimately matches
# nothing is never confused with a renamed/restructured top-level key. ───────────────────
def _build_funding(body, ticker=None, venues=None):
    """funding-rates: asset x venue matrix, rate1h (1h-normalized RAW FRACTION, same
    fraction convention as every venue-direct fundingRate print in this codebase —
    Binance/Bybit/Bitget all report a fraction, e.g. -0.003 == -0.3%) -> funding_pi_4h.

    SPEC-187 fix: funding_pi_4h used to be a bare `rate1h * 4`, which skips the
    fraction->percent step every OTHER funding read in the codebase applies
    (regime_flip.py: `funding_pi = funding_raw * 100` BEFORE `to_4h`) — every
    venue_breadth print was ~100x too small (live: brief showed Bybit rate1h -0.008838
    against the funding layer's own -1.026%/4h for the same venue/moment). Now goes
    through the SAME two-step transform regime_flip uses everywhere else:
    `funding_pi = rate1h * 100`, then `RF.to_4h(funding_pi, interval_min)` — rate1h
    really is hour-normalized (PerpFinder's documented convention, see module
    docstring) so interval_min is fixed at 60 here, making `to_4h` a plain x4 on the
    percent value, not a bespoke literal.

    SPEC-187: `rate_raw_pi` (the untouched raw fraction) + `interval_min` are now
    surfaced per row too — the old "deliberately no raw_pi key" doctrine hid exactly
    the data needed to debug/reconcile a units mismatch; the field is named
    `rate_raw_pi` (not `raw_pi`) so nothing downstream mistakes it for a
    verdict-relevant read (§3 — display/reconciliation only, PerpFinder funding still
    NEVER feeds a veto/floor/funding_leg, doctrine unchanged).

    SPEC-158: top-level container is `rows` (was `symbols`), each keyed `symbol` (was
    `asset`) with `exchanges` (was `venues`); per-venue fieldSupport now lives at
    `meta.fieldSupport[venue]` (oi/price only, never rate1h) — looked up per row, not
    invented. oi/price are now surfaced per venue alongside funding_pi_4h (req 3).
    SPEC-174 #4: `oi` on THIS endpoint is already USD notional, not base-asset quantity —
    live-verified 2026-08-28 (BTC/Binance oi=$8.396B; as token units that's 8.4B BTC,
    impossible against a ~19.8M supply). Never re-multiply it by `price` (the venue_breadth
    bug: total_oi_usd double-counted price and corrupted every total)."""
    raw = body.get("rows") or []
    field_support_by_venue = ((body.get("meta") or {}).get("fieldSupport")) or {}
    rows = []
    for sym in raw:
        asset = sym.get("symbol")
        if ticker and (asset or "").upper() != ticker.upper():
            continue
        for venue, vobj in (sym.get("exchanges") or {}).items():
            if venues and venue not in venues:
                continue
            vobj = vobj or {}
            rate1h = vobj.get("rate1h")
            funding_pi = None if rate1h is None else round(rate1h * 100, 6)
            rows.append({
                "asset": asset, "venue": venue,
                "funding_pi_4h": None if funding_pi is None else RF.to_4h(funding_pi, 60),
                "rate_raw_pi": rate1h, "interval_min": 60,
                "normalized": True,
                "oi": vobj.get("oi"),
                "price": vobj.get("price"),
                "field_support": field_support_by_venue.get(venue),
            })
    return {"rows": rows}, len({r["venue"] for r in rows}), len(raw)


def _build_oi(body):
    """open-interest: VENUE-LEVEL aggregate (byExchange), NOT per-asset (ADDENDUM 2) — null
    OI stays null (SPEC-129 rule 4), never coerced to 0."""
    raw = body.get("byExchange") or []
    by_exchange = [{"name": ex.get("name"), "oi": ex.get("oi")} for ex in raw]
    return {"byExchange": by_exchange, "total_oi": body.get("totalOI")}, len(by_exchange), len(raw)


def _build_oi_long_short(body, ticker=None, venues=None):
    """oi-long-short: SPEC-158 — top-level container is `protocols` (was documented as
    `rows`/always-empty in SPEC-151; live-verified 2026-08-25 it is now populated with
    longOI/shortOI/totalOI/longPct per protocol). Still renders ok:true/rows:[]/empty_reason
    on the rare call where `protocols` itself comes back empty.

    SPEC-174 #2: this is a DEX-PROTOCOL aggregate table (slug/name = e.g. "gmx"/"GMX"), NOT
    a per-token-ticker matrix — there is no symbol field to filter on. Before this fix
    `ticker` was silently discarded and every caller got the full unfiltered protocol table
    regardless of what they asked for. Now: `ticker` matches a protocol whose slug/name
    equals it case-insensitively (real for protocol-native tokens like GMX/DYDX/HYPE); any
    other ticker has no protocol-level row by construction and the result is an explicit
    `not_listed:true`, never the full table."""
    protocols = body.get("protocols") or []
    rows = [{"slug": p.get("slug"), "name": p.get("name"), "long_oi": p.get("longOI"),
            "short_oi": p.get("shortOI"), "total_oi": p.get("totalOI"),
            "long_pct": p.get("longPct")} for p in protocols]
    if ticker:
        t = ticker.upper()
        rows = [r for r in rows
               if (r.get("slug") or "").upper() == t or (r.get("name") or "").upper() == t]
    out = {"rows": rows, "total_long": body.get("totalLong"), "total_short": body.get("totalShort")}
    if not rows:
        if ticker:
            out["not_listed"] = True
            out["empty_reason"] = (f"PerpFinder /oi-long-short has no protocol matching "
                                   f"ticker {ticker!r} — this endpoint is a DEX-protocol "
                                   "aggregate table, not a per-asset matrix")
        else:
            out["empty_reason"] = ("PerpFinder /oi-long-short returned zero protocols this call "
                                   "— rendered as legitimately empty, not fabricated")
    return out, None, len(protocols)


def _build_volume(body):
    """volume: VENUE-LEVEL (exchanges), each with its own topSymbols — a liquidity-ranking
    read, not a per-name one (ADDENDUM 2)."""
    raw = body.get("exchanges") or []
    exchanges = [{"name": ex.get("name"), "volume24h": ex.get("volume24h"),
                 "symbol_count": ex.get("symbolCount"), "top_symbols": ex.get("topSymbols")}
                for ex in raw]
    return {"exchanges": exchanges}, len(exchanges), len(raw)


def _build_liqs(body, ticker=None, venues=None):
    """liquidations: actual multi-venue liq event stream (sourced from Coinalyze per their
    own docs — OKX-native, SPEC-103, remains ground truth; this is breadth/corroboration)."""
    raw = body.get("events") or []
    events = []
    for e in raw:
        if ticker and str(e.get("symbol") or "").upper() != ticker.upper():
            continue
        if venues and e.get("exchange") not in venues:
            continue
        events.append({"exchange": e.get("exchange"), "symbol": e.get("symbol"),
                       "side": e.get("side"), "size_usd": e.get("sizeUsd"),
                       "price": e.get("price"), "timestamp": e.get("timestamp")})
    return {"events": events}, None, len(raw)


def _build_slippage(body):
    """slippage: MAJORS-ONLY (BTC/ETH/SOL/XRP/BNB/DOGE/HYPE, live-verified) execution-cost
    ladder, sorted by totalBps ascending — never presented as a micro-cap size-to-exit
    input (the live-ladder `depth` read stays the only one for the desk's names)."""
    raw = body.get("rows") or body.get("venues") or []
    rows = [{"venue": r.get("venue") or r.get("name"), "total_bps": r.get("totalBps"),
            "vwap": r.get("vwap"), "spread": r.get("spread")}
           for r in raw]
    rows.sort(key=lambda r: (r["total_bps"] is None, r["total_bps"]))
    return {"rows": rows}, None, len(raw)


def _build_funding_history(body):
    """funding-history: BTC/ETH ONLY (live-verified) — passthrough of their self-collected,
    no-synthetic-backfill ledger; never a per-name history source for the desk's book
    (regime_check's own funding history stays that)."""
    raw = body.get("history") or body.get("rows") or []
    return {"dataset_start": body.get("datasetStart"), "maturity": body.get("maturity"),
           "history": raw}, None, len(raw)


_BUILDERS = {
    "funding": _build_funding,
    "oi": lambda body, **_: _build_oi(body),
    "oi_long_short": _build_oi_long_short,
    "volume": lambda body, **_: _build_volume(body),
    "liqs": _build_liqs,
    "slippage": lambda body, **_: _build_slippage(body),
    "funding_history": lambda body, **_: _build_funding_history(body),
}


def build_perpfinder(mode, ticker=None, venues=None, size=None, side=None, days=None,
                     use_cache=True, now=None):
    """The one entrypoint every mode goes through. Unknown mode / missing required args =
    LOUD fail (SPEC-120 addendum convention: ok:false, never a silent empty render).
    Returns {ok, mode, <shape-key(s)>, venues_covered?, meta} on success, {ok:false, mode,
    reason} on failure — never a partial render on a failure (§3)."""
    if mode not in MODES:
        return {"ok": False, "mode": mode, "reason": f"unknown perpfinder mode {mode!r}"}
    params = {}
    if mode == "slippage":
        if not (ticker and size and side):
            return {"ok": False, "mode": mode,
                    "reason": "slippage requires ticker, size and side"}
        params = {"asset": ticker.upper(), "size": size, "feeType": "taker", "side": side}
    elif mode == "funding_history":
        params = {"asset": (ticker or "BTC").upper(), "days": days or 30}

    try:
        body = fetch(mode, params=params, use_cache=use_cache, now=now)
    except PerpFinderError as e:
        return {"ok": False, "mode": mode, "reason": str(e)}

    if not isinstance(body, dict):
        return {"ok": False, "mode": mode, "reason": "malformed body (not a JSON object)"}
    if "error" in body:
        return {"ok": False, "mode": mode, "reason": str(body["error"])}

    shape, venues_covered, raw_count = _BUILDERS[mode](body, ticker=ticker, venues=venues)

    if raw_count == 0:
        try:
            body_size = len(json.dumps(body))
        except (TypeError, ValueError):
            body_size = 0
        if body_size > SHAPE_DRIFT_BODY_BYTES:
            first_keys = list(body.keys())[:10]
            return {"ok": False, "mode": mode, "reason": "shape_drift",
                    "shape_drift": {"body_type": type(body).__name__, "first_keys": first_keys,
                                    "body_bytes": body_size,
                                    "detail": (f"perpfinder {mode!r}: response parsed as a "
                                              f"{body_size}-byte body but yielded zero rows from "
                                              "the expected top-level container — treat as a "
                                              "parser/fixture mismatch, never as empty data")}}

    meta = {"source": "perpfinder", "updatedAt": body.get("updatedAt"),
           "dataStatus": body.get("dataStatus"), "schemaVersion": body.get("schemaVersion"),
           "attribution": ATTRIBUTION}
    if mode == "funding":
        meta["normalized"] = True
    sv = body.get("schemaVersion")
    if sv is not None and sv != EXPECTED_SCHEMA_VERSION:
        meta["warning"] = (f"schemaVersion drift: expected {EXPECTED_SCHEMA_VERSION}, got {sv!r} "
                           "— rendered anyway, verify the shape assumptions above still hold")

    out = {"ok": True, "mode": mode, "meta": meta}
    out.update(shape)
    if venues_covered is not None:
        out["venues_covered"] = venues_covered
    return out


def main():
    ap = argparse.ArgumentParser(description="SPEC-151 perpfinder — keyless multi-venue "
                                             "breadth API, second-source only")
    # SPEC-174 #2: nargs="?"+default so `perpfinder '{}'` (no mode) doesn't argparse-error —
    # "funding" (the flagship 2205x27 matrix, per the module docstring's own first example)
    # is the sensible default rather than requiring every caller to know a mode name.
    ap.add_argument("mode", nargs="?", default="funding", choices=list(MODES))
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--venues", default=None,
                    help='JSON array string, e.g. \'["binance","bybit"]\' — matches the '
                         'orchestrator\'s list-arg encoding (fill() sval)')
    ap.add_argument("--size", type=float, default=None)
    ap.add_argument("--side", choices=["buy", "sell"], default=None)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    venues = json.loads(args.venues) if args.venues else None
    r = build_perpfinder(args.mode, ticker=args.ticker, venues=venues, size=args.size,
                         side=args.side, days=args.days)
    out = {"ok": bool(r.get("ok")), "data": r, "meta": {}}
    if args.json:
        print(json.dumps(out, default=str))
    else:
        if not r["ok"]:
            print(f"# perpfinder {args.mode} — unavailable ({r.get('reason')})")
            return
        print(f"# perpfinder {args.mode}  (attribution: {r['meta']['attribution']})")
        print(json.dumps({k: v for k, v in r.items() if k not in ("ok", "mode")}, default=str)[:2000])


if __name__ == "__main__":
    main()
