#!/usr/bin/env python3
"""oic_watch.py — SPEC-180 req 6 (G7): OIC_FLIP / ROLE_DRIFT alerting.

Two new page_grammar event types, routed via the EXISTING `ops/notify.sh` +
`page_gate` — no new alert pathway. Every event appends to `inbox` regardless of
route (a desk/annotate-only event is still on the record, just not pushed).

  OIC_FLIP  — the oi_construction verdict crossed the arb/directional line:
              DIRECTIONAL(-like) -> ARB_DOMINATED = fuel evaporating;
              ARB_DOMINATED -> DIRECTIONAL(-like) = fuel arriving.
  ROLE_DRIFT — venue_roles drifted between two consecutive snapshots:
              the FLOW_CONFIRMED exit venue changed, or the anchor index's
              mark-constituent weights shifted materially.

Debounce (state/oic_watch_state.json, keyed by ticker): a flip only fires after
persisting 2 CONSECUTIVE computations — `classify_flip` tracks a `pending`
verdict+count separately from the last-CONFIRMED verdict, so one noisy tick can't
fire an alert. **UNKNOWN never participates**: a tick reading UNKNOWN is a pure
no-op on the state (X->UNKNOWN->X = nothing; X->UNKNOWN->Y still needs Y to persist
2 ticks before firing, same as any other transition). Staleness (`gating_ok:false`)
is treated as UNKNOWN here too — it flips `gating_ok` silently, never pages (req 6).

Routing (`route_event`) — the SPEC-180 req 6 table, `has_live_thesis` = an open
position OR an ARMED watch leg on that ticker:

| Event                          | live thesis | no live thesis |
|---------------------------------|-------------|-----------------|
| DIRECTIONAL->ARB (evaporating)  | phone       | desk            |
| ARB->DIRECTIONAL (arriving)     | desk        | annotate        |
| exit FLOW_CONFIRMED venue flip  | phone       | desk            |
| mark-constituent drift          | desk        | annotate        |

Cadence home: the 15m board tick recomputes the cheap keyless layer for LIVE-thesis
names only (`run_tick`'s default universe); other names refresh on the
discovery-tick cadence (a caller passes a wider `universe` there).
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))
sys.path.insert(0, str(ROOT / "ops"))

import inbox                      # noqa: E402  (SPEC 45 — every event lands here regardless of route)
import page_gate                  # noqa: E402  (SPEC 66 — throttles the page, not the read)
import page_grammar as PG         # noqa: E402  (SPEC-141/180 — the render layer)
import oi_construction as OC      # noqa: E402

STATE_PATH = ROOT / "state" / "oic_watch_state.json"
# SPEC-191 #4: config/venue_roles.json is the CURATED file (read-only for every code
# path) — this tick's own live drift-history moved to untracked state/, like every
# other runtime-regenerated file.
VENUE_ROLES_PATH = ROOT / "state" / "oic_watch_venue_roles.json"

_DIRECTIONAL_LIKE = {"DIRECTIONAL", "MIXED"}
_ARB_LIKE = {"ARB_DOMINATED"}

# route_event's table (req 6)
_ROUTE_TABLE = {
    ("oic_flip", "fuel_evaporating"): {True: "phone", False: "desk"},
    ("oic_flip", "fuel_arriving"): {True: "desk", False: "annotate"},
    ("role_drift", "exit_flow"): {True: "phone", False: "desk"},
    ("role_drift", "mark_constituent"): {True: "desk", False: "annotate"},
}


def _now_iso(now=None):
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=1))


def route_event(event_kind, direction, has_live_thesis):
    """event_kind ∈ {'oic_flip','role_drift'}; direction distinguishes the table's
    four rows. Returns 'phone'|'desk'|'annotate'."""
    return _ROUTE_TABLE[(event_kind, direction)][bool(has_live_thesis)]


def classify_flip(state, verdict):
    """Pure state-machine step (SPEC-180 req 6 debounce). `state` is
    `{"last": str|None, "pending": str|None, "pending_count": int}` (any missing key
    defaults sanely). Returns `(fired_direction|None, new_state, prior_last)` —
    `fired_direction` is `"fuel_evaporating"`/`"fuel_arriving"`/None; `prior_last` is
    the committed verdict BEFORE this call (only meaningful when a flip fired — lets
    the caller render an accurate "X → Y" line). `verdict=None`/`"UNKNOWN"` is a
    total no-op: returns `(None, state, state.get("last"))` UNCHANGED (not even
    copied) — a stale or genuinely-unknown read must never touch the pending count."""
    if verdict in (None, "UNKNOWN"):
        return None, state, state.get("last")
    st = dict(last=state.get("last"), pending=state.get("pending"),
             pending_count=int(state.get("pending_count") or 0))
    prior_last = st["last"]
    if verdict == st["last"]:
        st["pending"], st["pending_count"] = None, 0
        return None, st, prior_last
    if verdict == st["pending"]:
        st["pending_count"] += 1
    else:
        st["pending"], st["pending_count"] = verdict, 1
    if st["pending_count"] < 2:
        return None, st, prior_last
    st["last"], st["pending"], st["pending_count"] = verdict, None, 0
    direction = None
    if prior_last in _DIRECTIONAL_LIKE and verdict in _ARB_LIKE:
        direction = "fuel_evaporating"
    elif prior_last in _ARB_LIKE and verdict in _DIRECTIONAL_LIKE:
        direction = "fuel_arriving"
    return direction, st, prior_last


def detect_exit_flow_drift(prev_roles, cur_roles):
    """A change in the FLOW_CONFIRMED exit venue between two consecutive
    `venue_roles` snapshots. None unless BOTH snapshots graded FLOW_CONFIRMED and
    the venue actually changed (a DEPTH_INFERRED/UNKNOWN leg on either side is not a
    drift — that's just the normal absence of a confirmed flow)."""
    pe = (prev_roles or {}).get("exit") or {}
    ce = (cur_roles or {}).get("exit") or {}
    if (pe.get("grade") == "FLOW_CONFIRMED" and ce.get("grade") == "FLOW_CONFIRMED"
            and pe.get("venue") and ce.get("venue") and pe["venue"] != ce["venue"]):
        return pe["venue"], ce["venue"]
    return None


def detect_mark_constituent_drift(prev_roles, cur_roles, threshold_pct=5.0):
    """A material (>= threshold_pct) change in any single venue's anchor-index
    weight between two consecutive `mark_engine` snapshots. None when either
    snapshot has no weights (unpublished) or nothing moved enough."""
    pw = ((prev_roles or {}).get("mark_engine") or {}).get("weights") or {}
    cw = ((cur_roles or {}).get("mark_engine") or {}).get("weights") or {}
    if not pw or not cw:
        return None
    deltas = {v: abs(cw.get(v, 0) - pw.get(v, 0)) for v in set(pw) | set(cw)}
    biggest = max(deltas, key=deltas.get) if deltas else None
    if biggest and deltas[biggest] >= threshold_pct:
        return biggest, pw.get(biggest, 0), cw.get(biggest, 0)
    return None


def _emit(ticker, kind, direction, title, body, has_live_thesis, now, notify_fn, page_state_path):
    """Every event appends to inbox regardless of route (req 6). `annotate` never
    calls notify/page_gate at all — it's inbox-only by definition."""
    route = route_event(kind, direction, has_live_thesis)
    severity = "HIGH" if route == "phone" else "MED"
    inbox.append_event(ts=_now_iso(now), ticker=ticker, source=kind, severity=severity,
                       msg=f"{title} — {body}")
    paged = False
    if route in ("phone", "desk"):
        should_page = page_gate.should_page(
            {"label": f"{ticker}:{kind}:{direction}", "dest_kind": route},
            now=now, state_path=page_state_path)
        if should_page and notify_fn:
            try:
                notify_fn(title, body, route)
                paged = True
            except Exception:  # noqa: BLE001 — a dead notifier must not kill the tick
                pass
    return {"ticker": ticker, "kind": kind, "direction": direction, "route": route, "paged": paged}


def process_ticker(ticker, oic_state, prev_roles, cur_oic, has_live_thesis, now=None,
                   notify_fn=None, page_state_path=None):
    """One ticker's tick: debounced OIC_FLIP + both ROLE_DRIFT checks. Returns
    `(events, new_oic_state)` — `oic_state` is this ticker's persisted debounce
    state (see `classify_flip`); `cur_oic` is a fresh `build_oi_construction`-shaped
    envelope (or None — treated as UNKNOWN/stale, never pages, per req 6: "Staleness
    flips gating_ok silently, never pages")."""
    now = now or datetime.now(timezone.utc)
    events = []
    verdict = None
    if cur_oic and cur_oic.get("gating_ok"):
        verdict = cur_oic.get("verdict")
    direction, new_state, prior_last = classify_flip(oic_state or {}, verdict)
    if direction:
        title, body = PG.page_oic_flip(ticker, prior_last, new_state["last"], direction)
        events.append(_emit(ticker, "oic_flip", direction, title, body, has_live_thesis,
                           now, notify_fn, page_state_path))

    cur_roles = (cur_oic or {}).get("venue_roles")
    exit_drift = detect_exit_flow_drift(prev_roles, cur_roles)
    if exit_drift:
        title, body = PG.page_role_drift(ticker, "exit", exit_drift[0], exit_drift[1])
        events.append(_emit(ticker, "role_drift", "exit_flow", title, body, has_live_thesis,
                           now, notify_fn, page_state_path))
    mark_drift = detect_mark_constituent_drift(prev_roles, cur_roles)
    if mark_drift:
        title, body = PG.page_role_drift(ticker, "mark_engine",
                                         f"{mark_drift[0]} {mark_drift[1]:.1f}%",
                                         f"{mark_drift[0]} {mark_drift[2]:.1f}%")
        events.append(_emit(ticker, "role_drift", "mark_constituent", title, body,
                           has_live_thesis, now, notify_fn, page_state_path))

    return events, new_state


def run_tick(universe, oic_fn=None, has_thesis_fn=None, now=None, notify_fn=None,
            state_path=None, roles_path=None, page_state_path=None):
    """`universe`: list of tickers to check this tick (the 15m board tick's default
    is LIVE-thesis names only, per req 6's cadence-home note — the caller decides
    the universe, this function is universe-agnostic). Best-effort per ticker: a
    crash on one name never kills the tick."""
    oic_fn = oic_fn or OC.build_oi_construction
    has_thesis_fn = has_thesis_fn or (lambda t: False)
    state_path = state_path or STATE_PATH
    roles_path = roles_path or VENUE_ROLES_PATH
    page_state_path = page_state_path or page_gate.COOLDOWN_PATH
    now = now or datetime.now(timezone.utc)

    all_state = _read_json(state_path, {}) or {}
    all_roles_hist = _read_json(roles_path, {}) or {}
    all_events = []
    for ticker in universe:
        try:
            cur_oic = oic_fn(ticker)
        except Exception:  # noqa: BLE001
            cur_oic = None
        hist = all_roles_hist.get(ticker.upper()) or []
        prev_roles = hist[-1] if hist else None
        try:
            has_live_thesis = bool(has_thesis_fn(ticker))
        except Exception:  # noqa: BLE001
            has_live_thesis = False
        events, new_state = process_ticker(
            ticker, all_state.get(ticker.upper(), {}), prev_roles, cur_oic,
            has_live_thesis, now=now, notify_fn=notify_fn, page_state_path=page_state_path)
        all_state[ticker.upper()] = new_state
        all_events.extend(events)
        if cur_oic and cur_oic.get("venue_roles"):
            OC.save_venue_roles_snapshot(ticker, cur_oic["venue_roles"], path=roles_path)

    _write_json(state_path, all_state)
    return {"checked": len(universe), "events": len(all_events),
           "paged": sum(1 for e in all_events if e["paged"]), "detail": all_events}


if __name__ == "__main__":
    print(json.dumps(run_tick(sys.argv[1:] or [])))
