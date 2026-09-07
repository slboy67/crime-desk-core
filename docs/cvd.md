# cvd — spot-vs-perp CVD with REAL spot-venue resolution (SPEC 42)

The spot-CVD leg of the §4 neg-funding-LONG confluence gate, ported native. The old
`_oldrepo/scripts/cvd_spot_perp.py` leg was Binance-centric — a Bitget-primary or
DEX-primary name got a structurally blind spot read that still reported a verdict.
This capability resolves the token's **real primary spot venue first**, then computes
the divergence from that venue's flow.

## Call

```
python3 capabilities/cvd.py SKYAI --json
python3 capabilities/cvd.py LAB --min 60        # window minutes (default 30)
```

## Venue resolution (by 24h volume)

1. **Binance spot** vs **Bitget spot** — 24h quote volume compared; the larger book is
   primary (Binance wins a tie). Binance is *not* assumed.
2. **GeckoTerminal DEX pools** — only when neither CEX lists the pair. The contract is
   resolved from tracked config: `config/tracked_wallets.json` `tokens[SYM].contracts`,
   falling back to `config/watchlist.json` `contract_bsc`. CVD is read from the
   top-volume pool; `spot_vol_24h_usd` sums all pools.
3. Nothing anywhere → `spot_coverage:"none"` + `verdict:"UNAVAILABLE"` (degrade-EXPLICIT,
   SPEC-25 doctrine — never a silent fail).

## CVD sources per venue

- **binance** — spot 1m klines; aggressor split from the taker-buy quote-volume fields
  (buy = takerBuyQuote, sell = quote − takerBuyQuote).
- **bitget** — spot public fills (`side` = taker side), size×price USD.
- **dex:\<net\>** — GeckoTerminal pool trades. **Declared taker-side heuristic** (also
  emitted as `taker_side_heuristic`): GT's `kind` ("buy"/"sell") is the swap direction
  vs the pool's base token; AMM swaps are always taker-initiated, so `kind` maps 1:1 to
  the aggressor side.
- **Perp** is always Binance futures 1m klines — parity with what `analyse` compared
  previously.

Verdict math/thresholds are ported verbatim from the proven old script
(`divergence_verdict`): window spot < $1M → `UNRELIABLE_THIN_SPOT` (unless 24h spot
≥ $5M → `SPOT_REAL_WINDOW_THIN`); spot < 15% of perp → `UNRELIABLE_PERP_DRIVEN`;
< 30% → `low_confidence`; then `BULLISH_DIVERGENCE` / `BEARISH_DIVERGENCE` /
`ALIGNED_BULLISH` / `ALIGNED_BEARISH`.

## Contract

```json
{ "ticker":"SKYAI", "verdict":"BULLISH_DIVERGENCE", "reliable":true,
  "spot_venue":"bitget", "spot_vol_24h_usd":21000000, "spot_coverage":"full",
  "spot_cvd":2000000, "perp_cvd":-2000000, "spot_tot":4000000, "perp_tot":4000000,
  "window_min":30.0 }
```

- `spot_venue` ∈ `binance | bitget | dex:<network> | null`
- `spot_coverage` ∈ `full` (CVD computed from the primary venue) · `partial` (venue
  resolved with real 24h volume but the trades/klines window read failed this run →
  `verdict:"NO_DATA"` + `note`) · `none` (no spot anywhere → `verdict:"UNAVAILABLE"`).
- `taker_side_heuristic` present on DEX reads only.

## analyse wiring (the consumer)

`capabilities/analyse.py` runs this layer natively (`run_cvd`, replaces the old
`run_json("cvd_spot_perp.py", …)` subprocess). Its output adds `cvd_venue`,
`spot_coverage`, `spot_vol_24h_usd`, and its `notes[]` always name the venue the CVD
came from. On `UNAVAILABLE` the §4 gate leg reads **UNKNOWN** — the neg-funding LONG
still cannot be confirmed (WATCH), but the BEARISH hedge-trap veto never fires off a
missing read, and a positive-funding LONG is not vetoed (tier-downgraded to MILD only).

## Tests

`tests/test_cvd_venue.py` — offline/mocked: Bitget-primary → CVD from Bitget fills,
venue named; Binance-primary unchanged (and Bitget fills never pulled); DEX-primary
with the declared heuristic; no-spot-anywhere → `none`/`UNAVAILABLE`; partial path;
analyse wiring (venue in notes + `cvd_venue`, UNAVAILABLE = UNKNOWN leg, no false
negative).
