# sector_divergence — SOLO_PUMP vs SECTOR_MOVE Cat A pre-classifier (SPEC-114)

## Purpose
Onchain-Analysis-Workshop-CrimeDesk.md Lesson 10's cheapest Cat A first-pass test: one
coin pumping vertically while its sector/narrative basket sits flat is a manipulation
prior (LAB/ESPORTS/PIPPIN shape); a sector-wide rise is liquidity rotation, not operator
action. This is a **prior, not a gate** (§0.6) — it re-ranks/annotates, never suppresses
a name, never writes thesis/state.

## Contract
```
sector_divergence LAB --token-ret 80 --json
```
Out: `{ticker, verdict, window_days, strongest, baskets:[...]}`.
Verdicts: `SOLO_PUMP` | `SECTOR_MOVE` | `MIXED` | `UNMAPPED` | `UNAVAILABLE`. A basket
fetch failure is a data FAILURE (§3) — `UNAVAILABLE`, never rendered as basket-flat.

## Config
`config/sector_baskets.json`: `thresholds` (window_days, solo_min_token_ret,
solo_max_basket_ret, sector_min_basket_ret, sector_inline_multiple, min_basket_size),
`baskets` (`cg_category` or `cg_ids`, keyed by a desk-chosen basket name), `token_baskets`
(ticker → list of basket names; a token may belong to more than one — the strongest
verdict wins: SOLO_PUMP > SECTOR_MOVE > MIXED > UNAVAILABLE). Seed baskets ship for
AI/DePIN, meme, perp-dex, BSC-microcap — membership is desk-editable.

## Wiring
`tag_for(ticker, token_ret, ...)` is the integration seam: returns `None` for an
unmapped ticker (the caller adds NO key — unmapped rows stay byte-identical) or
`{verdict, annotation, cat_a_bump, detail}`.

- `classify.classify_token` — adds a `sector` key using the live 24h change (1d window)
  when live perp data is present and the ticker is mapped.
- `triage._build_row` — same, off the board's own `chg24`.
- `screener.build_screen` — cheap path only: screener's own fetched CoinGecko page IS
  the `binance-smart-chain` (bsc-microcap) basket, so a mapped candidate gets a `sector`
  tag with zero extra network calls; baskets outside that page (ai-agents, meme, ...)
  are left to classify/triage.

## Gotchas
- The token's own return is caller-supplied, not fetched by this module — each wiring
  site uses whatever return timeframe it already holds (screener: 7d `ch7`; classify/
  triage: 1d `chg24`). Different call sites may therefore report different windows for
  the same token; the `window_days` field on the tag says which.
- A `SECTOR_MOVE` tag carries a "sector beta, not operator action" annotation — a
  breakout candle on a SECTOR_MOVE name must not misread as an escalation.
