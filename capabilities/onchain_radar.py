#!/usr/bin/env python3
"""onchain_radar.py — full in-house replacement for the "onchain radar" TG feed (SPEC 16).

Widens distribution_radar (SPEC 15) so it covers everything the channel posts, not just
seeded dumps. Per Cat A watchlist token, ONE candidate pass (seeded recipients + GoPlus
top holders) yields three signals; plus a perp-side liq tracker:

  • distributors   — seeded-from-safe (HIGH) or independent (MED) wallets selling   (SPEC 15)
  • accumulators   — independent wallets net-BUYING in size on DEX (base forming → long setup)
  • independent sellers w/ cost-basis proxy — held_days + holder_type (early-winner cashing
    out vs operator dump) so a profit-taker isn't mistaken for operator distribution
  • perp_liqs      — large tracked-whale Hyperliquid positions + their liquidation prices
    (cascade fuel before it fires), from config/hyperdash_whales.json

Perp-fused, ranked, change-detected (only NEW/accelerated surface). Moralis+GoPlus (BSC)
+ Hyperliquid. Sweep is a periodic cadence tool (~mins); single token is fast.

  orchestrator.py onchain_radar '{"token":"ESPORTS"}'   # one token
  orchestrator.py onchain_radar '{}'                     # full Cat A sweep + perp liqs
"""
import argparse
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import colors as C
from onchain import _concentration, _entity_labels, _MORALIS_CHAIN, nonce_of
from verify_wallet import build_verify, SEED_TIERS, _live_price, _classify_addr, queryable_chains
from distribution_radar import (_cat_a_tokens, _seeded_recipients, SEED_DAYS, MAX_CANDIDATES, ZERO,
                                _parse_tickers, _write_progress, ONCHAIN_RADAR_BUDGET)


def _combined_nonce(addr, chains):
    """SPEC 21 — combined tx-count across all the token's chains (free RPC). Local copy so it
    binds THIS module's nonce_of (test-patchable). A non-default-chain move still moves it."""
    vals = [n for ck in chains if (n := nonce_of(addr, ck)) is not None]
    return sum(vals) if vals else None
from regime_flip import live_perp, sign_of, DEEP_NEG

STATE = ROOT / "state"
WALLETS = ROOT / "config" / "tracked_wallets.json"
HL_WHALES = ROOT / "config" / "hyperdash_whales.json"
HL_INFO = "https://api.hyperliquid.xyz/info"
NEAR_LIQ_PCT = 20.0   # flag a position whose price is within this % of its liq price


def _load(name):
    try:
        return json.loads((STATE / name).read_text())
    except Exception:
        return {}


def _save(name, obj):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / name).write_text(json.dumps(obj))
    except Exception:
        pass


def _diff(known, items, key="address", ts="last_event"):
    """Change-detection: return NEW + ACCELERATED items; mutate `known` in place."""
    alerts = []
    for c in items:
        k = c[key]
        prev = known.get(k)
        if prev is None:
            alerts.append({**c, "alert": "NEW"})
        elif c.get(ts) and prev.get(ts) and c[ts] > prev[ts]:
            alerts.append({**c, "alert": "ACCELERATED"})
        known[k] = {ts: c.get(ts)}
    return alerts


def _hl_positions(addr):
    try:
        body = json.dumps({"type": "clearinghouseState", "user": addr}).encode()
        req = urllib.request.Request(HL_INFO, data=body,
                                     headers={"Content-Type": "application/json", "User-Agent": "radar/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
    except Exception:
        return []
    out = []
    for ap in d.get("assetPositions", []):
        p = ap.get("position", {}) or {}
        try:
            szi = float(p.get("szi") or 0)
        except (TypeError, ValueError):
            szi = 0
        if szi == 0:
            continue
        out.append({"coin": p.get("coin"), "side": "LONG" if szi > 0 else "SHORT",
                    "size": abs(szi), "entry": p.get("entryPx"), "liq": p.get("liquidationPx"),
                    "value_usd": p.get("positionValue"), "roe": p.get("returnOnEquity")})
    return out


def _perp_liqs(coin_filter=None):
    """Tracked-whale HL positions + liq prices; flag those near liquidation. coin_filter =
    set of upper-case tickers to keep (None = all)."""
    try:
        whales = json.loads(HL_WHALES.read_text()).get("whales", [])
    except Exception:
        return {"available": False, "reason": "no whale config", "positions": []}
    rows = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda w: (w, _hl_positions(w["address"])), whales))
    for w, poss in results:
        for p in poss:
            coin = (p.get("coin") or "").upper()
            if coin_filter and coin not in coin_filter:
                continue
            liq = p.get("liq")
            near = None
            try:
                px, _ = _live_price(coin)
                if px and liq:
                    near = round(abs(px - float(liq)) / px * 100, 2)
            except Exception:
                pass
            rows.append({"whale": w.get("label"), "address": w["address"], **p,
                         "liq_distance_pct": near, "near_liq": bool(near is not None and near <= NEAR_LIQ_PCT)})
    rows.sort(key=lambda r: (r["liq_distance_pct"] is None, r.get("liq_distance_pct") or 1e9))
    return {"available": True, "positions": rows}


def _token_pass(tk, meta, labels):
    contracts = meta.get("contracts", {}) or {}
    # SPEC 21: query ALL chains the token is deployed on (multi-chain operators).
    chains = list(queryable_chains(meta).keys())
    if not chains:
        return {"available": False, "reason": "no Moralis-supported contract"}
    tracked = {w["address"].lower() for w in meta.get("wallets", [])}
    safes = [w for w in meta.get("wallets", []) if (w.get("tier") or "").lower() in SEED_TIERS]

    price, _ = _live_price(tk)
    seeded, seed_errors = _seeded_recipients(contracts, safes, SEED_DAYS)
    conc = _concentration(tk)
    indep = []
    for h in (conc.get("holders") or []):
        a = (h.get("address") or "").lower()
        if not a or a in tracked or h.get("is_contract") or h.get("is_locked"):
            continue
        if _classify_addr(a, {}, labels)[0] in ("cex", "dex"):
            continue
        indep.append(a)
    candidates = [a for a in (seeded | set(indep)) if a not in tracked][:MAX_CANDIDATES]
    vcache = _load(f"or_verify_{tk}.json")     # {addr: {nonce, v}} — SPEC 17 nonce-gated verify cache

    def check(addr):
        # nonce-gate the heavy read (free RPC): unchanged nonce → reuse cached verify, no Moralis.
        # SPEC 21: combined across all chains so a non-default-chain move busts the cache.
        nonce = _combined_nonce(addr, chains)
        prev = vcache.get(addr)
        if prev and nonce is not None and prev.get("nonce") == nonce and prev.get("v"):
            return {"_v": prev["v"], "_nonce": nonce, "_cached": True}
        v = build_verify(addr, tk, price=price)
        if v.get("available"):
            return {"_v": v, "_nonce": nonce}
        return {"_degraded": True} if v.get("degraded") else None

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = [c for c in ex.map(check, candidates) if c]
    checked = [c["_v"] for c in results if c.get("_v")]
    n_degraded = sum(1 for c in results if c.get("_degraded")) + seed_errors
    n_cached = sum(1 for c in results if c.get("_cached"))
    new_vcache = dict(vcache)
    for c in results:
        if c.get("_v"):
            new_vcache[c["_v"]["address"]] = {"nonce": c.get("_nonce"), "v": c["_v"]}
    _save(f"or_verify_{tk}.json", new_vcache)

    # SPEC 15 refinement (behavioral clustering): wallets feeding the SAME sell-hub as a
    # seeded operator wallet are the operator's fleet → promote shared-destination wallets to
    # HIGH even with unknown funding lineage. confidence = max(lineage, shared-destination).
    operator_dests, dest_feeders = set(), {}
    for v in checked:
        if not v.get("distributing"):
            continue
        for d in [x.get("address") for x in (v.get("sell_destinations") or []) if x.get("address")]:
            dest_feeders.setdefault(d, set()).add(v["address"])
            if v.get("seeded_staging"):
                operator_dests.add(d)
    sell_hubs = [{"address": d, "n_wallets": len(w), "wallets": sorted(w)}
                 for d, w in dest_feeders.items() if len(w) >= 2]
    sell_hubs.sort(key=lambda h: -h["n_wallets"])

    distributors, accumulators, indep_sellers = [], [], []
    for v in checked:
        row = {"address": v["address"], "balance_usd": v["net_flow_window_usd"],
               "holder_type": v["holder_type"], "last_event": v["last_out_ts"]}
        if v["distributing"]:
            lineage = bool(v["seeded_staging"])
            dests = [x.get("address") for x in (v.get("sell_destinations") or []) if x.get("address")]
            shared = (not lineage) and any(d in operator_dests for d in dests)
            distributors.append({**row, "confidence": "HIGH" if (lineage or shared) else "MED",
                                 "confidence_reason": "lineage" if lineage else ("shared-dest" if shared else "independent"),
                                 "cluster": shared, "seeded": lineage,
                                 "funded_by_kind": v.get("funded_by_kind"), "funded_by_cex": v.get("funded_by_cex"),
                                 "seeded_from": (v["seeded_sources"][0]["label"] if v.get("seeded_sources") else None)})
            if not lineage and not shared:   # genuinely independent — not the operator fleet
                indep_sellers.append({"address": v["address"], "balance_usd": v["net_flow_window_usd"],
                                      "held_days": v["held_days"], "holder_type": v["holder_type"]})
        if v["accumulating"]:
            accumulators.append({"address": v["address"], "balance_usd": v["net_flow_window_usd"],
                                 "recent_net": v["recent_net"], "held_days": v["held_days"],
                                 "last_event": v.get("first_seen_ts")})
    distributors.sort(key=lambda c: (0 if c["confidence"] == "HIGH" else 1, -(c["balance_usd"] or 0)))
    accumulators.sort(key=lambda c: -(c["recent_net"] or 0))

    dist_alerts = _diff(_kn_d.setdefault(tk, _load(f"or_dist_{tk}.json")), distributors)
    acc_alerts = _diff(_kn_a.setdefault(tk, _load(f"or_acc_{tk}.json")), accumulators)
    _save(f"or_dist_{tk}.json", _kn_d[tk])
    _save(f"or_acc_{tk}.json", _kn_a[tk])

    lp = live_perp(tk) or {}
    f4 = lp.get("funding_4h")
    perp = {"funding_4h": f4, "sign": sign_of(f4) if f4 is not None else None,
            "short_vetoed": bool(f4 is not None and f4 <= DEEP_NEG), "funding_split": lp.get("funding_split", False)}
    return {"available": True, "perp": perp, "n_candidates": len(candidates),
            "distributors": distributors, "accumulators": accumulators, "sell_hubs": sell_hubs,
            "independent_sellers": indep_sellers, "alerts": dist_alerts + acc_alerts,
            "degraded": bool(n_degraded or not conc.get("available", False)),
            "n_degraded": n_degraded, "n_nonce_cached": n_cached,
            "coverage": {"seeded_discovery_errors": seed_errors,
                         "candidates_unread": sum(1 for c in results if c.get("_degraded")),
                         "concentration": conc.get("available", False)}}


_kn_d, _kn_a = {}, {}


def build_onchain_radar(token=None, tickers=None, budget_sec=ONCHAIN_RADAR_BUDGET):
    global _kn_d, _kn_a
    _kn_d, _kn_a = {}, {}
    tokens = [token.upper()] if token else (_parse_tickers(tickers) or _cat_a_tokens())
    cfg = json.loads(WALLETS.read_text()).get("tokens", {})
    labels = _entity_labels()
    t0 = time.time()
    result = {"scanned": [], "tokens": {}, "alerts_total": 0, "partial": False,
              "tokens_total": len(tokens), "tokens_done": 0, "tokens_remaining": []}
    _write_progress("onchain_radar", 0, len(tokens), None, t0)
    for i, tk in enumerate(tokens):
        # SPEC 29: self-budget below the orchestrator timeout → return finished tokens as partial,
        # never an all-or-nothing bare timeout error.
        if time.time() - t0 > budget_sec:
            result["partial"] = True
            result["tokens_remaining"] = tokens[i:]
            _write_progress("onchain_radar", result["tokens_done"], len(tokens), tk, t0,
                            partial=True, last=tokens[i - 1] if i else None)
            break
        meta = cfg.get(tk)
        if not meta:
            result["tokens"][tk] = {"available": False, "reason": "untracked"}
        else:
            try:
                result["tokens"][tk] = _token_pass(tk, meta, labels)
            except Exception as e:  # noqa: BLE001
                result["tokens"][tk] = {"available": False, "reason": f"error: {str(e)[:120]}"}
        t = result["tokens"][tk]
        result["alerts_total"] += len(t.get("alerts", []))
        result["scanned"].append(tk)
        result["tokens_done"] = i + 1
        _write_progress("onchain_radar", i + 1, len(tokens), tk, t0,
                        last=f"{tk}({len(t.get('distributors', []))} dist/{len(t.get('accumulators', []))} acc)")
    # perp-side liq radar — only if budget remains (SPEC 29: don't blow the timeout on the tail call).
    if not result["partial"] and time.time() - t0 <= budget_sec:
        result["perp_liqs"] = _perp_liqs(None)
    else:
        result["perp_liqs"] = {"available": False, "reason": "skipped — sweep over budget (run separately)"}
    _write_progress("onchain_radar", result["tokens_done"], len(tokens), None, t0, partial=result["partial"])
    return result


def render_human(r):
    print(C.c(f"═══ ONCHAIN RADAR — {len(r['scanned'])} Cat A · {r['alerts_total']} new/accel ═══", "bold", "cyan"))
    for tk in r["scanned"]:
        t = r["tokens"].get(tk, {})
        if not t.get("available"):
            print(C.c(f"  {tk}: {t.get('reason','?')}", "grey")); continue
        p = t["perp"]
        fs = f"{p['funding_4h']:+.3f}%/4h" if p.get("funding_4h") is not None else "n/a"
        print(C.c(f"\n{tk}", "bold") + C.c(f"  funding {fs}{' ⛔veto' if p['short_vetoed'] else ''}"
              f"  · {len(t['distributors'])} dist / {len(t['accumulators'])} accum", "grey"))
        for c in t["distributors"][:5]:
            col = "red" if c["confidence"] == "HIGH" else "yellow"
            usd = f"${c['balance_usd']:,.0f}" if c["balance_usd"] is not None else "?"
            print("  " + C.c(f"{c['confidence']:4} SELL", "bold", col) + f" {c['address'][:12]}… {usd} {c['holder_type']}"
                  + (f" ⇐{c['seeded_from']}" if c["seeded"] else ""))
        for a in t["accumulators"][:5]:
            usd = f"${a['balance_usd']:,.0f}" if a["balance_usd"] is not None else "?"
            print("  " + C.c("ACC  BUY ", "bold", "green") + f" {a['address'][:12]}… {usd} net+{a['recent_net']:.0f}")
    pl = r.get("perp_liqs", {})
    if pl.get("available") and pl["positions"]:
        print(C.c("\n── perp positions (whales) ──", "grey"))
        for p in pl["positions"][:8]:
            flag = C.c(" ⚠NEAR-LIQ", "bold", "red") if p["near_liq"] else ""
            print(f"  {p['coin']:8} {p['side']:5} ${p.get('value_usd','?')} entry {p['entry']} liq {p['liq']} ({p['liq_distance_pct']}% away){flag}  {p['whale']}")


def main():
    ap = argparse.ArgumentParser(description="Full on-chain + perp radar (distribution + accumulation + liqs)")
    ap.add_argument("token", nargs="?", default=None)
    ap.add_argument("--tickers", default=None, help="SPEC 29: comma/list subset to sweep")
    ap.add_argument("--budget", type=int, default=ONCHAIN_RADAR_BUDGET, help="SPEC 29: time budget (s)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)
    r = build_onchain_radar(args.token, tickers=args.tickers, budget_sec=args.budget)
    if args.json:
        print(json.dumps(r))
    else:
        render_human(r)


if __name__ == "__main__":
    main()
