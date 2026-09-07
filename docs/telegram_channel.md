# Telegram public signal channel (SPEC-181/189, ops — not a registered capability)

## Purpose

User directive 2026-08-31: the desk broadcasts its trade calls to a **public** Telegram
channel, auto-posted, so followers see the calls without seeing how the desk works ("I
don't want people making the same thing"). This sits **beside** the private paging leg
(`ops/notify.sh` → osascript + ntfy, SPEC-111/141/154) — that leg is unchanged. The public
leg fires for exactly **five** event classes: a verdict flip to **TRIGGERS** (gated to
genuine entry crossings only, see below), a verdict flip to **BREAKS**, **STOP-BREACHED**,
a thesis **commit** (→ **NEW SETUP**), and a thesis **retire** (→ **CANCELLED**, SPEC-189,
user directive 2026-09-02). Nothing else — no watch_level/funding_watch crossings, no
tape_watch, no TP prints, no digests.

## The redaction contract (the load-bearing part)

`ops/telegram_post.format_card(row)` is **allowlist-constructed**: it reads only six
named fields off `row` —

- `ticker`
- `direction` (`LONG`/`SHORT`)
- `event` (`TRIGGERS` / `BREAKS` / `STOP_BREACH` → rendered as `ENTRY TRIGGERED` /
  `THESIS INVALIDATED` / `STOPPED OUT`)
- the numeric `entry_zone` / `stop` / `tp` (or `tps`)
- an optional integer `leg` (SPEC-189: which leg of a multi-leg thesis fired — a bare
  position number, rendered as `Leg N`, never leg prose)
- a UTC timestamp (generated at format time, not read off `row`)

`ops/telegram_post.format_setup_card(ticker, thesis)` / `format_cancel_card(ticker)`
(SPEC-189) extend the same discipline to commits/retires: `format_setup_card` reads
only `ticker`, `direction`, `time_stop`, and — per leg (top-level fields when the
thesis has no `legs[]`) — `entry_zone`/`stop`/`tp` plus a **fixed-vocabulary** trigger
phrase built ONLY from numeric/enum fields (`trigger_timeframe` ∈ `{1h, 4h}`,
`trigger_op` ∈ `{above, below}`, `entry_ref`): `Trigger: {timeframe} close {above|below}
{price}`. A leg's free-text `gate`/`entry`/`note` fields are **never** read, even when
they contain the same information in prose — string-scraping the gate would defeat the
allowlist boundary it's built to enforce. `format_cancel_card` reads only `ticker`.

None of these functions ever read — and their callers may safely hand them the raw
thesis dict, because the forbidden fields are simply never looked at — `signature`,
any thesis prose (`triggers`, `invalidation`, `notes`, `setup`, `conviction`, `gate`),
wallet addresses, `page_grammar` output, venue-role/funding/on-chain reasoning,
tier/ledger stats, or sizing. Redaction is by construction, not by scrubbing a rich
string.

A row/thesis whose fireable leg carries no numeric params (no `entry_zone`, `stop`, or
`tp`, on any leg) returns `None` — a paramless watch is not a public call, and the
poster is never invoked. `ops/telegram_post.has_leg_geometry(thesis)` is the shared
"does this thesis have anything numeric to show" check, used both by
`format_setup_card` and by `board_tick._setup_sweep` to decide what to track at all.

**Rule for future changes:** any field added to a card — even one that looks
harmless — must be re-reviewed against this contract before it ships. The allowlist
exists specifically so the channel can never leak the mechanics that produce a call;
adding a field "just for context" is exactly the failure mode it guards against.

## Leg-aware TRIGGERS/BREAKS/STOP_BREACH cards (SPEC-189 §A)

A §0.5 two-leg thesis keeps its numbers inside `thesis["legs"]` and leaves the
top-level `entry_zone`/`stop`/`tp` null — building a card from the top-level fields
(the SPEC-181 behavior) silently produced `None` for every such thesis (the ACE bug:
`tg:ACE|verdict|TRIGGERS` armed 2026-09-02 with no card ever sent — the desk's live
call of the day never reached the channel).

`board_tick._pick_firing_leg(thesis, board_row)` picks the leg the card is built from:
it matches this tick's `board_row["watch_leg"]["breached"]` prices (by numeric
equality) against each leg's `entry_zone` bounds / `entry_ref` — the leg whose entry
gate the price crossed. No match (or no `legs[]` at all) falls back to `legs[0]` /
the thesis's own top-level fields respectively. A multi-leg card shows the **firing
leg only**, labelled `Leg 1` / `Leg 2` by position (the `leg` field above) — never
both legs' numbers in one card.

## The entry-only TRIGGERS gate (SPEC-189 §B)

`eval_price_leg`'s verdict is `TRIGGERS` when **either** `tps_printed` **or**
`entered_zone` — but only `entered_zone` is a genuine entry. FET's verdict flipped to
TRIGGERS purely off a TP print (price already 9% *through* the zone, not into it); the
channel posted `ENTRY TRIGGERED · Entry 0.168–0.170` while a reader shorting at market
would have been entering at the TP.

`board_tick._entry_gate_ok(board_row, row_fields, matched, live_price)` requires
genuine entry evidence — `matched` (a leg's watch_level entry gate crossed this tick,
see above) or, for a single-leg/legacy thesis, `price_leg["entered_zone"]` — before a
TRIGGERS card is even attempted; `tps_printed` alone is deliberately **not** evidence,
which is what silences a TP-print-only flip, a `ZONE_BLOWN` overrun, and a
thesis_drift/STALE-driven transition alike, without ever string-matching the verdict
`reason` text. A belt-and-braces sanity check then requires the **live mark to be
within 3% of the firing leg's entry zone** at post time (using the price the board
already fetched — `format_card` itself still never reads the tape); outside that band,
no card, and one line logs to `state/telegram.err` with the reason. BREAKS and
STOP_BREACH cards are unaffected — this gate only narrows TRIGGERS.

## NEW SETUP / CANCELLED (SPEC-189 §C)

`board_tick._setup_sweep` runs every tick (independent of the price-board diff
baseline) and diffs `thesis_by_ticker` against `state/thesis_seen.json`, keyed by
`(ticker, committed_ts)`:

- a ticker+committed_ts pair not seen before (status `WATCH`/`ARMED`/`LIVE`/`PENDING`
  and `has_leg_geometry` true) → **NEW SETUP**, one-shot key `tg:<T>|setup|commit`;
- a previously-tracked ticker no longer present → **CANCELLED · setup withdrawn**, one-
  shot key `tg:<T>|setup|retire` — **unless** a STOP_BREACH or BREAKS card already
  posted for that ticker (checked via the existing `tg:<T>|stop_breach|stop` /
  `tg:<T>|verdict|BREAKS` armed state) — a public setup that already stopped out or
  broke is not also "withdrawn".

Each transition disarms the *sibling* key (`retire` on a fresh commit, `commit` on a
retire) so a later retire-then-recommit cycle posts a fresh card each time, not just
once ever. The first-ever run seeds `state/thesis_seen.json` quietly (no NEW SETUP
flood over the whole existing watchlist) — same convention as the price-board
baseline's own first-run seed. This is the **belt-and-braces** leg: whatever put the
thesis in `config/watchlist.json` — `thesis.py --op commit`/`--op retire`, or a
hand-edit — this diff catches it, independent of which code path wrote the file.

## Delivery (`ops/telegram_post.post`)

```
post(text) -> bool
```

POSTs `https://api.telegram.org/bot<TOKEN>/sendMessage` with `chat_id=<CHAT_ID>`,
`disable_web_page_preview: true`, a 10s timeout. Credentials are resolved **at send
time** (no plist render, unlike `ops/install_launchd.sh`'s `__NTFY_URL__`):

1. env `CRIMEDESK_TG_BOT_TOKEN` / `CRIMEDESK_TG_CHAT_ID`, else
2. one-line files `~/Library/Application Support/crimedesk/telegram_bot_token` and
   `~/Library/Application Support/crimedesk/telegram_chat_id`.

Missing or empty credentials = silent no-op (the feature is simply off). Best-effort
like `notify.sh`: `post` never raises; an HTTP failure appends one line to
`state/telegram.err` and returns `False`. `CRIMEDESK_NOTIFY=off` disables the leg
entirely — checked both inside `post()` (so a direct call is silenced) and inside
`board_tick.py`'s `_tg_push` (so an injected `tg_post_fn` in tests/replay is never
invoked either, same pattern as the ntfy kill-switch check).

## Wiring (`ops/board_tick.py`)

`_tg_push(ticker, cls, level, thesis)` sits inside the same `push_worthy` block that
already drives the ntfy leg (`cls == "stop_breach"` or `cls == "verdict" and level in
("TRIGGERS", "BREAKS")`) — it is never called for any other class, so there is no
separate filter to keep in sync. It picks the firing leg (`_pick_firing_leg`), gates a
TRIGGERS card on genuine entry evidence (`_entry_gate_ok`, SPEC-189 §B above), then
calls `format_card`. `_setup_sweep` (SPEC-189 §C) runs separately, once per tick,
independent of `push_worthy`/the price-board diff.

One-shot delivery for all five event classes reuses the exact SPEC-154 armed-key
machinery (`_arm_check`/`_disarm`) the ntfy leg uses, under a **`tg:`-prefixed key** in
the same `state/notify_sent.json` store — a key transitions to armed once (never-seen
or previously disarmed) and delivers; it stays silent while continuously armed. The
prefix keeps this leg's delivery record from ever colliding with or being read by the
ntfy leg's own keys, so a failure or a kill-switch flip on one leg never perturbs the
other's dedup state. **The key is consumed only once `post()` returns `True`**
(SPEC-189 §A) — a `None` card (nothing sendable) or a failed send leaves the key
armed=False so the next tick retries, rather than silently losing the call.

## BotFather setup (human's call)

1. Message `@BotFather` on Telegram → `/newbot` → name it, get the bot token.
2. Create (or use an existing) public channel; add the bot as an **admin** with post
   permission.
3. Get the channel's `chat_id`: for a public channel this is `@channelusername`
   (usable directly as `chat_id`), or resolve the numeric id via
   `https://api.telegram.org/bot<TOKEN>/getUpdates` after posting once in the channel.
4. Write the token and chat id to the one-line files above (or export the env vars for
   a one-off run):
   ```
   mkdir -p ~/"Library/Application Support/crimedesk"
   echo -n "<TOKEN>"   > ~/"Library/Application Support/crimedesk/telegram_bot_token"
   echo -n "<CHAT_ID>" > ~/"Library/Application Support/crimedesk/telegram_chat_id"
   ```

## Manual test

```
python3 -c "import sys; sys.path.insert(0,'ops'); import telegram_post as T; print(T.post('crime-desk TEST'))"
```

## Gotchas

- `tests/test_telegram_post.py` and the telegram-leg test classes (`_TelegramTestBase`
  subclasses) in `tests/test_board_tick.py` all redirect
  `TOKEN_FILE`/`CHAT_ID_FILE`/`ERR_PATH` (and clear the `CRIMEDESK_TG_*` env vars) to a
  tmpdir — any test exercising `board_tick`'s *default* (uninjected) `tg_post_fn` path
  MUST do the same, or a machine with real credentials configured would have a test
  suite silently post to the live channel.
- `format_card`/`format_setup_card`/`format_cancel_card` never read live price — the
  allowlist deliberately stops at the committed thesis numbers (`entry_zone`/`stop`/
  `tp`), not the tape. The one exception is the caller-side sanity check in
  `board_tick._entry_gate_ok` (SPEC-189 §B) — it reads the live price the board already
  fetched to decide whether to *call* `format_card` at all, but never passes it in.
- `board_tick._setup_sweep` needs `config/positions.json` and `config/watchlist.json`
  isolated too (`_Tmp` does this for every test) — an unmocked read of the real files
  feeds `_oic_watch_sweep` real tickers, which makes real network calls (a multi-minute
  test hang, not an offline unit test).
