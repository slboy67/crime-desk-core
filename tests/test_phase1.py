#!/usr/bin/env python3
"""Phase 1b–1e — regime_check, price_structure, liq_magnets, pull5 (native --json).

Run:  python3 tests/test_phase1.py

Each asserts the out-contract on a live ticker (LAB) + that the bare command stays
non-JSON. pull5 is exercised through the orchestrator (aggregation + envelope).
Live venue calls → slow-ish; on-chain layer is intentionally NOT requested.
SPEC-123: every live class here is gated behind CRIMEDESK_LIVE_TESTS=1, skipped by default.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORCH = ROOT / "orchestrator.py"
T = "LAB"

sys.path.insert(0, str(ROOT / "tests"))
from live_gate import LIVE, SKIP_REASON  # noqa: E402


def run(script, *flags):
    return subprocess.run([sys.executable, str(ROOT / "capabilities" / script), *flags],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=120)


def jrun(script, *flags):
    return json.loads(run(script, *flags).stdout)


def orch(cap, args):
    proc = subprocess.run([sys.executable, str(ORCH), cap, args],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    return json.loads(proc.stdout)


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestPriceStructure(unittest.TestCase):
    def test_contract(self):
        s = jrun("price_structure.py", T, "--json")
        self.assertEqual(s["ticker"], T)
        for k in ("range_pos", "squeezes", "squeeze_pattern", "vol_profile", "structure",
                  "compression", "young_listing", "off_ath_pct"):
            self.assertIn(k, s)
        self.assertIsInstance(s["range_pos"], (int, float))
        self.assertIn(s["squeeze_pattern"], {"diminishing", "growing", "mixed", "none"})
        self.assertIn(s["structure"]["read"], {"downtrend", "uptrend", "mixed"})

    def test_optional_days(self):
        s = jrun("price_structure.py", T, "--days", "30", "--json")
        self.assertLessEqual(s["days_available"], 30)

    def test_human_non_json(self):
        out = run("price_structure.py", T).stdout
        self.assertIn("price structure", out)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestLiqMagnets(unittest.TestCase):
    def test_contract(self):
        m = jrun("liq_magnets.py", T, "--json")
        self.assertEqual(m["ticker"], T)
        for k in ("current", "hvns", "upside_magnets", "downside_magnets", "round_numbers"):
            self.assertIn(k, m)
        self.assertIsInstance(m["current"], (int, float))
        self.assertIsInstance(m["hvns"], list)
        if m["hvns"]:
            self.assertIn("p_peak", m["hvns"][0])
            self.assertIn("dist_pct", m["hvns"][0])


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestRegimeCheck(unittest.TestCase):
    def test_contract(self):
        r = jrun("regime_check.py", T, "--json")
        self.assertEqual(r["ticker"], T)
        for v in ("binance", "bybit", "aster"):
            self.assertIn(v, r["funding"])
            self.assertIn("regime", r["funding"][v])
            self.assertIn("z", r["funding"][v])
        self.assertTrue(r["divergence"] is None or isinstance(r["divergence"], dict))

    def test_human_non_json(self):
        out = run("regime_check.py", T, "--no-color").stdout
        self.assertIn("regime check", out)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestPull5(unittest.TestCase):
    def test_aggregates_all_layers(self):
        env = orch("pull5", '{"ticker":"LAB"}')
        self.assertTrue(env["ok"], env)
        d = env["data"]
        for layer in ("layer0_coingecko", "layer2_regime", "layer3_magnets", "layer4_structure"):
            self.assertIn(layer, d)
        self.assertEqual(d["layer2_regime"]["ticker"], T)
        self.assertIsNone(d["layer1_onchain"])  # on-chain is opt-in


class TestRegistry(unittest.TestCase):
    def test_all_four_registered_native(self):
        caps = json.loads((ROOT / "capabilities.json").read_text())
        for c in ("regime_check", "price_structure", "liq_magnets", "pull5"):
            self.assertIn(c, caps)
            self.assertEqual(caps[c]["status"], "native")
            self.assertIsNone(caps[c]["filter"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
