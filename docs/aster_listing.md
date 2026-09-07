# aster_listing — is this ticker's perp actually listed on Aster? (SPEC-136)

## Purpose
Internal helper (not orchestrator-registered — same tier as `onchain.probe_contract`)
answering one narrow question: is `<TICKER>USDT` listed on Aster, the desk's sole
execution venue (CLAUDE.md §7)? Distinct from the cross-venue OI/funding SIGNAL layer
(`venue_map`, `regime_flip`) — that answers where the market's construction lives; this
answers whether the user can fill it. COTI printed a clean cross-venue regime-flip signal
and got committed with a real trigger; Aster carries no COTI market, so the trade never
existed for this user.

## Contract
```python
import aster_listing as AL
symbols = AL.fetch_aster_symbols()      # {"BTCUSDT", "ETHUSDT", ...} | None
AL.aster_listed("COTI", symbols)        # True | False | None
```
- `fetch_aster_symbols()` — one live call to Aster `fapi/v1/exchangeInfo`, disk-cached
  in `state/aster_symbols_cache.json` with a 6h TTL (the perp list churns slowly). A
  failed live fetch falls back to a stale cache before giving up; returns `None` only
  when there is truly no way to answer (§3 doctrine — unknown, never an empty listing
  standing in for "nothing is listed").
- `aster_listed(ticker, symbols)` — pure lookup, no I/O. `symbols is None` → `None`.

## Callers
- `capabilities/classify.py` `annotate_aster_listed(rows, tokens)` — one fetch per board
  run, tags every row; a watchlist entry's explicit `"aster_listed": false/true` overrides
  the live probe.
- `capabilities/brief.py` `_state_layer` — same override/probe logic for a single ticker.
- `ops/board_tick.py` `tick()` — reads the row-level flag to gate phone-page delivery
  only (never the inbox record).

See docs/classify.md §Execution-venue and docs/board_tick.md §Execution-venue for the
downstream wiring. Tests: `tests/test_spec136_aster_listed_veto.py`.
