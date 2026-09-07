# phase — Wyckoff cycle/event classifier (cycle-first breakout gate, SPEC-104)

```
python3 orchestrator.py phase '{"ticker":"OPN"}'
```

## Why it exists

Corpus rule (memory `feedback_wyckoff_cycle_first_breakout_gate`, source: @derrrrrrrq):
**the same breakout/breakdown candle is REAL in an accumulation/re-accumulation cycle and
BAIT (long-liquidity harvest) in a distribution cycle** — the cycle classification, not the
candle, decides tradability. The desk's entry triggers (§6) fire on candles (breakdown on
volume, lower-high, sweep-bounce) with no structural layer naming which Wyckoff cycle the
candle prints in; the stopped shorts in the ledger were candle-reactions inside the wrong
cycle (shorting a breakdown that was actually an engineered accumulation shakeout).

## Heuristics over ML — no black box

Every detector is a rule-based threshold check (`config/phase.json`), and every event
carries its evidence (bar index, volume ratio, range ratio) so the orchestrator can override
with a stated reason. `UNCLEAR` is a first-class honest answer — most bar windows don't
contain a clean Wyckoff sequence, and saying so is correct, not a fallback-of-shame.

## Event vocabulary (bottom-side / top-side mirror pairs)

| Event | Meaning |
|---|---|
| **SC** | Selling climax — climactic volume + range expansion, closing down. |
| **BC** | Buying climax — the top-side mirror. |
| **AR** | Automatic rally — the snap bounce (after SC) / pullback (after BC). Always tagged `fragile:true`: short-covering or smart-money absorption, never real demand. |
| **ST** | Secondary test — a retest of the climax extreme on MATERIALLY LOWER volume. |
| **SPRING** | Bottom-side range undercut that SNAPS BACK into the range within the reclaim window. Only valid in late accumulation (a prior SC→AR→ST chain already held the range) — **"small coins have no bottom"**: a falling-knife undercut with no held range is never labeled a spring, however clean the reclaim looks. |
| **UTAD** | Top-side mirror — a range-high sweep that FAILS back below the range, after a prior BC→AR→ST(top) chain. |

## Cycle vocabulary

`ACCUMULATION` / `RE-ACCUMULATION` / `DISTRIBUTION` / `RE-DISTRIBUTION` / `MARKUP` /
`MARKDOWN` / `UNCLEAR`.

- Bottom-side: `ACCUMULATION` once SC→AR→ST completes → `MARKUP` once a SPRING is followed
  by a confirmed impulse breakout above the range.
- Top-side: `DISTRIBUTION` once BC→AR→ST(top) completes, and **stays DISTRIBUTION through a
  UTAD + confirmed breakdown** (recorded as a `MARKDOWN_BREAK` event, but this v1 does not
  flip the cycle field to `MARKDOWN`) — a SHORT signal on that breakdown still gets a clean
  `cycle_gate` pass-through, since `DISTRIBUTION` is in the short-agree set.
- **Known limitations (v1, stated not hidden):** `RE-ACCUMULATION`/`RE-DISTRIBUTION` vs the
  plain forms is not distinguished (the corpus's core discipline — cycle-first, not
  candle-first — doesn't depend on that distinction); `MARKDOWN` is a valid enum value
  `cycle_gate_for` accepts but this v1's detectors never emit it; OI/liq inputs are accepted
  as a corroborating tag (`oi_corroborates` on SC/BC/UTAD evidence) but the core detectors
  are price/volume-only — a full OI/liq-substituted-for-volume detector chain (crypto-
  adapted Wyckoff per the corpus, for washed-volume names) is future work.

## Small-coin asymmetries baked in (from the corpus)

- Bottom SPRING requires the prior SC→AR→ST chain — enforced structurally
  (`_find_spring_or_utad` is only ever called once `st_b`/`st_t` exists).
- AR is always tagged `fragile:true` regardless of side.
- Both top-side and bottom-side detectors run every call (symmetric implementation) — but
  the desk's own short edge lives at tops (UTAD/top-ST), matching the corpus's stated
  priority; nothing in the code privileges one side over the other structurally, the
  priority is a reading discipline for the orchestrator, not a code gate.

## Grind-vs-impulse breakout flag

`classify_breakout_leg(bars, level)` — the "one fish eaten twice" fingerprint (memory
`reference_derq_freeland_corpus_playbook` §5): a breakout leg that grinds at the level
(≥`grind_dwell_bars` bars with closes within `grind_max_close_range_pct` of it, weak net
move) gets `grind_trap_suspect:true`; a clean few-bar impulse with a strong net move does
not.

## The gate — `cycle_gate_for(side, cycle)`

candle-signal (`side: "short"|"long"`) + cycle agree → pass-through (`agree:true`); disagree
(breakdown-short inside `ACCUMULATION`, breakout-long inside `DISTRIBUTION`) → an explicit
`CYCLE_CONFLICT` (`conflict:true`) — **a warning, not a hard veto** (§0.5 judgment stays with
the orchestrator, but the conflict must be impossible to miss). `UNCLEAR` never conflicts —
nothing confident enough to contradict the candle with.

Wired into `setup_score.py`: any setup whose legs include `breakdown_not_bought_back` or
`price_trigger` (i.e. a breakout/breakdown-triggered setup: `stage45_short`, `trap_long`/
`neg_funding_gate`, `defended_fade_short`/`_long`) gains a `cycle_gate` field when
`gather_signals` resolved a cycle — purely informational, never changes `score`/`verdict`.

## `brief` integration

`_phase_layer` runs as a 6th concurrent job in `build_brief` (same `LAYER_BUDGET` every
other layer respects — no new blocking latency). The one-liner
(`"phase: ACCUMULATION (last event: SPRING @14)"`) leads the headline, ahead of the
verdict/cluster-heat/liqs bits — "if the desk can't name the cycle, there is no breakout
trade." Degrades to `"phase: unavailable (<reason>)"` on thin/short history, never blocks.

## Tests

`tests/test_phase.py` — offline-deterministic, hand-constructed bar sequences (no network).
Covers all five DoD fixtures (SC→AR→ST→SPRING→MARKUP in order; BC→AR→ST→UTAD→DISTRIBUTION
with breakdown pass-through; breakdown inside ACCUMULATION → CYCLE_CONFLICT; falling-knife
with no held range → no SPRING label; grind-vs-impulse), plus the `brief`/`setup_score`
wiring.
