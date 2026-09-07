# oi_sampler — desk-run OI sampler (ops job + JSONL store, SPEC-178)

## Why it exists

Bitget/MEXC/KuCoin/Aster/Hyperliquid/Lighter publish **no keyless OI history** — only a
current snapshot (`.scratch/oi-construction/research/R1-*.md` S3 table). Binance's own
history (`futures/data/openInterestHist`) silently caps at ~30 days. OI-elasticity
(the arb-vs-directional decomposition — CLAUDE.md §0.6 item 3, "how OI is
CONSTRUCTED") is blind exactly where Cat-A OI lives until the desk samples and stores
OI itself.

```
python3 ops/oi_sampler.py tick --json
```

## One tick

For each symbol in the board-driven universe, call `venue_map.build_venue_map(ticker)`
and append one row per venue to `state/oi_samples/<SYM>.jsonl`. **Zero new fetch
code** — the sampler IS venue_map + append, inheriting its `ok`/`not_listed`/`error`
discipline and per-venue timeouts (SPEC-129/176). `venue_map`'s `oi_raw`/`mark_price`
additive fields (SPEC-178 prep, `capabilities/venue_map.py`) are what make the raw
series possible — every probe now surfaces the base-asset-unit OI/mark price it
already parses internally, alongside the USD conversion, rather than discarding it.

## Universe (`build_universe`)

Board-driven, capped at `UNIVERSE_CAP` (30) — the union of, recomputed **every tick**
(never a persisted list):
1. `config/watchlist.json` tickers carrying a thesis whose `status` is **not**
   terminal (`RETIRED`/`CLOSED`) — a token with no `thesis` block at all is excluded
   (nothing to sample for).
2. `config/positions.json` open (`positions[]`, not `closed[]`) tickers.
3. The most recent faded-bounce sweep's candidates (`state/faded_bounce_latest.json`
   → `data.candidates[].ticker`) — missing file degrades to an empty tier, not an
   error (§3: the sweep may simply not have run yet).

Over-cap enforcement order: **live theses > positions > candidates**. A ticker
appearing in more than one tier counts once, at its highest-priority slot, so a
live-thesis name is never pushed out of the cap by a lower-priority duplicate.

## Store

Append-only JSONL, `state/oi_samples/<SYM>.jsonl` (gitignored with the rest of
`state/`). One row per venue per tick:

```json
{"ts": 1788192619, "venue": "binance", "status": "ok",
 "oi_raw": 12655102.0, "oi_usd": 12707451.98, "mark_price": 1.00413667,
 "funding_pi_4h": 0.0118, "vol24h_usd": 2954788.27}
```

- **Raw AND USD, both** — a venue's contract-size/multiplier redenomination breaks a
  raw-unit series in a way a USD-only series hides; USD-only also hides a real OI
  move painted over by a mark-price move. `oi_raw`/`mark_price` are only present when
  the venue's own response actually carries a base-asset-unit figure (never
  re-derived by dividing `oi_usd`/`mark_price` — that would fabricate precision the
  venue never published; genuinely absent on Extended/Vest/BingX today).
- **Errors are explicit rows**, never a silent absence: `{"ts":…, "venue":…,
  "status":"error", "reason":…}`. A missing timestamp (the tick simply didn't run,
  or a symbol wasn't in the universe that cycle) is a **GAP** — this module never
  interpolates or backfills a row for a tick it didn't execute; consumers must treat
  a gap as missing data, never as flat OI.
- **Redenomination marker** (`_redenomination_marker`): a step of `≥3×` (or `≤1/3×`,
  `REDENOM_STEP_TOLERANCE`) in the `oi_usd/oi_raw` ratio versus the venue's own last
  `status:"ok"` row writes `{"ts":…, "venue":…, "status":"redenomination",
  "prev_ratio":…, "cur_ratio":…, "step":…}` instead of silently corrupting the raw
  series — a consumer must split the series at that marker, never read a fake OI
  cliff across it. The first-ever row for a (symbol, venue) never fires one (nothing
  to compare against yet).

## Cadence

Own 15-minute launchd job, `ops/com.crimedesk.oi-sampler.plist`
(`StartInterval 900`), picked up automatically by `ops/install_launchd.sh`'s
`com.crimedesk.*.plist` glob — no code change needed there. **Deliberately NOT**
folded into `funding_surveil` (a paging monitor must not die on an append-layer
throw) or `discovery_tick` (12h-cadence legs, far too coarse for OI-elasticity
sampling).

## Retention

90-day rolling window (`RETENTION_DAYS`), pruned by the sampler itself — weekly,
marker-file-gated (`state/.oi_sampler_prune_last`) exactly like `discovery_tick.sh`'s
`due()` pattern (`maybe_prune`/`_prune_due`). Pruning rewrites each symbol's file only
when something actually aged out, and always leaves the file valid JSONL (a
malformed line encountered while pruning is dropped, never propagated).

## Universe/consumer discipline (module docstring + here, per req 6)

- A missing timestamp in a symbol's series is a **GAP**, never flat OI.
- A `status:"redenomination"` row means: **split the series there.** Reading straight
  through it treats a unit-convention change as a real OI cliff or spike.
- `status:"error"`/`"not_listed"` rows carry no `oi_usd`/`oi_raw` — a consumer summing
  or diffing OI must filter to `status:"ok"` rows only.

## Tests

`tests/test_oi_sampler.py` — offline-deterministic (`venue_map_fn` injected on every
call, tmp store/config paths per test): two-tick per-venue-row coverage, timed-out/
not-listed venues never fabricate a datum, the 1000× redenomination-step DoD fixture,
prune-keeps-newer/still-valid-JSONL, universe cap-enforcement priority order, and the
plist's existence/`plutil -lint`/`StartInterval`/`install_launchd.sh` glob wiring.
