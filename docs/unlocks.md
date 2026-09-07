# unlocks — auto-populated token-unlock calendar + pre-unlock alerts (SPEC-95)

Unlocks are the most **predictable** supply event, and the desk missed the BEAT/Audiera one
entirely (21.24M / 7.4% / ~$60M, one day out, invisible until a human brought a Cointelegraph
headline). `unlocks` stops discovering them reactively: it pulls a public unlock feed, sizes
each event off the **canonical** supply, auto-populates `config/catalysts.json`, surfaces a
flag on the board/brief, and fires a once-per-event pre-unlock alert at **T-3d** and **T-1d**.

```
python3 orchestrator.py unlocks '{"ticker":"BEAT"}'        # upcoming unlocks for one token
python3 orchestrator.py unlocks '{"op":"sweep"}'           # full-watchlist → catalysts.json
python3 orchestrator.py unlocks '{"op":"fire-alerts"}'     # fire due T-3d/T-1d inbox alerts
```

## Why it exists

`config/catalysts.json` already existed as the manual "forward catalyst calendar" — but nobody
typed dates into it, so the desk discovered unlocks in a pull. `stake_schedule` reads *on-chain*
vesting contracts, but most unlocks (incl. BEAT/Audiera) are **managed / off-chain** and return
an empty on-chain schedule. `unlocks` automates the calendar from a public feed and pairs with
`stake_schedule` for on-chain confirmation where readable.

## Sizing discipline — the BEAT bug fix

`pct_circulating`, `usd_notional` and `vs_daily_volume` are computed off the **canonical
CoinGecko circulating supply + live price** (via `pull5.coingecko_layer`) — **never** a
single-chain GoPlus `total_supply`. The BEAT miss: GoPlus read 612k on BSC vs the real ~288M
circ, producing a nonsense **3431%**. Off the canonical 288M the same 21.24M unlock is the
correct **7.4% / ~$60M / 3.4× daily volume** — and `vs_daily_volume` is what makes a small-%
unlock matter on a thin name.

## Identity discipline (req #6)

Every read keys off the verified **cg_id**, not the bare ticker — multiple tokens share
tickers (BEAT = Audiera, not the other BEAT). `CG_ID_OVERRIDE` pins known collisions
(`BEAT → audiera`); an explicit `cg_id` arg overrides; otherwise the live CoinGecko resolution
(collision-safe, market-cap-ranked, perp-price-sanity-checked) supplies it.

## Sources

| layer | source | notes |
|---|---|---|
| PRIMARY (auto) | DefiLlama emissions datasets `https://defillama-datasets.llama.fi/emissions/<slug>` | public, key-free; ~340 tracked protocols. `api.llama.fi/emissions` is paywalled (402); CryptoRank is 401. |
| FALLBACK / override | `config/unlock_seed.json` | cg_id-keyed entries for names the auto feed doesn't track yet (brand-new tokens like Audiera). Populate from a public unlock tracker; remove once the feed picks the token up. |

DefiLlama `noOfTokens` are summed token-units; the seed path is authoritative on the controlled
names (it carries an exact amount). Both are sized identically off the canonical CG supply.

### `detail` template rendering (SPEC-110)

DefiLlama's per-event `description` is sometimes a **literal, unsubstituted template** —
`"On {timestamp} {tokens[0]} of Team tokens were unlocked"` — a real API quirk (the DefiLlama
frontend renders it client-side off parallel `timestamp`/`noOfTokens` arrays; the raw feed
never substitutes it). `parse_defillama` now renders `{timestamp}` and `{tokens[N]}` from the
event's own fields before persisting `detail`; if any placeholder can't be resolved, it falls
back to a self-composed `"<amount> <category> tokens unlock on <date>"` (category pulled from
whatever plain text survives around the placeholders, else the unlock `type`). A `{placeholder}`
literal must never reach `config/catalysts.json` — this was the 2026-07-03 M/memecore row.

## Coverage honesty (req #7)

- `coverage: false` — **the source was down** (network error on every candidate). Degrade
  gracefully; never crash the brief.
- `uncovered: true` — the source **answered but has no record** for this token (looked, found
  nothing). Distinct from "no unlock" — the sweep logs uncovered watchlist tokens so "no data"
  is never silently read as "no unlock".

## Surfacing (req #3)

`classify` (board + single-ticker, via `annotate_unlocks`) and `brief` (`_unlocks_layer`) read
the **sweep-populated `catalysts.json` offline** (no network in the board/brief path) and surface
the flag for any unlock ≤ `horizon` (default 7d):

```
⏰ UNLOCK T-1d: 7.4% / $60M cliff (3.4× daily vol)
```

This is a READ that prefixes the `reason` / leads the headline — it never overrides a verdict
(§0.5).

## Pre-unlock alerts (req #4)

`op=fire-alerts` fires one inbox event per `(event, leg)` at **T-3d** (`1 < days ≤ 3`) and
**T-1d** (`0 ≤ days ≤ 1`), cursor-deduped via `state/unlock_alert_cursor.json` (mirrors the
watch-armed / nonce-surveil dedup). Severity HIGH when `pct_circulating ≥ 5` or
`usd_notional ≥ $10M` or `vs_daily_volume ≥ 2`, else MED. Wired into `ops/surveil.sh` (every
nonce tick). The full sweep that populates the calendar is wired into `ops/board_tick.sh`,
time-gated to once / ~12h so the per-token CoinGecko reads never hammer the API.

## I/O contract

**In:** `{ticker?, cg_id?, horizon?, op?}` — `op` ∈ `ticker` (default) | `sweep` | `fire-alerts`.

**Out (ticker-mode):**

```jsonc
{
  "ticker": "BEAT", "cg_id": "audiera",
  "coverage": true, "uncovered": false, "source_down": false,
  "supply_available": true,
  "events": [{"date":"2026-07-01","amount":21240000,"type":"cliff","cg_id":"audiera",
              "source":"public-unlock-tracker...","pct_circulating":7.37,
              "usd_notional":62020000.0,"vs_daily_volume":3.44,"days_until":0,"upcoming":true}],
  "next_unlock": { ... },
  "within_horizon": true, "horizon_days": 7,
  "flag": "⏰ UNLOCK T-0d: 7.37% / $62M cliff (3.44× daily vol)"
}
```

**Out (sweep):** `{scanned, covered:[{ticker,n,next,flag}], uncovered:[ticker], source_down:[{ticker,reason}], written, refreshed, n_events}`.

**Out (fire-alerts):** `{fired: <int>}`.

## Tests

`tests/test_unlocks.py` — offline-deterministic (the public feed and the canonical-supply
lookup are injected; a fixed `now` drives every date/leg assertion). Covers: canonical sizing
(the 612k→288M fix), the BEAT/Audiera seed read, source-down vs source-up-no-data, DefiLlama
parsing, flag formatting + horizon, manual-preserving catalyst merge, T-3d/T-1d leg windows +
dedup, and the classify reason surfacing.
