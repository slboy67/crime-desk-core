#!/usr/bin/env python3
"""SPEC-124 — premerge.sh must not wedge on suite output capture.

Run:  python3 tests/test_premerge.py

Observed 2026-07-14 ~21:30-22:00 UTC: a command-substitution capture of the suite run
(`out="$(python3 -m unittest ...)"`) stayed alive 26+ minutes with zero CPU after the python
process was gone — an orphaned grandchild held the capture subshell's stdout pipe open, so
the merge gate never printed a verdict. This test exercises the fixed script in a SANDBOXED
git repo (own throwaway `main` + coder branch) so it never touches the real repo, and never
runs the real suite — PREMERGE_SUITE_CMD substitutes a fast/fake "suite" for determinism.

Pins: hard timeout kills the suite's whole process GROUP (an orphaned grandchild is the
confirmed wedge hazard, not just the direct child), a verdict line prints on every exit path
(PASS/FAIL/MERGE CONFLICT/TIMEOUT/ERR — a caller grepping `PREMERGE:` must never wait
forever), the suite writes straight to a log file (direct-file logging, no buffered capture),
and the merge-conflict / no-such-branch paths are unchanged.
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_SRC = ROOT / "ops" / "premerge.sh"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, check=True)


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / "ops").mkdir(parents=True)
        (self.repo / "tests").mkdir(parents=True)
        shutil.copy2(SCRIPT_SRC, self.repo / "ops" / "premerge.sh")
        os.chmod(self.repo / "ops" / "premerge.sh", 0o755)
        (self.repo / "tests" / "test_dummy.py").write_text("x = 1\n")
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)],
                       capture_output=True, check=True)
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "init")
        _git(self.repo, "checkout", "-qb", "coder/x")
        (self.repo / "tests" / "test_dummy2.py").write_text("y = 2\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "coder change")
        _git(self.repo, "checkout", "-q", "main")

    def tearDown(self):
        self._tmp.cleanup()

    def run_premerge(self, branch="coder/x", suite_cmd=None, timeout_s=None, wall_timeout=30):
        env = dict(os.environ)
        env["HOME"] = self._tmp.name
        if suite_cmd is not None:
            env["PREMERGE_SUITE_CMD"] = suite_cmd
        if timeout_s is not None:
            env["PREMERGE_SUITE_TIMEOUT_S"] = str(timeout_s)
        return subprocess.run(["/bin/bash", "ops/premerge.sh", branch],
                              capture_output=True, text=True, cwd=str(self.repo),
                              env=env, timeout=wall_timeout)

    def suite_logs(self):
        d = self.repo / "state"
        return sorted(d.glob("premerge-*.log")) if d.exists() else []


class TestVerdictAlwaysPrints(_Sandbox):
    def test_pass_path(self):
        r = self.run_premerge(suite_cmd="echo ok; exit 0")
        self.assertEqual(r.returncode, 0)
        self.assertIn("PREMERGE: PASS", r.stdout)

    def test_fail_path(self):
        r = self.run_premerge(suite_cmd="echo boom; exit 1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("PREMERGE: FAIL", r.stdout)

    def test_no_such_branch(self):
        r = self.run_premerge(branch="coder/does-not-exist")
        self.assertEqual(r.returncode, 2)
        self.assertIn("PREMERGE:", r.stdout)

    def test_merge_conflict(self):
        (self.repo / "tests" / "test_dummy.py").write_text("conflict-main\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "main edits the same line")
        _git(self.repo, "checkout", "-q", "coder/x")
        (self.repo / "tests" / "test_dummy.py").write_text("conflict-branch\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "branch edits the same line")
        _git(self.repo, "checkout", "-q", "main")
        r = self.run_premerge()
        self.assertEqual(r.returncode, 3)
        self.assertIn("PREMERGE: MERGE CONFLICT", r.stdout)
        self.assertIn("conflict:", r.stdout)


class TestDirectFileLogging(_Sandbox):
    def test_suite_output_written_straight_to_a_log_file(self):
        r = self.run_premerge(suite_cmd="echo line-one; echo line-two; exit 0")
        self.assertEqual(r.returncode, 0)
        logs = self.suite_logs()
        self.assertEqual(len(logs), 1, logs)
        content = logs[0].read_text()
        self.assertIn("line-one", content)
        self.assertIn("line-two", content)
        self.assertIn(str(logs[0]), r.stdout)   # the verdict/progress line names the log path

    def test_killed_run_leaves_partial_log_evidence(self):
        # a suite that writes, then hangs past the timeout — direct-file logging means the
        # partial output survives even though the process never finished (unlike a buffered
        # command-substitution capture, which would show nothing).
        r = self.run_premerge(
            suite_cmd='echo partial-output; python3 -c "import time; time.sleep(30)"',
            timeout_s=2)
        self.assertEqual(r.returncode, 4)
        logs = self.suite_logs()
        self.assertEqual(len(logs), 1)
        self.assertIn("partial-output", logs[0].read_text())


class TestTimeoutKillsProcessGroup(_Sandbox):
    def test_timeout_path_verdict_and_exit_code(self):
        t0 = time.time()
        r = self.run_premerge(suite_cmd='python3 -c "import time; time.sleep(30)"', timeout_s=2)
        elapsed = time.time() - t0
        self.assertEqual(r.returncode, 4)
        self.assertIn("PREMERGE: TIMEOUT after 2s", r.stdout)
        self.assertLess(elapsed, 15, "the timeout+kill path must not itself hang")

    def test_timeout_kills_orphaned_grandchild_not_just_the_direct_child(self):
        # the confirmed 2026-07-14 wedge hazard: a grandchild survives a direct-child-only kill.
        marker = Path(self._tmp.name) / "grandchild_alive"
        suite_cmd = (
            f'bash -c "sleep 1 && touch {marker} && sleep 30" & wait'
        )
        r = self.run_premerge(suite_cmd=suite_cmd, timeout_s=3, wall_timeout=30)
        self.assertEqual(r.returncode, 4)
        time.sleep(1)   # let a signal that only hit the parent finish tearing the child down
        # confirm no `sleep 30` process from this test's marker survived
        ps = subprocess.run(["ps", "-eo", "pid,command"], capture_output=True, text=True)
        leftover = [ln for ln in ps.stdout.splitlines() if "sleep 30" in ln]
        self.assertEqual(leftover, [], f"orphaned grandchild(ren) survived the timeout kill: {leftover}")


class TestConfigDriftGuard(_Sandbox):
    """SPEC-191 #4: a coder branch (or the suite itself) that leaves a tracked
    config/*.json file dirty after a green suite run must NOT pass premerge — this is
    the mechanical floor that should have caught SPEC-181's and SPEC-185's live-snapshot
    commits into config/venue_roles.json."""

    def setUp(self):
        super().setUp()
        # add a tracked config/*.json to the sandboxed main+coder branches
        for branch in ("main", "coder/x"):
            _git(self.repo, "checkout", "-q", branch)
        _git(self.repo, "checkout", "-q", "main")
        (self.repo / "config").mkdir(exist_ok=True)
        (self.repo / "config" / "venue_roles.json").write_text("{}\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "add config/venue_roles.json")
        _git(self.repo, "checkout", "-q", "coder/x")
        _git(self.repo, "merge", "-q", "main")
        _git(self.repo, "checkout", "-q", "main")

    def test_suite_dirtying_tracked_config_fails_premerge(self):
        r = self.run_premerge(suite_cmd='echo \'{"X":1}\' > config/venue_roles.json; exit 0')
        self.assertEqual(r.returncode, 1)
        self.assertIn("PREMERGE: FAIL", r.stdout)
        self.assertIn("config/venue_roles.json", r.stdout)

    def test_suite_leaving_tracked_config_untouched_still_passes(self):
        r = self.run_premerge(suite_cmd="echo ok; exit 0")
        self.assertEqual(r.returncode, 0)
        self.assertIn("PREMERGE: PASS", r.stdout)


class TestNoLiveSuiteRunInTests(unittest.TestCase):
    def test_script_always_overridable_offline(self):
        # sanity: the default SUITE_CMD is never invoked by these tests (every run_premerge
        # call above passes a fixture suite_cmd) — this test just documents the invariant.
        content = SCRIPT_SRC.read_text()
        self.assertIn("PREMERGE_SUITE_CMD", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
