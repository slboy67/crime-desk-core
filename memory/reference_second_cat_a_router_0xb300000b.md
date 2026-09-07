---
name: reference_second_cat_a_router_0xb300000b
description: "SECOND multi-Cat-A swap router 0xb300000b72... (180-byte proxy). Discovered 2026-05-27 via GUA team-distribution forensics. Mirror function of 0x238a3588 (router-1, 8347 bytes) — handles GUA/BILL/BSB/ESPORTS/BLUAI/BEAT + 22 more tokens. The apparatus uses 2+ routers in parallel."
metadata: 
  node_type: memory
  type: reference
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

**Address:** `0xb300000b72deaeb607a12d5f54773d1c19c7028d` (BSC)
**Code:** 180-byte proxy contract (vs 8347 bytes for router-1). Storage slot `0x5e12654f390e4153c4f63b3dfcc122cf7876a5cdfb496dccf7284c10517a35c5` — custom slot for impl address, EIP-1967-style pattern (small delegatecall router)
**BNB balance:** 1.247 BNB (gas)
**Discovered:** 2026-05-27 via user GUA whale tip — traced cash-out chunks from `0xa3a6f791` conduit landing here in 35× 3,000-GUA transfers.

**7-day flow snapshot (Moralis):**
| Token | IN | OUT | Sum |
|---|---|---|---|
| USDT | 209 | 234 | 178,975 (balanced) |
| quq | 24 | 25 | 44.3M (balanced) |
| BILL | 16 | 17 | 14,576 |
| GUA | 17 | 19 | 3,087 |
| BSB | 5 | 5 | 1,511 |
| ESPORTS | 4 | 4 | 7,176 |
| BLUAI | 2 | 2 | 6,062 |
| Beat (BEAT) | 4 | 3 | 285 |
| HDBANK, NEX, SHARE, WoD, SLX, B2, OPG, 42, SERAPH, $SUP, ST, ZEST, FOREST, PUP, WARD, BOS, CLO | balanced | balanced | various |

**Mostly balanced IN/OUT counts** = router pattern, not vault. Lower volume than router-1 (`0x238a3588`) but same architecture.

**Why two routers?**
- Splits operational risk (one paused/sanctioned doesn't kill the other)
- Different fee/path optimization per router (router-1 8347 bytes = full custom logic; router-2 180 bytes = small delegatecall to a centralized impl that can be upgraded)
- May serve different MM clients or different parts of the cluster

**Surveillance value (apparatus-tier monitor):**
1. Spike in router-2 throughput on a single token = team distribution active on that token (confirmed pattern on GUA 2026-05-27)
2. New tokens appearing in router-2 = new Cat A's coming online
3. Both routers fingering the same destination = single buyer/MM client identifiable

**Linked artifacts:**
- Added to GUA / BILL / BSB / ESPORTS / BLUAI / BEAT in `config/tracked_wallets.json` as `ROUTER-2-MULTI-CAT-A` (tier: op)
- Sister to [[reference_multi_cat_a_router_0x238a3588]] (router-1)
- The 0xa3a6f791 conduit + 0xa9929A0 team wallet chain documented in [[project_gua_team_distribution_chain_2026_05_27]]

**Open thread:**
- Decode the impl behind the EIP-1967 storage slot — that's the actual swap logic
- Cross-reference 0xb300000b's IN counterparties with our team-wallet maps — which other "team-seeded sub-wallets" send here?
- Owner / proxy admin not yet known
