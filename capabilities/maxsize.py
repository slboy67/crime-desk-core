#!/usr/bin/env python3
"""maxsize.py — pre-trade max-clean-size + slippage curve + venue routing (SPEC-87).

The execution-microstructure layer under `size.py` (§7). `size` answers "what's the max
*account-risk* size / does the trade pass the §7 gate"; **`maxsize` answers "given I'm entering, what's
the max *clean* size per venue, on which venue is it cheapest, and how should I clip it"** — automating
the by-hand order-book walk the desk does before every fill ([[project_execution_venue_aster]]).

Reuses `depth.py`'s book fetchers (Aster/Bitget/Binance + Hyperliquid) — no new venue logic. Walks the
correct side (LONG entry / SHORT exit → buy ASKS ; SHORT entry / LONG exit → sell BIDS), computes the
max notional fillable within --bps of mid (VWAP slippage), a fixed slippage ladder, and a routing
recommendation that respects the self-custody constraint (aster/hyperliquid executable; binance/bitget
signal-only).

Read-only INTEL — participates in NO auto-verdict, like `depth`. It informs the human; it never gates
or sizes a trade by itself. The headline `max_clean_usd` is **calm-tape** capacity (exit liquidity in a
cascade is a fraction of it, §7) and slippage is the *fill* cost — on Aster/HL liquidation is marked off
the ORACLE aggregate, not this book (§7).

  python3 capabilities/maxsize.py BEL --side long --leg entry --bps 25 --json
  python3 capabilities/maxsize.py LAB --side short --size 5000 --json
  python3 capabilities/maxsize.py BEL --venue aster --json
"""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # sibling-capability imports (house style)

import depth as _depth                       # book fetchers (SPEC 27/84/85) — reused, not duplicated

# Module globals so tests monkeypatch them (house style — mirrors size.py).
BOOK_FETCHERS = dict(_depth.VENUES)          # {venue: fn(sym) -> (bids, asks, err)} — aster/bitget/binance
HL_VENUE = "hyperliquid"
EXECUTABLE = {"aster", "hyperliquid"}        # self-custody venues we can actually FILL on (§7)

LADDER = [100, 250, 500, 1000, 2000, 5000, 10000]   # the fixed slippage-curve rungs ($ notional)
DEFAULT_BPS = 25.0
TRUNC_BAND_PCT = 1.0          # walked-side book ending inside this band of mid → can't see deeper → truncated

SNAPSHOT_CAVEAT = ("CALM-TAPE SNAPSHOT ONLY — one book read; thin venues swell/thin/spoof "
                   "second-to-second. Re-walk the live DOM immediately before sizing; treat the "
                   "headline clean-size as conservative and discount lone spoof-prone walls.")
CALM_VS_FLUSH = ("max_clean_usd is CALM-TAPE capacity. Exit liquidity DURING a cascade is a fraction "
                 "of this (§7 size-to-the-exit) — do NOT read it as a safe position size.")
ORACLE_MARK_NOTE = ("Slippage here is the FILL cost only. On Aster/Hyperliquid liquidation is marked "
                    "off the ORACLE aggregate (Pyth/Chainlink/Binance), NOT this book — this is not "
                    "the risk read (§7).")


# ── book fetch (omit on absence/error — never a present-but-unavailable entry) ─────────────────────
def _hl_book(ticker):
    """HL l2 book → (bids, asks) or (None, None). Patchable in tests."""
    return _depth._fetch_hl_book(ticker)


def _get_book(name, sym, ticker):
    if name == HL_VENUE:
        try:
            return _hl_book(ticker)
        except Exception:                      # noqa: BLE001 — absence-safe
            return None, None
    fn = BOOK_FETCHERS.get(name)
    if not fn:
        return None, None
    try:
        bids, asks, _err = fn(sym)
    except Exception:                          # noqa: BLE001 — fetcher blew up → omit, don't crash
        return None, None
    return bids, asks


# ── side / leg geometry ────────────────────────────────────────────────────────────────────────────
def walked_side(side, leg):
    """LONG entry / SHORT exit = buy → walk ASKS ; SHORT entry / LONG exit = sell → walk BIDS."""
    side, leg = side.lower(), leg.lower()
    buy = (side == "long" and leg == "entry") or (side == "short" and leg == "exit")
    return ("ask", "buy") if buy else ("bid", "sell")


# ── the walks (pure) ────────────────────────────────────────────────────────────────────────────────
def _max_clean(levels, mid, buy, bps):
    """Max notional fillable while the realized VWAP stays within `bps` of mid (VWAP is monotone in
    notional, so we accumulate full levels then a partial that pins VWAP exactly to the band).
    Returns (max_clean_usd, walks_to_price, book_exhausted) — exhausted=True means the band was never
    reached (the whole book fills under bps) so the figure is a FLOOR, not a true ceiling."""
    thr = mid * (1 + bps / 1e4) if buy else mid * (1 - bps / 1e4)
    usd = base = 0.0
    last_px = mid
    for p, q in levels:
        nb, nu = base + q, usd + p * q
        nv = nu / nb
        within = nv <= thr if buy else nv >= thr
        if within:
            usd, base, last_px = nu, nb, p
            continue
        # partial fill x of this level so VWAP == thr:  (usd + p·x)/(base + x) = thr
        denom = p - thr
        if denom != 0:
            x = (thr * base - usd) / denom
            if x > 0:
                usd += p * x
                last_px = p
        return round(usd, 2), round(last_px, 8), False
    return round(usd, 2), round(last_px, 8), True


def _walk_target(levels, mid, buy, target):
    """Walk to fill `target` USD; return (filled_usd, vwap, realized_bps, walks_to, exhausted)."""
    usd = base = 0.0
    last_px = mid
    for p, q in levels:
        take = min(p * q, target - usd)
        if take <= 0:
            break
        last_px = p
        base += take / p
        usd += take
        if usd >= target - 1e-9:
            break
    exhausted = usd < target - 1e-9
    vwap = usd / base if base else mid
    rbps = (vwap - mid) / mid * 1e4 if buy else (mid - vwap) / mid * 1e4
    return round(usd, 2), round(vwap, 8), round(rbps, 2), round(last_px, 8), exhausted


def _venue_maxsize(name, bids, asks, side, leg, bps, target):
    wside, action = walked_side(side, leg)
    buy = action == "buy"
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    levels = asks if buy else bids

    max_clean, ceiling_px, exhausted = _max_clean(levels, mid, buy, bps)
    depth_1pct = round(sum(p * q for p, q in levels if abs(p - mid) / mid * 100 <= 1.0), 2)
    ladder = []
    for tgt in LADDER:
        f_usd, vwap, rbps, walks_to, ex = _walk_target(levels, mid, buy, tgt)
        ladder.append({"target_usd": tgt, "filled_usd": f_usd, "realized_bps": rbps,
                       "walks_to": walks_to, "exhausted": ex})

    deepest = max(p for p, _ in levels) if buy else min(p for p, _ in levels)
    cover_pct = abs(deepest - mid) / mid * 100
    truncated = cover_pct < TRUNC_BAND_PCT or exhausted
    executable = name in EXECUTABLE
    out = {
        "venue": name, "executable": executable,
        "custody": "self-custody" if executable else "cex-signal-only",
        "walked_side": wside, "action": action,
        "mid": round(mid, 8), "spread_bps": round((best_ask - best_bid) / mid * 1e4, 3),
        "bps_band": bps, "max_clean_usd": max_clean, "clean_walks_to": ceiling_px,
        "clean_ceiling_truncated": exhausted,           # ceiling is a FLOOR (book ran out under bps)
        "depth_within_1pct_usd": depth_1pct,
        "ladder": ladder, "n_levels": len(levels),
        "truncated": truncated, "deepest_level_seen": round(deepest, 8),
        "coverage_pct": round(cover_pct, 3),
        "truncation_note": (f"walked book caps within ±{cover_pct:.2f}% of mid — clean-size is a FLOOR "
                            f"(true ceiling deeper); defer to the live DOM, do NOT read as 'no liquidity'"
                            if truncated else None),
    }
    if target:
        f_usd, vwap, rbps, walks_to, ex = _walk_target(levels, mid, buy, target)
        out["target_walk"] = {"target_usd": target, "filled_usd": f_usd, "realized_bps": rbps,
                              "walks_to": walks_to, "exhausted": ex}
    return out


# ── routing (the deliverable) ─────────────────────────────────────────────────────────────────────
def _route(venues, target, bps):
    if not venues:
        return None
    execs = {n: v for n, v in venues.items() if v["executable"]}
    pool = execs or venues          # respect self-custody; fall back to all (flagged) if none executable

    if target:
        def key(kv):
            w = kv[1]["target_walk"]
            # fillable venues first (by cheapest realized slippage); else by most of the target covered
            return (0, w["realized_bps"], 0) if not w["exhausted"] else (1, 0.0, -w["filled_usd"])
        route_name = sorted(pool.items(), key=key)[0][0]
    else:
        route_name = sorted(pool.items(), key=lambda kv: -kv[1]["max_clean_usd"])[0][0]

    route = venues[route_name]
    ceiling = route["max_clean_usd"]
    deepest_name = max(venues.items(), key=lambda kv: kv[1]["max_clean_usd"])[0]

    clip = None
    if target and ceiling > 0 and target > ceiling:
        clips = math.ceil(target / ceiling)
        clip_usd = round(target / clips, 2)
        clip = {"clips": clips, "clip_usd": clip_usd,
                "note": (f"target ${target:,.0f} > {route_name} clean ceiling ${ceiling:,.0f} at "
                         f"{bps:g}bps — split into {clips}× ~${clip_usd:,.0f}; TWAP over time and "
                         f"RE-WALK between clips (the book re-spoofs second-to-second)")}

    rec = {
        "route_to": route_name, "executable": route["executable"],
        "clean_ceiling_usd": ceiling, "at_bps": bps,
        "basis": ("cheapest for requested size" if target else "largest clean ceiling"),
        "deepest_venue_overall": deepest_name, "clip_schedule": clip,
        "self_custody_constrained": bool(execs),
    }
    if deepest_name != route_name and not venues[deepest_name]["executable"]:
        rec["note"] = (f"deeper book on {deepest_name} (${venues[deepest_name]['max_clean_usd']:,.0f}) "
                       f"but it's signal-only CEX — not self-custody, cannot route execution there")
    if not execs:
        rec["warning"] = ("no self-custody/executable venue has a live book — venues shown are "
                          "signal-only CEX; cannot route execution there")
    return rec


def build_maxsize(ticker, side="long", leg="entry", bps=DEFAULT_BPS, venue=None, target=None):
    """Per-venue max clean clip + slippage ladder + routing recommendation. Read-only intel."""
    sym = ticker.upper().replace("USDT", "") + "USDT"
    names = [venue.lower()] if venue else (list(BOOK_FETCHERS) + [HL_VENUE])
    venues = {}
    for name in names:
        bids, asks = _get_book(name, sym, ticker)
        if not bids or not asks:
            continue                            # omit unavailable venues entirely (no null entry)
        venues[name] = _venue_maxsize(name, bids, asks, side, leg, bps, target)
    return {
        "ticker": sym.replace("USDT", ""), "side": side.lower(), "leg": leg.lower(),
        "action": walked_side(side, leg)[1], "walked_side": walked_side(side, leg)[0],
        "bps_band": bps, "requested_size_usd": target,
        "venues": venues, "recommendation": _route(venues, target, bps),
        "snapshot_caveat": SNAPSHOT_CAVEAT, "calm_vs_flush": CALM_VS_FLUSH,
        "oracle_mark_note": ORACLE_MARK_NOTE,
        "note": "read-only execution intel (SPEC-87) — not an auto-verdict input",
    }


def render_human(d):
    print(f"# {d['ticker']} — max clean size + routing (read-only intel)")
    print(f"side={d['side']} leg={d['leg']} → {d['action'].upper()} {d['walked_side'].upper()}S, "
          f"band {d['bps_band']:g}bps"
          + (f", target ${d['requested_size_usd']:,.0f}" if d['requested_size_usd'] else "") + "\n")
    if not d["venues"]:
        print("No venue with a live book.\n")
    for name, v in d["venues"].items():
        tag = "executable" if v["executable"] else "signal-only CEX"
        print(f"## {name.title()} ({tag})  mid {v['mid']}  spread {v['spread_bps']:.2f}bps")
        ceil_tag = " (FLOOR — truncated)" if v["clean_ceiling_truncated"] else ""
        print(f"- Max clean @ {v['bps_band']:g}bps: **${v['max_clean_usd']:,.0f}**{ceil_tag} "
              f"(walks to {v['clean_walks_to']}); depth within 1%: ${v['depth_within_1pct_usd']:,.0f}")
        for r in v["ladder"]:
            ex = " — CAN'T FILL (book exhausted)" if r["exhausted"] else ""
            print(f"    ${r['target_usd']:>6,}: {r['realized_bps']:>6.1f}bps → {r['walks_to']}{ex}")
        if v["truncated"]:
            print(f"  ⚠ {v['truncation_note']}")
        print()
    rec = d["recommendation"]
    if rec:
        print(f"## Route → {rec['route_to'].upper()} ({rec['basis']}; clean ${rec['clean_ceiling_usd']:,.0f})")
        if rec.get("clip_schedule"):
            c = rec["clip_schedule"]
            print(f"  Clip: {c['clips']}× ~${c['clip_usd']:,.0f} — {c['note']}")
        if rec.get("note"):
            print(f"  {rec['note']}")
        if rec.get("warning"):
            print(f"  ⚠ {rec['warning']}")
        print()
    print(f"⚠ {d['snapshot_caveat']}")
    print(f"⚠ {d['calm_vs_flush']}")
    print(f"⚠ {d['oracle_mark_note']}")


def main():
    ap = argparse.ArgumentParser(description="Pre-trade max-clean-size + slippage curve + venue routing")
    ap.add_argument("ticker")
    ap.add_argument("--side", default="long", choices=["long", "short"])
    ap.add_argument("--leg", default="entry", choices=["entry", "exit"])
    ap.add_argument("--bps", type=float, default=DEFAULT_BPS)
    ap.add_argument("--size", type=float, default=None, help="target notional USD for routing/clip")
    ap.add_argument("--venue", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    d = build_maxsize(args.ticker, side=args.side, leg=args.leg, bps=args.bps,
                      venue=args.venue, target=args.size)
    if args.json:
        print(json.dumps(d))
    else:
        render_human(d)


if __name__ == "__main__":
    main()
