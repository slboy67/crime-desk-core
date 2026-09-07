---
name: feedback_aggregate_oi_faked_via_double_open
description: "On demon coins the 庄 fakes DIRECTIONAL aggregate OI via long-short double-open (对敲); confirm with per-side short OI, not aggregate OI deltas"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

Aggregate OI building during a price decline is NOT proof of a trapped side on operator-controlled demon coins — the 庄 (banker/MM) manufactures fake directional OI via **长短双开 / 对敲 (long-short double-open, self-matching both sides)**. The visible "openly stacked short OI" (OI up + price down + CVD down) reads as trapped shorts / danger, but the 庄 itself opened that side as a smokescreen before a pull-up.

**Why:** BSB 2026-05-22 — I read "OI +34/38% building" across 3 triages as trapped longs arming a rollover-short. User corrected: BSB's aggregate OI is faked by the 庄. The tell is per-side short OI (user's "Figure 3" tool): after the pull-up, short OI was FAR lower than before — real trapped shorts get *liquidated* on a pull-up (OI burns); fake double-opened OI just gets *closed quietly*, so aggregate OI "barely drops" while the short side collapses. User flagged BSB as one of the most demonic coins seen recently — new OI-falsification tricks.

**How to apply:**
- On demon/Cat-A coins, treat **aggregate OI deltas as a 庄 instrument, not a signal.** Don't infer "trapped longs/shorts" from aggregate OI building. This extends Section 5 OI-brushing (Vol/OI>20x) and the Section 2 OI÷L/S-frozen rule to a nastier case: faked *directional* OI even when L/S moves.
- The hard-to-fake signals are **funding** (positive flip = a real cash cost the 庄 pays on the long leg) and **price/CVD structure**. Lean on those + a confirmed breakdown; never short/long off the aggregate-OI build alone — that's often the bait.
- **Per-side short OI is the clean confirmation** — Real squeeze = short OI burning on the pull-up; fake = short OI quietly closed, aggregate ~flat. Read this before treating aggregate OI as directional. Related: [[feedback_weight_structure_layer_vs_onchain_print]] (don't let one mis-weighted signal drive the call).
- **TOOL BUILT 2026-05-29: `scripts/oi_sides.py <TICKER>`** — splits total OI into long/short sides (short share via Binance topLongShortPositionRatio) and scores the 对敲 fingerprint: OI-build-while-L/S-frozen + balanced-taker-on-build + Vol/OI brushing + single-venue ≥65%. Verdict 对敲 WASH / MIXED / REAL DIRECTIONAL, with a pull-up short-OI-burn note. `--period 5m/15m/1h --limit N`. Run it on any "OI building" demon-coin read before calling a trapped side. (Free Binance futures/data; top-account-weighted proxy, not exact — still the cleanest free per-side read.)
