# liq_magnets — liquidation-magnet estimator (Layer 3)

## Purpose
Volume-profile HVN proxy + round-number magnets above/below price: where stops/liqs
cluster, for §7 magnet execution (stop beyond the sweep-wick, TP 2–5 bps inside the
round magnet).

## Contract
```
liq_magnets '{"ticker":"HMSTR"}'
liq_magnets '{"ticker":"HMSTR","days":7,"interval":"5m","buckets":150}'
```
Out: `{ticker, current, hvns, upside_magnets, downside_magnets, round_numbers}`.

## Gotchas
- This is a VOLUME-PROFILE PROXY, not a liq heatmap — anchor TP zones to the live
  heatmap/DOM, never to this output alone
  (memory: feedback_liqmagnets_is_volume_profile_not_liq_heatmap).
- §7: a TP at the exact round level fade-skips (HMSTR +5%→0); a stop ON the magnet is
  donated to the MM sweep.
