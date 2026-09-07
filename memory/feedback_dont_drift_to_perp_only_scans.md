---
name: feedback_dont_drift_to_perp_only_scans
description: "Every token scan must include the on-chain layer (or explicitly flag it as blank); don't drift to perp-only verdicts because flows.py is slow"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

When running token scans (`$TICKER` requests, triage drill-downs, new discoveries), the on-chain layer is mandatory — even if `flows.py` is slow or unreliable on BSC, there are reliable alternatives that MUST be checked routinely:

**Why:** User called out 2026-05-24 — the last ~10 scans (BSB, BEAT, AGT, B2, GMT, IN, PLAY, recheck cycles) had drifted to **perp-only verdicts**, signaled around with "no wallet map = Layer 1 blind" rather than fixing the gap. This violates [[feedback_full_analysis_always]] (check X = full 5-layer pull) and is exactly the kind of half-blind verdict the playbook Section 13 hard-rule forbids ("never issue a directional verdict before completing the 5-layer pull"). The lapse cost real signal: BSB faked-OI lesson, ESPORTS regime read, EDEN whipsaw — all would have been clearer with on-chain confirmation routinely.

**How to apply:**
- **Default routine for any `$TICKER` request:**
  1. **First: check `config/tracked_wallets.json` for the ticker.** If mapped → read curated `_note` fields BEFORE running tools (often the most current intel, [[feedback_verify_wallet_history_not_just_balance]]).
  2. **For mapped tokens, run `safe_audit.py <TICKER>`** — slow but reliable, gives CEX-vs-internal lifetime classification, doesn't depend on flaky BSC getLogs.
  3. **For active monitoring, prefer `nonce_watch.py`** over flows.py — `eth_getTransactionCount` is a tiny reliable call per address, no range/timeout issues ([[feedback_flows_bsc_rpc_unreliable_for_getlogs]]).
  4. **For unmapped tokens** (IN, AGT, BEAT, etc.) — **flag Layer 1 as BLANK *explicitly* in the verdict**, and OFFER to build a quick map with `holders.py <contract>` if liquidity warrants the work (>$25M vol).
- **Stop using "no wallet map" as a skip excuse.** Either build it, or downgrade the verdict confidence to "perp-only, half-blind" so the user knows what they're getting.
- **Triage drill-downs MUST include the on-chain layer** — that's the whole point of moving from triage (4-row summary) to a "drill" (full picture).

**Second offense + behavioral correction (2026-05-27):** Same pattern repeated across MYX, LIGHT, WLD, LUNC, DRIFT, REQ in one session — I kept *asking* "want me to build the L1 map?" instead of just running `holders.py`. The "want me to" question IS the drift, just dressed up politely. User called it directly: "why you never do L1?"

**Updated behavioral rule (supersedes "OFFER to build" above):** for any `$TICKER` request, if the token has an EVM contract and isn't already mapped, **run `holders.py --chain <chain>` BEFORE delivering the verdict.** No "want me to" question. It takes <30 seconds via GoPlus, dies cleanly if the token isn't EVM, and frequently reveals decisive intel that perp data alone can't show.

**The cost of asking-first is silent** — there's no "yes" coming when the user is doing other things, and the data gap propagates into a half-blind verdict. Same family as [[feedback_auto_arm_named_triggers]] (auto-arm price alerts, don't ask).

**The LIGHT case 2026-05-27 is the validation:** 30-second L1 build revealed 4 of 10 top holders are shared with SKYAI, confirming LIGHT is in the same operator cluster (cross-Cat-A escrow + 2 shared CEX/MM hot wallets + 1 cross-token MEV bot). Meaningful cross-token risk that the perp-only read completely missed. Same pattern likely on every EVM Cat A — if I routinely built maps instead of asking, the cluster intelligence would compound naturally.

**Exceptions where holders.py won't help:** non-EVM chains (Cosmos like SAGA/LUNC, Solana-native like DRIFT, Bitcoin). For these, note "not EVM-mappable" explicitly and move on. Otherwise: build, no question.
