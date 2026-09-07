---
name: feedback_cat_a_verify_onchain_trigger_not_triage_notrade
description: STANDARD — on any Cat A name, never accept triage/analyse "no trade"/WATCH/"supply locked" at face value. Verify the on-chain distribution trigger (the named loaded apparatus wallets) directly with verify_wallet. Triage is perp-weighted and under-calls Cat A; the real trigger is on-chain.
metadata:
  type: feedback
---

**Rule (standard, user-mandated):** `triage` and `analyse` are perp-weighted and **under-call Cat A names** — they return "no trade" / WATCH / "supply locked = constructive base" while the real situation is different, because the actual setup trigger lives on-chain, not in funding/OI. On ANY Cat A name, before accepting a "no trade" / WATCH read, **verify the on-chain distribution trigger directly**: `verify_wallet` the named loaded apparatus wallet(s) (the DOMINO / staged / mega-safe in the thesis triggers), plus `distribution_radar` and the `onchain` nonce/concentration. Report what is loaded + dormant vs twitching, not just "no setup."

**Why:** PIEVERSE — triage said `short/live` chip but `analyse` said "supply locked + perp neutral = constructive base, no setup," committed state WATCH/spectate. Taken at face value = "no trade." The direct on-chain check told a far richer story: DOMINO $20.6M (`0x01b97cea`) **DORMANT, clean read** (trigger genuinely not fired) BUT top10 = 88.5% (fully loaded apparatus) and the **#1 holder (22.5%, `0xf89d7b9c`) had STARTED distributing** (~$33K, non-CEX) — the first twitch of Stage 2, which the perp board missed entirely. Outcome flips from "no trade" to "loaded gun, finger moving — watch these two named wallets." User: "triage comes up with 'no trade' too often while the real situation is different."

**How to apply:**
- Cat A + "no trade"/WATCH/"locked" from triage/analyse → NOT a conclusion, a prompt to check on-chain. The "locked" flag especially: verify the named apparatus wallet is actually dormant (clean `verify_wallet`, `degraded:False`), don't trust the flag.
- **Degraded radar ≠ quiet** (n_degraded high → getLogs missed wallets). Always hit the *specific* thesis-named apparatus wallet directly; the radar may never have read it. See [[feedback_flows_bsc_rpc_unreliable_for_getlogs]].
- Surface the trigger state concretely: which wallet is loaded, dormant vs moving, toward CEX or not — that's the early-warning the perp layer can't see. See [[project_onchain_is_the_spine_perp_came_second]], [[feedback_dont_drift_to_perp_only_scans]], [[feedback_dormant_safes_not_bullish_otc_blindspot]], [[feedback_live_top_signal_is_nonce_not_getlogs_audit]].
