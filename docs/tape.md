# tape — per-minute microstructure classifier (SPEC 31/35/38)

## Purpose
Operationalizes the operator-seat OI-reading lens (§0.6.2/3): labels each bar into the
four-force vocabulary (longs/shorts opening/closing) and flags staged-play sequences —
fake-breakdown short-recruitment, chain-squeeze through a round number,
tail-of-liquidation, downtrend slam. Read-only intel; NO auto-verdict.

## Contract
```
tape '{"ticker":"SKYAI"}'                          # 60-min window, binance
tape '{"ticker":"SKYAI","venue":"bitget","window":120,"level":0.17}'
```
Out: `{ticker, venue, oi_interval, window_min, bars:[{ts, price, d_price_pct, d_oi,
oi_force, forced, taker_imb, liq_long_usd, liq_short_usd}], patterns:[{type, bar_range,
detail}], round_numbers_near, liq_available}`.

## Example
`patterns: [{"type":"fake_breakdown_wick","bar_range":[12,15],"detail":"wick through
0.17 support, OI UP +4% (shorts recruited), full reclaim in 2 bars"}]`

## Gotchas
- `oi_interval` has a 5m floor (venue API granularity) — per-minute price bars share an
  interpolated OI step; `d_oi` inside one OI step is attribution, not measurement.
- SPEC 38: profit-take vs capitulation context keys on the OI-FORCE composition of the
  preceding leg, not just price location.
- §0.5: tape is a READ. A chain-squeeze pattern is data for the Designer, not a BREAK.
- SPEC-83: `defended_fade` (or `null`) reads the rolling wall history that `brief` records
  (read-only, no extra fetch) — `{wall_growing, snapshots, last_wall_notional_usd, last_mid,
  stall_lower_high}`. It composes the wall-thickening tell with the tape's lower-high stall
  so the wall-fade signal is visible between triggers; run `brief` to refresh the book.
