#!/usr/bin/env python3
"""claim_topup.py — SPEC-119: claim/distributor top-up tripwire.

`unlocks`/`stake_schedule` read the SCHEDULE (contract terms, cliff dates). This watches the
LIVE pre-signal the workshop's BARD case surfaced: the claims/distribution wallet was topped up
twice before claims opened; once claims began, recipients moved tokens to exchanges and price
fell ~50%. A top-up is the team physically staging the supply — it converts a calendar date
into an armed, on-chain-confirmed event, often days early, and also catches unscheduled/rolling
distributions the calendar never sees (CLAUDE.md §8: mega-safes are stock, the working-wallet
layer is where timing lives — memory/feedback_megasafe_is_stock_not_flow).

Registry-gated (feature is inert with no registrations): a wallet in
config/tracked_wallets.json tokens.<TICKER>.wallets tagged `"kind": "claim-distributor"`.
Reuses the existing token-flow provider seam (onchain.token_transfers — SPEC-97/101/102) and
the existing wallet-watch cadence (ops/surveil.sh, alongside unlocks.py --fire-alerts) — no new
polling loop.

  python3 capabilities/claim_topup.py BARD --json          # one-ticker read (no persist)
  python3 capabilities/claim_topup.py --fire-alerts --json # sweep hook (surveil.sh cadence)
"""
import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

WALLETS = ROOT / "config" / "tracked_wallets.json"
CONFIG_PATH = ROOT / "config" / "claim_topup.json"
STATE = ROOT / "state"
EVENT_RETENTION_DAYS = 30    # persisted-state event pruning window (independent of decay_window_h,
                              # which governs how long a top-up stays "recent" for annotation)

# Documented defaults — any key in config/claim_topup.json overrides these.
DEFAULT_CFG = {
    "min_usd": 200_000,          # inbound transfer >= this USD = a top-up (OR pct floor below)
    "min_pct_float": 2.0,        # ... OR >= this % of the token's circulating float
    "lookback_days": 14,         # token-flow history window checked each sweep
    "decay_window_h": 96,        # a top-up stays "recent" (annotated on unlocks/brief) this long
}


def load_cfg(path=None):
    cfg = dict(DEFAULT_CFG)
    try:
        d = json.loads(Path(path or CONFIG_PATH).read_text())
        cfg.update({k: v for k, v in d.items() if k in DEFAULT_CFG})
    except Exception:  # noqa: BLE001 — no config file = defaults
        pass
    return cfg


def _now_iso(now=None):
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(ts):
    if not ts:
        return None
    s = str(ts).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:  # noqa: BLE001
        return None


def _state_path(ticker, state_dir=None):
    return Path(state_dir or STATE) / f"claim_topup_{ticker.upper()}.json"


def _load_state(ticker, state_dir=None):
    try:
        return json.loads(_state_path(ticker, state_dir).read_text())
    except Exception:  # noqa: BLE001
        return {"seen": {}, "events": []}


def _atomic_write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _distributor_wallets(tok):
    return [w for w in (tok.get("wallets") or []) if (w.get("kind") or "").lower() == "claim-distributor"]


def _fmt_amt(n):
    if n is None:
        return "?"
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:.1f}{suf}"
    return f"{n:.0f}"


def _fmt_usd(v):
    return f"${_fmt_amt(v)}" if v is not None else "$?"


def _classify_source(addr, tracked_map, labels):
    from verify_wallet import SEED_TIERS
    a = (addr or "").lower()
    tw = tracked_map.get(a)
    if tw:
        tier = (tw.get("tier") or "").lower()
        label = tw.get("label")
        if tier in SEED_TIERS:
            return f"team safe ({label})" if label else "team safe"
        return f"tracked {tier} ({label})" if label else f"tracked-{tier}"
    ent = labels.get(a)
    if ent:
        return f"known entity ({ent})"
    return "unknown/staging EOA"


def _default_fetch(address, contract, chain_key, days, decimals):
    from onchain import token_transfers
    return token_transfers(address, contract, chain_key, days=days, decimals=decimals)


def _default_price(ticker):
    from verify_wallet import _live_price
    return _live_price(ticker)


def _default_catalyst(ticker, now):
    from unlocks import next_catalyst_for
    return next_catalyst_for(ticker, now=now, horizon=9999)


def evaluate_inbound(tx, distributor_addr, cfg, price, float_supply):
    """Pure: does one transfer clear the top-up floor? (to==distributor, size >= floor).
    Returns (passes, usd, pct_float, amount). Outbound (from==distributor, claims draining)
    or a transfer to any other address never passes — top-ups are strictly inbound."""
    to = (tx.get("to_address") or "").lower()
    if to != (distributor_addr or "").lower():
        return False, None, None, None
    amount = tx.get("value_decimal")
    if amount is None:
        amount = tx.get("value")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return False, None, None, None
    usd = amount * price if price else None
    pct_float = (amount / float_supply * 100) if float_supply else None
    passes = ((usd is not None and usd >= cfg["min_usd"])
              or (pct_float is not None and pct_float >= cfg["min_pct_float"]))
    return passes, usd, pct_float, amount


def build_topup_state(ticker, now=None, cfg=None, fetch_fn=None, price_fn=None, catalyst_fn=None,
                       persist=False, state_dir=None, wallets=None):
    """Scan registered claim-distributor wallets for NEW inbound top-ups (dedup vs persisted
    per-ticker state). Registry-gated: zero registered distributors => inert.

    Injectable seams (offline/deterministic tests):
      fetch_fn(addr, contract, chain, days, decimals) -> (txs, source, partial) — the
          onchain.token_transfers contract.
      price_fn(ticker) -> (price, source)
      catalyst_fn(ticker, now) -> catalyst dict | None  (unlocks.next_catalyst_for contract)
      wallets -> the parsed tracked_wallets.json doc ({"tokens": {...}}), default reads the file.

    A provider failure on one distributor is reported in `unavailable` (never a false quiet,
    CLAUDE.md §3) and never blocks the others. Returns:
      {ticker, checked, topups:[new events this run], unavailable:[{distributor,address,reason}]}
    """
    ticker = ticker.upper().replace("USDT", "")
    cfg = cfg or load_cfg()
    fetch_fn = fetch_fn or _default_fetch
    price_fn = price_fn or _default_price
    catalyst_fn = catalyst_fn or _default_catalyst
    now_ts = now if now is not None else time.time()

    if wallets is None:
        try:
            wallets = json.loads(WALLETS.read_text())
        except Exception:  # noqa: BLE001
            wallets = {"tokens": {}}
    tok = (wallets.get("tokens") or {}).get(ticker, {})
    distributors = _distributor_wallets(tok)
    if not distributors:
        return {"ticker": ticker, "checked": 0, "topups": [], "unavailable": []}

    state = _load_state(ticker, state_dir)
    seen = state.get("seen", {})
    events = list(state.get("events", []))
    contract_bsc = (tok.get("contracts", {}) or {}).get("binance-smart-chain")
    decimals = tok.get("decimals", 18)
    float_supply = tok.get("circulating_supply") or tok.get("supply")

    tracked_map = {w["address"].lower(): {"label": w.get("label"), "tier": (w.get("tier") or "").lower()}
                   for w in (tok.get("wallets") or [])}
    try:
        from onchain import _entity_labels
        labels = _entity_labels()
    except Exception:  # noqa: BLE001
        labels = {}

    try:
        price, _src = price_fn(ticker)
    except Exception:  # noqa: BLE001
        price = None

    try:
        catalyst = catalyst_fn(ticker, now_ts)
    except Exception:  # noqa: BLE001
        catalyst = None

    new_topups, unavailable = [], []
    seen_out = dict(seen)
    for w in distributors:
        addr = w["address"]
        chain = w.get("chain", "binance-smart-chain")
        contract = contract_bsc if chain == "binance-smart-chain" else (tok.get("contracts", {}) or {}).get(chain)
        already = set(seen.get(addr.lower(), []))
        try:
            txs, _source, _partial = fetch_fn(addr, contract, chain, cfg["lookback_days"], decimals)
        except Exception as e:  # noqa: BLE001 — provider down: report, never a false quiet
            unavailable.append({"distributor": w.get("label"), "address": addr, "reason": str(e)[:160]})
            continue
        new_seen = set(already)
        for tx in txs:
            to = (tx.get("to_address") or "").lower()
            if to != addr.lower():
                continue                                    # outbound (claims draining) — out of scope
            tx_id = tx.get("hash") or tx.get("transaction_hash") or f"{tx.get('block_timestamp')}:{tx.get('value')}"
            new_seen.add(tx_id)
            passes, usd, pct_float, amount = evaluate_inbound(tx, addr, cfg, price, float_supply)
            if tx_id in already or not passes:
                continue
            ev = {
                "ticker": ticker, "distributor": w.get("label"), "distributor_address": addr,
                "source_address": tx.get("from_address"),
                "source_kind": _classify_source(tx.get("from_address"), tracked_map, labels),
                "amount": amount, "usd": usd,
                "pct_float": round(pct_float, 2) if pct_float is not None else None,
                "tx": tx_id, "ts": tx.get("block_timestamp") or _now_iso(now_ts),
                "unscheduled": catalyst is None, "catalyst": catalyst,
            }
            new_topups.append(ev)
            events.append(ev)
        seen_out[addr.lower()] = sorted(new_seen)

    if persist:
        events = [e for e in events
                  if (_parse_ts(e.get("ts")) or now_ts) >= now_ts - EVENT_RETENTION_DAYS * 86400]
        _atomic_write_json(_state_path(ticker, state_dir), {"seen": seen_out, "events": events})

    return {"ticker": ticker, "checked": len(distributors), "topups": new_topups,
            "unavailable": unavailable}


def recent_annotation(ticker, now=None, cfg=None, state_dir=None):
    """Read persisted state; return top-up events still within the decay window (else [])."""
    cfg = cfg or load_cfg()
    now_ts = now if now is not None else time.time()
    state = _load_state(ticker.upper(), state_dir)
    out = []
    for ev in state.get("events", []):
        age_s = now_ts - (_parse_ts(ev.get("ts")) or now_ts)
        if age_s <= cfg["decay_window_h"] * 3600:
            out.append(ev)
    return out


def _format_msg(ev):
    addr_short = (ev.get("distributor_address") or "")[:10]
    src_short = (ev.get("source_address") or "")[:10]
    pct = f"{ev['pct_float']}%" if ev.get("pct_float") is not None else "?%"
    usd = _fmt_usd(ev.get("usd"))
    amt = _fmt_amt(ev.get("amount"))
    cat = ev.get("catalyst")
    tail = (f"unlock calendar says {cat.get('type', 'event')} {cat.get('date', '?')}" if cat
            else "UNSCHEDULED — no unlock-calendar entry, higher alarm")
    return (f"CLAIM TOP-UP: {ev.get('ticker')} distributor {ev.get('distributor') or addr_short} "
            f"funded +{amt} ({usd}, {pct} float) from {ev.get('source_kind')} {src_short} "
            f"— claims imminent, {tail}")


def fire_topup_alerts(now=None, tokens=None, append=None, cfg=None, state_dir=None, wallets=None,
                       fetch_fn=None, price_fn=None, catalyst_fn=None):
    """Sweep registered claim-distributors and fire one HIGH inbox event per NEW top-up.
    Mirrors unlocks.fire_unlock_alerts — same cadence hook (ops/surveil.sh), no new loop.
    `tokens` overrides the ticker set (default: every tracked ticker with a registered
    claim-distributor wallet). fetch_fn/price_fn/catalyst_fn pass straight through to
    build_topup_state (the offline test seam — defaults hit the real network/RPC)."""
    if append is None:
        import inbox
        append = inbox.append_event
    if wallets is None:
        try:
            wallets = json.loads(WALLETS.read_text())
        except Exception:  # noqa: BLE001
            wallets = {"tokens": {}}
    all_tokens = wallets.get("tokens") or {}
    names = tokens if tokens is not None else [tk for tk, t in all_tokens.items() if _distributor_wallets(t)]
    ts = _now_iso(now)
    fired = 0
    results = []
    for tk in names:
        r = build_topup_state(tk, now=now, cfg=cfg, persist=True, state_dir=state_dir, wallets=wallets,
                              fetch_fn=fetch_fn, price_fn=price_fn, catalyst_fn=catalyst_fn)
        results.append(r)
        for ev in r["topups"]:
            append(ts=ts, ticker=tk, source="claim_topup", severity="HIGH", msg=_format_msg(ev))
            fired += 1
    return {"fired": fired, "checked_tickers": len(names), "results": results}


def main():
    ap = argparse.ArgumentParser(description="claim/distributor top-up tripwire (SPEC-119)")
    ap.add_argument("ticker", nargs="?", default=None)
    ap.add_argument("--op", default="read", choices=["read", "fire-alerts"],
                    help="read (one ticker) | fire-alerts (sweep every registered ticker, "
                         "fire HIGH inbox events for new top-ups — the ops/surveil.sh hook)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.op == "fire-alerts":
        out = fire_topup_alerts()
    elif args.ticker:
        out = build_topup_state(args.ticker)
    else:
        print(json.dumps({"error": "ticker required (or use --op fire-alerts)"}))
        sys.exit(1)
    print(json.dumps(out, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
