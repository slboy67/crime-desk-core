---
name: feedback_engine_overflags_thinspot_shorts
description: "analyse.py mass-produces SHORT (mild) on thin-spot Cat A tokens — it's the engine's modal template output, not a per-token edge. Treat thin-spot SHORT (mild) as low-confidence; confirm before sizing."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 5e80cb56-db4e-4bb8-80db-b6b0f5b1d7f5
---

`analyse.py`'s **SHORT (mild)** verdict on thin-spot Cat A tokens is frequently the engine's **modal/default output, not an identified edge.** Don't read it as conviction.

**Why:** 2026-05-29 the adversarial-thesis-killer went **2-for-2 KILLING engine shorts** — ESPORTS (floor-chase, +72% squeeze magnet vs −24% target, wash book) and RIVER (empty trap: OI −2.8σ/−3.5σ generational lows + funding at the +0.005% floor = no crowd to squeeze + 48,000:1 perp-casino). The RIVER run surfaced the root pattern: **11 of 13 pending entries in base_rates_pending.json were SHORTs sharing the identical `UNRELIABLE_THIN_SPOT` + `NO_WHALES` flags.** The engine emits the same short template on every thin Cat A. This is the base-rate calibration gap (audit 2026-05-29) showing up live — SHORT_MILD is n=2, below the n≥10 gate ([[feedback_save_tokens_we_scan]]'s sibling discipline).

**How to apply:**
- A **SHORT (mild)** with `CVD: UNRELIABLE_THIN_SPOT` + `HL whales absent` + no Tier-1 on-chain execution = the **default template**, treat as discretionary/low-confidence, NOT a signal. The verdict header looks the same whether there's an edge or not.
- Before sizing any thin-spot Cat A short, demand a **non-template confirmation**: funding settling above the +0.005% floor across 3+ settlements, OI *building* (not at extreme-low), a confirmed breakdown trigger (not entry near the 24h high), or a Tier-1 on-chain CEX route. Or run the thesis-killer.
- The two genuine kill-patterns to watch for: **floor-chase** (shorting bottom-of-range after the cascade already fired, magnets inverted) and **empty trap** (positive funding + crowded L/S but OI at multi-sigma LOWS = the crowd already left; you can't trap who isn't there — a phase-table error vs a real Stage-5 top).

Related: [[feedback_aggregate_oi_faked_via_double_open]], [[feedback_workflow_model_tiering]]
