# venue_account — READ-ONLY Aster account capability (SPEC-170, -171, -186)

## Why it exists

ADR-0001: the desk READS the venue, it never executes. This is the one module that
touches Aster's signed (API-wallet / EIP-712) endpoints — equity, positions, fills,
resting orders, per-symbol max leverage — so `config/positions.json` and any sizing
math stop drifting from what the venue actually shows.

```
python3 capabilities/venue_account.py equity --json
python3 capabilities/venue_account.py positions --json
python3 capabilities/venue_account.py open_orders --json
python3 capabilities/venue_account.py fills --since 2026-08-01T00:00:00Z --json
python3 capabilities/venue_account.py max_leverage --write --json
python3 capabilities/venue_account.py sync --dry-run --json
```

## Boundaries (hard)

- **Read-only by construction, not by key permission** — `READ_PATHS` is the entire
  allowlist of endpoints this module can ever call. No `/order`, `/batchOrders`,
  `/allOpenOrders` (DELETE), `POST /leverage`/`/marginType`/`/positionMargin`,
  transfer/withdrawal, or either strategy-order **mutation** endpoint
  (`/placeStrategyOrder`, `/updateStrategyOrder`) is ever reachable —
  `tests/test_spec170_venue_account.py`'s `ReadOnlyByConstruction` class greps this
  file for those paths and any non-GET verb and fails the suite if found.
- Aster's V3 API signs every request as EIP-712 typed data (an API-wallet `signer` +
  its private key), not Binance-style HMAC — see the module docstring for the scheme
  and source.
- `sign_params` is scoped to this module; it is not exposed as a general
  "signed-request" utility a future caller could point at a mutating endpoint.

## Commands

- `equity` — perp total value / available / unrealized PnL (the Aster UI's "Perp
  Total Value").
- `positions` — open positions with TP/SL **attached to that position**
  (`resting_orders: [{type: STOP|TP, price, reduce_only}]`, sourced from
  `/fapi/v3/openOrders`). A dead openOrders leg degrades `resting_orders` to `[]`
  per position — never fatal to the positions read itself.
- `open_orders` — SPEC-186, see below.
- `fills` — `userTrades` per symbol; `--symbols` omitted resolves the query set from
  live venue positions + `config/positions.json` `positions[]`/`closed[]` within
  `--since` (SPEC-171) — an empty resolved union is a loud `fills_no_symbols_resolved`,
  never a silent `[]`.
- `max_leverage` — per-symbol max leverage from `leverageBracket` (exchangeInfo's
  margin fields do NOT encode it). `--write` refreshes
  `config/aster_max_leverage.json`, but only after `MAX_LEV_SANITY` (slider-verified
  values) passes — a bad/partial read never corrupts the size-cap config.
- `sync` — reconciles `config/positions.json` against the live venue read.
  `VENUE_OWNED_FIELDS` are overwritten on `--apply`; `DESK_OWNED_FIELDS`
  (`signature`, `thesis_ref`, `stop`, `tp`, ...) are never touched. A desk row with no
  matching venue position is `closed_on_venue`; a venue position with no desk row is
  `untracked`.

## `open_orders` (SPEC-186) — conditional/strategy orders

**Problem this closes (live-verified 2026-09-01):** a user's 3-leg OTOCO-style order
set (limit entry + stop + TP), UI-confirmed resting, was fully invisible to
`/fapi/v3/openOrders` while $42.80 of equity sat reserved — the ADR-0001 away-case
("the user's resting orders are the safety net") was unverifiable.

**Endpoint hunt (live-fetched 2026-09-01 from `github.com/asterdex/api-docs`,
`V3(Recommended)/EN/aster-finance-futures-api-v3.md`):** the only conditional/strategy
**read** Aster serves is `GET /fapi/v3/strategyOpenOrder`, and it is **ID-scoped** —
either `strategyId` or `clientStrategyId` is mandatory. There is **no bulk-list
endpoint** for strategy orders anywhere in the documented API; `V1(Legacy)` has no
strategy endpoints at all (the ticket's `/fapi/v1/openOrders: null` was simply an
unserved path, not a missing param). So a standalone conditional set is **structurally
unlistable** without already knowing its ID — this is an Aster API gap, not a bug in
this module.

Given that constraint, `open_orders(strategy_probes=None, ...)`:

1. Always reads regular `/fapi/v3/openOrders` and maps every row to the uniform shape
   `{symbol, kind, price, stopPrice, qty, reduce_only, attached_to_position}` —
   `kind` is `limit`/`stop`/`tp` from the order `type`; `attached_to_position` is true
   iff the symbol has an open venue position.
2. Resolves any **known** conditional/strategy order via `strategy_probes`
   (`[{"strategyId" | "clientStrategyId": ..., "strategyType": "OTO"|"OCO"|"OTOCO"}]`
   — pass one when the desk has hand-verified an ID against the UI, e.g. via
   `--client-strategy-id`/`--strategy-id`/`--strategy-type` on the CLI). Each
   `subOrders[]` entry maps to the same uniform shape; a suborder whose
   `firstDrivenId` points at another suborder (an OTO/OTOCO leg that only activates
   once its driver fills) has no live venue order yet — it is a **planned** leg, not a
   resting one, so its `kind` is `conditional-entry` regardless of its eventual
   limit/stop/tp type.
3. Cross-checks the **margin reservation**: `equity.total − equity.available − Σ
   position margin`. When that delta is `> $1` and the resolved order list came back
   **empty**, the gap has nothing visible to explain it — direct evidence of a resting
   order this read could not see. This is exactly the signal that caught the real
   2026-09-01 gap.

`orders_coverage` is `"partial"` (with a `coverage_reason`) whenever any probed path
(openOrders, a given strategy probe, or the equity/positions reads backing the
reservation check) errors/is unserved, OR the reservation gap fires. It is `"full"`
only when every probed path succeeded AND (orders were found, or the margin
reconciles). **This never renders "no orders" from a blind read** — an empty `orders:
[]` with `orders_coverage: "full"` means the account is genuinely flat, not that the
read failed quietly.

Caveat: once *any* order is resolved, the reservation warning is suppressed —
`open_orders` does not attempt per-order margin attribution (Aster's strategy-suborder
response carries no margin field), so a partially-resolved set alongside an
*additional* untracked reservation would not currently re-flag. Surfacing the known
legs already closes the ADR-0001 unverifiable-away-case gap; exact margin attribution
is future scope if it proves necessary.
