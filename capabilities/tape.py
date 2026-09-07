#!/usr/bin/env python3
"""tape.py — per-minute OI/price/taker/liquidation microstructure classifier (SPEC 31).

Operationalizes the operator-seat OI-reading lens (memory:
feedback_read_oi_price_from_operator_seat). The desk otherwise reads only aggregate/24h OI
(`oi_sides`, `regime_check`) — but the operator's chain-squeeze play lives at the
sub-minute candle, where FOUR forces move OI at once (shorts open/close, longs open/close)
so the net OI print is technically near-meaningless. `tape` classifies each candle into the
four-force vocabulary DETERMINISTICALLY and flags the staged-play sequences. The operator-
INTENT read ("what is the 庄 doing") stays with the Designer (ARCHITECTURE §0.3 / invariant 2);
this capability only surfaces the classified substrate.

READ-ONLY INTEL — participates in NO auto-verdict. It does NOT replace the §5 funding-flip +
OI-build short gate; it FEEDS the Designer's operator-lens between triggers. Pairs with
`oi_sides` (coarse wash read) — `tape` is the fine-grained sequence underneath it.

Data reality (declared, never pretended):
  - price + taker: 1m klines (taker-buy base vol is in the kline → taker imbalance is free,
    no raw-trade dump).
  - OI: Binance `futures/data/openInterestHist` floor is **5m** (period=1m returns []). So
    `oi_interval:"5m"`; each 1m bar inherits its 5m bucket's ΔOI. Surfaced, not hidden.
  - liquidations: Binance public `forceOrders` REST needs auth → on public access it
    degrades (`liq_available:false`); the forced-vs-voluntary short-exit split is then
    undetermined (a shorts_closing bar stays `forced:false`). The split LOGIC is exercised
    whenever a liq feed IS supplied.

  python3 capabilities/tape.py SKYAI --json
  python3 capabilities/tape.py SKYAI --window 90 --level 0.1836 --json
"""
import argparse
import json
import math
import sys
import urllib.request
from pathlib import Path
from urllib.error import URLError, HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oi_mc as OM   # SPEC-177: leverage_state, reusing tape's own already-fetched OI series

UA = {"User-Agent": "tape/1.0"}
FAPI = "https://fapi.binance.com"

# classification thresholds (percent). |move| below the eps = "flat" for that axis.
PRICE_EPS = 0.05        # %/1m price move below this = flat
OI_EPS_PCT = 0.05       # % OI move below this = flat
LIQ_FORCED_USD = 1000   # a liq event at/above this confirms a FORCED exit (vs voluntary)
# pattern-detector defaults
TAIL_N = 3              # consecutive longs_closing bars = tail_of_liquidation
WICK_K = 3             # bars within which a sub-level wick must recover = fake breakdown
MIN_SQUEEZE_SHORTS = 2  # shorts_opening bars in a rising run that crosses a round number


# ── fetch layer (patched in tests) ─────────────────────────────────────────────
def _get(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r:
            return json.loads(r.read())
    except (URLError, HTTPError, json.JSONDecodeError, TimeoutError, ValueError) as e:
        return {"_error": str(e)[:120]}


def fetch_klines_1m(sym, limit, venue="binance"):
    d = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}&interval=1m&limit={int(limit)}")
    return d if isinstance(d, list) else None


def fetch_oi_hist(sym, period, limit, venue="binance"):
    d = _get(f"{FAPI}/futures/data/openInterestHist?symbol={sym}&period={period}&limit={int(limit)}")
    if not isinstance(d, list):
        return None
    out = []
    for row in d:
        try:
            out.append({"ts": int(row["timestamp"]), "oi": float(row["sumOpenInterest"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_force_orders(sym, limit, venue="binance"):
    """Liquidations. Binance public forceOrders REST requires auth → returns None on the
    free path (degrade-explicit). Kept as a seam: an authed/alt-venue feed can populate it."""
    d = _get(f"{FAPI}/fapi/v1/forceOrders?symbol={sym}&limit={int(limit)}")
    if not isinstance(d, list):
        return None
    out = []
    for o in d:
        try:
            side = o["side"].upper()           # SELL order = a LONG was liquidated; BUY = a SHORT
            usd = float(o["price"]) * float(o["origQty"])
            out.append({"ts": int(o["time"]), "side": "long" if side == "SELL" else "short", "usd": usd})
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ── per-bar four-force classification (the deterministic core) ──────────────────
def classify_force(d_oi_pct, d_price_pct, taker_imb=0.0,
                   liq_long_usd=0.0, liq_short_usd=0.0,
                   price_eps=PRICE_EPS, oi_eps=OI_EPS_PCT, forced_usd=LIQ_FORCED_USD):
    """Map (ΔOI, Δprice, taker, liq) → one of the four forces (+flat/mixed).
    Returns (force_label, forced_bool). forced is meaningful only for shorts_closing."""
    oi_up = d_oi_pct > oi_eps
    oi_dn = d_oi_pct < -oi_eps
    px_up = d_price_pct > price_eps
    px_dn = d_price_pct < -price_eps
    px_flat = not (px_up or px_dn)

    if oi_up and px_up:
        return "longs_opening", False
    if oi_up and (px_dn or px_flat):
        # OI building while price falls OR stays flat = shorts being opened/recruited
        # (the SKYAI lesson: a flat tape with OI building is recruitment, not calm).
        return "shorts_opening", False
    if oi_dn and px_dn:
        return "longs_closing", False
    if oi_dn and px_up:
        # OI falling while price rises = shorts closing. Forced (chain-squeeze fuel) when the
        # liq feed shows SHORT positions being liquidated (force-bought to close).
        return "shorts_closing", (liq_short_usd >= forced_usd)
    if not oi_up and not oi_dn and not px_up and not px_dn:
        return "flat", False
    return "mixed", False


# ── assemble per-bar series: 1m price/taker + 5m OI mapped on ────────────────────
def _parse_kline(arr):
    return {"ts": int(arr[0]), "open": float(arr[1]), "high": float(arr[2]),
            "low": float(arr[3]), "close": float(arr[4]), "vol": float(arr[5]),
            "taker_buy": float(arr[9])}


def _oi_at(oi_series, ts):
    """Step-function OI: the latest 5m sample with timestamp ≤ ts (and the one before it)."""
    cur = prev = None
    for p in oi_series:
        if p["ts"] <= ts:
            prev = cur
            cur = p
        else:
            break
    return cur, prev


def _bucket_oi_delta(oi_series, ts):
    """ΔOI for the 5m bucket containing ts = oi(this bucket) − oi(previous bucket).
    Bars inside the same 5m bucket share this delta (the 5m granularity floor)."""
    cur, prev = _oi_at(oi_series, ts)
    if cur is None or prev is None:
        return 0.0, (cur["oi"] if cur else None)
    return cur["oi"] - prev["oi"], cur["oi"]


def _liq_for_bar(liq_series, ts, next_ts):
    long_usd = short_usd = 0.0
    if not liq_series:
        return 0.0, 0.0
    for e in liq_series:
        if ts <= e["ts"] < next_ts:
            if e["side"] == "long":
                long_usd += e["usd"]
            else:
                short_usd += e["usd"]
    return long_usd, short_usd


def classify_bars(klines, oi_series, liq_series=None, oi_interval="5m",
                  price_eps=PRICE_EPS, oi_eps=OI_EPS_PCT):
    """Parse raw 1m klines, map 5m OI on per-bucket, attach liqs, classify each bar."""
    parsed = [_parse_kline(k) for k in (klines or [])]
    oi_series = sorted(oi_series or [], key=lambda p: p["ts"])
    bars = []
    prev_close = None
    for i, k in enumerate(parsed):
        ts = k["ts"]
        next_ts = parsed[i + 1]["ts"] if i + 1 < len(parsed) else ts + 60000
        close = k["close"]
        d_price_pct = ((close - prev_close) / prev_close * 100) if prev_close else 0.0
        taker_imb = ((2 * k["taker_buy"] - k["vol"]) / k["vol"]) if k["vol"] else 0.0
        d_oi, oi_now = _bucket_oi_delta(oi_series, ts)
        d_oi_pct = (d_oi / (oi_now - d_oi) * 100) if (oi_now not in (None, 0) and (oi_now - d_oi) not in (0, None)) else 0.0
        liq_long, liq_short = _liq_for_bar(liq_series, ts, next_ts)
        force, forced = classify_force(d_oi_pct, d_price_pct, taker_imb,
                                       liq_long, liq_short, price_eps, oi_eps)
        bar = {
            "ts": ts, "price": close, "low": k["low"], "high": k["high"], "close": close,
            "d_price_pct": round(d_price_pct, 4), "d_oi": round(d_oi, 4),
            "d_oi_pct": round(d_oi_pct, 4), "oi": oi_now,        # SPEC 38: absolute OI (for oi-shed-over-run)
            "oi_force": force, "forced": forced,
            "taker_imb": round(taker_imb, 4),
            "liq_long_usd": round(liq_long, 2), "liq_short_usd": round(liq_short, 2),
        }
        if force == "mixed":
            bar["dominant"] = _dominant(d_oi_pct, d_price_pct, taker_imb, oi_eps, price_eps)
        bars.append(bar)
        prev_close = close
    return bars


def _dominant(d_oi_pct, d_price_pct, taker_imb, oi_eps, price_eps):
    """For a net-ambiguous bar, name the dominant component (spec: mixed-with-dominant).
    OI moved + price flat → lean on taker (buy-absorb = shorts covering; sell = longs exiting);
    price moved + OI flat → a position-neutral price push in that direction."""
    oi_moved = abs(d_oi_pct) > oi_eps
    px_moved = abs(d_price_pct) > price_eps
    if oi_moved and not px_moved:
        if d_oi_pct < 0:
            return "shorts_closing" if taker_imb > 0 else "longs_closing"
        return "shorts_opening"        # OI↑ price-flat already classified upstream; defensive
    if px_moved and not oi_moved:
        return "price_up" if d_price_pct > 0 else "price_down"
    return "taker_buy" if taker_imb > 0 else "taker_sell"


# ── round numbers near the window range ────────────────────────────────────────
def round_numbers_near(lo, hi):
    """'Nice' round levels within [lo, hi] — magnet/stop-hunt anchors (e.g. 0.20 for SKYAI)."""
    if lo is None or hi is None or hi <= 0:
        return []
    span = max(hi - lo, hi * 1e-6)
    mag = 10 ** math.floor(math.log10(span)) if span > 0 else hi
    out = set()
    for step in (mag, mag / 2, mag / 5):       # whole, half, fifth — covers .20/.25/.05-grids
        if step <= 0:
            continue
        n = math.floor(lo / step)
        while n * step <= hi + step:
            v = round(n * step, 10)
            if lo - step * 0.001 <= v <= hi + step * 0.001:
                out.add(round(v, 8))
            n += 1
    return sorted(out)


# ── pattern detectors (flag the staged-play sequences) ──────────────────────────
def _runs(bars, pred):
    """Maximal index runs [i, j] where every bar satisfies pred."""
    runs, start = [], None
    for i, b in enumerate(bars):
        if pred(b):
            start = i if start is None else start
        elif start is not None:
            runs.append((start, i - 1)); start = None
    if start is not None:
        runs.append((start, len(bars) - 1))
    return runs


# SPEC 35: an OI↓+price↓ run means OPPOSITE things by structural location — near a local high
# after markup = the operator taking profit + shaking out (do NOT short); deep in a down-leg =
# retail capitulation (where a short banks). SPEC 38: the PRIMARY discriminator is the OI-force
# composition + how much OI is shed over the run (real unwind vs wash); location only corroborates.
CAPITULATION_OI_SHED = 8.0      # SPEC 38: ≥ this % of OI shed over the run = a real long-unwind (not a wash)
PROFIT_TAKE_RANGE_POS = 0.70    # event sitting in the top ≥70% of the window range = near-high (corroborator)
PROFIT_TAKE_MAX_DD = 8.0        # ≤ this % below the local high = a shakeout, not a flush
CAPITULATION_DD = 15.0          # ≥ this % below the local high = a real capitulation flush
CAPITULATION_RANGE_POS = 0.40   # event in the bottom ≤40% of the window range = near-low
FAST_VELOCITY = 1.0             # avg |Δprice%|/bar at/above this = a fast flush (capitulation-like)


def _tail_context(bars, a, b, cs_ranges=None):
    """SPEC 38: classify an OI↓+price↓ tail by WHO is acting (force + OI-shed), not just where
    price sits. PRIMARY: dominant force + cumulative OI shed over the run. Range/drawdown only
    corroborate. Returns context + the discriminator values."""
    cs_ranges = cs_ranges or []
    run = bars[a:b + 1]
    forces = {}
    for x in run:
        forces[x["oi_force"]] = forces.get(x["oi_force"], 0) + 1
    dominant_force = max(forces, key=forces.get) if forces else None

    # OI shed over the run — absolute OI at the endpoints (positive = OI collapsing = real unwind).
    oi_start, oi_end = bars[a].get("oi"), bars[b].get("oi")
    if oi_start and oi_end and oi_start > 0:
        oi_shed = round((oi_start - oi_end) / oi_start * 100, 2)
    else:                                                       # fallback: net per-bar OI% over the run
        oi_shed = round(-sum(x.get("d_oi_pct", 0) for x in run), 2)

    # surrounding window: is the operator washing + recruiting shorts (shorts_opening / chain_squeeze)?
    W = 8
    lo_w, hi_w = max(0, a - W), min(len(bars) - 1, b + W)
    shorts_opening_nearby = sum(1 for x in bars[lo_w:hi_w + 1] if x["oi_force"] == "shorts_opening")
    chain_squeeze_nearby = any(not (hi < lo_w or lo > hi_w) for lo, hi in cs_ranges)

    # location corroborators (demoted — SPEC 38)
    lows = [x["low"] for x in bars]
    highs = [x["high"] for x in bars]
    w_low, w_high = min(lows), max(highs)
    rng = w_high - w_low
    local_high = max(x["high"] for x in bars[:a + 1])
    event_price = bars[b]["price"]
    range_pos = (event_price - w_low) / rng if rng > 0 else 0.5
    drawdown = (local_high - event_price) / local_high * 100 if local_high else 0.0
    velocity = sum(abs(x["d_price_pct"]) for x in run) / max(1, len(run))

    # PRIMARY discriminators (force + OI), then location as the tie-breaker.
    real_unwind = (dominant_force == "longs_closing") and oi_shed >= CAPITULATION_OI_SHED
    wash_recruit = (chain_squeeze_nearby or shorts_opening_nearby >= 2) and oi_shed < CAPITULATION_OI_SHED
    if real_unwind:
        context = "retail_capitulation"                        # regardless of range (VELVET mid-range case)
    elif wash_recruit:
        context = "operator_profit_take"                       # washing + recruiting, not a real unwind (BEAT)
    elif range_pos >= PROFIT_TAKE_RANGE_POS and drawdown <= PROFIT_TAKE_MAX_DD and oi_shed < CAPITULATION_OI_SHED:
        context = "operator_profit_take"                       # location corroboration when force inconclusive
    elif drawdown >= CAPITULATION_DD or range_pos <= CAPITULATION_RANGE_POS:
        context = "retail_capitulation"
    else:
        context = "ambiguous"
    return {"context": context,
            "dominant_force": dominant_force,
            "oi_shed_pct_over_run": oi_shed,
            "chain_squeeze_nearby": chain_squeeze_nearby,
            "shorts_opening_nearby": shorts_opening_nearby,
            "range_pos_at_event": round(range_pos, 3),
            "drawdown_from_local_high_pct": round(drawdown, 2),
            "velocity_pct_per_bar": round(velocity, 3)}


def detect_patterns(bars, level=None, round_numbers=None, *,
                    tail_n=TAIL_N, wick_k=WICK_K, min_squeeze_shorts=MIN_SQUEEZE_SHORTS):
    """Detect the operator staged-play sequences over the classified bar series."""
    round_numbers = round_numbers or []
    patterns = []
    n = len(bars)

    # chain_squeeze (computed FIRST — its ranges feed the tail_of_liquidation context, SPEC 38):
    # a rising run that recruits shorts (≥min_squeeze_shorts shorts_opening) then stop-runs UP
    # through a round number. ONE episode per crossing (bounded + skip-past → no duplicates).
    LOOKBACK, LOOKAHEAD = 20, 15
    cs_ranges = []
    i = 1
    while i < n:
        crossed = [r for r in round_numbers if bars[i - 1]["price"] < r <= bars[i]["price"]]
        if crossed:
            r = max(crossed)                       # the highest round number stop-run through
            lo, look = i - 1, 0
            while lo > 0 and bars[lo - 1]["price"] < r and look < LOOKBACK:
                lo -= 1; look += 1
            hi, look = i, 0
            while hi + 1 < n and look < LOOKAHEAD and bars[hi + 1]["price"] > bars[hi]["price"]:
                hi += 1; look += 1                 # stop at the local peak (strict rise)
            seg = bars[lo:hi + 1]
            shorts_open = sum(1 for s in seg if s["oi_force"] == "shorts_opening")
            if shorts_open >= min_squeeze_shorts and bars[hi]["price"] > bars[lo]["price"]:
                cs_ranges.append((lo, hi))
                patterns.append({"type": "chain_squeeze", "bar_range": [lo, hi],
                                 "detail": {"round_number": r, "round_numbers_crossed": crossed,
                                            "from": bars[lo]["price"], "to": bars[hi]["price"],
                                            "shorts_opening_bars": shorts_open,
                                            "short_liq_usd": round(sum(s["liq_short_usd"] for s in seg), 2)}})
                i = hi + 1                          # skip past the episode → no duplicates
                continue
        i += 1

    # tail_of_liquidation — ≥tail_n consecutive longs_closing (OI↓ price↓). SPEC 38: the `context`
    # keys on force + OI-shed (+ chain_squeeze_nearby), NOT just price location.
    for a, b in _runs(bars, lambda x: x["oi_force"] == "longs_closing"):
        if b - a + 1 >= tail_n:
            ctx = _tail_context(bars, a, b, cs_ranges)
            patterns.append({"type": "tail_of_liquidation", "bar_range": [a, b],
                             "detail": {"bars": b - a + 1,
                                        "price_from": bars[a]["price"], "price_to": bars[b]["price"],
                                        **ctx}})

    # fake_breakdown_wick — price breaks below `level`, shorts_opening into the break, then closes
    # back above within wick_k bars (= short-recruitment bait). The SKYAI trap.
    if level is not None:
        for i in range(n):
            if bars[i]["low"] < level:
                for j in range(i, min(i + wick_k + 1, n)):
                    if bars[j]["close"] >= level:
                        seg = bars[i:j + 1]
                        if any(s["oi_force"] == "shorts_opening" for s in seg):
                            patterns.append({"type": "fake_breakdown_wick", "bar_range": [i, j],
                                             "detail": {"level": level,
                                                        "break_low": min(s["low"] for s in seg),
                                                        "recovered_close": bars[j]["close"]}})
                        break
                else:
                    continue
                break   # report the first (earliest) fake breakdown

    # 4. downtrend_slam — a net-down run that interleaves longs_closing + shorts_opening
    #    (OI↓price↓ and OI↑price↓ both present): the fast slam that activates the tape.
    for a, b in _runs(bars, lambda x: x["oi_force"] in ("longs_closing", "shorts_opening")):
        seg = bars[a:b + 1]
        has_lc = any(s["oi_force"] == "longs_closing" for s in seg)
        has_so = any(s["oi_force"] == "shorts_opening" for s in seg)
        if has_lc and has_so and bars[b]["price"] < bars[a]["price"]:
            patterns.append({"type": "downtrend_slam", "bar_range": [a, b],
                             "detail": {"longs_closing": sum(1 for s in seg if s["oi_force"] == "longs_closing"),
                                        "shorts_opening": sum(1 for s in seg if s["oi_force"] == "shorts_opening"),
                                        "price_from": bars[a]["price"], "price_to": bars[b]["price"]}})

    # 4b. bounce_cover — ≥2 consecutive shorts_closing (price↑ OI↓) = covering on a bounce.
    for a, b in _runs(bars, lambda x: x["oi_force"] == "shorts_closing"):
        if b - a + 1 >= 2:
            patterns.append({"type": "bounce_cover", "bar_range": [a, b],
                             "detail": {"bars": b - a + 1, "forced_bars": sum(1 for s in bars[a:b + 1] if s["forced"])}})

    return patterns


# ── top-level build ────────────────────────────────────────────────────────────
def _defended_fade_tape(ticker, bars):
    """SPEC-83 — surface the wall-fade tell from the rolling wall history `brief` records (read-
    only, no extra fetch): is the defended wall GROWING across recent briefs, and is price
    stalling (lower-highs in the tape)? Composes with the SPEC-82 SCOUT tier. None if no history
    yet (run `brief` to record the book)."""
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parent))
    try:
        import setup_score as SS
    except Exception:  # noqa: BLE001
        return None
    ask = SS.read_wall_history(ticker, "ask")
    if not ask:
        return None
    highs = [b["high"] for b in bars[-6:]] if bars else []
    stall = len(highs) >= 3 and highs[-1] < max(highs[:-1])      # failing to break the ceiling
    last = ask[-1]
    return {
        "wall_growing": SS.derive_wall_growing(ask), "snapshots": len(ask),
        "last_wall_notional_usd": last.get("notional"), "last_mid": last.get("price"),
        "stall_lower_high": bool(stall),
        "note": "wall-dynamics from recorded brief snapshots (SPEC-83) — run `brief` to refresh the book",
    }


def _liqs_consistency_note(ticker, oi_series, window_min):
    """SPEC-103: cross-check the window's OI delta (from the OI series `tape` already
    fetched — no second OI call) against the `liqs` ground-truth stream. Purely additive;
    never touches the per-bar fetch_force_orders/classify_bars forced-close path above
    (SPEC 31's own liq input, a different granularity). None when there's nothing to say
    (short/empty OI series, liqs module unavailable, or the move is sub-material)."""
    if not oi_series or len(oi_series) < 2 or not oi_series[0].get("oi"):
        return None
    try:
        import liqs as LQ
    except Exception:  # noqa: BLE001
        return None
    oi_delta_pct = (oi_series[-1]["oi"] - oi_series[0]["oi"]) / oi_series[0]["oi"] * 100
    try:
        r = LQ.build_liqs(ticker, window_h=max(window_min / 60.0, 0.25),
                          oi_delta_pct=oi_delta_pct)
    except Exception:  # noqa: BLE001
        return None
    v = (r.get("oi_liq_consistency") or {}).get("verdict")
    if not v:
        return None
    return {"verdict": v, "reason": r["oi_liq_consistency"].get("reason"),
            "venue": r.get("venue"), "oi_delta_pct": round(oi_delta_pct, 2)}


def _leverage_state_from_tape(bars, oi_series, level, window_min):
    """SPEC-177 req 3 — "tape prints leverage_state". This is tape's OWN fine-grained
    window (`window_min`, default 60), NOT the board's 4h/48h convention — a distinct
    instrument-level read underneath oi_sides' coarse wash tag (module docstring).
    Reuses `oi_series`/`bars` this call already fetched — zero new fetch. `level` (an
    existing tape param, the defended/breakdown level under test) doubles as the swing
    point when given; range_held stays UNKNOWN without one. Direction is inferred from
    whether `level` sits above (short swing-high) or below (long swing-low) the last
    close."""
    delta_oi_pct = None
    if oi_series and len(oi_series) >= 2:
        first, last = oi_series[0].get("oi"), oi_series[-1].get("oi")
        if first:
            delta_oi_pct = round((last / first - 1) * 100, 2)
    range_held = None
    if level is not None and bars:
        closes = [b["close"] for b in bars]
        direction = "short" if level > closes[-1] else "long"
        range_held = OM._range_held(closes, level, direction)
    cfg = OM.load_bf_cfg()
    return OM.leverage_state_for_window(delta_oi_pct, range_held, None,
                                        cfg["delta_oi_sig_4h"], f"{window_min}m")


def build_tape(ticker, venue="binance", window_min=60, level=None):
    sym = ticker.upper().replace("USDT", "") + "USDT"
    window_min = int(window_min)
    klines = fetch_klines_1m(sym, window_min + 1, venue)        # +1 so the first bar has a Δ
    if not klines:
        return {"ticker": ticker.upper().replace("USDT", ""), "venue": venue, "available": False,
                "reason": "no 1m klines", "oi_interval": "5m", "window_min": window_min,
                "bars": [], "patterns": [], "round_numbers_near": [], "liq_available": False,
                "liq_consistency": None,
                "leverage_state": OM.leverage_state_for_window(None, None, None, 8.0,
                                                                f"{window_min}m")}
    oi_limit = window_min // 5 + 3
    oi_series = fetch_oi_hist(sym, "5m", oi_limit, venue) or []
    liqs = fetch_force_orders(sym, 100, venue)                  # None on public REST → degrade
    liq_available = liqs is not None

    bars = classify_bars(klines, oi_series, liqs, oi_interval="5m")
    lo = min((b["low"] for b in bars), default=None)
    hi = max((b["high"] for b in bars), default=None)
    rns = round_numbers_near(lo, hi)
    patterns = detect_patterns(bars, level=level, round_numbers=rns)

    return {
        "ticker": ticker.upper().replace("USDT", ""), "venue": venue, "available": True,
        "oi_interval": "5m", "window_min": window_min, "level": level,
        "bars": bars, "patterns": patterns, "round_numbers_near": rns,
        "leverage_state": _leverage_state_from_tape(bars, oi_series, level, window_min),
        "liq_available": liq_available,
        "liq_consistency": _liqs_consistency_note(ticker, oi_series, window_min),   # SPEC-103
        "defended_fade": _defended_fade_tape(ticker, bars),   # SPEC-83 wall-dynamics surface
        "note": ("read-only microstructure intel (SPEC 31) — NOT an auto-verdict input. "
                 "ΔOI is 5m-resolution (Binance OI floor); price/taker are 1m. "
                 + ("" if liq_available else "Liquidation feed unavailable on public REST → "
                    "forced-vs-voluntary short-exit undetermined (shorts_closing stays forced:false).")),
    }


def render_human(t):
    if not t.get("available"):
        print(f"# {t['ticker']} tape — unavailable ({t.get('reason')})"); return
    print(f"# {t['ticker']} tape  ({t['venue']}, {t['window_min']}m, OI {t['oi_interval']})\n")
    if t["patterns"]:
        print("PATTERNS:")
        for p in t["patterns"]:
            print(f"  ⚑ {p['type']}  bars {p['bar_range']}  {p['detail']}")
    else:
        print("PATTERNS: none active in window")
    print(f"\nround numbers near: {t['round_numbers_near']}")
    ls = t.get("leverage_state") or {}
    if ls.get("state"):
        print(f"leverage_state ({ls.get('window')}): {ls['state']}"
              + (f"  ΔOI {ls['delta_oi_pct']:+.1f}%" if ls.get("delta_oi_pct") is not None else ""))
    if not t["liq_available"]:
        print("⚠ liq feed unavailable (public REST) — forced/voluntary split undetermined")
    print(f"\nlast {min(8, len(t['bars']))} bars:")
    for b in t["bars"][-8:]:
        fl = b["oi_force"] + ("*" if b["forced"] else "")
        print(f"  {b['ts']}  px {b['price']:.6g}  Δpx {b['d_price_pct']:+.2f}%  "
              f"ΔOI {b['d_oi']:+.4g}  taker {b['taker_imb']:+.2f}  → {fl}")


def main():
    ap = argparse.ArgumentParser(description="Per-minute OI/price/taker/liq microstructure classifier (SPEC 31)")
    ap.add_argument("ticker")
    ap.add_argument("--venue", default="binance")
    ap.add_argument("--window", type=int, default=60, help="window minutes (default 60)")
    ap.add_argument("--level", type=float, default=None, help="support level for fake_breakdown_wick")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    t = build_tape(a.ticker, a.venue, a.window, a.level)
    if a.json:
        print(json.dumps(t))
    else:
        render_human(t)


if __name__ == "__main__":
    main()
