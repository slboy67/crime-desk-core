# setup_score — the §6 setup checklists as deterministic box-counters (SPEC 59)

```
python3 capabilities/setup_score.py '{"ticker":"BEAT","setup":"blowoff"}' --json
python3 capabilities/setup_score.py '{"ticker":"FOLKS"}' --json          # all setups
python3 capabilities/setup_score.py '{"setup":"catb_top","signals":{...}}' --json   # offline
```

The desk hand-counted "4 of 5 boxes" on every name this week (BEAT blowoff, FOLKS
Cat-B fade, PLAY trap-long, ID §4 gate). Counting boxes is mechanical; deciding what
to DO with the count is judgment. This capability does **only the counting** — scores,
never trade calls (no `direction`/`recommendation`/`action` field anywhere).

## The scorer

`score_setup(name, signals)` / `score_all(signals)` are **pure** over a flat normalized
`signals` snapshot — one value per leg input. The live CLI assembles that snapshot via
`gather_signals(ticker)` from the real capabilities (`price_structure`, `regime_flip`
`live_perp`, `oi_sides`, `tape`, `cvd`, on-chain). Pass `signals` directly (offline /
fixture replay) and no network is touched.

## Output per setup

```
{setup, score, required, legs:{name:{pass,value,source}}, vetoes:[], swing_vetoes:[],
 missing:[], add_triggers:[], verdict, tier}
```

Every leg names its **value** and its **source** — the Designer audits any leg in one
glance. `verdict`:

| verdict | `tier` | meaning |
|---|---|---|
| `VETOED` | — | a **hard** veto fired (oi_sides WASH, §5 deep-neg SHORT) — suppressed regardless of score |
| `ARMED` | `armed` | `score >= required` and no veto (the size-up / press tier) |
| `SCOUT` | `scout` | within `SCOUT_DELTA` (=1) of ARMED, OR an ARMED score demoted by a **swing** veto — scout-actionable now |
| `FORMING` | — | `0 < score < required − SCOUT_DELTA` (below threshold → stays quiet, no spam) |
| `ABSENT` | — | no leg passed |

For the **ALL-required** setups (`blowoff`, `trap_long`/`neg_funding_gate`) `required`
== number of legs, so `ARMED` means every box ticked.

### SCOUT tier (SPEC-82) — the near-armed ramp

The clean cascade rarely prints on operator-controlled squeezers (the full confluence is
engineered away by design), so the scanner used to stay silent through exactly the moves the
desk keeps missing. **SCOUT surfaces the near-armed state** (`score ≥ required − 1`): the
missing legs are reframed as **`add_triggers`** — what *confirms / sizes it up*, NOT entry
gates. Confluence is the AVOID-filter; a setup FORMING at a defended level is scout-actionable
now (defined-risk poke, §7 / $500 cap), and you size up into the adds.

- `add_triggers` = missing required legs + absent cascade `add_legs` (SPEC-83). Empty once
  VETOED/ABSENT (nothing to size into).
- `swing_vetoes` — a **swing-only** veto (chronic squeezer): a SWING short is vetoed but
  SCOUT/scalp is allowed ([[feedback_squeezer_scalp_not_no_trade]]), so an ARMED score is
  demoted to SCOUT and the squeezer name is NOT silenced. Distinct from the hard `vetoes`.
- SCOUT is **hypothesis-tier until n≥10** (§9) — ledger `tier:"scout"`, scored split from
  ARMED fills (SPEC-81 counterfactual scoring is how the base rate accrues).
- `grade_verdict(score, required, hard_vetoes, swing_vetoes)` is the pure grader.

## The checklists (§6)

| setup | required | legs | vetoes |
|---|---|---|---|
| `blowoff` | ALL (5) | parabolic (+50%/<48h) · ATH/window-high wicked ≥2% · lower-high · clean intraday break ≥1.5× vol **not re-bought** in 1-2 candles · OI off highs OR funding cooling | oi_sides **WASH**; squeeze-history **chronic** (>1 leg/10d over 60d) |
| `catb_top` | 4+ | ATH wick · lower-high · top L/S drop **15-25%** · OI peaked-and-rolled · volume declining · funding cooling | — |
| `trap_long` (= `neg_funding_gate`) | ALL (4) | multi-sigma-neg funding (floor-sentinel) · OI spiking **AND** oi_sides REAL not WASH · price trigger (magnet sweep-and-reclaim) · spot CVD diverging up (reliable only) | oi_sides **WASH** |
| `stage45_short` | 4+ | funding phase-match · sharp OI drop w/ L/S unfreezing · CEX deposits firing · lower-high · breakdown not bought back | — |

`trap_long` and `neg_funding_gate` are the same checklist under both names (§4 gate).

| `defended_fade_short` | ALL (3) | **wall_growing** (defended ask wall thickening while price tests it) · **empty_beyond** (vacuum above the wall — stop-above-empty) · **stall_lower_high** | oi_sides WASH · §5 deep-neg SHORT · **empty_beyond-false** (unsafe fade) · DEX-mark · liquidity-gate · chronic-squeezer (**swing-only**) |
| `defended_fade_long` | ALL (3) | wall_growing (bid floor thickening) · empty_beyond (vacuum below) · **stall_higher_low** | oi_sides WASH · empty_beyond-false · DEX-mark · liquidity-gate |

### defended_fade — the wall-fade entry (SPEC-83)

The size-able squeezer entry is **fading the defended extreme with a stop above empty
liquidity** ([[feedback_safe_short_is_stop_above_empty_liquidity]]) — short the rejection at a
*growing* ceiling wall when there's a vacuum beyond it (uneconomical for the operator to squeeze
into nothing). It **ARMS on the LEVEL read alone** — the cascade legs (`multi_sigma_neg`/funding-
flip, `breakdown_not_bought_back`, `cex_deposits_firing`) ride as **`add_legs` → `add_triggers`**
(the ADD, not the gate). Output adds:

- **`add_legs`** — cascade confirmations, evaluated but **not counted**; the absent ones flow into
  `add_triggers`, the present ones show `pass:true` (`confirmed adds`).
- **`geometry`** `{entry, stop, side, stop_buffer_pct}` — `entry` = the level (wall/floor price from
  `wall_price`); `stop` = `FADE_STOP_BUFFER_PCT` (=0.5%) **beyond** the wall, into the vacuum
  (above for a short, below for a long). Defined-risk poke (§7 / $500 cap).
- **Safety veto:** `empty_beyond:false` is a **hard** veto — a cluster beyond the wall means the
  stop would sit IN the squeeze fuel, so the fade isn't even scout-actionable.

**Wall-dynamics (the enabling data).** The book is a *snapshot* per call, so a thickening wall is
invisible to a single read. `record_wall_snapshot(ticker, side, mid, notional, ts)` persists a
bounded rolling history (`state/wall_history.json`, gitignored, like the nonce/accum baselines);
`derive_wall_growing(snapshots)` returns true only when the wall notional **rises strictly across
snapshots WHILE price stays within `WALL_HOLD_BAND_PCT` (=1.5%) of the level** (the wall is being
*tested*, not drifting away). `brief` records the snapshot each run and surfaces the
`defended_fade` read; `tape` reads the recorded history; `scan --scout` scores it across the
watchlist.

**Validation honesty (§9).** `defended_fade` is **NOT replayable from klines** (replay's cache has
no historical depth), so its base rate accrues **live** via SPEC-81 counterfactual scoring
(entry=level, stop=beyond-wall, walked forward). Until n≥10 it is **hypothesis-tier / scout-size
only**.

## Volume-climax confirmation (SPEC-74)

The top detectors (`blowoff`, `catb_top`, `stage45_short`) also carry a **`volume_climax`**
field — a §6 *confirmation* signal, **not a counted leg** (it never changes `score`/`required`,
so it cannot arm or block a short on its own). It codifies the climax-then-collapse top tell:

- **Relative tier (primary, micro-cap):** 4h Binance-futures quote-volume peak ≥ `VOL_CLIMAX_MULT`
  (=8×) the coin's trailing-7d-median baseline, evaluated **only while `parabolic`**, AND a
  **rollover** (latest 4h bar collapsed to ≤ `VOL_ROLLOVER_FRAC` =50% of the peak). The spike
  *and* the drop — not the peak alone — so it never fires mid-pump. BSB's 0.4222 top ran ~$6M
  baseline → $82M (~13.7×) → $4M; VELVET similar.
- **Absolute tier (second, MC-gated):** the raw $300-700M/4h (·$150-300M/1h·$50-100M/15m) topping
  bands only apply when MC/OI ≥ `ABS_MC_GATE_USD` (=$150M) — large enough to plausibly reach them.
  These bands **never gate a micro-cap** (they're calibrated ~5-10× too high for the desk's
  low-float names); micro-caps use the relative tier only.

`volume_climax` output: `{fires, tier:"relative"|"absolute"|null, ratio, rollover, parabolic,
relative_fires, absolute_eligible, absolute_fires, values, thresholds, source}`. n=2 (BSB,
VELVET) when filed — below the §9 base-rate gate, so it enriches the *read*, it does not authorize
sizing on volume alone.

## This week's hand counts (the test fixtures)

| name | setup | result |
|---|---|---|
| BEAT 06-11 | blowoff | **4/5 FORMING** — missing the un-re-bought break (it re-bought within a candle) |
| PLAY | trap_long | **VETOED** — oi_sides WASH |
| ID 06-10 | neg_funding_gate | **2/4 FORMING** — missing price trigger + spot CVD |
| FOLKS 06-12 | catb_top | **≥4 ARMED** — retest fail |

Read-only. Scores only. Judgment (what to do with the score) stays with the Designer
(ARCHITECTURE §0.3 / invariant 2).
