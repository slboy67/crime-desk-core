---
status: accepted
date: 2026-08-28
---
# The desk reads the execution venue but never sends orders

The user gave the desk an Aster API key so that positions, fills, equity and per-symbol max leverage come from the venue instead of a hand-maintained file, and asked whether the desk could also move or bank positions while he is away. We decided the desk is **read-only on the venue**: it never places, modifies or cancels an order, and no capability may call an order endpoint. The away-case is covered by the user's own resting stop/TP orders plus the existing ntfy push on any level or invalidation. The key was downgraded to read-only permissions accordingly.

## Considered options

- **Desk-managed exits** (engine trails stops / banks TPs unattended): rejected — an unattended agent sending orders on a live account is a line the desk does not cross, and a resting order on the venue enforces the same pre-committed plan without it.
- **No venue key at all** (manual `positions.json`, leverage from slider checks): rejected — drift was already visible (GALA logged at $500/10x vs $196/3x on the venue; the desk quoting 10x on 5x symbols).

## Consequences

- Every commit must carry restable stop/TP params the user can place as resting orders (already §0.5 policy); "the desk will manage it" is never an answer.
- `positions.json` becomes a venue-synced view; the desk-authored fields (signature, thesis ref, desk_disagreed) stay desk-owned.
