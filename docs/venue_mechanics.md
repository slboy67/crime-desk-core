# venue_mechanics — USDT-M perp public API contracts (Gate / MEXC / KuCoin / HTX / OKX / BingX)

Reference sheet for the six venues **not** currently wired into the engine. Today the perp layer
reads Binance, Bybit, Bitget, Hyperliquid, Aster and (for liquidations only) OKX
(`capabilities/liqs.py`, `capabilities/regime_flip.py`). This document is the contract-first
groundwork for widening the §0.6 cross-venue OI/funding construction read.

**Every fact below is either (a) a live keyless response captured on this host, or (b) a quote from
the venue's own docs with the URL cited.** Anything neither is marked **UNVERIFIED** — a
data-source claim is a single datum until a real call confirms it (CLAUDE.md §3).

## Observation metadata

| | |
|---|---|
| Capture window | 2026-08-19, ~17:32–17:41 UTC |
| Egress IP / geo | `86.84.19.106` — KPN B.V., North Holland, **NL** (`https://ipinfo.io/json`) |
| Auth used | **None.** Every request below is keyless, unsigned, no headers beyond `User-Agent`. |
| Geo result | **All six venues answered HTTP 200 from a Netherlands IP.** No 403/451, no geo-wall, no key demanded on any market-data path. |

> Geo caveat: this proves reachability **from NL only**. US-egress behaviour for OKX / BingX / MEXC
> / HTX is **UNVERIFIED** — not tested, and known restriction notices are secondary sources. If the
> desk ever runs from a US or UK IP, re-run the reachability probe before trusting these paths.

---

## 1. Summary comparison

| Venue | Funding interval | Varies per contract? | Interval exposed as a field? | Floor / placeholder print | Auth | Symbol format |
|---|---|---|---|---|---|---|
| **Gate** | 8h / 4h / 1h | **YES** — 592×8h, 345×4h, 2×1h (n=939) | ✅ `funding_interval` (**seconds**) | `0.0001` @8h, `0.00005` @4h. **Never prints 0** (0 of 939) | keyless | `BTC_USDT` |
| **MEXC** | 8h / 4h | **YES** — mixed in a 32-contract sample (18×4h, 14×8h) | ✅ `collectCycle` (**hours**), on the funding endpoint only — **not** on `contract/detail` | `0.00005` @4h, `0.0001` @8h, **and a hard `0`** on 226/1131 incl. real crypto perps | keyless | `BTC_USDT` |
| **KuCoin** | 8h / 4h / 1h | **YES** — 415×4h, 247×8h, 2×1h (n=664) | ✅ `fundingRateGranularity` + `currentFundingRateGranularity` (**ms**) | `0.00005` @4h, `0.0001` @8h | keyless | `XBTUSDTM` (⚠ BTC→**XBT**) |
| **HTX** | 8h / 4h / 1h | **YES** — 217×8h, 68×4h, 5×1h (n=290) | ✅ `settlement_period` (**hours, as a string**) | `0.0001` @8h, `0.00005` @4h, **and `"0E-18"`** on 153/294 (almost all tradfi contracts) | keyless | `BTC-USDT` |
| **OKX** | 8h / 4h (2h/1h possible) | **YES** — 236×8h, 200×4h (n=436) | ❌ **NO FIELD** — must derive `nextFundingTime − fundingTime` | `0.0001` @8h, `0.00005` @4h, **and exact `0`** on 131 (all `interestRate=0` tradfi) | keyless | `BTC-USDT-SWAP` |
| **BingX** | 8h / 4h / 1h | **YES** — 598×8h, 465×4h, 1×1h (n=1064) | ✅ `fundingIntervalHours` (**hours**) | `0.0001` @8h, `0.00005` @4h; plus an unexplained `±0.00004` cluster @8h | keyless | `BTC-USDT` |

**The single most important line in this table:** *every one of the six varies the funding interval
per contract*, and five of six expose it as a field. Assuming 8h uniformly produces a **2× error on
the 4h majority** at MEXC / KuCoin / BingX and an **8× error** on the 1h contracts. The
`regime_flip.to_4h(funding_pi, interval_min)` normalizer already exists — the venue adapters must
feed it the **per-contract** interval read from the field, never a venue-level constant.

---

## 2. Cross-cutting: the floor / placeholder problem

The desk's existing sentinel is `capabilities/regime_flip.py`:

```
FUNDING_FLOOR_RAW  = 0.00005   # ±0.005% — Binance/Bybit base-rate floor
FUNDING_FLOOR_BAND = 5e-6
```

That check is **narrow for these six venues in two ways**, both verified empirically:

1. **It only catches the 4h floor.** Every one of the six clamps to a base rate that scales with
   the interval: `0.00005` on 4h contracts and **`0.0001` on 8h contracts**. Gate has 476 contracts
   printing exactly `0.0001` and OKX has 44; both would sail past a ±0.00005 band as a "real"
   +0.01%/8h ≈ +0.005%/4h read. (Coincidentally the *normalized* %/4h value is identical, so the
   verdict may still land right — but the raw-value floor test does not fire, so the print is never
   flagged SUSPECT.)
2. **It does not catch a genuine `0`.** OKX prints exact `0.0000000000000000`, HTX prints the
   string `"0E-18"`, MEXC prints numeric `0`. A zero is a data FAILURE by CLAUDE.md §3 — and on
   MEXC it happens to *real crypto perps*, not just tokenized stocks (see §4).

**Recommended detection rule (spec-worthy):**

```
base = 0.0001 * (interval_hours / 8.0)        # 0.0001 @8h, 0.00005 @4h, 0.0000125 @1h
is_floor = (rate == 0) or (abs(abs(rate) - base) <= max(5e-6, base * 0.10))
```

Empirical support, per venue, cross-tabbed interval × rate over the full live universe:

| Venue | 8h modal rate (n) | 4h modal rate (n) | zero prints (n) |
|---|---|---|---|
| Gate | `0.0001` (476 / 592) | `0.00005` (252 / 345) | 0 / 939 |
| MEXC | `0.0001` (110 / 1131 all-interval) | `0.00005` (321 / 1131 all-interval) | **226 / 1131** |
| KuCoin | `0.0001` (213 / 247) | `0.00005` (316 / 415) | 0 observed |
| HTX | `0.0001` (72) | `0.00005` (46) | **153 / 294** |
| OKX | `0.0001` (44 / 236) | `0.00005` (137 / 200) | **131 / 436** |
| BingX | `0.0001` (251 / 598) | `0.00005` (282 / 465) | 0 observed |

Roughly **half the universe on every venue is a floor print at any given settlement.** That is not
"flat funding" — it is "this contract has no premium information right now."

---

## 3. Gate (gate.io)

Base URL `https://api.gateio.ws/api/v4` — confirmed by Gate's own generated client docs, which
state the default host is `https://api.gateio.ws/api/v4`
(<https://raw.githubusercontent.com/gateio/gateapi-python/master/docs/FuturesApi.md>).

### 3.1 Endpoints

| Purpose | Path |
|---|---|
| Instrument universe **+ current funding + OI, all in one** | `GET /futures/usdt/contracts` |
| Single contract | `GET /futures/usdt/contracts/{contract}` |
| 24h ticker (all or one) | `GET /futures/usdt/tickers[?contract=BTC_USDT]` |
| Settled funding history | `GET /futures/usdt/funding_rate?contract=BTC_USDT&limit=&from=&to=` |
| OI + L/S + liquidation time series | `GET /futures/usdt/contract_stats?contract=BTC_USDT&limit=` |

Gate's contracts endpoint is the densest universe fetch of the six: one call returns name,
`funding_rate`, `funding_rate_indicative`, `funding_interval`, `funding_next_apply`, `mark_price`,
`position_size` (OI in contracts) and `quanto_multiplier` for all 939 contracts.

Authorization on all four: `**Authorization** / No authorization required`
(<https://raw.githubusercontent.com/gateio/gateapi-python/master/docs/FuturesApi.md>).

### 3.2 Verified response — `/futures/usdt/contracts` (trimmed)

```json
{"name":"0G_USDT","funding_rate":"0.0001","funding_rate_indicative":"0.0001",
 "funding_interval":28800,"funding_next_apply":1787184000,"interest_rate":"0.0003",
 "funding_rate_limit":"0.02","funding_cap_ratio":"1","quanto_multiplier":"1",
 "mark_price":"0.1493","position_size":1778408,"status":"trading","type":"direct"}
```

`/futures/usdt/tickers?contract=BTC_USDT`:

```json
[{"contract":"BTC_USDT","last":"67806.7","low_24h":"64137","high_24h":"70066.7",
  "volume_24h":"610166601","volume_24h_base":"61016","volume_24h_quote":"4053655606",
  "funding_rate":"0.000034","funding_rate_indicative":"0.000034",
  "mark_price":"67817.59","total_size":"716037432","quanto_multiplier":"0.0001"}]
```

`/futures/usdt/funding_rate?contract=BTC_USDT&limit=3` → `[{"r":"-0.00003","t":1787155203}, …]`
(two-letter keys; `t` is **seconds**).

### 3.3 Funding interval

`funding_interval` — *"Funding application interval, unit in seconds"*
(<https://raw.githubusercontent.com/gateio/gateapi-python/master/docs/Contract.md>).

Live distribution over all 939 contracts: `{28800: 592, 14400: 345, 3600: 2}`. The 1h pair as of
capture: `CXMT_USDT`, `MRNA_USDT`.

### 3.4 Floor

Cross-tab `(interval_h, interest_rate, funding_rate)` over all 939:

```
(8, '0',      '0.0001')  344      (4, '0.0003', '0.00005') 254
(8, '0.0003', '0.0001')  135      (4, '0',      '0.00005')   4
```

So the base clamp is `0.0001` per 8h / `0.00005` per 4h and it fires **regardless of the
`interest_rate` field** — `SNXX_USDT` carries `interest_rate: "0"` yet prints `funding_rate:
"0.0001"`. Do **not** derive Gate's floor from `interest_rate`; use the interval-scaled constant.

Gate never printed an exact `0` (0 of 939) — a zero from Gate would itself be the anomaly.

`funding_rate_limit` is the ± cap, and it varies a lot: `0.02` (502), `0.0002` (194), `0.01` (183),
down to `0.000001` (8). `funding_cap_ratio` is documented as *"The factor for the maximum of the
funding rate. Maximum of funding rate = (1/market maximum leverage - maintenance margin rate) *
funding_cap_ratio"* (same Contract.md).

### 3.5 Auth + limits

Keyless. Live response headers (primary evidence):

```
x-gate-ratelimit-limit: 200
x-gate-ratelimit-requests-remain: 199
x-gate-ratelimit-reset-timestamp: 1787160954
```

The counter decremented 199 → 198 → 197 across three calls ~2s apart while the reset timestamp
advanced each time — i.e. a **sliding** window of 200 requests. The exact window length is
**UNVERIFIED** (Gate's rate-limit doc page returns HTTP 403 to non-browser fetches). Read the
headers at runtime rather than hardcoding a budget.

### 3.6 Gotchas

- **⚠ The universe fetch must omit `limit`.** `GET /futures/usdt/contracts` with **no** `limit`
  returns all **939** rows. Passing `limit` caps it: `limit=1000` →
  `{"label":"INVALID_PARAM_VALUE","message":"invalid limit, must in (0, 100]"}`. The generated docs
  say `limit … [default to 100]`, which contradicts observed behaviour. Verified:
  `?limit=100` → 100 rows, `?limit=100&offset=900` → 39 rows. **Do not paginate; just drop the param.**
- Funding history depth is short: `limit=1000` on `BTC_USDT` returned only **90** records
  (`limit=1001` → `INVALID_PARAM_VALUE`). ~30 days at 8h. Deeper history needs `from`/`to` walking,
  or is simply unavailable — **UNVERIFIED** which.
- OI is in **contracts**. `total_size × quanto_multiplier` = base units. Verified: `716037432 ×
  0.0001 = 71603 BTC`, matching `contract_stats.open_interest_usd = 4869190792` at mark 67823.
- `volume_24h` is contracts; use `volume_24h_quote` (or `volume_24h_settle`) for the USDT figure
  that the §7 liquidity gate wants.
- `contract_stats` is a genuine bonus: `lsr_taker`, `lsr_account`, `top_lsr_*`, `long_liq_usd`,
  `short_liq_usd`, `open_interest_usd` on a 5-minute grid. That is most of `oi_sides` in one call.

---

## 4. MEXC

Base URL `https://contract.mexc.com` (<https://mexcdevelop.github.io/apidocs/contract_v1_en/>).

### 4.1 Endpoints

| Purpose | Path |
|---|---|
| Instrument universe | `GET /api/v1/contract/detail[?symbol=BTC_USDT]` |
| Ticker — 24h vol + OI + funding, all symbols | `GET /api/v1/contract/ticker[?symbol=BTC_USDT]` |
| Current funding + interval — **all symbols** | `GET /api/v1/contract/funding_rate` |
| Current funding, one symbol | `GET /api/v1/contract/funding_rate/{symbol}` |
| Settled funding history | `GET /api/v1/contract/funding_rate/history?symbol=&page_num=1&page_size=` |
| Open interest | no dedicated path — `holdVol` on the ticker |

### 4.2 Verified responses

```json
GET /api/v1/contract/funding_rate/BTC_USDT
{"success":true,"code":0,"data":{"symbol":"BTC_USDT","fundingRate":0.0001,
 "maxFundingRate":0.0018,"minFundingRate":-0.0018,"collectCycle":8,
 "nextSettleTime":1787184000000,"idxPrice":67853,"fairPrice":67833.1}}
```

```json
GET /api/v1/contract/ticker?symbol=BTC_USDT
{"data":{"symbol":"BTC_USDT","lastPrice":67833.1,"volume24":1243512300,
 "amount24":8169664178.79,"holdVol":584116901,"lower24Price":64131.3,
 "high24Price":69957.7,"indexPrice":67853.2,"fairPrice":67835.2,"fundingRate":0.0001}}
```

```json
GET /api/v1/contract/funding_rate/history?symbol=BTC_USDT&page_num=1&page_size=3
{"data":{"totalCount":1618,"totalPage":540,"resultList":[
  {"symbol":"BTC_USDT","fundingRate":0.0001,"settleTime":1787155200000,"collectCycle":8},
  {"symbol":"BTC_USDT","fundingRate":0.000038,"settleTime":1787126400000,"collectCycle":8}]}}
```

`contract/detail` (no symbol) → **1123** contracts; `settleCoin` split `{USDT: 1000, USDC: 78,
USD1: 35, + 10 coin-margined}`. Filter on `settleCoin == "USDT"` **and** `state == 0`.

### 4.3 Funding interval — **the field is on the wrong endpoint**

`collectCycle` is *"charge cycle"*, in **hours**
(<https://mexcdevelop.github.io/apidocs/contract_v1_en/>). It is present on
`contract/funding_rate`, `contract/funding_rate/{symbol}` and every row of
`funding_rate/history` — but **not** on `contract/detail`, whose 78 fields contain nothing
funding-related (verified by dumping the full key list).

It varies. A 32-contract random sample of live USDT perps: `{4: 18, 8: 14}` — e.g. `RIVER_USDT`,
`WLFI_USDT`, `ACT_USDT`, `KITE_USDT` = 4h; `BTC_USDT`, `ETH_USDT`, `FET_USDT`, `QNT_USDT` = 8h.

**Practical consequence:** the universe fetch (`contract/detail`) cannot tell you the interval, and
the ticker (`contract/ticker`, which carries a `fundingRate`) cannot either. **Use
`GET /api/v1/contract/funding_rate` with no symbol** — verified to return every contract with
`collectCycle`, `maxFundingRate`, `minFundingRate` and `nextSettleTime` in a single call. Join it
to the ticker on `symbol`.

### 4.4 Floor — **and MEXC's hard zero, which is the decisive finding**

`contract/ticker` (all symbols, n=1131) modal `fundingRate`:

```
5e-05  → 321      0 → 229      0.0001 → 110      0.0002 → 50      -0.0002 → 23
```

`5e-05` and `0.0001` are the interval-scaled base rate as everywhere else. **The `0` is not.**

226 of 1131 contracts print numeric zero. Most are tokenized equity/commodity products
(`MUSTOCK_USDT`, `NVIDIA_USDT`, `XAU_USDT`, `SOXL_USDT`, `COINBASE_USDT` …), where 0% funding is
plausibly intentional. **But real crypto perps are in that set too**, and their history shows the
zero is persistent, not a one-off:

```
GET /api/v1/contract/funding_rate/VINE_USDT
{"symbol":"VINE_USDT","fundingRate":0,"collectCycle":4,"maxFundingRate":0.03,"minFundingRate":-0.03}

history (page_size=4, totalCount=3235):
 {"fundingRate":0,"settleTime":1787155200000,"collectCycle":4}
 {"fundingRate":0,"settleTime":1787140800000,"collectCycle":4}
 {"fundingRate":0,"settleTime":1787126400000,"collectCycle":4}
 {"fundingRate":0,"settleTime":1787112000000,"collectCycle":4}
```

`SNXX_USDT` likewise: `fundingRate: 0` live, and `0 / 0.000225 / 0 / 0` across its last four
settlements. Cross-venue on the same name at the same moment, **Gate prints `SNXX_USDT`
`funding_rate: "0.0001"`, `funding_interval: 28800`.**

**Rule: a MEXC funding print of exactly `0` is a DATA FAILURE (§3) — reject it, do not average it
into a cross-venue rate, and never let it be the venue that makes a name look "flat."** MEXC is
usable for OI and 24h volume; it is the least trustworthy funding source of the six.

### 4.5 Auth + limits

Keyless — *"Signature is not required for public endpoint."* Documented limits:
`contract/detail` *"Rate limit: 1 times / 5 seconds"*; ticker and most market data
*"Rate limit: 20 times /2 seconds"*
(<https://mexcdevelop.github.io/apidocs/contract_v1_en/>). Whether the bucket is IP- or
account-scoped is not stated → **UNVERIFIED**. No rate-limit response headers are returned (only
`x-cache`), so there is no runtime budget signal — back off on a schedule.

The 1-per-5-seconds cap on `contract/detail` is severe for a universe fetch. Cache it.

### 4.6 Gotchas

- `contract/detail` has **`apiAllowed`** per contract; respect it.
- `holdVol` is OI in **contracts**; multiply by `contractSize` (`BTC_USDT` = `0.0001`).
- `volume24` is contracts, `amount24` is the USDT turnover — `amount24` is the §7 liquidity number.
- The `funding_rate/history` response is paginated with `page_num`/`page_size` (not
  from/to timestamps) and reports `totalCount`/`totalPage`.
- Symbol `BTC_USDT` (underscore), same shape as Gate — but the two universes are **not** the same
  set, and MEXC additionally lists USDC- and USD1-settled contracts under near-identical names.

---

## 5. KuCoin Futures

Base URL `https://api-futures.kucoin.com`
(<https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-symbol>).

### 5.1 Endpoints

| Purpose | Path |
|---|---|
| Instrument universe — **+ funding, OI, 24h vol, interval, all in one** | `GET /api/v1/contracts/active` |
| Single contract | `GET /api/v1/contracts/{symbol}` |
| Current funding | `GET /api/v1/funding-rate/{symbol}/current` |
| Settled funding history | `GET /api/v1/contract/funding-rates?symbol=&from=&to=` |
| Best bid/ask ticker | `GET /api/v1/ticker?symbol=` / `GET /api/v1/allTickers` |

`contracts/active` is the single best call on this venue — 674 rows, each carrying
`fundingFeeRate`, `fundingRateGranularity`, `openInterest`, `turnoverOf24h`, `volumeOf24h`,
`markPrice`, `multiplier`, `fundingRateCap/Floor`, `nextFundingRateDateTime`.

### 5.2 Verified responses (trimmed)

```json
GET /api/v1/contracts/active  → code 200000, 674 rows; XBTUSDTM:
{"symbol":"XBTUSDTM","baseCurrency":"XBT","quoteCurrency":"USDT","settleCurrency":"USDT",
 "type":"FFWCSX","isInverse":false,"multiplier":0.001,
 "fundingFeeRate":-7.1e-05,"predictedFundingFeeRate":null,"dailyInterestRate":0.0003,
 "fundingRateGranularity":28800000,"currentFundingRateGranularity":28800000,
 "fundingRateCap":0.003,"fundingRateFloor":-0.003,
 "openInterest":"28127482","turnoverOf24h":719993040.669,"volumeOf24h":10741.132,
 "markPrice":67810.0,"indexPrice":67852.7,"nextFundingRateDateTime":1787184000000,
 "maxLeverage":125,"status":"Open"}
```

```json
GET /api/v1/funding-rate/XBTUSDTM/current
{"code":"200000","data":{"symbol":".XBTUSDTMFPI8H","granularity":28800000,
 "timePoint":1787155200000,"value":-7.1E-5,"dailyInterestRate":3.0E-4,
 "fundingRateCap":0.003,"fundingRateFloor":-0.003,"period":1,
 "fundingTime":1787184000000,"lastTimeFundingRate":-3.5E-5}}
```

```json
GET /api/v1/contract/funding-rates?symbol=XBTUSDTM&from=…&to=…
{"code":"200000","data":[{"symbol":"XBTUSDTM","fundingRate":-3.5E-5,"timepoint":1787155200000},
 {"symbol":"XBTUSDTM","fundingRate":6.5E-5,"timepoint":1787126400000}, …]}
```

### 5.3 Funding interval

`fundingRateGranularity` in **milliseconds**. Over the 664 USDT-margined contracts:

```
fundingRateGranularity:        {14400000: 415, 28800000: 247, 3600000: 2}
currentFundingRateGranularity: {14400000: 415, 28800000: 235, None: 12, 3600000: 2}
```

**4h is the majority here (63%).** Two distinct fields exist — `fundingRateGranularity` (the
contract's configured period) and `currentFundingRateGranularity` (the period in force now, which
is `null` for 12 contracts). Prefer `currentFundingRateGranularity`, fall back to
`fundingRateGranularity`. The same value also appears as `granularity` on the
`funding-rate/{symbol}/current` response, and is encoded in the funding-index symbol itself
(`.XBTUSDTMFPI8H` — the `8H` suffix).

### 5.4 Floor

`fundingFeeRate` modal values over the 664: `5e-05` (316) and `0.0001` (213) — i.e. **529 of 664
(80%) are at the interval-scaled base rate** at this instant. `dailyInterestRate` is `0.0003` for
661 of 664 (= 0.03%/day = 0.0001 per 8h = 0.00005 per 4h, which is exactly the observed floor —
KuCoin's floor *is* derivable from `dailyInterestRate × interval/24h`, unlike Gate's).

`fundingRateCap` / `fundingRateFloor` are the ± clamp and vary: `0.02` (608), `0.00525` (16),
`0.005` (16), `0.0375` (7). Note the contract-object cap (`0.003` on XBTUSDTM in the funding
endpoint) and the `contracts/active` cap disagree in the captured data — the `funding-rate/current`
response reports `0.003` while the aggregate tabulation over `contracts/active` shows `0.02` as
modal. Which is authoritative is **UNVERIFIED**.

No exact-zero prints observed.

### 5.5 Auth + limits

Keyless. Live headers on `/api/v1/contracts/active`:

```
gw-ratelimit-limit: 2000
gw-ratelimit-remaining: 1997
gw-ratelimit-reset: 22273
```

→ a **2000-unit pool with a ~30s reset window** (reset is remaining milliseconds). KuCoin's docs
describe a weighted model — *"Weight per request: 3"* for Get Symbol, and public endpoints are
classified as a `Public` resource pool
(<https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-symbol>); the exact "2000/30s"
label in the docs was not quotable from the fetched page, so treat the **headers** as the source of
truth. Weight is charged per request, not per row — the 674-row `contracts/active` costs the same
as a single-symbol call, which makes it the obvious universe fetch.

### 5.6 Gotchas

- **⚠ Symbol format is the odd one out: `XBTUSDTM`.** BTC is **XBT**, and every symbol carries a
  trailing `M`. `1000000MOGUSDTM`, `10000CATUSDTM` show the multiplier-prefix convention too. A
  canonical-ticker map for KuCoin needs a `BTC → XBT` special case plus prefix stripping.
- **`allTickers` has NO volume.** Verified key set: `bestAskPrice, bestAskSize, bestBidPrice,
  bestBidSize, price, sequence, side, size, symbol, tradeId, ts`. The §7 liquidity gate must read
  `turnoverOf24h` (USDT) from `contracts/active`, not from the ticker.
- **There is no separate open-interest endpoint** — `openInterest` (in contracts) lives on
  `contracts/active`. `openInterest × multiplier` = base units: `28127482 × 0.001 = 28127 BTC` ✅.
- `type` is `FFWCSX` for all 664 linear perps; the inverse book is `FFICSX`. Filter on
  `settleCurrency == "USDT" and isInverse == false`.
- `predictedFundingFeeRate` was `null` on XBTUSDTM at capture — do not depend on it.
- Funding history is timestamp-windowed (`from`/`to` in ms), unlike MEXC's page-number model.

---

## 6. HTX (Huobi) — linear swap

Base URL `https://api.hbdm.com`
(<https://huobiapi.github.io/docs/usdt_swap/v1/en/>).

### 6.1 Endpoints

| Purpose | Path |
|---|---|
| Instrument universe | `GET /linear-swap-api/v1/swap_contract_info[?contract_code=]` |
| Current funding, one | `GET /linear-swap-api/v1/swap_funding_rate?contract_code=` |
| Current funding, **all** | `GET /linear-swap-api/v1/swap_batch_funding_rate` |
| Settled funding history | `GET /linear-swap-api/v1/swap_historical_funding_rate?contract_code=&page_size=` |
| Open interest (all or one) | `GET /linear-swap-api/v1/swap_open_interest[?contract_code=]` |
| 24h ticker, one | `GET /linear-swap-ex/market/detail/merged?contract_code=` |
| 24h ticker, **all** | `GET /v2/linear-swap-ex/market/detail/batch_merged` |

Paths confirmed at <https://huobiapi.github.io/docs/usdt_swap/v1/en/>. Note the two different path
prefixes: `linear-swap-api/v1/…` for reference/funding/OI data, `linear-swap-ex/market/…` for the
market/ticker/kline data, and the batch ticker sits under a `/v2/` prefix.

### 6.2 Verified responses (trimmed)

```json
GET /linear-swap-api/v1/swap_contract_info?contract_code=BTC-USDT
{"status":"ok","data":[{"symbol":"BTC","contract_code":"BTC-USDT","contract_size":0.001,
 "price_tick":0.1,"settlement_date":"1787184000000","settlement_period":"8",
 "contract_status":1,"business_type":"swap","pair":"BTC-USDT","contract_type":"swap",
 "trade_partition":"USDT"}]}
```

```json
GET /linear-swap-api/v1/swap_funding_rate?contract_code=BTC-USDT
{"status":"ok","data":{"estimated_rate":null,
 "funding_rate":"-0.000017417222787085","contract_code":"BTC-USDT","symbol":"BTC",
 "fee_asset":"USDT","funding_time":"1787184000000","next_funding_time":null}}
```

```json
GET /linear-swap-api/v1/swap_open_interest?contract_code=BTC-USDT
{"status":"ok","data":[{"volume":31034739,"amount":31034.739,"value":2104319788.3167,
 "contract_code":"BTC-USDT","trade_amount":13471.114,"trade_turnover":895645473.25}]}
```

Bulk calls verified: `swap_batch_funding_rate` → **294** rows; `swap_open_interest` (no param) →
**306** rows; `/v2/…/batch_merged` → **306** ticks; `swap_contract_info` → **290** USDT swaps.

### 6.3 Funding interval

`settlement_period` — a **string of hours**. Distribution over the 290 USDT swaps:

```
{'8': 217, '4': 68, '1': 5}
```

Five contracts are on a 1-hour cycle: normalizing those at 8h would understate the %/4h magnitude
by **8×**. Parse the string to int.

### 6.4 Floor and the `0E-18` problem

`swap_batch_funding_rate` modal `funding_rate` over 294 rows:

```
"0E-18"                  → 153
"0.000100000000000000"   →  72     (the 8h base rate)
"0.000050000000000000"   →  46     (the 4h base rate)
```

**All rates are decimal strings, and zero serializes as the literal `"0E-18"`.** A naive string
comparison against `"0"` will miss it; parse to float.

The 153 zeros break down by `settlement_period` as `{'8': 141, '4': 11, '1': 1}`, and the largest by
OI are `SNDK-USDT`, `XAU-USDT`, `XAG-USDT`, `SPCX-USDT`, `USOIL-USDT`, `MU-USDT` — i.e. **HTX's zeros
are concentrated in tokenized equities and commodities, unlike MEXC's.** History confirms it is
deliberate, not missing data:

```json
GET /linear-swap-api/v1/swap_historical_funding_rate?contract_code=SNDK-USDT&page_size=5
{"data":{"total_size":362,"data":[
 {"avg_premium_index":"-0.000154611481326905","funding_rate":"0.000000000000000000",
  "realized_rate":null,"funding_time":"1787155200000","contract_code":"SNDK-USDT"}, …]}}
```

A nonzero `avg_premium_index` with a zeroed `funding_rate` = HTX explicitly runs 0% funding on
tradfi contracts. **For a crypto ticker, a `0E-18` from HTX is still a data failure — reject it.**

**`estimated_rate` was `null` on 294 of 294 rows.** HTX exposes no usable predicted rate through
this endpoint; `funding_rate` is the current-period rate for the settlement at `funding_time`.
`next_funding_time` was also `null` throughout. Whether these ever populate is **UNVERIFIED**.

### 6.5 Auth + limits

Keyless. Live headers on `swap_funding_rate` (primary evidence, the clearest of the six):

```
ratelimit-limit:     240
ratelimit-remaining: 239
ratelimit-interval:  3000
ratelimit-reset:     1787160958392
```

→ **240 requests per 3000 ms**, with `remaining` and the absolute `reset` epoch-ms exposed on every
response. Read them; do not hardcode. The docs reference an "API Rate Limit Illustration" section
whose numeric contents were not retrievable → the header values stand as the verified source. IP vs
account scoping is **UNVERIFIED**.

### 6.6 Gotchas

- **⚠ `detail/merged` mixes two different windows.** Verified against the 1-day kline:

  ```
  merged:            open 68518.4  high 68958.8  low 67792.5  close 67993  vol 13507800
  kline 1day (today) open 68518.4  high 68958.8  low 67792.5  close 67993  amount 1888.576
  kline 1day (prev)  open 64807.6  high 69761.8  low 64148.8  close 68522.9 amount 12020.392
  ```

  The **OHLC is the current UTC-day candle**, while `vol`/`amount`/`trade_turnover`/`count` are a
  **rolling 24h** (13507.8 ≈ 1888.6 + 12020.4). Using `merged.high`/`merged.low` as a "24h range"
  is wrong — at capture it reported a 68958/67792 range while the true 24h range was 70099/64141.
  For a real 24h high/low, use two 1-day klines or a 24×1h kline pull.
- OI: `volume` = contracts, `amount` = base units, `value` = USD. Verified `31034739 × 0.001 =
  31034.739` ✅. `trade_turnover` on the OI endpoint is 24h USDT turnover — a second liquidity source.
- `contract_status`: 1 = listed, 3 = suspended (1 of 290 at capture). Filter.
- Everything is a **string**, including numbers that look numeric (`settlement_period: "8"`,
  `funding_time: "1787184000000"`). Coerce explicitly.
- Symbol `BTC-USDT` (hyphen) — collides visually with BingX's identical format but the universes
  differ (HTX 290 vs BingX 1079).
- HTX's USDT swap universe is by far the **smallest of the six (290)**. It will miss most micro-cap
  names the desk cares about — good for majors cross-checks, near-useless for discovery.

---

## 7. OKX

Base URL `https://www.okx.com` (<https://www.okx.com/docs-v5/en/>). Already used by
`capabilities/liqs.py` for `/api/v5/public/liquidation-orders`.

### 7.1 Endpoints

| Purpose | Path |
|---|---|
| Instrument universe | `GET /api/v5/public/instruments?instType=SWAP` |
| Current funding, one **or all** | `GET /api/v5/public/funding-rate?instId=BTC-USDT-SWAP` / `instId=ANY` |
| Settled funding history | `GET /api/v5/public/funding-rate-history?instId=&limit=` |
| Open interest (all or one) | `GET /api/v5/public/open-interest?instType=SWAP[&instId=]` |
| 24h ticker (all or one) | `GET /api/v5/market/tickers?instType=SWAP` / `GET /api/v5/market/ticker?instId=` |

### 7.2 Verified responses (trimmed)

```json
GET /api/v5/public/funding-rate?instId=BTC-USDT-SWAP
{"code":"0","data":[{"instId":"BTC-USDT-SWAP","instType":"SWAP",
 "fundingRate":"0.0001000000000000","fundingTime":"1787184000000",
 "nextFundingRate":"","nextFundingTime":"1787212800000",
 "prevFundingTime":"1787155200000","settFundingRate":"0.0000467644705158",
 "settState":"settled","method":"current_period","formulaType":"withRate",
 "interestRate":"0.0001000000000000","impactValue":"20000",
 "minFundingRate":"-0.00375","maxFundingRate":"0.00375",
 "premium":"-0.0002490087536155","ts":"1787160689190"}],"msg":""}
```

```json
GET /api/v5/market/ticker?instId=BTC-USDT-SWAP
{"data":[{"instId":"BTC-USDT-SWAP","last":"67838.6","open24h":"64777.9",
 "high24h":"70099.2","low24h":"64141.9","volCcy24h":"149634.8738","vol24h":"14963487.38"}]}
```

```json
GET /api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP
{"data":[{"instId":"BTC-USDT-SWAP","oi":"3124363.54","oiCcy":"31243.6354",
 "oiUsd":"2119568225.54","ts":"1787160725306"}]}
```

Universe counts at capture: `instruments?instType=SWAP` → **452** (436 linear USDT-settled);
`market/tickers?instType=SWAP` → 451; `public/open-interest?instType=SWAP` (no instId) → 451.

### 7.3 Funding interval — **no field; you must derive it**

**OKX is the one venue of the six that does not publish the interval.** The `instruments` response
has no funding field at all (verified: `BTC-USDT-SWAP` carries `ctVal`, `ctMult`, `ctValCcy`,
`lotSz`, `state`, `settleCcy` … and nothing about funding). The only handle is:

```
interval_ms = int(nextFundingTime) - int(fundingTime)     # from public/funding-rate
```

Derived distribution over 436 USDT swaps: **`{8h: 236, 4h: 200}`**. Examples — 4h:
`LAYER-USDT-SWAP`, `KAITO-USDT-SWAP`, `PI-USDT-SWAP`; 8h: `BTC-USDT-SWAP`, `ZHIPU-USDT-SWAP`.

**And the interval is dynamic.** Per OKX's own announcement
(<https://www.okx.com/en-us/help/okx-to-enable-automatic-updates-for-funding-fee-settlement-period>,
effective *"Apr 14, 2026 (UTC)"*): *"Automatic frequency adjustment applies only to crypto perpetual
contracts"*, across four levels — **8h, 4h, 2h, 1h**. *"When the funding rate reaches its cap or
floor at settlement, the frequency will be escalated by one level at a time."* It reverts only *"if
… the funding rate at every settlement during the preceding 12 consecutive hours was within +/-0.20%
(20 bps)"*.

**This is the exact situation the desk cares about.** A name that escalates to a 1h cycle is, by
OKX's own rule, one that has been *pinned at the funding cap or floor* — i.e. a §4 deep-neg
squeeze-loading candidate. **A cached OKX interval is a live trap**: the contract most likely to
have changed interval is the contract you are about to trade. Re-derive the interval from the same
`funding-rate` response you take the rate from — never from a separate or stale call.

(No 1h or 2h OKX contracts existed at capture; that state is a snapshot, not a ceiling.)

### 7.4 Floor

Cross-tab `(interval_h, interestRate, fundingRate)` over 436 USDT swaps:

```
(4, 0.0001, 0.00005)  → 137        (8, 0.0,    0.0)     → 131
(8, 0.0001, 0.0001)   →  44
```

Three clean facts:
1. The base clamp is `interestRate` scaled to the interval: `interestRate × interval_h / 8`.
   `interestRate` is `0.0001` for 274 contracts and `0.0` for 162.
2. **When `interestRate == 0`, the floor is a literal `0.0000000000000000`** — 131 contracts. These
   are the tradfi perps (`UVXY-`, `SONY-`, `SMCI-`, `LLY-`, `COST-`, `TSEM-USDT-SWAP` …). The two
   4h contracts with `interestRate == 0` are `CL-USDT-SWAP` and `BZ-USDT-SWAP` (crude oil), and
   they print real nonzero rates.
3. `minFundingRate` / `maxFundingRate` are the per-contract ± clamp (`±0.00375` on BTC, `±0.01`
   on `LAYER`).

**Doc-vs-live conflict, flagged:** OKX's 2024 announcement
(<https://www.okx.com/en-us/help/okx-to-optimize-funding-rate-calculation>, effective 2024-03-05)
states *"Funding rate = Clamp[Average premium index, Funding cap, Funding floor]"* and *"Interest
rate = 0"*. The **live API contradicts that for 274 of 436 contracts** (`interestRate: "0.0001"`,
and `formulaType: "withRate"` on all 436 — the with-interest-rate formula). Take the live
`interestRate` field, not the announcement; the announcement appears superseded.

### 7.5 Auth + limits

Keyless — no `OK-ACCESS-KEY` needed on any `/api/v5/public/*` or `/api/v5/market/*` path (all
verified unsigned). OKX documents that *"Public unauthenticated REST calls are limited based on IP
address"* while private calls are limited by User ID, and that limits are per-endpoint
(<https://www.okx.com/docs-v5/en/>). The one limit quotable from the fetched page is **Get
instruments: "Rate Limit: 20 requests per 2 seconds"**. The per-endpoint limits for
`public/funding-rate`, `public/funding-rate-history`, `public/open-interest` and `market/tickers`
are **UNVERIFIED** — the docs page is a JS-rendered SPA and those sections did not render. OKX
returns **no rate-limit headers** (verified: only `cf-ray`, `x-brokerid`, `x-routed-to: TKY`), so
there is no runtime budget signal. Budget conservatively at 20 req / 2 s per endpoint until proven.

### 7.6 Gotchas

- **⭐ `instId=ANY` returns the entire funding universe in one call.** Verified: **584 rows** (436
  of them `-USDT-SWAP`), each with `fundingRate`, `fundingTime`, `nextFundingTime`, `interestRate`,
  `min/maxFundingRate`, `premium`, `settState`. This is not documented on the page that rendered →
  **doc text UNVERIFIED, behaviour VERIFIED**. It collapses what would be 436 calls into one, and
  it is the only practical way to get the derived interval for the whole book. Treat it as
  load-bearing-but-undocumented: if it ever starts 400ing, fall back to per-instId.
- **No USD 24h volume on the ticker.** `vol24h` is contracts, `volCcy24h` is base currency. The §7
  liquidity gate must compute `volCcy24h × last`. (Contrast: OI *does* come with `oiUsd`.)
- `oi` = contracts, `oiCcy` = base, `oiUsd` = USD — verified `3124363.54 × 0.01 (ctVal) = 31243.6 =
  oiCcy` ✅.
- `instType=SWAP` returns **both** linear and inverse. `BTC-USD-SWAP` is inverse (`ctType:
  "inverse"`, `ctValCcy: "USD"`, `settleCcy: "BTC"`). Filter `ctType == "linear" and settleCcy ==
  "USDT"` — 436 of 452. A `-USDT-SWAP` suffix check happens to work today but the `ctType` filter
  is the correct one.
- `nextFundingRate` was `""` (empty string, not null) on every row — no usable predicted rate.
  `settFundingRate` is the *last settled* rate; `fundingRate` is the current-period rate for the
  settlement at `fundingTime`.
- Symbol `BTC-USDT-SWAP`; `uly` / `instFamily` = `BTC-USDT`. `liqs.py` already builds the `uly`
  form, so the mapping helper exists.

---

## 8. BingX

Base URL `https://open-api.bingx.com` (<https://bingx-api.github.io/docs/>).

### 8.1 Endpoints

| Purpose | Path |
|---|---|
| Instrument universe | `GET /openApi/swap/v2/quote/contracts` |
| Mark price + funding + **interval** (one or all) | `GET /openApi/swap/v2/quote/premiumIndex[?symbol=]` |
| Settled funding history | `GET /openApi/swap/v2/quote/fundingRate?symbol=&limit=` |
| 24h ticker (one or all) | `GET /openApi/swap/v2/quote/ticker[?symbol=]` |
| Open interest | `GET /openApi/swap/v2/quote/openInterest?symbol=` |

**API version:** `v2` is current for swap quote endpoints. `v3` does **not** exist — verified:
`GET /openApi/swap/v3/quote/premiumIndex?symbol=BTC-USDT` →
`{"code":100400,"msg":"this api is not exist,please refer to the API docs https://bingx-api.github.io/docs"}`.

### 8.2 Verified responses (trimmed)

```json
GET /openApi/swap/v2/quote/premiumIndex?symbol=BTC-USDT
{"code":0,"msg":"","data":{"symbol":"BTC-USDT","markPrice":"67823.4","indexPrice":"67845.9",
 "lastFundingRate":"-0.00007700","nextFundingTime":1787184000000,
 "fundingIntervalHours":8,"minFundingRate":"-0.003000","maxFundingRate":"0.003000",
 "updateTime":1787155200000}}
```

```json
GET /openApi/swap/v2/quote/ticker?symbol=BTC-USDT
{"data":{"symbol":"BTC-USDT","priceChangePercent":"-0.00","lastPrice":"67822.8",
 "highPrice":"70385.7","lowPrice":"64136.8","volume":"24302.0900",
 "quoteVolume":"1608166628.85","openPrice":"67822.9","askPrice":"67822.9","bidPrice":"67822.8"}}
```

```json
GET /openApi/swap/v2/quote/fundingRate?symbol=BTC-USDT&limit=3
{"data":[{"symbol":"BTC-USDT","fundingRate":"-0.00011700","fundingTime":1787155200000,
  "markPrice":"68532.3"}, …]}
```

```json
GET /openApi/swap/v2/quote/openInterest?symbol=BTC-USDT
{"data":{"openInterest":"1360193030.3","symbol":"BTC-USDT","time":1787160758781}}
```

Universe counts: `quote/contracts` → **1079** (`currency`: `{USDT: 1030, USDC: 49}`);
`quote/premiumIndex` with **no symbol** → **1064** rows; `quote/ticker` with no symbol → all.

### 8.3 Funding interval

`fundingIntervalHours` — an **integer of hours**, right there on `premiumIndex`. Distribution over
all 1064 rows:

```
{8: 598, 4: 465, 1: 1}
```

4h examples: `AXS-USDT`, `BSV-USDT`, `MASK-USDT`, `ZRX-USDT`. The single 1h contract at capture:
`COTI-USDT` (printing `-0.00027100`/1h = **−1.08%/4h** normalized — a deep-neg §5 short-veto that
an 8h assumption would have read as −0.135%/4h and waved through). That one row is the whole case
for reading the interval field.

### 8.4 Floor

Cross-tab `fundingIntervalHours` × `lastFundingRate` over 1064 rows:

```
8h → "0.00010000" (251)   "0.00004000" (107)   "-0.00004000" (74)   "-0.00010000" (59)
4h → "0.00005000" (282)   "0.00003500" ( 20)   "0.00020000" (  8)
```

`0.0001` @8h / `0.00005` @4h is the familiar base rate — 533 of 1064. **The `±0.00004` and
`0.000035` clusters are unexplained**: they are too repetitive (107 + 74 + 20 = 201 contracts on
three exact values) to be genuine premium, but they do not match the interest-rate formula that
fits the other five venues. Their cause is **UNVERIFIED** — treat any of `±0.00004`, `0.000035`,
`±0.0001`, `±0.00005` from BingX as *suspect-not-datum* until a settled-history study explains them.

`minFundingRate`/`maxFundingRate` are per-contract and vary: `0.02` (622), `0.005` (205+47),
`0.0005` (34), `0.04` (24). Note the same cap appears in two string formats (`"0.005"` and
`"0.005000"`) — parse as float, never compare as string.

No exact-zero prints observed.

### 8.5 Auth + limits

Keyless on all `/openApi/swap/v2/quote/*` paths (verified unsigned). Live headers:

```
x-ratelimit-requests-remain:  499
x-ratelimit-requests-expire: 10000
```

→ **500 requests per 10 000 ms**, exposed per response. The docs site
(<https://bingx-api.github.io/docs/>) is a JS-rendered SPA that serves no retrievable markdown or
HTML partials (every probe of `…/docs/en-us/swapV2/market-api.{md,html}` and the `docs-v3` paths
returned 404), so the **documented** limit text, the IP-vs-key scoping, and the field definitions
for `fundingIntervalHours` / `lastFundingRate` / `openInterest` are all **UNVERIFIED**. The headers
are the operative source.

### 8.6 Gotchas

- **`lastFundingRate` is named "last", and it is ambiguous.** `updateTime` on the BTC row was
  `1787155200000` = the *previous* settlement, and `fundingRate` history for that same timestamp
  reported `-0.00011700` while `lastFundingRate` read `-0.00007700`. So the two do **not** agree —
  `lastFundingRate` is most likely the live/current-period rate despite the name, but this is
  **UNVERIFIED**. If BingX is ever load-bearing on a funding verdict, resolve this first.
- **`openInterest` units are undocumented.** Magnitudes strongly imply **quote currency (USDT)**:
  `BTC-USDT` 1 370 941 109.8 ÷ mark 67 999.6 = 20 161 BTC; `ETH-USDT` 549 137 465.83 ÷ 2 081.82 =
  263 772 ETH; `DOGE-USDT` 10 615 357.85 ÷ 0.07218 = 147 M DOGE. All plausible; the contracts
  reading (1.37 billion BTC) is absurd. Treating it as USDT is an **inference** — flag it as such
  until the doc confirms.
- **`openInterest` accepts one symbol at a time.** There is no bulk OI call. 1030 USDT contracts ×
  1 call each against a 500/10 s budget = ~21 s minimum for a full-universe OI sweep. Budget for it
  or subset by liquidity first.
- `quote/contracts` returns 1079 rows but no funding data; `quote/premiumIndex` returns 1064 rows
  with funding but no `size`/`status`. Join them on `symbol`, and honour `status` (1 = trading) and
  `apiStateOpen`/`apiStateClose` from the contracts side.
- BingX rejects unknown symbols loudly rather than returning empty — verified:
  `?symbol=SNXX-USDT` → `{"code":109425,"msg":"SNXX-USDT not exist, please verify it in api:
  /openApi/swap/v2/quote/contracts"}`. Good for detecting an unmapped ticker, but it means a
  cross-venue sweep must tolerate per-symbol error codes rather than assuming HTTP-level failure.
- Symbol `BTC-USDT` (hyphen) — same shape as HTX.

---

## 9. Symbol mapping quick reference

| Canonical | Gate | MEXC | KuCoin | HTX | OKX | BingX |
|---|---|---|---|---|---|---|
| BTC | `BTC_USDT` | `BTC_USDT` | **`XBTUSDTM`** | `BTC-USDT` | `BTC-USDT-SWAP` | `BTC-USDT` |
| ETH | `ETH_USDT` | `ETH_USDT` | `ETHUSDTM` | `ETH-USDT` | `ETH-USDT-SWAP` | `ETH-USDT` |
| Generic rule | `{T}_USDT` | `{T}_USDT` | `{T}USDTM`, BTC→XBT | `{T}-USDT` | `{T}-USDT-SWAP` | `{T}-USDT` |

Universe sizes at capture (USDT-margined perps only, live/trading): BingX 1030 · MEXC 1000 · Gate
939 · KuCoin 664 · OKX 436 · HTX 290. For micro-cap discovery the useful venues are BingX, MEXC and
Gate; HTX and OKX are majors-and-mid-caps cross-checks.

Multiplier-prefixed listings (`1000000BABYDOGE_USDT`, `10000CATUSDTM`, `1000PEPE…`) differ per
venue and are a real source of cross-venue mismatch — the canonical map needs prefix normalization,
not just a suffix template.

---

## 10. What this implies for the desk (spec candidates, not implemented here)

1. **Per-contract interval is mandatory.** Any venue adapter must carry the interval field
   (`funding_interval` / `collectCycle` / `currentFundingRateGranularity` / `settlement_period` /
   `fundingIntervalHours`) into `regime_flip.to_4h(..., interval_min)`. OKX has no field → derive
   `nextFundingTime − fundingTime` from the *same* response as the rate, never cached.
2. **Widen `_is_floor`.** The current ±0.00005 band misses the 8h floor `0.0001` (Gate 476, OKX 44,
   BingX 251, KuCoin 213, HTX 72) and misses zero entirely (OKX 131, HTX 153, MEXC 226). Use the
   interval-scaled `0.0001 × h/8` rule plus an explicit `rate == 0` branch.
3. **MEXC funding is not trustworthy.** Verified real-crypto perps (`VINE_USDT`, `SNXX_USDT`) print
   a persistent hard `0` while Gate prints a live rate on the same name. Use MEXC for OI/volume,
   exclude it from the funding consensus, or gate it behind a "MEXC-zero = reject" rule.
4. **Cheap bulk calls exist on five of six.** `OKX instId=ANY` (584 rows), `MEXC
   contract/funding_rate` (all), `HTX swap_batch_funding_rate` (294), `Gate contracts` (939, no
   `limit` param), `KuCoin contracts/active` (674). BingX needs `premiumIndex` (bulk, 1064) plus
   per-symbol `openInterest` (no bulk). A whole-universe cross-venue funding sweep is ~6 requests.
5. **Read rate-limit headers, don't hardcode.** Gate, HTX, KuCoin and BingX all expose live budget
   headers. MEXC and OKX expose none — those two need a conservative fixed schedule.
6. **HTX `detail/merged` must not be used for a 24h range.** Its OHLC is the current UTC-day
   candle while its volume is rolling 24h.
