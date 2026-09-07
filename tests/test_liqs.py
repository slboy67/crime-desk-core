#!/usr/bin/env python3
"""SPEC-103 — `liqs`: per-venue forced-liquidation stream as OI ground truth.

Run:  python3 -m unittest tests.test_liqs -v

Corpus rule under test: "when OI is faked you can't read OI — raw candles + liquidation
data are the only truth" (memory: feedback_aggregate_oi_faked_via_double_open,
feedback_hidden_build_cvd_oi_price_divergence). liq prints can't be faked the way
aggregate OI can (double-open / internal-transfer), so a liq-silent material OI move is
the double-open fingerprint.

Offline-deterministic: every venue fetch is monkeypatched (`fetch_fn` injected); no network.
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

spec = importlib.util.spec_from_file_location("liqs_t", ROOT / "capabilities" / "liqs.py")
LQ = importlib.util.module_from_spec(spec)
spec.loader.exec_module(LQ)

NOW = 2_000_000_000.0


def _ev(minutes_ago, side, usd):
    return {"ts": NOW - minutes_ago * 60, "side": side, "usd": usd}


class TestBucketLiqs(unittest.TestCase):
    def test_buckets_events_into_bars_with_cumulative_totals(self):
        events = [_ev(1, "long", 5000), _ev(1, "long", 3000), _ev(6, "short", 2000)]
        bars = LQ.bucket_liqs(events, window_h=0.2, bar_minutes=5, now=NOW)   # 12min window, 5m bars
        self.assertEqual(len(bars), 3)   # ceil(12/5)
        # most-recent bar (index -1) holds the 1-minute-ago events
        last = bars[-1]
        self.assertAlmostEqual(last["long_usd"], 8000)
        self.assertEqual(last["count"], 2)
        self.assertEqual(last["largest_usd"], 5000)
        self.assertFalse(last["liq_silence"])

    def test_empty_bar_is_liq_silent(self):
        bars = LQ.bucket_liqs([], window_h=0.2, bar_minutes=5, now=NOW)
        self.assertTrue(all(b["liq_silence"] for b in bars))
        self.assertTrue(all(b["long_usd"] == 0 and b["short_usd"] == 0 for b in bars))

    def test_cumulative_totals_accumulate_across_bars(self):
        events = [_ev(11, "short", 1000), _ev(1, "short", 1000)]
        bars = LQ.bucket_liqs(events, window_h=0.2, bar_minutes=5, now=NOW)
        self.assertEqual(bars[-1]["cum_short_usd"], 2000)


class TestOiLiqConsistency(unittest.TestCase):
    """The four DoD fixtures, verbatim."""

    def test_a_cascade_bar_heavy_long_liq_oi_drop_confirmed(self):
        events = [_ev(1, "long", 250_000), _ev(2, "long", 180_000)]
        bars = LQ.bucket_liqs(events, window_h=0.5, bar_minutes=5, now=NOW)
        r = LQ.oi_liq_consistency(oi_delta_pct=-22.0, liq_bars=bars)
        self.assertEqual(r["verdict"], "CONFIRMED")

    def test_b_bsb_shaped_oi_spike_liq_silence_suspect_fake(self):
        bars = LQ.bucket_liqs([], window_h=2, bar_minutes=5, now=NOW)   # zero liq prints
        r = LQ.oi_liq_consistency(oi_delta_pct=+18.0, liq_bars=bars, price_flat=False, cvd_confirms=None)
        self.assertEqual(r["verdict"], "SUSPECT_FAKE")

    def test_c_oi_bleed_flat_price_silence_transfer(self):
        bars = LQ.bucket_liqs([], window_h=1, bar_minutes=5, now=NOW)
        r = LQ.oi_liq_consistency(oi_delta_pct=-12.0, liq_bars=bars, price_flat=True)
        self.assertEqual(r["verdict"], "TRANSFER")

    def test_d_feed_down_unavailable_unknown_no_verdict_leakage(self):
        r = LQ.oi_liq_consistency(oi_delta_pct=-30.0, liq_bars=None)
        self.assertEqual(r["verdict"], "UNKNOWN")
        # a feed-down UNKNOWN must never accidentally read as any of the "real" verdicts
        self.assertNotIn(r["verdict"], ("CONFIRMED", "SUSPECT_FAKE", "TRANSFER"))

    def test_immaterial_oi_move_returns_no_verdict(self):
        bars = LQ.bucket_liqs([], window_h=1, bar_minutes=5, now=NOW)
        r = LQ.oi_liq_consistency(oi_delta_pct=+1.0, liq_bars=bars)
        self.assertIsNone(r["verdict"])

    def test_cvd_confirmation_prevents_suspect_fake_on_an_oi_build(self):
        bars = LQ.bucket_liqs([], window_h=1, bar_minutes=5, now=NOW)   # silent
        r = LQ.oi_liq_consistency(oi_delta_pct=+15.0, liq_bars=bars, cvd_confirms=True)
        self.assertEqual(r["verdict"], "CONFIRMED")

    def test_material_threshold_is_configurable(self):
        bars = LQ.bucket_liqs([], window_h=1, bar_minutes=5, now=NOW)
        r = LQ.oi_liq_consistency(oi_delta_pct=+3.0, liq_bars=bars, cfg={"material_oi_pct": 2.0})
        self.assertEqual(r["verdict"], "SUSPECT_FAKE")


class TestFetchProviderSeam(unittest.TestCase):
    def setUp(self):
        self._orig_providers = list(LQ._PROVIDERS)

    def tearDown(self):
        LQ._PROVIDERS[:] = self._orig_providers

    def test_okx_named_as_venue_when_it_answers(self):
        LQ._PROVIDERS[:] = [{"name": "okx", "fetch": lambda t: [_ev(1, "long", 1000)]}]
        events, venue = LQ.fetch_liqs("OPN")
        self.assertEqual(venue, "okx")
        self.assertEqual(len(events), 1)

    def test_falls_through_to_next_provider_on_none(self):
        LQ._PROVIDERS[:] = [{"name": "binance", "fetch": lambda t: None},
                            {"name": "okx", "fetch": lambda t: [_ev(1, "short", 500)]}]
        events, venue = LQ.fetch_liqs("OPN")
        self.assertEqual(venue, "okx")

    def test_falls_through_on_exception(self):
        def boom(t):
            raise RuntimeError("boom")
        LQ._PROVIDERS[:] = [{"name": "binance", "fetch": boom},
                            {"name": "okx", "fetch": lambda t: [_ev(1, "short", 500)]}]
        events, venue = LQ.fetch_liqs("OPN")
        self.assertEqual(venue, "okx")

    def test_all_providers_dead_returns_none_none(self):
        LQ._PROVIDERS[:] = [{"name": "binance", "fetch": lambda t: None},
                            {"name": "okx", "fetch": lambda t: None}]
        events, venue = LQ.fetch_liqs("NOTLISTEDANYWHERE")
        self.assertIsNone(events)
        self.assertIsNone(venue)


class TestBuildLiqs(unittest.TestCase):
    def test_available_result_names_the_venue_and_carries_verdict(self):
        def fake_fetch(ticker):
            return [_ev(1, "long", 250_000)], "okx"
        r = LQ.build_liqs("OPN", window_h=0.5, bar_minutes=5, oi_delta_pct=-20.0,
                          now=NOW, fetch_fn=fake_fetch)
        self.assertTrue(r["available"])
        self.assertEqual(r["venue"], "okx")
        self.assertEqual(r["oi_liq_consistency"]["verdict"], "CONFIRMED")
        self.assertIn("bars", r)
        self.assertGreater(r["total_long_usd"], 0)

    def test_unavailable_feed_never_a_clean_bill(self):
        r = LQ.build_liqs("NOTLISTED", oi_delta_pct=-20.0, now=NOW, fetch_fn=lambda t: (None, None))
        self.assertFalse(r["available"])
        self.assertEqual(r["oi_liq_consistency"]["verdict"], "UNKNOWN")
        self.assertIsNone(r["bars"])

    def test_fetch_exception_degrades_to_unavailable_not_a_crash(self):
        def boom(t):
            raise RuntimeError("network exploded")
        r = LQ.build_liqs("OPN", oi_delta_pct=-20.0, now=NOW, fetch_fn=boom)
        self.assertFalse(r["available"])
        self.assertEqual(r["oi_liq_consistency"]["verdict"], "UNKNOWN")

    def test_one_liner_surfaces_verdict_and_reason(self):
        r = {"oi_liq_consistency": {"verdict": "SUSPECT_FAKE", "reason": "material OI build, liq-silent"}}
        line = LQ.one_liner(r)
        self.assertIn("SUSPECT_FAKE", line)
        self.assertIn("liq-silent", line)

    def test_one_liner_none_when_no_verdict(self):
        r = {"oi_liq_consistency": {"verdict": None, "reason": "OI move below materiality threshold"}}
        self.assertIsNone(LQ.one_liner(r))


class TestBriefIntegration(unittest.TestCase):
    """DoD: `brief` carries the one-liner; no new blocking latency (liqs runs in the same
    bounded concurrent layer set every other brief section already uses)."""

    def setUp(self):
        BR_spec = importlib.util.spec_from_file_location("brief_liqs_t", ROOT / "capabilities" / "brief.py")
        self.BR = importlib.util.module_from_spec(BR_spec)
        BR_spec.loader.exec_module(self.BR)

    def test_headline_carries_the_liqs_one_liner(self):
        state = {"verdict": "CONFIRMS", "thesis_present": True}
        liqs_result = {"available": True, "venue": "okx",
                       "oi_liq_consistency": {"verdict": "SUSPECT_FAKE",
                                              "reason": "material OI build, liq-silent"}}
        h = self.BR._headline("OPN", state, {"available": False}, {"available": False},
                              {"available": False}, liqs=liqs_result)
        self.assertIn("SUSPECT_FAKE", h)

    def test_headline_unaffected_when_liqs_layer_is_none(self):
        state = {"verdict": "CONFIRMS", "thesis_present": True}
        h = self.BR._headline("OPN", state, {"available": False}, {"available": False},
                              {"available": False}, liqs=None)
        self.assertNotIn("liq:", h)

    def test_liqs_layer_is_wired_into_build_brief_jobs(self):
        import inspect
        src = inspect.getsource(self.BR.build_brief)
        self.assertIn('"liqs"', src)
        self.assertIn("_liqs_layer", src)

    def test_liqs_layer_feeds_analyse_oi_chg_pct_and_never_raises(self):
        import liqs as real_liqs
        orig_analyse = self.BR.build_analyse
        orig_fetch = real_liqs.fetch_liqs
        captured = {}

        def fake_fetch(ticker):
            return [{"ts": 0, "side": "long", "usd": 1.0}], "okx"

        self.BR.build_analyse = lambda ticker: {"oi_chg_pct": -25.0}
        real_liqs.fetch_liqs = fake_fetch
        try:
            r = self.BR._liqs_layer("OPN")
        finally:
            self.BR.build_analyse = orig_analyse
            real_liqs.fetch_liqs = orig_fetch
        self.assertTrue(r["available"])
        self.assertEqual(r["oi_liq_consistency"]["verdict"], "SUSPECT_FAKE")   # material, no confirming usd


class TestTapeIntegration(unittest.TestCase):
    """DoD: `tape` carries the one-liner, purely additive to its existing envelope."""

    def setUp(self):
        TP_spec = importlib.util.spec_from_file_location("tape_liqs_t", ROOT / "capabilities" / "tape.py")
        self.TP = importlib.util.module_from_spec(TP_spec)
        TP_spec.loader.exec_module(self.TP)

    def test_liq_consistency_note_uses_the_tape_oi_series_no_second_oi_fetch(self):
        oi_series = [{"ts": 1, "oi": 1_000_000.0}, {"ts": 2, "oi": 780_000.0}]   # -22%
        calls = []
        self.TP.liqs = None  # ensure a fresh import path inside the function
        import liqs as real_liqs

        def fake_build_liqs(ticker, window_h=4, oi_delta_pct=None, **k):
            calls.append(oi_delta_pct)
            return {"venue": "okx", "oi_liq_consistency": {"verdict": "CONFIRMED", "reason": "x"}}

        orig = real_liqs.build_liqs
        real_liqs.build_liqs = fake_build_liqs
        try:
            note = self.TP._liqs_consistency_note("OPN", oi_series, window_min=60)
        finally:
            real_liqs.build_liqs = orig
        self.assertEqual(note["verdict"], "CONFIRMED")
        self.assertAlmostEqual(calls[0], -22.0, places=1)

    def test_short_oi_series_returns_none(self):
        self.assertIsNone(self.TP._liqs_consistency_note("OPN", [], window_min=60))
        self.assertIsNone(self.TP._liqs_consistency_note("OPN", [{"ts": 1, "oi": 100.0}], window_min=60))

    def test_build_tape_envelope_carries_liq_consistency_key(self):
        import inspect
        src = inspect.getsource(self.TP.build_tape)
        self.assertIn("liq_consistency", src)


class TestWorkupIntegration(unittest.TestCase):
    """DoD: oi_sides WASH read gains the liq-silence corroborator (native code, _oldrepo
    untouched). `subprocess.run` is a process-wide stdlib singleton — every patch here is
    restored in tearDown so it never leaks into other tests."""

    def setUp(self):
        WU_spec = importlib.util.spec_from_file_location("workup_liqs_t", ROOT / "capabilities" / "workup.py")
        self.WU = importlib.util.module_from_spec(WU_spec)
        WU_spec.loader.exec_module(self.WU)
        self._orig_run = self.WU.subprocess.run
        import liqs as real_liqs
        self._real_liqs = real_liqs
        self._orig_build_liqs = real_liqs.build_liqs

    def tearDown(self):
        self.WU.subprocess.run = self._orig_run
        self._real_liqs.build_liqs = self._orig_build_liqs

    def _stub_run(self, stdout):
        class FakeCompleted:
            pass
        FakeCompleted.stdout = stdout
        self.WU.subprocess.run = lambda *a, **k: FakeCompleted()

    def test_wash_verdict_gains_liq_corroborator(self):
        self._stub_run(json.dumps({"ticker": "BSB", "verdict": "WASH", "oi_change_pct": 18.0}))
        self._real_liqs.build_liqs = lambda ticker, oi_delta_pct=None, **k: {
            "oi_liq_consistency": {"verdict": "SUSPECT_FAKE", "reason": "x"}}
        out = self.WU.oi_sides_read("BSB")
        self.assertEqual(out["liq_corroborator"]["verdict"], "SUSPECT_FAKE")

    def test_non_wash_verdict_has_no_corroborator(self):
        self._stub_run(json.dumps({"ticker": "BSB", "verdict": "REAL_DIRECTIONAL", "oi_change_pct": 18.0}))
        out = self.WU.oi_sides_read("BSB")
        self.assertNotIn("liq_corroborator", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
