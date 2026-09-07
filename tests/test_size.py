#!/usr/bin/env python3
"""SPEC 41 — `size` capability: mechanical §7 sizing gate.

Run:  python3 tests/test_size.py

Pins: liquidity gate FIRST (AUTO-PASS short-circuits — no constraint math, no book
fetch; SCOUT-ONLY haircuts the final size 50%); direction-aware size-to-exit (the
desk convention, spec-literal: SHORT exits into BIDS, LONG exits into ASKS);
truncated-book 0.5 haircut; OI cap 3%; bracket cap 70% of tier-1 (config-driven,
config/brackets.json); leverage 1/stop-distance with 75% practical; DEX-mark-weight
flag; cluster heat from config/clusters.json + live watchlist theses; max_size_usd
only when equity is passed. ALL venue/depth fetches mocked — offline-deterministic.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SZ = _load("size")


# ── canned fixtures ─────────────────────────────────────────────────────────────
def _live(vol_m=80.0, oi=10_000_000, price=0.37, primary="binance"):
    return {"venue": primary, "primary_venue": primary, "funding_4h": 0.01,
            "price": price, "vol_m": vol_m, "oi": oi,
            "venues": {primary: {"oi": oi, "vol_m": vol_m}}}


def _book_fetcher(bids, asks):
    """A depth.VENUES-shaped fetcher: sym -> (bids desc, asks asc, err)."""
    def fn(sym):
        return list(bids), list(asks), None
    return fn


def _raise_fetcher(sym):
    raise AssertionError("book fetch must NOT happen on AUTO-PASS short-circuit")


# book around mid=1.00: bids hold $100k within the 2% band, asks hold $200k.
# A dust level beyond the band keeps the window UNtruncated (book reaches past the band).
BIDS_100K = [(0.999, 50050.05), (0.99, 50505.05), (0.97, 0.001)]   # ≈ $50k + $50k + dust
ASKS_200K = [(1.001, 99900.10), (1.01, 99009.90), (1.03, 0.001)]   # ≈ $100k + $100k + dust


def _approx(test, a, b, tol_pct=0.5):
    test.assertLess(abs(a - b) / b * 100, tol_pct, f"{a} !≈ {b}")


class Base(unittest.TestCase):
    def setUp(self):
        self._live_perp = SZ.live_perp
        self._fetchers = SZ.BOOK_FETCHERS
        self._cfg = SZ.CONFIG_DIR
        self.tmp = tempfile.TemporaryDirectory()
        SZ.CONFIG_DIR = Path(self.tmp.name)   # empty config dir → pure defaults
        SZ.live_perp = lambda tk: _live()
        SZ.BOOK_FETCHERS = {"binance": _book_fetcher(BIDS_100K, ASKS_200K),
                            "bitget": _book_fetcher([], [])}

    def tearDown(self):
        SZ.live_perp = self._live_perp
        SZ.BOOK_FETCHERS = self._fetchers
        SZ.CONFIG_DIR = self._cfg
        self.tmp.cleanup()

    def _cfg_write(self, name, obj):
        (Path(self.tmp.name) / name).write_text(json.dumps(obj))


# ── liquidity gate ──────────────────────────────────────────────────────────────
class TestLiquidityGate(Base):
    def test_low_vol_auto_pass_short_circuits(self):
        SZ.live_perp = lambda tk: _live(vol_m=5.0)
        SZ.BOOK_FETCHERS = {"binance": _raise_fetcher}   # any fetch = test failure
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        self.assertEqual(d["gate"], "AUTO-PASS")
        self.assertIsNone(d["constraints"])
        self.assertIsNone(d["binding"])
        self.assertNotIn("max_size_usd", d)

    def test_low_mc_auto_pass(self):
        SZ.live_perp = lambda tk: _live(vol_m=80.0)
        SZ.BOOK_FETCHERS = {"binance": _raise_fetcher}
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40, mc_usd=12_000_000)
        self.assertEqual(d["gate"], "AUTO-PASS")
        self.assertIsNone(d["constraints"])

    def test_scout_band_haircuts_final_size_50pct(self):
        SZ.live_perp = lambda tk: _live(vol_m=15.0)
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40, equity=1_000_000)
        self.assertEqual(d["gate"], "SCOUT-ONLY")
        # same constraints at FULL gate → scout halves the final size
        SZ.live_perp = lambda tk: _live(vol_m=80.0)
        full = SZ.build_size("XXX", "SHORT", 0.37, 0.40, equity=1_000_000)
        self.assertEqual(full["gate"], "FULL")
        _approx(self, d["max_size_usd"] * 2, full["max_size_usd"])

    def test_full_gate(self):
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        self.assertEqual(d["gate"], "FULL")


# ── direction-aware size-to-exit ───────────────────────────────────────────────
class TestExitSide(Base):
    def test_short_exits_into_bids(self):
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        _approx(self, d["constraints"]["exit_absorbable_usd"], 100_000)
        self.assertEqual(d["exit_side"], "bid")

    def test_long_exits_into_asks(self):
        d = SZ.build_size("XXX", "LONG", 0.40, 0.37)
        _approx(self, d["constraints"]["exit_absorbable_usd"], 200_000)
        self.assertEqual(d["exit_side"], "ask")

    def test_both_venues_summed(self):
        SZ.BOOK_FETCHERS = {"binance": _book_fetcher(BIDS_100K, ASKS_200K),
                            "bitget": _book_fetcher(BIDS_100K, ASKS_200K)}
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        _approx(self, d["constraints"]["exit_absorbable_usd"], 200_000)

    def test_levels_outside_band_excluded(self):
        far_bids = BIDS_100K + [(0.90, 1_000_000)]   # -10% — outside the 2% band
        SZ.BOOK_FETCHERS = {"binance": _book_fetcher(far_bids, ASKS_200K)}
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        _approx(self, d["constraints"]["exit_absorbable_usd"], 100_000)


# ── truncated-book haircut ──────────────────────────────────────────────────────
class TestTruncationHaircut(Base):
    def test_truncated_exit_side_haircut_05(self):
        # bid book ends at -0.5% (window ran out INSIDE the 2% band) → floor estimate ×0.5
        shallow_bids = [(0.999, 50050.05), (0.995, 50251.26)]   # ≈$100k, deepest -0.5%
        SZ.BOOK_FETCHERS = {"binance": _book_fetcher(shallow_bids, ASKS_200K)}
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        _approx(self, d["constraints"]["exit_absorbable_usd"], 50_000)
        ven = d["exit_detail"]["binance"]
        self.assertTrue(ven["truncated"])
        self.assertEqual(ven["haircut"], 0.5)
        self.assertTrue(any("truncated" in n.lower() for n in d["notes"]))

    def test_untruncated_book_no_haircut(self):
        # default fixture book reaches beyond the band → no truncation, full count
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        self.assertFalse(d["exit_detail"]["binance"]["truncated"])
        _approx(self, d["constraints"]["exit_absorbable_usd"], 100_000)


# ── each constraint binds in turn ──────────────────────────────────────────────
class TestBinding(Base):
    def test_exit_binds(self):
        # exit $100k < oi cap (3% of $3.7M=...) make OI huge, bracket default 0.7*50k=35k
        # bracket default would bind at 35k — raise bracket via config
        self._cfg_write("brackets.json", {"default_tier1_usd": 10_000_000, "venues": {}})
        SZ.live_perp = lambda tk: _live(oi=1_000_000_000, price=1.0)
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        self.assertEqual(d["binding"], "exit_absorbable")

    def test_oi_cap_binds(self):
        self._cfg_write("brackets.json", {"default_tier1_usd": 10_000_000, "venues": {}})
        SZ.live_perp = lambda tk: _live(oi=1_000_000, price=1.0)   # OI $1M → cap $30k
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        self.assertEqual(d["binding"], "oi_cap")
        _approx(self, d["constraints"]["oi_cap_usd"], 30_000)

    def test_bracket_cap_binds(self):
        self._cfg_write("brackets.json", {"default_tier1_usd": 50_000,
                                          "venues": {"binance": 20_000}})
        SZ.live_perp = lambda tk: _live(oi=1_000_000_000, price=1.0, primary="binance")
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        self.assertEqual(d["binding"], "bracket_cap")
        _approx(self, d["constraints"]["bracket_cap_usd"], 14_000)   # 70% of 20k

    def test_leverage_cap_binds_with_equity(self):
        self._cfg_write("brackets.json", {"default_tier1_usd": 10_000_000, "venues": {}})
        SZ.live_perp = lambda tk: _live(oi=1_000_000_000, price=1.0)
        # stop 8.108% → lev_max 12.33, practical 9.25 → equity 1000 → cap ≈ $9,250
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40, equity=1000)
        self.assertEqual(d["binding"], "leverage_cap")
        _approx(self, d["max_size_usd"], 9250, tol_pct=1.0)

    def test_bracket_ticker_override(self):
        self._cfg_write("brackets.json", {"default_tier1_usd": 50_000, "venues": {},
                                          "overrides": {"XXX": 200_000}})
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        _approx(self, d["constraints"]["bracket_cap_usd"], 140_000)


# ── leverage ────────────────────────────────────────────────────────────────────
class TestLeverage(Base):
    def test_max_and_practical(self):
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        _approx(self, d["constraints"]["leverage_max"], 100 / (0.03 / 0.37 * 100), 1.0)
        _approx(self, d["constraints"]["leverage_practical"],
                0.75 * d["constraints"]["leverage_max"], 0.1)

    def test_dex_mark_flag(self):
        self._cfg_write("dex_mark_weight.json", {"tokens": {"XXX": 0.45}})
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        self.assertTrue(d["haircut_dex_mark"])
        self.assertTrue(any("dex" in n.lower() for n in d["notes"]))

    def test_dex_mark_flag_off_below_30pct(self):
        self._cfg_write("dex_mark_weight.json", {"tokens": {"XXX": 0.10}})
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        self.assertFalse(d["haircut_dex_mark"])


# ── cluster heat ────────────────────────────────────────────────────────────────
class TestCluster(Base):
    def _cluster_cfg(self):
        self._cfg_write("clusters.json", {
            "max_operator_heat_pct": 6.0,
            "clusters": {"TEST-MM": {"members": ["XXX", "YYY", "ZZZ"]}}})

    def test_cluster_open_risk_warning(self):
        self._cluster_cfg()
        self._cfg_write("watchlist.json", {"tokens": [
            {"ticker": "YYY", "thesis": {"status": "ACTIVE", "direction": "SHORT"}},
            {"ticker": "ZZZ", "thesis": {"status": "RETIRED"}}]})
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        cl = d["constraints"]["cluster"]
        self.assertEqual(cl["name"], "TEST-MM")
        self.assertTrue(cl["cluster_open_risk_warning"])
        self.assertEqual([m["ticker"] for m in cl["open_members"]], ["YYY"])

    def test_no_warning_when_cluster_quiet(self):
        self._cluster_cfg()
        self._cfg_write("watchlist.json", {"tokens": [
            {"ticker": "YYY", "thesis": {"status": "RETIRED"}}]})
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        self.assertFalse(d["constraints"]["cluster"]["cluster_open_risk_warning"])

    def test_unclustered_token(self):
        self._cluster_cfg()
        d = SZ.build_size("AAA", "SHORT", 0.37, 0.40)
        self.assertIsNone(d["constraints"]["cluster"])


# ── equity / output shape ───────────────────────────────────────────────────────
class TestEquityAndShape(Base):
    def test_no_equity_no_max_size(self):
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40)
        self.assertNotIn("max_size_usd", d)
        self.assertIn("binding", d)            # binding still computed over $ caps

    def test_equity_max_size_is_min_of_constraints(self):
        self._cfg_write("brackets.json", {"default_tier1_usd": 10_000_000, "venues": {}})
        SZ.live_perp = lambda tk: _live(oi=1_000_000_000, price=1.0)
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40, equity=1_000_000)
        # exit $100k binds (lev cap = 1M*9.25 huge)
        self.assertEqual(d["binding"], "exit_absorbable")
        _approx(self, d["max_size_usd"], 100_000)

    def test_json_serializable(self):
        d = SZ.build_size("XXX", "SHORT", 0.37, 0.40, equity=5000)
        json.dumps(d)   # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
