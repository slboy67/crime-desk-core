# workup — the §0.6 five-question scan as ONE call (SPEC 60)

```
python3 capabilities/workup.py '{"ticker":"ESPORTS"}' --json
```

The Designer ran 8-10 manual calls per full scan this week (PLAY, ESPORTS, FOLKS). The
assembly is mechanical; `workup` returns the whole §0.6 dossier in one call so the
Designer spends their context on **judgment**, not gathering. It **re-derives nothing** —
it composes the already-merged capabilities concurrently.

## The five sections (§0.6)

| # | section | source | carries |
|---|---|---|---|
| 1 | `chips` | `onchain` | concentration (top1/top10/holders) · tracked signal/bias/score · holder flags (contract/locked/burn) · `coverage` (unsupported-chain supply + unreadable %) · `newly_fired` nonces · `apparatus_balances` (wallets the config marks `watch_balance`) |
| 2 | `stage` | `price_structure` (full-history, SPEC 57) | `ath_alltime` + `prior_cycle` · `range_pos` · `off_ath_pct` / `off_ath_alltime_pct` · `squeezes` + `squeeze_pattern` · `structure` |
| 3 | `oi_construction` | `regime_flip.live_perp` + `oi_sides` | cross-venue normalized funding %/4h (floor-sentinel via `all_floor`) · `oi` · `chg24` · per-venue `venues` · `funding_divergence_4h` (max−min spread) · `oi_sides_tag` (WASH/REAL) |
| 4 | `size` | `live_perp` gate + `depth` + `cvd` | `liquidity_tier` (DUST/SCOUT/FULL) · `oi_cap_usd` (3% OI) · both venue `books` (shelves/walls/spoof/truncation) · spot venue/vol · `cvd_verdict`/`cvd_reliable` |
| 5 | `rr` | `liq_magnets` + `setup_score` (SPEC 59) | HVN/LVN + round-number `magnets` · `setup_score` (all setups) · `missing_legs` per setup |

Plus **`flags`** (computed from the already-fetched sections — no extra reads):
`squeeze_chronic` (>6 daily squeeze legs in the window), `venue_mark_divergence_pct` +
`_flag` (>2% mid divergence = composite-mark wick risk, §7), `liquidity_tier`,
`alerts` (inbox unconsumed for the ticker).

## Contract

- **NO verdict field.** The dossier informs; the Designer judges (§0/§0.6 division). Each
  section names its `source`. (`setup_score`'s per-setup `verdict` is a score state —
  ARMED/FORMING/VETOED/ABSENT — not a trade call.)
- **Degrade per section.** A failed/slow read returns `{available:false, reason}` for THAT
  section only — one dead read never aborts the dossier. Sections run concurrently
  (`ThreadPoolExecutor`), each under an 18s budget → `meta.ms` ≈ the slow read, not the sum.
- `setup_score` reuses the signals **derived from the sibling sections** (stage / oi /
  size / chips), so it adds no extra network and stays offline-deterministic in tests.

## Notes

- Read-only. Commits nothing, sizes nothing — a §0.6 READ that respects the state machine.
- `apparatus_balances` is best-effort: empty when no `watch_balance` wallets are configured.
