# GOAL — Coder session (standing objective, evergreen)

This file is deliberately spec-number-free: the queue lives in the queue, never here.
Your dispatch prompt names the exact spec(s) for this run; this file is the standing loop
and the hard guardrails that apply to every run.

## North star
Make the crime desk **fully trustworthy end-to-end**: every capability deterministic,
JSON-clean, tested, documented; every number the desk surfaces correct and unambiguous;
no alert fires a vetoed trade. The perp board (`classify`/`regime_flip`) is load-bearing —
change its read logic only where a spec explicitly says so.

## Where tickets live
- **Per-spec files (current):** `handoffs/specs/open/SPEC-N.md` — one file per spec; the
  **directory is the state**: `open/` → `in-review/` → `done/`.
- **Legacy queue:** older specs are `## SPEC N (...)` sections in
  `handoffs/phase2-onchain-analyser.md` with `(OPEN)` / `(IN-REVIEW)` / ✅ markers.

Take the specs your dispatch prompt names, in the order given (P0 first). Do not trawl for
other work.

## The loop (per spec — don't stop between specs)

**Commit-first ordering (hard rule): the full-suite run happens AFTER you commit, never
before.** A headless run that dies during an ~11-minute suite loses everything uncommitted.
A commit on this isolated `coder/auto-*` branch is free — nothing reaches `main` without
premerge — so there is no such thing as committing "too early" here.

1. Read the ticket. **Write the failing test(s) first** from its DoD.
2. Implement the **smallest** change that satisfies the DoD. Mock network/RPC/venue calls —
   tests must be offline-deterministic.
3. Run the tests you just wrote (plus any module they directly touch) — fast,
   seconds-to-a-minute. Green → **commit immediately**, spec id in the message. Do NOT wait
   for the full suite before this commit.
4. Only now run the full suite: `python3 -m unittest discover -s tests -p 'test_*.py'
   2>&1 | tail -40` (SPEC-193: a full ~2,300-test run is thousands of dots/lines that
   would otherwise sit in your context for nothing — the tail keeps only the summary
   and any failure block; if you need to see WHICH test failed beyond that window,
   re-run the single failing module directly, not the whole suite again).
   **Run it as a FOREGROUND command and wait for it in-process (~11 min is fine) — never
   background it and end your turn waiting for a notification.** You are a headless `-p`
   process: ending your turn ends the process, the notification has no session to land in,
   and the batch exits rc=0 with the REVIEW-REQUEST unwritten. If
   it reveals breakage, fix it and commit the fix on top (normal TDD — nothing is at risk,
   the good state is already committed). **Verify the DoD literally** — run it, paste the
   actual output in your report.
5. Write `handoffs/REVIEW-REQUEST-SPEC-N.md`: what changed, files touched, test result
   (including the post-commit full-suite result), literal DoD output, any blockers.
6. Mark the spec in-review **in your branch**: `git mv handoffs/specs/open/SPEC-N.md
   handoffs/specs/in-review/SPEC-N.md` (file tickets) or flip `(OPEN)` → `(IN-REVIEW)`
   (legacy). Commit. Start the next spec immediately.
7. **Blocked** (needs a decision, an API key, a missing file)? Write the REVIEW-REQUEST with
   the exact question, mark it 🚧 BLOCKED, commit whatever you have, move to the next spec.
   Surface every blocker at the end; never stall the batch on one.
8. **Never end a turn with uncommitted work in the worktree.** If you must wait on anything
   long-running (a background test run, a monitor), commit WIP first —
   `WIP: SPEC-N <what's done>` — before you start waiting. A WIP commit is strictly better
   than a dead session with a clean-looking empty branch (`ops/coder_dispatch.sh` also
   auto-commits a dirty worktree as `WIP: recovered` on exit as a last-resort safety net,
   but that is not a substitute for committing deliberately at each step above).

## Guardrails (hard)
- **Branch-only.** You run headless in an isolated `coder/auto-*` worktree. NEVER switch to,
  commit on, or merge `main`.
- Do NOT loosen the §5 deep-neg short veto (−0.30%/4h).
- Do NOT make any live verdict block on slow lifetime audits (getLogs).
- Do NOT modify `_oldrepo` logic except wiring an existing script in where a spec says so.
- No subagents, no workflows. TDD throughout.
- Capability discipline (ARCHITECTURE §5): `{ok, data, meta}` envelope on stdout, diagnostics
  to stderr, doc updated **in the same commit** as the code, no model calls inside a capability.

## The gate (how your work lands)
Dispatch is automatic — `ops/coder_dispatch.sh` (launchd WatchPaths on `handoffs/` and
`handoffs/specs/open/`) batches **all unclaimed OPEN specs on main** into one run; claims in
`~/Library/Application Support/crimedesk/claims/` stop re-fires while review is pending; a single-flight lock (with stale-pid
recovery) prevents overlapping coders.

The **merge is gated, not automatic**. The gate is owned by `ARCHITECTURE.md` §10 (premerge
floor + the orchestrator's semantics read against your spec's acceptance criteria; no human
review step) — read it there rather than here, so the two never drift. Only after merge is a
spec ✅ / moved to `done/`.
