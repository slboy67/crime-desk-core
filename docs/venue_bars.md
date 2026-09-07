# venue_bars — all-venue 1h OHLC sweep + dispersion (SPEC-188)

## Purpose
The user has twice told the desk (2026-09-01, 2026-09-02) to check EVERY venue before
making a price/structure statement (lower-high, stall, breakdown-hold, sweep,
level-crossed). Both times the orchestrator "noted it" and then read hourlies off one
or two venues by hand anyway (OP 2026-09-02 13:00Z: Bybit alone read "stall under the
prior high"; across 14 venues it was a higher high on 11/14 — 0.7% high-spread). Hand
discipline had failed twice, so this makes the sweep a deterministic engine layer that
`brief` prints every time — no structure claim can be made off one tape again.

## Contract
```
venue_bars '{"ticker":"OP","interval":"1h","n":6}'
```
Out (abbreviated):
```json
{
  "ticker": "OP", "interval": "1h", "n": 6,
  "venues": {
    "binance": {"venue": "binance", "available": true,
                "bars": [{"ts": 1788354000, "o": 0.0947, "h": 0.0949, "l": 0.0933,
                          "c": 0.0944, "quote_vol": 1578327.68, "live": false}, "..."],
                "turnover_24h_usd": 58035965.43},
    "kraken": {"venue": "kraken", "available": false, "reason": "no candles returned (unlisted or empty)"}
  },
  "n_total": 14, "n_available": 13,
  "bars": [{"ts": 1788354000, "live": true, "n_venues": 13,
            "median_h": 0.0975, "median_l": 0.0947, "median_c": 0.0968,
            "high_spread_pct": 0.7, "low_spread_pct": 0.9, "close_spread_pct": 0.5,
            "max_h_venue": "binance", "min_h_venue": "coinbase",
            "max_l_venue": "bybit", "min_l_venue": "kraken"}],
  "dominant_tape": {"venue": "binance", "turnover_24h_usd": 58035965.43},
  "execution_venue": "aster"
}
```

## Venue set
Fourteen venues, keyless, fanned out CONCURRENTLY (one thread per venue, 8s budget
each, whole sweep observed ≤3s live for OP/BTC): `binance`, `bybit`, `okx`, `bitget`,
`kucoin`, `gate`, `mexc`, `htx`, `bingx`, `hyperliquid`, `aster`, `kraken`, `blofin`,
`coinbase`. Endpoints + symbol conventions live-verified 2026-09-02 (recorded in the
SPEC-188 ticket's prototype). A venue that fails (HTTP error / timeout / malformed
body / genuinely unlisted) returns `available:false, reason:<...>` — loud, never
dropped (§3 zero-print rule), same adapter-isolation discipline as `venue_map.py`
(SPEC-129) reused here for bars instead of funding/OI.

Kraken uses its own symbol convention (`PF_XBTUSD`, never `PF_BTCUSD`) —
`KRAKEN_SYMBOL_ALIASES` maps `BTC`→`XBT`; add further aliases there if another ticker
needs one.

## turnover_24h_usd / dominant_tape
Fetched best-effort per venue via its own 24h-ticker endpoint. Ten venues have a
verified cheap USD field (binance, bybit, okx, bitget, kucoin, mexc, htx, bingx,
aster, hyperliquid); four (gate, kraken, blofin, coinbase) have none verified live and
report `turnover_24h_usd: null` rather than a guessed figure. `dominant_tape` is the
max over whichever venues DID report one — **not** the same as `venue_map`'s
`top_oi_venue` (OI-based size book) — both should be named side by side (a size book
and a dominant tape are different venues, e.g. OP 2026-09-02: Binance turnover $58.8M
vs Bybit $23.8M, while Binance OI was null in `venue_breadth`).

## Dispersion
Computed per bar timestamp, over whichever venues have a bar at that ts:
`high_spread_pct = (max_h - min_h) / median_h * 100` (same for low/close), plus the
venue names at the max/min high and low. The **live** bar (still forming) is flagged
`live: true` on every venue's own bar list and on the composed `bars[]` entry —
`*` in the human-rendered tape line.

## level_agreement(result, level, direction, bar_ts=None)
For one price level, which venues' bar crossed it on `bar_ts` (default: the live
bar)? `direction="above"` tests `bar.h >= level` (crossed) / `bar.c >= level` (closed
beyond); `direction="below"` mirrors on low/close. Returns
`{crossed, closed_beyond, n_crossed, n_total, execution_venue_crossed,
dominant_tape_crossed}` — `execution_venue_crossed` is Aster specifically (§7: fills
happen there), independent of whether Aster is the dominant tape.

## Consumers
- `brief` (SPEC-188 §2): prints the compact tape block unconditionally, plus one
  tape-agreement line per committed level a venue crossed in the last `n` bars.
- `classify` (SPEC-188 §3): a price-leg TRIGGERS/BREAKS/WATCH-ARMED event carries
  `venue_agreement: FULL|PARTIAL|SINGLE` — FULL requires Binance AND the OI size-book
  venue AND Aster (execution) to all have crossed; the verdict itself is unchanged,
  the label is annotation only (§0.5 — no new semantics on the state machine).

## Doctrine
Only `interval="1h"` is verified against every venue's own granularity string (the
spec's DoD is all-1h); `4h`/`1d` are passed through per-venue best-effort and not
guaranteed correct. Read-only — no thesis/verdict fields, matches `venue_map`'s
discipline.
