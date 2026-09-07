#!/usr/bin/env python3
"""nonevm_chains.py — SPEC-192 #4/#5/#6: keyless readers for chains with no JSON-RPC/
Etherscan-shaped API — Sui (GraphQL), Solana (official RPC), TON (toncenter v3),
Cardano (Koios), Algorand (Nodely). None of these chains has EVM's nonce/code/getLogs
concepts, so their liveness/identity fields are explicit `n/a`, never a fabricated 0
(SPEC-192 #6's own instruction). Every function is best-effort and network-injectable
(`post_fn`/`get_fn`) for offline tests — a dead/malformed response degrades to a named
reason, never a silent empty list (the desk-wide §3 doctrine).

Endpoints + live-verified shapes: reports/RESEARCH-2026-09-02-onchain-chain-coverage.md
(2026-09-02, keyless, no keys used). Cite it — do not re-derive the endpoints.

  Sui:      fullnode JSON-RPC is DEAD (deprecated week of 2026-07-27) — graphql.mainnet.
            sui.io/graphql is the keyless replacement. Holders come from GoPlus
            (onchain.py's _GOPLUS_CHAIN — this module owns FLOW only).
  Solana:   getSignaturesForAddress + getTransaction on the official public RPC (100
            req/10s/IP). getTokenLargestAccounts is 429-throttled on every call — not
            used; holders come from GoPlus (onchain.py), same split as Sui.
  TON:      toncenter v3 (1 rps keyless) — jetton/wallets (holders, sorted by balance)
            + jetton/transfers (flow).
  Cardano:  Koios (public tier unauthenticated) — asset_addresses (holders, ~20s+ on
            large assets) + asset_txs (flow).
  Algorand: Nodely indexer — assets/{id}/balances (holders) + assets/{id}/transactions
            (flow).
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

UA = {"User-Agent": "crime-desk-onchain/1.0"}


def _get_json(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def _post_json(url, payload, timeout=15):
    try:
        body = json.dumps(payload).encode()
        headers = dict(UA)
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def _parse_iso_ts(s):
    """"2026-03-30T03:32:38.713Z" style -> epoch seconds, or None."""
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2).timestamp()
    except (TypeError, ValueError):
        return None


# ── Sui — GraphQL flow (holders: onchain._goplus_concentration, chain="sui") ────────
SUI_GRAPHQL_URL = "https://graphql.mainnet.sui.io/graphql"

_SUI_TX_QUERY = """
query($addr: SuiAddress!, $n: Int!) {
  address(address: $addr) {
    transactions(last: $n, filter: {affectedAddress: $addr}) {
      nodes { digest effects { timestamp balanceChangesJson } }
    }
  }
}
"""
# Live-verified 2026-09-02 against graphql.mainnet.sui.io (MAGMA holder, digest
# EQyGoZwD5b7YuZD23pBHbmFbeccnnnD1MPFd9FC4Z9Vi — the same example transaction quoted in
# reports/RESEARCH-2026-09-02-onchain-chain-coverage.md §3.7): `balanceChanges` is a
# paginated Connection (BalanceChangeConnection), not a plain list — its own JSON
# passthrough field `balanceChangesJson` returns the flat array directly with a bare
# `address` field (never nested under `owner`). Using the JSON field sidesteps needing
# the Connection's `nodes`/`pageInfo` sub-schema entirely.


def _parse_sui_transactions(body, address, coin_type):
    """GraphQL response body -> Moralis-shaped flow rows for `coin_type` balance
    changes touching `address`. A tx with no matching-coinType change is skipped
    (irrelevant to this asset); the counterparty is resolved as the other address in the
    SAME balanceChangesJson list whose sign is opposite (best-effort, the common
    2-party transfer case) — left None when ambiguous, never guessed."""
    nodes = ((((body or {}).get("data") or {}).get("address") or {})
             .get("transactions") or {}).get("nodes") or []
    addr_l = (address or "").lower()
    rows = []
    for node in nodes:
        digest = node.get("digest")
        effects = node.get("effects") or {}
        ts = effects.get("timestamp")
        changes = [c for c in (effects.get("balanceChangesJson") or [])
                  if c.get("coinType") == coin_type]
        mine = next((c for c in changes if (c.get("address") or "").lower() == addr_l), None)
        if mine is None:
            continue
        try:
            amt = int(mine.get("amount"))
        except (TypeError, ValueError):
            continue
        counterpart = next((c for c in changes if c is not mine
                            and (amt > 0) == (_safe_int(c.get("amount")) < 0)), None)
        cp_addr = counterpart.get("address") if counterpart else None
        from_addr = address if amt < 0 else cp_addr
        to_addr = cp_addr if amt < 0 else address
        rows.append({
            "from_address": (from_addr or "").lower() or None,
            "to_address": (to_addr or "").lower() or None,
            "value": str(abs(amt)), "value_decimal": None,
            "block_timestamp": ts, "token_symbol": None, "token_decimals": None,
            "transaction_hash": digest, "address": coin_type,
        })
    return rows


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def sui_flow(address, coin_type, days=180, last_n=50, post_fn=None):
    """SPEC-192 #4: Sui flow via GraphQL (`transactions(filter:{affectedAddress})` +
    `balanceChanges`). Returns (rows, degraded_reason|None) — a dead/malformed response
    is a NAMED degradation, never a silent []. `post_fn(query, variables) -> dict` is
    injectable for tests; the default POSTs to graphql.mainnet.sui.io."""
    try:
        if post_fn is not None:
            body = post_fn(_SUI_TX_QUERY, {"addr": address, "n": last_n})
        else:
            body = _post_json(SUI_GRAPHQL_URL,
                              {"query": _SUI_TX_QUERY, "variables": {"addr": address, "n": last_n}})
    except Exception as e:  # noqa: BLE001
        return [], f"sui graphql unreachable: {str(e)[:120]}"
    if not isinstance(body, dict):
        return [], "sui graphql unreachable: no response"
    if body.get("errors"):
        return [], f"sui graphql error: {str(body['errors'])[:160]}"
    rows = _parse_sui_transactions(body, address, coin_type)
    cutoff = time.time() - days * 86400
    out = []
    for r in rows:
        e = _parse_iso_ts(r.get("block_timestamp"))
        if e is not None and e < cutoff:
            continue
        out.append(r)
    return out, None


# ── Solana — official RPC flow (holders: GoPlus top-10, chain="solana") ─────────────
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"


def _solana_rpc(method, params, get_fn=None, timeout=15):
    if get_fn is not None:
        return get_fn(method, params)
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return _post_json(SOLANA_RPC_URL, payload, timeout=timeout)


def _parse_solana_tx(tx, signature, block_time):
    """One `getTransaction` result -> a Moralis-shaped SPL-transfer row via the
    pre/postTokenBalances delta (works for any SPL token, not just a hardcoded mint).
    None when the tx carries no token-balance change at all (e.g. a vote/system tx)."""
    meta = (tx or {}).get("meta") or {}
    pre = {b.get("accountIndex"): b for b in (meta.get("preTokenBalances") or [])}
    post = {b.get("accountIndex"): b for b in (meta.get("postTokenBalances") or [])}
    changes = []
    for idx in set(pre) | set(post):
        p, q = pre.get(idx), post.get(idx)
        try:
            pa = float(((p or {}).get("uiTokenAmount") or {}).get("uiAmount") or 0)
            qa = float(((q or {}).get("uiTokenAmount") or {}).get("uiAmount") or 0)
        except (TypeError, ValueError):
            continue
        delta = qa - pa
        if abs(delta) < 1e-12:
            continue
        owner = (q or p or {}).get("owner")
        mint = (q or p or {}).get("mint")
        changes.append((owner, delta, mint))
    if not changes:
        return None
    sender = next((o for o, d, m in changes if d < 0), None)
    receiver = next((o for o, d, m in changes if d > 0), None)
    amt = max((abs(d) for o, d, m in changes), default=None)
    mint = next((m for o, d, m in changes if m), None)
    ts_iso = None
    if block_time is not None:
        try:
            ts_iso = datetime.fromtimestamp(block_time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except (TypeError, ValueError, OSError):
            ts_iso = None
    return {"from_address": (sender or "").lower() or None, "to_address": (receiver or "").lower() or None,
            "value": None, "value_decimal": amt, "block_timestamp": ts_iso,
            "token_symbol": None, "token_decimals": None,
            "transaction_hash": signature, "address": mint}


def solana_flow(address, days=180, limit=50, get_fn=None):
    """SPEC-192 #5: getSignaturesForAddress + getTransaction (the two methods that
    actually answer keyless on the public RPC — getTokenLargestAccounts/
    getTokenAccounts are 429-throttled on every attempt, live-verified). Returns
    (rows, degraded_reason|None)."""
    try:
        sig_resp = _solana_rpc("getSignaturesForAddress", [address, {"limit": limit}], get_fn=get_fn)
    except Exception as e:  # noqa: BLE001
        return [], f"solana rpc unreachable: {str(e)[:120]}"
    if not isinstance(sig_resp, dict):
        return [], "solana rpc unreachable: no response"
    sigs = sig_resp.get("result")
    if not isinstance(sigs, list):
        return [], f"solana getSignaturesForAddress malformed: {str(sig_resp)[:160]}"
    cutoff = time.time() - days * 86400
    rows = []
    for s in sigs:
        bt = s.get("blockTime")
        if bt is not None and bt < cutoff:
            continue
        sig = s.get("signature")
        if not sig:
            continue
        try:
            tx_resp = _solana_rpc(
                "getTransaction",
                [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                get_fn=get_fn)
        except Exception:  # noqa: BLE001 — one bad tx read never kills the sweep
            continue
        tx = (tx_resp or {}).get("result") if isinstance(tx_resp, dict) else None
        row = _parse_solana_tx(tx, sig, bt)
        if row:
            rows.append(row)
    return rows, None


# ── TON — toncenter v3 (holders + flow), keyless (1 rps) ────────────────────────────
TONCENTER_BASE = "https://toncenter.com/api/v3"


def ton_holders(jetton_master, limit=10, get_fn=None):
    """Jetton wallets sorted by balance = the top-holder list (SPEC-192 #6). No
    nonce/code concept on TON accounts — not attempted, not faked."""
    url = f"{TONCENTER_BASE}/jetton/wallets?jetton_address={jetton_master}&limit={limit}&sort=desc"
    d = get_fn(url) if get_fn else _get_json(url)
    wallets = (d or {}).get("jetton_wallets")
    if not isinstance(wallets, list):
        return {"available": False, "reason": "toncenter jetton/wallets unavailable"}
    holders = [{"address": w.get("owner"), "balance": w.get("balance"), "nonce": "n/a"}
              for w in wallets]
    return {"available": True, "source": "toncenter", "holders": holders,
           "holder_count": None, "top1_pct": None, "top10_pct": None}


def ton_flow(jetton_master, limit=50, get_fn=None):
    url = f"{TONCENTER_BASE}/jetton/transfers?jetton_master={jetton_master}&limit={limit}&sort=desc"
    d = get_fn(url) if get_fn else _get_json(url)
    xfers = (d or {}).get("jetton_transfers")
    if not isinstance(xfers, list):
        return [], "toncenter jetton/transfers unavailable"
    rows = []
    for t in xfers:
        rows.append({"from_address": t.get("source"), "to_address": t.get("destination"),
                     "value": t.get("amount"), "value_decimal": None,
                     "block_timestamp": None, "token_symbol": None, "token_decimals": None,
                     "transaction_hash": t.get("transaction_hash"), "address": jetton_master})
    return rows, None


# ── Cardano — Koios (holders + flow), keyless public tier ───────────────────────────
KOIOS_BASE = "https://api.koios.rest/api/v1"
# SPEC-192 #6 fix: live-verified 2026-09 — `_get_json`'s 15s default timed out on
# `asset_addresses` in practice (silently returning None -> "unavailable" on a query
# that actually succeeds at ~20s); the module docstring already warned "~20s+, budget
# accordingly" but the code never did. 30s gives real headroom above the observed ~22s.
KOIOS_TIMEOUT_S = 30


def cardano_holders(policy_id, asset_name_hex, limit=10, get_fn=None):
    """SPEC-192 #6: Koios `asset_addresses` — public tier is unauthenticated but SLOW
    on large assets (~20s+, live-verified on NIGHT); callers must budget accordingly.
    No nonce/code concept on Cardano UTXO addresses — n/a, never faked."""
    url = f"{KOIOS_BASE}/asset_addresses?_asset_policy={policy_id}&_asset_name={asset_name_hex}"
    d = get_fn(url) if get_fn else _get_json(url, timeout=KOIOS_TIMEOUT_S)
    if not isinstance(d, list):
        return {"available": False, "reason": "koios asset_addresses unavailable"}
    d = sorted(d, key=lambda r: -_safe_int(r.get("quantity")))[:limit]
    holders = [{"address": r.get("payment_address"), "balance": r.get("quantity"), "nonce": "n/a"}
              for r in d]
    return {"available": True, "source": "koios", "holders": holders,
           "holder_count": None, "top1_pct": None, "top10_pct": None}


def cardano_flow(policy_id, asset_name_hex, limit=50, get_fn=None):
    url = f"{KOIOS_BASE}/asset_txs?_asset_policy={policy_id}&_asset_name={asset_name_hex}&limit={limit}"
    d = get_fn(url) if get_fn else _get_json(url, timeout=KOIOS_TIMEOUT_S)
    if not isinstance(d, list):
        return [], "koios asset_txs unavailable"
    rows = [{"from_address": None, "to_address": None, "value": None, "value_decimal": None,
            "block_timestamp": None, "token_symbol": None, "token_decimals": None,
            "transaction_hash": r.get("tx_hash"), "address": policy_id} for r in d]
    return rows, None


# ── Algorand — Nodely indexer (holders + flow), keyless ─────────────────────────────
NODELY_IDX_BASE = "https://mainnet-idx.4160.nodely.dev/v2"


def algorand_holders(asset_id, limit=10, get_fn=None):
    """SPEC-192 #6: Nodely `assets/{id}/balances`. No nonce concept on Algorand
    accounts — liveness would be tx-count/last-round, not attempted here (n/a)."""
    url = f"{NODELY_IDX_BASE}/assets/{asset_id}/balances?limit={limit}&currency-greater-than=0"
    d = get_fn(url) if get_fn else _get_json(url)
    balances = (d or {}).get("balances")
    if not isinstance(balances, list):
        return {"available": False, "reason": "nodely assets/balances unavailable"}
    holders = [{"address": b.get("address"), "balance": b.get("amount"), "nonce": "n/a"}
              for b in sorted(balances, key=lambda b: -_safe_int(b.get("amount")))[:limit]]
    return {"available": True, "source": "nodely", "holders": holders,
           "holder_count": None, "top1_pct": None, "top10_pct": None}


def algorand_flow(asset_id, limit=50, get_fn=None):
    url = f"{NODELY_IDX_BASE}/assets/{asset_id}/transactions?limit={limit}"
    d = get_fn(url) if get_fn else _get_json(url)
    txs = (d or {}).get("transactions")
    if not isinstance(txs, list):
        return [], "nodely assets/transactions unavailable"
    rows = []
    for t in txs:
        # SPEC-192 #6 fix: live-verified — an asset's tx list includes non-transfer
        # rows too (asset-config creation, app calls that merely reference the asset)
        # with no "asset-transfer-transaction" key at all; emitting a row for those
        # produced a to_address=None/value=None row that isn't a transfer. Skip them —
        # every REAL transfer row does have this key (even a 0-amount opt-in transfer).
        xfer = t.get("asset-transfer-transaction")
        if not isinstance(xfer, dict):
            continue
        rows.append({"from_address": t.get("sender"), "to_address": xfer.get("receiver"),
                    "value": xfer.get("amount"), "value_decimal": None,
                    "block_timestamp": None, "token_symbol": None, "token_decimals": None,
                    "transaction_hash": t.get("id"), "address": str(asset_id)})
    return rows, None
