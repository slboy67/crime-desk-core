---
name: feedback_vesting_claim_tx_is_the_exit_signal_not_cliff_date
description: "The exit tell on vesting names is the CLAIM/MINT transaction executed AT the spike and forwarded to a CEX deposit within the hour — not the calendar cliff. LIGHT 08-28 (Sablier claim at the top → Gate → −23% in 8h), BTR 08-26 (CCIP burn/mint → Gate/Bithumb), ACE 09-01 (null-address mint → Binance, −23% after +9%). Watch the stream contract's claim events on board names."
metadata:
  type: feedback
---

# Vesting claim tx, not the cliff date, is the exit signal (2026-09-04)

**Rule:** on a name with a stream/vesting contract or a bridge mint pool, the sell signal is the
**claim (or mint) transaction itself** — executed at or within an hour of the local top and forwarded
to a CEX deposit address in the same hour. The calendar cliff is the *bound*; the claim tx is the
*timing*. Extends [[feedback_unlock_cliff_fade_n2_m_win]] and
[[feedback_river_lesson_methodology_not_token]] (which read the lock contract for the supply
CURVE): the curve says how much, the claim event says now.

**Instances (`reports/RESEARCH-2026-09-04-proxonchain-method.md` §3 rows 13/15/21; Binance 4h bars):**
- **LIGHT 08-28:** insider wallet claimed 1.05M from a Sablier vesting contract in the 12:00Z bar
  (the 0.2468 top), sent it straight to a Gate deposit. 0.205 by 16:00Z, 0.190 by 20:00Z (−23%),
  0.166 by 08-31.
- **BTR 08-26→31:** CCIP BurnMintTokenPool burn/mint by a lookalike-named wallet, routed to
  Gate/Bithumb. Held four days, then 0.162 → 0.091 (−44%) on 08-31 and 0.044 by 09-02.
- **ACE 09-01:** 2.0M minted from the null address in 6 min, routed to a Binance deposit, with
  funding at −0.993%/4h (squeeze running). 0.208 → 0.226 (+9%) then 0.173 (−23%) in 24h.

**Why:** a vested holder cannot sell before the stream releases, so the claim is the first moment
the supply is liquid — and they claim when price is worth claiming into. The deposit within the
hour turns "positioning" into near-execution ([[feedback_predictor_vs_cause_dex_sale_is_execution]]).
On-chain-clean at a fresh ATH stays NEUTRAL until this fires (§8 OTC blind spot).

**How to apply:**
- On any board name with a known stream/vesting contract, the tripwire is
  `Transfer(from=stream, to=EOA)` followed by an EOA → CEX deposit, not the unlock date.
  Date the cliff anyway ([[feedback_date_the_catalyst_before_making_it_load_bearing]]).
- Entry discipline unchanged: the claim fires the **veto/arm**, the trade is still the
  breakdown-hold (ACE went +9% first; BTR took four days).
- Open check before any spec: does `stake_schedule` already decode Sablier-style streams, and can
  `wallet_state` alert on the claim→deposit pair? If not, a low-priority keyless spec (ETH/Arb via
  Etherscan V2 free tier; BSC remains the quota problem). Not filed 2026-09-04.
- Hypothesis-tier n=3, all read after the fact. Update per resolution.
