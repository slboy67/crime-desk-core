# venue_map — full cross-venue coverage map (§0.6.3b, SPEC-129)

## Purpose
The §0.6.3b read — *across which venues is the OI constructed, and which venue plays
which role (mark-engine / size-book / exit / hedge)* — had no deterministic capability.
`regime_check` covers Binance/Bybit/Bitget/Aster; `hyperliquid` is separate; the DEX
perp long tail (Lighter, Paradex, Extended, Vest, …) where AMM crews increasingly run
their books was invisible to the desk. The SKYAI lesson (Binance CONFIRMS while the
real book was Bitget — `memory/feedback_skyai_exit_liquidity_is_bitget.md`) generalizes:
the same miss can happen with the real book on a venue the desk doesn't poll at all.
`venue_map` is one live sweep answering: which venues list this perp, and where is the
book.

## Contract
```
venue_map '{"ticker":"LAB"}'
```
Out:
```json
{
  "ticker": "LAB",
  "venues": {
    "binance": {"status": "ok", "funding_raw_pi": -0.20, "interval_min": 480,
                "funding_pi_4h": -0.10, "is_floor": false,
                "oi_usd": 40000000.0, "oi_share_pct": 40.0, "vol24h_usd": 5000000.0},
    "aster": {"status": "not_listed"},
    "paradex": {"status": "error", "reason": "timeout >6s"}
  },
  "n_listed": 4,
  "total_oi_usd": 100000000.0,
  "total_vol24h_usd": 5000000.0,
  "top_oi_venue": "binance",
  "oi_top_share_pct": 40.0,
  "funding_extreme": {"venue": "hyperliquid", "pi_4h": -2.0}
}
```

## Venue set
`binance`, `bybit`, `aster`, `bitget` (existing CEX four) + `hyperliquid` + the DEX long
tail: `lighter`, `paradex`, `extended`, `vest` + the CLAUDE.md §0.5 "10-33% of the mark"
tier: `gate`, `mexc`, `kucoin`, `okx`, `htx` (SPEC-176) + `bingx` (verify-first bonus,
see below). Each is a `probe_<venue>(ticker) -> {status, ...}` function behind the
`VENUES` dict — adding a venue later is one function + one dict row (the SPEC-97
provider-seam pattern).

- `total_vol24h_usd` (SPEC-176) — sum of `vol24h_usd` over `status:"ok"` venues
  reporting it (same discipline as `total_oi_usd`; a venue whose adapter doesn't
  resolve volume just doesn't contribute, never a fabricated 0).

## §3 data-failure doctrine
A probe error, timeout, or malformed response is `status:"error"` for that venue —
**never** rendered as `"not_listed"` and never a fabricated `0` datum. `"not_listed"`
means the venue answered cleanly and confirmed no market (Binance `-1121 Invalid
symbol`, Bybit `retCode 10001`, Bitget `code 40034`, Paradex
`INVALID_REQUEST_PARAMETER`, Lighter/absent from `orderBookDetails`, Extended's
all-zero-field `200 OK` for an unlisted market — a genuinely listed perp never marks at
exactly `0`). Floor prints (the CEX 0.005%-sentinel via `regime_flip._is_floor`; an
exact-zero rate on DEX-native venues, mirroring `hyperliquid.is_floor`) are flagged
`is_floor` and excluded from `funding_extreme`, but still shown.

## Per-venue optional fields — `oi_raw` / `mark_price` (SPEC-178 prep)
Additive, present only when the venue's own response carries the base-asset-unit OI
figure and/or mark price alongside its USD conversion — never re-derived by dividing
`oi_usd`/`mark_price` (that would silently fabricate precision the venue never
published). `oi_raw` is genuinely absent on venues that publish OI already in USD with
no separate base-asset field (Extended, Vest, BingX). Exists so a downstream OI sampler
(the ΔOI-elasticity/redenomination read) can store the raw series untainted by this
probe's own USD conversion — a venue's contract-size/multiplier redenomination breaks a
raw-unit series in a way a USD-only series hides.

## Derived fields
- `total_oi_usd` / `oi_share_pct` / `top_oi_venue` / `oi_top_share_pct` — concentration,
  computed over `status:"ok"` venues with a reported `oi_usd` only.
- `n_listed` — count of `status:"ok"` venues.
- `funding_extreme` — the most-extreme (max `abs()`) **non-floor**, already
  4h-normalized (SPEC-112) print across ALL listed venues (SPEC-108 convention, same
  rule `triage._select_funding` uses) — a 1h venue's raw print is normalized before
  comparison so it never loses to a larger-looking raw 4h print, or wins on a raw print
  that would lose after normalization.

## Concurrency (SPEC-127 lesson)
`build_venue_map` fans every venue probe out on its own thread, bounded by
`per_venue_timeout` (default 6s) via `ThreadPoolExecutor` + `future.result(timeout=…)` —
the same pattern `brief.py` uses. One dead/slow venue is isolated to that venue's
`status:"error"`; the rest of the sweep is unaffected. Registered capability timeout is
60s; live wall-clock with all venues up is ≤20s.

## Reuse
- `regime_flip.to_4h` / `._is_floor` / `.binance_interval_min` / `.bybit_interval_min` —
  the existing normalization/floor layer (CEX four).
- `hyperliquid.resolve` / `.universe` / `.coin_for` / `.is_floor` — the existing HL
  venue source (SPEC-84).
- New code is limited to the venues those modules don't cover (Lighter/Paradex/
  Extended/Vest) plus a `not_listed`-vs-`error` fetch wrapper (`_get`) —
  `regime_flip.fetch()` discards HTTPError response bodies, so it can't distinguish
  "invalid symbol" (a structured 4xx) from a real timeout/connection failure.

## Live-verified endpoints (2026-07-29)
- **Lighter** — `mainnet.zklighter.elliot.ai/api/v1/orderBookDetails` (mark price, base
  OI, 24h quote volume) + `/api/v1/funding-rates` (filter `exchange:"lighter"` for the
  venue's OWN rate — the endpoint also carries reference rates it mirrors from
  binance/bybit/hyperliquid for the same symbol; using those instead of the
  `"lighter"` row would silently report someone else's funding). Hourly settlement.
- **Paradex** — `api.prod.paradex.trade/v1/markets/summary?market={T}-USD-PERP`.
  `funding_rate` is the quoted **8h** amount despite Paradex's Funding V2 continuous
  recalculation (docs.paradex.trade/risk/funding-mechanism) — `interval_min=480`.
  `open_interest`/`volume_24h` need converting: OI is base-asset units (× `mark_price`
  for USD), `volume_24h` is already USD notional.
- **Extended** — `api.starknet.extended.exchange/api/v1/info/markets/{T}-USD/stats`.
  Hourly settlement (`interval_min=60`); `openInterest`/`dailyVolume` already USD — no
  conversion. An unlisted market returns `200 OK` with every field `"0"` (§3 not_listed
  case, detected via `markPrice == 0`).
- **Vest** — `serverprod.vest.exchange/v2/ticker/latest` per `docs.vest.exchange/
  vest-api`; returned a Cloudflare `530`/"origin unreachable" at build/verify time —
  implemented per the documented shape anyway. A dead origin exercises the same
  `status:"error"` path as any other outage; it never fabricates a datum. **Re-verify
  live before trusting a Vest read** — the exact response field names are unconfirmed
  since the endpoint could not be reached to check them.

## Not in scope (out of this spec)
No thesis/verdict fields — this is a read capability. `brief`/`analyse` surfacing
`top_oi_venue` when it diverges from the briefed venue is a follow-on spec once the
sweep is proven live. EdgeX/Drift (spec's nice-to-have tier) are not implemented.

## Live-verified endpoints (2026-08-31, SPEC-176)
- **Gate** — `api.gateio.ws/api/v4/futures/usdt/contracts/{T}_USDT` (funding_rate,
  funding_interval in seconds, mark_price, funding_rate_limit as the cap) +
  `.../contract_stats?contract={T}_USDT&interval=5m&limit=1` (`open_interest_usd`,
  already USD). Not-listed: `{"label":"CONTRACT_NOT_FOUND"}` on either call. No 24h-USD
  volume field found on either endpoint — Gate never reports `vol24h_usd`.
- **MEXC** — `contract.mexc.com/api/v1/contract/ticker?symbol={T}_USDT` (`holdVol`
  contract-count, `fairPrice`, `amount24` already-USD 24h volume) +
  `.../funding_rate/{T}_USDT` (`fundingRate`, `collectCycle` hours, `maxFundingRate`
  cap) + `.../detail?symbol={T}_USDT` (`contractSize` — varies wildly per symbol, BTC
  `0.0001` vs GALA `10`; the R1 S3 unit-convention trap). `oi_usd = holdVol ×
  contractSize × fairPrice`; a failed/missing detail call degrades `oi_usd` to absent,
  never a guessed value. Not-listed: `{"success":false,"code":1001}`.
- **KuCoin** — one call, `api-futures.kucoin.com/api/v1/contracts/{T}USDTM`, carries
  everything: `fundingFeeRate`, `fundingRateGranularity` (ms), `openInterest`
  (contract-count), `multiplier`, `markPrice`, `turnoverOf24h` (already USD),
  `fundingRateCap`. `oi_usd = openInterest × |multiplier| × markPrice`. Not-listed:
  `{"code":"404000"}`.
- **OKX** — `www.okx.com/api/v5/public/funding-rate?instId={T}-USDT-SWAP`
  (`fundingRate`, interval derived from `fundingTime − prevFundingTime` — never
  assumed 8h, `maxFundingRate` cap) + `api/v5/market/ticker?instId=` (`last` price,
  `volCcy24h` base-asset units — needs × `last` for USD, no direct USD-volume field) +
  `api/v5/public/open-interest?instId=` (`oiUsd` already USD). Not-listed: `code
  "51001"`.
- **HTX** — `api.hbdm.com/linear-swap-api/v1/swap_funding_rate?contract_code={T}-USDT`
  (`funding_rate` only, no interval field) + `swap_contract_info?contract_code=`
  (`settlement_period` hours, resolves the interval) + `swap_open_interest?
  contract_code=` (`value` = OI USD, `trade_turnover` = 24h volume USD, both in one
  call). No published funding cap found — `is_floor` stays `False` rather than
  guessed. Not-listed: `status:"error"`, `err_code` `1332` (funding) or `1014` (OI).
- **BingX** (verify-first, R1 carried zero probes for it) — live-verified keyless
  2026-08-31: `open-api.bingx.com/openApi/swap/v2/quote/premiumIndex?symbol={T}-USDT`
  (`lastFundingRate`, `fundingIntervalHours`, `maxFundingRate`) + `.../openInterest`
  (`openInterest` already USD) + `.../ticker` (`quoteVolume` already USD). Not-listed:
  `code 109425`. Verification succeeded, so it ships as a real venue, not omitted.
