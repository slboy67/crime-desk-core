---
name: feedback_hidden_build_cvd_oi_price_divergence
description: "Hidden operator position-build tell (@derrrrrrrq): CVD↑ + OI↑ + price FLAT = dealer quietly building the opposite side while old positions still on — exit the aligned trade. CVD alone is a smoke bomb; the read is WHOSE taker flow it is. When OI is faked, raw candles + liquidation prints are the only ground truth."
metadata:
  type: feedback
---

**Rule:** Three divergence tells, in priority order: (1) **CVD↑ + OI↑ + price FLAT** while the main force's old longs are still on = the operator is secretly building SHORTS into the passive bid — he force-closed a floating-profit long on exactly this read before the dump (2051200144952156614; a CVD↑+OI↑+price↑ variant of dealer short-building at 2047134456973234634). (2) **CVD↑ + price flat** alone = a thick limit-sell wall is absorbing the takers (2053609317333622901). (3) **OI↑ + price↓ + CVD↓ with NO matching liquidation prints** = smoke-bomb short OI piled by the dealer as bait (BSB, 2057931135511114127) — and the general escape hatch: "when OI is faked you can't read OI; raw candles + liquidation data are the only truth" (2057932606482874821), because liq prints cannot be faked while OI can (double-open, multi-account transfer — [[feedback_aggregate_oi_faked_via_double_open]]).

**Why:** The desk reads CVD as spot-vs-perp divergence (§6 trap-formation gate) and OI as fuel, but has no tell for the operator building AGAINST an open desk position during a flat tape — that's how winners turn into bagholders between triggers without any §0.5 BREAK firing. The liquidation-prints-as-ground-truth rule also upgrades the §3 "single-source read gets a second source" discipline: for OI the second source isn't another venue's OI (fakeable the same way), it's the liq stream + the raw tape.

**How to apply:**
- Any open position: alert on CVD↑+OI↑+price-flat forming against the position's direction — treat as an early-warning (bank profit / tighten), even though it's not a named-invalidation BREAK.
- Never take an OI verdict (fuel, capitulation, wash) at face value without checking the liquidation stream for confirming prints; OI moves with liq-silence = suspect fake/internal transfer.
- CVD verdicts must name the taker side and the likely taker identity (liq/stop cascade vs breakout chasers vs operator program) — direction alone is bait.
- Candidate coder tickets: (a) `liqs` capability — per-venue forced-liquidation stream as first-class data next to OI; (b) divergence detector for the three tells feeding `tape`/`classify`.
