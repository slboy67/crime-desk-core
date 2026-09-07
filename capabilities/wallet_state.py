#!/usr/bin/env python3
"""wallet_state.py — consolidated on-chain wallet state for a token (Phase 2).

Folds the parts-bin wallet cluster (watch_wallets / nonce_watch / flows /
activity_audit / safe_audit) into ONE capability with modes:

  mode=snapshot (default, NATIVE)
      Per tracked wallet: nonce + native (gas-token) balance + fired flag, read
      live from RPC against crime-desk/config/tracked_wallets.json. This is the
      Stage-5 staged-wallet detector (CLAUDE.md §8: "alert on nonces, not
      breakdowns") plus dormant-safe balance state in one snapshot.

  mode=audit (DELEGATED → safe_audit.py --json)
      Lifetime inbound/outbound token-flow audit per safe (CEX-selling vs internal
      staging vs pristine). The richer, slower distribution read.

  python3 capabilities/wallet_state.py LAB                  # snapshot, human
  python3 capabilities/wallet_state.py LAB --json
  python3 capabilities/wallet_state.py LAB --mode audit --days 90 --json

NOTE: the `onchain` capability wraps the audit path into a single scored verdict;
use wallet_state when you want the raw per-wallet nonce/balance grid (e.g. watching
a specific mega-safe nonce fire).
"""
import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import colors as C

ROOT = HERE.parent
WALLETS = ROOT / "config" / "tracked_wallets.json"


def _hex_int(x):
    try:
        return int(x, 16) if isinstance(x, str) else None
    except Exception:
        return None


def _load_token(ticker):
    cfg = json.loads(WALLETS.read_text())
    return cfg.get("tokens", {}).get(ticker.upper())


# SPEC-145: the single-URL, no-fallback `_rpc` this module used to own returned None on any
# failure with zero visibility into WHY — the review counted three incompatible RPC
# implementations across the desk. These two seams delegate to onchain.py's real ones
# (_rpc_pool: config RPC -> free-public fallback; _rpc_at: one JSON-RPC call) so there is
# exactly one place that knows how to reach a chain. Imported lazily (inside the function,
# not at module top) because onchain.py imports `build_snapshot` from this module at ITS
# top — a top-level `import onchain` here would deadlock that circular load.
def _default_rpc_pool(chain):
    import onchain
    return onchain._rpc_pool(chain)


def _default_rpc_at(url, method, params, timeout=12):
    import onchain
    return onchain._rpc_at(url, method, params, timeout=timeout)


def build_snapshot(ticker, rpc_pool_fn=None, rpc_at_fn=None):
    """NATIVE per-wallet nonce + native balance + fired flag from tracked_wallets config.

    SPEC-145: reads through the chain's RPC pool (config primary -> free-public fallbacks)
    instead of a single URL, and every wallet carries `reason` (None on a clean read,
    "unreadable: ..." / "no RPC configured..." otherwise) — a caller must be able to tell
    "read: nonce=N" apart from "unreadable: reason", never just a bare None.
    rpc_pool_fn/rpc_at_fn are injectable for offline tests; default to onchain's real pool."""
    ticker = ticker.upper().replace("USDT", "")
    tok = _load_token(ticker)
    if not tok:
        return {"ticker": ticker, "tracked": False, "wallets": [],
                "n_wallets": 0, "fired_count": 0, "dormant_count": 0,
                "wallets_total": 0, "wallets_read": 0, "wallets_unreadable": 0, "unreadable": []}
    wallets = tok.get("wallets", [])
    rpc_pool_fn = rpc_pool_fn or _default_rpc_pool
    rpc_at_fn = rpc_at_fn or _default_rpc_at

    def _pooled(chain, method, params):
        pool = rpc_pool_fn(chain)
        for url in pool:
            r = rpc_at_fn(url, method, params)
            if r is not None:
                return r, len(pool)
        return None, len(pool)

    def one(w):
        chain = w.get("chain")
        nonce_hex, pool_n = _pooled(chain, "eth_getTransactionCount", [w["address"], "latest"])
        nonce = _hex_int(nonce_hex)
        bal = None
        if nonce is not None:
            bal_hex, _ = _pooled(chain, "eth_getBalance", [w["address"], "latest"])
            bal_wei = _hex_int(bal_hex)
            bal = round(bal_wei / 1e18, 6) if bal_wei is not None else None
        rpc_ok = nonce is not None
        reason = None
        if not rpc_ok:
            reason = (f"no RPC configured for chain {chain!r}" if pool_n == 0
                      else f"unreadable: all {pool_n} provider(s) failed")
        return {
            "label": w.get("label"), "address": w["address"], "chain": chain,
            "tier": w.get("tier"), "subtag": w.get("subtag"),   # SPEC 28: e.g. staging-sink
            "nonce": nonce, "native_balance": bal,
            "fired": (nonce is not None and nonce > 0),
            "rpc_ok": rpc_ok, "reason": reason,
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(one, wallets))

    fired = [r for r in rows if r["fired"]]
    dormant = [r for r in rows if r["rpc_ok"] and not r["fired"]]
    # gas-primed-but-unfired = staged apparatus loaded (nonce==0, has gas) — §8 fire watch
    primed = [r for r in rows if r["rpc_ok"] and not r["fired"]
              and (r["native_balance"] or 0) > 0]
    unreadable = [{"address": r["address"], "chain": r["chain"], "reason": r["reason"]}
                  for r in rows if not r["rpc_ok"]]
    return {
        "ticker": ticker, "tracked": True, "n_wallets": len(rows),
        "fired_count": len(fired), "dormant_count": len(dormant),
        "primed_unfired_count": len(primed),
        "wallets_total": len(rows), "wallets_read": len(rows) - len(unreadable),
        "wallets_unreadable": len(unreadable), "unreadable": unreadable,
        "wallets": rows,
    }


def build_audit(ticker, days=90):
    """DELEGATED → parts-bin safe_audit.py --json (lifetime distribution audit)."""
    ticker = ticker.upper().replace("USDT", "")
    try:
        out = subprocess.run(
            ["python3", str(ROOT / "_oldrepo/scripts/safe_audit.py"), ticker,
             "--fast", "--days", str(days), "--json"],
            capture_output=True, text=True, timeout=200, cwd=str(ROOT))
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001
        return {"ticker": ticker, "_error": f"safe_audit delegate failed: {e}"}


def render_snapshot(s):
    if not s["tracked"]:
        print(f"# {s['ticker']} — no tracked wallets in config")
        return
    print(C.c(f"═══ {s['ticker']} WALLET SNAPSHOT ═══", "bold", "cyan")
          + C.c(f"  {s['n_wallets']} wallets · {s['fired_count']} fired · "
                f"{s['primed_unfired_count']} primed-unfired", "grey"))
    for w in sorted(s["wallets"], key=lambda x: (x["tier"] or "", x["label"] or "")):
        dot = "🔴" if w["fired"] else ("🟠" if (w["native_balance"] or 0) > 0 else "⚪")
        n = w["nonce"] if w["nonce"] is not None else "—"
        b = f"{w['native_balance']:.4f}" if w["native_balance"] is not None else "RPC?"
        print(f"  {dot} {C.c((w['label'] or '?')[:24].ljust(24), 'bold')} "
              f"{C.c((w['tier'] or '').ljust(6), 'grey')} nonce {str(n).rjust(4)}  gas {b}  "
              f"{C.c(w['chain'] or '', 'grey')}")
    print(C.c("\n🔴 fired (nonce>0)  🟠 primed-unfired (gas, nonce=0 = staged apparatus)  ⚪ empty/dormant", "grey"))
    print(C.c("Dormant mega-safe firing = the biggest Stage-5 escalation (§8) — watch nonces, not breakdowns.", "grey"))


def main():
    ap = argparse.ArgumentParser(description="Consolidated on-chain wallet state")
    ap.add_argument("ticker")
    ap.add_argument("--mode", choices=["snapshot", "audit"], default="snapshot")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)

    if args.mode == "audit":
        data = build_audit(args.ticker, args.days)
        print(json.dumps(data) if args.json else json.dumps(data, indent=2))
        return

    snap = build_snapshot(args.ticker)
    if args.json:
        print(json.dumps(snap))
    else:
        render_snapshot(snap)


if __name__ == "__main__":
    main()
