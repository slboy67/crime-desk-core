#!/usr/bin/env python3
"""SPEC-118 — deposit-breadth metric: many-wallets → CEX is a pattern-level signal the
desk's subject-level (tracked-wallet) layer can't compute. VELVET is the desk-native
proof: distribution rotated through fresh wallets NOT in the tracked set, the tracked
wallet's clock froze, and the engine was blind. This is the cheap aggregate that
catches many small untracked sellers at once — deliberately NOT gated on tracked_wallets.

Offline-deterministic throughout: classify_breadth/decompose_senders are pure;
build_breadth is exercised via injected transfers_fn/nonce_fn stubs.
"""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import deposit_breadth as DB

CEX_ADDR = "0xacd03d601e5bb1b275bb94076ff46ed9d753435a"   # Binance 1 (known_entities.json)
CONTRACT = "0xtoken00000000000000000000000000000000"
CHAIN = "binance-smart-chain"
TRACKED_ADDR = "0xaaaa000000000000000000000000000000aaaa"


def _tx(from_addr, to_addr, value, h, ts):
    return {"from_address": from_addr, "to_address": to_addr, "value_decimal": value,
            "transaction_hash": h, "block_timestamp": ts}


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


NOW = 1_784_000_000.0


class ClassifyBreadthTests(unittest.TestCase):
    def test_breadth_spike_fires_with_numbers(self):
        current = {"senders": 23, "usd": 410_000.0, "median_usd": 18_000.0}
        r = DB.classify_breadth(current, baseline_counts=[6])
        self.assertTrue(r["spike"])
        self.assertEqual(r["senders"], 23)
        self.assertEqual(r["baseline"], 6)
        self.assertEqual(r["usd"], 410_000.0)
        self.assertEqual(r["median_usd"], 18_000.0)

    def test_two_whale_senders_no_spike(self):
        # same USD, but only 2 distinct senders — that's the subject-level layer's job.
        current = {"senders": 2, "usd": 410_000.0, "median_usd": 205_000.0}
        r = DB.classify_breadth(current, baseline_counts=[1])
        self.assertFalse(r["spike"])
        self.assertEqual(r["senders"], 2)   # numbers still reported

    def test_small_sample_guard_baseline_near_zero(self):
        # baseline ~0 makes the ratio explode; the absolute-sender floor still gates it.
        current = {"senders": 5, "usd": 150_000.0, "median_usd": 30_000.0}
        r = DB.classify_breadth(current, baseline_counts=[0])
        self.assertFalse(r["spike"])
        self.assertEqual(r["senders"], 5)
        self.assertEqual(r["usd"], 150_000.0)

    def test_below_usd_floor_no_spike(self):
        current = {"senders": 20, "usd": 40_000.0, "median_usd": 2_000.0}
        r = DB.classify_breadth(current, baseline_counts=[3])
        self.assertFalse(r["spike"])

    def test_no_baseline_history_no_spike(self):
        current = {"senders": 23, "usd": 410_000.0, "median_usd": 18_000.0}
        r = DB.classify_breadth(current, baseline_counts=[])
        self.assertFalse(r["spike"])
        self.assertEqual(r["baseline"], 0)


class DecomposeSendersTests(unittest.TestCase):
    def test_tracked_fresh_unknown_split(self):
        addrs = [f"0x{i:040x}" for i in range(23)]
        tracked = set(addrs[:12])
        fresh = set(addrs[12:17])
        r = DB.decompose_senders(addrs, tracked, fresh)
        self.assertEqual(r, {"tracked": 12, "fresh": 5, "unknown": 6})


class FormatBreadthLineTests(unittest.TestCase):
    def test_spike_line_has_numbers_and_decomposition(self):
        r = DB.classify_breadth({"senders": 23, "usd": 410_000.0, "median_usd": 18_000.0},
                                baseline_counts=[6])
        r["decomposition"] = {"tracked": 12, "fresh": 5, "unknown": 6}
        line = DB.format_breadth_line(r)
        self.assertIn("BREADTH_SPIKE", line)
        self.assertIn("senders 23", line)
        self.assertIn("baseline 6", line)
        self.assertIn("$410K", line)
        self.assertIn("$18K", line)
        self.assertIn("12 tracked", line)
        self.assertIn("5 fresh", line)
        self.assertIn("6 unknown", line)

    def test_no_spike_line_still_reports_numbers(self):
        r = DB.classify_breadth({"senders": 5, "usd": 150_000.0, "median_usd": 30_000.0},
                                baseline_counts=[0])
        line = DB.format_breadth_line(r)
        self.assertNotIn("BREADTH_SPIKE", line)
        self.assertIn("senders 5", line)


class BuildBreadthTests(unittest.TestCase):
    def _stubs(self, txs_by_channel, nonce_by_addr):
        def transfers_fn(addr, contract, ck, days=None):
            return txs_by_channel.get(addr, []), "test", False

        def nonce_fn(addr):
            return nonce_by_addr.get(addr)

        return transfers_fn, nonce_fn

    def test_spike_with_tracked_fresh_unknown_decomposition(self):
        senders = [f"0x{i:040x}" for i in range(23)]
        txs = [_tx(s, CEX_ADDR, 1000, f"0xh{i}", _iso(NOW - 3600 * i)) for i, s in enumerate(senders)]
        transfers_fn, nonce_fn = self._stubs({CEX_ADDR: txs}, {senders[12]: 3, senders[13]: 4})
        with TemporaryDirectory() as td:
            out = DB.build_breadth("LABX", contract=CONTRACT, chain_key=CHAIN,
                                   channels=[{"address": CEX_ADDR, "label": "binance"}],
                                   tracked_addrs=set(senders[:12]), price=1.0,
                                   transfers_fn=transfers_fn, nonce_fn=nonce_fn,
                                   now=NOW, state_dir=td,
                                   cfg={**DB.DEFAULT_CFG, "usd_floor": 1000})
        self.assertTrue(out["available"])
        self.assertEqual(out["senders"], 23)
        self.assertIn("decomposition", out)
        self.assertEqual(out["decomposition"]["tracked"], 12)

    def test_provider_absent_unavailable(self):
        def boom(addr, contract, ck, days=None):
            raise RuntimeError("provider down")
        with TemporaryDirectory() as td:
            out = DB.build_breadth("LABX", contract=CONTRACT, chain_key=CHAIN,
                                   channels=[{"address": CEX_ADDR, "label": "binance"}],
                                   transfers_fn=boom, nonce_fn=lambda a: None,
                                   now=NOW, state_dir=td)
        self.assertFalse(out["available"])
        self.assertIn("reason", out)

    def test_quiet_token_zero_fields_no_crash(self):
        transfers_fn, nonce_fn = self._stubs({CEX_ADDR: []}, {})
        with TemporaryDirectory() as td:
            out = DB.build_breadth("LABX", contract=CONTRACT, chain_key=CHAIN,
                                   channels=[{"address": CEX_ADDR, "label": "binance"}],
                                   transfers_fn=transfers_fn, nonce_fn=nonce_fn,
                                   now=NOW, state_dir=td)
        self.assertTrue(out["available"])
        self.assertEqual(out["senders"], 0)
        self.assertFalse(out["spike"])
        self.assertEqual(out["decomposition"], {"tracked": 0, "fresh": 0, "unknown": 0})


class RotationFreshnessFifthLegTests(unittest.TestCase):
    """SPEC-118 point 4: a BREADTH_SPIKE while tracked top-holders read frozen is
    corroborating rotation evidence — wired as an additional FROZEN->ROTATED input."""

    def test_frozen_plus_breadth_spike_flips_rotated_via_default_leg(self):
        import rotation_freshness as RF

        fake_result = {"available": True, "spike": True, "senders": 23, "baseline": 6,
                       "usd": 410_000.0, "median_usd": 18_000.0,
                       "decomposition": {"tracked": 2, "fresh": 5, "unknown": 16}}
        orig_build = DB.build_breadth
        DB.build_breadth = lambda ticker, **kw: fake_result
        try:
            breadth_leg = RF._default_breadth_leg("LABX")
        finally:
            DB.build_breadth = orig_build

        self.assertTrue(breadth_leg["available"])
        self.assertTrue(breadth_leg["suspect"])

        r = RF.classify_freshness("2026-07-06T00:00:00.000Z",
                                  {"available": True, "dump_legs": 0}, {"available": False},
                                  breadth=breadth_leg, now=NOW, live_short_thesis=True)
        self.assertEqual(r["verdict"], "ROTATED")
        self.assertIn("BREADTH", r["line"])


if __name__ == "__main__":
    unittest.main()
