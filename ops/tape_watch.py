#!/usr/bin/env python3
"""tape_watch.py — SPEC-106: page on tape-pattern deterioration for WATCH names.

board_tick (SPEC 46) only diffs {verdict, stop_breached, tps_printed} — a committed WATCH
thesis with wide levels (BIRB: 0.070/0.095) is silent while the tape visibly deteriorates
INSIDE that range. BIRB moved -12% intraday off its pump high with operator_profit_take +
chain_squeeze + downtrend_slam printing on the SPEC-31 tape, and the desk emitted zero
alerts because nothing monitors the space between the committed levels.

This module runs the SPEC-31 `tape` pattern detectors + a funding-decay check over every
WATCH-thesis name on the board-tick cadence and pages (via the SPEC-66 page_gate, throttled)
when the read says the setup is deteriorating. READ-ONLY PAGING — it never writes a verdict
or touches the state machine (classify.py still owns TRIGGERS/BREAKS); this is awareness only.

Per-name cost per tick (skipped entirely for DUST-gated names, before any tape fetch):
  1x regime_flip.live_perp   — cross-venue price/funding/vol_m read (also the DUST gate input)
  1x tape.build_tape         — 1x 1m-klines pull + 1x 5m-OI-hist pull (+ a forceOrders attempt
                                that degrades on public REST) over `window_min` (default 60m)
So ~3 HTTP calls per non-dust WATCH name per tick — the same shape `brief`/`tape` already pay
per on-demand call, just run standing instead of on request.

  python3 ops/tape_watch.py           # one tick against the real watchlist, prints JSON
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "capabilities"))
sys.path.insert(0, str(REPO / "ops"))
import inbox                      # noqa: E402  (SPEC 45 — the consumer paged events flow into)
import tape as tape_mod           # noqa: E402  (SPEC 31 — the pattern detectors reused here)
import regime_flip                # noqa: E402  (cross-venue price/funding + the §7 DUST gate)
import page_gate                  # noqa: E402  (SPEC 66 — throttles the page, not the read)
import thesis as TH               # noqa: E402  (SPEC-146: the ONE typed geometry parser +
                                   # WL_PATH owner — aliased, this module's local vars are
                                   # named `thesis` throughout)

WL_PATH = TH.WL_PATH   # SPEC-146 req 4: thesis.py owns the path

DRIFT_PCT_DEFAULT = 10.0   # spec item 2: intraday move from commit w/o a level breach
FUNDING_DECAY_FRAC = 0.5   # "decaying past half its committed magnitude" (BIRB farm-closing tell)
TAPE_WINDOW_MIN = 60


def _now_iso(now):
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_watch_entries(wl_path=None):
    """WATCH-direction thesis entries from the watchlist. Pure file read; [] on anything
    missing/unparseable (mirrors onboard.py's tolerance of a bare-list or {"tokens": []} shape)."""
    path = Path(wl_path) if wl_path is not None else WL_PATH
    try:
        wl = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return []
    tokens = wl.get("tokens", wl) if isinstance(wl, dict) else wl
    out = []
    for tok in tokens or []:
        thesis = tok.get("thesis") or {}
        if thesis.get("direction") != "WATCH":
            continue
        out.append({"ticker": tok.get("ticker"), "thesis": thesis, "regime": tok.get("regime") or {}})
    return out


def _dust(vol_m):
    """§7 liquidity gate: <$10M/24h vol = auto-PASS, skip before any tape fetch."""
    return vol_m is not None and vol_m < regime_flip.LIQ_GATE_M


def _watch_level_breached(watch_levels, price):
    for wl in watch_levels or []:
        p, d = wl.get("price"), wl.get("dir")
        if p is None or d is None or price is None:
            continue
        if d == "above" and price >= p:
            return True
        if d == "below" and price <= p:
            return True
    return False


def _nearest_retire_level(watch_levels, price, direction):
    cands = [wl for wl in (watch_levels or []) if wl.get("dir") == direction and wl.get("price") is not None]
    if not cands or price is None:
        return None
    nearest = min(cands, key=lambda wl: abs(wl["price"] - price))
    return f"retire-level {nearest['price']:g} {direction}"


def _funding_decay(regime, funding_pi):
    """Deep-neg decaying past half its committed magnitude — even before the +/- flip
    (spec item 2: 'the BIRB farm-closing tell')."""
    committed = regime.get("funding_pi")
    if regime.get("funding_sign") != "neg" or committed is None or funding_pi is None or committed >= 0:
        return None
    half = committed * FUNDING_DECAY_FRAC
    if funding_pi > half:
        return {"committed": committed, "current": funding_pi}
    return None


def _pattern_reads(tape_result):
    """Compose pattern-derived read fragments (spec item 2: profit_take/capitulation tail,
    chain_squeeze cluster) + the severity they earn. HIGH only for the distribution-shaped
    tells (operator taking profit / a chain-squeeze cluster); MED otherwise.

    SPEC-141: phrases are human verdict language, never the raw detector-type token — a
    phone page reading "operator_profit_take" is engineer-speak; "operator taking profit"
    is the same read, legible on a lock screen. The inbox event (same string) inherits the
    fix for free — no separate page-vs-record translation needed."""
    bits, severity = [], "MED"
    if not tape_result or not tape_result.get("available"):
        return bits, severity
    patterns = tape_result.get("patterns") or []
    for p in patterns:
        if p.get("type") == "tail_of_liquidation":
            ctx = (p.get("detail") or {}).get("context")
            if ctx == "operator_profit_take":
                bits.append("operator taking profit")
                severity = "HIGH"
            elif ctx == "retail_capitulation":
                bits.append("retail capitulating")
        elif p.get("type") == "downtrend_slam":
            d = p.get("detail") or {}
            if d.get("shorts_opening"):
                bits.append("shorts still opening")
    squeeze_n = sum(1 for p in patterns if p.get("type") == "chain_squeeze")
    if squeeze_n >= 2:
        bits.append(f"chain-squeeze cluster x{squeeze_n}")
        severity = "HIGH"
    return bits, severity


def check_tape_deterioration(ticker, thesis, regime, *, price=None, funding_pi=None,
                              tape_result=None, drift_pct=DRIFT_PCT_DEFAULT):
    """Pure core (spec items 2/3): compose the page-worthy read for one WATCH name this tick.
    Returns (severity, msg) or None on a quiet tick. Paging/awareness only — no verdict write,
    no state-machine input (spec item 4)."""
    if thesis.get("direction") != "WATCH":
        return None

    # SPEC-146: was an inline `thesis.get("watch_level") or []` read (raises on a bare-
    # float element via `wl.get(...)` downstream) — now the one typed parser, malformed
    # elements dropped rather than crashing the tick.
    watch_levels = TH.parse({"thesis": thesis}).watch_levels
    ref_price = regime.get("price")
    breached = price is not None and _watch_level_breached(watch_levels, price)

    reasons = []
    drift = None
    if price is not None and ref_price:
        drift = (price - ref_price) / ref_price * 100.0
        if not breached and abs(drift) >= drift_pct:
            reasons.append(f"{drift:+.0f}% from commit ({ref_price:g}->{price:g})")

    pattern_bits, pat_sev = _pattern_reads(tape_result)
    if pattern_bits:
        reasons.append(" + ".join(pattern_bits))

    fd = _funding_decay(regime, funding_pi)
    if fd:
        reasons.append(f"funding {fd['current']:+.2f} (was {fd['committed']:+.2f}) — farm decaying")

    if not reasons:
        return None

    severity = pat_sev if pattern_bits else "MED"
    msg = f"{ticker} tape: " + ", ".join(reasons)
    if drift is not None and not breached and abs(drift) >= drift_pct:
        retire = _nearest_retire_level(watch_levels, price, "below" if drift < 0 else "above")
        if retire:
            msg += f" — {retire}"
    return (severity, msg)


def run_tick(wl_path=None, live_fn=None, tape_fn=None, now=None, page_state_path=None,
             notify_fn=None, drift_pct=DRIFT_PCT_DEFAULT, window_min=TAPE_WINDOW_MIN):
    """Board-tick entry point: check every WATCH name, page (throttled) on a deteriorating
    read. Never raises — a per-name fetch failure is skipped, not fatal, mirroring board_tick's
    own classify-failure isolation. Returns {checked, events, paged, detail}."""
    live_fn = live_fn or regime_flip.live_perp
    tape_fn = tape_fn or tape_mod.build_tape
    now = now or datetime.now(timezone.utc)
    page_state_path = page_state_path or page_gate.COOLDOWN_PATH

    checked, events = 0, []
    for ent in load_watch_entries(wl_path):
        ticker, thesis, regime = ent["ticker"], ent["thesis"], ent["regime"]
        try:
            live = live_fn(ticker) or {}
        except Exception as ex:  # noqa: BLE001 — a monitor that silently skips a name is
            # indistinguishable from one that found nothing (CLAUDE.md §3, SPEC-144)
            print(f"tape_watch: {ticker} skipped — live_fn error: {ex}", file=sys.stderr)
            continue
        if _dust(live.get("vol_m")):
            continue      # DUST-gated — skip before any tape fetch (the expensive call)
        checked += 1
        try:
            # SPEC-146: watch_levels normalized via thesis.parse — a malformed element
            # (e.g. a bare float) is dropped (+ caveat), never raises here anymore.
            watch_levels = TH.parse({"thesis": thesis}).watch_levels
            below = [wl["price"] for wl in watch_levels if wl["dir"] == "below"]
            level = min(below) if below else None
            tape_result = tape_fn(ticker, window_min=window_min, level=level)
        except Exception as ex:  # noqa: BLE001 — a per-name tape fetch failure must not
            # abort the whole tick (mirrors board_tick's own classify-failure isolation).
            print(f"tape_watch: {ticker} skipped — tape/watch_level error: {ex}", file=sys.stderr)
            continue

        result = check_tape_deterioration(
            ticker, thesis, regime, price=live.get("price"), funding_pi=live.get("funding_pi"),
            tape_result=tape_result, drift_pct=drift_pct)
        if result is None:
            continue
        severity, msg = result

        should_page = page_gate.should_page(
            {"label": ticker, "dest_kind": severity.lower()}, now=now, state_path=page_state_path)
        if not should_page:
            events.append({"ticker": ticker, "severity": severity, "msg": msg, "paged": False})
            continue

        inbox.append_event(ts=_now_iso(now), ticker=ticker, source="tape_watch",
                           severity=severity, msg=msg)
        if notify_fn:
            try:
                notify_fn(f"crime-desk {ticker}", msg)
            except Exception:  # noqa: BLE001 — a dead notifier must not kill the tick
                pass
        events.append({"ticker": ticker, "severity": severity, "msg": msg, "paged": True})

    return {"checked": checked, "events": len(events), "paged": sum(1 for e in events if e["paged"]),
            "detail": events}


if __name__ == "__main__":
    print(json.dumps(run_tick()))
