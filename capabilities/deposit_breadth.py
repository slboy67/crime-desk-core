#!/usr/bin/env python3
"""deposit_breadth.py — SPEC-118: deposit-breadth metric (many-wallets → CEX pattern signal).

The desk's on-chain layer is almost entirely subject-level (tracked wallets, named
clusters, verify_wallet on known holders). It has no pattern-level aggregate: a sudden
increase in token deposits to exchanges FROM MANY WALLETS may indicate an upcoming
sell-off (Onchain-Analysis-Workshop-CrimeDesk.md Lesson 8, predictor #2). VELVET is the
desk-native proof: distribution rotated through fresh wallets NOT in the tracked set,
the tracked wallet's clock froze, and the engine was blind. This is deliberately NOT
gated on tracked_wallets — the point is catching senders the desk has never seen.

  classify_breadth(...)   PURE: current-window {senders, usd, median_usd} + a baseline
                           of prior-window sender counts -> {spike, senders, baseline,
                           ratio, usd, median_usd}. Small-sample guard: a minimum
                           absolute sender count gates SPIKE even when a near-zero
                           baseline would otherwise blow the ratio up.
  decompose_senders(...)  PURE: bucket spiking senders tracked / fresh (SPEC-98
                           fingerprint) / unknown — corroboration AFTER detection,
                           never a gate.
  format_breadth_line(...) the BREADTH_SPIKE / breadth: ... render line — numbers
                           always, not just the flag.
  build_breadth(...)      orchestrator: scans known CEX deposit/hot channels for
                           distinct senders within a trailing window, persists the
                           window so the NEXT call has a real baseline. Provider down /
                           no known channels -> {available: False} (§3 — never a clean
                           "no breadth" bill).

  python3 capabilities/deposit_breadth.py LAB --json
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

STATE = ROOT / "state"
CONFIG_PATH = ROOT / "config" / "deposit_breadth.json"

# Documented defaults (SPEC-118) — override any key in config/deposit_breadth.json.
DEFAULT_CFG = {
    "window_h": 24,               # trailing window a breadth read covers
    "baseline_windows": 7,        # how many prior windows form the baseline (mean)
    "spike_multiple": 3.0,        # current senders >= baseline * this = spike candidate
    "usd_floor": 100_000,         # ...AND total USD (stable-leg/spot-priced) >= this
    "min_abs_senders": 8,         # small-sample guard: absolute floor regardless of ratio
    "days": 1,                    # transfer-log lookback (>= window_h/24)
    "young_nonce_max": 50,        # SPEC-98 fresh-wallet fingerprint (reused, not re-derived)
    "max_channels": 3,            # bounded known-CEX-channel reads (quota discipline)
}


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


# ── pure core: classify a current window against a baseline ────────────────────
def classify_breadth(current, baseline_counts, cfg=None):
    """PURE. `current` = {"senders", "usd", "median_usd"} for the trailing window.
    `baseline_counts` = prior-window sender counts (mean = the baseline). SPIKE needs
    BOTH the ratio gate (current >= baseline * spike_multiple, only when a real
    baseline exists) AND the absolute floor (>= min_abs_senders) AND the USD floor —
    the absolute floor is what stops a near-zero baseline from exploding the ratio
    into a false spike on 2 whale-sized senders."""
    cfg = cfg or DEFAULT_CFG
    n = current.get("senders", 0) or 0
    usd = current.get("usd", 0.0) or 0.0
    median_usd = current.get("median_usd", 0.0) or 0.0
    baseline_counts = list(baseline_counts or [])
    baseline = (sum(baseline_counts) / len(baseline_counts)) if baseline_counts else 0.0
    ratio = (n / baseline) if baseline > 0 else None
    spike = bool(n >= cfg["min_abs_senders"] and usd >= cfg["usd_floor"]
                and baseline > 0 and n >= baseline * cfg["spike_multiple"])
    return {"spike": spike, "senders": n, "baseline": baseline, "ratio": ratio,
            "usd": usd, "median_usd": median_usd}


def decompose_senders(sender_addrs, tracked_addrs, fresh_addrs):
    """PURE. Bucket each spiking sender: tracked (already known) / fresh (SPEC-98
    young-nonce fingerprint) / unknown (neither) — corroboration, never a gate (§3 of
    SPEC-118: deliberately NOT gated on tracked_wallets)."""
    tracked_addrs = {(a or "").lower() for a in (tracked_addrs or [])}
    fresh_addrs = {(a or "").lower() for a in (fresh_addrs or [])}
    tracked_n = fresh_n = unknown_n = 0
    for a in sender_addrs or []:
        al = (a or "").lower()
        if al in tracked_addrs:
            tracked_n += 1
        elif al in fresh_addrs:
            fresh_n += 1
        else:
            unknown_n += 1
    return {"tracked": tracked_n, "fresh": fresh_n, "unknown": unknown_n}


# ── render ───────────────────────────────────────────────────────────────────────
def format_breadth_line(result):
    """Numbers always — not just the flag (SPEC-118 point 2)."""
    label = "BREADTH_SPIKE" if result.get("spike") else "breadth"
    baseline = result.get("baseline") or 0
    decomp = result.get("decomposition")
    dtxt = ""
    if decomp:
        dtxt = (f" ({decomp.get('tracked', 0)} tracked, {decomp.get('fresh', 0)} fresh, "
                f"{decomp.get('unknown', 0)} unknown)")
    return (f"{label}: senders {result.get('senders', 0)} (baseline {baseline:.0f}) · "
            f"{_fmt_usd(result.get('usd'))} · median {_fmt_usd(result.get('median_usd'))}{dtxt}")


# ── live wiring (default seams) ─────────────────────────────────────────────────
def _window_stats(contract, chain_key, channels, transfers_fn, window_h, now, price, cfg):
    """Live seam: distinct senders depositing `contract` into any of `channels` within
    the trailing `window_h`. Returns None (not a zero-stats dict) when every channel
    read fails — a coverage gap is never a clean 'no breadth' bill (§3)."""
    cutoff = now - window_h * 3600
    by_sender, read_any = {}, False
    for ch in (channels or [])[:cfg["max_channels"]]:
        ch_addr = (ch.get("address") or "").lower()
        try:
            txs, _src, _partial = transfers_fn(ch_addr, contract, chain_key, cfg["days"])
        except Exception:  # noqa: BLE001 — a dead channel read is a coverage gap, not zero flow
            continue
        read_any = True
        for t in txs or []:
            to = (t.get("to_address") or "").lower()
            frm = (t.get("from_address") or "").lower()
            if to != ch_addr or not frm:
                continue
            ts = _parse_ts(t.get("block_timestamp"))
            if ts is not None and ts < cutoff:
                continue
            by_sender.setdefault(frm, 0.0)
            by_sender[frm] += float(t.get("value_decimal") or 0.0)
    if not read_any:
        return None
    sender_addrs = list(by_sender.keys())
    amounts = [by_sender[a] for a in sender_addrs]
    usd_per_sender = [a * price for a in amounts] if price else [0.0] * len(amounts)
    total_usd = sum(usd_per_sender)
    median_usd = sorted(usd_per_sender)[len(usd_per_sender) // 2] if usd_per_sender else 0.0
    return {"senders": len(sender_addrs), "usd": total_usd, "median_usd": median_usd,
            "sender_addrs": sender_addrs}


def _state_path(ticker, state_dir=None):
    return Path(state_dir or STATE) / f"deposit_breadth_{ticker.upper()}.json"


def _read_history(ticker, state_dir=None):
    try:
        return json.loads(_state_path(ticker, state_dir).read_text()).get("windows", [])
    except Exception:  # noqa: BLE001
        return []


def _write_history(ticker, windows, state_dir=None):
    try:
        p = _state_path(ticker, state_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"ticker": ticker.upper(), "windows": windows}))
    except Exception:  # noqa: BLE001 — persistence must never break the read
        pass


def build_breadth(ticker, contract=None, chain_key=None, channels=None, tracked_addrs=None,
                  transfers_fn=None, nonce_fn=None, price=None, now=None, state_dir=None, cfg=None):
    """Scan known CEX deposit/hot channels for distinct senders within the trailing
    window; classify against the persisted baseline (prior windows for this token);
    decompose senders tracked/fresh/unknown; persist the new window. Provider down /
    no known channels -> {available: False}, NEVER a clean 'no breadth' bill (§3)."""
    cfg = cfg or load_cfg()
    ticker = ticker.upper()
    now = now if now is not None else time.time()

    if contract is None or chain_key is None or tracked_addrs is None:
        try:
            import dex_execution as DX
            resolved = DX._tracked_for(ticker)
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": f"tracked_wallets.json unreadable: {str(e)[:80]}"}
        if resolved:
            r_contract, r_chain, r_tracked = resolved
            contract = contract or r_contract
            chain_key = chain_key or r_chain
            if tracked_addrs is None:
                tracked_addrs = set(r_tracked.keys())
        if contract is None or chain_key is None:
            return {"available": False, "reason": "untracked token (no contract/chain)"}
    tracked_addrs = {(a or "").lower() for a in (tracked_addrs or set())}

    if channels is None:
        try:
            import rotation_freshness as RF
            channels = RF._known_cex_channels(cfg["max_channels"])
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": f"CEX-channel config unreadable: {str(e)[:80]}"}
    if not channels:
        return {"available": False, "reason": "no known CEX channels to scan"}

    if transfers_fn is None:
        try:
            import onchain as _oc
            transfers_fn = lambda a, c, ck, days=cfg["days"]: _oc.token_transfers(a, c, ck, days=days)  # noqa: E731
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": f"provider seam unavailable: {str(e)[:80]}"}
    if nonce_fn is None:
        try:
            import onchain as _oc
            nonce_fn = _oc.nonce_of
        except Exception:  # noqa: BLE001
            nonce_fn = lambda a, ck="binance-smart-chain": None  # noqa: E731

    stats = _window_stats(contract, chain_key, channels, transfers_fn, cfg["window_h"], now, price, cfg)
    if stats is None:
        return {"available": False, "reason": "provider unavailable (all channel reads failed)"}

    history = _read_history(ticker, state_dir)
    baseline_counts = [w.get("senders", 0) for w in history]
    verdict = classify_breadth(stats, baseline_counts, cfg=cfg)

    fresh_addrs = set()
    for addr in stats["sender_addrs"]:
        if addr in tracked_addrs:
            continue
        try:
            n = nonce_fn(addr)
        except Exception:  # noqa: BLE001
            n = None
        if n is not None and n <= cfg["young_nonce_max"]:
            fresh_addrs.add(addr)
    decomposition = decompose_senders(stats["sender_addrs"], tracked_addrs, fresh_addrs)
    verdict["decomposition"] = decomposition

    history = (history + [{"ts": now, "senders": stats["senders"]}])[-cfg["baseline_windows"]:]
    _write_history(ticker, history, state_dir)

    return {"available": True, "ticker": ticker, "spike": verdict["spike"],
            "senders": verdict["senders"], "baseline": verdict["baseline"], "ratio": verdict["ratio"],
            "usd": verdict["usd"], "median_usd": verdict["median_usd"], "decomposition": decomposition,
            "sender_addrs": stats["sender_addrs"][:20], "line": format_breadth_line(verdict)}


def main():
    ap = argparse.ArgumentParser(description="SPEC-118 deposit-breadth metric")
    ap.add_argument("ticker")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    out = build_breadth(args.ticker)
    if args.json:
        print(json.dumps(out))
    elif not out.get("available"):
        print(f"{args.ticker}: unavailable — {out.get('reason')}")
    else:
        print(out["line"])


if __name__ == "__main__":
    main()
