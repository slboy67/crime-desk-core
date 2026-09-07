---
name: feedback_amm_active_market_making_mechanism
description: How the China/Asia "active market making" crime actually works on LAB/RAVE/Momentum — AMM controls ~all spot float, drives perp up via 1000s of KYC accounts, farms shorts via squeeze + funding, rugs the bid on their own timing. Never short the active pump; you are the fuel.
metadata:
  type: feedback
---

**Rule — the mechanism (user's authoritative account, treat as ground truth over re-derivation):**

The insane vertical candles on LAB / RAVE / Momentum etc. are **"active market making"** run by China/Asia-based AMM crews. The deal: the AMM fronts the capital to push the token to its highs, and in exchange for the project letting them "crime" it, they split the profit. Many claim RAVE/LAB-tier results; few can actually deliver. Projects (incl. scams) also approach the AMMs.

How it runs:
1. **Precondition #1 = absolute control of the spot float.** Insiders/team must control essentially the entire supply (RAVE ≈ 98%). Uncontrolled spot could be dumped into the perp-driven price and **bankrupt the AMM** = crime fails. This is *why* these tokens are **perp-listed (usually Binance perps) with little/no spot listing or liquidity** — it's intentional. AMMs even demand seats on the token multisigs so nothing can be dumped mid-crime.
2. **Execution = 1000s of KYC'd accounts in unison** driving the perp up. Not 1–2 accounts (those get frozen instantly). Explains huge, one-sided OI/volume with no single-entity flag.
3. **The trap:** with no spot available to sell, the *only* way to bet the price down is to **short the perp**. The AMM squeezes every short — each has a breaking point (tolerance or liquidation). Every stop-out / liq = AMM profit.
4. **Funding is a weapon, not a signal.** The perp/underlying dislocation throws off extreme funding — LAB was **−1%/hour, shorts pay longs (~8000%/yr)**. That bleeds short-holders out of their position AND pays the AMM's longs. **Deep-negative funding here = crowded shorts being farmed, NOT trapped shorts about to gift you a squeeze-long.**
5. **The rug is operator-timed.** The AMM is the only bid. When they decide the crime is complete they **pull the bid** → collapse candle to ~0. They control the timing and manner, and can join the short on the way down. **The top = the bid pulling, not a calendar date or a technical level.**

n=1 datapoint: **BriskCapital** shorted LAB large and publicly, lost 7 figures — straight to the AMM. They will not let a large public short win. His mistake was shorting at all; that is exactly what the AMM wants.

**Why:** This is the first-principles "why" behind the short-side veto and the operator-aligned long. The negative funding, the perp-only listing, the squeeze, and the rug are not separate facts — they are one engineered machine whose entire purpose is to harvest shorts and exit on the team's timing.

**How to apply:**
- **HARD VETO: never short an active-phase AMM pump** (deep-neg funding + supply-controlled + perp-only/no-spot). You are the fuel and the exit liquidity. Reinforces CLAUDE.md §5.
- Do **not** read deep-neg funding on these as "trapped shorts → long squeeze loading." On an active AMM pump it's the AMM *farming* shorts. A LONG is only justified operator-aligned at trap-formation, and **must exit before the bid-pull** — which is operator-timed (watch staged-wallet nonces / the bid), never a technical or calendar trigger. See [[feedback_why_funding_negative]], [[feedback_squeeze_fuel_is_a_long_signal]].
- The **only** safe bearish expression is the *post-crime / distribution* phase, after funding flips **positive** (longs now trapped paying carry) — that is the ESPORTS-style distribution-top short, a structurally different phase from the active pump. Don't conflate the two.
- **Recognition checklist for a loaded apparatus:** Binance-perp listed + ~no spot float/liquidity + supply concentrated in team multisigs (+ AMM seats on multisigs) + one-sided multi-account perp drive. See [[feedback_dormant_safes_not_bullish_otc_blindspot]], [[feedback_zach_says_no_short]].

**Relation to the OTC/VC vesting-hedge pattern (RESOLVED — two distinct sub-patterns, per user):** Deep-neg funding at a fresh Cat A ATH is one of two things, and they are NOT the same machine:
- **A (this memo) — active AMM pump:** AMM is **net long**, drives the perp up via 1000s of accounts, *farms* the shorts; **no on-chain lock**, supply in plain multisigs, rug is operator-discretionary.
- **B — OTC/VC vesting-hedge:** desks **short** the perp delta-neutral against cheap *locked* spot; there **is an on-chain lock contract + unlock cliff**, on-chain pristine.

The tell between them is the **on-chain lock contract** (B has one; A doesn't). Both produce deep-neg funding and both mean *don't short / long is operator-aligned / top = the bid pulling*. Codified in CLAUDE.md §4. See [[feedback_dormant_safes_not_bullish_otc_blindspot]] for B's OTC blind-spot.
