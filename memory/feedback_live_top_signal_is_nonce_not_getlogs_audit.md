---
name: feedback_live_top_signal_is_nonce_not_getlogs_audit
description: "Live bid-pull = a mega-safe NONCE tick (nonce_watch, cheap), NOT the getLogs lifetime audit (can't finish a multi-wallet BSC scan). BUT a cascade ≠ a bid-pull: deep-neg + OI collapsing = perp deleveraging (LAB did this, safes dormant); deep-neg + OI building + fresh nonce = real distribution. Check OI direction + safes before narrating a 'bid-pull top'."
metadata:
  node_type: memory
  type: feedback
---

The on-chain layer in `analyse` calls `onchain_analyser.py` → `safe_audit.py`, which walks `eth_getLogs` in ~49k-block chunks across every tracked wallet. On a multi-wallet BSC name this **cannot complete** — LAB (28 wallets) timed out at the FULL 150s subprocess budget (2:30 wall), returning `ON-CHAIN DEGRADED`. So `analyse`'s on-chain verdict is `UNAVAILABLE` on exactly the §4 vesting-hedge names where on-chain matters most.

**Why it matters (LAB 2026-06-02→03):** LAB was the §4 OTC/VC vesting-hedge squeeze (deep-neg funding −1 to −4%/int persisting at fresh ATH). Per §4 the top is *often* **the bid pulling — a mega-safe nonce firing**. LAB cascaded $21 → $5.76 (−76% low) with funding deep-neg the WHOLE way. While on-chain was down I asserted (too confidently) that the top "was a bid-pull we were blind to."

**Correction once on-chain came back:** the tracked LAB safes did NOT fire at the cascade — every team/Bitget-deposit safe last moved March–May, `newly_fired: 0`. Combined with OI collapsing **−57%** and funding staying deep-neg, the drop reads as a **perp-side deleveraging / long-liquidation cascade** (longs liquidated, OTC hedges stayed short), NOT a fresh on-chain bid-pull.

**The lesson (the real one):**
- **A cascade ≠ a bid-pull. Don't attribute a deep-neg drop to on-chain distribution without checking the safes.** Deep-neg + OI collapsing = leverage unwind / long-liq; deep-neg + OI building + a fresh safe nonce = actual distribution firing. The OI direction and the nonce check disambiguate — run them before narrating a "bid-pull top."
- nonce_watch is still the right *live* tool for a real bid-pull (cheap, dodges getLogs), and on-chain being down on a Cat A name is still a real blind spot — but "on-chain blind" is not licence to assume the worst-case distribution story. State it as unknown until checked.
- Caveat: the real seller can be off-chain/invisible (§8) or an unmapped wallet, and SPEC 9 (`baseline_seeded`) must land before the nonce ESCALATION verdict is precise. The deterministic safe_history (last-outbound timestamps) is trustworthy now regardless.

**How to apply:**
- The **live top signal on a vesting-hedge name = a mega-safe nonce tick**. Use **`nonce_watch.py`** (`eth_getTransactionCount` per safe — one tiny reliable call, dodges getLogs range limits). This is the *trigger* layer.
- **`safe_audit` (getLogs lifetime audit) is the wrong tool for a live top** — it's slow due-diligence ("is this safe a proven distributor"), not surveillance. It can't even finish a 28-wallet BSC scan; do NOT block a live verdict on it.
- When `analyse` returns `onchain:"UNAVAILABLE"` on a §4 name, that is **NOT "spectate calmly" — it is "blind to the exact signal that calls the top."** Flag it loudly and, for an armed vesting-hedge thesis, run `nonce_watch` on the mega-safes manually rather than waiting on `analyse`.
- The perp deep-neg veto (§5) correctly refuses the *preemptive* short at the top — that rule is +EV and was not wrong here. The miss was instrumentation: the tradeable signal lived on-chain and was uninstrumented for live. Don't "fix" this by loosening the §5 veto.
- See [[feedback_flows_bsc_rpc_unreliable_for_getlogs]] (same getLogs-on-BSC failure mode, same nonce_watch remedy) and [[reference_bsc_archive_rpc]] (blastapi-first pool — already correct, not the bottleneck).
