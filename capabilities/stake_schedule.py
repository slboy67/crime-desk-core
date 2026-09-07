#!/usr/bin/env python3
"""stake_schedule.py — unlock cliffs + catalysts from a staking/lock contract (SPEC 43).

Native port of _oldrepo/scripts/stake_schedule.py (the RIVER unlock methodology,
generalized): scan a lock contract's event logs over the last N days (chunked
eth_getLogs on archive RPC), pick the primary event by topic[0] frequency, decode
each event's data field heuristically (uint256 slots: amount vs unix-timestamp),
aggregate FORWARD unlocks into a weekly schedule, flag CLIFF dates where >5% of the
decoded locked supply unlocks on one day, and optionally write those cliffs to
config/catalysts.json (--catalyst TICKER).

Why it matters (§4 sub-pattern B): an on-chain lock contract + unlock cliff ahead is
THE tell for the OTC/VC vesting-hedge fingerprint — deep-neg funding that is
delta-neutral and won't cover on a squeeze. No lock contract → no calendar → watch
safe nonces instead.

  python3 capabilities/stake_schedule.py 0x.. --chain bsc --json
  python3 capabilities/stake_schedule.py 0x.. --chain bsc --window 90 --catalyst RIVER --json
  # Token-mode (passive vesting contracts emit no events of their own — scan ERC20
  # Transfer events on the TOKEN contract where from=vesting):
  python3 capabilities/stake_schedule.py 0xVESTING --chain bsc --token 0xTOKEN --json
  # Escrow-mode (SPEC-165/167) is tried FIRST on every call, --token included — a
  # contract exposing getUnlockSchedules() decodes straight off that instead of the
  # event-log/token-transfer scan:
  python3 capabilities/stake_schedule.py 0xESCROW --chain bsc --float-supply 17190000 --json

JSON contract (one object):
  {contract, chain, mode: "lock"|"token"|"escrow", primary_event, locked_total,
   schedule, cliffs:[{date, amount, pct}], catalyst_written: bool}
  mode=lock:   schedule:[{week, amount, pct_of_locked}]; event-log heuristic decode.
  mode=escrow: schedule:[{date, amount, pct}]; SPEC-165/167 UUPS unlock-escrow adapter.
               getUnlockSchedules() returns a SINGLE dynamic array of (uint256 time,
               uint256 cumulativeAmount) 2-word structs (one head offset -> length
               word -> inline pairs) — not two independent uint256[] arrays; the
               live DEBIT contract's non-word-aligned second head word ruled that
               reading out (SPEC-167). Also carries `schedule_hash` (tripwire — a
               changed schedule is itself the signal), `mutable`/`mutators`/
               `owner_safes` (owner-only setters exist on-chain → the schedule can
               be rewritten at will), `pct_basis` ("float" when --float-supply
               given, else "locked_total"), `available_amount`/`withdrawn_amount`/
               `start_time` (auxiliary getters, best-effort, None on a failed read).
Non-lock contract (no escrow selectors, no events / no decodable future unlocks) →
clean {cliffs:[]}, `escrow_adapter_reason` ALWAYS names why the escrow adapter
didn't apply — including in --token mode (never silently skipped there).

RPC: archive-supporting endpoint FIRST per chain so historical block reads work —
for BSC that is https://bsc-mainnet.public.blastapi.io, the only known working
public BSC archive (memory/reference_bsc_archive_rpc.md).
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALYST_FILE = ROOT / "config" / "catalysts.json"

# Per-chain RPC pool. Archive-supporting RPC FIRST (historical state works there).
RPC_POOL = {
    "ethereum": [
        "https://eth-mainnet.public.blastapi.io",
        "https://ethereum-rpc.publicnode.com",
        "https://1rpc.io/eth",
    ],
    "binance-smart-chain": [
        "https://bsc-mainnet.public.blastapi.io",   # the only known public BSC archive
        "https://bsc-dataseed.binance.org",
        "https://bsc.publicnode.com",
    ],
    "base": [
        "https://base-mainnet.public.blastapi.io",
        "https://mainnet.base.org",
        "https://base.llamarpc.com",
    ],
    "polygon": [
        "https://polygon-mainnet.public.blastapi.io",
        # SPEC-192 #7: polygon-rpc.com is DEAD (401 "API key disabled, reason: tenant
        # disabled", live-verified 2026-09-02) — removed rather than left to burn a
        # timeout on every pool exhaustion.
        "https://polygon-bor-rpc.publicnode.com",
    ],
    "arbitrum": [
        "https://arbitrum-one.public.blastapi.io",
        "https://arb1.arbitrum.io/rpc",
    ],
    "optimism": [
        "https://optimism-mainnet.public.blastapi.io",
        "https://mainnet.optimism.io",
    ],
}

CHAIN_ALIASES = {"bsc": "binance-smart-chain", "bnb": "binance-smart-chain",
                 "eth": "ethereum", "arb": "arbitrum", "op": "optimism"}

BLOCKS_PER_DAY = {
    "ethereum": 7200, "binance-smart-chain": 28800, "base": 43200,
    "polygon": 43200, "arbitrum": 345600, "optimism": 43200,
}

CHUNK_BLOCKS = 5000          # eth_getLogs chunk (public RPCs cap 5000-10000)
CLIFF_PCT = 5.0              # a single date unlocking ≥5% of locked supply = CLIFF
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
UA = "curl/8.0"

# SPEC-165 — UUPS unlock-escrow adapter (DEBIT 0x1088b932…aa74 class). Selectors are
# keccak256(signature)[:4] for the exact signatures named in the ticket; hardcoded
# (same convention as onchain.py's BALANCEOF_SELECTOR) rather than computed at
# runtime — no keccak dependency in the capability.
# SPEC-167: the LIVE return is NOT two independent uint256[] arrays — the second
# head word (0x30) isn't word-aligned, so it can't be a second ABI offset. It's a
# single dynamic array of (uint256 time, uint256 cumulativeAmount) 2-word structs:
# one head offset -> length word -> `length` inline (time, cumulativeAmount) pairs.
ESCROW_GETUNLOCKSCHEDULES_SELECTOR = "0x02857579"   # getUnlockSchedules() -> UnlockStep[] (time, cumulativeAmount)[]
ESCROW_GETAVAILABLEAMOUNT_SELECTOR = "0x7bb476f5"   # getAvailableAmount() -> uint256
ESCROW_WITHDRAWNAMOUNT_SELECTOR = "0x830de4b1"      # withdrawnAmount() -> uint256
ESCROW_STARTTIME_SELECTOR = "0x78e97925"            # startTime() -> uint256
OWNER_SELECTOR = "0x8da5cb5b"                        # owner() -> address (Ownable)
# owner-only mutators — existence (not call) is the tripwire: a schedule the team can
# rewrite at will is a different risk than an immutable one. Detected by scanning
# runtime bytecode for the PUSH4 <selector> pattern (0x63 + 4-byte selector), never
# invoked (they're state-changing and would need real ABI-encoded args we don't have).
MUTATOR_SELECTORS = {
    "setUnlockSchedules": "0x83f9441e",   # setUnlockSchedules(uint256[],uint256[])
    "setStartTime": "0x3e0a322d",         # setStartTime(uint256)
    "emergencyWithdraw": "0x95ccea67",    # emergencyWithdraw(address,uint256)
    "upgradeToAndCall": "0x4f1ef286",     # upgradeToAndCall(address,bytes) — UUPS proxy upgrade
}


def rpc(chain, method, params):
    urls = RPC_POOL.get(chain)
    if not urls:
        raise RuntimeError(f"chain {chain} not in RPC_POOL")
    last_err = None
    for url in urls:
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                               "params": params}).encode()
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read())
                if "result" in resp and resp["result"] is not None:
                    return resp["result"]
                last_err = resp.get("error", "no result")
        except Exception as e:
            last_err = str(e)[:80]
    raise RuntimeError(f"all {len(urls)} {chain} RPCs failed (last: {last_err})")


def block_timestamp(chain, block_num, _cache={}):
    key = (chain, block_num)
    if key in _cache:
        return _cache[key]
    try:
        blk = rpc(chain, "eth_getBlockByNumber", [hex(block_num), False])
        ts = int(blk["timestamp"], 16)
        _cache[key] = ts
        return ts
    except Exception:
        return None


def fetch_logs(chain, contract, from_block, to_block, topics=None):
    """Chunked eth_getLogs over [from_block, to_block]; tolerant of per-chunk errors."""
    out, cur, errors = [], from_block, 0
    while cur <= to_block:
        end = min(cur + CHUNK_BLOCKS - 1, to_block)
        flt = {"fromBlock": hex(cur), "toBlock": hex(end), "address": contract}
        if topics:
            flt["topics"] = topics
        try:
            out.extend(rpc(chain, "eth_getLogs", [flt]))
        except Exception:
            errors += 1
            if errors > 5:
                break
        cur = end + 1
    return out


def decode_uint(hex32):
    if not hex32:
        return 0
    h = hex32[2:] if hex32.startswith("0x") else hex32
    try:
        return int(h, 16)
    except (ValueError, TypeError):
        return 0


def looks_like_timestamp(n, now):
    """Heuristic: uint256 in [now-2y, now+5y] = likely a unix timestamp."""
    return (now - 2 * 365 * 86400) < n < (now + 5 * 365 * 86400)


def analyze_event_data(log, now):
    """Decode log.data as N x uint256 slots → [(value, 'timestamp'|'amount'|'other')]."""
    data = log.get("data", "0x")
    if not data or data == "0x":
        return []
    h = data[2:]
    decoded = []
    for i in range(0, len(h), 64):
        s = h[i:i + 64]
        if len(s) != 64:
            continue
        try:
            v = int(s, 16)
        except ValueError:
            continue
        if looks_like_timestamp(v, now):
            hint = "timestamp"
        elif 0 < v < 10**30:
            hint = "amount"
        else:
            hint = "other"
        decoded.append((v, hint))
    return decoded


def _eth_call(chain, contract, selector):
    return rpc(chain, "eth_call", [{"to": contract, "data": selector}, "latest"])


def _decode_unlock_steps(hex_data):
    """ABI-decode the LIVE getUnlockSchedules() shape (SPEC-167): ONE head offset
    word -> at that word index, a length word, then `length` inline (time,
    cumulativeAmount) 2-word pairs (a dynamic array of statically-sized structs —
    NOT two independent uint256[] arrays with two head offsets, which the live
    contract's non-word-aligned second head word (0x30) rules out).

    hex->int only on exact 64-char word slices (never an empty/short slice — the
    `int('', 16)` crash SPEC-167 was filed against); tolerates trailing bytes past
    the last consumed word. Raises on malformed/truncated data — caller degrades."""
    h = hex_data[2:] if hex_data.startswith("0x") else hex_data

    def word(i):
        s = h[i * 64:(i + 1) * 64]
        if len(s) != 64:
            raise ValueError(f"truncated word at index {i} (hex_len={len(h)})")
        return int(s, 16)

    off_bytes = word(0)
    if off_bytes % 32 != 0:
        raise ValueError(f"head offset not word-aligned: {off_bytes}")
    off_words = off_bytes // 32
    length = word(off_words)
    start = off_words + 1
    times, cumulative = [], []
    for i in range(length):
        times.append(word(start + 2 * i))
        cumulative.append(word(start + 2 * i + 1))
    return times, cumulative


def _selector_in_bytecode(code, selector):
    """Heuristic mutator-existence check: PUSH4 <selector> (opcode 0x63) is how
    Solidity's dispatcher jump table encodes a function selector literal."""
    if not code or not isinstance(code, str):
        return False
    return ("63" + selector[2:].lower()) in code.lower()


def _try_escrow_adapter(contract, chain, decimals, float_supply, now):
    """SPEC-165 problem 2 — try the UUPS unlock-escrow selector set. Returns
    {available:False, reason} when getUnlockSchedules() isn't present/decodable
    (never raises — a non-escrow contract is a clean degrade, not a crash)."""
    try:
        raw = _eth_call(chain, contract, ESCROW_GETUNLOCKSCHEDULES_SELECTOR)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"getUnlockSchedules_call_failed:{str(e)[:80]}"}
    if not raw or raw in ("0x", "0x0"):
        return {"available": False, "reason": "getUnlockSchedules_not_present"}
    try:
        times, cumulative = _decode_unlock_steps(raw)
    except Exception as e:  # noqa: BLE001
        h = raw[2:] if raw.startswith("0x") else raw
        return {"available": False,
                "reason": f"getUnlockSchedules_undecodable:{str(e)[:80]} (hex_len={len(h)})"}
    if not times or len(times) != len(cumulative):
        return {"available": False, "reason": "getUnlockSchedules_shape_mismatch"}

    pairs = sorted(zip(times, cumulative))
    locked_total_raw = pairs[-1][1] if pairs else 0
    locked_total = locked_total_raw / (10 ** decimals)
    float_basis_raw = float_supply * (10 ** decimals) if float_supply else None
    basis_raw = float_basis_raw or locked_total_raw

    schedule, cliffs, prev_cum = [], [], 0
    for ts, cum in pairs:
        step_raw = max(0, cum - prev_cum)
        prev_cum = cum
        amount = step_raw / (10 ** decimals)
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        pct = round(step_raw / basis_raw * 100, 4) if basis_raw else 0
        schedule.append({"date": date, "amount": round(amount, 6), "pct": pct})
        if pct >= CLIFF_PCT:
            cliffs.append({"date": date, "amount": round(amount, 6), "pct": pct})

    aux = {}
    for key, sel in (("available_amount", ESCROW_GETAVAILABLEAMOUNT_SELECTOR),
                     ("withdrawn_amount", ESCROW_WITHDRAWNAMOUNT_SELECTOR),
                     ("start_time_raw", ESCROW_STARTTIME_SELECTOR)):
        try:
            aux[key] = decode_uint(_eth_call(chain, contract, sel))
        except Exception:  # noqa: BLE001 — auxiliary reads are best-effort
            aux[key] = None
    available_amount = (aux["available_amount"] / (10 ** decimals)
                        if aux.get("available_amount") is not None else None)
    withdrawn_amount = (aux["withdrawn_amount"] / (10 ** decimals)
                        if aux.get("withdrawn_amount") is not None else None)

    try:
        code = rpc(chain, "eth_getCode", [contract, "latest"])
    except Exception:  # noqa: BLE001
        code = None
    mutators = [name for name, sel in MUTATOR_SELECTORS.items()
               if _selector_in_bytecode(code, sel)]
    owner_safes = []
    if mutators:
        try:
            owner_raw = _eth_call(chain, contract, OWNER_SELECTOR)
            if owner_raw and len(owner_raw) >= 42:
                addr = "0x" + owner_raw[-40:].lower()
                if int(addr, 16) != 0:
                    owner_safes = [addr]
        except Exception:  # noqa: BLE001
            pass

    return {
        "available": True, "schedule": schedule, "cliffs": cliffs,
        "locked_total": round(locked_total, 6),
        "schedule_hash": hashlib.sha256(raw.encode()).hexdigest()[:16],
        "mutable": bool(mutators), "mutators": mutators, "owner_safes": owner_safes,
        "pct_basis": "float" if float_supply else "locked_total",
        "available_amount": available_amount, "withdrawn_amount": withdrawn_amount,
        "start_time": aux.get("start_time_raw"),
    }


def _week_start(date_str):
    """ISO-week bucket: Monday of the week containing date_str (YYYY-MM-DD)."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def _write_catalysts(ticker, cliffs, contract, cliff_type, source):
    """Append cliff entries to config/catalysts.json, deduped on (ticker,date,type)."""
    try:
        cal = json.loads(Path(CATALYST_FILE).read_text())
    except Exception:
        cal = {"_note": "", "catalysts": []}
    existing = {(c.get("ticker"), c.get("date"), c.get("type"))
                for c in cal.get("catalysts", [])}
    added = 0
    for c in cliffs:
        key = (ticker, c["date"], cliff_type)
        if key in existing:
            continue
        cal["catalysts"].append({
            "ticker": ticker, "date": c["date"], "type": cliff_type,
            "detail": f"~{c['amount']:,.0f} tokens ({c['pct']:.1f}% of locked) "
                      f"from contract {contract[:10]}...",
            "pct_supply": round(c["pct"], 1),
            "source": source,
        })
        added += 1
    with open(CATALYST_FILE, "w") as f:
        json.dump(cal, f, indent=2)
    return added


def _token_mode(contract, chain, token, from_block, latest, decimals, catalyst):
    """Passive vesting: scan ERC20 Transfer events on TOKEN where from=vesting.
    Releases ≥5% of held balance = (historical) cliffs."""
    out = {"contract": contract, "chain": chain, "mode": "token", "token": token,
           "primary_event": TRANSFER_TOPIC, "locked_total": 0,
           "schedule": [], "cliffs": [], "catalyst_written": False}
    try:
        bal_hex = rpc(chain, "eth_call", [{
            "to": token, "data": "0x70a08231" + ("0" * 24) + contract[2:]}, "latest"])
        balance = decode_uint(bal_hex) / (10 ** decimals)
    except Exception:
        balance = None
    out["locked_total"] = round(balance, 6) if balance is not None else 0

    padded_from = "0x" + ("0" * 24) + contract[2:].lower()
    releases = fetch_logs(chain, token, from_block, latest,
                          topics=[TRANSFER_TOPIC, padded_from])
    if not releases:
        return out

    by_date = defaultdict(int)
    for log in releases:
        try:
            block = int(log["blockNumber"], 16)
        except (KeyError, ValueError, TypeError):
            continue
        ts = block_timestamp(chain, block)
        if ts:
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            by_date[date] += decode_uint(log.get("data", "0x"))

    total_released = sum(by_date.values()) / (10 ** decimals)
    by_week = defaultdict(float)
    cliffs = []
    for date, amt in sorted(by_date.items()):
        tok = amt / (10 ** decimals)
        by_week[_week_start(date)] += tok
        pct_bal = (tok / balance * 100) if balance else 0
        if pct_bal >= CLIFF_PCT:
            cliffs.append({"date": date, "amount": round(tok, 6),
                           "pct": round(pct_bal, 4)})
    out["schedule"] = [
        {"week": w, "amount": round(a, 6),
         "pct_of_locked": round(a / total_released * 100, 4) if total_released else 0}
        for w, a in sorted(by_week.items())]
    out["cliffs"] = cliffs

    if catalyst and cliffs:
        _write_catalysts(catalyst, cliffs, contract, "unlock_historical",
                         f"stake_schedule --token {token} (passive vesting Transfer scan)")
        out["catalyst_written"] = True
    return out


def build_schedule(contract, chain, window=60, token=None, decimals=18,
                   catalyst=None, now=None, float_supply=None):
    """Core build. Returns the JSON contract dict; never raises on a non-lock
    contract — that path is a clean {cliffs:[]}.

    SPEC-165/167: tries the UUPS unlock-escrow selector set FIRST
    (`_try_escrow_adapter`) — regardless of `--token` — a contract that exposes
    getUnlockSchedules() decodes straight from that instead of the heuristic
    event-log/token-transfer scan below. Missing/undecodable selectors degrade to
    the token/lock scan, ALWAYS tagged with `escrow_adapter_reason` (never
    silently skipped just because --token was passed)."""
    contract = contract.lower()
    chain = CHAIN_ALIASES.get(chain.lower(), chain.lower())
    if chain not in RPC_POOL:
        raise ValueError(f"unknown chain: {chain}")
    now = now or int(time.time())
    bpd = BLOCKS_PER_DAY[chain]

    escrow = _try_escrow_adapter(contract, chain, decimals, float_supply, now)
    if escrow.get("available"):
        out = {"contract": contract, "chain": chain, "mode": "escrow",
               "window_days": window, "primary_event": "getUnlockSchedules()",
               "locked_total": escrow["locked_total"], "schedule": escrow["schedule"],
               "cliffs": escrow["cliffs"], "catalyst_written": False,
               "schedule_hash": escrow["schedule_hash"], "mutable": escrow["mutable"],
               "mutators": escrow["mutators"], "owner_safes": escrow["owner_safes"],
               "pct_basis": escrow["pct_basis"],
               "available_amount": escrow["available_amount"],
               "withdrawn_amount": escrow["withdrawn_amount"],
               "start_time": escrow["start_time"]}
        if catalyst and out["cliffs"]:
            _write_catalysts(catalyst, out["cliffs"], contract, "unlock",
                             f"stake_schedule --escrow {contract} (getUnlockSchedules)")
            out["catalyst_written"] = True
        return out

    latest = int(rpc(chain, "eth_blockNumber", []), 16)
    from_block = max(1, latest - window * bpd)

    if token:
        out = _token_mode(contract, chain, token.lower(), from_block, latest,
                          decimals, catalyst)
        out["escrow_adapter_reason"] = escrow.get("reason")
        return out

    out = {"contract": contract, "chain": chain, "mode": "lock",
           "window_days": window, "primary_event": None, "locked_total": 0,
           "schedule": [], "cliffs": [], "catalyst_written": False,
           "events_scanned": 0, "events_decoded": 0,
           "escrow_adapter_reason": escrow.get("reason")}

    logs = fetch_logs(chain, contract, from_block, latest)
    out["events_scanned"] = len(logs)
    if not logs:
        out["note"] = ("no events in window — dormant, non-lock contract, "
                       "wrong chain, or window too narrow")
        return out

    # primary event = most frequent topic[0] (likely the Stake/Lock event)
    by_topic = defaultdict(list)
    for log in logs:
        topic = (log.get("topics") or [None])[0]
        if topic:
            by_topic[topic].append(log)
    if not by_topic:
        out["note"] = "events carry no topics — cannot identify a lock event"
        return out
    primary_topic, primary_logs = max(by_topic.items(), key=lambda x: len(x[1]))
    out["primary_event"] = primary_topic

    # decode: amount = largest non-timestamp uint; endTime = largest timestamp
    unlocks_by_date = defaultdict(int)
    decoded_n = 0
    for log in primary_logs:
        decoded = analyze_event_data(log, now)
        amounts = [v for v, h in decoded if h == "amount"]
        timestamps = [v for v, h in decoded if h == "timestamp"]
        if not amounts:
            continue
        decoded_n += 1
        end_time = max(timestamps) if timestamps else None
        if end_time and end_time > now:
            end_date = datetime.fromtimestamp(end_time, tz=timezone.utc).strftime("%Y-%m-%d")
            unlocks_by_date[end_date] += max(amounts)
    out["events_decoded"] = decoded_n

    if not unlocks_by_date:
        out["note"] = ("no future unlocks decodable — primary event has no endTime, "
                       "lock periods already passed, or non-lock contract")
        return out

    total_raw = sum(unlocks_by_date.values())
    locked_total = total_raw / (10 ** decimals)
    out["locked_total"] = round(locked_total, 6)

    by_week = defaultdict(float)
    cliffs = []
    for date, amt in sorted(unlocks_by_date.items()):
        tok = amt / (10 ** decimals)
        by_week[_week_start(date)] += tok
        pct = (amt / total_raw * 100) if total_raw else 0
        if pct >= CLIFF_PCT:
            cliffs.append({"date": date, "amount": round(tok, 6),
                           "pct": round(pct, 4)})
    out["schedule"] = [
        {"week": w, "amount": round(a, 6),
         "pct_of_locked": round(a / locked_total * 100, 4) if locked_total else 0}
        for w, a in sorted(by_week.items())]
    out["cliffs"] = cliffs

    if catalyst and cliffs:
        _write_catalysts(catalyst, cliffs, contract, "unlock",
                         f"stake_schedule {contract}")
        out["catalyst_written"] = True
    return out


def render_human(d):
    print(f"# stake_schedule — {d['contract']} on {d['chain']} ({d.get('mode')})")
    if d.get("note"):
        print(f"  note: {d['note']}")
    print(f"  primary event: {d.get('primary_event')}")
    print(f"  locked total (decoded): {d.get('locked_total'):,}")
    if d.get("schedule"):
        print("\n## Unlock schedule by week")
        for r in d["schedule"]:
            print(f"  {r['week']}: {r['amount']:>18,.2f}  ({r['pct_of_locked']:.1f}% of locked)")
    if d.get("cliffs"):
        print("\n## CLIFF dates (>5% of locked supply)")
        for c in d["cliffs"]:
            print(f"  {c['date']}: {c['amount']:>18,.2f}  ({c['pct']:.1f}%)")
    else:
        print("\n## No cliffs decoded")
    if d.get("catalyst_written"):
        print("\n✓ cliffs written to config/catalysts.json")


def main():
    ap = argparse.ArgumentParser(
        description="Unlock cliffs + catalysts from a staking/lock contract (SPEC 43)")
    ap.add_argument("contract", help="staking/lock (or vesting) contract address 0x..")
    ap.add_argument("--chain", default="bsc",
                    help="bsc|ethereum|base|polygon|arbitrum|optimism (default bsc)")
    ap.add_argument("--window", type=int, default=60, help="days to scan back (default 60)")
    ap.add_argument("--token", default=None,
                    help="TOKEN-MODE: scan Transfer events on this token where from=contract")
    ap.add_argument("--decimals", type=int, default=18)
    ap.add_argument("--catalyst", default=None,
                    help="write cliffs to config/catalysts.json under this ticker")
    ap.add_argument("--float-supply", type=float, default=None, dest="float_supply",
                    help="SPEC-165: circulating float (tokens) — escrow-mode cliffs are "
                         "pct-of-float when given, else pct-of-decoded-locked-total")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    c = args.contract.lower()
    if not c.startswith("0x") or len(c) != 42:
        print(json.dumps({"error": f"bad contract address: {args.contract}"}))
        sys.exit(2)
    if args.token:
        t = args.token.lower()
        if not t.startswith("0x") or len(t) != 42:
            print(json.dumps({"error": f"bad --token contract: {args.token}"}))
            sys.exit(2)
    chain = CHAIN_ALIASES.get(args.chain.lower(), args.chain.lower())
    if chain not in RPC_POOL:
        print(json.dumps({"error": f"unknown chain: {args.chain}",
                          "known": sorted(RPC_POOL) + sorted(CHAIN_ALIASES)}))
        sys.exit(2)

    try:
        d = build_schedule(c, chain, window=args.window, token=args.token,
                           decimals=args.decimals, catalyst=args.catalyst,
                           float_supply=args.float_supply)
    except Exception as e:
        print(json.dumps({"error": f"rpc/build failed: {str(e)[:160]}",
                          "contract": c, "chain": chain, "cliffs": []}))
        return  # runtime (non-usage) failure: JSON error, exit 0
    if args.json:
        print(json.dumps(d))
    else:
        render_human(d)


if __name__ == "__main__":
    main()
