#!/usr/bin/env python3
"""accumulation_radar.py — operator accumulation radar (SPEC-76, the LONG-side mirror).

The on-chain radars are token-centric + distribution-biased (watch a token's wallets dump →
short). This is the INVERSE and wallet-centric: watch the mapped OPERATOR cluster wallets
(team/op/distribution + the cross-Cat-A escrows, e.g. 0x73d8 holding positions across ~12
tokens) ACCUMULATE a NEW untracked token → the §1 sub-edge-1 / §0.6 operator-aligned LONG at
trap-formation, before the markup. The highest-value entry.

The pipeline (per §0.6 / multi-market OI construction — spot accumulation THEN perp markup):
  1. enumerate the cluster wallets' ERC20 inbounds (snapshot), diff vs a stored baseline (SPEC 55
     pattern) → NEW / >N%-GROWN positions;
  2. keep only UNTRACKED tokens (not already on the desk map) — candidate operator loads;
  3. classify ACTIVE accumulation (DEX swap / CEX withdrawal / open-market buy = tradeable load)
     vs PARKED allocation (inbound from a vesting/team/escrow safe = parked, NOT a pre-pump
     signal) — reuses verify_wallet's funded_by/source logic;
  4. CROSS WITH PERP CONSTRUCTION (the timing key): on-chain accumulation alone has no timing and
     can't fully disambiguate load-vs-park. Pull each candidate's perp and tier the confluence —
       IMMINENT  active load + perp construction firing (deep-neg trap-formation funding §4 + OI
                 building) → the operator revealed timing → the tradeable early-long;
       EARLY     accumulation present but perp dormant / flat / absent → loaded, not activated;
       PARK      parked allocation + no perp leg → not a pump-load, discard.
     The on-chain accumulation is the SPOT-side decompose the §4 long gate is missing: known
     operator accumulation resolves deep-neg funding = real trapped-shorts vs MM-hedge.

Caveats encoded (never hidden): accumulation ≠ imminent (discretionary timing); long-alongside-
operator still risks being exit liquidity if the stage is misread (§0.6). The backtest gate
(backtest_lead_time) is the go/no-go — see docs.

  orchestrator.py accumulation_radar '{}'                       # full cluster sweep
  orchestrator.py accumulation_radar '{"wallet":"0x73d8…"}'     # one operator wallet
"""
import argparse
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import colors as C
from onchain import _MORALIS_CHAIN, token_transfers   # SPEC-97: via the provider seam
from verify_wallet import build_verify
from regime_flip import live_perp, DEEP_NEG

STATE = ROOT / "state"
WALLETS = ROOT / "config" / "tracked_wallets.json"

# Operator-controlled tiers whose wallets we watch for a NEW load (§1 sub-edge 1). cex/burn/
# bridge/pool/dex are NOT operator inventory and are excluded.
CLUSTER_TIERS = {"team", "op", "distribution", "treasury", "mega", "mega-safe", "passthrough"}
ESCROW_MIN_TOKENS = 3       # an address holding positions across >= this many tokens = cross-cluster escrow
GROWTH_PCT = 50.0           # a position growing >= this % vs baseline = an accumulation event
OI_BUILD_PCT = 20.0         # OI up >= this % vs its baseline = "building" (perp construction)
ACCUM_DAYS = 7              # recent-inbound window for the accumulation snapshot
MAX_WALLETS_PER_RUN = 40    # quota discipline: cap wallets resolved per sweep (free Moralis daily quota)
ACCUM_RADAR_BUDGET = 350    # < the orchestrator timeout (SPEC 29 self-budget pattern)


# ───────────────────────── config / baseline I/O (test-patchable) ─────────────────────────
def _load_cfg():
    return json.loads(WALLETS.read_text())


def _tracked_contracts(cfg):
    """The set of token contracts the desk ALREADY tracks — a load into one of these is not a
    NEW name, so it's not an accumulation-radar candidate (the radar hunts untracked loads)."""
    out = set()
    for _tk, meta in (cfg.get("tokens", {}) or {}).items():
        for c in (meta.get("contracts", {}) or {}).values():
            if isinstance(c, str) and c:
                out.add(c.lower())
    return out


def _baseline_path(addr):
    return STATE / f"accum_baseline_{addr.lower()}.json"


def _load_baseline(addr):
    try:
        return json.loads(_baseline_path(addr).read_text())
    except Exception:
        return {}


def _save_baseline(addr, holds):
    """Atomic snapshot write (the SPEC 55 baseline pattern) — stores {contract:{in_amount}} so the
    next sweep diffs against it. Best-effort; never raises into a verdict."""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        p = _baseline_path(addr)
        fd, tmp = tempfile.mkstemp(dir=str(STATE), prefix=f".{p.stem}.", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(holds))
        os.replace(tmp, str(p))
    except Exception:
        pass


def _to_epoch(ts):
    """ISO-8601 (…Z) | epoch number | None → epoch seconds | None."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


# ───────────────────────── 1. wallet-centric cluster enumeration ─────────────────────────
def operator_cluster_wallets(cfg):
    """Req 1: the mapped operator cluster wallets (team/op/distribution + cross-Cat-A escrows),
    deduped by address across every token. An address holding positions across >= ESCROW_MIN_TOKENS
    tokens is flagged `is_escrow` (the 0x73d8-style cross-cluster escrow). cex/burn/bridge/pool are
    excluded — they aren't operator inventory."""
    by_addr = {}
    for tk, meta in (cfg.get("tokens", {}) or {}).items():
        for w in meta.get("wallets", []):
            if (w.get("tier") or "").lower() not in CLUSTER_TIERS:
                continue
            a = (w.get("address") or "").lower()
            if not a:
                continue
            e = by_addr.setdefault(a, {"address": a, "label": w.get("label"), "tier": w.get("tier"),
                                       "chain": w.get("chain") or "binance-smart-chain",
                                       "tokens": [], "is_escrow": False})
            if tk.upper() not in e["tokens"]:
                e["tokens"].append(tk.upper())
    for e in by_addr.values():
        e["is_escrow"] = len(e["tokens"]) >= ESCROW_MIN_TOKENS
    # escrows first (they touch the most names → highest discovery value), then by token count
    return sorted(by_addr.values(), key=lambda e: (not e["is_escrow"], -len(e["tokens"])))


# ───────────────────────── 2. inbound snapshot + diff ─────────────────────────
def _default_tokentx(addr, chains, days):
    """Live all-token ERC20 inbound read via the SPEC-97 provider seam (etherscan on ETH,
    moralis elsewhere), tagged with the source chain. Quota-metered + cached inside the seam.
    Degrades to [] per chain on provider/quota failure."""
    out = []
    for ck in chains:
        if ck not in _MORALIS_CHAIN:
            continue
        try:
            txs, _src, _partial = token_transfers(addr, None, ck, days=days)
        except Exception:  # noqa: BLE001 — degrade-explicit per chain, never fabricate
            continue
        for t in txs:
            t.setdefault("_chain", ck)
        out += txs
    return out


def wallet_token_inbounds(wallet, chains, days, tokentx_fn=None):
    """Req 1/2: enumerate a wallet's ERC20 INBOUNDS grouped per token (the accumulation snapshot).
    OUTbound transfers (from the wallet) are excluded — only what it RECEIVED counts as loading.
    Returns {token_contract: {symbol, chain, in_amount, n_in, first_in_ts, last_in_ts,
    last_in_from}}."""
    tokentx_fn = tokentx_fn or _default_tokentx
    w = (wallet or "").lower()
    holds = {}
    for t in tokentx_fn(wallet, chains, days):
        if (t.get("to_address") or "").lower() != w:
            continue
        c = (t.get("address") or "").lower()
        if not c:
            continue
        try:
            val = float(t.get("value_decimal") or 0)
        except (TypeError, ValueError):
            val = 0.0
        e = holds.setdefault(c, {"contract": c, "symbol": t.get("token_symbol"),
                                 "chain": t.get("_chain"), "in_amount": 0.0, "n_in": 0,
                                 "first_in_ts": None, "last_in_ts": None, "last_in_from": None})
        e["in_amount"] += val
        e["n_in"] += 1
        ts = t.get("block_timestamp")
        if ts:
            if e["first_in_ts"] is None or ts < e["first_in_ts"]:
                e["first_in_ts"] = ts
            if e["last_in_ts"] is None or ts > e["last_in_ts"]:
                e["last_in_ts"] = ts
                e["last_in_from"] = (t.get("from_address") or "").lower()
    for e in holds.values():
        e["in_amount"] = round(e["in_amount"], 6)
    return holds


def is_accumulation_candidate(contract, tracked_contracts):
    """Req 2: a token is a candidate operator-load only when it is NOT already a desk-tracked
    name (the radar hunts loads into UNTRACKED tokens — a tracked one already has its own pass)."""
    return (contract or "").lower() not in {c.lower() for c in tracked_contracts}


def diff_holdings(baseline, holds):
    """Req 2: NEW position (absent from baseline) or one growing >= GROWTH_PCT vs baseline. A
    flat/shrinking position is not flagged. Returns [{...holding, contract, change}]."""
    out = []
    for c, h in holds.items():
        amt = h.get("in_amount") or 0
        prev = baseline.get(c)
        if prev is None:
            out.append({**h, "contract": c, "change": "NEW"})
        else:
            p = prev.get("in_amount") or 0
            if p > 0 and (amt - p) / p * 100.0 >= GROWTH_PCT:
                out.append({**h, "contract": c, "change": "GROWN"})
    return out


# ───────────────────────── 3. active load vs parked allocation ─────────────────────────
def classify_accumulation(v):
    """Req 3: ACTIVE accumulation (DEX swap / CEX withdrawal / open-market buy = tradeable load)
    vs PARKED allocation (inbound from a vesting/team/escrow safe = parked, NOT a pre-pump signal).
    Reuses verify_wallet's funded_by/source classification. Returns (mode, reason); mode ∈
    active | parked | unknown (degrade-explicit — an unresolved source is NOT silently 'active')."""
    if not isinstance(v, dict) or not v.get("available"):
        return ("unknown", "verify unavailable")
    if v.get("seeded_staging"):
        return ("parked", "inbound from a tracked team/escrow/vesting safe — allocation parked")
    fk = v.get("funded_by_kind")
    if fk == "cex-withdrawal":
        return ("active", "CEX withdrawal → open-market load")
    if fk in ("dex", "dex-pool"):
        return ("active", "DEX swap / open-market buy")
    if v.get("accumulating"):
        return ("active", "net-buying from market (DEX/unknown source)")
    top = (v.get("funded_by") or [None])[0]
    if isinstance(top, dict) and top.get("kind") in ("dex", "cex"):
        return ("active", f"inbound from {top.get('kind')} ({top.get('label')})")
    return ("unknown", "inbound source not disambiguated")


# ───────────────────────── 4. perp construction cross ─────────────────────────
def perp_state(token, perp_fn=None, oi_baseline=None):
    """Req 4: the token's perp construction state. FIRING = a live perp + deep-neg trap-formation
    funding (§4, <= DEEP_NEG) + OI building (>= OI_BUILD_PCT vs its baseline when known, else OI
    present). Firing means the operator has revealed timing → the tradeable early-long."""
    perp_fn = perp_fn or live_perp
    try:
        lp = perp_fn(token)
    except Exception:  # noqa: BLE001
        lp = None
    if not lp:
        return {"has_perp": False, "funding_4h": None, "deep_neg": False, "oi": None,
                "oi_chg_pct": None, "oi_building": False, "firing": False}
    f4 = lp.get("funding_4h")
    oi = lp.get("oi")
    deep_neg = bool(f4 is not None and f4 <= DEEP_NEG)
    if oi_baseline and oi is not None and oi_baseline > 0:
        oi_chg_pct = round((oi - oi_baseline) / oi_baseline * 100.0, 1)
        oi_building = oi_chg_pct >= OI_BUILD_PCT
    else:
        oi_chg_pct = None
        oi_building = bool(oi and oi > 0)   # no baseline → OI present is the weak fallback
    firing = bool(deep_neg and oi_building)
    return {"has_perp": True, "funding_4h": f4, "deep_neg": deep_neg, "oi": oi,
            "oi_chg_pct": oi_chg_pct, "oi_building": oi_building, "firing": firing}


def confluence_tier(mode, perp):
    """Req 4: rank the confluence. active + perp firing → IMMINENT (tradeable); parked + no perp
    → PARK (discard); everything else (loaded but perp dormant/flat/absent) → EARLY (arm a watch)."""
    if mode == "active" and perp.get("firing"):
        return "IMMINENT"
    if mode == "parked" and not perp.get("has_perp"):
        return "PARK"
    return "EARLY"


# ───────────────────────── DoD backtest gate (the go/no-go) ─────────────────────────
def backtest_lead_time(tokens, accum_events, pump_events):
    """DoD §9 backtest gate: for each cluster token, did the operator wallets accumulate BEFORE
    the pump, and with what lead-time? Pure/deterministic given the (first-active-accumulation,
    pump-start) timestamps the live harness gathers. accum_events/pump_events = {TOKEN: iso_ts}.
    A token with accumulation that LAGGED the pump (lead<=0) or no accumulation found is NOT a
    predictive signal — reported honestly. The summary's n_accumulated_before / n_tokens IS the
    go/no-go: most pumps led by a tradeable accumulation → real; concurrent/lagging → not."""
    rows = []
    n_before = 0
    for tk in tokens:
        a_e = _to_epoch(accum_events.get(tk))
        p_e = _to_epoch(pump_events.get(tk))
        if a_e is None:
            rows.append({"token": tk, "accumulated_before": False, "lead_days": None,
                         "note": "no accumulation found"})
            continue
        if p_e is None:
            rows.append({"token": tk, "accumulated_before": False, "lead_days": None,
                         "note": "no pump event"})
            continue
        lead_days = round((p_e - a_e) / 86400.0, 2)
        before = lead_days > 0
        if before:
            n_before += 1
        rows.append({"token": tk, "accumulated_before": before, "lead_days": lead_days,
                     "note": "accumulation led the pump" if before else "accumulation lagged the pump"})
    leads = [r["lead_days"] for r in rows if r["accumulated_before"]]
    return {"n_tokens": len(tokens), "n_accumulated_before": n_before, "rows": rows,
            "median_lead_days": (sorted(leads)[len(leads) // 2] if leads else None),
            "verdict": ("predictive" if n_before > len(tokens) / 2 else "not-predictive")}


# ───────────────────────── the composed radar ─────────────────────────
def _default_verify(addr, token, contract=None, chain=None):
    try:
        return build_verify(addr, token, chain=chain)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"verify unavailable: {str(e)[:80]}"}


def build_accumulation_radar(wallet=None, wallets=None, ticker=None, budget_sec=ACCUM_RADAR_BUDGET,
                             tokentx_fn=None, verify_fn=None, perp_fn=None, oi_baseline_fn=None):
    """The LONG-side mirror of distribution_radar, gated by perp timing. Enumerate the operator
    cluster wallets → inbound snapshot + baseline diff → untracked candidates → active/parked →
    perp cross → ranked IMMINENT/EARLY/PARK candidates. Self-budgets (SPEC 29) and is fully
    injectable (tokentx_fn/verify_fn/perp_fn/oi_baseline_fn) for deterministic tests.

    SPEC-174 #1: `ticker` scopes the result to ONE off-board name — an off-board Cat-A token
    otherwise has no on-chain radar leg (this radar is wallet-centric; there's no way to know
    WHICH cluster wallet, if any, holds a given token without running the full cluster
    enumeration, so `ticker` filters the candidate list AFTER the sweep rather than narrowing
    the wallet scan). Same output shape either way; a ticker matching nothing sets
    `not_found:true` (a real, explicit result — never confused with a dead sweep)."""
    tokentx_fn = tokentx_fn or _default_tokentx
    verify_fn = verify_fn or _default_verify
    perp_fn = perp_fn or live_perp
    oi_baseline_fn = oi_baseline_fn or (lambda t: None)

    cfg = _load_cfg()
    tracked = _tracked_contracts(cfg)
    cluster = operator_cluster_wallets(cfg)
    if wallet:
        cluster = [w for w in cluster if w["address"] == (wallet or "").lower()]
    elif wallets:
        keep = {a.lower() for a in wallets}
        cluster = [w for w in cluster if w["address"] in keep]
    cluster = cluster[:MAX_WALLETS_PER_RUN]

    t0 = time.time()
    candidates, scanned, partial = [], [], False
    for i, w in enumerate(cluster):
        if time.time() - t0 > budget_sec:
            partial = True
            break
        addr = w["address"]
        chains = [w.get("chain") or "binance-smart-chain"]
        try:
            holds = wallet_token_inbounds(addr, chains, ACCUM_DAYS, tokentx_fn)
        except Exception:  # noqa: BLE001
            holds = {}
        diff = diff_holdings(_load_baseline(addr), holds)
        _save_baseline(addr, {c: {"in_amount": h["in_amount"]} for c, h in holds.items()})
        for d in diff:
            contract = d["contract"]
            if not is_accumulation_candidate(contract, tracked):
                continue
            symbol = d.get("symbol") or contract[:10]
            v = verify_fn(addr, symbol, contract=contract, chain=w.get("chain")) or {}
            mode, reason = classify_accumulation(v)
            perp = perp_state(symbol, perp_fn=perp_fn, oi_baseline=oi_baseline_fn(symbol))
            tier = confluence_tier(mode, perp)
            bal = v.get("balance_now") or {}
            candidates.append({
                "wallet": addr, "wallet_label": w.get("label"), "is_escrow": w.get("is_escrow"),
                "token": symbol, "token_contract": contract, "chain": w.get("chain"),
                "size": d.get("in_amount"), "n_in": d.get("n_in"),
                "pct_supply": bal.get("pct_supply"),
                "mode": mode, "mode_reason": reason, "change": d.get("change"),
                "perp": perp, "tier": tier, "first_seen": d.get("first_in_ts")})
        scanned.append(addr)

    order = {"IMMINENT": 0, "EARLY": 1, "PARK": 2}
    candidates.sort(key=lambda c: (order.get(c["tier"], 3), -(c.get("size") or 0)))

    if ticker:
        t = ticker.upper()
        candidates = [c for c in candidates if (c.get("token") or "").upper() == t]

    out = {"scanned_wallets": scanned, "n_wallets": len(scanned), "partial": partial,
          "candidates": candidates,
          "imminent": [c for c in candidates if c["tier"] == "IMMINENT"],
          "early": [c for c in candidates if c["tier"] == "EARLY"],
          "n_candidates": len(candidates),
          # §0.6 caveats surfaced, not hidden:
          "caveats": ["accumulation ≠ imminent (operator timing is discretionary)",
                      "long-alongside-operator still risks being exit liquidity if the stage is misread (§0.6)",
                      "PARK / EARLY are NOT entries — only IMMINENT (active load + perp firing) is tradeable"]}
    if ticker:
        out["ticker"] = ticker
        if not candidates:
            out["not_found"] = True
    return out


def render_human(r):
    print(C.c(f"═══ ACCUMULATION RADAR — {r['n_wallets']} operator wallets · "
              f"{len(r['imminent'])} IMMINENT / {r['n_candidates']} candidates ═══", "bold", "cyan"))
    if not r["candidates"]:
        print(C.c("  ⚪ no new operator loads vs baseline", "grey"))
    for c in r["candidates"][:20]:
        col = "green" if c["tier"] == "IMMINENT" else "yellow" if c["tier"] == "EARLY" else "grey"
        f4 = c["perp"]["funding_4h"]
        fs = f"{f4:+.2f}%/4h" if f4 is not None else "no-perp"
        pct = f" {c['pct_supply']}% supply" if c.get("pct_supply") is not None else ""
        print("  " + C.c(f"{c['tier']:8}", "bold", col)
              + f" {c['wallet'][:12]}…{' [escrow]' if c.get('is_escrow') else ''} → {c['token']} "
              + f"({c['mode']}, {c['change']},{pct} · {fs})")
    for cv in r.get("caveats", []):
        print(C.c(f"  ⚠ {cv}", "grey"))


def main():
    ap = argparse.ArgumentParser(description="Operator accumulation radar (SPEC-76, long-side mirror)")
    ap.add_argument("wallet", nargs="?", default=None, help="one operator wallet (omit to sweep the cluster)")
    ap.add_argument("--ticker", default=None,
                    help="SPEC-174: scope the result to one off-board token symbol "
                         "(still sweeps the full cluster, filters the candidate list)")
    ap.add_argument("--budget", type=int, default=ACCUM_RADAR_BUDGET, help="SPEC 29 time budget (s)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)
    r = build_accumulation_radar(wallet=args.wallet, ticker=args.ticker, budget_sec=args.budget)
    if args.json:
        print(json.dumps(r))
    else:
        render_human(r)


if __name__ == "__main__":
    main()
