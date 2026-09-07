# funding_surveil — standing extreme-negative-funding monitor (SPEC-93, +SPEC-96 location layer)

The funding mirror of `ops/surveil.sh` (the nonce-surveil cadence). `scan` already does the
cross-sectional read over the whole perp universe and buckets the deep-neg LONG side — but it
is **pull-only**: someone has to run it. This is the **standing monitor** that watches the
deep-neg band continuously and *pages* when a name crosses into it.

Motivating case: **IN** (deep-neg −2.5%/4h at a fresh ATH + OI +41%, real $14M spot) — a
squeeze-loading §4 LONG candidate the desk only saw because the ticker was dropped manually.
This is the layer that would have surfaced it unprompted.

## What a tick does

`ops/funding_surveil.sh` (launchd: `ops/com.crimedesk.funding-surveil.plist`, every 15 min):

1. **Read** — runs the deep-neg LONG side of `scan` (`--side long --thresh 1.0 --min-vol 10
   --include-hl`). Reuses scan's universe fetch + the SPEC-11 `to_4h` normalization; no
   re-implementation.
2. **Cross-venue verify (§3)** — each band candidate is re-read with `regime_flip.live_perp`,
   which takes the **most-negative NON-floor venue** as the rate and flags a lone floor /
   placeholder print as SUSPECT. A floor/zero/null is a **data FAILURE, not a datum**
   ([[feedback_funding_0005_is_placeholder_not_flat]]) → such a candidate is **rejected**, never
   paged.
3. **Enrich for the §4/§5 decode** — the value is the disambiguation baked into the alert, not a
   bare number:
   - **OI direction** vs the persisted baseline: deep-neg **+ OI rising = §4 squeeze-LONG
     candidate** (the IN signature, loading not hedging); deep-neg **+ OI flat/falling =
     hedge / OTC-distribution trap** (reason #4 — you = exit liquidity, *veto-context*). Funding
     alone is the coin-flip the gate exists to filter, so the OI context is mandatory.
   - **§5 short-veto** on every deep-neg hit regardless of OI.
   - **Liquidity tier** (`liq_tier`): `<$10M` = DUST (marked, **never paged** as tradeable),
     `$10–25M` = scout, `≥$25M` = tradeable.
   - **Price LOCATION (SPEC-96)** — deep-neg + OI-rising is a §4 **coin-flip** in isolation
     (trapped-shorts that squeeze vs delta-neutral hedge/arb that never does). The TAIKO +126%
     (2026-07-01) taught the screenable discriminator: **where the price sits.** Each candidate
     carries a `location_tag` (from a live `price_structure` snapshot, or an injected one):
     - **near a HELD low + basing + OI rising → `SQUEEZE-LONG (high conviction, TAIKO-type)`**
       (severity **HIGH**) — shorts are directional at a bottom, the low held (sellers exhausted),
       base-shorters trapped. The *take* (size downgraded for any incomplete leg, not a flat pass).
     - near a **HIGH** + OI rising → `AMBIGUOUS (hedge/AMM/blowoff — needs gate)` (**MED**) — the IN
       case, which then blew off −70%. Not a long; maybe a blowoff-short-watch.
     - near a low but **NOT basing** (fresh cascade) → `FORMING (watch for a base)` (lower-severity
       **WATCH**) — the LAB post-cascade case, tracked into the setup, not yet armed.
     - OI flat/falling → `no load` (de-prioritized).
4. **Change-detection** — the baseline (`state/funding_baseline.json`, per-ticker, like the
   nonce baseline) is the primary dedup. A **PAGE** fires only when (a) a **NEW** name enters
   the band, (b) one **materially deepens** (WATCH→HIGH, i.e. crosses −2.0%/4h), or (c) OI-context
   **flips to rising**. A name sitting in the band **logs but does not re-nag**.
5. **Record + page** — the LOG line (`state/funding_alerts.log`) and the inbox event
   (`inbox.append_event`, source `funding-surveil`) are **always** written for a PAGE hit; the
   macOS push notification is additionally throttled through `ops/page_gate.py` (band tier = the
   page-gate `dest_kind`, so a WATCH→HIGH deepen pierces the cooldown as a tier-upgrade).

On-demand reads stay `scan` — this is the cadence + verify + enrich + page layer on top, not a
new query capability.

## Thresholds (config defaults, `ops/funding_surveil.py`)

| const | value | meaning |
|---|---|---|
| `BAND_ENTER` | −1.0 %/4h | enters the WATCH band |
| `DEEP_EXTREME` | −2.0 %/4h | HIGH tier (the clamp region; IN was −2.5) |
| `OI_RISE_PCT` / `OI_FALL_PCT` | ±10 % | OI rising / falling vs baseline (mirrors analyse's §4 `oi_chg>10`) |
| `DUST_VOL_M` / `SCOUT_VOL_M` | 10 / 25 $M | §7 liquidity gate |
| `DEEP_NEG` (from `regime_flip`) | −0.30 %/4h | §5 short-veto line (reused, not redefined) |
| `NEAR_LOW_PCT` / `NEAR_HIGH_PCT` | 25 % | ≤ this above the low / below the high = "near" (SPEC-96) |
| `BASING_RANGE_PCT` | 25 % | recent ~3d high/low range below this = coiling (basing) |
| `LOW_HELD_BOUNCE_PCT` | 2 % | price bounced ≥ this off the low = held (not a fresh-breaking low) |

## Inbox copy

Carries the decode, e.g.:

```
DEEP-NEG: IN -2.50%/4h (bybit, non-floor; binance -2.00) + OI +41% → §4 squeeze-LONG candidate
 · §5 short-veto · spot $14M (scout). Run brief/tape before sizing.
```

With a price snapshot the tag becomes the location verdict (SPEC-96), e.g.:

```
DEEP-NEG: TAIKO -1.50%/4h (bybit, non-floor) + OI +71% + basing 21% off held low
 → SQUEEZE-LONG (high conviction, TAIKO-type) · §5 short-veto · spot $40M. Run brief/tape before sizing.
```

## Install

```
bash ops/install_launchd.sh --only funding-surveil   # renders the plist template for this machine + loads it
```

## Offline / debugging

Set `FUNDING_SURVEIL_SCAN` (a `scan --json` envelope) and optionally `FUNDING_SURVEIL_PERP`
(a `{ticker: live_perp}` table) and `FUNDING_SURVEIL_PRICE` (a `{ticker: price_ctx}` table of
SPEC-96 location snapshots — `{price, low, high, range_3d_pct, new_lows}`) to run a deterministic
tick without network. The pure decision core (`oi_context`, `decode`, `pct_above_low`,
`is_near_held_low`, `is_basing`, `classify_location`, `enrich_hit`, `assess`, `run_tick`) is
unit-tested offline in `tests/test_funding_surveil.py`.
