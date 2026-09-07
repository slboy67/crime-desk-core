# replay — backtest the setup scorers for real base rates (SPEC 62)

```
python3 capabilities/replay.py '{"action":"fetch","symbols":["BEAT","FOLKS"]}' --json
python3 capabilities/replay.py '{"action":"run","setup":"blowoff","symbols":["BEAT","FOLKS"]}' --json
python3 capabilities/replay.py '{"action":"report","setup":"blowoff"}' --json
python3 capabilities/replay.py '{"action":"ledger-import"}' --json
```

The §9 base-rate gate needs **n≥10 per signature**; live trading accumulates that over
months. The SPEC-59 scorers are **deterministic** — replay them over history and the gate
gets real numbers in days.

## Three stages (one CLI `action` each)

1. **`fetch`** — pull + cache per-symbol history under `state/replay/<SYM>.json` (gitignored):
   1h klines (full listing, limit 1500), funding-rate history, OI history. **Idempotent**
   (cached symbols skipped), **resumable**, rate-limit-respecting. *Binance caps OI history at
   ~30d* (documented) — replay uses what exists; the OI/funding legs degrade where the series
   doesn't reach.
2. **`run`** — step each symbol's bars; at each bar `derive_replay_signals` computes the
   **PRICE/OI/FUNDING-computable** legs (on-chain / L-S / spot-CVD / oi_sides-wash legs are
   **unavailable in replay** — omitted, and the evaluated legs recorded per outcome). The
   reduced signals feed the real `setup_score`; when it returns **ARMED**, `simulate_trade`
   runs the §6 entry + §7 stop/TP geometry (stop beyond the window-high wick +1.5%, TP1=1R,
   TP2=2R, 24-bar time-stop). One position per symbol at a time (no overlap). Outcomes append
   to `state/replay/outcomes.jsonl`.
3. **`report`** — per-signature `n`, `hit%`, `avg R`, `total R`, `max_adverse_excursion_r`,
   and a `by_symbol` breakdown. Every row tagged `source:"replay"`.
4. **`ledger-import`** — write per-outcome rows into the SPEC-40 ledger, tagged
   `source:"replay"`. `time_stop` maps to the signal-only `retired_unfilled` (counts in n,
   neither hit nor miss). **Never silently mixed with live rows** — the §9 gate reads them
   split (SPEC 63 `ledger stats` source split).

## Replay-able setups

Only **`blowoff`** and **`catb_top`** are price/OI/funding-computable end-to-end (both
SHORTs). `trap_long`/`neg_funding_gate` (spot CVD + oi_sides wash) and `stage45_short`
(on-chain deposits + L-S) have legs that klines can't reconstruct, so they are out of replay
scope — the DoD n≥10 target is the two replay-able signatures combined.

## Determinism & reproducibility

The engine is **pure over a bar list** — same bars + setup → identical outcomes (the gating
tests drive it on synthetic bars, no network). `derive_replay_signals` is strictly **causal**
(reads only `bars[:i+1]`; the "not re-bought in 1-2 candles" leg is confirmed only once those
candles have printed, so there is no look-ahead).

## Leg derivations (replay approximations, documented)

| leg | replay computation |
|---|---|
| `parabolic` | window-start close → window peak high, ≥50% |
| `ath_wick` | peak high ≥2% above the highest **close** (a wick that didn't hold) |
| `lower_high` | recent highs below the prior window peak |
| `clean_break` | close broke below the pre-break 12-bar support on ≥1.5× vol, held `BREAK_CONFIRM` bars |
| `oi_off_highs` / `oi_peaked_rolled` | OI ≥5% off its window high |
| `funding_cooling` | |funding| shrinking vs the window's first-half mean |
| `volume_declining` | recent-third mean volume < middle-third mean |

On-chain / L-S / spot-CVD / oi_sides-wash legs: **unavailable** (omitted; recorded in
`evaluable_legs`).

Tests: `tests/test_replay.py` (engine on synthetic bars — no network).
