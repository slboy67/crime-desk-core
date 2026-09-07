# counterfactual — score the desk's OWN committed calls (SPEC 81)

```
python3 capabilities/counterfactual.py '{"action":"backfill"}' --json
```

The §9 base-rate gate is meant to validate the desk's **actual judgment calls** — but the
live ledger can't feed it. A retire records only *that* a thesis closed, never *what price did
afterward*, so **20 of 23 live rows are `retired_unfilled` with `pnl_r: null`** — unscoreable.
`replay` (SPEC 62) backtests the deterministic **scorers** over price history (a *different*
population — it re-derives its own entries from klines); it does **not** evaluate the
entry/stop/TP the desk actually **committed**. Today the `live` bucket has ~1 scoreable row.

This capability closes that gap: walk forward klines from `commit_ts` through the **committed**
`entry_zone`/`stop`/`tp` geometry and record what *would* have happened — so each desk call
becomes a scoreable datum **without anyone having had to trade it**.

## What it computes (`score_counterfactual(thesis, bars)`)

Causal order, on 1h bars (the desk's native bar) ordered forward from `commit_ts`:

- **never filled** within the horizon (`time_stop_h` bars, else the whole window) → outcome
  stays `retired_unfilled` but carries a `counterfactual` block: `reason: "never_filled"`,
  the latched `entry_mode`, and the **MFE/MAE** the unfilled thesis would have seen (in R,
  relative to the committed entry edge). This is the *"correctly stood aside"* vs *"missed a
  runner"* distinction — both are signal quality.
- **filled** (price traded into the zone) → simulate from the fill bar with `stop`/`tp` and
  record the real outcome (`tp1`/`tp2`/`stopped`/`time_stop→mtm`) + `pnl_r` + `bars_held` +
  `mae_r`. Reuses **`replay.simulate_trade`** (STOP-checked-first conservatism — **no second
  simulator**); entry executes at the zone edge and simulation starts the bar *after* fill, so
  a pre-fill wick can't charge a phantom stop.

### The fill rule is shared with the board (SPEC-79 / SPEC-147)

Entry (whether it filled, at what price) comes from **`thesis.fill_of`** — the SAME rule
`classify.eval_price_leg` uses for the board's TRIGGERS/BREAKS verdict. Before SPEC-147
this module had its own private `_infer_mode`, which could (and did) disagree with the
board about the fill price for the same committed thesis — R is computed from that price,
so the two could agree a thesis filled and still disagree about its R. See `docs/thesis.md`
("Library surface: `fill_of`") for the full rule; the short version:

A SHORT's `entry_zone` is entered by one of two intents, **latched at commit** from commit
price vs zone (or an explicit thesis `entry_mode`):

| intent | zone vs commit price | fills when | entry edge (source=committed) |
|---|---|---|---|
| **breakdown** (sell-the-breakdown) | zone **below** spot | price trades **down** into it | zone **top** |
| **fade** (fade-the-rally) | zone **above** spot | price trades **up** into it | zone **bottom** |

Symmetric for LONG (buy-the-dip / buy-the-breakout). Latched once so a later rally/selloff
can't flip the interpretation. **But** when the mode is *inferred* (no explicit thesis
`entry_mode`) rather than committed, the edge above is a guess — so the fill charges the
**WORSE** edge for the position's direction instead (SHORT → zone low, LONG → zone high),
never the flattering one. The `counterfactual` block records both `entry_mode` (breakdown/
fade/immediate) and `fill_source` (`committed`/`inferred`) so a reader can tell which
happened.

## Source tagging — a third bucket

Every row is tagged **`source: "counterfactual"`** — never silently merged with `live` (a
hand-traded fill) or `replay` (a scorer re-derivation). `ledger._by_source` already splits it;
the §9 gate reads it as its own population and `sizeable` is judged on **live rows only**, so a
counterfactual never green-lights size. The row carries the **same `signature`** as the desk's
committed call (so it lands in that call's §9 bucket).

## `backfill` (CLI) + auto-wire

- **`backfill`** scores **every** `pnl_r: null` **live** row that has committed geometry.
  **SPEC-162: the ledger row's own inline `geometry` snapshot (written by
  `ledger.commit_open`) is read FIRST** (`_geometry_from_row`) — the watchlist join
  (ticker + `commit_ts` against `config/watchlist.json`'s tokens + retired blocks) is now
  the LEGACY fallback, used only when a row carries no snapshot. This is what makes
  scoring immune to a later board rewrite/re-anchor/retirement: the row no longer needs
  the watchlist to still hold the same thesis at the same timestamp. **Idempotent** — a
  (ticker, commit_ts) already scored as a counterfactual is `skipped: already_scored`.
  `discretionary` / WATCH rows with `entry_zone: null` (and no scoreable snapshot) are
  `skipped: no_geometry` (they were never machine-watchable — the SPEC-77 gap; see
  `ledger.md`'s `commit_open` for how a paramless commit now stays a visible,
  `unscoreable: true` fact rather than vanishing outright). Reports
  `{scored, skipped{...}, total_live_null}`.
- **Where the `pnl_r: null` live rows come from now:** `ledger.sweep_open_commits` (wired
  into `ops/board_tick.py`'s tick, every ~15 min) — see [ledger.md](ledger.md)'s
  `commit_open`/`sweep_open_commits` section. Before SPEC-162, only RETIRED/PASS theses
  ever got a row (`ledger.backfill`'s own, older sweep); an actively-committed thesis had
  none and was invisible to this scorer.
- **auto-wire**: `thesis` close/retire calls `score_on_close` so the live record self-populates
  going forward. It is **offline/cache-only** (reads `state/replay/<SYM>.json`; **never** a
  network read on the close path) and fully guarded — a dead read can never slow or break a
  close (degrades to `skip: no_klines`).

## `rescore` (CLI) — re-score existing rows under the unified fill rule (SPEC-147)

```
python3 capabilities/counterfactual.py '{"action":"rescore"}' --json
```

Every `source: "counterfactual"` row already in the ledger was scored under whatever fill
rule was live at the time — SPEC-147 unified that rule (see above), so a pre-147 row can
disagree with a fresh one about the same thesis's pnl_r. `rescore` re-derives every
counterfactual row's fill/outcome/pnl_r from its committed geometry (preferring the thesis
still in `config/watchlist.json`, joined on ticker+`commit_ts`; falling back to the
geometry the row itself already recorded when the thesis has since been pruned) against
the cached `state/replay/<SYM>.json` klines — **offline, no network** on a rewrite. Each
re-scored row is stamped `rescored_ts` + `rescored_from` (the prior `pnl_r`). `live` /
`backfill` / `replay` rows are never touched. A row with no cached klines is left
byte-identical and counted in `skipped.no_klines` — never silently dropped.

Re-derives every counterfactual row on every run (not add-only/idempotent in the usual
sense — the fill rule itself is what can change between runs); re-running is safe and
simply re-stamps `rescored_ts`.

`ledger.stats()` flags a **partially-rescored** counterfactual population rather than
silently blending it with a fully-rescored one: both the per-signature row and the
top-level `summary` carry `counterfactual_unrescored_n` / `counterfactual_fully_rescored`
(vacuously `True`/`0` when there are no counterfactual rows at all).

## Data source — no Moralis (G2)

Price/funding/OI only, so it runs while the Moralis quota is dead. `backfill`'s default provider
reads the `state/replay/*.json` kline cache when present, else fetches 1h klines from Binance
from `commit_ts` forward. The pure scorer takes bars directly — the gating tests drive it on
synthetic bars, **no network**.

Tests: `tests/test_counterfactual.py` (scorer + backfill on synthetic bars/fixtures — offline),
`tests/test_thesis_fill.py` (the shared `fill_of` rule + classify/counterfactual agreement),
`tests/test_rescore_counterfactual.py` (the `rescore` pass + the `ledger.stats` rescore-gap
warning).
