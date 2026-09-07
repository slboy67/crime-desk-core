# hyperliquid — read-only HL venue resolver (SPEC-84)

## Purpose
The desk is moving to self-custody (EU/MiCA). Hyperliquid is the cleaner **execution** venue for
the watchlist names it lists — deep on-chain liquidity, fully transparent funding/OI/liquidations,
and **oracle-based marks** (less composite-mark/wick risk than the operator-suspect Aster/Bitget
books, memory: `feedback_operator_venue_liquidity_is_suspect`). Coverage is **sparse by design**
(empirically 2/26 watchlist names: TNSR, CHIP — HL universe ≈ 230 perps): a *targeted* add for the
overlap, not a universe expansion. **Absence is the graceful default, never an error.**

This is a **library module** (no standalone CLI/verdict) consumed by `depth` (books.venues), `brief`
(perp.funding_by_venue), and `scan` (universe). It adds an additive cross-check source — **no new
verdict logic**; the engine's per-interval normalization and the §5 deep-neg veto are unchanged.

## API (public, no key — POST https://api.hyperliquid.xyz/info)
- `{"type":"meta"}` → `universe[]` of `{name}` — the listed-perp set (`universe()`).
- `{"type":"metaAndAssetCtxs"}` → `[meta, ctxs[]]`; each ctx (parallel to universe) has `funding`
  (HOURLY rate), `openInterest`, `markPx`, `oraclePx`, `premium`, `dayNtlVlm`. (`resolve()`)
- `{"type":"l2Book","coin":C}` → `levels:[bids, asks]` for depth (`l2_book()`).

## Key functions
- `resolve(ticker, meta_ctxs=None)` → the per-venue read `{venue, coin, funding_raw, funding_pi,
  interval_min:60, funding_4h, is_floor, mark, oracle, premium, price, oi, oi_base, vol_m}`, or
  **None** if the name isn't listed / the fetch failed (graceful absence — never raises, never
  null-poisons the caller).
- `l2_book(ticker, raw=None)` → `(bids, asks)` or `(None, None)`.
- `universe(meta=None)` → set of listed coin names. `coin_for(ticker)` applies the `ALIAS` map
  (identity for most names).

## Funding normalization (HOURLY)
HL funding settles **hourly** → `interval_min:60`, `funding_4h = hourly% × 4` (the way aster's 1h
rate is handled). `funding_pi` is the raw per-hour percent.

## Floor guard (the misfire HL would otherwise cause)
The CEX floor sentinel is the **0.005%** (`0.00005` raw) placeholder a venue returns for low-activity
intervals. **HL has no such placeholder** — it reports a real rate every hour — so `is_floor()` flags
**only an exact-zero** rate. A small HL hourly rate (CHIP's `0.0000125`, or even `0.00005/hr` which is
a *real* ±0.02%/4h rate) is **not** floored. This guards the CEX `FLOOR_RAW` from misfiring on HL's
native per-hour scale.

## Absence / degrade contract
HL not listing a name, an empty book, a timeout, or a non-200 all degrade the same way: **HL is
omitted** from `funding_by_venue` / `books.venues` / the scan universe, the four CEX venues' reads
stay **byte-identical**, and the brief/classify/scan never block (the SPEC-80 fail-safe pattern).

## Tests
`tests/test_hyperliquid.py`: resolve (TNSR/CHIP from a metaAndAssetCtxs fixture, OI/vol/mark/oracle),
graceful absence (BSB), degrade (post→None / malformed), the floor guard on the HL scale, universe/
alias, l2_book; plus the wiring into `depth` (omit-on-absence), `brief` (funding_by_venue surface),
and `scan` (HL candidates). All network mocked — offline-deterministic.
