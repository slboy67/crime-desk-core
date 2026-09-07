# regime_check — cross-venue funding/OI table (SPEC 18/19/26)

## Purpose
One ticker's full funding/OI picture across Binance/Bybit/Aster/Bitget: regime per §2,
divergence (§8), funding/OI z-scores vs the token's OWN history. The deep read behind a
funding verdict.

## Contract
```
regime_check '{"ticker":"EDEN"}'
```
Out: `{ticker, funding:{venue:{latest, history, regime, z}}, oi_z:{venue:{today, mean,
std, z, n}}, oi_24h:{venue:{first, last, trend_pct}}, divergence}`.

## Gotchas
- SPEC 18: the series HEAD is the live predicted rate; the settled history is only the
  z-score sample (settled +0.005% vs live −1.72% hid the EDEN veto).
- SPEC 19: the 0.005% venue floor is a placeholder — nulled in history; ALL-floor
  series = genuinely flat (`all_floor`), distinct from unavailable.
- §3: every threshold is per-interval normalized to %/4h; verify against the venue's
  funding interval, not an assumed 8h.
