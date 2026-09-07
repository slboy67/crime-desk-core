#!/usr/bin/env python3
"""aster_listing.py — SPEC-136: is this ticker's perp actually listed on Aster (the
desk's sole execution venue, CLAUDE.md §7)? The cross-venue layer (venue_map,
regime_flip) answers where the OI/funding SIGNAL lives; this answers a narrower
question the SIGNAL layer must never be conflated with — can the user FILL here.

COTI printed a clean §4 cross-venue regime-flip and got committed with a real trigger
— but Aster carries no COTI market, so the trade never existed for this user. The whole
board had to be hand-audited after the fact; nothing in the engine caught it.

One live call per run (`fapi/v1/exchangeInfo` lists every symbol Aster lists), disk-
cached with a 6h TTL — the perp list churns slowly, no reason to re-fetch every board
tick. Same cache-in-state/ pattern as onchain.py's `probe_contract` (SPEC-67).

§3 doctrine: a failed/empty fetch (network blip, cache miss) resolves to `None`
(unknown) — NEVER `False`. A `False` verdict must only ever mean "Aster answered and
confirmed no market," never "we couldn't check." Treating a transient failure as False
would silently suppress every page on the board.
"""
import json
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / "state"
CACHE_PATH = STATE / "aster_symbols_cache.json"
CACHE_TTL_S = 6 * 3600
EXCHANGE_INFO_URL = "https://fapi.asterdex.com/fapi/v1/exchangeInfo"


def _fetch_live(timeout=10):
    """Raw exchangeInfo fetch → set of symbols, or None on any failure/malformed body.
    Catches broadly (not just urllib's own exception types) — a dead venue must never
    propagate past this into classify's board loop and take the whole run down."""
    try:
        req = urllib.request.Request(EXCHANGE_INFO_URL, headers={"User-Agent": "crime-desk"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        symbols = {s["symbol"] for s in (data.get("symbols") or []) if s.get("symbol")}
        return symbols or None
    except Exception:  # noqa: BLE001 — any live-fetch failure degrades to unknown, never raises
        return None


def _load_cache_raw():
    try:
        return json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        return None


def _load_fresh_cache(ttl_s=CACHE_TTL_S, now=None):
    d = _load_cache_raw()
    if d is None:
        return None
    now = now if now is not None else time.time()
    if now - d.get("ts", 0) >= ttl_s:
        return None
    symbols = d.get("symbols") or []
    return set(symbols) if symbols else None


def _save_cache(symbols, now=None):
    now = now if now is not None else time.time()
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": now, "symbols": sorted(symbols)}))
        tmp.replace(CACHE_PATH)
    except OSError:  # noqa: BLE001 — a cache-write failure must never block a verdict
        pass


def fetch_aster_symbols(use_cache=True, ttl_s=CACHE_TTL_S, now=None, fetch_fn=None):
    """The set of USDT-margined perp symbols Aster lists ("COTIUSDT", ...). Returns
    None only when there is no way to answer — live fetch failed AND no cache (fresh
    or stale) exists. A live failure falls back to a STALE cache rather than None
    (a 6h-old listing is still far more informative than "unknown"); only a genuinely
    empty cache + failed fetch degrades to None."""
    if use_cache:
        fresh = _load_fresh_cache(ttl_s=ttl_s, now=now)
        if fresh is not None:
            return fresh
    symbols = (fetch_fn or _fetch_live)()
    if symbols:
        if use_cache:
            _save_cache(symbols, now=now)
        return symbols
    # live fetch failed — fall back to a stale cache before giving up entirely
    if use_cache:
        d = _load_cache_raw()
        if d and d.get("symbols"):
            return set(d["symbols"])
    return None


def aster_listed(ticker, symbols):
    """True/False/None given a pre-fetched symbol set. `symbols is None` (unknown) or
    no ticker → None. Never guesses."""
    if symbols is None or not ticker:
        return None
    return f"{ticker.upper()}USDT" in symbols


if __name__ == "__main__":
    import sys
    tk = sys.argv[1].upper() if len(sys.argv) > 1 else None
    syms = fetch_aster_symbols()
    print(json.dumps({"ticker": tk, "aster_listed": aster_listed(tk, syms) if tk else None,
                      "symbols_known": len(syms) if syms else 0}))
