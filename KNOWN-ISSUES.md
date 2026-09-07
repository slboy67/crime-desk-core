# KNOWN ISSUES — features that are buggy, incomplete, or not self-contained

This repo is the curated copy of `crime-desk` (taken 2026-09-07). Everything in it passed the
test suite at copy time (see README for the count). The list below is every feature that is
**included but has a documented defect**, or that was **left out** because it does not work
standalone. Sources: the open coder tickets in the original repo (`handoffs/specs/open/SPEC-*`),
the three unmerged coder branches, and `ARCHITECTURE.md §8`.

## A. Excluded — not self-contained (depends on the legacy parts bin `_oldrepo/`)

The original repo symlinks `_oldrepo` → an untested legacy repo outside git. That code is
NOT copied here (ARCHITECTURE §8 calls it a known invariant violation). The features below
call into it and therefore degrade or fail in this repo until natively ported:

| Feature | What breaks without `_oldrepo` | Where |
|---|---|---|
| `oi_sides` (registered as `status: delegated`) | Whole capability: invoke is `_oldrepo/scripts/oi_sides.py` | `capabilities.json`, `filters/oi_sides.py` |
| `setup_score` wash veto | `_run_oi_sides` shells to the parts bin; degrades to "oi_sides unavailable" so the WASH veto on 5/7 setups never fires | `capabilities/setup_score.py:751` |
| `workup` wash/real OI tag | same shell-out; field omitted | `capabilities/workup.py:58` |
| `oi_mc` leverage_state WASH override | consumes the oi_sides tag; without it the override is inert | `capabilities/oi_mc.py:190` |
| `analyse` sub-analysers | `run_json` runs every legacy sub-analyser from `_oldrepo/scripts`; returns `_err` per leg (native CVD leg still works) | `capabilities/analyse.py:180` |
| `onchain` Moralis layer (and everything importing it: `onchain_board`, `verify_wallet`, `accumulation_radar`, `distribution_radar`, `onchain_radar`, `brief` on-chain block, `backtest_accumulation`, `ops/balance_surveil.py`) | `_moralis()` loads `_oldrepo/scripts/moralis.py`; Moralis lifetime reads raise → callers report `available:false`/degraded. The keyless RPC nonce/balance/concentration reads still work | `capabilities/onchain.py:460` |
| `pull5 --onchain` layer | shells to `_oldrepo/scripts/onchain_analyser.py`; returns `_error` | `capabilities/pull5.py:193` |
| `wallet_state` audit mode | shells to `_oldrepo/scripts/safe_audit.py`; snapshot mode is native and works | `capabilities/wallet_state.py:139` |

**Needs work:** native ports of `oi_sides.py`, `moralis.py`, `safe_audit.py`, `onchain_analyser.py`
(the first is the roadmap's Phase 3a item).

**Curation applied in this copy (the only two code-level edits vs the original):**
1. The `oi_sides` registry entry was deleted from `capabilities.json` (its invoke target cannot exist
   here; the registry lint `tests/test_registry_docs.py::test_invoke_targets_exist` fails otherwise).
   `filters/oi_sides.py`, `docs/oi_sides.md` and the filter's own tests are kept for the port.
   The one test that asserted the registry wiring (`tests/test_orchestrator.py`) was replaced by a comment.
2. `config/tracked_wallets.json` is the **committed** version from the original's `main`, not the
   working tree: the working tree carried an uncommitted 1,275-line auto-onboarding block for nine
   tokens (PONS, TUT, ACE, USELESS, MARSCOIN, AKE, FLOCK, IOST, DGAI) with `tier: unclassified` and
   `GOPLUS-TOP` labels, which fails the SPEC-92 config lint
   (`tests/test_spec92_exchange_infra_reclassify.py`, 2 failures). Those rows need classifying
   before they are valid config. The other live config files (`watchlist.json`, `positions.json`,
   `catalysts.json`, `aster_max_leverage.json`) are the working-tree versions as of 2026-09-07.

## B. Excluded — not built yet (open tickets, no code on main)

- **SPEC-199** `aster_wallet` — read any Aster address (balance/positions/tx) from the keyless explorer endpoint.
- **SPEC-200** Aster watchlist reader — public-address subset sampler.
- **SPEC-201** Aster per-symbol index constituents → automate `config/dex_mark_weight.json`; keyless max-leverage brackets. Until then `config/dex_mark_weight.json` mirrors **Binance's** index, not Aster's, and is a wrong risk input for names like SKYAI/ASTER.
- **SPEC-190** `funding_settles` + `positioning` capabilities and the `aster_equiv` level translation in `venue_bars`; plus three loud-fail fixes (`scan mode=oi_surge` all-null board, `ledger` orchestrator arg alias, `brief` contract/chain override).

## C. Included, but with known defects (fix tickets open in the original repo)

### `classify` / `thesis` (the board) — SPEC-195, SPEC-196, SPEC-197 (P1)
- `time_stop` written as an ISO deadline is invisible to the validator/parser/evaluator; only `time_stop_h` is understood. The time-stop BREAK and the `time_stop_elapsed` retire flag never fire on rows using the ISO key (18/19 live rows at copy time).
- `status: WATCH` rows are treated as live: the pre-entry guard in `eval_price_leg` only recognises `PENDING`, so WATCH rows can false-BREAK on pre-entry highs. `RESOLVED-*` rows are still evaluated every tick.
- `config/watchlist.json` has three writers (`thesis.py` locked; `regime_flip.py` and `classify.py` unlocked/unvalidated); `legs[]` geometry is not modelled by `validate_thesis`/`parse`; `entered_ts` is never set from a venue fill.
- Verdict logic is duplicated across nine sites in `classify_token`, `board_tick.diff`, and `counterfactual` with different thresholds.

### `orchestrator.py` — SPEC-195 C
- A `[--flag]` optional group with no `{key}` is always emitted. Affected: `scan` always gets `--include-hl`, `thesis` always `--confirm-signature`, `size` always `--propose-stop`.

### `regime_flip` — SPEC-195 D
- A failed Binance OI fetch is coerced to `0.0` instead of `None` (a manufactured datum).

### `verify_wallet` — SPEC-195 E, SPEC-185
- Cannot distinguish a quota-dead chain from an empty one; a wallet on a quota-dead chain reads DORMANT with `available:true`.
- Base-chain reads return all-None with `ok:true` (SPEC-185). A fix exists on the unmerged branch `coder/auto-20260907-161634` in the original repo; it is NOT in this copy.

### `price_structure` — SPEC-184
- The kline resolver can serve a dead/stale symbol series as a plausible structure (HNT: a single 2024 bar). Fix on the same unmerged branch as above; NOT in this copy.

### `scan` (faded_bounce fan-out), `price_structure`, `depth`, `venue_map`, `venue_bars`, `oi_mc` — SPEC-202 (P1)
- Fan-out sweeps have no shared per-host rate budget or kline cache; three back-to-back `scan '{"mode":"faded_bounce"}'` runs got the desk IP banned on Binance (HTTP 418, ~35 min) and Aster. Run at most one sweep per session. A fix (`capabilities/venue_http.py`) exists on the unmerged branch `coder/auto-20260907-185445`; NOT in this copy.

### `cvd` / `brief` — SPEC-198
- `cvd_divergence` and `perp_aggressor` (the SPEC-191 addendum) are not implemented.
- Docs missing for the SPEC-191/192 changes (`cvd`, `brief` venue_breadth, `risk_card`, `venue_map` curated-vs-snapshot split, `nonevm_chains`, extended `onchain` chain maps).
- `oi_construction.venue_roles.size_book` is not proven to consume the filled OI set.
- `config/aster_max_leverage.json` is live-written by ticks (tracked config mutated at runtime); should move to `state/`.

### `maxsize` — SPEC-194
- Doc/wording still says "Pyth/Chainlink oracle"; stale.

### `oi_construction` (SPEC-179/180)
- Working but bounded by design: the on-chain injection points (`chip_state` / `flow_confirmed` / `lock_info`) have no wired source, so most reads land `UNKNOWN`. Correct behaviour per §3, not a bug, but the layer is incomplete.

### Coder pipeline (`ops/coder_dispatch.sh`, `ops/premerge.sh`) — SPEC-193/194
- Passes `--max-turns`, which the installed `claude` CLI ignores; `--max-budget-usd` is the only real cap.
- Both scripts expect `_oldrepo` and `config/secrets.json` to exist to link into worktrees.
- Machine-specific: launchd plists + claims under `~/Library/Application Support/crimedesk/`; `ops/install_launchd.sh` renders them.

### `mindshare_top_short` signature (CLAUDE.md §2)
- Zero live fills; the only record is a quarantined mechanical replay at −26.3R. Unvalidated.

## D. Not copied on purpose (not features)
- `state/` (runtime: ledger, baselines, samples — the ledger `state/ledger.jsonl` stays in the original repo).
- `handoffs/` ticket archive (136 done specs, 75 review requests, 13 open), `reports/` (desk posts; one research file kept because tests cite it), `EVAL-*.md`, `PRODUCT-perp.md`, the on-chain workshop PDF/MD, `.playwright-mcp/`, `.scratch/`, `worktrees/`.
- `config/secrets.json` → replaced by `config/secrets.example.json` (same keys, empty values).
- `config/positions.json.bak-*`.
