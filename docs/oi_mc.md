# oi_mc — OI/MC ratio as a "perp-casino" flag (SPEC-122)

```
python3 orchestrator.py oi_mc '{"ticker":"EVAA","oi_usd":50000000,"mc_usd":19841270,"direction":"SHORT"}'
python3 orchestrator.py oi_mc '{"ticker":"EVAA","oi_usd":50000000,"direction":"SHORT"}'   # mc fetched live
```

## Why it exists

The ledger (SPEC-40) shows the desk's single worst signature is **mindshare/blowoff-top
short: n=324, hit 32%, total −26.3R.** These are vertical-candle perp pumps on low-MC
names where the game is entirely in the derivatives — shorts get chopped on the
vertical, longs get caught in the OI unwind. The desk had no single metric that named
this profile on sight, so these names keep *looking* like setups (cinema, squeezy
verticals) and keep costing R.

The tell, surfaced live 2026-07-10 (EVAA, via CT analyst DoubleEdge): **OI/MC ratio.**
EVAA ran at **252% OI/MC** — open interest 2.5× the entire market cap = there's almost
no float, the whole move is a perp game. That one ratio instantly separates a
perp-manipulation casino (avoid) from a name with a real on-chain float (tradeable).

## Contract

`compute_ratio(oi_usd, mc_usd)` → `oi_usd / mc_usd`, or `None` when either input is
missing/non-positive (§3: a missing input is a data failure, never a fabricated ratio).

`flag_for_ratio(ratio, cfg=None)` → tiered flag, thresholds documented + overridable in
`config/oi_mc.json` (`perp_heavy_min` default 0.5, `perp_casino_min` default 1.5):

- **`normal`** (`< 0.5`) — on-chain float dominates; on-chain reads meaningful.
- **`PERP_HEAVY`** (`0.5–1.5`) — derivatives large vs float; weight perp signals,
  discount on-chain.
- **`PERP_CASINO`** (`> 1.5`) — OI dominates the entire market cap; vertical/chop/unwind
  profile — the desk's −26R zone.
- **`null`** — `ratio` is `None` (missing input); never a false `normal`.

`caveat_for(ratio, flag, direction, liq_verdict=None)` — the explicit short-discipline
caveat (req 3). Fires **only** on `PERP_CASINO` + a `SHORT`-direction row:

> ⚠ PERP_CASINO (OI/MC 252%) — blowoff-short is the −26R signature, timing-bet only,
> not a setup

It does **not** hard-block — an operator-timed OI-unwind short can still work — it just
renders so the desk never takes one of these blind (memory:
`feedback_lead_with_the_disqualifier_dont_make_user_extract_it` — EVAA was the case).
When `liqs`' `oi_liq_consistency` verdict is also `SUSPECT_FAKE`, the caveat appends a
cross-reference — the OI itself may be faked, not just heavy.

`build_oi_mc(oi_usd, mc_usd, direction=None, liq_verdict=None, cfg=None)` → the full
envelope: `{oi_usd, mc_usd, oi_mc_ratio, oi_mc_flag, oi_mc_caveat}`.

`fetch_market_cap(ticker, cg_id=None)` — live circulating MC (USD) via
`pull5.coingecko_layer` (the same canonical CoinGecko source `unlocks.py` sizes off,
never a single-chain on-chain `total_supply`). `None` on any resolution/fetch failure.

## Integration

Every call site reuses the **primary-venue OI each capability already resolves** — this
spec adds no second OI fetch, only the MC lookup:

- **`brief`** — `_perp_layer` computes `oi_usd = live["oi"] * live["price"]` off the
  SPEC-71 shared `live_perp` resolution (the same cross-venue OI the funding/squeeze
  signals already read), fetches MC, and adds `oi_mc_ratio` / `oi_mc_flag` /
  `oi_mc_caveat` to the perp layer.
- **`triage`** — `venue_pull` captures `sumOpenInterestValue` (already USD-denominated)
  off the Binance `openInterestHist` call it already makes — no extra OI fetch at all.
  `_build_row` adds the three fields to the row contract.
- **`classify`** — `annotate_oi_mc(rows, fetch_mc=None)` is a board-annotate pass (same
  shape as `annotate_cluster_heat`/`annotate_unlocks`) run in `main()` after
  `annotate_cluster_heat`; it reads `r["live"]["oi"]`/`["price"]`, fetches MC, and sets
  the three fields on every row (JSON output included).

Every wiring point degrades the whole block to `null`s on any failure (dead MC source,
missing `live` data, network exception) — this is a read-only annotation, never a crash
and never a verdict override (§0.5/§3).

## Tests

`tests/test_oi_mc.py` — offline-deterministic (CoinGecko fetch monkeypatched, no
network): the four DoD fixtures (EVAA-shaped → `PERP_CASINO` + caveat, normal `0.3×` →
`normal`, mid `0.8×` → `PERP_HEAVY`, MC-missing → `null`/no false flag), threshold
boundaries, the `SUSPECT_FAKE` cross-reference, and a regression check that existing
brief/triage/classify contract keys survive byte-identical alongside the new fields.

---

## SPEC-177 — battlefield verdict + leverage_state

The Cartel framework's primary classifier — is this name's game on the perp or the
spot — was not computed anywhere: no 24h perp/spot volume ratio, no `perp_led/spot_led`
verdict, and no join of ΔOI to range-hold (the leverage-entering/leaving read).

### `battlefield_verdict(perp_vol_24h, spot_vol_24h, oi_mc_flag, cfg=None) -> (ratio, verdict)`

`verdict ∈ perp_led | spot_led | mixed | UNKNOWN`, **asymmetric** (never symmetric
threshold logic — a missing spot leg is not the same failure mode as a missing perp
leg):

- **`perp_led`** if `ratio ≥ perp_led_min` (default 4.0) **OR** `oi_mc_flag ∈
  {PERP_HEAVY, PERP_CASINO}` — including when the spot leg is `None` (no real spot
  venue anywhere IS the perp_led condition, not a missing-input degrade).
- **`spot_led`** only when **both** legs are present and low: `ratio ≤ spot_led_max`
  (default 2.0) **AND** `oi_mc_flag == "normal"`.
- **`mixed`** — between, or a present ratio with an unknown/absent `oi_mc_flag` (a
  conservative degrade: never asserts `spot_led` without the OI/MC corroboration).
- **`UNKNOWN`** only when `ratio is None AND oi_mc_flag is None` (both legs
  unknowable).

`perp_vol_24h` = 24h perp volume in USD (any venue/sum — callers pick their own
reuse, see Integration below). `spot_vol_24h` = `cvd.resolve_spot_venue`'s 24h USD
(req 5 — the REAL spot venue, never Binance-assumed).

### `leverage_state_for_window(delta_oi_pct, range_held, oi_sides_tag, threshold_pct, window_label) -> dict`

One window's verdict — `{state, window, delta_oi_pct, range_held, reason}`.
`state ∈`:

| State | Condition |
|---|---|
| `WASH_PINNED` | `oi_sides_tag == "WASH"` — overrides every other read (checked first, even with `range_held=None`) |
| `RESET_CONSTRUCTIVE` | `ΔOI ≤ −threshold_pct` AND `range_held` |
| `MOVE_DONE` | `ΔOI ≤ −threshold_pct` AND NOT `range_held` |
| `LOADING` | `ΔOI ≥ +threshold_pct` AND `range_held` |
| `TREND_FEEDING` | `ΔOI ≥ +threshold_pct` AND NOT `range_held` |
| `None` (no verdict) | `abs(ΔOI) < threshold_pct` — sub-threshold, not a fabricated read |
| `UNKNOWN` | `delta_oi_pct` or `range_held` missing, with a named `reason` |

`build_battlefield(...)` computes **both** windows (`leverage_4h`/`leverage_48h`,
significance floors 8%/15% by default, `config/oi_mc.json`
`delta_oi_sig_4h`/`delta_oi_sig_48h`) and emits framing-only `annotations` — never a
veto/gate:
- `TREND_FEEDING` in either window → "chasing the first spike is trash — the
  reset+next-OI-expansion is the trade."
- `RESET_CONSTRUCTIVE` in either window on a `perp_led` name → "the next-OI-expansion
  is the entry, not the reset itself."

`_range_held(closes, swing_point, direction)` — **closes only**; wick-throughs never
break hold (a name can wick through its swing point intrabar and still hold on every
close). `direction="long"` treats `swing_point` as a swing LOW (a close below breaks
hold); `"short"` treats it as a swing HIGH (a close above breaks hold).

`commit_time_annotation(battlefield, entry_kind)` — the req-4-bullet-1 commit-time
flag: `entry_kind == "spot_level_retest"` on a `perp_led` name → `"spot-natured entry
on perp-led name"`. Pure helper; **not yet wired into the thesis-commit flow** (scope
note below).

`fetch_leverage_window(ticker, period, kline_interval, limit, swing_point, direction)`
— the one I/O helper: Binance `futures/data/openInterestHist` (ΔOI%) + `klines`
(closes, range-hold), same endpoint `triage.py` already uses for its own 24h ΔOI read.
Best-effort — every field degrades to `None` on any failure, never raises.

### Integration (per call site — `perp_spot_ratio`/`battlefield`/`leverage_state` added alongside `oi_mc_*`)

- **`classify`** `annotate_oi_mc` — perp leg reuses `live["vol_m"]` (zero new fetch,
  board scope). Spot leg is **not** fetched per-row on a board sweep (cost: a
  per-ticker spot-venue resolution on every board tick); `battlefield` still resolves
  `perp_led` correctly via the heavy-OI/MC-flag branch when applicable.
  `leverage_state` stays `UNKNOWN` (no OI-history source at board scope).
- **`brief`** `_perp_layer` — perp leg reuses `live["vol_m"]`; spot leg reuses
  `build_analyse`'s own `spot_vol_24h_usd` (`cvd.resolve_spot_venue`, req 5) — zero new
  fetch for the ratio on a single-ticker deep read. `leverage_state` stays `UNKNOWN`.
- **`triage`** `venue_pull`/`_build_row` — perp leg reuses `qvol24` (Binance, already
  fetched); spot leg is fetched fresh (`cvd.resolve_spot_venue`, req 5 — the one
  genuinely new per-ticker call, consistent with `venue_pull`'s existing fetch-heavy
  per-ticker design). `leverage_state` stays `UNKNOWN`.
- **`analyse`** `converge`/`build_analyse` — the §4 gate note names the battlefield
  verdict (req 3) so a `perp_led` name can't present a spot-natured read. Perp leg
  reuses `perp_analyser`'s own `metrics.turnover`; spot leg reuses `cvd`'s
  `spot_vol_24h_usd`; **`leverage_4h` reuses `oi_sides`' own WASH tag + 4h ΔOI**
  (`oi["verdict"]`/`oi["oi_change_pct"]`, already fetched every call) — a genuine WASH
  tag resolves to `WASH_PINNED` live; without one, `range_held` is unavailable at this
  call site (no swing-point source) so it degrades to `UNKNOWN`.
- **`tape`** — prints `leverage_state` (req 3), computed from `oi_series`/`bars` this
  call already fetched (zero new fetch). This is **tape's own fine-grained window**
  (`window_min`, default 60), not the board's 4h/48h convention — `level` (an existing
  tape param) doubles as the swing point when given.

### Scope note — leverage_state's live windows are mostly `UNKNOWN` today

The pure `leverage_state_for_window`/`_range_held`/`build_battlefield` machinery and
the `fetch_leverage_window` I/O helper are fully built and tested against every
acceptance-criteria fixture. Live wiring is deliberately conservative: none of
`classify`/`brief`/`triage` fetch a genuine ΔOI-history + swing-point series today (that
would mean 4 new HTTP calls **per row** on a board sweep, or per ticker on `brief`) — a
real 4h/48h leverage_state needs an OI-history store, which is SPEC-178's sampler. Two
places DO get a live, non-fabricated read today: `analyse`'s `leverage_4h` (via
`oi_sides`' own WASH tag — a genuine `WASH_PINNED` fires live) and `tape`'s own-window
read (via its own already-fetched `oi_series` + an optional `level`). Wiring the
remaining call sites is a natural follow-on once SPEC-178 lands.
