# crime-desk-core

Curated, self-contained copy of the `crime-desk` trading-desk engine, taken 2026-09-07:
the orchestrator, the capability registry, every native capability with its test and doc,
the config the capabilities read, the ops ticks, and the methodology docs the engine
references (`CLAUDE.md`, `ARCHITECTURE.md`, `CONTEXT.md`, `memory/`).

**What is deliberately not here** and **which included features carry known defects** is
in [`KNOWN-ISSUES.md`](KNOWN-ISSUES.md). Read it before relying on any on-chain read, the
board's time-stop, or fan-out scans.

## Test status at copy time

| Tree | Result |
|---|---|
| this repo, standalone (no `_oldrepo`, no secrets) | `Ran 2459 tests — OK (skipped=46)` |
| original `crime-desk` working tree, same day | 2460 tests, 2 failures (uncommitted config lint — see KNOWN-ISSUES §A) |

The 46 skips are live-network tests (gated by `CRIMEDESK_LIVE_TESTS`).

Run it yourself (stdlib `unittest`, no pytest; live network is blocked unless
`CRIMEDESK_LIVE_TESTS=1`):

```
python3 -m unittest discover -s tests
```

## Setup

```
pip install -r requirements.txt            # only eth_account (Aster EIP-712 signing)
cp config/secrets.example.json config/secrets.json   # fill in the keys you have
python3 orchestrator.py classify '{"render":"compact"}'   # the board
```

Every call returns `{"ok":true,"data":...,"meta":...}` or `{"ok":false,"error":...}`.

```
python3 orchestrator.py brief '{"ticker":"X","render":"compact"}'   # one name, all layers
python3 orchestrator.py scan '{"mode":"faded_bounce"}'               # discovery sweep (ONE per session — see KNOWN-ISSUES §C)
python3 orchestrator.py venue_map '{"ticker":"X"}'                   # venue roles
python3 orchestrator.py tape '{"ticker":"X"}'                        # per-bar OI force
python3 capabilities/ledger.py stats                                 # the desk's record
```

Optional launchd surveillance ticks: `bash ops/install_launchd.sh` renders the
`ops/com.crimedesk.*.plist` templates for this clone.

## Layout

```
orchestrator.py     switchboard: {capability, args} → runs the script → clean JSON
capabilities.json   registry (46 entries; each names its test + doc)
capabilities/       61 modules: 44 registered capabilities + library modules
                    (risk_card, counterfactual, operator_veto, venue_account, ...)
filters/            output filters for the two non-native paths (analyse, oi_sides)
tests/              159 test files
docs/               one doc per capability + docs/adr/
config/             curated + live desk config (watchlist, tracked wallets, clusters, ...)
ops/                board/discovery/funding/nonce/OI ticks, telegram poster, coder pipeline
memory/             the ported methodology lessons CLAUDE.md points to
handoffs/           GOAL-coder.md + fixtures + empty specs/{open,in-review,done}
state/              (gitignored) runtime: ledger, baselines, samples
```

## Registered capabilities

| Capability | Area |
|---|---|
| `classify`, `regime_flip`, `thesis`, `triage`, `brief`, `tape`, `inbox`, `onboard` | board / thesis state machine |
| `scan`, `screener`, `perpfinder`, `sector_divergence`, `phase`, `setup_score`, `workup`, `replay` | discovery + scoring |
| `regime_check`, `price_structure`, `liq_magnets`, `pull5`, `cvd`, `depth`, `liqs`, `venue_map`, `venue_bars`, `oi_mc`, `oi_construction` | perp / venue layer |
| `onchain`, `onchain_board`, `wallet_state`, `verify_wallet`, `onchain_radar`, `accumulation_radar`, `distribution_radar`, `trace_tree`, `drip_seller`, `deposit_breadth`, `claim_topup`, `stake_schedule`, `unlocks`, `dex_execution` | on-chain layer |
| `size`, `maxsize`, `ledger` | risk + record |
| `analyse`, `oi_sides` | legacy-delegated (see KNOWN-ISSUES §A) |

Origin: `~/dev/crime-desk` (private, no remote). This copy was made read-only from that tree.
