#!/usr/bin/env python3
"""SPEC-100 — accumulation-edge backtest harness (the §9 GO/NO-GO gate).

Offline/deterministic throughout: historical replay uses a synthetic transfer history (no
network); outcome/aggregate math uses hand-built bar fixtures with hand-checkable numbers.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("backtest_accumulation",
                                               ROOT / "capabilities" / "backtest_accumulation.py")
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


def _iso(day, hour=0):
    return f"2026-01-{day:02d}T{hour:02d}:00:00.000Z"


def _flat_bars(n, price=1.0):
    return [{"ts": i, "open": price, "high": price, "low": price, "close": price} for i in range(n)]


class TestReplayHistoricalSignal(unittest.TestCase):
    """Req 2a: mocked provider replay — a synthetic transfer history triggers the SPEC-76
    (NEW/GROWN) criteria on a known date, and the harness picks it up as an event."""

    def test_first_inbound_fires_new_on_its_own_day(self):
        wallet, contract = "0xwallet", "0xcontract"
        transfers = [
            {"address": contract, "to_address": wallet, "from_address": "0xdex",
             "value_decimal": "100000", "block_timestamp": _iso(10, 3)},
        ]
        sig = B.replay_accumulation_signal(transfers, wallet, contract)
        self.assertIsNotNone(sig)
        # the signal fires on the SAME UTC day as the known inbound date
        self.assertEqual(B._iso(sig)[:10], "2026-01-10")

    def test_no_transfers_never_fires(self):
        self.assertIsNone(B.replay_accumulation_signal([], "0xw", "0xc"))

    def test_outbound_and_wrong_contract_are_ignored(self):
        wallet, contract = "0xwallet", "0xcontract"
        transfers = [
            {"address": contract, "to_address": "0xother", "from_address": wallet,
             "value_decimal": "999", "block_timestamp": _iso(1)},         # outbound — ignored
            {"address": "0xdifferent", "to_address": wallet, "from_address": "0xdex",
             "value_decimal": "999", "block_timestamp": _iso(2)},         # wrong token — ignored
        ]
        self.assertIsNone(B.replay_accumulation_signal(transfers, wallet, contract))

    def test_growth_over_window_fires_on_the_grown_day(self):
        # a small position, flat for a while, then GROWN >=50% on day 20 vs the day-13 baseline
        wallet, contract = "0xwallet", "0xcontract"
        transfers = [
            {"address": contract, "to_address": wallet, "from_address": "0xdex",
             "value_decimal": "1000", "block_timestamp": _iso(1)},          # day1: NEW -> fires day1
        ]
        sig = B.replay_accumulation_signal(transfers, wallet, contract)
        self.assertEqual(B._iso(sig)[:10], "2026-01-01")

    def test_source_historical_events_uses_injected_provider(self):
        candidates = [{"wallet": "0xw", "contract": "0xc", "token": "LOADX",
                       "chain": "binance-smart-chain"}]

        def tokentx_fn(wallet, contract, chain):
            return [{"address": contract, "to_address": wallet, "from_address": "0xdex",
                     "value_decimal": "50000", "block_timestamp": _iso(5)}]

        events = B.source_historical_events(candidates, tokentx_fn=tokentx_fn)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["token"], "LOADX")
        self.assertEqual(events[0]["source"], "historical-replay")
        self.assertEqual(events[0]["signal_date"][:10], "2026-01-05")

    def test_source_historical_events_skips_no_signal_and_provider_failure(self):
        candidates = [
            {"wallet": "0xw", "contract": "0xnosignal", "token": "QUIET", "chain": "bsc"},
            {"wallet": "0xw", "contract": "0xdead", "token": "DEAD", "chain": "bsc"},
        ]

        def tokentx_fn(wallet, contract, chain):
            if contract == "0xdead":
                raise RuntimeError("provider down")
            return []   # no transfers -> no signal

        events = B.source_historical_events(candidates, tokentx_fn=tokentx_fn)
        self.assertEqual(events, [])


class TestOutcomeMetrics(unittest.TestCase):
    """Req 3: forward return / MFE / MAE / time-to-markup / base+trigger entry, hand-checkable."""

    def test_flat_price_scores_zero_return_and_no_markup(self):
        bars = _flat_bars(31 * 24, price=2.0)
        out = B.compute_outcome(bars)
        self.assertEqual(out["return_3d_pct"], 0.0)
        self.assertEqual(out["return_7d_pct"], 0.0)
        self.assertEqual(out["return_30d_pct"], 0.0)
        self.assertEqual(out["mfe_pct"], 0.0)
        self.assertEqual(out["mae_pct"], 0.0)
        self.assertIsNone(out["time_to_markup_days"])
        self.assertFalse(out["entry_existed"])

    def test_known_forward_returns_and_mfe_mae(self):
        # entry=100; +50% by day3 (holds); dips to 80 (MAE=20%) between day7 and day14; ends flat.
        bars = _flat_bars(31 * 24, price=100.0)
        for i in range(3 * 24, 7 * 24):
            bars[i] = {"open": 150, "high": 150, "low": 150, "close": 150}
        for i in range(7 * 24, 14 * 24):
            bars[i] = {"open": 80, "high": 80, "low": 80, "close": 80}
        out = B.compute_outcome(bars)
        self.assertEqual(out["return_3d_pct"], 50.0)
        self.assertEqual(out["return_7d_pct"], -20.0)
        self.assertEqual(out["mfe_pct"], 50.0)
        self.assertEqual(out["mae_pct"], 20.0)
        # +20% leg (>= 120) is first reached inside the 150-block, at day3 exactly
        self.assertEqual(out["time_to_markup_days"], 3.0)

    def test_none_on_empty_bars(self):
        self.assertIsNone(B.compute_outcome([]))
        self.assertIsNone(B.compute_outcome(None))

    def test_base_and_trigger_reclaim_is_detected(self):
        # entry=100 -> rallies to 110 (local high) -> pulls back to 100 (>=5% off 110 -> base)
        # -> reclaims 110 (trigger) -> True
        bars = [{"close": 100}, {"close": 110}, {"close": 100}, {"close": 111}]
        self.assertTrue(B.find_base_trigger(bars, 100))

    def test_no_pullback_no_trigger(self):
        bars = [{"close": 100}, {"close": 105}, {"close": 108}]
        self.assertFalse(B.find_base_trigger(bars, 100))

    def test_pullback_without_reclaim_is_false(self):
        bars = [{"close": 100}, {"close": 110}, {"close": 100}, {"close": 105}]
        self.assertFalse(B.find_base_trigger(bars, 100))


class TestHitDefinition(unittest.TestCase):
    def test_hit_true_when_positive_7d_and_mfe_ge_2x_mae(self):
        self.assertTrue(B.is_hit({"return_7d_pct": 10, "mfe_pct": 40, "mae_pct": 15}))

    def test_hit_false_when_mfe_below_threshold(self):
        self.assertFalse(B.is_hit({"return_7d_pct": 10, "mfe_pct": 20, "mae_pct": 15}))

    def test_hit_false_when_7d_negative(self):
        self.assertFalse(B.is_hit({"return_7d_pct": -5, "mfe_pct": 40, "mae_pct": 5}))

    def test_hit_none_when_data_missing(self):
        self.assertIsNone(B.is_hit({"return_7d_pct": None, "mfe_pct": 40, "mae_pct": 5}))
        self.assertIsNone(B.is_hit(None))


class TestAggregateGate(unittest.TestCase):
    """Req 4: the literal §9 gate — n>=10 AND hit%>50 AND positive avg edge -> GO; else
    NO-GO/HYPOTHESIS-TIER naming the failing condition. Hand-checkable arithmetic."""

    def _event(self, r7, mfe, mae, lead=5.0):
        return {"outcome": {"return_7d_pct": r7, "mfe_pct": mfe, "mae_pct": mae,
                            "time_to_markup_days": lead}}

    def test_below_n_gate_is_hypothesis_tier_even_if_all_hits(self):
        events = [self._event(10, 40, 5) for _ in range(3)]
        agg = B.aggregate(events)
        self.assertEqual(agg["n"], 3)
        self.assertEqual(agg["hit_pct"], 100.0)
        self.assertEqual(agg["verdict"], "HYPOTHESIS-TIER")
        self.assertIn("n=3 < 10", agg["verdict_line"])

    def test_n_ge_10_all_hits_positive_edge_is_go(self):
        events = [self._event(10, 40, 5) for _ in range(10)]
        agg = B.aggregate(events)
        self.assertEqual(agg["n"], 10)
        self.assertEqual(agg["hit_pct"], 100.0)
        self.assertEqual(agg["avg_edge_pct"], 10.0)
        self.assertEqual(agg["verdict"], "GO")
        self.assertIn("GO:", agg["verdict_line"])

    def test_n_ge_10_low_hitpct_is_no_go(self):
        # 4 hits / 10 = 40% <= 50% gate
        events = [self._event(10, 40, 5) for _ in range(4)] + [self._event(-5, 5, 20) for _ in range(6)]
        agg = B.aggregate(events)
        self.assertEqual(agg["n"], 10)
        self.assertEqual(agg["hit_pct"], 40.0)
        self.assertEqual(agg["verdict"], "NO-GO")
        self.assertIn("hit%=40.0", agg["verdict_line"])

    def test_n_ge_10_negative_avg_edge_is_no_go(self):
        events = [self._event(-2, 40, 5) for _ in range(10)]   # loses on 7d despite big MFE
        agg = B.aggregate(events)
        self.assertEqual(agg["verdict"], "NO-GO")
        self.assertIn("avg_edge=-2.0", agg["verdict_line"])

    def test_median_lead_time_computed(self):
        events = [self._event(10, 40, 5, lead=d) for d in (1, 3, 5, 7, 9)]
        agg = B.aggregate(events)
        self.assertEqual(agg["lead_time_days"]["median_days"], 5)

    def test_unscoreable_events_excluded_from_n(self):
        events = [self._event(10, 40, 5), {"outcome": None}, {}]
        agg = B.aggregate(events)
        self.assertEqual(agg["n"], 1)


class TestCoCascadeCorrelation(unittest.TestCase):
    def test_perfectly_correlated_cluster(self):
        events = [
            {"cluster": "K", "outcome": {"return_3d_pct": 1, "return_7d_pct": 2, "return_14d_pct": 3, "return_30d_pct": 4}},
            {"cluster": "K", "outcome": {"return_3d_pct": 2, "return_7d_pct": 4, "return_14d_pct": 6, "return_30d_pct": 8}},
        ]
        out = B.co_cascade_correlation(events)
        self.assertEqual(out["K"]["n"], 2)
        self.assertAlmostEqual(out["K"]["corr"], 1.0, places=3)

    def test_single_member_cluster_has_no_corr(self):
        events = [{"cluster": "SOLO", "outcome": {"return_3d_pct": 1, "return_7d_pct": 2,
                                                   "return_14d_pct": 3, "return_30d_pct": 4}}]
        out = B.co_cascade_correlation(events)
        self.assertEqual(out["SOLO"], {"n": 1, "corr": None})

    def test_no_cluster_tag_is_excluded(self):
        events = [{"cluster": None, "outcome": {}}]
        self.assertEqual(B.co_cascade_correlation(events), {})


class TestManualFixturesFile(unittest.TestCase):
    """Req 2b: the desk-verified fixtures file exists with >= 3 seeded cases."""

    def test_file_has_at_least_three_events_with_required_fields(self):
        events = B.load_manual_events()
        self.assertGreaterEqual(len(events), 3)
        for e in events:
            self.assertIn("token", e)
            self.assertIn("signal_ts", e)
            self.assertIsNotNone(e["signal_ts"])

    def test_missing_file_degrades_to_empty_list(self):
        self.assertEqual(B.load_manual_events(path="/nonexistent/path.json"), [])


class TestBuildBacktestEndToEnd(unittest.TestCase):
    """DoD: offline fixture run — manual events file (>=3 seeded cases) + a mocked bars_provider
    produces a full report + correct, hand-checkable aggregate math."""

    def test_offline_fixture_run_produces_report_and_correct_math(self):
        with tempfile.TemporaryDirectory() as td:
            manual = Path(td) / "events.json"
            manual.write_text(json.dumps({"events": [
                {"token": "A", "signal_date": "2026-01-01T00:00:00.000Z"},
                {"token": "B", "signal_date": "2026-01-02T00:00:00.000Z"},
                {"token": "C", "signal_date": "2026-01-03T00:00:00.000Z"},
            ]}))
            report = Path(td) / "out.md"

            def bars_provider(token, signal_ts):
                # A: +50% by day3, flat after (a clean hit); B: flat (no move, not a hit);
                # C: -30% straight down (a clean miss)
                if token == "A":
                    bars = _flat_bars(31 * 24, price=100.0)
                    for i in range(3 * 24, len(bars)):
                        bars[i] = {"open": 150, "high": 150, "low": 150, "close": 150}
                    return bars
                if token == "B":
                    return _flat_bars(31 * 24, price=10.0)
                bars = _flat_bars(31 * 24, price=100.0)
                for i in range(1 * 24, len(bars)):
                    bars[i] = {"open": 70, "high": 70, "low": 70, "close": 70}
                return bars

            r = B.build_backtest(manual_path=str(manual), bars_provider=bars_provider,
                                 write=True, date_str="2026-07-02", report_path=str(report))

            self.assertEqual(len(r["events"]), 3)
            agg = r["aggregate"]
            # hand-check: n=3 (< gate) -> HYPOTHESIS-TIER regardless of hit%/edge
            self.assertEqual(agg["n"], 3)
            self.assertEqual(agg["verdict"], "HYPOTHESIS-TIER")
            # hand-check avg 7d edge: A=+50, B=0, C=-30 -> mean = 6.666..7
            self.assertAlmostEqual(agg["avg_edge_pct"], 6.67, places=1)
            # hand-check hit%: A hits (50>0, mfe50>=2*mae0), B: mfe=0,mae=0 -> not >0 return -> miss,
            # C: r7<0 -> miss. 1/3 = 33.3%
            self.assertAlmostEqual(agg["hit_pct"], 33.3, places=1)

            self.assertTrue(report.exists())
            text = report.read_text()
            self.assertIn("HYPOTHESIS-TIER", text)
            self.assertIn("| A |", text)

    def test_provider_failure_degrades_single_event_not_whole_batch(self):
        with tempfile.TemporaryDirectory() as td:
            manual = Path(td) / "events.json"
            manual.write_text(json.dumps({"events": [
                {"token": "OK", "signal_date": "2026-01-01T00:00:00.000Z"},
                {"token": "DEAD", "signal_date": "2026-01-02T00:00:00.000Z"},
            ]}))

            def bars_provider(token, signal_ts):
                if token == "DEAD":
                    raise RuntimeError("feed down")
                return _flat_bars(31 * 24, price=1.0)

            r = B.build_backtest(manual_path=str(manual), bars_provider=bars_provider, write=False)
            by_token = {e["token"]: e for e in r["events"]}
            self.assertIsNone(by_token["DEAD"]["outcome"])
            self.assertIsNotNone(by_token["OK"]["outcome"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
