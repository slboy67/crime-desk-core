#!/usr/bin/env python3
"""counterfactual.py — score the desk's OWN committed calls (SPEC 81).

The §9 base-rate gate validates the desk's actual judgment, but the live ledger can't
feed it: a retire records only THAT a thesis closed, never WHAT price did afterward, so
20/23 live rows are `retired_unfilled` with `pnl_r: null` — unscoreable. `replay`
(SPEC 62) backtests the deterministic SCORERS over price history (a different
population — it re-derives its own entries from klines); it does NOT evaluate the
entry/stop/TP the desk actually COMMITTED.

This module closes that gap. Given a committed thesis (direction, entry_zone, stop,
tp[], commit_ts, time_stop) + forward klines, it walks forward from commit through the
COMMITTED geometry and records what would have happened — so each desk call becomes a
scoreable datum WITHOUT anyone having had to trade it:

  - never filled  → outcome stays `retired_unfilled`, but carries a `counterfactual`
                    block recording WHY (never reached zone) + the MFE/MAE the unfilled
                    thesis would have seen. "correctly stood aside" vs "missed a runner".
  - filled        → simulate from the fill bar with stop/tp (reusing
                    `replay.simulate_trade` — STOP-checked-first conservatism, no second
                    simulator) and record the real outcome + pnl_r + bars_held + mae_r.

The entry-fill respects the SPEC-79 breakdown-vs-fade split: a SHORT whose entry_zone
sits BELOW commit price is a sell-the-breakdown (fills on the trade DOWN into the zone,
entry at the zone TOP); a SHORT whose zone is ABOVE commit price is a fade-the-rally
(fills on the trade UP into it, entry at the zone BOTTOM). Symmetric for LONG.

Every row is tagged `source: "counterfactual"` — a THIRD bucket, never silently merged
with `live` (a hand-traded fill) or `replay` (a scorer re-derivation). `ledger._by_source`
already splits it; the §9 gate reads it as its own population.

Price/funding/OI only — NO Moralis (runs while quota is dead, G2).

  python3 capabilities/counterfactual.py '{"action":"backfill"}' --json
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ledger                         # SPEC 40/63 — the outcome scoreboard
import replay                         # SPEC 62 — reuse simulate_trade (do NOT fork it)
import thesis                         # SPEC-146: the ONE typed thesis-geometry parser + WL_PATH owner

REPO = HERE.parent
WL_PATH = thesis.WL_PATH   # SPEC-146 req 4: thesis.py owns the path
REPLAY_DIR = REPO / "state" / "replay"

SOURCE = "counterfactual"


# ── the pure scorer ──────────────────────────────────────────────────────────────
# SPEC-146: entry_zone parsing (formerly a private _zone(th) here) now goes through
# thesis.parse({"thesis": th}).zone — the same sorted (lo, hi) every other reader gets.
# SPEC-147: fill/mode/entry price now come from thesis.fill_of (formerly a private
# _infer_mode here) — the same rule classify.eval_price_leg uses.

def _build_window(bars, time_stop_bars):
    """The horizon-limited forward window: the committed time_stop in bars (1h), else
    the whole forward run. Shared by row-level and per-leg scoring (SPEC-155)."""
    H = len(bars)
    horizon = min(H, time_stop_bars) if time_stop_bars else H
    win = bars[:horizon]
    return {"high": max(b["high"] for b in win), "low": min(b["low"] for b in win),
            "candles": win}


def _score_from_fill(direction, stop, tps, bars, window, fill):
    """The post-fill half of scoring: MFE/MAE + (if filled) replay.simulate_trade,
    given an already-resolved `fill` (thesis.fill_of shape, or the leg close-beyond
    equivalent). One pipeline shared by row-level geometry and every leg (SPEC-155) —
    a fill is a fill regardless of which rule decided it."""
    win = window["candles"]
    horizon = len(win)
    mode, entry_px, fill_idx = fill["mode"], fill["entry_px"], fill["entry_idx"]
    risk = abs(stop - entry_px)
    if risk <= 0:
        return {"scored": False, "skip": "bad_geometry"}

    lows = [b["low"] for b in win]
    highs = [b["high"] for b in win]
    if direction == "SHORT":
        mfe_r = (entry_px - min(lows)) / risk
        mae_r = (max(highs) - entry_px) / risk
    else:
        mfe_r = (max(highs) - entry_px) / risk
        mae_r = (entry_px - min(lows)) / risk

    if fill_idx is None:
        cf = {"filled": False, "reason": "never_filled", "entry_mode": mode,
              "fill_source": fill["source"],
              "entry_ref": round(entry_px, 10), "mfe_r": round(mfe_r, 3),
              "mae_r": round(mae_r, 3), "horizon_bars": horizon}
        return {"scored": True, "outcome": "retired_unfilled", "pnl_r": None,
                "source": SOURCE, "counterfactual": cf}

    # filled → simulate from the bar AFTER the fill (entry executes at the zone edge;
    # excluding the fill bar avoids charging a stop against a pre-fill wick, and mirrors
    # replay's signal-bar-close convention). Reuse replay.simulate_trade — STOP first.
    if tps:
        tp1 = tps[0]
        tp2 = tps[1] if len(tps) > 1 else tps[0]
    else:
        # a no-tp leg (SPEC-155 req 1) scores stop/time-stop outcomes only — an
        # unreachable sentinel means simulate_trade's tp branches never fire, without
        # forking its logic (non-goal: no change to simulate_trade itself).
        tp1 = tp2 = float("-inf") if direction == "SHORT" else float("inf")
    remaining = max(1, horizon - fill_idx - 1)
    outcome, pnl_r, held, sim_mae = replay.simulate_trade(
        bars[fill_idx + 1:], entry_px, stop, tp1, tp2, direction, time_stop_bars=remaining)

    # map the sim outcome onto the ledger enum. time_stop → retired_unfilled but KEEPS
    # its mark-to-market pnl_r (a marked position, neither hit nor miss — like replay).
    if outcome in ("tp1", "tp2", "stopped"):
        ledger_outcome, ledger_pnl = outcome, pnl_r
    elif outcome == "time_stop":
        ledger_outcome, ledger_pnl = "retired_unfilled", pnl_r
    else:                                # invalid / no_data
        ledger_outcome, ledger_pnl = "retired_unfilled", None

    cf = {"filled": True, "entry_mode": mode, "fill_source": fill["source"],
          "entry_px": round(entry_px, 10),
          "fill_idx": fill_idx, "sim_outcome": outcome, "pnl_r": pnl_r,
          "bars_held": held, "mae_r": sim_mae, "horizon_bars": horizon}
    return {"scored": True, "outcome": ledger_outcome, "pnl_r": ledger_pnl,
            "source": SOURCE, "counterfactual": cf}


def score_counterfactual(th, bars, time_stop_bars=None):
    """Score one committed thesis against forward klines. Returns a dict:
      {scored: False, skip: <why>}                              — no machine-watchable geometry
      {scored: True, outcome, pnl_r, source, counterfactual{}}  — a scoreable datum
    `bars` are 1h klines ordered from commit_ts forward (the desk's native bar).

    SPEC-147: fill (whether/mode/price) now comes from thesis.fill_of — the SAME rule
    classify.eval_price_leg uses, so the board and this paper record can never disagree
    about what filled or at what price."""
    direction = (th.get("direction") or "").upper()
    z = thesis.parse({"thesis": th}).zone
    stop = th.get("stop")
    tps = th.get("tp") or th.get("tps") or []
    if direction not in ("SHORT", "LONG") or z is None or stop is None or not tps:
        return {"scored": False, "skip": "no_geometry"}
    if not bars:
        return {"scored": False, "skip": "no_klines"}

    if time_stop_bars is None and isinstance(th.get("time_stop_h"), (int, float)):
        time_stop_bars = max(1, round(th["time_stop_h"]))
    window = _build_window(bars, time_stop_bars)
    fill = thesis.fill_of(th, direction, window)
    return _score_from_fill(direction, stop, tps, bars, window, fill)


# ── SPEC-155: per-leg geometry ──────────────────────────────────────────────────
# The two-leg rule (CLAUDE.md §0.5) commits geometry in thesis.legs[], not the
# top-level entry_zone/stop/tp — those stay null on a rule-compliant WATCH row. Each
# leg is its own scoreable unit: its own direction (derived from its own geometry,
# never assumed from the row's direction — CYS's reclaim_long + momentum_breakdown
# point opposite ways within one row) and its own fill semantics by `kind`.

_LEG_NUM_RE = re.compile(r"[-+]?\d*\.\d+|[-+]?\d+")


def _leg_level(leg):
    """A leg's numeric entry reference as (lo, hi): entry_zone -> its bounds; a numeric
    `entry` -> (v, v); a string `entry` (e.g. 'hold_below_0.559') -> its trailing number,
    (v, v). None if nothing resolvable."""
    ez = leg.get("entry_zone")
    if (isinstance(ez, (list, tuple)) and len(ez) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in ez)):
        return (min(float(ez[0]), float(ez[1])), max(float(ez[0]), float(ez[1])))
    entry = leg.get("entry")
    if isinstance(entry, (int, float)) and not isinstance(entry, bool):
        return (float(entry), float(entry))
    if isinstance(entry, str):
        nums = _LEG_NUM_RE.findall(entry)
        if nums:
            v = float(nums[-1])
            return (v, v)
    return None


def _leg_direction(lo, hi, stop, row_direction=None):
    """stop above the entry reference = SHORT, below = LONG (SPEC-155 req 2). A stop
    landing exactly on a scalar entry is unresolvable (bad_leg_geometry). A stop inside
    a zone is ambiguous and falls back to the row's own direction as tiebreak."""
    if stop is None:
        return None
    if lo == hi:
        if stop == lo:
            return None
        return "SHORT" if stop > lo else "LONG"
    if stop > hi:
        return "SHORT"
    if stop < lo:
        return "LONG"
    rd = (row_direction or "").upper()
    return rd if rd in ("LONG", "SHORT") else None


def _leg_kind_family(kind):
    """retest_fade/reclaim_* fill on a zone TOUCH (SPEC-79 fade fill, reusing
    thesis.fill_of); momentum/breakdown/*_hold fill on a CLOSE beyond the named level
    (the hold requirement — a mere wick doesn't count). None if `kind` doesn't say."""
    k = (kind or "").lower()
    if k == "retest_fade" or k.startswith("reclaim"):
        return "zone_touch"
    if k == "momentum" or "breakdown" in k or k.endswith("_hold"):
        return "close_beyond"
    return None


def leg_units(th):
    """Every leg in th['legs'] as an independent scoreable unit descriptor:
      {idx, kind, status: 'ok'|'bad_leg_geometry'|'no_geometry', ...}
    'ok' units carry direction/family/zone/level/stop/tp/entry_mode. A leg with no
    numeric stop or no resolvable entry reference is 'no_geometry' (never machine-
    watchable, same bucket a paramless row lands in); a leg whose geometry can't
    resolve a direction is 'bad_leg_geometry'. Pure; never raises."""
    legs = th.get("legs")
    if not isinstance(legs, list):
        return []
    row_direction = th.get("direction")
    out = []
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            out.append({"idx": i, "kind": None, "status": "no_geometry"})
            continue
        kind = leg.get("kind")
        stop = leg.get("stop")
        if isinstance(stop, bool) or not isinstance(stop, (int, float)):
            out.append({"idx": i, "kind": kind, "status": "no_geometry"})
            continue
        stop = float(stop)
        lvl = _leg_level(leg)
        if lvl is None:
            out.append({"idx": i, "kind": kind, "status": "no_geometry"})
            continue
        lo, hi = lvl
        direction = _leg_direction(lo, hi, stop, row_direction)
        if direction is None:
            out.append({"idx": i, "kind": kind, "status": "bad_leg_geometry"})
            continue
        family = _leg_kind_family(kind) or ("zone_touch" if leg.get("entry_zone") else "close_beyond")
        raw_tps = leg.get("tp") or leg.get("tps") or []
        tps = [float(t) for t in raw_tps if isinstance(t, (int, float)) and not isinstance(t, bool)]
        out.append({"idx": i, "kind": kind, "status": "ok", "direction": direction,
                    "family": family, "zone": (lo, hi) if leg.get("entry_zone") else None,
                    "level": lo, "stop": stop, "tp": tps,
                    "entry_mode": leg.get("entry_mode")})
    return out


def _close_beyond_fill(direction, level, bars):
    """The momentum/breakdown-hold fill rule: the first candle whose CLOSE prints
    beyond `level` in `direction` — SHORT closes below, LONG closes above. A wick that
    crosses `level` without the bar closing past it does NOT fill (SPEC-79's hold
    requirement, modeled per leg here instead of per row)."""
    for i, b in enumerate(bars):
        c = b.get("close")
        if c is None:
            continue
        if (direction == "SHORT" and c < level) or (direction == "LONG" and c > level):
            return {"filled": True, "mode": "breakdown_hold", "entry_px": level,
                    "entry_idx": i, "source": "leg_close_beyond"}
    return {"filled": False, "mode": "breakdown_hold", "entry_px": level,
            "entry_idx": None, "source": "leg_close_beyond"}


def score_leg_counterfactual(unit, bars, time_stop_bars=None):
    """Score one 'ok' leg_units() unit against forward klines — same return shape as
    score_counterfactual. zone_touch legs fill via thesis.fill_of (the same touch rule
    a top-level zone uses); close_beyond legs fill via _close_beyond_fill."""
    if not bars:
        return {"scored": False, "skip": "no_klines"}
    window = _build_window(bars, time_stop_bars)
    if unit["family"] == "zone_touch" and unit["zone"]:
        temp_th = {"entry_zone": list(unit["zone"]), "entry_mode": unit.get("entry_mode")}
        fill = thesis.fill_of(temp_th, unit["direction"], window)
        if fill is None:
            return {"scored": False, "skip": "bad_geometry"}
    else:
        fill = _close_beyond_fill(unit["direction"], unit["level"], window["candles"])
    return _score_from_fill(unit["direction"], unit["stop"], unit["tp"], bars, window, fill)


# ── ledger plumbing ────────────────────────────────────────────────────────────
def _record_row(ticker, direction, entry_zone, stop, tp, res, signature, commit_ts,
                close_ts=None, notes="", leg_idx=None, leg_kind=None):
    """Write one scored counterfactual into the ledger, tagged source:"counterfactual".
    `leg_idx`/`leg_kind` are None for a top-level (row-geometry) score; set for a
    per-leg score (SPEC-155 req 4) — the pair is the per-leg dedup key."""
    row = {
        "ticker": ticker,
        "direction": (direction or "?").upper(),
        "signature": signature,
        "entry_zone": entry_zone,
        "stop": stop,
        "tp": tp,
        "outcome": res["outcome"],
        "pnl_r": res["pnl_r"],
        "commit_ts": commit_ts,
        "close_ts": close_ts,
        "counterfactual": res["counterfactual"],
        "notes": notes or "counterfactual scoring of the committed call (SPEC 81)",
        "source": SOURCE,
        "leg_idx": leg_idx,
        "leg_kind": leg_kind,
    }
    return ledger.record(row)["record"]


def _record_row_of(ticker, th, res, signature, commit_ts, close_ts=None, notes=""):
    """Top-level-geometry convenience wrapper: derives direction/entry_zone/stop/tp
    from the row's own thesis dict `th` (the pre-SPEC-155 call shape)."""
    z = thesis.parse({"thesis": th}).zone
    return _record_row(ticker, th.get("direction"), list(z) if z else None, th.get("stop"),
                       th.get("tp") or th.get("tps") or [], res, signature, commit_ts,
                       close_ts=close_ts, notes=notes)


def _thesis_signature(th, live_signature=None):
    """The counterfactual belongs in the SAME signature bucket as the desk's committed
    call: prefer the live row's recorded signature, else canon the thesis setup."""
    if live_signature in ledger.SIGNATURES:
        return live_signature
    return ledger.canon_signature(th.get("signature") or th.get("setup"))


def _geometry_from_row(r):
    """SPEC-162 req 3: reconstruct a thesis-shaped dict from a ledger row's own inline
    `geometry` snapshot (`ledger.commit_open`) — None when the row carries no snapshot
    (every row recorded before SPEC-162, including the 39 legacy open rows), so the
    caller falls back to the watchlist join exactly as before this spec."""
    snap = r.get("geometry")
    if not isinstance(snap, dict):
        return None
    return {"direction": r.get("direction") or snap.get("direction"),
           "entry_zone": snap.get("entry_zone"), "stop": snap.get("stop"),
           "tp": snap.get("tp"), "entry_mode": snap.get("entry_mode"),
           "legs": snap.get("legs")}


# ── backfill: score all existing pnl_r:null live rows that carry geometry ─────────
def _wl_geometry_index(wl_path=None):
    """Map (TICKER, committed_ts) → the committed thesis block, across tokens + retired."""
    wl_path = Path(wl_path) if wl_path else WL_PATH
    if not wl_path.exists():
        return {}
    wl = json.loads(wl_path.read_text())
    idx = {}
    for arr in ("tokens", "retired"):
        for tok in wl.get(arr, []):
            th = tok.get("thesis")
            if not th:
                continue
            key = ((tok.get("ticker") or "").upper(), th.get("committed_ts"))
            idx[key] = th
    return idx


def backfill(ledger_path=None, wl_path=None, bars_provider=None):
    """Score every `pnl_r: null` LIVE row that has committed geometry. Idempotent:
    a (ticker, commit_ts) already scored as a counterfactual row is never re-scored.
    discretionary / WATCH rows with null entry_zone are counted `skipped: no_geometry`
    (they were never machine-watchable — the SPEC-77 gap). Returns a scored/skipped
    report. `bars_provider(ticker, commit_ts) -> [bars]|None` is injected (tests) or
    defaults to the cache-or-fetch live provider.

    SPEC-155: the two-leg rule (CLAUDE.md §0.5) commits geometry in thesis.legs[], not
    top-level entry_zone/stop/tp — those stay null on a rule-compliant WATCH row, which
    made every such commit read `no_geometry` (59% of the open board, measured
    2026-08-24). When the row has no top-level geometry, each leg is scored as its own
    independent counterfactual unit instead, deduped per (ticker, commit_ts, leg_idx) —
    a two-leg row yields two datapoints. Top-level geometry, when present, still scores
    exactly as before (no legs are consulted for that row)."""
    if ledger_path:
        ledger.LEDGER_PATH = Path(ledger_path)
    if bars_provider is None:
        bars_provider = live_bars_provider
    geom = _wl_geometry_index(wl_path)

    recs = ledger._load()
    already_top = {(r.get("ticker"), r.get("commit_ts")) for r in recs
                   if r.get("source") == SOURCE and r.get("leg_idx") is None}
    already_leg = {(r.get("ticker"), r.get("commit_ts"), r.get("leg_idx")) for r in recs
                   if r.get("source") == SOURCE and r.get("leg_idx") is not None}
    skipped = {}
    scored = 0

    def _skip(reason):
        nonlocal skipped
        skipped[reason] = skipped.get(reason, 0) + 1

    for r in recs:
        if (r.get("source") or "live") != "live" or r.get("pnl_r") is not None:
            continue
        ticker = (r.get("ticker") or "").upper()
        commit_ts = r.get("commit_ts")
        # SPEC-162 req 3: the row's own inline geometry snapshot (ledger.commit_open)
        # is authoritative when present — scoring survives a later board rewrite/
        # re-anchor/retirement; the watchlist join is now the LEGACY fallback (rows
        # recorded before the snapshot existed).
        th = _geometry_from_row(r) or geom.get((ticker, commit_ts))
        if th is None:
            _skip("no_geometry")
            continue

        top_scoreable = (thesis.parse({"thesis": th}).zone is not None
                         and th.get("stop") is not None
                         and bool(th.get("tp") or th.get("tps")))
        if top_scoreable:
            top_key = (r.get("ticker"), commit_ts)
            if top_key in already_top:
                _skip("already_scored")
                continue
            bars = bars_provider(ticker, commit_ts)
            if not bars:
                _skip("no_klines")
                continue
            res = score_counterfactual(th, bars)
            if not res["scored"]:
                _skip(res["skip"])
                continue
            sig = _thesis_signature(th, r.get("signature"))
            _record_row_of(ticker, th, res, sig, commit_ts, close_ts=r.get("close_ts"),
                           notes=f"backfill counterfactual of live call ({r.get('outcome')})")
            already_top.add(top_key)
            scored += 1
            continue

        # no top-level geometry — score every leg independently (SPEC-155)
        units = leg_units(th)
        if not units:
            _skip("no_geometry")
            continue
        bars, bars_fetched = None, False
        for u in units:
            if u["status"] != "ok":
                _skip(u["status"])
                continue
            leg_key = (r.get("ticker"), commit_ts, u["idx"])
            if leg_key in already_leg:
                _skip("already_scored")
                continue
            if not bars_fetched:
                bars = bars_provider(ticker, commit_ts)
                bars_fetched = True
            if not bars:
                _skip("no_klines")
                continue
            res = score_leg_counterfactual(u, bars)
            if not res["scored"]:
                _skip(res["skip"])
                continue
            sig = _thesis_signature(th, r.get("signature"))
            entry_zone = list(u["zone"]) if u["zone"] else None
            _record_row(ticker, u["direction"], entry_zone, u["stop"], u["tp"], res, sig,
                       commit_ts, close_ts=r.get("close_ts"),
                       notes=f"backfill counterfactual leg[{u['idx']}] of live call "
                             f"({r.get('outcome')})",
                       leg_idx=u["idx"], leg_kind=u["kind"])
            already_leg.add(leg_key)
            scored += 1
    return {"scored": scored, "skipped": skipped,
            "total_live_null": sum(1 for r in recs
                                   if (r.get("source") or "live") == "live"
                                   and r.get("pnl_r") is None)}


# ── SPEC-147 req 4: re-score existing counterfactual rows under the unified fill rule ──

def _rescore_thesis_geometry(row, geom):
    """The thesis dict to re-score a stored counterfactual row with: prefer the committed
    thesis still in the watchlist (carries entry_mode/time_stop_h a bare row can't), else
    fall back to the geometry the row itself already recorded (the thesis may since have
    been pruned/retired past what backfill's index still holds)."""
    key = ((row.get("ticker") or "").upper(), row.get("commit_ts"))
    th = geom.get(key)
    if th is not None:
        return th
    return {"direction": row.get("direction"), "entry_zone": row.get("entry_zone"),
           "stop": row.get("stop"), "tp": row.get("tp")}


def rescore(ledger_path=None, wl_path=None, bars_provider=None):
    """SPEC-147 req 4: re-score every `source: counterfactual` ledger row under the
    unified fill rule (thesis.fill_of) — a paper record scored under two different fill
    rules is worse than a short one with a footnote, especially now that counterfactual
    rows are the desk's primary evidence source (the 2026-08-19 data-gathering directive).

    Every re-scored row is stamped `rescored_ts` + `rescored_from` (the prior pnl_r) so
    `ledger.stats` can tell a rescored population from a stale one (req 5). `live`/
    `backfill`/`replay` rows are never touched. Rows this pass can't re-score (no cached
    klines, geometry now missing entirely) are left byte-identical and counted in
    `skipped` — never silently dropped.

    `bars_provider(ticker, commit_ts) -> [bars]|None` defaults to the offline cache-only
    provider — a re-score pass must never depend on a live venue read."""
    lp = Path(ledger_path) if ledger_path else ledger.LEDGER_PATH
    if not lp.exists():
        return {"rescored": 0, "skipped": {}, "reason": "no ledger"}
    if bars_provider is None:
        bars_provider = cache_bars_provider
    geom = _wl_geometry_index(wl_path)

    rescored = 0
    skipped = {}
    out_lines = []
    for ln in lp.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            out_lines.append(ln)          # a corrupt line never takes the ledger down
            continue
        if r.get("source") != SOURCE:
            out_lines.append(json.dumps(r))
            continue
        ticker = (r.get("ticker") or "").upper()
        commit_ts = r.get("commit_ts")
        th = _rescore_thesis_geometry(r, geom)
        bars = bars_provider(ticker, commit_ts)
        if not bars:
            skipped["no_klines"] = skipped.get("no_klines", 0) + 1
            out_lines.append(json.dumps(r))
            continue
        res = score_counterfactual(th, bars)
        if not res["scored"]:
            skipped[res["skip"]] = skipped.get(res["skip"], 0) + 1
            out_lines.append(json.dumps(r))
            continue
        r["rescored_from"] = r.get("pnl_r")
        r["pnl_r"] = res["pnl_r"]
        r["outcome"] = res["outcome"]
        r["counterfactual"] = res["counterfactual"]
        r["rescored_ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rescored += 1
        out_lines.append(json.dumps(r))
    if rescored:
        lp.write_text("\n".join(out_lines) + "\n")
    return {"rescored": rescored, "skipped": skipped}


# ── auto-wire: score on close, when a thesis that HAD geometry closes ─────────────
def score_on_close(ticker, th, live_signature=None, close_ts=None, notes=""):
    """Called by thesis close/retire so the live record self-populates going forward.
    Best-effort + offline: reads ONLY the local kline cache (no network on the close
    path — a dead venue read must never slow/break a close). Returns the recorded row
    or {scored:False, skip:...}. Caches are populated by `replay fetch` / the backfill
    CLI's network provider."""
    if thesis.parse({"thesis": th}).zone is None or th.get("stop") is None \
            or not (th.get("tp") or th.get("tps")):
        return {"scored": False, "skip": "no_geometry"}
    try:
        bars = cache_bars_provider((ticker or "").upper(), th.get("committed_ts"))
        if not bars:
            return {"scored": False, "skip": "no_klines"}
        res = score_counterfactual(th, bars)
        if not res["scored"]:
            return res
        sig = _thesis_signature(th, live_signature)
        row = _record_row_of((ticker or "").upper(), th, res, sig, th.get("committed_ts"),
                             close_ts=close_ts, notes=notes or "auto counterfactual on close")
        return {"scored": True, "record": row, "counterfactual": res["counterfactual"]}
    except Exception as ex:               # noqa: BLE001 — never let scoring break a close
        return {"scored": False, "skip": f"error: {ex}"}


# ── data layer (cache + network; not unit-tested) ────────────────────────────────
def _ts_ms(commit_ts):
    """Parse an ISO commit_ts → epoch ms; None on failure."""
    if not commit_ts:
        return None
    try:
        from datetime import datetime
        s = commit_ts.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return None


def _bars_after(blob, commit_ts):
    bars = replay._bars_from_cache(blob)
    cut = _ts_ms(commit_ts)
    if cut is not None:
        bars = [b for b in bars if b.get("ts") is not None and b["ts"] >= cut]
    return bars


def cache_bars_provider(ticker, commit_ts):
    """Forward bars from the local state/replay cache only (offline). None if absent."""
    s = (ticker or "").upper().replace("USDT", "")
    cache = REPLAY_DIR / f"{s}.json"
    if not cache.exists():
        return None
    try:
        return _bars_after(json.loads(cache.read_text()), commit_ts) or None
    except Exception:  # noqa: BLE001
        return None


def live_bars_provider(ticker, commit_ts):
    """Cache first, then fetch 1h klines from Binance (price-only; no Moralis)."""
    b = cache_bars_provider(ticker, commit_ts)
    if b:
        return b
    s = (ticker or "").upper().replace("USDT", "")
    try:
        import urllib.request
        start = _ts_ms(commit_ts)
        url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={s}USDT&interval=1h&limit=1000"
               + (f"&startTime={start}" if start else ""))
        req = urllib.request.Request(url, headers={"User-Agent": "counterfactual/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            kl = json.loads(r.read())
        if not isinstance(kl, list):
            return None
        return _bars_after({"klines": kl, "funding": [], "oi": []}, commit_ts) or None
    except Exception:  # noqa: BLE001
        return None


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="counterfactual — score the desk's committed calls (SPEC 81)")
    ap.add_argument("payload", help='JSON {action:"backfill"|"rescore"}')
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        req = json.loads(args.payload)
    except ValueError as e:
        print(json.dumps({"error": f"unparseable payload: {e}"})); sys.exit(2)
    action = req.get("action", "backfill")
    if action == "backfill":
        out = backfill()
    elif action == "rescore":
        # SPEC-147 req 4: re-score every source=counterfactual row under the unified rule
        out = rescore()
    else:
        out = {"error": f"unknown action {action!r} (backfill, rescore)"}
    print(json.dumps(out, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
