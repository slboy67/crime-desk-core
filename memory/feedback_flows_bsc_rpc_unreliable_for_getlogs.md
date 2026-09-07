---
name: feedback_flows_bsc_rpc_unreliable_for_getlogs
description: "flows.py BSC getLogs scans across large block ranges chronically time out (HTTP 408) — \"no activity\" results are unreliable on BSC; use nonce_watch.py for single-address signals"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

`flows.py` for BSC tokens routinely fails large-range `eth_getLogs` calls with HTTP 408 Request Timeout (consistent across re-runs on the same block ranges). The script silently returns "no outbound from any tracked wallet" while the scan is actually incomplete — a false negative dressed as a clean read.

**Why:** ESPORTS 2026-05-24 — flows.py reported "✓ No outbound, 19/19 wallets dormant" twice in a row. Re-run reproduced the SAME getLogs timeouts on the SAME block ranges (100048129-100075131 area). The result was technically true for those block windows that did complete, but the overall read was structurally unreliable. Our own config notes (manually curated user intel) documented $16.1M DWF→CEX deposits earlier in the week — the digest and our own intel agreed, only flows.py was blind.

**How to apply:**
- On BSC tokens, **treat a `flows.py` "no activity" result as low-confidence** when the run shows any HTTP 408/timeout errors above the verdict line — those errors are getLogs failures, not "scanned and found nothing."
- For **single-address surveillance** (one team safe, one MM wallet, one specific escalation marker), use **`nonce_watch.py`** instead — it polls `eth_getTransactionCount` per address, which is a tiny reliable call that doesn't hit the getLogs range limits. Default tier filter is "distribution"; pass `--tier team` or `--tier all` to widen.
- For **full-watchlist 24h sweep** where flows.py is the only option, **read the timeout lines** before trusting the verdict, and consider the curated config `_note` fields ([[feedback_verify_wallet_history_not_just_balance]]) — manually-documented intel from prior sessions is often more current than a partial scan.
- TODO: backup BSC RPC endpoint in the script's pool, or chunk the getLogs into smaller ranges (currently 9000 blocks per call → reduce to 2000-3000 if endpoint won't honor larger).
