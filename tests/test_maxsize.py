#!/usr/bin/env python3
"""SPEC-87 — `maxsize` capability: pre-trade max-clean-size + slippage curve + venue routing.

Run:  python3 tests/test_maxsize.py

Walks the correct side of the live book (LONG entry / SHORT exit → asks; SHORT entry / LONG exit →
bids), reports per-venue max clean clip within --bps of mid + a slippage ladder, and recommends the
cheapest **self-custody/executable** venue to route to with a clip schedule when the target exceeds
the clean ceiling. Read-only intel — carries the calm-snapshot + calm-vs-flush + self-custody caveats.
Network mocked — offline-deterministic.
"""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


MS = _load("maxsize")


# A clean, hand-computable book. mid = 100, spread = 2 bps.
#   asks (ascending):  100.01×10 (=1000.1), 100.05×20 (=2001), 100.25×40 (=4010), 101.00×100 (=10100)
#   bids (descending):  99.99×10,            99.95×20,          99.75×40,           99.00×100
DEEP_ASKS = [(100.01, 10), (100.05, 20), (100.25, 40), (101.00, 100)]
DEEP_BIDS = [(99.99, 10), (99.95, 20), (99.75, 40), (99.00, 100)]
THIN_ASKS = [(100.01, 1)]                      # ~ $100 total
THIN_BIDS = [(99.99, 1)]


def _fetcher(bids, asks, err=None):
    return lambda sym: (bids, asks, err)


def _patch(monkey, **venues):
    """Set MS.BOOK_FETCHERS to the given {name: (bids, asks[, err])} and clear HL by default."""
    fetchers = {}
    for name, spec in venues.items():
        if name == "hyperliquid":
            continue
        fetchers[name] = _fetcher(*spec)
    monkey["BOOK_FETCHERS"] = fetchers
    MS.BOOK_FETCHERS = fetchers


class TestSideGeometry(unittest.TestCase):
    def setUp(self):
        self._bf = MS.BOOK_FETCHERS
        self._hl = MS._hl_book

    def tearDown(self):
        MS.BOOK_FETCHERS = self._bf
        MS._hl_book = self._hl

    def test_long_entry_walks_asks(self):
        MS.BOOK_FETCHERS = {"aster": _fetcher(DEEP_BIDS, DEEP_ASKS)}
        MS._hl_book = lambda t: (None, None)
        v = MS.build_maxsize("BEL", side="long", leg="entry", bps=25)["venues"]["aster"]
        self.assertEqual(v["walked_side"], "ask")
        self.assertEqual(v["action"], "buy")
        # ladder $1000 fills entirely at the 100.01 ask → ~1.0 bps realized
        rung = next(r for r in v["ladder"] if r["target_usd"] == 1000)
        self.assertAlmostEqual(rung["realized_bps"], 1.0, places=1)
        self.assertFalse(rung["exhausted"])
        # ladder $2000 walks into the 100.05 ask → ~3.0 bps realized
        rung2 = next(r for r in v["ladder"] if r["target_usd"] == 2000)
        self.assertAlmostEqual(rung2["realized_bps"], 3.0, places=1)
        # max clean at 25 bps: hand-walk → ~$7873 (VWAP pinned to 100.25)
        self.assertAlmostEqual(v["max_clean_usd"], 7873.0, delta=2.0)

    def test_short_entry_walks_bids(self):
        MS.BOOK_FETCHERS = {"aster": _fetcher(DEEP_BIDS, DEEP_ASKS)}
        MS._hl_book = lambda t: (None, None)
        v = MS.build_maxsize("BEL", side="short", leg="entry", bps=25)["venues"]["aster"]
        self.assertEqual(v["walked_side"], "bid")
        self.assertEqual(v["action"], "sell")
        # symmetric book → ~$7834 clean on the bid side
        self.assertAlmostEqual(v["max_clean_usd"], 7834.0, delta=3.0)

    def test_leg_exit_inverts_side(self):
        MS.BOOK_FETCHERS = {"aster": _fetcher(DEEP_BIDS, DEEP_ASKS)}
        MS._hl_book = lambda t: (None, None)
        # LONG exit = sell → bids ; SHORT exit = buy → asks
        long_exit = MS.build_maxsize("BEL", side="long", leg="exit")["venues"]["aster"]
        short_exit = MS.build_maxsize("BEL", side="short", leg="exit")["venues"]["aster"]
        self.assertEqual(long_exit["walked_side"], "bid")
        self.assertEqual(short_exit["walked_side"], "ask")


class TestRouting(unittest.TestCase):
    def setUp(self):
        self._bf = MS.BOOK_FETCHERS
        self._hl = MS._hl_book

    def tearDown(self):
        MS.BOOK_FETCHERS = self._bf
        MS._hl_book = self._hl

    def test_routes_to_deeper_executable_venue(self):
        # thin aster vs deep hyperliquid (both self-custody) → route to the deeper one
        MS.BOOK_FETCHERS = {"aster": _fetcher(THIN_BIDS, THIN_ASKS)}
        MS._hl_book = lambda t: (DEEP_BIDS, DEEP_ASKS)
        d = MS.build_maxsize("BEL", side="long", leg="entry", bps=25, target=5000)
        rec = d["recommendation"]
        self.assertEqual(rec["route_to"], "hyperliquid")
        self.assertTrue(rec["executable"])

    def test_clip_schedule_when_target_over_ceiling(self):
        MS.BOOK_FETCHERS = {"aster": _fetcher(THIN_BIDS, THIN_ASKS)}
        MS._hl_book = lambda t: (DEEP_BIDS, DEEP_ASKS)
        d = MS.build_maxsize("BEL", side="long", leg="entry", bps=25, target=20000)
        rec = d["recommendation"]
        self.assertEqual(rec["route_to"], "hyperliquid")
        clip = rec["clip_schedule"]
        self.assertIsNotNone(clip)
        self.assertGreaterEqual(clip["clips"], 2)
        self.assertLess(clip["clip_usd"], 20000)

    def test_no_clip_when_target_fits(self):
        MS.BOOK_FETCHERS = {"aster": _fetcher(DEEP_BIDS, DEEP_ASKS)}
        MS._hl_book = lambda t: (None, None)
        d = MS.build_maxsize("BEL", side="long", leg="entry", bps=25, target=2000)
        self.assertIsNone(d["recommendation"]["clip_schedule"])


class TestDegradeAndTruncation(unittest.TestCase):
    def setUp(self):
        self._bf = MS.BOOK_FETCHERS
        self._hl = MS._hl_book

    def tearDown(self):
        MS.BOOK_FETCHERS = self._bf
        MS._hl_book = self._hl

    def test_unavailable_venue_omitted_others_unaffected(self):
        MS.BOOK_FETCHERS = {"aster": _fetcher(None, None, "timeout"),
                            "bitget": _fetcher(DEEP_BIDS, DEEP_ASKS)}
        MS._hl_book = lambda t: (None, None)
        venues = MS.build_maxsize("BEL")["venues"]
        self.assertNotIn("aster", venues)        # omitted, not a null entry
        self.assertIn("bitget", venues)

    def test_fetcher_exception_does_not_crash(self):
        def boom(sym):
            raise RuntimeError("rpc down")
        MS.BOOK_FETCHERS = {"aster": boom, "bitget": _fetcher(DEEP_BIDS, DEEP_ASKS)}
        MS._hl_book = lambda t: (None, None)
        venues = MS.build_maxsize("BEL")["venues"]       # no exception
        self.assertNotIn("aster", venues)
        self.assertIn("bitget", venues)

    def test_truncated_book_no_false_capacity(self):
        # all levels bunch within ~0.2% of mid → book exhausts before the 25-bps band
        asks = [(100.01, 5), (100.05, 5), (100.10, 5), (100.18, 5)]   # ~ $2007 total
        bids = [(99.99, 5), (99.95, 5), (99.90, 5), (99.82, 5)]
        MS.BOOK_FETCHERS = {"aster": _fetcher(bids, asks)}
        MS._hl_book = lambda t: (None, None)
        v = MS.build_maxsize("BEL", side="long", leg="entry", bps=25, target=10000)["venues"]["aster"]
        self.assertTrue(v["truncated"])
        self.assertTrue(v["clean_ceiling_truncated"])    # ceiling is a floor, not a true max
        # the $10k rung cannot fill on this snapshot → exhausted, no false capacity
        rung = next(r for r in v["ladder"] if r["target_usd"] == 10000)
        self.assertTrue(rung["exhausted"])
        self.assertLess(rung["filled_usd"], 10000)


class TestCaveatsAndTags(unittest.TestCase):
    def setUp(self):
        self._bf = MS.BOOK_FETCHERS
        self._hl = MS._hl_book

    def tearDown(self):
        MS.BOOK_FETCHERS = self._bf
        MS._hl_book = self._hl

    def test_output_always_carries_caveats_and_tags(self):
        MS.BOOK_FETCHERS = {"aster": _fetcher(DEEP_BIDS, DEEP_ASKS),
                            "bitget": _fetcher(DEEP_BIDS, DEEP_ASKS)}
        MS._hl_book = lambda t: (None, None)
        d = MS.build_maxsize("BEL")
        self.assertIn("snapshot_caveat", d)
        self.assertIn("calm_vs_flush", d)
        self.assertIn("oracle_mark_note", d)
        # self-custody tags on every venue
        self.assertTrue(d["venues"]["aster"]["executable"])
        self.assertEqual(d["venues"]["aster"]["custody"], "self-custody")
        self.assertFalse(d["venues"]["bitget"]["executable"])
        self.assertEqual(d["venues"]["bitget"]["custody"], "cex-signal-only")

    def test_read_only_no_verdict_fields(self):
        MS.BOOK_FETCHERS = {"aster": _fetcher(DEEP_BIDS, DEEP_ASKS)}
        MS._hl_book = lambda t: (None, None)
        d = MS.build_maxsize("BEL")
        self.assertNotIn("verdict", d)
        self.assertNotIn("direction", d)
        self.assertIn("note", d)
        self.assertIn("read-only", d["note"])


class TestCapabilityRegistered(unittest.TestCase):
    def test_maxsize_in_capabilities_json(self):
        c = json.loads((ROOT / "capabilities.json").read_text())
        self.assertIn("maxsize", c)
        self.assertIn("maxsize.py", c["maxsize"]["invoke"])
        self.assertEqual(c["maxsize"]["status"], "native")


if __name__ == "__main__":
    unittest.main(verbosity=2)
