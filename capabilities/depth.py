#!/usr/bin/env python3
"""depth.py — live order-book shelf read (SPEC 27).

The resting-book shelf (the operator's defensive bid / the ask wall) was 100% hand-curled — the
decisive TP anchor on SKYAI was the Bitget bid at $0.156. `liq_magnets` is a volume-profile PROXY,
not the live book. This exposes the live resting shelf so the orchestrator stops hand-curling it.

Read-only INTEL — participates in NO auto-verdict; it just surfaces the book.

  python3 capabilities/depth.py SKYAI --json                 # all venues — Aster (execution) first
  python3 capabilities/depth.py SKYAI --venue aster --json
  python3 capabilities/depth.py SKYAI --venue bitget --json

Truncation caveat (the live lesson, memory feedback_orderbook_api_truncation_defer_to_live_dom):
merge-depth caps at ~100 levels and on thin names they bunch within ~1% of mid. When the returned
book doesn't reach the requested band, we mark `truncated:true` + `deepest_level_seen` and DO NOT
report "empty below" as "no liquidity" — the trader's live DOM sees beyond the window.
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path
from urllib.error import URLError, HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))   # SPEC-84: sibling-capability imports

WANT_PCT = 5.0          # we want the book out to ±5% of mid
BAND_PCT = 0.5          # bucket width for shelf detection
ROUND_TOL_PCT = 0.4     # a shelf within this % of a round number = higher-confidence magnet
UA = {"User-Agent": "depth/1.0"}


def fetch(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r:
            return json.loads(r.read())
    except (URLError, HTTPError, json.JSONDecodeError, TimeoutError, ValueError) as e:
        return {"_error": str(e)[:120]}


# ---- per-venue book pulls → normalized [(price, size_base), …] (bids desc, asks asc) ----
def _bitget_book(s):
    d = fetch(f"https://api.bitget.com/api/v2/mix/market/merge-depth?symbol={s}"
              f"&productType=usdt-futures&precision=scale0&limit=max")
    data = d.get("data") if isinstance(d, dict) else None
    if not data:
        return None, None, (d.get("_error") if isinstance(d, dict) else "no data")
    try:
        bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
    except (ValueError, TypeError):
        return None, None, "unparseable"
    return bids, asks, None


def _binance_book(s):
    d = fetch(f"https://fapi.binance.com/fapi/v1/depth?symbol={s}&limit=1000")
    if not isinstance(d, dict) or "bids" not in d:
        return None, None, (d.get("_error") if isinstance(d, dict) else "no data")
    try:
        bids = [(float(p), float(q)) for p, q in d.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in d.get("asks", [])]
    except (ValueError, TypeError):
        return None, None, "unparseable"
    return bids, asks, None


def _aster_book(s):
    # Aster's REST is Binance-shaped (fapi.asterdex.com) — same parse/contract as _binance_book.
    # The desk FILLS here, so this is the primary TP/exit-shelf book (NOT the liq reference — liq
    # fires off the oracle aggregate, not Aster's DOM; SPEC-85 note / §7). Read-only ingestion.
    d = fetch(f"https://fapi.asterdex.com/fapi/v1/depth?symbol={s}&limit=1000")
    if not isinstance(d, dict) or "bids" not in d:
        return None, None, (d.get("_error") if isinstance(d, dict) else "no data")
    try:
        bids = [(float(p), float(q)) for p, q in d.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in d.get("asks", [])]
    except (ValueError, TypeError):
        return None, None, "unparseable"
    return bids, asks, None


# Aster FIRST — it's the execution venue, so its book is the primary TP/exit/defended-fade anchor
# (build_depth default + size BOOK_FETCHERS + defended_fade all iterate this order). Bitget+Binance
# stay as the cross-venue operator-suspect compare (additive, not a replacement — §0.6).
VENUES = {"aster": _aster_book, "bitget": _bitget_book, "binance": _binance_book}


def _near_round(price):
    """Is `price` within ROUND_TOL_PCT of a 'round' number (1/2/2.5/5 × 10^k)? Higher-confidence magnet."""
    if price <= 0:
        return None
    import math
    mag = 10 ** math.floor(math.log10(price))
    for m in (1, 2, 2.5, 5, 10):
        rn = m * mag
        if abs(price - rn) / price * 100 <= ROUND_TOL_PCT:
            return round(rn, 10)
    return None


def _shelf(levels, mid, side):
    """Largest resting shelf on one side: bucket notional into BAND_PCT bands, return the band with
    the most $ — its representative (volume-weighted) price + total notional + level count."""
    if not levels:
        return None
    bands = {}
    for price, size in levels:
        dist = abs(price - mid) / mid * 100
        b = int(dist / BAND_PCT)
        notl = price * size
        e = bands.setdefault(b, {"notl": 0.0, "px_notl": 0.0, "n": 0})
        e["notl"] += notl
        e["px_notl"] += price * notl
        e["n"] += 1
    top = max(bands.values(), key=lambda x: x["notl"])
    wap = top["px_notl"] / top["notl"] if top["notl"] else mid
    return {"price": round(wap, 8), "notional_usd": round(top["notl"], 2), "levels": top["n"],
            "dist_pct": round((wap - mid) / mid * 100, 3),
            "round_number": _near_round(wap), "side": side}


def _venue_depth(name, bids, asks):
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    deepest_bid, deepest_ask = min(p for p, _ in bids), max(p for p, _ in asks)
    cover_below = (mid - deepest_bid) / mid * 100
    cover_above = (deepest_ask - mid) / mid * 100
    # truncation: the book didn't reach the band we asked for, and it's at the level cap → defer to DOM
    truncated = cover_below < WANT_PCT or cover_above < WANT_PCT
    bid_shelf = _shelf(bids, mid, "bid")
    ask_wall = _shelf(asks, mid, "ask")
    # spoof-prone = a lone wall NOT coinciding with a round number (the §7 spoof tell)
    for sh in (bid_shelf, ask_wall):
        if sh:
            sh["spoof_prone"] = sh["round_number"] is None and sh["levels"] <= 2
    return {
        "venue": name, "mid": round(mid, 8), "best_bid": best_bid, "best_ask": best_ask,
        "spread_pct": round((best_ask - best_bid) / mid * 100, 4),
        "bid_shelf_below": bid_shelf, "ask_wall_above": ask_wall,
        "n_levels": len(bids), "coverage_below_pct": round(cover_below, 3),
        "coverage_above_pct": round(cover_above, 3),
        "truncated": truncated, "deepest_level_seen": {"bid": deepest_bid, "ask": deepest_ask},
        "truncation_note": (f"book caps at {len(bids)} levels bunched within ±{max(cover_below, cover_above):.2f}% "
                            f"of mid — DO NOT read 'empty beyond' as 'no liquidity'; the live DOM sees deeper"
                            if truncated else None),
    }


HL_VENUE = "hyperliquid"   # SPEC-84: read-only HL book — additive, absence-safe


def _fetch_hl_book(ticker):
    """SPEC-84: fetch + parse the HL l2 book → (bids, asks) or (None, None). Patchable in tests."""
    import hyperliquid as HL
    return HL.l2_book(ticker)


def _hyperliquid_depth(ticker):
    """SPEC-84: HL book → normalized venue depth, or None if HL doesn't list the name / the fetch
    failed. None → the venue is OMITTED from books.venues (graceful absence; never a null/error entry,
    so the bitget/binance reads stay byte-identical for the 24/26 names HL doesn't carry)."""
    try:
        bids, asks = _fetch_hl_book(ticker)
        if not bids or not asks:
            return None
        return {"available": True, **_venue_depth(HL_VENUE, bids, asks)}
    except Exception:  # noqa: BLE001
        return None


def build_depth(ticker, venue=None):
    """Live resting-book shelf per venue. Read-only intel; never an auto-verdict input."""
    sym = ticker.upper().replace("USDT", "") + "USDT"
    # SPEC-84: HL is additive — it never enters the existing CEX loop (those reads stay byte-identical);
    # it is appended ONLY on a successful book, and omitted entirely on absence/error.
    want_hl = venue is None or venue.lower() == HL_VENUE
    names = [] if (venue and venue.lower() == HL_VENUE) else ([venue.lower()] if venue else list(VENUES))
    venues = {}
    for name in names:
        fn = VENUES.get(name)
        if not fn:
            venues[name] = {"available": False, "reason": f"unknown venue (have {list(VENUES)})"}
            continue
        bids, asks, err = fn(sym)
        if not bids or not asks:
            venues[name] = {"available": False, "reason": err or "empty book"}
            continue
        venues[name] = {"available": True, **_venue_depth(name, bids, asks)}
    if want_hl:
        hl = _hyperliquid_depth(ticker)
        if hl:
            venues[HL_VENUE] = hl
    return {"ticker": sym.replace("USDT", ""), "venues": venues,
            "note": "read-only book intel (SPEC 27) — not an auto-verdict input"}


def render_human(d):
    print(f"# {d['ticker']} — live order-book depth (read-only intel)\n")
    for name, v in d["venues"].items():
        if not v.get("available"):
            print(f"## {name.title()}: unavailable ({v.get('reason')})\n")
            continue
        print(f"## {name.title()}  mid {v['mid']}  (spread {v['spread_pct']:+.3f}%)")
        bs, aw = v["bid_shelf_below"], v["ask_wall_above"]
        if bs:
            rn = f" [round {bs['round_number']}]" if bs["round_number"] else (" [lone wall — spoof-prone]" if bs.get("spoof_prone") else "")
            print(f"- Bid shelf below: **{bs['price']}** (${bs['notional_usd']:,.0f}, {bs['dist_pct']:+.2f}%){rn}")
        if aw:
            rn = f" [round {aw['round_number']}]" if aw["round_number"] else (" [lone wall — spoof-prone]" if aw.get("spoof_prone") else "")
            print(f"- Ask wall above: **{aw['price']}** (${aw['notional_usd']:,.0f}, {aw['dist_pct']:+.2f}%){rn}")
        if v["truncated"]:
            print(f"- ⚠ TRUNCATED: {v['truncation_note']} (deepest seen: bid {v['deepest_level_seen']['bid']}, ask {v['deepest_level_seen']['ask']})")
        print()


def main():
    ap = argparse.ArgumentParser(description="Live order-book shelf read (read-only intel)")
    ap.add_argument("ticker")
    ap.add_argument("--venue", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    d = build_depth(args.ticker, venue=args.venue)
    if args.json:
        print(json.dumps(d))
    else:
        render_human(d)


if __name__ == "__main__":
    main()
