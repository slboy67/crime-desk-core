---
name: feedback_stop_beyond_the_sweep_not_on_it
description: On thin-index crime coins, a stop placed AT the obvious lower-high/round liquidity pocket is donated to the MM sweep. Place it beyond the pocket. To tell a near-stop sweep from a real reversal, check magnet structure.
metadata:
  type: feedback
---

**Rule:** On a thin-index crime coin, the obvious lower-high / round-number / prior-swing level is where short-stop liquidity pools — and that pool is exactly what the MM sweeps. A stop resting *on* that level is donating itself to the hunt. Place the stop *beyond* the pocket the heatmap shows liquidity pooling at, not on it.

**Distinguishing a near-stop liquidity sweep from a genuine reversal** (when price grinds into your stop):
- **Sweep (hold):** no upside magnet above (nothing structural was pulling price up — it went up only to take stops) + a strong downside HVN/magnet below + OI *falling* (covering, finite) + funding still on the thesis side + sellers still distributing on-chain. Price reverses off the swept level back toward the downside magnet.
- **Real reversal (cut):** price climbing toward a real upside magnet, OI *building*, funding flipping against you, sellers gone quiet / a bid being defended.

**Why:** ESPORTS short, ~$0.0492 entry, stop $0.0528 sat right at the prior lower-high. Price swept to ~$0.0508 and nearly stopped it, then reversed to $0.0497. `liq_magnets` showed **no upside magnets + a dominant 1.25B-vol downside HVN at $0.0434** (≈ TP1 $0.044); OI was −17% (covering), funding still +0.075% (positive, thesis-side), all three distributor wallets still DISTRIBUTING. Every layer said *liquidity grab, not reversal* — confirming the user's heatmap read. The stop held, but it was placed in the sweep zone; a wick could have taken it on the thin-index name the thesis itself flagged as wick-prone.

**How to apply:** When near-stopped, run `liq_magnets` and check on-chain/OI/funding before reacting. Upside magnet absent + downside magnet present + OI falling + supply still distributing = sweep, hold to the committed plan (do NOT widen the stop mid-trade — that's a separate sin; this is about *initial* placement). When *setting* the stop, look at where liquidity pools (heatmap is the user's layer the engine can't see) and place beyond it. See [[feedback_tp_inside_round_magnet]] (the TP analogue), [[feedback_weight_structure_layer_vs_onchain_print]], and [[feedback_amm_active_market_making_mechanism]].
