# wallet_state — consolidated on-chain wallet state (Phase 2)

Folds the parts-bin wallet cluster (`watch_wallets` / `nonce_watch` / `flows` /
`activity_audit` / `safe_audit`) into ONE capability with two modes. Use this for
the **raw per-wallet grid**; use `onchain` for the single scored verdict that wraps
the audit path.

## Modes

### snapshot (default, NATIVE)
Per tracked wallet: live `nonce`, native (gas-token) `balance`, and a `fired` flag,
read from `crime-desk/config/tracked_wallets.json` via RPC. This is the Stage-5
staged-wallet detector (CLAUDE.md §8: *alert on nonces, not breakdowns*) plus
dormant-safe balance state — folding `nonce_watch` + `watch_wallets` into one read.

```
python3 orchestrator.py wallet_state '{"ticker":"LAB"}'
```
```json
{ "ticker":"LAB", "tracked":true,
  "n_wallets":28, "fired_count":18, "dormant_count":2, "primed_unfired_count":8,
  "wallets":[
    {"label":"MM-HOT-XTOKEN","address":"0x11fc…","chain":"binance-smart-chain",
     "tier":"op","nonce":37013,"native_balance":0.357,"fired":true,"rpc_ok":true},
    ... ] }
```
- `fired` = `nonce > 0` (wallet has transacted). `primed_unfired_count` = wallets
  with gas but `nonce==0` — **staged apparatus loaded and waiting** (the §8 fire watch).
- `rpc_ok:false` → that RPC didn't answer; `nonce`/`native_balance` are null (don't
  read a null as "dormant" — it's "unknown", the `xverify` lesson).
- Untracked token → `tracked:false`, empty wallets.

### audit (DELEGATED → safe_audit.py --json)
Lifetime inbound/outbound token-flow audit per safe (CEX-selling vs internal staging
vs pristine) — the richer, slower distribution read.
```
python3 orchestrator.py wallet_state '{"ticker":"LAB","mode":"audit","days":90}'
```
Returns safe_audit's structured per-wallet verdicts. Slow (multi-chain RPC, 120s cap);
reads `_oldrepo/config` (delegated). The `onchain` capability scores this into one verdict.

## Reading it (the judgment layer)

- **Dormant mega-safe lighting up = the biggest Stage-5 escalation** (§8). Watch a
  specific safe's nonce here; the cascade outruns the technical breakdown.
- Verify wallet **lifetime history**, not just current balance — a "pristine safe"
  can be a proven distributor on a remainder (use `mode:audit`).
- A null balance from one RPC ≠ drained. Cross-check before claiming "terminal holder
  drained" (the `xverify` false-positive lesson).

## Consolidation map (Phase 2)

| Parts-bin script | Folded into |
|---|---|
| `nonce_watch` (staged-wallet nonces) | snapshot (`nonce`, `fired`, `primed_unfired_count`) |
| `watch_wallets` (balance/dormancy)   | snapshot (`native_balance`, `dormant_count`) |
| `safe_audit` (lifetime flow audit)   | audit mode (delegated) |
| `flows` / `activity_audit`           | audit mode (safe_audit already computes lifetime flows) |

`holders`, `xverify`, `moralis` stay distinct data-source capabilities (not folded) —
later tickets if needed.

## Tests

`tests/test_wallet_state.py`: tracked snapshot shape + per-wallet contract, count
consistency, untracked path, human-non-JSON, orchestrator envelope, registry modes.
