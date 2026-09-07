# TP structure follows thesis type — one TP for round-trip risk, two for destination theses

**Rule.** The number of take-profits on a leg is decided by what the thesis claims about the destination, not by preference:
- **ONE TP** (first real shelf, highest fill odds) when the dominant post-target scenario is a round-trip: counter-trend scalps on chronic squeezers / operator names, operator-flush banks, any long whose squeeze fuel is inferred rather than measured. (CYS 2026-08-24: reclaim-long scalp, single TP 0.648 under the $107K shelf.)
- **TWO TPs** (bank majority at first structure, runner to the thesis destination, stop→BE after TP1) when the destination IS the thesis: faded_bounce fades, stage5 breakdowns, unlock-cliff fades. (GALA 2026-08-24: 0.00165 pre-pump base + 0.00142 above the dump low.)
- **Never more than two rungs at the $500/10x size cap** — a third rung is a ~$500 notional slice: fee/noise territory, not risk management.

**Why.** The two failure modes are symmetric and both cost real R: holding ladder residue through a squeezer round-trip (LAB gave back all +28%; "TP into the spike" is standing), and fully exiting a confirmed distribution move at first structure (DEXE 29->3.86 banked 0R). One rule can't fix both; the thesis type is the discriminator. Upgrade path: a single-TP scalp earns a runner mid-trade ONLY if a measured squeeze signature prints while in position (funding multi-sigma neg + OI building + liqs printing) — evidence, not hope.

**How to apply.** At commit time, set each leg's `tp` list length from its signature class (squeezer-scalp/flush-bank -> 1; faded_bounce/stage5_short/unlock_cliff_fade continuation -> 2). The counterfactual scorer reads the committed tp list, so this convention shapes the paper record too. User confirmed as standing policy 2026-08-24.
