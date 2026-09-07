---
name: reference_hyperdash_whale_tracker
description: "hyperdash_whale_tracker.py — discovers + tracks the largest long/short wallets on Hyperliquid for tokens we discuss. Seed-list driven (HL has no 'top by coin' public endpoint). Per-token ranking, position-delta tracking, Hyperdash deeplinks. Aster excluded — public API lacks per-wallet positions."
metadata: 
  node_type: memory
  type: reference
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

# Hyperdash whale tracker — `scripts/hyperdash_whale_tracker.py`

Built 2026-05-26 in response to: "find the biggest wallets on Hyperliquid/Aster that are long or short on the same coins that we discuss, analyze them, make a tool, integrate with https://hyperdash.com/explore."

## Architecture decision — seed-list-driven, NOT crawl-driven

Hyperliquid's public API exposes per-wallet queries (`clearinghouseState`, `userFills`) but **NO public "top wallets per coin" endpoint**. Tested 2026-05-26: `leaderboard` and `vaults` endpoints return HTTP 422, no aggregation route exists. So the tool runs **seed-driven**: maintain a list of known whale addresses (`config/hyperdash_whales.json`), query each, aggregate per-coin server-side.

Aster (`fapi.asterdex.com`) only exposes per-wallet data via *authenticated* endpoints (needs API key for `positionRisk` etc). Public Aster API is aggregate-only (OI, ticker, klines). **Aster per-wallet positioning is therefore unobtainable without authentication** — Aster signal is captured via aggregate OI in `venue_risk.py` (the prop-MM-exposure layer). Tool focuses HL where the data is public.

Hyperdash.com / hyperdash.info **has Cloudflare anti-bot on its API** (tested 2026-05-26 — `/api/leaderboard` returns 403 to Python requests). Their HTML pages are public, but JSON endpoints require a real browser. Stretch goal of scraping their leaderboard needs Playwright integration — deferred.

## Commands

```
python3 scripts/hyperdash_whale_tracker.py scan <TICKER>           # rank seed whales positioned on this token
python3 scripts/hyperdash_whale_tracker.py scan-all [--min N]      # all positions across all seed whales
python3 scripts/hyperdash_whale_tracker.py rank <TICKER>           # rank from last saved snapshot
python3 scripts/hyperdash_whale_tracker.py snapshot                # save current positions for delta tracking
python3 scripts/hyperdash_whale_tracker.py deltas                  # changes since last snapshot
python3 scripts/hyperdash_whale_tracker.py add <ADDR> <LABEL> [--note ...]   # add to seed manually
python3 scripts/hyperdash_whale_tracker.py link <ADDR>             # emit Hyperdash deeplink
python3 scripts/hyperdash_whale_tracker.py seed [-v]               # list all seed whales
python3 scripts/hyperdash_whale_tracker.py discover [--min-deposit N] [--blocks N] [--auto-add]   # AUTO-DISCOVER new whales via Arbitrum bridge
```

## Engine-native integration (shipped 2026-05-27)

`analyse.py` now calls `hyperdash_whale_tracker.py scan <TICKER> --json` automatically as part of every engine run. The result feeds into convergence as a tier-2 signal:
- Output banner shows e.g. `HL whales: WHALES_SHORT (0L $0.00M / 1S $15.63M, seed n=4)` between CVD and Structure
- Convergence notes add `✅ HL whales confirm <direction>` when whale bias aligns with engine direction
- Convergence notes add `⚠ HL whales OPPOSE <direction>` when whale bias contradicts (auto-downgrades STRONG → MILD)
- `WHALES_SPLIT` (bilateral, both sides >30% of dominant) = explicit neutral note
- `NO_WHALES` / `TOO_SMALL` (sample-size or unlisted) = silent (absent signal isn't worth a line)

Verdict values: `WHALES_LONG`, `WHALES_SHORT`, `WHALES_SPLIT`, `NO_WHALES`, `TOO_SMALL`, `NO_DATA`. Validated 2026-05-27:
- ETH: engine shows `WHALES_SHORT (0L / 1S $15.63M)` — `0xecb6` basket trader's ETH short auto-surfaced
- NEAR: `WHALES_LONG (1L $2.61M / 1S $0.69M)` — `0x8ab13dbc` LONG outweighs `0xecb6` SHORT, net long > 30% threshold

The integration is non-additive to perp/on-chain scores (whale is confluence, not primary driver — see Section 7). Tier downgrade only fires on direct OPPOSE. This keeps the engine's existing scoring stable while adding the whale-positioning signal where it's available.

## Auto-discovery via Arbitrum HL bridge (the key innovation)

HL public API has no "top wallets per coin" endpoint AND fills don't expose counterparties. So *positional* discovery is blocked. But HL operates on Arbitrum, and the bridge contract `0x2df1c51e09aecf9cacb7bc98cb1742757f163df7` receives every USDC deposit a user makes when funding their HL account. **Top depositors = biggest whales committing fresh capital.**

`discover` scans recent USDC Transfer events to the bridge on Arbitrum (chunked 500 blocks for public RPC compatibility), aggregates by sender, filters by min deposit size, then cross-references each candidate against `clearinghouseState` to verify they have active HL positions. Only verified active depositors are auto-added (with `--auto-add` flag).

**Validated 2026-05-27** — first run in a ~3hr Arbitrum window (50K blocks):
- **`0xecb63caa…`** — $8.7M deposit → multi-position basket worth **~$40M total notional** (L BTC $24.5M + S ETH $15.6M + 10x shorts on AAVE/ADA/ARB/AVAX/BCH/ASTER/BERA = professional MM/quant)
- **`0x8ab13dbc…`** — $331K deposit → $2.6M LONG NEAR (single-token whale, TWAP-closing)
- **`0xd4eeed94…`** — $100K deposit → $30K LONG HYPE

Three high-quality discoveries in a single discover run, all auto-added to seed with --auto-add. Recurring discover runs (e.g., every few hours or daily) compound the seed over time — eventually building a comprehensive HL whale roster without manual curation.

## Output format

Per-coin ranked table with notional, leverage, entry, uPnL + Hyperdash deeplinks. Example:

```
# HMSTR  (1 whales, L:S notional $1.19M : $0.00M, ratio inf, delta $+1.19M)
  WHALE                       SIDE   NOTIONAL    LEV  ENTRY        uPnL
  HMSTR-PUMPER                LONG  $1,194,149    3x  $0.000167   +$49,246  0x02b0…2ffd

# Hyperdash links:
  HMSTR-PUMPER: https://hyperdash.info/trader/0x02b0f4d585456a1f808ffe25624a0e2dafe52ffd
```

## Delta tracking

`snapshot` saves all whale positions to `state/whale_snapshots.json` (rolling history of last 50 snapshots). `deltas` diffs against the previous snapshot and surfaces:
- 🆕 NEW position opened
- 💸 CLOSED position
- 🔄 FLIP (long→short or vice versa)
- ➕ ADD (size increase)
- ➖ REDUCE (size decrease)

Configurable `--min` threshold filters noise.

## Integration with existing infrastructure

- **Reuses `config/hl_whales.json`** — auto-merges those entries into the seed pool so `hl_whale_watch.py` (live alerter) and `hyperdash_whale_tracker.py` (ranker/snapshotter) share the same data layer
- **Hyperdash deeplinks**: `https://hyperdash.info/trader/<addr>` — opens the wallet's full Hyperdash profile (PnL history, fill history, ROE, all positions)
- **Cross-token discovery**: a whale that holds positions on multiple Cat A tokens appears once per coin in the ranking — `scan-all` aggregates the entire seed list across all tokens

## Validation 2026-05-26

- `scan HMSTR` correctly identified HMSTR-PUMPER ($1.19M LONG, 3x, +$49K uPnL = currently profitable, up from morning loss)
- `snapshot` saved + persisted; `deltas` correctly handled empty-history edge case
- Seed merge from `hl_whales.json` works (1 whale auto-imported)

## How to grow the seed list

Manual cadence: when investigating a token in session, if user mentions or we discover a notable HL trader, run `add 0x... <LABEL> --note "..."` to register them. Over time the seed list grows. The audit principle from [[feedback_audit_preloaded_entities_before_trust]] applies — verify activity via `hyperliquid.py 0x...` before adding to seed (avoid adding dormant addresses that won't generate signal).

## Open infrastructure follow-ups

1. **Hyperdash scrape via Playwright** — would auto-populate seed list with their top-100 from `hyperdash.com/explore`. Stretch goal, needs browser automation
2. **Auto-discovery via fill counterparty** — if HL exposes counterparty addresses in fills (uncertain), we could crawl outward from known whales. Initial probe: HL fills don't expose counterparty, so this approach blocked
3. **PnL-rank command** — rank whales by realized PnL or ROE, not just notional. Would need `userFills` history aggregation
4. **`watch` mode** — long-running daemon that snapshots every N minutes + fires alert on significant deltas. Currently snapshot/delta is manual; turning it into a continuous loop is straightforward extension
5. **Aster integration** — when (if) Aster ships public per-wallet endpoint, drop in alongside HL

Related: [[reference_arkham_entity_discovery_workflow]] (the EVM equivalent of this discovery problem), [[feedback_audit_preloaded_entities_before_trust]] (audit discipline), [[reference_cross_cat_a_escrow_0x73d8bd54]] (cross-token apparatus monitor — different layer, same observability pattern)
