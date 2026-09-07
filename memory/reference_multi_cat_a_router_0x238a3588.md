---
name: reference_multi_cat_a_router_0x238a3588
description: "0x238a358808379702088667322f80ac48bad5e6c4 — apparatus-tier MULTI-TOKEN SWAP ROUTER (NOT a staking lock). 8347-byte custom contract, owner 0xfa206dab, handles 22+ tokens including stablecoins + most mapped Cat A's. Distinct from 0x73d8bd54 escrow but same surveillance value. Discovered 2026-05-27 during ZAMA+KITE+COLLECT mapping."
metadata: 
  node_type: memory
  type: reference
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

**Address:** `0x238a358808379702088667322f80ac48bad5e6c4` (BSC)
**Owner:** `0xfa206dab60c014beb6833004d8848910165e6047`
**Bytecode:** 8347 bytes (mid-size custom contract, non-standard ERC interface — name/symbol/totalSupply revert; only owner() resolves cleanly)
**Function selectors in dispatch:** 0x01ffc9a7, 0x0b0d9c09, 0x11da60b4, 0x15dacbea, 0x17a1d80f, 0x322c3620, 0x36223ce9 (7 functions; non-standard interface)

**What it does (per Moralis 30d transfer flow):**
- 22+ distinct tokens flow IN/OUT, mostly BALANCED counts (router behavior, not vault)
- Stablecoins: USDT 171in/182out, USDC 64in/73out, USD1
- Mapped Cat A's: **BEAT, BSB, BILL, RIVER, COLLECT, SPACE, ZBT** — almost the entire watchlist
- Other tokens routed: SLX, NEX (524M in / 3.4B out — biggest token volume), TRIA, ACU, ZEST, CLO, DBT, BABYSHARK, UAI, BASED
- WBNB flows + 煎饼大朗 (Chinese-named token) suggest it routes across narrative/meme variants

**What it ISN'T:**
- NOT a staking lock contract (originally misclassified by `holders.py` heuristic because it held large supply % across multiple tokens). Probe of `unstake/stake/withdraw/deposit/userStakes` selectors all REVERT (not exist).
- NOT an EIP-1967 proxy (no slot-0 reveal)
- NOT an ERC721 staking pattern

**What it likely IS:**
- A **multi-token swap router / MM execution layer** used by the same operator across the Cat A cluster
- Function-distinct from `0x73d8bd54` escrow (which HOLDS positions): this one ROUTES flow
- Different selector dispatch from PancakeRouter / OneInch / standard aggregators → custom router

**Tokens holding positions in it (from holders.py top-10 sweeps):**
- ZAMA: 5.40% (BSC top-5)
- KITE: 1.14% (BSC top-7)
- COLLECT: 0.39% (BSC top-10)

**Surveillance value (treat as apparatus-tier monitor):**
1. Watch outbound to CEX hot wallets — that's the actual distribution path
2. Watch the owner `0xfa206dab` for any direct transactions (control wallet)
3. Cross-reference its IN/OUT bursts with price moves on the mapped Cat A's — confirms whether router fires before/after pumps

**Actions taken:**
- Reclassified in `config/tracked_wallets.json`: `staking_lock` → `op`, relabeled `MULTI-CAT-A-ROUTER`
- Note: this is the kind of cross-token apparatus discovery [[feedback_river_lesson_methodology_not_token]] points at — methodology > token. The router is the asset, not any one Cat A on it.

**Open thread:**
- Owner `0xfa206dab60c014beb6833004d8848910165e6047` needs an activity_audit pass when chain pool extended (BSC not currently in pool per [[reference_bsc_archive_rpc]]).
- Decode the 7 function selectors against 4byte.directory to understand the actual swap semantics — pending.
- Compare with 0x73d8bd54 escrow's implementation 0x1bb4a42d ([[reference_cross_cat_a_escrow_0x73d8bd54]]) — same operator likely deployed both.

Related: [[reference_cross_cat_a_escrow_0x73d8bd54]] (the holding-side companion), [[project_scan_2026_05_27]] (discovery), [[feedback_arkham_xtoken_mm_is_bitget_hot]] (broader apparatus-tier cluster context)
