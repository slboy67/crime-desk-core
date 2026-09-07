#!/usr/bin/env python3
"""SPEC-193 — `ops/coder_dispatch.sh --dry-run SPEC-N` + `--bare`/budget-cap flags.

Every coder request carried the ORCHESTRATOR's CLAUDE.md (40KB, addressed to a
different role) + MEMORY.md + skills/hooks/MCP servers — pure prefix the coder never
needed (its own guardrails live in GOAL-coder.md, read as a file). `--bare` drops that
whole layer; `--max-turns`/`--max-budget-usd` are backstops against the open-ended-run
failure mode (SPEC-125/133/143).

Run for real in a sandboxed empty git repo (same harness as test_coder_dispatch_retry.py)
— `--dry-run` must work with ZERO OPEN specs on main (it takes its spec id(s) as CLI
args, not from detection) and must never touch claims/lock/worktrees.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_SRC = ROOT / "ops" / "coder_dispatch.sh"


class TestDryRunArgv(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.rundir = Path(self._tmp.name) / "rundir"
        (self.repo / "ops").mkdir(parents=True)
        shutil.copy2(SCRIPT_SRC, self.repo / "ops" / "coder_dispatch.sh")
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)],
                       capture_output=True, check=True)

    def tearDown(self):
        self._tmp.cleanup()

    def run_dryrun(self, *args, env_extra=None):
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self._tmp.name),
               "NO_NOTIFY": "1", "CLAUDE_BIN": "/usr/bin/true",
               "CRIMEDESK_RUNDIR": str(self.rundir)}
        env.update(env_extra or {})
        return subprocess.run(
            ["/bin/bash", str(self.repo / "ops" / "coder_dispatch.sh"), "--dry-run", *args],
            capture_output=True, text=True, env=env, timeout=30, cwd=str(self.repo))

    def test_argv_contains_bare_max_turns_max_budget(self):
        r = self.run_dryrun("SPEC-999")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("--bare", r.stdout)  # 2026-09-07: --bare disables OAuth on a subscription desk
        self.assertIn("--disable-slash-commands", r.stdout)
        self.assertIn("--strict-mcp-config", r.stdout)
        self.assertIn("--max-turns", r.stdout)
        self.assertIn("--max-budget-usd", r.stdout)
        self.assertIn("/usr/bin/true", r.stdout)

    def test_default_max_turns_and_budget_values(self):
        r = self.run_dryrun("SPEC-999")
        lines = r.stdout.splitlines()
        self.assertEqual(lines[lines.index("--max-turns") + 1], "200")
        self.assertEqual(lines[lines.index("--max-budget-usd") + 1], "25")

    def test_env_overrides_max_turns_and_budget(self):
        r = self.run_dryrun("SPEC-999", env_extra={"CODER_MAX_TURNS": "50",
                                                    "CODER_MAX_BUDGET_USD": "5"})
        lines = r.stdout.splitlines()
        self.assertEqual(lines[lines.index("--max-turns") + 1], "50")
        self.assertEqual(lines[lines.index("--max-budget-usd") + 1], "5")

    def test_prompt_names_the_given_spec_ids(self):
        r = self.run_dryrun("SPEC-111", "SPEC-222")
        self.assertIn("SPEC-111 SPEC-222", r.stdout)

    def test_no_spec_id_is_a_usage_error_not_a_launch(self):
        r = self.run_dryrun()
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("--bare", r.stdout)

    def test_dry_run_never_creates_a_claim_lock_or_worktree(self):
        self.run_dryrun("SPEC-999")
        self.assertFalse((self.rundir / "claims" / "SPEC-999").exists())
        self.assertFalse((self.rundir / "coder.lock").exists())
        # `mkdir -p state worktrees` at the top of the script is harmless pre-existing
        # scaffolding — what matters is no ACTUAL worktree (a git branch checkout) landed.
        wt_dir = self.repo / "worktrees"
        self.assertEqual(list(wt_dir.iterdir()) if wt_dir.exists() else [], [])

    def test_dry_run_works_with_zero_open_specs_on_main(self):
        # no handoffs/specs/open/ directory at all in this sandbox — --dry-run still
        # prints an argv because it takes its spec id(s) as CLI args, not from detection.
        r = self.run_dryrun("SPEC-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("--bare", r.stdout)


if __name__ == "__main__":
    unittest.main()
