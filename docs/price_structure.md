# price_structure — daily price structure read (Layer 4)

## Purpose
ATH/ATL + range position, squeeze events + pattern (diminishing/growing), cascade/bounce
volume, swing structure, compression — the §6 setup-shape layer (squeeze-history
pre-check for the blowoff short lives here).

## Contract
```
price_structure '{"ticker":"ESPORTS"}'
price_structure '{"ticker":"ESPORTS","days":60,"squeeze_pct":20}'
```
Out: `{ticker, range_pos, range_3d_pct, squeezes, squeeze_pattern, vol_profile, structure,
compression, vol_daily_m, young_listing}`.
`range_3d_pct` (SPEC-96) = the recent 3-day high/low range as a % of the 3d low — the
basing/coiling input the funding monitor's location classifier reads (tight range + no
new lows = coiling, not still cascading).
`vol_daily_m` (SPEC-138) = last up-to-10 days' daily quote volume in $M, oldest→newest —
the recent-volume-SLOPE input (a short window mean vs the prior window mean) that
distinguishes "attention faded" from "far below a one-off historical peak"
(`faded_bounce`'s `vol_recent_avg_m`/`vol_prior_avg_m`).

## Lifetime vs window (SPEC 57)
The full listing history is always fetched; `listing_date`, `ath_alltime`,
`ath_alltime_date`, `off_ath_alltime_pct` are lifetime facts, while `ath`/`atl` (and the
honest aliases `window_high`/`window_low`/`window_days`) are window-scoped.
`prior_cycle:true` = the all-time high predates the window AND price sits < −80% off it
— a completed boom-bust the window stats can't see (FOLKS: window "ath" 2.61 vs the
real $47 contract ATH). Never quote the window high as ATH.

## Gotchas
- §6 squeeze-history pre-check: >1 short-squeeze leg per ~10d over 60d = chronic
  squeezer — scalp-only, never a swing short (ESPORTS: 8/60d). The token's own pattern
  overrides the framework.
- `young_listing:true` = the window is shorter than requested; range_pos/z readings are
  thin — don't read a 2-week-old listing's "range low" as accumulation.
- §0.6: price level/multiples are noise — this layer feeds stage/structure, not
  cheap/expensive calls.
