#!/usr/bin/env python3
"""pull5.py — unified multi-layer pull for one ticker (NATIVE aggregator, §14).

Where the parts-bin pull5 shelled six scripts and printed concatenated markdown,
this composes the NATIVE build_* functions into ONE structured object:

  Layer 0  token id / supply / cycle      (Coingecko)
  Layer 2  cross-venue perp regime        (regime_check.build_regime)
  Layer 3  liquidation magnet zones       (liq_magnets.build_magnets)
  Layer 4  daily price structure          (price_structure.build_structure)
  Layer 1  on-chain safe audit            (onchain capability — OPT-IN, slow RPC)

build_pull5(ticker, cg_id=None, include_onchain=False) -> dict (pure-ish: net I/O).

  python3 capabilities/pull5.py LAB
  python3 capabilities/pull5.py BILL --cg-id billions-network --json
  python3 capabilities/pull5.py LAB --onchain --json     # add the slow on-chain layer

On-chain is opt-in because the wallet sweep is slow/often RPC-degraded; the Designer
normally calls the `onchain` capability separately. Layer 5 (catalyst/sentiment) is
manual by design and is not fetched here.
"""
import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import colors as C
from regime_check import build_regime
from liq_magnets import build_magnets
from price_structure import build_structure

ROOT = HERE.parent
TIMEOUT = 15
UA = {"User-Agent": "pull5/1.0"}

KNOWN_CG_IDS = {
    "BILL": "billions-network", "MON": "monad", "LAB": "lab", "SKYAI": "skyai",
    "MYX": "myx-finance", "AIOT": "okzoo", "SIREN": "siren-2", "PLAY": "play", "COAI": "coai",
}


def fetch(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


# ── SPEC 36: ticker→coingecko-id resolution must pick the LIVE token on symbol collisions ──
def _cg_detail_url(cid):
    return (f"https://api.coingecko.com/api/v3/coins/{cid}?localization=false&tickers=false"
            f"&community_data=false&developer_data=false")


def _rank_cg_candidates(coins, ticker):
    """Rank /search candidates so the live token wins a symbol collision: exact-symbol matches
    first, ordered by market_cap_rank ascending (unranked last)."""
    sym = ticker.upper()
    exact = [c for c in coins if (c.get("symbol") or "").upper() == sym]
    pool = exact or list(coins)

    def key(c):
        r = c.get("market_cap_rank")
        return (0, r) if isinstance(r, int) else (1, 0)     # ranked first (asc), unranked last
    return sorted(pool, key=key)


def _rank_ambiguous(ranked):
    """Uncertain when the best candidate has no rank, or the top two ranks are <3× apart."""
    if not ranked:
        return True
    r1 = ranked[0].get("market_cap_rank")
    if not isinstance(r1, int):
        return True
    if len(ranked) > 1:
        r2 = ranked[1].get("market_cap_rank")
        if isinstance(r2, int) and r1 > 0 and r2 / r1 < 3:
            return True
    return False


def _perp_price(ticker):
    """Live perp last-price (Binance→Bybit) for the symbol-collision sanity check."""
    sym = ticker.upper().replace("USDT", "") + "USDT"
    for url, pick in (
        (f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}",
         lambda d: float(d.get("price"))),
        (f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}",
         lambda d: float(d["result"]["list"][0]["lastPrice"])),
    ):
        try:
            d = fetch(url)
            if isinstance(d, dict) and "_error" not in d:
                p = pick(d)
                if p and p > 0:
                    return p
        except Exception:  # noqa: BLE001
            pass
    return None


def _price_diverges(cg_price, perp_price, factor=5.0):
    """True when the coingecko price is >factor× away from the live perp price (= wrong token)."""
    try:
        cg_price, perp_price = float(cg_price or 0), float(perp_price or 0)
    except (TypeError, ValueError):
        return False
    if cg_price <= 0 or perp_price <= 0:
        return False
    hi, lo = max(cg_price, perp_price), min(cg_price, perp_price)
    return hi / lo > factor


def _resolve_by_search(ticker, coins):
    """Pick the live coingecko id from /search results → (cg_id, detail, low_confidence). Ranks by
    market_cap_rank, then uses the live perp price to reject a wildly-divergent (wrong) token."""
    ranked = _rank_cg_candidates(coins, ticker)
    if not ranked:
        return None, None, True
    low_conf = _rank_ambiguous(ranked)
    perp = _perp_price(ticker)
    fallback = None
    for cand in ranked[:3]:
        cid = cand.get("id")
        if not cid:
            continue
        d = fetch(_cg_detail_url(cid))
        if not isinstance(d, dict) or "_error" in d:
            continue
        if fallback is None:
            fallback = (cid, d)
        price = (d.get("market_data") or {}).get("current_price", {}).get("usd", 0)
        if perp and _price_diverges(price, perp):
            low_conf = True                                  # wrong token → re-resolve (next-best)
            continue
        return cid, d, low_conf
    if fallback:                                             # nothing price-sane → best-by-rank, flagged
        return fallback[0], fallback[1], True
    return ranked[0].get("id"), None, True


def coingecko_layer(ticker, cg_id=None):
    low_conf, d = False, None
    if cg_id:
        d = fetch(_cg_detail_url(cg_id))
    else:
        cg_id = KNOWN_CG_IDS.get(ticker.upper())
        if cg_id:
            d = fetch(_cg_detail_url(cg_id))
        else:
            s = fetch(f"https://api.coingecko.com/api/v3/search?query={ticker}")
            coins = s.get("coins") if isinstance(s, dict) else None
            if coins:
                cg_id, d, low_conf = _resolve_by_search(ticker, coins)
    if not cg_id:
        return {"_error": f"no coingecko id found for {ticker}"}
    if not isinstance(d, dict) or "_error" in d:
        return {"cg_id": cg_id, "_error": d.get("_error", "fetch failed") if isinstance(d, dict) else "fetch failed"}
    md = d.get("market_data", {})
    mc = md.get("market_cap", {}).get("usd", 0) or 0
    fdv = md.get("fully_diluted_valuation", {}).get("usd", 0) or 0
    cs = md.get("circulating_supply") or 0
    ts = md.get("total_supply") or md.get("max_supply") or 0
    return {
        "cg_id": cg_id, "resolved_id": cg_id,                # SPEC 36: explicit resolved id + confidence
        "low_confidence": low_conf, "ambiguous": low_conf,
        "name": d.get("name"), "symbol": (d.get("symbol") or "").upper(),
        "categories": d.get("categories") or [],
        "contracts": {k: v for k, v in (d.get("platforms") or {}).items() if v},
        "market_cap": mc, "fdv": fdv,
        "fdv_mc_ratio": round(fdv / mc, 2) if (mc and fdv) else None,
        "fdv_mc_redflag": bool(mc and fdv and fdv / mc > 4),
        "circulating": cs, "total_supply": ts,
        "pct_circulating": round(cs / ts * 100, 1) if (cs and ts) else None,
        "supply_locked_flag": bool(cs and ts and cs / ts * 100 <= 25),
        "price": md.get("current_price", {}).get("usd", 0),
        "ath": md.get("ath", {}).get("usd", 0), "ath_date": md.get("ath_date", {}).get("usd"),
        "atl": md.get("atl", {}).get("usd", 0), "atl_date": md.get("atl_date", {}).get("usd"),
        "chg_24h": md.get("price_change_percentage_24h", 0),
        "chg_7d": md.get("price_change_percentage_7d", 0),
        "chg_30d": md.get("price_change_percentage_30d", 0),
        "vol_24h": md.get("total_volume", {}).get("usd", 0),
    }


def onchain_layer(ticker):
    try:
        out = subprocess.run(
            ["python3", str(ROOT / "_oldrepo/scripts/onchain_analyser.py"), ticker, "--json"],
            capture_output=True, text=True, timeout=200, cwd=str(ROOT))
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001
        return {"_error": f"onchain layer failed: {e}"}


def build_pull5(ticker, cg_id=None, include_onchain=False):
    ticker = ticker.upper().replace("USDT", "")
    out = {
        "ticker": ticker,
        "layer0_coingecko": coingecko_layer(ticker, cg_id),
        "layer2_regime": build_regime(ticker),
        "layer3_magnets": build_magnets(ticker),
        "layer4_structure": build_structure(ticker),
    }
    out["layer1_onchain"] = onchain_layer(ticker) if include_onchain else None
    return out


def render_human(p):
    print(f"# {p['ticker']} — multi-layer pull\n")
    cg = p["layer0_coingecko"]
    print(C.c("## Layer 0 — token id (Coingecko)", "bold", "cyan"))
    if cg.get("_error"):
        print(f"- {cg['_error']}")
    else:
        print(f"- **{cg['name']}** ({cg['symbol']})  ·  cats: {', '.join(cg['categories'][:4])}")
        print(f"- MC ${cg['market_cap']:,.0f} / FDV ${cg['fdv']:,.0f}"
              + (f"  ·  FDV/MC {cg['fdv_mc_ratio']}×" + (" ⚠" if cg['fdv_mc_redflag'] else "") if cg['fdv_mc_ratio'] else ""))
        if cg["pct_circulating"] is not None:
            print(f"- Circ {cg['pct_circulating']}%" + (" ⚠ ≥75% locked (§3 fingerprint)" if cg["supply_locked_flag"] else ""))
        print(f"- Price ${cg['price']:,.6f}  ·  24h {cg['chg_24h']:+.1f}% / 7d {cg['chg_7d']:+.1f}% / 30d {cg['chg_30d']:+.1f}%")
        for chain, addr in cg["contracts"].items():
            print(f"  - {chain}: {addr}")
    print()
    if p.get("layer1_onchain"):
        oc = p["layer1_onchain"]
        print(C.c("## Layer 1 — on-chain safe audit", "bold", "cyan"))
        res = oc.get("result") if isinstance(oc, dict) else None
        if oc.get("_error"):
            print(f"- {oc['_error']}")
        elif res:
            print(f"- {res.get('bias')}  ·  phase {res.get('phase')}  ·  score {res.get('score')}")
        else:
            print("- no tracked safes (untracked token)")
        print()
    print(C.c("## Layer 2 — cross-venue perp regime", "bold", "cyan"))
    render_regime_brief(p["layer2_regime"])
    print(C.c("\n## Layer 3 — liquidation magnets", "bold", "cyan"))
    m = p["layer3_magnets"]
    if m.get("error"):
        print(f"- {m['error']}")
    else:
        print(f"- Current {m['current']}  ·  upside {m['upside_magnets'][:3]}  ·  downside {m['downside_magnets'][:3]}")
    print(C.c("\n## Layer 4 — price structure", "bold", "cyan"))
    s = p["layer4_structure"]
    if s.get("error"):
        print(f"- {s['error']}")
    else:
        print(f"- Range pos {s['range_pos']}%  ·  squeeze pattern {s['squeeze_pattern']}  ·  structure {s['structure']['read']}"
              + (f"  ·  {s['vol_profile']['read']} volume" if s['vol_profile']['read'] else ""))
    print(C.c("\n## Layer 5 — catalyst/sentiment (manual)", "bold", "cyan"))
    print("- ZachXBT / listings / KOL narratives — not automated (§14).")
    print(C.c("\n## Synthesis (you do this)", "bold"))
    print("- Cat A/B (§3) · phase from funding (§2) · convergence (§7) · verdict STRONG/MILD/NEUTRAL/PASS.")


def render_regime_brief(r):
    for v in ("binance", "bybit", "aster"):
        b = r["funding"][v]
        print(f"- {v.title()}: {b['regime']}")
    if r["divergence"]:
        print(f"- ⚠ divergence Δ{r['divergence']['delta_pct']:+.3f}% (pull per-venue heatmaps, §8)")


def main():
    ap = argparse.ArgumentParser(description="Unified multi-layer pull")
    ap.add_argument("ticker")
    ap.add_argument("--cg-id", default=None)
    ap.add_argument("--onchain", nargs="?", const="1", default="",
                    help="include the slow on-chain layer (bare flag or any truthy value)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)
    include_onchain = str(args.onchain).lower() not in ("", "0", "false", "no")
    p = build_pull5(args.ticker, args.cg_id, include_onchain)
    if args.json:
        print(json.dumps(p))
    else:
        render_human(p)


if __name__ == "__main__":
    main()
