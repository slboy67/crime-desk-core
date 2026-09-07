#!/usr/bin/env python3
"""onboard.py — config-driven token onboarding (SPEC 47).

Five of 43 SPECs were "onboard token X": full TDD dispatch + review cycles for config
work, each adding days of latency on names where CLAUDE.md §8 makes on-chain mandatory.
This composes the resolver pieces that already exist:

  coingecko id   pull5.coingecko_layer (SPEC 36 collision-safe path)
  contracts      the resolved platform map (chain-filterable)
  top holders    onchain._goplus_concentration (SPEC 12/34) as CANDIDATE wallets
  baseline       onchain.build_nonce_state seeds state/nonce_baseline_<T>.json

HARD GUARD — no auto-tiering. SPEC 20 proved tier classification (bridge vs distribution
vs seed) is the judgment call that decides false-fire behavior. Candidates are written
tier:"unclassified" — not in verify_wallet.SEED_TIERS, so they are excluded from radar
seed-discovery until the Designer classifies them. The output's `needs_judgment` list
({address, balance_pct, hints}) lets the Designer tier them in one read.

Idempotent: re-running on a tracked token reports current state (report-only — classified
tiers are never clobbered).

  python3 capabilities/onboard.py TT --json
  python3 capabilities/onboard.py TT --chain bsc --json
"""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parent
WALLETS = REPO / "config" / "tracked_wallets.json"
WL_PATH = REPO / "config" / "watchlist.json"

# user-facing chain aliases → coingecko platform keys (the config's chain vocabulary)
CHAIN_ALIASES = {
    "bsc": "binance-smart-chain", "bnb": "binance-smart-chain",
    "binance-smart-chain": "binance-smart-chain",
    "eth": "ethereum", "ethereum": "ethereum",
    "base": "base", "polygon": "polygon-pos", "polygon-pos": "polygon-pos",
    "arbitrum": "arbitrum-one", "arbitrum-one": "arbitrum-one",
    "optimism": "optimistic-ethereum", "optimistic-ethereum": "optimistic-ethereum",
    "avax": "avalanche", "avalanche": "avalanche",   # SPEC 56
}


# ── network seams (module-level so tests monkeypatch them) ─────────────────────
def _cg_resolve(ticker, cg_id=None):
    """SPEC 36 collision-safe ticker→coingecko resolution (pull5). SPEC 52: an explicit
    cg_id bypasses symbol search entirely (Designer resolved the collision by judgment)."""
    from pull5 import coingecko_layer
    return coingecko_layer(ticker, cg_id=cg_id) or {"_error": "coingecko resolution failed"}


# SPEC-175: obviously-non-crypto name patterns (tokenized ETFs/stocks/indices passing
# as a crypto ticker — the KORU case, a tokenized Direxion 3x ETF). Heuristic on the
# CoinGecko candidate's `name` — /search doesn't expose a real symbolType, this is the
# best signal available without an extra per-candidate round-trip.
_NON_CRYPTO_NAME_RE = re.compile(
    r"\b(ETF|ETN|DIREXION|ISHARES|PROSHARES|VANGUARD|SPDR|INVESCO|WISDOMTREE|VANECK|"
    r"S&P ?500|NASDAQ|NYSE\b|DOW JONES|RUSSELL ?2000|"
    r"\d+X (?:BULL|BEAR|LONG|SHORT)|(?:BULL|BEAR) ?\d+X|"
    r"\bINC\.?\b|\bCORP\.?\b|\bCORPORATION\b|\bPLC\b)",
    re.IGNORECASE,
)


def _looks_non_crypto(name):
    return bool(_NON_CRYPTO_NAME_RE.search(name or ""))


def _cg_search(ticker):
    """SPEC 52: raw /search candidates for the ambiguity payload — exact-symbol matches
    first (the collision set the Designer picks from). SPEC-175: capped at 5, each
    candidate annotated `not_crypto` (ETF/stock naming, matching the SPEC-173 gate's
    own field name) so a KORU-style tokenized-ETF match self-disqualifies before it
    reaches a thesis, instead of the Designer having to spot it by eye."""
    from pull5 import fetch
    s = fetch(f"https://api.coingecko.com/api/v3/search?query={ticker}")
    coins = s.get("coins") if isinstance(s, dict) else None
    out = [{"id": c.get("id"), "symbol": (c.get("symbol") or "").upper(),
            "name": c.get("name"), "market_cap_rank": c.get("market_cap_rank"),
            "not_crypto": _looks_non_crypto(c.get("name"))}
           for c in (coins or []) if c.get("id")]
    exact = [c for c in out if c["symbol"] == ticker.upper()]
    return (exact or out)[:5]


def _decimals(cg_id, platform):
    """detail_platforms decimal_place for the platform; 18 when unknown (EVM default)."""
    try:
        from pull5 import fetch, _cg_detail_url
        d = fetch(_cg_detail_url(cg_id))
        dp = ((d or {}).get("detail_platforms") or {}).get(platform) or {}
        return int(dp.get("decimal_place") or 18)
    except Exception:  # noqa: BLE001
        return 18


def _holders(contract, goplus_chain):
    """GoPlus top-holder read (SPEC 12/34 implementation in onchain.py)."""
    from onchain import _goplus_concentration
    return _goplus_concentration(contract, goplus_chain)


def _erc20_meta(contract, chain_key):
    """SPEC-165: RPC-only identity (name/decimals/totalSupply) — the explicit-contract
    onboard path, no CoinGecko dependency."""
    from onchain import erc20_meta
    return erc20_meta(contract, chain_key)


def _chain_stats(contract, goplus_chain):
    """SPEC 56: per-chain supply/holder stats for the primary-chain ranking (GoPlus)."""
    from onchain import goplus_chain_stats
    return goplus_chain_stats(contract, goplus_chain)


def _seed_baseline(ticker):
    """First nonce sweep — persists state/nonce_baseline_<T>.json off the fresh config."""
    from onchain import build_nonce_state
    r = build_nonce_state(ticker)
    return {"baseline_seeded": True, "tracked": r.get("tracked", True)}


# ── helpers ────────────────────────────────────────────────────────────────────
def _load_cfg():
    return json.loads(WALLETS.read_text())


def _write_cfg(cfg):
    fd, tmp = tempfile.mkstemp(dir=str(WALLETS.parent), prefix=".tracked_wallets.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=1)
        os.replace(tmp, WALLETS)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _goplus_chain_for(platform):
    """coingecko platform key → GoPlus chain key (None when GoPlus can't read it)."""
    from onchain import _CG_PLATFORM_TO_GOPLUS
    return _CG_PLATFORM_TO_GOPLUS.get(platform)


# SPEC 56: a chain is a bridge stub when it carries almost no supply or holders —
# it must never become the primary read (the FOLKS BSC stub blinded the desk)
BRIDGE_STUB_PCT = 1.0
BRIDGE_STUB_HOLDERS = 25


def _rank_chains(contracts, cg_total):
    """Rank deployment chains by where the supply actually lives.

    Returns (chains_ranked, primary_chain, unreadable_supply_pct): supported chains get
    a GoPlus supply/holder read and a supply_pct vs the coingecko total; non-EVM/
    unsupported chains are explicit `supported:false` entries; the unreadable remainder
    is said out loud — the Designer must SEE how much supply is invisible."""
    supported, unsupported = [], []
    for plat, addr in contracts.items():
        gp = _goplus_chain_for(plat)
        if not gp:
            unsupported.append({"chain": plat, "supported": False, "supply_pct": None})
            continue
        try:
            st = _chain_stats(addr, gp) or {}
        except Exception:  # noqa: BLE001 — a dark chain ranks by absence, not by crash
            st = {}
        supported.append({"chain": plat, "goplus_chain": gp, "supported": True,
                          "_supply": st.get("total_supply"),
                          "holder_count": st.get("holder_count")})
    known = sum(c["_supply"] or 0 for c in supported)
    denom = max(float(cg_total or 0), known) or None
    for c in supported:
        c["supply_pct"] = (round((c.pop("_supply") or 0) / denom * 100, 2) if denom
                           else (c.pop("_supply") and None))
        c["bridge_stub"] = bool(
            (c["supply_pct"] is not None and c["supply_pct"] < BRIDGE_STUB_PCT)
            or ((c.get("holder_count") or 0) < BRIDGE_STUB_HOLDERS))
    unreadable = (round(max(0.0, 100.0 - sum(c["supply_pct"] or 0 for c in supported)), 2)
                  if denom else None)
    ranked = sorted(supported, key=lambda c: (c["bridge_stub"], -(c["supply_pct"] or 0)))
    primary = ranked[0]["chain"] if ranked else None
    return ranked + unsupported, primary, unreadable


def refresh_chain_ranking(ticker):
    """SPEC-192: re-rank an ALREADY-tracked token's chains against the LIVE GoPlus map.
    Onboarding is a one-time snapshot — `_rank_chains` runs once inside `build_onboard`
    and nothing re-runs it later — so a GoPlus chain-map extension (SPEC-192 #1 added
    solana/sui/monad/…) leaves every already-tracked token's `chains_ranked`/
    `primary_chain` stale (still `supported:false` on a chain that answers today) until
    this runs. Never touches wallets/decimals/name — chain-ranking fields only, and
    persists via the same atomic `_write_cfg` every onboard write uses."""
    cfg = _load_cfg()
    tok = (cfg.get("tokens") or {}).get(ticker.upper())
    if not tok:
        return {"ok": False, "ticker": ticker.upper(), "reason": "not_tracked"}
    contracts = tok.get("contracts") or {}
    if not contracts:
        return {"ok": False, "ticker": ticker.upper(), "reason": "no_contracts"}
    cg_total = None
    if tok.get("cg_id"):
        try:
            cg = _cg_resolve(ticker, tok["cg_id"])
            cg_total = cg.get("total_supply") if isinstance(cg, dict) else None
        except Exception:  # noqa: BLE001 — a dead coingecko read just means no supply_pct
            cg_total = None
    if len(contracts) > 1:
        chains_ranked, primary_chain, unreadable_pct = _rank_chains(contracts, cg_total)
    else:
        only = next(iter(contracts))
        gp = _goplus_chain_for(only)
        try:
            st = _chain_stats(contracts[only], gp) if gp else {}
        except Exception:  # noqa: BLE001
            st = {}
        chains_ranked = [{"chain": only, "goplus_chain": gp, "supported": bool(gp),
                          "supply_pct": None, "holder_count": (st or {}).get("holder_count"),
                          "bridge_stub": False}]
        primary_chain = only
        unreadable_pct = None
    changed = (chains_ranked != tok.get("chains_ranked")) or (primary_chain != tok.get("primary_chain"))
    tok["chains_ranked"] = chains_ranked
    tok["primary_chain"] = primary_chain
    tok["unreadable_supply_pct"] = unreadable_pct
    cfg["tokens"][ticker.upper()] = tok
    _write_cfg(cfg)
    return {"ok": True, "ticker": ticker.upper(), "changed": changed,
           "primary_chain": primary_chain, "chains_ranked": chains_ranked}


def _report_tracked(ticker, tok):
    tiers = {}
    for w in tok.get("wallets", []):
        t = (w.get("tier") or "unclassified").lower()
        tiers[t] = tiers.get(t, 0) + 1
    unclassified = [w for w in tok.get("wallets", []) if (w.get("tier") or "").lower() == "unclassified"]
    baseline = (REPO / "state" / f"nonce_baseline_{ticker}.json").exists()
    return {"ok": True, "ticker": ticker, "already_tracked": True,
            "contracts": tok.get("contracts", {}), "wallets": len(tok.get("wallets", [])),
            "tiers": tiers, "baseline_seeded": baseline,
            "needs_judgment": [{"address": w["address"], "balance_pct": w.get("_balance_pct"),
                                "hints": w.get("_hints", {})} for w in unclassified],
            "note": "report-only — classified tiers are never clobbered; re-tier by editing config"}


# ── SPEC-165: onboard by explicit contract — no CoinGecko dependency ───────────
# Fresh BSC->Alpha names (the desk's core Cat-A class) have no CoinGecko entry on
# day one (DEBIT, KORU/resolve_ambiguous) — CoinGecko resolution isn't just slow
# there, it's UNAVAILABLE. When the Designer already knows the contract, skip
# resolution entirely: RPC identity + GoPlus holders is the whole onboard.
def _build_onboard_by_contract(ticker, contract, chain, cfg):
    if not chain:
        return {"ok": False, "ticker": ticker, "reason": "contract_requires_chain",
                "detail": "explicit contract onboarding needs a chain (bsc|eth|base|…)"}
    if (not isinstance(contract, str) or not contract.lower().startswith("0x")
            or len(contract) != 42):
        return {"ok": False, "ticker": ticker, "reason": "bad_contract",
                "detail": f"not a 0x + 40-hex address: {contract!r}"}
    want = CHAIN_ALIASES.get(chain.lower(), chain.lower())
    gp = _goplus_chain_for(want)
    # SPEC-192 #1: this path is 0x-address-only (already gated above) — solana/sui's
    # name-segment GoPlus endpoints mean the chain isn't an EVM address space at all,
    # so it's unsupported HERE regardless of GoPlus coverage (those chains onboard via
    # their own config contracts, not a manual 0x contract).
    from onchain import _GOPLUS_NAME_CHAINS
    if not gp or gp in _GOPLUS_NAME_CHAINS:
        return {"ok": False, "ticker": ticker, "reason": f"unsupported_chain:{want}",
                "detail": "no GoPlus/RPC support for this chain"}
    contract = contract.lower()

    try:
        meta = _erc20_meta(contract, want) or {}
    except Exception as ex:  # noqa: BLE001 — a dead RPC pool degrades, never crashes
        meta = {"available": False, "reason": f"rpc_meta_failed:{str(ex)[:100]}"}

    try:
        holders = _holders(contract, gp) or {"available": False, "reason": "no result"}
    except Exception as ex:  # noqa: BLE001
        holders = {"available": False, "reason": f"goplus_failed:{str(ex)[:100]}"}

    wallets, needs_judgment = [], []
    if holders.get("available"):
        rank = 0
        for h in holders.get("holders", []):
            if h.get("is_burn") or not h.get("address"):
                continue
            rank += 1
            hints = {"contract": bool(h.get("is_contract")), "locked": bool(h.get("is_locked")),
                     "cex_label": h.get("tag")}
            wallets.append({
                "label": f"GOPLUS-TOP{rank}", "address": h["address"], "chain": want,
                "tier": "unclassified",      # HARD GUARD: tiering is the Designer's judgment call
                "_balance_pct": h.get("percent"), "_hints": hints,
                "_note": "onboard candidate — classify (team/distribution/op/cex/…) before it can seed radar discovery",
            })
            needs_judgment.append({"address": h["address"], "balance_pct": h.get("percent"),
                                   "hints": hints})

    entry = {
        "name": meta.get("name") or ticker,
        "decimals": meta.get("decimals") if meta.get("decimals") is not None else 18,
        "cg_id": None,
        "onboarded_by": "contract",
        "contracts": {want: contract},
        "primary_chain": want,
        "chains_ranked": [{"chain": want, "goplus_chain": gp, "supported": True,
                          "supply_pct": None, "holder_count": holders.get("holder_count"),
                          "bridge_stub": False}],
        "wallets": wallets,
    }
    cfg.setdefault("tokens", {})[ticker] = entry
    _write_cfg(cfg)

    seed = {}
    try:
        seed = _seed_baseline(ticker) or {}
    except Exception as ex:  # noqa: BLE001 — config landed; a failed sweep is re-runnable
        seed = {"baseline_seeded": False, "reason": f"baseline_failed:{str(ex)[:100]}"}

    out = {"ok": True, "ticker": ticker, "already_tracked": False,
           "onboarded_by": "contract",
           "cg_id": None, "low_confidence": False,
           "contracts": {want: contract},
           "primary_chain": want,
           "token_meta": {"name": meta.get("name"), "decimals": entry["decimals"],
                          "total_supply": meta.get("total_supply"),
                          "meta_available": bool(meta.get("available"))},
           "candidates_written": len(wallets),
           "holders_source": (f"goplus_failed:{holders.get('reason')}" if not holders.get("available")
                              else f"goplus:{want}"),
           "needs_judgment": needs_judgment,
           "baseline_seeded": bool(seed.get("baseline_seeded"))}
    if not out["baseline_seeded"]:
        out["baseline_reason"] = seed.get("reason", "baseline_failed:unknown")
    return out


# ── core ───────────────────────────────────────────────────────────────────────
def build_onboard(ticker, chain=None, coingecko_id=None, contract=None):
    ticker = ticker.upper()
    cfg = _load_cfg()
    tok = cfg.get("tokens", {}).get(ticker)
    if tok:
        return _report_tracked(ticker, tok)

    if contract:
        return _build_onboard_by_contract(ticker, contract, chain, cfg)

    # SPEC 52: stage-specific failure surface — the Designer must be able to name what
    # failed (and retry with an explicit coingecko_id) without reading source.
    cg = _cg_resolve(ticker, coingecko_id)
    if not isinstance(cg, dict):
        cg = {"_error": "coingecko resolution failed"}
    if coingecko_id:
        # explicit id = the Designer's judgment; any failure here is the contract fetch
        if cg.get("_error") or not cg.get("contracts"):
            return {"ok": False, "ticker": ticker,
                    "reason": f"contract_fetch_failed:{coingecko_id} "
                              f"({cg.get('_error') or 'no contract platforms on the id'})"}
    elif cg.get("_error") or not cg.get("contracts") or cg.get("low_confidence"):
        # unaided resolution failed or is unsure — surface the collision set, write nothing
        try:
            candidates = _cg_search(ticker)
        except Exception:  # noqa: BLE001
            candidates = []
        if candidates:
            print(f"WARN onboard resolve_ambiguous {ticker}: {len(candidates)} candidates "
                  f"({', '.join(c.get('id', '?') for c in candidates)})", file=sys.stderr)
            return {"ok": False, "ticker": ticker, "reason": "resolve_ambiguous",
                    "candidates": candidates,
                    "hint": f"retry with onboard '{{\"ticker\":\"{ticker}\",\"coingecko_id\":\"<id>\"}}'"}
        return {"ok": False, "ticker": ticker, "reason": "resolve_not_found",
                "detail": cg.get("_error") or "no contract platforms resolved"}

    contracts = dict(cg["contracts"])
    if chain:
        want = CHAIN_ALIASES.get(chain.lower(), chain.lower())
        contracts = {k: v for k, v in contracts.items() if k == want}
        if not contracts:
            return {"ok": False, "ticker": ticker,
                    "reason": f"chain {chain} ({want}) not among resolved platforms {list(cg['contracts'])}"}

    # SPEC 56: primary-chain resolution — rank multi-chain deployments by where the
    # supply lives; single-chain tokens skip the extra reads (primary is trivial)
    supported_plats = [p for p in contracts if _goplus_chain_for(p)]
    if len(contracts) > 1:
        chains_ranked, primary_chain, unreadable_pct = _rank_chains(contracts, cg.get("total_supply"))
    else:
        only = next(iter(contracts), None)
        primary_chain = only
        chains_ranked = [{"chain": only, "goplus_chain": _goplus_chain_for(only),
                          "supported": bool(_goplus_chain_for(only)),
                          "supply_pct": None, "holder_count": None, "bridge_stub": False}]
        unreadable_pct = None
    if primary_chain is None and supported_plats:
        primary_chain = supported_plats[0]

    # candidate wallets from GoPlus top holders — on the PRIMARY chain (SPEC 56)
    holders, holder_chain = {"available": False, "reason": "no GoPlus-supported chain"}, None
    plat_order = ([primary_chain] if primary_chain in contracts else []) + \
                 [p for p in contracts if p != primary_chain]
    for plat in plat_order:
        gp = _goplus_chain_for(plat)
        if gp:
            holders = _holders(contracts[plat], gp)
            holder_chain = plat
            break

    wallets, needs_judgment = [], []
    if holders.get("available"):
        rank = 0
        for h in holders.get("holders", []):
            if h.get("is_burn") or not h.get("address"):
                continue   # burned supply is removed supply, not a holder (SPEC 37)
            rank += 1
            hints = {"contract": bool(h.get("is_contract")), "locked": bool(h.get("is_locked")),
                     "cex_label": h.get("tag")}
            wallets.append({
                "label": f"GOPLUS-TOP{rank}", "address": h["address"], "chain": holder_chain,
                "tier": "unclassified",      # HARD GUARD: tiering is the Designer's judgment call
                "_balance_pct": h.get("percent"), "_hints": hints,
                "_note": "onboard candidate — classify (team/distribution/op/cex/…) before it can seed radar discovery",
            })
            needs_judgment.append({"address": h["address"], "balance_pct": h.get("percent"),
                                   "hints": hints})

    plat0 = primary_chain if primary_chain in contracts else next(iter(contracts))
    entry = {
        "name": cg.get("name") or ticker,
        "decimals": _decimals(cg.get("cg_id"), plat0),
        "cg_id": cg.get("cg_id"),
        "contracts": contracts,
        "primary_chain": primary_chain,          # SPEC 56
        "chains_ranked": chains_ranked,
        "wallets": wallets,
    }
    if unreadable_pct is not None:
        entry["unreadable_supply_pct"] = unreadable_pct
    cfg.setdefault("tokens", {})[ticker] = entry
    _write_cfg(cfg)

    seed = {}
    try:
        seed = _seed_baseline(ticker) or {}
    except Exception as ex:  # noqa: BLE001 — config landed; a failed sweep is re-runnable
        seed = {"baseline_seeded": False, "reason": f"baseline_failed:{str(ex)[:100]}"}

    out = {"ok": True, "ticker": ticker, "already_tracked": False,
           "cg_id": cg.get("cg_id"), "low_confidence": bool(cg.get("low_confidence")),
           "contracts": contracts,
           "primary_chain": primary_chain,       # SPEC 56
           "chains_ranked": chains_ranked,
           "unreadable_supply_pct": unreadable_pct,
           "candidates_written": len(wallets),
           "holders_source": (f"goplus_failed:{holders.get('reason')}" if not holders.get("available")
                              else f"goplus:{holder_chain}"),
           "needs_judgment": needs_judgment,
           "baseline_seeded": bool(seed.get("baseline_seeded"))}
    if not out["baseline_seeded"]:
        out["baseline_reason"] = seed.get("reason", "baseline_failed:unknown")
    return out


# ── SPEC 51: sweep mode — watchlist mapping is an INVARIANT ─────────────────────
def unmapped_tickers():
    """Watchlist names missing from the on-chain map — pure set-difference, NO network.
    The cheap pre-check board_tick runs every tick (zero cost when fully mapped)."""
    wl = json.loads(WL_PATH.read_text())
    toks = wl.get("tokens", wl) if isinstance(wl, dict) else wl
    watch = [(t.get("ticker") or "").upper() for t in toks if t.get("ticker")]
    tracked = set(json.loads(WALLETS.read_text()).get("tokens", {}))
    return [t for t in watch if t not in tracked]


def build_sweep():
    """Onboard every unmapped watchlist name. A per-token failure is reported in
    `results`/`failed`, never aborts the sweep. Idempotent: fully-mapped → swept:0."""
    results, mapped, failed = [], [], []
    for tk in unmapped_tickers():
        try:
            r = build_onboard(tk)
        except Exception as ex:  # noqa: BLE001 — one bad token must not kill the sweep
            r = {"ok": False, "reason": f"exception:{str(ex)[:80]}"}
        if r.get("ok"):
            status = "already" if r.get("already_tracked") else "mapped"
            if status == "mapped":
                mapped.append(tk)
        else:
            status = "failed"
            failed.append(tk)
        results.append({"ticker": tk, "status": status, "reason": r.get("reason"),
                        "candidates": r.get("candidates"),
                        "needs_judgment": r.get("needs_judgment", [])})
    return {"ok": True, "swept": len(results), "mapped": mapped, "failed": failed,
            "results": results}


def main():
    ap = argparse.ArgumentParser(description="onboard — config-driven token onboarding (SPEC 47/51/52)")
    ap.add_argument("ticker", nargs="?", default=None)
    ap.add_argument("--all", default=None, dest="sweep_all",
                    help="sweep mode: onboard every unmapped watchlist name (SPEC 51)")
    ap.add_argument("--chain", default=None, help="restrict to one chain (bsc/eth/base/…)")
    ap.add_argument("--coingecko-id", default=None,
                    help="explicit coingecko id — bypasses symbol search (collision override)")
    ap.add_argument("--contract", default=None,
                    help="SPEC-165: explicit contract 0x.. — skips CoinGecko entirely "
                         "(requires --chain); fresh BSC->Alpha names have no CoinGecko entry")
    ap.add_argument("--refresh-chains", action="store_true",
                    help="SPEC-192: re-rank an already-tracked token's chains_ranked/"
                         "primary_chain against the LIVE GoPlus map (never re-onboards "
                         "wallets/decimals/name) — run after a chain-map extension")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.sweep_all:
        out = build_sweep()
    elif args.ticker and args.refresh_chains:
        out = refresh_chain_ranking(args.ticker)
    elif args.ticker:
        out = build_onboard(args.ticker, chain=args.chain, coingecko_id=args.coingecko_id,
                            contract=args.contract)
    else:
        out = {"ok": False, "error": "need a ticker or --all"}
    print(json.dumps(out, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
