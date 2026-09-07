#!/usr/bin/env python3
"""ops/telegram_post.py — SPEC-181: public Telegram signal-channel broadcast leg.

The desk pages the user privately via ops/notify.sh (osascript + ntfy). This is a
DIFFERENT animal: a public channel that must never reveal how the desk works ("I don't
want people making the same thing" — user directive 2026-08-31). Two pieces:

  post(text)          best-effort delivery to the configured Telegram channel. Never
                       raises; missing/empty credentials or CRIMEDESK_NOTIFY=off is a
                       silent no-op (feature off / tests must never post); an HTTP
                       failure logs one line to state/telegram.err and returns False.
                       Credentials are resolved AT SEND TIME (env first, then the
                       one-line files under ~/Library/Application Support/crimedesk/)
                       — unlike ops/install_launchd.sh's `__NTFY_URL__` plist render,
                       there is no build-time substitution here.

  format_card(row)     the load-bearing redaction boundary. Builds a card from an
                       ALLOWLIST of named numeric fields only — ticker, direction,
                       event label, entry_zone/stop/tp, an optional leg position, a UTC
                       timestamp — and never reads anything else `row` might carry
                       (signature, triggers/invalidation prose, wallet addresses,
                       page_grammar output, venue-role/funding/on-chain reasoning,
                       tier/ledger stats, sizing). Redaction is BY CONSTRUCTION: the
                       function simply never looks at those keys, so a caller can hand
                       it the raw thesis dict and nothing forbidden leaks. See
                       docs/telegram_channel.md — any new field added to the card must
                       be re-reviewed against this contract.

  format_setup_card() / format_cancel_card()   SPEC-189: the same allowlist contract,
                       extended (user directive 2026-09-02) to thesis commits/retires.
                       format_setup_card reads ONLY ticker/direction/time_stop plus,
                       per leg, entry_zone/stop/tp and a fixed-vocabulary trigger
                       phrase built from numeric fields only — never the leg's `gate`
                       prose (that would be string-scraping the redaction boundary).

Wired into ops/board_tick.py's TRIGGERS/BREAKS/STOP-BREACHED push path (format_card,
see board_tick._tg_push) and its per-tick thesis-commit sweep (format_setup_card /
format_cancel_card, see board_tick._setup_sweep).
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
from page_grammar import fmt_price  # noqa: E402  (pure numeric formatter, no prose)

ERR_PATH = REPO / "state" / "telegram.err"
CREDS_DIR = Path.home() / "Library" / "Application Support" / "crimedesk"
TOKEN_FILE = CREDS_DIR / "telegram_bot_token"
CHAT_ID_FILE = CREDS_DIR / "telegram_chat_id"

TIMEOUT_S = 10

# The ONLY three event classes board_tick ever posts (SPEC-181 req 2) — anything else
# passed as `row["event"]` is not a recognized public call and yields no card.
_EVENT_LABELS = {
    "TRIGGERS": "ENTRY TRIGGERED",
    "BREAKS": "THESIS INVALIDATED",
    "STOP_BREACH": "STOPPED OUT",
}


def _log_err(msg):
    ERR_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ERR_PATH.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}\n")


def _read_one_line(path):
    try:
        v = path.read_text().strip()
        return v or None
    except OSError:
        return None


def _credentials():
    """(token, chat_id), resolved at SEND TIME: env vars first, else the one-line
    files. Either missing/empty -> (None, None) — the feature-off signal."""
    token = os.environ.get("CRIMEDESK_TG_BOT_TOKEN") or _read_one_line(TOKEN_FILE)
    chat_id = os.environ.get("CRIMEDESK_TG_CHAT_ID") or _read_one_line(CHAT_ID_FILE)
    if not token or not chat_id:
        return None, None
    return token, chat_id


def _http_post(url, payload_bytes):
    """Isolated so tests can monkeypatch the network edge without touching post()'s
    control flow (credential/kill-switch checks) — same pattern as tape_watch.run_tick
    being swapped in tests/test_board_tick.py."""
    req = urllib.request.Request(
        url, data=payload_bytes, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        resp.read()


def post(text):
    """POST `text` to the configured Telegram channel. Best-effort — NEVER raises.
    Returns True on a delivered send, False on any no-op or failure (kill switch,
    missing credentials, empty text, HTTP error — the last is also logged to
    state/telegram.err)."""
    if os.environ.get("CRIMEDESK_NOTIFY", "").strip().lower() == "off":
        return False
    if not text:
        return False
    token, chat_id = _credentials()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }).encode()
    try:
        _http_post(url, payload)
        return True
    except Exception as ex:  # noqa: BLE001 — a dead channel must never raise
        _log_err(f"post failed: {ex}")
        return False


def _numeric_pair(zone):
    return (isinstance(zone, (list, tuple)) and len(zone) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in zone))


def _numeric_list(tp):
    if isinstance(tp, (int, float)) and not isinstance(tp, bool):
        return [tp]
    if isinstance(tp, (list, tuple)):
        return [t for t in tp if isinstance(t, (int, float)) and not isinstance(t, bool)]
    return []


def format_card(row):
    """Allowlist-constructed public trade card, or None when the fireable leg has no
    numeric params (a paramless watch is not a public call). `row` may carry arbitrary
    OTHER keys (signature, triggers, invalidation, notes, wallets, ...) — this function
    reads ONLY `ticker`, `event`, `direction`, `entry_zone`, `stop`, `tp`/`tps` and an
    optional integer `leg` (SPEC-189: which leg of a multi-leg thesis this card was
    built from, e.g. `2` for "Leg 2" — a bare position number, never leg prose) and
    never touches anything else, by construction."""
    row = row or {}
    ticker = row.get("ticker")
    label = _EVENT_LABELS.get(row.get("event"))
    if not ticker or not label:
        return None

    numeric_bits = []
    entry_zone = row.get("entry_zone")
    if _numeric_pair(entry_zone):
        numeric_bits.append(f"Entry {fmt_price(entry_zone[0])}–{fmt_price(entry_zone[1])}")
    stop = row.get("stop")
    if isinstance(stop, (int, float)) and not isinstance(stop, bool):
        numeric_bits.append(f"Stop {fmt_price(stop)}")
    tps = _numeric_list(row.get("tp") if row.get("tp") is not None else row.get("tps"))
    if tps:
        numeric_bits.append("TP " + "/".join(fmt_price(t) for t in tps))

    if not numeric_bits:
        return None

    direction = str(row.get("direction") or "").upper()
    head = f"{ticker} {direction} — {label}" if direction in ("LONG", "SHORT") \
        else f"{ticker} — {label}"
    leg = row.get("leg")
    leg_prefix = f"Leg {leg}  " if isinstance(leg, int) and not isinstance(leg, bool) else ""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return "\n".join([head, leg_prefix + " | ".join(numeric_bits), ts])


# ── SPEC-189: NEW SETUP (commit) / CANCELLED (retire) cards ─────────────────────────
# Same allowlist-by-construction discipline as format_card above, extended to thesis
# commits/retires per the user directive ("I do want the bot to post the thesis").

_TRIGGER_TIMEFRAMES = ("1h", "4h")
_TRIGGER_OPS = ("above", "below")


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _leg_geometry(leg):
    """(entry_zone|None, stop|None, tps[]) off ONE leg-or-top-level-thesis dict —
    reads only entry_zone/stop/tp(tps), same three fields format_card reads."""
    leg = leg or {}
    zone = leg.get("entry_zone")
    zone = zone if _numeric_pair(zone) else None
    stop = leg.get("stop")
    stop = stop if _is_num(stop) else None
    tps = _numeric_list(leg.get("tp") if leg.get("tp") is not None else leg.get("tps"))
    return zone, stop, tps


def _leg_trigger_phrase(leg):
    """Fixed-vocabulary `{timeframe} close {above|below} {price}` built ONLY from
    numeric/enum fields (`trigger_timeframe`, `trigger_op`, `entry_ref`) — never the
    leg's free-text `gate`/`entry` prose. Any field missing or off-vocabulary -> None
    (the trigger line is simply omitted, per SPEC-189 §C)."""
    leg = leg or {}
    tf = leg.get("trigger_timeframe")
    op = leg.get("trigger_op")
    price = leg.get("entry_ref")
    if tf not in _TRIGGER_TIMEFRAMES or op not in _TRIGGER_OPS or not _is_num(price):
        return None
    return f"Trigger: {tf} close {op} {fmt_price(price)}"


def has_leg_geometry(thesis):
    """True when `thesis` (top-level OR any element of `legs[]`) carries at least one
    numeric entry_zone/stop/tp — a paramless WATCH is not a public call (SPEC-189 §C,
    same rule format_card already applies per-leg)."""
    thesis = thesis or {}
    zone, stop, tps = _leg_geometry(thesis)
    if zone or stop is not None or tps:
        return True
    for leg in (thesis.get("legs") or []):
        if not isinstance(leg, dict):
            continue
        zone, stop, tps = _leg_geometry(leg)
        if zone or stop is not None or tps or _is_num(leg.get("entry_ref")):
            return True
    return False


def _fmt_valid_until(time_stop):
    if not time_stop:
        return None
    try:
        s = str(time_stop)
        dt = (datetime.fromisoformat(s.replace("Z", "+00:00")) if "T" in s
              else datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return None


def _setup_leg_line(idx, leg):
    zone, stop, tps = _leg_geometry(leg)
    bits = []
    trig = _leg_trigger_phrase(leg)
    if trig:
        bits.append(trig)
    if zone:
        bits.append(f"Entry {fmt_price(zone[0])}–{fmt_price(zone[1])}")
    if stop is not None:
        bits.append(f"Stop {fmt_price(stop)}")
    if tps:
        bits.append("TP " + "/".join(fmt_price(t) for t in tps))
    if not bits:
        return None
    return f"Leg {idx}  " + " · ".join(bits)


def format_setup_card(ticker, thesis):
    """NEW SETUP card on a thesis commit, or None when there is nothing numeric to
    show (a paramless WATCH). Reads ONLY ticker/direction/time_stop off `thesis`
    plus, per leg, the same entry_zone/stop/tp/entry_ref fields format_card and
    has_leg_geometry already read — never `setup`/`gate`/`conviction`/`note` prose,
    by construction (those keys are simply never looked at)."""
    thesis = thesis or {}
    if not ticker or not has_leg_geometry(thesis):
        return None
    legs = [leg for leg in (thesis.get("legs") or []) if isinstance(leg, dict)]
    leg_sources = legs if legs else [thesis]
    lines = [ln for ln in (_setup_leg_line(i, leg) for i, leg in enumerate(leg_sources, 1))
             if ln]
    if not lines:
        return None
    direction = str(thesis.get("direction") or "").upper()
    head = f"NEW SETUP · {ticker} · {direction}" if direction in ("LONG", "SHORT") \
        else f"NEW SETUP · {ticker}"
    out = [head] + lines
    valid = _fmt_valid_until(thesis.get("time_stop"))
    if valid:
        out.append(f"Valid until {valid}")
    out.append(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    return "\n".join(out)


def format_cancel_card(ticker):
    """CANCELLED card on a thesis retire — ticker + a fixed phrase + timestamp, no
    reason text (a public setup that silently vanishes misleads readers; this is the
    minimum honest close, SPEC-189 §C)."""
    if not ticker:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"CANCELLED · {ticker} · setup withdrawn\n{ts}"
