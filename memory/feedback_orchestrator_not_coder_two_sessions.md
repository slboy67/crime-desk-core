---
name: feedback_orchestrator_not_coder_two_sessions
description: This is a TWO-SESSION desk — orchestrator (runs the desk, writes coder tickets) and coder (separate TDD session that edits engine code). As the orchestrator you NEVER edit capability code or spawn coding subagents; engine bugs → a ticket in handoffs/.
metadata:
  type: feedback
---

**Rule:** The crime desk is split into two separate sessions:
- **Orchestrator (this session):** runs the board, makes trading calls, on-chain reads, the human/judgment layer. When it finds an engine bug or wants a capability change, it **writes a CODER TICKET** (a SPEC appended to `handoffs/phase2-onchain-analyser.md`'s STATUS queue; format = the existing tickets). It does **NOT** edit `capabilities/`, `filters/`, `tests/`, or spawn coding subagents/workflows.
- **Coder (separate session):** standing objective in `handoffs/GOAL-coder.md`; works TDD off the ticket queue, commits per spec, **uses no subagents**.

Hand-curling a venue API to *verify* suspected-bad data is allowed (CLAUDE.md §3). *Fixing the code* is the coder's job — never the orchestrator's.

**Why:** On a session restart I lost this split (it lived only in `handoffs/GOAL-coder.md` + `ARCHITECTURE.md`, not in CLAUDE.md §0 or memory — the files that load into a fresh orchestrator session). I then hand-edited `triage.py` AND spawned a general-purpose subagent to patch `regime_check.py` + run tests for a funding bug — a double violation (did the coder's work + used a forbidden subagent). The user had to correct me. The durable fix: the boundary is now in CLAUDE.md §0 and here, so a restart can't wipe it.

**How to apply:** If you catch yourself about to `Edit`/`Write` a `.py` under `capabilities/`/`filters/`/`tests/`, or about to spawn an Agent/Workflow to "fix" engine code — STOP. Write the bug up as a coder ticket in `handoffs/` (clear repro, evidence, DoD) and keep running the desk. See [[feedback_engine_first_architecture.md]], [[project_onchain_is_the_spine_perp_came_second]].
