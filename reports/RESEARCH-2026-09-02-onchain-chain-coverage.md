# RESEARCH — on-chain chain coverage: which chains the desk can read, which are blind, what closes each gap (2026-09-02)

Scope: read-only audit of the on-chain layer (`capabilities/onchain.py`, `verify_wallet.py`, `onboard.py`,
`wallet_state.py`, `trace_tree.py`, `stake_schedule.py`, `local_index.py`, `_oldrepo/scripts/moralis.py`,
`config/tracked_wallets.json`, `config/watchlist.json`, `config/blockscout.json`, `config/holder_enumeration.json`)
against the chains the board's names actually live on, then a live keyless probe of every candidate primary
source per chain (all curls run from the desk's home IP on 2026-09-02, `python3 urllib`, UA `Mozilla/5.0`,
no keys — the one exception is `solana.leorpc.com/?api_key=FREE`, a vendor's public literal key, flagged as such).
No code changes proposed here; this is the evidence a future spec can bet on. Prior round: `EVAL-free-apis-round2.md`.

---

## TL;DR

1. **What the code reads today is EVM-only, and only seven EVM chains** — the same `_GOPLUS_CHAIN` /
   `_MORALIS_CHAIN` map: `binance-smart-chain, ethereum, base, polygon, arbitrum, optimism, avalanche`.
   Everything else is `supported:false` in `onboard._rank_chains` and falls out of every read
   (`_concentration_primary` → `"no readable chain among ranked (['sui'])"` is exactly MAGMA/MMT/US).
2. **The board lives on far more than seven chains.** 134 tracked tokens: primary chain ethereum 38 ·
   BSC 33 · solana 8 · base 6 · sui 3 · robinhood 2 · optimism 1 · unresolved 43 (pre-SPEC-56 rows).
   Names whose *majority* supply sits on a chain the code cannot see: PENGU (99.9% off-EVM7), SLX (98.5%
   Solana), NIGHT (97.8% Cardano), FOLKS (94.7% Monad/Sei/Algorand), TAKE (92.8% Sui), BLESS (91.8% Solana),
   CLO (77.7% Sei), TAC (75.8% TON), BTR (62.3% Bitlayer), BMT (50.5% Solana), EVAA (40% TON), plus the three
   Sui-native names (MAGMA/MMT/US), 8 Solana-native names (FIDA/SKR/PUMP/JTO/TNSR/DRIFT/ARC/BIRB),
   CASHCAT/PONS (Robinhood Chain), ONG (Ontology, watchlist, unmapped), GALA 9.7% GalaChain.
3. **The single biggest fix costs $0 and no new vendor: GoPlus already serves keyless top-10 holders +
   holder_count on Solana, Sui, Monad, Berachain, Bitlayer, Sonic, Robinhood Chain, Mantle, Abstract,
   World Chain** (live-verified below, e.g. MAGMA: 17,854 holders, top-1 = 51.0%). The desk's map just
   doesn't list them. That alone turns "no readable chain" into a concentration read for every non-EVM
   name on the board except Sei/Hemi/TON/Cardano/Algorand/Ontology/GalaChain.
4. **Flow (transfer history) has a keyless path on every chain that matters, but the shape differs:**
   - Solana: official RPC `getSignaturesForAddress` + `getTransaction` work keyless (100 req/10s/IP);
     `getTokenLargestAccounts` is **method-throttled to 429 on every attempt** (paced 11s apart) — holders
     come from GoPlus (top-10) or a keyed Helius free tier (1M credits/mo, key already in `secrets.json`).
   - Sui: **JSON-RPC on `fullnode.mainnet.sui.io` is dead** (`-32601 … JSON-RPC on public fullnodes has been
     deprecated`, disabled week of 2026-07-27 per docs); `graphql.mainnet.sui.io/graphql` answers keyless
     (coinMetadata, address balance, `transactions(filter:{sentAddress|affectedAddress})` with balanceChanges),
     and `sui-rpc.publicnode.com` still serves the old `suix_*` JSON-RPC keyless.
   - TON (toncenter v3 + tonapi), Cardano (Koios), Algorand (Nodely): keyless holders AND transfers, verified.
   - EVM long tail (Monad/Berachain/Sonic/Mantle/Sei/Abstract/HyperEVM/World): **Etherscan V2 free tier
     covers them with the desk's existing key** (docs table "Free Tier Available"; keyless probe returns
     `Missing/Invalid API Key` rather than the paid-only refusal) — `_ETHERSCAN_CHAINID` is hard-coded to
     `{ethereum: 1}`. Arbitrum and Polygon are free-tier too. BSC / Base / Optimism / Avalanche stay
     "Paid Tier Only" (Lite $49/mo unlocks all chains but no PRO endpoints; holder lists are PRO = $199/mo).
   - Blockscout-hosted keyless REST (`/api/v2/tokens/{addr}/holders`, 50/page, `x-ratelimit-limit: 180`
     per window) answers on eth/arbitrum/optimism/polygon/hemi; Base's holders endpoint 500s on big tokens;
     no instance exists for BSC (re-confirmed), Berachain, Monad, Sonic, Avalanche; Robinhood Chain's is
     Cloudflare-challenged from curl; Mantle's is 502 today.
5. **BSC is still the quota pain and nothing keyless changed:** no explorer (Etherscan V2 paid-only,
   Blockscout none, BscScan V1 refuses as deprecated), `bsc-dataseed` rejects `eth_getLogs` even at 500
   blocks (`-32005 limit exceeded`), `bsc.drpc.org` and `blastapi` were 429 at test time; the working keyless
   log source right now is `bsc-rpc.publicnode.com` at ≤2,000 blocks/call (10,000 → "Archive requests require
   a personal token"). Moralis remains the only lifetime-history path and is quota-gated.
6. **Cost to close everything above except BSC lifetime flow: $0 (map/pool edits, one Solana + one Sui
   adapter over sources verified here).** Paid options, priced: Etherscan Lite $49/mo (BSC/Base/OP/Avax
   tokentx, no holders), Etherscan Standard $199/mo (adds `tokenholderlist`), Moralis Starter $149/mo
   (no free plan on the pricing page — the desk's key is grandfathered), Solscan Lite $49/mo, BlockVision
   free tier exists but Sui coin-holders is Pro-only (30 trial calls), Blockberry keyed but "Pro endpoint,
   currently available for free as part of the API campaign" — a tier that will rot.

---

## 1. Coverage matrix — TODAY's code (what a `brief`/`onchain`/`verify_wallet` call can actually do)

Legend: **WORKS** = wired and answered live · **QUOTA-GATED** = wired but the only provider is metered
(Moralis free plan / Etherscan 100k/day) · **BOUNDED** = works keyless but only over a bounded recent
window (getLogs chunking), not lifetime · **BLIND** = no code path (chain not in the provider maps).
"Holders" = top-N concentration; "Flow" = token-transfer history per wallet; "Nonce/Bal" = liveness +
balanceOf; "Code" = `eth_getCode` / contract probe; "Logs" = raw Transfer-log window; "Lock" =
`stake_schedule` vesting/escrow reads (needs archive `eth_call`).

| Chain (board exposure) | Holders | Flow (transfers) | Nonce/Bal | Code | Logs | Lock/vesting |
|---|---|---|---|---|---|---|
| **BSC** (33 primary, most Cat-A) | WORKS (GoPlus top-10 only; Bitquery >10 needs a key the desk lacks → skip) | QUOTA-GATED (Moralis) → BOUNDED getLogs fallback | WORKS | WORKS | BOUNDED (publicnode ≤2k blocks; dataseed refuses; drpc/blastapi 429 today) | WORKS (blastapi archive — rate-limited on logs today) |
| **Ethereum** (38) | WORKS (GoPlus top-10) | WORKS (Etherscan V2 free, key present) → Blockscout → Moralis | WORKS | WORKS | BOUNDED (publicnode refuses even 500 blocks as "archive"; `eth.drpc.org` 10k OK) | WORKS |
| **Base** (6) | WORKS (GoPlus top-10) | WORKS (Blockscout tokentx) → Moralis | WORKS | WORKS | BOUNDED (`mainnet.base.org` ≤500 blocks on busy tokens) | WORKS |
| **Arbitrum** (CHIP/ESP/LQTY/HYPER) | WORKS (GoPlus) | QUOTA-GATED (Moralis only; Etherscan free-tier + Blockscout exist but unwired) | WORKS | WORKS | BOUNDED (`arb1.arbitrum.io/rpc` 10k OK) | WORKS |
| **Optimism** (OP) | WORKS (GoPlus) | QUOTA-GATED (Moralis; Blockscout unwired) | WORKS | WORKS | BOUNDED (≤500 blocks) | WORKS |
| **Polygon** (SYN/SAND/STG) | WORKS (GoPlus) | QUOTA-GATED (Moralis; Etherscan free-tier + Blockscout unwired) | WORKS (`polygon-rpc.com` is DEAD: 401 tenant disabled — pool falls to blastapi) | WORKS | BOUNDED | WORKS (2nd pool URL dead) |
| **Avalanche** (KITE) | WORKS (GoPlus) | QUOTA-GATED (Moralis) | WORKS | WORKS | BOUNDED (≤2,048 blocks on api.avax.network; publicnode 10k OK) | BLIND (not in `stake_schedule.RPC_POOL`) |
| **Mantle** (BILL/OPG/ENA) | BLIND (GoPlus lists 5000 but map lacks it; ENA-on-Mantle returned no holder block) | BLIND | WORKS (config rpc `rpc.mantle.xyz`, used by wallet_state only) | WORKS | BLIND (no pool in onchain.py) | BLIND |
| **Solana** (8 primary + SLX/BLESS/BMT/PENGU majority) | BLIND | BLIND | BLIND | BLIND | BLIND | BLIND |
| **Sui** (MAGMA/MMT/US + TAKE 92.8%) | BLIND | BLIND | BLIND | BLIND | BLIND | BLIND |
| **Robinhood Chain** (CASHCAT/PONS) | BLIND | BLIND | BLIND | BLIND | BLIND | BLIND |
| **TON** (TAC 75.8%, EVAA 40%, ENA) | BLIND | BLIND | BLIND | BLIND | BLIND | BLIND |
| **Cardano** (NIGHT 97.8%, ID, FET) | BLIND | BLIND | BLIND | BLIND | BLIND | BLIND |
| **Sei EVM** (CLO 77.7%, FOLKS) | BLIND | BLIND | BLIND | BLIND | BLIND | BLIND |
| **Monad** (FOLKS, APR) | BLIND | BLIND | BLIND | BLIND | BLIND | BLIND |
| **Bitlayer** (BTR 62.3%) | BLIND | BLIND | BLIND | BLIND | BLIND | BLIND |
| **Berachain** (BR, STG, EUL) | BLIND | BLIND | BLIND | BLIND | BLIND | BLIND |
| **Hemi** (HEMI) | BLIND | BLIND | BLIND | BLIND | BLIND | BLIND |
| **Sonic** (OGN, EUL) | BLIND | BLIND | BLIND | BLIND | BLIND | BLIND |
| **Algorand** (FOLKS) | BLIND | BLIND | BLIND | n/a | n/a | BLIND |
| **Ontology** (ONG, watchlist) | BLIND | BLIND | BLIND | BLIND | BLIND | BLIND |
| **GalaChain** (GALA 9.7%) | BLIND | BLIND | BLIND | n/a | n/a | BLIND |
| Abstract / HyperEVM / World Chain (PENGU, WLD dust) | BLIND | BLIND | BLIND | BLIND | BLIND | BLIND |

Where the blindness comes from, by file:
- `capabilities/onchain.py:468` `_MORALIS_CHAIN` (7 chains) — gates `verify_wallet.queryable_chains` and every flow read.
- `capabilities/onchain.py:1220` `_GOPLUS_CHAIN` (7 chains) + `:1241` `_CG_PLATFORM_TO_GOPLUS` — gates `onboard._rank_chains` (`supported:false` for anything else) and therefore `_concentration_primary`.
- `capabilities/onchain.py:541` `_PUBLIC_RPCS` (bsc/eth/avalanche) + `config/tracked_wallets.json.rpcs` (eth/bsc/mantle/base/polygon/arbitrum/optimism/avalanche) — nonce/code/balanceOf pool.
- `capabilities/onchain.py:843` `_ETHERSCAN_CHAINID = {"ethereum": 1}` — comment says "free tier is ETH-only (v3, live-verified)"; that was true for BSC, but false for Arbitrum/Polygon and the whole new-chain long tail (see §2.x).
- `config/blockscout.json` — eth + base only.
- `capabilities/local_index.py:66` `RPC_PROVIDER_TABLE` — bsc + eth only.
- `capabilities/stake_schedule.py:60` `RPC_POOL` — eth/bsc/base/polygon/arbitrum/optimism.
- No Solana/Sui/TON/Cardano code path exists in `capabilities/`; `_oldrepo/scripts/sol_holders.py` is a parts-bin Solana script (getTokenLargestAccounts + getSignaturesForAddress, Helius key optional) that nothing imports. `config/secrets.json` already holds `helius_api_key` and `bscscan_api_key` (the latter is the same V2 key family; BscScan V1 is refused as deprecated).

## 2. Coverage matrix — AFTER the $0 fixes (every cell below is backed by a live keyless call in §3)

| Chain | Holders | Flow | Nonce/Bal | Code | Logs | Lock |
|---|---|---|---|---|---|---|
| BSC | GoPlus top-10 (unchanged; >10 = Bitquery key or Etherscan Standard $199) | unchanged (Moralis quota / bounded logs) | ✓ | ✓ | publicnode ≤2k blocks | ✓ |
| Ethereum | + Blockscout v2 `/tokens/{a}/holders` 50/page keyless | ✓ | ✓ | ✓ | `eth.drpc.org` 10k | ✓ |
| Base | GoPlus; Blockscout holders flaky (500 on AERO/ZORA) | ✓ | ✓ | ✓ | `base.drpc.org` (20k-result cap, hints range) | ✓ |
| Arbitrum | + Blockscout holders | + Etherscan V2 free tier (`chainid=42161`) + Blockscout | ✓ | ✓ | 10k | ✓ |
| Optimism | + Blockscout holders | + Blockscout tokentx | ✓ | ✓ | ≤500 | ✓ |
| Polygon | + Blockscout holders | + Etherscan V2 free + Blockscout | ✓ (drop `polygon-rpc.com`) | ✓ | publicnode 2k | ✓ |
| Avalanche | GoPlus | Moralis only (Etherscan paid-only, no Blockscout) | ✓ | ✓ | publicnode 10k | add pool |
| Mantle | GoPlus 5000 (partial) | Etherscan V2 free (`chainid=5000`, key) | `rpc.mantle.xyz` / publicnode | ✓ | 10k OK | add pool |
| Solana | GoPlus `/solana/token_security` top-10 keyless; Helius DAS (free key) for full list | official RPC `getSignaturesForAddress`+`getTransaction` (100 req/10s/IP) | no nonce — sig-count/last blockTime; `getTokenAccountsByOwner` balance | `getAccountInfo` (owner/executable) | n/a | n/a (program-specific) |
| Sui | GoPlus `/sui/token_security` top-10 keyless (live includes holders despite docs) | GraphQL `transactions(filter:{sentAddress})` + balanceChanges; publicnode `suix_queryTransactionBlocks` | GraphQL `address.balance(coinType)`; no nonce — tx count | `object(...)` / `sui_getObject` | events via GraphQL | n/a |
| Robinhood Chain | GoPlus 4663 keyless | RPC logs only (Blockscout instance Cloudflare-blocked from curl; no Etherscan V2) | `rpc.mainnet.chain.robinhood.com` | ✓ | 10k OK | add pool |
| TON | toncenter v3 `jetton/wallets` (sorted by balance) or tonapi `jettons/{m}/holders` keyless (1 rps) | toncenter v3 `jetton/transfers` | tonapi account | n/a | n/a | n/a |
| Cardano | Koios `asset_addresses` keyless (slow: ~21 s on NIGHT; 1000 rows/page) | Koios `asset_txs` | Koios address endpoints | n/a | n/a | n/a |
| Sei EVM | **BLIND keyless** (GoPlus code 2022 "main chain not supported"; Etherscan holders = PRO) — only RPC log replay | Etherscan V2 free (`chainid=1329`, key) | `evm-rpc.sei-apis.com` | ✓ | ≤2,000 blocks | add pool |
| Monad | GoPlus 143 keyless (APR: 5,020 holders) | Etherscan V2 free (`chainid=143`) | `rpc.monad.xyz` | ✓ | **≤100 blocks** (`-32614`) | add pool |
| Bitlayer | GoPlus 200901 keyless (BTR top-1 29.5%) | `api.btrscan.com/scan/api` Etherscan-compat `tokentx`/`txlist` keyless (no `tokenholderlist`) | `rpc.bitlayer.org` (`rpc.bitlayer-rpc.com` dead) | ✓ | 10k OK | add pool |
| Berachain | GoPlus 80094 keyless (BR top-1 41.9%) | Etherscan V2 free (`chainid=80094`) | `rpc.berachain.com` / publicnode | ✓ | 10k OK | add pool |
| Hemi | `explorer.hemi.xyz` Blockscout v2 holders keyless | same instance tokentx/v2 transfers | `rpc.hemi.network/rpc` | ✓ | 10k OK | add pool |
| Sonic | GoPlus 146 keyless | Etherscan V2 free (`chainid=146`) | `rpc.soniclabs.com` / publicnode | ✓ | 10k OK | add pool |
| Algorand | Nodely indexer `/v2/assets/{id}/balances` keyless | Nodely `/v2/assets/{id}/transactions` | Nodely account | n/a | n/a | n/a |
| Ontology (ONG) | **open** — `explorer.ont.io/v2` API answers but the ONG holder route wasn't found | open | open | – | – | – |
| GalaChain | **open** — no keyless source found (`explorer-api.gala.com` 404 at root) | open | open | – | – | – |
| Abstract / HyperEVM / World | GoPlus (Abstract 2741, World 480 listed) | Etherscan V2 free (2741 / 999 / 480) | chains.json RPCs | ✓ | untested | – |

---

## 3. Per-chain findings — citations + literal evidence

### 3.1 BSC (chain 56)

Board exposure: 33 primary-chain names incl. every Cat-A cluster name (SKYAI, LAB, BEAT, GUA, TUT, ACE, ASTER…).

**Explorer:** none keyless (unchanged from round 2).
- Etherscan V2 keyless probe: `GET https://api.etherscan.io/v2/api?chainid=56&module=account&action=tokentx&address=0x4efe…&page=1&offset=3` →
  `{"status":"0","message":"NOTOK","result":"Free API access is not supported for this chain. Please upgrade your api plan for full chain coverage. https://etherscan.io/apis"}`.
  Docs: https://docs.etherscan.io/supported-chains — "BNB Smart Chain Mainnet | 56 | Paid Tier Only". Pricing: https://etherscan.io/api/pricing — Lite $49/mo (5 cps, 100k/day, "All chains: Yes", "PRO endpoints: No"), Standard $199/mo (PRO endpoints incl. holder list). PRO list: https://docs.etherscan.io/api-pro/api-pro ("Get Token Holder List by Contract Address", "Get Top Token Holders", "Get Token Holder Count").
- BscScan V1: `GET https://api.bscscan.com/api?module=account&action=tokentx&…` → `"You are using a deprecated V1 endpoint, switch to Etherscan API V2 …"`.
- Blockscout: `https://bsc.blockscout.com/api/v2/...` → `404 default backend - 404`; `https://blockscout.com/bsc/mainnet/...` → `503 Service Unavailable`. (Blockscout rate-limit docs: https://docs.blockscout.com/devs/apis/requests-and-limits — "300 req/min (default)" per IP keyless; API key 10 rps; "per-instance API access will be deprecated soon".)

**Holders:** GoPlus keyless `GET https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses=0x92aa03137385f18539301349dcfc9ebc923ffb10` (SKYAI) →
`code=1 msg=OK holder_count=71024 n_holders_listed=10 top1={'address':'0xc882b111a75c0c657fc507c04fbfcd2cc984f071','percent':'0.3656…','is_contract':0,'is_locked':0}`.
Docs: https://docs.gopluslabs.io/reference/tokensecurityusingget_1 — `/api/v1/token_security/{chain_id}`, holders = "Top10 holders info"; the Authorization header is described ("Bearer <token>") but not marked required, and every keyless call in this audit returned data. Error table lists `4029 Request limit reached` (https://docs.gopluslabs.io/reference/api-status-code) — the keyless rate is undocumented; the desk already retries 429 with 1.2 s backoff. Supported-chains endpoint (keyless): `GET https://api.gopluslabs.io/api/v1/supported_chains` → 45 chains incl. `solana`, `tron`, `5000 Mantle`, `143 Monad`, `146 Sonic`, `80094 Berachain`, `200901 Bitlayer`, `4663 Robinhood`, `2741 Abstract`, `480 World Chain`, `169 Manta`, `81457 Blast`, `534352 Scroll`, `324 zkSync`. NOT listed: Sei (1329), Hemi (43111), TON, Cardano, Algorand, Sui (Sui has its own endpoint, §3.9).

**RPC (nonce/code/logs), live matrix on SKYAI Transfer logs:**
```
bsc-dataseed.binance.org      chainId=56 nonce OK code OK  getLogs 500/2000/10000 -> -32005 'limit exceeded' (all three)
bsc-rpc.publicnode.com        getLogs 500 OK n=11 · 2000 OK n=48 · 10000 -> HTTP403 "Archive requests require a personal token"
bsc.drpc.org                  HTTP429 "You reached Public endpoint rate limit, please upgrade to paid plan" (even eth_chainId)
bsc-mainnet.public.blastapi.io nonce/code OK · getLogs 500 -> HTTP429 "Your request has been rate-limited due to unusually…"
```
Consequence: the only keyless BSC log source that answered today is publicnode at ≤2,000 blocks/call. `local_index.RPC_PROVIDER_TABLE` doesn't list publicnode; `_PUBLIC_RPCS` does but `_getlogs_transfers` asks 50,000-block windows. Archive state for `stake_schedule` still depends on blastapi (memory `reference_bsc_archive_rpc`).

### 3.2 Ethereum (1)

- Etherscan V2 free tier: docs "Ethereum Mainnet | 1 | Free Tier Available"; https://docs.etherscan.io/rate-limits — free "3 calls/second, up to 100,000 calls/day" (note: `_ETHERSCAN_MIN_INTERVAL = 0.25` assumes 5 rps; the doc now says 3). Keyless probe → `"Missing/Invalid API Key"` (i.e. chain is free-tier; only the key is missing). `tokenholderlist` keyless → same message, but it is on the PRO list (paid).
- Blockscout keyless: `GET https://eth.blockscout.com/api/v2/tokens/0x57e1…6061/holders` → `200 n=50 top1={'address':'0xB805Ef84…','value':'1312361624809348143045558636'} next={…} hdrs={'x-ratelimit-limit':'180','x-ratelimit-remaining':'179'}`; `/api/v2/tokens/{a}` → `holders_count=98826`; `/api/v2/addresses/{a}/token-transfers` → 50 items + `next_page_params`; `/api/v2/addresses/{a}/counters` → `transactions_count`, `token_transfers_count` (a nonce-like liveness read for contracts, which `eth_getTransactionCount` can't give). Note `?limit=5` → 422 "Unexpected field" — pagination is cursor-only.
- RPC: `ethereum-rpc.publicnode.com` refuses `eth_getLogs` at 500 blocks (`HTTP403 "Archive requests require a personal token"`); `eth.drpc.org` 500/2000/10000 → `OK n=377 / 2026 / 9541`; `eth-mainnet.public.blastapi.io` → `HTTP400 "You can make eth_getLogs requests with up to a 10 bl[ock range]"`.

### 3.3 Base (8453)

- Etherscan V2: "Base Mainnet | 8453 | Paid Tier Only"; keyless probe → paid-only refusal.
- Blockscout: `/api/v2/tokens/{AERO}/transfers` and `/addresses/{a}/token-transfers` → 200 (50 items); `/tokens/{AERO}/holders` → `500 "Internal server error"` on three tries; ZORA holders → timeout. `holders_count=833502` via `/tokens/{a}`. Treat Blockscout-Base holders as unreliable; GoPlus (AERO holder_count 751,535, top-1 50.1%) works.
- RPC: `mainnet.base.org` 500 blocks OK n=7147; 2000 → `-32020 backend response too large`; `base.drpc.org` 2000 → `query exceeds max results 20000, retry with the range 50784086-50785570` (the error hints the safe range — usable for adaptive chunking); `base-rpc.publicnode.com` → archive-token refusal.

### 3.4 Arbitrum (42161) / Optimism (10) / Polygon (137) / Avalanche (43114)

- Etherscan V2 tiers (docs + keyless discriminator): Arbitrum **Free** (`Missing/Invalid API Key`), Polygon **Free** (docs; probe timed out), Optimism **Paid Tier Only**, Avalanche **Paid Tier Only**.
- Blockscout keyless holders 50/page: `arbitrum.blockscout.com` (ARB holders_count 2,389,278), `optimism.blockscout.com` (1,378,569), `polygon.blockscout.com` (416,453) — all 200 with `x-ratelimit-limit: 180`. `avalanche.blockscout.com` → 404 (no instance).
- RPC: `arb1.arbitrum.io/rpc` getLogs 10k OK (n=8179, 1.3 s); `mainnet.optimism.io` ≤500 (2000 → "backend response too large"); `polygon-rpc.com` → `HTTP401 {"error":"message: API key disabled, reason: tenant disabled …"}` (**dead; it is the second URL in `stake_schedule.RPC_POOL["polygon"]`**), `polygon-bor-rpc.publicnode.com` 2000 OK (8.6 s), 10k timeout; `api.avax.network` 10k → `maximum is set to 2048`, `avalanche-c-chain-rpc.publicnode.com` 10k OK n=25816.

### 3.5 Mantle (5000) — BILL, OPG, ENA

- Etherscan V2: "Mantle Mainnet | 5000 | Free Tier Available"; keyless → `Missing/Invalid API Key` (free with the desk's key; `mantlescan.xyz`).
- GoPlus: 5000 listed; ENA-on-Mantle returned `code=1` with **no holder block** (`holder_count=None, listed=0`) — partial coverage, verify per token.
- Blockscout `explorer.mantle.xyz` → `502 Bad Gateway` (all routes, today).
- RPC: `rpc.mantle.xyz` and `mantle-rpc.publicnode.com` chainId 5000, getLogs 10k OK.

### 3.6 Solana — FIDA, SKR, PUMP, JTO, TNSR, DRIFT, ARC, BIRB, DBR (+ SLX 98.5%, BLESS 91.8%, BMT 50.5%, PENGU, HOLO, ZAMA, ELIZAOS, HOME, GALA, XPON, PORTAL, ENA secondary)

- **Public RPC limits** (https://solana.com/docs/references/clusters): `https://api.mainnet-beta.solana.com` — "Maximum number of requests per 10 seconds per IP: 100 · … for a single RPC: 40 · Maximum concurrent connections per IP: 40 · Maximum amount of data per 30 seconds: 100 MB"; "The public RPC endpoints are not intended for production applications".
- Live, keyless, official endpoint (FIDA mint `Eches…Gnvp`):
  - `getTokenSupply` → `200 {"value":{"amount":"990910606055033","decimals":6,…}}`
  - `getSignaturesForAddress [mint,{limit:3}]` → `200 n=3 keys=[blockTime, confirmationStatus, err, memo, signature, slot, transactionIndex]`
  - `getTransaction [sig,{encoding:"jsonParsed",maxSupportedTransactionVersion:0}]` → `200 {blockTime, meta:{…innerInstructions, logMessages…}}` (the flow-decode path)
  - `getAccountInfo [mint,{encoding:"jsonParsed"}]` → `200 …"parsed":{"info":{"decimals":6,"freezeAuthority":null,…` (mint authority / owner = the "code" read)
  - `getTokenAccountsByOwner [owner,{mint},{jsonParsed}]` → `200` (balance read)
  - **`getTokenLargestAccounts` → `429 {"message":"Too many requests for a specific RPC call"}` with `retry-after: 10` on the first call and on three retries spaced 11 s, and again on a small mint (SKR) after 12 s** — this method is effectively closed on the public endpoint (consistent with round-2's "MARGINAL").
- Other keyless RPCs: `solana-rpc.publicnode.com` → `getSignaturesForAddress`/`getAccountInfo` OK, but `getTokenSupply`/`getTokenLargestAccounts` → `403 "Indexed requests require a personal token"` and `getTokenAccountsByOwner` → 429; `solana.drpc.org` → `"chain is not available on free plan"`; `rpc.ankr.com/solana` → 403 key required; `solana.leorpc.com/?api_key=FREE` → `getTokenLargestAccounts` **200 n=20** (top-1 `6b4AJbVm…` 249,525,157 FIDA) and `getTokenAccountsByOwner` OK, but `getSignaturesForAddress` → `-32603 Internal JSON-RPC error` (vendor literal key; usable as a top-20 fallback, not as a dependency).
- **Holders keyless: GoPlus** `GET https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses=Eches…` → `code=1 holder_count=50196 n_holders_listed=10 top1={'account':'6b4aypBh…','percent':'0.2518','token_account':'6b4AJbVm…','is_locked':0}` (matches leorpc's top-1). Docs https://docs.gopluslabs.io/reference/solanatokensecurityusingget say Authorization "Bearer <token>" and "List of top 10 addresses" — the keyless call worked regardless; treat the doc/live mismatch as a tier that could tighten.
- Keyed options: Helius (key already in `config/secrets.json`; https://www.helius.dev/pricing — Free "$0/month", "1M credits", "10 Requests / sec", DAS "2/sec"; enhanced-transactions API not in free tier). Solscan: `pro-api.solscan.io/v2.0/token/holders` keyless → `401 {"message":"Token is missing"}`; `public-api.solscan.io` → 404; Lite plan https://docs.solscan.io/solscan-api/solscan-api-lite-plan.md "$49 / month", "1,000 Requests / 60 seconds", references a "standard Free tier" without numbers. SolanaFM → 502.
- Nonce equivalent: Solana accounts have no nonce; liveness = newest `getSignaturesForAddress` blockTime / count delta (the parts-bin script already does this).

### 3.7 Sui — MAGMA, MMT, US (+ TAKE 92.8%)

- **JSON-RPC on the foundation fullnode is gone.** `POST https://fullnode.mainnet.sui.io:443 suix_getCoinMetadata` → `{"error":{"code":-32601,"message":"Method not found. JSON-RPC on public fullnodes has been deprecated. Please migrate to gRPC or GraphQL endpoints. See https://docs.sui.io/develop/accessing-data/json-rpc-migration …"}}` (same for `sui_getObject`, `suix_getBalance`, `suix_queryTransactionBlocks`, `sui_getLatestCheckpointSequenceNumber`). Docs (that URL): "Disable JSON-RPC on Sui Foundation's mainnet full nodes … Week of July 27, 2026", full removal "Mid-October 2026"; "The public URLs at `https://fullnode.<network>.sui.io` and `https://graphql.<network>.sui.io/graphql` are rate-limited and intended for development and public-good access" (no numbers; https://docs.sui.io/concepts/graphql-rpc says limits are exposed via `Query.serviceConfig`).
- **GraphQL keyless works** (`POST https://graphql.mainnet.sui.io/graphql`): schema introspected live — `Query.transactions(filter:{sentAddress|affectedAddress|affectedObject|function|kind|afterCheckpoint|…})`, `Address.balance(coinType)`, `Address.transactions`, `Query.coinMetadata`, `Query.events`. Query
  `{ coinMetadata(coinType:"0x9f85…::magma::MAGMA"){ decimals name symbol supply } address(address:"0x8e01…f2a"){ balance(coinType:"…MAGMA"){ totalBalance } } transactions(last:2, filter:{ sentAddress:"0x8e01…" }){ nodes{ digest effects{ timestamp } } } }` →
  `{"data":{"coinMetadata":{"decimals":9,"name":"Magma Token","symbol":"MAGMA","supply":"1000000000000000000"},"address":{"balance":{"totalBalance":"510000000000000000"}},"transactions":{"nodes":[{"digest":"EQyGoZwD…","effects":{"timestamp":"2026-03-30T03:32:38.713Z"}}…`
  and `transactions(filter:{affectedAddress})` with `effects.balanceChanges{ owner amount coinType }` → 200 (the flow read: per-tx balance deltas per owner per coinType).
- **Legacy JSON-RPC still served keyless by PublicNode**: `https://sui-rpc.publicnode.com` → `suix_getCoinMetadata` 200, `suix_getTotalSupply` 200 (`1000000000000000000`), `sui_getObject(pkg)` 200, `suix_getBalance(top1, MAGMA)` 200 (`coinObjectCount:2`), `suix_queryTransactionBlocks({FromAddress})` 200, `({InputObject: pkg})` 200 hasNext, `suix_getOwnedObjects` 200. `sui-mainnet.public.blastapi.io` → `403 "Blast API is no longer available. Please update your integration to use Alchemy's API"`.
- **Holders keyless: GoPlus** `GET https://api.gopluslabs.io/api/v1/sui/token_security?contract_addresses=0x9f85…::magma::MAGMA` → `code=1 holder_count=17854 n_holders_listed=10 top1={'address':'0x8e01c52e…','balance':'510000000.0','percent':'0.51','tag':None}` — MAGMA top-1 holds **51.0%** and GraphQL confirms the balance. Docs (https://docs.gopluslabs.io/reference/suitokensecurityusingget) list no holders field — the live response has one; another doc/live mismatch to re-verify before a spec leans on it.
- Full holder list = keyed: BlockVision `GET https://api.blockvision.org/v2/sui/coin/holders?coinType=…` keyless → `403 {"code":-32002,"message":"apikey must"}`; docs https://docs.blockvision.org/reference/retrieve-coin-holders.md — "The Coins Holder API on Sui is currently exclusively available to Pro Members" (30 trial calls); plans https://docs.blockvision.org/reference/rate-limits-and-compute-units.md — Free "$0/Month" 10M CU, Lite $29, Basic $99, Pro $199. Blockberry `GET https://api.blockberry.one/sui/v1/coins/{coinType}/holders` keyless → `401 Unauthorized`; docs https://docs.blockberry.one/reference/getholdersbycointype.md — `x-api-key` required, "This is the Pro endpoint, currently available for free as part of the API campaign". `suiscan.xyz` backend → 407.
- Nonce equivalent: none; use `Address.transactions` count / newest timestamp. Contract probe: `object(address)` / `sui_getObject` (package objects have `version:"1"`, type `package`).

### 3.8 Robinhood Chain (4663) — CASHCAT, PONS

- chains.json (https://chainid.network/chains.json, ethereum-lists): rpc `https://rpc.mainnet.chain.robinhood.com`, `https://robinhood-rpc.publicnode.com`; explorers `robinscan.io`, `robinhoodchain.blockscout.com`. CoinGecko `asset_platforms` (keyless): `('robinhood', 4663, 'ethereum')`.
- Live: `rpc.mainnet.chain.robinhood.com` → `chainId=0x1237 head=52675930 code_len=9662 nonce=0x1 getLogs10k=2194` (CASHCAT `0x020bfc…`); publicnode → archive-token refusal on logs.
- Holders: GoPlus `token_security/4663` → `code=1 holder_count=93191 listed=10 top1_pct=0.00194` — keyless works.
- Explorer: `robinhoodchain.blockscout.com/api/v2/...` → `403` Cloudflare "Just a moment…" challenge from curl (not a real 403; browser-only). Not in Etherscan V2 (`Missing or unsupported chainid parameter`).

### 3.9 TON — TAC (75.8%), EVAA (40%), ENA secondary

- toncenter v3 keyless (https://toncenter.com/ — "Using API without API key is limited to 1 request per second"; v3 docs at `/api/v3/`):
  `GET https://toncenter.com/api/v3/jetton/masters?address=EQBKMfjX…gVBp&limit=1` → `200 {"jetton_masters":[{"address":"0:4A31…","total_supply":"19045859787500000","mintable":true,"admin_address":"0:EEED…"`;
  `GET …/api/v3/jetton/wallets?jetton_address=EQBK…&limit=3&sort=desc` → `200 {"jetton_wallets":[{"address":"0:ECC8…","balance":"5246060876000000","owner":"0:06F9…"` (holders sorted by balance);
  `GET …/api/v3/jetton/transfers?jetton_master=EQBK…&limit=2&sort=desc` → `200 {"jetton_transfers":[{"query_id":…,"source":"0:2B5B…","destination":"0:A2CA…"` (flow).
- tonapi keyless: `GET https://tonapi.io/v2/jettons/{master}/holders?limit=3` → `200 {"addresses":[{"address":"0:ecc8…","owner":{"address":"0:06f9…","is_wallet":true},"balance":"5246060876000000"}…` (6.9 s); `/v2/jettons/{master}` → `200 {"mintable":true,"total_supply":…,"admin":{…}}`. Landing https://tonapi.io/ lists "API Start Free RPS 1 · Lite $9.9 RPS 10 · Standart $95 RPS 100".

### 3.10 Cardano — NIGHT (97.8%), ID, FET

- Koios keyless (OpenAPI https://api.koios.rest/koiosapi.yaml §Limits: "Burst Limit: A single IP can query an endpoint up to 100 times within 10 seconds … 429 … 60 seconds"; "paginated by 1000 records"; "public tier remains unauthenticated"; Free tier with Bearer token linked to a wallet):
  `GET https://api.koios.rest/api/v1/asset_info?_asset_policy=0691b2…af1fa&_asset_name=4e49474854` → `200 [{"policy_id":…,"asset_name_ascii":"NIGHT","fingerprint":"asset1wd3…","minting_tx_h…`;
  `asset_txs?…&_after_block_height=11000000&limit=2` → 200 (8.8 s);
  `asset_addresses?…` with `Range: 0-2` → `200 [{"payment_address":"Ae2tdPwUPEYvzsX…","quantity":"11149547374"}…` `content-range: 0-2/*` (**21.7 s**; a plain 20 s call timed out — NIGHT has ~1.16M txs per `asset_summary`);
  `asset_summary` → `200 {"total_transactions":1158795,"staked_wal…` (21.5 s).
  Note: the config stores the Cardano "contract" as `policy_id||hex(asset_name)` — Koios wants them split.
- Blockfrost keyless → `403 "Missing project token. Please include project_id"`; https://blockfrost.dev/overview/plans-and-billing — STARTER "free forever" (numbers only in an image, not quoted).

### 3.11 Algorand — FOLKS (ASA 3203964481)

- Nodely keyless (docs pages are JS-rendered and returned nothing quotable; the live calls are the evidence):
  `GET https://mainnet-idx.4160.nodely.dev/v2/assets/3203964481/balances?limit=3&currency-greater-than=0` → `200 {"balances":[{"address":"ABNUV3X3…","amount":427162132,…}]}`;
  `GET https://mainnet-api.4160.nodely.dev/v2/assets/3203964481` → `200 {"index":3203964481,"params":{"creator":"RKBPWO3M…","manager":…`;
  `GET https://mainnet-idx.algonode.cloud/v2/assets/3203964481/transactions?limit=2` → 200 with `next-token`.

### 3.12 Sei EVM (1329) — CLO (77.7%), FOLKS

- RPC: `evm-rpc.sei-apis.com` and `sei-evm-rpc.publicnode.com` chainId 1329; getLogs 2001 blocks → `-32000 block range too large (2001), maximum allowed is 2000 blocks`.
- GoPlus `token_security/1329` → `code=2022 "The main chain is not supported"` → **no keyless holder list**. Etherscan V2: docs "Sei Mainnet | 1329 | Free Tier Available"; keyless tokentx → `Missing/Invalid API Key` (works with the desk's key for transfers; holder list is PRO). `seitrace.com` → 521 (down at test time); `seiscan.io` API → HTML error page.

### 3.13 Monad (143) — FOLKS, APR

- RPC `rpc.monad.xyz` chainId 143; `eth_getLogs` → `HTTP413 -32614 "eth_getLogs is limited to a 100 range"` (any window > 100 blocks); `monad-rpc.publicnode.com` → 404.
- GoPlus 143 → `holder_count=5020 listed=10 top1_pct=0.0808` (APR). Etherscan V2 "Monad Mainnet | 143 | Free Tier Available" (keyless → `Missing/Invalid API Key`). `monad.blockscout.com` → 404; `monadvision.com` API → Cloudflare challenge. BlockVision also lists a keyed Monad token-holders API.

### 3.14 Bitlayer (200901) — BTR (62.3%)

- RPC `rpc.bitlayer.org` getLogs 10k OK (n=2399); `rpc.bitlayer-rpc.com` → 401 "tenant disabled".
- GoPlus 200901 → `holder_count=15487 top1_pct=0.295`. btrscan Etherscan-compat API keyless: `GET https://api.btrscan.com/scan/api?module=account&action=tokentx&contractaddress=0x0e4c…&page=1&offset=1` → `{"status":1,"message":"OK","result":[{"blockNumber":"25361643","timeStamp":"1788362302",…`; `txlist` OK; `tokenholderlist` → `{"code":4404,"message":"No handler found for GET /inner/tokenholderlist"}`. Not in Etherscan V2 (`unsupported chainid`).

### 3.15 Berachain (80094) — BR, STG, EUL

- RPC `rpc.berachain.com` / publicnode chainId 80094, getLogs 10k OK. GoPlus → `holder_count=6885 top1_pct=0.419` (BR). Etherscan V2 "Berachain Mainnet | 80094 | Free Tier Available" (berascan). `berachain.blockscout.com` → 404.

### 3.16 Hemi (43111) — HEMI

- RPC `rpc.hemi.network/rpc` getLogs 10k OK (n=26536, 7.8 s). Blockscout `explorer.hemi.xyz` keyless: `/api/v2/tokens/{a}/holders` → 200 (50 items, `address/token/value`), `/tokens/{a}/transfers` 200, `/addresses/{a}/counters` 200, Etherscan-compat `?module=account&action=tokentx` 200. GoPlus → 2022 unsupported; not in Etherscan V2.

### 3.17 Sonic (146) — OGN, EUL

- RPC `rpc.soniclabs.com` / publicnode chainId 146, getLogs 10k OK. GoPlus 146 → `holder_count=249 top1_pct=0.809` (OGN-on-Sonic is a bridge stub). Etherscan V2 "Sonic Mainnet | 146 | Free Tier Available" (sonicscan). `sonic.blockscout.com` → 404.

### 3.18 Ontology (ONG, watchlist `squeeze_exhaust_watch`, not in tracked_wallets)

- CoinGecko keyless `/coins/ong` → `platforms={'ontology': ''}` (native gas asset, no contract). `GET https://explorer.ont.io/v2/latest-blocks?count=1` → `200 {"code":0,"msg":"SUCCESS",…}` so the explorer API is live and keyless, but my guessed holder route returned `400 queryTokenDetail.tokenType: Incorrect token type` — route unknown, left open.

### 3.19 GalaChain (GALA 9.7%) — no keyless source found (`explorer-api.gala.com/` → 404 root). Open.

### 3.20 Cross-cutting vendor facts (re-verified 2026-09-02)

- Moralis: https://moralis.com/pricing shows no free plan; cheapest Starter "$149 per month, billed annually", "2 million Compute Units per month", "40 RPS". Desk state `state/moralis_quota.json` day 2026-08-31: 60 calls (59 from `brief`).
- Etherscan free key: 3 cps / 100k/day across *all* free-tier chains on one key (https://docs.etherscan.io/rate-limits); `_ETHERSCAN_MIN_INTERVAL` should be ≥0.34 s if the key is shared across chains.
- PublicNode: every "archive"/"indexed" refusal above is a `-32602` with the literal text "…require a personal token. Get one at: https://www.allnodes.com/publicnode" (the page itself is JS-only; the error text is the primary evidence).
- Resolver failures (`onboard_failed:resolve_ambiguous`, TUT/ACE) are CoinGecko symbol collisions, not chain gaps — both were onboarded via `--contract --chain bsc` (SPEC-165). LA (watchlist, unmapped) resolves keyless to Lagrange `ethereum 0x0fc2…` + `bsc 0x389a…`, i.e. readable today once onboarded.

---

## 4. Recommended source per read type per chain (for whoever files the spec — not a code proposal)

| Chain | Holders (top-N) | Flow / transfers | Nonce / balance | Code / probe | Logs window | Cost |
|---|---|---|---|---|---|---|
| BSC | GoPlus top-10 (keyless) · >10: Bitquery key (800/day budget already in config) or Etherscan Standard $199 | local_index over `bsc-rpc.publicnode.com` ≤2k blocks → Moralis | publicnode / dataseed | same | publicnode ≤2k | $0 (status quo) |
| Ethereum | Blockscout v2 holders (50/page, 180/window) + GoPlus | Etherscan V2 (key) → Blockscout → Moralis | publicnode | same | `eth.drpc.org` 10k | $0 |
| Base | GoPlus (Blockscout holders flaky) | Blockscout tokentx → Moralis | mainnet.base.org | same | `base.drpc.org` w/ hinted range | $0 |
| Arbitrum / Polygon | Blockscout v2 holders | **Etherscan V2 free tier (add chainids 42161/137)** → Blockscout → Moralis | publicnode (drop `polygon-rpc.com`) | same | arb1 10k / publicnode 2k | $0 |
| Optimism | Blockscout v2 holders | Blockscout tokentx → Moralis (Etherscan paid) | mainnet.optimism.io | same | ≤500 | $0 |
| Avalanche | GoPlus | Moralis only (Etherscan paid, no Blockscout) | api.avax.network | same | publicnode 10k | $0 / $49 Lite |
| Mantle, Berachain, Monad, Sonic, Sei, Abstract, HyperEVM, World | GoPlus (all but Sei) | **Etherscan V2 free tier with the existing key** | chains.json RPCs (verified) | same | 10k except Monad ≤100, Sei ≤2k | $0 |
| Bitlayer | GoPlus | `api.btrscan.com/scan/api` Etherscan-compat keyless | rpc.bitlayer.org | same | 10k | $0 |
| Hemi | `explorer.hemi.xyz` Blockscout v2 | same instance | rpc.hemi.network | same | 10k | $0 |
| Robinhood Chain | GoPlus 4663 | RPC log replay (no keyless explorer from curl) | rpc.mainnet.chain.robinhood.com | same | 10k | $0 |
| Solana | GoPlus `/solana/token_security` top-10 → Helius DAS `getTokenAccounts` (free key in secrets) → leorpc top-20 fallback | official RPC `getSignaturesForAddress` + `getTransaction` (≤100 req/10 s, ≤40/method) | `getTokenAccountsByOwner`; liveness = newest signature | `getAccountInfo` | n/a | $0 |
| Sui | GoPlus `/sui/token_security` top-10 → BlockVision/Blockberry (keyed) for full list | GraphQL `transactions(filter:{affectedAddress})` + balanceChanges; publicnode `suix_queryTransactionBlocks` as fallback | GraphQL `address.balance(coinType)` | `object()` | GraphQL `events` | $0 |
| TON | toncenter v3 `jetton/wallets` (1 rps keyless) / tonapi holders | toncenter v3 `jetton/transfers` | tonapi account | – | – | $0 |
| Cardano | Koios `asset_addresses` (Range header paging; expect 20 s+ on large assets) | Koios `asset_txs` | Koios | – | – | $0 |
| Algorand | Nodely indexer `assets/{id}/balances` | Nodely `assets/{id}/transactions` | Nodely | – | – | $0 |

Paid ladder if the desk ever wants lifetime BSC flow without Moralis: Etherscan Lite $49/mo (BSC/Base/OP/Avax `tokentx`, 5 cps, 100k/day, no holder list) → Standard $199/mo (adds `tokenholderlist` everywhere) vs Moralis Starter $149/mo. Per CLAUDE.md §9 this stays decoupled from GO (only on ≥3 quota-dead evenings in 30 d).

---

## 5. Open questions

1. **GoPlus keyless tier is undocumented** — docs mark Solana/Sui as Bearer-token endpoints and list error `4029 Request limit reached`, yet every keyless call here returned data. Before a spec depends on GoPlus for 10+ chains, measure the keyless ceiling (burst N calls, record the first 429/4029) and re-verify the Sui `holders` field, which the docs don't list.
2. **Sui GraphQL rate limits** are "rate-limited" without numbers (docs); read `Query.serviceConfig` live and record `maxQueryDepth`/`maxPageSize`/timeouts before the adapter is written. Also decide whether to rely on `sui-rpc.publicnode.com` legacy JSON-RPC (works today; the upstream code is being removed mid-October 2026).
3. **Solana holder depth beyond top-10** keyless is unsolved (`getTokenLargestAccounts` 429 on the public RPC in every attempt; leorpc is a vendor's public literal key). Helius free tier (key present) is the honest answer — confirm the DAS `getTokenAccounts` credit cost against 1M/month before committing.
4. **Sei holders** — no keyless holder list found (GoPlus unsupported, Etherscan holders PRO, seitrace down); only RPC log replay at ≤2k blocks/call. CLO's 77.7% Sei supply stays effectively unreadable for concentration.
5. **Monad `eth_getLogs` ≤100 blocks** makes log replay ~100× more calls than other chains; Etherscan V2 free tier is the practical flow path there — verify with the real key (not done here, keyless only).
6. **Ontology (ONG) and GalaChain (GALA)** — no holder/flow route located; both are small board exposures.
7. **Docs I could not quote** (JS-rendered / 404): Nodely free-tier limits, Blockfrost STARTER numbers, TONAPI "Free TONAPI limits" page, Solscan free tier, PublicNode personal-token tier, Blockberry pricing. Live calls above stand as the evidence; re-fetch these pages in a browser before pricing anything on them.
8. **Two dead URLs already in the desk's pools**: `https://polygon-rpc.com` (401 tenant disabled; `stake_schedule.RPC_POOL["polygon"][1]`) and `bsc.drpc.org` public tier (429 on `eth_chainId`; `local_index.RPC_PROVIDER_TABLE` position 1) — both silently fall through today, but they cost timeouts on every read.
9. **Etherscan free-tier rate** is now documented as 3 cps (the code paces at 4 cps assuming 5) — a spec that widens `_ETHERSCAN_CHAINID` to ten chains on one key should also drop the pace to ≤3 cps and raise the shared daily budget model (`provider_quota.DEFAULT_BUDGETS["etherscan"] = 20_000`) per chain, not per call.

Probe scripts (not committed): scratchpad `evm_matrix.py`, `explorers.py`, `nonevm.py`, `batchd.py`, `batche.py`, `batchf.py`, `batchg.py` — each prints the literal status + first ~300 bytes of every response quoted above.
