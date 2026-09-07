#!/usr/bin/env python3
"""SPEC-70 — coder_dispatch: close the WatchPaths-vs-commit race.

Run:  python3 tests/test_coder_dispatch_retry.py

launchd WatchPaths fires on the spec file WRITE in handoffs/specs/open/, but the
orchestrator commits moments later. Dispatch reads main, saw "OPEN in working tree but
NOT on main", WARN-exited — and the commit didn't re-touch the watched path, so every
spec needed a manual kick (SPEC-67/68/69, 3 occurrences in 24h).

Pins (script exercised for real in a sandboxed git repo, DRYRUN=1, retry knobs shrunk):
  1. Simulated race — spec written → dispatch fires → commit lands mid-retry-window →
     the SAME run dispatches it, no manual kick.
  2. Genuinely uncommitted spec still WARNs once the retry window is exhausted, and is
     not dispatched.
  3. Already-on-main specs dispatch immediately — no retry, no behavior change.

Env seams used (all default to production values): RETRY_N, RETRY_SLEEP,
CRIMEDESK_RUNDIR, CLAUDE_BIN, NO_NOTIFY.
"""
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_SRC = ROOT / "ops" / "coder_dispatch.sh"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, check=True)


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

    def tearDown(self):
        self._tmp.cleanup()

    def run_dispatch(self, retry_n="3", retry_sleep="1"):
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
               "HOME": self._tmp.name,
               "DRYRUN": "1", "NO_NOTIFY": "1",
               "CLAUDE_BIN": "/usr/bin/true",
               "CRIMEDESK_RUNDIR": str(self.rundir),
               "RETRY_N": retry_n, "RETRY_SLEEP": retry_sleep}
        return subprocess.run(["/bin/bash", str(self.repo / "ops" / "coder_dispatch.sh")],
                              capture_output=True, text=True, env=env, timeout=60)

    def log(self):
        p = self.repo / "state" / "coder_dispatch.log"
        return p.read_text() if p.exists() else ""

    def write_spec(self, sid):
        (self.repo / "handoffs" / "specs" / "open" / f"{sid}.md").write_text(f"# {sid}\n")

    def commit_all(self, msg):
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", msg)


class TestRaceClosed(_Sandbox):
    def test_commit_landing_mid_window_dispatches_without_manual_kick(self):
        self.write_spec("SPEC-99")                      # WatchPaths fires on the WRITE …

        def land_commit():
            time.sleep(1.5)                             # … the commit lands moments later
            self.commit_all("SPEC-99: ticket")

        t = threading.Thread(target=land_commit)
        t.start()
        r = self.run_dispatch(retry_n="6", retry_sleep="1")
        t.join()
        log = self.log()
        self.assertIn("DISPATCH: OPEN [SPEC-99]", log,
                      f"the same run must dispatch once the commit lands; log:\n{log}\nstderr:{r.stderr}")
        self.assertIn("retry", log.lower())             # the wait is named, not silent
        self.assertNotIn("manual", log)


class TestGenuinelyUncommitted(_Sandbox):
    def test_warns_after_retry_window_and_does_not_dispatch(self):
        self.write_spec("SPEC-98")                      # never committed
        self.run_dispatch(retry_n="2", retry_sleep="1")
        log = self.log()
        self.assertIn("WARN: SPEC-98 is OPEN in the working tree but NOT on main", log)
        self.assertNotIn("DISPATCH", log)


class TestOnMainUnchanged(_Sandbox):
    def test_committed_spec_dispatches_immediately_with_no_retry(self):
        self.write_spec("SPEC-97")
        self.commit_all("SPEC-97: ticket")
        t0 = time.time()
        self.run_dispatch(retry_n="6", retry_sleep="5")
        elapsed = time.time() - t0
        log = self.log()
        self.assertIn("DISPATCH: OPEN [SPEC-97]", log)
        self.assertNotIn("retry", log.lower())          # no retry path entered
        self.assertLess(elapsed, 5.0, "already-on-main must not wait the retry window")

    def test_nothing_open_is_a_silent_noop(self):
        self.run_dispatch()
        self.assertNotIn("DISPATCH", self.log())


FAKE_CLAUDE = """#!/usr/bin/env bash
# Stands in for the real `claude` binary in the SPEC-125 retry-sandbox tests. Behavior is
# driven entirely by FAKE_CLAUDE_MODE + a call counter persisted in FAKE_CLAUDE_STATE_DIR
# (survives across the fresh-worktree retry, since that dir lives OUTSIDE the worktree).
set -u
mkdir -p "$FAKE_CLAUDE_STATE_DIR"
COUNTER="$FAKE_CLAUDE_STATE_DIR/calls"
n=0
[ -f "$COUNTER" ] && n="$(cat "$COUNTER")"
n=$((n + 1))
echo "$n" > "$COUNTER"
echo "fake-claude invocation #$n in $(pwd)"

case "${FAKE_CLAUDE_MODE:-ok}" in
  transient_then_succeed)
    if [ "$n" -eq 1 ]; then
      echo "API Error: Connection closed mid-response"
      exit 1
    fi
    echo "coder work simulated OK"
    # SPEC-133 false-finish guard needs a real commit + REVIEW-REQUEST to accept
    # "finished" — simulate actual delivery, not just a clean exit.
    for spec in handoffs/specs/open/SPEC-*.md; do
      [ -e "$spec" ] || continue
      id="$(basename "$spec" .md)"
      printf '# REVIEW-REQUEST %s\n\nfake review body\n' "$id" > "handoffs/REVIEW-REQUEST-${id}.md"
    done
    git add -A
    git commit -qm "coder work simulated OK"
    exit 0
    ;;
  transient_always_fail)
    echo "API Error: Connection closed mid-response"
    exit 1
    ;;
  fail_with_commit)
    git commit --allow-empty -qm "partial work by coder"
    echo "some ordinary failure after partial work"
    exit 1
    ;;
  fail_no_signature)
    echo "some ordinary failure, not a transient-API signature"
    exit 1
    ;;
  *)
    exit 0
    ;;
esac
"""


class _RetrySandbox(_Sandbox):
    """SPEC-125 — exercises the ACTUAL coder invocation (DRYRUN=0) against a fake CLAUDE_BIN,
    with the retry backoff shrunk to keep the tests fast."""

    def setUp(self):
        super().setUp()
        self.fake_claude = Path(self._tmp.name) / "fake_claude.sh"
        self.fake_claude.write_text(FAKE_CLAUDE)
        self.fake_claude.chmod(0o755)
        self.fake_state_dir = Path(self._tmp.name) / "fake_claude_state"

    def call_count(self):
        p = self.fake_state_dir / "calls"
        return int(p.read_text().strip()) if p.exists() else 0

    def run_live(self, mode, max_retries="1", backoff="1"):
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
               "HOME": self._tmp.name,
               "DRYRUN": "0", "NO_NOTIFY": "1",
               "CLAUDE_BIN": str(self.fake_claude),
               "CRIMEDESK_RUNDIR": str(self.rundir),
               "FAKE_CLAUDE_MODE": mode,
               "FAKE_CLAUDE_STATE_DIR": str(self.fake_state_dir),
               "CODER_RETRY_BACKOFF_S": backoff, "CODER_MAX_RETRIES": max_retries}
        return subprocess.run(["/bin/bash", str(self.repo / "ops" / "coder_dispatch.sh")],
                              capture_output=True, text=True, env=env, timeout=60)

    def claim_branch(self, sid):
        p = self.rundir / "claims" / sid
        if not p.exists():
            return None
        line = p.read_text().strip()
        return line.split()[0].split("=", 1)[1] if line else None


class TestTransientRetrySucceeds(_RetrySandbox):
    def test_signature_plus_zero_commits_retries_once_then_succeeds(self):
        self.write_spec("SPEC-200")
        self.commit_all("SPEC-200: ticket")
        r = self.run_live("transient_then_succeed")
        log = self.log()
        self.assertIn("RETRY (transient API failure): [SPEC-200] attempt 2/2", log,
                     f"log:\n{log}\nstderr:{r.stderr}")
        self.assertIn("coder finished [SPEC-200]", log)
        self.assertNotIn("ERR: coder FAILED", log)
        self.assertEqual(self.call_count(), 2, "exactly one retry — not zero, not two")
        # the claim now names the SECOND (retried) branch, not the first
        branch = self.claim_branch("SPEC-200")
        self.assertIsNotNone(branch)


class TestTransientRetryFailsAgain(_RetrySandbox):
    def test_second_attempt_also_fails_no_third_attempt_claims_persist(self):
        self.write_spec("SPEC-201")
        self.commit_all("SPEC-201: ticket")
        r = self.run_live("transient_always_fail")
        log = self.log()
        self.assertEqual(log.count("RETRY (transient API failure): [SPEC-201]"), 1,
                         f"exactly one retry attempt, never a third; log:\n{log}")
        self.assertIn("ERR: coder FAILED [SPEC-201]", log)
        self.assertIn("claims persist", log)
        self.assertEqual(self.call_count(), 2, "attempt 1 + the single retry — never a 3rd")
        self.assertIsNotNone(self.claim_branch("SPEC-201"), "claim must survive a final failure")


class TestCommitsOnFailedBranchNeverRetries(_RetrySandbox):
    def test_partial_work_is_never_discarded_by_a_retry(self):
        self.write_spec("SPEC-202")
        self.commit_all("SPEC-202: ticket")
        r = self.run_live("fail_with_commit")
        log = self.log()
        self.assertNotIn("RETRY", log, f"a branch with commits (partial work) must never retry; log:\n{log}")
        self.assertIn("ERR: coder FAILED [SPEC-202]", log)
        self.assertEqual(self.call_count(), 1)


class TestNonTransientFailureNeverRetries(_RetrySandbox):
    def test_ordinary_failure_without_signature_never_retries(self):
        self.write_spec("SPEC-203")
        self.commit_all("SPEC-203: ticket")
        r = self.run_live("fail_no_signature")
        log = self.log()
        self.assertNotIn("RETRY", log, f"a non-transient failure must never retry; log:\n{log}")
        self.assertIn("ERR: coder FAILED [SPEC-203]", log)
        self.assertEqual(self.call_count(), 1)


class TestShellSafetyNoBackgroundedWrapper(unittest.TestCase):
    """The retry loop must never reintroduce the backgrounded-wrapper zombie mode: a
    foreground coder invocation killed by a wrapper that was itself backgrounded
    (`bash ops/coder_dispatch.sh &`) leaves a zombie (memory:
    feedback_never_nudge_dispatch_via_backgrounded_wrapper). The retry backoff/re-invocation
    stays entirely inside this single synchronous script — no `&` on the script itself."""

    def test_no_backgrounded_self_invocation_pattern_in_script(self):
        content = SCRIPT_SRC.read_text()
        self.assertNotIn("coder_dispatch.sh &", content)
        self.assertNotIn("coder_dispatch.sh&", content)

    def test_no_backgrounded_self_invocation_pattern_in_docs(self):
        for md in (ROOT / "handoffs").glob("*.md"):
            content = md.read_text(errors="replace")
            self.assertNotIn("coder_dispatch.sh &", content, f"{md}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
