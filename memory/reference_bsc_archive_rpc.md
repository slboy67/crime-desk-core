---
name: reference_bsc_archive_rpc
description: Only known public BSC RPC with full state retention for historical queries — bsc-mainnet.public.blastapi.io. All bsc-dataseed.* and bsc.publicnode.com PRUNE state.
metadata: 
  node_type: memory
  type: reference
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

For BSC historical state queries (`eth_getTransactionCount(addr, hex(historical_block))`, `eth_getBalance` at non-`latest` block, `eth_call` at historical, etc.), most public BSC RPCs **prune state and only serve `"latest"`**. Verified 2026-05-26 via direct test:

| RPC | Latest | Historical (30d ago) |
|---|---|---|
| **`https://bsc-mainnet.public.blastapi.io`** | ✅ | ✅ **ARCHIVE — only known working public BSC archive** |
| `https://bsc.drpc.org` | ❌ HTTP err on latest | ✅ (occasionally) — flaky, partial |
| `https://bsc-dataseed.binance.org` | ✅ | ❌ pruned (code -32xxx) |
| `https://bsc.publicnode.com` | ✅ | ❌ pruned |
| `https://bsc-dataseed1.defibit.io` | ✅ | ❌ pruned |
| `https://bsc-dataseed1.ninicoin.io` | ✅ | ❌ pruned |
| `https://binance.llamarpc.com` | ❌ flaky | ❌ flaky |
| `https://rpc.ankr.com/bsc` | ❌ requires key | ❌ |
| `https://bsc.blockpi.network/v1/rpc/public` | ❌ rate-limited | ❌ |
| `https://bsc.api.onfinality.io/public` | ❌ flaky | ❌ |
| `https://bsc-rpc.publicnode.com` | ✅ | ❌ pruned |

**How to apply:**
- In any script that needs BSC historical state (activity_audit, regime_check historical, etc.), **`bsc-mainnet.public.blastapi.io` MUST be first** in the RPC pool.
- Keep dataseed/publicnode as fallback for `"latest"`-only queries (they're faster).
- If even Blastapi returns rate-limit errors, the alternative is paid Ankr/QuickNode archive endpoints — or fall back to chunked `eth_getLogs` Transfer event scans (slower but works on any RPC for short ranges).
- Same pattern likely applies on other L1/L2s — `<chain>-mainnet.public.blastapi.io` is the canonical free archive for Polygon/Base/Arbitrum/Optimism too (verified in activity_audit pool 2026-05-26).

**Validation test (re-run quarterly):**
```python
addr = "0xbece45649e0bd6930c0bfa823d085c48e45e50b3"  # any BSC EOA with history
latest = int(rpc(url, "eth_blockNumber", []), 16)
n_now = int(rpc(url, "eth_getTransactionCount", [addr, "latest"]), 16)
n_hist = int(rpc(url, "eth_getTransactionCount", [addr, hex(latest - 30*28800)]), 16)
# If both succeed and n_hist <= n_now: archive support confirmed
```

Related: [[feedback_flows_bsc_rpc_unreliable_for_getlogs]] (flows.py BSC getLogs issue — separate but related), [[reference_goplus_holders_workflow]] (use GoPlus for top-holder data; activity audit fills the historical activity gap)
