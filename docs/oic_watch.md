# oic_watch — OIC_FLIP / ROLE_DRIFT alerting (SPEC-180 req 6 / G7)

## Why it exists

The OI-construction verdict and venue roles (SPEC-179) are point-in-time snapshots.
A name that reads `DIRECTIONAL` today and `ARB_DOMINATED` tomorrow had its squeeze
fuel evaporate underneath a live thesis with nobody told; the reverse (fuel
arriving) is exactly the "next-OI-expansion" entry SPEC-177's annotations already
name. This module watches for both transitions plus venue-role drift (the exit
venue or mark-engine composition a thesis was committed on moving), and routes
each into the EXISTING paging pipeline — no new alert pathway.

## Events

- **`OIC_FLIP`** — `oi_construction.verdict` crosses the arb/directional line.
  `DIRECTIONAL`/`MIXED` → `ARB_DOMINATED` = fuel evaporating; the reverse = fuel
  arriving.
- **`ROLE_DRIFT`** — `venue_roles` changed between two consecutive snapshots
  (`config/venue_roles.json`): the `FLOW_CONFIRMED` exit venue flipped, or a
  material (≥5 point) shift in the anchor index's mark-constituent weights.

## Debounce (`classify_flip`)

State per ticker (`state/oic_watch_state.json`): `{last, pending, pending_count}`.
A flip fires only after the SAME new verdict persists **2 consecutive
computations** — one noisy tick can't page. **`UNKNOWN` never participates**: a
tick reading `UNKNOWN` (or a `gating_ok:false` stale read, treated as `UNKNOWN`
here) is a total no-op on the state — `X→UNKNOWN→X` is nothing, `X→UNKNOWN→Y`
still needs `Y` to persist 2 ticks before firing (the intervening `UNKNOWN` neither
helps nor resets it).

## Routing (`route_event`)

`has_live_thesis` = an open position OR an `ARMED` watch leg on that ticker.

| Event | live thesis | no live thesis |
|---|---|---|
| DIRECTIONAL→ARB (fuel evaporating) | `phone` | `desk` |
| ARB→DIRECTIONAL (fuel arriving) | `desk` | `annotate` |
| exit `FLOW_CONFIRMED` venue flip | `phone` | `desk` |
| mark-constituent drift | `desk` | `annotate` |

`phone` pages via `ops/notify.sh` (ntfy + osascript); `desk` is desktop-only
(osascript, no ntfy POST); `annotate` never calls the notifier at all — but **every
event appends to `inbox` regardless of route** (`process_ticker`/`_emit`), so an
annotate-only event is still on the record. `phone`/`desk` routes additionally go
through `page_gate.should_page` (the SAME cooldown/dedup machinery every other
board-tick alert class uses) before actually notifying.

## Cadence

Wired into `ops/board_tick.py`'s existing 15-minute `tick()`
(`ops/com.crimedesk.board-tick.plist`, `StartInterval 900`) via
`_oic_watch_sweep` — no new launchd job. The universe is **LIVE-thesis names
only** (open positions + `ARMED` watch legs) per the spec's cadence note; other
names refresh on the discovery-tick cadence (a caller passing a wider `universe`
to `oic_watch.run_tick` directly). Best-effort: a sweep failure is logged
(`state/board_tick.err`) and never blocks the rest of the tick.

## Tests

`tests/test_oic_watch.py` — offline-deterministic (`oic_fn`/`has_thesis_fn`/
`notify_fn` all injected, tmp state paths): the full `classify_flip` debounce
matrix (including both `UNKNOWN`-no-op cases), the routing table, both drift
detectors, and `run_tick`/`process_ticker` integration (2-tick persistence pages
phone for a live thesis, annotate route never calls notify but still inboxes,
stale `gating_ok:false` never pages, one ticker's crash never kills the tick).

`tests/test_page_grammar.py` — `page_oic_flip`/`page_role_drift` render shape.

`tests/test_board_tick.py` — `_oic_watch_sweep`'s result rides along in `tick()`'s
return dict; a sweep failure never blocks the tick.
