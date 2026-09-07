---
name: feedback_river_lesson_methodology_not_token
description: "The RIVER case study from @derrrrrrrq 2026-05-26 — the asset isn't tracking RIVER (already played out), it's the generic methodology of reading lock contracts to derive supply-pressure curves BEFORE they hit price. Apply prospectively to every new Cat A."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

**User instruction (paraphrased):** "RIVER is the example, not the prize. Learn what was built and how to use it for future tokens." The asset is the methodology, not the specific token.

**What @derrrrrrrq's tool actually did (decomposed):**

1. **Inflow telemetry** — scanned every Stake/convertAndStake event from staking contract genesis. Logged (timestamp, tokenId, amount, lockDuration) per stake. Tells you AT WHAT PRICE capital got trapped (RIVER's FOMO inflows happened $23-30, locked).
2. **Unlock schedule** — aggregated by `endTime` (= stake_time + lockDuration) into a calendar of "supply unlocking on date D = $Y million." Forward-projects the supply pressure curve months ahead.
3. **Inflection-point math** — for each stake computed "amount × current early-exit penalty %" to identify the price level where penalty-paid exits become rational. Predicted RIVER's capitulation inflection at ~$6 (it actually went there from $30+).

**The brilliance:** all three are computable from contract data alone, **before** price reacts. The team's intent leaks the moment the contract is deployed. The early-exit penalty function (linear 100%→0% over lock duration) IS the smoking gun — "once the contract came out, the intent was crystal clear."

**How we generalize:**

This isn't a RIVER tool — it's a generic lock-contract telemetry workflow that applies to ANY Cat A token with NFT-based (or mapping-based) lock staking. Build `stake_schedule.py`:
- INPUT: staking contract address + chain (+ optional token contract for price lookup)
- SCAN: eth_getLogs for Stake events (chunked, archive RPC); for each decode (tokenId, amount, lockDuration)
- COMPUTE: endTime per stake, current-penalty per stake, weighted-average inflection price
- OUTPUT: unlock schedule (date → cumulative $ unlocking), cliff alerts (>5% in one day), penalty-inflection price, capital-trapped-by-entry-price histogram

**Where it lives in the toolchain:**
- Runs AFTER holders.py identifies a staking contract candidate (via Arkham "ERC1967Proxy" label + on-chain unstake() probe per [[reference_lock_contract_audit_workflow]])
- Output feeds catalyst.py — unlock cliffs become predictable forward catalysts
- The "inflection price" output adds a new column to triage: "current price vs penalty-rational inflection" — useful for sizing shorts/longs

**The asymmetric edge this unlocks:**
- **Pre-cliff short** (days before): pre-position before retail front-runs the cascade. Much better R:R than catching the cascade after −50%.
- **Post-cliff long**: supply pressure is GONE until next cliff. Cleanest pristine-bottom signal.
- **Token prioritization**: Cat A's with visible unlock cliffs ahead → prioritize. Cliff-less tokens → harder edge, deprioritize.

**Apply prospectively to current watchlist (2026-05-26 priority order):**
1. **BLUAI** — 62% in 3 matching-allocation vesting contracts, freshest deployment, smallest stake count = easiest to scan, highest asymmetric edge
2. **AGT** — 30% in 4 pristine mega-safes; if these are time-locked (not just dormant), unlock cliff is binary catalyst
3. **IN/TRUST (Base)** — 76% locked in 8 contracts, 49% in top-3
4. **BEAT** — 47% in 2 contracts; only 57 holders total = thin distribution after unlocks
5. **GENIUS BSC** — single 45% pristine wallet, would need to confirm if locked or just team-dormant

**Don't apply retroactively:** RIVER already played out ($30+ → $6). The methodology only has value forward-looking. Apply to tokens BEFORE their first major unlock cliff, not after.

**Open infrastructure follow-up (next session):**
1. Build `stake_schedule.py` MVP — works on one of {BLUAI, AGT, IN} as proof
2. Integrate output with `catalyst.py` — unlock cliffs as first-class catalyst events with calendar
3. Add "inflection_price" column to triage.py output for tokens with scanned schedules
4. Eventually: add price-at-stake-block lookup via CG historical or DEX aggregator (so we can show "$X million locked at $Y average price, currently underwater by Z%")

**The deeper lesson:** **the contract code is the operator's playbook in plain text.** Once a Cat A's staking contract is deployed, the team has committed to a specific supply pressure schedule that's visible months ahead. Reading the contract = reading the team's intent + timing. This is the most predictable signal in the framework. Use it.

Related: [[reference_lock_contract_audit_workflow]] (mechanism + identification), [[feedback_stage5_alerts_at_early_warning_not_structure_break]] (early-warning principle — unlock cliffs are the ultimate early warning), Section 5 "KOL Distribution Toolkit" (high-yield staking promos locking supply long), Section 9 Rule 6 (V3 LP stealth distribution — same intent, different mechanism)
