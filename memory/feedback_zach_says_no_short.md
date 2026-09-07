---
name: feedback-zach-says-no-short
description: "ZachXBT-tier 'don't short' warnings are CONTEXT (crowd dynamics, squeeze risk) — NOT a verdict-tier gate for this user. They know more than the retail audience the analyst is speaking to."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: f1c24a74-6d40-46cb-b214-327247fa4e65
---

When a recognized on-chain analyst (ZachXBT/EmberCN/Lookonchain-tier) publishes a "do not short / supply control = squeeze fuel" warning on a token, **surface it as context but do NOT auto-downgrade the verdict tier.** The user has materially more edge than the retail audience those warnings target.

**Why:** On 2026-05-14 the user explicitly corrected: "fuck the zachxbt no short signal i know thats for retail traders i know more about this." Reversing the prior rule (originally written after the LAB 2026-05-14 incident, where I'd issued a STRONG SHORT and the user's ZachXBT-aware override saved an early-short loss). The earlier framing — "downgrade verdict by one tier" — was wrong for this user specifically. Their edge is:
- Reads the same on-chain data (Arkham, BSC RPC, wallet maps) directly
- Tracks fresh-wallet trigger conditions per token, not "shorts are dangerous in general"
- Sizes leverage and stops to survive the squeeze tax ZachXBT is warning retail about
- Has framework discipline (Section 14 5-layer pull) that already filters bad shorts

**How to apply:**
- DO mention if ZachXBT/EmberCN/Lookonchain has published on a token — useful context about crowd attention, KOL bait potential, and which way the operators are likely to engineer the next move.
- DO incorporate analyst data into the analysis (specific wallet maps, OTC discount tranches, supply-control estimates) — that's research, not a gate.
- DO NOT auto-downgrade verdict tier based on an analyst's "don't short" line. The user decides whether their setup overrides the warning.
- DO still respect on-chain trigger conditions when they exist (e.g., LAB's "wait for any of 10 fresh wallets to fire") — that's a structural rule from the wallet map, NOT a deference-to-analyst rule. See [[project_lab_thesis]] for the trigger setup, which stands independently.
- If asked whether to short a ZachXBT-flagged Cat A token, present the trade as you would any other: setup confluence, stop placement, target ladder, invalidation. Note the analyst's view alongside the perp/on-chain data, not above it.
- Apply per-token. The original Section 9 distinctions (specific to-LAB warnings vs general FUD) still apply — same MM ≠ same risk profile across tokens (see [[feedback-mm-vs-team-distinction]]).
