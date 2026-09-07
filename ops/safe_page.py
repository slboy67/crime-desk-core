#!/usr/bin/env python3
"""safe_page.py — SPEC-163: route/compose ops/surveil.sh's "safe FIRED" and
"contract safe DRAINED" pages through ACT/DESK (the sender SPEC-157 missed —
those two `ops/notify.sh` calls had no 4th `route` arg, so every fired/drained
safe paged the phone regardless of whether the ticker was held or watched).

Given the raw JSON `ops/surveil.sh` already has in hand (the `onchain_board`
envelope for a FIRED page, `ops/balance_surveil.py tick`'s output for a
DRAINED page), this extracts one {ticker, label, usd} entry per fired/drained
safe, decides ACT vs DESK per `ops/route_for.py` (a ticker with an open
position or an ARMED/LIVE thesis is ACT; ANY qualifying ticker in a multi-
safe page makes the whole page ACT), reorders qualifying tickers first, and
composes the page body via `ops/page_grammar.page_safe_event`.

CLI (surveil.sh wiring) — reads the source JSON from stdin, prints exactly two
lines: the route (`act`/`desk`) then the composed body. Prints nothing (exit
0) when there are no entries — the caller already gates on count != 0 before
invoking this.

  printf '%s' "$out"     | python3 ops/safe_page.py fired
  printf '%s' "$bal_out" | python3 ops/safe_page.py drained
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "ops"))
import page_grammar as PG   # noqa: E402
import route_for            # noqa: E402


def entries_from_fired_envelope(envelope):
    """`onchain_board` envelope's `data.alerts` -> one entry per ticker, led by
    its highest-ranked fired wallet (already magnitude-sorted upstream by
    `_attribute_confirmed_fires`); a summary suffix names how many more fired
    on the same ticker, same convention the pre-SPEC-163 bash summary used."""
    data = (envelope or {}).get("data") or envelope or {}
    entries = []
    for a in (data.get("alerts") or []):
        ticker = a.get("ticker")
        wallets = a.get("escalation_fired") or []
        if not ticker or not wallets:
            continue
        lead = wallets[0]
        label = lead.get("label") or "?"
        if len(wallets) > 1:
            label += f" +{len(wallets) - 1} more"
        entries.append({"ticker": ticker, "label": label, "usd": lead.get("distributed_usd")})
    return entries


def entries_from_drained_payload(payload):
    """`balance_surveil.py tick --all --json` output -> `{ticker: {"fired": [...]}}`.
    One entry per fired wallet (a drained-safe page is already per-wallet
    granular, unlike the nonce FIRED page's per-ticker lead-wallet summary)."""
    entries = []
    for ticker, r in (payload or {}).items():
        for f in (r or {}).get("fired") or []:
            entries.append({"ticker": ticker, "label": f.get("label") or "?",
                             "usd": f.get("delta")})
    return entries


def route_and_compose(event_word, entries):
    """-> (route, body) or (None, None) when `entries` is empty."""
    if not entries:
        return None, None
    tickers = [e["ticker"] for e in entries]
    route = route_for.route_for_tickers(tickers)
    qualifying = set(route_for.qualifying_tickers(tickers))
    # stable sort: qualifying tickers first, original relative order preserved
    # within each group (req 3 — "the body then leads with the qualifying
    # ticker(s)").
    ordered_entries = sorted(entries, key=lambda e: e["ticker"] not in qualifying)
    body = PG.page_safe_event(event_word, ordered_entries)
    return route, body


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    if cmd == "fired":
        entries = entries_from_fired_envelope(payload)
        event_word = "FIRED"
    elif cmd == "drained":
        entries = entries_from_drained_payload(payload)
        event_word = "DRAINED"
    else:
        sys.exit(f"usage: {sys.argv[0]} fired|drained  (<json on stdin)")
    route, body = route_and_compose(event_word, entries)
    if route is not None:
        print(route)
        print(body)
