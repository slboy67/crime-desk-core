#!/usr/bin/env python3
"""liq_magnets.py — Layer 3 liquidation-magnet estimator (NATIVE).

Volume-profile proxy for where positioning (and thus liquidation cascades) cluster.
NOT a Coinglass heatmap replica — it produces the operational output you'd use
Coinglass for: candidate magnet price zones. Approximate; verify trade-critical
levels against the real chart.

build_magnets(sym, days, interval, buckets) -> dict (pure) ; render_human(m).

  python3 capabilities/liq_magnets.py LAB
  python3 capabilities/liq_magnets.py LAB --days 14 --interval 15m --buckets 100 --json
"""
import argparse
import json
import urllib.request

TIMEOUT = 10
UA = {"User-Agent": "liq-magnets/1.0"}
INTERVAL_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240, "1d": 1440}


def fetch(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fmt_p(x):
    if x is None:
        return "—"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.01:
        return f"{x:.5f}"
    return f"{x:.6f}"


def build_volume_profile(candles, buckets):
    lo = min(c["l"] for c in candles)
    hi = max(c["h"] for c in candles)
    if hi <= lo:
        return [], lo, hi
    width = (hi - lo) / buckets
    profile = [0.0] * buckets
    for c in candles:
        if c["h"] - c["l"] <= 0:
            profile[min(buckets - 1, int((c["c"] - lo) / width))] += c["qv"]
            continue
        i_lo = max(0, int((c["l"] - lo) / width))
        i_hi = min(buckets - 1, int((c["h"] - lo) / width))
        if i_hi == i_lo:
            profile[i_lo] += c["qv"]
        else:
            per = c["qv"] / (i_hi - i_lo + 1)
            for i in range(i_lo, i_hi + 1):
                profile[i] += per
    return profile, lo, hi


def find_hvns(profile, lo, hi, top_n=15, merge_gap=2):
    width = (hi - lo) / len(profile)
    threshold = (sum(profile) / len(profile)) * 1.3
    indexed = [(i, v) for i, v in enumerate(profile) if v > threshold]
    if not indexed:
        return []
    clusters = [[indexed[0]]]
    for idx, val in indexed[1:]:
        if idx - clusters[-1][-1][0] <= merge_gap:
            clusters[-1].append((idx, val))
        else:
            clusters.append([(idx, val)])
    out = []
    for cl in clusters:
        i_lo, i_hi = cl[0][0], cl[-1][0]
        peak_i = max(cl, key=lambda x: x[1])[0]
        out.append({"p_lo": lo + i_lo * width, "p_hi": lo + (i_hi + 1) * width,
                    "p_peak": lo + (peak_i + 0.5) * width,
                    "volume": sum(v for _, v in cl), "bucket_count": i_hi - i_lo + 1})
    out.sort(key=lambda z: -z["volume"])
    return out[:top_n]


def round_number_magnets(lo, hi):
    if hi <= 0 or hi <= lo:
        return []
    steps = [100, 50, 25, 20, 10, 5, 2.5, 2, 1, 0.5, 0.25, 0.2, 0.1, 0.05, 0.025,
             0.02, 0.01, 0.005, 0.0025, 0.002, 0.001, 0.0005, 0.0002, 0.0001]
    chosen = None
    for step in steps:
        if 5 <= int(hi / step) - int(lo / step) + 1 <= 15:
            chosen = step
            break
    if chosen is None:
        for step in steps:
            if int(hi / step) - int(lo / step) + 1 >= 3:
                chosen = step
                break
    if chosen is None:
        return []
    levels, n = [], int(lo / chosen)
    while n * chosen <= hi + chosen * 0.5:
        v = round(n * chosen, 8)
        if lo <= v <= hi:
            levels.append(v)
        n += 1
    return sorted(set(levels))


def build_magnets(sym, days=14, interval="15m", buckets=100):
    """Pure compute → the liq-magnet contract dict (or {error} if unlisted)."""
    sym = sym.upper().replace("USDT", "")
    bars = min(int(days * 1440 / INTERVAL_MIN.get(interval, 15)), 1500)
    raw = fetch(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}USDT&interval={interval}&limit={bars}")
    if not raw or not isinstance(raw, list):
        return {"ticker": sym, "error": f"no kline data on Binance Futures at {interval}"}
    candles = [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                "c": float(k[4]), "v": float(k[5]), "qv": float(k[7])} for k in raw]
    cur = candles[-1]["c"]
    profile, lo, hi = build_volume_profile(candles, buckets)
    if not profile:
        return {"ticker": sym, "current": cur, "error": "price range collapsed — no profile"}

    hvns = find_hvns(profile, lo, hi, top_n=15)
    for z in hvns:
        z["dist_pct"] = round((z["p_peak"] - cur) / cur * 100, 2)
        z["volume_m"] = round(z["volume"] / 1e6, 1)
    above = sorted([z["p_peak"] for z in hvns if z["p_peak"] > cur])[:5]
    below = sorted([z["p_peak"] for z in hvns if z["p_peak"] <= cur], reverse=True)[:5]
    rn = [{"price": v, "dist_pct": round((v - cur) / cur * 100, 2) if cur else 0,
           "implicit_magnet": abs((v - cur) / cur * 100) < 50 if cur else False}
          for v in round_number_magnets(lo, hi)]
    return {
        "ticker": sym, "current": cur, "interval": interval, "bars": len(candles),
        "range_lo": lo, "range_hi": hi, "buckets": buckets,
        "hvns": hvns, "upside_magnets": above, "downside_magnets": below,
        "round_numbers": rn,
    }


def render_human(m):
    if m.get("error"):
        print(f"# {m['ticker']}USDT — {m['error']}")
        return
    print(f"# {m['ticker']}USDT — liquidation magnet estimator (volume-profile proxy)\n")
    print(f"- Source: Binance Futures {m['interval']} klines × {m['bars']} bars")
    print(f"- Current: {fmt_p(m['current'])}   ·   Range {fmt_p(m['range_lo'])} → {fmt_p(m['range_hi'])} ({m['buckets']} buckets)")
    if not m["hvns"]:
        print("\n## No HVN clusters above threshold (avg×1.3)")
        return
    print(f"\n## High Volume Node clusters (top {len(m['hvns'])})\n")
    print("| Rank | Zone | Peak | Dist | Vol (M$) | Width |")
    print("|---|---|---|---|---|---|")
    for n, z in enumerate(m["hvns"], 1):
        d = "↑" if z["dist_pct"] > 0 else "↓"
        print(f"| {n} | {fmt_p(z['p_lo'])} – {fmt_p(z['p_hi'])} | {fmt_p(z['p_peak'])} | {d} {abs(z['dist_pct']):.2f}% | {z['volume_m']} | {z['bucket_count']} |")
    print(f"\n## Direction split\n- Upside magnets: {', '.join(fmt_p(v) for v in m['upside_magnets']) or '—'}")
    print(f"- Downside magnets: {', '.join(fmt_p(v) for v in m['downside_magnets']) or '—'}")
    print("\n## Round-number magnets in range\n")
    for v in m["round_numbers"]:
        d = "↑" if v["dist_pct"] > 0 else "↓" if v["dist_pct"] < 0 else "·"
        print(f"- {fmt_p(v['price'])}  ({d} {abs(v['dist_pct']):.2f}%)" + ("  ⭐" if v["implicit_magnet"] else ""))
    print("\n- HVN above = upside magnets (short stops, squeeze); below = downside (long stops, cascade). Verify vs the real chart.")


def main():
    ap = argparse.ArgumentParser(description="Layer 3 liquidation-magnet estimator")
    ap.add_argument("ticker")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--buckets", type=int, default=100)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    m = build_magnets(args.ticker, args.days, args.interval, args.buckets)
    if args.json:
        print(json.dumps(m))
    else:
        render_human(m)


if __name__ == "__main__":
    main()
