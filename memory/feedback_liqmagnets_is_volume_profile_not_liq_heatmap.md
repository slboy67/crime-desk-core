---
name: feedback_liqmagnets_is_volume_profile_not_liq_heatmap
description: liq_magnets is a VOLUME-PROFILE HVN proxy (NOT order-book / NOT a liquidation heatmap). When the user reads an actual liq heatmap, that is the authoritative liquidity layer — anchor targets to it, not to the HVN proxy.
metadata:
  type: feedback
---

**Rule:** `liq_magnets` returns **volume-profile HVNs + round-number magnets** — historical *price-acceptance* zones. Its own desc says **"NOT order-book."** It is **not** a liquidation heatmap. A volume HVN (where lots traded) and a liquidation cluster (where leveraged positions get liq'd) are different things and often sit at different prices. When the user reads an actual liquidation heatmap (Coinglass-style, the order-book/leverage layer the engine does not have), **that is the authoritative liquidity map — defer to it and anchor TP/target levels to it.** Don't quote the HVN proxy as if it were the liq cluster.

**Why:** On the ESPORTS short I claimed TP1 ~$0.044 "confident" off the `liq_magnets` HVN at $0.0434. User's heatmap showed the actual liq cluster from current (~$0.0493) down to **$0.045** — i.e. the high-confidence draw was ~$0.045, and my $0.0434 was *below* the visible liquidity (a stretch/second-leg target, not the base case). I'd presented a volume-profile proxy as a liquidation read.

**How to apply:** Use `liq_magnets` for volume structure / acceptance zones and round-number pulls only. For where price is *drawn to grab liquidity*, the user's heatmap is the real layer — ask for / defer to it, and set the near target at the cluster it shows (bank TP1 *into* the cluster), treating any HVN beyond the cluster as a runner target, not the base case. See [[feedback_stop_beyond_the_sweep_not_on_it]], [[feedback_weight_structure_layer_vs_onchain_print]], [[feedback_tp_inside_round_magnet]].
