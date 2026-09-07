#!/usr/bin/env python3
"""SPEC 11 — funding surfaced normalized to %/4h, veto/threshold compares on %/4h.

Run:  python3 tests/test_funding_normalized.py

Deterministic: monkeypatch regime_flip.live_perp so no network. Verifies a 1h
−0.17/int surfaces as ~−0.68%/4h, the −0.30 veto compares on %/4h (so it does NOT
read as "above the line"), and the JSON payload carries funding_4h + raw + interval.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

rf = importlib.util.spec_from_file_location("regime_flip", ROOT / "capabilities" / "regime_flip.py")
RF = importlib.util.module_from_spec(rf)
rf.loader.exec_module(RF)


class TestNormalization(unittest.TestCase):
    def test_to_4h(self):
        self.assertAlmostEqual(RF.to_4h(-0.17, 60), -0.68, places=2)   # 1h → ×4
        self.assertAlmostEqual(RF.to_4h(-0.30, 240), -0.30, places=2)  # 4h → ×1
        self.assertAlmostEqual(RF.to_4h(0.10, 480), 0.05, places=2)    # 8h → ×0.5
        self.assertIsNone(RF.to_4h(None, 60))
        self.assertIsNone(RF.to_4h(-0.1, None))

    def test_thresholds_are_4h(self):
        self.assertEqual(RF.DEEP_NEG, -0.30)
        self.assertEqual(RF.FLAT_BAND, 0.05)

    def test_1h_minus017_reads_deep_neg_not_above_veto(self):
        # a 1h −0.17/int = −0.68%/4h → deep-neg, must NOT look like it's above the −0.30 line
        live = {"price": 1.0, "chg24": 5.0, "funding_pi": -0.17, "interval_min": 60,
                "vol_m": 200.0, "funding_4h": RF.to_4h(-0.17, 60)}
        tok = {"ticker": "LAB", "state": "SHORT distribution top"}  # memo SHORT
        sev, tag, note = RF.classify(tok, live)
        self.assertEqual(tag, "REGIME_FLIP")                 # contra fired (deep-neg vs SHORT memo)
        self.assertIn("VETOED", note)
        self.assertIn("%/4h", note)
        self.assertIn("-0.68", note.replace("−", "-"))       # normalized value shown
        self.assertIn("raw -0.170", note)                    # raw secondary shown

    def test_short_veto_flag_on_4h(self):
        live = {"funding_pi": -0.17, "interval_min": 60, "funding_4h": RF.to_4h(-0.17, 60)}
        self.assertIn("SHORT vetoed", RF.short_veto_flag(live))
        # a 4h-interval −0.20/int = −0.20%/4h → above the −0.30 line → NOT vetoed
        ok = {"funding_pi": -0.20, "interval_min": 240, "funding_4h": RF.to_4h(-0.20, 240)}
        self.assertEqual(RF.short_veto_flag(ok), "")

    def test_json_payload_carries_4h_and_raw(self):
        """Ticker selection reads RF.WL_PATH — inject a temp watchlist carrying LAB rather
        than depending on the ambient config/watchlist.json still having a LAB row (it
        doesn't: LAB retired off the live board, which turned this into a silent
        IndexError on out[0] against an empty rows list — a test-isolation bug, not a
        regression in regime_flip.py itself)."""
        RF_live = {"venue": "bybit", "price": 26.0, "chg24": 80.0, "funding_pi": -0.17,
                   "interval_min": 60, "vol_m": 500.0, "oi": 1.0,
                   "funding_4h": RF.to_4h(-0.17, 60)}
        orig_live_perp = RF.live_perp
        orig_wl_path = RF.WL_PATH
        RF.live_perp = lambda t: RF_live
        with tempfile.TemporaryDirectory() as d:
            wl_path = Path(d) / "watchlist.json"
            wl_path.write_text(json.dumps({"tokens": [{"ticker": "LAB", "state": "WATCH"}]}))
            RF.WL_PATH = wl_path
            try:
                import io
                import sys as _s
                buf = io.StringIO()
                old = _s.argv, _s.stdout
                _s.argv = ["regime_flip.py", "LAB", "--json"]
                _s.stdout = buf
                try:
                    RF.main()
                finally:
                    _s.argv, _s.stdout = old
                out = json.loads(buf.getvalue())
                row = out if isinstance(out, dict) else out[0]
                self.assertAlmostEqual(row["funding_4h"], -0.68, places=2)
                self.assertAlmostEqual(row["funding_pi"], -0.17, places=2)
                self.assertEqual(row["interval_min"], 60)
            finally:
                RF.live_perp = orig_live_perp
                RF.WL_PATH = orig_wl_path


if __name__ == "__main__":
    unittest.main(verbosity=2)
