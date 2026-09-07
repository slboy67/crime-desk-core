#!/usr/bin/env python3
"""SPEC-164: ops/surveil.sh must not blanket-title every balance_surveil fire
'contract safe DRAINED' — DRAINED is reserved for drained_to_zero (§ ops/balance_surveil.py
PAGEABLE_KINDS/kind semantics). This extracts the exact embedded python snippets the shell
script runs (no duplication/drift risk: it reads the real file) and executes them with a
synthetic run_tick-shaped `bal_out` payload, the same way surveil.sh does at runtime.
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "ops" / "surveil.sh").read_text()


def _extract(var_name):
    marker = f'{var_name}="$(printf \'%s\' "$bal_out" | python3 -c \''
    start = SRC.index(marker) + len(marker)
    end = SRC.index("' 2>/dev/null", start)
    return SRC[start:end]


def _run(code, payload):
    out = subprocess.run([sys.executable, "-c", code], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=10)
    return out.stdout.strip()


class TestBalTitleHonestLabels(unittest.TestCase):
    def setUp(self):
        self.code = _extract("bal_title")

    def test_drained_to_zero_titles_drained(self):
        payload = {"TAG": {"fired": [{"kind": "drained_to_zero", "label": "SAFE-A", "delta": 100}]}}
        self.assertEqual(_run(self.code, payload), "crime-desk — contract safe DRAINED")

    def test_balance_drop_titles_outbound_not_drained(self):
        payload = {"SLX": {"fired": [{"kind": "balance_drop", "label": "SAFE-A", "delta": 100}]}}
        title = _run(self.code, payload)
        self.assertEqual(title, "crime-desk — contract wallet OUTBOUND")
        self.assertNotIn("DRAINED", title)

    def test_outbound_drip_titles_drip(self):
        payload = {"TAKE": {"fired": [{"kind": "outbound_drip", "label": "SAFE-A", "delta": 100}]}}
        title = _run(self.code, payload)
        self.assertIn("OUTBOUND-DRIP", title)
        self.assertNotIn("DRAINED", title)

    def test_mixed_kinds_prefers_drained(self):
        payload = {"TAG": {"fired": [{"kind": "balance_drop", "label": "A", "delta": 1},
                                      {"kind": "drained_to_zero", "label": "B", "delta": 2}]}}
        self.assertEqual(_run(self.code, payload), "crime-desk — contract safe DRAINED")


class TestBalSummaryTags(unittest.TestCase):
    def setUp(self):
        self.code = _extract("bal_summary")

    def test_summary_tags_each_kind(self):
        payload = {"TAG": {"fired": [{"kind": "drained_to_zero", "label": "SAFE-A", "delta": 100},
                                      {"kind": "balance_drop", "label": "SAFE-B", "delta": 50}]}}
        summary = _run(self.code, payload)
        self.assertIn("DRAINED", summary)
        self.assertIn("OUT", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
