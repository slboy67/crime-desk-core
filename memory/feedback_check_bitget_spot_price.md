---
name: feedback_check_bitget_spot_price
description: "Check Bitget SPOT price + 24h vol on these Cat A coins — it's the apparatus venue; cvd_spot_perp.py is blind to it (reads a ~3min fills window, false-flags UNRELIABLE_THIN_SPOT)"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 8e11afa5-c50d-49e8-984f-fa56c43b8784
---

For these crime-pump Cat A coins, **always check the Bitget SPOT price + 24h volume directly** (`api.bitget.com/api/v2/spot/market/tickers?symbol=<T>USDT`) — don't rely on `cvd_spot_perp.py`'s spot read.

**Why:** Bitget IS the apparatus venue (`0x1ab4973a` = Bitget Hot Wallet runs the cluster — [[feedback_arkham_xtoken_mm_is_bitget_hot]]). The operator's real spot hand shows up on Bitget, so the true spot price/liquidity and the cross-venue divergence live there. **`cvd_spot_perp.py` is blind to it**: it computes spot off a `fills?limit=500` window (~3 min on a busy coin) and judges spot liquidity off that tiny sample, so it reported `Bitget $0.0M → UNRELIABLE_THIN_SPOT` for LAB 2026-06-02 — when Bitget spot actually had **$21.1M/24h** real volume. The "thin spot, perp-driven, reason-#4 hedge" verdict was *blind*, not correct. This likely mis-labels/mis-vetoes longs across the whole class of Bitget-primary Cat A coins.

**The cross-venue read it unlocks (LAB 2026-06-02):** Bitget spot **$20.08** vs Binance perp **mark $19.29** = perp trades ~4% BELOW the apparatus spot. That's the OTC-hedge fingerprint in the open — a *real spot bid* on Bitget holding price up while perps are sold (the hedge leg). You see reason #4 via the spot bid being real, not via "thin spot." Always compare Bitget-spot vs Binance-perp-mark vs Bybit; a spot-premium-to-perp on the apparatus venue = real spot demand + perp hedge.

**How to apply:** (1) on any Cat A read, pull Bitget spot price + 24h quoteVolume and compare to the perp mark before trusting an UNRELIABLE_THIN_SPOT verdict. (2) TODO/known-bug: fix `cvd_spot_perp.py` to gauge spot *liquidity* off 24h ticker volume (stable) separately from the *directional CVD* (fills window), so a deep-Bitget-spot coin isn't false-flagged thin — test against a known token before changing the engine gate. Related: [[feedback_why_funding_negative]] [[feedback_engine_overflags_thinspot_shorts]] [[project_lab_thesis]].
