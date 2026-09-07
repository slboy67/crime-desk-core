# scan — the discovery scanner

The cross-sectional net. Where `triage` analyses the watchlist and `onchain`
analyses one named token, `scan` **discovers** candidates: it screens the entire
Bybit linear-perp universe for the playbook's core cross-sectional signal —
funding extremes normalized to %/4h (CLAUDE.md §2/§3). Native port of the parts-bin
`scan_all.py`.

## Call

```
python3 orchestrator.py scan '{}'                                  # both sides, gate $10M
python3 orchestrator.py scan '{"side":"long","min_vol":25}'       # long squeeze-fuel, tighter liquidity
python3 orchestrator.py scan '{"thresh":0.30,"top":10}'           # only sharper funding, top 10/side
```

Args (all optional): `side` (long|short|both), `min_vol` ($M turnover gate),
`top` (per side), `thresh` (funding %/4h threshold, default 0.10).

## Contract

```json
{ "universe": 412, "min_vol_m": 10, "thresh": 0.10, "side": "both",
  "longs":  [ {cand}, ... ],   // funding ≤ −thresh, most-negative first
  "shorts": [ {cand}, ... ] }  // funding ≥ +thresh, most-positive first
```

Each `cand`:
```json
{ "ticker": "MYX", "funding_4h": -0.83, "funding_raw": -0.277, "interval_h": 1,
  "turnover_m": 457.7, "chg24": 54.4, "oi": 1234567.0, "price": 2.31,
  "deep": true, "side": "long" }
```

- `funding_4h` — per-interval funding **normalized to %/4h** (never annualized) so
  symbols on 1h/4h/8h intervals compare directly. This is the ranking key.
- `funding_raw` / `interval_h` — the unnormalized rate and its native interval.
- `deep: true` when `|funding_4h| ≥ 0.50` (MYX-tier extreme).
- 🟢 `longs` = shorts paying carry (squeeze-fuel); 🔴 `shorts` = longs paying carry.

## Reading it (the judgment layer)

- This is a **first-pass net**, not a verdict. A hit means "funding is extreme here" —
  the squeeze/trap reasons (§4 gate) still need OI spiking, the right CVD divergence,
  and structure before any entry. Deep-neg funding alone is a coin-flip (§4).
- **Surface longs with equal weight** (CLAUDE.md §1) — `both` is the default; don't
  default to `side:"short"`.
- Funding is LIVE/predicted, single-venue (Bybit). Confirm on the trade venue
  (Velo authoritative) before acting.

## Gotchas

- `--json` → stdout JSON only; bare command → colored human columns (non-JSON).
- Liquidity gate is turnover-based; raise `min_vol` to cut microcap noise.
- One Bybit `tickers` + one `instruments-info` call — fast (~60s cap), no per-symbol fan-out.
- `--render compact` (SPEC-193, any mode incl. `--scout`/`faded_bounce`/`oi_surge`): caps
  the `excluded`/`dropped_below_top` bookkeeping lists to `{n, sample:[...]}` (first 3) —
  those can run to the size of the whole scanned universe; candidate/board rows
  themselves are already lean and pass through unchanged.

## SCOUT board (`--scout`, SPEC-82)

`scan` doubles as a **signal scanner over the watchlist**: it runs the §6 `setup_score`
checklists across every watchlist name and surfaces the graded tiers — so the board isn't
silent between WATCH and ARMED (confluence is the AVOID-filter, the near-armed ramp is the
signal). It surfaces SCOUT **even with no pre-committed thesis**.

```
python3 capabilities/scan.py --scout --json                         # live (network)
python3 capabilities/scan.py --scout-signals '{"LAB":{...}}' --json # offline (injected)
```

Output `{scout:[row], armed:[row], quiet:[ticker]}`; each row
`{ticker,setup,verdict,tier,score,required,add_triggers,swing_vetoes}`:
- `scout` — best setup is **near-armed** (`score ≥ required − 1`, no hard veto). `add_triggers`
  = the missing/cascade legs that *confirm and size it up* (NOT entry gates). Defined-risk
  poke, §7 / $500 cap. **hypothesis-tier until n≥10** (§9) — ledger `tier:"scout"`, split from ARMED.
- `armed` — full confluence (the size-up / press).
- `quiet` — below the SCOUT threshold (silence is correct, §0.5) — not surfaced as rows.
- `swing_vetoes` — a chronic-squeezer name stays SCOUT (scalp/scout-allowed) but flags that a
  SWING entry is vetoed ([[feedback_squeezer_scalp_not_no_trade]]). A §5 deep-neg SHORT or
  oi_sides WASH is a **hard** veto → the name drops out (not scout).

NO trade call — scores only (`setup_score` invariant).

## Tests

`tests/test_scan.py`: clean JSON, universe non-empty, candidate contract+types, the
funding-threshold partition, sort order, `--side`/`--top` filters, human view non-JSON,
and the SCOUT-board partition (`scout`/`armed`/`quiet`, offline).

## faded_bounce mode (`--mode faded_bounce`, SPEC-120)

The user's PRIMARY edge (`memory/feedback_user_edge_faded_distribution_bounces`, CLAUDE.md
§0.1): shorting the bounce of an already-played-out crime coin, still distributing post-dump,
after attention faded — no operator floor, no squeeze crowd, clean distribution read. This
mode is the one deterministic call for it, run at session-open alongside the board.

```
python3 orchestrator.py scan '{"mode":"faded_bounce"}'                         # live
python3 orchestrator.py scan '{"mode":"faded_bounce","fb_candidates":{...}}'   # offline (injected)
```

Universe = the Bybit perp universe `scan` already fetches (liquidity-gated) UNION watchlist
tickers (committed + retired within `2×bounce_window_days`). **Five hard gates** — ALL must
pass or the name is excluded entirely (never surfaced, reason recorded in `excluded`):

- `off_ath` — ≥ `off_ath_pct` (default 40%) below the trailing 90d high (the pump is over).
- `bounced` — ≥ `bounce_pct` (default 15%) above a post-dump low set within `bounce_window_days`
  (default 7d) — there's a bounce to fade.
- `faded` — (SPEC-138) recent-volume **slope**: the mean of the last `vol_slope_recent_days`
  (default 3) daily volumes ≤ the mean of the `vol_slope_prior_days` (default 3) days right
  before that × `vol_slope_max_ratio` (default 1.10) — flat-or-declining, not %-of-90d-peak.
  Current 24h volume must also still clear `vol_floor_m` ($5M default, §7 tradability floor).
  (The old %-of-peak check false-passed BICO/SNXX: a one-off historical volume spike made a
  later, still-rising print look "decayed" next to it — the exact opposite of the fingerprint's
  intent, "attention has left the name".) `vol_pct_of_peak` is still reported on each row
  (informational only, no longer gates).
- `funding_ok` — most-extreme non-floor cross-venue print (SPEC-108 selection) ≥
  `funding_floor_4h` (default −0.10%/4h). **Deep-neg below this is a HARD EXCLUDE** (§5 — a
  deep-neg bounce is squeeze fuel, the anti-pattern of this edge).
- `not_squeezing` — (SPEC-138) `squeeze_legs_60d` (count of squeeze legs in the trailing 60d,
  reported on every row — the §6 chronic-squeezer pre-check needs no extra pull) ≤
  `chronic_squeeze_legs_60d` (default 6, per CLAUDE §6 ">1 leg/~10d over 60d = chronic"). Replaces
  the old `squeeze_leg_pct`/`squeeze_window_h` pair — that 48h/30% window missed BICO (3 legs in
  3 days) and SNXX (5 legs in 21d).

**Tiering** (distribution status only affects tier, never exclusion):
- `tier-1` — on-watchlist name + top-holder read FRESH or ROTATED (SPEC-98) — "still
  distributing" is the edge.
- `tier-2` — on-chain leg unavailable/unknown (non-watchlist names, provider down — never
  implied clean, §3), or FROZEN (demoted with a `squeeze-risk` caveat — the VELVET lesson: a
  genuine pause can also be a reload, don't bank a dead-thesis short into the squeeze).

Ranked tier-1 first, then bounce size × volume-decay depth; capped at `top` (default 5,
`dropped_below_top` names the rest). Zero candidates is a normal explicit result
(`candidates: []`) — see §0.5, NONE is the answer, not a failure.

**Concurrency + per-name timeout (SPEC-183):** the live path used to enrich every candidate
ticker (90d/7d price-structure + the Aster execution gate) **serially**, which blew the
registered ~120s+ timeout on the full universe — the sweep returned `{"ok":false,"error":
"timeout running scan"}` and never completed at all. Per-name enrichment
(`_faded_bounce_enrich_ticker`) now fans out over a `ThreadPoolExecutor`
(`FADED_BOUNCE_MAX_WORKERS`, default 12) with a per-name budget (`FADED_BOUNCE_NAME_TIMEOUT_S`,
default 15s, injectable via `_faded_bounce_live_candidates(name_timeout_s=...)`). A name that
doesn't finish inside its budget is dropped into `skipped` (`[{"ticker","reason":"timeout"}]`)
— it costs itself, never the sweep (§3: listed, never silently dropped). A name whose
enrichment raises lands in `skipped` too, `reason: "error"`.

**Fail-loud arg routing:** an unrecognized `--mode` value (argparse `choices`) exits non-zero
with no stdout — through the orchestrator this surfaces as `{"ok": false, ...}`, never a
silent fallback to the default funding scan (the 8-day false-done masking bug this spec closes).

### Contract (`faded_bounce`)

```json
{ "mode": "faded_bounce", "cfg": {...}, "universe": 340,
  "candidates": [ {"ticker","tier","onchain","gates","caveats","rank_score",
                   "off_ath_pct","bounce_pct","vol_pct_of_peak","squeeze_legs_60d",
                   "funding_4h","next_step"} ],
  "excluded": [ {"ticker","reason","failed_gates","gates","squeeze_legs_60d"} ],
  "dropped_below_top": ["ticker", ...],
  "skipped": [ {"ticker","reason"} ] }
```

`skipped` (SPEC-183) is distinct from `excluded`: `excluded` names finished enrichment and
failed a gate; `skipped` names never finished (per-name timeout, or a raised error) —
always `[]` on the offline `fb_candidates`-injected path, since nothing was fetched.

Config: `config/faded_bounce.json` (documented defaults, any key overridable).

### Tests

`tests/test_scan.py` (`TestFadedBounceGates`, `TestFadedBounceUnknownModeFailsLoud`,
`TestFadedBounceCLIOffline`, `TestExistingScanModesUnchanged`): all five gates + tiering
(FRESH/ROTATED/FROZEN/unavailable), the deep-neg hard exclude, volume-not-faded exclude,
squeeze-leg exclude, zero-matches, ranking/cap, the fail-loud unknown-mode path (both the
script and through the orchestrator), and a byte-identical regression check on the default
scan modes after the `_fetch_universe` refactor. `TestFadedBounceEnrichTickerHelper`
(SPEC-183): the per-name enrichment body in isolation, offline (injected `price_structure`
module + a stubbed Aster gate). `TestFadedBounceLiveCandidatesConcurrencyAndTimeout`
(SPEC-183): a fixture with one hanging name (`enrich_fn` injected, no real network) proves
the sweep completes and the hanging name lands in `skipped` with `reason: "timeout"`,
never stalling the others.

---

## oi_surge mode (`--mode oi_surge`, SPEC-148)

Retires the manual `loris.tools/markets` browse (EVAL-loris.md §3.3): a cross-sectional net
over the full perp catalogs of Binance, Bybit, Bitget, Aster, and Hyperliquid answering
*"across the whole perp universe, where is OI being built RIGHT NOW, and which of those
names carry the desk's tells?"* — where `faded_bounce` is longitudinal (one fingerprint,
tracked over time) this is cross-sectional (every name, one moment).

```
python3 orchestrator.py scan '{"mode":"oi_surge"}'                                    # live
python3 orchestrator.py scan '{"mode":"oi_surge","oi_catalog":[...],"oi_baseline":{}}' # offline (injected)
```

**Baseline store** — `state/oi_surge_baseline.json`, per ticker: `{ts, oi_usd_total,
vol24h_usd_total, first_seen_ts}`. First run ever (file missing) SEEDS — `status:"seeded"`,
zero candidates, `ok:true` (never a silent NOTOK, SPEC-137). A **corrupt** baseline file is
LOUD: the script prints to stderr and exits non-zero with **no stdout**, which the
orchestrator surfaces as `{"ok": false, ...}` — never re-seeded mid-verdict, never read as a
clean empty sweep.

**Per-name signals** (thresholds in `config/oi_surge.json`, defaults below):
- `oi_surge` — cross-venue summed OI ≥ `oi_surge_pct` (default 40%) vs that ticker's baseline,
  **only compared when the baseline is ≤ `baseline_max_age_h`** (default 24h) old — a stale
  baseline reports `oi_delta_pct: null` + `baseline_age_h` instead of firing.
- `vol_oi_brush` — summed 24h vol / summed OI ≥ `vol_oi_ratio_min` (default 20, the §2 Cat-A
  brushing tell).
- `funding_extreme` — the most-extreme **non-floor** cross-venue print (SPEC-108 selection),
  4h-normalized (SPEC-112) before comparison — a floor print (Bybit/Binance/Aster/Bitget's
  0.005% sentinel) never wins regardless of raw magnitude. Fires as a flag when
  `|pi_4h| ≥ funding_extreme_thresh_4h` (default 0.20%/4h); always reported on the row
  (`funding_extreme: {venue, pi_4h} | null`) whether or not it fires.
- `new_listing` — ticker present in a venue catalog this run but **absent from the whole
  baseline store** (not just one venue's baseline) — `new_listing: {venue} | null`; an
  Aster/Bitget first-listing is a §2 pipeline tell.
- `dex_share_pct` — report-only (never gates), read from `config/dex_mark_weight.json` where
  populated (§0.5 mark-drag doctrine).

**Gates before ranking** (either drops the name to `excluded` with a `reason`, never into
`candidates`): `liquidity_floor` — summed 24h vol < `liquidity_floor_m` ($5M default, one
shelf below §7's $10M sizing floor — discovery looks wider than sizing says); `exclude_list`
— ticker in `config/oi_surge.json`'s static majors list (the desk hunts micro-caps).

Ranked by **number of flags firing** (desc), tiebreak **oi_delta_pct magnitude** (desc);
capped at `top` (default 10, `dropped_below_top` names the rest). Zero candidates is a normal
explicit result (`candidates: []`) — see §0.5, NONE is the answer, not a failure.

**Venue coverage / known gap:** Bybit, Bitget, and Hyperliquid all return OI in their bulk
catalog responses and carry the `oi_surge`/`vol_oi_brush` signal fully. Binance and Aster
expose no bulk per-symbol OI endpoint (verified against their documented API surface,
2026-08-19) — their catalog rows carry funding + volume + `new_listing` but `oi_usd: null`,
so they don't currently contribute to the OI signal. This mirrors the venue_map.py precedent
(Vest's dead endpoint) — implemented against the documented surface, degrades to `null`
rather than fabricating a datum, self-heals if a bulk OI endpoint appears later.

**Fan-out / fail-isolation** (SPEC-127 lesson): catalog fetches run CONCURRENTLY, one per
venue, each bounded by a ~6s budget. A dead/timing-out venue contributes nothing and its name
lands in `meta.venues_errored` — never a sweep failure.

### Contract (`oi_surge`)

```json
{ "mode": "oi_surge", "status": "ok", "cfg": {...}, "universe": 340,
  "candidates": [ {"ticker","venues","oi_usd_total","oi_delta_pct","baseline_age_h",
                   "vol24h_usd","vol_oi_ratio","funding_extreme","new_listing","flags",
                   "dex_share_pct","next_step"} ],
  "excluded": [ {"ticker","reason", ...} ],
  "dropped_below_top": ["ticker", ...],
  "meta": {"venues_errored": ["venue", ...]} }
```

`status` is `"seeded"` on the baseline store's first-ever run (`candidates`/`excluded` both
`[]`), otherwise `"ok"`. A corrupt baseline file never reaches this contract — the CLI exits
non-zero before emitting any JSON (see above).

Config: `config/oi_surge.json` (documented defaults, any key overridable). Offline testing:
`--oi-catalog` (list of `{ticker,venue,oi_usd,vol24h_usd,funding_raw_pct,interval_min,
is_floor}` rows) + `--oi-baseline` (the baseline dict) together bypass both network and the
baseline file entirely. `--oi-baseline-path` overrides only the state-file location (used
live too, e.g. for a dedicated test/staging baseline).

### Cadence

`ops/discovery_tick.sh` runs `oi_surge` every 6h (marker-gated, best-effort, output to
`state/oi_surge_latest.json`), paging via `ops/notify.sh` **only** when a candidate fires ≥3
signals or a `new_listing` lands on Aster or Bitget — the net must not become spam.

### Tests

`tests/test_scan.py` (`TestOiSurgeFundingExtreme`, `TestOiSurgeAggregation`,
`TestOiSurgeBuildBoard`, `TestOiSurgeBaselineStore`, `TestOiSurgeRunOrchestration`,
`TestOiSurgeUnknownModeFailsLoud`, `TestOiSurgeCorruptBaselineCLIFailsLoud`,
`TestOiSurgeCLIOffline`): the floor-exclusion rule (SPEC-108/112 regression), 1h-interval
normalization before extreme comparison, cross-venue OI/vol summation with partial-OI
tolerance, the 3-flags-fire-and-rank-first case, the stale-baseline no-fire case,
new_listing venue tagging, the liquidity/exclude-list gates, the seeded-first-run case, the
missing-vs-corrupt baseline-file distinction, one-venue-timeout isolation, and the fail-loud
unknown-mode + corrupt-baseline paths (both the script and through the orchestrator).

---

# screener — the BNB-chain discovery net (second scanner)

Where `scan` is cross-sectional over the whole perp universe (funding), `screener`
discovers by **structural fingerprint** within the BNB-chain ecosystem (the Cat A
pipeline, §3), perp-listed names only. Native port of the parts-bin `screener.py`.

## Call

```
python3 orchestrator.py screener '{}'                                   # pump mode (mid-cycle)
python3 orchestrator.py screener '{"mode":"accumulation","pages":3,"min_score":4}'
```

Modes: `pump` (already moving — rewards locked float + FDV/MC + recent gain + churn)
and `accumulation` (pre-cycle Pattern-A Phase 1 — rewards Cat A structure + QUIET
price + churn; hard-excludes anything already running).

## Contract

```json
{ "mode":"pump", "scanned":480, "perp_universe":{"binance":520,"bybit":610},
  "min_score":3, "baseline_age_h":12.0,
  "candidates":[
    {"ticker":"AIOT","score":6,"why":["float 8% (≥85% locked)","FDV/MC 9.1×","7d +62%"],
     "venues":"BIN+BYB","mc":..,"fdv_mc":9.1,"circ_ratio":0.08,"vol":..,"ch7":62,"ch30":..,
     "on_watchlist":true}, ... ] }
```

- `score` is a structural-fingerprint **prior, not a verdict** — confirm Cat A/B +
  clusters/flows/regime before trading.
- `on_watchlist:true` = already a committed thesis. New names → feed to `triage`.
- Coingecko's category endpoint is free-tier rate-limited; on a 429 `scanned` may be
  0 but `perp_universe` still populates (Binance/Bybit exchange-info are reliable).
- `accumulation` mode uses a volume baseline (`state/screener_vol.json`, 3–96h old) to
  reward rising churn — run twice a few hours apart to activate that signal.

## Tests

`tests/test_screener.py`: envelope/shape, perp_universe populated, candidate contract
when scanned, accumulation mode, human view non-JSON.
