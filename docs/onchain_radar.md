# onchain_radar — per-token holder-flow radar (SPEC 16/17/29)

## Purpose
Full onchain-radar feed replacement per Cat A token: distributors + accumulators +
cost-basis-tagged independent sellers + tracked-whale Hyperliquid positions near
liquidation. Perp-fused, change-detected.

## Contract
```
onchain_radar '{"token":"LAB"}'                 # one ticker
onchain_radar '{}'                              # sweep all Cat A names (budgeted)
```
Out: `{scanned, alerts_total, tokens:{TICKER:{available, perp, distributors,
accumulators, independent_sellers, alerts}}, perp_liqs:{available, positions:[…]}}`.

## Gotchas
- SPEC 29: long sweeps emit PARTIAL results + progress (state/radar_progress_*.json) —
  an interrupted sweep returns what it has, never nothing.
- SPEC 17: heavy verify reads are nonce-gated (unchanged nonce → cached verdict, no
  Moralis spend); Moralis free quota is a DAILY gate — don't hammer
  (memory: reference_moralis_free_daily_quota_gates_onchain).
- Discovery seeds come ONLY from SEED_TIERS wallets — `unclassified` onboard candidates
  (SPEC 47) never seed until the Designer tiers them.
