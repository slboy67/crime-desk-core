#!/usr/bin/env python3
"""verify_wallet.py — ad-hoc on-chain address lookup (SPEC 14, the §8 intel workflow).

When someone hands the desk a wallet ("0x… is dumping ESPORTS, $400K, received tokens
yesterday"), §8 says verify the identity/funding ON-CHAIN before acting. bscscan free
tier rejects BSC, so this routes through Moralis (covers BSC+ETH wallet token-transfers),
the same path onchain.safe_history uses.

  orchestrator.py verify_wallet '{"address":"0x..","token":"ESPORTS"}'

build_verify(address, token, days) -> dict. The headline: is this wallet SEEDED by a
tracked team/safe (= staging, not independent — §8) or an independent/CEX-funded holder,
and is it DISTRIBUTING now.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import colors as C
from onchain import (token_transfers, MoralisError, MoralisQuotaExhausted, _entity_labels,
                     _MORALIS_CHAIN, _get_json, classify_destination, balance_of, probe_contract)

WALLETS = ROOT / "config" / "tracked_wallets.json"
# tracked-wallet tiers that mean "operator staging" — inbound from one = seeded (§8)
SEED_TIERS = {"team", "distribution", "passthrough", "safe", "treasury", "mega", "mega-safe"}
# SPEC 21 — short aliases → tracked-config chain keys (so `chain="eth"` etc. resolves)
CHAIN_ALIASES = {"bsc": "binance-smart-chain", "bnb": "binance-smart-chain",
                 "binance-smart-chain": "binance-smart-chain", "eth": "ethereum",
                 "ethereum": "ethereum", "base": "base", "polygon": "polygon",
                 "matic": "polygon", "arbitrum": "arbitrum", "arb": "arbitrum",
                 "optimism": "optimism", "op": "optimism"}
CEX_HINTS = ("cex", "binance", "bybit", "gate", "kraken", "bitget", "okx", "kucoin", "mexc", "htx", "coinbase")
DEX_HINTS = ("dex", "pancake", "router", "pool", "uniswap", "1inch", "odos", "lp", " v2", " v3")


def _live_price(token):
    """LIVE USD price from the perp ticker (Bybit lastPrice, then Binance). Returns
    (price, source). The config `price_usd` is a stale manual field — do NOT value off it
    (it was ~13× high on ESPORTS); use the live market price for the USD valuation."""
    sym = f"{token}USDT"
    by = _get_json(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}", retries=0)
    try:
        return float(by["result"]["list"][0]["lastPrice"]), "bybit"
    except (TypeError, KeyError, IndexError, ValueError):
        pass
    bn = _get_json(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}", retries=0)
    try:
        return float(bn["price"]), "binance"
    except (TypeError, KeyError, ValueError):
        pass
    return None, None


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _classify_addr(addr, tracked, labels):
    """(kind, label) for a counterparty address. kind ∈ tracked-safe|cex|dex|entity|unknown."""
    a = (addr or "").lower()
    if a in tracked:
        return ("tracked-safe", f"{tracked[a]['label']} ({tracked[a]['tier']})")
    name = labels.get(a)
    if name:
        low = name.lower()
        if any(h in low for h in CEX_HINTS):
            return ("cex", name)
        if any(h in low for h in DEX_HINTS):
            return ("dex", name)
        return ("entity", name)
    return ("unknown", None)


def queryable_chains(tok, chain=None):
    """SPEC 21 — Moralis-supported chains a token is deployed on, as {chain_key: contract}.
    With `chain` given (alias-normalized), restrict to that single chain; empty dict if the
    token isn't deployed there (caller surfaces an explicit unavailable, never a false read)."""
    contracts = tok.get("contracts", {}) or {}
    supported = {ck: c for ck, c in contracts.items() if ck in _MORALIS_CHAIN}
    if chain:
        ck = CHAIN_ALIASES.get(chain.lower().strip(), chain.lower().strip())
        return {ck: supported[ck]} if ck in supported else {}
    return supported


# ── SPEC 53: quote-leg settlement detection ─────────────────────────────────────
# The settlement asset is invisible to a token-scoped read: an ESPORTS drip-sell showed
# ~200-token clips to an "unknown hub" while the USDT leg carried ~$530K in ~2,500
# micro-settlements from one swap counterparty (a live TWAP bot). Memory:
# feedback_check_stable_leg_before_calling_transfers_not_sells.
QUOTE_ASSETS = {
    "binance-smart-chain": {
        "USDT": {"contract": "0x55d398326f99059ff775485246999027b3197955", "decimals": 18, "stable": True},
        "WBNB": {"contract": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", "decimals": 18, "stable": False},
    },
    "ethereum": {
        "USDT": {"contract": "0xdac17f958d2ee523a2206206994597c13d831ec7", "decimals": 6, "stable": True},
        "USDC": {"contract": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "decimals": 6, "stable": True},
        "WETH": {"contract": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "decimals": 18, "stable": False},
    },
}


def _quote_transfers(address, chain_key, days):
    """The wallet's quote-leg transfers — ONE token_transfers read per quote asset
    (quota discipline). Returns {asset: [txs]}; raises on total failure (caller degrades)."""
    out = {}
    for asset, q in QUOTE_ASSETS.get(chain_key, {}).items():
        txs, _src, _partial = token_transfers(address, q["contract"], chain_key,
                                              days=days, decimals=q["decimals"])
        out[asset] = txs
    return out


def _settlement_read(a, outs, quote_by_asset, chain_key):
    """Pure correlation of token-out clips vs quote-leg clips.

    Detection: shared tx hashes (a swap's two legs), or mirrored cadence (≥5 token-out
    clips with quote-ins ≥50% as many inside the out-window ±1d). Returns
    (settlement_dict, accumulating_quote)."""
    from datetime import timedelta
    qmeta = QUOTE_ASSETS.get(chain_key, {})
    out_hashes = {t.get("transaction_hash") for t in outs if t.get("transaction_hash")}
    out_dts = [_ts(t["block_timestamp"]) for t in outs if t.get("block_timestamp")]
    lo = (min(out_dts) - timedelta(days=1)) if out_dts else None
    hi = (max(out_dts) + timedelta(days=1)) if out_dts else None
    last_out_dt = max(out_dts) if out_dts else None

    def val(t):
        try:
            return float(t.get("value_decimal") or 0)
        except (TypeError, ValueError):
            return 0.0

    best, any_quote_in = None, 0
    for asset, txs in (quote_by_asset or {}).items():
        q_ins = [t for t in txs if (t.get("to_address") or "").lower() == a]
        q_outs = [t for t in txs if (t.get("from_address") or "").lower() == a]
        any_quote_in += len(q_ins)
        if not q_ins:
            continue
        hash_hits = [t for t in q_ins if t.get("transaction_hash") in out_hashes]
        in_window = [t for t in q_ins
                     if lo and hi and _ts(t.get("block_timestamp"))
                     and lo <= _ts(t["block_timestamp"]) <= hi]
        detected = bool(hash_hits) or (len(outs) >= 5 and len(in_window) >= 0.5 * len(outs))
        if not detected:
            continue
        clips = hash_hits or in_window
        by_cp = {}
        for t in clips:
            src = (t.get("from_address") or "").lower()
            by_cp[src] = by_cp.get(src, 0) + 1
        stable = bool(qmeta.get(asset, {}).get("stable"))
        amount_in = round(sum(val(t) for t in clips), 2)
        after = [t for t in q_outs if last_out_dt and _ts(t.get("block_timestamp"))
                 and _ts(t["block_timestamp"]) >= last_out_dt]
        cand = {"available": True, "mode": "dex_swap_sell", "asset": asset,
                "n_settlements": len(clips),
                "usd_in": (amount_in if stable else None), "amount_in": amount_in,
                "counterparty": (max(by_cp, key=by_cp.get) if by_cp else None),
                "usd_out_after": (round(sum(val(t) for t in after), 2) if stable else None),
                "hash_matched": bool(hash_hits)}
        if best is None or cand["n_settlements"] > best["n_settlements"]:
            best = cand
    if best:
        return best, False
    # quote flowing IN with no correlated token-out = loading for a future op — surface,
    # don't verdict (the §0.5 judgment stays the Designer's)
    accumulating_quote = bool(any_quote_in and not outs)
    return {"available": True, "mode": None, "n_quote_in": any_quote_in}, accumulating_quote


def build_verify(address, token, days=180, price=None, chain=None):
    address = address.strip()
    token = token.upper().replace("USDT", "")
    tok = json.loads(WALLETS.read_text()).get("tokens", {}).get(token)
    if not tok:
        return {"address": address, "token": token, "available": False,
                "reason": f"{token} not in tracked config (need its contract to query transfers)"}
    contracts = tok.get("contracts", {}) or {}
    # SPEC 21: query ALL deployed chains (operators distribute multi-chain → single-chain reads
    # a non-default-chain-active wallet as false DORMANT). `chain` arg restricts to one.
    query = queryable_chains(tok, chain)
    if not query:
        if chain:
            return {"address": address, "token": token, "available": False,
                    "reason": f"{token} not deployed on {chain} (chains: {list(contracts)})"}
        return {"address": address, "token": token, "available": False,
                "reason": f"no Moralis-supported contract for {token} ({list(contracts)})"}
    if price is not None:                       # caller supplied a price (radar fetches once/token)
        price_source = "provided"
    else:
        price, price_source = _live_price(token)   # live perp price, NOT the stale config price_usd
    tracked = {w["address"].lower(): {"label": w.get("label"), "tier": (w.get("tier") or "").lower()}
               for w in tok.get("wallets", [])}
    labels = _entity_labels()

    decimals = tok.get("decimals", 18)
    # SPEC 17 per chain: provider-fallback pool (Moralis → free-RPC getLogs). SPEC 21: aggregate
    # across chains; a chain whose providers all fail is recorded degraded (NOT read as 0 sells).
    txs, chains_status = [], {}
    for ck, contract in query.items():
        try:
            ctxs, csource, cpartial = token_transfers(address, contract, ck, days=days, decimals=decimals)
        except Exception as e:  # noqa: BLE001
            chains_status[ck] = {"available": False, "degraded": True, "reason": str(e)[:120]}
            continue
        for t in ctxs:
            t = dict(t)
            t["_chain"] = ck
            txs.append(t)
        chains_status[ck] = {"available": True, "source": csource, "partial": cpartial, "n": len(ctxs)}
    ok_chains = [ck for ck, s in chains_status.items() if s.get("available")]
    degraded_chains = [ck for ck, s in chains_status.items() if not s.get("available")]
    if not ok_chains:
        # SPEC-1b: ALL providers on ALL chains failed → degrade EXPLICITLY, never null/zero fields
        # (downstream must distinguish "0 sells" from "we couldn't read it").
        reason = "; ".join(f"{ck}: {chains_status[ck].get('reason')}" for ck in degraded_chains)
        return {"address": address, "token": token, "available": False, "degraded": True,
                "chains": chains_status, "reason": f"all on-chain providers failed: {reason[:140]}"}
    # SPEC 58: the primary chain is where the ADDRESS actually HOLDS the token (balanceOf>0),
    # NOT where stray bridge transfers landed — the FOLKS miss read an ETH holder as avalanche
    # because avalanche had more transfer rows. balanceOf is cross-checked (§3) in balance_of.
    holdings, balance_chains_checked = {}, []
    for ck, contract in query.items():
        try:
            b = balance_of(address, contract, ck, decimals=decimals)
        except Exception:  # noqa: BLE001 — a balance read never blocks the flow verdict
            b = {"available": False, "chain": ck, "reason": "balance read error"}
        balance_chains_checked.append(ck)
        if b.get("available") and (b.get("value") or 0) > 0:
            holdings[ck] = b
    holds_on = sorted(holdings, key=lambda c: -(holdings[c].get("value") or 0))
    holding_ok = [c for c in holds_on if c in ok_chains]
    if holding_ok:
        chain = holding_ok[0]                                     # where the balance lives
    else:
        chain = max(ok_chains, key=lambda c: chains_status[c]["n"])   # fallback: most-active chain
    source = "+".join(sorted({chains_status[c]["source"] for c in ok_chains}))
    partial = any(chains_status[c].get("partial") for c in ok_chains) or bool(degraded_chains)

    # SPEC 58: balance_now = the REAL on-chain balance on the primary chain (NOT net_flow_window).
    # On the holding chain we already have the cross-checked read; otherwise read it explicitly so
    # the field is honest (degrade-explicit, never a fabricated 0).
    if chain in holdings:
        balance_now = dict(holdings[chain])
    else:
        try:
            balance_now = balance_of(address, query.get(chain), chain, decimals=decimals)
        except Exception:  # noqa: BLE001
            balance_now = {"available": False, "chain": chain, "reason": "balance read error"}
    if balance_now.get("available") and balance_now.get("value") is not None and price is not None:
        balance_now["value_usd"] = round(balance_now["value"] * price, 2)

    a = address.lower()
    ins = [t for t in txs if (t.get("to_address") or "").lower() == a]
    outs = [t for t in txs if (t.get("from_address") or "").lower() == a]

    def amt(t):
        try:
            return float(t.get("value_decimal") or 0)
        except (TypeError, ValueError):
            return 0.0

    # funded_by — aggregate inbound by source, classify, flag seeded staging
    by_src = {}
    for t in ins:
        src = (t.get("from_address") or "").lower()
        kind, label = _classify_addr(src, tracked, labels)
        e = by_src.setdefault(src, {"address": src, "kind": kind, "label": label, "amount": 0.0,
                                    "first_ts": t.get("block_timestamp"), "chain": t.get("_chain") or chain})
        e["amount"] += amt(t)
        if t.get("block_timestamp") and (e["first_ts"] is None or t["block_timestamp"] < e["first_ts"]):
            e["first_ts"] = t["block_timestamp"]
    funded_by = sorted(by_src.values(), key=lambda x: -x["amount"])

    # SPEC-67: a tracked LABEL must not override chain FACTS. Before a tracked source counts
    # as seeding (= operator-staging, §8), probe it on-chain: if it's a DEX pool its inbound
    # clips are ordinary swap fills, NOT a seed. Probe ONLY the sources that would otherwise
    # seed (tracked + SEED_TIERS) — the minimal RPC footprint. A pool overrides kind→dex-pool,
    # raises label_conflict, and is excluded from seeded_sources regardless of its tier.
    label_conflicts = []
    for f in funded_by:
        a_src = f["address"]
        if a_src in tracked and tracked[a_src]["tier"] in SEED_TIERS:
            try:
                pr = probe_contract(a_src, f.get("chain") or chain)
            except Exception:  # noqa: BLE001 — a probe failure never fabricates a seed verdict
                pr = {"available": False}
            if pr.get("is_pool"):
                f["kind"] = "dex-pool"
                f["label_conflict"] = True
                f["probe"] = {k: pr.get(k) for k in ("is_contract", "is_pool", "token0", "token1", "fee")}
                label_conflicts.append({"address": a_src, "label": f.get("label"),
                                        "tracked_tier": tracked[a_src]["tier"], "probe": f["probe"]})
                sys.stderr.write(
                    f"WARN SPEC-67: tracked source {a_src} ({f.get('label')}) probes as a DEX pool "
                    f"(token0={pr.get('token0')} token1={pr.get('token1')} fee={pr.get('fee')}) — "
                    f"label_conflict, NOT counted as seeding\n")
    seeded_sources = [f for f in funded_by if f["address"] in tracked
                      and tracked[f["address"]]["tier"] in SEED_TIERS
                      and f.get("kind") != "dex-pool"]
    seeded_staging = bool(seeded_sources)

    net = round(sum(amt(t) for t in ins) - sum(amt(t) for t in outs), 4)
    balance_usd = round(net * price, 2) if price is not None else None

    all_ts = [t["block_timestamp"] for t in txs if t.get("block_timestamp")]
    first_seen = min(all_ts) if all_ts else None
    out_ts = [t["block_timestamp"] for t in outs if t.get("block_timestamp")]
    last_out = max(out_ts) if out_ts else None
    last_out_dt = _ts(last_out) if last_out else None
    days_since_out = ((datetime.now(timezone.utc) - last_out_dt).days if last_out_dt else None)

    recent_out = []
    for t in sorted(outs, key=lambda x: x.get("block_timestamp") or "", reverse=True)[:5]:
        kind, label = _classify_addr(t.get("to_address"), tracked, labels)
        recent_out.append({"to": t.get("to_address"), "to_kind": kind, "to_label": label,
                           "value": amt(t), "ts": t.get("block_timestamp"), "chain": t.get("_chain")})
    distributing = bool(out_ts and days_since_out is not None and days_since_out <= 14)

    # SPEC 16 — accumulation + cost-basis proxy. recent (≤7d) net flow; a net-BUYER that
    # received from a DEX/unknown source (not a tracked safe) = accumulating.
    now = datetime.now(timezone.utc)

    def _recent(rows):
        return [t for t in rows if (_ts(t.get("block_timestamp")) and (now - _ts(t["block_timestamp"])).days <= 7)]
    recent_in, recent_out_rows = _recent(ins), _recent(outs)
    recent_net = round(sum(amt(t) for t in recent_in) - sum(amt(t) for t in recent_out_rows), 4)
    bought_from_market = any(_classify_addr(t.get("from_address"), tracked, labels)[0] in ("dex", "unknown")
                             for t in recent_in)
    accumulating = bool(recent_net > 0 and bought_from_market and not seeded_staging)
    first_dt = _ts(first_seen) if first_seen else None
    held_days = ((now - first_dt).days if first_dt else None)

    if seeded_staging:
        holder_type = "operator-seeded"
    elif distributing and (held_days or 0) >= 14:
        holder_type = "early-winner"          # independent, held a while, now cashing out (≠ operator dump)
    elif distributing:
        holder_type = "recent-seller"
    elif accumulating:
        holder_type = "accumulator"
    elif net > 0:
        holder_type = "holder"
    else:
        holder_type = "dormant"

    # SPEC 15 refinement (CEX-sourced funding): categorize the dominant inbound source,
    # don't drop a CEX hot wallet to "unknown". A wallet pulling from a CEX then dumping on
    # DEX is the operator sourcing supply off-exchange to obscure lineage (distinct from a
    # tracked-safe seed) — name the CEX and flag the CEX-withdrawal→fresh→DEX-dump pattern.
    top = funded_by[0] if funded_by else None
    if seeded_staging:
        funded_by_kind = "seeded"
    elif top and top["kind"] == "cex":
        funded_by_kind = "cex-withdrawal"
    elif top:
        funded_by_kind = top["kind"]
    else:
        funded_by_kind = None
    cex_sourced = bool(top and top["kind"] == "cex")
    funded_by_cex = top["label"] if cex_sourced else None
    cex_sourced_distribution = bool(cex_sourced and distributing)

    # SPEC 15 refinement (behavioral clustering): expose aggregated sell destinations (the
    # downstream hubs this wallet feeds) + a clip signature so the radar can cluster wallets
    # feeding the SAME hub as one operator, even when funding lineage is unknown.
    by_dest = {}
    for t in outs:
        d = (t.get("to_address") or "").lower()
        if not d or d == a:
            continue
        kind, label = _classify_addr(d, tracked, labels)
        # SPEC 28: cex-execution (the real sell) vs staging-internal (operator-controlled consolidation)
        dest_kind = classify_destination(kind, tracked.get(d, {}).get("tier"))
        e = by_dest.setdefault(d, {"address": d, "kind": kind, "label": label,
                                   "dest_kind": dest_kind, "amount": 0.0, "count": 0})
        e["amount"] += amt(t)
        e["count"] += 1
    for e in by_dest.values():
        e["amount"] = round(e["amount"], 4)
    sell_destinations = sorted(by_dest.values(), key=lambda x: -x["amount"])[:8]
    # SPEC 28: which dominates the OUTFLOW $ — CEX execution (the dump) vs internal staging (loading)?
    by_kind = {}
    for e in by_dest.values():
        by_kind[e["dest_kind"]] = by_kind.get(e["dest_kind"], 0.0) + e["amount"]
    distribution_mode = max(by_kind, key=by_kind.get) if by_kind else None
    out_amts = sorted(amt(t) for t in outs)
    clip_median = round(out_amts[len(out_amts) // 2], 4) if out_amts else None
    sig = {"clip_median": clip_median, "n_out": len(outs)}

    # SPEC 53: quote-leg settlement read — only when the wallet has token flows at all
    # (quota discipline), degrade-explicit on provider/quota failure.
    settlement, accumulating_quote = {"available": False, "reason": "no token flows — quote leg skipped"}, False
    flow_degraded = False   # SPEC-80: the quote-leg read failed (quota/provider) → flow picture incomplete
    if ins or outs:
        try:
            qtx = _quote_transfers(a, chain, days)
            settlement, accumulating_quote = _settlement_read(a, outs, qtx, chain)
            if settlement.get("mode") == "dex_swap_sell":
                distribution_mode = "dex_swap_sell"   # the clips were SELLS, not redistribution
        except Exception as e:  # noqa: BLE001 — quota failure never blocks the token verdict
            flow_degraded = True
            settlement = {"available": False, "reason": f"quote-leg unavailable: {str(e)[:80]}"}

    # SPEC-80: DORMANT is a positive "this wallet is quiet" claim — only assert it on a
    # COMPLETE read. A degraded read (getlogs-partial token leg, a failed chain, or a
    # quota-failed quote leg) CANNOT establish dormancy, so it must never collapse to a
    # clean DORMANT (§3: a partial/placeholder print is a data FAILURE, not a datum). Fail
    # SAFE: carry a known distributor forward when out-flow evidence is already on record,
    # else surface DEGRADED (unverified, re-check). Live miss: 0xffa8 read n:0/partial →
    # false DORMANT while the full read was DISTRIBUTING -$33M→Bitget; that false negative
    # (waving off a firing distributor) is the worst direction for the desk.
    read_degraded = bool(partial) or flow_degraded
    if seeded_staging:
        verdict = "SEEDED-STAGING"
    elif accumulating:
        verdict = "ACCUMULATING"
    elif distributing:
        verdict = "DISTRIBUTING"
    elif net > 0:
        verdict = "INDEPENDENT-HOLDER"
    elif read_degraded:
        verdict = "DISTRIBUTING" if outs else "DEGRADED"
    else:
        verdict = "DORMANT"
    if flow_degraded:
        partial = True   # surface the incomplete read so callers never trust this as a clean verdict

    return {
        "address": address, "token": token, "chain": chain, "available": True, "degraded": False,
        # SPEC 21: multi-chain aggregation — primary `chain` above, full picture here
        "chains_queried": ok_chains, "degraded_chains": degraded_chains, "chains": chains_status,
        "source": source, "partial": partial,        # SPEC 17: getlogs fallback → partial (recent window only)
        "verdict": verdict, "seeded_staging": seeded_staging,
        "funded_by": funded_by[:5], "seeded_sources": seeded_sources,
        "label_conflicts": label_conflicts,   # SPEC-67: tracked labels refuted by on-chain probe
        "funded_by_kind": funded_by_kind, "funded_by_cex": funded_by_cex,
        "cex_sourced": cex_sourced, "cex_sourced_distribution": cex_sourced_distribution,
        "sell_destinations": sell_destinations, "distribution_mode": distribution_mode, "sig": sig,
        "settlement": settlement, "accumulating_quote": accumulating_quote,   # SPEC 53
        # SPEC 58: net_flow_window is IN-minus-OUT over the window — NOT a balance (it printed 0 for
        # a 0.33%-holder and negatives for emptied wallets; the Designer misread it twice). balance_now
        # is the real cross-checked balanceOf (§3).
        "first_seen_ts": first_seen, "net_flow_window": net, "net_flow_window_usd": balance_usd,
        "balance_now": balance_now, "holds_on": holds_on, "balance_chains_checked": balance_chains_checked,
        "price_usd": price, "price_source": price_source,
        "in_count": len(ins), "out_count": len(outs), "last_out_ts": last_out,
        "days_since_out": days_since_out, "distributing": distributing,
        "recent_net": recent_net, "accumulating": accumulating,
        "held_days": held_days, "holder_type": holder_type,
        "recent_outbounds": recent_out,
    }


def render_human(v):
    if not v.get("available"):
        print(f"# {v['token']} {v['address'][:10]}… — unavailable: {v.get('reason')}")
        return
    col = "red" if v["verdict"] in ("SEEDED-STAGING", "DISTRIBUTING") else "yellow" if v["verdict"] in ("INDEPENDENT-HOLDER", "DEGRADED") else "grey"
    print(C.c(f"═══ {v['token']} {v['address']} ═══  {v['verdict']}", "bold", col))
    if v["verdict"] == "DEGRADED":   # SPEC-80: an incomplete read is NOT a confirmed dormant
        print(C.c("  ⚠ DEGRADED — on-chain flow read incomplete (partial/quota); verdict UNVERIFIED, re-check (§3)", "bold", "yellow"))
    pxs = f" @ ${v['price_usd']:g} ({v['price_source']})" if v.get("price_usd") is not None else ""
    bn = v.get("balance_now") or {}
    if bn.get("available") and bn.get("value") is not None:
        pct = f" ({bn['pct_supply']:.3g}% supply)" if bn.get("pct_supply") is not None else ""
        xs = "" if bn.get("cross_checked") else " ⚠1-src"
        usd = f" (${bn['value_usd']:,.0f}{pxs})" if bn.get("value_usd") is not None else ""
        balnow = f"{bn['value']:.2f} {v['token']}{pct}{usd} on {bn.get('chain')}{xs}"
    else:
        balnow = f"n/a ({(bn.get('reason') or 'unread')})"
    nf = v['net_flow_window']
    print(f"  balance_now {balnow} · net_flow {nf:+.2f} {v['token']} · in {v['in_count']} / out {v['out_count']} · last out {v['last_out_ts'] or '—'} ({v['days_since_out']}d)")
    if v["seeded_staging"]:
        s = ", ".join(f"{x['label']}" for x in v["seeded_sources"])
        print(C.c(f"  🔴 SEEDED by tracked safe(s): {s} — staging, NOT independent (§8)", "bold", "red"))
    for lc in v.get("label_conflicts", []):
        p = lc.get("probe", {})
        print(C.c(f"  ⚠ label_conflict: {lc['label']} ({lc['tracked_tier']}) is a DEX pool "
                  f"(token0={p.get('token0')} token1={p.get('token1')} fee={p.get('fee')}) — NOT seeding", "yellow"))
    print("  funded_by:")
    for f in v["funded_by"]:
        tag = "  ⚠pool" if f.get("label_conflict") else ""
        print(f"    {f['kind']:12} {f['label'] or f['address'][:14]:24} {f['amount']:.2f} {v['token']}{tag}")
    if v["recent_outbounds"]:
        print("  recent out:")
        for o in v["recent_outbounds"]:
            print(f"    → {o['to_kind']:10} {o['to_label'] or (o['to'] or '')[:14]:22} {o['value']:.2f} {o['ts']}")


def attach_freshness(v, freshness_fn=None):
    """SPEC-98: attach the rotation-aware distribution-freshness verdict to a verify read
    (CLI surface only — build_verify itself stays lean because the SPEC-89 top-holder sweep
    calls it per holder and the legs would multiply cost). The wallet's own last_out_ts is
    the tracked leg; tape/on-chain rotation legs run inside rotation_freshness with their
    own degrade-explicit defaults. NEVER raises — a freshness failure must not break the
    wallet verdict."""
    if not isinstance(v, dict) or not v.get("available"):
        return v
    try:
        if freshness_fn is None:
            import rotation_freshness as RF
            freshness_fn = lambda ticker, last_out_ts: RF.build_freshness(ticker, last_out_ts=last_out_ts)  # noqa: E731
        fr = freshness_fn(v.get("token"), v.get("last_out_ts"))
        if isinstance(fr, dict) and fr.get("verdict"):
            v["distribution_freshness"] = {"verdict": fr["verdict"], "line": fr.get("line"),
                                           "note": fr.get("note")}
    except Exception:  # noqa: BLE001 — degrade silently, the wallet verdict stands alone
        pass
    return v


def main():
    ap = argparse.ArgumentParser(description="Ad-hoc on-chain wallet verification (§8)")
    ap.add_argument("address")
    ap.add_argument("--token", required=True)
    ap.add_argument("--chain", default=None, help="restrict to one chain (eth/bsc/base); default = all deployed (SPEC 21)")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)
    v = build_verify(args.address, args.token, args.days, chain=args.chain)
    attach_freshness(v)   # SPEC-98: FRESH/FROZEN/ROTATED on the distribution clock
    if args.json:
        print(json.dumps(v))
    else:
        render_human(v)
        fr = v.get("distribution_freshness")
        if fr and fr.get("line"):
            col = "red" if fr["verdict"] == "ROTATED" else "yellow" if fr["verdict"] == "FROZEN" else "grey"
            print(C.c(f"  {fr['line']}", "bold", col))
            if fr.get("note"):
                print(C.c(f"    ↳ {fr['note']}", col))


if __name__ == "__main__":
    main()
