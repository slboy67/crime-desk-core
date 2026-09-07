#!/usr/bin/env python3
"""regime_check.py — cross-venue derivative regime checker (NATIVE).

Funding + OI for a perp across Binance / Bybit / Aster, with regime classification
per CLAUDE.md §2 (the phase-dependent funding table), cross-venue divergence (§8),
and funding/OI z-scores vs this token's OWN ~30-40D distribution (relative-
extremeness, not hardcoded thresholds).

build_regime(sym) -> dict (pure) ; render_human(r) (default markdown view).

  python3 capabilities/regime_check.py LAB
  python3 capabilities/regime_check.py LAB --json

NOTE: distinct from `regime_flip` (live-vs-stored single-funding drift, the board
dep). This is the full multi-venue funding/OI table. Funding values are per-interval %.

SPEC 18: each venue's `funding[v].latest` (and the cross-venue `divergence`) is the
LIVE PREDICTED rate — Bybit `tickers.fundingRate`, Binance/Aster
`premiumIndex.lastFundingRate` — NOT the last SETTLED print, which lags and hid the
§5 short-veto (EDEN settled +0.005% vs live −1.72%). The settled funding/history
series is retained ONLY as the z-score sample (`funding[v].z`). Live is prepended to
the series so the block + divergence read it as the head; a live-fetch failure
degrades to the settled head, never crashing a token.

SPEC 19: the settled HISTORY also gets the SPEC-10 floor treatment. A value at the
0.005% venue floor (`_is_floor_pct`) is a placeholder, not a real settled rate — it is
excluded from the z-score sample and `regime_hint`'s trend windows and rendered `null`
in `history`. ALL-floor series = genuinely flat (`all_floor`, kept distinct from
'unknown'); a mix of real + floor must not let floors distort the trend. `regime_hint`
gained a NEGATIVE-cooling branch (deep-neg easing toward flat) and an
'insufficient real history' guard when too few real points remain for a trend call.
"""
import argparse
import json
import statistics
import sys
import urllib.request
from urllib.error import URLError, HTTPError

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import colors as C

TIMEOUT = 10
UA = {"User-Agent": "regime-check/1.0"}


def fetch(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except (URLError, HTTPError, json.JSONDecodeError, TimeoutError) as e:
        return {"_error": str(e)}


def fmt_pct(x):  # x is already a percent
    return "—" if x is None else C.sign_color(f"{x:+.4f}%", x, flip=True)


def fmt_oi(x):
    if x is None:
        return "—"
    if x >= 1_000_000:
        return f"{x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"{x/1_000:.1f}K"
    return f"{x:.0f}"


FLOOR_PCT = 0.005   # SPEC 19: venue base-rate FLOOR (raw 0.00005 ×100) — a placeholder the
                    # settled endpoint returns for low-activity intervals, NOT a real rate (cf. SPEC 10).


def _is_floor_pct(x):
    """SPEC 19: True if a %-funding value is the venue floor sentinel (±0.005%). Mirrors
    regime_flip._is_floor at the %-scale so the history/z/trend path drops it like classify does."""
    return x is not None and abs(abs(x) - FLOOR_PCT) < 1e-6


def zscore(value, sample):
    n = len(sample) if sample else 0
    if n < 10:
        return None, None, None, n
    mean = statistics.mean(sample)
    std = statistics.stdev(sample)
    if std == 0:
        return None, mean, std, n
    return (value - mean) / std, mean, std, n


# ---- per-venue pulls (raw decimal funding) ----
def _binance(s):
    return {
        "funding": fetch(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={s}&limit=120"),
        "oi_hist": fetch(f"https://fapi.binance.com/futures/data/openInterestHist?symbol={s}&period=1h&limit=24"),
        "oi_30d": fetch(f"https://fapi.binance.com/futures/data/openInterestHist?symbol={s}&period=1d&limit=30"),
    }


def _bybit(s):
    return {
        "funding": fetch(f"https://api.bybit.com/v5/market/funding/history?category=linear&symbol={s}&limit=120"),
        "oi_hist": fetch(f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={s}&intervalTime=1h&limit=24"),
        "oi_30d": fetch(f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={s}&intervalTime=1d&limit=30"),
    }


def _aster(s):
    return {
        "funding": fetch(f"https://fapi.asterdex.com/fapi/v1/fundingRate?symbol={s}&limit=120"),
        "oi_now": fetch(f"https://fapi.asterdex.com/fapi/v1/openInterest?symbol={s}"),
    }


# SPEC 26: Bitget — a PRIMARY venue for cluster names (its OI ≈ Binance, it's the desk's execution
# venue) but previously absent from the cross-venue funding/OI/price reads → the §5 veto was blind
# to a venue carrying ~half the OI.
def _bitget(s):
    base = "https://api.bitget.com/api/v2/mix/market"
    return {
        "funding_hist": fetch(f"{base}/history-fund-rate?symbol={s}&productType=usdt-futures&pageSize=120"),
        "current": fetch(f"{base}/current-fund-rate?symbol={s}&productType=usdt-futures"),
        "oi": fetch(f"{base}/open-interest?symbol={s}&productType=usdt-futures"),
        "ticker": fetch(f"{base}/ticker?symbol={s}&productType=usdt-futures"),
    }


def _bg_funding(d):
    """Bitget history-fund-rate → settled rates, NEWEST-FIRST (matches the other parsers)."""
    lst = d.get("funding_hist", {}).get("data", []) if isinstance(d.get("funding_hist"), dict) else []
    return [float(x["fundingRate"]) for x in lst] if lst else []


def _bg_interval_min(d):
    """Bitget funding interval (minutes) from current-fund-rate.fundingRateInterval (hours)."""
    try:
        data = d.get("current", {}).get("data") if isinstance(d.get("current"), dict) else None
        return int(float(data[0]["fundingRateInterval"])) * 60 if data else None
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def _bg_price(d):
    try:
        data = d.get("ticker", {}).get("data") if isinstance(d.get("ticker"), dict) else None
        return float(data[0]["lastPr"]) if data and data[0].get("lastPr") else None
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def _bg_oi_usd(d):
    """Bitget OI in USD = base size × last price."""
    try:
        oi = d.get("oi", {}).get("data", {}).get("openInterestList") if isinstance(d.get("oi"), dict) else None
        px = _bg_price(d)
        return float(oi[0]["size"]) * px if oi and px else None
    except (KeyError, ValueError, TypeError, IndexError):
        return None


VETO_4H = -0.30   # §5 deep-neg short veto threshold, %/4h


def _interval_min_from_times(times_ms):
    """Funding interval (minutes) from two consecutive settle timestamps (ms)."""
    ts = sorted(int(t) for t in times_ms if t not in (None, ""))
    return round((ts[-1] - ts[-2]) / 60000) if len(ts) >= 2 else None


def _to_4h(latest_pct, interval_min):
    """Normalize a %/interval rate to %/4h (SPEC 11). Default 240min (4h) if interval unknown."""
    if latest_pct is None:
        return None
    return round(latest_pct * 240 / (interval_min or 240), 4)


def _cross_venue_veto(rates_4h):
    """SPEC 13/26 — §5 veto across ALL covered venues (the MORE-VETOING wins), incl. Bitget.
    rates_4h = {venue: fr_4h|None}. veto = any covered venue ≤ −0.30%/4h; funding_split = venues
    straddle the veto line."""
    vals = {v: r for v, r in rates_4h.items() if r is not None}
    if not vals:
        return {"min_4h": None, "min_venue": None, "veto": False, "funding_split": False, "per_venue_4h": {}}
    mv = min(vals, key=vals.get)
    veto = vals[mv] <= VETO_4H
    split = any(r <= VETO_4H for r in vals.values()) and any(r > VETO_4H for r in vals.values())
    return {"min_4h": round(vals[mv], 4), "min_venue": mv, "veto": veto,
            "funding_split": split, "per_venue_4h": {v: round(r, 4) for v, r in vals.items()}}


def _live_funding_pct(venue, s):
    """SPEC 18: LIVE predicted funding rate (%/interval) — NOT the last SETTLED print (which
    lags and hid the §5 short-veto on EDEN). Bybit: tickers.fundingRate; Binance/Aster:
    premiumIndex.lastFundingRate. None on failure → caller keeps the settled head."""
    try:
        if venue == "bybit":
            d = fetch(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={s}")
            lst = d.get("result", {}).get("list", []) if isinstance(d, dict) else []
            if lst and lst[0].get("fundingRate") not in (None, ""):
                return float(lst[0]["fundingRate"]) * 100
            return None
        if venue == "bitget":                             # SPEC 26: live predicted = current-fund-rate
            d = fetch(f"https://api.bitget.com/api/v2/mix/market/current-fund-rate?symbol={s}&productType=usdt-futures")
            data = d.get("data") if isinstance(d, dict) else None
            if data and data[0].get("fundingRate") not in (None, ""):
                return float(data[0]["fundingRate"]) * 100
            return None
        host = "fapi.binance.com" if venue == "binance" else "fapi.asterdex.com"
        d = fetch(f"https://{host}/fapi/v1/premiumIndex?symbol={s}")
        if isinstance(d, dict) and d.get("lastFundingRate") not in (None, ""):
            return float(d["lastFundingRate"]) * 100
    except (KeyError, ValueError, TypeError, IndexError):
        return None
    return None


def _prepend_live(rates_pct, live_pct):
    """SPEC 18: make the LIVE predicted rate the series head (the 'latest'); the settled
    series stays intact as the z-score SAMPLE. Degrade to the settled head if live is None."""
    return ([live_pct] + rates_pct) if live_pct is not None else rates_pct


def _bn_funding(d):
    f = d.get("funding")
    return [float(x["fundingRate"]) for x in reversed(f)] if isinstance(f, list) and f else []


def _by_funding(d):
    f = d.get("funding", {}).get("result", {}).get("list", []) if isinstance(d.get("funding"), dict) else []
    return [float(x["fundingRate"]) for x in f] if f else []


def _bn_oi30(d):
    v = d.get("oi_30d")
    return [float(x["sumOpenInterest"]) for x in v] if isinstance(v, list) and v else []


def _by_oi30(d):
    v = d.get("oi_30d", {}).get("result", {}).get("list", []) if isinstance(d.get("oi_30d"), dict) else []
    return [float(x["openInterest"]) for x in reversed(v)] if v else []


def regime_hint(latest_pct, history_pct, all_floor=False):
    """Classify funding regime per §2. Trend-aware (transitions, not just snapshots). Inputs in %.
    SPEC 19: `history_pct` is the REAL (floor-stripped) latest-first series; `all_floor=True` means
    every reading was the venue floor → genuinely ~0% (kept distinct from 'unknown', cf. SPEC 13/10)."""
    if all_floor:
        return f"near flat (venue floor ~{(latest_pct or 0):+.3f}%) — genuinely ~0%, no real funding signal"
    if latest_pct is None:
        return "no data"
    if not history_pct:
        return "insufficient real history (all floor/null) — trend unknown"
    pct = latest_pct
    if len(history_pct) >= 4:
        recent = history_pct[:2]
        older = history_pct[2:5]
        ra = sum(recent) / len(recent)
        oa = sum(older) / len(older)
        if oa > 0.05 and ra < -0.01:
            return f"POS→NEG FLIP ({oa:+.3f}% → {ra:+.3f}%) — Cat A breakdown trigger signature"
        if oa < -0.05 and ra > 0.01:
            return f"NEG→POS FLIP ({oa:+.3f}% → {ra:+.3f}%) — squeeze firing or fired"
        # SPEC 19: deep-neg easing UP toward flat (less negative, still negative). Without this the
        # contaminated EDEN series read as a false "flat then spiked"; the real trend was cooling.
        if oa < -0.05 and ra > oa + 0.02 and ra < -0.01:
            return f"NEGATIVE cooling toward flat ({oa:+.3f}% → {ra:+.3f}%) — deep-neg easing toward the §5 veto line"
        if oa > 0.03 and abs(ra) < abs(oa) * 0.5:
            return f"POSITIVE cooling ({oa:+.3f}% → {ra:+.3f}%) — Cat B top OR Cat A breakdown forming"
        if oa < -0.05 and ra < oa:
            return f"NEGATIVE deepening ({oa:+.3f}% → {ra:+.3f}%) — Cat A trap-formation loading"
    # snapshot (too few real points for a trend, or no trend pattern matched)
    insuf = " — insufficient real history for trend" if len(history_pct) < 4 else ""
    if pct < -1.0:
        return f"DEEPLY NEGATIVE ({pct:+.3f}%) — Cat A trap-formation pre-squeeze signature{insuf}"
    if pct < -0.1:
        return f"negative ({pct:+.3f}%) — shorts paying{insuf}"
    if abs(pct) <= 0.05:
        return f"near flat ({pct:+.3f}%) — transition zone{insuf}"
    if pct > 0.5:
        return f"POSITIVE heavy ({pct:+.3f}%) — longs paying (Cat B pump OR Cat A distribution top){insuf}"
    return f"moderately positive ({pct:+.3f}%) — longs paying{insuf}"


def _venue_funding_block(rates_pct):
    """{latest, history[7], regime, z, n_real, all_floor} from a %-rate list (latest first; the
    head is the SPEC-18 live latest). SPEC 19: the 0.005% venue floor is a placeholder — it is
    excluded from the z-score sample and the regime trend windows, and rendered null in `history`
    (mirror SPEC 10). ALL-floor series = genuinely flat (the SPEC 13/10 distinction is kept)."""
    latest = rates_pct[0] if rates_pct else None
    non_floor = [x for x in rates_pct if not _is_floor_pct(x)]
    all_floor = bool(rates_pct) and not non_floor
    # trend series = real points only, but ALWAYS keep the live latest as the head (SPEC 18), even
    # if it sits at the floor — it's a real predicted reading, not a settled placeholder.
    trend = non_floor if (latest is None or not _is_floor_pct(latest)) else [latest] + non_floor
    sample = [x for x in rates_pct[1:] if not _is_floor_pct(x)]   # real settled points = the z sample
    z = {"latest": latest, "mean": None, "std": None, "z": None, "n": len(sample)}
    if latest is not None and len(sample) >= 10:
        zz, mean, std, n = zscore(latest, sample)
        z = {"latest": latest, "mean": mean, "std": std, "z": round(zz, 2) if zz is not None else None, "n": n}
    history = [None if _is_floor_pct(x) else x for x in rates_pct[:7]]   # floors → null/stale
    return {
        "latest": latest,
        "history": history,
        "regime": regime_hint(latest, trend, all_floor=all_floor),
        "z": z,
        "n_real": len(non_floor),
        "all_floor": all_floor,
    }


# ---- SPEC 23: cross-venue LONG/SHORT positioning leg ----
# Binance is the authoritative source (the only one exposing an L/S ratio for thin crime-coins,
# PROBED LIVE 2026-06-04). Bybit best-effort secondary (empty list for thin symbols → unavailable);
# Bitget/Aster expose nothing usable → always unavailable. NEVER fabricate a ratio (SPEC 10/18 lesson).
LS_WINDOW = 12          # last N × 1h points → a trend/Δ% is computable, not just a snapshot
LS_DROP_PCT = 15.0      # §6: top-trader L/S dropping ≥15% (toward ≥25%) = trapped-longs capitulating
LS_CROWD_LONG = 60.0    # long% ≥ this = long-crowded (the bags; short-side uncrowded = §7 squeeze fuel)
LS_CROWD_SHORT = 40.0   # long% ≤ this = short-crowded (with the crowd on a short = late/bait, size down)


def _binance_ls(s, limit=LS_WINDOW):
    """Binance futures/data L/S cohorts. Each endpoint returns an ASCENDING list
    [{longAccount, shortAccount, longShortRatio, timestamp}, …] (oldest→newest)."""
    base = "https://fapi.binance.com/futures/data"
    return {
        "top_position": fetch(f"{base}/topLongShortPositionRatio?symbol={s}&period=1h&limit={limit}"),
        "top_account": fetch(f"{base}/topLongShortAccountRatio?symbol={s}&period=1h&limit={limit}"),
        "global": fetch(f"{base}/globalLongShortAccountRatio?symbol={s}&period=1h&limit={limit}"),
    }


def _bybit_ls(s, limit=LS_WINDOW):
    """Bybit account L/S — best-effort. retCode:0 but `list` is empty for thin symbols."""
    return fetch(f"https://api.bybit.com/v5/market/account-ratio?category=linear&symbol={s}&period=1h&limit={limit}")


def _ls_cohort(series, venue="binance"):
    """Parse one Binance L/S cohort series → latest ratio + long/short% + window trend Δ%.
    Empty/error → unavailable (NEVER a fabricated 0/flat, the SPEC 10/18 floor-sentinel lesson)."""
    if not (isinstance(series, list) and series):
        return {"available": False, "ratio": None, "reason": "no data / empty list"}
    try:
        pts = [(float(x["longShortRatio"]), float(x["longAccount"]), float(x["shortAccount"]))
               for x in series]
    except (KeyError, ValueError, TypeError):
        return {"available": False, "ratio": None, "reason": "unparseable"}
    latest, first = pts[-1], pts[0]
    trend = round((latest[0] - first[0]) / first[0] * 100, 2) if first[0] else None
    return {"available": True, "venue": venue, "ratio": round(latest[0], 4),
            "long_pct": round(latest[1] * 100, 2), "short_pct": round(latest[2] * 100, 2),
            "trend_pct": trend, "n": len(pts), "window_first_ratio": round(first[0], 4)}


def _ls_signals(cohorts):
    """§6 short-fire (top-trader L/S drops ≥15% over the window) + §7 crowdedness read.
    Top-trader = top_position (most decision-relevant, position-weighted) + top_account."""
    top = {k: cohorts[k] for k in ("top_position", "top_account")
           if cohorts.get(k, {}).get("available") and cohorts[k].get("trend_pct") is not None}
    worst_cohort, worst = None, None
    for k, c in top.items():
        if worst is None or c["trend_pct"] < worst:
            worst, worst_cohort = c["trend_pct"], k
    short_fire = worst is not None and worst <= -LS_DROP_PCT
    # crowdedness from the most decision-relevant available cohort (prefer top_position → top_account → global)
    ref = next((cohorts[k] for k in ("top_position", "top_account", "global")
                if cohorts.get(k, {}).get("available")), None)
    crowd, note = None, None
    if ref:
        lp = ref["long_pct"]
        if lp >= LS_CROWD_LONG:
            crowd = "long-crowded"
            note = ("longs are the bags; short-side uncrowded → §7 squeeze fuel for a short "
                    "(crowded AGAINST a short = the good kind)")
        elif lp <= LS_CROWD_SHORT:
            crowd = "short-crowded"
            note = "shorts crowded → with the crowd on a short = late/bait, size down (§7)"
        else:
            crowd = "balanced"
            note = "no positioning crowd edge"
    return {"short_fire": short_fire, "short_fire_cohort": worst_cohort if short_fire else None,
            "short_fire_threshold_pct": -LS_DROP_PCT, "worst_top_trend_pct": worst,
            "crowdedness": crowd, "crowd_note": note}


def build_ls(sym):
    """SPEC 23 — the cross-venue L/S positioning leg. Binance-authoritative; Bybit best-effort;
    Bitget/Aster always unavailable. Importable so triage/analyse can read positioning without a
    hand-curl. Returns {source, cohorts{top_position,top_account,global}, venues{…}, signals{…}}."""
    sym = sym.upper().replace("USDT", "")
    s = f"{sym}USDT"
    bn = _binance_ls(s)
    cohorts = {k: _ls_cohort(bn.get(k)) for k in ("top_position", "top_account", "global")}
    bn_ok = any(c.get("available") for c in cohorts.values())

    by = _bybit_ls(s)
    by_list = by.get("result", {}).get("list", []) if isinstance(by, dict) else []
    by_ok = bool(by_list)

    venues = {
        "binance": {"available": bn_ok, "reason": None if bn_ok else "no L/S data"},
        "bybit": {"available": by_ok,
                  "reason": None if by_ok else "account-ratio list empty for thin symbol"},
        "bitget": {"available": False, "reason": "account-long-short 'data empty' (40054) for these names"},
        "aster": {"available": False, "reason": "DEX — exposes no L/S endpoint"},
    }
    source = "binance" if bn_ok else ("bybit" if by_ok else None)
    return {"source": source, "cohorts": cohorts, "venues": venues, "signals": _ls_signals(cohorts)}


def build_regime(sym):
    """Pure compute → the cross-venue regime contract dict."""
    sym = sym.upper().replace("USDT", "")
    s = f"{sym}USDT"
    bn, by, ax, bg = _binance(s), _bybit(s), _aster(s), _bitget(s)
    bn_r = [r * 100 for r in _bn_funding(bn)]
    by_r = [r * 100 for r in _by_funding(by)]
    ax_r = [r * 100 for r in _bn_funding(ax)]  # aster mirrors binance shape
    bg_r = [r * 100 for r in _bg_funding(bg)]  # SPEC 26: Bitget

    # SPEC 18: the LATEST per venue is the LIVE predicted rate (settled history = z sample only).
    # Prepending makes the existing block + the divergence (which read [0]) use the live value;
    # a None live degrades to the settled head. So divergence now compares LIVE latests.
    bn_r = _prepend_live(bn_r, _live_funding_pct("binance", s))
    by_r = _prepend_live(by_r, _live_funding_pct("bybit", s))
    ax_r = _prepend_live(ax_r, _live_funding_pct("aster", s))
    bg_r = _prepend_live(bg_r, _live_funding_pct("bitget", s))

    funding = {v: _venue_funding_block(r) for v, r in
               (("binance", bn_r), ("bybit", by_r), ("aster", ax_r), ("bitget", bg_r))}

    # SPEC 26: §5 veto across ALL venues incl. Bitget, normalized to %/4h (interval derived per venue).
    bn_iv = _interval_min_from_times(x.get("fundingTime") for x in (bn.get("funding") or []) if isinstance(x, dict))
    by_iv = _interval_min_from_times(x.get("fundingRateTimestamp")
                                     for x in (by.get("funding", {}).get("result", {}).get("list", [])
                                               if isinstance(by.get("funding"), dict) else []) if isinstance(x, dict))
    ax_iv = _interval_min_from_times(x.get("fundingTime") for x in (ax.get("funding") or []) if isinstance(x, dict))
    cross_venue = _cross_venue_veto({
        "binance": _to_4h(bn_r[0] if bn_r else None, bn_iv),
        "bybit": _to_4h(by_r[0] if by_r else None, by_iv),
        "aster": _to_4h(ax_r[0] if ax_r else None, ax_iv),
        "bitget": _to_4h(bg_r[0] if bg_r else None, _bg_interval_min(bg)),
    })

    # OI z-score (today vs prior ~30D daily)
    oi_z = {}
    for name, series in (("binance", _bn_oi30(bn)), ("bybit", _by_oi30(by))):
        if series and len(series) >= 11:
            zz, mean, std, n = zscore(series[-1], series[:-1])
            oi_z[name] = {"today": series[-1], "mean": mean, "std": std,
                          "z": round(zz, 2) if zz is not None else None, "n": n}
        else:
            oi_z[name] = {"today": series[-1] if series else None, "mean": None, "std": None, "z": None,
                          "n": (len(series) - 1 if series else 0)}

    # OI 24h trend
    oi_24h = {}
    bn_oi = bn.get("oi_hist")
    if isinstance(bn_oi, list) and bn_oi:
        vals = [float(x["sumOpenInterest"]) for x in bn_oi]
        oi_24h["binance"] = {"first": vals[0], "last": vals[-1],
                             "trend_pct": round((vals[-1] - vals[0]) / vals[0] * 100, 2) if vals[0] else 0}
    by_list = by.get("oi_hist", {}).get("result", {}).get("list", []) if isinstance(by.get("oi_hist"), dict) else []
    if by_list:
        vals = [float(x["openInterest"]) for x in by_list]  # newest→oldest
        old, new = vals[-1], vals[0]
        oi_24h["bybit"] = {"first": old, "last": new,
                           "trend_pct": round((new - old) / old * 100, 2) if old else 0}
    ax_oi = ax.get("oi_now", {})
    if isinstance(ax_oi, dict) and ax_oi.get("openInterest"):
        oi_24h["aster"] = {"current": float(ax_oi["openInterest"])}
    bg_oi_usd = _bg_oi_usd(bg)                            # SPEC 26: Bitget OI in USD (size × price)
    if bg_oi_usd is not None:
        oi_24h["bitget"] = {"current_usd": round(bg_oi_usd, 2)}

    divergence = None
    if bn_r and by_r:
        delta = by_r[0] - bn_r[0]
        if abs(delta) > 0.05:
            divergence = {"binance": bn_r[0], "bybit": by_r[0], "delta_pct": round(delta, 4)}

    ls = build_ls(sym)   # SPEC 23: cross-venue L/S positioning leg (Binance-authoritative)

    return {"ticker": sym, "funding": funding, "oi_z": oi_z, "oi_24h": oi_24h,
            "divergence": divergence, "cross_venue": cross_venue, "ls": ls}


def render_human(r):
    print(f"# {r['ticker']}USDT — derivative regime check\n")
    print("## Funding history (latest first, per-interval %)\n")
    print("| Venue | Latest | -1 | -2 | -3 | -4 | -5 | -6 |")
    print("|---|---|---|---|---|---|---|---|")
    for v in ("binance", "bybit", "aster", "bitget"):
        h = r["funding"][v]["history"]
        cells = [fmt_pct(h[i]) if i < len(h) else "—" for i in range(7)]
        print(f"| {v.title()} | " + " | ".join(cells) + " |")

    print("\n## Regime per venue (§2 phase table)\n")
    for v in ("binance", "bybit", "aster", "bitget"):
        b = r["funding"][v]
        print(f"- **{v.title()}** {fmt_pct(b['latest'])} → {b['regime']}")

    print("\n## Funding z-score (latest vs prior ~40D, this token only)\n")
    print("| Venue | Latest | Mean | Std | z | n |")
    print("|---|---|---|---|---|---|")
    for v in ("binance", "bybit", "aster", "bitget"):
        z = r["funding"][v]["z"]
        print(f"| {v.title()} | {fmt_pct(z['latest'])} | {fmt_pct(z['mean'])} | {fmt_pct(z['std'])} | {z['z'] if z['z'] is not None else 'n/a'} | {z['n']} |")

    print("\n## OI z-score (today vs prior ~30D daily)\n")
    print("| Venue | Today | Mean | Std | z | n |")
    print("|---|---|---|---|---|---|")
    for v in ("binance", "bybit"):
        z = r["oi_z"][v]
        print(f"| {v.title()} | {fmt_oi(z['today'])} | {fmt_oi(z['mean'])} | {fmt_oi(z['std'])} | {z['z'] if z['z'] is not None else 'n/a'} | {z['n']} |")

    print("\n## OI 24h trend\n")
    for v in ("binance", "bybit"):
        o = r["oi_24h"].get(v)
        if o:
            print(f"- {v.title()}: {fmt_oi(o['first'])} → {fmt_oi(o['last'])} ({o['trend_pct']:+.2f}% over 24h)")
    if r["oi_24h"].get("aster"):
        print(f"- Aster current OI: {fmt_oi(r['oi_24h']['aster']['current'])}")

    if r["divergence"]:
        d = r["divergence"]
        print(f"\n## ⚠ Funding divergence\nBybit {fmt_pct(d['bybit'])} vs Binance {fmt_pct(d['binance'])} (Δ {d['delta_pct']:+.3f}%)")
        print("→ §8: pull per-venue heatmaps separately — aggregated clusters miss venue-specific positioning.")

    ls = r.get("ls")
    if ls:
        print(f"\n## Long/Short positioning (source: {ls['source'] or '—'}, §6/§7)\n")
        print("| Cohort | L/S ratio | Long% | Short% | Trend Δ% (12h) |")
        print("|---|---|---|---|---|")
        for k, name in (("top_position", "Top-position"), ("top_account", "Top-account"), ("global", "Global")):
            c = ls["cohorts"].get(k, {})
            if c.get("available"):
                tr = f"{c['trend_pct']:+.2f}%" if c.get("trend_pct") is not None else "—"
                print(f"| {name} | {c['ratio']:.3f} | {c['long_pct']:.1f}% | {c['short_pct']:.1f}% | {tr} |")
            else:
                print(f"| {name} | — | — | — | unavailable |")
        unavail = [v for v, b in ls["venues"].items() if not b["available"]]
        if unavail:
            print(f"\nVenues unavailable (no fabricated ratio): {', '.join(unavail)}")
        sig = ls["signals"]
        if sig.get("short_fire"):
            print(f"\n🔴 §6 SHORT-FIRE: top-trader L/S dropped {sig['worst_top_trend_pct']:+.1f}% "
                  f"(≤ {sig['short_fire_threshold_pct']:.0f}%, cohort {sig['short_fire_cohort']}) — trapped longs capitulating.")
        if sig.get("crowdedness"):
            print(f"Crowdedness: **{sig['crowdedness']}** — {sig['crowd_note']}")


def main():
    ap = argparse.ArgumentParser(description="Cross-venue derivative regime checker")
    ap.add_argument("ticker")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)
    r = build_regime(args.ticker)
    if args.json:
        print(json.dumps(r))
    else:
        render_human(r)


if __name__ == "__main__":
    main()
