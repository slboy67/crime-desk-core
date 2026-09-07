#!/usr/bin/env python3
"""board_tick.py — standing classify loop with verdict-delta alerts (SPEC 46).

The scanner stops waiting for a session: every launchd tick runs the board, diffs each
ticker's {verdict, stop_breached, tps_printed} against state/board_last.json, and pushes
deltas into the SPEC-45 inbox + a push notification. No-delta ticks write nothing to
the inbox (§0.5 silence). VELVET's stop printed a full day before anyone saw it — this
is the fix.

Safety:
  - lockfile (state/board_tick.lock) guards against overlapping runs;
  - a classify failure SKIPS the tick (logged to state/board_tick.err) — the baseline
    is never overwritten with a bogus/partial board;
  - baseline update is atomic (tmp + rename).

Severity: HIGH for a flip to BREAKS or a fresh stop-breach (the cascade outruns the
session), MED for TRIGGERS flips and fresh TP prints, MED for any other verdict change.

SPEC-106: each tick also runs tape_watch.run_tick() over every WATCH-thesis name — the
between-committed-levels blind spot (BIRB moved -12% intraday inside its 0.070/0.095 watch
levels with operator_profit_take on the tape and the desk stayed silent). Paging only, gated
by page_gate; never touches the verdict/state machine.

SPEC-111 — DELIVERY (the generation above already existed; this is the gap between a
committed level firing into state/ and a human seeing it between sessions — BIRB's
short-conversion trigger crossed silently, LAB's WATCH-ARMED breach went unread −16%
past trigger). Exactly 3 event classes push via ops/notify.sh (no others — noise kills
alerting): a watch_level/funding_watch crossing (read fresh off each row's `watch_leg`/
`funding_leg`), a verdict transition to TRIGGERS or BREAKS, STOP-BREACHED, and a
tape_watch event at severity HIGH. TP prints, thesis_drift, and any other verdict
transition are inbox-only, never pushed.

SPEC-154 — ONE-SHOT (SPEC-111's 6h TTL re-ping was the bulk of the volume — 75 pages/48h
at a ~5:1 repeat-to-new-information ratio). A page now delivers exactly when a key
transitions to armed (never-seen, or previously disarmed) and never again while
continuously armed — there is no re-ping window. A "continuous" read (watch_level,
funding_watch, tape_watch — re-evaluated fresh every tick, not a delta) that leaves its
condition disarms, so the NEXT fresh crossing delivers immediately. thesis_drift is
inbox-only (12 pushes/7d, none actionable at page time — the STALE-THESIS inbox record
from classify.py is unchanged). CRIMEDESK_NOTIFY=off silences delivery entirely
(tests/replay set this).

Wired by ops/board_tick.sh + ops/com.crimedesk.board-tick.plist (StartInterval 900).
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "capabilities"))
sys.path.insert(0, str(REPO / "ops"))
import inbox      # noqa: E402  (SPEC 45 — the consumer the deltas flow into)
import onboard    # noqa: E402  (SPEC 51 — watchlist mapping is an invariant; swept per tick)
import tape_watch  # noqa: E402  (SPEC-106 — the between-levels tape-deterioration page)
import page_grammar as PG  # noqa: E402  (SPEC-141 — lock-screen page grammar)
import ledger      # noqa: E402  (SPEC-162 — open-commit sweep, feeds the paper-track scorer)
import oic_watch   # noqa: E402  (SPEC-180 req 6 — OIC_FLIP/ROLE_DRIFT alerting)
import telegram_post  # noqa: E402  (SPEC-181 — public signal-channel broadcast leg)
# SPEC-141: the committed thesis fields (entry_zone/stop/tp/direction) the page grammar's
# action phrase is pulled from — bare module-level name so tests can isolate it from the
# real config/watchlist.json, same pattern as onboard.WL_PATH.
from classify import load_watchlist as load_thesis_watchlist  # noqa: E402

BASELINE_PATH = REPO / "state" / "board_last.json"
LOCK_PATH = REPO / "state" / "board_tick.lock"
ERR_PATH = REPO / "state" / "board_tick.err"
NOTIFY_STATE_PATH = REPO / "state" / "notify_sent.json"   # SPEC-111: per-key delivery dedup
NOTIFY_SH = REPO / "ops" / "notify.sh"
POSITIONS_PATH = REPO / "config" / "positions.json"       # SPEC-157: ACT-routing source of truth
THESIS_SEEN_PATH = REPO / "state" / "thesis_seen.json"    # SPEC-189: commit/retire snapshot

LOCK_STALE_S = 30 * 60   # a lock older than this is a crashed tick, not a running one


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_err(msg):
    ERR_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ERR_PATH.open("a") as f:
        f.write(f"{_now_iso()} {msg}\n")


def _classify_board():
    """Default classify source: the orchestrator envelope (never raw script output)."""
    out = subprocess.run(
        [sys.executable, str(REPO / "orchestrator.py"), "classify", "{}"],
        capture_output=True, text=True, timeout=300)
    env = json.loads(out.stdout)
    if not env.get("ok"):
        raise RuntimeError(f"classify failed: {env.get('error', '?')}")
    data = env["data"]
    # SPEC 64: the board is now a {board, meta} envelope (meta carries the dead-man
    # surveil age); older shape was a bare list. Accept both.
    return data["board"] if isinstance(data, dict) else data


def _notify_sh_deliver(title, msg, priority="default", route="act"):
    """Default notifier: shells to ops/notify.sh (osascript + optional ntfy.sh POST, kill
    switch all handled inside the script). SPEC-141: `priority` forwards as the ntfy
    Priority header when it's meaningfully non-default — "default" omits the 3rd shell
    arg entirely (today's behavior, no header). SPEC-157: `route` forwards as the 4th
    arg — "desk" tells notify.sh to skip the CRIMEDESK_NTFY_URL POST; the osascript leg
    always fires. A placeholder empty 3rd arg is passed when priority is default but
    route isn't, so positional args land correctly."""
    args = ["bash", str(NOTIFY_SH), str(title), str(msg)]
    if route and route != "act":
        args.append(priority if (priority and priority != "default") else "")
        args.append(route)
    elif priority and priority != "default":
        args.append(priority)
    subprocess.run(args, capture_output=True, timeout=15)


# ── SPEC-111: per-key delivery dedup ─────────────────────────────────────────────

def _notify_key(ticker, event_class, level):
    return f"{ticker}|{event_class}|{level}"


def _load_notify_state():
    try:
        return json.loads(NOTIFY_STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_notify_state(state):
    try:
        NOTIFY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = NOTIFY_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(NOTIFY_STATE_PATH)
    except OSError:
        pass


def _arm_check(state, key, now_ts):
    """True = deliver now (and mark delivered). A never-seen or explicitly-disarmed key
    (see `_disarm`) delivers immediately — "a re-cross after the condition cleared re-arms
    it." SPEC-154: a key that stays continuously armed NEVER re-delivers — there is no TTL
    re-ping. Re-delivery requires the condition to clear (→ `_disarm`) and re-fire, or a
    human to re-arm the row (re-timestamp `committed_ts`)."""
    entry = state.get(key)
    if entry is None or not entry.get("armed"):
        state[key] = {"last_sent": now_ts, "armed": True}
        return True
    return False


def _disarm(state, key):
    """A continuous read (watch_level/thesis_drift) that no longer holds this tick clears
    its armed flag so the NEXT fresh crossing delivers immediately, not after 6h."""
    e = state.get(key)
    if e is not None:
        e["armed"] = False


def _current_continuous_keys(rows, tape_high_tickers=None):
    """watch_level and funding_watch (SPEC-142) are re-evaluated fresh every tick from live
    price/funding (not a delta like the diff()-sourced classes). SPEC-154: tape_watch joins
    via its own paged-HIGH-this-tick ticker set (tape_watch.run_tick has no watch_leg of its
    own to read fresh — the caller passes what it saw this tick). Any previously-armed key
    NOT in this tick's live set gets disarmed (see `_disarm`)."""
    keys = set()
    for r in rows:
        tk = r.get("ticker")
        wl = r.get("watch_leg") or {}
        if wl.get("legacy"):   # SPEC-113: unresolvable commit_ts — never pushed, so never armed
            continue
        for b in (wl.get("breached") or []):
            price, d = b.get("price"), b.get("dir")
            if price is None or d is None:
                continue
            keys.add(_notify_key(tk, "watch_level", f"{price:g}{d}"))
        fl = r.get("funding_leg") or {}
        for b in (fl.get("breached") or []):
            threshold, op = b.get("threshold_4h"), b.get("op")
            if threshold is None or op is None:
                continue
            keys.add(_notify_key(tk, "funding_watch", f"{threshold:g}{op}"))
    for tk in (tape_high_tickers or ()):
        keys.add(_notify_key(tk, "tape_watch", "high"))
    return keys


# ── SPEC-157: ACT vs DESK page routing ───────────────────────────────────────────

_ENTRY_CONDITION_CLASSES = ("watch_level", "funding_watch")


def _load_open_position_tickers():
    """config/positions.json's open rows (no `closed_ts`) — the ACT rule-1 source of
    truth: a live position makes EVERYTHING on that name actionable. Returns None (not
    an empty set) when the file is missing/malformed — `_route_for` treats None as
    "can't tell, never risk silencing a real page" and routes ACT unconditionally."""
    try:
        data = json.loads(POSITIONS_PATH.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("positions")
    if not isinstance(rows, list):
        return None
    return {r.get("ticker") for r in rows
            if isinstance(r, dict) and r.get("ticker") and not r.get("closed_ts")}


def _route_for(ticker, event_class, level, open_tickers, thesis_status, body=None):
    """ACT (phone) vs DESK (desktop-only) for one page. `open_tickers=None` means
    config/positions.json was unreadable this tick — degrade to ACT for everything
    rather than risk silencing a live position (AC-6). Otherwise:
      1. ticker has an open position -> ACT, any event class.
      2. thesis.status == ARMED and this is an entry-condition event (a watch_level/
         funding_watch crossing, or verdict TRIGGERS) -> ACT, UNLESS the composed body
         can't name an action off committed thesis fields (page_grammar's "read board"
         fallback) — that's DESK by definition (AC-4).
      3. everything else -> DESK.
    """
    if open_tickers is None:
        return "act"
    if ticker in open_tickers:
        return "act"
    is_entry_event = event_class in _ENTRY_CONDITION_CLASSES or (
        event_class == "verdict" and level == "TRIGGERS")
    if (thesis_status or "").upper() == "ARMED" and is_entry_event:
        if body and body.rstrip().endswith("read board"):
            return "desk"
        return "act"
    return "desk"


# ── SPEC-181: public Telegram signal channel ─────────────────────────────────────
# Exactly the three event classes board_tick already treats as push_worthy — no
# watch_level/funding_watch/tape_watch/TP/drift ever reaches this map, by construction
# (the caller only invokes _tg_push from inside the push_worthy block below).
_TG_EVENT = {
    ("stop_breach", "stop"): "STOP_BREACH",
    ("verdict", "TRIGGERS"): "TRIGGERS",
    ("verdict", "BREAKS"): "BREAKS",
}


# ── SPEC-189: leg-aware card construction + the entry-only TRIGGERS gate ─────────

def _leg_entry_prices(leg):
    """Numeric entry anchors off ONE leg — its entry_zone bounds and/or its
    entry_ref — the only fields a board-row watch_level crossing can be matched
    against by price."""
    leg = leg or {}
    out = []
    zone = leg.get("entry_zone")
    if isinstance(zone, (list, tuple)):
        out.extend(x for x in zone if isinstance(x, (int, float)) and not isinstance(x, bool))
    ref = leg.get("entry_ref")
    if isinstance(ref, (int, float)) and not isinstance(ref, bool):
        out.append(ref)
    return out


def _pick_firing_leg(thesis, board_row):
    """(leg, matched, index) — SPEC-189 §A: "the leg whose entry_zone/watch_level/
    entry_ref the price-leg event crossed (board rows carry which watch_level
    crossed; map it to the leg by price)." `matched=True` means a CURRENTLY-breached
    watch_level on this ticker numerically matches one of the leg's entry anchors —
    that is also this tick's entry-crossing EVIDENCE (see `_entry_gate_ok`), not just
    a card-construction convenience. `index` is the 1-based display position ("Leg
    N"). No legs at all -> (None, False, None) — the caller falls back to the
    thesis's own top-level fields, unchanged single-leg behavior."""
    legs = [leg for leg in ((thesis or {}).get("legs") or []) if isinstance(leg, dict)]
    if not legs:
        return None, False, None
    breached = ((board_row or {}).get("watch_leg") or {}).get("breached") or []
    for b in breached:
        bp = b.get("price")
        if bp is None:
            continue
        for i, leg in enumerate(legs, start=1):
            if any(math.isclose(cand, bp, rel_tol=1e-6, abs_tol=1e-9)
                   for cand in _leg_entry_prices(leg)):
                return leg, True, i
    return legs[0], False, 1   # SPEC-189 §A fallback: legs[0], unmatched


def _leg_row_fields(thesis, firing_leg, leg_index):
    """entry_zone/stop/tp(+leg) for format_card's row — the firing leg when the
    thesis is leg-based, else the thesis's own top-level fields (unchanged for a
    single-leg/legacy thesis, whose top-level fields ARE the position)."""
    if firing_leg is not None:
        return {"entry_zone": firing_leg.get("entry_zone"), "stop": firing_leg.get("stop"),
                "tp": firing_leg.get("tp") if firing_leg.get("tp") is not None
                else firing_leg.get("tps"),
                "leg": leg_index}
    thesis = thesis or {}
    return {"entry_zone": thesis.get("entry_zone"), "stop": thesis.get("stop"),
            "tp": thesis.get("tp") if thesis.get("tp") is not None else thesis.get("tps")}


_ENTRY_SANITY_PCT = 0.03   # SPEC-189 §B: live mark must be within 3% of the firing zone


def _entry_gate_ok(board_row, row_fields, matched, live_price):
    """True only for a genuine ENTRY crossing on the firing leg — never a TP print,
    a ZONE_BLOWN overrun, or thesis_drift/STALE (SPEC-189 §B, the FET false-post
    fix). Evidence is either a leg's watch_level entry gate having crossed this tick
    (`matched`) or, for a single-leg/legacy thesis, the price-leg's own
    `entered_zone` flag — `tps_printed` alone is deliberately NOT evidence. A belt-
    and-braces sanity check then requires the live mark to be within
    `_ENTRY_SANITY_PCT` of the firing leg's entry zone at post time; price data the
    board already fetched, format_card itself still never reads the tape."""
    price_leg = (board_row or {}).get("price_leg") or {}
    if not (matched or price_leg.get("entered_zone")):
        return False, "no entry crossing on the firing leg (tp print / zone-blown / stale drift)"
    zone = row_fields.get("entry_zone")
    if (live_price is not None and isinstance(zone, (list, tuple)) and len(zone) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in zone)):
        lo, hi = min(zone), max(zone)
        if not (lo <= live_price <= hi):
            ref = lo if live_price < lo else hi
            dist = abs(live_price - ref)
            if ref and (dist / abs(ref)) > _ENTRY_SANITY_PCT:
                return False, (f"live mark {live_price:g} outside firing-leg zone "
                              f"{lo:g}-{hi:g} by >{_ENTRY_SANITY_PCT * 100:g}%")
    return True, "ok"


def _priority_for(cls, level):
    """SPEC-141/142: severity -> ntfy Priority header. BREAKS/stop-breach are the cascade-
    outruns-the-session classes -> urgent; TRIGGERS/watch-cross/funding-cross are
    actionable-now but not emergencies -> high; everything else (thesis_drift, tape_watch)
    -> default."""
    if cls == "stop_breach" or (cls == "verdict" and level == "BREAKS"):
        return "urgent"
    if cls in ("watch_level", "funding_watch") or (cls == "verdict" and level == "TRIGGERS"):
        return "high"
    return "default"


def snapshot(rows):
    """Board rows → the per-ticker baseline shape that gets diffed and persisted."""
    snap = {}
    for r in rows:
        pl = r.get("price_leg") or {}
        snap[r["ticker"]] = {
            "verdict": r.get("verdict"),
            "stop_breached": bool(pl.get("stop_breached")),
            "tps_printed": sorted(pl.get("tps_printed") or []),
        }
    return snap


def diff(old, new):
    """Per-ticker deltas. A ticker absent from the old baseline is growth, not a delta."""
    # SPEC-111: each event carries (event_class, level) — the push-delivery filter keys
    # off these, NOT the message text, so it never has to string-match "TP printed" vs
    # "verdict … →BREAKS" to decide what's push-worthy.
    events = []
    for tk, cur in new.items():
        prev = old.get(tk)
        if prev is None:
            continue
        if cur["verdict"] != prev["verdict"]:
            sev = "HIGH" if cur["verdict"] == "BREAKS" else "MED"
            events.append((tk, sev, f"verdict {prev['verdict']}→{cur['verdict']}",
                          "verdict", cur["verdict"]))
        if cur["stop_breached"] and not prev["stop_breached"]:
            events.append((tk, "HIGH", "STOP BREACHED since last tick — a wick through the stop IS a break",
                          "stop_breach", "stop"))
        new_tps = [t for t in cur["tps_printed"] if t not in prev["tps_printed"]]
        if new_tps:
            events.append((tk, "MED", "TP printed: " + "/".join(f"{t:g}" for t in new_tps),
                          "tp_print", "tp"))
    return events


def _write_baseline(snap):
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(BASELINE_PATH.parent), prefix=".board_last.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(snap, f, indent=1)
        os.replace(tmp, BASELINE_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            age = datetime.now(timezone.utc).timestamp() - LOCK_PATH.stat().st_mtime
        except OSError:
            age = 0
        if age < LOCK_STALE_S:
            return False
        LOCK_PATH.unlink(missing_ok=True)   # crashed tick — break the stale lock
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _tape_watch_sweep():
    """SPEC-106: per-tick tape-deterioration check for WATCH names — read-only paging, never
    touches the verdict/state machine. A failure is logged and never kills the tick.

    SPEC-111: no `notify_fn` is passed to `tape_watch.run_tick` here — that would push on
    ANY paged severity (MED included), which is exactly the noise SPEC-111 stops. Delivery
    for the HIGH subset is decided in `tick()` from the returned `detail`, through the same
    dedup as every other push-worthy class."""
    try:
        return tape_watch.run_tick(wl_path=onboard.WL_PATH)
    except Exception as ex:  # noqa: BLE001
        _log_err(f"tape_watch sweep failed: {ex}")
        return {"error": str(ex)[:80]}


def _sweep_unmapped():
    """SPEC 51: per-tick mapping sweep. Pure set-difference first — ZERO network calls
    when nothing is unmapped; a sweep failure is logged and never kills the tick."""
    try:
        missing = onboard.unmapped_tickers()
        if not missing:
            return {"unmapped": 0}
        res = onboard.build_sweep()
        return {"unmapped": len(missing), "mapped": res.get("mapped", []),
                "failed": res.get("failed", [])}
    except Exception as ex:  # noqa: BLE001
        _log_err(f"onboard sweep failed: {ex}")
        return {"unmapped": -1, "error": str(ex)[:80]}


def _sweep_open_commits():
    """SPEC-162: keep the paper-track ledger fed every tick — diff the watchlist against
    existing OPEN ledger rows and commit_open() any (ticker, committed_ts) not yet
    recorded; the orchestrator's manual step disappears. Best-effort: a sweep failure is
    logged and never kills the tick."""
    try:
        return ledger.sweep_open_commits(wl_path=onboard.WL_PATH)
    except Exception as ex:  # noqa: BLE001
        _log_err(f"open-commit sweep failed: {ex}")
        return {"error": str(ex)[:80]}


def _oic_notify(title, body, route, notify=None):
    """Adapter: oic_watch's route ('phone'/'desk') -> _notify_sh_deliver's
    (priority, route='act'/'desk') shape. 'annotate' never reaches here (oic_watch
    skips notify_fn entirely for that route)."""
    fn = notify or _notify_sh_deliver
    fn(title, body, "high" if route == "phone" else "default",
      "act" if route == "phone" else "desk")


def _oic_watch_sweep(open_position_tickers, thesis_by_ticker, notify_fn=None):
    """SPEC-180 req 6 — the 15m board-tick cadence home for OIC_FLIP/ROLE_DRIFT.
    Universe = LIVE-thesis names only (open positions + ARMED watch legs) — other
    names refresh on the discovery-tick cadence per the spec. Best-effort: a sweep
    failure is logged and never kills the tick."""
    try:
        universe = sorted((open_position_tickers or set())
                          | {tk for tk, th in (thesis_by_ticker or {}).items()
                             if (th or {}).get("status") == "ARMED"})
        if not universe:
            return {"checked": 0, "events": 0, "paged": 0, "detail": []}
        live_set = set(open_position_tickers or set())

        def has_thesis(tk):
            return tk in live_set or (thesis_by_ticker.get(tk) or {}).get("status") == "ARMED"

        return oic_watch.run_tick(
            universe, has_thesis_fn=has_thesis,
            notify_fn=lambda t, b, r: _oic_notify(t, b, r, notify=notify_fn))
    except Exception as ex:  # noqa: BLE001
        _log_err(f"oic_watch sweep failed: {ex}")
        return {"error": str(ex)[:80]}


# ── SPEC-189: NEW SETUP (commit) / CANCELLED (retire) public cards ──────────────
# The "belt and braces" leg (ARCHITECTURE §C): whatever put the thesis in
# config/watchlist.json — `thesis.py --op commit`/`--op retire`, or a hand-edit —
# this per-tick diff against the last-seen snapshot catches it. Keyed by
# (ticker, committed_ts) so a retire-then-recommit is a fresh "new" (see
# `_setup_sweep`'s re-disarm of the sibling key on each transition).
_SETUP_STATUSES = ("WATCH", "ARMED", "LIVE", "PENDING")


def _load_thesis_seen():
    try:
        return json.loads(THESIS_SEEN_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_thesis_seen(state):
    try:
        THESIS_SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = THESIS_SEEN_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(THESIS_SEEN_PATH)
    except OSError:
        pass


def _card_already_posted(notify_state, ticker):
    """A STOP_BREACH or BREAKS public card already told readers this thesis died —
    stacking a CANCELLED card on top would misdescribe a stop-out/invalidation as a
    quiet withdrawal (SPEC-189 §C)."""
    for cls, level in (("stop_breach", "stop"), ("verdict", "BREAKS")):
        e = notify_state.get(f"tg:{_notify_key(ticker, cls, level)}")
        if e and e.get("armed"):
            return True
    return False


def _tg_setup_push(ticker, thesis, notify_state, now_ts, tg_post_fn, notify_off):
    if notify_off:
        return
    key = f"tg:{ticker}|setup|commit"
    if not _arm_check(notify_state, key, now_ts):
        return
    card = telegram_post.format_setup_card(ticker, thesis)
    if card is None:   # nothing sendable (paramless) — don't burn the one-shot
        _disarm(notify_state, key)
        return
    fn = tg_post_fn or telegram_post.post
    try:
        sent = fn(card)
    except Exception as ex:  # noqa: BLE001
        _log_err(f"telegram setup post failed for {key}: {ex}")
        sent = False
    if not sent:
        _disarm(notify_state, key)   # SPEC-189 §A rule, reused: retry next tick


def _tg_cancel_push(ticker, notify_state, now_ts, tg_post_fn, notify_off):
    # the ticker's NEXT commit deserves a fresh NEW SETUP, so clear the sibling key now
    _disarm(notify_state, f"tg:{ticker}|setup|commit")
    if notify_off or _card_already_posted(notify_state, ticker):
        return
    key = f"tg:{ticker}|setup|retire"
    if not _arm_check(notify_state, key, now_ts):
        return
    card = telegram_post.format_cancel_card(ticker)
    fn = tg_post_fn or telegram_post.post
    try:
        sent = fn(card)
    except Exception as ex:  # noqa: BLE001
        _log_err(f"telegram cancel post failed for {key}: {ex}")
        sent = False
    if not sent:
        _disarm(notify_state, key)


def _setup_sweep(thesis_by_ticker, notify_state, now_ts, tg_post_fn, notify_off):
    """Runs every tick, independent of the price-board baseline's own bootstrap
    state. First-ever run seeds the snapshot quietly (no NEW SETUP flood over the
    whole existing watchlist) — same convention as `_write_baseline`'s seed path."""
    first_run = not THESIS_SEEN_PATH.exists()
    seen = _load_thesis_seen()
    current = {}
    for tk, th in (thesis_by_ticker or {}).items():
        th = th or {}
        status = (th.get("status") or "").upper()
        cts = th.get("committed_ts")
        if status in _SETUP_STATUSES and cts and telegram_post.has_leg_geometry(th):
            current[tk] = cts
    if not first_run:
        for tk, cts in current.items():
            if seen.get(tk) != cts:
                _disarm(notify_state, f"tg:{tk}|setup|retire")
                _tg_setup_push(tk, thesis_by_ticker.get(tk), notify_state, now_ts,
                               tg_post_fn, notify_off)
        for tk in seen:
            if tk not in current:
                _tg_cancel_push(tk, notify_state, now_ts, tg_post_fn, notify_off)
    _save_thesis_seen(current)


def tick(classify_fn=None, notify_fn=None, now_fn=None, tg_post_fn=None):
    """One tick. Injectable classify/notify/now/tg_post for tests; returns a small
    status dict."""
    if not _acquire_lock():
        return {"skipped": "lock held (overlapping run)"}
    try:
        try:
            rows = (classify_fn or _classify_board)()
        except Exception as ex:  # noqa: BLE001 — any classify failure skips the tick
            _log_err(f"classify error, tick skipped: {ex}")
            return {"skipped": f"classify error: {ex}"}

        notify = notify_fn or _notify_sh_deliver
        now_ts = (now_fn or time.time)()
        # SPEC-111: kill switch — respected here (not just inside notify.sh) so an injected
        # notify_fn (tests, replay) is never invoked either.
        notify_off = os.environ.get("CRIMEDESK_NOTIFY", "").strip().lower() == "off"
        notify_state = _load_notify_state()

        # SPEC-136: execution-venue veto — a page for a trade the user cannot fill on
        # Aster is worse than no page (COTI: clean signal, un-executable, hand-audited
        # after the fact). `aster_listed` rides each row from classify's annotate step;
        # false SUPPRESSES the phone page (the inbox record still writes, tagged
        # [SIGNAL-ONLY] — see below); null (fetch failed / unknown) is never suppressed,
        # only tagged ⚠ venue-unverified — a transient network blip must never look like
        # "not listed" and silently kill every page on the board.
        aster_by_ticker = {r.get("ticker"): r.get("aster_listed") for r in rows}
        # SPEC-141: live price + committed thesis (entry_zone/stop/tp/direction) per
        # ticker — the page grammar's action phrase is pulled off these, never invented
        # at page time. Thesis load isolated behind a bare module name (see import above)
        # so a classify-fn-only test double never has to also fake the watchlist.
        live_by_ticker = {r.get("ticker"): r.get("live_price") for r in rows}
        # SPEC-189: board rows carry the price-leg/watch-leg detail `_tg_push` needs to
        # pick a leg-based thesis's firing leg and gate a TRIGGERS card on a genuine entry.
        row_by_ticker = {r.get("ticker"): r for r in rows}
        try:
            _wl_tokens, _wl_err = load_thesis_watchlist()
        except Exception:  # noqa: BLE001 — a dead watchlist read degrades to no thesis info
            _wl_tokens = []
        thesis_by_ticker = {t.get("ticker"): (t.get("thesis") or {}) for t in (_wl_tokens or [])}
        # SPEC-157: rule-1 source of truth for ACT routing, loaded once per tick.
        open_position_tickers = _load_open_position_tickers()
        # SPEC-189: NEW SETUP / CANCELLED — runs every tick, independent of the
        # price-board baseline's own bootstrap state (see _setup_sweep).
        _setup_sweep(thesis_by_ticker, notify_state, now_ts, tg_post_fn, notify_off)

        def _push(key, title, msg, ticker=None, priority="default", event_class=None, level=None):
            al = aster_by_ticker.get(ticker)
            if al is False:
                return
            if al is None:
                msg = f"⚠ venue-unverified — {msg}"
            if notify_off or not _arm_check(notify_state, key, now_ts):
                return
            thesis_status = (thesis_by_ticker.get(ticker) or {}).get("status")
            route = _route_for(ticker, event_class, level, open_position_tickers,
                               thesis_status, body=msg)
            try:
                try:
                    notify(title, msg[:120], priority, route)
                except TypeError:
                    try:
                        notify(title, msg[:120], priority)
                    except TypeError:
                        notify(title, msg[:120])   # legacy 2-arg notify_fn (tests, older callers)
            except Exception as ex:  # noqa: BLE001 — a dead notifier must not kill the tick
                _log_err(f"notify failed for {key}: {ex}")

        def _tg_push(ticker, cls, level, thesis):
            """SPEC-181/189: the public channel's own one-shot arm state, kept under a
            `tg:` key prefix in the SAME notify_state store so it never interferes with
            the private ntfy leg's delivery record. Only the 3 event classes in
            _TG_EVENT ever reach here (see call site) — nothing else is a public call.

            SPEC-189: (a) leg-aware — a legs[]-based thesis (top-level entry_zone/
            stop/tp all null) builds the card from the FIRING leg, matched to this
            tick's board row by price, never the empty top-level fields; (b) a
            TRIGGERS card additionally requires genuine entry-crossing evidence + a
            live-price sanity check (`_entry_gate_ok`) — a TP print / ZONE_BLOWN
            overrun / stale drift must never read as "ENTRY TRIGGERED" in public.
            The one-shot key is consumed only once `post()` returns True — a `None`
            card or a failed send leaves it armed so the next tick retries."""
            event = _TG_EVENT.get((cls, level))
            if event is None:
                return
            if notify_off:
                return
            key = f"tg:{_notify_key(ticker, cls, level)}"
            if not _arm_check(notify_state, key, now_ts):
                return
            thesis = thesis or {}
            board_row = row_by_ticker.get(ticker) or {}
            firing_leg, matched, leg_index = _pick_firing_leg(thesis, board_row)
            row_fields = _leg_row_fields(thesis, firing_leg, leg_index)
            if event == "TRIGGERS":
                ok, why = _entry_gate_ok(board_row, row_fields, matched, live_by_ticker.get(ticker))
                if not ok:
                    _log_err(f"telegram TRIGGERS card suppressed for {ticker}: {why}")
                    _disarm(notify_state, key)
                    return
            row = {"ticker": ticker, "event": event, "direction": thesis.get("direction"),
                   **row_fields}
            card = telegram_post.format_card(row)
            if card is None:
                _disarm(notify_state, key)
                return
            fn = tg_post_fn or telegram_post.post
            try:
                sent = fn(card)
            except Exception as ex:  # noqa: BLE001 — a dead poster must not kill the tick
                _log_err(f"telegram post failed for {key}: {ex}")
                sent = False
            if not sent:
                _disarm(notify_state, key)

        # SPEC-111: tape_watch's own notify_fn stays unset (see _tape_watch_sweep) — only
        # its HIGH+paged events are push-worthy here. Run it FIRST (SPEC-154) so its
        # paged-HIGH-this-tick ticker set can feed the continuous-disarm sweep below —
        # otherwise a tape_watch key, once armed, could never disarm and would page once
        # per lifetime instead of once per HIGH episode.
        tw = _tape_watch_sweep()
        tape_high_tickers = {ev.get("ticker") for ev in (tw.get("detail") or [])
                             if ev.get("paged") and ev.get("severity") == "HIGH"}

        # SPEC-180 req 6: OIC_FLIP/ROLE_DRIFT — self-contained (own inbox/page_gate/
        # notify wiring), so it's fine to run any time relative to the classes above.
        oic = _oic_watch_sweep(open_position_tickers, thesis_by_ticker, notify_fn=notify_fn)

        # SPEC-111/SPEC-142/SPEC-154: watch_level + funding_watch + tape_watch are read
        # FRESH every tick (not a delta) — disarm anything that left its condition this
        # tick so the next fresh crossing re-delivers immediately, then push whatever is
        # currently breached (always allowed regardless of severity — these classes are
        # the point). thesis_drift is inbox-only (see classify.py) — never pushed here.
        current_keys = _current_continuous_keys(rows, tape_high_tickers)
        for key in list(notify_state):
            parts = key.split("|", 2)
            if (len(parts) == 3 and parts[1] in ("watch_level", "funding_watch", "tape_watch")
                    and key not in current_keys):
                _disarm(notify_state, key)
        for r in rows:
            tk = r.get("ticker")
            fl = r.get("funding_leg") or {}
            for b in (fl.get("breached") or []):
                threshold, op = b.get("threshold_4h"), b.get("op")
                if threshold is None or op is None:
                    continue
                title, body = PG.page_funding_armed(tk, fl.get("funding_4h"), b)
                _push(_notify_key(tk, "funding_watch", f"{threshold:g}{op}"),
                     title, body, ticker=tk, priority=_priority_for("funding_watch", None),
                     event_class="funding_watch")
            wl = r.get("watch_leg") or {}
            if wl.get("legacy"):
                # SPEC-113: a trailing_24h_fallback window (no resolvable committed_ts, or
                # the commit predates the kline reach) can't verify the crossing traded
                # AFTER commit — never push it as a phone alert, only the tagged inbox event.
                continue
            for b in (wl.get("breached") or []):
                price, d = b.get("price"), b.get("dir")
                if price is None or d is None:
                    continue
                title, body = PG.page_watch_armed(tk, live_by_ticker.get(tk), b,
                                                  thesis_by_ticker.get(tk))
                _push(_notify_key(tk, "watch_level", f"{price:g}{d}"),
                     title, body, ticker=tk, priority=_priority_for("watch_level", None),
                     event_class="watch_level")

        for ev in (tw.get("detail") or []):
            if ev.get("paged") and ev.get("severity") == "HIGH":
                tk = ev.get("ticker")
                title, body = PG.page_tape(tk, ev.get("msg", ""))
                _push(_notify_key(tk, "tape_watch", "high"), title, body, ticker=tk,
                     priority=_priority_for("tape_watch", None), event_class="tape_watch")

        new = snapshot(rows)
        if not BASELINE_PATH.exists():
            _write_baseline(new)
            _save_notify_state(notify_state)
            return {"seeded": True, "tickers": len(new), "events": 0,
                    "onboard_sweep": _sweep_unmapped(), "open_commit_sweep": _sweep_open_commits(),
                "tape_watch": tw, "oic_watch": oic}

        try:
            old = json.loads(BASELINE_PATH.read_text())
        except (OSError, ValueError) as ex:
            _log_err(f"baseline unreadable, reseeding: {ex}")
            _write_baseline(new)
            _save_notify_state(notify_state)
            return {"seeded": True, "tickers": len(new), "events": 0, "tape_watch": tw, "oic_watch": oic}

        events = diff(old, new)
        ts = _now_iso()
        for tk, sev, msg, cls, level in events:
            # SPEC-136: a false-listed name still gets its inbox record (the read is
            # preserved for review) but tagged [SIGNAL-ONLY] — the suppression below is
            # on the phone page only, never on the record.
            inbox_msg = f"[SIGNAL-ONLY] {msg}" if aster_by_ticker.get(tk) is False else msg
            inbox.append_event(ts=ts, ticker=tk, source="board_tick", severity=sev, msg=inbox_msg)
            # SPEC-111: exactly verdict->TRIGGERS/BREAKS and STOP-BREACHED push; a TP print
            # or any other verdict transition is inbox-only.
            push_worthy = cls == "stop_breach" or (cls == "verdict" and level in ("TRIGGERS", "BREAKS"))
            if push_worthy:
                # SPEC-141: lock-screen grammar — title/body built from the committed
                # thesis + live price, never the raw diff() delta text.
                price = live_by_ticker.get(tk)
                thesis = thesis_by_ticker.get(tk)
                if cls == "stop_breach":
                    title, body = PG.page_stop_breach(tk, price, thesis)
                elif level == "TRIGGERS":
                    title, body = PG.page_triggers(tk, price, thesis)
                else:   # BREAKS
                    title, body = PG.page_breaks(tk, price, thesis)
                _push(_notify_key(tk, cls, level), title, body, ticker=tk,
                     priority=_priority_for(cls, level), event_class=cls, level=level)
                _tg_push(tk, cls, level, thesis)
        _write_baseline(new)
        _save_notify_state(notify_state)
        return {"tickers": len(new), "events": len(events),
                "onboard_sweep": _sweep_unmapped(), "open_commit_sweep": _sweep_open_commits(),
                "tape_watch": tw, "oic_watch": oic}
    finally:
        LOCK_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    print(json.dumps(tick()))
