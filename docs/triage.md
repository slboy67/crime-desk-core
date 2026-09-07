# triage — watchlist crime-triage board

The 60–90 min refresh board (CLAUDE.md §"How to operate"). Per token: live
funding/OI/L/S signals, a direction read, and a live/watch/dust tier. Native port
of the parts-bin `triage.py` (413-line noisy CLI) — same flag logic, compute now
separated from render.

## Two paths, one compute

- `python3 orchestrator.py triage '{}'` — the machine path. Clean `{ok,data,meta}`;
  `data` is a `{board, meta}` envelope — `data.board` is the JSON board (the per-token
  contract below), `data.meta.agents` is launchd watcher health (SPEC 64). **This is what
  the Designer calls.**
- `python3 capabilities/triage.py` — the human path: colored one-glance cards
  (unchanged from the original). `--json` flag = the machine path bare.

`build_board(tickers=None)` is the pure-compute function (no printing);
`render_human(board)` is the card view. Both read the same contract dicts.

## JSON contract (per token)

```json
{ "ticker": "LAB", "category": "A",
  "price": 24.06, "chg24": 57.2, "range_pct": 97, "vol_m": 1675.8,
  "funding_pi": -1.0688, "funding_venue": "binance", "funding_range": [-1.0688, -0.10],
  "funding_suspect": false, "funding_raw_pi": -1.0688, "funding_interval_min": 240,
  "oi_chg_pct": -22, "ls_ratio": 0.35,
  "signals": ["OI-flush", "deep-neg-fund", "..."],
  "direction": "short", "tier": "live",
  "memo": "<the watchlist thesis/state string>" }
```

- `funding_pi` — **%/4h-equivalent** (SPEC-112: normalized `* 240/interval_min`), **never
  annualized**. **SPEC-108: cross-venue** — the **most-extreme non-floor** live print across
  bybit/binance/bitget/aster (max by `abs(value)` after dropping floor-sentinel prints and
  normalizing to 4h; a floor is a data failure, not a datum — CLAUDE.md §3). A single-venue
  Bybit-preferred/Binance-fallback print masked real multi-venue deep-neg signatures (SLX
  2026-07-07: board printed −0.0249 while the real cross-venue read was −0.50). A raw
  1h-interval print sitting unnormalized in the same column as a 4h print was a second,
  related bug (SLX again: aster −0.0852/1h read next to LAB's −0.6405/4h, 4x different units) —
  every venue is normalized to 4h **before** the most-extreme comparison and before render.
  `funding_venue` names which venue supplied it; `funding_range` is `[min, max]`, also
  4h-normalized, across the non-floor venues. `funding_raw_pi` / `funding_interval_min` carry
  the un-normalized print + its interval for the winning venue (same convention as `brief`).
  If every venue is a floor print, `funding_pi` is `null` and `funding_suspect: true` (never a
  confident flat). **SPEC 18: this is the LIVE PREDICTED rate** (Bybit `tickers.fundingRate`,
  Binance/Bitget/Aster `premiumIndex`/`current-fund-rate` equivalents), NOT the last settled
  print — the settled head lags and masked the §5 short-veto (EDEN settled +0.005% vs live
  −1.72%). Settled `fundingRate` history is kept only as `binance.fund_hist` (the series). If
  the live endpoint is unavailable, the value degrades to the settled head (never crashes the
  row). The `fund-divergence` signal (bybit vs binance only) is unchanged by this.
- `range_pct` — position in the 24h hi/lo range (0–100).
- `oi_chg_pct` — Binance OI 24h change %. `oi_chg_pct_24h` (SPEC-174 #6) is the identical
  value, window labelled in the key — additive alias, `oi_chg_pct` unchanged for existing
  consumers. `ls_ratio` — Binance top-trader L/S (1h).
- `signals` — plain-string chips (the same flags the human card shows); no ANSI.
- `direction` ∈ `short|long|neutral`; `tier` ∈ `live|watch|dust`
  (`dust` if vol <$10M, else `live`≥2 signals / `watch`=1 / `dust`=0).
- Any field can be `null` when a venue has no data for that token (dust tokens often
  lack funding). `vol_m` defaults to `0.0`. A per-token pull error adds an `error` key
  and degrades that one row to dust — never the whole board.
- Sorted: signals desc, then `vol_m` desc (same as the human ranking).

## Agent health — `meta.agents` (SPEC 64)

The nonce-surveil launchd agent once sat unloaded for 9 days unnoticed. Every `--json`
run now reports the standing watchers, one glance per scan:
```json
"meta": {"agents": {"coder_dispatch": "loaded", "nonce_surveil": "MISSING",
                    "board_tick": "loaded"}}
```
Each state ∈ `loaded | MISSING | unknown`, read via `launchctl list <label>` (exit 0 =
loaded). `unknown` is returned when launchctl is unavailable (non-macOS / no binary) — a
missing probe is NEVER reported as a false `MISSING`. The human card view prints a red
`🚨 AGENTS:` banner for any non-`loaded` watcher. Labels: `com.crimedesk.coder-dispatch`,
`com.crimedesk.nonce-surveil`, `com.crimedesk.board-tick`.

## Example

```
$ python3 orchestrator.py triage '{"tickers":"LAB BILL"}'
{"ok":true,"data":[
  {"ticker":"LAB","category":"A","price":24.06,"chg24":57.2,"range_pct":97,
   "vol_m":1675.8,"funding_pi":-1.0688,"oi_chg_pct":-22,"ls_ratio":0.35,
   "signals":["OI-flush","deep-neg-fund","..."],"direction":"short","tier":"live",
   "memo":"[REGIME_FLIP ...] short VETOED ..."},
  {"ticker":"BILL","category":"A","price":0.0843,"chg24":-8.2,"range_pct":35,
   "vol_m":82.0,"funding_pi":0.005,"oi_chg_pct":-2,"ls_ratio":1.32,
   "signals":["L/S1.32 retail-long"],"direction":"short","tier":"watch","memo":"..."}],
 "meta":{"capability":"triage","args":{"tickers":"LAB BILL"},"ms":...}}
```

`{tickers}` is optional (space-separated subset); omit for the full watchlist.
Tickers not on the watchlist are pulled ad-hoc (`category:"?"`, `memo:"ad-hoc scan"`).

## Gotchas

- `--json` writes JSON to stdout ONLY (diagnostics → stderr). The bare command is
  the human card view and is intentionally non-JSON.
- `--render compact` (SPEC-193, JSON-only): drops `leverage_state` (mostly UNKNOWN
  placeholders, not a decision key) and any None-valued key per row. `signals`/`memo`
  (the decision text) pass through unchanged — triage rows carry no raw-bulk fields
  to begin with, so compact is a modest, not dramatic, reduction.
- Live venue pulls — a slow/blocked venue degrades a row, not the run (120s cap).
- Signal/flag strings are the parts-bin originals verbatim (`fund-divergence …`,
  `L/S0.35 retail-short`); they were deliberately NOT rewritten.

## Tests

`tests/test_triage_json.py` (stdlib `unittest`): clean-JSON/no-ANSI, board incl. LAB,
per-row contract+types+enums, sort order, `meta.agents` health, and that the bare view
stays non-JSON. `tests/test_deadman.py` covers the agent-health probe→state mapping offline.
```
python3 tests/test_triage_json.py
```

## Template for Phase 1b–1e

`regime_check`, `price_structure`, `liq_magnets`, `pull5` follow this exact shape:
extract a `build_*()` pure-data fn → keep human render as default → add `--json` →
register (`filter:null`) → TDD + doc. Only the `out` contract keys differ.
