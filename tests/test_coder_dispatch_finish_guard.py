#!/usr/bin/env python3
"""SPEC-133 — dispatch must not report "finished" on an empty coder branch.

Run:  python3 tests/test_coder_dispatch_finish_guard.py

2026-07-29: four coder runs (SPEC-127/128/130/132) exited rc=0 and coder_dispatch.sh
logged "coder finished ... run premerge, review, then merge" while the branch had ZERO
commits ahead of main. An empty branch premerges PASS trivially (merge result IS main),
so the false "finished" log gave false confidence until caught by hand-diffing every
branch (feedback_spec_done_moves_must_be_diff_verified).

Guard (post-coder, before logging "finished"), ALL of:
  1. `git rev-list --count main..<branch>` >= 1 — the branch actually has commits.
  2. every spec in the batch has its `handoffs/REVIEW-REQUEST-<ID>.md` committed on the
     branch — the standing coder loop (GOAL-coder.md) requires one per spec, done or
     blocked; its absence means the coder exited without completing the loop even if it
     made SOME commit (e.g. died mid-spec after an unrelated commit).

Either failing means: log a WARN (not "coder finished"), do NOT tell the orchestrator to
premerge/merge, and leave the claim in place (same "no auto-retry, human inspects" contract
as an ordinary coder failure).

Env seams used (all default to production values): CRIMEDESK_RUNDIR, CLAUDE_BIN, NO_NOTIFY,
CODER_RETRY_BACKOFF_S, CODER_MAX_RETRIES.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_SRC = ROOT / "ops" / "coder_dispatch.sh"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, check=True)


FAKE_CLAUDE = """#!/usr/bin/env bash
# Stands in for the real `claude` binary — SPEC-133 false-finish-guard sandbox tests.
# Behavior driven by FAKE_CLAUDE_MODE; runs with cwd = the coder's worktree.
set -u
mkdir -p "$FAKE_CLAUDE_STATE_DIR"
COUNTER="$FAKE_CLAUDE_STATE_DIR/calls"
n=0
[ -f "$COUNTER" ] && n="$(cat "$COUNTER")"
n=$((n + 1))
echo "$n" > "$COUNTER"

case "${FAKE_CLAUDE_MODE:-ok}" in
  success_no_commit)
    # quota-starved coder: exits clean, touched nothing (the 07-29 signature)
    exit 0
    ;;
  success_commit_no_review)
    # made SOME commit but never wrote the REVIEW-REQUEST (loop died mid-spec)
    git commit --allow-empty -qm "coder commit, no review request (test)"
    exit 0
    ;;
  dirty_no_commit)
    # SPEC-143: the session dies after writing finished work but before committing —
    # the exact loss pattern (coder/auto-20260807-014321 held 357 insertions uncommitted).
    mkdir -p capabilities
    echo "finished work, never committed" > capabilities/uncommitted_work.txt
    exit 0
    ;;
  success_commit_with_review)
    mkdir -p handoffs
    for id in $FAKE_REVIEW_IDS; do
      printf '# REVIEW-REQUEST %s\\n\\nfake review body\\n' "$id" > "handoffs/REVIEW-REQUEST-${id}.md"
    done
    git add -A
    git commit -qm "coder commit with review request (test)"
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.rundir = Path(self._tmp.name) / "rundir"
        (self.repo / "handoffs" / "specs" / "open").mkdir(parents=True)
        (self.repo / "ops").mkdir()
        shutil.copy2(SCRIPT_SRC, self.repo / "ops" / "coder_dispatch.sh")
        (self.repo / "handoffs" / "specs" / "open" / ".gitkeep").write_text("")
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)],
                       capture_output=True, check=True)
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "init")

        self.fake_claude = Path(self._tmp.name) / "fake_claude.sh"
        self.fake_claude.write_text(FAKE_CLAUDE)
        self.fake_claude.chmod(0o755)
        self.fake_state_dir = Path(self._tmp.name) / "fake_claude_state"

    def tearDown(self):
        self._tmp.cleanup()

    def write_spec(self, sid):
        (self.repo / "handoffs" / "specs" / "open" / f"{sid}.md").write_text(f"# {sid}\n")

    def commit_all(self, msg):
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", msg)

    def log(self):
        p = self.repo / "state" / "coder_dispatch.log"
        return p.read_text() if p.exists() else ""

    def claim_exists(self, sid):
        return (self.rundir / "claims" / sid).exists()

    def run_live(self, mode, review_ids=""):
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
               "HOME": self._tmp.name,
               "DRYRUN": "0", "NO_NOTIFY": "1",
               "CLAUDE_BIN": str(self.fake_claude),
               "CRIMEDESK_RUNDIR": str(self.rundir),
               "FAKE_CLAUDE_MODE": mode,
               "FAKE_CLAUDE_STATE_DIR": str(self.fake_state_dir),
               "FAKE_REVIEW_IDS": review_ids,
               "CODER_RETRY_BACKOFF_S": "1", "CODER_MAX_RETRIES": "0"}
        return subprocess.run(["/bin/bash", str(self.repo / "ops" / "coder_dispatch.sh")],
                              capture_output=True, text=True, env=env, timeout=60)


class TestEmptyBranchFalseFinish(_Sandbox):
    def test_zero_commits_does_not_log_finished_and_keeps_claim(self):
        self.write_spec("SPEC-300")
        self.commit_all("SPEC-300: ticket")
        r = self.run_live("success_no_commit")
        log = self.log()
        self.assertNotIn("coder finished [SPEC-300]", log, f"log:\n{log}\nstderr:{r.stderr}")
        self.assertIn("WARN", log)
        self.assertIn("SPEC-300", log)
        self.assertTrue(self.claim_exists("SPEC-300"),
                        "claim must persist — false-finish is not a success, no silent re-dispatch loss")


class TestMissingReviewRequestFalseFinish(_Sandbox):
    def test_commit_without_review_request_does_not_log_finished(self):
        self.write_spec("SPEC-301")
        self.commit_all("SPEC-301: ticket")
        r = self.run_live("success_commit_no_review")
        log = self.log()
        self.assertNotIn("coder finished [SPEC-301]", log, f"log:\n{log}\nstderr:{r.stderr}")
        self.assertIn("WARN", log)
        self.assertIn("REVIEW-REQUEST", log)
        self.assertTrue(self.claim_exists("SPEC-301"))


class TestDirtyWorktreeAutoCommitsWip(_Sandbox):
    """SPEC-143: a coder that writes finished work but exits without committing must never
    lose it — coder_dispatch.sh auto-commits the dirty worktree as `WIP: recovered` before
    the SPEC-133 finish checks run, so recovery never depends on an orchestrator noticing.
    The auto-commit existing does NOT make the run report "finished" — it still has no
    REVIEW-REQUEST, so the SPEC-133 guard still WARNs and the claim still persists."""
    def test_dirty_exit_gets_a_wip_commit_and_still_fails_the_finish_guard(self):
        self.write_spec("SPEC-303")
        self.commit_all("SPEC-303: ticket")
        r = self.run_live("dirty_no_commit")
        log = self.log()
        self.assertIn("WIP: recovered", log, f"log:\n{log}\nstderr:{r.stderr}")
        self.assertNotIn("coder finished [SPEC-303]", log, f"log:\n{log}\nstderr:{r.stderr}")
        self.assertIn("WARN", log)
        self.assertTrue(self.claim_exists("SPEC-303"))
        # the branch actually recovered the work — find the auto-commit + the file it saved
        branches = _git(self.repo, "branch", "--list", "coder/auto-*").stdout
        branch = next(b.strip().lstrip("*+ ").strip() for b in branches.splitlines() if b.strip())
        show = _git(self.repo, "show", f"{branch}:capabilities/uncommitted_work.txt")
        self.assertIn("finished work, never committed", show.stdout)


class TestRealFinishStillReportsFinished(_Sandbox):
    def test_commit_with_review_request_logs_finished_normally(self):
        self.write_spec("SPEC-302")
        self.commit_all("SPEC-302: ticket")
        r = self.run_live("success_commit_with_review", review_ids="SPEC-302")
        log = self.log()
        self.assertIn("coder finished [SPEC-302]", log, f"log:\n{log}\nstderr:{r.stderr}")
        self.assertNotIn("WARN", log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
