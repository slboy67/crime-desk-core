---
name: reference_arkham_entity_discovery_workflow
description: "Workflow to discover ACTIVE entity wallets via Arkham + audit, building the active subset for vc_entity_watch"
metadata: 
  node_type: memory
  type: reference
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

Reusable workflow for adding entity (VC/MM) addresses to `config/vc_entities.json`. Uses Arkham's logged-in browser session ([[reference_arkham_playwright]]) to surface entity-tagged addresses, then activity_audit to filter to the live subset.

**Key Arkham API endpoints (verified working with logged-in session):**
- `GET https://api.arkm.com/intelligence/entity/{entity_id}/summary` — returns `{entityId, numAddresses, volumeUsd, balanceUsd, firstTx, lastTx}` — useful for assessing whether worth scraping
- `GET https://api.arkm.com/balances/entity_top_address/{entity_id}?customEntity=false` — returns the TOP single address (string)
- `GET https://api.arkm.com/intelligence/entity/{entity_id}` — full intelligence (entity object; `addresses` field is null on free tier — paid Arkham API likely needed for full list)
- Entity page URL: `https://intel.arkm.com/explorer/entity/{entity_id}` — renders ~13-30 addresses with labels (top counterparties + entity-owned)

**Known working entity IDs** (verified 2026-05-26, with address counts):
- `wintermute` (10,281 addrs, $7.26T volume), `cumberland` (12,945), `jump-trading` (231), `flow-traders` (497), `galaxy-digital` (3,817)
- Small N (worth scraping fully): `iosg-ventures` (6), `dwf-labs` (25), `dragonfly-capital` (22), `pantera-capital` (34), `three-arrows-capital` (32), `multicoin-capital` (44), `polychain-capital` (49), `a16z` (91)
- NOT found (try alt ids): `gsr`, `amber-group`, `paradigm`, `jump-crypto`, `dwf`, `polychain`, `pantera`, `iosg`

**The 4-step workflow per entity:**

1. **Verify entity exists** — call summary endpoint via `browser_evaluate`:
   ```js
   await fetch('https://api.arkm.com/intelligence/entity/<id>/summary', {credentials:'include'}).then(r=>r.json())
   ```
   If 200 and `numAddresses < 100`, proceed (small enough to scrape from page).

2. **Navigate + extract addresses** — `mcp__playwright__browser_navigate` to `intel.arkm.com/explorer/entity/<id>`, then `browser_evaluate` extracts all `a[href*="/explorer/address/"]` links with their text labels.

3. **Filter by label semantics:**
   - **OWNED** = bare hex `0x...` (no friendly label), or `(0xXXX)` short label, or `Hot Wallet`, `Gnosis Safe Proxy` (without exchange prefix)
   - **COUNTERPARTY** (SKIP) = labels containing `Binance Deposit`, `Bybit Deposit`, `Gate Deposit`, `Coinbase`, `Cobo`, `Bitget Deposit`, `Bitrue Deposit`, `OKX Deposit`, `KuCoin Deposit`, `Kraken`, `V3 Pool`, `CL Pool`, `DexRouter`, `PoolManager`, `FluidLiquidityProxy`, `MerkleVault*` (these are DEX pools/routers, not entity-owned)
   - **AMBIGUOUS** = anything else — verify manually

4. **Audit OWNED candidates** via `activity_audit.py` (or inline RPC nonce-historical):
   - ACTIVE (tx_7d > 0): add to `vc_entities.json`
   - SEMI (tx_30d > 0 only): consider for high-priority entities, skip otherwise
   - DORMANT / PRISTINE: skip (not forward signal)

5. **Decide `alert_strategy` per address:**
   - High-frequency (>1000 tx/30d): `cex_bound_only` (silent on internal cycling)
   - Low-frequency (<100 tx/30d): `nonce_any` (every tick significant)
   - Rule of thumb: cutoff around 500 tx/30d

**Validation 2026-05-26** — workflow surfaced:
- **IOSG-VENTURES** (6 Arkham addrs / 13 on page): 3 active OWN wallets, incl. `0x5bdf85...` HOT WALLET with 611K nonce and 11K tx/30d = the actual treasury wallet. The digest's `0xfa9389...` was NOT in Arkham's list (fresh wallet untagged).
- **DWF-LABS** (25 / 13 on page): 3 active OWN wallets (primary 0x53c, secondary 0xDfc, tertiary 0x1c7) at 8-51 tx/30d — the apparatus behind ESPORTS cascade ($15M+ team profit).

**Counter-finding to remember:** eth-labels' "Wintermute 2" (nonce 415K) shows DORMANT in our audit. Arkham's `entity_top_address` for Wintermute returns `0x51C72848...` ('Market Maker'), which is the actual current primary. **Always cross-reference eth-labels with Arkham + activity_audit before trusting.**

**Next entities to apply this workflow to** (cited from 2026-05-26 discovery):
- `dragonfly-capital` (22 addrs), `pantera-capital` (34), `multicoin-capital` (44), `three-arrows-capital` (32, mostly historical)
- For Wintermute/Jump/Cumberland (huge address counts), focus on the top-3 addresses shown on entity page + the `entity_top_address` API result.

Related: [[reference_arkham_playwright]] (browser session access), [[feedback_audit_preloaded_entities_before_trust]] (audit before trust), [[feedback_dont_drift_to_perp_only_scans]] (on-chain coverage discipline)
