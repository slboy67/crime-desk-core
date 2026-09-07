---
name: output-format
description: "Token-read responses must use the structured scannable template (verdict header → snapshot → on-chain → perp → levels table → most-likely path), NOT dense paragraphs. User is ADHD."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: e94ca703-b256-430c-897b-c7124651d8f5
---

User (2026-05-22): "I want the output more organised, it's hard to read now." User is ADHD — dense paragraphs don't work; visual structure does.

**Standard token-read template (use for every single-token analysis). User asked for it ELABORATE (2026-05-22) — full depth in each section, not a thin skeleton:**

```
# <emoji> $TICKER (<full name>) — <VERDICT in 3-5 words>
**One-liner:** the single most important takeaway.

## 📊 Snapshot      (TABLE: price+24h%, MC/FDV+ratio flag, circ %+lock, vs ATH+date,
                     7d/30d, range pos, 24h vol+gate — with a 'note' col)
## ⛓️ On-chain — <PHASE>  (TABLE of key wallets: wallet | action | size, bold $ amounts;
                     CEX-sold vs staged vs internal; dormant count; a bold 'Read:' line)
## 📈 Perp          (TABLE: funding/premium/OI/L-S with a 'mechanical read' col;
                     + a bold trap/fuel note if config is squeeze-prone)
## 📐 Structure     (vs ATH, lower-highs, breakdowns on volume, re-squeeze history)
## 🎯 Levels        (TABLE: Dir | Level | Why — ▲ squeeze/cap, ▼ targets, ✋ invalidation)
## 🔮 Most likely path  (NUMBERED phases, each with the WHY + a rough likelihood:
                     "1. Now → $X squeeze (~60% first move) because …; 2. → rejection …;
                      3. → cascade $Y → $Z")
## 📋 Trade setup    (ALWAYS — even on PASS. TABLE with: Direction | Entry zone |
                     Stop + stop% | Leverage (from Section 6 table, haircut thin/churn) |
                     TP1/TP2/TP3 ladder | Invalidation | Timeframe(scalp/hold).
                     On PASS: give the CONDITIONAL setup — "no trade until X; then entry Y…")
**The call:** one bold line summarising the setup.
```

**ALWAYS give a trade setup (user, 2026-05-22: "also give me a trade setup always").** Even when the verdict is PASS/WATCH, provide the conditional setup: the exact trigger to wait for, then the entry/stop/targets/leverage that would apply. Never leave them without an actionable plan. Use the Section 6 leverage table (max safe = 1/stop%, haircut for thin-venue/perp-casino/churn) and position sizing (account_risk% / stop_distance% = notional %).

**Rules:**
- Verdict FIRST (header + one-liner), details after. Never bury the call.
- Use tables for snapshot / perp metrics / levels — not prose.
- **Bold the key numbers** (prices, $ flows, funding).
- Emoji section headers as visual anchors (📊 ⛓️ 📈 🎯 🔮) — consistent with the in-terminal color scheme 🔴live/🟠watch/⚪dust.
- Keep prose to short bullets; the "most likely path" is an arrow sequence, not a paragraph.
- Pairs with [[plumber-facts-not-predictions]] (decisive verdict + forecast, no suggestion-lists) and [[Dashboard rejected and deleted]] (in-terminal/in-chat visual, no separate web UI).
- For a quick triage sweep, the compact-card style (from triage.py) is the equivalent; this fuller template is for single-token deep reads.
