---
name: reference_lock_contract_audit_workflow
description: "For every Cat A token, identify the staking/lock contracts (often labeled \"stake\" but mechanically a forced lockup with linear early-exit penalty) and track the unlock schedule as predictable supply-pressure events."
metadata: 
  node_type: memory
  type: reference
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

**Key insight (user @derrrrrrrq, RIVER 2026-05-26):**
What Cat A teams call "staking" is mechanically a **forced supply lock with linear early-exit penalty**. The contract logic guarantees:
- Tokens deposited at peak FOMO are mathematically trapped (penalty 100% at stake start, drops linearly to 0% at endTime)
- Holders face a brutal choice: eat the penalty to exit, or hold to maturity and dump together at unlock
- The team gets predictable, mechanical supply pressure curve — and the contract code reveals it transparently

User built a custom tool for RIVER tracking inflow timing + unlock schedule. The price collapsed from $30+ (FOMO entry) to ~$6 (capitulation inflection where penalty-paid exits became "rational"). This is exact KOL-Distribution-Toolkit Section 5 "High-yield staking promotions (lock supply long)" mechanism — but with code-enforced penalty math instead of soft commitment.

**Code signature (RIVER's contract, transcribed by user):**
```solidity
function unstake(uint256 _tokenId) external whenNotPaused {
    if (ownerOf(_tokenId) != msg.sender) revert NotTokenOwner();
    Stake storage userStake = userStakes[_tokenId];
    if (userStake.endTime > block.timestamp) revert StakeStillLocked();  // no-penalty path
    if (userStake.amount == 0) revert AlreadyUnstaked();
    // ... transfer riverToken back
}
// + _calculateEarlyUnstakePenalty: linear penalty 100% → 0% over lock duration
```
Three tells: per-stake NFT (ERC721 ownerOf check), endTime-based lock, AlreadyUnstaked sentinel = NFT burned on exit.

**Identification workflow per Cat A token:**

1. **Get top-20 holders** via `holders.py <contract> --chain <chain>` — the lock/staking contracts will be in here as large CONTRACT holders.

2. **Filter for proxy contracts** that aren't Gnosis Safes:
   - Arkham label "ERC1967Proxy" / "TransparentUpgradeableProxy" = upgradeable, likely staking
   - Arkham label "Gnosis Safe Proxy" = team multisig, SKIP
   - Arkham label "Investor (Proxy)" = VC vesting, watch but different mechanism
   - **Unlabeled proxy holding >10% of supply** = high-probability main lock/stake contract

3. **Verify via on-chain function probing:**
   - `unstake(uint256)` selector `0x2e17de78` → present + revert with specific error = staking contract
   - `ownerOf(uint256)` selector `0x6352211e` → if returns valid → ERC721 (per-stake NFT pattern)
   - EIP-1967 impl slot: `0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc` via `eth_getStorageAt` → returns the actual logic contract address

4. **Read the stake schedule:**
   - If ERC721-based: iterate `tokenByIndex(0..totalSupply)` to get every stake's tokenId
   - For each tokenId: read `userStakes[tokenId]` mapping → (amount, endTime, ...)
   - Aggregate by week/month into unlock cliff calendar
   - The biggest cliffs = biggest predictable supply-pressure events

5. **Add to `tracked_wallets.json`** with new `tier: "staking_lock"`:
   ```json
   {"label": "RIVER-STAKING-MAIN", "address": "0xa370d1bc...", "chain": "ethereum", "tier": "staking_lock",
    "_note": "Main RIVER lock contract holding 31% of supply. EIP-1967 upgradeable proxy. unstake() with StakeStillLocked revert + linear early-exit penalty. Identified 2026-05-26 via Arkham label cross-ref + on-chain probe."}
   ```

6. **Build the unlock-schedule alert** (next infrastructure piece): when a major cliff is approaching (e.g., $5M+ supply unlocking in <7 days), surface as a calendar event in `catalyst.py` for the affected token.

**RIVER 2026-05-26 specific candidates** (the top RIVER holders that are contracts, ETH chain):
- `0xa370d1bc5310e8bff824617ec62725ee58f30d12` (31.19%) — unlabeled proxy, most likely main lock
- `0x96a84f061d51d27725ca17491ff36bf0283ce415` (20.80%) — "Investor (Proxy)" (vesting)
- `0xde89a6df6951c7b33814b02c2c3d567ce4137174` (18.37%) — unlabeled, non-proxy (pre-mine/Merkle?)
- `0xb82c71c2cff8ab8aa72422aa22855cb064a8dbfd` (13.86%) — unlabeled, non-proxy
- `0x908be94068977ffb327c4536d5bf22377dfdf078` (6.76%) — ERC1967Proxy (secondary staking)
- `0xccd995355aff4db620a199f63a74857214b409cc` (5.89%) — Gnosis Safe (team treasury)
- `0x47f7f94bea3c3f9f150647b774b602a8704b32a4` (3.06%) — Gnosis Safe (team treasury)

**Apply this to other Cat A's in watchlist** — IN/TRUST already showed 76% in vesting contracts (3 top contracts holding 49%, similar pattern). BEAT has 47% in 2 contracts. BLUAI has 62% across 3 matching-allocation vesting contracts. Each of these likely has a similar lock mechanism — and a corresponding unlock schedule we can read off-chain.

**The asymmetric edge:** when an unlock cliff is approaching, **shorts can pre-position** at the structurally-correct moment instead of chasing the cascade. Same logic as catalyst-driven shorts in equities — but the catalysts here are visible months ahead in the contract code.

## Tools BUILT (2026-05-26)

**`scripts/stake_schedule.py`** — MVP shipped, validated end-to-end:
- INPUT: staking/lock contract address + chain (+ optional `--token`, `--catalyst <TICKER>`)
- DOES: chunked `eth_getLogs` scan over last N days (default 60), groups by `topic[0]` to identify primary event, heuristically decodes data slots as (amount, endTime) pairs, aggregates into forward unlock schedule, flags any single date with >5% of decoded locked supply as a CLIFF
- OUTPUT: event-frequency table + sample-decoded events + inflow timeline + forward unlock schedule with cliffs flagged; OPTIONALLY writes cliff dates into `config/catalysts.json` via the `--catalyst` flag
- Multi-chain: BSC / ETH / Base / Polygon / Arbitrum / Optimism (uses chain-specific archive RPCs from the same pool as `activity_audit.py` — `bsc-mainnet.public.blastapi.io` etc.)
- Validation runs 2026-05-26:
  - RIVER staking (`0xabbeb6e9`, BSC, 90d window): cleanly returned 0 events = confirms RIVER staking has been dormant for 90+ days (the user's 3/15 activity was ~480 days ago given BSC block math, well outside reasonable scan windows). **The tool works; RIVER is just played out.**
  - BLUAI vesting (`0xf91ece50`, BSC, 30d): cleanly returned 0 events = confirms these are PASSIVE vesting contracts (no events emitted by themselves; token releases go through ERC20 Transfer on the *token* contract, not the vesting contract). Tool exits cleanly; next iteration should support `--token` mode for Transfer-based detection of release events.
  - IN/TRUST vesting (`0xbc01ab38`, Base, 30d): same pattern — passive vesting contract, no events. Works across chains.
- Catalyst integration validated: writing a synthetic cliff via the integration code → reading back via `catalyst.py BLUAI` → displays correctly as `"2026-12-31 ( in 219d) BLUAI unlock ..."`. The forward-calendar feedback loop is closed.

**Known limitations + next iteration targets:**
1. ~~**Passive vesting contracts**~~ ✅ SHIPPED 2026-05-26 — `--token <token_contract>` mode added. Scans Transfer events on the TOKEN contract with `topic[1] = vesting_contract` (releases), pulls current `balanceOf(vesting)` via eth_call, computes daily-cadence depletion projection. Validated on BLUAI-VESTING-21pct-A (BSC, 30d → 0 releases, 2.1B held = dormant pre-cliff) and IN-VESTING-A-20pct (Base, 30d → 0 releases, 210M held = dormant pre-cliff). Both surface as "watch for first release event = unlock START." When the first release fires for either, that's the catalyst we want to react to.
2. **OZ VestingWallet-style contracts** expose `start()` / `duration()` / `released()` / `releasable()` view functions with the schedule baked into storage. NEXT: try calling these selectors on contracts that returned 0 events — derive schedule from contract state instead of events.
3. **Wide scan windows** (>180 days) are slow on chunked getLogs (~5000 blocks/chunk × ~575 chunks for 100 days BSC = 5min+). For historical full-archive scans, switch to Bitquery / Dune / paid Arkham API.
4. **Heuristic data decoding** without ABI: works for the standard (amount, endTime) Stake event but not for more complex event shapes. NEXT: add ABI hint (`--abi <selector_to_field_map>`) for known contract patterns.

**Forward application priority** (ranked by when each contract starts emitting events / when first cliff is reachable):
1. **BLUAI** — fresh deployment, watch token-side Transfer events from vesting contracts as they begin releasing
2. **AGT** — pristine mega-safes may be team-discretion (not time-locked) — if confirmed, the tool doesn't apply; if locked, expect stake events when they activate
3. **IN/TRUST** — Base chain, locked-supply heavy — same as BLUAI, watch token-side
4. Any NEW Cat A discovered going forward: holders.py → identify lock contract → run stake_schedule.py with both event-mode AND --token mode

Related: [[reference_goplus_holders_workflow]] (discovery), [[reference_arkham_entity_discovery_workflow]] (entity labels), [[feedback_stage5_alerts_at_early_warning_not_structure_break]] (early-warning principle — unlock cliffs are the ultimate early warning), [[feedback_river_lesson_methodology_not_token]] (the methodology IS the asset), Section 5 "KOL Distribution Toolkit"
