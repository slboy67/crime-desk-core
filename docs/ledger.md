# ledger — per-signature outcome scoreboard (SPEC 40)

Makes the §9 base-rate gate enforceable: **don't size on a signature until n≥10
historical calls with hit% >50 and positive edge.** Every closed/retired thesis is
one JSONL record in `state/ledger.jsonl`; `stats` computes the gate per signature.

**SPEC-150: this is the DESK's report card, never the user's P&L.** The user trades
outside the desk too ("I take trades outside of what the desk tells me to do") and
those are deliberately NOT recorded here — every row defaults `scope: "desk"`; an
`off_desk` row (if one is ever written) is excluded from every aggregate unless
`include_off_desk: true` is passed explicitly. Read `stats` as "how has the desk's own
calling done", never as "how has the user done".

## record — one call per thesis retire

Every §0.5 BREAKS that retires a thesis ends with:

```
python3 orchestrator.py ledger '{"action":"record","ticker":"VELVET","direction":"SHORT",
  "signature":"stage5_short","outcome":"stopped","pnl_r":-1.0,
  "commit_ts":"2026-06-08","close_ts":"2026-06-09","notes":"stop 0.40 printed 0.4749"}'
```

- `signature` — enum: `trap_formation_long` · `mindshare_top_short` ·
  `blowoff_top_short` · `stage5_short` · `unlock_cliff_fade` (SPEC-110, §6 unlock-cliff
  fade) · `spring_reclaim_long` (SPEC-110, §6 BASED-type post-blowoff spring) ·
  `discretionary`. Free-form setup names (watchlist `setup` strings) AND the
  **SPEC-59 setup keys** are canonicalized (`stage5*`/`stage4*`/`distribution`/
  `markdown`/`squeezer`/`*breakdown_short` → stage5_short, `*blowoff*` →
  blowoff_top_short, `catb_top`/`mindshare` → mindshare_top_short, `neg_funding`/
  `trap`/`squeeze`/`accumulation` → trap_formation_long, `unlock_cliff*` →
  unlock_cliff_fade, `spring_reclaim*` → spring_reclaim_long, unknown → discretionary).
- **SPEC-110: an explicitly-typed `--signature` fails loudly on an unknown name.** The
  CLI's `record` action routes a given `--signature` through `record_cli_signature`, NOT
  the bare `canon_signature` catch-all: a name that resolves via a real heuristic bucket
  (including an exact enum member) still works as before; a name that falls through
  every heuristic is a typo or a genuinely new setup, and silently rebucketing it to
  `discretionary` corrupts the §9 base-rate record (exactly what happened to the
  `unlock_cliff_fade` win before this taxonomy existed). It now raises
  (`unknown signature 'X'; known: [...] or pass --allow-new`) unless `--allow-new` is
  passed. Free-text auto-classification elsewhere (replay, thesis-close, backfill) is
  unaffected — it still resolves through `canon_signature` directly and keeps the legacy
  always-resolves-to-something behavior.
- **SPEC-172: `--allow-new` means VERBATIM — no heuristic pass at all, not even a real-
  bucket match.** Before this fix, `record_cli_signature` ran `_raw_canon_strict` FIRST
  regardless of `--allow-new`, so `--signature squeeze_exhaust_watch --allow-new` still
  matched the `squeeze` substring and silently persisted `trap_formation_long` — the
  desk's best LONG signature's base-rate corrupted by a geometry-less WATCH row. Now
  `--allow-new` short-circuits straight to `record(..., signature=<raw, untouched>,
  allow_new=True)`, bypassing `canon_signature` entirely.
- **SPEC-172: a `*_watch` signature (or `direction: "WATCH"`) is NEVER heuristically
  mapped, allow-new or not.** Watch-tier theses are signal-only tripwire coverage, never
  a trade — corrupting a trade signature's n_commits/total_r with one is worse than a
  typo'd `discretionary`, because it inflates a real edge's apparent sample size. `stats`
  splits these rows out FIRST (before any per-signature aggregate, `n_commits` included)
  into a `watch` bucket (`{signature: {n, tickers}}`, no hit%/R — not meaningful on
  signal-only rows); `total_records` still counts them.
- **SPEC-172: `ledger.py migrate-watch [--apply] [--wl-path P]`** — scans the ledger for
  rows with `direction: "WATCH"` whose stored signature is a real trade signature (the
  pre-fix bug's fingerprint) and re-tags them from the matching watchlist entry's
  `thesis.signature`. Dry-run by default (prints the `mistagged` list, writes nothing);
  `--apply` rewrites `state/ledger.jsonl` in place. A row with no matching/no-signature
  watchlist entry is left alone (nothing to re-tag it TO).
- One-off: `ledger.py migrate-spec110` re-tags the M record (ticker M, commit_ts
  2026-07-03, `discretionary`) to `unlock_cliff_fade` — hand-enumerated retag targets
  (`SPEC110_RETAG_TARGETS`), idempotent.
- **Direction guard (SPEC-90):** when `direction` is known, `canon_signature` NEVER
  buckets a trade onto the opposite side — a SHORT can only land in a `*_short`
  signature, a LONG only in `trap_formation_long`. A cross-direction free-form
  (e.g. `distributing_squeezer_breakdown_short`, whose `squeezer` substring once hit
  the LONG `squeeze` branch and credited a SHORT win to `trap_formation_long`) falls
  back to the correct-side default (SHORT → `stage5_short`, LONG →
  `trap_formation_long`), never the wrong side. Direction is threaded from `record`,
  `backfill`, and thesis-close (`_resolve_signature`); query-time `stats` lookups pass
  no direction and keep the legacy pure-name resolution.
- `outcome` — `stopped` | `tp1` | `tp2` | `retired_unfilled` | `zone_blown` |
  `closed_manual` (SPEC-69: user discretionary close while every kill line was intact —
  NOT a stop-out).
- `pnl_r` — realized R multiple; `null` for signal-only calls.
- `source` — `live` (unset) | `replay` (SPEC 62 backtest) | `backfill` (seed). Kept on the
  row so `stats` never silently mixes a backtest or a seed with booked live R.
- `scope` (SPEC-150) — `desk` (default) | `off_desk`. A row with no `scope` field
  (pre-SPEC-150) counts as `desk` — nothing already recorded silently drops out.
- `desk_disagreed` (SPEC-150) — bool, default `false`. `true` marks a trade the user
  took that the desk would NOT have called ("I would have shorted them anyway and I
  need the desk to agree") — the field that makes the override population measurable.

## stats — the §9 gate as a query

```
python3 orchestrator.py ledger '{"action":"stats"}'                      # all signatures
python3 orchestrator.py ledger '{"action":"stats","signature":"stage5_short"}'  # one row + records
python3 orchestrator.py ledger '{"action":"stats","include_replay":true}'  # + replay aggregate
python3 orchestrator.py ledger '{"action":"stats","include_off_desk":true}'  # + off_desk rows
```

Per signature: `{n_commits, n, n_filled, fill_rate, hit_pct, avg_r, total_r, by_source,
last_5, sizeable[, replay], counterfactual_unrescored_n, counterfactual_fully_rescored}`
(`n` is kept as an `n_commits` alias for older callers). Every aggregate — per-signature
and top-level — is **desk-scoped by default** (`scope: "desk"` rows only); pass
`include_off_desk: true` to fold in `off_desk` rows too (never the default).

Top level also carries:

```
summary: {
  live_n_filled, live_total_r,                    # desk-scoped, across every signature
  desk_disagreed_split: {
    agreed:    {n_filled, hit_pct, avg_r, total_r},   # desk_disagreed=false population
    disagreed: {n_filled, hit_pct, avg_r, total_r},   # desk_disagreed=true population
    rules_change,   # bool — true only once disagreed n_filled>=10 AND its avg_r beats
                    # agreed's — the number that settles the desk-vs-override argument
  },
  desk_checkpoint,            # "HALT_CALLS" | null
  desk_checkpoint_progress: {n_filled, threshold, n_remaining},
  earned_signatures,          # [sig, ...] — live filled n>=1 AND total_r>0 (SPEC-149 tier input)
  counterfactual_unrescored_n, counterfactual_fully_rescored,
}
```

**SPEC-150: `desk_checkpoint` replaces the old kill-switch.** The prior "checkpoint at
filled n=30" was computed on a subset of the user's trading (desk-recorded rows only)
and could never validly gate HIS trading. It now gates the DESK instead: at
desk-originated **filled** n=`DESK_CHECKPOINT_N` (30), if no signature has printed GO
(`sizeable`) and cumulative desk-scoped filled R is negative, `desk_checkpoint` reads
`"HALT_CALLS"` — the desk drops to tracking + veto only, the user keeps trading
unsupported. Below 30, `desk_checkpoint` is `null` and `desk_checkpoint_progress`
reports where the count stands so the halt is never a surprise. `off_desk` rows never
count toward it (excluded by the default `include_off_desk=False` scope filter).

**`earned_signatures`** is the ledger's earned set that SPEC-149's board `tier` field
derives `tradeable` from (`classify.annotate_tier`/`classify.earned_signatures()`) — a
signature with zero fills or a non-positive `total_r` never appears in it.

**SPEC-147: `counterfactual_fully_rescored` — never silently blend two fill rules.**
`source: "counterfactual"` rows (see `docs/counterfactual.md`) are scored by
`counterfactual.score_counterfactual`, which can be re-derived under a newer fill rule
via `counterfactual.py '{"action":"rescore"}'`. A row a `rescore` pass hasn't reached yet
(no cached klines, or the pass hasn't run since) has no `rescored_ts` and can disagree
with a fresh row about the same call's `pnl_r` — `stats` surfaces this rather than
quietly averaging the two populations together: `counterfactual_unrescored_n` (count) and
`counterfactual_fully_rescored` (bool, vacuously `True`/`0` with no counterfactual rows at
all). Neither field feeds `sizeable` or the live headline (counterfactual never does).

**SPEC-139 (grill 2026-08-06): the headline is LIVE-ONLY and FILLED-ONLY by default.**
A polluted headline (`mindshare_top_short` showing n=324/−26.3R, 100% replay from an
untrusted mechanical harness) drove a wrong "proven negative-edge" conclusion in a desk
review, and `n` counting unfilled commits as evidence hid that 74% of live records never
filled (`stage5_short` read "n=14, 40% hit" when the filled reality was n=3).

- **Commits vs fills.** `n_commits` = live thesis commits for the signature. `n_filled` =
  those with a real fill: outcome ∈ {tp1, tp2, tp3, stopped, closed_manual} (or, for an
  unrecognized outcome, a numeric `pnl_r`) — `retired_unfilled`/`zone_blown` are NEVER
  filled, even if a `pnl_r` somehow landed on the row; they tested nothing. `fill_rate` =
  `n_filled / n_commits`, `null` when there are no commits. A signature under ~40%
  fill_rate has an entry-mechanics problem (§0.5 two-leg rule), not a direction problem.
- `hit_pct`/`avg_r`/`total_r` compute over the **filled** set only. `hit_pct` = hits
  (tp1/tp2) over decided fills (tp1/tp2/stopped); `closed_manual` counts in `n_filled`
  WITH its realized `pnl_r` (avg_r/total_r) but is neither hit nor miss — a manual close
  says nothing about whether the committed stops work, so it never pollutes the
  kill-discipline read (SPEC-69). One-off: `ledger.py migrate-spec69` recategorized the
  pre-enum H row (2026-06-12, recorded `stopped` pnl_r 0.0 with a SPEC-69 marker in the
  notes) → `closed_manual`; idempotent. One-off: `ledger.py migrate-spec90` repairs any
  row whose committed `signature` contradicts its `direction` (the BLESS breakdown-SHORT
  tp2 win that landed in `trap_formation_long`) → the correct-side default; idempotent.
- `by_source` (SPEC 63) — the same `{n, hit_pct, avg_r, total_r}` split by row source
  (`live` / `replay` / `backfill`), always visible (it never feeds the headline). Pass
  `include_replay: true` to also get a per-signature `replay` block (display-only
  convenience mirror of `by_source.replay`) — either way, replay is never summed into
  `n_commits`/`n_filled`/`hit_pct`/`avg_r`/`total_r`/`sizeable`.
- **`sizeable` — the §9 GO gate exactly:** live **filled** `n_filled ≥ 10 AND total_r ≥
  +5 AND avg_r > 0`. `hit_pct` is NOT a factor (dropped from the old `n≥10 AND hit%>50
  AND total_r>0` gate in the 2026-08-06 rewrite — a signature can be sizeable at
  hit_pct==50 if the R-math clears). Zero commits → `{n_commits: 0, sizeable: false}`,
  never an error. Replay volume (even 324 wins) can never flip it — it isn't in the
  population the gate reads.
- **Near-duplicate guard.** `record` compares the new row's (ticker, signature, outcome)
  against existing rows recorded within 30s and adds `dup_warning` to the response (never
  blocks the write, never collapses rows) — catches an accidental double-record like the
  TLM `blowoff_top_short` tp1 duplicate (one `pnl_r`, one null, 1s apart).

## backfill — seed from the watchlist graveyard

```
python3 orchestrator.py ledger '{"action":"backfill"}'
```

Seeds one record per dead thesis (`status` RETIRED/PASS) in `config/watchlist.json`,
inferring `outcome` from the state string (stopped/banked/zone-blown keywords →
otherwise `retired_unfilled`). `pnl_r` stays null — backfilled rows are signal
history, not booked R. Idempotent on (ticker, commit_ts).

**This never covers an ACTIVE commit** (PENDING/ARMED/OPEN/LIVE) — only RETIRED/PASS
theses. See `commit_open`/`sweep_open_commits` below for the gap that leaves open.

## commit_open / sweep_open_commits — feed the paper-track scorer from ACTIVE commits (SPEC-162)

**Problem this closes:** `counterfactual.backfill` (the desk's paper-track scorer, see
[counterfactual.md](counterfactual.md)) iterates ledger rows with `source: "live",
pnl_r: null` and joins geometry from `config/watchlist.json` by exact `(ticker,
commit_ts)`. Before this, no API ever wrote such a row for an *actively* committed
thesis — `backfill` above only ever seeds RETIRED/PASS ones — so recent commits (GALA,
CASHCAT, PIEVERSE, …) had no ledger row at all and were invisible to the scorer despite
CLAUDE.md's "commit more freely, every commit is a datapoint" directive.

```python
import ledger
ledger.commit_open({"ticker": "GALA", "thesis": {...}})   # one open row
ledger.sweep_open_commits(wl_path=None)                   # diff-and-create over the whole board
```
```
python3 capabilities/ledger.py commit --ticker GALA --json     # reads GALA's current thesis off the watchlist
python3 capabilities/ledger.py sweep --json                    # the diff sweep, CLI form
```

- **`commit_open(thesis_row)`** writes ONE row: `outcome: null, pnl_r: null, source:
  "live"`, `commit_ts` = `thesis.committed_ts`, plus a `geometry` snapshot — a verbatim
  copy of `entry_zone`/`stop`/`tp`/`entry_mode`/`legs` (SPEC-155). The row is
  **self-contained**: `counterfactual.backfill` reads this snapshot FIRST (falling back
  to the watchlist join only when a row carries none — legacy rows) so scoring survives
  a later board rewrite, re-anchor, or retirement. Never refuses to record: a commit
  with neither top-level geometry (`entry_zone`+`stop`+`tp`) nor any leg carrying
  `(entry|entry_zone)+stop` is still written, tagged `unscoreable: true` — the
  no-geometry cost stays a visible, countable fact instead of a silent gap.
- **`sweep_open_commits(wl_path=None)`** diffs the watchlist against existing OPEN rows
  (`source: "live", outcome: null`) and `commit_open`s any `(ticker, committed_ts)` not
  yet recorded. Idempotent: a second run with no watchlist changes creates nothing
  (`{created: [], retired: [], skipped: [...]}`). **Re-anchor handling:** re-timestamping
  `committed_ts` on arm/reconciliation is standing policy (CLAUDE.md §0.5) — a changed
  `committed_ts` for a ticker that already has an open row is a re-anchor, not a new
  thesis. Every stale open row for that ticker is retired in place
  (`outcome: "retired_unfilled", notes: "... window_re-anchored"`, rewriting
  `state/ledger.jsonl` the same way `migrate_spec*` does) and a fresh open row is
  written for the new timestamp — so a re-arm never double-counts as two live open
  commits. A row some OTHER path already resolved (a real close/retire, `outcome` set)
  is never touched by this — only genuinely open rows are eligible for retirement.
- **Wired into `ops/board_tick.py`'s `tick()`** (every ~15 min, `open_commit_sweep` in
  the return dict) — best-effort, a failure is logged and never blocks the tick. This is
  "the orchestrator's manual step" the ticket refers to; it no longer exists.
- **Known consequence, not a bug:** an open row and its eventual real resolution (via
  `thesis.py`'s close/retire → a separate `ledger.record` call) are TWO distinct ledger
  rows sharing `(ticker, commit_ts)` — `n_commits` counts both. This is unchanged
  resolution-recording behavior (explicit non-goal); `n_filled`/`sizeable`/`total_r`
  (the §9 GO gate) are unaffected since the open row is never itself filled.

Tests: `tests/test_ledger_open_commits.py` (commit_open validation, sweep
create/idempotent/re-anchor), `tests/test_counterfactual.py`
`TestBackfillPrefersSnapshotOverWatchlist` (snapshot-first read), `tests/test_board_tick.py`
`TestOpenCommitSweep` (tick wiring).

Tests: `tests/test_ledger.py` (offline; ledger path redirected to a temp dir).
