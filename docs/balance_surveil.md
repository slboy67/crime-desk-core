# balance_surveil — contract-wallet balance-delta surveillance (SPEC-126)

`nonce_surveil` (`capabilities/onchain.py` `build_nonce_state` + `capabilities/wallet_state.py`
`build_snapshot`) watches account **nonces**. That is permanently blind to a **Gnosis Safe
proxy**: a Safe executes via `execTransaction` from a signer EOA, so the safe's own account
nonce never changes (contract nonces only bump on `CREATE`).

**Incident:** DEXE 2026-07-21. The committed tripwire was "page on first outbound from any
`DEXE-STAGED-HOP2-*` safe." Those 6 wallets are Gnosis Safe proxies. They fired 15:43-16:00 UTC
(625K DEXE → a relay → Binance, price 29 → 3.86) and **nothing paged** — the desk's own note
("surveil balances not nonces") lived only in a wallet's `_note` prose field; no code read it.

This module is the fix: a **balance**-mode surveillance path, parallel to (never replacing)
`nonce_surveil`, for wallets that are contracts.

## Per-wallet mode

```json
{"label": "DEXE-STAGED-HOP2-A", "address": "0x0076...", "chain": "binance-smart-chain",
 "tier": "distribution", "surveil": "balance"}
```

- `surveil` omitted → **autodetect**: `onchain.probe_contract` (an `eth_getCode` bytecode probe,
  disk-cached — SPEC-67's cache, so no per-tick RPC after the first probe) decides contract
  (→ `balance`) vs EOA (→ `nonce`).
- `surveil: "nonce"` — force nonce-only (an EOA whose tx-count still matters more than its token
  balance; also the escape hatch if a probe ever misdetects).
- `surveil: "balance"` — force balance-only, no probe call at all (used for the 6 known
  DEXE hop-2 Gnosis safes, since their contract-ness is already verified on-chain).
- `surveil: "both"` — this module ALSO balance-diffs the wallet, in addition to whatever the
  (untouched) nonce pipeline already does with it.

`nonce`-mode wallets are a no-op for this module — `nonce_surveil` already covers them, and this
module never imports or mutates `onchain.py` / `wallet_state.py`.

## Balance diff

Per BALANCE-mode wallet, per tick: a cross-checked `balanceOf` (`onchain.balance_of` — up to 2
free RPC sources) diffed against `state/balance_baseline_<TICKER>.json`:

- **first-ever tick** → seeds the baseline, never fires (no false alert on deploy).
- **decrease > `DUST` (1e-6)** → `HIGH` `balance_drop`, delta in the message + payload.
- **decrease to exactly 0, NOT cross-RPC-confirmed** (single source answered, or the two sources
  disagree) → `MED` `data_quality` instead of a page — the baseline is **not** overwritten with
  the unconfirmed zero (a later real read still diffs against the last TRUSTED balance). Mirrors
  `[[feedback_cross_rpc_verify]]`: a single-RPC zero is a data failure, not a datum.
- **N (`FAIL_THRESHOLD`=3) consecutive unreadable ticks** → `MED` `surveillance_blind` — going
  quiet must be distinguishable from genuinely "no movement" (§3).

Events go through `inbox.append_event` (source `balance_surveil`) — the same producer API
`funding_surveil` / `tape_watch` / `board_tick` use.

```
python3 ops/balance_surveil.py tick DEXE --json
python3 ops/balance_surveil.py tick --all --json      # every tracked token
```

```json
{"ticker": "DEXE", "checked": 6, "events": [
  {"severity": "HIGH", "kind": "balance_drop", "prev_balance": 510218, "balance": 138908,
   "delta": 371310.0, "msg": "DEXE-STAGED-HOP2-A: balance 510.22K → 138.91K (-371310 · -72.77%) — "
   "contract-wallet OUTBOUND (nonce-blind Gnosis safe, SPEC-126 §8)",
   "address": "0x076b...", "label": "DEXE-STAGED-HOP2-A"}
], "fired": [ ... same entry ... ]}
```

SPEC-141: the `msg` field renders through `page_grammar.fmt_balance_change` — compact
units + a signed raw delta + a signed % change, never scientific notation (the prior
`:g` formatting rendered a 7-digit balance as `1.90324e+06`, meaningless on a phone).
`prev_balance`/`balance`/`delta` in the structured payload stay raw floats — only the
human-facing `msg` string is reformatted.

## Tests

`tests/test_balance_surveil.py`: seed/no-fire, HIGH on a real decrease, no-event on
unchanged/increase/dust, the to-zero cross-RPC-disagreement → MED-not-page case, the
cross-confirmed-zero → HIGH case, the N-consecutive-failure → MED-blind case (+ recovery resets
the counter), explicit-mode override (incl. invalid values falling back to autodetect, and
"both"), getCode-cache reuse (a second probe on the same tick loop hits the cache, not a fresh
RPC), and `run_tick`'s wiring (nonce-mode wallets skipped, seed tick silent, second tick emits via
`emit_fn`, `fired` list populated).
