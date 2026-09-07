# depth — live order-book shelf read (SPEC 27, +SPEC-85 Aster)

## Purpose
The DOM the desk hand-curls: largest resting bid shelf below + ask wall above per venue
(**Aster FIRST** — the execution venue we FILL on — then Bitget + Binance as the
cross-venue operator-suspect compare) with price + $size, round-number/spoof tags.
Read-only intel for TP placement (§7 magnet execution) and size-to-exit (§0.6.4).

⚠ The Aster book is the **TP/exit-shelf** anchor only — liq fires off the **oracle
aggregate** (Pyth / Chainlink / Binance Oracle), NOT Aster's DOM, so never read it as the
liq reference (§7).

## Contract
```
depth '{"ticker":"SKYAI"}'                  # all venues — Aster listed first
depth '{"ticker":"SKYAI","venue":"aster"}'  # execution-venue book only
depth '{"ticker":"SKYAI","venue":"bitget"}'
```
Out: `{ticker, venues:{venue:{mid, bid_shelf_below, ask_wall_above, truncated,
deepest_level_seen}}}`.

## Hyperliquid (SPEC-84, additive read-only)
`hyperliquid` is surfaced alongside Bitget/Binance **only for the names HL lists** (the ~2/26
overlap, e.g. TNSR/CHIP). HL's oracle-marked book is the transparent counterpoint to the
operator-suspect CEX books (§7). It is **omitted entirely** — never a present-but-`unavailable`
entry — when HL doesn't carry the name, on an empty book, or on any HL fetch error; the
Bitget/Binance reads stay byte-identical. `--venue hyperliquid` requests it alone.

## Gotchas
- Merge-depth APIs cap ~100 levels near mid (`truncated:true`,
  `deepest_level_seen`) — NEVER refute a trader's "liquidity below" claim off a blind
  snapshot; defer to the live DOM
  (memory: feedback_orderbook_api_truncation_defer_to_live_dom).
- For cluster names the real exit book can be Bitget even when Binance carries the
  OI/mark risk (SKYAI lesson) — read both, that's the default.
