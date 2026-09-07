---
name: engine-first-architecture
description: "Run analyse.py FIRST and reason over its output; don't hand-curl funding/CVD/levels and re-derive the rules in your head. The mechanical gates are deterministic CODE, not prose to recompute."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: e94ca703-b256-430c-897b-c7124651d8f5
---

Architecture redesign 2026-05-22 (user: the prose-heavy, re-derive-every-message setup is token/context-hungry; make it deterministic + code-based). Now CLAUDE.md §0 "HOW TO OPERATE — engine-first."

**The rule:** for any "should I trade this / which way" question, **run `analyse.py <TICKER>` first** and reason over its output. Do NOT hand-curl funding/CVD/OI/levels and re-apply Sections 2/6/7/9 in your head — that's slower, burns context, and is where the stale-print mistakes came from (BB stale-funding, EDEN +0.041% near-miss). The engine live-verifies natively, so it's MORE disciplined than manual derivation.

**What the engine now enforces deterministically** (built into analyse.py + perp_analyser + onchain_analyser + cvd_spot_perp, validated 2026-05-22):
- GATE 0 liquidity (auto-PASS if 24h turnover < $10M; scout flag $10-25M)
- live-funding verify (native), who's-trapped (funding×L/S), funding phase, OI z-score
- **funding "4 reasons" / spot-perp CVD confirm:** a neg-funding LONG is VETOED if CVD = BEARISH_DIVERGENCE (spot selling = MM-hedge trap), downgraded to scout if CVD doesn't confirm (aligned/thin), confirmed if BULLISH_DIVERGENCE
- **short-side veto:** a SHORT is VETOED if funding ≤ −0.30%/4h (deep-neg = pay carry + crowded shorts = squeeze fuel; the ALT lesson)
- convergence perp×onchain → tier; operator-heat, catalyst, base-rate gates
- `cvd_spot_perp.py --json` emits the verdict for the engine; `perp_analyser` metrics now include price/turnover/fr_4h.

**What stays HUMAN (the engine can't code these):** Cat A/B classification, bait-fingerprint reads, news/X/user intel, structure-direction reads the engine doesn't see (liq-stack-below, breakdown triggers — e.g. the GENIUS fade-short composite needed liq-map + $0.60 break judgment), trade-management nuance (chronic re-squeeze = scalp), and WHEN to override the engine (with a stated reason).

**Honest scope note:** the CLAUDE.md line-count only dropped ~10% (most remaining content is judgment/why/reference, unsafe to delete — moving discipline to on-demand files = it won't fire). The real efficiency win is the BEHAVIORAL shift: run the engine once (deterministic) instead of hand-deriving every message. Tool registry → references/tooling.md (on-demand). Related: [[why-funding-negative]], [[output-format]], [[5-layer-pull-tooling-in-scripts]].
