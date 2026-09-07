# onchain — the on-chain analyser (the desk spine)

On-chain wallet-movement surveillance is the desk's **original core** (the perp board
came second). The live top on a §4 vesting-hedge name is **a mega-safe nonce tick**, not
a getLogs lifetime audit — that distinction cost the LAB $21→$7.46 cascade when the old
`safe_audit` path timed out to `UNAVAILABLE`. So this capability is built on the **cheap
nonce read** (one `eth_getTransactionCount` per tracked safe, ~2s for 28 wallets), diffed
against a committed baseline.

## Call

```
python3 orchestrator.py onchain '{"ticker":"LAB"}'            # composed picture
python3 orchestrator.py onchain '{"ticker":"LAB","depth":"nonce"}'   # just the live signal
```

## Contract

```json
{ "ticker":"LAB", "bias":"...", "score":-30, "signal":"ESCALATION",
  "nonces": { "signal":"ESCALATION", "score":-30, "ms":1638,
              "fired_count":18, "primed_unfired_count":8, "dormant_count":2,
              "newly_fired":[...], "escalation_fired":[{"label":"MEGA-SAFE-3","nonce_prev":0,"nonce_now":1,"tier":"distribution"}],
              "baseline_seeded":false },
  "safe_history": {"available":false,"reason":"bscscan free tier rejects BSC"},
  "recent_flows": {"available":false,"reason":"..."},
  "vc_overlap":   {"available":true,"matches":[...]},
  "concentration":{"available":false,"reason":"holders.py not yet json-native"},
  "unsupported_chains":[{"chain":"algorand","supply_pct":94.66}],
  "unreadable_supply_pct":94.66,
  "coverage": {"nonces":true,"safe_history":false,"vc_overlap":true,"concentration":false} }
```

## Unseen supply (SPEC 58) — `unsupported_chains` at the top level
`build_onchain` surfaces `unsupported_chains:[{chain, supply_pct}]` + `unreadable_supply_pct`
at the TOP level, read straight from the onboard config — **independent of the GoPlus
concentration read**, so the number survives when concentration returns n/a (the FOLKS live
miss, where it was buried inside `concentration.coverage` and disappeared). When exactly one
unsupported chain is genuinely non-EVM (a non-`0x` contract — Algorand's ASA id, vs the
monad/sei EVM bridge stubs), the unreadable remainder is attributed to it: FOLKS = 94.66% on
algorand. This belongs in every multi-chain brief headline — say out loud how much you cannot see.

## The signal (the spine)

`signal` ∈ `ESCALATION | BASELINE_SETTLING | LOADING | DORMANT | QUIET | UNTRACKED`, with
`score` in the convergence sign-convention (negative = distribution pressure):

- **ESCALATION** (`-30`) — a normally-dormant safe's nonce advanced since the committed
  baseline = **distribution / bid-pull firing** (§8 Stage-5, the cascade outruns the
  breakdown). The `bias` line **names the actual fired safe(s)**. Only a fire from a
  non-operator tier at confidence `high` can set this headline (SPEC 55).
- **BASELINE_SETTLING** (`0`) — the fires this read are ALL tier `unclassified` (freshly
  onboarded candidates settling their baseline). NOT a §8 event: the Designer's tiering
  pass is the gate that makes a later fire meaningful (SPEC 55).
- **LOADING** (`-10`) — staged safes gas-primed but unfired (`nonce==0`, has gas).
- **DORMANT** (`+10`) — safes locked/dormant. At a fresh-ATH deep-neg this is **NEUTRAL,
  not bullish** (§4 OTC vesting-hedge trap).
- **QUIET** (`0`) — nothing notable, or first run (`baseline_seeded:true`, no false escalation).

### Top-holder distribution — QUIET must mean *verified* quiet (SPEC-89)

The nonce/signal layer reads the **staging/nonce** layer, not the **actual token transfers**,
so a bare `QUIET`/`DORMANT` once hid top holders that were actively distributing (BLESS 0x73d8
escrow + H 0x28e2ea — `onchain` QUIET while `verify_wallet` said DISTRIBUTING). Every read now
runs `verify_wallet` on the **top N holders** (default 3, CEX/DEX-tagged + burn holders skipped;
the cluster escrow is captured via its top-holder position) and surfaces:

- `top_holder_distribution: {checked, distributing, n_checked, holders[{address, verdict,
  last_out_ts, sell_destinations}]}` — each holder's `verdict` matches a direct `verify_wallet`.
- `distribution_checked: bool` — `QUIET` means *verified quiet* **only** when this is `true`.
  Moralis-quota-aware: at quota-exhaustion it degrades to `false` (never blocks/hangs the read).
- `signal_caveat` — set to `"top-holder distribution present (verify_wallet)"` when any top
  holder's verdict is `DISTRIBUTING`; in that case a quiet headline (`QUIET`/`DORMANT`/
  `BASELINE_SETTLING`/`NONCE-ONLY-WATCH`/`NONCE-CHURN`) **flips to `DISTRIBUTING`** so a deep-neg
  squeeze long-screen can never read "not distributing" from the nonce layer alone (§4/§8).

### Rotation-aware distribution freshness — FRESH / FROZEN / ROTATED (SPEC-98)

The live exit signal on a distribution short is the top holder's `last_out_ts` freshness —
and VELVET proved the binary fresh/frozen read defeatable: DWF rotated supply through **fresh
wallets** between pump legs, so the tracked timestamp FROZE while distribution continued (a
**false pause**). `capabilities/rotation_freshness.py` replaces the binary read with a
three-state verdict, surfaced as `distribution_freshness` on the composed read:

- **FRESH** — tracked top holder out within `fresh_window_h` (12h default) — the clock is live.
- **FROZEN** — tracked quiet AND both rotation legs quiet → genuine pause; on a live short
  thesis it carries the VELVET discipline note (*bank/exit — don't ride a dead-thesis short
  into the squeeze*). If a leg was unreadable the pause is **UNCONFIRMED** (§3 caveat, no
  bank note — never a clean bill off a dead provider).
- **ROTATED (suspected)** — tracked quiet BUT distribution evidence continues. Two independent
  legs, EITHER flips it: **tape** (≥`tape_min_legs` runs of ≥`tape_leg_min_bars` consecutive
  `longs_closing` bars with net move ≤ −`tape_leg_min_move_pct`% — no on-chain quota) and
  **on-chain** (recent transfers into known CEX channels from senders NOT tracked for the
  token, young — nonce ≤ `onchain_young_nonce_max` — and sized vs `onchain_min_usd` /
  `onchain_min_pct_float`; provider-gated through the SPEC-97 seam).

Thresholds: `config/rotation_freshness.json` (documented defaults in `DEFAULT_CFG`).
Quota discipline: a FRESH tracked leg answers the question — the legs only run on a quiet
clock; the whole layer is gated on a **live SHORT thesis + a checked top-holder sweep**.
State persists per ticker (`state/rotation_freshness_<T>.json`) so the board echoes it
cheaply (`classify.annotate_freshness` appends `distribution: <VERDICT> — …` to live-SHORT
rows), and a **FROZEN→ROTATED transition on a live thesis fires ONE HIGH inbox event**
(`rotation_freshness` source — the "about to bank into a false pause" moment). Also surfaced
in `brief`'s on-chain layer/headline and `verify_wallet --token` output
(`attach_freshness`, CLI layer). Tests: `tests/test_rotation_freshness.py`.

### What never sets the headline (SPEC 55)

The topline (`signal`/`bias`/`score`) obeys the per-wallet tiering it already computes —
it no longer escalates on noise the tiering layer already flagged:

- A fire with `tier: cex`/`operational:true` (`confidence:"noise"`) — e.g. a KRAKEN-HOT
  EOA ticking — still appears in `newly_fired` for visibility, but the topline stays
  **DORMANT/QUIET** and `escalation_fired` is empty.
- An all-`unclassified` batch → **BASELINE_SETTLING**, never ESCALATION.
- A MIXED read (≥1 real distribution-tier fire + a cex fire) → ESCALATION whose `bias`
  names **only** the real safe (`escalation_fired` excludes the cex one).

### Reads don't move the baseline (SPEC 55)

The baseline lives at `state/nonce_baseline_<TICKER>.json`. A plain **read**
(`onchain`, `brief`, `analyse`) **does not advance** it — it only seeds one if none
exists. Two reads of the same token in the same minute therefore return the **same**
verdict (previously each read advanced the baseline, so a direct `onchain` and a `brief`
seconds later diffed against different baselines and disagreed — PLAY/UAI drift). The
**surveillance sweep** (`onchain_board` / `ops/surveil.sh`, `persist=True`) is the loop
that advances the baseline — it consumes each fire so the next sweep catches the next one.

## Coverage / what's not yet wired

- **nonces** — always covered (the spine, ~2s).
- **vc_overlap** — native config join (tracked wallets vs `config/vc_entities.json` +
  `known_entities.json`).
- **safe_history / recent_flows** — **Moralis** (`deep-index.moralis.io`, `moralis_api_key`
  in secrets), per distribution/team safe, concurrent + bounded (~3s for LAB's 10 safes).
  Covers BSC — which the free BscScan/Etherscan-V2 key **cannot** (`"Free API access is not
  supported for this chain"`); the getLogs fallback is deliberately NOT used (it is the 90s
  hang this rebuild removes). `out_count`/`last_out_ts` per safe + recent outbound transfers
  with destination tagged against known entities. Moralis free tier = 40K CU/day.
- **concentration** — **GoPlus Token Security** (free, no key, all EVM chains incl. BSC):
  `top1_pct`, `top10_pct`, `holder_count`, and per-holder `{percent, tag, is_contract,
  is_locked}` for the token's primary contract. Free tier throttles bursts → `_get_json`
  retries; a sustained 429 returns `available:false` (coverage-labeled, never hangs).
  **SPEC 34 — coingecko fallback:** for a TRACKED name the contract comes from
  `tracked_wallets.json` (`tracked:true, discovery:false`). For an **UNTRACKED** name the
  contract is resolved from the coingecko/`pull5` platform map and concentration is read
  against the first GoPlus-supported chain, flagged `tracked:false, discovery:true` — so
  every daily-gainer discovery scan surfaces the §0.6 chip read (top1/top10/holders) instead
  of UNTRACKED, even before per-name wallet onboarding. (Concentration only — nonce /
  safe-history / distribution still need onboarding; those stay UNTRACKED for non-onboarded
  names.)

## Holder-enumeration provider seam (SPEC-101)

`enumerate_holders(contract, chain_key, top_n=50)` is a contract-scoped provider seam,
Bitquery → GoPlus → unavailable, mirroring `token_transfers`'s chain-scoped seam one level
up (`register_holder_provider` / `unregister_holder_provider` / `holder_providers_for` —
the socket a future vendor or SPEC-102's local BSC indexer plugs into). Returns
`{available, source, partial, holders:[{address, balance, share_pct}], top10_share_pct}`.

**Provider order is Bitquery → GoPlus, not the originally-scoped Bitquery → Moralis** — no
Moralis-based holder read exists anywhere in this codebase; the concentration section above
(GoPlus, SPEC 12/56) is the actual, already-working, free/keyless enumeration path, so it is
the real fallback the seam degrades to (`_p_goplus_holders` wraps `_goplus_concentration`).
Key from `config/secrets.json` `"bitquery_api_key"` — absent (today's state) → Bitquery is
skipped silently, GoPlus path unchanged. Cache TTL + Bitquery's per-provider daily budget
(default 800, `BITQUERY_DAILY_BUDGET`-overridable via `provider_quota.py`) live in
`config/holder_enumeration.json`.

**Not yet wired:** `_concentration()` / `_concentration_primary()` / `_top_holder_distribution`
(the existing radar/concentration consumers) still call `_goplus_concentration` directly,
not through `enumerate_holders` — GoPlus's `token_security` payload carries `is_burn` /
`is_contract` / `is_locked` / `tag` metadata (SPEC 37 burn-exclusion,
`_NONOPERATOR_TAGS` CEX/DEX/pool filtering) that Bitquery's balance-only schema doesn't
have, and the Bitquery schema itself couldn't be live-verified (no key). Migrating those
consumers onto the seam is future work once a live key confirms Bitquery's actual response
shape and a metadata-preserving mapping (or a documented "GoPlus stays primary for
metadata reads, Bitquery only for raw top-N balances") is designed. New consumers (e.g. a
future radar needing more than GoPlus's top-10 cap) can call `enumerate_holders` directly
today with no further plumbing. Tests: `tests/test_holder_enumeration.py`.

## Tracked-set local indexer (SPEC-102)

`capabilities/local_index.py` sunset-proofs the recurring surveillance loop against vendor
death/throttle (Sim: announced and sunset inside a month; Moralis: throttles): a small
SQLite index (`state/local_index.db`) of ERC-20 Transfer logs for exactly the wallets/
contracts in `config/tracked_wallets.json` — a few dozen addresses, not the chain.

**Write half (network, ops-scheduled):** `ingest_contract(chain, contract, rpc_call=...)` /
`tick()` / `--tick` CLI walk Transfer logs forward from the stored head via free-RPC
`eth_getLogs`, chunked per `RPC_PROVIDER_TABLE` (each free BSC RPC caps `getLogs` ranges
wildly differently — live-verified 2026-07-02, see the module docstring; `bsc.publicnode.com`
is now token-gated, `bsc-mainnet.public.blastapi.io`, the current config RPC, caps at 10
blocks). Idempotent (`INSERT OR IGNORE`, PK = tx_hash+log_index), resumable (head persists),
reorg-tolerant (re-scans + replaces the trailing `reorg_tail_blocks` every run), and
budget-capped (`rpc_call_budget` `getLogs` calls per run — a cold backfill resumes over
several ticks). Ops wires the `--tick` schedule; this module never touches launchd.

**Read half (pure SQLite, zero network):** `query_transfers()` returns
`token_transfers`-shape rows; `freshness()` gates on the index head's age (approximated from
block height, not a live RPC call). `query_or_stale()` is the provider-facing envelope
(`{available, source, partial, stale_index, txs}`) — registered as `token_transfers`'s
**first** provider (`onchain._p_local_index`, BSC+ETH-scoped): a tracked + fresh contract
serves off SQLite with zero network/quota; an untracked contract, an un-ingested range, or a
stale head all raise `ProviderSkip` and fall through to Etherscan → Moralis → getlogs
unchanged (§3 gap honesty — a gap is `unavailable`, never an empty-clean result standing in
for unread data). Config: `config/local_index.json`. Tests: `tests/test_local_index.py`.

## Moralis quota meter (SPEC 66)

`capabilities/moralis_quota.py` is the desk's gas gauge for the Moralis free tier, which
dies **silently** mid-session (memory: `reference_moralis_free_daily_quota_gates_onchain`
— the daily quota resets ~00:00 UTC, not minutes). Every **live** `_moralis_tokentx` call
(cache hits don't count) increments a per-UTC-day counter in `state/moralis_quota.json`
via `record_call()`; the counter resets on a UTC-day boundary. `build_onchain` surfaces it
as `moralis_calls_today` (plus a full `moralis_quota` block: `{calls, budget, pct, warn,
exhausted, prefix}`) and, at **≥80%** of the configured daily budget, prepends a
`[QUOTA n%]` marker to its `bias` line so the wall is visible *before* flow reads start
degrading. Budget defaults to 2000 calls/day; override with the `MORALIS_DAILY_BUDGET` env
var (ops-tunable, no code change). At 100% the existing per-section
`available:false`/`degraded:true` pattern takes over — the meter only **warns**, it never
itself blocks a read. Tests: `tests/test_quota_meter.py` (offline; clock + state path
injected). `with_prefix(text, …)` is the helper any Moralis-backed capability can call to
blindly prepend the marker to its reason/notes.

**Per-provider generalization (SPEC-97):** `capabilities/provider_quota.py` extends the
same meter to every seam provider — `record_call(provider)` / `status(provider)` /
`with_prefix(provider, text)` with a per-provider state file (`state/<provider>_quota.json`;
`moralis` keeps `state/moralis_quota.json`) and an env-overridable budget
(`<PROVIDER>_DAILY_BUDGET`, e.g. `ETHERSCAN_DAILY_BUDGET`; etherscan defaults to a
conservative 20k of the ~100k/day free tier). Same `[QUOTA n%]` warn contract at 80%.
Tests: `tests/test_provider_seam.py::TestProviderQuota`.

**Per-caller attribution + CU estimate (SPEC-123):** the 2026-07-14 incident (premerge
ran 3x + a coder TDD loop, consuming the whole free-plan day) exposed that the meter
counted calls but not *who* made them — `{"day":..., "calls":84}` cannot answer "what
spent the quota" once the plan is exhausted. `record_call(caller=None, cu=None)` now
attributes every call to a `caller` (defaults to the running script's own name via
`Path(sys.argv[0]).stem` — zero plumbing needed at any of the ~30 call sites) with a CU
estimate (defaults to `DEFAULT_CU_PER_CALL=15`, the Moralis-quoted midpoint for
`erc20/transfers`), stored under `by_caller` in the state file next to the flat total:
`{"day":..., "calls":84, "cu_estimate":1260, "by_caller":{"verify_wallet":{"calls":40,
"cu_estimate":600}, "onchain_radar":{"calls":30,"cu_estimate":450}, ...}}`.
`provider_quota.record_call` passes `caller`/`cu` straight through, so etherscan/bitquery
get the same attribution. Tests: `tests/test_quota_meter.py::TestCallerAttribution`,
`::TestProviderQuotaCallerPassthrough`.

**Quota exhaustion is a named error, not a timeout (SPEC-123):** the same incident showed
a Moralis 401 (`"Validation service blocked: ... included usage has been consumed"`)
retried (2 retries, backoff) inside `_moralis_tokentx` until the whole capability blew the
orchestrator's per-capability subprocess timeout — the operator only ever saw a bare
`{"ok": false, "error": "timeout running verify_wallet"}`, the real 401 signal discarded.
A 401 matching the plan-consumed signature now raises `MoralisQuotaExhausted` (a
`MoralisError` subclass) **immediately, no retry** — retrying an exhausted quota is
futile, and skipping the backoff is what keeps the capability inside its timeout budget so
it returns a real envelope instead of a bare timeout. The message always starts
`QUOTA_EXHAUSTED (moralis, resets 00:00 UTC): ...`, so every existing `reason: str(e)[:N]`
degrade path (verify_wallet's per-chain read, `_safe_history_and_flows`, etc.) surfaces it
verbatim with zero call-site changes. `token_transfers()`'s provider-fallback loop
prioritizes a `MoralisQuotaExhausted` over a later, less-actionable provider failure.
Tests: `tests/test_onchain.py::TestSpec17RateLimitProofing` (the quota/retry tests),
`tests/test_verify_wallet.py::test_quota_exhausted_degrades_with_named_reason_not_generic`.

## verify_wallet — ad-hoc address lookup (§8 intel)

When someone hands you a wallet to check, `onchain`/`wallet_state` only cover *tracked*
safes — `verify_wallet` verifies an **arbitrary** address (§8: verify any CT/intel
wallet-identity claim on-chain before acting).

```
python3 orchestrator.py verify_wallet '{"address":"0x..","token":"ESPORTS"}'
python3 orchestrator.py verify_wallet '{"ticker":"ESPORTS","wallet":"0x.."}'   # SPEC-89: desk-natural aliases also work
python3 orchestrator.py verify_wallet '{"address":"0x..","token":"PLAY","chain":"base"}'  # restrict to one chain
```

> SPEC-89 arg aliases: `ticker`→`token`, `wallet`→`address`. The desk uses `{ticker,wallet}`
> everywhere; the registry maps them to the capability's canonical `<address> --token <TKR>`
> before invoking (was: `argument --token: expected one argument`). The documented
> `{address,token}` form still works unchanged.
```json
{ "address":"0xbb58…ef275","token":"ESPORTS","chain":"binance-smart-chain",
  "chains_queried":["binance-smart-chain"],"degraded_chains":[],
  "verdict":"SEEDED-STAGING","seeded_staging":true,
  "funded_by":[{"address":"0x…","kind":"tracked-safe","label":"FRESH-STAGED-1 (distribution)","amount":8579113.0}],
  "token_balance":7202724.2,"token_balance_usd":356030.7,"price_usd":0.04943,"price_source":"bybit",
  "in_count":2,"out_count":44,"last_out_ts":"2026-06-03T…","recent_outbounds":[...] }
```
- **MULTI-CHAIN (SPEC 21).** Operators distribute across ETH/BSC/Base and route CEX deposits
  through secondary wallets on whichever chain. `verify_wallet` queries **every chain the token
  is deployed on** (per `tracked_wallets.json` `contracts`) and **aggregates** the transfers, so a
  wallet active on a non-default chain is no longer read as a false `DORMANT`. The repro: EDEN's
  default-chain pick is BSC, but its team/reserve wallets distribute on **ethereum** — single-chain
  read = DORMANT, multi-chain read = `DISTRIBUTING` with the real 9 outflows. Verified live: PLAY
  dumpers `0x24d0…`/`0x68a3…` resolve `DISTRIBUTING` on **base** (136/154 outs) where the BSC-only
  read showed 0. Output adds `chain` (primary = most-active), `chains_queried`, `degraded_chains`,
  and a per-chain `chains` status block; `recent_outbounds[].chain` tags each sell's chain.
- **`chain` arg** restricts to one chain (alias-normalized: `eth`/`bsc`/`base`/`bnb`/`arb`/…). Asking
  for a chain the token isn't deployed on returns `available:false` (never a silent DORMANT). If a
  chain's providers all fail it lands in `degraded_chains` + sets `partial:true`; only when **all**
  chains fail does the whole read degrade (`available:false`, `degraded:true`).
- **Token-deployment caveat (≠ the multi-chain fix).** A wallet active on a chain the *token isn't
  mapped to* still won't resolve — that's a config gap, not a code gap (fix it by adding the
  contract, as SPEC 20 did for RAVE). E.g. the SPEC-21-reported "ESPORTS secondaries" move *no*
  ESPORTS (BSC nonce 0); their real activity is a different token on ETH — flagged in config, not
  resolvable under ESPORTS.
- `token_balance_usd` is valued at the **live perp price** (`price_usd`/`price_source`),
  not the stale config `price_usd` (which drifts — was ~13× high on ESPORTS). `null` if no live price.
- `verdict` ∈ `SEEDED-STAGING | DISTRIBUTING | INDEPENDENT-HOLDER | DORMANT`.
- **`SEEDED-STAGING`** = funded by a tracked team/distribution/passthrough safe → the
  wallet is desk-seeded staging, **NOT** an independent holder (§8 — a CT "smart money
  dumper" claim that's really a seeded wallet). `funded_by` classifies each inbound source
  (tracked-safe / cex / dex / entity / unknown).
- **CEX-sourced funding** (SPEC 15 refinement): the dominant inbound source is categorized at the
  top level via `funded_by_kind` (`seeded` | `cex-withdrawal` | `dex` | `entity` | `unknown`).
  A wallet whose largest funder is a CEX hot wallet (cross-ref `known_entities.json`) is tagged
  `cex_sourced:true` + `funded_by_cex:"<name>"` rather than dropped to "unknown" — the
  **CEX-withdrawal → fresh wallet → DEX-dump** sequence (`cex_sourced_distribution:true`) is the
  operator sourcing supply off-exchange to obscure lineage (a distinct, meaningful category from a
  tracked-safe seed; implies reserves → distribution likely continues). *Requires Moralis up — the
  getLogs fallback's recent window may miss the inbound, leaving `funded_by` empty + `partial:true`.*
- **Sell-hub signature for clustering** (SPEC 15 refinement): `sell_destinations` aggregates the
  downstream addresses this wallet feeds (address/kind/amount/count) and `sig` carries
  `clip_median`/`n_out`. These are the keys the radar uses to cluster wallets feeding the SAME hub
  as one operator (see below). *Live-verified: 0xbb58 surfaces hub `0x5bb5…b4462`, 135 outs.*
- Moralis-backed (covers BSC, which the free BscScan key can't). Token must have a contract
  in `tracked_wallets.json` (needed to query its transfers). Works for any address.
- **Degrades explicitly** (SPEC-1b principle): on a provider rate-limit/error the read returns
  `available:false` + `degraded:true` and **omits** balance/verdict fields — never a fabricated
  `0` (downstream must distinguish "0 sells" from "we couldn't read it"). The radars surface
  `degraded:true` + `n_degraded` + `coverage{seeded_discovery_errors,candidates_unread,concentration}`
  so an empty distributor list under degrade isn't mistaken for "no distributors."
- Moralis calls go through `onchain._moralis_tokentx`: a 0.2s-min-interval cross-thread throttle
  + 90s TTL in-process cache + retry/backoff (raises `MoralisError` on persistent failure) — so a
  sweep's concurrent burst / high-frequency polling within a run doesn't trip the free-tier limit.
  (Cache is per-process; the throttle is the burst guard. Free tier is ~40K CU/day.)

### Rate-limit-proofing (SPEC 17) — match query to the cheapest tool
- **Free-RPC nonce = liveness** (`onchain.nonce_of`, `eth_getTransactionCount`, zero quota). The
  radars **nonce-gate** the heavy read: a known wallet whose nonce hasn't advanced since the last
  sweep can't have new sells/buys → reuse the cached row, **no Moralis call** (`n_nonce_cached`).
- **Chain-scoped provider seam** (`onchain.token_transfers`, SPEC-97): the wallet-scoped
  token-flow read runs an ordered, **per-chain** provider list —
  **ETH: Etherscan-V2 → Blockscout → Moralis → free-RPC `getLogs`(recent window)**;
  **Base: Blockscout → Moralis → `getLogs`**;
  **BSC (+ every other chain): Moralis → `getLogs`** (SPEC-17 behavior, unchanged).
  A provider declares the chains it serves; an unsupported chain skips it silently (a BSC
  query never touches Etherscan — its free tier is **ETH-only**, live-verified 2026-07-02).
  Falls through on real errors; raises only when *all* providers fail. The getLogs fallback
  is recent-window only → `source:"getlogs"`, `partial:true` (a `verify_wallet` served this
  way may show `DISTRIBUTING` rather than `SEEDED-STAGING` because the lifetime seeding
  inbound predates the window — honestly flagged via `partial`).
  - **Etherscan-V2 reader** (`onchain._etherscan_tokentx`): `module=account&action=tokentx`
    via `https://api.etherscan.io/v2/api?chainid=1`, key = `etherscan_api_key` in
    `config/secrets.json` (absent → provider skipped, behavior identical to Moralis-first).
    Throttled (0.25s min interval, under the 5 rps cap) + 90s TTL cache + metered via
    `provider_quota`. Envelope quirk handled per §3: `status:"0"` + `"No transactions
    found"` is an **empty result** (a datum — no fallback, no false "no activity" error);
    a NOTOK (e.g. `"Free API access is not supported for this chain"`) IS an error →
    fallthrough + one stderr WARN per distinct reason.
  - **Blockscout reader** (`onchain._blockscout_tokentx`, SPEC-152): a second, keyless,
    Etherscan-compatible provider (same `module=account&action=tokentx` shape, reuses
    `_etherscan_map_row` rather than a third mapper) registered `chains={"ethereum","base"}`,
    positioned **after** Etherscan-V2, **before** Moralis — quota relief on ETH, and the
    desk's first real coverage on **Base** (SPEC-145's first live run found 4 unreadable HOME
    wallets there, "all 1 provider(s) failed"). Instance map is `config/blockscout.json`
    (chain → base URL); live-verified 2026-08-20: `eth.blockscout.com` and
    `base.blockscout.com` answer keyless HTTP 200, `bsc.blockscout.com` is 404 and
    `blockscout.com/bsc/mainnet` is 503 — **no official BSC instance exists**, so BSC stays on
    Moralis + local_index per the G2 v3 decision; do not add an unverified community instance.
    Single retry on failure, then a real `BlockscoutError` (falls through to the next
    provider) — same §3 empty-vs-error distinction as Etherscan.
  - **Deliberate cross-check** (`onchain.blockscout_cross_check(address, contract, chain_key,
    days, decimals)`, SPEC-152 req 4): separate from `token_transfers`'s first-success-wins
    fallback (which never calls a second provider once one succeeds) — runs the seam's normal
    winner AND Blockscout together and diffs row counts. A mismatch beyond `tolerance_pct`
    (default 10%) returns a caveat string naming **both** providers and **both** counts; a
    fallback that quietly disagrees with the primary is worse than none. No-ops (caveat=None)
    on a chain Blockscout doesn't cover, or when Blockscout itself was already the winner.
  - **Registration socket** (`onchain.register_provider(name, fetch, chains=None,
    before=None, enabled=None)`): SPEC-101 (Bitquery) / SPEC-102 (local indexer) / SPEC-152
    (Blockscout) register providers ahead of Moralis (`before="moralis"`) without touching
    the rest of `onchain.py`.
    `fetch(address, contract, chain_key, days, decimals) -> (moralis-shaped txs, partial)`.
    Consumers (`verify_wallet`, the radars, `counterfactual`) all read through
    `token_transfers` — no direct Moralis imports remain. Tests: `tests/test_provider_seam.py`.
- So a daily Moralis-quota exhaustion no longer blinds the desk: liveness stays free, recent
  activity still reads via getLogs, and only a total provider outage degrades.
- **Contract/pool probe** (`onchain.code_of` + `onchain.probe_contract`, SPEC-67, zero Moralis
  quota): `eth_getCode` (EOA vs contract) plus the V3 getters `token0()`/`token1()`/`fee()`.
  Both tokens resolve → `{is_pool:true, kind:"dex-pool"}`. Cached per address in
  `state/contract_probe_cache.json` (bytecode immutable). `verify_wallet` probes a tracked
  funding source before trusting its tier — a mislabeled pool can't make a swap-buyer read
  `SEEDED-STAGING` (the live ESPORTS false positive). `onchain.onboard_lint` reuses the probe
  to block silently tagging a pool/contract as a safe/hub (`contract_ok` required).

## distribution_radar / onchain_radar — the in-house "onchain radar" feed

Where `verify_wallet` checks a NAMED wallet, these **discover** the wallets — replacing the
external TG channel.

`distribution_radar` (SPEC 15) — finds distributing wallets across Cat A watchlist names:
```
python3 orchestrator.py distribution_radar '{"token":"ESPORTS"}'   # one token (~15s)
python3 orchestrator.py distribution_radar '{}'                     # full Cat A sweep (~5min, cadence tool)
```
- **Seeded-recipient discovery** = inverse of `verify_wallet.funded_by`: recent OUTBOUND from
  each tracked safe → the `to` addresses are freshly-seeded wallets. **Multi-chain (SPEC 21):**
  each safe is read on **its own** `chain` (not the token's default), and the nonce-gate sums
  tx-counts across **all** the token's chains, so a safe/candidate active on a non-default chain
  (e.g. Base for PLAY) isn't missed by discovery or false-cached as quiet.
- **Independent-holder discovery** = GoPlus top holders minus tracked/CEX/LP/contracts.
- Each candidate runs the `verify_wallet` check → **HIGH** = seeded-from-safe + distributing
  (operator distribution, the 0xbb58 case); **MED** = independent holder distributing; dormant dropped.
- **Behavioral clustering** (SPEC 15 refinement) — confidence ≠ funding lineage alone.
  `confidence = max(lineage, shared-destination)`: a wallet feeding the SAME sell-hub as a seeded
  (lineage-HIGH) wallet is promoted MED→HIGH (`confidence_reason:"shared-dest"`, `cluster:true`)
  even when its funding lineage is `unknown` — it's the operator's distribution fleet routing
  through an intermediary (the ESPORTS 0x2609 ⇄ 0xbb58 via hub `0x5bb5` case). The token result
  carries `sell_hubs` (each `{address, n_wallets, wallets}` fed by ≥2 distributors) as a discovery
  key — enumerate every wallet feeding an operator hub to find the whole fleet.
- **Perp-fused** (funding %/4h, short-vetoed) and **change-detected** (`state/radar_distributors_<T>.json`)
  — only NEW / accelerated distributors surface, no re-alerting.

`onchain_radar` (SPEC 16) — the full feed superset; one candidate pass/token yields:
```
python3 orchestrator.py onchain_radar '{}'      # distributors + accumulators + sellers + perp liqs
```
- **distributors** (as above) + **accumulators** (independent wallets net-BUYING on DEX in size →
  base forming / long setup) + **independent_sellers** tagged `holder_type` + `held_days`
  (cost-basis proxy: `early-winner` cashing out vs `operator-seeded` dump).
- **perp_liqs** — tracked-whale Hyperliquid positions (`config/hyperdash_whales.json`) with
  entry/liq/`liq_distance_pct` and a `near_liq` flag (cascade fuel before it fires; sorted nearest-liq).
- Perp-fused, ranked, change-detected (`state/or_dist_*`/`or_acc_*`). Cadence tool (~mins on the sweep).

## Relationship to `analyse` and `wallet_state`

- `analyse` sources its on-chain layer from `build_nonce_state` (this spine) — completes
  in ~2s, so `analyse LAB` returns `onchain:"OK"` (not the old `UNAVAILABLE`).
- `wallet_state` (snapshot mode) is the raw per-wallet nonce/gas grid; `onchain` adds the
  baseline-diff signal + composition on top.

## Gotchas

- Native; reads `crime-desk/config/tracked_wallets.json` + the RPCs there.
- `onchain:"UNAVAILABLE"` on a Cat A name is **P0 blindness** (you can't see the top
  signal), not "spectate calmly" — run the nonce read manually on the mega-safes.

## Surveillance sweep + cadence (`onchain_board`)

The baseline-diff only catches a fire when run **repeatedly** — so there's a board sweep:

```
python3 orchestrator.py onchain_board '{}'
```
Runs the cheap nonce signal (RPC only, no Moralis) across every watchlist token that has a
wallet map (~16 names, ~6s, token-concurrent), persists each baseline, and returns:
```json
{ "scanned":16,
  "alerts":[ {"ticker":"SKYAI","signal":"ESCALATION",
              "escalation_fired":[{"label":"SKYAI-EOA-CEX-20Mnonce","tier":"distribution","nonce_prev":...,"nonce_now":...}]} ],
  "loading":["LAB"],
  "board":[ {ticker,signal,score,fired_count,...}, ... ] }
```
`alerts` = `ESCALATION` only (an operator safe went dormant→fired). Exchange-side tiers
(`cex`/`dex`/`exchange`) and operational/MM hot wallets are excluded — they tick constantly.

**Run it on a cadence** so consecutive sweeps catch a fire:
- Durable (survives the session): `ops/surveil.sh` + `ops/com.crimedesk.nonce-surveil.plist`
  (launchd, every 15 min; logs ESCALATION → `state/nonce_alerts.log` + a macOS notification).
  Install: `bash ops/install_launchd.sh --only nonce-surveil` (renders the plist template for this machine + loads it)
- In-session (active trade, intelligent escalation): `/loop 15m` running the sweep + auto-`onchain`/`analyse` on any alert.

## Tests

`tests/test_onchain.py`: deterministic baseline-diff logic (escalation / op-&-cex-noise-ignored
/ loading / dormant / seeded / untracked), Moralis safe_history read, composed-picture shape +
coverage labels, the board sweep (escalation→alert, subset), and a live `<30s` no-hang smoke
(SPEC-123: gated behind `CRIMEDESK_LIVE_TESTS=1`, skipped by default — set it to run live).

## Primary-chain concentration (SPEC 56)
A token with onboard-resolved `primary_chain`/`chains_ranked` reads concentration where
the supply lives, not a BSC default. Split supply (2nd chain ≥20%) merges the top-2
reads — each holder tagged `chain` + supply-scaled `global_pct`. Every read carries
`coverage`: `{primary_chain, chains_read, unsupported:[{chain, supply_pct}],
unreadable_supply_pct}` — the FOLKS lesson: the engine must say how much it can't see.
Tokens without a chain ranking (BSC-native, pre-SPEC-56 entries) take the legacy path
unchanged. Avalanche is a first-class chain (GoPlus/Moralis/RPC wired).
