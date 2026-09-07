#!/usr/bin/env python3
"""route_for.py — SPEC-163: ACT vs DESK routing for ops/surveil.sh pages (safe
FIRED / contract safe DRAINED), reusing the same "is this ticker something the
user must act on" facts SPEC-157's board_tick._route_for uses (open position,
or a committed thesis) — one source of truth so the two routing rules never
drift apart.

board_tick._route_for additionally gates its ARMED branch on the page being an
entry-condition event (a watch_level/funding_watch crossing or verdict
TRIGGERS) — a rule specific to board_tick's own page classes. A safe firing or
draining is never one of those (it's an on-chain apparatus event, not a price/
funding trigger), so this module does not call _route_for itself; it
reimplements just the shared "held or committed" test, using the SAME loaders
(`board_tick._load_open_position_tickers`, `board_tick.load_thesis_watchlist`)
so both routing rules read the exact same positions.json / watchlist.json.

A drained/fired safe on a ticker that is in config/positions.json (open) or
whose thesis `status` is ARMED or LIVE is genuinely ACT (an operator safe
firing on a name you HOLD or are committed to = exit signal) -> phone. Any
other ticker -> DESK (desktop + inbox only). "surveillance BLIND" never routes
through here — it is an ops-health message, not about a specific ticker, and
is hardcoded `desk` at the ops/surveil.sh call site.

Callable from shell:
    python3 ops/route_for.py TICKER1 TICKER2 ...     # prints "act" or "desk"
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "ops"))
import board_tick  # noqa: E402  (reuse its loaders — one source of truth)

_LIVE_STATUSES = ("ARMED", "LIVE")


def _thesis_status_by_ticker():
    try:
        tokens, _err = board_tick.load_thesis_watchlist()
    except Exception:  # noqa: BLE001 — a dead watchlist read degrades to "no thesis info"
        return {}
    out = {}
    for t in (tokens or []):
        tk = t.get("ticker")
        if tk:
            out[tk] = (t.get("thesis") or {}).get("status") or ""
    return out


def qualifies(ticker, open_tickers, thesis_status_by_ticker):
    """True = a safe event on `ticker` is genuinely ACT. `open_tickers=None`
    (positions.json unreadable) degrades to True — never risk silencing a real
    page (the same AC-6 rule board_tick._route_for uses)."""
    if open_tickers is None:
        return True
    if ticker in open_tickers:
        return True
    status = (thesis_status_by_ticker.get(ticker) or "").upper()
    return status in _LIVE_STATUSES


def qualifying_tickers(tickers):
    """The subset of `tickers` that make this page ACT, preserving input order."""
    open_tickers = board_tick._load_open_position_tickers()
    statuses = _thesis_status_by_ticker()
    return [t for t in tickers if qualifies(t, open_tickers, statuses)]


def route_for_tickers(tickers):
    """ACT if ANY listed ticker qualifies, else DESK."""
    return "act" if qualifying_tickers(tickers) else "desk"


def sort_qualifying_first(tickers):
    """Stable reorder — qualifying tickers first, original relative order
    preserved within each group (SPEC-163 req 3: "the body then leads with the
    qualifying ticker(s)")."""
    qual = set(qualifying_tickers(tickers))
    return sorted(tickers, key=lambda t: t not in qual)


if __name__ == "__main__":
    print(route_for_tickers(sys.argv[1:]))
