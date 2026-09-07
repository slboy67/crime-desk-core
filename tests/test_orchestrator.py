#!/usr/bin/env python3
"""Phase 0 — orchestrator hardening suite.

Run:  python3 tests/test_orchestrator.py     (or)  python3 -m unittest -v tests.test_orchestrator

Asserts the orchestrator contract (envelope, no-leak, error paths) and that the
four registered capabilities resolve. `classify` + `regime_flip` are native and
exercised live (they hit Velo funding — fast). `analyse` is slow/network-bound,
so its FILTER is unit-tested against a captured banner instead of a live run.
SPEC-123: the live classify/regime_flip smokes are gated behind CRIMEDESK_LIVE_TESTS=1,
skipped by default.
"""
import json
import re
import shlex
import subprocess
import sys
import unittest
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORCH = ROOT / "orchestrator.py"

sys.path.insert(0, str(ROOT / "tests"))
from live_gate import LIVE, SKIP_REASON  # noqa: E402


def _load_orchestrator():
    spec = importlib.util.spec_from_file_location("orchestrator", ORCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_engine_verdicts():
    """Source the verdict set from the engine (capabilities/classify.py) so this
    test can't drift from the real enum the way the old hardcoded 3-set did —
    WATCH-ARMED (SPEC 77) was missing here and reddened every premerge whenever a
    watch-leg name was armed (SPEC 86)."""
    spec = importlib.util.spec_from_file_location(
        "classify", ROOT / "capabilities" / "classify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(mod.VERDICTS)


VERDICTS = _load_engine_verdicts()

# Live classify tests shell out to the real board (network + current watchlist
# state) — non-deterministic offline. Same live-smoke convention as test_analyse.py.
SKIP_LIVE = not LIVE


def call(cap, args="{}"):
    """Shell the orchestrator exactly as the Designer does; return (raw_stdout, parsed)."""
    proc = subprocess.run(
        [sys.executable, str(ORCH), cap, args],
        capture_output=True, text=True, cwd=str(ROOT), timeout=240,
    )
    return proc.stdout, json.loads(proc.stdout)


def load_filter(name):
    fp = ROOT / "filters" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, fp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestVerdictSet(unittest.TestCase):
    """Deterministic (no network): the test's accepted-verdict set is the engine's
    own enum, and WATCH-ARMED is a first-class member (SPEC 86)."""

    def test_watch_armed_is_a_real_verdict(self):
        self.assertIn("WATCH-ARMED", VERDICTS)

    def test_set_matches_engine_enum(self):
        self.assertEqual(VERDICTS, _load_engine_verdicts())
        self.assertSetEqual(
            VERDICTS, {"CONFIRMS", "TRIGGERS", "BREAKS", "WATCH-ARMED"})


class TestInvokeExpansion(unittest.TestCase):
    """SPEC-88: invoke templates must use `{key}` placeholders only — never literal
    alternations (`a|b`) or bare default values inside `[ ... ]` optional groups, which
    leak straight to the shell (`short: command not found`, `exit: --bps: numeric ...`)."""

    def setUp(self):
        self.orch = _load_orchestrator()
        self.caps = json.loads((ROOT / "capabilities.json").read_text())

    def test_maxsize_no_args_drops_all_optionals(self):
        # Only ticker present → every optional flag group drops; no stray flags/pipes.
        cmd = self.orch.fill(self.caps["maxsize"]["invoke"], {"ticker": "BLESS"})
        self.assertEqual(
            shlex.split(cmd),
            ["python3", "capabilities/maxsize.py", "BLESS", "--json"],
        )
        self.assertNotIn("|", cmd)

    def test_maxsize_side_bps_match_direct_call(self):
        cmd = self.orch.fill(
            self.caps["maxsize"]["invoke"],
            {"ticker": "BLESS", "side": "short", "bps": 25},
        )
        self.assertEqual(
            shlex.split(cmd),
            ["python3", "capabilities/maxsize.py", "BLESS",
             "--side", "short", "--bps", "25", "--json"],
        )

    def test_maxsize_all_optionals(self):
        cmd = self.orch.fill(
            self.caps["maxsize"]["invoke"],
            {"ticker": "LAB", "side": "long", "leg": "exit",
             "bps": 30, "size": 5000, "venue": "aster"},
        )
        self.assertEqual(
            shlex.split(cmd),
            ["python3", "capabilities/maxsize.py", "LAB",
             "--side", "long", "--leg", "exit", "--bps", "30",
             "--size", "5000", "--venue", "aster", "--json"],
        )

    def test_no_invoke_has_literal_tokens_in_optional_groups(self):
        # Lint EVERY registered cap: inside any `[ ... ]` group, the only non-flag,
        # non-whitespace tokens allowed are `{key}` placeholders (and leading `--flag`).
        for name, spec in self.caps.items():
            if name.startswith("_"):
                continue
            invoke = spec.get("invoke")
            if not invoke:
                continue
            for grp in re.findall(r"\[([^\[\]]*)\]", invoke):
                # strip the {key} placeholders, the --flags, and shell quote chars
                # (intentional patterns: `[--tp "{tp}"]`, bare bool flag `[--propose-stop]`).
                # What remains can only be a literal default value or alternation = a leak.
                residue = re.sub(r"\{\w+\}", "", grp)
                residue = re.sub(r"--?[\w-]+", "", residue)
                residue = residue.replace('"', "").replace("'", "").strip()
                self.assertEqual(
                    residue, "",
                    f"{name}: optional group '[{grp}]' has non-placeholder token(s) "
                    f"'{residue}' that would leak to the shell")
                self.assertNotIn("|", grp, f"{name}: literal alternation in '[{grp}]'")


class TestEnvelope(unittest.TestCase):
    @unittest.skipIf(SKIP_LIVE, SKIP_REASON)
    def test_classify_single_ticker(self):
        _, env = call("classify", '{"ticker":"LAB"}')
        self.assertTrue(env["ok"], env)
        d = env["data"]
        self.assertEqual(d["ticker"], "LAB")
        self.assertIn(d["verdict"], VERDICTS)
        self.assertTrue(d["thesis_present"])
        self.assertIn("capability", env["meta"])

    @unittest.skipIf(SKIP_LIVE, SKIP_REASON)
    def test_classify_board(self):
        # SPEC 64: the board is a {board, meta} envelope — meta carries the dead-man
        # surveil age so a dead watcher is visible on every read.
        _, env = call("classify", "{}")
        self.assertTrue(env["ok"], env)
        data = env["data"]
        self.assertIsInstance(data, dict)
        self.assertIn("board", data)
        self.assertGreaterEqual(len(data["board"]), 15)
        for row in data["board"]:
            self.assertIn(row["verdict"], VERDICTS)
        meta = data["meta"]
        self.assertIn("surveil_age_h", meta)
        self.assertIn("surveil_stale", meta)
        self.assertIn("board_tick_stale", meta)

    @unittest.skipIf(SKIP_LIVE, SKIP_REASON)
    def test_no_leak_success(self):
        # Every success → stdout is a single JSON object, nothing before/after.
        raw, env = call("classify", "{}")
        self.assertTrue(env["ok"])
        reparsed = json.loads(raw)  # must parse the FULL stdout cleanly
        self.assertEqual(set(reparsed), {"ok", "data", "meta"})
        self.assertEqual(raw.strip(), raw.strip().rstrip("\n").strip())

    def test_unknown_capability(self):
        _, env = call("definitely_not_a_cap", "{}")
        self.assertFalse(env["ok"])
        self.assertIn("unknown capability", env["error"])

    def test_malformed_args(self):
        _, env = call("classify", "{not valid json")
        self.assertFalse(env["ok"])
        self.assertIn("bad args", env["error"])

    def test_args_not_object(self):
        _, env = call("classify", "[1,2,3]")
        self.assertFalse(env["ok"])


class TestRegimeFlip(unittest.TestCase):
    @unittest.skipIf(SKIP_LIVE, SKIP_REASON)
    def test_regime_flip_native(self):
        _, env = call("regime_flip", '{"ticker":"LAB"}')
        self.assertTrue(env["ok"], env)
        d = env["data"]
        self.assertEqual(d["ticker"], "LAB")
        self.assertIn("tag", d)
        self.assertIn("note", d)


class TestAnalyseFilter(unittest.TestCase):
    """analyse.py prints a human banner (no --json); filters/analyse.py distills it."""

    SAMPLE = (
        "# LAB — COMBINED ANALYSIS (onchain + perp + CVD convergence)\n\n"
        "Running perp + on-chain analysers + spot/perp CVD...\n\n"
        "═══════════════════════════\n"
        "  🟢  VERDICT: LONG  [STRONG]  🟢  \n"
        "═══════════════════════════\n\n"
        "Perp score: +35  (accumulation)  |  On-chain score: +12\n"
    )

    def setUp(self):
        self.f = load_filter("analyse")

    def test_extracts_contract(self):
        out = self.f.run(self.SAMPLE, {"ticker": "LAB"})
        self.assertEqual(out, {
            "ticker": "LAB", "verdict": "LONG", "tier": "STRONG", "direction": "LONG",
        })

    def test_compound_direction(self):
        banner = (
            "# HOME — COMBINED ANALYSIS (x)\n"
            "  🔴  VERDICT: SHORT (loading)  [WATCH]  🔴  \n"
        )
        out = self.f.run(banner, {})
        self.assertEqual(out["ticker"], "HOME")
        self.assertEqual(out["verdict"], "SHORT")
        self.assertEqual(out["direction"], "SHORT (loading)")
        self.assertEqual(out["tier"], "WATCH")

    def test_pass_with_qualifier(self):
        banner = "# X — COMBINED ANALYSIS (x)\n  ⚪  VERDICT: PASS (CVD: hedge-trap)  [PASS]  ⚪\n"
        out = self.f.run(banner, {})
        self.assertEqual(out["verdict"], "PASS")

    def test_strips_ansi(self):
        banner = "# X — COMBINED ANALYSIS (x)\n\x1b[1m\x1b[32m  VERDICT: LONG  [MILD]  \x1b[0m\n"
        out = self.f.run(banner, {})
        self.assertEqual(out["verdict"], "LONG")
        self.assertEqual(out["tier"], "MILD")

    def test_raises_on_garbage(self):
        with self.assertRaises(ValueError):
            self.f.run("no banner here\n", {"ticker": "X"})


class TestOiSidesFilter(unittest.TestCase):
    """SPEC-174 #6 — filters/oi_sides.py relabels oi_change_pct's window in the key
    (oi_sides.py's own --period/--limit defaults, 5m x 48 = 4h, are never overridden by
    the orchestrator's invoke template) without touching the delegated _oldrepo script."""

    def setUp(self):
        self.f = load_filter("oi_sides")

    def test_adds_window_labelled_alias_alongside_the_original(self):
        raw = json.dumps({"ticker": "MANTRA", "symbol": "MANTRAUSDT", "verdict": "MIXED",
                          "wash_score": 2, "oi_change_pct": 8.2, "short_change_pct": -3.0,
                          "signals": {}})
        out = self.f.run(raw, {"ticker": "MANTRA"})
        self.assertEqual(out["oi_change_pct"], 8.2)   # unchanged, existing consumers survive
        self.assertEqual(out["oi_chg_pct_4h"], 8.2)

    def test_no_data_verdict_has_no_oi_change_pct_no_alias_added(self):
        raw = json.dumps({"ticker": "X", "verdict": "NO_DATA", "wash_score": None,
                          "reason": "no Binance perp"})
        out = self.f.run(raw, {"ticker": "X"})
        self.assertNotIn("oi_chg_pct_4h", out)

    # crime-desk-core: the delegated `oi_sides` registry entry was removed (its invoke target
    # lives in the legacy parts bin outside this repo — see KNOWN-ISSUES.md §A). The filter
    # itself is pure and stays tested above; the registry-wiring assertion no longer applies.


class TestRegistry(unittest.TestCase):
    def test_statuses_resolved(self):
        caps = json.loads((ROOT / "capabilities.json").read_text())
        for name, spec in caps.items():
            if name.startswith("_"):
                continue
            self.assertNotIn("unverified", spec["status"],
                             f"{name} still unverified")

    def test_analyse_native(self):
        # analyse was upgraded from delegated+filter to a native capability
        # (SPEC 1b on-chain budget + SPEC 4 §4 gate). The retired filter file is kept.
        caps = json.loads((ROOT / "capabilities.json").read_text())
        self.assertIsNone(caps["analyse"]["filter"])
        self.assertEqual(caps["analyse"]["status"], "native")
        self.assertTrue((ROOT / "capabilities" / "analyse.py").exists())
        self.assertTrue((ROOT / "filters" / "analyse.py").exists())  # retired, kept

    def test_every_cap_has_test_and_doc(self):
        caps = json.loads((ROOT / "capabilities.json").read_text())
        for name, spec in caps.items():
            if name.startswith("_"):
                continue
            self.assertTrue((ROOT / spec["test"]).exists(), f"{name} test missing")
            self.assertTrue((ROOT / spec["doc"]).exists(), f"{name} doc missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
