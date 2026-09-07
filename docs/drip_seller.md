# drip_seller — drip/DCA-pattern seller detector (SPEC-117)

## Purpose
SPEC-115 flags LARGE single DEX swaps — a per-swap USD floor that programmatic drip
distribution evades by construction. The desk has already eaten this once: the ESPORTS
drip-bot sold ~$530K via micro-swaps individually below any reasonable per-swap floor
(SPEC-53 fixed the *valuation* — stable-leg first — but nothing *detected* the pattern).
This fingerprints recurring, metronomic same-direction executions per wallet — DEX
swaps and/or CEX-channel deposits — over a trailing window: regular cadence + similar
sizes + a repeating channel + a cumulative USD floor. Workshop Lesson 7 example 3
(VINE DCA) is the same shape from the front.

## Contract
```
drip_seller LAB --json
```
Out: `{available, ticker, n_hits, hits:[{verdict: DRIP_SELLER|DRIP_BUYER, wallet,
direction, channel, channel_kind: dex-swap|cex-deposit, cumulative_usd, tx_count,
median_gap_sec, first_ts, last_ts, run_rate_usd_per_day, attribution:{kind:
tracked|fresh_child, cluster, parent}}], lines:[...]}`. `available:false` (with
`reason`) on provider-down / untracked token / no known dex-or-CEX addresses — never
a clean "no drip" bill (§3).

## Config
`config/drip_seller.json`: `window_h` (trailing fingerprint window, default 72h),
`min_n` (minimum same-direction executions, default 6), `cv_gap_max`/`cv_size_max`
(cadence/size regularity ceilings — coefficient of variation, defaults 0.35/0.5),
`gap_quantized_tol`/`gap_quantized_frac` (alt cadence test for fixed-interval bots
whose CV still trips on a few noisy gaps), `cumulative_floor_usd` (default $50K),
`days` (transfer-log lookback), `young_nonce_max` (SPEC-98's fresh-wallet fingerprint,
reused verbatim from `config/rotation_freshness.json`'s `onchain_young_nonce_max`),
`max_channels`/`max_wallets` (bounded provider reads).

## Mechanism
- `channel_executions_dex` — reuses `dex_execution.size_swap_hits` with the per-swap
  floor disabled (`min_usd: 0`) — the whole point; every swap still needs a same-tx
  quote-leg match (SPEC-53), never guesses a size off the token leg alone.
- `channel_executions_cex` — a straight token transfer to a known CEX deposit address;
  there's no second leg to correlate, so it's sized off spot `price` when given.
  Unpriced deposits are dropped before the cumulative test (never guessed).
- `fingerprint_drip` — PURE core: groups by (wallet, direction, channel) already done
  by the caller; requires `>= min_n` executions within the trailing window with
  regular cadence (`cv_gap` OR quantized gaps) AND similar sizes (`cv_size`) AND
  cumulative USD `>= cumulative_floor_usd`.
- `build_drip` — orchestrator: tracked wallets + SPEC-98 fresh children (via
  `dex_execution.discover_fresh_children`, reused not re-derived) + optional ad-hoc
  `extra_wallets`, scanned against known DEX addresses (`dex_execution.known_dex_addresses`)
  and/or known CEX channels (`rotation_freshness._known_cex_channels`).

## Wiring
`rotation_freshness.classify_freshness`/`build_freshness` take a fourth evidence leg
(`drip`, via `_default_drip_leg`) alongside the existing tape/onchain/dex legs — a
qualifying DRIP_SELLER hit by a tracked wallet or fresh child while the tracked top
holder is frozen flips FROZEN → ROTATED, cited right after the `dex` leg (same
seniority tier — both are confirmed on-chain execution, not inference). This rides
through the existing SPEC-98 surfacing in `onchain.build_onchain`
(`distribution_freshness`) and `brief`'s headline synthesis with no further wiring —
the `EXECUTION (drip): ...` line is embedded in `distribution_freshness.line` when the
leg fires.

## Gotchas
- `fingerprint_drip` drops executions with `usd=None` (an unpriced CEX deposit) BEFORE
  counting `min_n`/cumulative — a wallet's cadence can look regular while its sizing
  is unverifiable; it simply won't hit until priced.
- The window filter is relative to the LAST execution's timestamp, not "now" — a drip
  sequence that ended outside `window_h` still fingerprints correctly off its own span.
- `DRIP_BUYER` is the accumulation mirror (cheap, same math) — same function, `direction
  == BUY`.
