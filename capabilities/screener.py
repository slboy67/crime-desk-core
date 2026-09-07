#!/usr/bin/env python3
"""screener.py — BNB-chain DISCOVERY net by structural fingerprint (NATIVE).

Complements `scan` (cross-sectional funding over the whole perp universe): this
screens the BNB-chain ecosystem (the Cat A pipeline, §3) for the playbook's
STRUCTURAL fingerprints, keeping only perp-listed names. Two modes:

  pump          MID-CYCLE — already moving. Rewards locked float + FDV/MC + recent gain + churn.
  accumulation  PRE-CYCLE (Pattern A Phase 1) — rewards Cat A structure + QUIET price + churn.
                Hard-excludes anything already running.

build_screen(mode, min_score, pages, top) -> dict (pure-ish: net I/O).

  python3 capabilities/screener.py
  python3 capabilities/screener.py --mode accumulation --pages 3 --min-score 4 --json

Score is a structural-fingerprint PRIOR, not a verdict — confirm Cat A/B + the
relevant layers (clusters / flows / regime) before trading.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import colors as C
import sector_divergence as SD

REPO = HERE.parent
SNAP = REPO / "state" / "screener_vol.json"
MARKETS_CACHE = REPO / "state" / "screener_markets_cache.json"   # SPEC-174 #5
MARKETS_CACHE_TTL_S = 900        # 15 min
MARKETS_BACKOFF_BASE_S = 0.05    # exponential backoff base delay per 429 retry
MARKETS_MAX_RETRIES = 3          # per-page retry budget on a 429
UA = "Mozilla/5.0 (screener.py)"
CG = "https://api.coingecko.com/api/v3"

BLOCKLIST = {
    "BNB", "BTCB", "WBNB", "ETH", "BTC", "USDT", "USDC", "BUSD", "FDUSD", "DAI",
    "TUSD", "CAKE", "XRP", "ADA", "DOGE", "LTC", "TRX", "DOT", "LINK", "UNI",
    "WETH", "SOL", "AVAX", "MATIC", "SHIB", "PEPE", "USD1", "WBETH", "STETH",
}


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def binance_perps():
    try:
        d = get("https://fapi.binance.com/fapi/v1/exchangeInfo")
        return {s["baseAsset"].upper() for s in d.get("symbols", [])
                if s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}
    except Exception as e:
        sys.stderr.write(f"[screener] binance perp list failed: {e}\n")
        return set()


def bybit_perps():
    try:
        d = get("https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000")
        return {x["baseCoin"].upper() for x in d.get("result", {}).get("list", [])}
    except Exception as e:
        sys.stderr.write(f"[screener] bybit perp list failed: {e}\n")
        return set()


def _load_markets_cache(now=None):
    """The most recent on-disk cache of successfully-fetched market pages — the live-fetch
    failure fallback (SPEC-187: ANY age, not gated to `MARKETS_CACHE_TTL_S` — a rate limit
    or vendor outage must degrade to stale data, never zero rows, as long as SOME cache
    exists; the 2026-09-01 incident: both pages 429'd, no cache within the old 15-min TTL,
    `screener_degraded: coingecko_429` AND zero rows — "a rate limit shouldn't zero the
    layer"). Returns (rows, age_h) or (None, None) on a missing/corrupt file — age is
    always reported so staleness stays visible to the caller, never hidden (§3)."""
    try:
        d = json.loads(MARKETS_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(d, dict) or "ts" not in d or "rows" not in d:
        return None, None
    now = now if now is not None else time.time()
    return d["rows"], round((now - d["ts"]) / 3600, 2)


def _save_markets_cache(rows, now=None):
    try:
        MARKETS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        MARKETS_CACHE.write_text(json.dumps({"ts": now if now is not None else time.time(),
                                             "rows": rows}))
    except OSError:  # noqa: BLE001 — a cache-write failure must never block the sweep
        pass


def _fetch_page(url, timeout=20, max_retries=MARKETS_MAX_RETRIES,
                base_delay=MARKETS_BACKOFF_BASE_S, sleep_fn=None):
    """One page, retrying a 429 with exponential backoff (base, 2x, 4x, ...) up to
    `max_retries` attempts total before giving up; any other error/status fails
    immediately (never retried) — SPEC-174 #5."""
    sleep_fn = sleep_fn or time.sleep
    delay = base_delay
    last_err = None
    for attempt in range(max_retries):
        try:
            return get(url, timeout=timeout)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code != 429 or attempt == max_retries - 1:
                raise
            sleep_fn(delay)
            delay *= 2
    raise last_err  # pragma: no cover — loop always returns or raises above


def markets(pages, now=None, sleep_fn=None):
    """Returns (rows, errors, degraded, cache_age_h). SPEC-160 #3: a scanned==0 run must
    never be a bare "fetch_failed" label — `errors` carries the failing URL + status (HTTP
    status code for an HTTPError, else str(exception)) per page, so a dead/changed/
    rate-limited CoinGecko endpoint is diagnosable from the output alone, no re-derivation
    from stderr.

    SPEC-174 #5: each page retries a 429 with exponential backoff (`_fetch_page`) before
    it counts as a failure.

    SPEC-187: if the live fetch still comes up short (any page failed), the on-disk cache
    backfills `rows` regardless of its age (was gated to a 15-min TTL — a rate limit that
    outlasted 15 minutes still zeroed the layer entirely: "screener_degraded:
    coingecko_429, scanned=0" against CoinGecko outages that run longer than that, the
    2026-09-01 incident). `cache_age_h` always reports how old the fallback is so a stale
    read stays visibly stale; `degraded` names why (`coingecko_429`/`coingecko_error`,
    suffixed `_stale_cache` once the cache is older than `MARKETS_CACHE_TTL_S`) — surfaced
    by `build_screen` as `screener_degraded` + `markets_cache_age_h`. Only when NO cache
    exists at all does a failure still zero `rows` (build_screen's fetch_failed/NOTOK
    path, unchanged — no data really is no data, §3). A fully successful live fetch always
    refreshes the cache for the next run."""
    out, errors = [], []
    for p in range(1, pages + 1):
        url = (f"{CG}/coins/markets?vs_currency=usd&category=binance-smart-chain"
               f"&order=volume_desc&per_page=250&page={p}&price_change_percentage=24h,7d,30d")
        try:
            rows = _fetch_page(url, sleep_fn=sleep_fn)
            if isinstance(rows, list):
                out += rows
        except urllib.error.HTTPError as e:
            sys.stderr.write(f"[screener] coingecko markets page {p} failed: {e}\n")
            errors.append({"url": url, "status": e.code})
        except Exception as e:
            sys.stderr.write(f"[screener] coingecko markets page {p} failed: {e}\n")
            errors.append({"url": url, "status": str(e)})

    degraded, cache_age_h = None, None
    if errors:
        base = ("coingecko_429" if any(e.get("status") == 429 for e in errors)
               else "coingecko_error")
        cached, age_h = _load_markets_cache(now=now)
        if cached is not None:
            out = cached
            cache_age_h = age_h
            stale = age_h > (MARKETS_CACHE_TTL_S / 3600)
            degraded = f"{base}_stale_cache" if stale else base
            sys.stderr.write(f"[screener] falling back to cached markets pages "
                             f"({degraded}, {age_h}h old)\n")
        else:
            degraded = base
            sys.stderr.write(f"[screener] no markets cache available — degraded ({degraded}), "
                             f"scanned={len(out)}\n")
    elif out:
        _save_markets_cache(out, now=now)
    return out, errors, degraded, cache_age_h


def load_vol_baseline():
    """Returns (vol_dict, age_hours, reason). `reason` is None when the baseline is
    usable; otherwise it names WHY (`baseline_missing`, `baseline_stale_<h>h`,
    `baseline_too_fresh_<h>h`) — SPEC-137: a null age must never be silently folded
    into a clean-looking result, so the caller always gets the cause."""
    try:
        d = json.loads(SNAP.read_text())
        ts = d.get("ts")
        if not ts:
            return {}, None, "baseline_missing"
    except Exception:
        return {}, None, "baseline_missing"
    age_h = (time.time() - ts) / 3600
    if age_h >= 96:
        return {}, None, f"baseline_stale_{age_h:.0f}h"
    if age_h <= 3:
        return {}, None, f"baseline_too_fresh_{age_h:.1f}h"
    return d.get("vol", {}), round(age_h, 1), None


def save_vol_baseline(coins):
    try:
        if time.time() - json.loads(SNAP.read_text()).get("ts", 0) < 3 * 3600:
            return
    except Exception:
        pass
    try:
        SNAP.parent.mkdir(parents=True, exist_ok=True)
        SNAP.write_text(json.dumps({"ts": time.time(),
            "vol": {c["id"]: c.get("total_volume") or 0 for c in coins}}))
    except Exception:
        pass


def _common(c, bn, bb, min_vol):
    sym = (c.get("symbol") or "").upper()
    mc = c.get("market_cap") or 0
    vol = c.get("total_volume") or 0
    if sym in BLOCKLIST or vol < min_vol or mc < 15_000_000:
        return None
    venues = [v for v, s in (("BIN", bn), ("BYB", bb)) if sym in s]
    if not venues:
        return None
    return c, venues


def _metrics(c, venues, pts, why):
    sym = (c.get("symbol") or "").upper()
    mc = c.get("market_cap") or 0
    fdv = c.get("fully_diluted_valuation") or 0
    circ = c.get("circulating_supply") or 0
    tot = c.get("total_supply") or c.get("max_supply") or 0
    return {
        "ticker": sym, "score": pts, "why": why, "venues": "+".join(venues),
        "mc": mc, "fdv_mc": round(fdv / mc, 2) if (fdv and mc) else None,
        "circ_ratio": round(circ / tot, 3) if tot else None,
        "vol": c.get("total_volume") or 0,
        "ch7": c.get("price_change_percentage_7d_in_currency"),
        "ch30": c.get("price_change_percentage_30d_in_currency"),
    }


def score_pump(c, bn, bb, _baseline):
    g = _common(c, bn, bb, 10_000_000)
    if not g:
        return None
    c, venues = g
    mc = c["market_cap"] or 0
    fdv = c.get("fully_diluted_valuation") or 0
    circ = c.get("circulating_supply") or 0
    tot = c.get("total_supply") or c.get("max_supply") or 0
    ch7 = c.get("price_change_percentage_7d_in_currency")
    ch30 = c.get("price_change_percentage_30d_in_currency")
    pts, why = 0, []
    cr = (circ / tot) if tot else None
    if cr is not None:
        if cr < 0.15: pts += 3; why.append(f"float {cr*100:.0f}% (≥85% locked)")
        elif cr < 0.25: pts += 2; why.append(f"float {cr*100:.0f}% (locked)")
        elif cr < 0.40: pts += 1; why.append(f"float {cr*100:.0f}%")
    fm = (fdv / mc) if (fdv and mc) else None
    if fm is not None:
        if fm > 8: pts += 2; why.append(f"FDV/MC {fm:.1f}×")
        elif fm > 4: pts += 1; why.append(f"FDV/MC {fm:.1f}×")
    if ch7 is not None:
        if ch7 > 50: pts += 2; why.append(f"7d +{ch7:.0f}%")
        elif ch7 > 20: pts += 1; why.append(f"7d +{ch7:.0f}%")
    if ch30 is not None:
        if ch30 > 200: pts += 2; why.append(f"30d +{ch30:.0f}%")
        elif ch30 > 100: pts += 1; why.append(f"30d +{ch30:.0f}%")
    if (vm := (c.get("total_volume") or 0) / mc if mc else 0) > 1.0:
        pts += 1; why.append(f"vol/MC {vm:.1f}× (churn)")
    return _metrics(c, venues, pts, why)


def score_accumulation(c, bn, bb, baseline):
    g = _common(c, bn, bb, 8_000_000)
    if not g:
        return None
    c, venues = g
    mc = c["market_cap"] or 0
    fdv = c.get("fully_diluted_valuation") or 0
    vol = c.get("total_volume") or 0
    circ = c.get("circulating_supply") or 0
    tot = c.get("total_supply") or c.get("max_supply") or 0
    ch24 = c.get("price_change_percentage_24h") or 0
    ch7 = c.get("price_change_percentage_7d_in_currency")
    ch30 = c.get("price_change_percentage_30d_in_currency") or 0
    ath_chg = c.get("ath_change_percentage")
    if ch24 > 25 or (ch7 or 0) > 60:
        return None
    pts, why = 0, []
    cr = (circ / tot) if tot else None
    if cr is not None:
        if cr < 0.15: pts += 3; why.append(f"float {cr*100:.0f}% (≥85% locked)")
        elif cr < 0.25: pts += 2; why.append(f"float {cr*100:.0f}% (locked)")
        elif cr < 0.40: pts += 1; why.append(f"float {cr*100:.0f}%")
    fm = (fdv / mc) if (fdv and mc) else None
    if fm is not None:
        if fm > 8: pts += 2; why.append(f"FDV/MC {fm:.1f}×")
        elif fm > 4: pts += 1; why.append(f"FDV/MC {fm:.1f}×")
    if ch7 is not None and -20 <= ch7 <= 25 and ch30 <= 120:
        pts += 2; why.append(f"quiet (7d {ch7:+.0f}% / 30d {ch30:+.0f}%)")
    vm = vol / mc if mc else 0
    if vm > 0.6: pts += 2; why.append(f"churn vol/MC {vm:.1f}×")
    elif vm > 0.3: pts += 1; why.append(f"churn vol/MC {vm:.1f}×")
    if ath_chg is not None and ath_chg < -55:
        pts += 1; why.append(f"{ath_chg:.0f}% off ATH (reload zone)")
    vp = baseline.get(c.get("id"))
    if vp and vol > vp * 1.3:
        pts += 1; why.append(f"vol +{(vol/vp-1)*100:.0f}% vs last scan")
    return _metrics(c, venues, pts, why)


def _watchlist():
    try:
        return {t["ticker"].upper() for t in
                json.loads((REPO / "config" / "watchlist.json").read_text())["tokens"]}
    except Exception:
        return set()


def _bsc_basket_def(cfg):
    """The one basket whose cg_category matches screener's own universe (binance-smart-chain)
    — screener already holds that exact page, so the divergence read costs no extra fetch."""
    for name, bdef in (cfg.get("baskets") or {}).items():
        if bdef.get("cg_category") == "binance-smart-chain":
            return name, bdef
    return None, None


def _apply_sector_tags(scored, coins):
    """SPEC-114: sector-divergence Cat A prior. Cheap path only — screener's `coins` page
    IS the bsc-microcap basket, so no new network call. Baskets the candidate also belongs
    to (ai-agents, meme, ...) aren't in hand here and are left to classify/triage/screener's
    other callers; tag is omitted entirely when unmapped (regression: byte-identical)."""
    try:
        cfg = SD.load_config()
        basket_name, bdef = _bsc_basket_def(cfg)
        if not bdef:
            return
        stats = SD.basket_stats([c.get("price_change_percentage_7d_in_currency")
                                 for c in coins if c.get("price_change_percentage_7d_in_currency") is not None])
        mapping = cfg.get("token_baskets") or {}
        for m in scored:
            names = mapping.get(m["ticker"].upper())
            if not names or basket_name not in names or m.get("ch7") is None:
                continue
            entry = SD.classify_divergence(m["ch7"], stats, cfg["thresholds"])
            entry.update({"label": bdef.get("label", basket_name), "window_days": 7})
            tag = SD.format_tag(entry)
            if tag:
                m["sector"] = tag
    except Exception:  # noqa: BLE001 — a prior must never break the screen
        pass


def build_screen(mode="pump", min_score=3, pages=2, top=25):
    """Pure-ish compute → {mode, scanned, perp_universe, candidates[], baseline_age_h,
    status, reason?, screener_degraded?, markets_cache_age_h?}. SPEC-137: a missing/stale
    baseline or a scanned==0 fetch failure must never look like a clean empty sweep —
    either flips status to "NOTOK" with a named reason, distinct from a genuine
    "scanned N>0, 0 matches" NONE. SPEC-187: `markets_cache_age_h` (present only alongside
    `screener_degraded`) says how old the cache-fallback rows are — a rate limit degrades
    to stale data, it does not zero the layer."""
    bn, bb = binance_perps(), bybit_perps()
    coins, fetch_errors, markets_degraded, markets_cache_age_h = markets(pages)
    baseline, base_age, base_reason = load_vol_baseline()
    save_vol_baseline(coins)
    wl = _watchlist()
    scorer = score_accumulation if mode == "accumulation" else score_pump

    scored = [r for r in (scorer(c, bn, bb, baseline) for c in coins) if r and r["score"] >= min_score]
    scored.sort(key=lambda r: r["score"], reverse=True)
    for m in scored:
        m["on_watchlist"] = m["ticker"] in wl
    _apply_sector_tags(scored, coins)

    reason = "fetch_failed" if len(coins) == 0 else base_reason
    out = {
        "mode": mode, "scanned": len(coins),
        "perp_universe": {"binance": len(bn), "bybit": len(bb)},
        "min_score": min_score, "baseline_age_h": base_age,
        "candidates": scored[:top],
        "status": "NOTOK" if reason else "OK",
    }
    if reason:
        out["reason"] = reason
    if reason == "fetch_failed" and fetch_errors:
        out["fetch_errors"] = fetch_errors
    # SPEC-174 #5: loud even when the cache fallback kept `scanned` non-zero (reason stays
    # unset in that case) — a degraded-but-usable sweep must never look like a clean one.
    # SPEC-187: `markets_cache_age_h` names HOW stale the fallback is (§3 — degraded output
    # must say which layer is stale, never just that something is wrong).
    if markets_degraded:
        out["screener_degraded"] = markets_degraded
        out["markets_cache_age_h"] = markets_cache_age_h
    return out


def fnum(n):
    if n is None:
        return "—"
    for u, d in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= u:
            return f"${n/u:.1f}{d}"
    return f"${n:.0f}"


def render_human(s):
    if s.get("status") == "NOTOK":
        print(C.c(f"⚠ NOTOK — {s.get('reason')} — this is NOT a clean empty sweep, do not read it as one", "bold", "red"))
    if s.get("screener_degraded"):
        age_txt = (f" (cache {s['markets_cache_age_h']}h old)"
                  if s.get("markets_cache_age_h") is not None else "")
        print(C.c(f"⚠ DEGRADED — {s['screener_degraded']}{age_txt} — markets pulled from a "
                  "cached page set (or came up short), not a fresh live fetch", "bold", "yellow"))
    label = "early-accumulation (Pattern A Phase 1)" if s["mode"] == "accumulation" else "mid-cycle pump"
    print(f"# Screener — {label} — {s['scanned']} BNB-chain coins, "
          f"{len(s['candidates'])} candidates ≥ score {s['min_score']}\n")
    print(f"Perp universe: Binance {s['perp_universe']['binance']} · Bybit {s['perp_universe']['bybit']}"
          + (f" · vol baseline {s['baseline_age_h']}h old" if s["baseline_age_h"] else ""))
    print("\n| # | Ticker | Score | Perp | MC | FDV/MC | Float | 7d | 30d | Vol | Fingerprints |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, m in enumerate(s["candidates"], 1):
        tag = " ⭐WL" if m["on_watchlist"] else ""
        fl = f"{m['circ_ratio']*100:.0f}%" if m["circ_ratio"] is not None else "—"
        fm = f"{m['fdv_mc']:.1f}×" if m["fdv_mc"] is not None else "—"
        c7 = f"{m['ch7']:+.0f}%" if m["ch7"] is not None else "—"
        c30 = f"{m['ch30']:+.0f}%" if m["ch30"] is not None else "—"
        dot = "🔴" if m["score"] >= 6 else "🟠" if m["score"] >= 4 else "⚪"
        print(f"| {i} | **{m['ticker']}**{tag} | {dot} {m['score']} | {m['venues']} | {fnum(m['mc'])} "
              f"| {fm} | {fl} | {c7} | {c30} | {fnum(m['vol'])} | {', '.join(m['why'])} |")
    print("\n⭐WL = already on watchlist. Score is a structural-fingerprint PRIOR, not a verdict — "
          "confirm Cat A/B + clusters/flows/regime before trading.")


def main():
    ap = argparse.ArgumentParser(description="BNB-chain structural-fingerprint screener")
    ap.add_argument("--mode", choices=["pump", "accumulation"], default="pump")
    ap.add_argument("--min-score", type=int, default=3)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)
    s = build_screen(args.mode, args.min_score, args.pages, args.top)
    if args.json:
        print(json.dumps(s))
    else:
        render_human(s)


if __name__ == "__main__":
    main()
