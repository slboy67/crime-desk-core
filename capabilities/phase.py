#!/usr/bin/env python3
"""phase.py — SPEC-104: Wyckoff cycle/event classifier (cycle-first breakout gate).

Corpus rule (memory: feedback_wyckoff_cycle_first_breakout_gate): the same breakout/
breakdown candle is REAL in an accumulation/re-accumulation cycle and BAIT (long-liquidity
harvest) in a distribution cycle — the CYCLE, not the candle, decides tradability. The
desk's entry triggers (§6) fire on candles with no structural layer naming which cycle they
print in; the stopped shorts in the ledger were candle-reactions inside the wrong cycle.

Heuristics over ML — every detector is a rule-based threshold check (config in
config/phase.json), every event carries its evidence (bar index, volume ratio, range
ratio) so the orchestrator can override with a stated reason. No black box.
`UNCLEAR` is a first-class honest answer, not a fallback-of-shame — most bar windows don't
contain a clean Wyckoff sequence, and saying so is correct, not a failure.

Event vocabulary (bottom-side / top-side mirror pairs):
  SC   selling climax   — climactic volume + range expansion, closing down.
  BC   buying climax    — the top-side mirror: climactic volume + range expansion, closing up.
  AR   automatic rally  — the snap bounce/pullback after a climax. FRAGILE — short-covering
                          (after SC) or smart-money absorption (after BC), never real demand.
  ST   secondary test   — a retest of the climax extreme on MATERIALLY LOWER volume than the
                          climax (bottom: retests the SC low; top: retests the BC high).
  SPRING  — a bottom-side range undercut that SNAPS BACK into the range within the reclaim
            window. Only valid in LATE accumulation (a prior SC→AR→ST chain already
            established the range) — "small coins have no bottom": a falling-knife undercut
            with no held range is NEVER labeled a spring, however clean the reclaim looks.
  UTAD    — the top-side mirror: a range-high sweep that FAILS back below the range within
            the reclaim window, after a prior BC→AR→ST(top) chain.

Cycle vocabulary: ACCUMULATION / RE-ACCUMULATION / DISTRIBUTION / RE-DISTRIBUTION / MARKUP /
MARKDOWN / UNCLEAR. Bottom-side: cycle reads ACCUMULATION once SC→AR→ST completes, and
transitions to MARKUP once a SPRING is followed by a confirmed impulse breakout above the
range. Top-side: cycle reads DISTRIBUTION once BC→AR→ST(top) completes and STAYS
DISTRIBUTION through a UTAD + confirmed breakdown (the breakdown is recorded as a
`MARKDOWN_BREAK` event, but does not flip the cycle field to MARKDOWN in this v1) — a SHORT
signal on that breakdown still gets a clean `cycle_gate` pass-through, since DISTRIBUTION is
in the short-agree set. Known limitations (v1, honest not hidden): RE-ACCUMULATION/
RE-DISTRIBUTION vs the plain forms is not distinguished (both read as
ACCUMULATION/DISTRIBUTION — the corpus's core discipline, cycle-first not candle-first,
doesn't depend on that distinction); MARKDOWN is a valid enum value `cycle_gate_for` accepts
but this v1's event detectors never emit it; OI/liq inputs (crypto-adapted Wyckoff per the
corpus — OI/liq replace pure volume where volume is washed) are accepted by `build_phase`
as a corroborating signal but the core `classify_events` detectors are price/volume-only —
a full OI/liq-substituted-for-volume detector chain is future work.

Bar shape: {open, high, low, close, volume} (a ts/index is implicit — position in the list).

  python3 capabilities/phase.py OPN --json
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG_PATH = ROOT / "config" / "phase.json"

DEFAULT_CFG = {
    "vol_lookback": 10,           # trailing bars used for the volume/range baseline
    "sc_vol_mult": 2.5,           # climactic volume >= this x the trailing average
    "sc_range_mult": 1.8,         # climactic range >= this x the trailing average range
    "ar_window": 6,               # bars after a climax to find the AR
    "ar_min_bounce_pct": 8.0,     # AR must move this %+ off the climax extreme
    "st_window": 15,              # bars after the AR to find the ST
    "st_vol_ratio_max": 0.5,      # ST volume <= this fraction of the climax volume
    "st_retest_tolerance_pct": 6.0,   # ST extreme must be within this % of the climax extreme
    "spring_window": 20,          # bars after the ST to look for a spring/UTAD
    "spring_reclaim_bars": 3,     # bars allowed to reclaim back into the range
    "impulse_min_pct": 4.0,       # min net move to call a breakout/breakdown "impulse"
    "impulse_max_bars": 3,        # an impulse move completes within this many bars
    "grind_dwell_bars": 6,        # bars hugging the level before/while breaking = grind candidate
    "grind_max_close_range_pct": 2.5,   # close dispersion while dwelling <= this % = grind
}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        d = json.loads(CONFIG_PATH.read_text())
        cfg.update({k: v for k, v in d.items() if k in DEFAULT_CFG})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


# ── baseline / small helpers ─────────────────────────────────────────────────
def _avg(vals):
    return sum(vals) / len(vals) if vals else 0.0


def _baseline(bars, i, lookback):
    lo = max(0, i - lookback)
    window = bars[lo:i]
    vols = [b["volume"] for b in window]
    ranges = [b["high"] - b["low"] for b in window]
    return (_avg(vols) or 1e-9), (_avg(ranges) or 1e-9)


def _pct(a, b):
    """% move from a to b."""
    return (b - a) / a * 100 if a else 0.0


# ── climax detection (SC bottom-side, BC top-side) ──────────────────────────
def _find_climax(bars, cfg, start, direction):
    """First climax bar at/after `start`. direction='down' -> SC (closes down), 'up' -> BC
    (closes up). Returns {bar_index, volume, low, high, close, vol_ratio, range_ratio} or None."""
    for i in range(max(start, cfg["vol_lookback"]), len(bars)):
        b = bars[i]
        vbase, rbase = _baseline(bars, i, cfg["vol_lookback"])
        vol_ratio = b["volume"] / vbase
        rng = b["high"] - b["low"]
        range_ratio = rng / rbase if rbase else 0.0
        if vol_ratio >= cfg["sc_vol_mult"] and range_ratio >= cfg["sc_range_mult"]:
            is_down = b["close"] < b["open"]
            is_up = b["close"] > b["open"]
            if (direction == "down" and is_down) or (direction == "up" and is_up):
                return {"bar_index": i, "volume": b["volume"], "low": b["low"], "high": b["high"],
                        "close": b["close"], "vol_ratio": round(vol_ratio, 2),
                        "range_ratio": round(range_ratio, 2)}
    return None


def _oi_corroborates(oi_series, bar_index, oi_drop_pct=3.0):
    """Crypto-adapted Wyckoff (corpus §4/§9): OI/liq replace pure volume where volume is
    washed. A real SC/UTAD is positions actually closing — OI should drop around it; a
    WASHED climax (fake volume, double-open) often shows OI flat/rising through it. Returns
    True/False, or None when no OI series was supplied — absence never reads as a negative,
    it's a missing corroborator, not a contradiction (§3)."""
    if not oi_series or bar_index <= 0 or bar_index >= len(oi_series):
        return None
    prev, cur = oi_series[bar_index - 1], oi_series[bar_index]
    if not prev:
        return None
    return (cur - prev) / prev * 100 <= -oi_drop_pct


def _find_ar(bars, cfg, climax, direction):
    """The snap AR after a climax. direction='down' -> bounce UP off the SC low; 'up' ->
    pullback DOWN off the BC high. Returns {bar_index, close, move_pct} or None."""
    i0 = climax["bar_index"]
    extreme = climax["low"] if direction == "down" else climax["high"]
    best = None
    for i in range(i0 + 1, min(i0 + 1 + cfg["ar_window"], len(bars))):
        b = bars[i]
        move = _pct(extreme, b["close"]) if direction == "down" else _pct(extreme, b["close"])
        if direction == "down" and move >= cfg["ar_min_bounce_pct"]:
            if best is None or b["close"] > best["close"]:
                best = {"bar_index": i, "close": b["close"], "move_pct": round(move, 2)}
        elif direction == "up" and move <= -cfg["ar_min_bounce_pct"]:
            if best is None or b["close"] < best["close"]:
                best = {"bar_index": i, "close": b["close"], "move_pct": round(move, 2)}
    return best


def _find_st(bars, cfg, climax, ar, direction):
    """The retest after the AR, on materially lower volume than the climax. direction='down'
    -> retests the SC low; 'up' -> retests the BC high. Returns {bar_index, extreme,
    volume, vol_ratio_to_climax} or None."""
    i0 = ar["bar_index"]
    climax_extreme = climax["low"] if direction == "down" else climax["high"]
    tol = cfg["st_retest_tolerance_pct"] / 100.0
    for i in range(i0 + 1, min(i0 + 1 + cfg["st_window"], len(bars))):
        b = bars[i]
        extreme = b["low"] if direction == "down" else b["high"]
        near = abs(extreme - climax_extreme) <= abs(climax_extreme) * tol
        low_vol = b["volume"] <= cfg["st_vol_ratio_max"] * climax["volume"]
        # a retest must actually approach the climax extreme, not merely be quiet
        approaches = (extreme <= climax_extreme * (1 + tol)) if direction == "down" \
            else (extreme >= climax_extreme * (1 - tol))
        if (near or approaches) and low_vol:
            return {"bar_index": i, "extreme": extreme, "volume": b["volume"],
                    "vol_ratio_to_climax": round(b["volume"] / climax["volume"], 3)}
    return None


def _find_spring_or_utad(bars, cfg, climax, ar, st, direction):
    """The terminal shakeout: bottom-side undercut-then-reclaim (SPRING) or top-side
    sweep-then-fail (UTAD). Requires a prior SC/BC + AR + ST chain (the late-accumulation /
    post-deleverage precondition) — callers must not invoke this without one. Returns
    {bar_index, reclaim_bar_index, range_low, range_high} or None."""
    i0 = st["bar_index"]
    range_low = min(climax["low"], st["extreme"]) if direction == "down" else min(climax["close"], ar["close"])
    range_high = max(climax["high"], ar["close"]) if direction == "down" else max(climax["high"], st["extreme"])
    for i in range(i0 + 1, min(i0 + 1 + cfg["spring_window"], len(bars))):
        b = bars[i]
        if direction == "down" and b["low"] < range_low:
            for j in range(i, min(i + cfg["spring_reclaim_bars"] + 1, len(bars))):
                if bars[j]["close"] >= range_low:
                    return {"bar_index": i, "reclaim_bar_index": j,
                            "range_low": round(range_low, 8), "range_high": round(range_high, 8)}
        elif direction == "up" and b["high"] > range_high:
            for j in range(i, min(i + cfg["spring_reclaim_bars"] + 1, len(bars))):
                if bars[j]["close"] <= range_high:
                    return {"bar_index": i, "reclaim_bar_index": j,
                            "range_low": round(range_low, 8), "range_high": round(range_high, 8)}
    return None


def _find_impulse_break(bars, cfg, start, level, direction):
    """A clean breakout ('up', above `level`) or breakdown ('down', below `level`)
    completing within impulse_max_bars bars with a net move >= impulse_min_pct. Returns
    {bar_index, move_pct} or None (no qualifying impulse found in the window scanned)."""
    for i in range(start, len(bars)):
        b = bars[i]
        crossed = (b["close"] > level) if direction == "up" else (b["close"] < level)
        if not crossed:
            continue
        for span in range(1, cfg["impulse_max_bars"] + 1):
            j = max(0, i - span + 1)
            move = _pct(bars[j]["open"], b["close"])
            if (direction == "up" and move >= cfg["impulse_min_pct"]) or \
               (direction == "down" and move <= -cfg["impulse_min_pct"]):
                return {"bar_index": i, "move_pct": round(move, 2)}
    return None


# ── event/cycle classification ──────────────────────────────────────────────
def classify_events(bars, cfg=None, oi_series=None):
    """Detects the bottom-side (SC/AR/ST/SPRING) and top-side (BC/AR/ST/UTAD) sequences,
    each independently, and returns (events, cycle). `events` is chronologically ordered;
    every event dict carries its `type` + detection evidence. `oi_series` (optional, one
    value per bar) tags the climax evidence with `oi_corroborates` — never changes the
    detection itself (see `_oi_corroborates`)."""
    cfg = cfg or load_cfg()
    events = []
    cycle = "UNCLEAR"

    # bottom-side chain
    sc = _find_climax(bars, cfg, 0, "down")
    ar_b = _find_ar(bars, cfg, sc, "down") if sc else None
    st_b = _find_st(bars, cfg, sc, ar_b, "down") if ar_b else None
    spring = _find_spring_or_utad(bars, cfg, sc, ar_b, st_b, "down") if st_b else None

    if sc:
        sc = {**sc, "oi_corroborates": _oi_corroborates(oi_series, sc["bar_index"])}
        events.append({"type": "SC", "bar_index": sc["bar_index"], "evidence": sc})
    if ar_b:
        events.append({"type": "AR", "bar_index": ar_b["bar_index"],
                       "evidence": {**ar_b, "fragile": True}})
    if st_b:
        events.append({"type": "ST", "bar_index": st_b["bar_index"], "evidence": st_b})
        cycle = "ACCUMULATION"
    if spring:
        events.append({"type": "SPRING", "bar_index": spring["bar_index"], "evidence": spring})
        impulse = _find_impulse_break(bars, cfg, spring["reclaim_bar_index"],
                                      spring["range_high"], "up")
        if impulse:
            events.append({"type": "MARKUP_BREAK", "bar_index": impulse["bar_index"],
                           "evidence": impulse})
            cycle = "MARKUP"

    # top-side chain (independent scan — a bar set can carry either or, rarely, both
    # sequences in sequence; the later-confirmed cycle wins if both complete)
    bc = _find_climax(bars, cfg, 0, "up")
    ar_t = _find_ar(bars, cfg, bc, "up") if bc else None
    st_t = _find_st(bars, cfg, bc, ar_t, "up") if ar_t else None
    utad = _find_spring_or_utad(bars, cfg, bc, ar_t, st_t, "up") if st_t else None

    top_events = []
    if bc:
        bc = {**bc, "oi_corroborates": _oi_corroborates(oi_series, bc["bar_index"])}
        top_events.append({"type": "BC", "bar_index": bc["bar_index"], "evidence": bc})
    if ar_t:
        top_events.append({"type": "AR", "bar_index": ar_t["bar_index"],
                           "evidence": {**ar_t, "fragile": True}})
    if st_t:
        top_events.append({"type": "ST", "bar_index": st_t["bar_index"], "evidence": st_t})
    if utad:
        # "post-deleverage high-sweep" (req. 1) — a real UTAD sits on OI that already
        # dropped (positions de-levered) going into the sweep.
        utad = {**utad, "oi_corroborates": _oi_corroborates(oi_series, utad["bar_index"])}
        top_events.append({"type": "UTAD", "bar_index": utad["bar_index"], "evidence": utad})
        impulse = _find_impulse_break(bars, cfg, utad["reclaim_bar_index"],
                                      utad["range_low"], "down")
        if impulse:
            top_events.append({"type": "MARKDOWN_BREAK", "bar_index": impulse["bar_index"],
                               "evidence": impulse})

    # merge: prefer whichever chain reached the MORE DEFINITIVE cycle tier (a confirmed
    # breakout/breakdown > a merely-established range > nothing); a lone unconfirmed climax
    # on the opposite side (no ST/UTAD) must never override a chain that actually completed
    # (fixes the false-UNCLEAR bug: a strong breakout bar itself looks like a lone BC).
    if top_events:
        # top-side reads DISTRIBUTION once the ST completes and STAYS DISTRIBUTION through
        # a UTAD + breakdown (the breakdown is the confirming EVENT, not a phase change to
        # MARKDOWN in this v1 — see the module's "Known limitation" note; a SHORT signal on
        # that breakdown still gets a clean cycle_gate pass-through since DISTRIBUTION is in
        # _SHORT_AGREE).
        top_cycle = "DISTRIBUTION" if st_t else "UNCLEAR"

        def _tier(c):
            if c in ("MARKUP", "MARKDOWN"):
                return 2
            if c in ("ACCUMULATION", "RE-ACCUMULATION", "DISTRIBUTION", "RE-DISTRIBUTION"):
                return 1
            return 0

        bottom_tier, top_tier = _tier(cycle), _tier(top_cycle)
        if top_tier > bottom_tier:
            cycle = top_cycle
        elif top_tier == bottom_tier and top_tier > 0 and events:
            bottom_last = events[-1]["bar_index"]
            top_last = top_events[-1]["bar_index"]
            if top_last > bottom_last:
                cycle = top_cycle
        events = sorted(events + top_events, key=lambda e: e["bar_index"])
    else:
        events.sort(key=lambda e: e["bar_index"])

    return events, cycle


# ── grind-vs-impulse breakout flag ──────────────────────────────────────────
def classify_breakout_leg(bars, level, cfg=None):
    """The 'one fish eaten twice' fingerprint (memory:
    reference_derq_freeland_corpus_playbook §5): a breakout leg that GRINDS at the level
    (many small closes hugging it, low net impulse over many bars) gets
    `grind_trap_suspect:True`; a clean few-bar impulse break does not. Operates on the bars
    AROUND the level (caller passes the relevant window)."""
    cfg = cfg or load_cfg()
    if not bars:
        return {"grind_trap_suspect": False, "reason": "no bars"}
    near = [b for b in bars if abs(_pct(level, b["close"])) <= cfg["grind_max_close_range_pct"]]
    dwell_bars = len(near)
    closes = [b["close"] for b in bars]
    net_move = _pct(closes[0], closes[-1])
    n_bars = len(bars)
    is_grind = dwell_bars >= cfg["grind_dwell_bars"] and abs(net_move) < cfg["impulse_min_pct"] * 1.5
    is_clean_impulse = n_bars <= cfg["impulse_max_bars"] and abs(net_move) >= cfg["impulse_min_pct"]
    return {"grind_trap_suspect": bool(is_grind and not is_clean_impulse),
            "dwell_bars": dwell_bars, "net_move_pct": round(net_move, 2), "n_bars": n_bars}


# ── cycle_gate (requirement 4) ───────────────────────────────────────────────
_SHORT_AGREE = {"DISTRIBUTION", "RE-DISTRIBUTION", "MARKDOWN"}
_SHORT_CONFLICT = {"ACCUMULATION", "RE-ACCUMULATION"}
_LONG_AGREE = {"ACCUMULATION", "RE-ACCUMULATION", "MARKUP"}
_LONG_CONFLICT = {"DISTRIBUTION", "RE-DISTRIBUTION"}


def cycle_gate_for(side, cycle):
    """candle-signal (side: 'short'|'long') + cycle agree -> pass-through (agree:True);
    disagree (breakdown-short inside accumulation, breakout-long inside distribution) ->
    an explicit CYCLE_CONFLICT (conflict:True) — a WARNING, not a hard veto (§0.5 judgment
    stays with the orchestrator, but the conflict must be impossible to miss). UNCLEAR never
    conflicts (nothing confident enough to contradict the candle with)."""
    side = (side or "").lower()
    cycle = (cycle or "UNCLEAR").upper()
    if cycle == "UNCLEAR":
        return {"agree": None, "cycle": cycle, "conflict": False,
                "note": "cycle UNCLEAR — no gate applied"}
    agree_set = _SHORT_AGREE if side == "short" else _LONG_AGREE
    conflict_set = _SHORT_CONFLICT if side == "short" else _LONG_CONFLICT
    if cycle in conflict_set:
        return {"agree": False, "cycle": cycle, "conflict": True,
                "note": f"CYCLE_CONFLICT: {side} signal inside {cycle} — classify the cycle "
                       f"before trusting the candle (memory: "
                       f"feedback_wyckoff_cycle_first_breakout_gate)"}
    if cycle in agree_set:
        return {"agree": True, "cycle": cycle, "conflict": False,
                "note": f"cycle {cycle} agrees with the {side} signal"}
    return {"agree": None, "cycle": cycle, "conflict": False,
            "note": f"cycle {cycle} — no strong agree/conflict read for a {side} signal"}


def one_liner(result):
    """`phase: ACCUMULATION (last event: SPRING @14)`-shaped line, or a degrade line."""
    if not result or not result.get("available"):
        return f"phase: unavailable ({(result or {}).get('reason', 'no data')})"
    events = result.get("events") or []
    last = events[-1] if events else None
    tail = f" (last event: {last['type']} @{last['bar_index']})" if last else ""
    return f"phase: {result['cycle']}{tail}"


# ── top-level build (network — fetches bars, then pure-classifies) ─────────
def build_phase(ticker, bars=None, oi_series=None, min_bars=25, window_min=180, cfg=None):
    """bars/oi_series injectable for tests/callers with their own data. Without bars, fetches
    real 1m klines via tape.py's existing (already-tested) Binance reader — never fabricates
    OHLC from a derived/processed series. Thin/short history degrades to unavailable rather
    than guessing (DoD: 'degrades to phase: unavailable on thin/short history without
    blocking'). Known limitation: the live path does not wire OI in this v1 (klines are 1m,
    Binance's OI floor is 5m — see tape.py's own oi_interval note — a correct alignment is
    future work); oi_series stays available for callers who supply pre-aligned data."""
    cfg = cfg or load_cfg()
    if bars is None:
        try:
            import tape as T
            sym = ticker.upper().replace("USDT", "") + "USDT"
            klines = T.fetch_klines_1m(sym, window_min, "binance")
            if not klines:
                return {"ticker": ticker.upper(), "available": False,
                        "reason": "no 1m klines", "cycle": None, "events": []}
            parsed = [T._parse_kline(k) for k in klines]
            bars = [{"open": p["open"], "high": p["high"], "low": p["low"],
                    "close": p["close"], "volume": p["vol"]} for p in parsed]
        except Exception as e:  # noqa: BLE001
            return {"ticker": ticker.upper(), "available": False,
                    "reason": f"phase fetch error: {str(e)[:120]}", "cycle": None, "events": []}
    if len(bars) < min_bars:
        return {"ticker": ticker.upper(), "available": False,
                "reason": f"thin history ({len(bars)} bars < {min_bars} minimum)",
                "cycle": None, "events": []}
    events, cycle = classify_events(bars, cfg, oi_series=oi_series)
    return {"ticker": ticker.upper(), "available": True, "cycle": cycle, "events": events,
            "n_bars": len(bars)}


def main():
    ap = argparse.ArgumentParser(description="SPEC-104 phase — Wyckoff cycle/event classifier")
    ap.add_argument("ticker")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    r = build_phase(args.ticker)
    out = {"ok": bool(r.get("available")), "data": r, "meta": {}}
    if args.json:
        print(json.dumps(out, default=str))
    else:
        print(one_liner(r))
        for e in r.get("events") or []:
            print(f"  {e['type']} @{e['bar_index']}  {e['evidence']}")


if __name__ == "__main__":
    main()
