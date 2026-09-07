#!/usr/bin/env python3
"""SPEC-169 — the risk-card line (`capabilities/risk_card.py`).

`build_risk_line` is pure (every live input injected) so the arithmetic + each binding
cap + the equity-unknown path are testable with no network. The `resolve_*` helpers are
tested separately against fixtures (aster_max_leverage.json / positions.json / an
injected maxsize fetch function / a monkeypatched classify.cluster_heat_for) — never a
live venue/network call. All ledger reads go through a temp LEDGER_PATH.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import ledger as LG            # noqa: E402
import risk_card as RC         # noqa: E402


class _LedgerTmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = LG.LEDGER_PATH
        LG.LEDGER_PATH = Path(self._tmp.name) / "ledger.jsonl"

    def tearDown(self):
        LG.LEDGER_PATH = self._orig
        self._tmp.cleanup()

    def _fill_go(self, sig="trap_formation_long"):
        for i in range(10):
            LG.record({"ticker": f"T{i}", "direction": "LONG", "signature": sig,
                      "outcome": "tp1", "pnl_r": 1.0, "close_ts": f"2026-01-{i + 1:02d}T00:00:00Z"})


class BuildRiskLineArithmetic(_LedgerTmp):
    def test_hypothesis_tier_no_records_line_shape(self):
        row = RC.build_risk_line("GALA", "faded_bounce", "SHORT", entry=100.0, stop=105.0,
                                 equity=200.0, max_lev=5.0, maxsize_exit_usd=10_000.0)
        self.assertEqual(row["tier"], "hypothesis")
        self.assertEqual(row["risk_pct"], 5)
        self.assertAlmostEqual(row["risk_usd"], 10.0)                 # 200 * 5%
        self.assertAlmostEqual(row["stop_distance_pct"], 5.0)         # (105-100)/100
        self.assertAlmostEqual(row["notional_usd"], 200.0)            # 10 / (5/100)
        self.assertIn("tier=hypothesis (n=0, 0R, trailing-10 n/aR)", row["line"])
        self.assertIn("max lev 5x (venue)", row["line"])
        self.assertIn("risk cap 5% eq = $10 at stop", row["line"])
        self.assertIn("exit-absorbable $10,000", row["line"])
        self.assertIn("liq-distance 20% at 5x", row["line"])          # 100/5

    def test_go_tier_uses_go_risk_pct_when_ceiling_condition_met(self):
        # SPEC-180 req 4: GO-tier 10% requires oi_construction verdict != UNKNOWN AND
        # gating_ok=true AT THIS COMMIT.
        self._fill_go()
        oic = {"verdict": "DIRECTIONAL", "gating_ok": True}
        row = RC.build_risk_line("X", "trap_formation_long", "LONG", entry=1.0, stop=0.9,
                                 equity=1000.0, max_lev=10.0, oi_construction=oic)
        self.assertEqual(row["tier"], "go")
        self.assertEqual(row["risk_pct"], 10)
        self.assertAlmostEqual(row["risk_usd"], 100.0)
        self.assertIn("tier=go (n=10, 10R, trailing-10 10R)", row["line"])
        self.assertIsNone(row["go_ceiling_note"])

    def test_go_tier_caps_at_hypothesis_when_ceiling_condition_not_met(self):
        # SPEC-180 req 4: no oi_construction at commit -> GO caps at hypothesis 5%,
        # reason printed — never a trade-blocking failure, just a size haircut.
        self._fill_go()
        row = RC.build_risk_line("X", "trap_formation_long", "LONG", entry=1.0, stop=0.9,
                                 equity=1000.0, max_lev=10.0)
        self.assertEqual(row["tier"], "go")
        self.assertEqual(row["risk_pct"], 5)
        self.assertIn("GO-ceiling not met", row["go_ceiling_note"])
        self.assertIn("GO-ceiling not met", row["line"])

    def test_go_tier_stale_gating_ok_also_caps(self):
        self._fill_go()
        oic = {"verdict": "DIRECTIONAL", "gating_ok": False}
        row = RC.build_risk_line("X", "trap_formation_long", "LONG", entry=1.0, stop=0.9,
                                 equity=1000.0, max_lev=10.0, oi_construction=oic)
        self.assertEqual(row["risk_pct"], 5)

    def test_hypothesis_tier_never_haircut_by_missing_oic(self):
        # req 4: "Hypothesis-tier commits: UNKNOWN annotates only, never haircuts."
        row = RC.build_risk_line("GALA", "faded_bounce", "SHORT", entry=100.0, stop=105.0,
                                 equity=200.0, max_lev=5.0)
        self.assertEqual(row["tier"], "hypothesis")
        self.assertEqual(row["risk_pct"], 5)
        self.assertIsNone(row["go_ceiling_note"])

    def test_decaying_flag_surfaces_in_line(self):
        self._fill_go()
        for i in range(10):
            LG.record({"ticker": f"D{i}", "direction": "LONG", "signature": "trap_formation_long",
                      "outcome": "tp1", "pnl_r": 0.05, "close_ts": f"2026-02-{i + 1:02d}T00:00:00Z"})
        row = RC.build_risk_line("X", "trap_formation_long", "LONG", equity=100.0)
        self.assertEqual(row["tier_flag"], "decaying")
        self.assertIn("DECAYING", row["line"])

    def test_equity_unknown_no_risk_cap(self):
        row = RC.build_risk_line("X", "discretionary", "LONG", entry=1.0, stop=0.9)
        self.assertIsNone(row["risk_usd"])
        self.assertIn("risk cap n/a (equity=unknown)", row["line"])
        self.assertIsNone(row["bound"])                    # no equity -> no tier/lev cap

    def test_max_lev_unknown_no_liq_distance(self):
        row = RC.build_risk_line("X", "discretionary", "LONG", entry=1.0, stop=0.9, equity=100.0)
        self.assertIsNone(row["max_lev"])
        self.assertIn("max lev unknown (venue)", row["line"])
        self.assertIn("liq-distance unknown", row["line"])

    def test_bound_picks_smallest_cap_tier(self):
        # tier cap: risk_usd=100 / 10% stop = notional 1000 -> SMALLEST
        row = RC.build_risk_line("X", "discretionary", "LONG", entry=1.0, stop=0.9,
                                 equity=1000.0, max_lev=50.0,          # lev cap = 50,000
                                 maxsize_exit_usd=20_000.0)            # exit cap = 20,000
        self.assertEqual(row["bound"], "tier")
        self.assertAlmostEqual(row["sized_usd"], row["notional_usd"])
        self.assertIn("bound=tier", row["line"])

    def test_bound_picks_smallest_cap_exit(self):
        row = RC.build_risk_line("X", "discretionary", "LONG", entry=1.0, stop=0.5,
                                 # huge stop distance -> huge tier notional
                                 equity=1_000_000.0, max_lev=50.0, maxsize_exit_usd=500.0)
        self.assertEqual(row["bound"], "exit")
        self.assertAlmostEqual(row["sized_usd"], 500.0)

    def test_bound_picks_smallest_cap_lev(self):
        row = RC.build_risk_line("X", "discretionary", "LONG", entry=1.0, stop=0.5,
                                 equity=1_000_000.0, max_lev=0.001, maxsize_exit_usd=500_000.0)
        self.assertEqual(row["bound"], "lev")

    def test_cluster_heat_appended_when_given(self):
        row = RC.build_risk_line("X", "discretionary", "LONG", entry=1.0, stop=0.9,
                                 equity=1000.0, cluster_heat_usd=120.0)
        self.assertEqual(row["cluster_heat_pct"], 12.0)
        self.assertIn("cluster heat 12% of 6%", row["line"])

    def test_no_cluster_heat_line_when_not_given(self):
        row = RC.build_risk_line("X", "discretionary", "LONG", entry=1.0, stop=0.9, equity=1000.0)
        self.assertNotIn("cluster heat", row["line"])

    def test_no_entry_stop_still_renders_no_crash(self):
        row = RC.build_risk_line("X", "discretionary", "LONG", equity=100.0, max_lev=5.0)
        self.assertIsNone(row["notional_usd"])
        self.assertIsInstance(row["line"], str)


class LiveRiskLineResolveMaxsizeToggle(_LedgerTmp):
    def test_resolve_maxsize_false_skips_the_network_leg(self):
        calls = []

        def fake_maxsize_import_guard(*a, **kw):
            calls.append(a)
            return 999.0

        orig = RC.resolve_maxsize_exit_usd
        RC.resolve_maxsize_exit_usd = fake_maxsize_import_guard
        try:
            row = RC.live_risk_line("GALA", "faded_bounce", "SHORT", equity_arg=100.0,
                                    resolve_maxsize=False)
        finally:
            RC.resolve_maxsize_exit_usd = orig
        self.assertEqual(calls, [])                       # never invoked
        self.assertIsNone(row["maxsize_exit_usd"])

    def test_resolve_maxsize_true_calls_the_resolver(self):
        orig = RC.resolve_maxsize_exit_usd
        RC.resolve_maxsize_exit_usd = lambda *a, **kw: 555.0
        try:
            row = RC.live_risk_line("GALA", "faded_bounce", "SHORT", equity_arg=100.0,
                                    resolve_maxsize=True)
        finally:
            RC.resolve_maxsize_exit_usd = orig
        self.assertEqual(row["maxsize_exit_usd"], 555.0)

    def test_resolve_oic_default_false_never_calls_the_resolver(self):
        calls = []
        orig = RC.resolve_oi_construction
        RC.resolve_oi_construction = lambda *a, **kw: (calls.append(a), None)[1]
        try:
            RC.live_risk_line("GALA", "faded_bounce", "SHORT", equity_arg=100.0)
        finally:
            RC.resolve_oi_construction = orig
        self.assertEqual(calls, [])

    def test_resolve_oic_true_calls_the_resolver(self):
        orig = RC.resolve_oi_construction
        RC.resolve_oi_construction = lambda t: {"verdict": "DIRECTIONAL", "gating_ok": True}
        try:
            row = RC.live_risk_line("GALA", "trap_formation_long", "SHORT", equity_arg=100.0,
                                    resolve_oic=True)
        finally:
            RC.resolve_oi_construction = orig
        self.assertIsNone(row["go_ceiling_note"])  # condition met, no downgrade note


class LoadSizingCfg(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cfg_dir = RC.CONFIG_DIR
        RC.CONFIG_DIR = Path(self._tmp.name)

    def tearDown(self):
        RC.CONFIG_DIR = self._orig_cfg_dir
        self._tmp.cleanup()

    def test_defaults_when_no_file(self):
        cfg = RC.load_sizing_cfg()
        self.assertEqual(cfg["risk_pct_by_tier"], {"hypothesis": 5, "go": 10, "demoted": 5})
        self.assertEqual(cfg["cluster_heat_pct"], 6)
        self.assertEqual(cfg["exit_slippage_pct"], 0.5)

    def test_reads_sizing_json(self):
        (RC.CONFIG_DIR / "sizing.json").write_text(json.dumps(
            {"risk_pct_by_tier": {"hypothesis": 3, "go": 8, "demoted": 3},
             "cluster_heat_pct": 4, "exit_slippage_pct": 1.0}))
        cfg = RC.load_sizing_cfg()
        self.assertEqual(cfg["risk_pct_by_tier"]["go"], 8)
        self.assertEqual(cfg["cluster_heat_pct"], 4)

    def test_fallback_to_positions_json_max_operator_heat_pct(self):
        (RC.CONFIG_DIR / "sizing.json").write_text(json.dumps(
            {"risk_pct_by_tier": {"hypothesis": 5, "go": 10, "demoted": 5}}))
        (RC.CONFIG_DIR / "positions.json").write_text(json.dumps({"max_operator_heat_pct": 9.0}))
        cfg = RC.load_sizing_cfg()
        self.assertEqual(cfg["cluster_heat_pct"], 9.0)


class ResolveEquity(unittest.TestCase):
    """SPEC-191 #3: `resolve_equity` returns (equity, source, reason) — the historical
    bug was reading `perp_total_value_usd` off the TOP level of venue_account.equity()'s
    return, when the real shape nests it under `data` (`{"ok": True, "data": {...}}`);
    that silently made the venue path never win even with a live key. Fakes here use the
    REAL shape so a regression back to the old flat read fails these tests."""

    def test_no_venue_module_falls_back_to_arg(self):
        import types
        fake = types.ModuleType("venue_account")
        fake.equity = lambda: {"ok": False, "error": "boom"}
        sys.modules["venue_account"] = fake
        try:
            eq, src, reason = RC.resolve_equity(equity_arg=250.0)
        finally:
            sys.modules.pop("venue_account", None)
        self.assertEqual((eq, src), (250.0, "arg"))
        self.assertIsNone(reason)   # arg fallback succeeded -> no alarm needed

    def test_no_venue_no_arg_is_unknown_names_reason(self):
        import types
        fake = types.ModuleType("venue_account")
        fake.equity = lambda: {"ok": False, "error": "aster_key_missing"}
        sys.modules["venue_account"] = fake
        try:
            eq, src, reason = RC.resolve_equity(equity_arg=None)
        finally:
            sys.modules.pop("venue_account", None)
        self.assertEqual((eq, src), (None, None))
        self.assertEqual(reason, "no_key")

    def test_module_missing_entirely_is_import_error(self):
        import builtins
        orig_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "venue_account":
                raise ImportError("no module")
            return orig_import(name, *a, **kw)
        builtins.__import__ = fake_import
        try:
            eq, src, reason = RC.resolve_equity(equity_arg=None)
        finally:
            builtins.__import__ = orig_import
        self.assertEqual((eq, src), (None, None))
        self.assertEqual(reason, "import_error")

    def test_venue_module_present_wins_over_arg(self):
        import types
        fake = types.ModuleType("venue_account")
        fake.equity = lambda: {"ok": True, "data": {"perp_total_value_usd": 199.0}}
        sys.modules["venue_account"] = fake
        try:
            eq, src, reason = RC.resolve_equity(equity_arg=9999.0)
            self.assertEqual((eq, src), (199.0, "venue"))
            self.assertIsNone(reason)
        finally:
            sys.modules.pop("venue_account", None)

    def test_venue_module_raising_falls_back_to_arg(self):
        import types
        fake = types.ModuleType("venue_account")
        def _boom():
            raise RuntimeError("dead key")
        fake.equity = _boom
        sys.modules["venue_account"] = fake
        try:
            eq, src, reason = RC.resolve_equity(equity_arg=42.0)
            self.assertEqual((eq, src), (42.0, "arg"))
            self.assertIsNone(reason)
        finally:
            sys.modules.pop("venue_account", None)

    def test_no_key_reason_no_arg_fallback(self):
        import types
        fake = types.ModuleType("venue_account")
        fake.equity = lambda: {"ok": False, "error": "aster_key_missing"}
        sys.modules["venue_account"] = fake
        try:
            eq, src, reason = RC.resolve_equity(equity_arg=None)
        finally:
            sys.modules.pop("venue_account", None)
        self.assertIsNone(eq)
        self.assertEqual(reason, "no_key")

    def test_http_status_reason(self):
        import types
        fake = types.ModuleType("venue_account")
        fake.equity = lambda: {"ok": False, "error": "Unauthorized", "http_status": 401}
        sys.modules["venue_account"] = fake
        try:
            eq, src, reason = RC.resolve_equity(equity_arg=None)
        finally:
            sys.modules.pop("venue_account", None)
        self.assertEqual(reason, "http_401")

    def test_timeout_exception_reason(self):
        import types
        fake = types.ModuleType("venue_account")
        def _boom():
            raise TimeoutError("timed out")
        fake.equity = _boom
        sys.modules["venue_account"] = fake
        try:
            eq, src, reason = RC.resolve_equity(equity_arg=None)
        finally:
            sys.modules.pop("venue_account", None)
        self.assertEqual(reason, "timeout")

    def test_line_carries_the_reason_when_equity_unknown(self):
        row = RC.build_risk_line("X", "discretionary", "LONG", entry=1.0, stop=0.9,
                                 equity_reason="no_key")
        self.assertIn("equity=unknown: no_key", row["line"])

    def test_line_backward_compatible_when_no_reason_given(self):
        row = RC.build_risk_line("X", "discretionary", "LONG", entry=1.0, stop=0.9)
        self.assertIn("equity=unknown)", row["line"])
        self.assertNotIn("equity=unknown:", row["line"])


class ResolveMaxLev(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cfg_dir = RC.CONFIG_DIR
        RC.CONFIG_DIR = Path(self._tmp.name)

    def tearDown(self):
        RC.CONFIG_DIR = self._orig_cfg_dir
        self._tmp.cleanup()

    def test_arg_wins(self):
        self.assertEqual(RC.resolve_max_lev("GALA", max_lev_arg=7.0), 7.0)

    def test_reads_config_dict_shape(self):
        (RC.CONFIG_DIR / "aster_max_leverage.json").write_text(
            json.dumps({"GALAUSDT": {"max_lev": 5, "brackets": []}}))
        self.assertEqual(RC.resolve_max_lev("GALA"), 5.0)

    def test_missing_symbol_is_none(self):
        (RC.CONFIG_DIR / "aster_max_leverage.json").write_text(json.dumps({}))
        self.assertIsNone(RC.resolve_max_lev("GALA"))

    def test_missing_file_is_none(self):
        self.assertIsNone(RC.resolve_max_lev("GALA"))


class ResolveMaxsizeExit(unittest.TestCase):
    def test_injected_fetch_fn(self):
        self.assertEqual(
            RC.resolve_maxsize_exit_usd("GALA", "SHORT", 0.5, fetch_fn=lambda t, s, b: 4321.0),
            4321.0)

    def test_injected_fetch_fn_raising_is_none(self):
        def boom(t, s, b):
            raise RuntimeError("dead book")
        self.assertIsNone(RC.resolve_maxsize_exit_usd("GALA", "SHORT", 0.5, fetch_fn=boom))


class PositionRiskUsd(unittest.TestCase):
    def test_explicit_risk_usd(self):
        self.assertEqual(RC._position_risk_usd({"risk_usd": 47.7}), 47.7)

    def test_derived_from_entry_stop_notional(self):
        # stop_dist_pct = |0.9-1.0|/1.0 = 0.10; notional 1000 -> 100
        self.assertAlmostEqual(
            RC._position_risk_usd({"entry": 1.0, "stop": 0.9, "notional_usd": 1000.0}), 100.0)

    def test_derived_from_size_usd_and_leverage_string(self):
        # notional = 50 * 5x = 250; stop_dist = |0.9-1.0|/1.0=0.10 -> 25
        self.assertAlmostEqual(
            RC._position_risk_usd({"entry": 1.0, "stop": 0.9, "size_usd": 50.0, "leverage": "5x"}),
            25.0)

    def test_unresolvable_shape_is_none(self):
        self.assertIsNone(RC._position_risk_usd({"ticker": "X"}))


class ResolveClusterHeat(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        import classify as CL
        self._CL = CL
        self._orig_fn = CL.cluster_heat_for

    def tearDown(self):
        self._CL.cluster_heat_for = self._orig_fn
        self._tmp.cleanup()

    def _positions_path(self, positions):
        p = Path(self._tmp.name) / "positions.json"
        p.write_text(json.dumps({"positions": positions}))
        return p

    def test_no_cluster_is_none(self):
        self._CL.cluster_heat_for = lambda ticker, **kw: None
        self.assertIsNone(RC.resolve_cluster_heat_usd("SOLO", this_trade_risk_usd=10.0))

    def test_cluster_but_no_mate_position_is_none(self):
        self._CL.cluster_heat_for = lambda ticker, **kw: {"cluster": "X", "members": ["SOLO", "OTHER"]}
        pos_path = self._positions_path([{"ticker": "UNRELATED", "risk_usd": 50.0}])
        self.assertIsNone(RC.resolve_cluster_heat_usd(
            "SOLO", this_trade_risk_usd=10.0, positions_path=pos_path))

    def test_cluster_with_mate_position_sums(self):
        self._CL.cluster_heat_for = lambda ticker, **kw: {"cluster": "X", "members": ["SOLO", "MATE"]}
        pos_path = self._positions_path([
            {"ticker": "MATE", "risk_usd": 30.0},
            {"ticker": "SOLO", "risk_usd": 999.0},        # self excluded from the mate sum
        ])
        total = RC.resolve_cluster_heat_usd("SOLO", this_trade_risk_usd=10.0, positions_path=pos_path)
        self.assertAlmostEqual(total, 40.0)               # 10 (own) + 30 (mate), SOLO's own row skipped


class NoDeadSizeConstantsInCardStrings(unittest.TestCase):
    """SPEC-169 req 3: the $500 / $1k figures are DEAD (account restarted 2026-08-28) —
    grep every capability source file for the literal strings so a reintroduced hardcode
    fails the suite instead of silently reappearing on a card."""

    def test_no_500_or_1k_dollar_literals_in_capabilities(self):
        offenders = []
        for f in sorted((ROOT / "capabilities").glob("*.py")):
            text = f.read_text()
            for needle in ("$500", "$1k", "$1,000"):
                if needle in text:
                    offenders.append(f"{f.name}: {needle!r}")
        self.assertEqual(offenders, [], f"dead size constants found: {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
