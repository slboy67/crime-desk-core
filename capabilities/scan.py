#!/usr/bin/env python3
"""scan.py — cross-sectional scanner over the ENTIRE Bybit linear-perp universe.

The DISCOVERY net (not a watchlist analyser): screens every liquid perp for the
playbook's core cross-sectional signal — FUNDING extremes, normalized to %/4h so
symbols on different funding intervals are comparable (CLAUDE.md §2/§3).

  🟢 LONG squeeze-fuel  — funding ≤ −thresh %/4h  (shorts paying carry = trapped)
  🔴 SHORT longs-trapped — funding ≥ +thresh %/4h  (longs paying carry)
  ⚡ deep extreme        — |funding| ≥ 0.50%/4h (MYX-tier), flagged separately

Liquidity gate: 24h turnover ≥ --min-vol $M (§7). LIVE/predicted funding from the
ticker, not the last settled print. First-pass net only — verify per-venue +
OI/L-S/structure before any entry.

  python3 capabilities/scan.py                 # human, longs+shorts, gate $10M
  python3 capabilities/scan.py --json
  python3 capabilities/scan.py --side long --min-vol 25 --top 20
"""
import argparse
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import colors as C
import regime_flip as RF
import risk_card as RC          # SPEC-169: the risk-card line

UA = {"User-Agent": "Mozilla/5.0 (scan_all)"}


# ── SPEC-173: §7 hard gates shared across EVERY scan mode — one definition each, imported
# by every mode rather than re-derived by hand (the scout sweep 2026-08-28 finding: modes
# passed names a by-hand §7 check would have killed — H/BSB/SLX/USELESS/ROBO on thin vol,
# COTI/KORU on a thin Aster book / not-a-crypto-instrument respectively). ─────────────────
LIQUIDITY_HARD_FLOOR_USD = 10_000_000.0     # < this on the size venue -> excluded:liquidity
LIQUIDITY_SCOUT_CEILING_USD = 25_000_000.0  # [floor, ceiling) passes, tagged liquidity_tier:scout
INSTRUMENT_EXCLUSIONS_PATH = Path(__file__).resolve().parent.parent / "config" / "instrument_exclusions.json"
SIZING_CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "sizing.json"
DEFAULT_MIN_ASTER_EXIT_USD = 10_000.0
DEFAULT_ASTER_EXIT_BAND_PCT = 2.0


def liquidity_gate(vol24h_usd):
    """§7 gate on a $ 24h-volume figure from the SIZE venue (Binance/Bybit/Bitget/KuCoin —
    never Aster's own prints, §7). `None` (unverified) never excludes on its own — a missing
    datum is a caveat for the caller, not a false exclude."""
    if vol24h_usd is None:
        return {"excluded": False, "reason": None, "liquidity_tier": None}
    if vol24h_usd < LIQUIDITY_HARD_FLOOR_USD:
        return {"excluded": True, "reason": "liquidity", "liquidity_tier": None}
    if vol24h_usd < LIQUIDITY_SCOUT_CEILING_USD:
        return {"excluded": False, "reason": None, "liquidity_tier": "scout"}
    return {"excluded": False, "reason": None, "liquidity_tier": None}


def load_instrument_exclusions(path=None):
    """config/instrument_exclusions.json — stock/ETF/index perps (KORU/SOXL/MSTR/NVDA/XAU/…),
    not desk instruments. Missing/malformed file -> empty set (fail OPEN — never blocks the
    sweep on a config-file problem; the exclude-list itself is a convenience filter, not a
    safety gate)."""
    try:
        d = json.loads(Path(path or INSTRUMENT_EXCLUSIONS_PATH).read_text())
        return {str(t).upper() for t in d.get("tickers", [])}
    except Exception:  # noqa: BLE001
        return set()


def is_excluded_instrument(ticker, excluded_set=None):
    excluded_set = excluded_set if excluded_set is not None else load_instrument_exclusions()
    return (ticker or "").upper().replace("USDT", "") in excluded_set


def load_sizing_cfg(path=None):
    try:
        return json.loads(Path(path or SIZING_CFG_PATH).read_text())
    except Exception:  # noqa: BLE001
        return {}


def _aster_book_fetcher():
    import depth as _depth          # sibling-capability import (house style) — reused, not duplicated
    return _depth.VENUES["aster"]


def aster_execution_gate(ticker, direction="SHORT", cfg=None, book_fn=None):
    """SPEC-173 — live Aster execution-depth read: $ notional resting within
    `aster_exit_band_pct` (config/sizing.json, default 2%) of mid on the ENTRY side you'd be a
    taker on (SHORT -> bid, LONG -> ask; matches the scout's 2026-08-28 'Aster bid shelves …
    for the SHORT candidates' evidence). Reuses depth.py's Aster book fetcher (no
    re-implemented HTTP). A missing/empty book excludes CLOSED (aster_book) — an unreadable
    execution venue is never a green light. Returns {excluded, reason, exit_absorbable_usd,
    available}."""
    cfg = cfg if cfg is not None else load_sizing_cfg()
    min_usd = cfg.get("min_aster_exit_usd", DEFAULT_MIN_ASTER_EXIT_USD)
    band_pct = cfg.get("aster_exit_band_pct", DEFAULT_ASTER_EXIT_BAND_PCT)
    fn = book_fn or _aster_book_fetcher()
    sym = ticker.upper().replace("USDT", "") + "USDT"
    try:
        bids, asks, err = fn(sym)
    except Exception as e:  # noqa: BLE001
        return {"excluded": True, "reason": "aster_book", "exit_absorbable_usd": None,
                "available": False, "detail": str(e)[:120]}
    if not bids or not asks:
        return {"excluded": True, "reason": "aster_book", "exit_absorbable_usd": None,
                "available": False, "detail": err or "empty book"}
    mid = (bids[0][0] + asks[0][0]) / 2
    levels = bids if direction.upper() == "SHORT" else asks
    notional = round(sum(p * q for p, q in levels if mid and abs(p - mid) / mid * 100 <= band_pct), 2)
    excluded = notional < min_usd
    return {"excluded": excluded, "reason": "aster_book" if excluded else None,
            "exit_absorbable_usd": notional, "available": True}


def fetch(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        print(f"fetch error: {e}", file=sys.stderr)
        return None


def hl_candidates(meta_ctxs=None, min_vol=10.0):
    """SPEC-84: HL perps as scan candidates (same row shape as the Bybit path), so HL-native names
    are AVAILABLE to scan's universe. Absence-safe — a failed/empty HL fetch yields []. `meta_ctxs`
    injectable (offline tests). HL funding is HOURLY (interval_h:1) → funding_4h = hourly% × 4."""
    import hyperliquid as HL
    if meta_ctxs is None:
        meta_ctxs = HL.post({"type": "metaAndAssetCtxs"})
    if not (isinstance(meta_ctxs, list) and len(meta_ctxs) == 2):
        return []
    meta, ctxs = meta_ctxs
    if not (isinstance(meta, dict) and isinstance(ctxs, list)):
        return []
    out = []
    for u, ctx in zip(meta.get("universe", []), ctxs):
        if not (isinstance(u, dict) and isinstance(ctx, dict) and u.get("name")):
            continue

        def _f(key, c=ctx):
            v = c.get(key)
            try:
                return float(v) if v not in (None, "") else None
            except (ValueError, TypeError):
                return None
        fund_raw, px, vol = _f("funding"), _f("markPx"), _f("dayNtlVlm")
        if fund_raw is None or px is None or vol is None:
            continue
        turnover_m = vol / 1e6
        if turnover_m < min_vol:
            continue
        f = fund_raw * 100              # per-interval (HOURLY) %
        f4 = f * 4.0                    # → %/4h
        prev = _f("prevDayPx")
        chg = ((px - prev) / prev * 100) if prev else 0.0
        out.append({
            "ticker": u["name"], "funding_4h": round(f4, 4), "funding_raw": round(f, 4),
            "interval_h": 1, "turnover_m": round(turnover_m, 1), "chg24": round(chg, 1),
            "oi": _f("openInterest") or 0.0, "price": px, "deep": abs(f4) >= 0.50,
            "venue": "hyperliquid",
        })
    return out


def _fetch_universe(min_vol=10.0, include_hl=False, cross_venue_catalog_rows=None,
                    cross_venue_timeout=None):
    """The liquidity-gated raw candidate list — BEFORE the funding-threshold long/short split.
    Factored out of build_scan (SPEC-120) so a mode that needs the whole universe (funding-flat
    names included, e.g. faded_bounce) can reuse the same fetch/gate without re-fetching.
    Each candidate: {ticker, funding_4h, funding_raw, interval_h, turnover_m, chg24, oi, price, deep}.

    SPEC-160 #1: the single-venue-Bybit liquidity gate made this universe BLIND to names
    thin on Bybit but liquid cross-venue (ONT: $24M cross-venue vol, invisible here at n=88,
    visible to oi_surge's cross-venue catalog at n=1000). `_merge_cross_venue_extras` folds
    in any ticker that clears the SAME liquidity floor on summed cross-venue volume, sharing
    oi_surge's exact catalog+aggregate builder — never a second, diverging universe notion.
    `cross_venue_catalog_rows=[]` (tests) makes the merge an explicit offline no-op."""
    tk = fetch("https://api.bybit.com/v5/market/tickers?category=linear")
    if not tk or tk.get("retCode") != 0:
        raise RuntimeError("could not fetch Bybit tickers")
    rows = tk["result"]["list"]

    inst = fetch("https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000")
    interval = {}
    if inst and inst.get("retCode") == 0:
        for it in inst["result"]["list"]:
            try:
                interval[it["symbol"]] = int(it["fundingInterval"])
            except Exception:
                pass

    cands = []
    for r in rows:
        sym = r["symbol"]
        if not sym.endswith("USDT"):
            continue
        try:
            f = float(r["fundingRate"]) * 100          # per-interval %
            turnover = float(r["turnover24h"])
            chg = float(r["price24hPcnt"]) * 100
            oi = float(r.get("openInterest", 0) or 0)
            px = float(r["lastPrice"])
        except Exception:
            continue
        if turnover < min_vol * 1e6:
            continue
        iv = interval.get(sym, 480)                    # default 8h if unknown
        f4 = f * (240.0 / iv)                          # normalize to %/4h
        cands.append({
            "ticker": sym.replace("USDT", ""),
            "funding_4h": round(f4, 4), "funding_raw": round(f, 4),
            "interval_h": iv // 60, "turnover_m": round(turnover / 1e6, 1),
            "chg24": round(chg, 1), "oi": oi, "price": px,
            "deep": abs(f4) >= 0.50,
        })

    if include_hl:                      # SPEC-84: append HL-native names absent from the Bybit set
        have = {c["ticker"] for c in cands}
        try:
            cands += [c for c in hl_candidates(min_vol=min_vol) if c["ticker"] not in have]
        except Exception:  # noqa: BLE001 — HL never blocks the scan
            pass

    bybit_n = len(cands)
    cands, extras_n = _merge_cross_venue_extras(
        cands, min_vol=min_vol, catalog_rows=cross_venue_catalog_rows,
        per_venue_timeout=cross_venue_timeout)
    print(f"funding scan universe: bybit={bybit_n} cross_venue_extras={extras_n} "
          f"total={len(cands)}", file=sys.stderr)
    return cands


def _merge_cross_venue_extras(cands, min_vol, catalog_rows=None, per_venue_timeout=None):
    """SPEC-160 #1 — fold in tickers that clear the SAME liquidity gate on SUMMED cross-venue
    volume but are thin/absent on Bybit alone. Shares oi_surge's exact catalog+aggregate
    builder (`_fetch_oi_surge_catalog` / `_aggregate_oi_catalog`) rather than re-deriving
    universe logic — one universe builder, per the ticket. Never re-fetches Bybit (reuses the
    rows already in `cands`, converted to catalog-row shape) — avoids re-entering
    `_fetch_universe` via `_catalog_bybit()`. `catalog_rows=[]` (offline tests) is an
    explicit no-op merge; `catalog_rows=None` live-fetches the other 4 venues, best-effort
    (a dead venue contributes nothing, never blocks the scan, SPEC-127 convention).
    Returns (cands_with_extras, n_extras)."""
    have = {c["ticker"] for c in cands}
    if catalog_rows is None:
        other_venues = {k: v for k, v in OI_SURGE_VENUES.items() if k != "bybit"}
        timeout = per_venue_timeout if per_venue_timeout is not None else OI_SURGE_VENUE_TIMEOUT
        try:
            other_rows, _errored = _fetch_oi_surge_catalog(
                venues=other_venues, per_venue_timeout=timeout)
        except Exception:  # noqa: BLE001 — cross-venue merge never blocks the scan
            other_rows = []
        bybit_rows = [{"ticker": c["ticker"] + "USDT", "venue": "bybit",
                       "oi_usd": (c["oi"] or 0.0) * (c["price"] or 0.0),
                       "vol24h_usd": c["turnover_m"] * 1e6,
                       "funding_raw_pct": c["funding_raw"], "interval_min": c["interval_h"] * 60,
                       "is_floor": RF._is_floor((c["funding_raw"] or 0.0) / 100.0)}
                      for c in cands]
        catalog_rows = other_rows + bybit_rows

    agg = _aggregate_oi_catalog(catalog_rows)
    extras = []
    for tk, a in agg.items():
        if tk in have:
            continue
        vol_total = a["vol24h_usd_total"]
        if vol_total < min_vol * 1e6:
            continue
        fx = select_funding_extreme(a["funding_candidates"])
        if fx is None:
            continue
        winner = next((c for c in a["funding_candidates"]
                       if c["venue"] == fx["venue"] and c["pi_4h"] == fx["pi_4h"]), None)
        raw = winner.get("raw_pct") if winner else None
        iv_min = winner.get("interval_min") if winner else None
        extras.append({
            "ticker": tk, "funding_4h": round(fx["pi_4h"], 4),
            "funding_raw": round(raw, 4) if raw is not None else None,
            "interval_h": round(iv_min / 60, 2) if iv_min else None,
            "turnover_m": round(vol_total / 1e6, 1), "chg24": None,
            "oi": round(a["oi_usd_total"], 2) if a["oi_known"] else None, "price": None,
            "deep": abs(fx["pi_4h"]) >= 0.50,
            "venues": sorted(set(a["venues"])), "cross_venue_only": True,
        })
    return cands + extras, len(extras)


def build_scan(min_vol=10.0, side="both", top=25, thresh=0.10, include_hl=False,
               cross_venue_catalog_rows=None):
    """Pure compute: scan the universe → {universe, min_vol_m, thresh, side, longs[], shorts[]}.

    Each candidate: {ticker, funding_4h, funding_raw, interval_h, turnover_m,
    chg24, oi, price, deep, side}. funding_4h is per-interval funding normalized
    to %/4h (never annualized). longs sorted most-negative first; shorts most-positive.

    SPEC-84: include_hl=True additionally merges HL-native perps (names Bybit doesn't carry) so the
    self-custody venue is reachable from the scan; default OFF keeps the Bybit-only run byte-identical.

    SPEC-160 #1: `cross_venue_catalog_rows` — offline/injectable override for the cross-venue
    liquidity merge (see `_merge_cross_venue_extras`); None (default) live-fetches oi_surge's
    other-venue catalog, [] is an explicit no-op (tests).
    """
    cands = _fetch_universe(min_vol=min_vol, include_hl=include_hl,
                            cross_venue_catalog_rows=cross_venue_catalog_rows)
    # SPEC-173: instrument-type exclusion (stock/ETF/index perps — KORU et al are not desk
    # instruments) before the funding-threshold partition.
    excluded_instruments = load_instrument_exclusions()
    cands = [c for c in cands if not is_excluded_instrument(c["ticker"], excluded_instruments)]

    longs = sorted([c for c in cands if c["funding_4h"] <= -thresh], key=lambda c: c["funding_4h"])
    shorts = sorted([c for c in cands if c["funding_4h"] >= thresh], key=lambda c: -c["funding_4h"])
    for c in longs:
        c["side"] = "long"
    for c in shorts:
        c["side"] = "short"
    # SPEC-173: §7 scout-tier tag ($10-25M) — universe is already hard-gated at min_vol by
    # _fetch_universe/_merge_cross_venue_extras, this just labels the marginal band.
    for c in longs + shorts:
        c["liquidity_tier"] = liquidity_gate((c.get("turnover_m") or 0) * 1e6)["liquidity_tier"]

    return {
        "universe": len(cands), "min_vol_m": min_vol, "thresh": thresh, "side": side,
        "longs": longs[:top] if side in ("long", "both") else [],
        "shorts": shorts[:top] if side in ("short", "both") else [],
    }


# ── SPEC-120: faded-bounce discovery mode (the user's primary edge, CLAUDE.md §0.1) ──────────
# memory/feedback_user_edge_faded_distribution_bounces: short the bounce of an already-played-out
# crime coin, still distributing post-dump, after attention faded — no operator floor, no squeeze
# crowd, clean distribution read. Documented defaults; any key overridable via
# config/faded_bounce.json.
FADED_BOUNCE_DEFAULTS = {
    "off_ath_pct": 40.0,       # gate: >= this % below the trailing 90d high (the pump is over)
    "bounce_pct": 15.0,        # gate: >= this % above a post-dump low set within bounce_window_days
    "bounce_window_days": 7,
    # SPEC-138: "faded" = recent-volume SLOPE, not %-of-90d-peak (a one-off historical spike
    # made any later print look "decayed" even mid-active-squeeze — the BICO/SNXX false-pass).
    "vol_slope_recent_days": 3,   # short window: mean volume over the last N days...
    "vol_slope_prior_days": 3,    # ...vs the mean over the N days immediately before that
    "vol_slope_max_ratio": 1.10,  # gate: recent mean <= prior mean * this (flat-or-declining)
    "vol_floor_m": 5.0,        # current 24h vol must clear this absolute $M (tradability)
    "funding_floor_4h": -0.10, # gate: most-extreme non-floor funding >= this (SPEC-108 selection;
                               # deep-neg below this is a HARD EXCLUDE — §5, the anti-pattern)
    "chronic_squeeze_legs_60d": 6,  # gate: squeeze_legs_60d must be <= this (CLAUDE §6: >1
                                     # leg/~10d over 60d = chronic squeezer, never a swing fade;
                                     # subsumes the old squeeze_leg_pct/squeeze_window_h pair —
                                     # that 48h/30% window missed BICO/SNXX's multi-day legs)
    # SPEC-166: no_recent_leg gate — the live-bounce rule (MAGMA/ONG/USELESS class)
    # applied mechanically. A squeeze_pct_threshold-sized leg still inside the last
    # `recent_leg_days`, or a bounce peak younger than `min_days_since_peak`, means the
    # operator hasn't finished the leg yet — 'rolled_over' alone (structure read) doesn't
    # catch a leg from yesterday sitting inside an otherwise-mixed 7-candle window.
    "recent_leg_days": 3,
    "min_days_since_peak": 4,
    "top": 5,
}
FADED_BOUNCE_CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "faded_bounce.json"


def load_faded_bounce_cfg(path=None):
    cfg = dict(FADED_BOUNCE_DEFAULTS)
    try:
        d = json.loads(Path(path or FADED_BOUNCE_CFG_PATH).read_text())
        cfg.update({k: v for k, v in d.items() if k in FADED_BOUNCE_DEFAULTS})
    except Exception:  # noqa: BLE001 — no config file = documented defaults
        pass
    return cfg


def evaluate_faded_bounce_candidate(ticker, c, cfg=None):
    """Pure gate evaluation for ONE candidate (fixture-testable, no network).

    `c` fields: off_ath_pct (negative = below the 90d high), bounce_pct (% above the
    post-dump low), low_age_days (days since that low was set), vol24h_m, vol_peak24h_m
    (pump-peak 24h volume, informational only — see SPEC-138), vol_recent_avg_m/
    vol_prior_avg_m (recent-vs-prior daily-volume means, the "faded" gate's real input),
    funding_4h (SPEC-108 most-extreme non-floor print), squeeze_legs_60d (int, count of
    squeeze legs in the trailing 60d — also the chronic-squeezer gate's input),
    distribution (FRESH|ROTATED|FROZEN|None), on_watchlist (bool), onchain_available
    (bool, default: distribution is not None). structure_read (str, price_structure's
    structure.read — "uptrend"/"downtrend"/"mixed"), lower_highs/higher_highs (int,
    price_structure's structure.lower_highs/higher_highs) and last_swing_higher_high
    (bool, structure.last_swing_higher_high) are the rolled-over gate's inputs (SPEC-160
    #2: WLD/1000BONK passed the other gates while being live uptrends; SPEC-166: MAGMA
    passed with a "mixed" read because a bare lower_highs>=1 check ignored that its
    higher_highs tied/led AND its most recent swing was still a fresh higher high — a
    live squeezer, not a rolled-over bounce). recent_squeeze_leg_days_ago (list[int]) and
    bounce_high_age_days (int, s7's window-high age) are the SPEC-166 no_recent_leg gate's
    inputs — a squeeze leg still inside `recent_leg_days`, or a bounce peak younger than
    `min_days_since_peak`, means the leg isn't over yet regardless of the structure read.

    Returns {ticker, excluded, reason?, gates, squeeze_legs_60d, tier?, onchain?,
    caveats?, rank_score?, ...}. The ten hard gates (SPEC-173: liquidity/not_crypto, then
    off_ath/bounced/faded/funding_ok/not_squeezing/rolled_over/no_recent_leg, then
    aster_book) are an ALL-OR-EXCLUDE filter — any failure excludes the name entirely
    (never surfaced as a candidate). Distribution status
    (FRESH/ROTATED/FROZEN/unavailable) only affects TIER, never exclusion."""
    cfg = cfg or FADED_BOUNCE_DEFAULTS
    gates = {}

    # SPEC-173: §7 hard gates, run FIRST (CLAUDE §7 "liquidity gate FIRST") — liquidity on the
    # SIZE venue, then instrument-type. vol24h_usd wins if the caller supplies it explicitly;
    # else derived from vol24h_m ($M, the existing field every caller already populates).
    vol24h_usd = c.get("vol24h_usd")
    if vol24h_usd is None and c.get("vol24h_m") is not None:
        vol24h_usd = c["vol24h_m"] * 1e6
    lg = liquidity_gate(vol24h_usd)
    gates["liquidity"] = not lg["excluded"]

    instrument_exclusions = cfg.get("instrument_exclusions")
    if instrument_exclusions is None:
        instrument_exclusions = load_instrument_exclusions()
    gates["not_crypto"] = not is_excluded_instrument(ticker, instrument_exclusions)

    off_ath_pct = c.get("off_ath_pct")
    gates["off_ath"] = off_ath_pct is not None and off_ath_pct <= -cfg["off_ath_pct"]

    bounce_pct = c.get("bounce_pct")
    low_age = c.get("low_age_days")
    gates["bounced"] = (bounce_pct is not None and bounce_pct >= cfg["bounce_pct"]
                        and low_age is not None and low_age <= cfg["bounce_window_days"])

    # SPEC-138: "faded" = recent-volume SLOPE (short window vs the window right before it),
    # not distance from a one-off 90d peak — %-of-peak alone false-passed BICO (volume
    # 16→96→132M over 3 sessions, an ACTIVE squeeze) because 132M still read "decayed" next
    # to a much larger historical spike. vol_pct_of_peak is still computed below (informational).
    vol24 = c.get("vol24h_m")
    vol_peak = c.get("vol_peak24h_m")
    vol_pct_of_peak = (vol24 / vol_peak * 100) if (vol24 is not None and vol_peak) else None
    vol_recent = c.get("vol_recent_avg_m")
    vol_prior = c.get("vol_prior_avg_m")
    vol_declining = (vol_recent is not None and vol_prior is not None and vol_prior > 0
                     and vol_recent <= vol_prior * cfg["vol_slope_max_ratio"])
    gates["faded"] = (vol24 is not None and vol24 >= cfg["vol_floor_m"] and vol_declining)

    funding_4h = c.get("funding_4h")
    gates["funding_ok"] = funding_4h is not None and funding_4h >= cfg["funding_floor_4h"]

    # SPEC-138: squeeze_legs_60d subsumes the old squeeze_leg_pct_48h check (too short a
    # window — BICO/SNXX passed it with multiple legs inside 5 days).
    squeeze_legs_60d = int(c.get("squeeze_legs_60d") or 0)
    gates["not_squeezing"] = squeeze_legs_60d <= cfg["chronic_squeeze_legs_60d"]

    # SPEC-160 #2 / SPEC-166: WLD/1000BONK passed all five mechanical gates above while
    # being live uptrends (HH/HL sequences, recent squeeze legs) — price_structure already
    # computes structure.read + structure.lower_highs/higher_highs/last_swing_higher_high
    # (last-7-candle read). Reject a name still in a confirmed uptrend, with no lower-high
    # since the bounce peak, OR (SPEC-166 — the MAGMA fix) whose higher-highs tie/lead its
    # lower-highs AND whose most recent swing was still a fresh higher high: a "mixed" read
    # can hide an active squeezer when lower_highs>=1 alone is the only check. Fails closed
    # (its OWN reason, "structure_unavailable" — never risk a false pass on unreadable data,
    # and never conflate "unreadable" with "confirmed still climbing").
    structure_read = c.get("structure_read")
    lower_highs = c.get("lower_highs")
    higher_highs = c.get("higher_highs")
    last_swing_hh = c.get("last_swing_higher_high")
    structure_unavailable = structure_read is None or lower_highs is None
    if structure_unavailable:
        gates["rolled_over"] = False
    else:
        still_climbing = (higher_highs is not None and higher_highs >= lower_highs
                          and bool(last_swing_hh))
        gates["rolled_over"] = (structure_read != "uptrend" and lower_highs != 0
                                and not still_climbing)

    # SPEC-166 #3: a squeeze leg still inside `recent_leg_days`, or a bounce peak younger
    # than `min_days_since_peak`, means the leg isn't over — applies even when the 7-candle
    # structure read already looks rolled-over (a leg from yesterday can sit inside an
    # otherwise-mixed window). Absent data (fields not provided) defaults to PASS — this
    # gate only excludes on a POSITIVELY known recent leg/peak, never on missing data (that
    # risk is already covered by the rolled_over gate's own fail-closed behavior above).
    recent_leg_days_ago = c.get("recent_squeeze_leg_days_ago") or []
    recent_leg_hit = any(d <= cfg["recent_leg_days"] for d in recent_leg_days_ago)
    bounce_high_age = c.get("bounce_high_age_days")
    peak_too_recent = bounce_high_age is not None and bounce_high_age < cfg["min_days_since_peak"]
    gates["no_recent_leg"] = not recent_leg_hit and not peak_too_recent

    # SPEC-173: Aster execution gate — the candidate must clear a minimum resting notional on
    # the desk's execution venue (config/sizing.json min_aster_exit_usd, live-computed by the
    # caller and passed in as exit_absorbable_usd; see aster_execution_gate()). None (not yet
    # computed / venue read failed) fails CLOSED — an unread execution book is not a pass.
    exit_absorbable_usd = c.get("exit_absorbable_usd")
    min_aster_usd = cfg.get("min_aster_exit_usd")
    if min_aster_usd is None:
        min_aster_usd = load_sizing_cfg().get("min_aster_exit_usd", DEFAULT_MIN_ASTER_EXIT_USD)
    gates["aster_book"] = exit_absorbable_usd is not None and exit_absorbable_usd >= min_aster_usd

    hard_gates = ("liquidity", "not_crypto", "off_ath", "bounced", "faded", "funding_ok",
                 "not_squeezing", "rolled_over", "no_recent_leg", "aster_book")
    gate_reason = {"no_recent_leg": "recent_leg"}
    failed = [g for g in hard_gates if not gates[g]]
    if failed:
        primary = failed[0]
        if primary == "rolled_over":
            reason = "structure_unavailable" if structure_unavailable else "not_rolled_over"
        else:
            reason = gate_reason.get(primary, primary)
        return {"ticker": ticker, "excluded": True, "reason": reason,
                "failed_gates": failed, "gates": gates, "squeeze_legs_60d": squeeze_legs_60d,
                "liquidity_tier": lg["liquidity_tier"], "exit_absorbable_usd": exit_absorbable_usd}

    distribution = c.get("distribution")
    onchain_available = c.get("onchain_available", distribution is not None)
    on_watchlist = bool(c.get("on_watchlist"))
    caveats = []
    if not onchain_available:
        tier, onchain_note = "tier-2", "unavailable"
    elif on_watchlist and distribution in ("FRESH", "ROTATED"):
        tier, onchain_note = "tier-1", distribution
    elif distribution == "FROZEN":
        tier, onchain_note = "tier-2", distribution
        caveats.append("squeeze-risk: distribution FROZEN — genuine pause or reload risk "
                       "(VELVET lesson), treat as squeeze-risk not a clean fade")
    else:
        tier, onchain_note = "tier-2", (distribution or "unavailable")

    vol_decay_depth = round((100 - vol_pct_of_peak) / 100, 4) if vol_pct_of_peak is not None else 0.0
    rank_score = round((bounce_pct or 0.0) * vol_decay_depth, 4)
    next_step = (f"verify_wallet <top-holder> / brief {ticker}" if tier == "tier-1"
                else f"brief {ticker}")
    return {
        "ticker": ticker, "excluded": False, "tier": tier, "onchain": onchain_note,
        "gates": gates, "caveats": caveats, "rank_score": rank_score,
        "off_ath_pct": off_ath_pct, "bounce_pct": bounce_pct,
        "vol_pct_of_peak": round(vol_pct_of_peak, 1) if vol_pct_of_peak is not None else None,
        "squeeze_legs_60d": squeeze_legs_60d,
        "funding_4h": funding_4h, "next_step": next_step,
        "structure_read": structure_read, "lower_highs": lower_highs,
        "higher_highs": higher_highs,
        "recent_squeeze_legs": c.get("recent_squeeze_legs", []),
        "liquidity_tier": lg["liquidity_tier"], "exit_absorbable_usd": exit_absorbable_usd,
    }


def build_faded_bounce(candidates, cfg=None, skipped=None):
    """Partition/rank a {ticker: candidate-fields} dict → the faded_bounce contract.
    Tier-1 first, then bounce size × volume-decay depth (rank_score). Capped at cfg['top'];
    zero matches is a normal explicit result (candidates: []), never an error.

    SPEC-166 #4: `rejected` ({ticker: reason}) and `gate_fail_counts` ({gate: n}) surface
    WHY the sweep died on each name — not just the raw excluded rows — so a session-open
    read doesn't have to reconstruct it by hand from `excluded`.

    SPEC-183: `skipped` ([{ticker, reason}]) is the per-name-timeout casualty list from
    `_faded_bounce_live_candidates` — names that never finished enrichment, distinct from
    `excluded` (names that finished and failed a gate). Defaults to [] for the offline/
    injected-candidates path (--fb-candidates), where nothing timed out because nothing
    was fetched."""
    cfg = {**FADED_BOUNCE_DEFAULTS, **(cfg or {})}
    results = [evaluate_faded_bounce_candidate(tk, c, cfg) for tk, c in candidates.items()]
    excluded = [r for r in results if r["excluded"]]
    passed = [r for r in results if not r["excluded"]]
    passed.sort(key=lambda r: (0 if r["tier"] == "tier-1" else 1, -r["rank_score"]))
    top = cfg["top"]
    dropped = passed[top:]
    hard_gates = ("liquidity", "not_crypto", "off_ath", "bounced", "faded", "funding_ok",
                 "not_squeezing", "rolled_over", "no_recent_leg", "aster_book")
    gate_fail_counts = {g: sum(1 for r in excluded if not r["gates"].get(g, True))
                        for g in hard_gates}
    return {
        "mode": "faded_bounce", "cfg": cfg, "universe": len(candidates),
        "candidates": passed[:top], "excluded": excluded,
        "dropped_below_top": [r["ticker"] for r in dropped],
        "rejected": {r["ticker"]: r["reason"] for r in excluded},
        "gate_fail_counts": gate_fail_counts,
        "skipped": skipped or [],
    }


def _faded_bounce_structure_fields(ticker, s90):
    """SPEC-166 #1 — pull price_structure's `structure` sub-block out of an already-fetched
    s90 dict. Any malformed/missing block fails LOUD (stderr, per-ticker) and CLOSED (every
    field None) — a silent None here is exactly the SPEC-137 class the ticket calls out;
    the caller (evaluate_faded_bounce_candidate) then excludes with reason
    'structure_unavailable' rather than risking a false pass."""
    try:
        structure = s90.get("structure")
        if not isinstance(structure, dict):
            raise ValueError(f"structure block missing/malformed: {structure!r}")
        return {
            "structure_read": structure.get("read"),
            "lower_highs": structure.get("lower_highs"),
            "higher_highs": structure.get("higher_highs"),
            "last_swing_higher_high": structure.get("last_swing_higher_high"),
        }
    except Exception as e:  # noqa: BLE001 — never abort the candidate, degrade + log
        print(f"faded_bounce structure {ticker}: {e}", file=sys.stderr)
        return {"structure_read": None, "lower_highs": None,
                "higher_highs": None, "last_swing_higher_high": None}


def _faded_bounce_recent_leg_fields(s90, s7, cfg):
    """SPEC-166 #3 — squeeze-recency inputs for the no_recent_leg gate, computed from the
    already-fetched s90 (90d, carries `squeezes`) / s7 (7d, carries the bounce peak's age
    via `days_since_ath`) pair — no extra fetch. `recent_squeeze_leg_days_ago`: days-ago for
    every squeeze leg in the 90d window (the gate itself filters to `recent_leg_days`).
    `recent_squeeze_legs`: last 3 (day, pct) for display. `bounce_high_age_days`: age of the
    CURRENT bounce's peak (the 7d window high), not the all-time pump top."""
    squeezes = s90.get("squeezes") or []
    today = datetime.now(timezone.utc).date()
    days_ago = []
    for sq in squeezes:
        d = sq.get("day")
        if not d:
            continue
        try:
            days_ago.append((today - datetime.strptime(d, "%Y-%m-%d").date()).days)
        except Exception:  # noqa: BLE001 — a malformed date entry never blocks the rest
            continue
    recent_legs = [{"day": sq.get("day"), "pct": sq.get("pct")} for sq in squeezes[-3:]]
    return {
        "recent_squeeze_leg_days_ago": days_ago,
        "recent_squeeze_legs": recent_legs,
        "bounce_high_age_days": s7.get("days_since_ath"),
    }


FADED_BOUNCE_NAME_TIMEOUT_S = 15.0   # SPEC-183: one name's enrichment budget (2x
                                      # build_structure + the aster book fetch) — a slow or
                                      # hanging name costs itself (-> skipped, reason
                                      # "timeout"), never the whole sweep.
FADED_BOUNCE_MAX_WORKERS = 12        # concurrency fan-out, same convention as
                                      # _fetch_oi_surge_catalog's per-venue ThreadPoolExecutor


def _faded_bounce_enrich_ticker(tk, univ_row, on_watchlist, cfg):
    """SPEC-183: the per-NAME enrichment body — 90d/7d price-structure
    (price_structure.build_structure) for off_ath/bounce/vol-peak, the 60d squeeze-leg
    count, the persisted rotation_freshness read (watchlist names only), and the Aster
    execution-depth gate. Factored out of `_faded_bounce_live_candidates` so it can run
    inside a ThreadPoolExecutor fan-out (the timeout fix — this used to be a serial loop
    over the whole universe) AND be unit-tested/injected directly, e.g. a fixture that
    hangs one name.

    `univ_row` is this ticker's already-fetched _fetch_universe row (or None — a
    watchlist/retired ticker thin on the size venue). Returns the candidate-fields dict,
    or None if price_structure has no data for this ticker (unlisted/thin history — a
    normal exclusion, never a timeout)."""
    import price_structure as PS
    try:
        s90 = PS.build_structure(tk, days=90)
        if s90.get("error"):
            return None
        s7 = PS.build_structure(tk, days=7)
    except Exception:
        return None
    vol_peak24h_m = max((sq.get("vol_m") or 0) for sq in s90.get("squeezes", [])) if s90.get("squeezes") else None
    vol24h_m = univ_row["turnover_m"] if univ_row else None
    funding_4h = univ_row["funding_4h"] if univ_row else None
    # SPEC-138: recent-volume-SLOPE (short window vs the window right before it), not
    # %-of-90d-peak — a one-off historical spike made later prints look "decayed" even
    # mid-active-squeeze (BICO/SNXX). Insufficient daily history → both None → the "faded"
    # gate fails closed (excludes rather than risks a false pass).
    vol_daily = s90.get("vol_daily_m") or []
    rd, pd_ = cfg["vol_slope_recent_days"], cfg["vol_slope_prior_days"]
    vol_recent_avg_m = vol_prior_avg_m = None
    if len(vol_daily) >= rd + pd_:
        recent_win, prior_win = vol_daily[-rd:], vol_daily[-(rd + pd_):-rd]
        vol_recent_avg_m = round(sum(recent_win) / len(recent_win), 2)
        vol_prior_avg_m = round(sum(prior_win) / len(prior_win), 2)
    # 60d squeeze-history count (chronic-squeezer gate input, CLAUDE §6) — s90 already
    # covers the window; SPEC-173: reuse price_structure's OWN windowing helper (no extra
    # fetch, no re-derived cutoff loop — the divergence bug this fixes).
    squeeze_legs_60d = PS.squeeze_legs_in_window(s90.get("squeezes", []), 60)
    onchain_available = False
    distribution = None
    if on_watchlist:
        try:
            import rotation_freshness as RF
            st = RF.read_state(tk)
            if st and st.get("verdict"):
                distribution = st["verdict"]
                onchain_available = True
        except Exception:
            pass
    # SPEC-160 #2 / SPEC-166: price_structure already computes structure.read/
    # lower_highs/higher_highs/last_swing_higher_high (last-7-candle read) and the
    # squeeze-leg list — no extra fetch, reuse the s90/s7 calls already made above.
    structure_fields = _faded_bounce_structure_fields(tk, s90)
    recent_leg_fields = _faded_bounce_recent_leg_fields(s90, s7, cfg)
    # SPEC-173: Aster execution gate — faded_bounce is always a SHORT (§0.1), so the
    # relevant book side is the bid (the ENTRY side you'd be a taker on). Best-effort:
    # a dead/thin-book read never crashes the sweep, just excludes THAT candidate.
    try:
        exit_absorbable_usd = aster_execution_gate(tk, direction="SHORT")["exit_absorbable_usd"]
    except Exception as e:  # noqa: BLE001
        print(f"faded_bounce aster gate {tk}: {e}", file=sys.stderr)
        exit_absorbable_usd = None
    return {
        "off_ath_pct": s90.get("off_ath_pct"),
        "bounce_pct": round((s90["current_close"] / s7["atl"] - 1) * 100, 2) if s7.get("atl") else None,
        "low_age_days": s7.get("days_since_atl"),
        "vol24h_m": vol24h_m, "vol_peak24h_m": vol_peak24h_m,
        "vol_recent_avg_m": vol_recent_avg_m, "vol_prior_avg_m": vol_prior_avg_m,
        "funding_4h": funding_4h, "squeeze_legs_60d": squeeze_legs_60d,
        "distribution": distribution, "on_watchlist": on_watchlist,
        "onchain_available": onchain_available, "exit_absorbable_usd": exit_absorbable_usd,
        **structure_fields, **recent_leg_fields,
    }


def _faded_bounce_live_candidates(cfg=None, enrich_fn=None, name_timeout_s=None, max_workers=None):
    """Live assembly (best-effort, never crashes the mode): Bybit universe (liquidity-gated,
    reusing _fetch_universe) UNION watchlist tickers (committed + retired within
    bounce_window_days*2 days — the edge's names are often just-retired board names).

    SPEC-183: per-name enrichment (`_faded_bounce_enrich_ticker` — 90d/7d price-structure,
    cross-venue funding, rotation_freshness, the Aster execution gate) fans out over a
    ThreadPoolExecutor instead of a serial loop (the >120s timeout on the full universe),
    bounded by a per-name timeout (`name_timeout_s`, default FADED_BOUNCE_NAME_TIMEOUT_S) —
    a name that doesn't finish in time is dropped into `skipped` ({ticker, reason}) rather
    than stalling the sweep (§3: skipped is LISTED, never silently dropped).
    `enrich_fn`/`name_timeout_s`/`max_workers` are injection points for tests.

    Returns (candidates: {ticker: fields}, skipped: [{ticker, reason}])."""
    cfg = cfg or load_faded_bounce_cfg()
    enrich_fn = enrich_fn or _faded_bounce_enrich_ticker
    name_timeout_s = FADED_BOUNCE_NAME_TIMEOUT_S if name_timeout_s is None else name_timeout_s
    max_workers = max_workers or FADED_BOUNCE_MAX_WORKERS
    cands, skipped = {}, []
    try:
        universe = _fetch_universe(min_vol=cfg["vol_floor_m"])
    except Exception as e:  # noqa: BLE001 — a dead Bybit fetch never kills the mode
        print(f"faded_bounce universe fetch: {e}", file=sys.stderr)
        universe = []
    tickers = {c["ticker"] for c in universe}
    univ_by_tk = {c["ticker"]: c for c in universe}

    watchlist_tickers, retired_tickers = set(), set()
    try:
        wl = json.loads((Path(__file__).resolve().parent.parent / "config" / "watchlist.json").read_text())
        watchlist_tickers = {t["ticker"] for t in wl.get("tokens", []) if t.get("ticker")}
        cutoff_days = cfg["bounce_window_days"] * 2
        import time as _time
        now = _time.time()
        for r in wl.get("retired", []):
            tk, rd = r.get("ticker"), r.get("retired_date")
            if not (tk and rd):
                continue
            try:
                age_days = (now - datetime.strptime(rd, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) / 86400
            except Exception:
                continue
            if age_days <= cutoff_days:
                retired_tickers.add(tk)
    except Exception as e:  # noqa: BLE001
        print(f"faded_bounce watchlist union: {e}", file=sys.stderr)

    all_tickers = tickers | watchlist_tickers | retired_tickers
    if not all_tickers:
        return cands, skipped

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(all_tickers)))) as ex:
        futs = {ex.submit(enrich_fn, tk, univ_by_tk.get(tk), tk in watchlist_tickers, cfg): tk
               for tk in all_tickers}
        for fut, tk in futs.items():
            try:
                result = fut.result(timeout=name_timeout_s)
            except FuturesTimeout:
                skipped.append({"ticker": tk, "reason": "timeout"})
                continue
            except Exception as e:  # noqa: BLE001 — one bad name never kills the sweep
                print(f"faded_bounce enrich {tk}: {e}", file=sys.stderr)
                skipped.append({"ticker": tk, "reason": "error"})
                continue
            if result is not None:
                cands[tk] = result
    return cands, skipped


def render_faded_bounce_human(board):
    print(C.c("═══ FADED-BOUNCE SCREEN ═══", "bold", "cyan")
          + C.c("  the user's primary edge — off-ATH + bounced + faded + funding-flat + not-squeezing", "grey"))
    if not board["candidates"]:
        print(C.c("\n  none — no name clears all five gates right now (§0.5: NONE is the answer)", "grey"))
    for r in board["candidates"]:
        tag = C.c(r["tier"].upper(), "bold", "green" if r["tier"] == "tier-1" else "yellow")
        print(f"  {C.c(r['ticker'], 'bold'):<12} {tag}  off_ath {r['off_ath_pct']:+.1f}%  "
              f"bounce {r['bounce_pct']:+.1f}%  vol {r['vol_pct_of_peak']}% of peak  "
              f"funding {r['funding_4h']:+.3f}%/4h  squeeze_legs_60d {r['squeeze_legs_60d']}  "
              f"onchain {r['onchain']}  → {r['next_step']}")
        for cv in r["caveats"]:
            print(C.c(f"    ⚠ {cv}", "yellow"))
        try:
            # SPEC-169: faded_bounce is always the SHORT side (§0.6); no committed
            # geometry yet at discovery time, so the line covers tier/equity/lev/liq —
            # resolve_maxsize=False (board loop, never one live book walk per candidate).
            rc = RC.live_risk_line(r["ticker"], "faded_bounce", "SHORT", resolve_maxsize=False)
            print(f"    {rc['line']}")
        except Exception as e:  # noqa: BLE001 — a risk-line failure must never break the board
            print(f"    risk_card unavailable: {e}", file=sys.stderr)
    if board["excluded"]:
        print(C.c(f"\n  excluded ({len(board['excluded'])}): "
                  + ", ".join(f"{r['ticker']}[{r['reason']}]" for r in board["excluded"][:10]), "grey"))
    if board.get("skipped"):
        print(C.c(f"\n  skipped ({len(board['skipped'])}, ran out of per-name time budget): "
                  + ", ".join(f"{s['ticker']}[{s['reason']}]" for s in board["skipped"][:10]), "yellow"))


# ── SPEC-148: oi_surge — cross-sectional discovery net (retires the manual loris.tools/markets
# browse, EVAL-loris.md §3.3). Across the whole perp universe, where is OI being built RIGHT NOW,
# and which of those names carry the desk's tells (vol/OI brushing, funding extremity, fresh
# listings)? Baseline-driven: state/oi_surge_baseline.json persists a per-ticker OI/vol snapshot
# so a later sweep can compute a Δ. Read-only — no thesis writes, discovery only.
OI_SURGE_DEFAULTS = {
    "oi_surge_pct": 40.0,           # gate: cross-venue summed OI delta vs baseline
    "vol_oi_ratio_min": 20.0,       # gate: 24h vol / OI (§2 Cat-A brushing tell)
    "funding_extreme_thresh_4h": 0.20,  # gate: |most-extreme non-floor cross-venue print| %/4h
    # SPEC-173: the discovery floor is now the SAME hard §7 gate every scan mode carries
    # (liquidity_gate(): <$10M excluded:liquidity, $10-25M tagged liquidity_tier:scout) —
    # no separate "one shelf below §7" number (the scout sweep 2026-08-28 finding: that
    # looser floor was exactly what let sub-$10M names through by hand).
    "baseline_max_age_h": 24.0,     # oi_surge only fires vs a baseline this fresh
    "top": 10,
    "exclude": ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "TRX",
                "LINK", "DOT", "MATIC", "LTC", "SHIB", "TON", "SUI", "NEAR", "USDT", "USDC"],
}
OI_SURGE_CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "oi_surge.json"
OI_SURGE_BASELINE_PATH = Path(__file__).resolve().parent.parent / "state" / "oi_surge_baseline.json"
OI_SURGE_VENUE_TIMEOUT = 6   # per-venue catalog-fetch budget, SPEC-127 lesson


def load_oi_surge_cfg(path=None):
    cfg = dict(OI_SURGE_DEFAULTS)
    try:
        d = json.loads(Path(path or OI_SURGE_CFG_PATH).read_text())
        cfg.update({k: v for k, v in d.items() if k in OI_SURGE_DEFAULTS})
    except Exception:  # noqa: BLE001 — no config file = documented defaults
        pass
    return cfg


def select_funding_extreme(candidates):
    """SPEC-108/112 selection: the most-extreme (max |pi_4h|) NON-floor print. A floor print
    never wins regardless of its raw magnitude (floors are tiny anyway, but this must hold
    even if one were somehow large). `candidates`: [{venue, pi_4h, is_floor}]. None if every
    candidate is a floor print or the list is empty."""
    non_floor = [c for c in candidates if not c.get("is_floor") and c.get("pi_4h") is not None]
    if not non_floor:
        return None
    best = max(non_floor, key=lambda c: abs(c["pi_4h"]))
    return {"venue": best["venue"], "pi_4h": best["pi_4h"]}


def _aggregate_oi_catalog(rows):
    """Per-venue-per-symbol catalog rows -> per-ticker cross-venue aggregate. Each input row:
    {ticker, venue, oi_usd, vol24h_usd, funding_raw_pct, interval_min, is_floor}. funding_raw_pct
    is normalized to %/4h HERE (SPEC-112) before it's ever compared across venues/intervals."""
    agg = {}
    for r in rows:
        tk = r["ticker"].upper().replace("USDT", "")
        a = agg.setdefault(tk, {"venues": [], "oi_usd_total": 0.0, "oi_known": False,
                                "vol24h_usd_total": 0.0, "funding_candidates": []})
        a["venues"].append(r["venue"])
        if r.get("oi_usd") is not None:
            a["oi_usd_total"] += r["oi_usd"]
            a["oi_known"] = True
        if r.get("vol24h_usd") is not None:
            a["vol24h_usd_total"] += r["vol24h_usd"]
        if r.get("funding_raw_pct") is not None and r.get("interval_min"):
            pi4h = RF.to_4h(r["funding_raw_pct"], r["interval_min"])
            a["funding_candidates"].append({"venue": r["venue"], "pi_4h": pi4h,
                                            "is_floor": bool(r.get("is_floor")),
                                            "raw_pct": r["funding_raw_pct"],
                                            "interval_min": r["interval_min"]})
    return agg


def _dex_share_pct(ticker):
    """Report-only (§0.5 / config/dex_mark_weight.json doctrine) — never gates. None when
    the ticker isn't in the manually-populated weight file."""
    try:
        d = json.loads((Path(__file__).resolve().parent.parent
                        / "config" / "dex_mark_weight.json").read_text())
        w = (d.get("tokens") or {}).get(ticker)
        return round(w * 100, 2) if w is not None else None
    except Exception:  # noqa: BLE001
        return None


def _evaluate_oi_surge_row(ticker, a, baseline_entry, cfg, now_ts):
    exclude_set = {x.upper() for x in cfg.get("exclude", [])}
    if ticker in exclude_set:
        return {"ticker": ticker, "excluded": True, "reason": "exclude_list"}

    # SPEC-173: instrument-type exclusion (stock/ETF/index perps — KORU et al) before the
    # liquidity math even runs; cfg["instrument_exclusions"] is a test/injection override,
    # else lazy-loaded from config/instrument_exclusions.json.
    instrument_exclusions = cfg.get("instrument_exclusions")
    if instrument_exclusions is None:
        instrument_exclusions = load_instrument_exclusions()
    if is_excluded_instrument(ticker, instrument_exclusions):
        return {"ticker": ticker, "excluded": True, "reason": "not_crypto"}

    vol_total = a["vol24h_usd_total"]
    lg = liquidity_gate(vol_total)
    if lg["excluded"]:
        return {"ticker": ticker, "excluded": True, "reason": "liquidity",
                "vol24h_usd": round(vol_total, 2)}

    oi_total = a["oi_usd_total"] if a["oi_known"] else None
    funding_extreme = select_funding_extreme(a["funding_candidates"])
    vol_oi_ratio = round(vol_total / oi_total, 4) if oi_total else None

    is_new = baseline_entry is None
    new_listing = {"venue": a["venues"][0]} if (is_new and a["venues"]) else None

    oi_delta_pct = None
    baseline_age_h = None
    oi_surge_fired = False
    if baseline_entry and baseline_entry.get("oi_usd_total") and oi_total is not None:
        baseline_age_h = round((now_ts - baseline_entry["ts"]) / 3600.0, 2)
        if baseline_age_h <= cfg["baseline_max_age_h"]:
            base_oi = baseline_entry["oi_usd_total"]
            if base_oi > 0:
                oi_delta_pct = round((oi_total / base_oi - 1) * 100, 2)
                oi_surge_fired = oi_delta_pct >= cfg["oi_surge_pct"]

    vol_oi_brush_fired = vol_oi_ratio is not None and vol_oi_ratio >= cfg["vol_oi_ratio_min"]
    funding_extreme_fired = (funding_extreme is not None
                             and abs(funding_extreme["pi_4h"]) >= cfg["funding_extreme_thresh_4h"])

    flags = []
    if oi_surge_fired:
        flags.append("oi_surge")
    if vol_oi_brush_fired:
        flags.append("vol_oi_brush")
    if funding_extreme_fired:
        flags.append("funding_extreme")
    if is_new:
        flags.append("new_listing")

    return {
        "ticker": ticker, "excluded": False, "venues": sorted(set(a["venues"])),
        "oi_usd_total": round(oi_total, 2) if oi_total is not None else None,
        "oi_delta_pct": oi_delta_pct, "baseline_age_h": baseline_age_h,
        "vol24h_usd": round(vol_total, 2), "vol_oi_ratio": vol_oi_ratio,
        "funding_extreme": funding_extreme, "new_listing": new_listing, "flags": flags,
        "dex_share_pct": _dex_share_pct(ticker),
        # SPEC-160 (minor): flat funding_4h/vol_m — triage's own row-naming convention —
        # lifted up from the nested funding_extreme/vol24h_usd so a generic ticker-row
        # consumer doesn't need to know oi_surge's nested shape.
        "funding_4h": funding_extreme["pi_4h"] if funding_extreme else None,
        "vol_m": round(vol_total / 1e6, 2),
        "liquidity_tier": lg["liquidity_tier"],
        "next_step": f"brief {ticker}",
    }


def build_oi_surge(catalog_rows, baseline=None, cfg=None, seeded=False, venues_errored=None,
                   now_ts=None):
    """Pure compute: catalog rows + a baseline snapshot -> the oi_surge board.

    `baseline`: {ticker: {ts, oi_usd_total, vol24h_usd_total, first_seen_ts}} — the WHOLE
    baseline store (a ticker absent from it entirely is `new_listing`, never "excluded").
    `seeded=True` means the baseline STORE itself is missing (first run ever) — the whole
    sweep returns zero candidates, status "seeded", and a computed baseline_out to persist;
    this is a different condition from any one ticker being new (SPEC-137: a missing
    baseline must never look like a clean empty sweep — here it's explicit, never silent).
    Primary sort: number of flags fired (desc); tiebreak: oi_delta_pct magnitude (desc)."""
    cfg = {**OI_SURGE_DEFAULTS, **(cfg or {})}
    baseline = baseline or {}
    venues_errored = venues_errored or []
    now_ts = now_ts if now_ts is not None else time.time()

    agg = _aggregate_oi_catalog(catalog_rows)
    baseline_out = {
        tk: {"ts": now_ts,
             "oi_usd_total": a["oi_usd_total"] if a["oi_known"] else None,
             "vol24h_usd_total": a["vol24h_usd_total"],
             "first_seen_ts": (baseline.get(tk) or {}).get("first_seen_ts", now_ts)}
        for tk, a in agg.items()
    }

    if seeded:
        return {"mode": "oi_surge", "status": "seeded", "cfg": cfg, "universe": len(agg),
                "candidates": [], "excluded": [], "dropped_below_top": [],
                "baseline_out": baseline_out, "meta": {"venues_errored": venues_errored}}

    results = [_evaluate_oi_surge_row(tk, a, baseline.get(tk), cfg, now_ts) for tk, a in agg.items()]
    excluded = [r for r in results if r["excluded"]]
    passed = [r for r in results if not r["excluded"]]
    passed.sort(key=lambda r: (-len(r["flags"]), -(r["oi_delta_pct"] or 0.0)))
    top = cfg["top"]
    dropped = passed[top:]
    return {"mode": "oi_surge", "status": "ok", "cfg": cfg, "universe": len(agg),
            "candidates": passed[:top], "excluded": excluded,
            "dropped_below_top": [r["ticker"] for r in dropped],
            "baseline_out": baseline_out, "meta": {"venues_errored": venues_errored}}


def load_oi_surge_baseline(path=None):
    """Returns (baseline_dict, status). status: "ok" (usable), "missing" (no file — first
    run, SEED not NOTOK), "corrupt" (file present but unreadable/malformed — LOUD, SPEC-137:
    never silently re-seeded mid-verdict)."""
    p = Path(path) if path else OI_SURGE_BASELINE_PATH
    if not p.exists():
        return {}, "missing"
    try:
        d = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}, "corrupt"
    if not isinstance(d, dict):
        return {}, "corrupt"
    return d, "ok"


def save_oi_surge_baseline(baseline_out, path=None):
    p = Path(path) if path else OI_SURGE_BASELINE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(baseline_out))


# ---- live catalog venue adapters (catalog_<venue>() -> list[row], best-effort) -------------
# Each row: {ticker, venue, oi_usd, vol24h_usd, funding_raw_pct, interval_min, is_floor}.
# oi_usd/funding fields are None where a venue has no cheap bulk endpoint for them — the
# aggregation layer treats a missing field as "unknown", never as zero (never fabricates a
# datum, CLAUDE.md §3). Binance/Aster expose no bulk per-symbol OI endpoint (verified against
# their documented API surface, 2026-08-19) — catalog-wide OI for those two is a known gap;
# Bybit/Bitget/Hyperliquid all return OI in their bulk ticker responses and carry the signal.

def _catalog_bybit():
    cands = _fetch_universe(min_vol=0.0)
    rows = []
    for c in cands:
        oi_usd = (c["oi"] or 0.0) * (c["price"] or 0.0)
        raw_frac = (c["funding_raw"] or 0.0) / 100.0
        rows.append({"ticker": c["ticker"], "venue": "bybit",
                     "oi_usd": oi_usd, "vol24h_usd": c["turnover_m"] * 1e6,
                     "funding_raw_pct": c["funding_raw"], "interval_min": c["interval_h"] * 60,
                     "is_floor": RF._is_floor(raw_frac)})
    return rows


def _catalog_hyperliquid():
    rows = []
    for c in hl_candidates(min_vol=0.0):
        oi_usd = (c["oi"] or 0.0) * (c["price"] or 0.0)
        rows.append({"ticker": c["ticker"], "venue": "hyperliquid",
                     "oi_usd": oi_usd, "vol24h_usd": c["turnover_m"] * 1e6,
                     "funding_raw_pct": c["funding_raw"], "interval_min": c["interval_h"] * 60,
                     "is_floor": c["funding_raw"] == 0.0})
    return rows


def _catalog_binance():
    """Bulk premiumIndex (funding+mark) + ticker/24hr (volume). No bulk OI endpoint —
    oi_usd is None (see module note above); funding+volume+new_listing still carry."""
    pi = fetch("https://fapi.binance.com/fapi/v1/premiumIndex")
    t24 = fetch("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not isinstance(pi, list) or not isinstance(t24, list):
        raise RuntimeError("binance catalog fetch failed")
    vol_by_sym = {x["symbol"]: x for x in t24 if isinstance(x, dict) and x.get("symbol")}
    rows = []
    for x in pi:
        sym = x.get("symbol", "")
        if not sym.endswith("USDT") or sym not in vol_by_sym:
            continue
        try:
            funding_raw = float(x["lastFundingRate"])
            vol_usd = float(vol_by_sym[sym]["quoteVolume"])
        except (KeyError, TypeError, ValueError):
            continue
        interval_min = RF.binance_interval_min(sym)
        rows.append({"ticker": sym.replace("USDT", ""), "venue": "binance",
                     "oi_usd": None, "vol24h_usd": vol_usd,
                     "funding_raw_pct": funding_raw * 100, "interval_min": interval_min,
                     "is_floor": RF._is_floor(funding_raw)})
    return rows


def _catalog_aster():
    """Same shape as Binance (Aster mirrors the Binance Futures API); no bulk OI endpoint."""
    pi = fetch("https://fapi.asterdex.com/fapi/v1/premiumIndex")
    t24 = fetch("https://fapi.asterdex.com/fapi/v1/ticker/24hr")
    if not isinstance(pi, list) or not isinstance(t24, list):
        raise RuntimeError("aster catalog fetch failed")
    vol_by_sym = {x["symbol"]: x for x in t24 if isinstance(x, dict) and x.get("symbol")}
    rows = []
    for x in pi:
        sym = x.get("symbol", "")
        if not sym.endswith("USDT") or sym not in vol_by_sym:
            continue
        try:
            funding_raw = float(x["lastFundingRate"])
            vol_usd = float(vol_by_sym[sym]["quoteVolume"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append({"ticker": sym.replace("USDT", ""), "venue": "aster",
                     "oi_usd": None, "vol24h_usd": vol_usd,
                     "funding_raw_pct": funding_raw * 100, "interval_min": 240,
                     "is_floor": RF._is_floor(funding_raw)})
    return rows


def _catalog_bitget():
    """Bulk ticker (OI + volume) + best-effort bulk funding; a dead/changed funding leg
    degrades that field to None per-row (never blocks OI/vol/new_listing)."""
    tick = fetch("https://api.bitget.com/api/v2/mix/market/tickers?productType=usdt-futures")
    if not (isinstance(tick, dict) and tick.get("code") == "00000"
           and isinstance(tick.get("data"), list)):
        raise RuntimeError("bitget catalog fetch failed")
    rows = []
    for x in tick["data"]:
        sym = x.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            price = float(x["lastPr"])
            oi_usd = float(x["holdingAmount"]) * price if x.get("holdingAmount") else None
            vol_usd = float(x["usdtVolume"]) if x.get("usdtVolume") not in (None, "") else None
        except (KeyError, TypeError, ValueError):
            continue
        funding_raw_pct = interval_min = None
        is_floor = False
        if x.get("fundingRate") not in (None, ""):
            try:
                fr = float(x["fundingRate"])
                funding_raw_pct = fr * 100
                interval_min = int(float(x.get("fundingRateInterval") or 8)) * 60
                is_floor = RF._is_floor(fr)
            except (TypeError, ValueError):
                funding_raw_pct = interval_min = None
        rows.append({"ticker": sym.replace("USDT", ""), "venue": "bitget",
                     "oi_usd": oi_usd, "vol24h_usd": vol_usd,
                     "funding_raw_pct": funding_raw_pct, "interval_min": interval_min,
                     "is_floor": is_floor})
    return rows


OI_SURGE_VENUES = {
    "bybit": _catalog_bybit,
    "binance": _catalog_binance,
    "aster": _catalog_aster,
    "bitget": _catalog_bitget,
    "hyperliquid": _catalog_hyperliquid,
}


def _fetch_oi_surge_catalog(venues=None, per_venue_timeout=OI_SURGE_VENUE_TIMEOUT):
    """Fans `venues` (default OI_SURGE_VENUES) out CONCURRENTLY, one thread per venue, each
    bounded by `per_venue_timeout` (SPEC-127 lesson: one dead venue must never stall the
    sweep). Returns (rows, venues_errored) — a venue whose fetch raises/times out/returns a
    non-list contributes [] and its name to venues_errored, never a sweep failure."""
    probes = venues if venues is not None else OI_SURGE_VENUES
    rows, errored = [], []
    with ThreadPoolExecutor(max_workers=max(1, len(probes))) as ex:
        futs = {name: ex.submit(fn) for name, fn in probes.items()}
        for name, f in futs.items():
            try:
                r = f.result(timeout=per_venue_timeout)
                if isinstance(r, list):
                    rows.extend(r)
                else:
                    errored.append(name)
            except FuturesTimeout:
                errored.append(name)
            except Exception as e:  # noqa: BLE001
                print(f"oi_surge {name}: {e}", file=sys.stderr)
                errored.append(name)
    return rows, sorted(errored)


def run_oi_surge(cfg=None, baseline_path=None, catalog_rows=None, venues=None,
                 per_venue_timeout=OI_SURGE_VENUE_TIMEOUT, now_ts=None):
    """Orchestrates one sweep: load the baseline file, fetch the catalog (live fan-out
    unless `catalog_rows` is injected — offline/test path), evaluate, persist the new
    baseline. Returns the board dict, or None if the baseline file is present but corrupt
    (SPEC-137 LOUD-NOTOK sentinel — main() turns this into a nonzero exit / no stdout,
    never a clean-looking empty sweep)."""
    cfg = cfg or load_oi_surge_cfg()
    baseline, status = load_oi_surge_baseline(baseline_path)
    if status == "corrupt":
        return None
    venues_errored = []
    if catalog_rows is None:
        catalog_rows, venues_errored = _fetch_oi_surge_catalog(
            venues=venues, per_venue_timeout=per_venue_timeout)
    board = build_oi_surge(catalog_rows, baseline=baseline, cfg=cfg,
                           seeded=(status == "missing"), venues_errored=venues_errored,
                           now_ts=now_ts)
    save_oi_surge_baseline(board.pop("baseline_out"), baseline_path)
    return board


def render_oi_surge_human(board):
    print(C.c("═══ OI-SURGE — cross-sectional discovery net ═══", "bold", "cyan")
          + C.c(f"  {board['universe']} names swept, status={board['status']}", "grey"))
    if board["status"] == "seeded":
        print(C.c("\n  baseline SEEDED this run — no candidates yet, compare next sweep", "grey"))
        return
    if not board["candidates"]:
        print(C.c("\n  none — no name clears the liquidity floor with a signal firing (§0.5)", "grey"))
    for r in board["candidates"]:
        flags = ", ".join(r["flags"]) or "—"
        oi = f"${r['oi_usd_total']:,.0f}" if r["oi_usd_total"] is not None else "—"
        delta = f"{r['oi_delta_pct']:+.1f}%" if r["oi_delta_pct"] is not None else "n/a"
        fe = (f"{r['funding_extreme']['pi_4h']:+.3f}%/4h ({r['funding_extreme']['venue']})"
             if r["funding_extreme"] else "—")
        print(f"  {C.c(r['ticker'], 'bold'):<12} flags[{flags}]  OI {oi} (Δ{delta})  "
             f"vol/OI {r['vol_oi_ratio']}  funding {fe}  venues {'+'.join(r['venues'])}  "
             f"→ {r['next_step']}")
    if board["excluded"]:
        print(C.c(f"\n  excluded ({len(board['excluded'])}): "
                 + ", ".join(f"{r['ticker']}[{r['reason']}]" for r in board["excluded"][:10]), "grey"))
    if board["meta"]["venues_errored"]:
        print(C.c(f"\n  ⚠ venues errored (excluded from this sweep): "
                 + ", ".join(board["meta"]["venues_errored"]), "yellow"))


def _scout_row(tk, s):
    return {"ticker": tk, "setup": s["setup"], "verdict": s["verdict"],
            "tier": s.get("tier"), "score": s["score"], "required": s["required"],
            "add_triggers": s.get("add_triggers", []),
            "swing_vetoes": s.get("swing_vetoes", [])}


def score_watchlist(signals_by_ticker):
    """SPEC-82 — run the §6 scorers across a watchlist (one normalized signals snapshot per
    ticker) and partition into the graded tiers. A name is `armed` if ANY setup ARMS, `scout`
    if its best setup is near-armed (SCOUT) and none armed, else `quiet`. Pure/offline — the
    caller assembles the snapshots (live via setup_score.gather_signals, or injected).

    Returns {scout:[row], armed:[row], quiet:[ticker]}; row = {ticker,setup,verdict,tier,
    score,required,add_triggers,swing_vetoes}. This is what makes scan a signal SCANNER and
    not just a thesis-tracker — SCOUT surfaces even with no pre-committed thesis."""
    import setup_score
    scout, armed, quiet = [], [], []
    for tk, sig in signals_by_ticker.items():
        scores = setup_score.score_all(sig or {})
        armed_setups = [s for s in scores.values() if s.get("verdict") == "ARMED"]
        scout_setups = [s for s in scores.values() if s.get("verdict") == "SCOUT"]
        if armed_setups:
            armed.append(_scout_row(tk, max(armed_setups, key=lambda s: s["score"])))
        elif scout_setups:
            scout.append(_scout_row(tk, max(scout_setups, key=lambda s: s["score"])))
        else:
            quiet.append(tk)
    armed.sort(key=lambda r: -r["score"])
    scout.sort(key=lambda r: -r["score"])
    return {"scout": scout, "armed": armed, "quiet": quiet}


def _watchlist_signals():
    """Live path: assemble a signals snapshot per watchlist ticker (network; best-effort)."""
    import setup_score
    from pathlib import Path as _P
    wl = _P(__file__).resolve().parent.parent / "config" / "watchlist.json"
    tickers = [t["ticker"] for t in json.loads(wl.read_text()).get("tokens", []) if t.get("ticker")]
    out = {}
    for tk in tickers:
        try:
            sig, _meta = setup_score.gather_signals(tk)
            out[tk] = sig
        except Exception as e:  # noqa: BLE001 — one dead read must not abort the board
            print(f"scout gather {tk}: {e}", file=sys.stderr)
    return out


def render_scout_human(board):
    print(C.c("═══ SCOUT BOARD ═══", "bold", "cyan")
          + C.c("  graded §6 setups across the watchlist (SPEC-82)", "grey"))

    def rows(label, rs, col):
        print(C.c(f"\n{label} ({len(rs)})", "bold", col))
        for r in rs:
            adds = ", ".join(r["add_triggers"]) or "—"
            sw = C.c(f"  ⚠swing-veto: {r['swing_vetoes'][0]}", "yellow") if r["swing_vetoes"] else ""
            print(f"  {C.c(r['ticker'], 'bold'):<14} {r['setup']} {r['score']}/{r['required']}"
                  f"  add: {adds}{sw}")
    rows("🟠 SCOUT — near-armed (defined-risk poke, §7 / risk-at-stop % equity, SPEC-169)", board["scout"], "yellow")
    rows("🔴 ARMED — full confluence (the size-up)", board["armed"], "red")
    print(C.c(f"\n  quiet: {len(board['quiet'])} names below the SCOUT threshold "
              "(silence between triggers is correct, §0.5)", "grey"))
    print(C.c("  SCOUT = hypothesis-tier until n≥10 (§9); ledger them tier:\"scout\", split from "
              "ARMED fills. NO trade call — scores only.", "grey"))


def compact_scan_output(board):
    """SPEC-193: `scan`'s boards (default funding scan, scout, faded_bounce, oi_surge)
    are already candidate-lean (scalars + short strings, no raw depth/kline bulk) —
    the one long tail is the `excluded`/`dropped_below_top` bookkeeping lists, which
    can run to the size of the whole scanned universe. Capped to a count + a 3-item
    sample (the same 'events beyond the last 3 with a count' convention used
    elsewhere) — never present on the default (non-mode) board, which carries
    neither key."""
    if not isinstance(board, dict):
        return board
    out = dict(board)
    for key in ("excluded", "dropped_below_top", "skipped"):
        val = out.get(key)
        if isinstance(val, list) and len(val) > 3:
            out[key] = {"n": len(val), "sample": val[:3]}
    return out


def render_human(scan):
    print(C.c("═══ ALL-PERP SCAN ═══", "bold", "cyan")
          + C.c(f"  {scan['universe']} liquid perps (≥${scan['min_vol_m']:.0f}M)"
                f"  ·  funding normalized %/4h", "grey"))
    th = scan["thresh"]
    print(C.c(f"🟢 LONG squeeze-fuel = funding ≤ −{th:.2f}%/4h (shorts trapped)   "
              f"🔴 SHORT = funding ≥ +{th:.2f}%/4h (longs trapped)", "grey"))

    def line(c):
        deep = "⚡" if c["deep"] else "  "
        fcol = "green" if c["side"] == "long" else "red"
        chgcol = "green" if c["chg24"] > 0 else "red"
        sym = C.c(f"{c['ticker']:<11}", "bold")
        fund = C.c(f"{c['funding_4h']:+.3f}%/4h", "bold", fcol)
        raw = f"(raw {c['funding_raw']:+.3f}/{c['interval_h']}h)"
        chg = C.c(f"{c['chg24']:+.1f}%", chgcol)
        vol = C.c(f"${c['turnover_m']:.0f}M", "grey")
        return f"  {deep}{sym} fund {fund} {raw}  {chg} 24h  {vol} vol"

    if scan["side"] in ("long", "both"):
        print(C.c(f"\n🟢 LONG squeeze-fuel — shorts paying carry ({len(scan['longs'])} hits)", "bold", "green"))
        print("\n".join(line(c) for c in scan["longs"]) or C.c("   none", "grey"))
    if scan["side"] in ("short", "both"):
        print(C.c(f"\n🔴 SHORT — longs paying carry ({len(scan['shorts'])} hits)", "bold", "red"))
        print("\n".join(line(c) for c in scan["shorts"]) or C.c("   none", "grey"))

    print(C.c("\n⚡ = |funding| ≥ 0.50%/4h (MYX-tier extreme). Funding is LIVE/predicted. "
              "Verify per-venue + OI/L-S/structure before any entry — first-pass net.", "grey"))


def main():
    ap = argparse.ArgumentParser(description="All-perp cross-sectional funding scanner")
    ap.add_argument("--min-vol", type=float, default=10, help="min 24h turnover $M (liquidity gate)")
    ap.add_argument("--side", choices=["long", "short", "both"], default="both")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--thresh", type=float, default=0.10, help="funding threshold %%/4h")
    ap.add_argument("--json", action="store_true", help="emit JSON (machine path)")
    ap.add_argument("--include-hl", action="store_true",
                    help="SPEC-84: also merge HL-native perps into the universe (self-custody venue)")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--scout", action="store_true",
                    help="SCOUT board: run §6 scorers across the watchlist (live; network)")
    ap.add_argument("--scout-signals", default=None,
                    help='SCOUT board offline: JSON {ticker:{signals}} injected (no network)')
    ap.add_argument("--mode", choices=["faded_bounce", "oi_surge"], default=None,
                    help="SPEC-120: faded_bounce = the user's primary edge screen. SPEC-148: "
                         "oi_surge = cross-sectional OI-construction discovery net. An "
                         "unrecognized --mode value fails loudly (argparse, exit 2) — never a "
                         "silent fallback to the default funding scan.")
    ap.add_argument("--fb-candidates", default=None,
                    help='faded_bounce offline: JSON {ticker:{fields}} injected (no network)')
    ap.add_argument("--oi-catalog", default=None,
                    help='oi_surge offline: JSON [{ticker,venue,oi_usd,vol24h_usd,'
                         'funding_raw_pct,interval_min,is_floor}, ...] injected (no network)')
    ap.add_argument("--oi-baseline", default=None,
                    help='oi_surge offline: JSON {ticker:{ts,oi_usd_total,vol24h_usd_total,'
                         'first_seen_ts}} injected — used only with --oi-catalog (no file I/O)')
    ap.add_argument("--oi-baseline-path", default=None,
                    help="oi_surge: override the baseline STATE FILE path (default "
                         "state/oi_surge_baseline.json) — test/ops isolation knob")
    ap.add_argument("--render", choices=["full", "compact"], default="full",
                    help="SPEC-193: 'compact' caps excluded/dropped_below_top to a "
                         "count + 3-item sample")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)

    if args.scout or args.scout_signals is not None:
        if args.scout_signals is not None:
            sigs = json.loads(args.scout_signals)
        else:
            sigs = _watchlist_signals()
        board = score_watchlist(sigs)
        if args.json:
            print(json.dumps(compact_scan_output(board) if args.render == "compact" else board))
        else:
            render_scout_human(board)
        return

    if args.mode == "faded_bounce":
        if args.fb_candidates is not None:
            cands, skipped = json.loads(args.fb_candidates), []
        else:
            cands, skipped = _faded_bounce_live_candidates()
        board = build_faded_bounce(cands, skipped=skipped)
        if args.json:
            print(json.dumps(compact_scan_output(board) if args.render == "compact" else board))
        else:
            render_faded_bounce_human(board)
        return

    if args.mode == "oi_surge":
        if args.oi_catalog is not None:
            # fully offline: catalog + baseline both injected, no file I/O, no network
            catalog_rows = json.loads(args.oi_catalog)
            baseline = json.loads(args.oi_baseline) if args.oi_baseline is not None else {}
            board = build_oi_surge(catalog_rows, baseline=baseline, cfg=load_oi_surge_cfg())
        else:
            board = run_oi_surge(baseline_path=args.oi_baseline_path)
        if board is None:
            # SPEC-137 LOUD-NOTOK: corrupt baseline — no stdout, nonzero exit. Through the
            # orchestrator this surfaces as {"ok": false, ...}, never a clean empty sweep.
            print("oi_surge: baseline file corrupt — LOUD, not re-seeded mid-verdict", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(compact_scan_output(board) if args.render == "compact" else board))
        else:
            render_oi_surge_human(board)
        return

    scan = build_scan(args.min_vol, args.side, args.top, args.thresh, include_hl=args.include_hl)
    if args.json:
        print(json.dumps(compact_scan_output(scan) if args.render == "compact" else scan))
    else:
        render_human(scan)


if __name__ == "__main__":
    main()
