---
name: feedback_deepneg_veto_is_swing_not_scalp
description: The §5 deep-neg short-side veto is a SWING rule (carry bleed + multi-day squeeze). It does NOT bind a sub-hour SCALP — on an ~11-min hold no funding settlement is crossed so carry ≈ 0, and fading a local pop is microstructure, not the distribution-top thesis. Scalp shorts of deep-neg pops can win; the desk currently surfaces swing setups, not scalps.
metadata:
  type: feedback
---

**Rule:** The CLAUDE.md §5 short-side veto ("never short deep-neg funding") is scoped to **swing/position** trades. Its two legs both assume a multi-interval hold: (1) carry bleed (−0.9%/4h ≈ −5.5%/day) and (2) deep-neg = crowded shorts → squeeze fuel over hours/days. On a **scalp** (sub-funding-interval hold), the carry leg evaporates and the trade is pure microstructure (fade the local pop, quick mean-reversion). Don't apply the swing veto to a scalp.

**Why:** User scalped EDENUSDT SHORT 2026-06-03: in 0.05326 → out 0.05100, held ~11 min = **+4.2%**, while EDEN funding was deep-neg (−0.39%/4h, the desk's "vetoed short"). The hold (21:39→21:50) crossed **no funding settlement** (Bybit 4h clock: nothing between 20:00–24:00) → **zero carry paid**. EDEN was +24% pumped; the scalp faded the local top with a fast exit. The veto's carry rationale literally did not apply.

**How to apply:**
- Veto = SWING gate. For a scalp, ignore the carry leg; the only deep-neg risk that survives is the **squeeze** — so a deep-neg scalp short demands a **tight stop + fast exit** (the squeeze can rip against you in minutes). User did exactly this (~11 min).
- **n=1** — one clean scalp is NOT a validated signature (base-rate gate: need n≥10, hit% >50). The *deterministic* part (no carry on a sub-settlement hold) is solid; the "you can reliably scalp deep-neg pops" part is an unproven hypothesis. Don't size on it yet.
- **Desk gap:** the engine (triage/analyse/classify/the veto) surfaces SWING setups and will keep saying "no trade / vetoed" on names that are perfectly scalpable. If scalp opportunities matter, that's a distinct capability the desk doesn't have. Flag, don't conflate. See [[feedback_amm_active_market_making_mechanism]], [[feedback_stay_strict_on_confluence]], [[feedback_blowoff_short_deepneg_veto]].
