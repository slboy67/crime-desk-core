# maxsize — pre-trade max-clean-size + slippage curve + venue routing (SPEC-87)

## Purpose
The execution-microstructure layer under [`size`](size.md) (§7). `size` answers *"what's the max
**account-risk** size / does the trade pass the §7 gate."* **`maxsize` answers *"given I'm entering,
what's the max **clean** clip per venue, on which venue is it cheapest, and how do I clip it"*** —
automating the by-hand order-book walk the desk does before every fill
([[project_execution_venue_aster]]).

Reuses `depth.py`'s book fetchers (Aster/Bitget/Binance + Hyperliquid) — **no new venue logic**.

⚠ Read-only **intel** — participates in NO auto-verdict, like `depth`. It informs the human; it never
gates or sizes a trade by itself.

## Contract
```
maxsize BEL --side long --leg entry --bps 25 --json     # buy → walk ASKS, 25-bps band
maxsize BEL --side short --json                          # short entry: sell → walk BIDS
maxsize LAB --side long --size 5000 --json               # route a $5k clip; clip schedule if > ceiling
maxsize BEL --venue aster --json                         # one venue only
```

Via the orchestrator: `orchestrator.py maxsize '{"ticker":"BLESS","side":"short","bps":25}'`. The
registry `invoke` uses **`{key}` placeholders only** — `[--side {side}] [--leg {leg}] [--bps {bps}] …` —
never literal alternations (`long|short`) or bare defaults (`25`) inside the optional `[ … ]` groups;
those leak straight to the shell (`short: command not found`). Defaults (side=long, leg=entry, bps=25)
live in `maxsize.py`'s argparse, so an omitted key still runs. (SPEC-88; enforced by the invoke-lint
test in `tests/test_orchestrator.py`.)

### Side / leg geometry (the whole point)
| side | leg | action | walks |
|---|---|---|---|
| long | entry | buy | **asks** |
| short | exit | buy | **asks** |
| short | entry | sell | **bids** |
| long | exit | sell | **bids** |

Default `--side long --leg entry` (walk asks). The output reports `action` + `walked_side` actually used.

### Per venue
- `max_clean_usd` — max notional fillable while the **realized VWAP** stays within `--bps` of mid
  (default 25). `clean_ceiling_truncated:true` ⇒ the whole book fills *under* the band, so the figure
  is a **FLOOR**, not a true ceiling.
- `ladder` — fixed rungs $100/250/500/1k/2k/5k/10k → `realized_bps` + `walks_to` price + `exhausted`
  (a rung the snapshot can't fill is flagged, never reported as a clean fill).
- `depth_within_1pct_usd` on the walked side; `spread_bps`; `mid`.
- `truncated` / `truncation_note` — the walked book ends inside ±1% of mid ⇒ defer to the live DOM,
  **never** read "empty beyond" as "no liquidity" (memory:
  feedback_orderbook_api_truncation_defer_to_live_dom).
- `executable` / `custody` — **aster, hyperliquid = self-custody (executable)**; **binance, bitget =
  signal-only CEX**.

### Routing (the deliverable)
`recommendation.route_to` = the cheapest **executable** venue for the requested `--size` (or the largest
clean ceiling if no size). If the target exceeds that venue's clean ceiling, `clip_schedule` suggests
`N × $X` clips + a TWAP/re-walk note. `deepest_venue_overall` is reported separately and, if it's a
non-executable CEX, flagged with a note that execution can't route there.

## Hard caveats the output always carries
1. `snapshot_caveat` — **calm-tape snapshot only**; thin venues swell/thin/spoof second-to-second.
2. `calm_vs_flush` — `max_clean_usd` is **calm-tape** capacity; exit liquidity *during* a cascade is a
   fraction of it (§7 size-to-the-exit). Do NOT read it as a safe position size.
3. `oracle_mark_note` — slippage is the **fill** cost; on Aster/HL liquidation is marked off the
   **oracle aggregate**, not this book (§7).

## Pairs with
`size` (§7 gate). A natural follow-up is `size` consuming `maxsize`'s per-venue clean ceiling as the
execution-feasibility input; SPEC-87 stays standalone read-only. Venue coverage grows as more
self-custody DEXs are wired into `depth.py` (the HL pattern).
