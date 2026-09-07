# claim_topup — claim/distributor top-up tripwire (SPEC-119)

`unlocks`/`stake_schedule` read the **schedule** (contract terms, cliff dates). `claim_topup`
watches the **live pre-signal**: the workshop BARD case — the claims/distribution wallet was
topped up twice before claims opened; once claims began, recipients moved tokens to exchanges
and price fell ~50%. A top-up is the team physically staging the supply — it converts a
calendar date into an armed, on-chain-confirmed event, often days early, and catches
unscheduled/rolling distributions the calendar never sees.

```
python3 capabilities/claim_topup.py BARD --json          # one-ticker read (no persist)
python3 capabilities/claim_topup.py --fire-alerts --json # sweep hook (surveil.sh cadence)
```

## Registry-gated (empty registry = feature inert)

A wallet in `config/tracked_wallets.json` `tokens.<TICKER>.wallets` tagged
`"kind": "claim-distributor"` is watched. Everything else is a no-op — no registration, no
checks, no false signal.

## Detection

On each sweep (`--fire-alerts`, wired into `ops/surveil.sh` alongside `unlocks.py
--fire-alerts` — same cadence, no new loop): every registered distributor's inbound transfers
(via `onchain.token_transfers`, the SPEC-97/101/102 provider seam) are checked over
`lookback_days`. An inbound transfer clearing **either** `min_usd` or `min_pct_float` (of the
token's circulating float) is a TOP-UP. Dedup is cursor-based per distributor
(`state/claim_topup_<TICKER>.json`) — a transfer only ever fires once.

Each top-up event carries: distributor, source address + classification (team safe /
tracked-tier / known entity / unknown-staging-EOA), token amount, USD value, % of float, and
whether an `unlocks`/`stake_schedule` calendar entry exists for this ticker
(`unlocks.next_catalyst_for`) — a top-up with **no** calendar entry is flagged `unscheduled`
(the more alarming variant: a rolling/off-calendar distribution).

## Firing loud

A new top-up → one **HIGH** `inbox` event per transfer:

```
CLAIM TOP-UP: BARD distributor 0xdef... funded +48.0M ($390.0K, 4.8% float) from team safe
0x123... — claims imminent, unlock calendar says cliff 2026-07-15
```

While a top-up is within `decay_window_h` (default 96h), `unlocks.build_unlocks()` carries it
in a `claim_topup` field (`recent_annotation()`) — so the calendar view and `brief`'s on-chain
section see it without a second query. The §4-B unlock-cliff-fade lens
(`memory/feedback_unlock_cliff_fade_n2_m_win`) is the arming-signal pointer for this event —
no auto-thesis, committing remains the orchestrator's job.

## Provider-gated, never a false quiet

A handful of addresses — cheap. A quota-exhausted/down provider on one distributor reports
`unavailable: [{distributor, address, reason}]` for that check; it is never read as "no
top-up" (CLAUDE.md §3), and never blocks the other registered distributors.

## Config

`config/claim_topup.json` (documented defaults, any key overridable):
`min_usd` (200000), `min_pct_float` (2.0), `lookback_days` (14), `decay_window_h` (96).

## Contract

```json
{ "ticker": "BARD", "checked": 1,
  "topups": [ {"ticker","distributor","distributor_address","source_address","source_kind",
               "amount","usd","pct_float","tx","ts","unscheduled","catalyst"} ],
  "unavailable": [ {"distributor","address","reason"} ] }
```

`--fire-alerts` → `{"fired": int, "checked_tickers": int, "results": [per-ticker read]}`.

## Tests

`tests/test_claim_topup.py`: registry-gated inert paths, two-topups-above-floor (source/size/
float%), unscheduled flagging, below-floor no-fire, outbound-is-not-a-topup, provider-down
`unavailable` (never false-quiet), cross-sweep dedup, and the `unlocks.build_unlocks`
`claim_topup` annotation linkage (injected seam).
