#!/usr/bin/env python3
"""regime_flip.py — Regime-flip & memo-drift detector across the watchlist.

Goal: a watchlist memo is a point-in-time read; the market moves underneath it.
The highest-signal drift is the FUNDING-SIGN FLIP (funding direction = phase,
CLAUDE.md Section 2): neg->pos on a Cat A = trap-formation-LONG -> distribution-SHORT
(the BSB 2026-05-30 case). This re-verifies LIVE funding per token, diffs the regime
against the stored baseline + memo, and ranks the board by drift severity.

HARD RULE (the whole reason this exists): funding is read from the LIVE venue ticker
(Bybit /v5/market/tickers fundingRate, Binance /fapi/v1/premiumIndex lastFundingRate),
NEVER a settled/annualized print. Reintroducing the stale-print error here would defeat
the goal. All thresholds are PER-INTERVAL.

Usage:
  python3 scripts/regime_flip.py            # ranked board, read-only
  python3 scripts/regime_flip.py --once     # same, terse (cron)
  python3 scripts/regime_flip.py --write     # reconcile drifted memos into watchlist.json
                                             # (old memo -> prev_state; live regime -> `regime`)
  python3 scripts/regime_flip.py BSB PLAY    # subset
"""
import json, re, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WL_PATH = REPO / "config" / "watchlist.json"

FLAT_BAND = 0.05      # |funding %/4h| < this = FLAT (SPEC 11: normalized, not per-interval)
DEEP_NEG = -0.30      # <= this %/4h = deep-neg (short-veto regime, §5/§3 — normalized %/4h)
LIQ_GATE_M = 10.0     # $M/24h liquidity gate


def to_4h(funding_pi, interval_min):
    """Normalize a per-interval funding rate to %/4h (SPEC 11). The veto/threshold
    line (−0.30) is %/4h; a raw −0.16/int on a 1h name is −0.65%/4h, still vetoed."""
    if funding_pi is None or not interval_min:
        return None
    return round(funding_pi * 240.0 / interval_min, 4)


FUNDING_FLOOR_RAW = 0.00005   # SPEC 10: Bybit/Binance base-rate FLOOR — surfaced (as "0.005%")
                              # when a token has no real funding, NOT a live read. Never trust it.
FUNDING_FLOOR_BAND = 5e-6     # SPEC 44: the LIVE-predicted rate hovers just off the exact 0.00005
                              # clamp (e.g. 0.000054) and evaded the old exact-match check, surfacing
                              # as a confident "+0.005 flat". Treat anything rounding to the ±0.005%
                              # display-floor as the sentinel. Real micro-reads (EDEN/CHIP +0.001-
                              # 0.002%/4h, raw ~1.5-1.8e-5) stay well outside this band.


def _is_floor(raw):
    """True if a raw funding rate is at the venue floor sentinel (±0.005% ≈ 0.00005, banded) —
    fabricated/economically-zero flat, never trust it as a confident funding read (SPEC 10/44)."""
    return raw is not None and abs(abs(raw) - FUNDING_FLOOR_RAW) <= FUNDING_FLOOR_BAND


def fetch(url, timeout=12):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "regime-flip/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None


def bybit_interval_min(sym):
    d = fetch(f"https://api.bybit.com/v5/market/instruments-info?category=linear&symbol={sym}")
    try:
        return int(d["result"]["list"][0]["fundingInterval"])
    except (TypeError, KeyError, IndexError, ValueError):
        return 240  # default 4h


_BN_FUNDING_INFO = None   # cache: {SYMBOL: interval_min} for non-8h Binance perps


def binance_interval_min(sym):
    """Binance funding interval (min). fundingInfo lists only non-8h symbols; default 8h."""
    global _BN_FUNDING_INFO
    if _BN_FUNDING_INFO is None:
        info = fetch("https://fapi.binance.com/fapi/v1/fundingInfo")
        _BN_FUNDING_INFO = {}
        if isinstance(info, list):
            for x in info:
                try:
                    _BN_FUNDING_INFO[x["symbol"]] = int(x.get("fundingIntervalHours", 8)) * 60
                except (KeyError, ValueError, TypeError):
                    pass
    return _BN_FUNDING_INFO.get(sym, 480)


# ── SPEC 49: per-run venue snapshot — one bulk fetch per venue covers every board
# symbol; per-token reads are served from it (per-symbol fetch only as fallback).
_SNAP = None


def load_venue_snapshot(symbols=None):
    """Bulk tickers/premium-index per venue + threaded Binance OI for `symbols`.
    After this, _venue_bybit/_venue_binance serve snapshot-covered symbols with zero
    HTTP. Failure of any bulk call degrades to the per-symbol path (empty map)."""
    global _SNAP
    snap = {"bybit": {}, "bybit_iv": {}, "bn_pi": {}, "bn_t24": {}, "bn_oi": {}}
    d = fetch("https://api.bybit.com/v5/market/tickers?category=linear")
    try:
        snap["bybit"] = {x["symbol"]: x for x in d["result"]["list"]}
    except (TypeError, KeyError):
        pass
    cursor = ""
    for _ in range(5):   # instruments-info paginates; ~600 linear perps ≈ 1 page at limit=1000
        url = "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000"
        d = fetch(url + (f"&cursor={cursor}" if cursor else ""))
        try:
            for x in d["result"]["list"]:
                snap["bybit_iv"][x["symbol"]] = int(x["fundingInterval"])
            cursor = d["result"].get("nextPageCursor") or ""
        except (TypeError, KeyError, ValueError):
            break
        if not cursor:
            break
    d = fetch("https://fapi.binance.com/fapi/v1/premiumIndex")
    if isinstance(d, list):
        snap["bn_pi"] = {x.get("symbol"): x for x in d if isinstance(x, dict)}
    d = fetch("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if isinstance(d, list):
        snap["bn_t24"] = {x.get("symbol"): x for x in d if isinstance(x, dict)}
    binance_interval_min("_WARM_")   # prime the fundingInfo cache (also a bulk endpoint)
    want = [s for s in (symbols or []) if s in snap["bn_pi"]]
    if want:   # Binance has no bulk OI endpoint — thread the per-symbol calls
        from concurrent.futures import ThreadPoolExecutor

        def _oi(s):
            od = fetch(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={s}")
            try:
                return s, float(od["openInterest"])
            except (TypeError, KeyError, ValueError):
                return s, 0.0
        with ThreadPoolExecutor(max_workers=8) as ex:
            snap["bn_oi"] = dict(ex.map(_oi, want))
    _SNAP = snap
    return {"bybit": len(snap["bybit"]), "binance": len(snap["bn_pi"]), "oi": len(snap["bn_oi"])}


def clear_venue_snapshot():
    global _SNAP
    _SNAP = None


def _venue_bybit(sym):
    if _SNAP is not None and sym in _SNAP["bybit"]:
        x = _SNAP["bybit"][sym]
        try:
            return {"venue": "bybit", "funding_raw": float(x["fundingRate"]),
                    "interval_min": _SNAP["bybit_iv"].get(sym, 240), "price": float(x["lastPrice"]),
                    "chg24": float(x["price24hPcnt"]) * 100, "vol_m": float(x["turnover24h"]) / 1e6,
                    "oi": float(x.get("openInterest", 0) or 0)}
        except (TypeError, KeyError, ValueError):
            pass   # torn bulk row → per-symbol fallback below
    d = fetch(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}")
    try:
        x = d["result"]["list"][0]
        return {"venue": "bybit", "funding_raw": float(x["fundingRate"]),
                "interval_min": bybit_interval_min(sym), "price": float(x["lastPrice"]),
                "chg24": float(x["price24hPcnt"]) * 100, "vol_m": float(x["turnover24h"]) / 1e6,
                "oi": float(x.get("openInterest", 0) or 0)}
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def _venue_binance(sym):
    if _SNAP is not None and sym in _SNAP["bn_pi"]:        # SPEC 49: snapshot-served
        pi, t24 = _SNAP["bn_pi"][sym], _SNAP["bn_t24"].get(sym)
        oi_v = _SNAP["bn_oi"].get(sym)
    else:
        pi = fetch(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}")
        t24, oi_v = None, None
    try:
        fund_raw = float(pi["lastFundingRate"])
        price = float(pi["markPrice"])
    except (TypeError, KeyError, ValueError):
        return None
    if t24 is None:
        t24 = fetch(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={sym}")
    if oi_v is None:
        oid = fetch(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={sym}")
        try:
            oi_v = float(oid["openInterest"]) if oid and "openInterest" in oid else 0.0
        except (TypeError, KeyError, ValueError):
            oi_v = 0.0
    return {"venue": "binance", "funding_raw": fund_raw, "interval_min": binance_interval_min(sym),
            "price": price, "vol_m": (float(t24["quoteVolume"]) / 1e6 if t24 and "quoteVolume" in t24 else None),
            "chg24": (float(t24["priceChangePercent"]) if t24 and "priceChangePercent" in t24 else None),
            "oi": oi_v}


def _venue_bitget(sym):
    """SPEC 44 addendum: Bitget as a floor-resolution secondary (SPEC 26 wired it for
    regime_check; live_perp pulls it LAZILY — only when binance/bybit give no real
    non-floor read). current-fund-rate carries the live rate + interval in one call."""
    base = "https://api.bitget.com/api/v2/mix/market"
    cur = fetch(f"{base}/current-fund-rate?symbol={sym}&productType=usdt-futures")
    try:
        d = cur["data"][0]
        fund_raw = float(d["fundingRate"])
        interval_min = int(float(d.get("fundingRateInterval") or 4)) * 60
    except (TypeError, KeyError, IndexError, ValueError):
        return None
    tick = fetch(f"{base}/ticker?symbol={sym}&productType=usdt-futures")
    try:
        t = tick["data"][0]
        price = float(t["lastPr"])
    except (TypeError, KeyError, IndexError, ValueError):
        return None

    def _f(key):
        try:
            return float(t[key]) if t.get(key) not in (None, "") else None
        except (ValueError, TypeError):
            return None
    vol, chg = _f("usdtVolume"), _f("change24h")
    return {"venue": "bitget", "funding_raw": fund_raw, "interval_min": interval_min,
            "price": price, "chg24": (chg * 100 if chg is not None else None),
            "vol_m": (vol / 1e6 if vol is not None else None),
            "oi": _f("holdingAmount") or 0.0}


def _venue_aster(sym):
    """SPEC 44 addendum: Aster as a floor-resolution secondary (Binance-fork API).
    Interval derived from the last two settle timestamps (no fundingInfo); default 4h."""
    pi = fetch(f"https://fapi.asterdex.com/fapi/v1/premiumIndex?symbol={sym}")
    try:
        fund_raw = float(pi["lastFundingRate"])
        price = float(pi["markPrice"])
    except (TypeError, KeyError, ValueError):
        return None
    interval_min = 240
    hist = fetch(f"https://fapi.asterdex.com/fapi/v1/fundingRate?symbol={sym}&limit=2")
    try:
        ts = sorted(int(x["fundingTime"]) for x in hist)
        if len(ts) >= 2 and ts[-1] > ts[-2]:
            interval_min = round((ts[-1] - ts[-2]) / 60000)
    except (TypeError, KeyError, ValueError):
        pass
    return {"venue": "aster", "funding_raw": fund_raw, "interval_min": interval_min,
            "price": price, "chg24": None, "vol_m": None, "oi": 0.0}


def live_perp(ticker):
    """CROSS-VENUE live funding (SPEC 13). Reads BOTH Binance + Bybit; the veto-relevant
    funding_4h = the MORE-VETOING (most-negative) NON-floor venue (so a single venue's floor
    can't mask another's deep-neg). All venues floored → a genuine FLAT read (not UNAVAILABLE).

    SPEC 44 addendum: when binance/bybit yield no real (non-floor) read, the SPEC-26
    secondaries (Bitget, Aster) are consulted before any funding verdict — a real
    secondary beats both an uncorroborated lone floor (SUSPECT) and a 2-venue floor
    (all_floor), per the SPEC 13 more-vetoing-real-venue precedence.
    Returns dict (with per-venue `venues` + `funding_split`) or None if no venue lists the perp."""
    sym = f"{ticker}USDT"
    venues = {}

    def _add(v):
        if not v:
            return
        v["is_floor"] = _is_floor(v["funding_raw"])
        v["funding_pi"] = round(v["funding_raw"] * 100, 6)
        v["funding_4h"] = to_4h(v["funding_pi"], v["interval_min"])
        venues[v["venue"]] = v

    for v in (_venue_bybit(sym), _venue_binance(sym)):
        _add(v)

    def _real():
        return {k: v for k, v in venues.items() if not v["is_floor"] and v["funding_4h"] is not None}

    real = _real()
    if venues and not real:
        # SPEC 44 addendum: floored/absent primaries — consult the secondaries before verdicting
        for vf in (_venue_bitget, _venue_aster):
            _add(vf(sym))
        real = _real()
    if not venues:
        return None   # truly no perp listing on any venue

    primary = max(venues, key=lambda k: (venues[k].get("oi") or 0, venues[k].get("vol_m") or 0))
    all_4h = [v["funding_4h"] for v in venues.values() if v["funding_4h"] is not None]
    # venues straddle the §5 line (e.g. one venue floors flat while the dominant venue is deep-neg)
    funding_split = bool(all_4h and min(all_4h) <= DEEP_NEG < max(all_4h))

    if real:
        canon = min(real, key=lambda k: real[k]["funding_4h"])   # the more-vetoing real venue wins
        all_floor = False
        funding_suspect = False
    elif len(venues) >= 2:
        canon = primary                                          # every covered venue floored, ≥2 sources
        all_floor = True                                         # corroborate → genuine ~0% flat
        funding_suspect = False
    else:
        # SPEC 44: the ONLY covered venue is at the floor — no secondary to corroborate.
        # CLAUDE.md §3: a single-source read gating a verdict needs a second source, so a lone
        # floor print is SUSPECT, not a confident flat (the floor once masked −1.65%/4h).
        canon = primary
        all_floor = False
        funding_suspect = True

    c = venues[canon]
    p = venues[primary]
    return {
        "venue": canon, "primary_venue": primary,
        "funding_pi": c["funding_pi"], "interval_min": c["interval_min"], "funding_4h": c["funding_4h"],
        "funding_stale": False, "all_floor": all_floor, "funding_suspect": funding_suspect,
        "funding_split": funding_split,
        "price": p["price"], "chg24": p.get("chg24"), "vol_m": p.get("vol_m"), "oi": p.get("oi"),
        "venues": {k: {"funding_4h": v["funding_4h"], "funding_pi": v["funding_pi"],
                       "interval_min": v["interval_min"], "is_floor": v["is_floor"],
                       "oi": v.get("oi"), "vol_m": v.get("vol_m")} for k, v in venues.items()},
    }


def sign_of(fund_pi):
    if fund_pi is None:
        return "n/a"
    if abs(fund_pi) < FLAT_BAND:
        return "flat"
    return "pos" if fund_pi > 0 else "neg"


def memo_funding_sign(memo):
    """Best-effort parse of the funding sign the memo describes."""
    m = memo.lower()
    # explicit numeric funding in memo, e.g. "funding -0.749%/4h" / "+0.164%/4h"
    nums = re.findall(r"funding[^%]{0,18}?([+-]?\d+\.?\d*)\s*%?\s*/?\s*\d*h", m)
    if nums:
        try:
            v = float(nums[0])
            return sign_of(v)
        except ValueError:
            pass
    if any(k in m for k in ["deeply negative", "deep-neg", "deeply neg", "negative funding",
                            "funding flips negative", "neg funding", "funding -"]):
        return "neg"
    if any(k in m for k in ["funding positive", "positive funding", "funding flip positive",
                            "longs trapped paying", "funding +", "strongly positive"]):
        return "pos"
    if any(k in m for k in ["funding flat", "flat funding", "funding still flat", "funding neutral"]):
        return "flat"
    return "?"


def memo_direction(memo):
    m = memo.lower()
    if "not a short" in m:
        return "LONG/notshort"
    short = any(k in m for k in ["short", "distribution top", "stage-5", "stage 5", "fade", "blowoff"])
    long_ = any(k in m for k in ["long", "trap-formation", "squeeze-fuel", "accumulation", "scout long", "pullback-long"])
    if short and not long_:
        return "SHORT"
    if long_ and not short:
        return "LONG"
    if short and long_:
        return "MIXED"
    if "pass" in m or "dust" in m or "no edge" in m:
        return "PASS"
    return "?"


def memo_zone(memo):
    """Extract a $ entry zone (low, high) if the memo names one."""
    # ranges like $0.115-0.118 or $7.45-$7.65 or $0.70-0.75
    r = re.search(r"\$?(\d+\.\d+)\s*[-–]\s*\$?(\d+\.\d+)", memo)
    if r:
        a, b = float(r.group(1)), float(r.group(2))
        return (min(a, b), max(a, b))
    return None


def classify(tok, live):
    """Return (severity, tag, note). severity: 0 flip,1 zone,2 confirm,3 none."""
    memo = tok.get("state", "")
    if live is None:
        return (3, "NO_PERP", "no live perp data — spot-only or delisted")
    if live.get("funding_4h") is None:
        # SPEC 10: price may be live but funding is unavailable/floor-fabricated — say so,
        # never report it as a flat CONFIRM (a real deep-neg could be hiding behind the floor).
        return (3, "FUNDING_UNAVAILABLE",
                "live price OK but funding unavailable (venue floor/stale) — price-only, NOT a funding read")
    if live.get("funding_suspect"):
        # SPEC 44: floor print on the only covered venue, no secondary to corroborate.
        # Degrade, don't fabricate — the funding leg is NOT a basis for a confident flat CONFIRM
        # (a single-venue floor once masked −1.65%/4h deep-neg).
        return (3, "FUNDING_SUSPECT",
                "live funding at the venue floor on the only covered venue (no secondary to "
                "corroborate) — FUNDING_SUSPECT, price-only, NOT a confident flat")
    vol = live.get("vol_m")
    dust = vol is not None and vol < LIQ_GATE_M

    f4 = live["funding_4h"]                       # SPEC 11+13: more-vetoing cross-venue %/4h
    iv_h = int((live.get("interval_min") or 240) / 60)
    raw = f"(raw {live['funding_pi']:+.3f}%/{iv_h}h, {live.get('venue', '?')})"
    vsplit = " ⚠VENUE-SPLIT (venues straddle −0.30%/4h — using the more-vetoing)" if live.get("funding_split") else ""
    vfloor = " [all_floor: venue floor on all covered venues = genuine ~0% flat]" if live.get("all_floor") else ""
    live_sign = sign_of(f4)
    # baseline: structured `regime` if present (compounds across runs), else memo-parse
    base = tok.get("regime", {})
    base_sign = base.get("funding_sign") or memo_funding_sign(memo)
    mdir = memo_direction(memo)
    # A memo already reconciled by --write embeds the OLD direction word ("memo SHORT but...")
    # — re-parsing it would re-trip contra/zone every run forever. Once reconciled, trust the
    # structured `regime` baseline (funding-sign diff) only; the original drift is already recorded.
    reconciled = memo.startswith("[REGIME_FLIP") or memo.startswith("[ZONE_BLOWN")

    notes = []
    # --- REGIME FLIP: funding sign crossed neg<->pos (flat is not a flip on its own) ---
    flip = (base_sign in ("neg", "pos") and live_sign in ("neg", "pos") and base_sign != live_sign)
    # direction-vs-funding contradiction (e.g. memo SHORT but funding deep-neg = squeeze, do-not-short)
    contra = (not reconciled) and (
              (mdir == "SHORT" and live_sign == "neg" and f4 <= DEEP_NEG) or
              (mdir.startswith("LONG") and live_sign == "pos" and f4 > FLAT_BAND))
    if flip:
        newdir = "SHORT-load (longs now trapped)" if live_sign == "pos" else "LONG/squeeze-fuel (shorts now paying)"
        notes.append(f"funding {base_sign}->{live_sign} → regime flipped to {newdir}")
        return (0, "REGIME_FLIP", "; ".join(notes) + (" [DUST: signal-only]" if dust else ""))
    if contra:
        if mdir == "SHORT":
            notes.append(f"memo SHORT but live funding {f4:+.3f}%/4h {raw} deep-neg = squeeze fuel, short VETOED{vsplit}")
        else:
            notes.append(f"memo LONG but live funding {f4:+.3f}%/4h {raw} positive = longs paying, long weakened")
        return (0, "REGIME_FLIP", "; ".join(notes) + (" [DUST: signal-only]" if dust else ""))

    # --- ZONE BLOWN ---
    zone = None if reconciled else memo_zone(memo)
    px = live["price"]
    if zone and px is not None:
        lo, hi = zone
        if px < lo * 0.97:
            return (1, "ZONE_BLOWN", f"price ${px:g} is below memo zone ${lo:g}-${hi:g} (cascaded past / expired)" + (" [DUST]" if dust else ""))
        if px > hi * 1.03:
            return (1, "ZONE_BLOWN", f"price ${px:g} is above memo zone ${lo:g}-${hi:g} (ran past entry)" + (" [DUST]" if dust else ""))

    if dust:
        return (3, "DUST", f"${vol:.1f}M/24h < ${LIQ_GATE_M:g}M gate — signal-only, not tradeable")
    # confirm
    return (2, "CONFIRM", f"live funding {live_sign} ({f4:+.3f}%/4h {raw}) consistent with memo{vfloor}{vsplit}")


def short_veto_flag(live):
    if live and live.get("funding_4h") is not None and live["funding_4h"] <= DEEP_NEG:
        iv_h = int((live.get("interval_min") or 240) / 60)
        return (f"  ⚠ deep-neg funding {live['funding_4h']:+.3f}%/4h "
                f"(raw {live['funding_pi']:+.3f}%/{iv_h}h) → SHORT vetoed (carry+squeeze)")
    return ""


def log_pending_flips(rows):
    """Stretch 6 (goal 2026-05-30): append each REGIME_FLIP to state/base_rates_pending.json in
    the SAME schema analyse.py uses, flagged regime_flip=True. backtest.py --record-pending then
    resolves them (forward price at horizon) into a REGIME_FLIP_LONG/SHORT base-rate bucket — does
    a neg<->pos funding flip precede a tradeable move in the flip direction? Append-only + idempotent
    (skip a ticker already pending unresolved the same UTC day). Direction from live funding sign:
    flipped-positive = longs trapped = SHORT-load; flipped-negative = shorts paying = LONG/squeeze-fuel."""
    PENDING = REPO / "state" / "base_rates_pending.json"
    if not PENDING.exists():
        print(f"  (no {PENDING.name} — skip flip logging)")
        return 0
    pend = json.loads(PENDING.read_text())
    items = pend.setdefault("pending", [])
    now = int(time.time())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    appended = 0
    for sev, tag, tk, live, note, tok in rows:
        if tag != "REGIME_FLIP" or not live or live.get("funding_pi") is None:
            continue
        direction = "SHORT" if live["funding_pi"] > 0 else "LONG"
        dup = any(it.get("ticker") == tk and it.get("regime_flip") and not it.get("resolved")
                  and str(it.get("ts_human", "")).startswith(day) for it in items)
        if dup:
            continue
        items.append({
            "ticker": tk, "direction": direction, "tier": "MILD",
            "entry_price": live["price"],
            "funding_4h": live.get("funding_4h"),
            "ls_ratio": None, "oi_zscore": None,
            "ts": now, "ts_human": datetime.now(timezone.utc).isoformat(),
            "resolved": False, "regime_flip": True, "structure_blowoff": False,
            "verdict_string": f"REGIME_FLIP->{direction}", "note": str(note)[:120],
        })
        appended += 1
    PENDING.write_text(json.dumps(pend, indent=2))
    return appended


def main():
    args = [a for a in sys.argv[1:]]
    do_write = "--write" in args
    once = "--once" in args
    log_pending = "--log-pending" in args
    as_json = "--json" in args
    subset = [a.upper() for a in args if not a.startswith("-")]

    wl = json.loads(WL_PATH.read_text())
    tokens = wl["tokens"] if isinstance(wl, dict) else wl
    if subset:
        tokens_run = [t for t in tokens if t.get("ticker", "").upper() in subset]
    else:
        tokens_run = tokens

    rows = []
    for tok in tokens_run:
        tk = tok.get("ticker")
        live = live_perp(tk)
        sev, tag, note = classify(tok, live)
        rows.append((sev, tag, tk, live, note, tok))
        time.sleep(0.15)  # gentle on the public APIs

    rows.sort(key=lambda r: (r[0], r[2]))

    if as_json:
        out = [{"ticker": tk, "tag": tag, "note": note,
                "funding_4h": (live or {}).get("funding_4h"),         # SPEC 11/13: more-vetoing cross-venue
                "funding_pi": (live or {}).get("funding_pi"),         # raw per-interval (secondary)
                "interval_min": (live or {}).get("interval_min"),
                "venue": (live or {}).get("venue"),                   # SPEC 13: venue providing funding_4h
                "primary_venue": (live or {}).get("primary_venue"),   # dominant by OI/vol
                "funding_split": (live or {}).get("funding_split", False),
                "all_floor": (live or {}).get("all_floor", False),
                "funding_stale": (live or {}).get("funding_stale", False),
                "venues": (live or {}).get("venues"),
                "price": (live or {}).get("price"),
                "chg24": (live or {}).get("chg24")}
               for sev, tag, tk, live, note, tok in rows]
        print(json.dumps(out[0] if len(out) == 1 else out))
        return

    TAGI = {"REGIME_FLIP": "🔴", "ZONE_BLOWN": "🟠", "CONFIRM": "🟢", "DUST": "⚪", "NO_PERP": "⚫"}
    print(f"\n═══ REGIME-FLIP BOARD — {len(rows)} tokens (live-verified funding) ═══")
    print("(funding is LIVE, shown %/4h-normalized; FLIP = funding sign crossed neg<->pos)\n")
    for sev, tag, tk, live, note, tok in rows:
        px = f"${live['price']:g}" if live and live.get("price") is not None else "—"
        chg = f"{live['chg24']:+.1f}%" if live and live.get("chg24") is not None else "?"
        fund = f"{live['funding_4h']:+.3f}%/4h" if live and live.get("funding_4h") is not None else "n/a"
        vol = f"${live['vol_m']:.1f}M" if live and live.get("vol_m") is not None else "?"
        print(f"{TAGI.get(tag,'·')} {tag:11s} {tk:9s} {px:11s} {chg:8s} fund {fund:14s} vol {vol:9s}")
        print(f"      {note}")
        v = short_veto_flag(live)
        if v:
            print(v)

    # --- reconcile drifted memos ---
    if do_write:
        ts = time.strftime("%Y-%m-%d")
        changed = 0
        for sev, tag, tk, live, note, tok in rows:
            # record the live regime baseline on EVERY token (so flips compound next run)
            if live and live.get("funding_pi") is not None:
                tok["regime"] = {
                    "funding_sign": sign_of(live["funding_4h"]),
                    "funding_pi": round(live["funding_pi"], 4),
                    "funding_4h": live.get("funding_4h"),
                    "price": live["price"],
                    "chg24": round(live["chg24"], 1) if live.get("chg24") is not None else None,
                    "vol_m": round(live["vol_m"], 1) if live.get("vol_m") is not None else None,
                    "ts": ts,
                }
            # reconcile memo only for real drift (flip/zone), preserving prev_state
            if tag in ("REGIME_FLIP", "ZONE_BLOWN"):
                if tok.get("state") and tok.get("prev_state") != tok["state"]:
                    tok["prev_state"] = tok["state"]
                tok["state"] = f"[{tag} {ts} via regime_flip.py] {note}. (prior memo in prev_state)"
                changed += 1
        WL_PATH.write_text(json.dumps(wl, indent=2))
        print(f"\n✍  --write: reconciled {changed} drifted memo(s); live regime baseline stamped on all. prev_state preserved.")
    else:
        flips = sum(1 for r in rows if r[1] == "REGIME_FLIP")
        zones = sum(1 for r in rows if r[1] == "ZONE_BLOWN")
        print(f"\nSummary: {flips} REGIME_FLIP · {zones} ZONE_BLOWN. Re-run with --write to reconcile memos.")

    if log_pending:
        n = log_pending_flips(rows)
        print(f"📥 --log-pending: appended {n} flip(s) to base_rates_pending.json "
              f"(resolve later via `backtest.py --record-pending` → REGIME_FLIP_* bucket).")


if __name__ == "__main__":
    main()
