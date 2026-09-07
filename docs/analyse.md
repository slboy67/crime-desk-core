# analyse — combined perp + on-chain verdict engine

The full convergence read for one token: pulls every data layer (perp / on-chain /
spot-perp CVD / intraday structure / per-side OI / HL whales) **concurrently**, runs
the deterministic gate stack, and returns one verdict. Native — replaces the old
delegated+filtered path.

Use `analyse` for the deep single-token verdict; `classify`/`triage` for the board;
`onchain`/`wallet_state` for just the on-chain layer.

## Call

```
python3 orchestrator.py analyse '{"ticker":"LAB"}'
python3 orchestrator.py analyse '{"ticker":"LAB","days":30}'   # on-chain lookback
```

## Contract

```json
{ "ticker":"LAB", "verdict":"WATCH", "direction":"WATCH (neg-funding §4 incomplete)",
  "tier":"WATCH",
  "reason":"neg-funding LONG blocked (§4 confluence incomplete): OI not rising; no spot-CVD up-divergence",
  "onchain":"UNAVAILABLE", "onchain_ms":90014,
  "perp_score":40, "onchain_score":0, "funding_4h":-10.0, "oi_chg_pct":-49.8,
  "oi_chg_pct_48h":-49.8,
  "near_ath":false, "cvd_verdict":"SPOT_REAL_WINDOW_THIN",
  "cvd_venue":"bitget", "spot_coverage":"full", "spot_vol_24h_usd":21000000,
  "price":23.7, "notes":[ "...", "..." ] }
```

- `verdict` = coarse `LONG|SHORT|WATCH|PASS`; `direction` = the full gated label; `tier`
  = `STRONG|MILD|CAUTION|WATCH|PASS`. **`{ticker,verdict,tier,direction}` is preserved in
  every path** (incl. on-chain unavailable / perp-only).
- `reason` = one line naming the deciding gate (for a blocked neg-funding long, names the
  failed §4 leg). `notes` = the full convergence trace.

## SPEC 1b — hard on-chain budget (90s)

The on-chain layer is capped at `ONCHAIN_BUDGET=90s`. If it doesn't return in time it is
**dropped** and the run completes perp-only with `onchain:"UNAVAILABLE"`; it never hangs
the run or surfaces as `ok:false`. `onchain` ∈ `OK | DEGRADED | UNAVAILABLE | UNMAPPED`,
with `onchain_ms` the measured time. Layers run concurrently, so a typical call is ~90s
(well under the 150s orchestrator timeout) even when on-chain maxes its budget.

## SPEC 4 — the §4 neg-funding LONG confluence gate

A LONG on **negative funding** (`funding_4h ≤ −0.10%/4h`) is only emitted when the FULL
§4 confluence is present:

1. **multi-sigma-neg funding** (`≤ −0.30%/4h`),
2. **OI rising** (loading, not flat/falling = hedge),
3. **a price trigger** (a swept-magnet bounce — *not* a fresh-ATH chase), and
4. **spot-CVD diverging UP** (`BULLISH_DIVERGENCE`).

Any missing leg → `WATCH` (spectate), with `reason` naming the failed leg. Crucially, when
funding is deep-neg AND price is at/near a fresh ATH AND on-chain is clean/locked/unverified
— the **OTC vesting-hedge trap** (LAB/RIVER/RAVE, §4) — on-chain-clean is treated as
**NEUTRAL, not bullish**, and the verdict is `WATCH (spectate / pre-unlock short)`, never
LONG. Missing/unavailable CVD ⇒ cannot confirm leg 4 ⇒ WATCH, never LONG.

This is the difference between spectating LAB correctly and longing a delta-neutral hedge
squeeze. The other gates (liquidity, blowoff-short deep-neg veto, CVD hedge-trap veto,
short-side deep-neg veto, OI-wash caveat) are ported verbatim from the proven engine.

### SPEC-89 — "on-chain not distributing" is satisfied only by a clean top-holder check

The §4 long gate's "is the operator distributing?" leg must NOT be answered by the nonce-QUIET
signal alone (it reads staging, not actual token transfers — the BLESS/H false-quiet long). When
funding is in the deep-neg LONG region and the perp leans long, `build_analyse` runs
`_top_holder_distribution_check` (the `verify_wallet` sweep of the top holders, Moralis-gated).
If a top holder / cluster escrow is **DISTRIBUTING**, the gate flips to `PASS (top-holder
distributing)` — a reason-#4 hedge/distribution **TRAP**: you are the exit liquidity, not
trapped-short squeeze fuel. The check is bounded to that case (quota-frugal) and degrades to
unchecked on any quota/provider failure (never a false TRAP). The result rides on
`top_holder_distribution: {checked, distributing}` in the verdict.

## SPEC 42 — the spot-CVD leg reads the token's REAL spot venue

The CVD layer is now the native `capabilities/cvd.py` (see `docs/cvd.md`), not the
Binance-centric `_oldrepo` script: the primary spot venue is resolved by 24h volume
(Binance spot ↔ Bitget spot, then GeckoTerminal DEX pools via the tracked contract) and
spot CVD is computed from *that* venue; perp CVD stays Binance futures for parity.

- Output adds `cvd_venue` (`binance|bitget|dex:<net>|null`), `spot_coverage`
  (`full|partial|none`), `spot_vol_24h_usd`; `notes[]` always names the venue the CVD
  came from.
- **Degrade-EXPLICIT (SPEC-25 doctrine):** no spot read anywhere → `cvd_verdict:
  "UNAVAILABLE"` + `spot_coverage:"none"`, and the §4 CVD leg is treated as **UNKNOWN**:
  a neg-funding LONG still cannot be confirmed (WATCH, the reason names the UNAVAILABLE
  leg), but it is **never a false negative** — the BEARISH hedge-trap veto does not fire
  off a missing read, and a positive-funding LONG is only tier-downgraded, not vetoed.

## SPEC-71 — funding floor demotion (a floor print never wins selection)

The ±0.005%/interval venue print is a floor **sentinel** — a placeholder the venue emits, not
a rate (it once masked −1.65%/4h; memory `feedback_funding_0005_is_placeholder_not_flat`).
Two leaks fixed:

- `_hardened_funding_4h` (the SPEC-25 cross-venue backfill) demotes floor prints: the
  most-vetoing **non-floor** venue wins; only when every covered venue is floored does a
  floor return, tagged `'all_floor'` (≥2 venues = corroborated ~0%, SPEC 19/44) or
  `'suspect'` (lone venue, no corroboration). Returns a 3-tuple `(fr_4h, venue, floor_state)`.
- perp_analyser's own Bybit live print is floor-checked in `_backfill_perp` (on the RAW
  per-interval value — normalization shifts the sentinel): floored ⇒ re-resolve cross-venue
  and use the real rate (`funding_floor_demoted:true` + a note), else keep it flagged
  (`floor_suspect`/`all_floor`).

Output: `funding_venue` is always attributed (Bybit passthroughs included — never null),
plus `floor_suspect` / `all_floor` booleans. `converge()` is untouched.

## Gotchas

- Native engine; the data sub-analysers still live in `_oldrepo` (called with `--json`).
  Retired `filters/analyse.py` is kept for the old delegated banner path if ever needed.
- Slow by nature (live multi-venue + on-chain). 150s orchestrator timeout; on-chain self-caps at 90s.

## Tests

`tests/test_analyse.py`: deterministic unit tests of the §4 gate (full confluence allows
LONG; missing CVD / falling OI / fresh-ATH-clean → WATCH; positive funding not gated) and
the on-chain budget (timeout→UNAVAILABLE with perp verdict intact; OK/DEGRADED statuses;
liquidity gate), plus a live orchestrator smoke (SPEC-123: gated behind
`CRIMEDESK_LIVE_TESTS=1`, skipped by default — set it to run live).
`tests/test_cvd_venue.py` covers the SPEC-42 venue resolution + the UNAVAILABLE/UNKNOWN
gate-leg wiring.
