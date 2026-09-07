---
name: feedback_aster_oi_signals_prop_mm_absorption
description: "Aster perp OI spikes on a Cat A cascade signal arbitrageur flow vs Aster's prop market maker. Use Aster OI as an INDEPENDENT cross-venue confirmation signal alongside KuCoin spot/perp spread."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

**Mechanism (from @derrrrrrrq's read of ESPORTS 2026-05-26 cascade):**
During an engineered Cat A cascade, Aster perp OI on the token can spike from near-zero to hundreds of thousands of dollars in minutes. **This isn't whale activity — it's dozens of arbitrage wallets simultaneously routing shorts through Aster** because that's where the spread opened. The counterparty filling them is **Aster's own proprietary market maker desk**, which has to absorb the flow as cost-of-doing-business. Aster's "weak risk control" isn't a bug — it's how they attract flow (eating prop MM losses to capture deposit/withdrawal traffic).

**Why this matters for our framework:**
1. **Aster OI building rapidly from a low base = independent cross-venue confirmation** that a cascade is firing. Different signal from price/funding/OI on the primary venue — arbs only pile in when the apparatus is actually moving supply.
2. **This is the early-warning signal we missed on ESPORTS.** Structure-break at $0.66 fired at $0.05 — useless. Aster OI spike during the live cascade would have been visible 10-15 minutes earlier, before price leapfrogged the breakdown trigger. Same intent as [[feedback_stage5_alerts_at_early_warning_not_structure_break]], more specific instrument.
3. **Venue risk implication:** when Aster prop MM is eating big losses, expect them to *pull liquidity, widen spreads, or freeze deposits at exactly the wrong moment for a short* — TP fills get worse during the cascade you want.

**Validation 2026-05-26 (this check):**
| Token | Aster OI | Context |
|---|---|---|
| LAB | **$19.7M** | huge prop book after yesterday's 98%-range blow-off cooled — Aster prop MM likely stuck/long absorbing losses; ~30% of LAB's Binance OI |
| SKYAI | **$5.9M** | mid-cascade −16% — heavy arb piling in shorting via Aster |
| IN | $2.6M | meaningful for a Base-native token bouncing |
| GENIUS | $1.9M | rollover-mode -10.6% |
| BEAT | $876K | smaller but rising as cascade extends |
| EDEN/BSB/BILL/AGT | <$200K | quiet, post-cascade chop |

**How to apply:**
1. **Add Aster OI to triage / regime_check matrix** — pull `fapi.asterdex.com/fapi/v1/openInterest?symbol={sym}USDT` per token. Flag any token where Aster OI > 10% of Binance OI as "**Aster apparatus tracking**".
2. **Watch for OI spikes from a low base on a cascading token** — that's the cascade confirmation signal:
   - if a Cat A is dropping 5%+/hour AND Aster OI just spiked from <$50K to >$200K → cascade is real, in progress, arbs are piling in. Short with confidence in continuation.
3. **Don't trust your TP fills during high-Aster-OI cascades** — Aster prop MM is likely the counterparty and may pull liquidity. Size in by tranches; don't put a market order at the bottom expecting clean fills.
4. **The asymmetric short:** when both Aster OI is high AND funding is positive AND structure is breaking → the prop MM is hemorrhaging on a setup they can't defend. That's the maximum-conviction short.
5. **The trap:** when Aster OI is high but the cascade *stalls* and funding starts flipping negative → prop MM may defend the price actively to cap losses → squeeze risk for shorts.

**Open infrastructure follow-up:**
- Add Aster OI column to `triage.py` output
- Track historical Aster OI ratio (Aster/Binance) per token over time — anomalies = signal
- For tokens like LAB/SKYAI with persistent Aster OI > $5M, treat Aster as a primary venue, not a secondary

Related: [[feedback_arkham_xtoken_mm_is_bitget_hot]] (Bitget IS the apparatus too — different venue, same pattern), [[feedback_stage5_alerts_at_early_warning_not_structure_break]] (cross-venue spread early-warning principle), [[reference_arkham_entity_discovery_workflow]]
