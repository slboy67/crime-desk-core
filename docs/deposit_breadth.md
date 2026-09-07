# deposit_breadth — deposit-breadth metric (SPEC-118)

## Purpose
The desk's on-chain layer is almost entirely subject-level (tracked wallets, named
clusters, `verify_wallet` on known holders). It has no pattern-level aggregate: a
sudden increase in token deposits to exchanges **from many wallets** may indicate an
upcoming sell-off (Onchain-Analysis-Workshop-CrimeDesk.md Lesson 8, predictor #2).
VELVET is the desk-native proof: distribution rotated through fresh wallets NOT in
the tracked set, the tracked wallet's clock froze, and the engine was blind. This is
the cheap aggregate that catches many small untracked sellers at once — deliberately
NOT gated on `tracked_wallets`.

## Contract
```
deposit_breadth LAB --json
```
Out: `{available, ticker, spike, senders, baseline, ratio, usd, median_usd,
decomposition:{tracked, fresh, unknown}, sender_addrs, line}`. `available:false`
(with `reason`) on provider-down / no known CEX channels — never a clean "no breadth"
bill (§3). A quiet token still returns `available:true` with zero fields (that's a
verified "nothing happening", distinct from an unreadable provider).

## Config
`config/deposit_breadth.json`: `window_h` (trailing window, default 24h),
`baseline_windows` (how many prior windows form the baseline mean, default 7),
`spike_multiple` (current senders `>= baseline * this` = spike candidate, default
3.0), `usd_floor` (total USD must also clear this, default $100K), `min_abs_senders`
(small-sample guard — absolute floor regardless of ratio, default 8), `days`
(transfer-log lookback), `young_nonce_max` (SPEC-98's fresh-wallet fingerprint, reused
verbatim from `config/rotation_freshness.json`'s `onchain_young_nonce_max`),
`max_channels` (bounded provider reads).

## Mechanism
- `_window_stats` — the live seam: distinct senders depositing the token into any
  known CEX channel within the trailing window, their summed USD (spot-priced) and
  median deposit size. Every channel read failing → `None` (a coverage gap, never a
  zero-stats bill).
- `classify_breadth` — PURE core: SPIKE requires ALL THREE — the ratio gate
  (`senders >= baseline * spike_multiple`, only when a real non-zero baseline
  exists), the absolute floor (`senders >= min_abs_senders`), and the USD floor. The
  absolute floor is what stops a near-zero baseline from exploding the ratio into a
  false spike on 2 whale-sized senders.
- `decompose_senders` — PURE: buckets the spiking senders tracked / fresh (SPEC-98
  young-nonce fingerprint) / unknown, AFTER detection — corroboration, never a gate
  (the whole point is catching senders the desk has never seen).
- `build_breadth` — orchestrator: reads known CEX channels
  (`rotation_freshness._known_cex_channels`, reused not re-derived), computes the
  current window, loads the persisted baseline (`state/deposit_breadth_<TICKER>.json`
  — a rolling list of `{ts, senders}` capped at `baseline_windows`), classifies, and
  persists the new window so the NEXT call has a real baseline.

## Wiring
`rotation_freshness.classify_freshness`/`build_freshness` take a fifth evidence leg
(`breadth`, via `_default_breadth_leg`) alongside the existing tape/onchain/dex/drip
legs — a BREADTH_SPIKE while the tracked top holder is quiet flips FROZEN → ROTATED,
cited alongside the `onchain` (fresh-wallet→CEX) leg (both are inferred deposit
activity, weaker than the confirmed-execution `dex`/`drip` legs but catching what a
single-wallet read structurally misses). This rides through the existing SPEC-98
surfacing in `onchain.build_onchain` (`distribution_freshness`) and `brief`'s headline
synthesis with no further wiring — the `BREADTH_SPIKE: ...` numbers are embedded in
`distribution_freshness.line` when the leg fires. On the board it therefore only
surfaces when a distribution/rotation thesis is already live (the gate `_freshness_layer`
already applies — SPEC-118 point 4).

## Gotchas
- The FIRST call on a token has no persisted baseline (`baseline_counts=[]`) →
  `baseline=0` → SPIKE can never fire yet, even on a genuinely large sender count.
  The baseline builds up over subsequent windows — this is a cold-start property, not
  a bug.
- `usd`/`median_usd` are `0.0` (not `None`) when `price` isn't supplied — a caller
  that cares about the USD floor must pass a real spot price; an unpriced call can
  still report `senders` correctly but will never clear `usd_floor`.
- `decompose_senders` is corroboration, not a gate — a spike with 100% "unknown"
  senders still fires; that's the entire point of the metric.
