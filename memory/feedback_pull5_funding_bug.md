---
name: feedback-pull5-funding-bug
description: pull5/regime_check funding can be stale last-settled value + wrong interval label — verify live fundingRate from the venue ticker
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 923b18ba-24cd-47e2-b2f8-58163b4782ad
---

`pull5.py` / `regime_check.py` funding output is unreliable in two ways: (1) it prints the last *settled* funding rate, not the live/predicted rate — so a fresh flip is missed; (2) it labels everything "per 8h" but venues use different intervals (Bybit LAB funds every 240 min / 4h).

**Why:** 2026-05-19, pull5 reported LAB Bybit funding "+0.0050% per 8h" across all periods. User reported "-170". Direct Bybit v5 API check (`/v5/market/tickers`) showed live `fundingRate` = -0.0754% with `fundingInterval` = 240 min. The "+0.005%" was the last settled value; funding had since flipped negative and pull5 never saw it.

**How to apply:** When funding is decision-relevant, verify against the venue ticker endpoint directly — Bybit `/v5/market/tickers?category=linear&symbol=XUSDT` gives live `fundingRate` + `nextFundingTime`, and `/v5/market/instruments-info` gives `fundingInterval`. Always confirm the interval before applying Section 2 thresholds (−2%/4h trap, <−0.10%/4h trigger are PER-INTERVAL). Also: an annualized funding figure (e.g. "−170%") = per-interval rate × intervals/day × 365 — never compare an annualized number to a per-interval threshold. Related: [[feedback-cross-rpc-verify]].

**REPEAT FAILURE 2026-05-21 BB thesis:** I called BB a STRONG LONG trap-formation pre-squeeze setup with "Bybit funding z-4.30 HISTORICALLY EXTREME." Source was the last settled rate (-0.267% at 20:00 UTC, ~3h prior). User checked Velo.xyz which showed Bybit funding NEUTRAL live. Direct Bybit ticker query confirmed live = +0.005% NEUTRAL. The -0.267% was a SINGLE PRINT in an alternating mixed regime — not a sustained trap-formation. The "STRONG LONG" verdict was withdrawn after verification. **This lesson EXISTED and I had read it earlier in the same session — I still didn't apply it before calling the trade.**

**Mandatory rule going forward — applies BEFORE any funding-based verdict (STRONG/MILD/etc.):**

1. **If `regime_check.py` shows funding |z| ≥ 2 or |rate| ≥ 0.10%/interval as the central reason for a directional verdict → STOP and verify live ticker BEFORE delivering the verdict.** Not after. Not when asked. As part of generating the verdict.
2. Cross-check: settled funding from `/v5/market/funding/history` (Bybit) or `/fapi/v1/premiumIndex` (Binance) — look at the last 5-6 settles. A single extreme print bracketed by mixed/neutral settles ≠ regime. Section 2 explicit: "Funding rate (regime, not snapshot)."
3. Confirm venue's actual funding interval — Binance perp can be 4h OR 8h (varies by symbol); Bybit usually 8h on stablecoin pairs, 4h on others. `regime_check.py` blanket-labels "per 8h" which is wrong for 4h-interval symbols.
4. **The Velo.xyz live display is authoritative** — user can cross-check there in 5 seconds. When my data conflicts with Velo, I'm wrong, full stop. Apply that with extreme prejudice.

The cost of the repeat: 1 false-strong verdict on BB (caught by user before they entered). The cost if user HADN'T checked Velo: another loss on a setup built on stale data. This must not happen again — the verification is fast and free.
