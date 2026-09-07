# trace_tree — recursive hop tracer with CEX-termination verdict (§8, SPEC-109, SPEC-116)

## Purpose
The desk's most repeated manual workflow was tracing a distribution/fragmentation/
rotation tree hop-by-hop with `verify_wallet.py`, one address per call (XPIN 2026-07-03:
~10 manual rounds to establish "zero CEX termination"). The discriminator that decides
these theses (rotation-pre-markup vs obfuscated exit) is always the same question: **does
any branch of the tree terminate at a CEX, and at what size?** `trace_tree` is that one call.

## Contract
```
trace_tree '{"root_addr":"0x1ac61c…","token":"XPIN","chain":"bsc"}'
trace_tree '{"wallet":"0x1ac61c…","ticker":"XPIN","depth":4,"min_usd":500,"max_fetches":30}'
```
`ticker`→`token` and `wallet`→`root_addr` aliases (same SPEC-89 pattern as `verify_wallet`).

Out:
```json
{ "available": true, "root": "0x…", "token": "XPIN", "depth": 3, "min_usd": 1000.0,
  "nodes": {"0x…": {"terminal": "cex", "label": "Binance Hot Wallet 6", "depth": 2}},
  "edges": [{"parent": "0x…", "child": "0x…", "amount_token": 80.0,
             "amount_usd": 4000.0, "label": null}],
  "branches": [{"path": ["0xroot…", "0xa…", "0xcex…"], "amount_usd": 4000.0,
               "label": "Binance Hot Wallet 6"}],
  "bridge_branches": [],
  "terminated": {"cex": {"usd": 4000.0, "token": 80.0}, "bridge": {"usd": 0, "token": 0},
                "dex": {"usd": 0, "token": 0}, "unresolved": {"usd": 0, "token": 0}},
  "cex_terminated": true, "bridge_terminated": false, "dex_terminated": false,
  "unresolved_contract_nodes": [], "fetched": 2, "max_fetches": 25,
  "truncated": false, "skipped": [],
  "verdict": "CEX_TERMINATED (branch 0xroot…→…→Binance Hot Wallet 6, $4,000 total across 1 branch)" }
```

## How it walks
BFS from `root_addr`, reusing `verify_wallet.build_verify` per node — its
`sell_destinations` gives the outbound edges AND their destination classification for
free, so this is a **selection/traversal layer, not a new fetch/vendor dependency**.

Per visited node, in order:
1. **Identity-terminal check** (no fetch): `config/bridges.json` first (SPEC-116) —
   an address seeded there → terminal `bridge`, **never recursed**, `trail_continues`
   (the destination chain set, or `["unknown"]`) carried onto the node. Then
   `verify_wallet._classify_addr` + `onchain.classify_destination` — `kind=cex` (or tier
   `cex-hot`/`exchange`) → terminal `cex`; `kind=dex` or a SPEC-94 `dex-router`/
   `dex-execution` label → terminal `dex`. **Never recursed** — a router/CEX/bridge's own
   onward flows are plumbing/custody/another chain, not the operator's tree on this chain.
2. **Depth-exhausted**: `depth >= --depth` → terminal `open` (branch continues, unexplored
   — never silently reported "clean").
3. **Budget-exhausted**: the fetch cap (`--max-fetches`, default 25 — Moralis free-tier
   quota is the real constraint) is hit → terminal `open` + added to `skipped`,
   `truncated: true`.
4. **Contract guard** (SPEC-116 §4): for any node not already tracked/labeled, a cheap
   `probe_contract` bytecode check runs before the fetch — an unlabeled contract (a pool,
   router, or bridge adapter the seed lists missed) becomes terminal `unresolved_contract`
   and is **never recursed as if it were a hop wallet**
   (`feedback_probe_contracts_before_labeling_wallets`).
5. Otherwise: fetch (`build_verify`), prune destinations below `--min-usd` (default
   $1000), and either recurse into the survivors or, if none survive, terminal `resting`
   (no real outs in the window).

## Cross-chain continuation (SPEC-116 §3)
After the BFS, every `bridge`-terminal node gets one extra check: the **traced EOA that
sent to the bridge** (its parent in the tree — never the bridge contract itself) is
re-queried via `build_verify` on one of the token's *other* deployed chains — `trail_continues`
narrows the candidate chain(s) when the bridge's config entry names them, otherwise every
other queryable chain is a candidate and the first is used. This catches an operator
reusing the same address cross-chain (the XPIN pattern), not a full cross-chain graph trace
(explicitly out of scope). The result lands on the bridge node as `continuation`:
```json
"continuation": {"available": true, "chain_checked": "ethereum", "cex_terminated": true,
                 "amount_usd": 2500.0, "verdict": "cross-chain CEX termination on ethereum: $2,500"}
```
Provider-gated and quota-aware — it shares the walk's `--max-fetches` budget. A failed read
(`available: false`) **never carries a `cex_terminated` key** — the caller must not read a
failed continuation as a clean bill (§3). `continuation` is `null`/absent when there's
nothing to check: `chain` was left unrestricted (the main walk already covered every
queryable chain) or the token has no other deployed chain.

## Verdict string
- `CEX_TERMINATED (branch <root>→…→<label>, $X total across N branches)` — X = summed USD
  of the direct edge into each CEX-terminal node across all branches that hit a CEX.
- `NO_CEX_TERMINATION (M leaf wallets resting, deepest depth D, $Y still in tree)` — Y =
  total USD moved anywhere in the traced tree (none of it exited to a CEX).
- `  BRIDGE_TERMINATED: $X across N bridges — trail continues cross-chain` is appended
  whenever any bridge hop is present — **even on a tree with zero CEX hops**, so
  `NO_CEX_TERMINATION … BRIDGE_TERMINATED: $X` reads as "trail continues on another chain",
  never as clean/quiet re-staging (the XPIN 2026-07-03 caveat this spec fixes: "zero CEX
  termination" silently meant zero *same-chain* CEX termination).
- `  OPEN_BRANCHES: n` is appended whenever any depth-exhausted or budget-skipped branch
  remains — the silent-truncation rule: a tree with unexplored branches is never "clean".

## Verdict decomposition (`terminated`)
`terminated.{cex,bridge,dex,unresolved}` — `{usd, token}` summed over the direct
parent-edge into every node of that terminal kind. `unresolved` covers `resting` +
`open` + `unresolved_contract` leaves (size still sitting in the tree, not confirmed
exited anywhere). A tree with no bridge hops produces byte-identical `verdict`/`branches`
to pre-SPEC-116 output — `terminated.bridge` is just `{"usd": 0, "token": 0}` and
`bridge_terminated`/`dex_terminated` are `false`.

## Gotchas
- Price is fetched ONCE (`verify_wallet._live_price`) and reused across every node's
  `build_verify` call — same one-fetch-per-token pattern the radars use, not N live-price
  calls for N nodes.
- `--min-usd` gates on **USD**, not token quantity — if the live price is unavailable the
  gate falls back to raw token amount (documented in the return via `price_usd: null`,
  never silently drops every edge).
- A node whose own `build_verify` read comes back `available:false` (all providers
  failed) is reported `resting` + `degraded:true` — a failed read is never conflated with
  a genuinely quiet wallet.
- Router/CEX/bridge terminal nodes are classified from **labels/tracked config**
  (`known_entities`/`tracked_wallets`/`config/bridges.json`), not a fresh on-chain probe —
  cheap and matches the same plumbing `verify_wallet`/`onchain.classify_destination`
  already use everywhere else.
- `config/bridges.json` is desk-editable seed data (`{"bridges": {"0x…": {"name": "…",
  "trail_continues": ["eth", …]}}}`) — the top ~8 BSC/ETH bridges is enough; an unseeded
  bridge just degrades to pre-SPEC-116 behavior (recursed like any unknown wallet), no
  worse than today.

## Tests
`tests/test_trace_tree.py` (stdlib `unittest`, fully offline — `verify_wallet`'s
`token_transfers`/`_live_price`/`_quote_transfers`/`balance_of`/`probe_contract`/
`_entity_labels`/`queryable_chains` and `trace_tree._load_bridges` monkeypatched exactly
like `tests/test_verify_wallet.py`): CEX-terminated path + summed USD, fresh-wallet resting
leaves, a labeled router never recursed, min-usd pruning, a fetch-budget cap that truncates
+ marks `OPEN_BRANCHES`, a seeded bridge terminal (never recursed, sized, `trail_continues`
populated), an XPIN-shaped fresh-wallet-tree-plus-one-bridge-hop verdict distinct from an
all-quiet tree, cross-chain EOA continuation to a CEX (available + provider-absent →
`continuation` never carries `cex_terminated`), an unlabeled contract mid-tree becoming
`unresolved_contract` and never recursed, and a no-bridge-hops regression check on the
`terminated` decomposition's zero fields.
```
python3 tests/test_trace_tree.py
```
