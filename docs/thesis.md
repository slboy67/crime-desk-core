# thesis — schema-validated §0.5 lifecycle, ledger-wired (SPEC 48)

## Purpose
The §0.5 state machine's core state — the thesis block — was the only desk state
mutated by hand-edited JSON (inline Python against `config/watchlist.json` twice in one
day: retiring VELVET, closing SIREN). No schema validation, no automatic ledger row,
drift risk on every touch. `thesis` makes commit/close/retire mechanical and feeds the
SPEC-40 `ledger` so the §9 base-rate gate accumulates without anyone remembering to log.

## Contract
```
thesis '{"op":"commit","ticker":"SKYAI","thesis":{
  "direction":"SHORT","setup":"blowoff_top","entry_zone":[0.118,0.122],
  "stop":0.131,"tp":[0.105,0.098],"triggers":["held break of 0.115 on >=1.5x vol"],
  "invalidation":{"price_reclaim":0.125},"time_stop_h":72}}'
→ {ok, ticker, op:"commit", thesis (stamped committed_ts/committed_by, status PENDING), warnings}

thesis '{"op":"close","ticker":"SKYAI","outcome":"tp1","exit_px":0.105,"reason":"TP1 banked"}'
thesis '{"op":"retire","ticker":"SKYAI","reason":"zone blown without trigger"}'
→ {ok, ticker, op, ledger_row, pnl_r, pnl_r_estimated, signature, signature_source, suggested_signature}
```

## Signature + realized R at close (SPEC 63)
- **`signature`** — an optional commit-time field (vocabulary = the SPEC-59 setup keys
  `blowoff`/`catb_top`/`trap_long`/`neg_funding_gate`/`stage45_short` + `discretionary`),
  validated at commit and passed through to the ledger row (canonicalized). At close it
  also takes an explicit `--signature` override.
- **No silent guessing.** Closing a thesis with no committed signature does NOT guess: the
  row lands `discretionary` (`signature_source:"defaulted"`) with the keyword-inferred
  `suggested_signature` offered; pass `confirm_signature:true` to write the suggestion
  (`signature_source:"inferred-confirmed"`).
- **Mechanical `pnl_r`.** Pass `exit_px` and the row's `pnl_r` is computed from the thesis
  entry (entry_zone midpoint) + stop (`SHORT`: `(entry−exit)/risk`; `LONG`: `(exit−entry)/
  risk`). Priority: `exit_px` (compute) → an explicit `pnl_r` (authoritative) → a `tp1`/`tp2`
  outcome **estimated** from the committed TP level, flagged `pnl_r_estimated:true`.

## Rules enforced
- **No named invalidation → no commit.** §0.5's hard rule ("if you can't name the broken
  `invalidation` field, there is no BREAK") starts at commit time: `invalidation{}` must
  carry at least one non-null field.
- **Commit once.** A token with an active (PENDING/ARMED/OPEN) thesis rejects a second
  commit — close or retire first.
- `close` needs an explicit `outcome` (ledger enum: stopped|tp1|tp2|retired_unfilled|
  zone_blown|closed_manual); `retire` defaults to `retired_unfilled`. `closed_manual` =
  the user closed at discretion while every kill line was intact (SPEC-69) — pass the
  realized R via `pnl_r`/`exit_px`; counts in n, never as a stop-out.
- Close/retire moves the entry `tokens` → `retired` (+`retired_date`/`retired_reason`),
  sets `thesis.status` CLOSED/RETIRED, and appends the ledger row (signature canonical-
  ized from `setup` via `ledger.canon_signature`, direction, outcome, pnl_r, banked TPs).
- Missing `stop`/`time_stop_h` on a directional thesis is a **warning**, not a reject —
  but the SPEC-39 price leg and the time-stop BREAK can't watch what isn't committed.

## Commit-time geometry check (SPEC 54)
On commit, stop/TP/entry-zone levels are checked against the prior-24h traded range —
any level inside it returns `commit_warning` ("stop 0.105 inside prior-24h churn (high
0.112 / low 0.0552)…"): stale prints would read as instant fires on the board, and a
stop inside the churn is donated. Warning only — the commit still lands; a dead venue
read never blocks it.

## Concurrency
Read-modify-write on `config/watchlist.json` is flock-guarded (`state/watchlist.lock`)
and the write is atomic (tmp + rename) — safe against a concurrent board_tick or a
second session.

## Gotchas
- The ledger row is written BEFORE the watchlist move; if the ledger rejects the outcome
  (bad enum), nothing moves — fix the args and re-run.
- Through the orchestrator, the thesis block is a real JSON object in args (the
  orchestrator shell-quotes dict args as JSON — SPEC 48 `fill` enhancement).

## Library surface: `parse` / `check_board` (SPEC-146)
Beyond the commit/close/retire capability above, `thesis.py` is also imported directly
(no registry entry, no JSON interface — the `regime_flip.live_perp` / `setup_score`
precedent) by `classify`, `counterfactual`, `ops/tape_watch`, and `brief` as the ONE
typed parser for committed thesis geometry:

```python
from thesis import parse, check_board
p = parse(tok)   # tok = a watchlist token, or {"thesis": th} for a bare thesis dict
# p.direction, p.zone (sorted (lo,hi) or None), p.stop, p.tps, p.watch_levels,
# p.anchors, p.committed_epoch, p.time_stop_h, p.signature, p.operator_veto_caveat, p.caveats
```

`p.signature` (raw, direction-guarded via `ledger.canon_signature` downstream) is what
`classify.annotate_tier` (SPEC-149) checks against the ledger's earned set to derive the
board's `tier: tradeable|tracking`. `p.operator_veto_caveat` (SPEC-149 req 3) is an
optional free-text field, set at commit time when `classify`'s live `operator_not_done`
veto was `true` for that row — `validate_thesis` rejects a non-string value; `parse()`
drops a malformed one to a `p.caveats` entry rather than raising.

Before this, direction/entry_zone/committed_epoch/time_stop/watch_level/anchors were
each re-derived independently in four places and drifted apart — 14/27 board rows
silently lost their `watch_level` (SPEC-144), and re-deriving direction from live price
put 6/29 levels BACKWARDS because `entry_zone` was read unsorted in one place (classify's
`ZONE_BLOWN` branch) and sorted in another (`eval_price_leg`, `retire_flag_for`,
counterfactual's old `_zone`). `parse()` always returns `zone` SORTED — this fixes rather
than perpetuates that one outlier. A field `parse()` would otherwise silently drop
(a malformed `watch_level` element, a 3-element `entry_zone`, a non-numeric `stop`/`tp`)
instead appends a caveat to `p.caveats` — a reader that discards committed intent must
say so. `validate_thesis` (write-time, the `commit` op above) REJECTS the same shapes
`parse()` (read-time) reports as caveats.

**SPEC-161:** each `p.watch_levels[i]` also carries an optional `page_label` (≤40 chars)
— the ONLY prose `ops/page_grammar.py` is allowed to push to the phone (the analyst's
free-text `note` is desk-record-only, never pushed). Validation happens HERE, at
commit-read time (`thesis._clip_page_label`, shared with `classify._normalize_funding_watch`
for `funding_watch` entries): a label over 40 chars is truncated (never the element
dropped) and a `p.caveats` entry logged; a non-string/empty value degrades to `None`. See
[page_grammar.md](page_grammar.md).

`check_board(tokens=None, wl_path=None) -> [{ticker, caveats}]` is the standalone
board-level invariant sweep (generalizes SPEC-144's watch_level-only
`check_watch_level_invariant`): `build_thesis` validates on the `commit` path, but the
orchestrator's actual write path is often a direct `json.dump` to `watchlist.json` that
never touches `build_thesis` — a hand-written row can carry exactly the malformed shapes
`validate_thesis` would have rejected and stay silently broken forever. `classify`'s
board mode (`classify.py` with no ticker arg) runs `check_board` every tick and surfaces
`board_meta.thesis_geometry_violations` + a `🚨 THESIS GEOMETRY INVARIANT` line — hand-
writing stays legal, staying wrong does not.

## Library surface: `fill_of` — the ONE fill rule (SPEC-147)

`classify.eval_price_leg` (whether a thesis filled) and `counterfactual.score_counterfactual`
(whether/at-what-price it filled) used to answer that question independently — R is
computed from the fill price, so the board and the paper record could agree a thesis
filled and still disagree about its R, the exact number the desk's paper track record is
made of. `fill_of` is now the one place both decide it:

```python
from thesis import fill_of
fill = fill_of(th, direction, window)   # window = {"high","low","candles"} — the same
                                         # shape price_window_range/a bars slice produce
# fill -> None (no entry_zone, or an empty window), else:
#   {filled, mode, entry_px, entry_idx, source}
#   mode      "breakdown" | "fade" | "immediate"
#   source    "committed" (explicit th.entry_mode) | "inferred" (derived from geometry)
#   entry_px  the reference fill price — populated even when filled is False (a
#             counterfactual score needs "what WOULD it have filled at" for its
#             never-filled MFE/MAE read)
#   entry_idx None unless filled is True (nothing to sequence when it never printed)
```

An explicit `th["entry_mode"]` (`breakdown`/`break`/`dip` or `fade`/`breakout`/`rally`)
latches as `source:"committed"` and takes that mode's natural edge (breakdown → zone top,
fade → zone bottom) at face value — the desk said where it filled. Absent that, the mode
is **inferred** from the first candle's open/close (the commit-time price) vs the zone —
and because the top/bottom pick is then a guess, an inferred fill charges the **WORSE**
edge for the position's direction (SHORT → zone low, LONG → zone high) instead of
whichever edge the geometry would naturally suggest. This fixed a real bug: the pre-147
default (no commit reference at all) always assumed "fade" and charged the zone bottom
regardless of direction — flattering every inferred LONG fade. `immediate` (commit price
already inside the zone) has an exact reference price, so there's no edge to hedge.
`filled`/`entry_idx` are a causal range-intersection over `candles` (the first bar whose
traded range overlaps the zone), degrading to the aggregate window high/low when no
candle detail is available (no sequencing possible then — `entry_idx` stays `None`).
