---
name: feedback_index_composition_and_reduce_only_risk
description: "Binance Futures mark-price index composition reveals the manipulation surface — when a single DEX pool drives >30% of the mark (ESPORTS=52.63% PancakeSwap V3), the cascade can be triggered by a small DEX swap. Combined with per-account reduce-only triggers ($200K notional, 5% OI, 25% liq-gap), these become code-level risks that need pre-trade detection."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

**User intel @derrrrrrrq 2026-05-26 — two compound risk mechanisms:**

### Mechanism 1: Mark-price index composition = manipulation surface

Binance Futures mark price is a weighted index of multiple venue prices. The weights are PUBLIC via `https://fapi.binance.com/fapi/v1/constituents?symbol={SYM}USDT`. Returns:
```json
{ "constituents": [ {"exchange": "pancakeswapV3", "symbol": "...", "weight": "0.52631579"}, ... ] }
```
**`weight` is a 0-1 FRACTION**, not a percentage. Multiply by 100 to display.

**Why this matters:** when a single DEX pool (PancakeSwap V3 / V2 / Uniswap) is >30% of the index, a relatively small swap on that DEX can drag the futures mark price down across ALL CEX perps simultaneously, triggering correlated liquidation cascades on Bitget/MXC/Gate/Aster/Binance/etc. **That's how engineered cascades go from −30% to −90% in hours** — the DEX is the lever, not just the destination.

**Validation 2026-05-26 — Cat A watchlist scan:**

| Token | Top component | Risk |
|---|---|---|
| **ESPORTS** | PancakeSwap V3 / WBNB-ESPORTS = **52.63%** | 🚨 validated by cascade |
| **SKYAI** | PancakeSwap V2 / SKYAI-WBNB = **40.00%** | 🚨 LIVE Cat A, same architecture |
| EDEN | binance / EDENUSDT = 65.79% | ⚠ single-venue, not DEX |
| HMSTR | binance / HMSTRUSDT = 59.70% | ⚠ single-venue |
| PROVE | binance / PROVEUSDT = 48.19% (DEX 7.23%) | ⚠ single-venue |
| BILL | binance_alpha / BILLUSDT = 44.44% | ⚠ single-venue |

SKYAI is the pre-loaded cascade vehicle in the current watchlist — same DEX-weight architecture as ESPORTS pre-cascade.

### Mechanism 2: Per-account reduce-only triggers (Binance UI screenshot)

A position automatically becomes `reduce-only` (cannot open new positions, can only reduce existing) if ANY of these hit:
- Position notional ≥ tier threshold (e.g., **$200K for ESPORTSUSDT**)
- Position ≥ **5% of total contract OI**
- **Liquidation-price gap ≤ 25%** from mark price

The "5% of OI" rule is particularly nasty on thin-OI cascade tokens: OI flushes fast during a cascade, so a position that was 3% of OI at entry can become >5% as OI drains, kicking the account into reduce-only mode WITHOUT the trader doing anything. Combined with the 25% liq-gap rule, a winning short can lose its ability to add or hedge at exactly the worst moment.

**These thresholds appear to be on the Binance UI but NOT in their public API** (account-level risk-control). Per-symbol notional tier (~$200K for ESPORTS) is in `fapi.binance.com/fapi/v1/leverageBracket?symbol=X` — the `notionalCap` field per bracket — but the 5% OI and 25% liq-gap rules are formula-only (apply universally to all symbols).

### Framework integration — pre-trade discipline

**Standard check when drilling any Cat A token:**
1. **Pull index composition** via `/fapi/v1/constituents?symbol={SYM}USDT`. Compute total DEX-tagged weight (`pancakeswap`, `uniswap`, `sushi`, `curve`, `balancer`, `camelot`, `raydium`, `aerodrome`, `trader_joe`). If >30% = **🚨 DEX-MANIPULABLE flag**. If single venue >40% = ⚠ concentration flag.
2. **Pull notional tier** via `/fapi/v1/leverageBracket?symbol={SYM}USDT`. Note the `notionalCap` at bracket 1 — that's the typical reduce-only notional threshold.
3. **Pre-trade position-sizing rule**: target position notional ≤ 70% of bracket-1 notional AND ≤ 3% of current OI (gives 40% safety margin before reduce-only auto-triggers).
4. **In-trade monitoring**: as cascade plays out, OI shrinks → your position becomes a higher % of OI. Set alerts for reduce-only proximity (position-OI ratio crossing 4%).

### The compound apparatus picture for ESPORTS-class cascades

Putting today's three intel layers together — the cascade mechanism is now fully readable:

| Layer | Signal | How to detect pre-trade |
|---|---|---|
| Operator apparatus | Bitget+KuCoin hot wallets ARE the MM ([[feedback_arkham_xtoken_mm_is_bitget_hot]]) | Arkham address lookup on suspected MM wallets |
| Velocity multiplier | Aster prop MM absorbs cascade arb flow ([[feedback_aster_oi_signals_prop_mm_absorption]]) | `fapi.asterdex.com/fapi/v1/openInterest` |
| **Cascade lever** | **DEX-pool dominance of mark price index** (this memo) | **`/fapi/v1/constituents`** |
| Position trap | Reduce-only auto-triggers during cascade | leverageBracket + OI ratio math |

A Cat A with ALL FOUR signatures loaded = a pre-built cascade machine. **SKYAI has 3 of 4 confirmed** (Bitget-pipe + Aster $5.9M OI + 40% PancakeSwap V2 weight). If a structure break fires there, the cascade mechanics are pre-loaded.

### Open infrastructure follow-up

Build `venue_risk.py`:
- INPUT: token symbol or contract address
- PULL: `/fapi/v1/constituents` + `/fapi/v1/leverageBracket` + `fapi.asterdex.com/fapi/v1/openInterest` + cross-ref Arkham address lookup on known MM/team wallets
- OUTPUT: 4-row risk matrix (apparatus, velocity, lever, position-trap) with red/yellow/green flags per layer
- Integrate into triage.py — surface "🚨 DEX-MANIPULABLE" flag for any watchlist token

Related: [[feedback_arkham_xtoken_mm_is_bitget_hot]], [[feedback_aster_oi_signals_prop_mm_absorption]], [[feedback_stage5_alerts_at_early_warning_not_structure_break]] (each one of these is a layer; index composition is the missing one)
