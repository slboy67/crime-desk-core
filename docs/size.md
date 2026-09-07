# size — mechanical §7 sizing gate (SPEC 41)

```
python3 capabilities/size.py SKYAI --direction SHORT --entry 0.37 --stop 0.40 --json
python3 capabilities/size.py SKYAI --direction SHORT --entry 0.37 --stop 0.40 --equity 25000 --json
python3 capabilities/size.py LAB --direction LONG --entry 1.10 --stop 1.02 --band 3 --mc 45000000 --json
```

Turns CLAUDE.md §7 ("sizing IS the edge") into one deterministic READ: the liquidity
gate first, then every sizing constraint with its own number, and **which one binds**.
Read-only — it sizes nothing, commits nothing.

## Order of operations

1. **Liquidity gate FIRST (short-circuit).** From `regime_flip.live_perp` (cross-venue
   24h vol) + optional `--mc`:
   - 24h vol < **$10M** or MC < **$15M** → `gate:"AUTO-PASS"`, `constraints:null`,
     `binding:null` — **no constraint math, no book fetch**. Not tradeable, stop reading.
   - vol **$10–25M** → `gate:"SCOUT-ONLY"` — constraints computed normally, but the
     final `max_size_usd` is haircut **50%**.
   - otherwise `gate:"FULL"`. (vol unavailable → FULL + an explicit "gate NOT verified" note.)
2. Constraints (below), then `binding` = the smallest available $ cap.

## Constraints

| key | rule | source |
|---|---|---|
| `exit_absorbable_usd` | $ resting within `--band`% (default 2%) of mid on the **EXIT side**, summed across BOTH venue books | `depth.py` fetchers (Binance + Bitget), reused not duplicated |
| `oi_cap_usd` | ≤ **3%** of primary-venue OI (`oi × price` from `live_perp`) | §7 reduce-only trap |
| `bracket_cap_usd` | ≤ **70%** of tier-1 leverage-bracket notional | `config/brackets.json` |
| `leverage_max` / `leverage_practical` | `100 / stop_distance_pct`; practical = **75%** of max | entry/stop args |
| `leverage_cap_usd` (only with `--equity`) | `equity × leverage_practical` | — |
| `cluster` | operator-cluster heat (warning, not a $ number) | `config/clusters.json` + `config/watchlist.json` |

### Exit-side convention (direction-aware)

**A SHORT exits into the BIDS; a LONG exits into the ASKS** — spec-literal, the desk's
convention: your exit is absorbed where price is heading, and what you can actually
carry is bounded by the resting shelf that will be there to cover/sell into (for a
short, the defended bid shelf below — the SKYAI Bitget-bid lesson; for a long, the ask
side you distribute into). This deliberately ignores the order-mechanics quibble
(a market buy technically lifts asks) — the §7 question is "what does the *destination*
side of the book absorb", and that is the side the trade exits *toward*.

### Truncated books (floor estimate × 0.5)

`depth`-style merge-depth windows cap ~100 levels and can end *inside* the band
(memory: `feedback_orderbook_api_truncation_defer_to_live_dom`). When the exit side's
deepest visible level is closer than the band, the visible $ is a **floor estimate** of
a window we can't see past — it is counted at a **0.5 haircut** (conservative: don't
trust an unverifiable window for full size, but never read it as "no liquidity").
Per-venue detail in `exit_detail` (`truncated`, `haircut`, `usd_counted`); a note is
emitted telling you to defer to the live DOM.

### Bracket cap — config, not API

Binance's `GET /fapi/v1/leverageBracket` is auth-gated, so `config/brackets.json` is
the source of truth: `default_tier1_usd` (50,000) → per-venue `venues{}` → per-ticker
`overrides{}` (overrides win). **Override path:** read the real tier-1 max notional off
the venue's leverage & margin table and pin it under `overrides` for the ticker.

### DEX-mark flag

`config/dex_mark_weight.json` maps ticker → known DEX-pool index weight (0–1).
Weight > 0.30 → `haircut_dex_mark:true` + a note (§7: composite-index marks can wick
you off a venue you're not trading — haircut leverage further). Curated, not derived;
full index decomposition is out of scope.

### Cluster heat

`config/clusters.json` holds curated operator clusters (e.g. XTOKEN-MM:
BILL/BSB/EDEN/LAB/SKYAI/OPN). If any *other* member of the ticker's cluster has a live
thesis on the watchlist (`thesis.status` `ACTIVE*` or `PENDING`),
`cluster_open_risk_warning:true` fires with the open members listed — cluster-mates are
ONE position at multiplied size (~6% combined cap, `max_operator_heat_pct`); a
long+short pair across cluster-mates is NOT a hedge.

## Equity is never assumed

Without `--equity` you get every $ constraint + `binding`, but **no** `max_size_usd` —
the capability does not pretend to know the account. With `--equity`:
`max_size_usd = min(all $ caps incl. leverage_cap) × (0.5 if SCOUT-ONLY)`, plus
`max_size_note` naming the binding constraint.

## Output shape

```json
{"ticker":"SKYAI","direction":"SHORT","gate":"FULL",
 "gate_detail":{"vol_m_24h":78.7,"mc_usd":null},
 "exit_side":"bid","exit_detail":{"binance":{...},"bitget":{...}},
 "constraints":{"exit_absorbable_usd":215000,"oi_cap_usd":561000,
   "bracket_cap_usd":35000,"leverage_max":12.33,"leverage_practical":9.25,
   "cluster":{"name":"XTOKEN-MM","open_members":[...],"cluster_open_risk_warning":true}},
 "binding":"bracket_cap","haircut_dex_mark":false,
 "stop_distance_pct":8.108,"notes":["..."]}
```

`max_size_usd` appears only when `--equity` is passed. AUTO-PASS returns
`constraints:null, binding:null` — the short-circuit is the verdict.

## Stop geometry + entry-type guard (SPEC 61, `--propose-stop`)

Two realized −1R lessons as code: ESPORTS (a stop beyond the 2h-tape wick got out-wicked
by the true-range leg-9 wick) and the BEAT 8.21-under-8.365 near-miss (a stop anchored to
a *partial* window).

```
python3 capabilities/size.py ESPORTS --direction SHORT --entry 0.105 --stop 0.111 --propose-stop --json
```

- **`propose_stop(direction, entry, …)`** (pure): the proposed stop is **beyond the TRUE
  relevant wick** = `max(24h extreme, full-history window extreme)` on the stop side (SHORT →
  above the highest wick; LONG → below the lowest), plus a buffer, with a **magnet-aware
  nudge** so it never lands AT a round level (§7 — a stop on the magnet is donated to the
  sweep). Output names the `cleared_wick` and `cleared_source` (`24h_extreme` |
  `window_full_history`). The live wrapper `propose_stop_live` gathers the 24h range
  (`classify._ticker_hl`), the full-history window (`price_structure`, scoped to the current
  ~30d leg), and the round magnets (`tape.round_numbers_near`).
- **`entry_type_guard(…)`**: when the squeeze-history pre-check fires (**>1 leg/10d over the
  window** — ESPORTS 8/60d = 1.33/10d), output `entry_type_required:"post_breakdown"` and a
  warning if the caller's entry sits inside the retest zone (within 3% of the adverse wick).
  Encodes `feedback_squeezer_wicks_escalate`: on chronic squeezers take only the momentum
  entry AFTER the level breaks.
- With `--propose-stop`, `build_size` attaches `stop_proposal` + `entry_type_required`, and a
  note when the **committed stop is INSIDE the proposed true-wick stop** (it can be out-wicked).

**Commit-time integration (`thesis`):** a `thesis` commit calls the same check and attaches
`geometry_warning` to its response when the committed stop sits inside the proposed
full-history true-wick stop — extending SPEC 54's churn check with the full-history wick.

Tests: `tests/test_size.py` + `tests/test_stop_geometry.py` (all fetches mocked / pure
functions fixture-driven, offline-deterministic).
