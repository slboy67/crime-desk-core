# orchestrator — the desk switchboard

The single entry point. The Designer (main agent) calls capabilities by JSON
contract and reads only the distilled envelope — never a script's raw stdout
(ARCHITECTURE.md §1–§3).

## CLI contract

```
python3 orchestrator.py <capability> '<json-args>'
```

- `<capability>` — a key in `capabilities.json`.
- `<json-args>` — a JSON **object** (`{}` for none). Placeholders `{key}` in the
  capability's `invoke` are filled from these args; absent optional keys drop out.

stdout is **always exactly one JSON object** — nothing before or after it.

## The envelope

Success:
```json
{ "ok": true,
  "data": <the capability's out-contract>,
  "meta": { "capability": "classify", "args": {...}, "ms": 675 } }
```

Failure:
```json
{ "ok": false, "error": "unknown capability: foo", "known": ["analyse","classify",...] }
```

Failure modes the orchestrator guarantees as `ok:false` (never a crash, never prose):
unknown capability · malformed/non-object args · capability timeout · output that
won't parse (includes `raw_head` + `stderr_head` for debugging).

## How a call flows

1. Look up the capability in `capabilities.json`.
2. `fill(invoke, args)` → a shell command.
3. Run it (`cwd=repo root`, `timeout` from the entry, default 180s).
4. If the entry has a `filter`, pass stdout through `filters/<filter>.py:run(stdout, args)`;
   otherwise `json.loads(stdout)` directly (the script is already JSON-clean).
5. Wrap the result in the envelope.

`filter: null` = the script emits clean JSON natively. `filter: "<name>"` = a
legacy script prints a human report and `filters/<name>.py` distills it (the
bridge; end-state folds a native `--json` into the script and drops the filter).

> Note: the registry is `capabilities.json`, not `capabilities.yaml` as some
> tickets say — there's no `pyyaml` on the host. Same schema, JSON syntax.

## The four capabilities (call + response)

### tape — candle-level OI/price/taker/liq microstructure classifier (native ✅, SPEC 31)
Operationalizes the operator-seat OI lens (`memory/feedback_read_oi_price_from_operator_seat.md`):
four forces move OI at once, so the net print is technically near-meaningless during a pump.
`tape` labels each 1m candle into the four-force vocabulary — `longs_opening` (OI↑ px↑),
`shorts_opening` (OI↑ px↓/**flat** = recruitment), `longs_closing` (OI↓ px↓),
`shorts_closing` (OI↓ px↑; `forced:true` when the liq feed confirms a short liquidation) —
and flags the operator staged-play **sequences**: `fake_breakdown_wick` (sub-support wick
with shorts recruited then recovered within K bars — the bait), `chain_squeeze` (rising tape
recruiting shorts then a stop-run UP through a round number), `tail_of_liquidation`
(≥3 consecutive longs_closing), `downtrend_slam` + `bounce_cover`. **SPEC 38 — each
`tail_of_liquidation` carries a `context`** keyed on WHO is transacting, not just where price
sits: `retail_capitulation` (the run is `longs_closing` with **OI collapsing** over it — a real
unwind → where a short banks, **regardless of range**) vs `operator_profit_take` (longs_closing
tail with ~**flat OI** alongside `shorts_opening`/`chain_squeeze` nearby = the operator washing +
recruiting shorts → do NOT short the shakeout) vs `ambiguous`. PRIMARY discriminators
`dominant_force` + `oi_shed_pct_over_run` + `chain_squeeze_nearby`; the location corroborators
(`range_pos_at_event`, `drawdown_from_local_high_pct`, `velocity_pct_per_bar`) only tie-break
(SPEC 35's location-only read got a mid-range OI-collapse and a near-high flat-OI wash backwards). **Read-only intel — NO
auto-verdict** (does not replace the §5 funding-flip + OI-build short gate; it FEEDS the
Designer's operator-lens between triggers). The candle classification is a deterministic
decision-tree; the "what is the operator doing" judgment stays with the Designer (invariant 2).
```
python3 orchestrator.py tape '{"ticker":"SKYAI","window":60}'
python3 orchestrator.py tape '{"ticker":"SKYAI","window":90,"level":0.1836}'   # level = support for fake_breakdown_wick
→ {"ok":true,"data":{"ticker":"SKYAI","venue":"binance","oi_interval":"5m","window_min":60,
     "bars":[{"ts":...,"price":0.215,"d_price_pct":0.13,"d_oi":-85600,"oi_force":"shorts_closing",
              "forced":false,"taker_imb":0.41,"liq_long_usd":0,"liq_short_usd":0}, ...],
     "patterns":[{"type":"chain_squeeze","bar_range":[13,34],
                  "detail":{"round_number":0.21,"from":0.205,"to":0.214,"shorts_opening_bars":4}}],
     "round_numbers_near":[0.206,0.208,0.21,...],"liq_available":false},"meta":{...}}
```
- **Granularity floor declared, never pretended:** price/taker are 1m (taker-buy vol is in the
  kline → no raw-trade dump); Binance OI floor is **5m** (`oi_interval:"5m"`, each 1m bar
  inherits its 5m bucket's ΔOI). Net-ambiguous bars are `oi_force:"mixed"` with a `dominant`
  component.
- **Liquidations:** Binance public `forceOrders` is auth-gated → `liq_available:false`
  degrades-explicit (the forced-vs-voluntary short-exit split is then undetermined; the split
  logic runs whenever a liq feed IS supplied).

### brief — one-call full-stack token read (native ✅, SPEC 30)
**The session-default on any ticker.** A bare ticker drop should return the WHOLE picture
in one call; `brief` makes completeness mechanical instead of a thing the Designer has to
remember (the recurring partial-read failure mode: a single-venue funding verdict, perp
without the book, the book without on-chain — the SKYAI Bitget-exit-book blind spot,
`memory/feedback_skyai_exit_liquidity_is_bitget.md`). It **re-derives nothing** — it
composes the already-MERGED capabilities **concurrently**: `state←classify`,
`perp←analyse`, `books←depth` (**BOTH** Bitget+Binance), `onchain←onchain`. Each section
is **degrade-explicit** (`available:false`+reason on a failed/slow layer — never a silent
null, never a crash that loses the other layers); each layer is budgeted 90s (SPEC-1b) and
they run concurrently so **latency ≈ analyse** (the slow layer), not the sum. `venue`
omitted = both venue books. It is a **READ, not a commit** (§0.5): it surfaces all the data
but `state.verdict` stays authoritative — a CONFIRMS doesn't become a reframe.
```
python3 orchestrator.py brief '{"ticker":"SKYAI"}'
→ {"ok":true,"data":{
     "ticker":"SKYAI",
     "state":{"available":true,"verdict":"CONFIRMS","thesis_present":true,"direction":"SHORT",
              "thesis":{"direction":"SHORT","entry_zone":[0.165,0.168],"stop":0.175,"tp":[...],
                        "triggers":[...],"invalidation":{...},"time_stop":null}},
     "perp":{"available":true,"verdict":"WATCH","direction":"SHORT-loading-WATCH","tier":"watch",
             "funding_4h":0.005,"funding_venue":"binance","oi_chg_pct":-3,"near_ath":false,
             "cvd_verdict":"distribution","price":0.1834},
     "books":{"available":true,"venues":{
       "bitget":{"available":true,"mid":0.1865,"bid_shelf_below":{"price":0.1858,...},
                 "ask_wall_above":{...},"truncated":true,"deepest_level_seen":{...}},
       "binance":{"available":true,"mid":0.1864,"bid_shelf_below":{"price":0.1857,...},...}}},
     "onchain":{"available":true,"signal":"LOADING","bias":"LEAN BEARISH (distribution loading)",
                "score":-10,"newly_fired":[],"staging_vs_execution":null,
                "concentration":{"available":true,"top1_pct":38.4,"top10_pct":63.4,"holder_count":55752}},
     "headline":"CONFIRMS — SHORT-loading-WATCH / funding +0.005%/4h — on-chain LOADING — Bitget bid 0.1858"
   },"meta":{...}}

python3 orchestrator.py brief '{"ticker":"SKYAI","venue":"bitget"}'   # books = bitget only
```
- `onchain.newly_fired` = the **SPEC-24 token-out-recency-gated** confirmed fires (a high-nonce
  CEX-MM EOA whose tracked-token-out is stale is gated out — no false ESCALATION).
  `staging_vs_execution` = **SPEC 28** (CEX/DEX-routed = `execution`, internal sink = `staging`).
- Pure composition over `classify`/`analyse`/`depth`/`onchain` — no new data sources. If a
  composed capability degrades (e.g. `analyse` perp subprocess down → `perp.verdict:"PASS"`),
  `brief` surfaces that faithfully rather than hiding it.
- **`render:"compact"` (SPEC-193):** `{"ticker":"OP","render":"compact"}` returns
  `compact_brief()`'s reduced envelope — the SAME decision keys (`state.verdict`,
  `state.reason` UNTRUNCATED, `state.thesis` byte-for-byte, `headline`, `perp.oic`,
  `risk_card.line`, `venue_breadth` size_book/blind%, one-line funding dispersion,
  one line per venue book, `venue_bars` dispersion summary + tape-agreement lines) with
  the bulk dropped (raw depth levels, raw OHLC bar arrays, per-venue kline payloads,
  `meta.layer_ms`). Typically ~8-17x smaller than the default `render:"full"` (unchanged,
  still the default) — a name with an unusually verbose multi-leg watch thesis can still
  land slightly over the ~3KB rule of thumb (the thesis's own text is preserved
  byte-for-byte, never trimmed). Read `render:"compact"` for anything the §0.5/§0.6 read
  consumes; drop to `render:"full"`/`--json` only when you need a field compact omits.

### classify — the daily board (native ✅)
CONFIRMS/TRIGGERS/BREAKS per token vs its committed thesis. Omit `ticker` for the board.
`render:"compact"` (SPEC-193) drops `leverage_state` and the raw per-venue
`crossed`/`closed_beyond` name arrays inside `price_leg`/`watch_leg`'s `venue_agreement`
(kept as `{label,n_crossed,n_total}`) and prints with no pretty-print indent — `reason`
is never truncated. On a board whose rows carry long tape-annotated reasons (this repo's
current watchlist, post-SPEC-188) the compact board can still run several KB for ~20
rows — the untruncated-reason guarantee costs bytes the row count multiplies; it is
still a large reduction vs the indented full render for the same rows.
```
python3 orchestrator.py classify '{"ticker":"LAB"}'
→ {"ok":true,"data":{"ticker":"LAB","verdict":"CONFIRMS",
     "reason":"rf=CONFIRM: live funding neg (-1.750%/4h (raw -0.437%/1h)) consistent with memo",
     "thesis_present":true},"meta":{...}}

python3 orchestrator.py classify '{}'        # → data is a list of ~20 such dicts
```
`price_leg` and `watch_leg` each carry `venue_agreement` (SPEC-188 §3, null unless the leg
fired off a real level crossing) — `{label:FULL|PARTIAL|SINGLE, n_crossed, n_total, crossed,
closed_beyond, size_book_venue}`. FULL requires Binance AND the OI size-book venue
(`venue_map.top_oi_venue`) AND Aster (execution) to all have crossed the breached level;
SINGLE is exactly one venue; PARTIAL is anything in between. Pure annotation (§0.5) — the
verdict is unchanged; the label is also appended to `reason` as `(tape: PARTIAL 6/14)` so it
flows into any downstream alert payload that reads `reason`. Any failure of the cross-venue
sweep degrades `venue_agreement` to `null`, never touches the verdict. See
[venue_bars.md](venue_bars.md).

Each row also carries `watch_leg` (SPEC 77 trip-wire) and `thesis_drift` (SPEC-91): null
unless live price has run past the thesis's WHOLE committed structure, else
`{stale, live_price, nearest_anchor, nearest_dist_pct, furthest_anchor, all_tps_printed,
side}`. A stale thesis's `reason` LEADS with a `⏳ STALE-THESIS [DRIFT]: … RE-ANCHOR` prompt
(before the echoed commit-time note, tagged `[commit-time, stale]`) and fires ONE HIGH inbox
event per `(ticker, committed_ts)` episode — re-anchoring (bumping `committed_ts`) resets it.
Drift is a READ: it never changes the verdict (§0.5). Threshold `classify.DRIFT_PCT` (default
8%): stale when all anchors are one side of live and the nearest is ≥ that % away, OR every TP
printed and live ran ≥ that % beyond the furthest TP.

Each row also carries `retire_flag` (SPEC-140, null when healthy) — a mechanical staleness
tag so a crowded board can't hide a dead row (the LAB −16%-past-trigger / BEAT −75% misses).
Fired by the first of: `time_stop_elapsed` (the thesis's `time_stop` is in the past) ·
`zone_blown_unfilled` (the entry zone was never traded into and live price already ran past
it — reuses the price-leg's own `entered_zone`/`stop_breached` read, no new fetch) ·
`stale_unresolvable` (a committed `committed_ts` that can't be parsed — SPEC-113 legacy row,
undatable by definition) · `stale_14d` (≥14 days since commit with zero TRIGGERS/BREAKS
log events for the row). Advisory only — it never touches `verdict`/`reason`; retirement
itself stays a Designer commit (§0.5). The whole-board response (`classify '{}'`) additionally
carries `meta.retire_flagged` (the flagged tickers) and `meta.retire_flagged_count`, so the
session-open read is one line.

### regime_flip — live-vs-stored funding drift (native ✅)
REGIME_FLIP / ZONE_BLOWN / CONFIRM / DUST / FUNDING_UNAVAILABLE. Funding is read
**CROSS-VENUE** (Binance + Bybit, SPEC 13) and surfaced **%/4h-normalized** (SPEC 11):
`funding_4h` = the more-vetoing non-floor venue (so one venue's floor can't mask
another's deep-neg); raw `funding_pi` + `interval_min` + per-venue `venues{}` secondary;
`primary_venue` = dominant by OI; `funding_split:true` when venues straddle the −0.30%/4h
veto line. Floor on **all** covered venues → a genuine ~0% flat (`all_floor`), not
UNAVAILABLE; UNAVAILABLE only when no venue returns funding. Veto/threshold logic
compares on the cross-venue %/4h value.
```
python3 orchestrator.py regime_flip '{"ticker":"LAB"}'
→ {"ok":true,"data":{"ticker":"LAB","tag":"CONFIRM",
     "note":"live funding neg (-1.750%/4h (raw -0.437%/1h)) consistent with memo",
     "funding_4h":-1.75,"funding_pi":-0.4374,"interval_min":60,"funding_stale":false,
     "price":26.36,"chg24":79.6},"meta":{...}}
```
Verified Phase 0: clean `--json`. Single ticker → dict; bare/multi → list.

### regime_check — cross-venue funding/OI + L/S positioning (native ✅)
Cross-venue funding (live predicted, SPEC 18) + OI z-score, plus the **L/S positioning
leg** (SPEC 23): `ls` block, **Binance-authoritative** (the only venue exposing an L/S
ratio for thin crime-coins). Cohorts `top_position` (position-weighted, most decision-
relevant), `top_account`, `global` — each with latest `ratio`, `long_pct`/`short_pct`,
and `trend_pct` over a 12×1h window. `signals.short_fire` fires when a top-trader cohort
L/S drops ≥15% (§6 trapped-longs capitulating); `signals.crowdedness` = long-crowded
(short-side uncrowded → §7 squeeze fuel) / short-crowded (with the crowd → late/bait) /
balanced. Bybit best-effort (empty list for thin names → `unavailable`); Bitget/Aster
always `unavailable` — **never a fabricated ratio** (the SPEC 10/18 floor-sentinel lesson).
Importable as `regime_check.build_ls(sym)` so triage/analyse can read positioning without a hand-curl.
```
python3 orchestrator.py regime_check '{"ticker":"SKYAI"}'
→ {...,"ls":{"source":"binance","cohorts":{"top_position":{"ratio":2.385,"long_pct":70.5,
     "short_pct":29.6,"trend_pct":0.44,...},...},"venues":{"bybit":{"available":false,...},
     "bitget":{"available":false,...},"aster":{"available":false,...}},
     "signals":{"short_fire":false,"worst_top_trend_pct":-11.79,"crowdedness":"long-crowded"}}}
```

### oi_sides — per-side OI + wash fingerprint (delegated → _oldrepo)
```
python3 orchestrator.py oi_sides '{"ticker":"LAB"}'
→ {"ok":true,"data":{"ticker":"LAB","wash_score":...,"verdict":"..."},"meta":{...}}
```
Native port is a Phase-1 ticket; works today (no config dependency).

### analyse — combined perp+onchain verdict (native; full doc: `docs/analyse.md`)
Native convergence engine: pulls perp/on-chain/CVD/structure/OI/whales concurrently,
90s on-chain budget (SPEC 1b), §4 neg-funding LONG confluence gate (SPEC 4).
```
python3 orchestrator.py analyse '{"ticker":"LAB"}'
→ {"ok":true,"data":{"ticker":"LAB","verdict":"WATCH",
     "direction":"WATCH (neg-funding §4 incomplete)","tier":"WATCH",
     "reason":"...","onchain":"UNAVAILABLE","onchain_ms":90014,...},"meta":{...}}
```
- `{ticker,verdict,tier,direction}` preserved in every path (incl. on-chain unavailable).
- `onchain` ∈ `OK|DEGRADED|UNAVAILABLE|UNMAPPED`; `reason` names the deciding gate.
- A neg-funding LONG is only emitted with the full §4 confluence — else `WATCH`. See `docs/analyse.md`.

## How to add a capability

1. **Registry entry** in `capabilities.json`:
   `{invoke, filter, status, desc, contract{in,out}, test, doc}`
   (+ optional `timeout`). `{placeholders}` in `invoke` are filled from args.
2. **Filter** (only if the script is noisy): `filters/<name>.py` exposing
   `run(stdout, args) -> dict`; set `"filter":"<name>"`. If the script emits
   clean JSON, use `"filter": null`.
3. **Test row**: assert the out-contract (shape, types, ranges). Live-test fast
   native scripts; unit-test the filter against a captured sample for slow/network ones.
4. **Doc**: add the call+response here (or a `docs/<cap>.md` for big capabilities).

## Tests

`tests/test_orchestrator.py` (stdlib `unittest`, no pytest needed):
```
python3 tests/test_orchestrator.py
```
Covers: envelope shape, no-leak, error paths, `classify`/`regime_flip` live,
the `analyse` filter against a captured banner, and registry integrity
(no `unverified` statuses; every cap has an existing test + doc).
