#!/usr/bin/env python3
"""SPEC 16 — onchain_radar: distributors + accumulators + cost-basis sellers + perp liqs.

Run:  python3 tests/test_onchain_radar.py

Fully offline — the discovery, per-candidate verify, perp, HL positions, price, and
state are all monkeypatched. SPEC-123: still gated behind CRIMEDESK_LIVE_TESTS=1 — the
progress emitter (_write_progress) prints unconditional "radar-progress onchain_radar: ..."
stderr lines that read as a live sweep to anyone watching premerge output (the
2026-07-14 false-alarm that prompted this spec), even though this file makes zero
network calls.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("onchain_radar", ROOT / "capabilities" / "onchain_radar.py")
OR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OR)

sys.path.insert(0, str(ROOT / "tests"))
from live_gate import LIVE, SKIP_REASON  # noqa: E402

SEEDED = "0xseeded0000000000000000000000000000000001"
SELLER = "0xseller0000000000000000000000000000000002"
BUYER = "0xbuyer00000000000000000000000000000000003"


def fake_verify(addr, token, days=180, price=None):
    base = {"available": True, "address": addr, "net_flow_window_usd": 100000.0,
            "seeded_staging": False, "seeded_sources": [], "distributing": False,
            "accumulating": False, "recent_net": 0, "held_days": 5, "holder_type": "holder",
            "last_out_ts": "2026-06-03T10:00:00.000Z", "first_seen_ts": "2026-05-01T00:00:00.000Z"}
    if addr == SEEDED:
        base.update(seeded_staging=True, seeded_sources=[{"label": "FRESH-STAGED-1 (distribution)"}],
                    distributing=True, holder_type="operator-seeded")
    elif addr == SELLER:
        base.update(distributing=True, held_days=30, holder_type="early-winner")
    elif addr == BUYER:
        base.update(accumulating=True, recent_net=500000.0, holder_type="accumulator")
    return base


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestOnchainRadar(unittest.TestCase):
    def setUp(self):
        self._orig = (OR._cat_a_tokens, OR._seeded_recipients, OR._concentration, OR.build_verify,
                      OR.live_perp, OR._live_price, OR._hl_positions, OR._load, OR._save, OR.nonce_of)
        OR.nonce_of = lambda addr, chain_key="binance-smart-chain": 1
        OR._cat_a_tokens = lambda: ["ESPORTS"]
        OR._seeded_recipients = lambda contract, chain, safes, days=4: ({SEEDED}, 0)
        OR._concentration = lambda tk: {"available": True, "holders": [
            {"address": SELLER, "percent": 4.0, "is_contract": False, "is_locked": False},
            {"address": BUYER, "percent": 3.0, "is_contract": False, "is_locked": False}]}
        OR.build_verify = fake_verify
        OR.live_perp = lambda tk: {"funding_4h": 0.02, "funding_split": False}
        OR._live_price = lambda tk: (0.05, "bybit")
        OR._hl_positions = lambda addr: [{"coin": "DOGE", "side": "LONG", "size": 2e6,
                                          "entry": "0.10", "liq": "0.045", "value_usd": "2000000", "roe": "0.1"}]
        self._d, self._a = {}, {}
        OR._load = lambda name: {}
        OR._save = lambda name, obj: None

    def tearDown(self):
        (OR._cat_a_tokens, OR._seeded_recipients, OR._concentration, OR.build_verify,
         OR.live_perp, OR._live_price, OR._hl_positions, OR._load, OR._save, OR.nonce_of) = self._orig

    def test_all_sections_present(self):
        t = OR.build_onchain_radar("ESPORTS")["tokens"]["ESPORTS"]
        self.assertTrue(t["available"])
        addrs = {c["address"]: c for c in t["distributors"]}
        self.assertEqual(addrs[SEEDED]["confidence"], "HIGH")
        self.assertEqual(addrs[SELLER]["confidence"], "MED")
        self.assertEqual([a["address"] for a in t["accumulators"]], [BUYER])
        self.assertEqual(len(t["independent_sellers"]), 1)               # SELLER, not the seeded one
        self.assertEqual(t["independent_sellers"][0]["holder_type"], "early-winner")  # cost-basis proxy

    def test_perp_liqs_near_flag(self):
        r = OR.build_onchain_radar("ESPORTS")
        pl = r["perp_liqs"]
        self.assertTrue(pl["available"])
        self.assertGreater(len(pl["positions"]), 0)
        p = pl["positions"][0]
        self.assertEqual(p["coin"], "DOGE")
        self.assertTrue(p["near_liq"])                  # price 0.05 vs liq 0.045 = 10% ≤ 20%
        self.assertAlmostEqual(p["liq_distance_pct"], 10.0, places=1)

    def test_change_detection_no_realert(self):
        # persist known via an in-memory store
        store = {}
        OR._load = lambda name: dict(store.get(name, {}))
        OR._save = lambda name, obj: store.__setitem__(name, dict(obj))
        r1 = OR.build_onchain_radar("ESPORTS")
        self.assertGreater(r1["alerts_total"], 0)       # first = all NEW
        r2 = OR.build_onchain_radar("ESPORTS")
        self.assertEqual(r2["alerts_total"], 0)         # no new chain activity → no re-alert


if __name__ == "__main__":
    unittest.main(verbosity=2)
