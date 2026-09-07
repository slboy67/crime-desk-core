# regime_flip — live-vs-stored funding drift (SPEC 11/13/44)

## Purpose
A watchlist memo is a point-in-time read; the highest-signal drift is the funding-sign
flip (§2/§3 phase change). Re-verifies LIVE funding per token, diffs against the stored
regime + memo, tags REGIME_FLIP / ZONE_BLOWN / CONFIRM / DUST.

## Contract
```
regime_flip '{"ticker":"BSB"}'      # one token; omit for the full watchlist
```
Out: `{ticker, tag, note}` rows; `live_perp()` (imported by classify/brief/radars)
returns the cross-venue live read `{venue, funding_pi, funding_4h, all_floor,
funding_suspect, funding_split, price, vol_m, oi, venues{…}}`.

## Rules encoded
- Funding read from the LIVE venue ticker, never a settled/annualized print.
- SPEC 11: thresholds compare on %/4h-normalized funding (a 1h −0.17 is −0.70%/4h).
- SPEC 13: cross-venue — the more-vetoing NON-floor venue wins; venues straddling
  −0.30%/4h flag `funding_split`.
- SPEC 44: a +0.005 floor print is a data FAILURE. Floored primaries consult the
  Bitget/Aster secondaries lazily; floor on ALL covered venues → `all_floor` flat
  (tag leads the CONFIRM note); floor on the ONLY venue → `FUNDING_SUSPECT`, funding
  nulled, never a flat CONFIRM.
- SPEC 49: board runs serve venue reads from a per-run bulk snapshot
  (`load_venue_snapshot`); single-ticker reads stay per-symbol.

## Gotchas
- §5: the engine vetoes a SHORT at funding ≤ −0.30%/4h — do not loosen.
- A memo already reconciled ([REGIME_FLIP…) is not re-tripped; the structured
  `regime` baseline is the diff source.
