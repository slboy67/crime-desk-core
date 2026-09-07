# dex_execution — DEX execution detector: EXECUTION ≠ POSITIONING (SPEC-115)

## Purpose
§8's "the SELL is off-chain/invisible — a CEX deposit is positioning, not execution"
only holds for CEX exits. A DEX sell IS visible execution: direction, size, pool, tx
hash and timestamp are all on the public ledger (Onchain-Analysis-Workshop-CrimeDesk.md
Lesson 8; memory: `feedback_predictor_vs_cause_dex_sale_is_execution`). This watches
known DEX routers/pools for large swaps by tracked-cluster wallets or their SPEC-98
fresh-wallet children, sizes them off the quote/stable leg (SPEC-53 — never the token
leg), and flags the post-exploit dump pattern (Lesson 8's UXLINK case).

## Contract
```
dex_execution LAB --json
```
Out: `{available, ticker, n_hits, hits:[{direction, usd, pool, tx_hash, timestamp,
sender, attribution:{kind: tracked|fresh_child, cluster, parent}, exploit_dump?}],
lines:[...]}`. `available:false` (with `reason`) on provider-down / untracked token /
no known dex addresses — never a clean "no execution" bill (§3).

## Config
`config/dex_execution.json`: `days` (transfer-log lookback), `min_usd`/`min_pct_float`
(the "large" swap floor, sized off the quote leg), `young_nonce_max` (the SPEC-98
fresh-wallet fingerprint, reused verbatim from `config/rotation_freshness.json`'s
`onchain_young_nonce_max` — override here only to diverge), `exploit_window_sec`/
`exploit_min_pct_float` (the EXPLOIT_DUMP_PATTERN? gate), `max_pools`.

## Mechanism
- `known_dex_addresses()` — router-1 (`0x238a3588`, confirmed shared PancakeSwap-
  Infinity plumbing per SPEC-94) and router-2 (`0xb300000b`, still apparatus-tier) from
  `config/tracked_wallets.json`, plus any DEX-hinted label in `config/known_entities.json`.
  Both routers are valid swap venues regardless of provenance — operator-specificity
  comes from the SENDER, not the pool.
- `discover_fresh_children` — a tracked wallet's own outbound token transfers name its
  recipients; a recipient whose lifetime nonce is `<= young_nonce_max` is a fresh child
  (SPEC-98's fingerprint, reused not re-derived).
- `size_swap_hits` — a hit needs a SAME-tx-hash quote-leg (stable-asset) transfer to
  correlate the swap's two legs; no correlation, no hit (never guesses a USD size off
  the token leg alone — the SPEC-53 lesson).
- `exploit_dump_flag` — a large inbound TOKEN transfer (>= `exploit_min_pct_float`% of
  float) shortly before a sell, from any source — a pattern ALERT, not an attribution
  claim.
- `build_execution` accepts `extra_wallets` for the §8 hand-curled-tip workflow (a
  user-flagged address, same spirit as `verify_wallet`'s ad-hoc lookup).

## Wiring
`rotation_freshness.classify_freshness`/`build_freshness` take a third `dex` evidence
leg (`_default_dex_leg`) alongside the existing tape/onchain legs — a qualifying DEX
sell by a tracked wallet or fresh child while the tracked top holder is frozen flips
FROZEN → ROTATED, and is cited FIRST in the evidence line (senior to the CEX-inference
leg: execution confirmed, not inferred). This rides through the existing SPEC-98
surfacing in `onchain.build_onchain` (`distribution_freshness`) and `brief`'s headline
synthesis with no further wiring — the `EXECUTION: ...` line is embedded in
`distribution_freshness.line` when the leg fires.

## Gotchas
- Every hit needs a matching quote-leg transfer in the SAME transaction — a token
  transfer to a pool with no correlated stable-asset inflow is silently dropped, not
  guessed at.
- `exploit_dump_flag` never fires without a `float_supply` (no price/float → no %,
  never a guessed flag).
- Router-2 (`0xb300000b`) has no `kind` field in `tracked_wallets.json` (only router-1
  was SPEC-94 reclassified) — `known_dex_addresses` also matches on the `ROUTER-2-*`
  label text; don't assume `kind=='router'` alone covers every apparatus router.
