---
name: feedback_funding_0005_is_placeholder_not_flat
description: A Bybit funding read of exactly +0.005% in regime_check/triage is a FETCH-FAILURE PLACEHOLDER, not a real flat rate. It masks deep-neg veto conditions. Verify live from the venue ticker before any funding-based (esp. short-veto) call.
metadata:
  type: feedback
---

**Rule:** When `regime_check` or `triage` reports Bybit funding as **exactly +0.005%**, treat it as a **failed live read / placeholder**, NOT a genuine flat rate — especially when the same `0.005` appears across many names at once (the tell). It silently hides deep-negative funding, which is the input to the short-side veto (§5). Verify the live per-interval rate from the venue ticker before any funding-based verdict.

Direct verify (Bybit, the tiebreaker when the engine is suspect):
- `GET https://api.bybit.com/v5/market/tickers?category=linear&symbol=<SYM>USDT` → `fundingRate` (per-interval, ×100 = %).
- `GET .../v5/market/instruments-info?...` → `fundingInterval` (minutes; 240 = 4h) to normalize the unit.

**Why:** EDEN — `regime_check`/triage showed Bybit **+0.005% ("flat")**; I reported flat and was about to treat a +27% bounce as a fadeable dead-cat *short*. User said "−3 on bybit." Live Bybit API: **fundingRate −0.01653 = −1.65%/4h** (interval 240m) — deep negative. `analyse` had actually shown −1.52% (closer to truth); the +0.005 was the placeholder. The short was **VETOED** (deep-neg = crowded-short squeeze fuel; EDEN is LAB's XTOKEN-MM cluster-mate = active-pump archetype, shorts being farmed). The placeholder nearly produced a veto-violating short.

**How to apply:**
- Never make a short-veto decision off a `0.005` Bybit print — verify live first.
- The whole triage funding column is suspect when multiple names share `0.005`; real deep-neg names (came through as −0.62/−0.45/−2.37 etc.) are fine, but "flat 0.005" names need a live re-check before initiating any short.
- A dead-cat-bounce *fade* is a valid setup in general — but NOT into deep-neg funding; that combo is the framework error the veto blocks. See [[feedback_amm_active_market_making_mechanism]], [[feedback_zach_says_no_short]], [[feedback_pull5_funding_bug]], [[feedback_triage_memo_vs_live_price]], [[feedback_arkham_xtoken_mm_is_bitget_hot]].
