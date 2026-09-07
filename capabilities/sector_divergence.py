#!/usr/bin/env python3
"""sector_divergence.py — SOLO_PUMP vs SECTOR_MOVE Cat-A prior (SPEC-114).

Onchain-Analysis-Workshop-CrimeDesk.md Lesson 10's cheapest Cat A first-pass test:
one coin pumping vertically while its sector/narrative basket sits flat is a
manipulation prior (LAB/ESPORTS/PIPPIN shape); a sector-wide rise is liquidity
rotation, not operator action. This module is the pure verdict core (fixture-
testable, no network) plus a CoinGecko fetch seam behind an injectable callable
so classify/triage/screener can each supply the token return they already hold
and never re-derive the basket math by hand.

  classify_divergence(token_ret, stats, thresholds)  PURE core verdict.
  basket_stats(rets)                                 PURE median/breadth/n.
  evaluate_token(ticker, token_ret, ...)              config-driven, multi-basket.
  tag_for(ticker, token_ret, ...)                      integration seam: None when
                                                       UNMAPPED (caller adds no key).
  format_tag(entry)                                    the compact row/board string.

Verdicts: SOLO_PUMP | SECTOR_MOVE | MIXED | UNMAPPED | UNAVAILABLE. A basket fetch
failure is a data FAILURE (§3) — UNAVAILABLE, never rendered as basket-flat.

This is a PRIOR, not a gate (§0.6): it re-ranks/annotates, it never suppresses a
name and never writes thesis/state.

  python3 capabilities/sector_divergence.py LAB --token-ret 80 --json
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CONFIG_PATH = REPO / "config" / "sector_baskets.json"
CG = "https://api.coingecko.com/api/v3"
UA = "Mozilla/5.0 (sector_divergence.py)"

SOLO_PUMP   = "SOLO_PUMP"
SECTOR_MOVE = "SECTOR_MOVE"
MIXED       = "MIXED"
UNMAPPED    = "UNMAPPED"
UNAVAILABLE = "UNAVAILABLE"

# Documented defaults (SPEC-114) — override any key in config/sector_baskets.json.
DEFAULT_THRESHOLDS = {
    "window_days": 7,
    "solo_min_token_ret": 40.0,
    "solo_max_basket_ret": 10.0,
    "sector_min_basket_ret": 15.0,
    "sector_inline_multiple": 2.0,
    "min_basket_size": 4,
}


def load_config(path=None):
    """{"thresholds": {...merged over defaults...}, "baskets": {...}, "token_baskets": {...}}.
    Missing/bad config → defaults + empty maps (degrade gracefully, never raise)."""
    cfg = {"thresholds": dict(DEFAULT_THRESHOLDS), "baskets": {}, "token_baskets": {}}
    try:
        d = json.loads(Path(path or CONFIG_PATH).read_text())
    except Exception:  # noqa: BLE001
        return cfg
    cfg["thresholds"].update(d.get("thresholds") or {})
    cfg["baskets"] = d.get("baskets") or {}
    cfg["token_baskets"] = {k.upper(): v for k, v in (d.get("token_baskets") or {}).items()}
    return cfg


# ── pure core ────────────────────────────────────────────────────────────────
def basket_stats(rets):
    """PURE: list of numeric % returns -> {median, breadth, n}. Empty -> None (a data
    failure — the caller must not treat an empty list as a flat basket)."""
    rets = [r for r in (rets or []) if r is not None]
    if not rets:
        return None
    s = sorted(rets)
    n = len(s)
    mid = n // 2
    median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
    breadth = sum(1 for x in rets if x > 0) / n
    return {"median": median, "breadth": breadth, "n": n}


def classify_divergence(token_ret, stats, thresholds=None):
    """PURE verdict core. `stats` = basket_stats(...) output or None (fetch failure ⇒
    UNAVAILABLE — never rendered as basket-flat, §3). Returns a flat dict merge-ready
    for format_tag (add `label`/`window_days`/`basket` at the call site)."""
    t = dict(DEFAULT_THRESHOLDS)
    t.update(thresholds or {})
    if stats is None:
        return {"verdict": UNAVAILABLE, "token_ret": token_ret, "basket_median_ret": None,
                "breadth": None, "n": 0, "divergence": None,
                "caveat": "basket data unavailable"}

    median, breadth, n = stats["median"], stats.get("breadth"), stats["n"]
    divergence = token_ret - median
    caveat = f"small basket (n={n})" if n < t["min_basket_size"] else None

    if caveat is not None:
        verdict = MIXED
    elif token_ret >= t["solo_min_token_ret"] and abs(median) <= t["solo_max_basket_ret"]:
        verdict = SOLO_PUMP
    elif median >= t["sector_min_basket_ret"] and token_ret <= t["sector_inline_multiple"] * median:
        verdict = SECTOR_MOVE
    else:
        verdict = MIXED

    return {"verdict": verdict, "token_ret": token_ret, "basket_median_ret": median,
            "breadth": breadth, "n": n, "divergence": divergence, "caveat": caveat}


def format_tag(entry):
    """entry: classify_divergence(...) output + label/window_days (+ optional basket
    name). Returns the compact annotation string, or None for UNMAPPED (caller decides
    whether to surface UNMAPPED at all — see tag_for)."""
    if entry is None or entry.get("verdict") == UNMAPPED:
        return None
    label = entry.get("label", "?")
    wd = entry.get("window_days", DEFAULT_THRESHOLDS["window_days"])
    if entry["verdict"] == UNAVAILABLE:
        return f"sector: UNAVAILABLE ({entry.get('caveat') or 'basket data fetch failed'})"
    tok, med = entry.get("token_ret"), entry.get("basket_median_ret")
    tok_s = f"{tok:+.0f}%" if tok is not None else "—"
    med_s = f"{med:+.0f}%" if med is not None else "—"
    out = f"sector: {entry['verdict']} ({tok_s} vs {label} {med_s}, {wd}d)"
    if entry["verdict"] == SECTOR_MOVE:
        out += " — sector beta, not operator action"
    if entry.get("caveat"):
        out += f" [{entry['caveat']}]"
    return out


# ── network seam ─────────────────────────────────────────────────────────────
def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _ret_field(window_days):
    if window_days <= 1:
        return "price_change_percentage_24h_in_currency"
    if window_days <= 7:
        return "price_change_percentage_7d_in_currency"
    if window_days <= 14:
        return "price_change_percentage_14d_in_currency"
    return "price_change_percentage_30d_in_currency"


def fetch_basket_returns(basket_def, window_days, http_fetch=None, pages=1):
    """Network seam (injectable): basket_def -> list[float] of member % returns for
    window_days. Raises on failure/empty — the caller (evaluate_token) converts that
    into an explicit UNAVAILABLE verdict, never a silent flat basket (§3)."""
    http_fetch = http_fetch or _http_get
    field = _ret_field(window_days)
    rets = []
    if basket_def.get("cg_category"):
        for p in range(1, pages + 1):
            url = (f"{CG}/coins/markets?vs_currency=usd&category={basket_def['cg_category']}"
                   f"&order=market_cap_desc&per_page=250&page={p}"
                   f"&price_change_percentage=24h,7d,14d,30d")
            rows = http_fetch(url)
            if not isinstance(rows, list):
                raise RuntimeError(f"unexpected CG response for category={basket_def['cg_category']}")
            rets += [r.get(field) for r in rows if r.get(field) is not None]
    elif basket_def.get("cg_ids"):
        ids = ",".join(basket_def["cg_ids"])
        url = (f"{CG}/coins/markets?vs_currency=usd&ids={ids}"
               f"&order=market_cap_desc&per_page=250&page=1&price_change_percentage=24h,7d,14d,30d")
        rows = http_fetch(url)
        if not isinstance(rows, list):
            raise RuntimeError(f"unexpected CG response for ids={ids}")
        rets += [r.get(field) for r in rows if r.get(field) is not None]
    else:
        raise RuntimeError("basket definition needs cg_category or cg_ids for a live fetch")
    if not rets:
        raise RuntimeError(f"no basket members returned a {field} value")
    return rets


# ── config-driven, multi-basket ─────────────────────────────────────────────
def evaluate_token(ticker, token_ret, config=None, http_fetch=None, window_days=None):
    """Evaluate every basket a token is mapped to (a token may belong to more than
    one, §2) and report the strongest-signal verdict alongside per-basket detail.
    Priority when baskets disagree: SOLO_PUMP > SECTOR_MOVE > MIXED > UNAVAILABLE.
    Unmapped tokens get an explicit UNMAPPED verdict — never silently SOLO."""
    cfg = config or load_config()
    thresholds = cfg["thresholds"]
    wd = window_days or thresholds["window_days"]
    names = cfg["token_baskets"].get(ticker.upper())
    if not names:
        return {"ticker": ticker.upper(), "verdict": UNMAPPED, "window_days": wd,
                "strongest": None, "baskets": []}

    results = []
    for name in names:
        bdef = cfg["baskets"].get(name)
        if not bdef:
            continue
        label = bdef.get("label", name)
        try:
            rets = fetch_basket_returns(bdef, wd, http_fetch=http_fetch)
            stats = basket_stats(rets)
            entry = classify_divergence(token_ret, stats, thresholds)
        except Exception as e:  # noqa: BLE001 — a failed fetch is UNAVAILABLE, never an exception
            entry = {"verdict": UNAVAILABLE, "token_ret": token_ret, "basket_median_ret": None,
                     "breadth": None, "n": 0, "divergence": None,
                     "caveat": f"basket fetch failed: {str(e)[:100]}"}
        entry.update({"basket": name, "label": label, "window_days": wd})
        results.append(entry)

    if not results:
        return {"ticker": ticker.upper(), "verdict": UNMAPPED, "window_days": wd,
                "strongest": None, "baskets": []}

    order = {SOLO_PUMP: 0, SECTOR_MOVE: 1, MIXED: 2, UNAVAILABLE: 3}
    best = min(results, key=lambda r: order.get(r["verdict"], 9))
    return {"ticker": ticker.upper(), "verdict": best["verdict"], "window_days": wd,
            "strongest": best, "baskets": results}


def tag_for(ticker, token_ret, config=None, http_fetch=None, window_days=None):
    """Integration seam for classify/triage/screener. Returns None for UNMAPPED (the
    caller adds NO key — regression: unmapped rows stay byte-identical) or
    {verdict, annotation, cat_a_bump, detail}. Never raises — a total failure
    degrades to an UNAVAILABLE tag, not a dropped row."""
    try:
        result = evaluate_token(ticker, token_ret, config=config,
                                http_fetch=http_fetch, window_days=window_days)
    except Exception as e:  # noqa: BLE001
        wd = window_days or DEFAULT_THRESHOLDS["window_days"]
        entry = {"verdict": UNAVAILABLE, "caveat": f"sector read failed: {str(e)[:100]}",
                  "label": "?", "window_days": wd}
        return {"verdict": UNAVAILABLE, "annotation": format_tag(entry),
                "cat_a_bump": False, "detail": None}
    if result["verdict"] == UNMAPPED:
        return None
    return {"verdict": result["verdict"], "annotation": format_tag(result["strongest"]),
            "cat_a_bump": result["verdict"] == SOLO_PUMP, "detail": result}


def main():
    ap = argparse.ArgumentParser(description="SPEC-114 sector-divergence Cat A pre-classifier")
    ap.add_argument("ticker")
    ap.add_argument("--token-ret", type=float, required=True,
                    help="the token's own %% return for the window (caller-supplied — "
                         "screener/triage/classify already hold this)")
    ap.add_argument("--window-days", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    out = evaluate_token(args.ticker, args.token_ret, window_days=args.window_days)
    if args.json:
        print(json.dumps(out))
    else:
        tag = format_tag(out["strongest"]) if out["strongest"] else None
        print(tag or f"{out['ticker']}: {out['verdict']}")


if __name__ == "__main__":
    main()
