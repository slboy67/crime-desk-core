---
name: feedback-full-analysis-always
description: "When user asks to \"check\" a token, always run the full 5-layer pull at the depth shown in BILL/LAB analyses — not a thin verdict-first writeup."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 7d189419-ea54-42ce-9123-4ae76f85face
---

When the user asks me to "check" or analyze a token, **always run the full 5-layer pull at the same depth as the BILL and LAB analyses** — even when the token appears to be PASS or off-setup. The verdict doesn't dictate the depth; the user wants the work shown so they can verify the reasoning and re-examine later if context changes.

**Why:** On 2026-05-14 after BILL closed, user asked me to check SKYAI and ESPORTS. I produced thin verdict-first writeups for both (~10 lines each) instead of the deep 5-layer analysis I'd done on BILL and LAB earlier the same session. User pushed back: *"why if ask you to check u do such a small analysis u were doing better earlier it doesnt have to be a setup i just want you to do a full analysis not half"*. The compression was driven by my judgment that the tokens weren't tradeable — but that's not the user's question. The user wants the framework applied uniformly so they can audit my reasoning, store results in theses for later reference, and rebuild conviction as new data arrives. A "PASS" verdict is still a thesis that needs to be supported by evidence.

**How to apply:**
- Full 5-layer pull on every "check" or "analyze" request, regardless of whether the setup looks live or stale.
- Each layer should have its own section with concrete data points: Layer 0 full token id (MC, FDV, supply, ATH/ATL, categories, contracts); Layer 1 wallet sweep (top holders, MM-XTOKEN cross-check, distribution wallets); Layer 2 full perp regime (funding history per venue, OI trend, L/S progression, taker ratio, CVD); Layer 3 liquidation magnets (HVN + round numbers per timeframe); Layer 4 daily + intraday structure with named breakdowns, S/R, ATH wicks; Layer 5 catalysts/sentiment.
- Save full analyses to `theses/<TICKER>_<DATE>_analysis.md` so user can re-examine later when context changes.
- Verdict tier should be at the END of the analysis, not the beginning. Lead with data, conclude with synthesis.
- For PASS verdicts, explicitly state what WOULD change my mind (long-side setup conditions / short-side trigger conditions). PASS today might be a watch-list candidate; user needs the trigger criteria documented.
