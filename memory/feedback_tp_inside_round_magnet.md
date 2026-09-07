---
name: feedback_tp_inside_round_magnet
description: "Place TP1 a tick INSIDE the round-number magnet, not AT it. Round numbers ($0.001500, $5, $10) are where MM bids/asks stack; price gets approached-but-not-breached. A TP at exactly the round level gets fade-skipped by 2-5 bps and never fills, even when the directional call is correct."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

**Trigger 2026-05-26:** HMSTR short directional call was correct — price moved exactly where predicted, bottomed at **$0.001525**, then bounced. TP1 was placed at **$0.001500**. The trade was a textbook directional win that paid zero because the fill never triggered ($0.000025 = 1.6% short).

**Why it happened:** round numbers are the most-clustered limit-order zones in the book. Other traders place TP/entry/stop orders at the round level → MMs fade exactly into that wall. The level becomes a *magnet that price approaches but rarely breaches cleanly*. You can be 100% right on direction and structure and still never get filled.

This is the **inverse of Section 15 Pattern A's stop rule**: "round numbers are implicit magnets even when not bright on heatmap — stops below them get nicked." The same applies to TPs in the other direction — TPs *at* magnets get skipped.

**Rule:**
- **Short → fade SHORT, TP1 placed BELOW the round magnet** → bias 2-5 bps *above* the round (e.g. TP1 at $0.001510 instead of $0.001500 on HMSTR). Worse R:R by a few bps, but actual fill probability >> 0 to actual probability.
- **Long → fade LONG, TP1 placed ABOVE the round magnet** → bias 2-5 bps *below* the round (e.g. TP1 at $4.95 instead of $5.00). Same logic mirrored.
- **Wider magnets** (round dollar levels: $5, $10, $50, $100) deserve a wider cushion — 10-20 bps. **Sub-cent fractional rounds** ($0.001500, $0.05000) get 2-5 bps.
- This *only* applies to TP1 / TP2 — runner / TP3 can sit at deeper structure where the magnet isn't the dominant influence.

**Quick math — when does this matter?**
On a 2-5% fade short with TP1 ~5% from entry, a 1.6% miss-by-a-hair is the difference between **+5% (3R)** and **+0%**. The cushion costs you ~0.3R on R:R but converts maybe-fill into yes-fill. On the HMSTR trade, the missed cushion = the entire trade.

**Mechanical application going forward:** when `analyse.py` or any per-token TP suggests a price ending in `00` / `50` / a "clean" number, **shift it 2-5 bps inside the move direction before placing the order**. If the engine surfaces "$0.001500 TP1," I'll cite it as "$0.001510 TP1 (inside the $0.001500 magnet)" in writeups.

Related: [[project_hmstr_thesis]] (the trigger case), Section 15 Pattern A (the stop-side mirror), [[feedback_save_tokens_we_scan]] (TP-misses are valuable graveyard data too)
