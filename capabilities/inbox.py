#!/usr/bin/env python3
"""inbox.py — alert ingestion: surveillance events actually reach the board (SPEC 45).

ops/surveil.sh + launchd write nonce escalations to state/nonce_alerts.log — and until
this capability nothing read it. CLAUDE.md §8 calls a dormant mega-safe lighting up "the
biggest Stage-5 escalation"; the desk's only push channel dead-ended in a log file.

Sources (extensible — SPEC 46's board_tick appends to the JSONL feed):
  state/nonce_alerts.log     ops/surveil.sh lines (`<ts> ESCALATION xN <envelope-json>`
                             and `<ts> SWEEP_ERROR ...`)
  state/inbox_events.jsonl   normalized events appended by other producers via
                             append_event() — one JSON object per line

Cursor: state/inbox_cursor.json {"through_ts": ...} — events at or before the cursor
are consumed. Writes are atomic (tmp + rename).

§0.5 purity: the inbox SURFACES; it never overrides a verdict. classify attaches each
row's unconsumed-alert summary and a reason note on HIGH — the Designer judges.

  python3 capabilities/inbox.py --json                                  # unconsumed events
  python3 capabilities/inbox.py --op ack --through-ts 2026-06-10T08:00:00Z --json
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG_PATH = REPO / "state" / "nonce_alerts.log"
EVENTS_PATH = REPO / "state" / "inbox_events.jsonl"
CURSOR_PATH = REPO / "state" / "inbox_cursor.json"

_SEV_RANK = {"LOW": 0, "MED": 1, "HIGH": 2}


# ── producers ──────────────────────────────────────────────────────────────────
def append_event(ts, ticker, source, severity, msg):
    """Append one normalized event to the JSONL feed (the SPEC 46 producer API)."""
    sev = severity if severity in _SEV_RANK else "MED"
    rec = {"ts": ts, "ticker": (ticker.upper() if ticker else None),
           "source": source, "severity": sev, "msg": str(msg)}
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_PATH.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


# ── parsers ────────────────────────────────────────────────────────────────────
def _extract_alerts(env):
    """Locate the ESCALATION alert list in a sweep envelope, tolerant of BOTH shapes:
      - orchestrator-wrapped: {"ok":..,"data":{"alerts":[...]},"meta":..}
      - raw onchain_board:    {"scanned":N,"alerts":[...],"board":[...]}
    SPEC 64: the restored sweep logged the raw `onchain_board` JSON; the wrapped-only
    read (env["data"]["alerts"]) found nothing there → 0 unconsumed despite N alerts
    (the 9-day silent death). Return the alert list, or [] if neither shape matches."""
    if not isinstance(env, dict):
        return []
    data = env.get("data")
    if isinstance(data, dict) and isinstance(data.get("alerts"), list):
        return data["alerts"]
    if isinstance(env.get("alerts"), list):
        return env["alerts"]
    return []


def _parse_nonce_log():
    """state/nonce_alerts.log → events. ESCALATION lines carry the full orchestrator
    envelope after the marker; one event per alerted ticker. SWEEP_ERROR → LOW ops event.
    Unparseable lines are skipped, never fatal (the log is append-only shell output)."""
    if not LOG_PATH.exists():
        return []
    events = []
    for ln in LOG_PATH.read_text(errors="replace").splitlines():
        parts = ln.split(None, 2)
        if len(parts) < 2 or "T" not in parts[0]:
            continue
        ts, marker = parts[0], parts[1]
        if marker == "SWEEP_ERROR":
            events.append({"ts": ts, "ticker": None, "source": "nonce_surveil",
                           "severity": "LOW", "msg": ln[len(ts):].strip()})
            continue
        if marker != "ESCALATION" or len(parts) < 3:
            continue
        # payload = "xN {envelope}"
        payload = parts[2]
        brace = payload.find("{")
        if brace < 0:
            continue
        try:
            alerts = _extract_alerts(json.loads(payload[brace:]))
        except (ValueError, AttributeError):
            continue
        for a in alerts:
            fired = ", ".join(f"{f.get('label','?')}({f.get('nonce_prev','?')}→{f.get('nonce_now','?')})"
                              for f in a.get("escalation_fired", []))
            kind = a.get("escalation_kind")
            events.append({"ts": ts, "ticker": (a.get("ticker") or "").upper() or None,
                           "source": "nonce_surveil", "severity": "HIGH",
                           "msg": f"dormant safe FIRED [{fired}]"
                                  + (f" kind={kind}" if kind else "") + " — §8 bid-pull/top"})
    return events


def _parse_events_feed():
    if not EVENTS_PATH.exists():
        return []
    events = []
    for ln in EVENTS_PATH.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(ln)
            if rec.get("ts") and rec.get("severity") in _SEV_RANK:
                events.append(rec)
        except ValueError:
            continue
    return events


# ── reads ──────────────────────────────────────────────────────────────────────
def _cursor():
    try:
        return json.loads(CURSOR_PATH.read_text()).get("through_ts") or ""
    except (OSError, ValueError):
        return ""


def all_events():
    """Every event from every source, ts-ascending (ISO strings sort lexically)."""
    return sorted(_parse_nonce_log() + _parse_events_feed(), key=lambda e: e["ts"])


def unconsumed():
    """Events after the ack cursor (cursor is inclusive-consumed)."""
    cur = _cursor()
    return [e for e in all_events() if e["ts"] > cur]


def alerts_for(ticker):
    """Per-ticker summary for board rows: {n, max_severity, events}."""
    tk = (ticker or "").upper()
    ev = [e for e in unconsumed() if e.get("ticker") == tk]
    if not ev:
        return {"n": 0, "max_severity": None, "events": []}
    mx = max(ev, key=lambda e: _SEV_RANK.get(e["severity"], 0))["severity"]
    return {"n": len(ev), "max_severity": mx, "events": ev}


# ── ack ────────────────────────────────────────────────────────────────────────
def ack(through_ts):
    """Advance the cursor (atomic tmp+rename). Events ts <= through_ts are consumed."""
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(CURSOR_PATH.parent), prefix=".inbox_cursor.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"through_ts": through_ts}, f)
        os.replace(tmp, CURSOR_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"acked_through": through_ts, "remaining": len(unconsumed())}


# ── cli ────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="inbox — surveillance event ingestion (SPEC 45)")
    ap.add_argument("--op", default="list", choices=["list", "ack"])
    ap.add_argument("--through-ts", default=None)
    ap.add_argument("--ticker", default=None, help="filter unconsumed to one ticker")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.op == "ack":
        if not args.through_ts:
            print(json.dumps({"error": "ack needs --through-ts"})); sys.exit(1)
        out = ack(args.through_ts)
    elif args.ticker:
        out = alerts_for(args.ticker)
    else:
        ev = unconsumed()
        out = {"unconsumed": ev, "n": len(ev)}
    print(json.dumps(out, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
