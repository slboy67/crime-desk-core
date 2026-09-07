# pull5 — unified multi-layer pull (§14)

## Purpose
Coingecko id + perp regime + liq magnets + price structure (+ opt-in on-chain) in one
object — the discovery-read bundle for an UNTRACKED name.

## Contract
```
pull5 '{"ticker":"NEW"}'
pull5 '{"ticker":"NEW","cg_id":"new-token","onchain":"1"}'
```
Out: `{ticker, layer0_coingecko, layer2_regime, layer3_magnets, layer4_structure,
layer1_onchain}`.

## Gotchas
- SPEC 36: ticker→coingecko-id resolution is collision-safe (rank by market_cap_rank +
  live-perp price sanity) but `low_confidence:true` means eyeball `resolved_id` before
  trusting contracts (the dead-2021-"siren" trap).
- The on-chain layer is opt-in because it's the slow path; everything else is fast.
- `coingecko_layer()` is the resolver other capabilities import (onboard, onchain
  SPEC-34 fallback).
