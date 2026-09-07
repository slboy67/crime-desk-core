---
name: reference_goplus_holders_workflow
description: "How to build Cat A team-wallet maps for BSC/Base/Polygon tokens via GoPlus API (free, no key) when Arkham has no entity coverage and holders.py is ETH-only"
metadata: 
  node_type: memory
  type: reference
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

For Cat A micro-cap tokens (engineered pumps), neither Arkham nor our `holders.py` reliably surfaces team safes. Arkham covers funds/MMs/known projects but not engineered-pump coins; `holders.py` uses Ethplorer = ETH-only. **GoPlus Token Security API fills the gap** — free, no key, returns top-10 holders + concentration metrics per chain.

**The endpoint:**
```
GET https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={contract}
```
Chain IDs: `56` = BSC, `1` = ETH, `8453` = Base, `137` = Polygon, `42161` = Arbitrum.

**Returns** (under `result.<contract_lower>`):
- `token_name`, `token_symbol`, `total_supply`, `holder_count`
- `holders[]`: array of top 10 with `address`, `balance`, `percent` (0.XX), `is_contract`, `is_locked`, `tag`

**The Cat A fingerprint identification workflow:**

1. Pull top 10 via GoPlus.
2. **Spot the "matching-allocation" pattern** — multiple wallets holding *exactly the same* percentage (e.g., 3 wallets at 8.00% each on AGT) = coordinated team batch (MYX/COAI/AGT fingerprint).
3. Audit each top EOA on the appropriate chain. For BSC, public RPCs don't support historical state — fall back to current `eth_getTransactionCount` + `eth_getBalance` only. Classify:
   - nonce 0 + 0 BNB + large %% = **🚩 PRISTINE MEGA-SAFE** (tier=team in `tracked_wallets.json`)
   - low nonce + minimal BNB + large %% = semi-dormant team safe
   - high nonce + small balance = ACTIVE MM / distribution wallet (tier=distribution)
4. Add to `config/tracked_wallets.json` under the token's `wallets` array with proper tier.
5. Arm `nonce_watch.py <TICKER> --tier team` to catch any pristine-mega-safe activation = Stage-5 fire moment.

**Validation 2026-05-26 — AGT (Alaya AI, BSC) discovery:**
- 161K holders, top 10 = 72.4% supply concentration
- **3 EOAs at EXACTLY 8.00% each, ALL nonce 0 + 0 BNB** = coordinated pristine mega-safes (`0xfd0871...`, `0xe1469d...`, `0x75bc9b...`)
- 4th pristine at 6.22% (`0x02323f...`) = 30.22% combined pristine team overhang
- 3 active distribution/MM wallets identified separately (high nonce, small balances)
- Built full 9-wallet map in 5 minutes; armed nonce_watch on the 4 pristine safes

**Counter-finding:** when GoPlus returns holders, treat the `is_contract` field as authoritative. Top 2 BEAT holders (46.7% combined) are CONTRACTS, not EOAs — those are vesting/staking contracts, not team safes. Watch via Transfer events on the contract, not nonce.

**Caveat — BSC RPC historical-state limitation:** public BSC RPCs (`bsc-dataseed*`) prune state. Activity audit can't query nonce/balance at historical blocks. We can only verify CURRENT state — meaning we can identify "pristine" (nonce=0) and "active" (nonce > 100) tiers, but can't compute tx_7d/tx_30d like we do for ETH. For ongoing activity tracking, rely on:
- `nonce_watch.py` polling current nonce (detects ticks live)
- `flows.py` (when working — has BSC getLogs reliability issues per [[feedback_flows_bsc_rpc_unreliable_for_getlogs]])

**Next applications:** apply same workflow to BEAT/IN (we have top-holder data already pulled 2026-05-26), and to any new Cat A token from `screener.py` that lacks a wallet map.

Related: [[reference_arkham_entity_discovery_workflow]] (for entities Arkham DOES cover — funds/MMs/known projects), [[feedback_dont_drift_to_perp_only_scans]] (build wallet maps; don't skip L1), [[feedback_flows_bsc_rpc_unreliable_for_getlogs]] (BSC RPC limits)
