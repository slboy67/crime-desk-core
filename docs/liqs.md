# liqs — per-venue forced-liquidation stream as OI ground truth (SPEC-103)

```
python3 orchestrator.py liqs '{"ticker":"OPN"}'
python3 orchestrator.py liqs '{"ticker":"OPN","window":4,"bar":15,"oi_delta":-18.0}'
```

## Why it exists

The desk reads OI as squeeze fuel / capitulation / wash — but on demon coins aggregate OI is
operator-**fakeable** (double-open 对敲, multi-account internal transfer: memory
`feedback_aggregate_oi_faked_via_double_open`). The BSB case: OI↑ + price↓ + CVD↓ read as
"dealer piling shorts", yet post-pull-up short OI was **lower** than pre-pull — the pile was
bait (memory `feedback_hidden_build_cvd_oi_price_divergence`). The corpus rule: **"when OI is
faked you can't read OI — raw candles + liquidation data are the only truth"** (memory
`reference_derq_freeland_corpus_playbook` §4) — a liq print is a real forced close, it cannot
be faked the way an aggregate OI number can. `liqs` gives the desk that cross-check as a
first-class input instead of only entering when a human pastes a heatmap.

## Venue reality (live-verified 2026-07-02 — the ticket's assumed primary is dead)

The spec named Binance USDT-M `forceOrder` as the primary source. Live-checked this session:

| Endpoint | Result |
|---|---|
| Binance `/fapi/v1/forceOrders` | `401 API-key format invalid` — needs an authenticated key, always has for the all-symbols form |
| Binance `/fapi/v1/allForceOrders` | `400 The endpoint has been out of maintenance` — deprecated |
| Bybit `/v5/market/liquidation` | `404 Not Found` — removed |
| **OKX `/api/v5/public/liquidation-orders`** | **Works, keyless, public** — confirmed live against a real desk-tracked token (OPN) |

This matches `tape.py`'s own existing finding (`fetch_force_orders`, SPEC 31) that Binance's
REST liquidation feed is auth-gated on the free path — `liqs` doesn't rediscover that, it
routes around it. **OKX is the actual v1 venue** — single-venue is an acceptable v1 per the
ticket's own DoD, with the venue named in every result (`venue: "okx"`). Binance is kept coded
(`fetch_liqs_binance`) as a documented dead seam — always returns `None`, so a future
authenticated key or a websocket client (`!forceOrder@arr` is still public, but this codebase
is stdlib-`urllib`-only; no websocket dependency exists anywhere in it) can populate it without
touching the provider-seam wiring.

OKX needs `uly=<TICKER>-USDT` (the underlying) — an unlisted ticker returns
`code:"51014" / "Index doesn't exist"` cleanly, which `fetch_liqs_okx` maps to `None`
(unavailable), never an empty-but-"answered" list.

## Contract

`build_liqs(ticker, window_h=4, bar_minutes=5, oi_delta_pct=None, price_flat=None, cvd_confirms=None)`
→
```
{available, venue, window_h, bar_minutes,
 bars: [{ts, long_usd, short_usd, count, largest_usd, cum_long_usd, cum_short_usd, liq_silence}],
 total_long_usd, total_short_usd,
 oi_liq_consistency: {verdict, reason}}
```

`oi_liq_consistency` verdicts (never computed without a caller-supplied `oi_delta_pct` from
the same window — this capability does not fetch OI itself, callers already have it):

- **`CONFIRMED`** — the OI move is accompanied by matching liq prints (a real capitulation/
  squeeze leg going down, or a real build corroborated by liq activity/a CVD step going up).
- **`SUSPECT_FAKE`** — a material OI move (threshold `config/liqs.json:material_oi_pct`,
  default 10%) with liq-silence and no confirming CVD step → the double-open/internal-
  transfer fingerprint (the BSB pattern).
- **`TRANSFER`** — OI **down** + liq-silence + flat price → the multi-account profit-transfer
  fingerprint (corpus playbook §4) — distinct from `SUSPECT_FAKE` by the flat-price tell.
- **`UNKNOWN`** — no liq feed at all (venue down / symbol unlisted). **Never a clean bill on
  an empty stream** — `liq_bars=None` is checked first and unconditionally returns `UNKNOWN`,
  it can never silently read as `CONFIRMED` (§3: a zero/null print is a data FAILURE, not a
  datum).
- **`null`** — the OI move is below the materiality threshold; nothing to corroborate, no
  line is worth printing.

## Integration

- **`brief`** — a 5th concurrent layer (`_liqs_layer`, same `LAYER_BUDGET` timeout every other
  layer already respects — no new blocking latency) feeds `build_analyse`'s `oi_chg_pct` in as
  the OI-delta input; the one-liner (`liqs.one_liner`) appends to the headline whenever a
  verdict is present.
- **`tape`** — `liq_consistency` is computed from the OI series `tape` already fetched (no
  extra OI call), purely additive to its output; never touches the existing per-bar
  `fetch_force_orders`/`classify_bars` forced-close classification (SPEC 31's own liq path,
  left untouched — this is a window-level cross-check, not a bar-level one).
- **`workup`** — `oi_sides_read` (the delegated `_oldrepo/scripts/oi_sides.py` WASH/REAL tag)
  gains a `liq_corroborator` field when the tag is `WASH`, computed natively in `workup.py`
  (the delegated script itself is never touched, per the coder guardrail against editing
  `_oldrepo`).

## Tests

`tests/test_liqs.py` — offline-deterministic, every venue fetch injected via `fetch_fn`/
`_PROVIDERS`, no live network. Covers bucketing, all four DoD verdict fixtures (cascade→
CONFIRMED, BSB-shaped spike→SUSPECT_FAKE, bleed+flat→TRANSFER, feed-down→UNKNOWN), provider
fallthrough, and the one-liner.
