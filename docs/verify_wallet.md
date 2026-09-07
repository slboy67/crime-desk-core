# verify_wallet — ad-hoc address intel (§8, SPEC 21)

## Purpose
One address against one token: funding source (flags seeded-by-tracked-safe), balance,
distribution activity, verdict (SEEDED-STAGING | DISTRIBUTING | INDEPENDENT-HOLDER |
DORMANT). The §8 tool for verifying CT "smart money" claims on-chain.

## Contract
```
verify_wallet '{"address":"0xabc…","token":"SKYAI"}'           # all deployed chains
verify_wallet '{"ticker":"SKYAI","wallet":"0xabc…"}'           # SPEC-89: desk-natural aliases (ticker→token, wallet→address)
verify_wallet '{"address":"0xabc…","token":"SKYAI","chain":"bsc","days":90}'
```
SPEC-89: the registry maps `ticker`→`token` and `wallet`→`address` before invoking, so the
desk's natural `{ticker,wallet}` no longer trips `argument --token: expected one argument`.
This is also the per-top-holder path `onchain` (SPEC-89) uses to verify whether a QUIET signal
is hiding a DISTRIBUTING top holder.
Out: `{address, token, verdict, chain, seeded_staging, funded_by:[{address, kind, label,
amount, label_conflict?, probe?}], label_conflicts, net_flow_window, net_flow_window_usd,
balance_now, holds_on, balance_chains_checked, out_count, …}`.

## Pool/contract-aware funding (SPEC-67) — a label never overrides chain facts
Before a tracked source counts as **seeding** (= operator-staging, §8), its address is
probed on-chain (`onchain.probe_contract`: `eth_getCode` + `token0()`/`token1()`/`fee()`).
A source that probes as a **DEX pool** (both tokens resolve) is `kind:"dex-pool"`, carries
`label_conflict:true` + a `probe` block, is **excluded from `seeded_staging`**, and a WARN
goes to stderr — its inbound clips are ordinary swap fills, not a seed. This killed the live
2026-06-12 false positive: the ESPORTS whale `0x4fec3d…2ceb` read as `operator-seeded`
because its top funder `0x5bb59bb…4462` carried the desk label "TWAP-HUB-6.8M" — but that
address is the PancakeSwap V3 WBNB/ESPORTS 0.01% pool. Probes cache per address in
`state/contract_probe_cache.json` (bytecode is immutable); only sources that *would* seed
(tracked + SEED_TIERS) are probed, so the RPC footprint is minimal. EOAs and non-pool
tracked safes seed exactly as before; an **unreadable** probe never fabricates a pool (the
tier still applies — degrade-explicit, §3). `label_conflicts` lists every refuted label.

**Onboarding lint** (`onchain.onboard_lint`, wired into `onboard_staging_sink`): an address
being added to `tracked_wallets` that probes as a contract requires an explicit
`contract_ok=True`; a pool is rejected outright unless acknowledged, and the probe result is
recorded in the entry's `_note` — a pool can never be silently tagged as a safe/hub again.

## Balance semantics (SPEC 58) — net_flow_window ≠ balance_now
Two DISTINCT numbers; do not conflate them (the Designer misread the old `token_balance`
twice — it was renamed for exactly this reason):
- **`net_flow_window`** (was `token_balance`) — IN minus OUT over the lookback window. NOT a
  balance: it prints `0` for an untouched holder and NEGATIVE for an emptied wallet.
  `net_flow_window_usd` values it off the LIVE perp price.
- **`balance_now`** — the REAL current holding: one cross-checked `balanceOf` (§3 second-source,
  two RPCs) → `{available, chain, value, pct_supply, total_supply, cross_checked, agree,
  sources, value_usd}`. `pct_supply` matches GoPlus's holder %. A read that can't be made
  (non-EVM / no RPC pool / all providers fail) returns `available:false` — never a fabricated 0.

## Chain selection (SPEC 58) — holding chain, not stray-transfer chain
The primary `chain` is where the address actually HOLDS the token (`balanceOf > 0`), not the
chain with the most transfer rows — the FOLKS miss read an ETH holder as `avalanche` off a
stray bridge transfer. `holds_on` lists the chains with a positive balance (largest first);
`balance_chains_checked` reports every chain probed. Falls back to the most-active chain only
when balanceOf is zero/unreadable everywhere.

## Settlement read (SPEC 53)
Outbound token clips are correlated with the wallet's QUOTE-leg transfers (USDT/WBNB on
BSC; USDT/USDC/WETH on ETH — one read per asset, quota-disciplined). Token-out clips
mirrored by quote-in clips (shared tx hashes, or ≥5 clips at ≥50% cadence inside the
out-window ±1d) → `distribution_mode:"dex_swap_sell"` +
`settlement:{asset, usd_in, n_settlements, counterparty, usd_out_after, hash_matched}` —
a drip-sell bot is a SELL, not "unknown redistribution" (the ESPORTS ~$530K miss).
Quote-in with NO token-out → `accumulating_quote:true` (surfaced, never a verdict).
Quota/provider failure → `settlement:{available:false}`, the token verdict is untouched.

## Gotchas
- §8: verify LIFETIME history, not current balance — a "pristine safe" can be a proven
  distributor on a remainder; nonce=0 refutes "accumulating for months".
- SPEC 21: single-chain reads false-DORMANT multi-chain operators — default is ALL
  deployed chains; only restrict with `chain` when you know why.
- Inbound from a team-safe = seeded staging, not independent accumulation.

## Rotation-aware freshness on the CLI read (SPEC-98)

`verify_wallet --token X` attaches a `distribution_freshness` block
(`{verdict: FRESH|FROZEN|ROTATED, line, note}`) via `attach_freshness` — the wallet's own
`last_out_ts` is the tracked leg; the tape / fresh-wallet→CEX rotation legs run inside
`rotation_freshness` (degrade-explicit, only when the clock is quiet). CLI-layer only:
`build_verify` stays lean because the SPEC-89 top-holder sweep calls it per holder. A
freshness failure never breaks the wallet verdict. See docs/onchain.md §SPEC-98.
