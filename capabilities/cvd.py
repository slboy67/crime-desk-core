#!/usr/bin/env python3
"""cvd.py — SPEC 42: spot-vs-perp CVD divergence with REAL spot-venue resolution (NATIVE).

The old parts-bin cvd_spot_perp.py leg consumed by analyse was Binance-centric: a
Bitget-primary or DEX-primary name got a structurally blind spot-CVD read that still
reported a verdict into the §4 neg-funding-LONG confluence gate. This native port:

  1. Resolves the token's REAL primary spot venue by 24h volume:
     Binance spot ↔ Bitget spot (largest 24h quote volume wins; Binance on a tie) →
     GeckoTerminal DEX pools (contract resolved from tracked config:
     config/tracked_wallets.json tokens[SYM].contracts, then config/watchlist.json
     contract_bsc) when neither CEX lists it.
  2. Computes spot CVD from THAT venue:
       binance — spot 1m klines (taker-buy quote-volume fields 7/10)
       bitget  — spot public fills (side = taker/aggressor side)
       dex     — GeckoTerminal pool trades (taker-side heuristic DECLARED below)
     Perp CVD comes from Binance futures 1m klines (parity with what analyse compared
     before — same buy−sell aggressor math, same divergence_verdict thresholds as the
     proven _oldrepo script).
  3. Degrades EXPLICIT (SPEC-25 doctrine): no spot read anywhere → spot_coverage
     "none" + verdict "UNAVAILABLE" (analyse treats the §4 CVD leg as UNKNOWN, never a
     silent fail-the-gate); venue resolved but window read failed → "partial"/NO_DATA.

DEX taker-side heuristic (DECLARED): GeckoTerminal's trade `kind` ("buy"/"sell") is the
swap direction relative to the pool's base token. AMM swaps are always taker-initiated
(there is no resting maker side on-chain), so `kind` maps 1:1 to the aggressor side:
kind=="buy" = taker bought the token from the pool.

  python3 capabilities/cvd.py SKYAI --json
  python3 capabilities/cvd.py LAB --min 60
"""
import argparse
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import colors as C

UA = {"User-Agent": "Mozilla/5.0 (crime-desk cvd)"}
GT_NET = {"binance-smart-chain": "bsc", "bsc": "bsc", "ethereum": "eth",
          "base": "base", "solana": "solana", "mantle": "mantle"}
DEX_TAKER_HEURISTIC = ("GeckoTerminal trade kind=buy|sell is the swap direction vs the pool's "
                       "base token; AMM swaps are always taker-initiated, so kind == taker side "
                       "(buy = taker bought the token).")
N_BUCKETS = 6


def fetch(url, timeout=15):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ---- contract resolution (tracked config, same sources onchain/pull5 use) ----

def resolve_contract(sym):
    """(gecko_network, contract) from config/tracked_wallets.json tokens[SYM].contracts,
    falling back to config/watchlist.json contract_bsc. None when unresolvable."""
    sym = sym.upper()
    try:
        tok = json.loads((ROOT / "config" / "tracked_wallets.json").read_text())["tokens"].get(sym) or {}
        for chain, addr in (tok.get("contracts") or {}).items():
            net = GT_NET.get(chain.lower())
            if net and str(addr).startswith("0x"):
                return net, str(addr).lower()
    except Exception:
        pass
    try:
        for tok in json.loads((ROOT / "config" / "watchlist.json").read_text()).get("tokens", []):
            if str(tok.get("ticker", "")).upper() == sym and tok.get("contract_bsc"):
                return "bsc", str(tok["contract_bsc"]).lower()
    except Exception:
        pass
    return None


# ---- 24h spot volume per venue (the venue-resolution gauge) ----

def binance_spot_24h(sym):
    d = fetch(f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}USDT")
    try:
        return float(d["quoteVolume"])
    except (KeyError, TypeError, ValueError):
        return None


def bitget_spot_24h(sym):
    d = fetch(f"https://api.bitget.com/api/v2/spot/market/tickers?symbol={sym}USDT")
    try:
        row = d["data"][0]
        return float(row.get("quoteVolume") or row.get("usdtVolume"))
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def resolve_spot_venue(sym):
    """Primary spot venue by 24h volume. Returns (venue, vol_24h_usd, dex_pool|None);
    venue ∈ 'binance' | 'bitget' | 'dex:<net>' | None. CEX with the larger 24h quote
    volume wins (Binance on a tie); DEX only when neither CEX has spot."""
    cands = [(v, n) for v, n in ((binance_spot_24h(sym) or 0, "binance"),
                                 (bitget_spot_24h(sym) or 0, "bitget")) if v > 0]
    if cands:
        vol, name = max(cands, key=lambda x: x[0])
        return name, vol, None
    rc = resolve_contract(sym)
    if rc:
        net, contract = rc
        pools = (fetch(f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{contract}/pools")
                 or {}).get("data") or []
        vols = []
        for p in pools:
            try:
                h24 = float(((p.get("attributes") or {}).get("volume_usd") or {}).get("h24") or 0)
                vols.append((h24, p["id"].split("_", 1)[-1]))
            except (KeyError, TypeError, ValueError):
                continue
        if vols:
            top = max(vols, key=lambda x: x[0])
            return f"dex:{net}", sum(v for v, _ in vols), top[1]
    return None, None, None


# ---- CVD math (parity with the proven _oldrepo cvd_spot_perp computation) ----

def _kline_cvd(bars):
    """Aggressor CVD from Binance kline rows (idx 7 = quote vol, idx 10 = taker-buy
    quote vol): buy = takerBuyQuote, sell = quote − takerBuyQuote."""
    if not isinstance(bars, list) or not bars:
        return None
    buy = sell = 0.0
    buckets = [0.0] * N_BUCKETS
    n = len(bars)
    for i, k in enumerate(bars):
        try:
            q, tb = float(k[7]), float(k[10])
        except (TypeError, ValueError, IndexError):
            continue
        buy += tb
        sell += q - tb
        buckets[min(N_BUCKETS - 1, i * N_BUCKETS // n)] += 2 * tb - q
    if buy == 0 and sell == 0:
        return None
    window = (float(bars[-1][0]) - float(bars[0][0])) / 60000.0 + 1
    return dict(buy=buy, sell=sell, cvd=buy - sell, buckets=buckets, n=n, window_min=window)


def _trades_cvd(rows, minutes):
    """rows = (ts_seconds, is_buy, usd). Same bucketed aggressor math as the old script."""
    cutoff = time.time() - minutes * 60
    rows = sorted((ts, usd if is_buy else -usd) for ts, is_buy, usd in rows if ts >= cutoff)
    if not rows:
        return None
    buy = sum(u for _, u in rows if u > 0)
    sell = -sum(u for _, u in rows if u < 0)
    t0, t1 = rows[0][0], rows[-1][0]
    span = max(1, t1 - t0)
    buckets = [0.0] * N_BUCKETS
    for ts, u in rows:
        buckets[min(N_BUCKETS - 1, int((ts - t0) / span * N_BUCKETS))] += u
    return dict(buy=buy, sell=sell, cvd=buy - sell, buckets=buckets, n=len(rows),
                window_min=(t1 - t0) / 60.0)


def spot_cvd_binance(sym, minutes):
    bars = fetch(f"https://api.binance.com/api/v3/klines?symbol={sym}USDT&interval=1m"
                 f"&limit={min(max(minutes, 1), 1000)}")
    return _kline_cvd(bars)


def spot_cvd_mexc(sym, minutes):
    """MEXC spot klines mirror the Binance array shape (idx 7 quote vol, idx 10
    taker-buy quote vol) — SPEC-191 #1."""
    bars = fetch(f"https://api.mexc.com/api/v3/klines?symbol={sym}USDT&interval=1m"
                 f"&limit={min(max(minutes, 1), 1000)}")
    return _kline_cvd(bars)


def spot_cvd_bitget(sym, minutes):
    d = fetch(f"https://api.bitget.com/api/v2/spot/market/fills?symbol={sym}USDT&limit=500")
    rows = (d or {}).get("data") if isinstance(d, dict) else None
    if not rows:
        return None
    try:
        return _trades_cvd([(int(t["ts"]) / 1000, t["side"] == "buy",
                             float(t["size"]) * float(t["price"])) for t in rows], minutes)
    except (KeyError, TypeError, ValueError):
        return None


def spot_cvd_bybit(sym, minutes):
    """Bybit spot klines don't carry a taker-buy split — recent-trades aggressor
    read instead (SPEC-191 #1)."""
    d = fetch(f"https://api.bybit.com/v5/market/recent-trade?category=spot&symbol={sym}USDT&limit=1000")
    rows = (((d or {}).get("result") or {}).get("list")) or []
    if not rows:
        return None
    try:
        return _trades_cvd([(float(t["time"]) / 1000, t["side"] == "Buy",
                             float(t["size"]) * float(t["price"])) for t in rows], minutes)
    except (KeyError, TypeError, ValueError):
        return None


def spot_cvd_okx(sym, minutes):
    """OKX candles don't carry a taker-buy split — recent-trades aggressor read
    instead (SPEC-191 #1)."""
    d = fetch(f"https://www.okx.com/api/v5/market/trades?instId={sym}-USDT&limit=500")
    rows = (d or {}).get("data") or []
    if not rows:
        return None
    try:
        return _trades_cvd([(float(t["ts"]) / 1000, t["side"] == "buy",
                             float(t["sz"]) * float(t["px"])) for t in rows], minutes)
    except (KeyError, TypeError, ValueError):
        return None


def spot_cvd_gate(sym, minutes):
    """Gate spot candlesticks don't carry a taker-buy split — recent-trades
    aggressor read instead (SPEC-191 #1)."""
    d = fetch(f"https://api.gateio.ws/api/v4/spot/trades?currency_pair={sym}_USDT&limit=1000")
    rows = d if isinstance(d, list) else []
    if not rows:
        return None
    try:
        return _trades_cvd([(float(t["create_time"]), t["side"] == "buy",
                             float(t["amount"]) * float(t["price"])) for t in rows], minutes)
    except (KeyError, TypeError, ValueError):
        return None


def spot_cvd_kucoin(sym, minutes):
    """KuCoin klines don't carry a taker-buy split — recent-trades (histories)
    aggressor read instead (SPEC-191 #1)."""
    d = fetch(f"https://api.kucoin.com/api/v1/market/histories?symbol={sym}-USDT")
    rows = (d or {}).get("data") or []
    if not rows:
        return None
    try:
        return _trades_cvd([(float(t["time"]) / 1e9, t["side"] == "buy",
                             float(t["size"]) * float(t["price"])) for t in rows], minutes)
    except (KeyError, TypeError, ValueError):
        return None


# ── SPEC-191 #1: aggregated multi-venue spot with an adaptive window ────────────────
CEX_SPOT_VENUES = ("binance", "bybit", "okx", "bitget", "gate", "mexc", "kucoin")
WINDOW_LADDER_MIN = (30, 60, 120, 240)
AGG_NOTIONAL_TARGET_USD = 1_000_000

_VENUE_SPOT_FN = {
    "binance": spot_cvd_binance, "bybit": spot_cvd_bybit, "okx": spot_cvd_okx,
    "bitget": spot_cvd_bitget, "gate": spot_cvd_gate, "mexc": spot_cvd_mexc,
    "kucoin": spot_cvd_kucoin,
}


VENUE_FETCH_TIMEOUT_S = 8   # per-venue wall-clock cap when aggregating (never sum sequentially)


def aggregate_spot_cvd(sym, minutes, venues=None, fetch_map=None):
    """Sum buy/sell across every keyless spot venue that has data for `sym` at this
    window. Venues are fetched CONCURRENTLY (a sequential sweep of 7 venues x up to 4
    window rungs could take minutes if even a couple time out — bounded here to
    roughly the slowest single venue, not the sum). Per-venue failure is loud
    (`venues_failed`, never silent) and never stops the rest of the sweep. Returns
    None when EVERY venue failed (the caller falls back to the single-venue/DEX
    resolution path); otherwise a dict shaped like the single-venue cvd dicts
    (`buy`/`sell`/`cvd`/`window_min`) plus
    `venues_used`/`venues_failed`/`spot_notional_usd`."""
    venues = list(venues) if venues is not None else list(CEX_SPOT_VENUES)
    fetch_map = fetch_map or _VENUE_SPOT_FN
    used, failed = [], []
    buy = sell = n = 0.0
    to_run = [(v, fetch_map[v]) for v in venues if fetch_map.get(v) is not None]
    ex = ThreadPoolExecutor(max_workers=max(1, len(to_run)))
    try:
        futs = {ex.submit(fn, sym, minutes): v for v, fn in to_run}
        for fut, v in futs.items():
            try:
                r = fut.result(timeout=VENUE_FETCH_TIMEOUT_S)
            except FuturesTimeout:
                failed.append({"venue": v, "reason": f"timeout>{VENUE_FETCH_TIMEOUT_S}s"})
                continue
            except Exception as e:  # noqa: BLE001 — one venue's failure never kills the sweep
                failed.append({"venue": v, "reason": str(e)[:160]})
                continue
            if not r:
                failed.append({"venue": v, "reason": "no_data"})
                continue
            buy += r["buy"]
            sell += r["sell"]
            n += r.get("n", 0)
            used.append(v)
    finally:
        ex.shutdown(wait=False)   # never wait on an abandoned/timed-out venue thread
    if not used:
        return None
    return {"buy": buy, "sell": sell, "cvd": buy - sell, "n": n, "window_min": minutes,
            "venues_used": sorted(used), "venues_failed": failed,
            "spot_notional_usd": buy + sell}


def spot_cvd_dex(net, pool, minutes):
    d = fetch(f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/{pool}/trades")
    rows = []
    for tr in (d or {}).get("data") or []:
        a = tr.get("attributes", {})
        try:
            ts = datetime.strptime(a["block_timestamp"], "%Y-%m-%dT%H:%M:%SZ") \
                         .replace(tzinfo=timezone.utc).timestamp()
            rows.append((ts, a.get("kind") == "buy", float(a.get("volume_in_usd") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return _trades_cvd(rows, minutes) if rows else None


def perp_cvd_binance(sym, minutes):
    bars = fetch(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}USDT&interval=1m"
                 f"&limit={min(max(minutes, 1), 1000)}")
    return _kline_cvd(bars)


def divergence_verdict(spot, perp, spot_24h=None):
    """Deterministic verdict label + reliability — thresholds ported verbatim from the
    proven _oldrepo divergence_verdict (the 2026-05-27 ALT noisy-spot lesson):
      spot window < $1M → UNRELIABLE_THIN_SPOT (unless 24h spot ≥ $5M → SPOT_REAL_WINDOW_THIN)
      spot < 15% of perp → UNRELIABLE_PERP_DRIVEN; < 30% → low_confidence."""
    if not (perp and spot):
        return dict(verdict="NO_DATA", reliable=False, spot_cvd=None, perp_cvd=None)
    sp, pe = spot["cvd"], perp["cvd"]
    spot_tot, perp_tot = spot["buy"] + spot["sell"], perp["buy"] + perp["sell"]
    base = dict(spot_cvd=round(sp), perp_cvd=round(pe), spot_tot=round(spot_tot),
                perp_tot=round(perp_tot), window_min=round(spot.get("window_min", 0), 1))
    if spot_24h is not None:
        base["spot_24h"] = round(spot_24h)
    if spot_tot < 1_000_000:
        if spot_24h is not None and spot_24h >= 5_000_000:
            return dict(verdict="SPOT_REAL_WINDOW_THIN", reliable=False, low_confidence=True, **base)
        return dict(verdict="UNRELIABLE_THIN_SPOT", reliable=False, **base)
    if perp_tot and spot_tot < 0.15 * perp_tot:
        return dict(verdict="UNRELIABLE_PERP_DRIVEN", reliable=False, **base)
    low_conf = bool(perp_tot and spot_tot < 0.30 * perp_tot)
    if sp > 0 and pe < 0:
        v = "BULLISH_DIVERGENCE"
    elif sp < 0 and pe > 0:
        v = "BEARISH_DIVERGENCE"
    elif sp > 0 and pe > 0:
        v = "ALIGNED_BULLISH"
    else:
        v = "ALIGNED_BEARISH"
    return dict(verdict=v, reliable=True, low_confidence=low_conf, **base)


def build_cvd(ticker, minutes=30):
    """One JSON-able dict: divergence verdict + spot_venue / spot_vol_24h_usd /
    spot_coverage (full|partial|none). Degrade-EXPLICIT: coverage 'none' carries
    verdict 'UNAVAILABLE' — the §4 leg is UNKNOWN, never a silent gate-fail.

    SPEC-191 #1: spot is now AGGREGATED across every keyless CEX spot venue that
    lists the pair (never a single-venue read), with an adaptive window that starts
    at `minutes` (rounded up to the ladder 30/60/120/240) and extends until the
    aggregated notional clears $1M or the 240min (4h) cap is hit. `cvd_detail`
    carries the window/notional/venues actually used so a "thin" verdict names WHY.
    Only when every CEX venue in the aggregate comes up empty does this fall back to
    the old single-venue-by-24h-volume resolution (which also covers the DEX-pool
    path — GeckoTerminal has no per-venue equivalent to aggregate across)."""
    sym = ticker.upper().replace("USDT", "")
    ladder = [w for w in WINDOW_LADDER_MIN if w >= minutes] or list(WINDOW_LADDER_MIN)
    agg, chosen_w = None, None
    for w in ladder:
        cand = aggregate_spot_cvd(sym, w)
        if cand:
            agg, chosen_w = cand, w
        if agg and agg["spot_notional_usd"] >= AGG_NOTIONAL_TARGET_USD:
            break

    if agg:
        perp = perp_cvd_binance(sym, chosen_w)
        out = dict(ticker=sym, spot_venue=",".join(agg["venues_used"]),
                   spot_coverage="full", spot_vol_24h_usd=None)
        out["cvd_detail"] = {
            "window_min": chosen_w,
            "spot_notional_usd": round(agg["spot_notional_usd"]),
            "venues_used": agg["venues_used"],
            "spot_cvd": round(agg["cvd"]),
            "perp_cvd": round(perp["cvd"]) if perp else None,
        }
        if agg["venues_failed"]:
            out["venues_failed"] = agg["venues_failed"]
        out.update(divergence_verdict(agg, perp))
        if out.get("verdict") == "UNRELIABLE_THIN_SPOT":
            cap_bit = " — still under $1M at the 4h cap" if chosen_w >= WINDOW_LADDER_MIN[-1] else ""
            out["note"] = (f"aggregated {len(agg['venues_used'])} venue(s) "
                           f"({', '.join(agg['venues_used'])}) at {chosen_w}min window, spot "
                           f"notional ${agg['spot_notional_usd']:,.0f}{cap_bit}")
        return out

    # ---- fallback: no CEX venue anywhere had data — single-venue-by-24h-volume /
    # DEX-pool resolution (unchanged from the pre-SPEC-191 read) ----
    perp = perp_cvd_binance(sym, minutes)
    venue, vol24, pool = resolve_spot_venue(sym)
    out = dict(ticker=sym, spot_venue=venue,
               spot_vol_24h_usd=round(vol24) if vol24 else None)
    if venue is None:
        out.update(spot_coverage="none", verdict="UNAVAILABLE", reliable=False,
                   spot_cvd=None, perp_cvd=round(perp["cvd"]) if perp else None,
                   note="no spot read anywhere (Binance/Bitget spot unlisted, no resolvable "
                        "DEX pool) — the §4 CVD leg is UNKNOWN, not bearish")
        return out
    if venue == "binance":
        spot = spot_cvd_binance(sym, minutes)
    elif venue == "bitget":
        spot = spot_cvd_bitget(sym, minutes)
    else:
        spot = spot_cvd_dex(venue.split(":", 1)[1], pool, minutes)
        out["taker_side_heuristic"] = DEX_TAKER_HEURISTIC
    out["spot_coverage"] = "full" if spot else "partial"
    out.update(divergence_verdict(spot, perp, spot_24h=vol24))
    if not spot:
        out["note"] = (f"spot venue {venue} resolved (24h ${vol24:,.0f}) but the trades/klines "
                       "window read failed — coverage partial, divergence not computable this run")
    return out


def render_human(r):
    print(C.c(f"═══ SPOT vs PERP CVD — {r['ticker']} ═══", "bold", "cyan"))
    cov = r.get("spot_coverage")
    v24 = r.get("spot_vol_24h_usd")
    print(f"spot venue: {r.get('spot_venue') or '—'}"
          + (f"  (${v24/1e6:.1f}M/24h)" if v24 else "") + f"  coverage: {cov}")
    style = {"BULLISH_DIVERGENCE": "green", "BEARISH_DIVERGENCE": "red",
             "UNAVAILABLE": "yellow"}.get(r["verdict"], "grey")
    print(C.c(f"verdict: {r['verdict']}", "bold", style))
    if r.get("spot_cvd") is not None:
        print(f"  spot CVD {r['spot_cvd']/1e6:+.2f}M (tot ${r.get('spot_tot', 0)/1e6:.2f}M)"
              f" · perp CVD {r['perp_cvd']/1e6:+.2f}M (tot ${r.get('perp_tot', 0)/1e6:.2f}M)"
              f" · window {r.get('window_min')}min")
    if r.get("taker_side_heuristic"):
        print(C.c(f"  DEX heuristic: {r['taker_side_heuristic']}", "grey"))
    if r.get("note"):
        print(C.c(f"  ⚠ {r['note']}", "yellow"))


def main():
    ap = argparse.ArgumentParser(description="spot-vs-perp CVD with real spot-venue resolution (SPEC 42)")
    ap.add_argument("ticker")
    ap.add_argument("--min", type=int, default=30, help="window minutes (default 30)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)
    r = build_cvd(args.ticker, args.min)
    if args.json:
        print(json.dumps(r))
    else:
        render_human(r)


if __name__ == "__main__":
    main()
