# kr_listings — Korean listing/delisting tripwire (SPEC-153)

## Purpose
Automates the UB/CAP listing-pump class the user's manual feed caught 2026-08-06:
Upbit/Bithumb listing announcements precede the listing itself, so the notice — not a
market-code diff — is the earliest tradeable signal. The inverse is free: a delisting
notice (거래지원 종료) is early warning of the B3 single-venue-isolation risk on a
tracked name. Also surfaces Upbit's own caution flags (incl.
`CONCENTRATION_OF_SMALL_ACCOUNTS`) — a crime-desk fingerprint straight from the exchange.

Three keyless sources, each degrading independently (§3 — a dead source is loud, never
a silently smaller sweep):
1. `GET api.upbit.com/v1/market/all?is_details=true` — every KRW market + caution flags.
2. `GET api-manager.upbit.com/api/v1/announcements` — structured notices (browser
   User-Agent required; a Cloudflare 403 degrades this source only, never reads as
   "no notices").
3. `GET feed-api.bithumb.com/v1/notices` — keyless JSON notices.

## Contract
```python
import kr_listings as KR
r = KR.sweep()          # poll all three sources, diff vs state/kr_listings_baseline.json
KR.status()              # current baseline summary, no network
```
`sweep()` returns `{ok, first_run, events, degraded_sources, ts}`. Each event:
`{class, severity, ticker, tracked, perp_listed, page, unmapped, msg, ...}`.

Event classes:
| class | severity | page? |
|---|---|---|
| `KR_LISTING_NOTICE` | HIGH always | tracked-or-perp-listed only |
| `KR_LISTED` (market-code diff) | MED | never |
| `KR_DELISTING_NOTICE` | HIGH if tracked, else MED | tracked-or-perp-listed only |
| `KR_FLAG_CHANGE` (caution/warning flip) | MED | never |

First run seeds the baseline and emits zero events (nothing to diff against yet — the
`deposit_breadth` cold-start rule). `unmapped: true` marks an event whose symbol
couldn't be extracted from the notice title (no parenthesized ticker) — surfaced, never
dropped (req 6).

Ticker mapping is by symbol only (`KRW-XPIN` → `XPIN`); `tracked` checks
`config/watchlist.json`, `perp_listed` cross-references `screener.binance_perps()`
(both injectable via `tracked_tickers_fn` / `perp_listed_fn`, and both degrade to
`None`/unknown on failure — never `False`, per §3).

## Cadence
`ops/discovery_tick.sh` — 15-minute leg (listing pumps move in minutes; the desk's
other legs run 6-12h). Pages `KR_LISTING_NOTICE` / `KR_DELISTING_NOTICE` on a
tracked-or-perp-listed symbol through `ops/notify.sh` (SPEC-141 grammar, `high`
priority); everything else rides `state/kr_listings_latest.json` inbox-only.

Tests: `tests/test_kr_listings.py` (offline-deterministic, fixtures shaped from the
live responses captured 2026-08-20).
