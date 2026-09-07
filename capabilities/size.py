#!/usr/bin/env python3
"""size.py — mechanical §7 sizing gate (SPEC 41).

Turns CLAUDE.md §7 into one deterministic read: liquidity gate FIRST (AUTO-PASS
short-circuits everything), then every sizing constraint with its own number and
which one BINDS:

  python3 capabilities/size.py SKYAI --direction SHORT --entry 0.37 --stop 0.40 --json
  python3 capabilities/size.py SKYAI --direction SHORT --entry 0.37 --stop 0.40 --equity 25000 --json

Constraints (each emitted separately; binding = the smallest $ cap):
  - exit_absorbable_usd — size-to-EXIT: $ resting within --band % of mid on the EXIT
    side across BOTH venue books (reuses depth.py's fetchers). DESK CONVENTION
    (spec-literal): a SHORT exits into the BIDS (you cover into the resting bid
    shelf on the way down — the defended bid is what absorbs your exit); a LONG
    exits into the ASKS. Truncated exit side (book window ends INSIDE the band,
    memory feedback_orderbook_api_truncation_defer_to_live_dom) = floor estimate
    → 0.5 haircut, never read as "no liquidity".
  - oi_cap_usd — ≤3% of primary-venue OI (reduce-only trap, §7).
  - bracket_cap_usd — ≤70% of tier-1 leverage-bracket notional. Binance's bracket
    API is auth-gated → config/brackets.json (per-venue tier-1 defaults + per-ticker
    overrides) is the source; edit it when the real bracket is known.
  - leverage_max / leverage_practical — 1/stop-distance%, practical = 75% of max;
    haircut_dex_mark flags >30% DEX-pool mark-weight (config/dex_mark_weight.json).
  - cluster — operator-cluster heat from config/clusters.json: cluster-mates with a
    live thesis (ACTIVE*/PENDING) on the watchlist ⇒ cluster_open_risk_warning
    (one position at multiplied size, ~6% combined cap — NOT a hedge).

Equity is never assumed: max_size_usd (= min of all $ caps, SCOUT haircut applied)
is emitted ONLY when --equity is passed. Read-only — a sizing READ, not a commit.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Reused capabilities — module globals so tests monkeypatch them (house style).
from regime_flip import live_perp            # vol/MC gate + primary-venue OI
import depth as _depth                       # book fetchers (SPEC 27) — reused, not duplicated

BOOK_FETCHERS = dict(_depth.VENUES)          # {venue: fn(sym) -> (bids, asks, err)}
CONFIG_DIR = HERE.parent / "config"

# §7 thresholds
VOL_AUTOPASS_M = 10.0        # < $10M/24h → AUTO-PASS
VOL_SCOUT_M = 25.0           # $10–25M   → SCOUT-ONLY
MC_AUTOPASS = 15_000_000     # < $15M MC → AUTO-PASS
BAND_PCT_DEFAULT = 2.0       # exit-liquidity band (±% of mid)
OI_CAP_PCT = 3.0             # target ≤3% of OI
BRACKET_CAP_PCT = 70.0       # ≤70% of tier-1 notional
LEV_PRACTICAL = 0.75         # practical leverage = 75% of max
SCOUT_HAIRCUT = 0.5          # SCOUT-ONLY final-size haircut
TRUNC_HAIRCUT = 0.5          # truncated exit-book floor-estimate haircut
DEX_MARK_WEIGHT_MAX = 0.30   # >30% DEX-pool mark-weight → flag
DEFAULT_TIER1_USD = 50_000   # fallback tier-1 notional when no config

# ── SPEC 61: stop geometry + entry-type guard ──────────────────────────────────
STOP_BUFFER_PCT = 1.5        # default beyond-the-wick buffer (% of the cleared wick)
MAGNET_EPS_PCT = 0.30        # proposed stop within this % of a round magnet → nudge beyond it
MAGNET_NUDGE_PCT = 0.40      # how far beyond the round magnet to push (% — never AT it, §7)
SQUEEZE_PER_10D_CHRONIC = 1.0  # >1 squeeze leg / 10d over the window = chronic squeezer (§6)
RETEST_BAND_PCT = 3.0        # entry within this % of the adverse wick = a retest-zone entry


def _cfg(name, default):
    p = CONFIG_DIR / name
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return default


# ── size-to-exit ────────────────────────────────────────────────────────────────
def _exit_liquidity(ticker, direction, band_pct, notes):
    """$ resting within band_pct of mid on the EXIT side, summed across venues.
    SHORT exits into BIDS, LONG exits into ASKS (desk convention — see module doc)."""
    sym = ticker.upper() + "USDT"
    exit_side = "bid" if direction == "SHORT" else "ask"
    total, detail = 0.0, {}
    any_book = False
    for name, fn in BOOK_FETCHERS.items():
        try:
            bids, asks, err = fn(sym)
        except Exception as e:                                   # fetcher blew up → degrade
            detail[name] = {"available": False, "reason": str(e)[:120]}
            continue
        if not bids or not asks:
            detail[name] = {"available": False, "reason": err or "empty book"}
            continue
        any_book = True
        mid = (bids[0][0] + asks[0][0]) / 2
        levels = bids if exit_side == "bid" else asks
        usd = sum(p * q for p, q in levels if abs(p - mid) / mid * 100 <= band_pct)
        # truncation: the book window ended INSIDE the band → what we see is a floor
        deepest_dist = max(abs(p - mid) / mid * 100 for p, _ in levels)
        truncated = deepest_dist < band_pct
        haircut = TRUNC_HAIRCUT if truncated else None
        counted = usd * (haircut or 1.0)
        if truncated:
            notes.append(f"{name} {exit_side} book truncated (deepest {deepest_dist:.2f}% < "
                         f"{band_pct:g}% band) — floor estimate, {TRUNC_HAIRCUT:g} haircut applied; "
                         f"live DOM sees deeper")
        detail[name] = {"available": True, "mid": round(mid, 8), "usd_within_band": round(usd, 2),
                        "truncated": truncated, "haircut": haircut, "usd_counted": round(counted, 2)}
        total += counted
    if not any_book:
        notes.append("no venue book available — exit_absorbable unknown")
        return None, exit_side, detail
    return round(total, 2), exit_side, detail


# ── bracket / dex-mark / cluster config reads ──────────────────────────────────
def _bracket_cap(ticker, primary_venue, notes):
    cfg = _cfg("brackets.json", {})
    tier1 = (cfg.get("overrides", {}).get(ticker)
             or cfg.get("venues", {}).get(primary_venue or "")
             or cfg.get("default_tier1_usd")
             or DEFAULT_TIER1_USD)
    if not cfg:
        notes.append(f"config/brackets.json missing — tier-1 default ${DEFAULT_TIER1_USD:,} used "
                     f"(override there when the real bracket is known)")
    return round(BRACKET_CAP_PCT / 100 * float(tier1), 2), float(tier1)


def _dex_mark_flag(ticker, notes):
    w = _cfg("dex_mark_weight.json", {}).get("tokens", {}).get(ticker)
    if w is not None and float(w) > DEX_MARK_WEIGHT_MAX:
        notes.append(f"DEX-pool mark-weight {float(w)*100:.0f}% > {DEX_MARK_WEIGHT_MAX*100:.0f}% — "
                     f"composite-mark wick/liquidation risk, haircut leverage further (§7)")
        return True
    return False


def _live_status(status):
    s = (status or "").upper()
    return s.startswith("ACTIVE") or s == "PENDING"


def _cluster_heat(ticker, notes):
    cfg = _cfg("clusters.json", {})
    name, members = None, []
    for cname, c in cfg.get("clusters", {}).items():
        if ticker in [m.upper() for m in c.get("members", [])]:
            name, members = cname, [m.upper() for m in c.get("members", [])]
            break
    if not name:
        return None
    wl = _cfg("watchlist.json", {})
    open_members = []
    for tok in wl.get("tokens", []):
        tk = (tok.get("ticker") or "").upper()
        th = tok.get("thesis") or {}
        if tk != ticker and tk in members and _live_status(th.get("status")):
            open_members.append({"ticker": tk, "status": th.get("status"),
                                 "direction": th.get("direction")})
    warn = bool(open_members)
    if warn:
        notes.append(f"cluster {name}: live theses on {[m['ticker'] for m in open_members]} — "
                     f"cluster-mates are ONE position; cap combined risk "
                     f"~{cfg.get('max_operator_heat_pct', 6.0):g}% (§7 operator-heat)")
    return {"name": name, "members": members, "open_members": open_members,
            "cluster_open_risk_warning": warn,
            "max_operator_heat_pct": cfg.get("max_operator_heat_pct", 6.0)}


# ── SPEC 61: stop geometry (pure) ───────────────────────────────────────────────
def propose_stop(direction, entry, h24=None, l24=None, window_high=None, window_low=None,
                 round_magnets=None, buffer_pct=STOP_BUFFER_PCT):
    """Propose a stop BEYOND the TRUE relevant wick, not a partial-window one (the ESPORTS
    −1R / BEAT near-miss lessons). The adverse extreme is max(24h extreme, full-history
    window extreme scoped to the current leg) on the stop side; the buffer keeps the stop
    beyond it; a magnet-aware nudge keeps it off a round level (§7 — a stop AT the round is
    donated to the sweep). Names the wick it cleared + which source produced it.

    SHORT → stop ABOVE the highest adverse wick; LONG → BELOW the lowest adverse wick."""
    direction = (direction or "").upper()
    round_magnets = round_magnets or []
    if direction == "SHORT":
        cands = {"24h_extreme": h24, "window_full_history": window_high}
    elif direction == "LONG":
        cands = {"24h_extreme": l24, "window_full_history": window_low}
    else:
        return {"error": f"direction must be LONG or SHORT, got {direction!r}"}
    present = {k: float(v) for k, v in cands.items() if v is not None}
    if not present:
        return {"direction": direction, "entry": entry, "stop_proposed": None,
                "cleared_wick": None, "cleared_source": None,
                "note": "no 24h/window extreme available — cannot propose a true-wick stop"}

    if direction == "SHORT":
        source = max(present, key=present.get)          # the highest wick wins
        wick = present[source]
        proposed = wick * (1 + buffer_pct / 100)
    else:
        source = min(present, key=present.get)          # the lowest wick wins
        wick = present[source]
        proposed = wick * (1 - buffer_pct / 100)

    magnet_adjusted = False
    for m in sorted(round_magnets, reverse=(direction == "SHORT")):
        if m <= 0:
            continue
        if abs(proposed - m) / m * 100 <= MAGNET_EPS_PCT:
            proposed = (m * (1 + MAGNET_NUDGE_PCT / 100) if direction == "SHORT"
                        else m * (1 - MAGNET_NUDGE_PCT / 100))
            magnet_adjusted = True
            break

    return {"direction": direction, "entry": entry,
            "stop_proposed": round(proposed, 10), "cleared_wick": round(wick, 10),
            "cleared_source": source, "buffer_pct": buffer_pct,
            "magnet_adjusted": magnet_adjusted, "candidates": present,
            "note": (f"stop beyond the {source} wick {wick:g} (+{buffer_pct:g}% buffer"
                     + (", nudged off a round magnet" if magnet_adjusted else "") + ")")}


def entry_type_guard(direction, entry, squeezes_count, window_days,
                     adverse_extreme=None, retest_band_pct=RETEST_BAND_PCT):
    """SPEC 61 / memory feedback_squeezer_wicks_escalate: a chronic squeezer (>1 squeeze leg
    per 10d over the window) is scalp-only post-breakdown — kill retest-zone entries. Emits
    `entry_type_required:"post_breakdown"` + a warning if the caller's entry sits inside the
    retest zone (within retest_band% of the adverse wick on the squeeze side)."""
    direction = (direction or "").upper()
    legs_per_10d = round(squeezes_count / (window_days / 10.0), 3) if window_days else 0.0
    chronic = legs_per_10d > SQUEEZE_PER_10D_CHRONIC
    out = {"chronic_squeezer": chronic, "legs": squeezes_count, "window_days": window_days,
           "legs_per_10d": legs_per_10d, "entry_type_required": None, "warning": None}
    if not chronic:
        return out
    out["entry_type_required"] = "post_breakdown"
    in_retest = False
    if adverse_extreme and entry:
        dist = abs(adverse_extreme - entry) / adverse_extreme * 100
        # SHORT retest = entry just BELOW the high; LONG retest = entry just ABOVE the low
        on_squeeze_side = (entry <= adverse_extreme) if direction == "SHORT" else (entry >= adverse_extreme)
        in_retest = on_squeeze_side and dist <= retest_band_pct
    if in_retest or adverse_extreme is None:
        out["warning"] = (f"chronic squeezer ({legs_per_10d}/10d) — entry_type_required: "
                          f"post_breakdown; this entry sits in the retest zone (within "
                          f"{retest_band_pct:g}% of the {adverse_extreme} wick). Take only the "
                          f"momentum entry AFTER the level breaks (feedback_squeezer_wicks_escalate).")
    return out


def propose_stop_live(ticker, direction, entry):
    """Live wrapper: gather the 24h extreme + full-history window extreme (scoped to the
    current leg) + round magnets, then propose. Degrades to a null proposal on read failure
    (never raises — a sizing READ, not a gate)."""
    tk = ticker.upper().replace("USDT", "")
    h24 = l24 = window_high = window_low = None
    squeezes_count, window_days = 0, None
    magnets = []
    try:
        from classify import _ticker_hl
        hl = _ticker_hl(f"{tk}USDT", "binance") or _ticker_hl(f"{tk}USDT", "bybit")
        if hl:
            h24, l24 = hl[0], hl[1]
    except Exception:  # noqa: BLE001
        pass
    try:
        from price_structure import build_structure
        ps = build_structure(tk, days=30)               # current leg ≈ trailing 30d window
        if ps and not ps.get("error"):
            window_high, window_low = ps.get("window_high"), ps.get("window_low")
            squeezes_count = len(ps.get("squeezes") or [])
            window_days = ps.get("window_days") or ps.get("days_available")
    except Exception:  # noqa: BLE001
        pass
    try:
        import tape
        magnets = tape.round_numbers_near(window_low or l24, window_high or h24)
    except Exception:  # noqa: BLE001
        magnets = []
    prop = propose_stop(direction, entry, h24=h24, l24=l24,
                        window_high=window_high, window_low=window_low, round_magnets=magnets)
    guard = entry_type_guard(direction, entry, squeezes_count, window_days or 60,
                             adverse_extreme=prop.get("cleared_wick"))
    return {**prop, "entry_guard": guard}


# ── main build ──────────────────────────────────────────────────────────────────
def build_size(ticker, direction, entry, stop, equity=None, band_pct=BAND_PCT_DEFAULT,
               mc_usd=None, propose_stop_flag=False):
    tk = ticker.upper().replace("USDT", "")
    direction = direction.upper()
    notes = []
    out = {"ticker": tk, "direction": direction, "entry": entry, "stop": stop,
           "band_pct": band_pct, "notes": notes}

    # ── SPEC 61: stop geometry + entry-type guard (additive; never blocks sizing) ──
    if propose_stop_flag:
        try:
            geo = propose_stop_live(tk, direction, entry)
        except Exception as e:  # noqa: BLE001
            geo = {"error": f"stop-geometry read failed: {str(e)[:120]}"}
        out["stop_proposal"] = geo
        if stop is not None and geo.get("stop_proposed") is not None:
            inside = (stop <= geo["stop_proposed"]) if direction == "SHORT" \
                else (stop >= geo["stop_proposed"])
            if inside:
                notes.append(f"⚠ committed stop {stop:g} is INSIDE the proposed true-wick stop "
                             f"{geo['stop_proposed']:g} (beyond the {geo.get('cleared_source')} wick "
                             f"{geo.get('cleared_wick')}) — it can be out-wicked (SPEC 61)")
        g = geo.get("entry_guard") or {}
        if g.get("entry_type_required"):
            out["entry_type_required"] = g["entry_type_required"]
        if g.get("warning"):
            notes.append(g["warning"])

    # ── LIQUIDITY GATE FIRST (§7) — AUTO-PASS short-circuits all constraint math ──
    live = live_perp(tk)
    vol_m = live.get("vol_m") if live else None
    out["gate_detail"] = {"vol_m_24h": vol_m, "mc_usd": mc_usd}
    if (vol_m is not None and vol_m < VOL_AUTOPASS_M) or (mc_usd is not None and mc_usd < MC_AUTOPASS):
        why = (f"24h vol ${vol_m:g}M < ${VOL_AUTOPASS_M:g}M" if (vol_m is not None and vol_m < VOL_AUTOPASS_M)
               else f"MC ${mc_usd/1e6:g}M < ${MC_AUTOPASS/1e6:g}M")
        notes.append(f"AUTO-PASS: {why} — not tradeable, no constraint math run")
        out.update({"gate": "AUTO-PASS", "constraints": None, "binding": None})
        return out
    scout = False
    if vol_m is None:
        out["gate"] = "FULL"
        notes.append("24h vol unavailable (no perp listing read) — liquidity gate NOT verified")
    elif vol_m < VOL_SCOUT_M:
        out["gate"] = "SCOUT-ONLY"
        scout = True
        notes.append(f"SCOUT-ONLY: ${vol_m:g}M/24h in the ${VOL_AUTOPASS_M:g}–{VOL_SCOUT_M:g}M band — "
                     f"final size haircut {SCOUT_HAIRCUT*100:.0f}%")
    else:
        out["gate"] = "FULL"

    # ── constraints ──
    exit_usd, exit_side, exit_detail = _exit_liquidity(tk, direction, band_pct, notes)
    out["exit_side"] = exit_side
    out["exit_detail"] = exit_detail

    oi_cap = None
    primary = live.get("primary_venue") if live else None
    if live and live.get("oi") and live.get("price"):
        oi_usd = float(live["oi"]) * float(live["price"])
        oi_cap = round(OI_CAP_PCT / 100 * oi_usd, 2)
        out["oi_usd"] = round(oi_usd, 2)
        out["oi_venue"] = primary
    else:
        notes.append("primary-venue OI unavailable — oi_cap not computed")

    bracket_cap, tier1 = _bracket_cap(tk, primary, notes)
    out["bracket_tier1_usd"] = tier1

    stop_dist_pct = abs(stop - entry) / entry * 100
    lev_max = round(100 / stop_dist_pct, 2) if stop_dist_pct > 0 else None
    lev_practical = round(lev_max * LEV_PRACTICAL, 2) if lev_max else None
    out["stop_distance_pct"] = round(stop_dist_pct, 3)
    out["haircut_dex_mark"] = _dex_mark_flag(tk, notes)

    cluster = _cluster_heat(tk, notes)

    constraints = {"exit_absorbable_usd": exit_usd, "oi_cap_usd": oi_cap,
                   "bracket_cap_usd": bracket_cap, "leverage_max": lev_max,
                   "leverage_practical": lev_practical, "cluster": cluster}
    candidates = {k: v for k, v in {"exit_absorbable": exit_usd, "oi_cap": oi_cap,
                                    "bracket_cap": bracket_cap}.items() if v is not None}
    if equity is not None and lev_practical:
        lev_cap = round(float(equity) * lev_practical, 2)
        constraints["leverage_cap_usd"] = lev_cap
        candidates["leverage_cap"] = lev_cap
    out["constraints"] = constraints

    if candidates:
        binding = min(candidates, key=candidates.get)
        out["binding"] = binding
        if equity is not None:
            max_size = candidates[binding] * (SCOUT_HAIRCUT if scout else 1.0)
            out["max_size_usd"] = round(max_size, 2)
            out["max_size_note"] = (f"min of {sorted(candidates)} = {binding}"
                                    + (f" × {SCOUT_HAIRCUT:g} scout haircut" if scout else ""))
    else:
        out["binding"] = None
        notes.append("no $ constraint computable — do not size off this read")
    return out


def render_human(d):
    print(f"# {d['ticker']} {d['direction']} — §7 sizing gate  [{d['gate']}]")
    gd = d.get("gate_detail", {})
    print(f"  vol ${gd.get('vol_m_24h')}M/24h"
          + (f", MC ${gd['mc_usd']/1e6:.1f}M" if gd.get("mc_usd") else ""))
    if d.get("constraints"):
        c = d["constraints"]
        def usd(v):
            return f"${v:,.0f}" if v is not None else "n/a"
        print(f"  exit_absorbable ({d.get('exit_side')}s ±{d['band_pct']:g}%): {usd(c['exit_absorbable_usd'])}")
        print(f"  oi_cap (3% of OI): {usd(c['oi_cap_usd'])}   bracket_cap (70% tier-1): {usd(c['bracket_cap_usd'])}")
        print(f"  leverage: max {c['leverage_max']}x / practical {c['leverage_practical']}x"
              + ("  ⚠ DEX-mark haircut" if d.get("haircut_dex_mark") else ""))
        if c.get("cluster"):
            cl = c["cluster"]
            print(f"  cluster {cl['name']}: open {[m['ticker'] for m in cl['open_members']]}"
                  + ("  ⚠ OPERATOR-HEAT" if cl["cluster_open_risk_warning"] else ""))
        print(f"  BINDING: {d.get('binding')}"
              + (f"  → max_size ${d['max_size_usd']:,.0f}" if "max_size_usd" in d else "  (pass --equity for max_size_usd)"))
    for n in d.get("notes", []):
        print(f"  - {n}")


def main():
    ap = argparse.ArgumentParser(description="Mechanical §7 sizing gate (SPEC 41) — read-only")
    ap.add_argument("ticker")
    ap.add_argument("--direction", required=True, help="LONG|SHORT")
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--stop", type=float, required=True)
    ap.add_argument("--equity", type=float, default=None,
                    help="account equity $ — only then max_size_usd is emitted")
    ap.add_argument("--band", type=float, default=BAND_PCT_DEFAULT,
                    help="exit-liquidity band, %% of mid")
    ap.add_argument("--mc", type=float, default=None, help="market cap $ (gate input)")
    ap.add_argument("--propose-stop", action="store_true",
                    help="propose a stop beyond the TRUE wick + entry-type guard (SPEC 61)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.direction.upper() not in ("LONG", "SHORT"):
        print(json.dumps({"error": f"direction must be LONG or SHORT, got {args.direction!r}"}))
        sys.exit(2)
    if args.entry <= 0 or args.stop <= 0 or args.entry == args.stop:
        print(json.dumps({"error": "entry/stop must be positive and distinct"}))
        sys.exit(2)
    d = build_size(args.ticker, args.direction, args.entry, args.stop,
                   equity=args.equity, band_pct=args.band, mc_usd=args.mc,
                   propose_stop_flag=args.propose_stop)
    if args.json:
        print(json.dumps(d))
    else:
        render_human(d)


if __name__ == "__main__":
    main()
