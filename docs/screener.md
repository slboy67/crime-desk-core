# screener — BNB-chain discovery net

## Purpose
DISCOVERY by structural fingerprint over the perp-listed universe: `pump` mode finds
mid-cycle engineered pumps, `accumulation` finds pre-cycle Phase-1 loading. Feeds the
watchlist funnel; not a verdict.

## Contract
```
screener '{}'                                   # pump mode, defaults
screener '{"mode":"accumulation","min_score":4,"pages":3,"top":40}'
```
Out: `{mode, scanned, perp_universe:{binance,bybit}, min_score, baseline_age_h,
candidates:[{ticker, score, why, venues, mc, fdv_mc, circ_ratio, vol, ch7, ch30,
on_watchlist}], status, reason?, screener_degraded?, markets_cache_age_h?}`.

- `screener_degraded` (SPEC-174 #5, widened SPEC-187) — `"coingecko_429"` /
  `"coingecko_error"` (suffixed `"_stale_cache"` once the fallback cache is older than
  `MARKETS_CACHE_TTL_S`, 15min), present only when `markets()` fell back to its on-disk
  cache (or came up short after that) once its per-page 429 exponential-backoff retries
  were exhausted. **SPEC-187: the cache fallback is used at ANY age, not gated to the
  15-min TTL** — a CoinGecko outage/rate-limit that outlasts 15 minutes used to zero the
  whole layer (`screener_degraded: coingecko_429` with `scanned: 0`, the 2026-09-01
  incident: "a rate limit shouldn't zero the layer"); now it degrades to stale data
  instead. `markets_cache_age_h` (present only alongside `screener_degraded`) is the
  fallback cache's age in hours — always shown so a stale read stays visibly stale (§3:
  degraded output must say which layer is stale, never silently thin the row set). This
  can fire even on a `status: "OK"` sweep (the cache backfilled `scanned` back above
  zero) — a degraded-but-usable sweep must never look identical to a clean one. The one
  case that still legitimately zeroes `scanned`: no cache file exists on disk at all (a
  first-ever run, or one that has never yet had a clean fetch) — genuinely no data to
  fall back to.

- `status` — `"OK"` for a real sweep (`scanned > 0` and a usable vol baseline), or
  `"NOTOK"` (SPEC-137) when the result can't be trusted as a clean sweep: `scanned == 0`
  (CoinGecko fetch failed/rate-limited — `reason: "fetch_failed"`) or the volume baseline
  is missing/stale/too-fresh (`reason: "baseline_missing"` /
  `"baseline_stale_<h>h"` / `"baseline_too_fresh_<h>h"`; `baseline_age_h` is `null`
  in all three). `reason` is present only when `status` is `"NOTOK"`. A `"NOTOK"` sweep
  with `candidates: []` is a data failure, NOT a valid "swept the universe, found
  nothing" — that valid-NONE case is `status: "OK"`, `scanned > 0`, `baseline_age_h`
  non-null, `candidates: []`.

## Gotchas
- Perp-listed only — a pre-listing crime coin is invisible here by design (no edge
  without a perp to trade).
- §1: surface LONGS with equal weight — accumulation mode exists exactly so scans don't
  default to "where's the short".
- Scores are fingerprint counts (§2 Cat-A traits), not conviction; the §0.6 read still
  decides.
- Never read `candidates == []` alone as "clean empty sweep" — always check `status`
  first (§3 CLAUDE.md: a zero/null print is a data failure, not a datum).
