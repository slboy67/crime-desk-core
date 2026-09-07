#!/usr/bin/env python3
"""desk.py — live trading-desk status (operationalize goal, item 4).

The "where am I" command: one call shows every armed watch + its levels + any fire, with the live
price/funding for each, so you never reconstruct state by hand. Scans the running watch processes
and their /tmp logs (the watch scripts + arm_setup.py all emit the same marked lines).

Usage:
  python3 scripts/desk.py            # full desk
  python3 scripts/desk.py --fires    # only watches that have fired something actionable
"""
import sys, re, glob, json, subprocess, urllib.request

FIRE_RE = re.compile(r"🔴|🎯|🚨|⚠|✅ ARMED|✅ STAGE|↟|CASCADE|FIRE|INVALIDATED|TIME-STOP|RE-GATED")
TICK_RE = re.compile(r"\b([A-Z]{2,12})(?:USDT)?\b")

def live(sym):
    try:
        with urllib.request.urlopen(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}USDT", timeout=6) as r:
            d = json.loads(r.read().decode())["result"]["list"][0]
            return float(d["lastPrice"]), float(d["fundingRate"]) * 100, float(d["price24hPcnt"]) * 100
    except Exception:
        return None, None, None

def running_watches():
    """Map of ticker-ish -> (script, pid) for live watch/arm processes."""
    out = {}
    try:
        ps = subprocess.run(["ps", "-eo", "pid,command"], capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return out
    for ln in ps.splitlines():
        if not re.search(r"(_watch\.sh|_manage\.sh|_trigger\.sh|arm_setup\.py|staging_watch)", ln):
            continue
        if "grep" in ln or "desk.py" in ln:
            continue
        pid = ln.split()[0]
        m = re.search(r"/?(\w+?)_(?:short|blowoff|fade|scalp|staging|stage5|trigger|manage|watch)", ln) \
            or re.search(r"arm_setup\.py (\w+)", ln)
        name = (m.group(1).upper() if m else "?")
        out.setdefault(name, []).append((ln.split(None, 1)[1][:34], pid))
    return out

def last_fire(ticker):
    """Most recent actionable line across this ticker's /tmp logs."""
    best = None
    for path in glob.glob("/tmp/*.log"):
        base = path.split("/")[-1].lower()
        if ticker.lower() not in base:
            continue
        try:
            lines = open(path).read().splitlines()
        except Exception:
            continue
        for ln in reversed(lines):
            if FIRE_RE.search(ln):
                best = ln.strip()[:120]; break
        if best:
            break
    return best

def main():
    fires_only = "--fires" in sys.argv
    watches = running_watches()
    INFRA = {"CLUSTER", "CLUSTER_DIST", "CLUSTER_DIST_NONCE", "VC", "HL", "NONCE", "?"}
    tickers = {k: v for k, v in watches.items() if k not in INFRA}
    infra = {k: v for k, v in watches.items() if k in INFRA}
    print(f"\n═══ DESK — {len(tickers)} ticker watches · {len(infra)} infra ═══\n")
    if not tickers:
        print("  (no per-ticker watches running)")
    for tk in sorted(tickers):
        px, fr, chg = live(tk)
        fire = last_fire(tk)
        if fires_only and not fire:
            continue
        if px is None:   # name didn't resolve to a perp → treat as infra, not a ticker
            infra.setdefault(tk, []).extend(tickers[tk]); continue
        pxs = f"${px:g} {chg:+.1f}% f{fr:+.3f}%"
        procs = ", ".join(f"{s.split()[0].split('/')[-1]}@{p}" for s, p in tickers[tk])
        print(f"  {tk:9s} {pxs:30s} [{procs}]")
        if fire:
            print(f"      ↳ {fire}")
    if infra:
        print("\n  infra watches: " + " · ".join(f"{k}@{v[0][1]}" for k, v in sorted(infra.items())))
    # cluster distribution snapshot (cheap: is cluster_monitor running?)
    try:
        cm = subprocess.run(["pgrep", "-f", "cluster_monitor|cluster_dist"], capture_output=True, text=True, timeout=5).stdout.strip()
        print(f"\n  cluster-distribution watch: {'ALIVE' if cm else 'not running'}")
    except Exception:
        pass
    print("\n  next: `triage.py` for the open-setup board · `regime_flip.py --once` for funding drift\n")

if __name__ == "__main__":
    main()
