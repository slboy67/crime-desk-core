#!/usr/bin/env python3
"""SPEC 50 — registry lint: no capability without a test and a doc (ARCHITECTURE §8 inv. 3).

Run:  python3 tests/test_registry_docs.py

Every registered capability must name a `test` and a `doc`, and both paths must exist.
This is the lint that keeps invariant 3 from drifting back to 12/25.
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "capabilities.json"


class TestRegistryDocs(unittest.TestCase):
    def setUp(self):
        d = json.loads(REGISTRY.read_text())
        self.caps = {k: v for k, v in d.items() if not k.startswith("_")}

    def test_every_capability_names_a_doc_that_exists(self):
        missing = []
        for name, e in self.caps.items():
            doc = e.get("doc")
            if not doc:
                missing.append(f"{name}: no doc field")
            elif not (ROOT / doc).exists():
                missing.append(f"{name}: doc {doc} does not exist")
        self.assertEqual(missing, [])

    def test_every_capability_names_a_test_that_exists(self):
        missing = []
        for name, e in self.caps.items():
            t = e.get("test")
            if not t:
                missing.append(f"{name}: no test field")
            elif not (ROOT / t).exists():
                missing.append(f"{name}: test {t} does not exist")
        self.assertEqual(missing, [])

    def test_invoke_placeholders_are_known_args_or_payload(self):
        # SPEC 65: every {placeholder} in an invoke template must be either a
        # documented arg of that capability's in-contract or the special {payload}
        # (the full args dict, shell-quoted JSON). Otherwise orchestrator.fill
        # silently drops the token and the underlying script dies on argparse.
        bad = []
        for name, e in self.caps.items():
            invoke = e.get("invoke") or ""
            placeholders = set(re.findall(r"\{(\w+)\}", invoke))
            known = set((e.get("contract", {}).get("in", {}) or {}).keys())
            known.add("payload")
            unknown = placeholders - known
            if unknown:
                bad.append(f"{name}: invoke placeholders {sorted(unknown)} "
                           f"not in contract.in {sorted(known)}")
        self.assertEqual(bad, [])

    def test_invoke_targets_exist(self):
        # the invoked script path (2nd token of the invoke template) must exist
        missing = []
        for name, e in self.caps.items():
            parts = (e.get("invoke") or "").split()
            if len(parts) >= 2 and parts[0] == "python3":
                if not (ROOT / parts[1]).exists():
                    missing.append(f"{name}: invoke target {parts[1]} does not exist")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
