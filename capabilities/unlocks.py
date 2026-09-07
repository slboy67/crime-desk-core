#!/usr/bin/env python3
"""unlocks.py — auto-populated token-unlock calendar + pre-unlock alerts (SPEC-95).

Unlocks are the most PREDICTABLE supply event and the desk missed the BEAT/Audiera one
entirely (21.24M / 7.4% / ~$60M, one day out, invisible until a human brought a headline).
This capability stops discovering unlocks reactively:

  1. `unlocks '{"ticker":"BEAT"}'`  — upcoming unlock events for one token, SIZED off the
     CANONICAL CoinGecko circulating supply + live price (NOT a single-chain GoPlus
     total_supply — the BEAT bug that read 612k on BSC and printed a nonsense 3431%).
  2. `unlocks '{"op":"sweep"}'`     — full-watchlist sweep; merges upcoming unlocks into
     config/catalysts.json (manual entries preserved; auto entries marked source+fetched_ts).
  3. `unlocks '{"op":"fire-alerts"}'` — fire a once-per-event inbox alert at T-3d and T-1d
     for watchlist coins (cursor-deduped; mirrors the watch-armed / nonce-surveil pattern).

Identity discipline (the recurring wrong-token bug): every read keys off the verified
**cg_id**, never the bare ticker — BEAT resolves via cg_id `audiera`, not the other BEAT.

Sources:
  - PRIMARY (auto):  DefiLlama emissions datasets — the public, key-free feed
    (https://defillama-datasets.llama.fi/emissions/<slug>). The paywalled api.llama.fi
    /emissions (402) and CryptoRank (401) are NOT used. Covers ~340 tracked protocols.
  - FALLBACK/override: config/unlock_seed.json — cg_id-keyed entries for names the public
    feed doesn't track yet (brand-new tokens like Audiera). Populated from a public unlock
    tracker (Tokenomist/CryptoRank web) when the auto feed has no coverage. This is the
    "documented fallback (scrape a public unlocks page)" the spec calls for.

Coverage honesty: a token the source has nothing for is UNCOVERED (looked, found nothing),
distinct from source-DOWN (coverage:false) — "no data" is never silently read as "no unlock".

  python3 capabilities/unlocks.py BEAT --json
  python3 capabilities/unlocks.py --sweep --json
  python3 capabilities/unlocks.py --fire-alerts --json
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SEED_FILE = ROOT / "config" / "unlock_seed.json"
CATALYST_FILE = ROOT / "config" / "catalysts.json"
WATCHLIST_FILE = ROOT / "config" / "watchlist.json"
ALERT_CURSOR = ROOT / "state" / "unlock_alert_cursor.json"

HORIZON_DAYS = 7              # default brief/board flag horizon
WRITE_HORIZON_DAYS = 120     # how far ahead the sweep writes events into catalysts.json
ALERT_LEGS = (3, 1)          # pre-unlock alert legs: T-3d then T-1d
UNLOCK_TYPES = ("unlock", "cliff", "tge", "vesting")

DEFILLAMA_URL = "https://defillama-datasets.llama.fi/emissions/{slug}"
UA = {"User-Agent": "crimedesk-unlocks/1.0"}
TIMEOUT = 15

# Known cg_id overrides for ticker collisions (req #6 — BEAT is Audiera, not the other BEAT).
CG_ID_OVERRIDE = {"BEAT": "audiera"}

NOT_FOUND = "NOT_FOUND"      # sentinel: source responded but has no record for this slug


# ── date helpers ────────────────────────────────────────────────────────────────
def _now_date(now=None):
    if now is None:
        now = time.time()
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now, tz=timezone.utc).date()
    if isinstance(now, str):
        return datetime.fromisoformat(now.replace("Z", "+00:00")).date()
    return now


def days_until(date_str, now=None):
    d = datetime.fromisoformat(date_str).date() if isinstance(date_str, str) else date_str
    return (d - _now_date(now)).days


def _now_iso(now=None):
    if now is None:
        now = time.time()
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(now)


# ── sizing (the BEAT bug fix) ────────────────────────────────────────────────────
def size_event(amount, supply):
    """pct_circulating / usd_notional / vs_daily_volume off the CANONICAL CoinGecko
    circulating supply + live price (never a single-chain GoPlus total_supply)."""
    if not isinstance(supply, dict) or supply.get("_error"):
        return {"pct_circulating": None, "usd_notional": None, "vs_daily_volume": None}
    circ = supply.get("circulating") or 0
    price = supply.get("price") or 0
    vol = supply.get("vol_24h") or 0
    usd = amount * price if (amount and price) else None
    pct = round(amount / circ * 100, 2) if (amount and circ) else None
    vsvol = round(usd / vol, 2) if (usd and vol) else None
    return {"pct_circulating": pct, "usd_notional": usd, "vs_daily_volume": vsvol}


def _money(usd):
    if usd is None:
        return None
    a = abs(usd)
    if a >= 1e9:
        return f"${usd / 1e9:.1f}B"
    if a >= 1e6:
        return f"${usd / 1e6:.0f}M"
    if a >= 1e3:
        return f"${usd / 1e3:.0f}K"
    return f"${usd:.0f}"


def format_flag(ev, now=None):
    """`⏰ UNLOCK T-1d: 7.4% / $60M cliff (3× daily vol)` — robust to missing size fields."""
    days = days_until(ev["date"], now)
    tlabel = f"T-{days}d" if days >= 0 else f"T+{-days}d"
    parts = []
    pct = ev.get("pct_circulating")
    if pct is None and ev.get("pct_supply") is not None:
        pct = ev.get("pct_supply")
    if pct is not None:
        parts.append(f"{pct:g}%")
    m = _money(ev.get("usd_notional"))
    if m:
        parts.append(m)
    if parts:
        size = " / ".join(parts)
    elif ev.get("amount"):
        size = f"{ev['amount']:,.0f} tok"
    else:
        size = ""
    typ = ev.get("type") or "unlock"
    flag = f"⏰ UNLOCK {tlabel}: {size} {typ}".replace("  ", " ").rstrip()
    vv = ev.get("vs_daily_volume")
    if vv:
        flag += f" ({vv:g}× daily vol)"
    return flag


# ── sources ──────────────────────────────────────────────────────────────────────
def _fetch_defillama(slug):
    """GET the public emissions dataset for a protocol slug.
      payload dict on 200 · NOT_FOUND on 404 (source up, no record) · None on net error."""
    url = DEFILLAMA_URL.format(slug=slug)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:  # noqa: BLE001
        if e.code == 404:
            return NOT_FOUND
        return None
    except Exception:  # noqa: BLE001 — network/timeout/parse → source down
        return None


_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_TOKENS_IDX = re.compile(r"^tokens\[(\d+)\]$")
_CATEGORY_FROM_DESC = re.compile(r"of\s+([A-Za-z][\w\s]{0,30}?)\s+tokens", re.IGNORECASE)


def _render_description(desc, ts, amounts):
    """SPEC-110A: DefiLlama's `description` is sometimes a LITERAL unsubstituted template
    (`{timestamp}`, `{tokens[0]}`, …) — a real API quirk (the frontend renders it client-side
    off parallel arrays; we never got the substituted string). Render what we CAN from the
    event's own fields (`ts`, `amounts` = noOfTokens); return None if anything is left
    unresolved so the caller falls back to a self-composed detail — never persist a brace."""
    if not desc or "{" not in desc:
        return desc
    date_str = None
    if ts:
        try:
            date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %-d, %Y")
        except (ValueError, OSError, OverflowError):
            date_str = None

    def _sub(m):
        key = m.group(1).strip()
        if key == "timestamp" and date_str:
            return date_str
        idx_m = _TOKENS_IDX.match(key)
        if idx_m:
            idx = int(idx_m.group(1))
            if idx < len(amounts) and amounts[idx] is not None:
                return f"{amounts[idx]:,.0f}"
        return m.group(0)   # leave unresolved — caller checks for remaining braces

    rendered = _PLACEHOLDER.sub(_sub, desc)
    return rendered if "{" not in rendered else None


def _compose_detail(desc, date, amt, typ):
    """Fallback when the template has an unresolvable placeholder: never persist the raw
    braces (§ data hygiene) — self-compose `<amount> <category> tokens unlock on <date>`,
    pulling the category out of whatever plain text survives around the placeholders."""
    cat_m = _CATEGORY_FROM_DESC.search(desc or "")
    cat = cat_m.group(1).strip() if cat_m else typ
    amt_str = f"{amt:,.0f} " if amt else ""
    return f"{amt_str}{cat} tokens unlock on {date}"


def parse_defillama(payload, now=None):
    """metadata.events[] → normalized raw events. noOfTokens are summed (token units)."""
    if not isinstance(payload, dict):
        return []
    md = payload.get("metadata") or {}
    tok = md.get("token") or ""
    cg = tok.split(":")[-1] if ":" in tok else None
    out = []
    for e in md.get("events") or []:
        ts = e.get("timestamp")
        if not ts:
            continue
        date = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        amounts = e.get("noOfTokens") or []
        amt = sum(amounts) or None
        typ = e.get("unlockType") or "unlock"
        raw_desc = (e.get("description") or "")[:140]
        detail = _render_description(raw_desc, ts, amounts)
        if detail is None:   # unresolved placeholder(s) remain — never persist the braces
            detail = _compose_detail(raw_desc, date, amt, typ)
        out.append({"date": date, "amount": amt, "type": typ,
                    "cg_id": cg, "source": "defillama-emissions", "detail": detail[:140]})
    return out


def _load_seed():
    try:
        return json.loads(SEED_FILE.read_text()).get("seed", [])
    except Exception:  # noqa: BLE001
        return []


def _slug_candidates(ticker, cg_id):
    out = []
    for s in (cg_id, (ticker or "").lower()):
        if s and s not in out:
            out.append(s)
    return out


def _seed_match(s, ticker, cg_id):
    if cg_id and (s.get("cg_id") or "").lower() == (cg_id or "").lower():
        return True
    return (s.get("ticker") or "").upper() == (ticker or "").upper()


def resolve_raw_events(ticker, cg_id, fetcher, seed):
    """Merge seed (override) + DefiLlama (auto) raw events for one token.
    Returns (events, src_down). src_down = the public feed errored on every candidate."""
    events = []
    have_seed = False
    for s in seed or []:
        if _seed_match(s, ticker, cg_id):
            have_seed = True
            events.append({"date": s.get("date"), "amount": s.get("amount"),
                           "type": s.get("type") or "unlock",
                           "cg_id": s.get("cg_id") or cg_id, "source": s.get("source") or "seed",
                           "detail": s.get("detail") or ""})
    src_down, src_responded = False, False
    for slug in _slug_candidates(ticker, cg_id):
        payload = fetcher(slug)
        if payload is None:
            src_down = True
            continue
        src_responded = True
        if payload == NOT_FOUND:
            continue
        events += parse_defillama(payload)
        break          # first protocol hit wins
    # src down only matters if neither seed nor any source responded with data
    src_down = src_down and not have_seed and not (src_responded and events)
    return events, src_down


# ── supply (canonical CoinGecko) ─────────────────────────────────────────────────
def _live_supply(ticker, cg_id=None):
    try:
        sys.path.insert(0, str(HERE))
        from pull5 import coingecko_layer
        cg = coingecko_layer(ticker, cg_id=cg_id)
        if not isinstance(cg, dict) or cg.get("_error"):
            return {"_error": (cg or {}).get("_error", "supply resolve failed"),
                    "cg_id": (cg or {}).get("cg_id") or cg_id}
        return {"cg_id": cg.get("cg_id") or cg_id, "circulating": cg.get("circulating"),
                "total_supply": cg.get("total_supply"), "price": cg.get("price"),
                "vol_24h": cg.get("vol_24h")}
    except Exception as e:  # noqa: BLE001
        return {"_error": f"supply: {e}", "cg_id": cg_id}


# ── build (one token) ─────────────────────────────────────────────────────────────
def build_unlocks(ticker, cg_id=None, now=None, horizon_days=HORIZON_DAYS,
                  fetcher=None, supply_fn=None, seed=None, claim_topup_fn=None):
    """Upcoming unlock events for one token, sized off canonical CG supply. Degrade-explicit:
    coverage:false on source-down, uncovered:true on source-up-no-data — never a crash.

    SPEC-119: `claim_topup` carries any LIVE claim-distributor top-up (the pre-unlock staging
    signal) still within its decay window — the calendar's confirmation, or an unscheduled
    warning when no calendar entry exists. `claim_topup_fn(ticker, now)` is the test seam;
    default lazily imports claim_topup.recent_annotation (module-load-order safe — that module
    itself lazily imports unlocks.next_catalyst_for, so neither top-level-imports the other)."""
    ticker = (ticker or "").upper().replace("USDT", "")
    fetcher = fetcher or _fetch_defillama
    supply_fn = supply_fn or _live_supply
    seed = _load_seed() if seed is None else seed
    if claim_topup_fn is None:
        def claim_topup_fn(tk, n):
            sys.path.insert(0, str(HERE))
            import claim_topup
            return claim_topup.recent_annotation(tk, now=n)
    try:
        claim_topup_annotation = claim_topup_fn(ticker, now)
    except Exception:  # noqa: BLE001 — annotation is a bonus read, never blocks the calendar
        claim_topup_annotation = []

    cg_id = cg_id or CG_ID_OVERRIDE.get(ticker)
    supply = supply_fn(ticker, cg_id)
    resolved_cg = (supply.get("cg_id") if isinstance(supply, dict) else None) or cg_id

    raw, src_down = resolve_raw_events(ticker, resolved_cg, fetcher, seed)
    # dedupe raw on (date,type) — seed overrides an identical defillama row
    seen, deduped = set(), []
    for e in raw:
        k = (e.get("date"), e.get("type"))
        if k in seen:
            continue
        seen.add(k)
        deduped.append(e)

    events = []
    for e in deduped:
        if not e.get("date"):
            continue
        sized = size_event(e.get("amount"), supply)
        du = days_until(e["date"], now)
        events.append({**e, **sized, "days_until": du, "upcoming": du >= 0})
    events.sort(key=lambda e: e["date"])

    upcoming = [e for e in events if e["upcoming"]]
    nxt = upcoming[0] if upcoming else None
    within = bool(nxt and nxt["days_until"] <= horizon_days)
    coverage = bool(events) or (not src_down)
    uncovered = coverage and not events

    return {
        "ticker": ticker, "cg_id": resolved_cg,
        "coverage": coverage, "uncovered": uncovered, "source_down": src_down,
        "supply_available": not (isinstance(supply, dict) and supply.get("_error")),
        "events": events, "next_unlock": nxt,
        "within_horizon": within, "horizon_days": horizon_days,
        "flag": format_flag(nxt, now) if within else None,
        "claim_topup": claim_topup_annotation,   # SPEC-119: live top-up(s), else []
    }


# ── catalysts.json (auto-populate, merge not clobber) ─────────────────────────────
def _load_catalysts():
    try:
        return json.loads(CATALYST_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {"_note": "", "catalysts": []}


def _atomic_write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _cat_key(c):
    return ((c.get("cg_id") or c.get("ticker") or "").lower(), c.get("date"), c.get("type"))


def _is_auto(c):
    return str(c.get("source", "")).startswith("unlocks-auto")


def merge_catalysts(cal, new_entries):
    """Merge auto unlock entries into the calendar. Manual entries are NEVER touched; an
    existing AUTO entry with the same (cg_id|ticker, date, type) key is refreshed in place;
    a key already held by a MANUAL entry is skipped (don't duplicate)."""
    out = list(cal.get("catalysts", []))
    existing_keys = {_cat_key(c) for c in out}
    added = refreshed = 0
    for ne in new_entries:
        k = _cat_key(ne)
        idx = next((i for i, c in enumerate(out) if _cat_key(c) == k and _is_auto(c)), None)
        if idx is not None:
            out[idx] = ne
            refreshed += 1
        elif k in existing_keys:
            continue                      # manual (or other) already holds this slot
        else:
            out.append(ne)
            existing_keys.add(k)
            added += 1
    cal["catalysts"] = out
    return cal, added, refreshed


def _to_catalyst_entry(ticker, ev, now):
    return {
        "ticker": ticker, "cg_id": ev.get("cg_id"), "date": ev["date"],
        "type": ev.get("type") or "unlock",
        "pct_circulating": ev.get("pct_circulating"),
        "pct_supply": ev.get("pct_circulating"),     # legacy field consumers read
        "usd_notional": ev.get("usd_notional"),
        "vs_daily_volume": ev.get("vs_daily_volume"),
        "amount": ev.get("amount"),
        "detail": ev.get("detail") or f"{ticker} {ev.get('type')} unlock (auto)",
        "source": f"unlocks-auto:{ev.get('source') or 'feed'}",
        "fetched_ts": _now_iso(now),
    }


# ── flag readers (offline; used by classify/brief over the populated catalysts.json) ──
def next_catalyst_for(ticker, now=None, cats=None, horizon=None):
    cats = _load_catalysts() if cats is None else cats
    tk = (ticker or "").upper()
    cands = []
    for c in cats.get("catalysts", []):
        if (c.get("ticker") or "").upper() != tk:
            continue
        if c.get("type") not in UNLOCK_TYPES or not c.get("date"):
            continue
        du = days_until(c["date"], now)
        if du < 0:
            continue
        if horizon is not None and du > horizon:
            continue
        cands.append((du, c))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def unlock_flag_for(ticker, now=None, horizon_days=HORIZON_DAYS, cats=None):
    ev = next_catalyst_for(ticker, now, cats, horizon=horizon_days)
    return format_flag(ev, now) if ev else None


# ── pre-unlock alerts (T-3d / T-1d, cursor-deduped) ───────────────────────────────
def _leg_due(du, leg):
    if leg == 3:
        return 1 < du <= 3
    if leg == 1:
        return 0 <= du <= 1
    return False


def due_alerts(cats, now=None, fired=None):
    fired = fired or set()
    out = []
    for c in cats.get("catalysts", []):
        if c.get("type") not in UNLOCK_TYPES or not c.get("date"):
            continue
        cgkey = (c.get("cg_id") or c.get("ticker") or "").lower()
        du = days_until(c["date"], now)
        for leg in ALERT_LEGS:
            if not _leg_due(du, leg):
                continue
            key = f"{cgkey}:{c['date']}:T-{leg}"
            if key in fired:
                continue
            out.append({"key": key, "leg": leg, "catalyst": c, "days_until": du})
    return out


def _load_fired():
    try:
        return set(json.loads(ALERT_CURSOR.read_text()).get("fired", []))
    except Exception:  # noqa: BLE001
        return set()


def _severity(c):
    pct = c.get("pct_circulating") or c.get("pct_supply") or 0
    usd = c.get("usd_notional") or 0
    vv = c.get("vs_daily_volume") or 0
    if (pct and pct >= 5) or (usd and usd >= 1e7) or (vv and vv >= 2):
        return "HIGH"
    return "MED"


def fire_unlock_alerts(now=None, cats=None, fired=None, append=None, persist=True):
    """Fire one inbox event per due (event, leg), cursor-deduped. Returns the count fired.
    `append`/`fired` injectable for tests; defaults wire to inbox + the on-disk cursor."""
    cats = _load_catalysts() if cats is None else cats
    own_fired = fired is None
    fired = _load_fired() if own_fired else fired
    if append is None:
        sys.path.insert(0, str(HERE))
        import inbox
        append = inbox.append_event
    due = due_alerts(cats, now, fired)
    ts = _now_iso(now)
    for d in due:
        c = d["catalyst"]
        flag = format_flag(c, now)
        append(ts=ts, ticker=c.get("ticker"), source="unlock_monitor",
               severity=_severity(c), msg=f"{flag} — pre-unlock T-{d['leg']}d (§4/§8)")
        fired.add(d["key"])
    if persist and own_fired and due:
        _atomic_write_json(ALERT_CURSOR, {"fired": sorted(fired)})
    return len(due)


# ── watchlist sweep (auto-populate catalysts.json) ────────────────────────────────
def _watchlist_tickers():
    try:
        w = json.loads(WATCHLIST_FILE.read_text())
        toks = w.get("tokens") if isinstance(w, dict) else w
        return [t.get("ticker") for t in toks if t.get("ticker")]
    except Exception:  # noqa: BLE001
        return []


def sweep(now=None, write=True, fetcher=None, supply_fn=None, seed=None, tokens=None):
    """Sweep the watchlist, size upcoming unlocks, and merge them into catalysts.json.
    Coverage honesty: tokens with no data found are reported as `uncovered`, source-down as
    `down` — never silently dropped (so 'no data' is never read as 'no unlock')."""
    tokens = tokens if tokens is not None else _watchlist_tickers()
    covered, uncovered, down, new_entries = [], [], [], []
    for tk in tokens:
        try:
            u = build_unlocks(tk, now=now, fetcher=fetcher, supply_fn=supply_fn, seed=seed)
        except Exception as e:  # noqa: BLE001 — one bad token never kills the sweep
            down.append({"ticker": tk, "reason": str(e)[:120]})
            continue
        if u["source_down"] and not u["events"]:
            down.append({"ticker": tk, "reason": "source down"})
            continue
        up = [e for e in u["events"]
              if e["upcoming"] and e["days_until"] <= WRITE_HORIZON_DAYS]
        if up:
            covered.append({"ticker": tk, "n": len(up),
                            "next": up[0]["date"], "flag": u.get("flag")})
            for e in up:
                new_entries.append(_to_catalyst_entry(tk, e, now))
        else:
            uncovered.append(tk)
    added = refreshed = 0
    if write and new_entries:
        cal = _load_catalysts()
        cal, added, refreshed = merge_catalysts(cal, new_entries)
        _atomic_write_json(CATALYST_FILE, cal)
    return {"scanned": len(tokens), "covered": covered, "uncovered": uncovered,
            "source_down": down, "written": added, "refreshed": refreshed,
            "n_events": len(new_entries)}


# ── cli ────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Token-unlock calendar + pre-unlock alerts (SPEC-95)")
    ap.add_argument("ticker", nargs="?", default=None)
    ap.add_argument("--cg-id", default=None)
    ap.add_argument("--horizon", type=int, default=HORIZON_DAYS)
    ap.add_argument("--sweep", action="store_true", help="full-watchlist sweep → catalysts.json")
    ap.add_argument("--no-write", action="store_true", help="sweep without writing catalysts.json")
    ap.add_argument("--fire-alerts", action="store_true", help="fire due T-3d/T-1d inbox alerts")
    ap.add_argument("--op", default=None, help="ticker|sweep|fire-alerts (alias for flags)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    op = a.op or ("sweep" if a.sweep else "fire-alerts" if a.fire_alerts else "ticker")
    if op == "sweep":
        out = sweep(write=not a.no_write)
    elif op == "fire-alerts":
        out = {"fired": fire_unlock_alerts()}
    else:
        if not a.ticker:
            print(json.dumps({"error": "ticker required (or --sweep / --fire-alerts)"}))
            sys.exit(1)
        out = build_unlocks(a.ticker, cg_id=a.cg_id, horizon_days=a.horizon)
    print(json.dumps(out, indent=None if a.json else 2))


if __name__ == "__main__":
    main()
