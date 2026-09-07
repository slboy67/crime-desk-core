#!/usr/bin/env python3
"""kr_listings.py — SPEC-153: Korean listing/delisting tripwire (Upbit + Bithumb,
keyless, verified 2026-08-20 desk-side — the Cowork 403 on Upbit announcements was
Cowork's datacenter IP, a home IP passes).

Automates a signature the desk already hunts by hand: the UB/CAP listing-pump class
arrived via the user's manual feed 2026-08-06. A listing NOTICE precedes the listing
itself, so the notice — not the market-code diff — is the earliest tradeable signal.
The inverse is free: a delisting notice (거래지원 종료) is early warning of the B3
single-venue-isolation risk on tracked names. Bonus: Upbit's own caution flags include
CONCENTRATION_OF_SMALL_ACCOUNTS — a crime-desk fingerprint straight from the exchange.

Three keyless sources, each independently degradable (§3: a dead source is LOUD, never
a silently smaller sweep):
  1. GET api.upbit.com/v1/market/all?is_details=true   — every KRW market + caution flags
  2. GET api-manager.upbit.com/api/v1/announcements     — structured notices (may 403)
  3. GET feed-api.bithumb.com/v1/notices                — keyless JSON notices

Event classes (`sweep()`'s `events` list), each carrying its own severity + `page` flag:
  KR_LISTING_NOTICE    HIGH  — always; this is how the desk finds NEW listing-pump
                                names, not just tracks old ones (req 3).
  KR_LISTED             MED  — market code appeared in the Upbit diff (no notice caught it).
  KR_DELISTING_NOTICE  HIGH if the symbol is a tracked desk ticker, else MED.
  KR_FLAG_CHANGE         MED — a caution/warning flag flipped; log-only, never pages.
`page` is true only for KR_LISTING_NOTICE / KR_DELISTING_NOTICE on a tracked-or-perp-
listed symbol (req 7) — ops/discovery_tick.sh pages those through ops/notify.sh, the
rest ride the JSON feed inbox-only.

First run seeds the baseline and emits zero events (the deposit_breadth cold-start
rule) — there is nothing to diff against yet.

  python3 capabilities/kr_listings.py sweep --json
  python3 capabilities/kr_listings.py status --json
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

STATE = ROOT / "state"
BASELINE_NAME = "kr_listings_baseline.json"

UPBIT_MARKET_ALL_URL = "https://api.upbit.com/v1/market/all?is_details=true"
UPBIT_ANNOUNCEMENTS_URL = ("https://api-manager.upbit.com/api/v1/announcements"
                           "?os=web&page=1&per_page=20&category=trade")
BITHUMB_NOTICES_URL = "https://feed-api.bithumb.com/v1/notices?count=20"

MAX_SEEN_NOTICES = 500          # bounded history — dedupes without growing forever

# 종료 ("ends") must be checked BEFORE the listing keywords — "거래지원 종료" contains
# "거래지원" (listing support) but means the OPPOSITE (delisting).
_DELISTING_KEYWORDS = ("종료",)
_LISTING_KEYWORDS = ("거래지원", "마켓 추가", "상장")
_SYMBOL_RE = re.compile(r"\(([A-Za-z0-9]{2,15})\)")


# ── pure: market-code / notice parsing ──────────────────────────────────────────

def krw_market_to_symbol(market_code):
    """'KRW-BTC' -> 'BTC'. Anything not a KRW-prefixed market code -> None."""
    if not market_code or not str(market_code).upper().startswith("KRW-"):
        return None
    return market_code.split("-", 1)[1].upper()


def parse_market_all(rows):
    """Raw market/all (is_details=true) response -> {market_code: {warning, caution,
    korean_name, english_name}}, KRW markets only (spec scope)."""
    out = {}
    for row in rows or []:
        code = row.get("market") or ""
        if not code.upper().startswith("KRW-"):
            continue
        ev = row.get("market_event") or {}
        caution = ev.get("caution") or {}
        out[code] = {
            "warning": bool(ev.get("warning")),
            "caution": {k: bool(v) for k, v in caution.items()},
            "korean_name": row.get("korean_name"),
            "english_name": row.get("english_name"),
        }
    return out


def diff_new_listings(baseline_markets, current_markets):
    """Market codes present in `current_markets` but not `baseline_markets`, sorted."""
    baseline_codes = set((baseline_markets or {}).keys())
    return sorted(set((current_markets or {}).keys()) - baseline_codes)


def diff_flag_changes(baseline_markets, current_markets):
    """Warning/caution flags that flipped on a market present in BOTH baselines (a
    brand-new market's flags are reported via KR_LISTED, not KR_FLAG_CHANGE)."""
    baseline_markets = baseline_markets or {}
    changes = []
    for code, cur in (current_markets or {}).items():
        base = baseline_markets.get(code)
        if base is None:
            continue
        if bool(cur.get("warning")) != bool(base.get("warning")):
            changes.append({"market": code, "flag": "warning",
                            "old": bool(base.get("warning")), "new": bool(cur.get("warning"))})
        cur_c, base_c = cur.get("caution") or {}, base.get("caution") or {}
        for flag in sorted(set(cur_c) | set(base_c)):
            ov, nv = bool(base_c.get(flag)), bool(cur_c.get(flag))
            if ov != nv:
                changes.append({"market": code, "flag": flag, "old": ov, "new": nv})
    return changes


def classify_notice_title(title):
    """'LISTING' | 'DELISTING' | None by keyword. 종료 (ends) wins over 거래지원 —
    '거래지원 종료' contains the listing keyword but means the opposite."""
    t = title or ""
    if any(k in t for k in _DELISTING_KEYWORDS):
        return "DELISTING"
    if any(k in t for k in _LISTING_KEYWORDS):
        return "LISTING"
    return None


def extract_symbol_from_title(title):
    """Best-effort ticker out of a parenthesized notice title ('유비(UB) 마켓 ...' ->
    'UB'). No parenthesized token -> None (req 6: unmapped, not a crash)."""
    m = _SYMBOL_RE.search(title or "")
    return m.group(1).upper() if m else None


def parse_upbit_announcements(data):
    """Tolerant of {"data":{"list":[...]}} (the live shape) or a bare list."""
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        d = data.get("data") if isinstance(data.get("data"), dict) else data
        rows = d.get("list") or d.get("notices") or []
    else:
        rows = []
    return [{"id": r.get("id"), "title": r.get("title") or "",
            "ts": r.get("listed_at") or r.get("created_at")} for r in rows]


def parse_bithumb_notices(data):
    """Tolerant of {"data":[...]} (the live shape) or a bare list."""
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("data") or data.get("list") or []
    else:
        rows = []
    return [{"id": r.get("id") or r.get("notice_id"), "title": r.get("title") or "",
            "ts": r.get("date") or r.get("created_at")} for r in rows]


# ── event builders ───────────────────────────────────────────────────────────────

def _tracked_set(tracked_tickers_fn):
    try:
        return {(t or "").upper() for t in (tracked_tickers_fn() or []) if t}
    except Exception:  # noqa: BLE001 — a broken tracked-list read must never crash the sweep
        return set()


def _probe_perp(symbol, perp_listed_fn):
    if not symbol:
        return None
    try:
        return perp_listed_fn(symbol)
    except Exception:  # noqa: BLE001 — §3: a probe failure is unknown, never False
        return None


def build_listed_event(market_code, tracked, perp_listed_fn):
    symbol = krw_market_to_symbol(market_code)
    is_tracked = bool(symbol and symbol in tracked)
    perp_listed = _probe_perp(symbol, perp_listed_fn)
    return {"class": "KR_LISTED", "severity": "MED", "source": "upbit_market_all",
            "market": market_code, "ticker": symbol, "tracked": is_tracked,
            "perp_listed": perp_listed, "page": False, "unmapped": symbol is None,
            "msg": (f"KR_LISTED {symbol or market_code} — new Upbit KRW market"
                   + (" [perp-listed]" if perp_listed else ""))}


def build_flag_event(change, tracked, perp_listed_fn):
    symbol = krw_market_to_symbol(change["market"])
    is_tracked = bool(symbol and symbol in tracked)
    perp_listed = _probe_perp(symbol, perp_listed_fn)
    return {"class": "KR_FLAG_CHANGE", "severity": "MED", "source": "upbit_market_all",
            "market": change["market"], "flag": change["flag"], "old": change["old"],
            "new": change["new"], "ticker": symbol, "tracked": is_tracked,
            "perp_listed": perp_listed, "page": False, "unmapped": symbol is None,
            "msg": f"KR_FLAG_CHANGE {symbol or change['market']}: {change['flag']} "
                  f"{change['old']}→{change['new']}"}


def build_notice_event(source, notice, tracked, perp_listed_fn):
    """None when the title matches neither the listing nor delisting keyword set
    (not every notice is a listing/delisting notice — most aren't)."""
    title = notice.get("title") or ""
    kind = classify_notice_title(title)
    if kind is None:
        return None
    symbol = extract_symbol_from_title(title)
    is_tracked = bool(symbol and symbol in tracked)
    perp_listed = _probe_perp(symbol, perp_listed_fn)
    if kind == "LISTING":
        cls, sev = "KR_LISTING_NOTICE", "HIGH"
    else:
        cls, sev = "KR_DELISTING_NOTICE", ("HIGH" if is_tracked else "MED")
    page = bool(is_tracked or perp_listed)
    return {"class": cls, "severity": sev, "source": source, "ticker": symbol,
            "tracked": is_tracked, "perp_listed": perp_listed, "page": page,
            "unmapped": symbol is None, "title": title, "notice_id": notice.get("id"),
            "msg": f"{cls} {symbol or '?'}: {title}"
                  + (" [tracked]" if is_tracked else "")
                  + (" [perp-listed]" if perp_listed else "")}


# ── state (baseline persistence) ────────────────────────────────────────────────

def _baseline_path(state_dir=None):
    return Path(state_dir or STATE) / BASELINE_NAME


def _read_baseline(state_dir=None):
    try:
        return json.loads(_baseline_path(state_dir).read_text())
    except Exception:  # noqa: BLE001
        return None


def _write_baseline(baseline, state_dir=None):
    try:
        p = _baseline_path(state_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(baseline))
        tmp.replace(p)
    except OSError:  # noqa: BLE001 — persistence must never break the read
        pass


# ── live wiring (default seams) ─────────────────────────────────────────────────

def _http_get_json(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "crime-desk"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _default_market_all_fetch():
    return _http_get_json(UPBIT_MARKET_ALL_URL)


def _default_announcements_fetch():
    # A browser User-Agent is required (spec source 2 note) — Cloudflare blocks the
    # bare default urllib UA more aggressively than a browser-shaped one.
    return _http_get_json(UPBIT_ANNOUNCEMENTS_URL,
                          headers={"User-Agent": "Mozilla/5.0 (crime-desk kr_listings)"})


def _default_bithumb_fetch():
    return _http_get_json(BITHUMB_NOTICES_URL)


def _watchlist_tickers():
    try:
        raw = json.loads((ROOT / "config" / "watchlist.json").read_text())
        tokens = raw.get("tokens", raw) if isinstance(raw, dict) else raw
        return [t.get("ticker") for t in tokens if t.get("ticker")]
    except Exception:  # noqa: BLE001
        return []


def _default_perp_listed_fn():
    """Binance-perp cross-reference (req 4), cached ONCE per sweep call — degrades to
    an always-unknown probe (never False) if the live fetch/import fails, per §3."""
    cache = {}

    def _fn(symbol):
        if "symbols" not in cache:
            try:
                import screener as SC
                cache["symbols"] = SC.binance_perps() or None
            except Exception:  # noqa: BLE001
                cache["symbols"] = None
        symbols = cache["symbols"]
        if not symbols:
            return None
        return symbol.upper() in symbols

    return _fn


# ── orchestrator ─────────────────────────────────────────────────────────────────

def sweep(now=None, state_dir=None, fetch_market_all=None, fetch_announcements=None,
         fetch_bithumb=None, tracked_tickers_fn=None, perp_listed_fn=None):
    """Poll all three sources, diff against the persisted baseline, emit events. Each
    source degrades independently (req 6/§3) — a dead source is named in
    `degraded_sources`, never silently folded into an empty/smaller sweep."""
    now = now if now is not None else time.time()
    fetch_market_all = fetch_market_all or _default_market_all_fetch
    fetch_announcements = fetch_announcements or _default_announcements_fetch
    fetch_bithumb = fetch_bithumb or _default_bithumb_fetch
    tracked_tickers_fn = tracked_tickers_fn or _watchlist_tickers
    perp_listed_fn = perp_listed_fn or _default_perp_listed_fn()

    baseline = _read_baseline(state_dir)
    first_run = baseline is None
    baseline = baseline or {"markets": {}, "seen_notices": []}
    tracked = _tracked_set(tracked_tickers_fn)

    degraded = []
    events = []

    # ── source 1: market/all diff ────────────────────────────────────────────────
    try:
        raw_market = fetch_market_all()
    except Exception as e:  # noqa: BLE001
        raw_market = None
        degraded.append({"source": "upbit_market_all", "reason": f"fetch failed: {str(e)[:120]}"})
    if raw_market is not None:
        current_markets = parse_market_all(raw_market)
        if not first_run:
            for code in diff_new_listings(baseline["markets"], current_markets):
                events.append(build_listed_event(code, tracked, perp_listed_fn))
            for change in diff_flag_changes(baseline["markets"], current_markets):
                events.append(build_flag_event(change, tracked, perp_listed_fn))
        baseline["markets"] = current_markets
    # raw_market is None (source dead) -> baseline["markets"] left untouched; the
    # NEXT successful fetch diffs against the last-known-good state, never a wipe.

    # ── sources 2+3: notices, deduped against the persisted seen-set ────────────────
    seen = set(baseline.get("seen_notices") or [])
    new_seen = set(seen)

    def _run_notice_source(name, fetch_fn, parse_fn):
        try:
            raw = fetch_fn()
        except Exception as e:  # noqa: BLE001
            degraded.append({"source": name, "reason": f"fetch failed: {str(e)[:120]}"})
            return
        for n in parse_fn(raw):
            key = f"{name}:{n.get('id') if n.get('id') is not None else n.get('title')}"
            if key in new_seen:
                continue
            new_seen.add(key)
            if first_run:
                continue  # seed-only — nothing to compare the first notice batch against
            ev = build_notice_event(name, n, tracked, perp_listed_fn)
            if ev:
                events.append(ev)

    _run_notice_source("upbit_announcements", fetch_announcements, parse_upbit_announcements)
    _run_notice_source("bithumb_notices", fetch_bithumb, parse_bithumb_notices)

    baseline["seen_notices"] = sorted(new_seen)[-MAX_SEEN_NOTICES:]
    _write_baseline(baseline, state_dir)

    return {"ok": True, "first_run": first_run, "events": events,
            "degraded_sources": degraded, "ts": now}


def status(state_dir=None):
    """Render the current persisted baseline — no network."""
    baseline = _read_baseline(state_dir) or {}
    markets = baseline.get("markets") or {}
    return {"ok": True, "market_count": len(markets),
            "warning_count": sum(1 for m in markets.values() if m.get("warning")),
            "seen_notice_count": len(baseline.get("seen_notices") or [])}


def main():
    ap = argparse.ArgumentParser(description="SPEC-153 Korean listing/delisting tripwire")
    ap.add_argument("mode", choices=["sweep", "status"], nargs="?", default="sweep")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    out = sweep() if args.mode == "sweep" else status()
    if args.json:
        print(json.dumps(out))
    else:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
