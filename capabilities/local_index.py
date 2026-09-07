#!/usr/bin/env python3
"""local_index.py — SPEC-102: incremental local SQLite index of tracked-set ERC-20 Transfer
logs, sunset-proofing the recurring surveillance loop against vendor death/throttle (Sim:
announced and sunset inside a month; Moralis: throttles). The loop's actual needs are
NARROW — the wallets/contracts in config/tracked_wallets.json, not the chain — so a small
local index removes third-party risk from the core loop entirely.

Two independent halves:
  INGEST (write, network) — `ingest_contract()` / `--tick` CLI mode. Walks ERC-20 Transfer
    logs forward from the stored head via free-RPC eth_getLogs, chunked per
    RPC_PROVIDER_TABLE (per-provider max-getLogs-range + pacing — free BSC RPCs cap ranges
    wildly differently, live-verified below), into SQLite at state/local_index.db.
    Idempotent (INSERT OR IGNORE, PK=tx_hash+log_index) + resumable (head persists across
    runs) + reorg-tolerant (re-scans + replaces the trailing `reorg_tail_blocks` each run) +
    budget-capped (`rpc_call_budget` getLogs calls per run — a cold backfill resumes over
    several ticks instead of hammering public RPCs). Ops wires the schedule separately
    (`--tick`); this module never touches launchd.
  READ (pure SQLite, no network) — `query_transfers()` returns token_transfers-envelope-
    compatible rows (from_address/to_address/value_decimal/block_timestamp/token_symbol).
    `freshness()` gates whether the index is current enough to serve
    (head within `freshness_max_age_min` of the live chain head — approximated from block
    height, not a live RPC call, so a read never blocks on network). `query_or_stale()` is
    the provider-facing envelope: {available, source, partial, stale_index, txs} — a query
    for a contract outside the tracked set is bypassed entirely (available:False, no
    stale_index flag at all — it was never this index's job); an un-ingested range or a
    stale head is `available:False` (§3 gap honesty — NEVER an empty-clean result: an
    unread range must never look identical to "no transfers happened").

Live-verified BSC RPC facts (orchestrator + coder spot-check, 2026-07-02; RE-VERIFIED
2026-09-02, reports/RESEARCH-2026-09-02-onchain-chain-coverage.md — re-verify again
before trusting at scale, it rots by design):
  bsc-rpc.publicnode.com    WORKS keyless at up to 2,000 blocks/call (500 OK n=11, 2000
                             OK n=48; 10000 -> HTTP403 "Archive requests require a
                             personal token") — PRIMARY as of 2026-09-02.
  1rpc.io/bnb                50-block getLogs cap — chunked fallback.
  bsc-mainnet.public.blastapi.io   10-block getLogs cap (coder-confirmed 2026-07-02: "You
                             can make eth_getLogs requests with up to a 10 block range") —
                             chunked fallback; this is the CURRENT config RPC for BSC, so
                             effectively crippled for lookback without this table.
  EXCLUDED (not a logs source): bsc-dataseed.bnbchain.org/defibit ("limit exceeded" on
    every attempt — dataseed rejects >500 blocks with -32005 even at 500), bsc.publicnode.com
    getLogs (coder-confirmed 2026-07-02: "Archive requests require a personal token" —
    NOTE this is the DIFFERENT bsc-rpc.publicnode.com host above, which does work),
    binance.llamarpc.com (dead/non-JSON), rpc.ankr.com/bsc (key-required now),
    bsc.drpc.org (SPEC-192 #7: RETIRED 2026-09-02 — its public tier now 429s
    "You reached Public endpoint rate limit, please upgrade to paid plan" even on
    eth_chainId, confirmed on three separate calls; it was PRIMARY as of 2026-07-02 but
    is no longer usable keyless). A dead/gated entry just burns a chunk-budget slot for
    nothing — exclude, don't demote.
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

DB_PATH = ROOT / "state" / "local_index.db"
TRACKED_WALLETS_PATH = ROOT / "config" / "tracked_wallets.json"
CONFIG_PATH = ROOT / "config" / "local_index.json"

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Approximate chain block time — used ONLY to convert block height <-> wall-clock for the
# freshness gate and query-window filter (no per-block RPC lookup; same approximation
# pattern onchain.py's _getlogs_transfers already uses for its `_approx_ts` rows).
_BLOCK_TIME_S = {"binance-smart-chain": 3.0, "ethereum": 12.0}

RPC_PROVIDER_TABLE = {
    "binance-smart-chain": [
        # SPEC-192 #7: bsc.drpc.org's public tier now 429s on every call (RETIRED,
        # live-verified 2026-09-02) — replaced with bsc-rpc.publicnode.com, which
        # answers keyless at up to 2,000 blocks/call (the desk's new BSC ceiling).
        {"url": "https://bsc-rpc.publicnode.com", "max_range": 2000, "min_interval_s": 0.5},
        {"url": "https://1rpc.io/bnb", "max_range": 50, "min_interval_s": 0.6},
        {"url": "https://bsc-mainnet.public.blastapi.io", "max_range": 10, "min_interval_s": 0.5},
    ],
    "ethereum": [
        # Not BSC-scope live-verified this pass (req 7: BSC first-class); conservative
        # default reusing the existing free pool — re-verify before trusting at scale.
        {"url": "https://ethereum-rpc.publicnode.com", "max_range": 2000, "min_interval_s": 0.3},
    ],
}

DEFAULT_CFG = {
    "reorg_tail_blocks": 200,      # req 2: re-scan + replace this many trailing blocks each run
    "backfill_days": 30,           # req 5: initial backfill bound
    "freshness_max_age_min": 30,   # req 3: index head within this of chain head -> "fresh"
    "rpc_call_budget": 40,         # req 6: max getLogs calls per ingest() run
}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        d = json.loads(CONFIG_PATH.read_text())
        cfg.update({k: v for k, v in d.items() if k in DEFAULT_CFG})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


class ProviderSkip(Exception):
    """Not applicable (untracked contract, or the ingest half needs an rpc_call it wasn't
    given) — the caller should treat this exactly like onchain.ProviderSkip: skip silently."""


# ── SQLite ───────────────────────────────────────────────────────────────────
def _conn(db_path=None):
    p = Path(db_path) if db_path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute("""CREATE TABLE IF NOT EXISTS transfers (
        chain TEXT NOT NULL, contract TEXT NOT NULL, tx_hash TEXT NOT NULL,
        log_index INTEGER NOT NULL, from_address TEXT, to_address TEXT,
        value_raw TEXT, block_number INTEGER NOT NULL,
        PRIMARY KEY (tx_hash, log_index))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transfers_from ON transfers(chain, contract, from_address)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transfers_to ON transfers(chain, contract, to_address)")
    conn.execute("""CREATE TABLE IF NOT EXISTS index_head (
        chain TEXT NOT NULL, contract TEXT NOT NULL, head_block INTEGER NOT NULL,
        head_ts REAL, updated_ts REAL, PRIMARY KEY (chain, contract))""")
    conn.commit()
    return conn


def _tracked_contracts(chain_key):
    """The set of contract addresses config/tracked_wallets.json tracks on this chain —
    queries outside the tracked set bypass the index entirely (req 3)."""
    try:
        toks = json.loads(TRACKED_WALLETS_PATH.read_text()).get("tokens", {})
    except (OSError, json.JSONDecodeError):
        return set()
    out = set()
    for info in toks.values():
        c = (info.get("contracts") or {}).get(chain_key)
        if c:
            out.add(c.lower())
    return out


# ── ingest (write, network) ─────────────────────────────────────────────────
def _current_block(rpc_call, provider_table):
    for prov in provider_table:
        try:
            r = rpc_call(prov["url"], "eth_blockNumber", [])
            return int(r, 16)
        except Exception:  # noqa: BLE001
            continue
    return None


def _plan_chunks(lo, hi, max_range):
    chunks, b = [], lo
    while b <= hi:
        e = min(b + max_range - 1, hi)
        chunks.append((b, e))
        b = e + 1
    return chunks


def _fetch_logs_chunk(contract, lo, hi, provider_table, rpc_call, sleep_fn, calls_used):
    """Try each provider in table order for [lo,hi] (subdividing further per-provider when
    the chunk exceeds that provider's own max_range). Returns the raw log list, or None when
    every provider failed this range (a real gap — the caller must NOT record it as empty)."""
    for prov in provider_table:
        span = hi - lo + 1
        sub = [(lo, hi)] if span <= prov["max_range"] else _plan_chunks(lo, hi, prov["max_range"])
        rows, ok = [], True
        for (slo, shi) in sub:
            sleep_fn(prov["min_interval_s"])
            calls_used[0] += 1
            try:
                logs = rpc_call(prov["url"], "eth_getLogs", [{
                    "address": contract, "fromBlock": hex(slo), "toBlock": hex(shi),
                    "topics": [TRANSFER_TOPIC]}])
            except Exception:  # noqa: BLE001 — HTTP/timeout/408: try the next provider, never treat as empty
                ok = False
                break
            if not isinstance(logs, list):
                ok = False
                break
            rows.extend(logs)
        if ok:
            return rows
    return None


def _parse_transfer_log(lg):
    try:
        frm = ("0x" + lg["topics"][1][-40:]).lower()
        to = ("0x" + lg["topics"][2][-40:]).lower()
        value_raw = str(int(lg["data"], 16))
        block_number = int(lg["blockNumber"], 16)
        tx_hash = lg["transactionHash"]
        log_index = int(lg["logIndex"], 16)
    except (KeyError, ValueError, IndexError, TypeError):
        return None
    return {"from_address": frm, "to_address": to, "value_raw": value_raw,
            "block_number": block_number, "tx_hash": tx_hash, "log_index": log_index}


def ingest_contract(chain, contract, db_path=None, rpc_call=None, sleep_fn=None, now=None, cfg=None):
    """One tick for one (chain, contract): extend the index from the stored head (or a
    fresh backfill start) toward the live chain head, chunked + budget-capped + reorg-tail-
    rescanned. Returns {available, ingested_rows, new_head, chain_head, calls_used,
    budget_exhausted, gap}. `gap:True` means a chunk failed on every provider — the head
    stops BEFORE it (never silently skipped, never advanced past unread data)."""
    contract = contract.lower()
    provider_table = RPC_PROVIDER_TABLE.get(chain, [])
    if not provider_table:
        return {"available": False, "reason": f"no rpc providers configured for chain {chain}"}
    if rpc_call is None:
        raise ProviderSkip("ingest_contract needs an rpc_call function")
    cfg = cfg or load_cfg()
    sleep_fn = sleep_fn or (lambda s: time.sleep(s))
    now = now if now is not None else time.time()
    block_time = _BLOCK_TIME_S.get(chain, 3.0)

    chain_head = _current_block(rpc_call, provider_table)
    if chain_head is None:
        return {"available": False, "reason": "chain head unreadable (all providers failed)"}

    conn = _conn(db_path)
    try:
        head_row = conn.execute(
            "SELECT head_block, head_ts FROM index_head WHERE chain=? AND contract=?",
            (chain, contract)).fetchone()
        if head_row is None:
            backfill_blocks = int(cfg["backfill_days"] * 86400 / block_time)
            lo = max(chain_head - backfill_blocks, 0)
        else:
            lo = max(head_row[0] - cfg["reorg_tail_blocks"] + 1, 0)   # req 2: reorg tail rescan
            conn.execute("DELETE FROM transfers WHERE chain=? AND contract=? AND block_number>=?",
                        (chain, contract, lo))
        hi = chain_head
        if lo > hi:
            conn.commit()
            return {"available": True, "ingested_rows": 0, "new_head": head_row[0] if head_row else None,
                    "chain_head": chain_head, "calls_used": 0, "budget_exhausted": False, "gap": False}

        provider_max = max(p["max_range"] for p in provider_table)
        chunks = _plan_chunks(lo, hi, provider_max)
        calls_used = [0]
        new_head = head_row[0] if head_row else None
        inserted, gap, budget_exhausted = 0, False, False
        for (clo, chi) in chunks:
            if calls_used[0] >= cfg["rpc_call_budget"]:
                budget_exhausted = True
                break
            rows = _fetch_logs_chunk(contract, clo, chi, provider_table, rpc_call, sleep_fn, calls_used)
            if rows is None:
                gap = True   # req 4: a real gap — stop, do NOT advance the head past it
                break
            for lg in rows:
                parsed = _parse_transfer_log(lg)
                if parsed is None:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO transfers "
                    "(chain, contract, tx_hash, log_index, from_address, to_address, value_raw, block_number) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (chain, contract, parsed["tx_hash"], parsed["log_index"], parsed["from_address"],
                     parsed["to_address"], parsed["value_raw"], parsed["block_number"]))
                if cur.rowcount:
                    inserted += 1
            new_head = chi

        if new_head is not None:
            new_head_ts = now - (chain_head - new_head) * block_time
            conn.execute(
                "INSERT INTO index_head(chain,contract,head_block,head_ts,updated_ts) VALUES (?,?,?,?,?) "
                "ON CONFLICT(chain,contract) DO UPDATE SET head_block=excluded.head_block, "
                "head_ts=excluded.head_ts, updated_ts=excluded.updated_ts",
                (chain, contract, new_head, new_head_ts, now))
        conn.commit()
        return {"available": True, "ingested_rows": inserted, "new_head": new_head, "chain_head": chain_head,
                "calls_used": calls_used[0], "budget_exhausted": budget_exhausted, "gap": gap}
    finally:
        conn.close()


# ── read (pure SQLite, no network) ──────────────────────────────────────────
def freshness(chain, contract, now=None, db_path=None, max_age_min=None):
    contract = contract.lower()
    now = now if now is not None else time.time()
    cfg = load_cfg()
    max_age_min = max_age_min if max_age_min is not None else cfg["freshness_max_age_min"]
    conn = _conn(db_path)
    try:
        head = conn.execute("SELECT head_block, head_ts FROM index_head WHERE chain=? AND contract=?",
                            (chain, contract)).fetchone()
    finally:
        conn.close()
    if head is None or head[1] is None:
        return {"indexed": False, "fresh": False, "reason": "not ingested"}
    age_s = now - head[1]
    return {"indexed": True, "fresh": age_s <= max_age_min * 60, "age_s": age_s, "head_block": head[0]}


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def query_transfers(address, contract, chain_key, days=180, decimals=18, db_path=None, now=None):
    """token_transfers-envelope-compatible rows for one address (either leg) — pure SQLite,
    no network. Windowed to `days` via the approximate block-time conversion."""
    address, contract = address.lower(), contract.lower()
    now = now if now is not None else time.time()
    block_time = _BLOCK_TIME_S.get(chain_key, 3.0)
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT tx_hash, from_address, to_address, value_raw, block_number FROM transfers "
            "WHERE chain=? AND contract=? AND (from_address=? OR to_address=?) "
            "ORDER BY block_number DESC",
            (chain_key, contract, address, address)).fetchall()
        head = conn.execute("SELECT head_block, head_ts FROM index_head WHERE chain=? AND contract=?",
                            (chain_key, contract)).fetchone()
    finally:
        conn.close()
    out = []
    cutoff = now - days * 86400 if days is not None else None
    for tx_hash, frm, to, val_raw, block_number in rows:
        try:
            val = int(val_raw) / (10 ** decimals)
        except (TypeError, ValueError):
            val = None
        ts = None
        if head is not None and head[1] is not None:
            ts = head[1] - (head[0] - block_number) * block_time
        if cutoff is not None and ts is not None and ts < cutoff:
            continue
        out.append({"from_address": frm, "to_address": to, "value_decimal": val,
                    "block_timestamp": _iso(ts) if ts is not None else None,
                    "token_symbol": None, "transaction_hash": tx_hash})
    return out


def query_or_stale(address, contract, chain_key, days=180, decimals=18, db_path=None, now=None):
    """The provider-facing envelope: {available, source, partial, stale_index, txs}. A
    contract outside the tracked set bypasses the index entirely (no stale_index flag — it
    was never this index's job to serve it). An un-ingested range or a stale head is
    available:False (§3 gap honesty — never an empty-clean result standing in for unread
    data)."""
    if not contract or contract.lower() not in _tracked_contracts(chain_key):
        return {"available": False, "reason": "not in tracked set — index bypassed"}
    fr = freshness(chain_key, contract, now=now, db_path=db_path)
    if not fr["indexed"]:
        return {"available": False, "reason": "un-ingested range", "stale_index": False}
    if not fr["fresh"]:
        return {"available": False, "reason": f"index stale ({fr['age_s']:.0f}s old)",
                "stale_index": True, "age_s": fr["age_s"]}
    txs = query_transfers(address, contract, chain_key, days=days, decimals=decimals,
                          db_path=db_path, now=now)
    return {"available": True, "source": "local_index", "partial": False, "stale_index": False, "txs": txs}


# ── CLI ──────────────────────────────────────────────────────────────────────
def _live_rpc_call(url, method, params, timeout=12):
    import urllib.request
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    if "error" in d:
        raise RuntimeError(str(d["error"])[:160])
    return d.get("result")


def _tracked_chain_contracts():
    try:
        toks = json.loads(TRACKED_WALLETS_PATH.read_text()).get("tokens", {})
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for tok, info in toks.items():
        for chain, contract in (info.get("contracts") or {}).items():
            if chain in RPC_PROVIDER_TABLE:
                out.append((tok, chain, contract))
    return out


def tick(db_path=None, rpc_call=None):
    """One ops-cadence tick: ingest every tracked-set (chain, contract) pair whose chain has
    an RPC provider table. Ops wires the schedule (cron/launchd) separately — this is the
    single unit of work one invocation performs."""
    rpc_call = rpc_call or _live_rpc_call
    results = {}
    for tok, chain, contract in _tracked_chain_contracts():
        try:
            results[f"{tok}:{chain}"] = ingest_contract(chain, contract, db_path=db_path, rpc_call=rpc_call)
        except Exception as e:  # noqa: BLE001 — one contract's failure never sinks the tick
            results[f"{tok}:{chain}"] = {"available": False, "reason": str(e)[:160]}
    return results


def main():
    import argparse
    ap = argparse.ArgumentParser(description="SPEC-102 tracked-set local indexer")
    ap.add_argument("--tick", action="store_true", help="ingest every tracked (chain, contract) once")
    ap.add_argument("--status", action="store_true", help="freshness for every tracked (chain, contract)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.tick:
        out = {"ok": True, "data": tick(), "meta": {"mode": "tick"}}
    elif args.status:
        data = {f"{tok}:{chain}": freshness(chain, contract)
                for tok, chain, contract in _tracked_chain_contracts()}
        out = {"ok": True, "data": data, "meta": {"mode": "status"}}
    else:
        out = {"ok": False, "data": None, "meta": {"reason": "pass --tick or --status"}}

    if args.json:
        print(json.dumps(out, default=str))
    else:
        for k, v in (out["data"] or {}).items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    main()
