# board_tick — standing scanner (SPEC 46/51/106/111, ops — not a registered capability)

## Purpose
The scanner stops waiting for a session: every launchd tick (`StartInterval 900`) runs
`classify '{}'`, diffs each ticker's `{verdict, stop_breached, tps_printed}` against
`state/board_last.json`, and pushes deltas into the SPEC-45 inbox + a push notification
(`ops/notify.sh`, SPEC-111). VELVET's stop printed a full day before anyone saw it — this
is the fix.

## Behavior
- Delta severities: HIGH for a flip to BREAKS or a fresh stop-breach, MED for TRIGGERS
  flips and fresh TP prints.
- No-delta ticks write nothing to the inbox (§0.5 silence).
- SPEC 51: each tick also sweeps unmapped watchlist names (`onboard` sweep) — pure
  set-difference first, ZERO network when fully mapped; a sweep failure is logged to
  `state/board_tick.err` and never kills the tick.
- SPEC-106: each tick also runs `tape_watch.run_tick()` over every WATCH-thesis name —
  the between-committed-levels blind spot (BIRB moved −12% intraday inside its
  0.070/0.095 watch levels with operator taking profit on the tape and the desk stayed
  silent). Read-only paging via the SPEC-66 `page_gate`, never touches the verdict/state
  machine.
- Safety: lockfile (30-min stale-break); a classify failure SKIPS the tick — the
  baseline is never overwritten with a bogus board; baseline writes are atomic.

## Push delivery (SPEC-111) — generation existed, delivery didn't

Committed watch levels and HIGH alerts were firing into `state/` (inbox events, tape
pages) that nobody saw until the next session opened — BIRB's short-conversion trigger
(`<0.0865 hold`) crossed silently between sessions, blowing the zone by the time anyone
looked. `board_tick` now pushes via `ops/notify.sh` for **exactly 4 event classes** (no
others — noise kills alerting):

1. **watch_level crossing** — read fresh off every row's `watch_leg.breached` (SPEC 77),
   independent of the verdict-diff baseline. **SPEC-113:** a row whose `watch_leg["legacy"]`
   is `true` (no resolvable `committed_ts`, or one beyond the kline reach — the crossing can't
   be proven to have traded after commit, e.g. a fresh SLX commit with no `committed_ts` field
   reading a pre-commit low as "crossed") is **never pushed** — it's excluded from the armed-key
   set entirely, so it can't page and can't leave a stale "armed" entry behind either. The
   SPEC-77 inbox event still fires (tagged `(pre-commit history — verify live price)`); only
   this phone-push path is gated.
2. **verdict transition to `TRIGGERS` or `BREAKS`** — any OTHER verdict transition (or a
   TP print) is inbox-only, never pushed.
3. **STOP-BREACHED**.
4. **`tape_watch` (SPEC-106) or `thesis_drift` (SPEC-91) at severity HIGH** — `tape_watch`
   pages at MED too (inbox-only); `thesis_drift` fires HIGH only, read fresh off each
   row's `thesis_drift.stale`.

**Dedup** (`state/notify_sent.json`): the same `(ticker, event_class, level)` delivers at
most once per 6h while continuously "armed". `watch_level` and `thesis_drift` are
re-evaluated fresh every tick (not a delta) — if a row's condition clears (price left the
zone / thesis re-anchored), that key is explicitly **disarmed**, so the *next* fresh
crossing delivers immediately instead of waiting out the 6h window. The other classes
(`verdict`, `stop_breach`) are inherently one-shot per `diff()` transition, so the same
6h cap mainly guards against a baseline reset re-firing a stale delta as if it were new.

**Kill switch**: `CRIMEDESK_NOTIFY=off` disables delivery — checked inside `board_tick`
itself (so an injected `notify_fn`, e.g. in tests/replay, is never called either) AND
inside `ops/notify.sh` (so a direct/manual invocation is also silenced). Body is
truncated to 120 chars and reuses the existing alert message verbatim — no new copy to
maintain.

`ops/notify.sh <title> <body>` is the actual delivery: `osascript` desktop notification
(zero new deps), plus a best-effort `curl` POST to `$CRIMEDESK_NTFY_URL` when set
(ntfy.sh-style — reaches the phone). Both legs are best-effort — a failure is logged to
`state/notify.err`, never raised; `state/inbox_events.jsonl` remains the source of truth
regardless of whether delivery succeeded.

⚠ **launchd/TCC caveat**: the first `osascript` notification fired from a launchd agent
needs one interactive permission grant (System Settings → Notifications, or the
Terminal/osascript prompt) — see
`memory/reference_coder_dispatch_launchd_tcc_broken.md` for the prior art on launchd
permission traps. Until granted, delivery silently no-ops (logged to
`state/notify.err`); the inbox event still lands.

## Execution-venue (Aster) fillability veto (SPEC-136)

Every row classify hands to `tick()` carries `aster_listed: true|false|null` (see
docs/classify.md). `_push()` checks it per-ticker before any delivery:
- `false` — suppressed entirely, no `notify.sh` call. The tick still writes the inbox
  event for that (ticker, class) — tagged `[SIGNAL-ONLY]` — so the read survives for
  review; only the phone page is gated.
- `null` (venue probe failed/unknown) — still pages, with the body prefixed
  `⚠ venue-unverified —` (fail toward informing, never suppress on a transient blip).
- `true`, or the field simply absent (older/injected rows) — unchanged, pages as before.

## Lock-screen page grammar (SPEC-141)

Every pushed title/body is built through `ops/page_grammar.py` — TICKER · PRICE · WHAT
HAPPENED · DO THIS, never the raw diff() delta text (`verdict CONFIRMS→BREAKS`) or desk
jargon (`STALE-THESIS`). `tick()` loads the committed thesis (`entry_zone`/`stop`/`tp`/
`direction`) via `load_thesis_watchlist` (a `classify.load_watchlist` alias — bare
module name so tests can isolate it, same pattern as `onboard.WL_PATH`) plus the live
price off each row's `live_price`, and pulls the action phrase from there — never
fabricated. `_priority_for(cls, level)` maps the same event class to the ntfy
`Priority` header (BREAKS/stop-breach → urgent, TRIGGERS/watch-cross → high, else
default), forwarded through `_notify_sh_deliver`'s 3rd arg (SPEC-141 addition to
`ops/notify.sh`). `_push()` tries `notify(title, body, priority)` and falls back to the
2-arg form on `TypeError` — legacy/test `notify_fn` doubles that only accept
`(title, msg)` keep working unchanged. See docs/page_grammar.md.

## Funding-cross tripwire (SPEC-142)

Each row's `funding_leg` (docs/classify.md §Funding leg) rides the exact same
continuous-key mechanism as `watch_leg`: `_current_continuous_keys` includes any
currently-breached `funding_watch` entry (`ticker|funding_watch|<threshold><op>`),
disarming anything that left its condition this tick so the next fresh cross re-pages
immediately rather than waiting out the 6h TTL. Pushed through `PG.page_funding_armed`
at `high` priority (same tier as a watch_level cross) via the identical `_push` path.

## Public signal channel (SPEC-181)

Beside the private ntfy leg above, `_tg_push` posts a redacted public card via
`ops/telegram_post.py` for exactly 3 of the classes above: verdict→`TRIGGERS`,
verdict→`BREAKS`, and `STOP-BREACHED`. Never watch_level/funding_watch/tape_watch/TP
prints. One-shot delivery reuses the same `_arm_check`/`_disarm` machinery under a
`tg:`-prefixed key in `state/notify_sent.json`, kept independent of the ntfy leg's own
keys. See docs/telegram_channel.md for the redaction contract and setup.

## Install (human's call)
```
bash ops/install_launchd.sh --only board-tick   # renders the plist template for this machine + loads it
```
One manual tick: `python3 ops/board_tick.py` (prints the status JSON).
One manual notification test: `ops/notify.sh "crime-desk TEST" "hello"`.

## Gotchas
- First run seeds the baseline quietly (no events) — deltas start on the second tick.
- The tick consumes the orchestrator envelope, never raw classify output.
- `tests/test_board_tick.py` and `tests/test_spec111_watch_level_push_notification.py`
  both redirect `BT.NOTIFY_STATE_PATH` to a tmpdir — any new persistent state added to
  `board_tick.py` needs the same isolation or it leaks into the real `state/` dir when
  tests run.
