# onboard — config-driven token onboarding (SPEC 47)

## Purpose
Five of the first 43 SPECs were "onboard token X" — full coder dispatch + review cycles
for what is config work, each adding days of latency on names where CLAUDE.md §8 makes
on-chain mandatory. `onboard` composes the resolver pieces that already exist (SPEC 36
collision-safe coingecko resolution, SPEC 12/34 GoPlus holders, the nonce-baseline
seeder) into one call. After it runs, `onchain TICKER` reads tracked instead of
UNTRACKED.

## Contract
```
onboard '{"ticker":"X"}'                          → resolve + write skeleton + candidates + baseline
onboard '{"ticker":"X","chain":"bsc"}'            → restrict contracts to one chain (bsc/eth/base/…)
onboard '{"ticker":"X","coingecko_id":"solstice"}' → SPEC 52: explicit id, skips symbol search
```

Failure reasons are stage-named (SPEC 52) — never a bare "fetch failed":
`resolve_ambiguous` (+ `candidates:[{id,symbol,name,market_cap_rank}]`, no write — retry
with the chosen `coingecko_id`), `resolve_not_found`, `contract_fetch_failed:<id>`,
`goplus_failed:<why>` (still onboards the skeleton), `baseline_failed:<why>` (config
landed; re-run to seed).
Output (new token): `{ok, ticker, cg_id, low_confidence, contracts, candidates_written,
needs_judgment:[{address, balance_pct, hints:{contract, locked, cex_label}}],
baseline_seeded}`.
Output (already tracked): report-only `{ok, already_tracked:true, contracts, wallets,
tiers, needs_judgment, baseline_seeded}` — **never clobbers classified tiers**.

## Onboard by explicit contract (SPEC-165) — no CoinGecko dependency
```
onboard '{"ticker":"DEBIT","contract":"0x6666…ce49","chain":"binance-smart-chain"}'
```
Fresh BSC->Alpha names (the desk's core Cat-A class) have no CoinGecko entry on day
one — unaided resolution fails exactly when it matters (DEBIT day-1, KORU
`resolve_ambiguous`). When `contract` is given, CoinGecko is skipped entirely:
decimals/name/totalSupply are read straight off-chain (`onchain.erc20_meta`, RPC-only,
zero paid quota), and GoPlus top holders are pulled the same way as the resolved path.
Requires `chain`; both bad input paths are loud structured errors, never a traceback:
`contract_requires_chain` (chain missing), `bad_contract` (not a `0x` + 40-hex address),
`unsupported_chain:<chain>` (no GoPlus/RPC support). An RPC identity read or a GoPlus
read failing degrades non-fatally — the contract skeleton still lands
(`token_meta.meta_available:false` / `holders_source:"goplus_failed:<why>"`); only a bad
`chain`/`contract` argument itself blocks the write.
Output: `{ok, ticker, onboarded_by:"contract", contracts, primary_chain,
token_meta:{name, decimals, total_supply, meta_available}, candidates_written,
needs_judgment, baseline_seeded}` — same `needs_judgment`/hard-guard shape as the
resolved path, so downstream tiering is identical either way.

## Sweep mode (SPEC 51)
`onboard '{"all":true}'` onboards every watchlist name missing from
`config/tracked_wallets.json` in one call — per-token output `{ticker, status:
mapped|already|failed, reason, candidates?, needs_judgment}`; one failure never aborts
the sweep, and a fully-mapped watchlist returns `swept:0` with zero network calls.
`board_tick` runs this each tick (set-difference pre-check), so a token added to the
watchlist is mapped within ~15 min unprompted; `brief` on an unmapped name runs the
onboard inline first and never blocks the perp read (failure surfaces as
`UNMAPPED(onboard_failed:<why>)`).

## Primary-chain resolution (SPEC 56)
Multi-chain tokens are ranked by where the supply actually lives: a GoPlus supply/holder
read per supported chain → `primary_chain` + per-chain `supply_pct` in the config entry.
Bridge stubs (<1% supply or <25 holders) never become primary. Non-EVM chains (algorand,
sei, …) are explicit `supported:false` entries, and `unreadable_supply_pct` says exactly
how much supply the engine cannot see (FOLKS: 94.66% Algorand-native — said out loud,
never a silent zero). Candidates and the nonce baseline come from the primary chain.
Single-chain tokens skip the ranking reads entirely.

## Hard guard — no auto-tiering
SPEC 20 proved tier classification (bridge vs distribution vs seed) is the judgment call
that decides false-fire behavior. Candidates land as `tier:"unclassified"`, which is NOT
in `verify_wallet.SEED_TIERS` — they are excluded from radar seed-discovery and the
escalation seed set until the Designer classifies them. `needs_judgment` carries the
balance % and hints (is-contract / is-locked / CEX tag) so tiering is one read: edit the
wallet's `tier` in `config/tracked_wallets.json`.

## Example
```
$ python3 orchestrator.py onboard '{"ticker":"TT","chain":"bsc"}'
{"ok": true, "data": {"ok": true, "ticker": "TT", "already_tracked": false,
  "cg_id": "testtoken", "low_confidence": false,
  "contracts": {"binance-smart-chain": "0xabc…01"}, "candidates_written": 3,
  "holders_source": "goplus:binance-smart-chain",
  "needs_judgment": [{"address": "0x11…11", "balance_pct": 40.0,
                      "hints": {"contract": true, "locked": true, "cex_label": null}}],
  "baseline_seeded": true}, …}
```

## Gotchas
- `low_confidence:true` means the SPEC-36 resolver wasn't sure of the coingecko id
  (symbol collision / price divergence) — eyeball `cg_id` before trusting the contracts.
- Burn sinks (`0x0…0`, `0x…dEaD`) are dropped from candidates (SPEC 37: removed supply,
  not a holder).
- GoPlus-unavailable still onboards the skeleton + baseline; `needs_judgment` is just
  empty — re-run later or add wallets by hand.
- A failed baseline sweep does not roll back the config write; re-running seeds it
  (the report path shows `baseline_seeded`).
