---
name: verify-wallet-history-not-just-balance
description: "A wallet that looks like a pristine \"dormant mega-safe\" by current balance + recent dormancy can actually have a full distribution history. Verify lifetime inbound/outbound, not just current balance + last-30d activity, before labeling a holder pristine."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: e94ca703-b256-430c-897b-c7124651d8f5
---

When tiering a large holder as a "dormant mega-safe / pristine long-term hold," verify its LIFETIME flow history (full inbound + outbound), not just current balance + recent (30d) dormancy. A wallet can be dormant NOW but already have distributed heavily in the past — making it a proven distributor sitting on a remainder, not an untouched allocation.

**Why:** EDEN 2026-05-21 — I had `0x80AA92...` mapped as "MEGA-SAFE-200M, dormant, first outbound = MAJOR distribution signal (Section 9 Rule 4)." Cited it as pristine-dormant in every EDEN check. User surfaced Twitter flow analysis: the wallet had RECEIVED 352.8M (near a top) and DISTRIBUTED 152.8M (149.4M near bottom + 3.42M recently), holding 200M remainder. Arkham confirmed unlabeled EOA "EDEN Whale" (not CEX). My note was wrong — it ALREADY had its first outbound and many more. The "200M dormant safe" was actually the leftover after a proven distribution campaign. My scans only checked current balance + recent dormancy (nonce 3, 0 inbound 30d) and concluded "pristine," missing the >30d distribution history.

**How to apply:**
- For any wallet tiered as Mega-Safe / Reserve / pristine-hold: run a LIFETIME flow scan (or check Arkham's full history / first-funder + cumulative in/out), not just current balance.
- "nonce low + recent dormancy + round-number balance" does NOT mean pristine — it can mean "distributed in the past, now paused, holding a round remainder."
- The distinction matters for the trade: a pristine safe's first-outbound = the distribution START signal; a proven-distributor's resumed-outbound = distribution RESUMING (different, and the overhang is more likely to keep selling since it already has).
- Round-number holdings (exactly 200M) + low nonce are a flag to check history — allocation/vesting wallets often distribute down to round remainders.
- Default scan window (flows.py 24h, watch_wallets recent) is too short to catch historical distribution. For tiering decisions, scan 60-90d or use Arkham lifetime view.

**Generalizes:** Applies to every Cat A mega-safe/reserve tiering. The "dormant safe lighting up = biggest escalation signal" (Section 9 Rule 4) assumes the safe is pristine. If it's already a proven distributor, the escalation framing is wrong — resumed selling is continuation, not a fresh start.

Related: [[feedback-cex-deposit-is-positioning-not-execution]], [[feedback-cross-rpc-verify]], [[project-eden-thesis]], [[reference-arkham-playwright]]
