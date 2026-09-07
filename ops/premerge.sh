#!/usr/bin/env bash
# premerge.sh <coder-branch> — the mechanical floor for the merge gate.
#
# Individually-green coder branches can be JOINTLY broken (two specs touching the same
# capability merge cleanly per-branch and fail combined). This script proves the MERGE
# RESULT before anything touches real main:
#   1. throwaway worktree at main (temp branch — real main is never moved)
#   2. merge the coder branch into it; a conflict → exit 3 with the conflicting files
#   3. run the full test suite ON THE MERGE RESULT; red → exit 1
# PASS means: merge is clean AND the suite is green on main+branch. The human review that
# follows is judgment-only — correctness of approach, contract fit — not mechanics.
#
# Gate tiers (ARCHITECTURE §10): P0/P1 specs stay human-merged; pure-hygiene specs
# (docs-only, lint) may be merged by the orchestrator once this passes.
#
# SPEC-124: this gate must never wedge silently. Observed 2026-07-14 ~21:30-22:00 UTC —
# `out="$(python3 -m unittest discover ... )"` stayed alive 26+ minutes with zero CPU after
# the python process was gone (a grandchild reparented to init, PPID 1, held the capture
# subshell's stdout pipe open — confirmed post-mortem, not theoretical). Fixes:
#   - no command-substitution capture — the suite writes straight to a log file (state/
#     premerge-<branch>-<ts>.log), stdin from /dev/null; a killed run leaves real evidence
#     of how far it got (the old buffered-capture pattern left a 97-byte log on a killed run).
#   - hard timeout (PREMERGE_SUITE_TIMEOUT_S, default 1200s) that kills the suite's whole
#     PROCESS GROUP (`set -m` job control gives the backgrounded suite its own pgid — no
#     `setsid` binary on macOS) — an orphaned grandchild was the confirmed pipe-holder, so a
#     direct-child-only kill is not enough.
#   - the EXIT trap reaps any still-running suite process/group before the worktree is torn
#     down (orphan hygiene), and a verdict line (PASS/FAIL/MERGE CONFLICT/TIMEOUT/ERR) prints
#     on every exit path — a caller grepping for `PREMERGE:` must never wait forever.
# PREMERGE_SUITE_CMD overrides the suite invocation itself (test seam — a fake sleeping
# "suite" exercises the timeout path deterministically, no live suite run inside the tests).
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
mkdir -p state

branch="${1:?usage: premerge.sh <coder-branch>}"
git rev-parse --verify "$branch" >/dev/null 2>&1 || { echo "PREMERGE: ERR no such branch '$branch'"; exit 2; }

SUITE_TIMEOUT_S="${PREMERGE_SUITE_TIMEOUT_S:-1200}"
SUITE_CMD="${PREMERGE_SUITE_CMD:-python3 -m unittest discover -s tests -p 'test_*.py'}"

stamp="$(date -u +%Y%m%d%H%M%S)-$$"
tmpbranch="premerge-tmp-$$"
tmp="$DIR/worktrees/${tmpbranch}"
SUITE_LOG="$DIR/state/premerge-${branch//\//-}-${stamp}.log"
SUITE_PID=""

# SPEC-124: kill the suite's whole process GROUP (never just the direct child — the
# confirmed wedge hazard is an orphaned grandchild). Falls back to a plain kill of the PID
# if the negative-PGID form is rejected (e.g. the job already reaped).
_kill_suite() {
  [ -z "$SUITE_PID" ] && return 0
  kill -- "-$SUITE_PID" 2>/dev/null || kill "$SUITE_PID" 2>/dev/null || true
  sleep 1
  kill -9 -- "-$SUITE_PID" 2>/dev/null || kill -9 "$SUITE_PID" 2>/dev/null || true
}

cleanup() {
  _kill_suite   # orphan hygiene: reap any leftover suite process/group before tearing down
  cd "$DIR"
  git worktree remove --force "$tmp" >/dev/null 2>&1 || true
  git branch -D "$tmpbranch" >/dev/null 2>&1 || true
}
trap cleanup EXIT

git worktree add -b "$tmpbranch" "$tmp" main >/dev/null 2>&1 || { echo "PREMERGE: ERR worktree add failed"; exit 2; }

# same gitignored runtime deps the coder worktrees get
for dep in _oldrepo config/secrets.json; do
  [ -e "$DIR/$dep" ] && [ ! -e "$tmp/$dep" ] && ln -s "$DIR/$dep" "$tmp/$dep"
done

cd "$tmp"
if ! git merge --no-ff --no-edit "$branch" >/dev/null 2>&1; then
  echo "PREMERGE: MERGE CONFLICT main <- $branch"
  git diff --name-only --diff-filter=U | sed 's/^/  conflict: /'
  exit 3
fi
echo "PREMERGE: merge clean (main <- $branch); running suite on the merge result (log: $SUITE_LOG)..."

: > "$SUITE_LOG"
# `set -m` (job control) gives the backgrounded suite its own process group — same effect as
# `setsid` (not present on macOS) without depending on a GNU-coreutils binary.
set -m
eval "$SUITE_CMD" </dev/null >"$SUITE_LOG" 2>&1 &
SUITE_PID=$!
set +m

waited=0
rc=""
while kill -0 "$SUITE_PID" 2>/dev/null; do
  if [ "$waited" -ge "$SUITE_TIMEOUT_S" ]; then
    _kill_suite
    SUITE_PID=""
    echo "PREMERGE: TIMEOUT after ${SUITE_TIMEOUT_S}s — suite log at $SUITE_LOG"
    exit 4
  fi
  sleep 2
  waited=$((waited + 2))
done
wait "$SUITE_PID" 2>/dev/null
rc=$?
SUITE_PID=""

tail -20 "$SUITE_LOG"
if [ "$rc" -eq 0 ]; then
  # SPEC-191 #4: tracked config/*.json must never be a live-write target — a suite run
  # (or the coder branch itself) that leaves one dirty is the exact SPEC-181/SPEC-185
  # failure mode (live venue_roles snapshots committed into config/). Named + failed here
  # so the mechanical floor catches it instead of a human noticing at session-open.
  dirty_cfg="$(git status --porcelain -- 'config/*.json' 2>/dev/null)"
  if [ -n "$dirty_cfg" ]; then
    echo "PREMERGE: FAIL — tracked config/*.json changed by the suite run (must be untracked runtime state, never live-written):"
    echo "$dirty_cfg" | sed 's/^/  /'
    exit 1
  fi
  echo "PREMERGE: PASS — '$branch' merges clean and the suite is green on the result. Safe to merge."
else
  echo "PREMERGE: FAIL (suite rc=$rc on the merge result) — do NOT merge '$branch' yet. log: $SUITE_LOG"
  exit 1
fi
