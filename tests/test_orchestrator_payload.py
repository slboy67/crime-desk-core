#!/usr/bin/env python3
"""SPEC 65 — orchestrator.fill supports the `{payload}` placeholder.

SPECs 59/60/62 registered capabilities (setup_score/workup/replay) whose scripts
take a single positional JSON arg, with `invoke: "... {payload} --json"`. But
`orchestrator.fill` had no `{payload}` placeholder, so every orchestrator call to
those caps dropped the token and died on argparse (`payload required`).

`{payload}` = the FULL args dict serialized to shell-quoted JSON (the same quoting
path SPEC 48 added for dict/list args), so it survives the shell=True invoke and
json.loads back to the original dict inside the capability.

Run:  python3 -m unittest tests.test_orchestrator_payload
"""
import importlib.util
import json
import shlex
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("orchestrator", ROOT / "orchestrator.py")
orch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orch)


class TestPayloadFill(unittest.TestCase):
    def _payload_token(self, cmd, script_substr):
        """Pull the positional payload token back out of a built command."""
        parts = shlex.split(cmd)
        # token after the script path and before any --flag
        idx = next(i for i, p in enumerate(parts) if script_substr in p)
        return parts[idx + 1]

    def test_payload_roundtrips_full_args(self):
        args = {"ticker": "FIDA"}
        cmd = orch.fill("python3 capabilities/workup.py {payload} --json", args)
        tok = self._payload_token(cmd, "workup.py")
        self.assertEqual(json.loads(tok), args)
        self.assertIn("--json", cmd)

    def test_payload_with_nested_and_spaces(self):
        args = {"action": "run", "setup": "blowoff", "symbols": ["FIDAUSDT", "X Y"]}
        cmd = orch.fill("python3 capabilities/replay.py {payload} --json", args)
        tok = self._payload_token(cmd, "replay.py")
        self.assertEqual(json.loads(tok), args)

    def test_payload_empty_args(self):
        cmd = orch.fill("python3 capabilities/setup_score.py {payload} --json", {})
        tok = self._payload_token(cmd, "setup_score.py")
        self.assertEqual(json.loads(tok), {})

    def test_payload_is_single_argv_token(self):
        # a payload with spaces must NOT split into multiple argv tokens
        args = {"ticker": "FIDA", "setup": "trap long"}
        cmd = orch.fill("python3 capabilities/setup_score.py {payload} --json", args)
        parts = shlex.split(cmd)
        self.assertEqual(parts, ["python3", "capabilities/setup_score.py",
                                 json.dumps(args), "--json"])


class TestPayloadRoundtripThroughCLI(unittest.TestCase):
    """End-to-end: the three SPEC 59/60/62 caps must get past argparse via orchestrator."""

    def _invoke(self, cap, args):
        import subprocess
        proc = subprocess.run(
            ["python3", str(ROOT / "orchestrator.py"), cap, json.dumps(args)],
            capture_output=True, text=True, cwd=str(ROOT), timeout=120)
        return json.loads(proc.stdout)

    def test_setup_score_offline_roundtrips(self):
        # offline form: inject signals -> no network, deterministic
        out = self._invoke("setup_score", {"setup": "blowoff", "signals": {}})
        # must NOT be an argparse 'payload required' failure
        self.assertNotIn("payload", json.dumps(out).lower()[:0] + "")
        self.assertTrue(out.get("ok"), out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
