# classify — state-machine verdicts vs the committed thesis

`classify` maps live data onto each token's committed `thesis` block and returns
exactly ONE of **CONFIRMS / TRIGGERS / WATCH-ARMED / BREAKS** per token (precedence:
BREAKS > TRIGGERS > WATCH-ARMED > CONFIRMS). It is the §0.5 state machine: the thesis
is committed once; classify only reports how new data relates to it.

```
python3 orchestrator.py classify '{}'              # the board
python3 orchestrator.py classify '{"ticker":"X"}'  # one token
```

## The four legs (evaluated in order)

1. **Time-stop** — `thesis.committed_ts + time_stop_h` elapsed → BREAKS.
2. **Trigger logs** — `state/*_trigger.log` fire lines since commit → BREAKS/TRIGGERS.
3. **Price leg (SPEC 39)** — the high/low RANGE since thesis commit vs the
   thesis's own `stop` / `tp` / `entry_zone`.
4. **Watch leg (SPEC 77)** — the same RANGE vs an optional `thesis.watch_level`
   trip-wire → WATCH-ARMED. Runs even when the price leg can't (WATCH direction,
   null committed levels).
5. **Funding/regime leg (`rf=`)** — regime_flip's funding-sign read.

## Price-leg precedence rule (SPEC 39)

The committed thesis is mostly PRICE fields; a funding-only read implements a
fraction of the contract (live proof: VELVET's stop 0.40 wicked to 0.4749 and TP1
0.30 traded through while classify said `rf=CONFIRM` off negative funding).

- **BREAKS** ⇐ the window high/low printed through `stop` (direction-aware:
  SHORT → high ≥ stop; LONG → low ≤ stop). **A stop printed on a wick IS a
  break** — the position is stopped even if price came back.
- **TRIGGERS** ⇐ a not-yet-banked `tp` printed (SHORT → low ≤ tp; LONG → high ≥ tp),
  or the range entered `entry_zone` while status is ARMED/PENDING — and the stop
  did NOT print.
- **Stop-breach beats everything.** If both a TP and the stop printed, the verdict
  is BREAKS; when klines resolve the order, the reason reports the sequence
  ("TP 0.30 traded 06-09 04:00 then stop breached 06-09 09:00").
- **The funding leg can only set the verdict when the price leg is silent.** When
  the price leg fires, the rf tag is appended to the reason as
  `rf=<TAG> (funding leg, demoted by price-leg verdict)` but never outvotes it.
  Time-stop and trigger-log BREAKS keep their normal precedence.

### PENDING entry gate (SPEC-78)

A committed-but-unentered position is **not** live, so its `stop`/`tp` are **not
watched** yet. While `status == PENDING` and the entry has never printed, the price
leg evaluates **only entry detection** — `stop_breached` is reported `null` (not
evaluated) and the verdict cannot be BREAKS off the stop. This kills the false-BREAK
where a breakdown SHORT commits an `entry_zone` BELOW current price with a `stop`
that is also below price (correct short geometry): with price above the stop the old
leg read `stop_breached: true` every tick and cried BREAKS though nothing ever filled
(RIVER 06-17: entry `[4.15,4.32]`, stop `4.80`, price drifted UP to 5.07 → CONFIRMS,
not a fake BREAK). It is the inverse of SPEC-77's silent WATCH miss.

- **Entry = the traded range intersects the committed zone** (direction-agnostic:
  `high ≥ zone_lo AND low ≤ zone_hi`). A breakdown SHORT must see price actually fall
  into the zone — being anywhere above it is no longer read as "entered".
- **Once the zone prints, the position latches live** and the stop/TP are watched
  normally — a short that enters then reclaims its stop still BREAKS. The latch is any
  of: the zone printing this window, an `entered_ts` stamp on the thesis, or the
  orchestrator flipping `status` off `PENDING` (→ `OPEN`/`LIVE`/`ACTIVE`).
- A live thesis (`status` not `PENDING`, or `entered_ts` set) watches its stop exactly
  as before — byte-identical to pre-SPEC-78 behavior.
- The SPEC-54/61 commit-time geometry warnings still fire (a stop inside prior churn
  is still worth flagging), but they no longer correspond to an actual false-fire.

### Sell-the-breakdown entry sequencing (SPEC-79)

SPEC-78 stopped a breakdown SHORT from reading "entered" while price drifts above the
zone, but it still evaluated the stop/TP against the **aggregate** window high/low. A
sell-the-breakdown SHORT falls from ABOVE its stop DOWN into the entry zone — so the
window's pre-entry high sits above the stop, and the moment the zone finally prints the
old leg read `stop_breached` off that pre-entry high → a false **BREAKS** instead of the
**TRIGGERS** the entry earns (and a buy-the-breakout LONG mirrors it: it rises from BELOW
its stop, so a pre-entry dip below the stop false-broke).

The fix sequences the entry from the candle list:

- When the thesis goes **live this window** (`PENDING` → the zone prints, with no prior
  `entered_ts`) and candles are present, the entry bar is the **first** candle whose range
  trades into the zone. Stop/TP are then evaluated **only from that bar forward** — the
  pre-entry drift no longer counts. A breakdown SHORT that trades down through into the
  zone reads `entered_zone: true, stop_breached: false → TRIGGERS`; a genuine post-entry
  reclaim of the stop still BREAKS.
- **No candles** (24h-ticker fallback) → the order is unresolvable, so SPEC-78's
  conservative full-window read is kept (enter + reclaim in one aggregate window → BREAKS).
- A position **already live** (`status` not `PENDING`, or `entered_ts` set) watches the
  full window exactly as before — byte-identical to pre-SPEC-79 behavior.

The entry-intent split (fade-the-rally zone above spot vs sell-the-breakdown zone below
spot) needs no per-tick guess: the range-intersection entry + post-entry stop/TP window
handle both sides from the geometry alone. RIVER `[4.15,4.32]`/stop `4.80` and BSB
`[0.44,0.475]` both read CONFIRMS while price sits above the zone and TRIGGERS (never
BREAKS) once it trades down through.

**The filled/entry_idx read is now `thesis.fill_of` (SPEC-147)** — the same rule
`counterfactual.score_counterfactual` uses to score the paper record, so the board and
the paper record can never disagree about whether a thesis filled or at what price.
`eval_price_leg` only consumes `fill["filled"]`/`fill["entry_idx"]` for its own
BREAKS/TRIGGERS sequencing (unchanged), but carries the full `fill` result (`mode`,
`entry_px`, `source`) through in its return dict for a caller that needs it. See
`docs/thesis.md` ("Library surface: `fill_of`") for the rule itself.

### Window source

Klines (1h) from the thesis's primary venue covering `committed_ts → now` when the
commit is inside the venue's kline reach (Binance 1500h, Bybit 1000h); otherwise the
venue's 24h ticker high/low. Unfetchable window → the price leg stays silent and the
funding leg classifies as before. Dead theses (`status` RETIRED/PASS) and theses with
no price fields skip the leg entirely (no fetch).

### Evidence

Every result carries the inputs so a CONFIRMS never needs a hand-curl to trust:

```json
"price_leg": {"high": 0.4749, "low": 0.2642, "stop_breached": true,
              "tps_printed": [0.30], "entered_zone": false,
              "window": "klines:binance:31x1h"}
```

`price_leg: null` = the leg didn't run (no thesis price fields, dead thesis, or
window unfetchable). `stop_breached: null` (vs `false`) = the stop was **not
evaluated** because the position is a PENDING entry that has not printed yet
(SPEC-78) — distinct from `false` = evaluated and intact.

## Watch leg (SPEC 77) — a trip-wire for WATCH theses

A `WATCH` thesis whose re-arm trigger lived only in the note was **invisible** to the
board: the price leg fires only when the thesis carries a committed `stop`/`tp`/`entry_zone`,
so a `direction: WATCH` thesis with those null could return nothing but CONFIRMS. The move
it was written to catch happened silently (live proof: **BEAT** parked WATCH with a prose-only
re-arm condition, ran an 11.57 ATH and collapsed −75% to 2.83 in 4 days — the board printed
`BEAT CONFIRMS` the entire time).

`watch_level` closes that blind spot. It is a **monitor-only** trip-wire — it does NOT imply a
committed/armed position, so a breach surfaces as **WATCH-ARMED**, never BREAKS/TRIGGERS (those
mean a committed position moved; conflating them corrupts the §0.5 state machine). Shape on the
thesis block (a single object or a list):

```json
"watch_level": {"price": 9.20, "dir": "below",
                "note": "lower-high rebuild below 9.20 + funding cooling → post-breakdown short",
                "page_label": "post-breakdown short"}
```

- `dir: "below"` arms when the window **low ≤ price**; `dir: "above"` when the window **high ≥
  price** (touch counts). The window is the same SPEC 54 klines the price leg uses — so a
  breach counts **only if price traded through the level at/after `thesis.committed_ts`**
  (SPEC-113); a pre-commit wick that never repeats post-commit is NOT a crossing.
- The breach carries `watch_level.note` into the `reason` so the operator sees *why* it armed
  (desk-record/inbox only — **SPEC-161: the pushed phone page never reads `note`**, only the
  optional `page_label`, ≤40 chars, validated/truncated at commit-read time. See
  [page_grammar.md](page_grammar.md)).
- It runs regardless of `direction` and of null committed levels — it is the one leg that fires
  for a bare WATCH thesis. Dead theses (RETIRED/PASS) and a null/absent `watch_level` skip it
  (behavior then byte-identical to before SPEC 77).
- **Idempotent (no nag):** a breached watch_level reads as WATCH-ARMED on every board tick, but
  fires **one** MED inbox event per `(ticker, level)` episode (deduped via
  `state/watch_armed_cursor.json`). Once armed, convert it to a committed thesis or dismiss it.
- WATCH-ARMED surfaces in the full board AND in `--breaks` triage. Evidence rides in `watch_leg`:

```json
"watch_leg": {"breached": [{"price": 9.20, "dir": "below", "note": "..."}],
              "high": 11.57, "low": 2.83, "window": "klines:binance:72x60m", "legacy": false}
```

`watch_leg: null` = no watch_level, dead thesis, no breach, or window unfetchable.

**SPEC-113 — `legacy` (commit-time reset):** a thesis with no resolvable `committed_ts`, or one
older than the kline reach, falls to the SPEC-54 `trailing_24h_fallback` window — a window that
can't prove the crossing traded *after* commit (a pre-commit wick from before the position even
existed reads "crossed"; SLX 2026-07-07 committed @ 0.2077 with no `committed_ts` on the hand-
written thesis block, and the 0.1846 exit line read "crossed" off the 07-06 pre-commit low while
live price was 0.2094). `watch_leg["legacy"]` is `true` in that case and the `reason` string is
tagged `(pre-commit history — verify live price)`. The SPEC-77 inbox event still fires (tagged)
so the episode isn't silently dropped, but `ops/board_tick.py`'s SPEC-111 phone-push delivery
**never** fires for a legacy-tagged crossing — only a verified (klines, post-commit) crossing
pages the user.

## Funding leg (SPEC-142) — the funding mirror of watch_level

`watch_level` gives a committed PRICE cross a paging home; a committed FUNDING cross had
none — "page me when `<name>`'s funding crosses `<level>`" (AEON, and the BLESS §6 arming
leg "funding cools under +0.02%/4h") was hand-rolled as `ops/interim_funding_watch.sh`
(deleted by this spec's landing) until now. Shape on the thesis block (a single object or
a list — same convention as `watch_level`):

```json
"funding_watch": {"threshold_4h": 0.02, "op": "lt",
                  "note": "cools under the ramp -> check lower-high",
                  "page_label": "cool-arm the short"}
```

**SPEC-161:** `note` is desk-record-only (inbox events, `funding_leg.breached[].note`)
and is NEVER pushed to the phone. `page_label` (≤40 chars, optional — validated/
truncated at commit-read time by `thesis._clip_page_label`, never at page time) is the
only prose a pushed page may carry. See [page_grammar.md](page_grammar.md).

- `op: "lt"` arms when the CROSS-VENUE verified live rate (`live.funding_4h` —
  `regime_flip.live_perp`'s already-floor-rejecting resolution, §3: never a single-venue
  read) is **below** `threshold_4h`; `op: "gt"` when **above**.
- No lookback window — funding has no kline-reach issue like price, so the current tick's
  rate is the whole read (no `committed_ts` cut to apply). A condition that reads breached
  on the very first tick after commit (within 30 min) fires anyway and tags
  `already_true_at_commit: true` — it is never silently skipped just because it predates
  the first look.
- Malformed entries (missing `op`, a bad `op` value, a bare number instead of a dict) are
  **dropped but reported** in the row's `funding_watch_caveats` list — never silently (the
  SPEC-77 bare-float watch_level lesson: a silently-dropped entry is never armed and never
  known to be dead).
- Fires the same **WATCH-ARMED** verdict as `watch_level` (never BREAKS/TRIGGERS — a
  monitor-only trip-wire is not a committed-position move) and rides `funding_leg` on the
  row:

```json
"funding_leg": {"breached": [{"threshold_4h": 0.02, "op": "lt", "note": "..."}],
                "funding_4h": 0.018, "already_true_at_commit": false}
```

`funding_leg: null` = no `funding_watch`, dead thesis, or not breached.

`ops/board_tick.py` pages a fresh breach through the same continuous-key re-arm mechanism
as `watch_level` (SPEC-141/161 grammar: `👁 TICKER ARMED` / `funding +0.018%/4h crossed lt
+0.020% → <page_label or "convert to entry or dismiss">`) — page once, no re-page while
still breached, re-arm on leave+return.
`classify.fire_funding_armed` mirrors `fire_watch_armed` for the once-per-episode inbox
record (`state/funding_watch_cursor.json`).

Tests: `tests/test_spec142_funding_cross_tripwire.py`.

## Other modes

- `--breaks` — board filtered to BREAKS + TRIGGERS + WATCH-ARMED.
- `--migrate [--dry-run]` — bootstrap thesis blocks from legacy state memos.
- `--arm TICKER` — print the arm_setup.py command for a thesis.

Tests: `tests/test_classify.py` (price leg, offline-deterministic),
`tests/test_classify_watch_level.py` (watch leg + inbox dedup),
`tests/test_funding_*.py` (funding leg).

## Board envelope + surveillance dead-man (SPEC 64)
The full board (`classify '{}'`, no ticker filter) returns a `{board, meta}` envelope —
a single ticker / subset still returns the per-ticker object/list:
```json
{"board": [{"ticker": "...", "verdict": "...", "reason": "...", ...}],
 "meta": {"surveil_age_h": 0.2, "surveil_stale": false,
          "board_tick_age_h": 0.1, "board_tick_stale": false, "cadence_h": 0.25}}
```
`meta` is the **dead-man switch**: the nonce-surveil launchd agent once sat unloaded for
9 days while the Designer kept reading "surveillance armed" off a dead board. `surveil_age_h`
= now − the surveil layer's newest observable tick (an ESCALATION line in
`state/nonce_alerts.log` OR a quiet `state/surveil.heartbeat`). When that age exceeds **2×
the launchd cadence** (`surveil_stale`), EVERY board row's `reason` is prefixed
`[SURVEIL STALE Nh]` and **one** HIGH inbox event fires per stale-episode (deduped via
`state/surveil_deadman.json` — never per-read spam). `board_tick`'s baseline
(`state/board_last.json` mtime) gets the same check → `[BOARD-TICK STALE Nh]`. No state at
all → stale-by-absence prefix with no event (no episode to bound; `triage` agent-health
reports the MISSING agent). The board is physically unreadable without seeing a dead watcher.

## Price-leg window (SPEC 54)
The price leg evaluates from `thesis.committed_ts` FORWARD — 15m bars for the first
24h, 1h beyond; bars opening before the commit are dropped (a straddling bar carries
pre-commit prints). The cut applies with or without `time_stop_h`. A fresh commit with
no closed bars is SILENT. Only a missing `committed_ts` or a commit beyond the kline
reach uses the trailing 24h ticker, tagged `trailing_24h_fallback` in `window`.

## Execution-venue (Aster) fillability veto (SPEC-136)

`annotate_aster_listed(rows, tokens)` tags every row `aster_listed: true|false|null` — a
question distinct from the cross-venue OI/funding SIGNAL the rest of the board reads
(§0.6.3b: never conflate the two). The user fills exclusively on Aster (CLAUDE.md §7); COTI
printed a clean cross-venue regime-flip signal and got committed with a real trigger, but
Aster carries no COTI market — the trade never existed for this user, and nothing in the
engine caught it until a manual board audit.

One live/cached call to Aster's `fapi/v1/exchangeInfo` per board run (`capabilities/
aster_listing.py`, disk-cached 6h TTL — same pattern as onchain.py's SPEC-67 contract-probe
cache), never per-ticker. `false` appends `| SIGNAL-ONLY (no Aster market)` to the row's
`reason` — advisory only, never touches `verdict` (§0.5); `true` leaves the row byte-
identical. `null` means the fetch (and any cache) failed — §3 doctrine: unknown, never
treated as "not listed." A watchlist entry's explicit `"aster_listed": false` (COTI/LQTY/
DRIFT — verified un-Aster-listed names) overrides the live probe.

`ops/board_tick.py` reads this flag to gate the PHONE PAGE only (never the inbox record):
`aster_listed:false` suppresses `notify.sh` entirely but still writes the inbox event
(tagged `[SIGNAL-ONLY]`); `aster_listed:null` still pages, tagged `⚠ venue-unverified`
(fail toward informing — a transient network blip must never silently suppress a page).

Tests: `tests/test_spec136_aster_listed_veto.py`.

## `live_price` in the JSON board output (SPEC-141)

`--json` mode also emits `live_price` (the same `live.price` already computed for the
funding/regime leg) per row — `ops/board_tick.py`'s lock-screen page grammar needs the
live price to render a page (see docs/page_grammar.md); `null` when `live_perp` degraded
for that row.

## Rotation-freshness annotation (SPEC-98)

`annotate_freshness(rows)` appends `distribution: FRESH|FROZEN|ROTATED — …` to any board
row with a **live SHORT thesis**, read from the persisted `state/rotation_freshness_<T>.json`
(produced by the last onchain/brief/verify read — the board itself spends nothing). Stale
state (> `board_max_age_h`, default 24h) is not echoed. A READ: it never changes the verdict
(§0.5). The FROZEN→ROTATED HIGH inbox event fires in `rotation_freshness.build_freshness`,
not here. See docs/onchain.md §SPEC-98.

## `operator_not_done` live veto + `tier` (SPEC-149)

Every board row carries `operator_not_done: true|false|"unknown"` — the CLAUDE.md §0.6
counterparty read as a LIVE veto, never a stored commit-time verdict. Computed by
`capabilities/operator_veto.py` (`classify()` is the pure gate, `sweep()` the headless
per-row entry point — decoupled from any live-SHORT-thesis gate, unlike
`onchain._freshness_layer`) from the rotation-freshness stack:

- `FRESH` / `FROZEN`-but-`ROTATED` → `true` (the selling machine is running).
- `FROZEN` under the 24h floor → `true` (flow alone is the weakest evidence).
- `FROZEN` ≥24h with **no confirmed breakdown-hold** → `"unknown"` — flow may only ever
  BLOCK the unlock, it never fires one alone (grill Q12=c).
- `FROZEN` ≥24h + breakdown-hold, but a squeeze leg is still inside the name's own
  cadence (`squeeze_cadence_threshold_days` = 2× the median gap between its own squeeze
  legs, never a fixed day count — grill Q13=c) → `true`.
- `FROZEN` ≥24h + breakdown-hold + squeeze machine off → `false` — the short unlocks.
- The newest distribution evidence aging past a window (default 4h) downgrades any of
  the above to `"unknown"` — a stale read is not a veto (§3).

`true` prefixes the row's `reason` with `⛔ VETO — <reason> | …` — **advisory only**,
`verdict` (BREAKS/TRIGGERS/WATCH-ARMED/CONFIRMS) is never touched (§0.5: the desk never
silently blocks the user). A `true → false` transition fires exactly ONE HIGH inbox
event (`operator_veto`) — the entry unlocking, the moment the user is away for.

`tier: tradeable|tracking` is derived, never hand-set: `tradeable` iff the row's
canonical `signature` is in the ledger's earned set (`classify.earned_signatures()` —
live filled n≥1 AND total_R>0, today `trap_formation_long`/`stage5_short`) AND
`aster_listed is True` AND the §7 liquidity gate passes (`live.vol_m >= LIQ_GATE_M`).
Board `meta` carries `tier_tradeable_count`/`tier_tracking_count`/`operator_veto_count`
so session-open surfaces the split without scanning every row.
