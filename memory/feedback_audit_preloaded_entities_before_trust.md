---
name: feedback_audit_preloaded_entities_before_trust
description: Always audit preloaded entity wallets for actual activity before treating as forward signals — 66% of eth-labels CEX wallets are retired/dormant despite high lifetime nonces
metadata: 
  node_type: memory
  type: feedback
  originSessionId: cb5fb967-ff26-46b5-a233-47cbc7324bb0
---

Preloaded entity wallet datasets (eth-labels.com, Arkham public tags, etc.) are heavily polluted with **retired/dormant addresses that still have public tags**. A wallet labeled "KuCoin Hot Wallet" with nonce 2.3M can be **completely dormant for 30+ days** — historically a hot wallet, now retired. Don't trust the tag; audit activity first.

**Why:** 2026-05-26 audit run via `activity_audit.py` on the CEX subset of `known_entities.json` showed:
- 12/67 ACTIVE (18%), 2 SEMI, 44 DORMANT (66%), 9 PRISTINE
- Both mapped "Binance" addresses DORMANT (nonces of 1 — entries are decorative)
- "KuCoin" (nonce 2.3M), "Bitfinex 1" (391K), "OKX 2" (665K) — all DORMANT for 30+ days, retired entirely
- Wintermute's 7 eth-labels addresses include both `Wintermute 1` (nonce 1, never used) and `Wintermute 2` (nonce 415K but 0 txs in last 30 days = retired)
- **Bitget was the standout — 5/5 ACTIVE**, all firing daily; Crypto.com 2 / Kraken Hot Wallet 4 / KuCoin 20 are the high-activity exchange wallets currently
- User's instinct was correct: "most of them arent active that are preloaded" — confirmed quantitatively

**How to apply:**
1. **Before adding ANY wallet to vc_entities.json / tracked_wallets.json / HL whales / etc., audit it:**
   ```
   python3 scripts/activity_audit.py --address 0x<addr> --chain ethereum
   ```
   Reports: nonce, tx_7d, tx_30d, status (ACTIVE / SEMI / DORMANT / PRISTINE / DEAD).
2. **For dormant wallets** (high lifetime nonce but no recent activity): they're escalation markers, NOT forward signals. ANY tick = significant (rare event). Use `alert_strategy: nonce_any` in vc_entities.json.
3. **For active wallets** (high tx_7d/30d): they're high-volume MM/CEX wallets. Nonce ticks every minute. Use `alert_strategy: cex_bound_only` so only actionable destination-bound transfers alert. Otherwise the watcher spams.
4. **For pre-loaded label datasets (eth-labels, known_entities.json):** trust the LABEL for classification (a "Binance Hot Wallet" tag is still a Binance address) but don't trust ACTIVITY — many tagged wallets are retired. The vc_entity_watch correctly uses labels for destination classification regardless.
5. **Quarterly audit cadence:** rerun `activity_audit.py --source known_entities --labels binance,coinbase,...` every quarter to refresh which preloaded labels are still live. New retirements happen as CEXs rotate hot wallets.

**Use the audit tool as a gatekeeper:** every wallet add request gets an audit FIRST. The discipline is "don't watch dormants as if they were live signals, don't add to entity lists without verifying."

Related: [[feedback_dont_drift_to_perp_only_scans]] (full coverage discipline), [[feedback_auto_arm_named_triggers]] (auto-arm only after verification), [[feedback_cross_rpc_verify]] (single-RPC reads can be silently wrong)
