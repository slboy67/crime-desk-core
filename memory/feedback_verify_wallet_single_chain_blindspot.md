---
name: feedback_verify_wallet_single_chain_blindspot
description: verify_wallet/radar check ONE chain per token; multi-chain operators distribute across ETH/BSC/Base, so the desk reads active distributor wallets as false DORMANT/0-out. Trust the user's multi-chain explorer read over a single-chain DORMANT.
metadata:
  type: feedback
---

**Rule:** `verify_wallet` / `distribution_radar` / `onchain_radar` resolve a token on ONE chain (its default config chain). Crime operators distribute **multi-chain** (ESPORTS dumps on BSC, EDEN's apparatus is ETH, CEX-deposit legs route through secondary wallets on whichever chain). So a wallet actively distributing on a non-default chain reads **`available:true, verdict:DORMANT, out:0`** — a FALSE quiet. Do not treat a single-chain DORMANT as "not distributing"; verify the chain, or trust a credible multi-chain manual read (Arkham/explorer) over the engine.

**Why:** 2026-06-03 the user reported live distribution today on six wallets; the desk read ALL of them DORMANT/0-out:
- ESPORTS CEX-deposit secondary wallets `0xc2F8C63d…`, `0x2a500f79…` (the escalation leg on the live short).
- EDEN Gate-deposit team wallet `0xad11e97f5044db890a75a7d3e51eaa7099d7e7ff` (~$500K deposited at a local high, ~$3M still loaded).
- PLAY DEX dumpers `0x24d0315b…`, `0x68a3067a…` (~$200K out, minimal price impact).
This also explains an earlier miss: I'd called EDEN's tracked distributors "dormant" — the REAL distributor (`0xad11e9`) was on the chain/wallet the desk wasn't reading. SPEC 21 filed to make verify_wallet multi-chain + onboard these wallets.

**How to apply:**
- A DORMANT/0-out from `verify_wallet` on a multi-chain token is NOT proof of no distribution — it's "no distribution on the chain I checked." Caveat it.
- When the user gives multi-chain on-chain intel, treat it as authoritative over the single-chain engine read (same spirit as [[feedback_liqmagnets_is_volume_profile_not_liq_heatmap]] — the user's layer beats the proxy). Fold their wallets into the watch.
- For a live short, the CEX-deposit escalation may be invisible to the desk — rely on the user's read + check the right chain. See [[feedback_cat_a_verify_onchain_trigger_not_triage_notrade]], [[feedback_cex_deposit_is_positioning_not_execution]], [[feedback_flows_bsc_rpc_unreliable_for_getlogs]].
