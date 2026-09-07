---
name: why-funding-negative
description: "Negative funding ≠ short squeeze, especially on alts. Ask WHY funding is negative (4 reasons); default alt reason is MM-hedge distribution (the trap), not trapped shorts. Require confluence before a negative-funding long."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: e94ca703-b256-430c-897b-c7124651d8f5
---

From a funding-rate article the user had me integrate (2026-05-22). Now in CLAUDE.md Section 2 ("Why is funding negative? — the 4 reasons"). This is partly CORRECTIVE to my reflexive "deep-negative funding = long squeeze-fuel" bias (see [[squeeze-fuel-is-a-long-signal]]).

**Core:** funding doesn't give direction — it tells you WHO is driving the move. The key question is not "is funding negative?" but **"WHY is it negative?"** Four reasons, only ONE is squeeze-fuel:
1. Aggressive shorts (the squeeze case — only ¼ on alts).
2. Liquidation cascade (old longs destroyed, OI DROPPED — opposite of a squeeze).
3. Absorption wall (passive seller eats every bid; perp stays at discount, no squeeze).
4. **MM hedge / OTC distribution — THE ALT TRAP:** MM got cheap tokens, sells spot, hedges by shorting perp → persistent negative funding that's a hedge byproduct, not retail positioning. Persists far longer than retail expects in thin markets. CT screams "squeeze," retail longs, selling never stops, retail = exit liquidity. "Win the funding, lose the trade." This is the perp-side view of the Section 9 Rule 7 OTC trap.

**Negative funding is a long ONLY with the full confluence gate:** genuine multi-sigma extreme (not "slightly negative") + **OI SPIKING** (loading, not hedging — flat/falling OI voids it) + price-action trigger (sweep/reclaim/SFP) + **spot CVD diverging UP from perp CVD** (real money vs leverage panic — often more reliable than funding itself).

**OI is the discriminator:** funding-extreme + OI rising + price flat = a player LOADING (bullish). Funding-extreme + OI flat/falling or price grinding down = hedge/cascade = the trap.

**BTC vs alts:** funding extremes are genuine sentiment on BTC (deep, unmanipulable); on alts they're usually market-structure/hedging flow. Never apply BTC funding logic to alts. Our universe is alts → default-skeptical on negative funding.

**Tools (built 2026-05-22):** `intraday.py` now computes real aggressor CVD (Binance aggTrades buyer-maker flag, replaced the candle proxy). `cvd_spot_perp.py` = spot-vs-perp CVD divergence, the article's "more reliable than funding" long-confirm. ALT 2026-05-22 test: both spot+perp CVD were selling = aligned bearish = no real spot support → the negative-funding long would've been the hedge trap; the divergence tool flagged it.

Related: [[squeeze-fuel-is-a-long-signal]] (reconcile: lead with the long idea but prove reason #1 not #4 before sizing), [[feedback-verify-wallet-history-not-just-balance]], [[5-layer-pull-tooling-in-scripts]].
