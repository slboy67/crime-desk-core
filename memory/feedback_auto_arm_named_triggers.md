---
name: feedback_auto_arm_named_triggers
description: "Auto-arm price-level alert watches by default when a setup trigger is named, rather than asking \"want me to arm it?\" — the ask-first gate costs trades"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

When I identify a setup and name a specific trigger level (e.g., "BEAT short fires on $1.135 break + no reclaim"), **arm the price-alert watch immediately by default**. Don't end the message with "want me to arm it?" and wait for user confirmation — that gate costs trades when the user moves on to another token and the trigger fires unattended.

**Why:** BEAT 2026-05-25/26 — I correctly identified the blowoff-top short setup ($1.529 ATH → cascading), named the trigger ($1.135 break, no reclaim), described the TPs ($1.08, $0.96), and asked "want me to arm it?" — user didn't reply, conversation moved to other tokens, the $1.135 break fired hours later during a stale-funding-watch window, BEAT cascaded to $0.97 (−15% from trigger / ~5R short missed). The setup played out exactly as called. The framework was right; the execution failed because the alert wasn't proactively armed.

**How to apply:**
- **Default behavior after naming a setup trigger: arm the watch immediately**, then mention it in passing ("armed `<id>` at $X break / $Y reclaim — disarm if you don't want it"). User can disarm; they shouldn't have to confirm to enable.
- This applies to any setup where I've named: (a) a specific price trigger, (b) a specific entry/stop/TP plan, (c) a defined invalidation.
- Exception: if the trigger requires non-price confirmation (e.g., "fires on X-wallet nonce tick"), arm whatever subset is alertable (price levels at minimum) and note the rest.
- Cost of false-arms: zero (background bash polls cheaply, will exit on either trigger). Cost of missed trades from un-armed triggers: real, measurable in R-multiples.
- This complements [[feedback_dont_drift_to_perp_only_scans]] (full coverage discipline) — proactive arming is the execution-side mirror of proactive on-chain pulls.
