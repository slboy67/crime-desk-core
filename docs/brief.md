# brief — one-call full-stack token read (SPEC 30)

## Purpose
A bare ticker drop returns the WHOLE picture: state (classify vs committed thesis) +
perp (analyse) + BOTH venue books (depth bitget+binance) + on-chain — fused in one
envelope. Exists because hand-assembly skipped pieces (the SKYAI single-venue blind
spot: a Binance-funding CONFIRMS missing the Bitget exit book).

## Contract
```
brief '{"ticker":"SKYAI"}'             # the default — BOTH books
brief '{"ticker":"SKYAI","venue":"bitget"}'
```
Out: `{ticker, state:{available, verdict, reason, thesis_present, direction, thesis,
alerts, aster_listed}, perp:{available, verdict, direction, tier, funding_4h, funding_venue,
floor_suspect, funding_by_venue, oi_chg_pct, oi_chg_pct_48h, vol24h_usd, mc_usd,
mc_unavailable_reason, near_ath, cvd_verdict, price},
books:{available, venues{…}}, onchain:{available, signal, bias, score,
top_holder_distribution, distribution_checked, signal_caveat, …}}`.

SPEC-174 #3/#6: `vol24h_usd` (the size-venue 24h turnover, from `live_perp`'s primary-venue
`vol_m` — no second fetch) and `mc_usd` (CoinGecko, cached; `mc_unavailable_reason` names why
when it's null — never silently absent) fill in for an unmapped/off-board name that
previously had neither. `oi_chg_pct_48h` is the SAME value as `oi_chg_pct` with its window
labelled in the key (`perp_analyser.py`'s `openInterestHist?period=1h&limit=48`, a fixed
48h window) — distinct from `oi_sides`' `oi_chg_pct_4h` and `triage`'s `oi_chg_pct_24h`,
three genuinely different OI-change reads that used to share one unlabelled name (the
MANTRA scout-sweep confusion).

SPEC-89: the `onchain` layer passes through `top_holder_distribution`/`distribution_checked`/
`signal_caveat` from `build_onchain`, so a one-call read can't show on-chain `QUIET` while a
top holder dumps — when a top holder is DISTRIBUTING the headline reads `on-chain DISTRIBUTING`
(and a `⚠ top-holder DISTRIBUTING (verify_wallet)` bit is appended if the signal wasn't already
distribution-flavored).

SPEC-98: the `onchain` layer also passes through `distribution_freshness`
(`FRESH|FROZEN|ROTATED` — the VELVET false-pause read); a ROTATED or FROZEN line joins the
headline (`distribution: ROTATED — top holder frozen … but N fresh wallets → CEX …`). See
docs/onchain.md §SPEC-98.

SPEC-136: `state.aster_listed` is `true|false|null` — the fillability gate (can the user
FILL on Aster, the sole execution venue), distinct from the cross-venue OI/funding SIGNAL
the rest of the brief reads (§0.6.3b: never conflate the two). `false` appends
`| SIGNAL-ONLY (no Aster market)` to `state.reason`. See docs/classify.md §Execution-venue.

## Gotchas
- Layers are degrade-explicit: a failed/slow layer returns {available:false, reason},
  never a silent null. Layers run concurrently (≈ slowest layer, budget 90s each).
- §0.5: brief is a READ — `state.verdict` stays authoritative; fresh data in the other
  layers does not reframe a CONFIRMS.
- SPEC 45: `state.alerts` carries the unconsumed inbox events for the ticker.
- SPEC-71: live funding is resolved ONCE per brief (`regime_flip.live_perp` — cross-venue,
  floor-demoting) and BOTH the state leg and the perp leg read that same resolution, so one
  payload can never assert two different live funding verdicts (the BILL incident: the perp
  leg headlined a venue's ±0.005 floor placeholder while the state leg held the real,
  trigger-qualifying Binance rate). `perp.funding_venue` is the venue actually used (never
  null); `perp.funding_by_venue` surfaces every venue's `{funding_4h, funding_pi,
  interval_min, is_floor}` — bitget is fetched display-only when the lazy resolver didn't
  consult it (`surfaced_only:true`). A lone floored venue ⇒ `floor_suspect:true` + the
  headline carries `⚠floor-suspect`: a floor print is a data-failure signal, not a flat
  verdict (§3).
- SPEC-84: for the ~2/26 names Hyperliquid lists (e.g. TNSR/CHIP), `funding_by_venue`
  also carries a `hyperliquid` entry (`surfaced_only:true`) with the HL hourly funding
  normalized to /4h (`interval_min:60`) plus `oi/vol_m/mark/oracle/premium` — HL's
  transparent on-chain funding + oracle mark as a cross-check against the operator-suspect
  CEX books (§3/§7). Display-only — HL feeds NO verdict. For the 24/26 HL doesn't carry,
  it is omitted entirely (no key), and on any HL fetch error it degrades to omission —
  the brief never blocks. See [hyperliquid.md](hyperliquid.md).
- SPEC 55: `onchain` is passed through verbatim from `build_onchain` — brief RE-DERIVES
  NOTHING, so its `signal`/`bias`/`score` match a direct `onchain` call of the same
  minute. (They used to drift: every read advanced the nonce baseline, so a direct
  `onchain` then a `brief` seconds later diffed against different baselines — PLAY went
  `2-fired → DORMANT` between reads. Reads no longer advance the baseline; only the
  surveillance sweep does. See [onchain.md](onchain.md).) A cex/operational/`unclassified`
  fire shows in `onchain.newly_fired` but never drives the topline.
- SPEC-83: `defended_fade` surfaces the wall-fade tell — it records the top-of-book
  defended wall (`state/wall_history.json`, gitignored) each brief and reports
  `defended_fade.ask.wall_growing` (the wall NOTIONAL thickening into the ceiling while
  price tests the level = operator capping while distributing — the size-able fade entry,
  stop above the empty liquidity beyond it). Needs ≥2 briefs at the level to light up. The
  headline appends `ask-wall GROWING …` when it fires; `ask.geometry` carries the
  entry=level / stop=beyond-wall poke (see [setup_score.md](setup_score.md) for the scorer +
  the §9 hypothesis-tier / scout-size caveat).
- **SPEC-159:** `venue_breadth` — PerpFinder's funding-rates matrix (SPEC-151) as the
  wide-and-shallow complement to `venue_map` (SPEC-129, still the deep 9-venue read).
  Fetched LAST, best-effort, its own ≤10s budget — never blocks or slows the wired reads
  above. Shape: `{available, source:"perpfinder", normalized_rates:true,
  venues:[{venue, oi, oi_share_pct, price, funding_pi_4h, rate_raw_pi, interval_min}]
  (sorted OI desc, null-oi last), total_oi_usd, n_venues, size_book, wired_oi_share_pct,
  desk_blind_share_pct}`.
  **SPEC-187 fix:** the per-venue rate used to be surfaced under the misleading key
  `rate1h` — its value was already the 4h-normalized print, not a raw hourly one, AND
  (separately) `perpfinder.py`'s own conversion was missing a fraction→percent step,
  making every print ~100x too small (live: brief showed Bybit `rate1h -0.008838` against
  the funding layer's own independently-computed `-1.026%/4h` for the same venue/moment —
  fixed at the source in `perpfinder.py::_build_funding`, which now runs the SAME
  raw→percent→`RF.to_4h` transform `regime_flip.py` uses everywhere else). Three explicit
  keys now: `funding_pi_4h` (the SPEC-112-normalized %/4h print), `rate_raw_pi` (the
  untouched raw fraction), `interval_min` (always 60 here — PerpFinder's `rate1h` is
  documented as already hour-normalized).
  **SPEC-174 #4 fix:** `oi` on this endpoint is already USD notional (live-verified
  2026-08-28 — BTC/Binance `oi=$8.396B`; as token units that's 8.4B BTC, impossible against
  a ~19.8M supply). `total_oi_usd` is the straight sum of `oi` — it used to multiply by
  `price` again, corrupting every total (H: total $1.13M vs its own Bybit row $8.29M;
  MANTRA total $12K vs `venue_map`'s $5.2M).
  `available:false` (with `reason`) on perpfinder error/timeout/shape_drift, and
  `reason:"not_in_matrix"` when the ticker isn't in PerpFinder's universe — the rest of the
  brief is untouched either way. **Doctrine (unchanged, SPEC-151):** `funding_pi_4h`/
  `rate_raw_pi` are display-only — they can never feed a verdict, the §5 short veto, floor
  detection, or `funding_leg`; that's why the block is fetched outside the shared
  `live_perp` resolution `_perp_layer` reads, and why `perp.*`/`state.*` never contain
  either key. `wired_oi_share_pct` is the fraction of total OI sitting on the desk's wired venues
  (bybit/binance/bitget/aster/hyperliquid, case-insensitive); `desk_blind_share_pct` is the
  rest. When `desk_blind_share_pct > 30` the headline appends the automated §0.6.3b prompt:
  `⚠ 62% of OI off-desk (size book: Hyperliquid)` (the CASHCAT 2026-08-25 case: brief saw
  Bybit+HL only while the matrix showed 9 venues and HL as the real size book at $30.4M OI
  vs Bybit's $14.1M). See [perpfinder.md](perpfinder.md).
- **SPEC-169:** `risk_card` — the §9 risk-card line (tier/max-lev/risk-cap/exit-absorbable/
  liq-distance), built off the committed thesis geometry (`state.thesis`) when present. Its
  live legs (venue equity, a maxsize exit-book walk) are the one real network cost in a
  `brief` call. See [risk_card.md](risk_card.md) if present, else `capabilities/risk_card.py`.
- **SPEC-188:** `venue_bars` — the all-venue 1h OHLC sweep (`capabilities/venue_bars.py`),
  printed UNCONDITIONALLY (never gated on a thesis) — the deterministic engine layer behind
  CLAUDE.md §3's all-venue structure rule (repeated user directive 2026-09-01/09-02: hand
  discipline read one/two venues by hand and got the structure call wrong both times). Human
  render prints the compact `tape (1h, N venues) …` block, a `dominant tape … · size book
  (OI) … · execution aster · high-spread max …` summary line, then one **tape-agreement**
  line per committed thesis level (`entry_zone`/`stop`/each `tp`/each `watch_level`) any
  venue crossed in the fetched bars: `0.098 watch_level: crossed 13/14 (closed beyond 1/14)
  · Binance ✓ · Aster ✓ · Kraken ✓ · Coinbase ✗`. Unavailable prints `tape: UNAVAILABLE
  (<reason>)` — never silent. See [venue_bars.md](venue_bars.md).
- **SPEC-172 — per-component budgets, never a silent full-timeout.** Every sub-read carries
  its own wall-clock budget: the main-loop layers (state/perp/books/onchain/liqs/phase) each
  get `LAYER_BUDGET` (90s); the layers that run AFTER them, concurrently among themselves
  (defended_fade/unlocks/venue_breadth/risk_card/venue_bars — risk_card and venue_bars are
  the real network legs there), each get their own smaller budget (`risk_card` 20s,
  `venue_bars` 20s, `venue_breadth` 10s, the rest 15s as a safety net over otherwise-offline
  reads). A slow/hung component degrades to
  `{available:false, reason:"<name> exceeded <n>s budget"}` and is named in the top-level
  `timed_out` list — every OTHER component that completed still returns normally (never a
  chopped-up empty error). `meta.layer_ms` carries every component's wall-clock ms, timed
  out or not. Root cause of the pre-fix 170s+ GALA/SKR timeouts: each layer LOOKED bounded
  (`.result(timeout=...)`), but the `with ThreadPoolExecutor(...) as ex:` pattern's
  `__exit__` calls `ex.shutdown(wait=True)` — blocking until every submitted thread
  finishes, including ones already reported as timed-out inside the loop. A single stalled
  network call silently re-imposed its own wall-clock on the WHOLE brief regardless of any
  per-layer timeout passed to `.result()`. Fixed via `_bounded()` (no `with`,
  `shutdown(wait=False)` — an abandoned thread is left to finish on its own time, unread).
