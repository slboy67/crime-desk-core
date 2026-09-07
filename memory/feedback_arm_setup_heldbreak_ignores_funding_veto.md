---
name: feedback_arm_setup_heldbreak_ignores_funding_veto
description: "arm_setup.py (a SIGNAL generator, not an executor) held-break SHORT alert has NO funding gate — funding-guard sets armed=False but held-break ignores `armed`. So it SIGNALS a short entry into deep-neg funding, a §5-vetoed entry. LAB signalled exactly this 2026-06-02 (no position taken)."
metadata:
  node_type: memory
  type: feedback
---

`arm_setup.py` is a **SIGNAL/alert generator** (prints 🔴 ENTRY / 🎯 TP / 🚨 INVALIDATED lines for the human to act on — it does NOT place orders). It **signalled** a held-break SHORT ENTRY on LAB @ $17.81 (2026-06-02 23:40) at funding **−1.985%/int** (deep-neg), then signalled INVALIDATED @ ~$20.3 three minutes later on the re-squeeze. No position was taken (desk flat, positions.json empty) — but had the signal been followed it would have stopped for a ~−14% loss before price cascaded to $7.46. The flaw: the alerter surfaced an ENTRY signal for a §5-vetoed short (never short funding ≤ −0.30%/4h), contradicting the watch's own stated funding-guard ("stays disarmed until funding cools >−0.30").

**The bug (confirmed in code):**
- The funding-guard (arm_setup.py ~line 115) only does `armed = False` + skips one poll cycle on a deep-neg read.
- But **held-break mode sets `trig = held` directly from price closes and never reads `armed`** (only *self-arming* mode gates on `armed`, ~line 142). So in held-break mode the funding-guard cannot prevent an entry — there is **no hard funding veto on the entry path**.
- Likely compounded by a units mismatch: guard threshold passed as raw `-0.3` while funding rates are ~`-0.02` raw, so `fr <= guard` never even fired the cycle-skip.

**How to apply:**
- **Trust-but-verify any armed SHORT against §5 manually** until arm_setup enforces the veto: a held-break short can fire into deep-neg funding today. Do not assume an arm respects the funding-guard.
- The §5 veto is a HARD pre-entry gate, not a post-arm disarm. A short entry must be blocked outright when funding ≤ the veto threshold, in every trigger mode (held-break, break, self-arming, now).
- Coder ticket filed (`handoffs/` phase): add a funding pre-entry veto to the trig/ENTRY path itself, normalize the guard units (%/4h, not raw), and unit-test "held-break does NOT enter while funding ≤ guard."
- Related: [[feedback_live_top_signal_is_nonce_not_getlogs_audit]] (same LAB episode, the on-chain blind spot), [[feedback_blowoff_short_deepneg_veto]] (the engine-side veto that arm_setup must mirror).
