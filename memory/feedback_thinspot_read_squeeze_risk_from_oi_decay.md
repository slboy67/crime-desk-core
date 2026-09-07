---
name: feedback_thinspot_read_squeeze_risk_from_oi_decay
description: On perp-only crime coins the spot-vs-perp dislocation / spot-CVD read is unavailable (analyse returns cvd_verdict=UNRELIABLE_THIN_SPOT) — and that absence IS the signal. Read remaining squeeze risk from OI decay + funding sign, not spot basis.
metadata:
  type: feedback
---

**Rule:** On perp-only crime coins you cannot compute the "is spot leading down while perp is held at a premium" read — `analyse` returns **`cvd_verdict = UNRELIABLE_THIN_SPOT`** because there is no meaningful spot tape. Don't treat the missing read as inconclusive; the *absence* is the answer: price discovery is entirely on the perp, there is **no spot anchor** pulling the perp back, so the perp is a closed leverage game. Read remaining squeeze risk from **OI decay + funding sign**, not from spot basis/CVD.

How to answer "do I survive the stop" without spot CVD:
- **OI falling hard** (covering) = squeeze fuel being *consumed*; the shorts that already bought back can't squeeze you again → squeeze risk **falling**.
- **Funding still on your side / not deep-neg** = no crowded-short *reload* (deep-neg is the configuration that re-arms a squeeze) → no fresh fuel.
- **OI building + funding flipping deep-neg** = the opposite; squeeze risk *rising*, the bounce has fuel.
- Caveat: a thin perp book can still *wick* a stop mechanically even with no fuel for a sustained leg — that's placement/size risk, not squeeze risk.

**Why:** ESPORTS short, near-stopped on a sweep. User asked for spot-perp dislocation to size the remaining squeeze risk. `analyse` returned `cvd_verdict=UNRELIABLE_THIN_SPOT`, `price 0.0493`, `funding_4h +0.070%`, `oi_chg_pct −34%`. The dislocation read was unavailable, but OI −34% (covering exhausting) + positive funding (no deep-neg reload) said squeeze risk was LOW and falling → survive the stop, hold. Also: `analyse` tagged Tier-2 "no on-chain distribution confirmation" — that was the **degraded getLogs radar**, not reality; direct `verify_wallet` had all 3 wallets DISTRIBUTING. Don't trust the engine's on-chain leg when the radar is degraded — confirm wallets directly.

**How to apply:** When spot CVD/basis is the read you want on a thin-spot name and the engine flags UNRELIABLE_THIN_SPOT, pivot to OI-decay + funding-sign for squeeze risk. And cross-check any "no on-chain confirmation" verdict against direct wallet reads when the radar reports degraded/getLogs errors. See [[feedback_stop_beyond_the_sweep_not_on_it]], [[feedback_amm_active_market_making_mechanism]], [[feedback_weight_structure_layer_vs_onchain_print]], [[feedback_flows_bsc_rpc_unreliable_for_getlogs]].
