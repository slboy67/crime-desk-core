# onchain_board — nonce surveillance sweep (SPEC 5/24/28)

## Purpose
The cheap standing sweep: nonce signal across all tracked watchlist names, alerting on
ESCALATION — a dormant operator safe going dormant→fired, CLAUDE.md §8's "biggest
Stage-5 escalation". This is what ops/surveil.sh runs every 15 min; SPEC 45's inbox
ingests its alerts.

## Contract
```
onchain_board '{}'
```
Out: `{scanned, alerts:[{ticker, signal, escalation_fired:[{label, tier, nonce_prev,
nonce_now}], escalation_kind, wallets_total, wallets_read, wallets_unreadable,
unreadable:[{address,chain,reason,ticker}], …}], loading, nonce_churn,
wallets_total, wallets_read, wallets_unreadable, unreadable:[…],
coverage_alerts:[{severity:"HIGH", kind:"coverage_collapse", chain, msg}], board:[…]}`.

## Gotchas
- SPEC 24: nonce-delta ≠ token distribution — high-frequency CEX-MM EOAs are gated on
  the token's last_out_ts (`nonce_churn` = fired-but-stale-token-out = noise, not
  ESCALATION).
- SPEC 28: `escalation_kind` separates CEX-execution from internal-consolidation
  staging.
- Operational/exchange tiers (op/hot/mm/cex/…) are excluded from escalation — they tick
  constantly; the signal is team/distribution/treasury/mega safes.
- Free-RPC only (no Moralis spend) — survives quota exhaustion.
- SPEC-145: a wallet whose RPC read fails is counted, never silently dropped —
  `wallets_unreadable`/`unreadable[]` are top-level on every per-token row AND the whole
  board. A per-token signal that would otherwise read a clean `QUIET`/`DORMANT` while some
  of its wallets are unreadable reports `PARTIAL` instead — a caller must not mistake
  half-blind coverage for "nothing happened". `coverage_alerts` fires HIGH when a whole
  chain is unreadable this sweep, or >25% of the tracked set is — `ops/surveil.sh` pages on
  it the same way it pages on an ESCALATION (un-throttled, no page_gate cooldown — that gate
  is keyed on fired-wallet identity, not coverage).

## Page cooldown (SPEC 66)
`ops/surveil.sh` always writes the ESCALATION **log** line, but routes the **page** through
`ops/page_gate.py` (state: `state/page_cooldowns.json`, keyed by wallet address). A wallet
that already paged within the cooldown (default 6h) is logged-not-paged — the fix for BILL
paging 6× in one afternoon as the same apparatus wallets re-fired every sweep. The cooldown
is **pierced** when (a) the fire is a **tier upgrade** — `token_out` reaching a higher-rank
`dest_kind` than the one it last paged on (`staging-internal` → `dex-execution` →
`cex-execution`), or (b) it is the wallet's **first fire after >24h quiet** (a notable
re-emergence). `gate_envelope()` decides per-wallet across all `alerts[].escalation_fired[]`
and pages iff ANY wallet pierces — every wallet is still reported (the log is complete; only
the page is throttled). The CLI fails **open** (defaults to PAGE on any error) — better to
over-page than miss a real §8 escalation. Tests: `tests/test_page_cooldown.py`.
