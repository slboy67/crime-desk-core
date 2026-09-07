# backtest_accumulation — accumulation-edge backtest harness (SPEC-100, the §9 GO/NO-GO gate)

## Purpose
`GOAL-close-gaps.md` G1: the accumulation-radar long edge (SPEC-76, the SIREN chip-control case,
the `0xaDFffc33` accumulator pattern) is **n=1 and unbacktested** — §9's hard gate (n≥10, hit%>50,
positive edge) blocks trading it at size. This harness measures whether verified accumulation →
perp construction → markup leads price with tradeable timing, or whether cluster names cascade
together on distribution, and prints the literal GO/NO-GO. The desk does NOT size the edge until
this prints GO.

## Contract
```
backtest_accumulation '{}'                                    # manual fixtures only
backtest_accumulation '{"manual_path":"config/other.json"}'   # override the fixtures file
```
Out: `{events:[{token, wallet?, contract?, chain?, cluster?, signal_ts, signal_date, source,
outcome:{entry_ref, return_3d_pct, return_7d_pct, return_14d_pct, return_30d_pct, mfe_pct,
mae_pct, time_to_markup_days, entry_existed}|None}], aggregate:{n, n_hit_evaluable, hit_pct,
avg_edge_pct, lead_time_days:{n,median_days,min_days,max_days}, hit_definition, verdict,
verdict_line}, co_cascade:{cluster: {n, corr}}, report_path}`.

## Two event feeds
1. **Manual** (`load_manual_events`) — `config/backtest_accumulation_events.json`, desk-verified
   cases sourced from memory (SIREN, XPIN 10-wallet set, the `0xaDFffc33` COLLECT/SKYAI cluster).
   Runs even before wide enumeration is affordable. `signal_date` in this file is a best-effort
   desk reconstruction (`date_confidence: "approximate-desk-memory"`), not a precise on-chain date.
2. **Historical replay** (`source_historical_events` → `replay_accumulation_signal`) — given a
   `{wallet, contract, token, chain}` candidate, pulls the wallet's transfer history via the
   SPEC-97 provider seam (`onchain.token_transfers`) and walks it day-by-day, reusing
   `accumulation_radar.diff_holdings` (the SAME NEW/GROWN criteria production uses against a
   7-day rolling baseline) to find the first date the SPEC-76 radar WOULD have fired. This is the
   precise measurement once wallet+contract pairs are enumerable; it does NOT reimplement the
   threshold — it calls the production function.

## Per-event outcome metrics (`compute_outcome`)
On 1h forward bars from the signal (`bars[0]` = entry):
- **Forward return** at +3d/+7d/+14d/+30d (`return_Nd_pct`), the close at bar `N*24`.
- **MFE/MAE** (`mfe_pct`/`mae_pct`) — max favorable/adverse excursion over the whole window,
  relative to the entry close (long-only, matching the accumulation-radar's long-side edge).
- **time_to_markup_days** — first bar where close ≥ entry × 1.20 (the "first +20% leg"); `None`
  if the window never gets there.
- **entry_existed** (`find_base_trigger`) — a §6-style base+trigger shape: price must pull back
  ≥5% from a running local high (the base) and then reclaim that high (the trigger). This is a
  simplified proxy for "was a machine-watchable entry live after the signal", not the full
  magnet/sweep engine.

## Hit definition + aggregate (`is_hit`, `aggregate`)
`hit = +7d return > 0 AND MFE ≥ 2× MAE` (`HIT_DEFINITION`, always echoed in the output — the
number is meaningless without the definition next to it). Aggregate prints the literal §9 gate:
- **n < 10** → always `HYPOTHESIS-TIER` (insufficient n), regardless of hit%/edge on the small
  sample — the point of the gate is exactly to refuse a verdict on too few events.
- **n ≥ 10 AND hit% > 50 AND avg 7d edge > 0** → `GO`.
- **n ≥ 10** and any condition fails → `NO-GO`, naming the failing condition(s) inline.

## Cluster cascade (`co_cascade_correlation`)
The G1 "do cluster names cascade together on distribution" question: events sharing a `cluster`
tag get their forward-return series (the 4 horizon checkpoints) Pearson-correlated pairwise,
averaged per cluster. A cluster with fewer than 2 fully-scored members reports `corr: None` (never
a fake 0 — absence of data is not "uncorrelated").

## Report artifact
`write_report` writes `reports/backtest_accumulation_<date>.md`: the verdict line, the aggregate
summary line, and a per-event row table — so the desk decision is auditable without re-running
the harness.

## Gotchas / caveats
- Price data is Binance perp klines only (`_default_bars_provider`) — no Moralis on this path
  (G2-independent, per `counterfactual.py`'s precedent). Not unit-tested (network); tests inject
  a deterministic `bars_provider`.
- The manual fixtures' dates are approximate — treat the historical-replay feed as authoritative
  once a candidate's wallet+contract pair is enumerable.
- `entry_existed` is a simplified base+trigger proxy, not the full §6 magnet/sweep read.
- A provider failure degrades ONE event to `outcome: None` (excluded from `aggregate`'s `n`) —
  never the whole batch, and never fabricated data.
