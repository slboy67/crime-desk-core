---
name: feedback_identical_size_fanout_is_predistribution_staging
description: "One source → N fresh wallets, each the SAME round size, within minutes = supply being staged below whale-alert thresholds; the sell follows from any leaf to a CEX. Pre-hoc form of the VELVET rotating-wallet lesson. Seen RIVER/CLO/BEAT/SKR/AKE in ten days (2026-08-26→09-03), all observed after the fact — hypothesis-tier n=4."
metadata:
  type: feedback
---

# Identical-size fan-out = pre-distribution staging (2026-09-04)

**Rule:** when a tracked/suspect wallet splits a bag into N fresh wallets of the **same round
size** within minutes (a script, not a human), read it as **supply being staged for sale** —
chunked below whale-alert thresholds, ready to drip from any leaf into a CEX. The name is
**WORKING** from that moment (§0.6 veto): no long into the bounce, short only on a confirmed
breakdown-hold.

**Instances (all from the @Proxonchain review, `reports/RESEARCH-2026-09-04-proxonchain-method.md`
§3; outcomes on Binance 4h bars):**

| Name | Shape | Then |
|---|---|---|
| RIVER 08-27→30 | 1.08M sliced into 100K blocks across 11 fresh wallets over 72h; Gnosis safe → 500K to a fresh wallet as the shelf broke | 1.706 → 1.427 in 15h, 1.26 by 09-02 (−25%) |
| BEAT 08-29 | Gnosis safe released 30M to 3 wallets; one chopped 8.8M into 9 wallets in 2 min (1.2M×2, 1.0M×4, 0.8M×3); 999.8K → KuCoin | 0.1596 → 0.121 by 08-30 (−24%) |
| SKR 08-31 | 100K slices every minute; 35–41K deposits trickling into Gate | 0.0285 → 0.0187 by 09-03 (−34%) |
| CLO 08-31 | 10.0M → 93 clips of 85–105K (the desk verified this at 08-31 13:37Z, commit 72d6f05, 40h before CT) | +16% first (0.17 → 0.198), then 0.132 (−22%) |
| AKE 08-29 | 80+ wallets × ~210M each within an hour (>17B) | +430% blowoff 09-02 then −70% next bar — staging PRECEDED the pump; the shape says "supply is ready", not "down now" |

**Why:** the operator needs the float in many hands before the exit so no single deposit trips an
alert and the drip can rotate wallets (the VELVET DWF-cycle lesson,
[[feedback_rotating_wallets_defeat_single_wallet_verify_dwf_cycle]] — this is what that rotation
looks like BEFORE the first leg). The mega-safe stays pristine; this is the working layer firing
([[feedback_megasafe_is_stock_not_flow]]).

**How to apply:**
- When `verify_wallet` / `trace_tree` shows the shape on a board name: persist the **hub + top
  leaves** (full addresses) in `config/tracked_wallets.json` with SPEC-126 balance-delta tripwires
  ([[feedback_persist_full_addresses_of_verified_wallets]]); flag the thesis WORKING.
- It is a **veto and a tripwire, never a short entry**: CLO ran +16% and AKE +430% AFTER the
  fan-out. Entry stays the breakdown-hold ([[feedback_squeezer_bounce_entry_costs_the_whole_move]]).
- The SELL is the leaf → CEX deposit or the DEX swap (check the stable leg,
  [[feedback_check_stable_leg_before_calling_transfers_not_sells]]). Until a leaf moves, it is
  positioning ([[feedback_predictor_vs_cause_dex_sale_is_execution]]).
- Hypothesis-tier: n=4 bearish resolutions + 1 pump-first, every one read after the fact. Update
  this table each time the shape resolves; nothing here changes size.
