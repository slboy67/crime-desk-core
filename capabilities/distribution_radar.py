#!/usr/bin/env python3
"""distribution_radar.py — in-house clone of the "onchain radar" TG feed (SPEC 15).

Stops the desk depending on the external Telegram channel. That channel surfaces wallets
freshly SEEDED by an operator safe that are now DUMPING on DEX (the ESPORTS 0xbb58 case).
`verify_wallet` checks a NAMED wallet; this does the missing piece — DISCOVERY: it finds
the distributing wallets itself, across the watchlist Cat A names.

Per Cat A token with a tracked safe map:
  1. Seeded-recipient discovery — recent OUTBOUND from each tracked distribution/team safe
     (Moralis) → the `to` addresses are freshly-seeded wallets (inverse of verify_wallet.funded_by).
  2. Independent-holder discovery — GoPlus top holders minus tracked/CEX/LP/contracts.
  3. Distribution check — verify_wallet logic on each candidate (balance, recent DEX outbounds, last_out).
  4. Rank by confidence — HIGH = seeded-from-safe AND distributing (operator distribution);
     MED = independent large holder distributing; drop dormant / non-sellers.
  5. Perp fusion — tag the token with live funding (%/4h, sign, short-vetoed) so the on-chain
     signal connects to a trade (the desk's edge over the raw feed).
  6. Change-detection — persist the known-distributor set per token to state/; each sweep
     surfaces only NEW or materially-accelerated distributors (no re-alerting every run).

  orchestrator.py distribution_radar '{"token":"ESPORTS"}'   # one token
  orchestrator.py distribution_radar '{}'                     # sweep all Cat A watchlist names
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import colors as C
from onchain import token_transfers, nonce_of, MoralisError, _entity_labels, _MORALIS_CHAIN, _concentration  # SPEC-97 seam
from verify_wallet import build_verify, SEED_TIERS, _live_price, _classify_addr, queryable_chains
from regime_flip import live_perp, sign_of, DEEP_NEG

STATE = ROOT / "state"
WALLETS = ROOT / "config" / "tracked_wallets.json"
WATCHLIST = ROOT / "config" / "watchlist.json"
ZERO = "0x0000000000000000000000000000000000000000"
SEED_DAYS = 4            # "freshly seeded" window
MAX_CANDIDATES = 12      # per token, bound the work (radar, not exhaustive audit)


def _cat_a_tokens():
    """Watchlist Cat A names that have a tracked safe map + a Moralis-supported contract."""
    tracked = json.loads(WALLETS.read_text()).get("tokens", {})
    try:
        wl = json.loads(WATCHLIST.read_text())["tokens"]
    except Exception:
        wl = []
    cat = {t["ticker"].upper(): (t.get("category") or "") for t in wl}
    out = []
    for tk, meta in tracked.items():
        contracts = meta.get("contracts", {}) or {}
        chain = "binance-smart-chain" if "binance-smart-chain" in contracts else next(iter(contracts), None)
        if not chain or chain not in _MORALIS_CHAIN:
            continue
        if cat.get(tk, "").strip().upper().startswith("A"):
            out.append(tk)
    return out


def _radar_path(token):
    return STATE / f"radar_distributors_{token.upper()}.json"


def _load_known(token):
    try:
        return json.loads(_radar_path(token).read_text())
    except Exception:
        return {}


def _save_known(token, known):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        _radar_path(token).write_text(json.dumps(known))
    except Exception:
        pass


# ---- SPEC 29: time-budget + partial-results + progress emission for the whole-watchlist sweeps ----
# The orchestrator hard-kills at the capability timeout and emits a bare error, discarding ALL
# completed work. So the sweep must SELF-BUDGET below that timeout and return what finished with
# partial:true — never all-or-nothing — and emit live progress so the orchestrator can see "7 of 14".

def _parse_tickers(raw):
    """Accept the `tickers` subset arg in any shape the orchestrator passes it: a python-list str
    ("['BILL', 'SKYAI']"), a comma- or space-separated string, or None. → uppercased list | None."""
    if not raw:
        return None
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        s = str(raw).strip().strip("[]")
        items = s.replace(",", " ").replace("'", " ").replace('"', " ").split()
    out = [t.strip().upper().replace("USDT", "") for t in items if t and t.strip()]
    return out or None


def _progress_path(name):
    return STATE / f"radar_progress_{name}.json"


def _write_progress(name, done, total, current, t0, partial=False, last=None):
    """Sidecar liveness signal the orchestrator can read MID-RUN (state/radar_progress_<name>.json)
    + a stderr line. Atomic write; never raises (best-effort instrumentation)."""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        payload = {"capability": name, "tokens_done": done, "tokens_total": total,
                   "current": current, "last_completed": last, "partial": partial,
                   "elapsed_sec": round(time.time() - t0, 1)}
        p = _progress_path(name)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(p)
    except Exception:
        pass
    try:
        msg = f"[{done}/{total}] {('done ' + str(last)) if last else (current or '…')}"
        print(f"radar-progress {name}: {msg}" + (" PARTIAL(budget)" if partial else ""), file=sys.stderr, flush=True)
    except Exception:
        pass


DIST_RADAR_BUDGET = 300        # < the 360s orchestrator timeout (margin for serialization)
ONCHAIN_RADAR_BUDGET = 350     # < the 420s orchestrator timeout


def _combined_nonce(addr, chains):
    """SPEC 21 — sum of tx-counts across every chain the token is on (free RPC, zero quota).
    A wallet active on a non-default chain still moves the combined nonce, so the radar's
    nonce-gate can't miss its activity by only watching one chain. None if all chains read None."""
    vals = [n for ck in chains if (n := nonce_of(addr, ck)) is not None]
    return sum(vals) if vals else None


def _seeded_recipients(contracts, safes, days=SEED_DAYS):
    """`to` addresses of recent OUTBOUND from each tracked seed-tier safe = freshly seeded.
    SPEC 21: each safe is read on ITS OWN chain (operators seed on whichever chain), not the
    token's default chain. Returns (recipients, n_errors) — n_errors>0 means partial discovery."""
    recips, errors = set(), 0
    for w in safes:
        ck = w.get("chain")
        contract = contracts.get(ck)
        if not ck or ck not in _MORALIS_CHAIN or not contract:
            continue
        try:
            txs, _src, _partial = token_transfers(w["address"], contract, ck, days=days)   # SPEC-97 seam
        except Exception:   # rate-limit/transient — count it, don't pretend the safe sent nothing
            errors += 1
            continue
        for t in txs:
            if (t.get("from_address") or "").lower() == w["address"].lower():
                to = (t.get("to_address") or "").lower()
                if to and to != ZERO:
                    recips.add(to)
    return recips, errors


def build_radar(token=None, days=SEED_DAYS, tickers=None, budget_sec=DIST_RADAR_BUDGET):
    tokens = [token.upper()] if token else (_parse_tickers(tickers) or _cat_a_tokens())
    cfg = json.loads(WALLETS.read_text()).get("tokens", {})
    labels = _entity_labels()
    t0 = time.time()
    result = {"scanned": [], "tokens": {}, "alerts_total": 0, "partial": False,
              "tokens_total": len(tokens), "tokens_done": 0, "tokens_remaining": []}
    _write_progress("distribution_radar", 0, len(tokens), None, t0)

    for i, tk in enumerate(tokens):
        # SPEC 29: stop BEFORE the orchestrator hard-kills us; return what finished as partial.
        if time.time() - t0 > budget_sec:
            result["partial"] = True
            result["tokens_remaining"] = tokens[i:]
            _write_progress("distribution_radar", result["tokens_done"], len(tokens), tk, t0,
                            partial=True, last=tokens[i - 1] if i else None)
            break
        meta = cfg.get(tk)
        if not meta:
            result["tokens"][tk] = {"available": False, "reason": "untracked"}
            result["scanned"].append(tk)
            result["tokens_done"] = i + 1
            _write_progress("distribution_radar", i + 1, len(tokens), tk, t0, last=tk)
            continue
        contracts = meta.get("contracts", {}) or {}
        # SPEC 21: a token can be deployed on several chains — query ALL of them.
        chains = list(queryable_chains(meta).keys())
        if not chains:
            result["tokens"][tk] = {"available": False, "reason": "no Moralis-supported contract"}
            result["scanned"].append(tk)
            result["tokens_done"] = i + 1
            _write_progress("distribution_radar", i + 1, len(tokens), tk, t0, last=tk)
            continue
        tracked = {w["address"].lower() for w in meta.get("wallets", [])}
        safes = [w for w in meta.get("wallets", []) if (w.get("tier") or "").lower() in SEED_TIERS]

        try:
            price, _ = _live_price(tk)
            seeded, seed_errors = _seeded_recipients(contracts, safes, days)
            conc = _concentration(tk)
            indep = []
            for h in (conc.get("holders") or []):
                a = (h.get("address") or "").lower()
                if not a or a in tracked or h.get("is_contract") or h.get("is_locked"):
                    continue
                kind, _lab = _classify_addr(a, {}, labels)
                if kind in ("cex", "dex"):
                    continue
                indep.append(a)

            candidates = [a for a in (seeded | set(indep)) if a not in tracked][:MAX_CANDIDATES]
            known = _load_known(tk)

            def check(addr):
                # SPEC 17 layer 1: free-RPC nonce gates the heavy read — a known wallet whose
                # nonce hasn't advanced since last sweep can't have new sells → reuse cached row,
                # zero provider quota. SPEC 21: combined across all chains so a non-default-chain
                # move still busts the cache.
                nonce = _combined_nonce(addr, chains)
                prev = known.get(addr)
                if prev and nonce is not None and prev.get("nonce") == nonce and prev.get("row"):
                    return {"_cached": True, "address": addr, "nonce": nonce, "row": prev["row"]}
                v = build_verify(addr, tk, price=price)
                if not v.get("available"):
                    return {"_degraded": True, "address": addr} if v.get("degraded") else None
                dex = any(o.get("to_kind") == "dex" for o in v.get("recent_outbounds", []))
                row = {"address": addr, "seeded": v["seeded_staging"],
                       "seeded_from": (v["seeded_sources"][0]["label"] if v.get("seeded_sources") else None),
                       "distributing": v["distributing"], "dex_selling": dex,
                       "balance": v["net_flow_window"], "balance_usd": v["net_flow_window_usd"],
                       "last_out": v["last_out_ts"], "verdict": v["verdict"],
                       # SPEC 15 refinement: carry funding-source category + sell-hubs for clustering
                       "funded_by_kind": v.get("funded_by_kind"), "funded_by_cex": v.get("funded_by_cex"),
                       "sell_dests": [d.get("address") for d in (v.get("sell_destinations") or []) if d.get("address")]}
                return {"_fresh": True, "address": addr, "nonce": nonce, "row": row}

            with ThreadPoolExecutor(max_workers=6) as ex:
                results = [c for c in ex.map(check, candidates) if c]
            n_degraded = sum(1 for c in results if c.get("_degraded")) + seed_errors
            n_cached = sum(1 for c in results if c.get("_cached"))   # Moralis calls saved by nonce-gate

            dists = []
            for c in results:
                row = c.get("row")
                if not row or not row.get("distributing"):
                    continue                         # dormant / degraded / non-sellers dropped
                dists.append((c["address"], c["nonce"], dict(row)))

            # SPEC 15 refinement (behavioral clustering): confidence ≠ funding lineage alone.
            # Wallets feeding the SAME downstream sell-hub are one actor. A hub fed by a
            # SEEDED (lineage-HIGH) wallet is an operator hub → every wallet feeding it is the
            # operator's distribution fleet, even a funded_by:unknown one (the ESPORTS 0x2609
            # ⇄ 0xbb58 via hub 0x5bb5 case). confidence = max(lineage, shared-destination).
            operator_dests, dest_feeders = set(), {}
            for addr, _n, row in dists:
                for d in (row.get("sell_dests") or []):
                    dest_feeders.setdefault(d, set()).add(addr)
                    if row.get("seeded"):
                        operator_dests.add(d)
            sell_hubs = [{"address": d, "n_wallets": len(w), "wallets": sorted(w)}
                         for d, w in dest_feeders.items() if len(w) >= 2]
            sell_hubs.sort(key=lambda h: -h["n_wallets"])
            for addr, _n, row in dists:
                lineage = bool(row.get("seeded"))
                shared = (not lineage) and any(d in operator_dests for d in (row.get("sell_dests") or []))
                row["confidence"] = "HIGH" if (lineage or shared) else "MED"
                row["confidence_reason"] = "lineage" if lineage else ("shared-dest" if shared else "independent")
                row["cluster"] = shared
            dists.sort(key=lambda x: (0 if x[2]["confidence"] == "HIGH" else 1, -(x[2]["balance_usd"] or 0)))

            # change-detection — cached rows carry prior last_out → no spurious accel
            alerts, new_known = [], dict(known)
            for addr, nonce, row in dists:
                prev = known.get(addr)
                if prev is None or not prev.get("row"):
                    alerts.append({**row, "alert": "NEW"})
                elif row.get("last_out") and prev.get("last_out") and row["last_out"] > prev["last_out"]:
                    alerts.append({**row, "alert": "ACCELERATED"})
                new_known[addr] = {"nonce": nonce, "last_out": row.get("last_out"), "row": row}
            _save_known(tk, new_known)
            distributors = [row for _, _, row in dists]

            lp = live_perp(tk) or {}
            f4 = lp.get("funding_4h")
            perp = {"funding_4h": f4, "sign": sign_of(f4) if f4 is not None else None,
                    "short_vetoed": bool(f4 is not None and f4 <= DEEP_NEG),
                    "funding_split": lp.get("funding_split", False)}

            result["tokens"][tk] = {"available": True, "perp": perp,
                                    "n_candidates": len(candidates), "distributors": distributors,
                                    "sell_hubs": sell_hubs,
                                    "alerts": alerts, "concentration_ok": conc.get("available", False),
                                    "degraded": bool(n_degraded or not conc.get("available", False)),
                                    "n_degraded": n_degraded, "n_nonce_cached": n_cached,
                                    "coverage": {"seeded_discovery_errors": seed_errors,
                                                 "candidates_unread": sum(1 for c in results if c.get("_degraded")),
                                                 "concentration": conc.get("available", False)}}
            result["alerts_total"] += len(alerts)
        except Exception as e:  # noqa: BLE001 — one token's provider hiccup never kills the sweep
            result["tokens"][tk] = {"available": False, "reason": f"error: {str(e)[:120]}"}
        result["scanned"].append(tk)
        result["tokens_done"] = i + 1
        nd = len(result["tokens"].get(tk, {}).get("distributors", []))
        _write_progress("distribution_radar", i + 1, len(tokens), tk, t0, last=f"{tk}({nd} dist)")

    _write_progress("distribution_radar", result["tokens_done"], len(tokens),
                    None, t0, partial=result["partial"])
    return result


def render_human(r):
    print(C.c(f"═══ DISTRIBUTION RADAR — {len(r['scanned'])} Cat A names · {r['alerts_total']} new/accel ═══", "bold", "cyan"))
    for tk in r["scanned"]:
        t = r["tokens"].get(tk, {})
        if not t.get("available"):
            print(C.c(f"  {tk}: {t.get('reason','?')}", "grey")); continue
        p = t["perp"]
        fs = f"{p['funding_4h']:+.3f}%/4h ({p['sign']})" if p.get("funding_4h") is not None else "n/a"
        veto = " ⛔short-vetoed" if p["short_vetoed"] else ""
        print(C.c(f"\n{tk}", "bold") + C.c(f"  funding {fs}{veto}  · {len(t['distributors'])} distributors", "grey"))
        for c in t["distributors"][:8]:
            col = "red" if c["confidence"] == "HIGH" else "yellow"
            usd = f"${c['balance_usd']:,.0f}" if c["balance_usd"] is not None else "?"
            tag = " 🔴NEW" if any(a["address"] == c["address"] and a["alert"] == "NEW" for a in t["alerts"]) else ""
            seed = f" ⇐{c['seeded_from']}" if c["seeded"] else ""
            if c.get("cluster"):
                seed = " ⇄shared-hub (operator fleet)"
            elif c.get("funded_by_cex"):
                seed = f" ⇐{c['funded_by_cex']} (cex-withdrawal)"
            print("  " + C.c(f"{c['confidence']:4}", "bold", col) + f" {c['address'][:12]}… {usd} bal{seed} · last out {c['last_out']}{tag}")
        if t.get("sell_hubs"):
            for h in t["sell_hubs"][:3]:
                print(C.c(f"    🔗 sell-hub {h['address'][:12]}… fed by {h['n_wallets']} wallets", "grey"))


def main():
    ap = argparse.ArgumentParser(description="On-chain distribution radar (discovery)")
    ap.add_argument("token", nargs="?", default=None)
    ap.add_argument("--days", type=int, default=SEED_DAYS)
    ap.add_argument("--tickers", default=None, help="SPEC 29: comma/list subset to sweep")
    ap.add_argument("--budget", type=int, default=DIST_RADAR_BUDGET, help="SPEC 29: time budget (s)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)
    r = build_radar(args.token, args.days, tickers=args.tickers, budget_sec=args.budget)
    if args.json:
        print(json.dumps(r))
    else:
        render_human(r)


if __name__ == "__main__":
    main()
