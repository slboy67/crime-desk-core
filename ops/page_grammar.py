#!/usr/bin/env python3
"""page_grammar.py — SPEC-141: one grammar for every pushed page. SPEC-161 (v2): pages
are COMPOSED, never quoted.

TICKER · PRICE · WHAT HAPPENED · DO THIS. The user is live on the ntfy phone channel;
before SPEC-141, pages read as engineer-speak (`verdict CONFIRMS→BREAKS` — no price, no
level, no action; `TAG STALE-THESIS: live 0.53 is 12% past nearest anchor 0.60 —
RE-ANCHOR` — desk jargon; wallet balances rendered `1.90324e+06 → 1.90252e+06
(-718.135)` — scientific notation, meaningless on a lock screen).

SPEC-161 (2026-08-25, "I'm still getting a lot of unreadable ntfys"): the root cause
was that `note` — analyst marginalia, multi-clause desk prose with dates/§-refs/
em-dashes — got interpolated VERBATIM into `page_watch_armed`/`page_funding_armed`
bodies, then hard-truncated at BODY_MAX, so the phone showed 120 chars of mid-sentence
jargon. `note` is now NEVER read by any page_* function — bodies build ONLY from
structured fields (ticker, live price, level+direction, thesis numbers) plus the new
optional `page_label` (≤40 chars, validated/truncated at commit-read time by
thesis._clip_page_label — never here), which is the ONLY prose a body may ever carry.

Title = severity emoji + ticker + event word. Body = live price, the level that fired,
and the pre-committed action — pulled off the committed thesis (`entry_zone`/`stop`/
`tp`/`direction`), NEVER invented at page time. A thesis that names no usable action
degrades to `read board`, never a fabricated instruction. Design-to-length: compose for
<=90 chars; BODY_MAX=120 is a hard backstop that must never cut a number — `_finalize_body`
drops the trailing prose clause instead of slicing mid-token.

Pure functions, no I/O — callers (board_tick.py, tape_watch.py, balance_surveil.py)
own fetching the live price / committed thesis / balance delta.
"""

BODY_MAX = 120   # SPEC-161: matches board_tick.py's own msg[:120] hard cut — one number

_DIR_ARROW = {"below": "↓", "above": "↑"}
_DIR_OP = {"below": "<", "above": ">"}


def fmt_price(x):
    """Natural-precision price string — never scientific notation, never None-crashes."""
    if x is None:
        return "—"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "—"
    ax = abs(x)
    if ax == 0:
        return "0"
    if ax >= 1000:
        s = f"{x:,.2f}"
    elif ax >= 1:
        s = f"{x:.4f}"
    elif ax >= 0.01:
        s = f"{x:.5f}"
    elif ax >= 0.0001:
        s = f"{x:.6f}"
    else:
        s = f"{x:.10f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def fmt_compact(n):
    """Human compact units — 1903240 -> '1.90M'. Never scientific notation."""
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if n < 0 else ""
    an = abs(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if an >= div:
            return f"{sign}{an / div:.2f}{suf}"
    return f"{sign}{an:.0f}"


def fmt_balance_change(prev, cur):
    """'1.90M → 1.90M (-718 · -0.04%)' — compact units + signed raw delta + signed %.
    Never scientific notation (the wallet-event bug this spec fixes:
    `1.90324e+06 → 1.90252e+06 (-718.135)`)."""
    if prev is None or cur is None:
        return "—"
    try:
        prev_f, cur_f = float(prev), float(cur)
    except (TypeError, ValueError):
        return "—"
    delta = cur_f - prev_f
    pct_s = ""
    if prev_f:
        pct_s = f" · {delta / prev_f * 100.0:+.2f}%"
    return f"{fmt_compact(prev_f)} → {fmt_compact(cur_f)} ({delta:+.0f}{pct_s})"


def _finalize_body(body, hard_max=BODY_MAX):
    """SPEC-161 req 4: design-to-length. Composition itself targets <=90 chars; this is
    only the hard backstop. A body under hard_max passes through untouched. Over it,
    the backstop must never cut a number — bodies are composed numbers-first/prose-last,
    so dropping the trailing clause at the last clean separator (never a blind [:N]
    mid-token slice) is always safe."""
    if len(body) <= hard_max:
        return body
    for sep in (" — ", "; ", ", "):
        idx = body.rfind(sep, 0, hard_max)
        if idx > 0:
            return body[:idx]
    return body[:hard_max]


def _tp1_of(thesis):
    tp = thesis.get("tp") or thesis.get("tps")
    if isinstance(tp, (list, tuple)):
        return tp[0] if tp else None
    if isinstance(tp, (int, float)):
        return tp
    return None


def _tp_index(price, thesis):
    """SPEC-161 req 3: the TP tag (TP1/TP2/…) is derived MECHANICALLY from the breached
    level's position in the committed thesis's own tp list — never parsed out of free
    text. Falls back to None (caller defaults to TP1) when the level doesn't match any
    committed TP (e.g. a TP-flavored watch_level with no matching thesis.tp)."""
    if price is None:
        return None
    tps = (thesis or {}).get("tp") or (thesis or {}).get("tps") or []
    if not isinstance(tps, (list, tuple)):
        return None
    for i, tp in enumerate(tps):
        if isinstance(tp, (int, float)) and abs(float(tp) - float(price)) < 1e-9:
            return i + 1
    return None


def _entry_display(thesis):
    """A single representative entry price for the STOP page (req 3's `(entry 0.00198)`)
    — thesis.entry (a fill-style scalar some callers set) if present, else the entry_zone
    bound nearest the stop (the edge the fill is expected to sit closest to)."""
    e = thesis.get("entry")
    if isinstance(e, (int, float)) and not isinstance(e, bool):
        return e
    zone = thesis.get("entry_zone")
    if (isinstance(zone, (list, tuple)) and len(zone) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in zone)):
        stop = thesis.get("stop")
        if isinstance(stop, (int, float)) and not isinstance(stop, bool):
            return min(zone, key=lambda z: abs(z - stop))
        return zone[0]
    return None


def _triggers_action(thesis):
    direction = thesis.get("direction")
    if not direction:
        return "read board"
    bits = [f"{direction} per thesis"]
    stop = thesis.get("stop")
    if stop is not None:
        bits.append(f"stop {fmt_price(stop)}")
    tp1 = _tp1_of(thesis)
    if tp1 is not None:
        bits.append(f"tp1 {fmt_price(tp1)}")
    return ", ".join(bits)


def page_triggers(ticker, price, thesis=None):
    """🟢 TICKER TRIGGERED — price in entry lo–hi → DIRECTION per thesis, stop S, tp1 T."""
    thesis = thesis or {}
    title = f"🟢 {ticker} TRIGGERED"
    entry = thesis.get("entry_zone")
    if entry and len(entry) == 2 and entry[0] is not None and entry[1] is not None:
        lead = f"{fmt_price(price)} in entry {fmt_price(entry[0])}–{fmt_price(entry[1])}"
    else:
        lead = fmt_price(price)
    body = f"{lead} → {_triggers_action(thesis)}"
    return title, _finalize_body(body)


def page_breaks(ticker, price, thesis=None):
    """🔴 TICKER BREAKS — price — invalidation hit (stop S) → thesis dead, do not chase."""
    thesis = thesis or {}
    title = f"🔴 {ticker} BREAKS"
    stop = thesis.get("stop")
    if stop is not None:
        body = f"{fmt_price(price)} — invalidation hit (stop {fmt_price(stop)}) → thesis dead, do not chase"
    else:
        body = f"{fmt_price(price)} — invalidation hit → read board"
    return title, _finalize_body(body)


def page_stop_breach(ticker, price, thesis=None):
    """SPEC-161 req 3: `STOP: GALA 0.00212 ≥ 0.00211 — close short (entry 0.00198)` —
    imperative ACT grammar, numbers first. Falls back to the SPEC-141 shape when the
    thesis names no direction/entry (never fabricated)."""
    thesis = thesis or {}
    title = f"⛔ {ticker} STOP"
    stop = thesis.get("stop")
    if stop is None:
        return title, _finalize_body(f"STOP: {ticker} {fmt_price(price)} — stop breached → read board")
    direction = str(thesis.get("direction") or "").upper()
    if direction == "SHORT":
        op, action = "≥", "close short"
    elif direction == "LONG":
        op, action = "≤", "close long"
    else:
        op, action = "≥" if (price is not None and price >= stop) else "≤", "exit if filled"
    body = f"STOP: {ticker} {fmt_price(price)} {op} {fmt_price(stop)} — {action}"
    entry = _entry_display(thesis)
    if entry is not None:
        body += f" (entry {fmt_price(entry)})"
    return title, _finalize_body(body)


def page_watch_armed(ticker, price, breach, thesis=None):
    """SPEC-161 req 3 — two shapes off the SAME trip-wire, chosen by whether the breach
    is TP-flavored (page_label containing "tp", case-insensitive):
      TP zone:      `TP1: GALA 0.00165 tagged — bank 60%, stop→BE`
      armed-entry:  `ENTRY? CASHCAT 0.00177 < 0.178 — needs HOLD+vol; stop 0.1955`
    `note` is NEVER read (SPEC-161 root cause); `page_label` — already validated/clipped
    to <=40 chars at commit-read time by thesis._clip_page_label — is the only prose."""
    breach = breach or {}
    thesis = thesis or {}
    label = (breach.get("page_label") or "").strip()
    lvl_price, d = breach.get("price"), breach.get("dir")
    if label and "tp" in label.lower():
        idx = _tp_index(lvl_price, thesis) or 1
        title = f"🟢 {ticker} TP{idx}"
        body = f"TP{idx}: {ticker} {fmt_price(price)} tagged — {label}"
        return title, _finalize_body(body)
    title = f"👁 {ticker} ARMED"
    op = _DIR_OP.get(d, "crossed")
    action = label or "convert to entry or dismiss"
    body = f"ENTRY? {ticker} {fmt_price(price)} {op} {fmt_price(lvl_price)} — {action}"
    stop = thesis.get("stop")
    if stop is not None:
        body += f"; stop {fmt_price(stop)}"
    return title, _finalize_body(body)


def page_drift(ticker, drift):
    """⚠️ TICKER DRIFTED — live is N% past anchor A → re-anchor or retire."""
    drift = drift or {}
    title = f"⚠️ {ticker} DRIFTED"
    live = drift.get("live_price")
    anchor = drift.get("nearest_anchor")
    pct = drift.get("nearest_dist_pct")
    pct_s = f"{pct:g}%" if pct is not None else "?%"
    body = f"{fmt_price(live)} is {pct_s} past anchor {fmt_price(anchor)} → re-anchor or retire"
    return title, _finalize_body(body)


def page_funding_armed(ticker, funding_4h, breach):
    """👁 TICKER ARMED — funding +0.018%/4h crossed lt +0.02% → <page_label or generic>
    (SPEC-142, the funding mirror of page_watch_armed). SPEC-161: `note` is NEVER read —
    `page_label` (already validated/clipped at commit-read time) is the only prose."""
    breach = breach or {}
    title = f"👁 {ticker} ARMED"
    threshold = breach.get("threshold_4h")
    op = breach.get("op") or ""
    label = (breach.get("page_label") or "").strip() or "convert to entry or dismiss"
    fh = f"{funding_4h:+.3f}" if funding_4h is not None else "?"
    th = f"{threshold:+.3f}" if threshold is not None else "?"
    body = f"funding {fh}%/4h crossed {op} {th}% → {label}"
    return title, _finalize_body(body)


def page_safe_event(event_word, entries):
    """SPEC-163: `SAFE FIRED: TICKER label -$usd | TICKER2 label2 -$usd2 →
    read board / exit?` — the ops/surveil.sh nonce-FIRED / balance-DRAINED
    page. `event_word` is "FIRED" or "DRAINED"; `entries` is a list of
    {ticker, label, usd} dicts, already ordered qualifying-ticker-first by the
    caller (route_for.sort_qualifying_first) — this function never re-sorts.
    Not a committed-thesis page (there is no per-ticker action to look up), so
    the action phrase is the fixed "read board / exit?" prompt: a firing/
    draining operator safe on a name you hold IS the exit signal."""
    parts = []
    for e in entries:
        seg = f"{e.get('ticker') or '?'} {e.get('label') or '?'}"
        usd = e.get("usd")
        if usd is not None:
            seg += f" -${fmt_compact(abs(usd))}"
        parts.append(seg)
    body = f"SAFE {event_word}: " + " | ".join(parts) + " → read board / exit?"
    return _finalize_body(body)


def page_tape(ticker, raw_msg):
    """⚠️ TICKER TAPE — <one verdict phrase> → check book (never raw detector names —
    tape_watch.py composes the phrase; this only titles + terminates it)."""
    title = f"⚠️ {ticker} TAPE"
    prefix = f"{ticker} tape: "
    body = raw_msg[len(prefix):] if raw_msg and raw_msg.startswith(prefix) else (raw_msg or "")
    if "check book" not in body:
        body = f"{body} → check book" if body else "check book"
    return title, _finalize_body(body)


def page_oic_flip(ticker, from_verdict, to_verdict, direction):
    """SPEC-180 req 6 — ⚠️ TICKER OIC FLIP — DIRECTIONAL → ARB_DOMINATED (fuel
    evaporating) → re-check the gate. `direction` is 'fuel_evaporating' or
    'fuel_arriving' (already decided by the caller's 2-tick-persisted classifier —
    this only titles + renders it, never re-derives the flip)."""
    title = f"⚠️ {ticker} OIC FLIP"
    fuel = "fuel evaporating" if direction == "fuel_evaporating" else "fuel arriving"
    body = f"{from_verdict} → {to_verdict} ({fuel}) → re-check the §4 gate"
    return title, _finalize_body(body)


def page_role_drift(ticker, role, from_val, to_val):
    """SPEC-180 req 6 — ⚠️ TICKER ROLE DRIFT — exit venue bitget → binance → the
    exit/mark read this thesis was committed on has moved. `role` is 'exit' or
    'mark_engine'."""
    title = f"⚠️ {ticker} ROLE DRIFT"
    label = "exit venue" if role == "exit" else "mark-constituent weight"
    body = f"{label} {from_val} → {to_val} → the read this thesis was committed on has moved"
    return title, _finalize_body(body)
