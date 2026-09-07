#!/usr/bin/env python3
"""drip_seller.py — SPEC-117: drip/DCA-pattern seller detector (evades SPEC-115's per-swap floor).

SPEC-115 flags LARGE single swaps — a per-swap USD floor that programmatic drip
distribution evades by construction. The desk has already eaten this once: the ESPORTS
drip-bot sold ~$530K via micro-swaps individually below any reasonable floor (SPEC-53
fixed the *valuation* — stable-leg first — but nothing *detected* the pattern). This
fingerprints recurring, metronomic same-direction executions per wallet — DEX swaps
and/or CEX-channel deposits — over a trailing window: regular cadence + similar sizes +
a repeating channel + a cumulative USD floor.

  fingerprint_drip(...)        PURE: a single (wallet, direction, channel) execution
                                sequence -> a DRIP_SELLER/DRIP_BUYER hit, or None.
  channel_executions_dex(...)  PURE: reuses dex_execution.size_swap_hits with the
                                per-swap floor disabled (min_usd=0) — the whole point.
  channel_executions_cex(...)  PURE: straight token transfers to a known CEX deposit
                                address, priced off spot (no swap leg to correlate).
  format_drip_line(...)        the EXECUTION (drip): ... render line.
  build_drip(...)              orchestrator: tracked wallets + SPEC-98 fresh children +
                                 (optional) ad-hoc extra_wallets, scanned against known
                                 dex addresses and/or known CEX channels. Provider down /
                                 untracked token / no known addresses -> {available: False}
                                 (§3 — never a clean "no drip" bill).

  python3 capabilities/drip_seller.py LAB --json
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

CONFIG_PATH = ROOT / "config" / "drip_seller.json"

# Documented defaults (SPEC-117) — override any key in config/drip_seller.json.
DEFAULT_CFG = {
    "window_h": 72,               # trailing window a drip sequence is fingerprinted over
    "min_n": 6,                   # minimum same-direction executions to consider a pattern
    "cv_gap_max": 0.35,           # inter-tx gap coefficient of variation ceiling (regular cadence)
    "cv_size_max": 0.5,           # size coefficient of variation ceiling (similar sizes)
    "gap_quantized_tol": 0.15,    # alt cadence test: fraction-of-median tolerance
    "gap_quantized_frac": 0.7,    # ...fraction of gaps that must cluster near the median
    "cumulative_floor_usd": 50_000,  # cumulative USD (via stable/quote leg) to qualify
    "days": 3,                    # transfer-log lookback window (>= window_h/24)
    "young_nonce_max": 50,        # SPEC-98 fresh-wallet fingerprint (reused, not re-derived)
    "max_channels": 3,            # bounded known-CEX-channel reads (quota discipline)
    "max_wallets": 20,            # bounded candidate-wallet reads (quota discipline)
}

SELL, BUY = "sell", "buy"


def load_cfg(path=None):
    cfg = dict(DEFAULT_CFG)
    try:
        d = json.loads(Path(path or CONFIG_PATH).read_text())
        cfg.update({k: v for k, v in d.items() if k in DEFAULT_CFG})
    except Exception:  # noqa: BLE001 — missing/bad config = documented defaults
        pass
    return cfg


def _parse_ts(ts_iso):
    if not ts_iso:
        return None
    if isinstance(ts_iso, (int, float)):
        return float(ts_iso)
    try:
        return datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _fmt_usd(v):
    v = abs(v or 0)
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _fmt_gap(sec):
    if not sec:
        return "?"
    sec = int(sec)
    if sec < 3600:
        return f"{max(sec // 60, 1)}min"
    if sec < 86400:
        h, m = sec // 3600, (sec % 3600) // 60
        return f"{h}h{m:02d}m" if m else f"{h}h"
    return f"{sec // 86400}d"


def _clip_amt(t):
    try:
        return float(t.get("value_decimal") or 0)
    except (TypeError, ValueError):
        return 0.0


def _cv(values):
    """Coefficient of variation: std/mean. None when undefined (< 2 values or mean 0)."""
    if not values or len(values) < 2:
        return None
    mean = sum(values) / len(values)
    if mean == 0:
        return None
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return (var ** 0.5) / mean


def _gap_quantized(gaps, cfg):
    """Alt cadence test for fixed-interval bots whose gap CV still trips on outliers: most
    gaps cluster tightly around the median even if a few are noisy."""
    if not gaps:
        return False
    med = sorted(gaps)[len(gaps) // 2]
    if med <= 0:
        return False
    tol = med * cfg["gap_quantized_tol"]
    within = sum(1 for g in gaps if abs(g - med) <= tol)
    return (within / len(gaps)) >= cfg["gap_quantized_frac"]


# ── pure core: fingerprint a (wallet, direction, channel) execution sequence ───────
def fingerprint_drip(wallet, direction, channel, channel_kind, executions, cfg=None):
    """PURE. `executions` are same-wallet, same-direction, same-channel executions —
    {"usd", "timestamp"} at minimum. Requires >= min_n executions within the trailing
    window with regular cadence (cv_gap or quantized gaps) AND similar sizes (cv_size)
    AND cumulative USD >= the floor. Never guesses a size — executions with usd=None
    (e.g. an unpriced CEX deposit) are dropped before counting."""
    cfg = cfg or DEFAULT_CFG
    execs = sorted([e for e in executions or [] if e.get("usd") is not None and e.get("timestamp")],
                   key=lambda e: _parse_ts(e["timestamp"]) or 0)
    if not execs:
        return None
    last_ts = _parse_ts(execs[-1]["timestamp"])
    cutoff = last_ts - cfg["window_h"] * 3600
    execs = [e for e in execs if (_parse_ts(e["timestamp"]) or 0) >= cutoff]
    n = len(execs)
    if n < cfg["min_n"]:
        return None

    sizes = [e["usd"] for e in execs]
    cumulative = sum(sizes)
    if cumulative < cfg["cumulative_floor_usd"]:
        return None

    tss = [_parse_ts(e["timestamp"]) for e in execs]
    gaps = [b - a for a, b in zip(tss, tss[1:])]
    cv_gap = _cv(gaps)
    cv_size = _cv(sizes)
    cadence_regular = (cv_gap is not None and cv_gap <= cfg["cv_gap_max"]) or _gap_quantized(gaps, cfg)
    size_regular = cv_size is not None and cv_size <= cfg["cv_size_max"]
    if not (cadence_regular and size_regular):
        return None

    median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 0
    span_sec = tss[-1] - tss[0]
    span_days = (span_sec / 86400.0) if span_sec > 0 else None
    run_rate = (cumulative / span_days) if span_days else None
    verdict = "DRIP_SELLER" if direction == SELL else "DRIP_BUYER"
    return {"verdict": verdict, "wallet": wallet.lower(), "direction": direction,
            "channel": channel, "channel_kind": channel_kind,
            "cumulative_usd": round(cumulative, 2), "tx_count": n,
            "median_gap_sec": median_gap, "first_ts": execs[0]["timestamp"],
            "last_ts": execs[-1]["timestamp"],
            "run_rate_usd_per_day": round(run_rate, 2) if run_rate else None,
            "cv_gap": round(cv_gap, 3) if cv_gap is not None else None,
            "cv_size": round(cv_size, 3) if cv_size is not None else None}


# ── channel-level execution extraction ──────────────────────────────────────────
def channel_executions_dex(wallet, outs, ins, quote_ins, quote_outs, dex_addrs, cfg=None):
    """PURE. Every DEX swap by `wallet` regardless of size — SPEC-115's per-swap floor
    is disabled here BY DESIGN (drip evades that floor by construction); reuses
    dex_execution's same-tx quote-leg correlation (SPEC-53) rather than re-deriving it."""
    import dex_execution as DX
    raw = DX.size_swap_hits(wallet, outs, ins, quote_ins, quote_outs, dex_addrs,
                            cfg={**DX.DEFAULT_CFG, "min_usd": 0})
    out = []
    for h in raw:
        h["channel"] = h.pop("pool")
        h["channel_kind"] = "dex-swap"
        out.append(h)
    return out


def channel_executions_cex(wallet, outs, cex_addrs, price=None, cfg=None):
    """PURE. A straight token transfer wallet -> known CEX deposit address — no swap leg
    to correlate (there isn't one). Sized off spot `price`; unpriced deposits still carry
    cadence info but drop out of fingerprint_drip's cumulative test (never guessed)."""
    cex_addrs = {a.lower() for a in (cex_addrs or [])}
    out = []
    for t in outs or []:
        to = (t.get("to_address") or "").lower()
        if to not in cex_addrs:
            continue
        amt = _clip_amt(t)
        usd = amt * price if price else None
        out.append({"direction": SELL, "usd": usd, "channel": to, "channel_kind": "cex-deposit",
                    "tx_hash": t.get("transaction_hash"), "timestamp": t.get("block_timestamp"),
                    "sender": wallet.lower(), "token_amount": amt})
    return out


# ── render ───────────────────────────────────────────────────────────────────────
def format_drip_line(hit):
    """The compact EXECUTION (drip): ... render line — same EXECUTION label as SPEC-115,
    drip-variant wording; distinguishes deposit-drip (positioning cadence) from
    swap-drip (executed)."""
    verb = "sold" if hit["direction"] == SELL else "bought"
    attr = hit.get("attribution") or {}
    tag = ""
    if attr.get("kind") == "tracked":
        tag = f" (cluster: {attr.get('cluster')})"
    elif attr.get("kind") == "fresh_child":
        tag = f" (cluster: {attr.get('cluster')}, fresh child of {attr.get('parent')})"

    first_e, last_e = _parse_ts(hit.get("first_ts")), _parse_ts(hit.get("last_ts"))
    span_txt = f"{round((last_e - first_e) / 3600.0, 1)}h" if first_e is not None and last_e is not None else "?"
    is_swap = hit["channel_kind"] == "dex-swap"
    noun = "swaps" if is_swap else "deposits"
    via = f"via pool {hit['channel']}" if is_swap else f"via CEX deposit {hit['channel']}"
    cadence = _fmt_gap(hit.get("median_gap_sec"))
    rr = f" — run-rate {_fmt_usd(hit['run_rate_usd_per_day'])}/day" if hit.get("run_rate_usd_per_day") else ""
    note = "" if is_swap else " (positioning cadence, not executed)"
    return (f"EXECUTION (drip): {hit['wallet']}{tag} {verb} {_fmt_usd(hit['cumulative_usd'])} "
            f"over {hit['tx_count']} micro-{noun} / {span_txt} (~1 per {cadence}) {via}{rr}{note}")


# ── live wiring (default seams, reused from dex_execution — not re-derived) ────────
def build_drip(ticker, contract=None, chain_key=None, tracked=None, dex_addrs=None, cex_addrs=None,
              extra_wallets=None, transfers_fn=None, nonce_fn=None, quote_fn=None, price=None,
              cfg=None, max_wallets=None):
    """Scan candidate wallets (tracked + SPEC-98 fresh children + ad-hoc extra_wallets) for
    drip patterns via known DEX addresses and/or known CEX deposit channels. Provider down /
    untracked token / no known addresses -> {available: False}, NEVER a clean 'no drip' bill."""
    import dex_execution as DX
    cfg = cfg or load_cfg()
    ticker = ticker.upper()
    if contract is None or chain_key is None or tracked is None:
        try:
            resolved = DX._tracked_for(ticker)
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": f"tracked_wallets.json unreadable: {str(e)[:80]}"}
        if not resolved:
            return {"available": False, "reason": "untracked token (no contract/wallet map)"}
        contract, chain_key, tracked = resolved

    if dex_addrs is None:
        try:
            dex_addrs = DX.known_dex_addresses()
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": f"dex-address config unreadable: {str(e)[:80]}"}
    if cex_addrs is None:
        try:
            import rotation_freshness as RF
            cex_addrs = {c["address"] for c in RF._known_cex_channels(cfg["max_channels"])}
        except Exception:  # noqa: BLE001
            cex_addrs = set()
    if not dex_addrs and not cex_addrs:
        return {"available": False, "reason": "no known dex or CEX channel addresses to scan"}

    if transfers_fn is None:
        try:
            transfers_fn = DX._default_transfers_fn()
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": f"provider seam unavailable: {str(e)[:80]}"}
    if nonce_fn is None:
        try:
            nonce_fn = DX._default_nonce_fn()
        except Exception:  # noqa: BLE001
            nonce_fn = lambda a, ck="binance-smart-chain": None  # noqa: E731
    if quote_fn is None:
        try:
            quote_fn = DX._default_quote_fn()
        except Exception:  # noqa: BLE001
            quote_fn = lambda addr, ck, days: {}  # noqa: E731

    dex_addrs = {a.lower() for a in (dex_addrs or [])}
    cex_addrs = {a.lower() for a in (cex_addrs or [])}

    children = []
    for addr, label in (tracked or {}).items():
        children += DX.discover_fresh_children(addr, label, contract, chain_key, transfers_fn, nonce_fn, cfg)
    child_by_addr = {c["address"]: c for c in children}

    candidates = dict(tracked or {})
    for c in children:
        candidates.setdefault(c["address"], None)
    for addr in (extra_wallets or {}):
        candidates.setdefault(addr.lower(), (extra_wallets.get(addr) if isinstance(extra_wallets, dict) else None))

    max_wallets = max_wallets or cfg["max_wallets"]
    wallet_list = list(candidates)[:max_wallets]

    read_any, groups = False, {}
    for wallet in wallet_list:
        try:
            txs, _src, _partial = transfers_fn(wallet, contract, chain_key, cfg["days"])
        except Exception:  # noqa: BLE001 — a dead wallet read is a coverage gap, not zero flow
            continue
        read_any = True
        outs = [t for t in txs if (t.get("from_address") or "").lower() == wallet.lower()]
        ins = [t for t in txs if (t.get("to_address") or "").lower() == wallet.lower()]
        try:
            qtx = quote_fn(wallet, chain_key, cfg["days"]) or {}
        except Exception:  # noqa: BLE001
            qtx = {}
        quote_ins, quote_outs = [], []
        for asset_txs in qtx.values():
            quote_ins += [t for t in asset_txs if (t.get("to_address") or "").lower() == wallet.lower()]
            quote_outs += [t for t in asset_txs if (t.get("from_address") or "").lower() == wallet.lower()]

        execs = []
        if dex_addrs:
            execs += channel_executions_dex(wallet, outs, ins, quote_ins, quote_outs, dex_addrs, cfg=cfg)
        if cex_addrs:
            execs += channel_executions_cex(wallet, outs, cex_addrs, price=price, cfg=cfg)

        attr = None
        if candidates.get(wallet):
            attr = {"kind": "tracked", "cluster": candidates[wallet], "parent": None}
        elif wallet in child_by_addr:
            c = child_by_addr[wallet]
            attr = {"kind": "fresh_child", "cluster": c.get("parent_label"), "parent": c.get("parent")}

        for e in execs:
            key = (wallet, e["direction"], e["channel"])
            groups.setdefault(key, {"attr": attr, "kind": e["channel_kind"], "execs": []})
            groups[key]["execs"].append(e)

    if not read_any:
        return {"available": False, "reason": "provider unavailable (all wallet reads failed)"}

    hits = []
    for (wallet, direction, channel), g in groups.items():
        hit = fingerprint_drip(wallet, direction, channel, g["kind"], g["execs"], cfg=cfg)
        if hit:
            hit["attribution"] = g["attr"]
            hits.append(hit)

    hits.sort(key=lambda h: h.get("last_ts") or "", reverse=True)
    lines = [format_drip_line(h) for h in hits]
    return {"available": True, "ticker": ticker, "n_hits": len(hits), "hits": hits, "lines": lines}


def main():
    ap = argparse.ArgumentParser(description="SPEC-117 drip/DCA-pattern seller detector")
    ap.add_argument("ticker")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    out = build_drip(args.ticker)
    if args.json:
        print(json.dumps(out))
    elif not out.get("available"):
        print(f"{args.ticker}: unavailable — {out.get('reason')}")
    elif not out["hits"]:
        print(f"{args.ticker}: no qualifying drip hits")
    else:
        for line in out["lines"]:
            print(line)


if __name__ == "__main__":
    main()
