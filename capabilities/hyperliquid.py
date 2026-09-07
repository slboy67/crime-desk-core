#!/usr/bin/env python3
"""hyperliquid.py — Hyperliquid read-only venue resolver (SPEC-84).

The desk is moving to self-custody (EU/MiCA). Hyperliquid is the cleaner EXECUTION venue for
the watchlist names it lists — deep on-chain liquidity, fully transparent funding/OI/liquidations,
and ORACLE-based marks (less composite-mark/wick risk than the operator-suspect Aster/Bitget books,
cf. memory feedback_operator_venue_liquidity_is_suspect). Coverage is sparse BY DESIGN (empirically
2/26 watchlist names: TNSR, CHIP — HL universe ≈ 230 perps), so this is a TARGETED add for the
overlap, not a universe expansion — and ABSENCE is the graceful default, never an error.

Read-only INTEL — an additive cross-check source. NO verdict logic lives here; the engine's
existing per-interval normalization + §5 veto are unchanged. HL funding settles HOURLY → the /4h
display = hourly × 4 (interval_min:60), the same way aster's 1h rate is handled.

Public API (no key): POST https://api.hyperliquid.xyz/info
  {"type":"meta"}             → {"universe":[{"name":...}, …]}   (the listed-perp set)
  {"type":"metaAndAssetCtxs"} → [meta, [ctx, …]]  ctx parallel to universe; each ctx has
                                funding (HOURLY), openInterest, markPx, oraclePx, premium, dayNtlVlm
  {"type":"l2Book","coin":C}  → {"levels":[bids, asks]}  (depth; each level {px,sz,n})
"""
import json
import urllib.request
import urllib.error

INFO_URL = "https://api.hyperliquid.xyz/info"
TIMEOUT = 10
INTERVAL_MIN = 60          # HL funding settles HOURLY (the /4h display = hourly × 4)

# Ticker → HL coin `name`. IDENTITY for most names; add an entry only where they differ.
ALIAS = {
    # "DESKTKR": "HLNAME",
}


def is_floor(raw_hourly):
    """HL reports a REAL funding rate every hour — it has NO placeholder/floor sentinel like the
    CEX 0.005% (0.00005 raw) clamp. So a small HL hourly rate is a GENUINE reading, not a floor;
    only an exact-zero rate is 'no funding'. This guards against the binance/bybit FLOOR_RAW
    misfiring on HL's native per-hour scale, where 0.00005/hr is a real ±0.02%/4h rate."""
    return raw_hourly is None or abs(raw_hourly) < 1e-12


def coin_for(ticker):
    """Ticker → HL coin name (USDT suffix stripped, alias applied)."""
    t = ticker.upper().replace("USDT", "")
    return ALIAS.get(t, t)


def funding_4h(funding_pi):
    """HL hourly %-rate → %/4h (×4). funding_pi is the per-hour rate in percent."""
    return None if funding_pi is None else round(funding_pi * 4.0, 4)


def post(payload):
    """POST a JSON body to the HL info endpoint. None on ANY failure (timeout / non-200 / parse)
    → the caller OMITS HL, never blocks the brief/classify/scan (the SPEC-80 fail-safe pattern)."""
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            INFO_URL, data=data,
            headers={"Content-Type": "application/json", "User-Agent": "hyperliquid/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if getattr(r, "status", 200) != 200:
                return None
            return json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None


def universe(meta=None):
    """Set of HL-listed coin names. `meta` optional (injectable for tests / reuse)."""
    if meta is None:
        meta = post({"type": "meta"})
    try:
        return {u["name"] for u in meta["universe"] if isinstance(u, dict) and u.get("name")}
    except (TypeError, KeyError):
        return set()


def _ctx_block(name, meta, ctxs):
    """Per-venue read for coin `name` from a (meta, ctxs) pair. None if `name` is not in the
    universe (graceful absence) or the ctx is malformed."""
    try:
        names = [u["name"] for u in meta["universe"]]
    except (TypeError, KeyError):
        return None
    if name not in names:
        return None
    idx = names.index(name)
    try:
        ctx = ctxs[idx]
    except (IndexError, TypeError):
        return None
    if not isinstance(ctx, dict):
        return None

    def _f(key):
        v = ctx.get(key)
        try:
            return float(v) if v not in (None, "") else None
        except (ValueError, TypeError):
            return None

    funding_raw = _f("funding")          # HOURLY decimal rate
    funding_pi = round(funding_raw * 100, 6) if funding_raw is not None else None
    mark, oracle, premium = _f("markPx"), _f("oraclePx"), _f("premium")
    oi_base = _f("openInterest")         # in base coin units
    vol = _f("dayNtlVlm")                # 24h notional (USD)
    oi_usd = round(oi_base * mark, 2) if (oi_base is not None and mark is not None) else None
    return {
        "venue": "hyperliquid", "coin": name,
        "funding_raw": funding_raw, "funding_pi": funding_pi,
        "interval_min": INTERVAL_MIN, "funding_4h": funding_4h(funding_pi),
        "is_floor": is_floor(funding_raw),
        "mark": mark, "oracle": oracle, "premium": premium, "price": mark,
        "oi": oi_usd, "oi_base": oi_base,
        "vol_m": (round(vol / 1e6, 4) if vol is not None else None),
    }


def resolve(ticker, meta_ctxs=None):
    """Ticker → HL venue read, or None if not listed / fetch failed (graceful absence — never
    raises, never poisons the caller with a null block)."""
    name = coin_for(ticker)
    if meta_ctxs is None:
        meta_ctxs = post({"type": "metaAndAssetCtxs"})
    if not (isinstance(meta_ctxs, list) and len(meta_ctxs) == 2):
        return None
    meta, ctxs = meta_ctxs
    if not (isinstance(meta, dict) and isinstance(ctxs, list)):
        return None
    return _ctx_block(name, meta, ctxs)


def l2_book(ticker, raw=None):
    """HL l2 order book → (bids, asks) as [(price, size_base), …] (bids desc, asks asc), or
    (None, None) if not listed / empty / malformed. `raw` injectable for tests."""
    name = coin_for(ticker)
    if raw is None:
        raw = post({"type": "l2Book", "coin": name})
    try:
        bids_raw, asks_raw = raw["levels"]
    except (TypeError, KeyError, ValueError):
        return None, None

    def _parse(side):
        out = []
        for lv in side or []:
            try:
                out.append((float(lv["px"]), float(lv["sz"])))
            except (KeyError, ValueError, TypeError):
                continue
        return out

    bids, asks = _parse(bids_raw), _parse(asks_raw)
    if not bids or not asks:
        return None, None
    return bids, asks
