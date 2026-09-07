#!/usr/bin/env python3
"""thesis.py — schema-validated thesis lifecycle wired to ledger (SPEC 48).

The §0.5 state machine's core state — the thesis block — was the only desk state
mutated by hand-edited JSON (the orchestrator inlined Python against
config/watchlist.json twice in one day). This makes the lifecycle mechanical:

  commit   validate the §0.5 contract (direction, entry_zone, stop, tp, triggers[],
           invalidation{}, time_stop_h), stamp committed_ts/committed_by, write into
           tokens. A commit with NO NAMED INVALIDATION is rejected — "if you can't
           name the broken invalidation field, there is no BREAK".
  close    position concluded — tokens → retired (+retired_date/reason), thesis
  retire   status CLOSED/RETIRED, and the outcome row lands in the SPEC-40 ledger
           so the §9 base-rate gate accumulates without anyone remembering to log.

Writes are atomic (tmp + rename) and lock-guarded (flock) — safe against a concurrent
board_tick / second session touching the watchlist.

  python3 capabilities/thesis.py --op commit --ticker SKYAI --thesis '{"direction":"SHORT",...}' --json
  python3 capabilities/thesis.py --op close --ticker SKYAI --outcome tp1 --pnl-r 1.8 --reason "TP1 banked" --json
  python3 capabilities/thesis.py --op retire --ticker SKYAI --reason "zone blown without trigger" --json
"""
import argparse
import ast
import fcntl
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ledger  # noqa: E402  (SPEC 40 — every close/retire ends in one record call)

REPO = HERE.parent
WL_PATH = REPO / "config" / "watchlist.json"
LOCK_PATH = REPO / "state" / "watchlist.lock"

DIRECTIONS = ("SHORT", "LONG", "WATCH")
_ACTIVE = ("PENDING", "ARMED", "OPEN", "LIVE", "")   # statuses a new commit may not bulldoze

# SPEC 63: the commit-time signature vocabulary = the SPEC-59 setup keys + the catch-all.
SETUP_SIGNATURES = ("blowoff", "catb_top", "trap_long", "neg_funding_gate",
                    "stage45_short", "discretionary")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def _locked():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _read_wl():
    return json.loads(WL_PATH.read_text())


def _write_wl(wl):
    fd, tmp = tempfile.mkstemp(dir=str(WL_PATH.parent), prefix=".watchlist.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(wl, f, indent=1)
        os.replace(tmp, WL_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


_WATCH_LEVEL_SHAPE = "{price:number, dir:'above'|'below'}"
PAGE_LABEL_MAX = 40   # SPEC-161 req 2: the ONLY prose a pushed page may ever carry


def _clip_page_label(raw, caveats, where):
    """SPEC-161 req 2: page_label is validated HERE (commit-read time), never at page
    time — a >PAGE_LABEL_MAX label is truncated with a caveat logged, not silently cut
    inside ops/page_grammar.py. Non-string/empty -> None (no caveat; simply absent)."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        if raw not in (None, ""):
            caveats.append(f"{where} page_label dropped: {raw!r} is not a non-empty string")
        return None
    label = raw.strip()
    if len(label) > PAGE_LABEL_MAX:
        caveats.append(f"{where} page_label truncated to {PAGE_LABEL_MAX} chars: {label!r}")
        label = label[:PAGE_LABEL_MAX]
    return label


def _watch_level_element_error(w):
    """One watch_level element: dict with numeric price and dir in {above, below}.
    Returns an error string naming the offending element and the required shape, or None."""
    if not isinstance(w, dict):
        return f"watch_level element {w!r} is not {_WATCH_LEVEL_SHAPE}"
    price = w.get("price")
    d = w.get("dir")
    if not isinstance(price, (int, float)) or isinstance(price, bool) or d not in ("above", "below"):
        return f"watch_level element {w!r} is not {_WATCH_LEVEL_SHAPE}"
    return None


def validate_thesis(th):
    """The §0.5 contract. Returns (errors, warnings)."""
    errors, warnings = [], []
    if not isinstance(th, dict):
        return ["thesis must be an object"], []
    d = (th.get("direction") or "").upper()
    if d not in DIRECTIONS:
        errors.append(f"direction must be one of {DIRECTIONS}, got {th.get('direction')!r}")
    inv = th.get("invalidation")
    if not isinstance(inv, dict) or not any(v not in (None, "", []) for v in inv.values()):
        errors.append("invalidation{} with at least one NAMED field is required — "
                      "if you can't name the broken invalidation field, there is no BREAK (§0.5)")
    # SPEC-144: watch_level is read downstream by classify._normalize_watch_levels, which
    # SILENTLY DROPS anything that isn't {price, dir} — a bare float/string/malformed entry
    # passed validation here and then evaporated at read time (14/27 board rows, 2026-08-19).
    # The writer must reject what the reader can silently drop.
    wl = th.get("watch_level")
    if wl is not None:
        if not isinstance(wl, (list, tuple, dict)):
            errors.append(f"watch_level must be a dict or list of {_WATCH_LEVEL_SHAPE} dicts, "
                          f"got {wl!r}")
        else:
            items = wl if isinstance(wl, (list, tuple)) else [wl]
            for item in items:
                err = _watch_level_element_error(item)
                if err:
                    errors.append(err)
    z = th.get("entry_zone")
    if z is not None and not (isinstance(z, (list, tuple)) and len(z) == 2
                              and all(isinstance(x, (int, float)) for x in z)):
        errors.append("entry_zone must be [lo, hi] numbers (or null)")
    for key in ("stop", "time_stop_h"):
        if th.get(key) is not None and not isinstance(th[key], (int, float)):
            errors.append(f"{key} must be a number (or null)")
    tp = th.get("tp", th.get("tps"))
    if tp is not None and not (isinstance(tp, list) and all(isinstance(x, (int, float)) for x in tp)):
        errors.append("tp must be a list of numbers")
    if th.get("triggers") is not None and not isinstance(th["triggers"], list):
        errors.append("triggers must be a list")
    sig = th.get("signature")
    if sig is not None and sig not in SETUP_SIGNATURES:
        errors.append(f"signature must be one of {SETUP_SIGNATURES} (SPEC-59 setup keys + "
                      f"discretionary), got {sig!r}")
    # SPEC-149 req 3: a SHORT committed while classify's live operator_not_done veto is
    # true must carry a caveat (advisory only — the desk never silently blocks the user,
    # grill Q6). Free-text; only type-checked here, never required.
    ovc = th.get("operator_veto_caveat")
    if ovc is not None and not isinstance(ovc, str):
        errors.append(f"operator_veto_caveat must be a string, got {ovc!r}")
    if d in ("SHORT", "LONG"):
        if th.get("stop") is None:
            warnings.append("no stop committed — the price leg (SPEC 39) cannot watch a stop")
        if th.get("time_stop_h") is None:
            warnings.append("no time_stop_h — the time-stop BREAK can never fire")
    return errors, warnings


# ─────────────────────────────────────────────────────────────────────────────
# SPEC-146 — the ONE typed parse entry point for committed thesis geometry.
#
# Before this, geometry was re-derived independently in four places (classify's
# thesis_direction/entry_zone/committed_epoch/time_stop + _normalize_watch_levels +
# _collect_anchors, counterfactual's _zone, tape_watch's inline wl.get() reads) — no
# module owned the schema, so validate_thesis (write-time) and the actual read paths
# drifted apart (14/27 board rows silently lost their watch_level, 6/29 levels came out
# BACKWARDS because entry_zone was read unsorted in one place and sorted in another).
#
# `parse(tok) -> Thesis` is the single source of truth every reader now consumes.
# Zone is always returned SORTED (lo, hi) — the majority convention (eval_price_leg,
# retire_flag_for, the old counterfactual._zone all sorted; only classify's ZONE_BLOWN
# branch unpacked raw order, which this fixes rather than perpetuates). Malformed input
# a reader would otherwise silently drop appends a caveat instead (req 2) — a reader that
# discards committed intent must say so. validate_thesis (write-time) REJECTS the same
# shapes that parse() (read-time) reports as caveats — the writer rejects what the reader
# can no longer silently drop.
# ─────────────────────────────────────────────────────────────────────────────
from dataclasses import dataclass, field   # noqa: E402
from typing import List, Optional, Tuple   # noqa: E402


@dataclass
class Thesis:
    direction: Optional[str] = None
    zone: Optional[Tuple[float, float]] = None
    stop: Optional[float] = None
    tps: List[float] = field(default_factory=list)
    watch_levels: List[dict] = field(default_factory=list)
    anchors: List[float] = field(default_factory=list)
    committed_epoch: Optional[float] = None
    time_stop_h: Optional[float] = None
    signature: Optional[str] = None
    operator_veto_caveat: Optional[str] = None   # SPEC-149 req 3: free-text, set at commit
                                                  # time when operator_not_done was true
    caveats: List[str] = field(default_factory=list)


def _parse_watch_level_element(w, caveats):
    """One raw watch_level element -> {price,dir,note} or None (+ a caveat on drop)."""
    if not isinstance(w, dict):
        caveats.append(f"watch_level element dropped: {w!r} is not {_WATCH_LEVEL_SHAPE}")
        return None
    price = w.get("price")
    d = str(w.get("dir") or "").lower()
    if price is None or isinstance(price, bool) or d not in ("below", "above"):
        caveats.append(f"watch_level element dropped: {w!r} is not {_WATCH_LEVEL_SHAPE}")
        return None
    try:
        return {"price": float(price), "dir": d, "note": w.get("note") or "",
                "page_label": _clip_page_label(w.get("page_label"), caveats, "watch_level")}
    except (TypeError, ValueError):
        caveats.append(f"watch_level element dropped: {w!r} is not {_WATCH_LEVEL_SHAPE}")
        return None


def _parse_watch_levels(th, caveats):
    """Accepts a single dict or a list; absent/null -> []. Drops malformed elements,
    appending a caveat for each (SPEC-144's behavior, generalized here)."""
    wl = th.get("watch_level")
    if not wl:
        return []
    if isinstance(wl, dict):
        wl = [wl]
    if not isinstance(wl, (list, tuple)):
        caveats.append(f"watch_level dropped: {wl!r} is not a dict or list of "
                       f"{_WATCH_LEVEL_SHAPE} dicts")
        return []
    out = []
    for w in wl:
        parsed = _parse_watch_level_element(w, caveats)
        if parsed is not None:
            out.append(parsed)
    return out


def _parse_zone(th, caveats):
    """entry_zone -> sorted (lo, hi) floats, or None (+ caveat if present-but-malformed)."""
    z = th.get("entry_zone")
    if not z:
        return None
    if (isinstance(z, (list, tuple)) and len(z) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in z)):
        return (min(float(z[0]), float(z[1])), max(float(z[0]), float(z[1])))
    caveats.append(f"entry_zone dropped: {z!r} is not [lo, hi] numbers")
    return None


def _parse_number(th, key, caveats):
    """A scalar numeric field -> float, or None (+ caveat if present-but-not-a-number)."""
    v = th.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        caveats.append(f"{key} dropped: {v!r} is not a number")
        return None
    return float(v)


def _parse_time_stop_h(th, caveats):
    """time_stop_h -> float, or None. A falsy value (0/None/'') reads as None — matches
    the pre-SPEC-146 thesis_time_stop contract (a 0h time-stop is nonsensical, never
    committed on purpose)."""
    v = th.get("time_stop_h")
    if not v:
        return None
    return _parse_number(th, "time_stop_h", caveats)


def _parse_tps(th, caveats):
    """tp (or legacy tps) -> list of floats. Non-numeric elements are dropped with a
    caveat each; the rest of the list still parses (a single bad TP must not blind the
    board to the good ones)."""
    raw = th.get("tp", th.get("tps"))
    if not raw:
        return []
    if not isinstance(raw, (list, tuple)):
        caveats.append(f"tp dropped: {raw!r} is not a list")
        return []
    out = []
    for t in raw:
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            caveats.append(f"tp element dropped: {t!r} is not a number")
            continue
        out.append(float(t))
    return out


def _parse_committed_epoch(th, caveats):
    """committed_ts (ISO or bare date) -> epoch seconds, or None (+ caveat if present
    but unparseable)."""
    ts_str = th.get("committed_ts")
    if not ts_str:
        return None
    try:
        if "T" in str(ts_str):
            dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(str(ts_str), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        caveats.append(f"committed_ts unparseable: {ts_str!r}")
        return None


def _parse_direction(th, tok):
    """thesis.direction, else a best-effort read of the legacy memo (tok['state']) —
    the fallback every pre-thesis-migration row still needs."""
    if th.get("direction"):
        return str(th["direction"]).upper()
    from regime_flip import memo_direction
    return memo_direction(tok.get("state", "") or "")


def parse(tok):
    """The SPEC-146 typed parse entry point. `tok` is a watchlist token
    ({"ticker","state","thesis":{...}}) — a bare thesis dict also works (parse({"thesis": th})),
    the memo fallbacks just degrade to their absent-memo default. Pure; never raises."""
    tok = tok or {}
    th = tok.get("thesis") or {}
    caveats = []

    direction = _parse_direction(th, tok)
    raw_zone = _parse_zone(th, caveats)
    if raw_zone is None:
        from regime_flip import memo_zone
        zone = memo_zone(tok.get("state", "") or "")
    else:
        zone = raw_zone
    stop = _parse_number(th, "stop", caveats)
    time_stop_h = _parse_time_stop_h(th, caveats)
    tps = _parse_tps(th, caveats)
    watch_levels = _parse_watch_levels(th, caveats)
    committed_epoch = _parse_committed_epoch(th, caveats)

    # anchors reflect ONLY what was actually committed (SPEC-91's thesis_drift contract) —
    # never the memo-inferred zone fallback above, which exists for direction/display, not
    # as a price anchor for a thesis that never committed one.
    anchors = [w["price"] for w in watch_levels]
    if raw_zone:
        anchors.extend(raw_zone)
    if stop is not None:
        anchors.append(stop)
    anchors.extend(tps)

    ovc = th.get("operator_veto_caveat")
    if ovc is not None and not isinstance(ovc, str):
        caveats.append(f"operator_veto_caveat dropped: {ovc!r} is not a string")
        ovc = None

    return Thesis(direction=direction, zone=zone, stop=stop, tps=tps,
                 watch_levels=watch_levels, anchors=anchors,
                 committed_epoch=committed_epoch, time_stop_h=time_stop_h,
                 signature=th.get("signature"), operator_veto_caveat=ovc, caveats=caveats)


# ─────────────────────────────────────────────────────────────────────────────
# SPEC-147 — the ONE fill rule: whether a thesis filled, at what price, and how sure.
#
# Before this, classify.eval_price_leg decided WHETHER a thesis filled (range-intersection
# of the traded window against entry_zone) and counterfactual._infer_mode separately decided
# AT WHAT PRICE (breakdown → zone top, fade → zone bottom, commit-inside-zone → immediate,
# defaulting to "fade" — i.e. the zone BOTTOM — when there was no commit-price reference).
# R is computed from the fill price, so the board and the paper record could agree a thesis
# filled and still disagree about its R — and worse, that silent "fade" default always
# resolved to the zone's cheap-for-a-LONG / cheap-for-a-SHORT edge regardless of direction,
# flattering exactly the population (LONG fades, SHORT breakdowns) it should have been most
# conservative about.
#
# `fill_of` is the one place both callers now decide this. An explicit `entry_mode` on the
# thesis latches as `source="committed"` — the desk said where it filled, so it gets the
# mode's natural edge (breakdown → zone top, fade → zone bottom) at face value. Absent that,
# the mode is INFERRED from the first candle's open/close (the commit-time price) vs the
# zone, and — because the top/bottom pick is then a guess, not a fact — the entry prices at
# the WORSE edge for the position's direction (SHORT → zone low, LONG → zone high) rather
# than whichever edge the geometry would naturally suggest. `immediate` (commit price already
# inside the zone) has an exact reference price, so there is no edge to hedge.
# ─────────────────────────────────────────────────────────────────────────────

def _fill_commit_price(candles):
    """The first candle's open (else close) — the commit-time price reference used to
    infer breakdown/fade/immediate. None if candles are absent or carry neither field."""
    if not candles:
        return None
    b = candles[0]
    for key in ("open", "close"):
        v = b.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _fill_worse_edge(direction, zlo, zhi):
    """The less favorable zone edge for `direction` — SHORT sells for less at zlo,
    LONG pays more at zhi. The conservative default whenever the fill is inferred."""
    return zlo if (direction or "").upper() == "SHORT" else zhi


def fill_of(th, direction, window):
    """The SPEC-147 fill rule. `window` is {"high","low","candles"} — the same shape
    classify's `price_window_range` and a counterfactual bars slice both already produce
    (candles optional: a fallback aggregate window with no per-candle detail can only
    report `filled`, never a sequenced `entry_idx`). `th` is the raw thesis dict.

    Returns None when there is no entry_zone to fill against (nothing to decide) or no
    window. Otherwise {filled, mode, entry_px, entry_idx, source}:
      mode      "breakdown" | "fade" | "immediate"
      source    "committed" (explicit th.entry_mode) | "inferred" (derived from geometry)
      entry_px  the reference fill price for this mode/edge — populated even when
                `filled` is False (a counterfactual score needs "what WOULD it have
                filled at" for its never-filled MFE/MAE read; classify's price leg
                simply ignores it there)
      entry_idx None unless `filled` is True (nothing to sequence when it never printed)"""
    th = th or {}
    window = window or {}
    z = parse({"thesis": th}).zone
    if not z:
        return None
    candles = window.get("candles")
    if not candles and window.get("high") is None and window.get("low") is None:
        return None   # nothing traded — no window to fill against
    zlo, zhi = z

    explicit = str(th.get("entry_mode") or "").lower() or None
    if explicit in ("breakdown", "break", "dip"):
        mode, entry_px, source = "breakdown", zhi, "committed"
    elif explicit in ("fade", "breakout", "rally"):
        mode, entry_px, source = "fade", zlo, "committed"
    else:
        source = "inferred"
        commit_px = _fill_commit_price(candles)
        if commit_px is not None and zlo <= commit_px <= zhi:
            mode, entry_px = "immediate", commit_px
        elif commit_px is not None and zhi < commit_px:
            mode, entry_px = "breakdown", _fill_worse_edge(direction, zlo, zhi)
        else:
            # zone sits above spot (fade), or there's no commit reference at all — either
            # way the edge is a guess, so charge the worse one rather than defaulting to
            # a single edge regardless of direction (the pre-147 bug).
            mode, entry_px = "fade", _fill_worse_edge(direction, zlo, zhi)

    # filled + entry_idx: causal range-intersection (classify's original rule) — the FIRST
    # candle whose traded range overlaps the zone; degrades to the aggregate window high/low
    # when no per-candle detail is available (no sequencing possible, `entry_idx` stays None).
    if mode == "immediate":
        filled, entry_idx = True, 0
    elif candles:
        entry_idx = next((i for i, k in enumerate(candles)
                          if k["high"] >= zlo and k["low"] <= zhi), None)
        filled = entry_idx is not None
    else:
        high, low = window.get("high"), window.get("low")
        filled = high is not None and low is not None and high >= zlo and low <= zhi
        entry_idx = None

    return {"filled": filled, "mode": mode, "entry_px": entry_px,
           "entry_idx": entry_idx, "source": source}


def _load_tokens(wl_path=None):
    path = Path(wl_path) if wl_path else WL_PATH
    try:
        wl = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return wl.get("tokens", wl) if isinstance(wl, dict) else wl


def check_board(tokens=None, wl_path=None):
    """SPEC-146 req 3 — the standalone geometry-invariant sweep. `build_thesis` validates
    on commit, but the orchestrator's actual write path is often a direct json.dump to
    watchlist.json (never touching build_thesis/validate_thesis) — a hand-written row can
    carry the exact malformed shapes validate_thesis would have rejected and stay silently
    broken forever (SPEC-144: 14/27 board rows, 2026-08-19). This runs standalone at
    session-open and on the board tick so that CANNOT happen quietly: hand-writing stays
    legal (req 3), staying wrong does not. Generalizes SPEC-144's watch_level-only
    check_watch_level_invariant to every droppable geometry field parse() reports.

    Returns [{ticker, caveats}] for every token whose thesis parse dropped >=1 field."""
    if tokens is None:
        tokens = _load_tokens(wl_path)
    violations = []
    for tok in tokens or []:
        if not tok.get("thesis"):
            continue
        p = parse(tok)
        if p.caveats:
            violations.append({"ticker": tok.get("ticker", "?"), "caveats": list(p.caveats)})
    return violations


def _prior_24h_range(ticker):
    """SPEC 54: prior-24h traded range at commit time (binance -> bybit). None on failure."""
    from classify import _ticker_hl
    sym = f"{ticker}USDT"
    return _ticker_hl(sym, "binance") or _ticker_hl(sym, "bybit")


def _commit_geometry_warning(ticker, th):
    """SPEC 54: stop/TP/entry-zone INSIDE the prior-24h traded range = levels parked in
    today's churn — the board would instantly flag them off stale prints (and a stop in
    the churn is donated). Caught at COMMIT time so the Designer fixes geometry before
    the board ever fires. Never blocks the commit."""
    try:
        hl = _prior_24h_range(ticker)
    except Exception:  # noqa: BLE001 — a dead venue read must not block a commit
        return None
    if not hl:
        return None
    hi24, lo24 = hl
    levels = [("stop", th.get("stop"))]
    levels += [(f"tp{i+1}", t) for i, t in enumerate(th.get("tp") or th.get("tps") or [])]
    z = th.get("entry_zone") or []
    levels += [(f"zone_{w}", v) for w, v in zip(("lo", "hi"), z)]
    inside = [f"{name} {v:g}" for name, v in levels
              if isinstance(v, (int, float)) and lo24 <= v <= hi24]
    if not inside:
        return None
    return (f"{', '.join(inside)} inside prior-24h churn (high {hi24:g} / low {lo24:g}) — "
            "stale prints will read as instant fires; check the geometry")


def _propose_stop_geometry(ticker, direction, entry):
    """SPEC 61 seam: the full-history true-wick stop proposal (size.propose_stop_live).
    Monkeypatched offline in tests; degrades to None on any read failure."""
    try:
        from size import propose_stop_live
        return propose_stop_live(ticker, direction, entry)
    except Exception:  # noqa: BLE001 — a dead venue read must never block a commit
        return None


def _commit_stop_geometry_warning(ticker, th):
    """SPEC 61: at commit, check the committed stop sits BEYOND the true (full-history) wick,
    not inside the trailing range / a partial-window wick (the ESPORTS −1R lesson). Extends
    SPEC 54's churn check with the full-history wick. Never blocks the commit."""
    direction = (th.get("direction") or "").upper()
    if direction not in ("SHORT", "LONG"):
        return None
    stop = th.get("stop")
    if stop is None:
        return None
    z = th.get("entry_zone") or []
    # use the worse zone edge as the entry reference (short = top, long = bottom)
    entry = (max(z) if direction == "SHORT" else min(z)) if z else stop
    geo = _propose_stop_geometry(ticker, direction, entry)
    if not geo or geo.get("stop_proposed") is None:
        return None
    proposed = geo["stop_proposed"]
    inside = (stop <= proposed) if direction == "SHORT" else (stop >= proposed)
    if not inside:
        return None
    return (f"committed stop {stop:g} is INSIDE the proposed true-wick stop {proposed:g} "
            f"(beyond the {geo.get('cleared_source')} wick {geo.get('cleared_wick')}) — it can be "
            f"out-wicked like ESPORTS leg-9 (SPEC 61); move the stop beyond the full-history wick")


def _thesis_entry(th):
    """The reference entry for R math: an explicit `entry`, else the entry_zone midpoint."""
    if isinstance(th.get("entry"), (int, float)):
        return float(th["entry"])
    z = th.get("entry_zone")
    if isinstance(z, (list, tuple)) and len(z) == 2 and all(isinstance(x, (int, float)) for x in z):
        return (z[0] + z[1]) / 2
    return None


def _compute_pnl_r(direction, entry, stop, exit_px):
    """Realized R = profit / risk. SHORT profits as price falls; LONG as it rises."""
    if entry is None or stop is None or exit_px is None:
        return None
    risk = abs(stop - entry)
    if risk <= 0:
        return None
    r = (entry - exit_px) / risk if direction == "SHORT" else (exit_px - entry) / risk
    return round(r, 3)


def _resolve_pnl_r(th, outcome, pnl_r, exit_px):
    """SPEC 63: compute pnl_r mechanically. Priority: an explicit exit_px (compute) →
    an explicit pnl_r (authoritative, e.g. a hand-booked R) → a tp1/tp2 outcome estimated
    from the committed TP level (flagged). Returns (pnl_r, estimated)."""
    direction = (th.get("direction") or "?").upper()
    entry, stop = _thesis_entry(th), th.get("stop")
    if exit_px is not None:
        return _compute_pnl_r(direction, entry, stop, float(exit_px)), False
    if pnl_r is not None:
        return float(pnl_r), False
    tps = th.get("tp") or th.get("tps") or []
    idx = {"tp1": 0, "tp2": 1}.get(outcome)
    if idx is not None and len(tps) > idx:
        return _compute_pnl_r(direction, entry, stop, float(tps[idx])), True
    return None, False


def _resolve_signature(th, signature, confirm_signature, reason):
    """SPEC 63: choose the row signature WITHOUT silently guessing.
    committed signature → explicit arg → (inferred-from-notes ONLY if confirmed) → else the
    honest `discretionary` default, always returning the inference as a suggestion."""
    direction = th.get("direction")          # SPEC-90: never bucket a close on the wrong side
    committed = th.get("signature") or th.get("setup")
    inferred = ledger.canon_signature((reason or "") + " " + (th.get("setup") or ""), direction)
    if signature:
        return ledger.canon_signature(signature, direction), "explicit", inferred
    if committed:
        return ledger.canon_signature(committed, direction), "committed", inferred
    if confirm_signature:
        return inferred, "inferred-confirmed", inferred
    return "discretionary", "defaulted", inferred


def _ledger_row(ticker, th, outcome, pnl_r, reason, close_ts, signature):
    row = {
        "ticker": ticker,
        "direction": (th.get("direction") or "?").upper(),
        "signature": signature,
        "outcome": outcome,
        "pnl_r": pnl_r,
        "banked": th.get("banked") or [],
        "commit_ts": th.get("committed_ts"),
        "close_ts": close_ts,
        "notes": reason or "",
    }
    return ledger.record(row)["record"]


def _score_counterfactual_on_close(ticker, th, signature, close_ts, reason):
    """SPEC 81 seam: score the just-closed committed call's counterfactual outcome.
    Offline/cache-only + fully guarded — a dead read must never break a close. Returns
    a small status dict (scored / skip)."""
    try:
        from counterfactual import score_on_close
        return score_on_close(ticker, th, live_signature=signature, close_ts=close_ts,
                              notes=f"auto counterfactual on close: {reason or ''}")
    except Exception as ex:  # noqa: BLE001 — scoring is never load-bearing on a close
        return {"scored": False, "skip": f"error: {ex}"}


def build_thesis(op, ticker, thesis=None, reason=None, outcome=None, pnl_r=None,
                 exit_px=None, signature=None, confirm_signature=False):
    ticker = (ticker or "").upper()
    if not ticker:
        return {"ok": False, "error": "ticker is required"}

    if op == "commit":
        errors, warnings = validate_thesis(thesis)
        if errors:
            return {"ok": False, "error": f"{ticker}: " + "; ".join(errors)}
        with _locked():
            wl = _read_wl()
            tok = next((t for t in wl.get("tokens", []) if t.get("ticker", "").upper() == ticker), None)
            if tok and tok.get("thesis") and (tok["thesis"].get("status") or "").upper() in _ACTIVE \
                    and tok["thesis"].get("committed_ts"):
                return {"ok": False,
                        "error": f"{ticker} already has an active thesis "
                                 f"(status={tok['thesis'].get('status') or 'PENDING'}) — "
                                 "close or retire it first (§0.5: commit once)"}
            th = dict(thesis)
            th["direction"] = th["direction"].upper()
            th.setdefault("status", "PENDING")
            th["committed_ts"] = _now_iso()
            th["committed_by"] = "thesis-capability"
            if tok is None:
                tok = {"ticker": ticker, "state": ""}
                wl.setdefault("tokens", []).append(tok)
            tok["thesis"] = th
            _write_wl(wl)
        warning = _commit_geometry_warning(ticker, th)   # SPEC 54: catch churn-parked levels NOW
        if warning:
            warnings.append(warning)
        geometry_warning = _commit_stop_geometry_warning(ticker, th)  # SPEC 61: true-wick stop
        if geometry_warning:
            warnings.append(geometry_warning)
        return {"ok": True, "ticker": ticker, "op": "commit", "thesis": th,
                "warnings": warnings, "commit_warning": warning,
                "geometry_warning": geometry_warning}

    if op in ("close", "retire"):
        outcome = outcome or ("retired_unfilled" if op == "retire" else None)
        if not outcome:
            return {"ok": False, "error": "close needs an explicit --outcome "
                                          f"(one of {ledger.OUTCOMES})"}
        with _locked():
            wl = _read_wl()
            tokens = wl.get("tokens", [])
            tok = next((t for t in tokens if t.get("ticker", "").upper() == ticker), None)
            if tok is None:
                return {"ok": False, "error": f"{ticker} not in tokens (already retired?)"}
            th = tok.get("thesis")
            if not th:
                return {"ok": False, "error": f"{ticker} has no committed thesis to {op}"}
            close_ts = _now_iso()
            sig, sig_source, suggested = _resolve_signature(th, signature, confirm_signature, reason)
            resolved_pnl_r, estimated = _resolve_pnl_r(th, outcome, pnl_r, exit_px)
            try:
                row = _ledger_row(ticker, th, outcome, resolved_pnl_r, reason, close_ts, sig)
            except ValueError as ex:
                return {"ok": False, "error": f"ledger rejected the outcome row: {ex}"}
            th["status"] = "CLOSED" if op == "close" else "RETIRED"
            tokens.remove(tok)
            tok["retired_date"] = close_ts[:10]
            tok["retired_reason"] = reason or outcome
            wl.setdefault("retired", []).append(tok)
            _write_wl(wl)
        # SPEC 81: a closed call that HAD machine-watchable geometry self-populates the
        # live record with its counterfactual outcome (offline/cache-only, best-effort —
        # never blocks the close). discretionary/WATCH null-zone theses score no_geometry.
        cf = _score_counterfactual_on_close(ticker, th, sig, close_ts, reason)
        return {"ok": True, "ticker": ticker, "op": op, "ledger_row": row,
                "pnl_r": resolved_pnl_r, "pnl_r_estimated": estimated,
                "signature": sig, "signature_source": sig_source,
                "suggested_signature": suggested, "counterfactual": cf}

    return {"ok": False, "error": f"unknown op {op!r} (commit|close|retire)"}


def _parse_blob(s):
    """CLI thesis blob: JSON first, python-literal fallback (a str(dict) that slipped through)."""
    if s is None:
        return None
    try:
        return json.loads(s)
    except ValueError:
        return ast.literal_eval(s)


def main():
    ap = argparse.ArgumentParser(description="thesis — §0.5 lifecycle, ledger-wired (SPEC 48)")
    ap.add_argument("--op", required=True, choices=["commit", "close", "retire"])
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--thesis", default=None, help="JSON thesis block (commit)")
    ap.add_argument("--reason", default=None)
    ap.add_argument("--outcome", default=None, help=f"one of {ledger.OUTCOMES}")
    ap.add_argument("--pnl-r", default=None, type=float)
    ap.add_argument("--exit-px", default=None, type=float,
                    help="exit price → mechanical pnl_r (SPEC 63)")
    ap.add_argument("--signature", default=None,
                    help=f"setup signature at commit/close — one of {SETUP_SIGNATURES}")
    ap.add_argument("--confirm-signature", action="store_true",
                    help="on close with no committed signature, write the inferred suggestion")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        thesis = _parse_blob(args.thesis)
    except (ValueError, SyntaxError) as ex:
        print(json.dumps({"ok": False, "error": f"unparseable --thesis: {ex}"})); return
    # --signature at commit folds into the thesis block; at close it's the explicit override.
    if args.op == "commit" and args.signature and isinstance(thesis, dict):
        thesis.setdefault("signature", args.signature)
    out = build_thesis(args.op, args.ticker, thesis=thesis, reason=args.reason,
                       outcome=args.outcome, pnl_r=args.pnl_r, exit_px=args.exit_px,
                       signature=(args.signature if args.op != "commit" else None),
                       confirm_signature=args.confirm_signature)
    print(json.dumps(out, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
