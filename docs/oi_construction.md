# oi_construction — OI-type decomposition + venue roles (SPEC-179)

## Why it exists

CLAUDE.md §0.6 item 3 — how OI is CONSTRUCTED, "the part the framework calls the
heart of it" — was computed nowhere: no arb-vs-directional decomposition, no
mechanical venue-role read (§0.6.3b). This capability closes that gap.

```
python3 orchestrator.py oi_construction '{"ticker":"LAB"}'
```

**The output may only VETO** ("this OI is not squeeze fuel"), never manufacture
confidence. `UNKNOWN` is first-class and always carries a reason.

## Contract

```json
{"ticker": "X", "as_of": "…",
 "battlefield": {"…the SPEC-177 oi_mc.build_battlefield envelope, incl. leverage_state…"},
 "venue_roles": {"mark_engine": {"venues": […], "anchor": "aster", "weights": {…}, "verdict": "…"},
                 "size_book": {"venue": "…", "share_pct": …, "verdict": "…"},
                 "exit": {"venue": "…", "grade": "FLOW_CONFIRMED|DEPTH_INFERRED|UNKNOWN", "flow_age_h": …},
                 "hedge": {"verdict": "…"}, "as_of": "…"},
 "oi_types": [{"type": "…", "side": "long|short|both",
               "share_read": "unknown|minor|material|dominant",
               "evidence": [{"signal": "…", "detail": "…"}],
               "mandatory_met": true, "verdict": "ASSERTED|NOT_ASSERTED|UNKNOWN"}],
 "verdict": "DIRECTIONAL|ARB_DOMINATED|MIXED|UNKNOWN",
 "aggregate": {"total_oi_usd": …, "overstated_by_cross_venue": false},
 "degraded": [{"instrument": "…", "reason": "…"}],
 "gating_ok": true}
```

Every role/type resolves to a value / `SPLIT` / `NONE_DETECTED` / `UNKNOWN` — `UNKNOWN`
always with a reason. Evidence entries are always `{signal, detail}` pairs.

## Boundaries (hard)

- **CALLS** the keyless layer live: `venue_map.build_venue_map`, `oi_mc.build_battlefield`,
  `cvd.resolve_spot_venue`, the one genuinely new endpoint,
  `fetch_mark_constituents` (Aster `fapi/v3/indexreferences`, falling back to Binance
  `fapi/v1/constituents` — both keyless, weight fields converted fraction→percent), and
  — **SPEC-182** — `stake_schedule.build_schedule` via `fetch_lock_info` (keyless RPC,
  never Moralis; see "Live wiring" below).
- **CONSUMES on-chain from EXISTING desk state, never initiates a fresh Moralis call.**
  `chip_state` (§2 Cat-A chip-control read) and `flow_confirmed` (a tracked operator
  deposit-rail termination) are **caller-injected** — this module has no import of
  `verify_wallet` or `onchain` anywhere, so there is no code path here that could touch
  Moralis. `lock_info` is caller-injectable too (an explicit value always wins) but
  defaults to the live `fetch_lock_info` wiring when the caller passes nothing.
- **Writes ONLY `config/venue_roles.json`** (via `--save-roles` / `save_venue_roles_snapshot`) —
  append-friendly history per ticker for drift detection, never overwritten — plus the
  local `state/lock_info_cache.json` cache (SPEC-182, keyless, not Moralis-backed).

## Live wiring (SPEC-182 — closes the RR-179 "no live wiring" gap for 2 of 5 injection points)

**OI-funding elasticity floor** (`_elasticity_from_store`, wired into `FUNDING_FARM`'s
`elasticity_confirmed` whenever the caller doesn't inject `chip_state.elasticity_inputs`):
reads `state/oi_samples/<SYM>.jsonl` (the SPEC-178 sampler store), splits each venue's
series at its own last `redenomination` marker row (raw-unit OI isn't comparable across
one), and detects a "settlement" as a change in a venue's stored `funding_pi_4h` between
consecutive `status:"ok"` rows (the store carries no `interval_min` field, so the
settlement itself — not a derived interval — is the join key). Floor: **≥3 settlements
in-window AND ≥70% time-bucket coverage** (default window 3 days, 4h buckets — a
sampler-uptime proxy, independent of the settlement count); below either ⇒
`elasticity_confirmed=None` + a `degraded` row (`no_history` below 3 settlements or no
file, `gappy` for coverage <70%) and `FUNDING_FARM` stays capped at
`farm_viable_unconfirmed` exactly as before. Above the floor: `elasticity_confirmed=True`
when a majority of settlements are **concordant** (OI moves the same direction as the
change in `|funding_pi_4h|` — the carry-chasing signature), else `False`.

**`lock_info`** (`fetch_lock_info`, wired into `VESTING_HEDGE`'s mandatory gate and
`OPERATOR_AMM`'s `no_lock_contract` supporting signal): `config/lock_registry.json`
(ticker → `{contract, chain}`, empty by default — populate only from a *verified*
lock-contract audit) resolves WHICH contract to read; `stake_schedule.build_schedule`
(keyless RPC) reads it, cached in `state/lock_info_cache.json` **until the cached cliff
date passes** (SPEC-179's "locks until cliff" TTL) so a resolved lock isn't re-scanned
(chunked `eth_getLogs`) on every call. No registry entry for a ticker ⇒ not attempted,
`lock_info=None`, **no** `degraded` row (identical to any other uninjected signal, e.g.
`chip_state=None`). A registry entry present but `stake_schedule` finds no future cliff
⇒ attempted, `lock_info=None`, **with** a `degraded` row (`"no resolvable on-chain lock
contract"`).

`chip_state`/`flow_confirmed`/`top_book_neutral`/`cross_venue_dispersion` remain
caller-injected only — no existing desk-state source wires them yet (unchanged from
SPEC-179/RR-179). With an empty registry and an empty/absent sampler store (true for
every ticker today until the sampler accumulates history or a lock is verified and
registered), `build_oi_construction`'s output is unchanged from the pre-SPEC-182
baseline except for the new `degraded` rows this layer can now emit — it only gained the
ability to CONFIRM, never lost its conservatism.

## OI types (G1)

Five types, each an `evaluate_<type>(...)` pure function taking already-resolved
evidence. **Mandatory-alone is never enough to assert:**

| Type | Mandatory (hard gate) | Supporting (>=1, grades `share_read`) |
|---|---|---|
| `FUNDING_FARM` | carry viability (\|APR\| ≥ ~10%, `FARM_APR_HURDLE_PCT`) — **viable alone still degrades to `UNKNOWN` (`farm_viable_unconfirmed`)**, it's an economic bound, not an identification | OI funding-elasticity · account-ratio divergence · basis behaving |
| `VESTING_HEDGE` | on-chain lock contract w/ cliff ahead (`lock_info.cliff_date`) — **missing lock ⇒ `NOT_ASSERTED` regardless of how perfect the supporting tape is** | carry violation · perp discount · pristine chain · HL direct observation |
| `OPERATOR_AMM` | chip control (`chip_state.chip_control`) — quota-dead/unreadable ⇒ `UNKNOWN`, unassertable, nothing fabricated | deep-neg at highs · no free spot · no lock contract · squeeze-cadence legs |
| `MM_INVENTORY` | top-book neutrality (`top_book_neutral`) | OI insensitive to funding · OI stable under vol churn |
| `CROSS_VENUE_FUNDING_ARB` | persistent cross-venue funding dispersion (`cross_venue_dispersion`) | OI elevated both venues · price-inelastic · paired unwind — sets `overstated_by_cross_venue` |

**Contamination rule**: a suspected (asserted) `OPERATOR_AMM` downgrades
`account_ratio_divergence` evidence for **every other type** on the same name —
`build_oi_construction` withholds that signal from `evaluate_funding_farm` whenever
`OPERATOR_AMM` asserts, and appends a `degraded` entry naming the downgrade.

## Roll-up (`roll_up_verdict`, G3)

- **`ARB_DOMINATED`** — a non-directional type (`FUNDING_FARM`/`VESTING_HEDGE`/
  `MM_INVENTORY`/`CROSS_VENUE_FUNDING_ARB`) reads `share_read: dominant`.
- **`MIXED`** — anything `ASSERTED` (an asserted `OPERATOR_AMM` forces `>= MIXED` on
  its own, per G3 — "OPERATOR_AMM forces ≥MIXED + its own printed line").
- **`DIRECTIONAL`** — only when every instrument actually RAN (its mandatory signal
  was readable — `ASSERTED` or `NOT_ASSERTED`, never `UNKNOWN`) and none asserted.
- **`UNKNOWN`** — else (nothing ran / everything degraded).

## Venue roles (G2)

- **`mark_engine`** — constituents ≥20% weight of the anchor index (`fetch_mark_constituents`,
  default anchor `aster`). `SPLIT` when more than one venue clears the threshold;
  `NONE_DETECTED` when constituents are published but nothing clears it; `UNKNOWN`
  when unpublished everywhere.
- **`size_book`** — `venue_map`'s own per-venue `oi_share_pct` (zero new fetch): the
  top venue at ≥40% share crowns; within 15 points of the runner-up ⇒ `SPLIT`; a
  plausible-size venue erroring mid-sweep ⇒ `UNKNOWN` (a partial sweep can't crown —
  implemented conservatively: **any** `status:"error"` venue in the sweep blocks
  crowning, not just ones provably large enough to matter, since a dead probe gives
  no signal either way to judge "plausible size" from).
- **`exit`** — `FLOW_CONFIRMED` (an injected tracked-wallet deposit-rail termination
  ≤14 days old) beats `DEPTH_INFERRED` (`cvd.resolve_spot_venue`'s deepest real spot
  book). No flow + no spot ⇒ `UNKNOWN` (perp-only red flag).
- **`hedge`** — mandatory: a persistent funding outlier vs the pack (sign flip or
  ≥2× magnitude vs the median of the other listed venues, `_funding_is_outlier` over
  `venue_map.funding_extreme` — zero new fetch) + 1 of {elevated OI share, contrarian
  basis}. `NONE_DETECTED` (no outlier) is a normal answer, distinct from `UNKNOWN`
  (outlier read itself unavailable).

## Staleness (`gating_ok`)

`compute_gating_ok(roles_as_of_ts, now_ts)` — `false` when the roles block (the
veto-bearing block) is older than `TTL_ROLES_H` (4h). A stale read still prints (the
roles block is never withheld), it just stops gating anything — governing principle
carried from SPEC-180 G4: a data gap must never block a trade today's rules would
take.

## Tests

`tests/test_oi_construction.py` — offline-deterministic (`venue_map_fn`/
`battlefield_fn`/`spot_resolver`/`mark_constituents_fn` all injected). Covers every
`evaluate_<type>`/`resolve_<role>` mandatory-gate case, the roll-up matrix, and the
spec's literal acceptance fixtures: RIVER-shaped (lock+cliff+paying-short+discount →
`VESTING_HEDGE` asserted, verdict ≥ `MIXED`), LAB-shaped (chips+deep-neg-at-highs+no-lock
→ `OPERATOR_AMM` asserted, verdict `MIXED`, account-ratio evidence downgraded on
`FUNDING_FARM`), missing-lock-with-perfect-tape (→ `VESTING_HEDGE` `NOT_ASSERTED`),
quota-dead (→ `OPERATOR_AMM` `UNKNOWN` + a `degraded` row, nothing fabricated),
stale-roles (→ `gating_ok: false`, roles still printed), and a static check that the
module never imports `verify_wallet`/`onchain` (no Moralis code path exists here).
`TestElasticityFromStore`/`TestFetchLockInfo`/`TestDefaultPathLiveWiring` (SPEC-182)
cover the elasticity floor (`no_history`/`gappy`/confirmed-true/redenomination-split),
the lock registry+cache seam, and the default-path wiring including the req-3
byte-identical-minus-degraded-rows regression check.
