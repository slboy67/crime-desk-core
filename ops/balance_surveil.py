#!/usr/bin/env python3
"""balance_surveil.py — SPEC-126: balance-delta surveillance for CONTRACT tracked wallets.

DEXE 2026-07-21: the committed tripwire was "page on first outbound from any
DEXE-STAGED-HOP2-* safe." The safes fired 15:43-16:00 UTC (625K DEXE -> a relay -> Binance,
price 29 -> 3.86) and NOTHING PAGED. Root cause: those tracked wallets are Gnosis Safe
PROXIES — a Safe executes via `execTransaction` from a signer EOA, so the safe's own account
nonce never changes (contract nonces only bump on CREATE). `nonce_surveil`
(capabilities/onchain.py build_nonce_state / capabilities/wallet_state.py build_snapshot,
both left untouched by this module) watches account nonces, so a contract wallet is
permanently invisible to it. "Surveil balances not nonces" as a prose note in a wallet's
`_note` field is not a tripwire — nothing reads it.

This is the balance mirror of ops/funding_surveil.py: pure decision functions over an
injectable clock + RPC reads, a persisted per-ticker baseline
(`state/balance_baseline_<TICKER>.json`), and inbox.append_event as the producer API onto
the board feed — the SAME channel every post-nonce surveillance monitor uses (funding_surveil,
tape_watch, board_tick).

Per-wallet mode (`surveil_mode`):
  - explicit `"surveil": "nonce" | "balance" | "both"` on the tracked-wallet entry always wins.
  - otherwise autodetect via `onchain.probe_contract` (getCode, cached on disk — SPEC-67's
    cache, no per-tick RPC after the first probe): a contract -> "balance", an EOA -> "nonce".
  - "nonce"-mode wallets are this module's no-op (nonce_surveil already covers them);
    "balance"/"both" wallets get a cross-checked `balanceOf` diffed against the baseline.

Balance diff (`assess_wallet`):
  - first-ever tick for a wallet SEEDS the baseline — never fires (no false alert on deploy).
  - a decrease > DUST vs the baseline = HIGH `balance_drop` (the DEXE hop-2-A 510,218 ->
    138,908 case).
  - a decrease TO ZERO that isn't cross-RPC-confirmed (single source, or the two sources
    disagree) is a data FAILURE, not a fire (§3 / [[feedback_cross_rpc_verify]]) — downgrades
    to a MED `data_quality` event and the baseline is NOT overwritten with the unconfirmed zero.
  - N consecutive unreadable balance ticks (FAIL_THRESHOLD) = MED `surveillance_blind` — a
    monitor going quiet must be distinguishable from genuinely "no movement" (§3).

CLI:
  python3 ops/balance_surveil.py tick DEXE --json
  python3 ops/balance_surveil.py tick --all --json      # every token with a wallet map

SPEC-164 (2026-08-26): a balance-mode wallet delta only pages once it clears a
MATERIALITY gate (BAL_MIN_PCT of the wallet's own baseline OR BAL_MIN_USD absolute) —
SLX -0.09%, TAG -0.05%, TAKE -0.19%, VELVET LP -421 tokens all fired HIGH "contract-wallet
OUTBOUND" and paged "contract safe DRAINED" (urgent) for what was exchange-proxy/top-holder
churn, not a drain. Sub-threshold deltas still land in the inbox (LOW, `[SUB-THRESHOLD]`)
so a slow drip is still on the record; a 24h cumulative drip guard fires ONE `OUTBOUND-DRIP`
event if the sum crosses the gate even though no single tick did. "DRAINED" is now reserved
for balance -> ~0 (`drained_to_zero`); a real balance decrease that clears the gate is
`balance_drop` labeled `OUTBOUND <pct>% (...)`; a decrease on a known-exchange wallet
(known_entities label, or a BINANCE-*/GATE-*/... tracked-wallet label prefix) is
`exchange_churn` and never pages regardless of size.
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))
sys.path.insert(0, str(ROOT / "ops"))

import onchain as O  # noqa: E402 — reuse the cross-RPC primitives (probe_contract/balance_of)
import page_grammar as PG  # noqa: E402 — SPEC-141: human-number rendering, no scientific notation

WALLETS = ROOT / "config" / "tracked_wallets.json"
STATE_DIR = ROOT / "state"
ENTITIES_PATH = ROOT / "config" / "known_entities.json"

DUST = 1e-6              # negligible-decrease filter (float/rounding noise, never a real move)
FAIL_THRESHOLD = 3       # consecutive unreadable balance ticks -> "surveillance blind"
VALID_MODES = {"nonce", "balance", "both"}
BALANCE_MODES = {"balance", "both"}

# ── SPEC-164: materiality gate + honest labels ────────────────────────────────────
BAL_MIN_PCT = 2.0        # % of the wallet's own baseline
BAL_MIN_USD = 25_000.0   # absolute $ — whichever of the two the delta clears
DRIP_WINDOW_HOURS = 24   # cumulative sub-threshold-delta window for the drip guard

# known_entities.json "label" values that are exchange infra, never an operator drain
EXCHANGE_ENTITY_LABELS = {
    "binance", "bitget", "bitfinex", "bitmart", "coinbase", "crypto-com",
    "gate-io", "kraken", "kucoin", "mexc", "okx",
}
# tracked_wallets.json's own `label` naming convention (e.g. "BINANCE-WALLET-PROXY")
EXCHANGE_LABEL_PREFIXES = (
    "BINANCE", "BITGET", "BITFINEX", "BITMART", "COINBASE", "GATE",
    "KRAKEN", "KUCOIN", "MEXC", "OKX", "CRYPTO-COM", "CEX",
)

# event kinds that surveil.sh is allowed to page — never sub_threshold/exchange_churn
PAGEABLE_KINDS = {"drained_to_zero", "balance_drop", "outbound_drip"}

_ENTITY_LABEL_CACHE = None


def _entity_labels():
    """{addr_lower: known_entities "label" category (binance/bitget/...)}. Cached — this
    is static repo config, not a per-tick RPC read."""
    global _ENTITY_LABEL_CACHE
    if _ENTITY_LABEL_CACHE is None:
        try:
            raw = json.loads(ENTITIES_PATH.read_text()).get("entities", {})
        except Exception:  # noqa: BLE001 — missing/corrupt config = no entity hints, never fatal
            raw = {}
        _ENTITY_LABEL_CACHE = {
            addr.lower(): info.get("label")
            for addr, info in (raw.items() if isinstance(raw, dict) else [])
            if isinstance(addr, str) and addr.startswith("0x") and isinstance(info, dict)
        }
    return _ENTITY_LABEL_CACHE


def is_exchange_wallet(wallet_cfg):
    """True if this wallet is known exchange infra (label from known_entities.json, or the
    tracked-wallet's own `label` uses the desk's BINANCE-*/GATE-*/... naming convention) —
    a decrease there is EXCHANGE-CHURN, never an operator drain, and never pages."""
    own_label = (wallet_cfg.get("label") or "").upper()
    if any(own_label.startswith(p) for p in EXCHANGE_LABEL_PREFIXES):
        return True
    ent_label = wallet_cfg.get("_entity_label")
    return ent_label in EXCHANGE_ENTITY_LABELS


def _is_material(pct, usd, min_pct, min_usd):
    """Materiality gate: clears if EITHER the %-of-baseline or the absolute-$ bound is hit
    — a whale wallet's 0.3% can be $250K (dollar bound catches it), a dust wallet's 40% can
    be $12 (percent bound catches it); either alone is real signal."""
    if pct is not None and abs(pct) >= min_pct:
        return True
    if usd is not None and abs(usd) >= min_usd:
        return True
    return False


def baseline_path(ticker, state_dir=None):
    return (Path(state_dir) if state_dir else STATE_DIR) / f"balance_baseline_{ticker.upper()}.json"


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001 — missing/corrupt baseline = start fresh, never fatal
        return default


def _write_json(path, obj):
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, str(p))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def surveil_mode(wallet_cfg, probe_fn):
    """Explicit `surveil` field on the tracked-wallet entry wins; else autodetect via
    getCode (probe_fn — cached bytecode probe, no per-tick RPC after the first call)."""
    explicit = (wallet_cfg.get("surveil") or "").lower()
    if explicit in VALID_MODES:
        return explicit
    probe = probe_fn(wallet_cfg["address"], wallet_cfg.get("chain", "binance-smart-chain"))
    return "balance" if probe.get("is_contract") else "nonce"


def _iso(now):
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _drip_check(wallet_cfg, prev_drip, prev_bal, value, delta, price_usd, now,
                 min_pct, min_usd, window_hours):
    """SPEC-164 req 3: sum sub-threshold deltas per wallet over a rolling window; once the
    CUMULATIVE delta (vs the window's own anchor balance) clears the materiality gate, fire
    ONE `outbound_drip` event — then keep the window's anchor but mark it fired so a slow
    drain that keeps ticking below the single-tick gate doesn't re-page every tick. A window
    older than `window_hours`, or one that hasn't been seeded yet, re-anchors on this tick's
    prior balance."""
    prev_drip = prev_drip or {}
    start_ts, start_balance, fired = (prev_drip.get("start_ts"), prev_drip.get("start_balance"),
                                       prev_drip.get("fired", False))
    expired = True
    if start_ts:
        try:
            expired = (now - _parse_iso(start_ts)) > timedelta(hours=window_hours)
        except Exception:  # noqa: BLE001 — a corrupt timestamp re-anchors, never crashes the tick
            expired = True
    if not start_ts or expired:
        start_ts, start_balance, fired = _iso(now), prev_bal, False

    cum_delta = start_balance - value
    cum_pct = (cum_delta / start_balance * 100.0) if start_balance else None
    cum_usd = (cum_delta * price_usd) if price_usd is not None else None

    if not fired and _is_material(cum_pct, cum_usd, min_pct, min_usd):
        return True, {"start_ts": start_ts, "start_balance": start_balance, "fired": True}
    return False, {"start_ts": start_ts, "start_balance": start_balance, "fired": fired}


def assess_wallet(wallet_cfg, prev, balance_fn, dust=DUST, fail_threshold=FAIL_THRESHOLD, now=None):
    """Pure per-wallet balance-delta diff.

    balance_fn(address, contract, chain, decimals) -> {available, value, cross_checked, agree}
    (onchain.balance_of's shape — cross-checked against up to 2 free RPCs).
    prev: prior baseline entry {"balance":.., "fail_count":.., "drip":..}, or None on the
    first-ever tick. `now` (injectable clock) only matters for the SPEC-164 drip window —
    defaults to wall-clock for CLI/prod use, always injected by tests.
    Returns (event | None, new_baseline_entry)."""
    prev = prev or {}
    now = now or datetime.now(timezone.utc)
    contract = wallet_cfg.get("_contract")
    chain = wallet_cfg.get("chain", "binance-smart-chain")
    decimals = wallet_cfg.get("_decimals", 18)
    read = balance_fn(wallet_cfg["address"], contract, chain, decimals)
    fail_count = prev.get("fail_count", 0)
    label = wallet_cfg.get("label") or wallet_cfg["address"]
    min_pct = wallet_cfg.get("bal_min_pct", BAL_MIN_PCT)
    min_usd = wallet_cfg.get("bal_min_usd", BAL_MIN_USD)
    price_usd = wallet_cfg.get("_price_usd")

    if not read.get("available"):
        fail_count += 1
        entry = {**prev, "fail_count": fail_count}
        if fail_count >= fail_threshold:
            return ({"severity": "MED", "kind": "surveillance_blind",
                     "msg": f"{label}: balance read failed {fail_count}x consecutive ticks — "
                            f"surveillance BLIND (silence ≠ no movement, §3)"},
                    entry)
        return None, entry

    value = read["value"]
    if prev.get("balance") is None:
        return None, {"balance": value, "fail_count": 0}          # seed — never fires

    prev_bal = prev["balance"]
    delta = prev_bal - value
    if delta <= dust:
        return None, {"balance": value, "fail_count": 0}          # unchanged or grew

    confirmed = bool(read.get("cross_checked") and read.get("agree"))
    if value == 0 and not confirmed:
        # an unconfirmed to-zero read is a data FAILURE, not a datum — do NOT overwrite the
        # trusted baseline with it (feedback_cross_rpc_verify: a real re-check later must still
        # diff against the last TRUSTED balance, not a fluke zero).
        return ({"severity": "MED", "kind": "data_quality",
                 "msg": f"{label}: balance read 0 (single-source or cross-RPC disagreement) — "
                        f"NOT confirmed, re-verify before treating as a drain"},
                {**prev, "fail_count": 0})

    pct = (delta / prev_bal * 100.0) if prev_bal else None
    usd = (delta * price_usd) if price_usd is not None else None
    pct_disp = f"{abs(pct):.2f}%" if pct is not None else "?%"
    usd_disp = f"${PG.fmt_compact(abs(usd))}" if usd is not None else "$?"

    if not _is_material(pct, usd, min_pct, min_usd):
        # SPEC-164 req 1: sub-materiality churn is a LOW inbox record, never HIGH, never
        # paged — but req 3's drip guard still watches the cumulative window so a slow
        # drain isn't silenced by ticking under the gate every single time.
        fires, new_drip = _drip_check(wallet_cfg, prev.get("drip"), prev_bal, value, delta,
                                       price_usd, now, min_pct, min_usd, DRIP_WINDOW_HOURS)
        entry = {"balance": value, "fail_count": 0, "drip": new_drip}
        if fires:
            drip_msg = (f"{label}: cumulative {PG.fmt_balance_change(new_drip['start_balance'], value)} "
                         f"over ≤{DRIP_WINDOW_HOURS}h — OUTBOUND-DRIP (sub-threshold ticks aggregated "
                         f"past the {BAL_MIN_PCT:.1f}%/${BAL_MIN_USD:,.0f} gate)")
            return {"severity": "HIGH", "kind": "outbound_drip", "msg": drip_msg}, entry
        sub_msg = (f"{label}: {PG.fmt_balance_change(prev_bal, value)} — [SUB-THRESHOLD] below "
                   f"materiality gate ({min_pct:.1f}%/${min_usd:,.0f}), no page")
        return {"severity": "LOW", "kind": "sub_threshold", "msg": sub_msg}, entry

    if value <= dust:
        # SPEC-164 req 2: DRAINED is reserved for balance -> ~0 — everything else material
        # is an honest "OUTBOUND", never the alarming (and usually wrong) "DRAINED".
        drained_msg = (f"{label}: {PG.fmt_balance_change(prev_bal, value)} — DRAINED "
                        f"(contract-wallet OUTBOUND, SPEC-126 §8)")
        return ({"severity": "HIGH", "kind": "drained_to_zero", "prev_balance": prev_bal,
                  "balance": value, "delta": round(delta, 8), "msg": drained_msg},
                {"balance": value, "fail_count": 0})

    if is_exchange_wallet(wallet_cfg):
        # req 2: exchange-proxy churn (top-holder rebalance, withdrawal sweep, etc.) is not
        # an operator drain — labeled honestly, never paged regardless of size.
        churn_msg = (f"{label}: {PG.fmt_balance_change(prev_bal, value)} — EXCHANGE-CHURN "
                      f"(known exchange wallet, not an operator drain)")
        return ({"severity": "MED", "kind": "exchange_churn", "prev_balance": prev_bal,
                  "balance": value, "delta": round(delta, 8), "msg": churn_msg},
                {"balance": value, "fail_count": 0})

    outbound_msg = (f"{label}: OUTBOUND {pct_disp} ({PG.fmt_compact(abs(delta))} · {usd_disp}) — "
                     f"{PG.fmt_balance_change(prev_bal, value)} (nonce-blind Gnosis safe, SPEC-126 §8)")
    return ({"severity": "HIGH", "kind": "balance_drop", "prev_balance": prev_bal,
             "balance": value, "delta": round(delta, 8), "msg": outbound_msg},
            {"balance": value, "fail_count": 0})


def _wallet_ctx(tok, wallet):
    """Attach the per-chain token contract + decimals a balance read needs, plus the
    SPEC-164 materiality context: the token's USD price (for the $-bound leg of the gate,
    when the desk has one on file) and the wallet's known_entities label (for the
    exchange-churn check — `label` here is the entity CATEGORY, e.g. "binance", never
    confused with the tracked-wallet's own display `label`)."""
    chain = wallet.get("chain", "binance-smart-chain")
    contract = (tok.get("contracts") or {}).get(chain)
    return {**wallet, "_contract": contract, "_decimals": tok.get("decimals", 18),
            "_price_usd": tok.get("price_usd"),
            "_entity_label": _entity_labels().get(wallet.get("address", "").lower())}


def run_tick(ticker, wallets_path=WALLETS, state_path=None, now=None,
             probe_fn=None, balance_fn=None, emit_fn=None):
    """One surveillance tick for one tracked token: contract-detect + balance-diff every
    BALANCE-mode wallet, persist the baseline, emit an inbox event per fire.

    probe_fn defaults to onchain.probe_contract (disk-cached bytecode probe — no per-tick RPC
    after the first). balance_fn defaults to onchain.balance_of (cross-checked, up to 2 free
    RPC sources). emit_fn(ts, ticker, source, severity, msg) defaults to inbox.append_event —
    the same producer API funding_surveil/tape_watch/board_tick use."""
    ticker = ticker.upper()
    now = now or datetime.now(timezone.utc)
    now_iso = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    probe_fn = probe_fn or O.probe_contract
    balance_fn = balance_fn or O.balance_of
    state_file = Path(state_path) if state_path else baseline_path(ticker)

    cfg = json.loads(Path(wallets_path).read_text())
    tok = cfg.get("tokens", {}).get(ticker, {})
    wallets = tok.get("wallets", [])

    baseline = _read_json(state_file, {})
    new_baseline = dict(baseline)
    events = []
    checked = []
    for w in wallets:
        mode = surveil_mode(w, probe_fn)
        if mode not in BALANCE_MODES:
            continue
        wctx = _wallet_ctx(tok, w)
        addr = w["address"].lower()
        ev, entry = assess_wallet(wctx, baseline.get(addr), balance_fn, now=now)
        new_baseline[addr] = entry
        checked.append(addr)
        if ev:
            events.append({**ev, "address": w["address"], "label": w.get("label")})

    if emit_fn is None:
        import inbox
        emit_fn = inbox.append_event

    for ev in events:
        try:
            emit_fn(now_iso, ticker, "balance_surveil", ev["severity"], ev["msg"])
        except Exception:  # noqa: BLE001 — a dead producer must not abort the tick
            pass

    _write_json(state_file, new_baseline)
    # SPEC-164: "fired" (what surveil.sh is allowed to page) excludes sub_threshold and
    # exchange_churn — materiality-gated and honestly-labeled, never a page.
    return {"ticker": ticker, "checked": len(checked), "events": events,
            "fired": [e for e in events if e["kind"] in PAGEABLE_KINDS]}


def run_all(wallets_path=WALLETS, tickers=None, **kwargs):
    """Run one tick per tracked token (or the given subset). Returns {ticker: run_tick result}."""
    cfg = json.loads(Path(wallets_path).read_text())
    names = [t.upper() for t in tickers] if tickers else sorted(cfg.get("tokens", {}).keys())
    return {t: run_tick(t, wallets_path=wallets_path, **kwargs) for t in names
            if t in cfg.get("tokens", {})}


def main():
    ap = argparse.ArgumentParser(description="SPEC-126 contract-wallet balance-delta surveillance")
    sub = ap.add_subparsers(dest="cmd")
    t = sub.add_parser("tick", help="run one surveillance tick")
    t.add_argument("ticker", nargs="?", default=None)
    t.add_argument("--all", action="store_true", help="run every tracked token")
    t.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.cmd != "tick":
        ap.print_help(sys.stderr)
        return 2
    if args.all:
        out = run_all()
    elif args.ticker:
        out = run_tick(args.ticker)
    else:
        ap.error("tick needs a ticker or --all")
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
