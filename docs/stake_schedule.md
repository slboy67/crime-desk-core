# stake_schedule — unlock cliffs + catalysts (SPEC 43)

Native port of `_oldrepo/scripts/stake_schedule.py` (the RIVER unlock methodology).
Derives an unlock schedule + CLIFF dates from a staking/lock contract's event logs,
and optionally writes the cliffs to `config/catalysts.json`.

Why: §4 sub-pattern B (OTC/VC vesting-hedge) hinges on **an on-chain lock contract +
unlock cliff ahead**. This capability is how the desk gets that calendar. No lock
contract → no calendar → watch safe nonces instead (sub-pattern A).

## Invoke

```
python3 orchestrator.py stake_schedule '{"contract":"0x..","chain":"bsc"}'
python3 orchestrator.py stake_schedule '{"contract":"0x..","chain":"bsc","window":90,"catalyst":"RIVER"}'
# token-mode (passive vesting contracts that emit no events — scan token Transfers from=vesting):
python3 orchestrator.py stake_schedule '{"contract":"0xVESTING","chain":"bsc","token":"0xTOKEN"}'
# escrow-mode is automatic (SPEC-165) — any lock-mode call tries the UUPS
# unlock-escrow selectors first; --float-supply makes cliff pct relative to float:
python3 orchestrator.py stake_schedule '{"contract":"0xESCROW","chain":"bsc","float_supply":17190000}'
```

Args: `contract` (required), `chain` (default `bsc`; aliases bsc/bnb/eth/arb/op
accepted), `window` days back (default 60, lock/token-mode only), `token`
(token-mode), `catalyst` (ticker → write cliffs to `config/catalysts.json`, deduped
on ticker+date+type), `float_supply` (SPEC-165, escrow-mode: cliff `pct` is
pct-of-float when given, else pct-of-decoded-locked-total).

## Output (one JSON object)

```
{contract, chain, mode: "lock"|"token"|"escrow", primary_event, locked_total,
 schedule, cliffs: [{date, amount, pct}], catalyst_written: bool}
```

- **lock-mode** (default): chunked `eth_getLogs` over the window → primary event by
  topic[0] frequency → heuristic uint256 decode (amount = largest non-timestamp
  slot, endTime = largest timestamp slot) → FUTURE unlocks only.
  `schedule: [{week, amount, pct_of_locked}]` — ISO-week (Monday) buckets.
- **token-mode**: Transfer-from-vesting scan; `locked_total` = current `balanceOf`
  the vesting contract; cliffs are *historical* releases ≥5% of held balance
  (catalyst type `unlock_historical`).
- **escrow-mode (SPEC-165)**: tried FIRST on any lock-mode call (no `--token`) —
  `getUnlockSchedules()` (`(uint256[] times, uint256[] cumulativeAmounts)`) decodes
  directly into per-step `schedule: [{date, amount, pct}]`; `locked_total` = the
  last cumulative step. Also carries:
  - `schedule_hash` — sha256 of the raw return, truncated. A later run with a
    different hash means the schedule itself CHANGED — that is a tripwire event
    in its own right, independent of any single date.
  - `mutable` / `mutators` / `owner_safes` — the runtime bytecode is scanned for
    the owner-only setter selectors (`setUnlockSchedules`, `setStartTime`,
    `emergencyWithdraw`, `upgradeToAndCall`); when any are present the schedule is
    team-settable at will and `owner()` is read for who holds that lever. These
    selectors are checked for PRESENCE only — never called (they're state-changing
    and take args this adapter doesn't have).
  - `available_amount` / `withdrawn_amount` / `start_time` — the escrow's own
    `getAvailableAmount()`/`withdrawnAmount()`/`startTime()` getters, best-effort
    (`None` on a failed read, never fatal).
  - `pct_basis`: `"float"` when `float_supply` was given, else `"locked_total"` —
    always check this before trusting a cliff `pct` (11% of float can be 2% of a
    much larger locked total, and vice versa).
- **No escrow selectors, no lock events → clean `{cliffs:[]}`** with a `note`
  (lock-mode) or `escrow_adapter_reason` (why escrow-mode didn't apply), never a
  crash: no events, no topics, no decodable future endTime, and a reverting/absent
  `getUnlockSchedules()` all degrade explicitly.

## RPC

Archive endpoint FIRST per chain — for BSC that is
`https://bsc-mainnet.public.blastapi.io`, the only known working public BSC archive
(`memory/reference_bsc_archive_rpc.md`). dataseed/publicnode kept as latest-only
fallbacks.

## Caveats

- Heuristic decoder: contracts whose lock event carries no endTime slot need a
  custom decoder — the output says so in `note` rather than guessing.
- `locked_total` = sum of *decoded future* unlocks (lock-mode), not total supply;
  `pct` is relative to that. Cross-check magnitude vs token supply before sizing
  a verdict on it.
- OTC is still the blind spot (§8): a clean schedule bounds *locked* supply only.

Test: `tests/test_stake_schedule.py` (offline; module-level `rpc` mocked).
