---
name: feedback-wallet-polling-gap
description: "watch_wallets.py polling misses intra-interval drains — for full picture, query Transfer event logs directly via eth_getLogs."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 7d189419-ea54-42ce-9123-4ae76f85face
---

`watch_wallets.py` only catches balance deltas between poll cycles, so a wallet draining in multiple tranches within a single interval shows up as one delta — or the early tranches get missed entirely if the script wasn't running yet.

**Why:** BILL TEAM-MAIN drained 40M BILL on 2026-05-13 through six staging wallets (each got 5-10M + a 100-token test send first). watch_wallets logged three 5M deltas totaling 15M (alerts at 14:50, 15:38, 17:20 UTC) — but the actual on-chain drain was 40M starting 07:47 UTC, before the script began polling. The missing 25M ($4.85M) included two full 10M transfers that defined the operator structure. Polling-only made the case look 2.6× smaller than reality.

**How to apply:** When a tracked wallet shows any non-zero outbound, immediately backfill with `eth_getLogs` against the token's Transfer event signature filtered to `topic1 = wallet padded to 32 bytes`. Use curl, not Python urllib (publicnode RPC 403s on Python user-agent). Look for the operator test-send pattern: 100 tokens followed minutes later by the main 5M+ transfer to the same address = staging-wallet fingerprint. Trace each staging wallet's onward transfers — they usually funnel into a single aggregator. Aggregator balance + ongoing outflow rate is the real distribution metric, not the team wallet's drained balance. For active Cat A trades, treat the polling alerts as triggers to do a full on-chain log sweep, not as the picture itself.
