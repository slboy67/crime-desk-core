---
name: reference_wash_detect_tool
description: wash_detect.py — tape-side wash detector (complements oi_sides); two wash modes (pure 对敲 vs pinned-distribution); not engine-native until calibrated
metadata: 
  node_type: memory
  type: reference
  originSessionId: 8e11afa5-c50d-49e8-984f-fa56c43b8784
---

**`scripts/wash_detect.py <TICKER> [--samples N] [--watch] [--json]`** — built 2026-06-02. The **tape-side** wash-trading detector; `oi_sides.py` is the OI-side. Reads Bybit recent-trades.

**Signals (calibrated, what survived):** CHURN (sustained vol in a tiny range *over time* — time-span≥3min guard is essential; a crash packs 1000 trades into seconds and false-flags) · TAKER-SYMMETRY (buy≈sell vol = matched self-trade) · PERP-PREMIUM (mark>>index = manufactured bid) · IDENTICAL-CLIP (one size = large % of *volume*). **Dropped:** count-based size-repetition — fires ~97% on genuine flow (MM lot-standardization), doesn't discriminate. Honest tools > impressive ones.

**Wash is SUSTAINED → multi-sample.** A single snapshot spans ~1-2min and the taker oscillates (ESPORTS flipped REAL↔WASH between snapshots). `--samples 4` (default) accumulates a time-spanning de-noised read. This was the key robustness fix.

**★ The deep finding — two manipulation MODES, one boolean can't separate them:**
- **Pure 对敲 wash** = CHURN ∧ TAKER-SYMMETRY (matched self-trade in a band).
- **Pinned-distribution** = CHURN + PREMIUM with *directional* taker (operator defends a band / manufactured bid while real selling happens underneath). **ESPORTS is this** — over a stable 5min window it shows churn + premium but directional taker, so it's pinned-distribution, NOT pure wash. oi_sides flagged it WASH (OI-side); wash_detect shows the tape is directional. **Read BOTH together** — neither alone is the full picture; both → real-directional = wash genuinely done.

**The actionable event = the wash→REAL transition** (`--watch` fires on it): operator stops pinning → taker goes one-sided + range expands on a break → the real move (cascade/bleed) starts. Short the *transition/breakdown*, never the pinned chop.

**⚠ NOT engine-native yet (deliberate).** Documented in CLAUDE.md tool registry but NOT auto-run by analyse.py — needs calibration to n≥10 labeled cases first (Section 13 base-rate gate). Shipping an uncalibrated wash boolean would mislabel real-but-quiet coins. **TODO: log wash_detect verdicts on known-washed (ESPORTS-type) vs known-real coins over the coming days, validate the thresholds + the two-mode split, THEN promote to engine-native like oi_sides.** [[feedback_aggregate_oi_faked_via_double_open]] [[feedback_engine_overflags_thinspot_shorts]]