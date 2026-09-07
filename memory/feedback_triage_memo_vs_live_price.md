---
name: triage-memo-vs-live-price
description: "Triage per-token memos carry static entry/stop/TP zones — always cross-check vs the live price before classifying a setup as \"live firing.\""
metadata: 
  node_type: memory
  type: feedback
  originSessionId: e94ca703-b256-430c-897b-c7124651d8f5
---

When synthesizing `/triage` output, the per-token memo lines (entry zone, stop, TPs) are STATIC notes attached to each watchlist token — they were written at a prior moment and may be stale vs the current live price printed in the same line.

**Why:** RIVER 2026-05-20 — triage memo said "MILD SHORT scout, entry $7.45-$7.65, TPs $7.00/$6.80/$6.56" but live price was $6.13 (below ALL three TPs). I promoted it to "Live signals firing now" anyway. The short setup was past-tense — it already played out — but I treated the memo as if it were today's entry box. User caught the error.

**How to apply:**
- For every token I'm about to flag as "live firing" in a triage synthesis, verify the memo's entry zone still brackets the current price.
- If current price is past the entry zone (in TP territory or stop territory), the setup is past tense. Reclassify to "Watching — needs re-setup" or "Past-trade, watch for re-arm." Never promote it as live just because the memo has actionable language.
- The signals-firing count in the triage table is venue-data-driven and current; the per-token memo line is human-written and can lag. Trust the live price + signal count over the memo's prose when they disagree.
- This applies to BOTH short and long memos.

Related: [[feedback-stay-strict-on-confluence]], [[feedback-scanner-not-therapist]]
