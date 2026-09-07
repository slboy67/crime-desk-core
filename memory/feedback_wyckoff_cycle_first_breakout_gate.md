---
name: feedback_wyckoff_cycle_first_breakout_gate
description: "@derrrrrrrq's Wyckoff overlay — classify the CYCLE (accumulation vs distribution) before reading any breakout/breakdown; AR is fragile (short-covering, not demand); on small coins ST/Spring are TOP tools (small coins have no bottom); tops are squeezed out, not predicted."
metadata:
  type: feedback
---

**Rule:** Before reacting to any breakout or breakdown on a crime coin, classify the Wyckoff CYCLE first: in an accumulation/re-accumulation cycle, breakouts are real and shorting them is the error; in a distribution/re-distribution cycle, fake breakouts that harvest long liquidity are the NORM and longing them is the error (2056762127801819340). Structural sub-rules: (1) the AR bounce after a selling climax is fragile — short-covering + leverage, not demand; the tradeable read is short-at-AR in a down cycle, never knife-catch the SC; real demand is confirmed only at a low-volume ST. (2) On micro-caps use ST/Spring logic at TOPS (UTAD after deleveraging) — "small coins have no bottom", so bottom-springs are only for late accumulation with a confirmed snap-back into the range. (3) Tops are SQUEEZED out, not predicted: continuation above a top-ST requires fresh liq clusters/squeeze space above; fuel exhausted = top, regardless of level.

**Why:** This is the missing structure layer between the desk's stage read (§0.6 #2) and its entry triggers (§6). The stopped shorts in the desk ledger were breakout/breakdown reactions inside the wrong cycle (shorting inside accumulation squeezes). The cycle classification is exactly what makes a "clean breakdown candle" tradeable or bait — same candle, opposite meaning by cycle. Source: @derrrrrrrq Wyckoff articles (ST 2039521127509426337, Spring 2039896257192489359) + [[reference_derq_freeland_corpus_playbook]] §2.

**How to apply:**
- Add "which cycle" as the FIRST line of any breakout/breakdown read — if the desk can't name the cycle, there is no breakout trade.
- Blowoff/Stage-4-5 short setups (§6): require the distribution-cycle classification, not just the candle + volume conditions.
- Never long a breakout inside a distribution cycle even with volume; never short a breakdown inside accumulation (that's the engineered shakeout of §1 sub-edge 1 — often the trap-formation LONG).
- Slow-grind breakouts at obvious technical levels = "one fish eaten twice" trap (shorts cover + breakout longs enter, then the needle takes both). Clean = real; grinding = bait (2053609317333622901).
- Candidate coder ticket: a `phase` capability — SC/AR/ST/Spring/UTAD detection on 1m/15m from price+volume+OI+liq, feeding classify/tape.
