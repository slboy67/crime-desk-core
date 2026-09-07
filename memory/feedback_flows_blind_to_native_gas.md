---
name: flows-blind-to-native-gas
description: "flows.py only watches ERC-20 Transfer events on the token contract — it cannot see native BNB/ETH gas funding from CEX → wallet, which is the textbook pre-fire signal for Stage-5 distribution."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: e94ca703-b256-430c-897b-c7124651d8f5
---

`flows.py` scans transfer FLOWS for the specific token (e.g. ESPORTS) by calling `eth_getLogs` on that token's contract with the Transfer event topic. **It DOES NOT see native BNB or ETH transfers** to the same wallets — those don't emit a Transfer event on the token contract; they emit a value transfer in the block native layer that needs a separate scan path.

**Why:** ESPORTS 2026-05-20 — 8 staged wallets had been "dormant" per flows.py scans (4h, 6h, 24h, 36h, 72h all reported 9/9 dormant). I treated this as no on-chain activity. Reality: the wallets had received **BNB gas funding from MEXC + Bitget hot wallets** in two waves (~55min and ~14min before I checked), totaling ~$55 of BNB across 8 wallets — surgical pre-fire prep. The dump hadn't fired yet, but the operator had primed the infrastructure. I missed the entire pre-fire phase because the scanner doesn't cover native value transfers. User caught it from an external on-chain feed (@0xNoxxx + Etherscan/BSCscan-style view of native BNB inflows).

**How to apply:**
- When a Cat A token has mapped staged wallets sitting dormant for >24h with large token holdings, **separately verify they aren't being primed with BNB gas** before declaring "no on-chain activity." 
- For ESPORTS/BSC: pull native BNB transfers via a separate `eth_getLogs`-equivalent path, or check via BSCscan/Bubblemaps-style external view, or rely on user-provided intel.
- A wallet holding $1M+ of tokens that suddenly receives $5-15 of native gas from a CEX hot wallet = imminent dump preparation. This pattern is the LAST step before the dump (operators don't fund gas until they're about to fire).
- For ETH/Base/Arbitrum: same — native ETH gas funding from CEX → wallet is also invisible to ERC-20-only scans.
- Volume signature: $5-15 = enough gas for a few txs. $50+ = funding for many txs / runway. Either way, "tiny CEX → wallet inflow" on a sleeper Cat A wallet = signal.

**Script gap to fix later:** flows.py should optionally scan native value transfers (BSC: `eth_getBlockByNumber` with full tx list, filter by `to` address and `value > 0`). Until then, this is a manual verification step.

**Generalizes:** Any tooling that "scans on-chain activity" should be questioned for what layer it actually scans. ERC-20 Transfer events ≠ native transfers ≠ contract internal txs. Three different scan paths needed for complete picture.

Related: [[feedback-wallet-polling-gap]], [[feedback-cross-rpc-verify]], [[project-esports-thesis]]
