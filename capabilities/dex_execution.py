#!/usr/bin/env python3
"""dex_execution.py — SPEC-115: DEX execution detector (EXECUTION ≠ POSITIONING).

§8's "the SELL is off-chain/invisible — a CEX deposit is positioning, not execution"
only holds for CEX exits. A DEX sell IS visible execution: direction, size, pool,
tx hash and timestamp are all on the public ledger (Onchain-Analysis-Workshop-
CrimeDesk.md Lesson 8; memory/feedback_predictor_vs_cause_dex_sale_is_execution).
This module watches known DEX routers/pools for large swaps by tracked-cluster
wallets or their SPEC-98 fresh-wallet children, sizes them off the quote/stable
leg (SPEC-53 — never the token leg), and flags the post-exploit dump pattern
(Lesson 8's UXLINK case) as a pattern alert, not an attribution claim.

  size_swap_hits(...)          PURE: transfer fixtures -> EXECUTION hits.
  discover_fresh_children(...)  live seam: who did a tracked wallet fund, and are
                                 they young (SPEC-98 fingerprint, reused not re-derived)?
  exploit_dump_flag(...)       PURE: a large inbound funding right before a sell.
  format_execution_line(...)   the EXECUTION: ... render line.
  build_execution(...)         orchestrator: tracked wallets + their fresh children +
                                 (optional) ad-hoc extra_wallets, scanned against known
                                 dex/router/pool addresses. Provider down / untracked
                                 token / no known dex addresses -> {available: False}
                                 (§3 — never a clean "no execution" bill).

  python3 capabilities/dex_execution.py LAB --json
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

CONFIG_PATH = ROOT / "config" / "dex_execution.json"
TRACKED_PATH = ROOT / "config" / "tracked_wallets.json"
ENTITIES_PATH = ROOT / "config" / "known_entities.json"

try:
    from rotation_freshness import DEFAULT_CFG as _RF_CFG
    _YOUNG_NONCE_MAX_DEFAULT = _RF_CFG.get("onchain_young_nonce_max", 50)
except Exception:  # noqa: BLE001
    _YOUNG_NONCE_MAX_DEFAULT = 50

# Documented defaults (SPEC-115) — override any key in config/dex_execution.json.
DEFAULT_CFG = {
    "days": 14,                       # transfer-log lookback window
    "min_usd": 50_000,                # "large" swap floor, sized off the quote/stable leg
    "min_pct_float": 0.5,             # ...or this % of float, when no price is available
    "young_nonce_max": _YOUNG_NONCE_MAX_DEFAULT,   # SPEC-98 fresh-wallet fingerprint (reused)
    "exploit_window_sec": 24 * 3600,  # inbound funding -> sell within this window = exploit-dump
    "exploit_min_pct_float": 1.0,     # inbound >= this % of float shortly before a sell = flag
    "max_pools": 6,                   # bounded pool reads (quota discipline)
}

# Shared DeFi plumbing (Pancake et al.) AND the apparatus's own routers are both valid swap
# venues to scan — the operator-specificity comes from the SENDER, not the pool (memory:
# reference_multi_cat_a_router_0x238a3588 is confirmed shared plumbing per SPEC-94;
# reference_second_cat_a_router_0xb300000b is still apparatus-tier — scan both regardless).
DEX_HINTS = ("dex", "pancake", "router", "pool", "uniswap", "1inch", "odos", " v2", " v3", "amm")

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


def _short_ts(ts_iso):
    e = _parse_ts(ts_iso)
    if e is None:
        return "unknown"
    return datetime.fromtimestamp(e, tz=timezone.utc).strftime("%m-%d %H:%M")


def _fmt_usd(v):
    v = abs(v or 0)
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _clip_amt(t):
    try:
        return float(t.get("value_decimal") or 0)
    except (TypeError, ValueError):
        return 0.0


# ── known dex/router/pool addresses ─────────────────────────────────────────────
def known_dex_addresses():
    """DEX router/pool addresses to scan: known_entities.json labels matching DEX hints,
    plus any tracked_wallets.json wallet tagged kind=='router' — both fingerprinted
    apparatus routers (memory/reference_multi_cat_a_router_0x238a3588,
    memory/reference_second_cat_a_router_0xb300000b) are swept in via the latter."""
    out = set()
    try:
        ents = json.loads(ENTITIES_PATH.read_text()).get("entities", {})
        for addr, e in ents.items():
            blob = f"{e.get('label', '')} {e.get('name', '')}".lower()
            if any(h in blob for h in DEX_HINTS):
                out.add(addr.lower())
    except Exception:  # noqa: BLE001
        pass
    try:
        tw = json.loads(TRACKED_PATH.read_text())
        for tok in (tw.get("tokens") or {}).values():
            for w in tok.get("wallets", []) or []:
                kind = (w.get("kind") or "").lower()
                subtag = (w.get("subtag") or "").lower()
                tier = (w.get("tier") or "").lower()
                label = (w.get("label") or "").lower()
                # router-1 (0x238a3588) was SPEC-94 reclassified kind=='router'; router-2
                # (0xb300000b) still carries tier 'op' with no kind field — its LABEL
                # ("ROUTER-2-MULTI-CAT-A"/"ROUTER-2-SWAP-EXIT") is the only reliable tag.
                if (kind == "router" or subtag.startswith("dex-router") or tier in ("dex", "router")
                        or "router" in label):
                    addr = (w.get("address") or "").lower()
                    if addr:
                        out.add(addr)
    except Exception:  # noqa: BLE001
        pass
    out.discard("")
    return out


# ── pure core: hit sizing off the quote/stable leg (SPEC-53) ───────────────────
def size_swap_hits(sender, outs, ins, quote_ins, quote_outs, dex_addrs, cfg=None):
    """PURE. `outs`/`ins` are `sender`'s own token transfers; `quote_ins`/`quote_outs` are
    its quote-asset (stable) transfers. A hit requires a SAME-tx-hash quote-leg match (the
    swap's two legs) — no correlation, no hit; never guess a USD size off the token leg
    alone (the SPEC-53 lesson: token-scoped reads miss swap settlements)."""
    cfg = cfg or DEFAULT_CFG
    dex_addrs = {a.lower() for a in (dex_addrs or [])}
    quote_in_by_hash = {t.get("transaction_hash"): t for t in (quote_ins or []) if t.get("transaction_hash")}
    quote_out_by_hash = {t.get("transaction_hash"): t for t in (quote_outs or []) if t.get("transaction_hash")}
    hits = []
    for t in outs or []:              # token OUT of sender, TO a dex address = a SELL
        to = (t.get("to_address") or "").lower()
        if to not in dex_addrs:
            continue
        h = t.get("transaction_hash")
        q = quote_in_by_hash.get(h)
        if not q:
            continue
        usd = _clip_amt(q)
        if usd < cfg["min_usd"]:
            continue
        hits.append({"direction": SELL, "usd": round(usd, 2), "pool": to, "tx_hash": h,
                     "timestamp": t.get("block_timestamp"), "sender": sender.lower(),
                     "token_amount": _clip_amt(t)})
    for t in ins or []:                # token IN to sender, FROM a dex address = a BUY
        frm = (t.get("from_address") or "").lower()
        if frm not in dex_addrs:
            continue
        h = t.get("transaction_hash")
        q = quote_out_by_hash.get(h)
        if not q:
            continue
        usd = _clip_amt(q)
        if usd < cfg["min_usd"]:
            continue
        hits.append({"direction": BUY, "usd": round(usd, 2), "pool": frm, "tx_hash": h,
                     "timestamp": t.get("block_timestamp"), "sender": sender.lower(),
                     "token_amount": _clip_amt(t)})
    return hits


# ── SPEC-98 fresh-wallet fingerprint reused (not re-derived) ────────────────────
def discover_fresh_children(tracked_addr, label, contract, chain_key, transfers_fn, nonce_fn, cfg=None):
    """Children = addresses the tracked wallet sent TOKEN to whose current lifetime nonce
    is <= young_nonce_max (SPEC-98's fresh-wallet fingerprint, config/rotation_freshness.json
    onchain_young_nonce_max — reused verbatim here, never re-derived). Provider failure ->
    [] (a coverage gap, not a claim of zero children)."""
    cfg = cfg or DEFAULT_CFG
    try:
        txs, _src, _partial = transfers_fn(tracked_addr, contract, chain_key, cfg["days"])
    except Exception:  # noqa: BLE001
        return []
    recipients = set()
    for t in txs or []:
        if (t.get("from_address") or "").lower() != tracked_addr.lower():
            continue
        to = (t.get("to_address") or "").lower()
        if to:
            recipients.add(to)
    out = []
    for addr in recipients:
        try:
            n = nonce_fn(addr)
        except Exception:  # noqa: BLE001
            n = None
        if n is not None and n <= cfg["young_nonce_max"]:
            out.append({"address": addr, "nonce": n, "parent": tracked_addr.lower(), "parent_label": label})
    return out


# ── exploit-dump pattern (Lesson 8, UXLINK) ─────────────────────────────────────
def exploit_dump_flag(sell_hit, ins, cfg=None, float_supply=None):
    """PURE. A large inbound TOKEN transfer shortly before `sell_hit`, sized as a % of
    float — the post-exploit realisation pattern: receive a big balance, then dump it.
    A pattern ALERT, not an attribution claim (no tracked/cluster requirement — this can
    fire on any wallet the caller is watching, e.g. an ad-hoc §8 tip). Needs float_supply
    to size (no price/float -> never flagged, not guessed)."""
    cfg = cfg or DEFAULT_CFG
    if not float_supply:
        return None
    hit_ts = _parse_ts(sell_hit.get("timestamp"))
    if hit_ts is None:
        return None
    best = None
    for t in ins or []:
        ts = _parse_ts(t.get("block_timestamp"))
        if ts is None or ts > hit_ts or (hit_ts - ts) > cfg["exploit_window_sec"]:
            continue
        amt = _clip_amt(t)
        pct = (amt / float_supply * 100) if float_supply else None
        if pct is None or pct < cfg["exploit_min_pct_float"]:
            continue
        if best is None or amt > best["amount"]:
            best = {"tx_hash": t.get("transaction_hash"), "amount": amt, "pct_float": round(pct, 3),
                    "timestamp": t.get("block_timestamp"), "from": (t.get("from_address") or "").lower()}
    return best


# ── render ───────────────────────────────────────────────────────────────────────
def format_execution_line(hit):
    """The compact EXECUTION render line — distinct from POSITIONING (CEX-deposit)
    language (memory: feedback_predictor_vs_cause_dex_sale_is_execution)."""
    verb = "SOLD" if hit["direction"] == SELL else "BOUGHT"
    attr = hit.get("attribution") or {}
    tag = ""
    if attr.get("kind") == "tracked":
        tag = f" (cluster: {attr.get('cluster')})"
    elif attr.get("kind") == "fresh_child":
        tag = f" (cluster: {attr.get('cluster')}, fresh child of {attr.get('parent')})"
    line = (f"EXECUTION: {hit['sender']}{tag} {verb} {_fmt_usd(hit['usd'])} via pool {hit['pool']}, "
            f"tx {hit['tx_hash']}, {_short_ts(hit['timestamp'])}")
    ed = hit.get("exploit_dump")
    if ed:
        line += (f"  ⚠ EXPLOIT_DUMP_PATTERN? (funded {ed.get('pct_float')}% of float "
                 f"via tx {ed.get('tx_hash')}, {_short_ts(ed.get('timestamp'))})")
    return line


# ── live wiring (default seams) ─────────────────────────────────────────────────
def _default_transfers_fn():
    import onchain as _oc
    return lambda addr, contract, ck, days: _oc.token_transfers(addr, contract, ck, days=days)


def _default_nonce_fn():
    import onchain as _oc
    return _oc.nonce_of


def _default_quote_fn():
    from verify_wallet import _quote_transfers as _qt
    return _qt


def _tracked_for(ticker):
    tok = json.loads(TRACKED_PATH.read_text()).get("tokens", {}).get(ticker.upper())
    if not tok:
        return None
    contracts = tok.get("contracts", {}) or {}
    chain_key = "binance-smart-chain" if "binance-smart-chain" in contracts else next(iter(contracts), None)
    if not chain_key:
        return None
    tracked = {(w.get("address") or "").lower(): (w.get("label") or w.get("address"))
              for w in tok.get("wallets", []) or []}
    return contracts.get(chain_key), chain_key, tracked


def build_execution(ticker, contract=None, chain_key=None, tracked=None, dex_addrs=None,
                    extra_wallets=None, transfers_fn=None, nonce_fn=None, quote_fn=None,
                    float_supply=None, cfg=None, max_pools=None):
    """Scan known dex/router/pool addresses for large swaps by tracked-cluster wallets,
    their SPEC-98 fresh-wallet children, and any ad-hoc `extra_wallets` (the §8 hand-curled
    tip workflow — a user-flagged address, same spirit as verify_wallet's SPEC 14). Provider
    down / untracked token / no known dex addresses -> {available: False}, NEVER a clean
    'no execution' bill (§3)."""
    cfg = cfg or load_cfg()
    ticker = ticker.upper()
    if contract is None or chain_key is None or tracked is None:
        try:
            resolved = _tracked_for(ticker)
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": f"tracked_wallets.json unreadable: {str(e)[:80]}"}
        if not resolved:
            return {"available": False, "reason": "untracked token (no contract/wallet map)"}
        contract, chain_key, tracked = resolved

    if dex_addrs is None:
        try:
            dex_addrs = known_dex_addresses()
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": f"dex-address config unreadable: {str(e)[:80]}"}
    if not dex_addrs:
        return {"available": False, "reason": "no known dex/router/pool addresses to scan"}

    if transfers_fn is None:
        try:
            transfers_fn = _default_transfers_fn()
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": f"provider seam unavailable: {str(e)[:80]}"}
    if nonce_fn is None:
        try:
            nonce_fn = _default_nonce_fn()
        except Exception:  # noqa: BLE001
            nonce_fn = lambda a, ck="binance-smart-chain": None  # noqa: E731
    if quote_fn is None:
        try:
            quote_fn = _default_quote_fn()
        except Exception:  # noqa: BLE001
            quote_fn = lambda addr, ck, days: {}  # noqa: E731

    max_pools = max_pools or cfg["max_pools"]
    dex_addrs = {a.lower() for a in dex_addrs}
    _ = sorted(dex_addrs)[:max_pools]   # quota discipline note: bounds a full-scan variant; per-wallet
                                        # scans below already read only the candidate wallets' own logs

    children = []
    for addr, label in (tracked or {}).items():
        children += discover_fresh_children(addr, label, contract, chain_key, transfers_fn, nonce_fn, cfg)
    child_by_addr = {c["address"]: c for c in children}

    candidates = dict(tracked or {})
    for c in children:
        candidates.setdefault(c["address"], None)
    for addr in (extra_wallets or {}):
        candidates.setdefault(addr.lower(), (extra_wallets.get(addr) if isinstance(extra_wallets, dict) else None))

    read_any, hits = False, []
    for wallet in candidates:
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

        wallet_hits = size_swap_hits(wallet, outs, ins, quote_ins, quote_outs, dex_addrs, cfg=cfg)
        attr = None
        if candidates.get(wallet):
            attr = {"kind": "tracked", "cluster": candidates[wallet], "parent": None}
        elif wallet in child_by_addr:
            c = child_by_addr[wallet]
            attr = {"kind": "fresh_child", "cluster": c.get("parent_label"), "parent": c.get("parent")}
        for h in wallet_hits:
            h["attribution"] = attr
            if h["direction"] == SELL:
                ed = exploit_dump_flag(h, ins, cfg=cfg, float_supply=float_supply)
                if ed:
                    h["exploit_dump"] = ed
            hits.append(h)

    if not read_any:
        return {"available": False, "reason": "provider unavailable (all wallet reads failed)"}

    hits.sort(key=lambda h: h.get("timestamp") or "", reverse=True)
    lines = [format_execution_line(h) for h in hits]
    return {"available": True, "ticker": ticker, "n_hits": len(hits), "hits": hits, "lines": lines}


def main():
    ap = argparse.ArgumentParser(description="SPEC-115 DEX execution detector")
    ap.add_argument("ticker")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    out = build_execution(args.ticker)
    if args.json:
        print(json.dumps(out))
    elif not out.get("available"):
        print(f"{args.ticker}: unavailable — {out.get('reason')}")
    elif not out["hits"]:
        print(f"{args.ticker}: no qualifying DEX execution hits")
    else:
        for line in out["lines"]:
            print(line)


if __name__ == "__main__":
    main()
