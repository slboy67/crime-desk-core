# accumulation_radar — operator accumulation radar (SPEC-76, the LONG-side mirror)

## Purpose
The on-chain radars (`distribution_radar`, `onchain_radar`) are **token-centric** and
**distribution-biased** — watch a token's wallets dump → short. This is the **inverse** and
**wallet-centric**: watch the mapped **operator cluster** wallets (team/op/distribution +
cross-Cat-A escrows, e.g. `0x73d8` which holds positions across ~12 tokens) **accumulate a NEW
untracked token** → the §1 sub-edge-1 / §0.6 operator-aligned **LONG** at trap-formation, before
the markup. The highest-value entry.

## Contract
```
accumulation_radar '{}'                         # full operator-cluster sweep (budgeted)
accumulation_radar '{"wallet":"0x73d8…"}'       # one operator wallet
```
Out: `{scanned_wallets, n_wallets, partial, candidates:[{wallet, wallet_label, is_escrow, token,
token_contract, chain, size, pct_supply, mode, change, perp:{has_perp,funding_4h,deep_neg,oi,
oi_chg_pct,oi_building,firing}, tier, first_seen}], imminent, early, n_candidates, caveats}`.

## The pipeline (spot accumulation THEN perp markup — multi-market OI construction)
1. **Enumerate** the cluster wallets' ERC20 **inbounds** (snapshot), diff vs a stored baseline
   (`state/accum_baseline_<addr>.json`, the SPEC 55 pattern) → **NEW** / **>50%-GROWN** positions.
2. **Untracked-only** — keep tokens NOT already on the desk map (a tracked name has its own pass).
3. **Active vs parked** (`classify_accumulation`, reusing `verify_wallet` funded_by/source):
   - **active** — DEX swap / CEX withdrawal / open-market buy = a tradeable load.
   - **parked** — inbound from a vesting/team/escrow safe (`seeded_staging`) = allocation parked,
     NOT a pre-pump signal.
   - **unknown** — source not disambiguated (degrade-explicit; never silently "active").
4. **Perp cross** (`perp_state` — the timing key): **firing** = a live perp + **deep-neg
   trap-formation funding** (§4, `≤ DEEP_NEG`) + **OI building** (`≥20%` vs its baseline when
   known, else OI present). The confluence tier:
   - **IMMINENT** — active load **+ perp firing** → the operator revealed timing → tradeable.
   - **EARLY** — accumulation present but perp dormant / flat / absent → arm a watch, don't enter.
   - **PARK** — parked allocation + no perp leg → discard.

The on-chain accumulation is the **spot-side decompose** the §4 long gate is missing: known
operator accumulation resolves deep-neg funding = real trapped-shorts vs MM-hedge.

## Backtest gate (the go/no-go — §9)
`backtest_lead_time(tokens, accum_events, pump_events)` is a pure scorer: per cluster token it
computes whether the operator wallets accumulated **before** the pump and the lead-time, and a
summary `verdict` (`predictive` if >half the pumps had a tradeable accumulation lead, else
`not-predictive`). **The live data-gather (historical Moralis inbounds + pump-start dates) is the
orchestrator's go/no-go run** — the engine ships *behind* this gate; validate before sizing on it.

## Gotchas / caveats (surfaced in `caveats`, never hidden)
- **accumulation ≠ imminent** — operator timing is discretionary; only IMMINENT is tradeable.
- **long-alongside-operator still risks being exit liquidity** if the stage is misread (§0.6).
- **PARK / EARLY are NOT entries.**
- **Quota:** snapshot+diff on a cadence (not full re-scan); capped at 40 wallets/run; the free
  Moralis daily quota is the binding constraint
  (memory: `reference_moralis_free_daily_quota_gates_onchain`).
- Depends on the cluster map being accurate — pairs with re-tiering `0x73d8` (currently mislabeled
  GOPLUS-TOP on EVAA/CLO/SLX).
