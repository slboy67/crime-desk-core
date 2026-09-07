---
name: feedback_analyse_liquidity_gate_bybit_only
description: analyse.py liquidity gate reads Bybit-only turnover — false-PASS on Binance-primary tokens; cross-check the real venue
metadata: 
  node_type: memory
  type: feedback
  originSessionId: b126a2a6-a6d8-4177-a0fa-679af095765b
---

`analyse.py`'s liquidity gate computes 24h turnover from **Bybit only**. On tokens whose primary perp venue is Binance (or elsewhere), it reads a thin Bybit book and auto-PASS's on "liquidity FAIL <$10M" when the token actually trades fine.

**Why:** RIVER 2026-05-22 — engine read Bybit turnover $9.3M → auto-PASS (liquidity). Live check showed Binance perp $50M/24h, CoinGecko spot $12.7M, Vol/OI 3× (clean, not brushed). The token clears the gate easily for moderate size; the gate FAIL was a single-venue blind spot.

**How to apply:** when `analyse.py` PASS's on the liquidity gate, before accepting it cross-check the real venue — `curl fapi.binance.com/fapi/v1/ticker/24hr?symbol=XUSDT` (quoteVolume) + CoinGecko aggregate vol. Only honor the liquidity PASS if turnover is thin across ALL real venues, not just Bybit. The PASS reason may be a false negative; find the actual trade-killer (e.g. flat funding / no trapped side) instead. Related: [[feedback_pull5_funding_bug]], [[feedback_engine_first_architecture]].
