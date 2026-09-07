#!/usr/bin/env python3
"""liqs.py — SPEC-103: per-venue forced-liquidation stream as OI ground truth.

Corpus rule: on demon coins aggregate OI is operator-FAKEABLE (double-open 对敲, multi-
account internal transfer — memory: feedback_aggregate_oi_faked_via_double_open). The BSB
case: OI up + price down + CVD down read as "dealer piling shorts", but post-pull-up short
OI was LOWER than pre-pull — the pile was bait (memory:
feedback_hidden_build_cvd_oi_price_divergence). "When OI is faked you can't read OI — raw
candles + liquidation data are the only truth" (memory:
reference_derq_freeland_corpus_playbook §4) — liq prints cannot be faked the way aggregate
OI can, so they are the cross-check.

Venue reality (live-verified 2026-07-02, this session — build against THIS, not the
ticket's assumed primary):
  Binance USDT-M forceOrders (both `/fapi/v1/forceOrders` — needs an authenticated API key,
    confirmed 401 "API-key format invalid" — and the older `/fapi/v1/allForceOrders` —
    confirmed 400 "The endpoint has been out of maintenance") is DEAD on the free/keyless
    REST path. This matches tape.py's own existing finding (`fetch_force_orders`, SPEC 31).
    A public websocket (`!forceOrder@arr`) remains, but this codebase is stdlib-urllib-only
    (no websocket dependency anywhere) — kept coded as a documented dead seam, not wired.
  Bybit `/v5/market/liquidation` is 404 (removed; a public websocket topic may still exist,
    not explored this pass).
  OKX `/api/v5/public/liquidation-orders` (instType=SWAP, `uly=<TICKER>-USDT`, state=filled)
    is GENUINELY PUBLIC, keyless, and live — confirmed working against a real desk-tracked
    token (OPN) this session, and cleanly errors (`code:"51014"`, empty data) for an
    unlisted underlying. **This is the actual v1 venue** — the ticket names Binance as
    primary but OKX is what actually answers; "single-venue is an acceptable v1 with the
    venue named in output" per the spec's own DoD. See REVIEW-REQUEST-SPEC-103 for the
    live-verification transcript.

Output: `build_liqs(ticker)` -> {available, venue, window_h, bar_minutes, bars, total_*_usd,
oi_liq_consistency}. `bars` = per-bucket {ts, long_usd, short_usd, count, largest_usd,
cum_long_usd, cum_short_usd, liq_silence}. `oi_liq_consistency` cross-checks the caller-
supplied window OI delta against the liq prints in the same window:
  CONFIRMED     — OI move accompanied by matching liq prints (real capitulation/squeeze leg,
                  or a real build corroborated by liq activity / a CVD step).
  SUSPECT_FAKE  — material OI move + liq-silence + no confirming CVD step -> double-open /
                  internal-transfer fingerprint (the BSB pattern).
  TRANSFER      — OI DOWN + liq-silence + flat price -> multi-account profit-transfer
                  fingerprint (corpus playbook §4).
  UNKNOWN       — no liq feed at all (venue down / symbol unlisted) -> NEVER a clean bill on
                  an empty stream (§3 — a zero/null print is a data FAILURE, not a datum).
  None          — the OI move is below the materiality threshold; nothing to corroborate.

  python3 capabilities/liqs.py OPN --json
  python3 capabilities/liqs.py OPN --window 4 --bar 5 --oi-delta -18.0 --json
"""
import argparse
import json
import math
import sys
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG_PATH = ROOT / "config" / "liqs.json"

UA = {"User-Agent": "liqs/1.0"}

DEFAULT_CFG = {
    "material_oi_pct": 10.0,    # an OI move below this magnitude has nothing to corroborate
    "min_confirm_usd": 10_000,  # in-window liq notional below this reads as "silence"
}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        d = json.loads(CONFIG_PATH.read_text())
        cfg.update({k: v for k, v in d.items() if k in DEFAULT_CFG})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def _get(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (URLError, HTTPError, json.JSONDecodeError, TimeoutError, ValueError):
        return None


# ── venue fetchers: each returns a flat list of {ts (epoch s), side ('long'|'short'), usd}
# or None (venue unavailable / symbol unlisted — never an empty list standing in for "no
# liqs", §3: absence-of-answer and answer-of-zero must never look identical). ─────────────
def fetch_liqs_okx(ticker, timeout=10):
    """Genuinely public, keyless (live-verified 2026-07-02 — see module docstring)."""
    uly = f"{ticker.upper()}-USDT"
    url = f"https://www.okx.com/api/v5/public/liquidation-orders?instType=SWAP&uly={uly}&state=filled"
    d = _get(url, timeout=timeout)
    if not isinstance(d, dict) or d.get("code") != "0":
        return None
    events = []
    for batch in d.get("data", []) or []:
        for det in batch.get("details", []) or []:
            try:
                ts = int(det["ts"]) / 1000.0
                px = float(det["bkPx"])
                sz = float(det["sz"])
                pos_side = det["posSide"]
            except (KeyError, TypeError, ValueError):
                continue
            events.append({"ts": ts, "side": "long" if pos_side == "long" else "short",
                           "usd": px * sz})
    return events


def fetch_liqs_binance(ticker, timeout=10):
    """DEAD on the free/keyless REST path (live-confirmed 2026-07-02 — both forceOrders
    endpoints require auth/are deprecated). Kept as a documented seam: a future authed key
    or websocket client can populate this without touching the provider-seam wiring."""
    return None


_PROVIDERS = [
    {"name": "okx", "fetch": fetch_liqs_okx},
    {"name": "binance", "fetch": fetch_liqs_binance},
]


def fetch_liqs(ticker):
    """First provider that answers wins; a provider that raises or returns None is skipped
    silently, exactly like the onchain.py token-flow seam. (None, None) = every provider
    dead/unlisted."""
    for p in _PROVIDERS:
        try:
            events = p["fetch"](ticker)
        except Exception:  # noqa: BLE001 — one venue's failure never sinks the read
            events = None
        if events is not None:
            return events, p["name"]
    return None, None


# ── bucketing ────────────────────────────────────────────────────────────────
def bucket_liqs(events, window_h=4, bar_minutes=5, now=None):
    """events in the trailing `window_h` window, bucketed into `bar_minutes` bars, oldest
    first. Every bar in the window is emitted even if empty (liq_silence:True) — a gap in
    the printed bars would itself misread as "silence wasn't checked there"."""
    now = now if now is not None else time.time()
    window_s = window_h * 3600
    bar_s = bar_minutes * 60
    start = now - window_s
    n_bars = max(1, int(math.ceil(window_s / bar_s)))
    bars = []
    cum_long, cum_short = 0.0, 0.0
    for i in range(n_bars):
        lo = start + i * bar_s
        hi = lo + bar_s
        bucket = [e for e in events if lo <= e["ts"] < hi]
        long_usd = sum(e["usd"] for e in bucket if e["side"] == "long")
        short_usd = sum(e["usd"] for e in bucket if e["side"] == "short")
        largest = max((e["usd"] for e in bucket), default=0.0)
        cum_long += long_usd
        cum_short += short_usd
        bars.append({"ts": lo, "long_usd": round(long_usd, 2), "short_usd": round(short_usd, 2),
                    "count": len(bucket), "largest_usd": round(largest, 2),
                    "cum_long_usd": round(cum_long, 2), "cum_short_usd": round(cum_short, 2),
                    "liq_silence": len(bucket) == 0})
    return bars


# ── OI/liq cross-check verdict ──────────────────────────────────────────────
def oi_liq_consistency(oi_delta_pct, liq_bars, price_flat=None, cvd_confirms=None, cfg=None):
    """See module docstring for the verdict vocabulary. `liq_bars=None` (feed unavailable)
    is checked FIRST and unconditionally returns UNKNOWN — an empty/absent stream must
    never fall through to a "confirmed" or "fake" read (§3)."""
    cfg = dict(DEFAULT_CFG, **(cfg or {}))
    if liq_bars is None:
        return {"verdict": "UNKNOWN", "reason": "liq feed unavailable"}
    if oi_delta_pct is None or abs(oi_delta_pct) < cfg["material_oi_pct"]:
        return {"verdict": None, "reason": "OI move below materiality threshold"}
    total_usd = sum(b["long_usd"] + b["short_usd"] for b in liq_bars)
    silent = total_usd < cfg["min_confirm_usd"]
    if oi_delta_pct < 0:
        if not silent:
            return {"verdict": "CONFIRMED",
                    "reason": f"OI {oi_delta_pct:+.1f}% with ${total_usd:,.0f} matching liq prints in-window"}
        if price_flat:
            return {"verdict": "TRANSFER",
                    "reason": "OI down, liq-silent, price flat — multi-account profit-transfer "
                             "fingerprint (corpus playbook §4)"}
        return {"verdict": "SUSPECT_FAKE",
                "reason": "material OI drop, liq-silent, no confirming prints — "
                         "double-open/internal-transfer fingerprint"}
    # OI up (a build): silence + no CVD confirmation = the BSB-shaped fake-pile signature
    if silent and cvd_confirms is not True:
        return {"verdict": "SUSPECT_FAKE",
                "reason": "material OI build, liq-silent, no matching CVD step — "
                         "double-open/internal-transfer fingerprint (BSB pattern)"}
    return {"verdict": "CONFIRMED",
            "reason": f"OI {oi_delta_pct:+.1f}% with liq activity / CVD confirmation present"}


def one_liner(result):
    """`liq: SUSPECT_FAKE — material OI build, liq-silent...`-shaped line, or None when
    there is nothing material to say (feed unavailable is its own UNKNOWN line, not None —
    only an immaterial OI move suppresses the line entirely)."""
    v = (result or {}).get("oi_liq_consistency") or {}
    verdict = v.get("verdict")
    if not verdict:
        return None
    return f"liq: {verdict} — {v.get('reason', '')}"


# ── top-level build ──────────────────────────────────────────────────────────
def build_liqs(ticker, window_h=4, bar_minutes=5, oi_delta_pct=None, price_flat=None,
              cvd_confirms=None, now=None, fetch_fn=None):
    now = now if now is not None else time.time()
    fetch_fn = fetch_fn or fetch_liqs
    cfg = load_cfg()
    try:
        events, venue = fetch_fn(ticker)
    except Exception as e:  # noqa: BLE001 — a venue exception degrades, never crashes the caller
        events, venue = None, None
    if events is None:
        return {"ticker": ticker.upper(), "available": False,
                "reason": "no liquidation feed available (venue down or symbol unlisted)",
                "venue": venue, "window_h": window_h, "bar_minutes": bar_minutes, "bars": None,
                "oi_liq_consistency": oi_liq_consistency(oi_delta_pct, None, cfg=cfg)}
    window_events = [e for e in events if e["ts"] >= now - window_h * 3600]
    bars = bucket_liqs(window_events, window_h=window_h, bar_minutes=bar_minutes, now=now)
    verdict = oi_liq_consistency(oi_delta_pct, bars, price_flat=price_flat,
                                 cvd_confirms=cvd_confirms, cfg=cfg)
    return {"ticker": ticker.upper(), "available": True, "venue": venue,
            "window_h": window_h, "bar_minutes": bar_minutes, "bars": bars,
            "total_long_usd": round(sum(b["long_usd"] for b in bars), 2),
            "total_short_usd": round(sum(b["short_usd"] for b in bars), 2),
            "oi_liq_consistency": verdict}


def main():
    ap = argparse.ArgumentParser(description="SPEC-103 liqs — per-venue forced-liquidation "
                                             "stream as OI ground truth")
    ap.add_argument("ticker")
    ap.add_argument("--window", type=float, default=4.0, help="window hours (default 4)")
    ap.add_argument("--bar", type=float, default=5.0, help="bar minutes (default 5)")
    ap.add_argument("--oi-delta", type=float, default=None, help="window OI delta %% for the cross-check")
    ap.add_argument("--price-flat", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    r = build_liqs(args.ticker, window_h=args.window, bar_minutes=args.bar,
                   oi_delta_pct=args.oi_delta, price_flat=args.price_flat or None)
    out = {"ok": bool(r.get("available")), "data": r, "meta": {}}
    if args.json:
        print(json.dumps(out, default=str))
    else:
        if not r["available"]:
            print(f"# {r['ticker']} liqs — unavailable ({r['reason']})")
            return
        print(f"# {r['ticker']} liqs  ({r['venue']}, {r['window_h']}h, {r['bar_minutes']}m bars)")
        print(f"total long-liq ${r['total_long_usd']:,.0f}  short-liq ${r['total_short_usd']:,.0f}")
        line = one_liner(r)
        if line:
            print(line)


if __name__ == "__main__":
    main()
