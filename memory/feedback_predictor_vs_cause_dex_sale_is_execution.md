---
name: feedback_predictor_vs_cause_dex_sale_is_execution
description: "CEX deposit = POSITIONING (predictor; the sell is invisible, timing decides meaning) vs DEX sale = EXECUTION (direct cause; visible, timestamped, direct evidence) — weight them differently in Stage-5 reads. Outside validation: Onchain School independently names LAB/ESPORTS/PIPPIN as team-supply futures-extraction structures; their solo-pump-vs-sector test = cheap Cat A pre-classifier (SPEC-114); DEX-execution detector = SPEC-115."
metadata:
  node_type: memory
  type: feedback
---

**Rule:** In any distribution/Stage-5 read, split on-chain exit evidence into two tiers and weight
them differently:
- **CEX deposit = POSITIONING** (predictor). It shows intent; the actual sell executes inside the
  exchange's internal database and is invisible — only the deposit address, hot wallet, and public
  order book remain observable. Timing decides meaning per §8 (pre-pump = loading, mid-pump = bait
  theater, post-cascade = real exit). Evidentiary weight: circumstantial.
- **DEX sale = EXECUTION** (direct cause). The trade IS the on-chain event: direction, size, pool,
  tx hash, timestamp, all on the public ledger, and the MM/arb propagation loop carries the DEX
  print to every venue. Evidentiary weight: direct, timestamped proof of realisation.

**Why:** §8's "the SELL is off-chain/invisible" is a CEX-only truth, and treating it as universal
made the desk deposit-inference-only — while the apparatus's own swap routers
([[reference_multi_cat_a_router_0x238a3588]], [[reference_second_cat_a_router_0xb300000b]]) sit
fingerprinted but unwatched. A tracked wallet's $400K router sell is a *stronger* Stage-5
escalation than a same-size CEX deposit (execution confirmed vs inferred), and the exploit-dump
pattern (fresh wallet dumps a just-received balance on a pool — UXLINK) is the clearest form.
Source: Onchain-Analysis-Workshop-CrimeDesk.md Lesson 8 (predictor vs cause).

**How to apply:** Label the two tiers explicitly in briefs/board notes — `POSITIONING:` for
deposits, `EXECUTION:` for DEX sells — and never let a "no deposits seen" read stand as "not
distributing" without checking the DEX leg (and vice versa). A DEX sell by a tracked wallet or
its fresh child is a third rotation-evidence leg for the FROZEN→ROTATED flip (SPEC-98), senior to
the CEX leg. Engine: SPEC-115 (detector). Chips context: the holder-map read this feeds is
[[feedback_chips_positions_framework]].

**Secondary note — outside validation of Cat A:** independent analysts (Onchain School workshop,
Lesson 10 Q&A) name **LAB, ESPORTS, PIPPIN** as the same structure the desk calls Cat A: solo
pump + team-controlled supply + no fundamentals + token as a tool to extract liquidity from the
futures market. Their first-pass test — one coin vertical while its sector basket is flat →
presume manipulation; sector-wide rise → liquidity rotation — is the cheap pre-classifier the
desk lacked; engine: SPEC-114.
