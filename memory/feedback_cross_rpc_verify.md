---
name: feedback-cross-rpc-verify
description: "When a wallet balance check returns ZERO or a result you didn't expect, verify against a second RPC before building a narrative on it. Transient RPC errors silently parse as zero."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: f1c24a74-6d40-46cb-b214-327247fa4e65
---

When an eth_call balance check returns **ZERO** for a wallet that the user is trading on, or returns any value that materially changes the trade thesis, **verify against a second RPC endpoint before reporting**. Single-RPC zero results are unreliable.

**Why:** On 2026-05-14, while doing the full 5-layer pull on BILL after user entered a 5× short at $0.196, my eth_call to `https://ethereum-rpc.publicnode.com` for terminal holder `0xe92e65049b3c2ca12806e9567b08895118c5a03f` returned a parsed value of 0 BILL. I built a "terminal holder drained to zero overnight → distribution accelerated" narrative on top. User asked "did u check everything on BILL" and a re-check via the SAME RPC returned the correct value: 12.7M BILL ($2.46M). The thesis claim was false — actual drain was only -1.16M ($225K), modest, not accelerated. I had to retract a key bullish-for-short claim mid-trade.

Root cause: my python parser had `int(d.get('result','0x0') or '0x0',16)/1e18` — if the RPC returns a transient error (rate limit, timeout, malformed JSON), the result field can be absent or empty string, and the default `'0x0'` parses to 0. A balance "drop to zero" silently looks identical to a real drain.

**How to apply:**
- For ANY balance check that materially affects a trade verdict (terminal holder, mega-safe, aggregator), if the result is **zero** or **drops dramatically** vs prior reading, re-run against a second RPC. Free RPCs that work: `https://ethereum-rpc.publicnode.com`, `https://eth.llamarpc.com`. For BSC: `https://bsc-dataseed.binance.org`, `https://bsc.publicnode.com`. (Ankr requires API key now — don't bother.)
- If the second RPC disagrees, **don't report the zero — report "RPC inconsistency, need third source"** and try a different endpoint or use Etherscan/Bscscan UI.
- Add a tighter parser to `scripts/watch_wallets.py` and `scripts/holders.py`: distinguish between "result: 0x0" (real zero) and "no result field / RPC error" (unreliable). Treat the latter as missing, not zero.
- Same principle for `eth_getCode`, `eth_getTransactionCount`: a "0" can be real or RPC-error. Verify before claiming "EOA with 0 txs."
- See [[feedback-wallet-polling-gap]] for the related rule about backfilling Transfer events when any outbound is seen — that's the *what* monitor; this is the *whether to trust* monitor.
