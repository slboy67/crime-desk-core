---
name: feedback_funding_reload_exits_a_cooled_fade
description: "On thin manipulated names a cooled-funding fade can see funding RE-DEEPEN within the hour = squeeze reloads; exit on the funding reload, not the price stop"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 8e11afa5-c50d-49e8-984f-fa56c43b8784
---

A blowoff-fade short that fires correctly (funding cooled flat + clean break held) can still die fast: on thin manipulated Cat B/perp-casino names the funding **re-deepens within ~40min** and the squeeze reloads.

**Why:** PORTAL 2026-05-30 — funding cooled −2.5%→flat (+0.005%), broke $0.01216, held 6 candles (legit fire, the fixed funding-gated watch was right). Entered ~$0.01146. Within 40min funding reloaded **flat → −0.18 → −0.31 → −0.49%/4h** while price bounced to retest the broken floor (wicked $0.0124). Price never confirmed-reclaimed (rejected back to $0.0118, stop technically held) — but the *premise* (funding cooled = squeeze fuel gone) had fully broken. Cut at ~scratch on the rejection rather than wait for the price stop. Right call: deep-neg funding = the never-short-deep-neg veto re-applies + you pay ~3%/day carry into reloading squeeze fuel.

**How to apply:**
1. **Funding is the leading tell; price is the lagging one.** On a cooled-funding fade, watch funding as closely as price — if it re-deepens past ~−0.30%/4h while the trade isn't already paying out, the edge is gone *before* the price stop hits. Exit on the funding reload, don't wait to be proven wrong on price.
2. **A price-only trade-manager watch is half-blind here** — it tracks levels (TP/inval/retest) but NOT funding, so it can't see the premise breaking. For cooled-funding fades, either add a funding-reload guard to the watch or personally track funding each heartbeat.
3. After cutting, **re-arm the funding-GATED fade watch** (not a price-only manager) so it correctly stays gated while funding is deep-neg and only re-fires when funding cools to flat AND it breaks down again. [[feedback_why_funding_negative]] / Section 2 deep-neg-short veto.