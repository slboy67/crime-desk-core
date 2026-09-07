---
name: squeeze-fuel-is-a-long-signal
description: "When heatmap is dominantly upside-loaded + retail L/S crowded short + chronic re-squeeze pattern, that's a Cat A LONG squeeze setup — propose the long even if strict Section 6 trap-formation criteria (deeply negative funding) aren't all firing."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: e94ca703-b256-430c-897b-c7124651d8f5
---

⚠️ **REFINED 2026-05-22 (the funding-article integration — read FIRST):** this memory's pro-long bias is correct WHEN the squeeze positioning is real, but it must NOT collapse into "negative funding → long." Negative funding on alts is a coin flip until you know WHY (CLAUDE.md Section 2 "4 reasons"): the default alt reason is **MM-hedge distribution** (sell spot + short perp = persistent negative funding while they distribute — you become exit liquidity, "win the funding lose the trade"), NOT trapped shorts. The signals in THIS memory (upside-heatmap dominance + crowded-short L/S + chronic re-squeeze) are legit squeeze-fuel POSITIONING — richer than bare negative funding — but still confirm it's **loading, not hedging**: (a) **OI SPIKING** alongside (loading) vs flat/falling (hedge/cascade → trap); (b) **spot CVD diverging UP from perp CVD** (`cvd_spot_perp.py` — real spot money vs leverage panic; often more reliable than funding). No spot-buy confirm + flat OI = likely the hedge trap, downgrade the long. So: still LEAD with the long when the positioning fires (don't bury it in a PASS), but gate size/conviction on the loading-vs-hedging confirm. The two notes reconcile: lead with the long *idea*, but prove it's reason #1 not reason #4 before sizing.

When the framework signals point unambiguously at a squeeze UP, the trade idea is LONG — even if the strict Section 6 Cat A LONG (trap-formation) checklist isn't fully met. The Cat A LONG edge is broader than the trap-formation funding signature per [[feedback-cat-a-long-broader-than-trap]] — squeeze-fuel readings count too.

**RECURRENCE — LEAD with the long, don't bury it (FIDA 2026-05-21):** scanned FIDA at $0.0357 with squeeze-fuel firing (DWF-managed momentum, negative funding, OI rising, retail short) — I noted all the signals but LED with "late chase, PASS" and buried the long as a conditional footnote ("breakout >$0.040 OR pullback $0.032"). FIDA ripped to $0.046 (+28%), volume exploded 40× ($5.5M→$224M). The breakout-long I footnoted would have paid +15%, but the framing drowned it. **The failure isn't missing the signal — it's HEDGING it into a PASS.** When squeeze-fuel fires, the headline verdict should be "LONG (squeeze-fuel), entry here, risk X" — NOT "PASS, but if it breaks out maybe." Caution is an entry-location/sizing question, not a reason to default the verdict to PASS. Second occurrence (LAB was first) = this is my standing bias to correct: I under-weight squeeze-fuel longs by reflex.

**Why:** LAB 2026-05-20 15:35-15:50 — I gave a "PASS, no entry" verdict on LAB while listing all of these signals correctly:
- Heatmap dominantly upside-loaded ($6.49 / $4.69 brightest = short stops crowded)
- L/S 0.43 retail crowded SHORT (squeeze fuel)
- Chronic re-squeeze pattern (14 squeezes in 90d, ESPORTS-class)
- ZachXBT explicit warning: shorts give operators fuel to squeeze higher
- Compression wedge tightness 0.50 = directional break loading
- Funding flat + OI below avg = potential trap-formation pre-squeeze loading
I even wrote "squeeze UP is much more probable than cascade DOWN, based on positioning alone." THEN I proposed a SHORT conditional entry. LAB squeezed +7% within 10 minutes ($4.09 → $4.38). User had closed their CHIP long and was looking for an alternative; the LAB long was right there in the signals and I didn't propose it.

**How to apply:**
- If 4+ of these fire simultaneously, propose a LONG squeeze scout entry, not a PASS:
  - Heatmap upside cluster intensity ≥ 80% AND dwarfs downside intensity
  - Retail L/S ≤ 0.50 (crowded short = bait fingerprint = squeeze fuel)
  - Chronic re-squeeze regime (>1 squeeze per 10 days in window)
  - Recent cascade fired + chop forming (cascade-is-the-setup, [[feedback-cascade-is-the-setup]])
  - Funding flat or trending negative (room for trap-formation to deepen)
  - Compression wedge tightening
- Entry zone: intraday support reclaim or pullback to broken resistance
- Stop: below cascade low or wedge low + cushion
- TPs: brightest upside heatmap clusters
- Funding deeply negative is a TIER UPGRADE (higher conviction, bigger size) — NOT a gate. Same logic as [[feedback-cat-a-long-broader-than-trap]] applied to squeeze-fuel rather than operator-alignment.
- Trigger-armed setups (LAB WITHDRAW-FRESH gate) only gate the SHORT side; the LONG side has its own setup criteria via squeeze-fuel reading.

**The framework error to avoid:** treating "the operator-trigger gate isn't met" as "no trade." The operator-trigger gates the SHORT. The LONG opportunity can exist independently when the bilateral fuel is loaded for an up-move BEFORE the operator trigger fires.

Related: [[feedback-cat-a-long-broader-than-trap]], [[feedback-cascade-is-the-setup]], [[feedback-token-specific-pattern-overrides-framework]], [[feedback-stay-strict-on-confluence]] (caveat: "stay strict" means don't loosen on user wins on sub-threshold setups — it does NOT mean ignore signals that fire correctly outside the most-strict Section 6 checklist)
