---
name: feedback_headless_coder_background_suite_loses_work
description: "Headless coder (-p one-shot) ran the test suite in the BACKGROUND and ended its turn 'until the wakeup fires' — no wakeup exists in print mode, process exited rc=0, all WIP uncommitted/destroyed. Happened twice in one night (SPEC-112/113). rc=0 + empty branch = this signature; check the worktree for orphaned WIP + the log for 'background/wakeup' transcript leaks. Fixed by prompt constraint in ops/coder_dispatch.sh."
metadata:
  type: feedback
---

# Headless coder + background tasks = silent work loss (rc=0!)

**What happened (2026-07-07, twice):** the dispatched coder implemented SPEC-112/113 fully (worktree
had modified engine files + both new test files), then ran the full suite **in the background** and
ended its turn — transcript leak in `state/coder_dispatch.log`: *"I've kicked off the full test
suite in the background and will resume analysis once it completes or the scheduled wakeup fires."*
In `-p` print mode there IS no monitor/wakeup: end-of-turn = process exit. Dispatch logged
`finished (rc=0)`; the branch had **zero commits**; the WIP died with the worktree.

**Why it's dangerous:** rc=0 + a "finished" log line looks like success, and `premerge.sh` on an
empty branch trivially PASSES (nothing to merge = suite green). The only tell is the branch tip
still being the orchestrator's own spec commit.

**Rules:**
1. **After any coder run, verify the branch has commits BEFORE running premerge** — an empty branch
   premerge-PASS is vacuous. `git log <branch> --oneline | head -1` must show a coder commit.
2. The dispatch prompt now hard-forbids background execution / monitors / wakeups and requires
   foreground suite runs + commit-per-spec (patched in `ops/coder_dispatch.sh` 2026-07-07). If this
   signature recurs anyway, check GOAL-coder.md carries the same constraint.
3. Recovery = the zombie-coder playbook: ps-verify dead → inspect worktree WIP (don't hand-commit
   unverified engine WIP — redo is cheaper than merging untested code) → remove worktree + branch →
   clear the dead run's claims → re-dispatch.
4. Model behavior is nondeterministic across runs: the first batch (same night, same prompt) ran the
   suite in the foreground and succeeded. A pipeline that works "usually" still needs the
   constraint in the prompt, not in luck.

Related: [[feedback_verify_process_death_after_kill]] (verify-before-cleanup),
[[feedback_stale_state_may_be_load_bearing]] (claims suppress re-dispatch on purpose — inspect, then clear).
