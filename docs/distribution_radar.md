# distribution_radar — distributing-wallet discovery (SPEC 15/29)

## Purpose
DISCOVERS distributing wallets across Cat A names: recipients seeded from tracked safes
(HIGH confidence) + independent top holders distributing (MED). Perp-fused (funding/veto
tag per token), change-detected across runs.

## Contract
```
distribution_radar '{"token":"SKYAI","days":7}'
distribution_radar '{}'                         # sweep, default 4-day seed window
```
Out: `{scanned, alerts_total, tokens:{TICKER:{available, perp, distributors:[{address,
confidence, seeded, seeded_from, balance, balance_usd, last_out, dex_selling, verdict}],
alerts, n_candidates}}}`.

## Gotchas
- §8: a CEX deposit is positioning, not execution — the SELL is off-chain/invisible;
  timing (pre-pump/mid-pump/post-cascade) decides what a deposit means.
- SPEC 28: board destinations are classified CEX-execution vs internal-consolidation —
  trace MM-labeled hops to the CEX before believing a label
  (memory: feedback_trace_mm_labeled_hops_to_the_cex).
- SPEC 29: partial results + progress on timeout, same as onchain_radar.
