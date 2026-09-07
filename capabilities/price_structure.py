#!/usr/bin/env python3
"""price_structure.py — Layer 4 daily price-structure analyzer (NATIVE).

build_structure(sym, days, squeeze_pct) -> dict (pure) ; render_human(s) (default).

  python3 capabilities/price_structure.py LAB
  python3 capabilities/price_structure.py LAB --days 60 --squeeze-pct 20 --json

Pulls daily klines (Binance Futures -> Bybit -> Aster fallback, SPEC-175 — the
`meta.kline_venue` key names the venue that answered) and computes ATH/ATL + range
position, squeeze events + diminishing/growing pattern, cascade-vs-bounce volume,
last-7 swing structure, compression, and cycle age.
"""
import argparse
import json
import sys
import urllib.request
from urllib.error import URLError, HTTPError
from datetime import datetime, timezone

TIMEOUT = 10
UA = {"User-Agent": "price-structure/1.0"}


def fetch(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except (URLError, HTTPError, json.JSONDecodeError, TimeoutError):
        return None


# SPEC-175: multi-venue kline fallback — Binance → Bybit → Aster. Binance is
# perp-casino-listed but IP-banned/rate-limited (418/429/-1003) or simply doesn't
# list a name (BTR/HNT/ZKC/TUT); `fetch()` already returns None on any HTTPError
# (418/429 raise HTTPError), so a ban falls through to the next venue instead of
# dying. First venue producing >= MIN_KLINE_BARS daily bars wins.
MIN_KLINE_BARS = 30


def _binance_klines(sym, limit=1500):
    d = fetch(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}USDT&interval=1d&limit={limit}")
    if not isinstance(d, list) or not d:
        return None
    try:
        return [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                 "c": float(k[4]), "v": float(k[5]), "qv": float(k[7])} for k in d]
    except (TypeError, IndexError, ValueError):
        return None


def _bybit_klines(sym, limit=1500):
    d = fetch(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}USDT"
              f"&interval=D&limit={min(limit, 1000)}")
    try:
        rows = d["result"]["list"]   # bybit returns newest-first
    except (TypeError, KeyError):
        return None
    if not rows:
        return None
    try:
        out = [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                "c": float(r[4]), "v": float(r[5]), "qv": float(r[6])} for r in rows]
    except (IndexError, ValueError):
        return None
    out.reverse()   # normalize to oldest-first, matching Binance's order
    return out


def _aster_klines(sym, limit=1500):
    d = fetch(f"https://fapi.asterdex.com/fapi/v1/klines?symbol={sym}USDT&interval=1d&limit={limit}")
    if not isinstance(d, list) or not d:
        return None
    try:
        return [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                 "c": float(k[4]), "v": float(k[5]), "qv": float(k[7])} for k in d]
    except (TypeError, IndexError, ValueError):
        return None


KLINE_VENUES = [("binance", _binance_klines), ("bybit", _bybit_klines), ("aster", _aster_klines)]


def fetch_klines_multi(sym, limit=1500):
    """Try each venue in KLINE_VENUES order; first with >= MIN_KLINE_BARS daily bars
    wins immediately. If no venue clears the bar, fall back to the longest history any
    venue DID return (a genuinely young listing has < MIN_KLINE_BARS everywhere — that's
    `young_listing`, not a failure) rather than erroring out from under it.
    Returns (candles oldest-first, venue_name) or (None, None) if every venue is empty."""
    best, best_venue = None, None
    for venue, fn in KLINE_VENUES:
        candles = fn(sym, limit)
        if not candles:
            continue
        if len(candles) >= MIN_KLINE_BARS:
            return candles, venue
        if best is None or len(candles) > len(best):
            best, best_venue = candles, venue
    return (best, best_venue) if best is not None else (None, None)


def fmt_d(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def fmt_p(x):
    if x is None:
        return "—"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.01:
        return f"{x:.5f}"
    return f"{x:.6f}"


def build_structure(sym, days=90, squeeze_pct=15.0):
    """Pure compute → the price-structure contract dict (or {error} if unlisted).

    SPEC 57: the FULL listing history is always fetched (one 1d call at limit 1500 ≈
    4 years) — `ath_alltime`/`listing_date`/`off_ath_alltime_pct` come from the
    lifetime; the window stats (`ath`, honestly aliased `window_high`) come from the
    requested `days` slice. A 90d window once relabeled FOLKS' $47 contract ATH as
    "ath: 2.613" — a −98% prior cycle silently erased; `prior_cycle` now flags it."""
    sym = sym.upper().replace("USDT", "")
    full, kline_venue = fetch_klines_multi(sym)
    if not full:
        return {"ticker": sym, "error": "no kline data (not listed on Binance/Bybit/Aster Futures?)"}
    candles = full[-days:] if days and len(full) > days else full
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    closes = [c["c"] for c in candles]

    # lifetime read (SPEC 57)
    full_highs = [c["h"] for c in full]
    ath_all_i = max(range(len(full_highs)), key=lambda i: full_highs[i])
    ath_alltime = full_highs[ath_all_i]
    cur_close = closes[-1]
    off_ath_alltime = round((cur_close / ath_alltime - 1) * 100, 2) if ath_alltime else None
    ath_predates_window = full[ath_all_i]["t"] < candles[0]["t"]
    prior_cycle = bool(ath_predates_window and off_ath_alltime is not None
                       and off_ath_alltime < -80.0)

    ath_i = max(range(len(highs)), key=lambda i: highs[i])
    atl_i = min(range(len(lows)), key=lambda i: lows[i])
    ath, atl, cur = highs[ath_i], lows[atl_i], closes[-1]

    squeezes = []
    for i in range(1, len(candles)):
        p = (closes[i] - closes[i - 1]) / closes[i - 1] * 100
        if p >= squeeze_pct:
            squeezes.append({"day": fmt_d(candles[i]["t"]), "pct": round(p, 1),
                             "close": closes[i], "vol_m": round(candles[i]["qv"] / 1e6, 1)})
    pattern = "none"
    if len(squeezes) >= 2:
        mags = [s["pct"] for s in squeezes]
        if all(mags[i] >= mags[i + 1] for i in range(len(mags) - 1)):
            pattern = "diminishing"   # Stage-5 exit fingerprint
        elif all(mags[i] <= mags[i + 1] for i in range(len(mags) - 1)):
            pattern = "growing"       # trap-formation accelerating
        else:
            pattern = "mixed"

    recent = candles[-14:] if len(candles) >= 14 else candles
    green_v = sum(c["qv"] for c in recent if c["c"] >= c["o"])
    red_v = sum(c["qv"] for c in recent if c["c"] < c["o"])
    ratio = round(red_v / green_v, 2) if (green_v and red_v) else None
    vol_read = None
    if ratio is not None:
        vol_read = ("cascade" if ratio > 1.5 else "bounce" if ratio < 0.5 else "balanced")

    last7 = candles[-7:] if len(candles) >= 7 else candles
    lh = sum(1 for i in range(1, len(last7)) if last7[i]["h"] < last7[i - 1]["h"])
    hh = (len(last7) - 1) - lh
    ll = sum(1 for i in range(1, len(last7)) if last7[i]["l"] < last7[i - 1]["l"])
    hl = (len(last7) - 1) - ll
    struct_read = "downtrend" if (lh >= 4 and ll >= 4) else "uptrend" if (hh >= 4 and hl >= 4) else "mixed"
    # SPEC-166: the MOST RECENT swing specifically — a name can carry a "mixed" 7-candle
    # read (some lower-highs mixed in) while its very last swing is still a fresh higher
    # high (MAGMA: hh=3/lh=3 read "mixed" but still actively squeezing). Ties count as a
    # higher high (consistent with the hh = complement-of-strict-lower-high convention above).
    last_swing_higher_high = (len(last7) >= 2 and not (last7[-1]["h"] < last7[-2]["h"]))

    compression = None
    if len(candles) >= 21:
        recent_range = max(c["h"] for c in candles[-7:]) - min(c["l"] for c in candles[-7:])
        prior = candles[-21:-7]
        prior_range = max(c["h"] for c in prior) - min(c["l"] for c in prior)
        if prior_range > 0:
            tight = round(recent_range / prior_range, 2)
            compression = {"recent_range": recent_range, "prior_range": prior_range,
                           "tightness": tight, "wedge": tight < 0.5}

    # SPEC-96: recent 3-day high/low range (%) — the basing/coiling input for the funding
    # monitor's location classifier (tight 3d range + no new lows = coiling, not cascading).
    r3 = candles[-3:] if len(candles) >= 3 else candles
    r3_lo = min(c["l"] for c in r3)
    r3_hi = max(c["h"] for c in r3)
    range_3d_pct = round((r3_hi - r3_lo) / r3_lo * 100, 1) if r3_lo > 0 else None

    # SPEC-138: last-10-days daily volume (oldest→newest) — the recent-volume-SLOPE input
    # for callers (e.g. faded_bounce) that must distinguish "attention faded" (declining
    # slope) from "far below a one-off historical peak" (%-of-peak alone false-passes an
    # actively squeezing name whose volume is rising hard right now).
    vol_daily_m = [round(c["qv"] / 1e6, 2) for c in candles[-10:]]

    age_days = round((full[-1]["t"] - full[0]["t"]) / 86_400_000)
    return {
        "ticker": sym, "days_available": len(candles), "current_close": cur,
        # window-scoped (existing keys preserved; honest aliases added — SPEC 57)
        "ath": ath, "ath_date": fmt_d(candles[ath_i]["t"]), "days_since_ath": len(candles) - 1 - ath_i,
        "window_high": ath, "window_low": atl, "window_days": len(candles),
        "atl": atl, "atl_date": fmt_d(candles[atl_i]["t"]), "days_since_atl": len(candles) - 1 - atl_i,
        "range_pos": round((cur - atl) / (ath - atl) * 100, 1) if ath > atl else 0,
        "range_3d_pct": range_3d_pct,   # SPEC-96: recent 3d high/low range % (basing input)
        "off_ath_pct": round((cur / ath - 1) * 100, 2), "off_atl_pct": round((cur / atl - 1) * 100, 2),
        # lifetime (SPEC 57 — the window must never masquerade as the contract's history)
        "listing_date": fmt_d(full[0]["t"]),
        "ath_alltime": ath_alltime, "ath_alltime_date": fmt_d(full[ath_all_i]["t"]),
        "off_ath_alltime_pct": off_ath_alltime,
        "prior_cycle": prior_cycle,
        "squeeze_pct_threshold": squeeze_pct, "squeezes": squeezes, "squeeze_pattern": pattern,
        "vol_profile": {"green_m": round(green_v / 1e6, 1), "red_m": round(red_v / 1e6, 1),
                        "red_green_ratio": ratio, "read": vol_read},
        "structure": {"lower_highs": lh, "higher_highs": hh, "lower_lows": ll,
                      "higher_lows": hl, "read": struct_read,
                      "last_swing_higher_high": last_swing_higher_high},
        "compression": compression,
        "vol_daily_m": vol_daily_m,
        "age_days": age_days, "young_listing": age_days < 30,
        "meta": {"kline_venue": kline_venue},   # SPEC-175: which venue's klines this ran on
    }


def squeeze_legs_in_window(squeezes, window_days, as_of=None):
    """SPEC-173 — THE squeeze-leg definition (build_structure's `squeezes`: a day-close move
    >= squeeze_pct, default 15%). Counts how many entries of an already-computed `squeezes`
    list (from build_structure()['squeezes']) fall within the trailing `window_days` days of
    `as_of` (default: today, UTC date). Callers needing a windowed squeeze-leg count (e.g. the
    CLAUDE §6 chronic-squeezer pre-check) import THIS rather than re-deriving the date-cutoff
    filter — two independently-written cutoff loops is how `faded_bounce.squeeze_legs_60d` and
    a hand-run `price_structure` call drifted apart (scout sweep 2026-08-28)."""
    as_of = as_of or datetime.now(timezone.utc).date()
    cutoff = as_of.toordinal() - window_days
    n = 0
    for sq in squeezes:
        d = sq.get("day") if isinstance(sq, dict) else None
        if not d:
            continue
        try:
            if datetime.strptime(d, "%Y-%m-%d").date().toordinal() >= cutoff:
                n += 1
        except ValueError:
            continue
    return n


def render_human(s):
    if s.get("error"):
        print(f"# {s['ticker']}USDT — {s['error']}")
        return
    print(f"# {s['ticker']}USDT — price structure ({s['days_available']}d daily klines)\n")
    print(f"- Current close: {fmt_p(s['current_close'])}")
    print(f"- Window ATH: {fmt_p(s['ath'])} on {s['ath_date']} ({s['days_since_ath']}d ago)")
    print(f"- ALL-TIME high: {fmt_p(s['ath_alltime'])} on {s['ath_alltime_date']} "
          f"({s['off_ath_alltime_pct']:+.1f}% from here, listed {s['listing_date']})")
    if s.get("prior_cycle"):
        print("  ⚠ PRIOR CYCLE: the all-time high predates this window at <-80% — a completed "
              "boom-bust the window stats can't see; do NOT quote the window high as ATH")
    print(f"- Window ATL: {fmt_p(s['atl'])} on {s['atl_date']} ({s['days_since_atl']}d ago)")
    print(f"- Position in range: {s['range_pos']}% (0=ATL, 100=ATH)")
    print(f"- Off ATH: {s['off_ath_pct']:+.2f}%   /   Off ATL: {s['off_atl_pct']:+.2f}%")

    print(f"\n## Squeeze events (daily close-to-close > {s['squeeze_pct_threshold']}%)\n")
    if s["squeezes"]:
        print("| # | Date | Move | Close | Quote vol |")
        print("|---|---|---|---|---|")
        for n, q in enumerate(s["squeezes"], 1):
            print(f"| {n} | {q['day']} | +{q['pct']:.1f}% | {fmt_p(q['close'])} | ${q['vol_m']:.1f}M |")
        notes = {"diminishing": "→ Diminishing-returns pattern. Stage 5 exit fingerprint (§4).",
                 "growing": "→ Squeeze magnitudes growing. Trap-formation accelerating.",
                 "mixed": "→ Mixed squeeze pattern — no clear trend."}
        if s["squeeze_pattern"] in notes:
            print("\n" + notes[s["squeeze_pattern"]])
    else:
        print("- No daily squeeze events above threshold in window.")

    vp = s["vol_profile"]
    print(f"\n## Volume profile (last 14d)\n- Green ${vp['green_m']}M / Red ${vp['red_m']}M"
          + (f"  ·  Red/Green {vp['red_green_ratio']} → {vp['read']}" if vp["read"] else ""))

    st = s["structure"]
    print(f"\n## Structure (last 7 candles)\n- Lower-highs {st['lower_highs']} / Higher-highs {st['higher_highs']}"
          f"  ·  Lower-lows {st['lower_lows']} / Higher-lows {st['higher_lows']}  → {st['read']}")

    if s["compression"]:
        c = s["compression"]
        print(f"\n## Compression\n- Tightness {c['tightness']} (recent7 {fmt_p(c['recent_range'])} / prior14 {fmt_p(c['prior_range'])})"
              + ("  → wedge/compression — directional break often follows" if c["wedge"] else ""))

    print(f"\n## Cycle position\n- {s['days_available']}d of futures data ({s['age_days']}d span)"
          + ("  → young listing, multi-leg cycles may lie ahead" if s["young_listing"] else ""))


def main():
    ap = argparse.ArgumentParser(description="Layer 4 price-structure analyzer")
    ap.add_argument("ticker")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--squeeze-pct", type=float, default=15.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    s = build_structure(args.ticker, args.days, args.squeeze_pct)
    if args.json:
        print(json.dumps(s))
    else:
        render_human(s)


if __name__ == "__main__":
    main()
