---
name: feedback_blowoff_short_deepneg_veto
description: Engine bug fixed — blowoff-short gate now enforces the deep-neg-funding veto (was early-returning a STRONG SHORT on LAB-type squeeze-fuel)
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 8e11afa5-c50d-49e8-984f-fa56c43b8784
---

The `analyse.py` blowoff-top short gate was an **early return** that fired on the `blowoff_short` structure flag (ATH wick + lower-high + clean breakdown) BEFORE the Section-2 deep-neg-funding short veto downstream. Result: on LAB 2026-06-02 ($20, +43%/24h, funding −0.72%/1h ≈ −17%/day, perp −3.1% to index) the engine emitted **`SHORT (blowoff-top) [STRONG]`** while its own perp layer said +46 LONG / "trap-formation squeeze-fuel" — the exact framework error made on LAB twice before, this time produced by the engine itself.

**Why:** the blowoff structure signature can print while funding is still deeply negative. On a deep-neg Cat A that "breakdown" is a bilateral bait-dip into a re-squeeze (OTC-hedgers don't cover → funding reloads), not a clean cascade. Shorting it pays massive carry into short-liq clusters stacked above.

**How to apply:** the engine now returns `WATCH (blowoff vs deep-neg funding)` when `blowoff_short` coincides with `fr_4h ≤ −0.30%/4h`; a real blowoff short still fires when funding has COOLED toward flat (unit-tested both paths). Codifies CLAUDE.md §2 short-side veto + §6 funding-reload + Rule 7b onto the blowoff path. General lesson: the human-judgment override (deep-neg → don't short) was correct and is exactly the Section 0 division of labor — but mechanical vetoes belong IN the engine. When a live read exposes the engine contradicting its own funding layer, fix the gate, don't just override. Related: [[project_lab_thesis]] [[feedback_funding_reload_exits_a_cooled_fade]] [[feedback_why_funding_negative]].
