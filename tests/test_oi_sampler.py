#!/usr/bin/env python3
"""SPEC-178 — ops/oi_sampler.py: desk-run OI sampler (ops job + JSONL store).

Run:  python3 -m unittest tests.test_oi_sampler -v

Offline-deterministic: `venue_map_fn` is injected on every tick call (no network);
store/config paths point at a tmp directory per test.
"""
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("oi_sampler", ROOT / "ops" / "oi_sampler.py")
OS_MOD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OS_MOD)


def _venue_ok(oi_raw, oi_usd, mark_price=1.0, funding_pi_4h=0.01, vol24h_usd=1_000_000):
    return {"status": "ok", "oi_raw": oi_raw, "oi_usd": oi_usd, "mark_price": mark_price,
           "funding_pi_4h": funding_pi_4h, "vol24h_usd": vol24h_usd}


def _fake_vm(venues_by_symbol):
    """venue_map_fn stub: {ticker: {venue: block}} -> build_venue_map-shaped result."""
    def fn(ticker):
        return {"ticker": ticker, "venues": venues_by_symbol.get(ticker.upper(), {})}
    return fn


class _TmpBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store_dir = self.tmp / "oi_samples"

    def tearDown(self):
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# DoD — two consecutive ticks on a 3-symbol fixture universe produce per-symbol
# JSONL with one row per venue per tick; a forced-timeout venue yields status:error.
# ---------------------------------------------------------------------------
class TestSampleOneAndTicks(_TmpBase):
    def test_two_ticks_produce_one_row_per_venue_per_tick(self):
        venues = {
            "A": {"binance": _venue_ok(100, 1000), "bybit": _venue_ok(90, 900)},
        }
        vm_fn = _fake_vm(venues)
        OS_MOD.sample_one("A", ts=1000, venue_map_fn=vm_fn, store_dir=self.store_dir)
        OS_MOD.sample_one("A", ts=1900, venue_map_fn=vm_fn, store_dir=self.store_dir)
        rows = OS_MOD._read_rows("A", store_dir=self.store_dir)
        self.assertEqual(len(rows), 4)   # 2 venues x 2 ticks
        self.assertEqual(sum(1 for r in rows if r["venue"] == "binance"), 2)
        self.assertEqual(sum(1 for r in rows if r["venue"] == "bybit"), 2)

    def test_timed_out_venue_yields_status_error_row_never_a_zero_datum(self):
        venues = {"A": {"binance": _venue_ok(100, 1000),
                        "paradex": {"status": "error", "reason": "timeout >6s"}}}
        vm_fn = _fake_vm(venues)
        OS_MOD.sample_one("A", ts=1000, venue_map_fn=vm_fn, store_dir=self.store_dir)
        rows = OS_MOD._read_rows("A", store_dir=self.store_dir)
        err_rows = [r for r in rows if r["venue"] == "paradex"]
        self.assertEqual(len(err_rows), 1)
        self.assertEqual(err_rows[0]["status"], "error")
        self.assertNotIn("oi_usd", err_rows[0])
        self.assertEqual(err_rows[0]["reason"], "timeout >6s")

    def test_not_listed_venue_row_carries_no_fabricated_oi(self):
        venues = {"A": {"binance": _venue_ok(100, 1000), "vest": {"status": "not_listed"}}}
        OS_MOD.sample_one("A", ts=1000, venue_map_fn=_fake_vm(venues), store_dir=self.store_dir)
        rows = OS_MOD._read_rows("A", store_dir=self.store_dir)
        nl = next(r for r in rows if r["venue"] == "vest")
        self.assertEqual(nl["status"], "not_listed")
        self.assertNotIn("oi_usd", nl)

    def test_run_tick_across_three_symbol_universe(self):
        venues = {tk: {"binance": _venue_ok(100, 1000)} for tk in ("A", "B", "C")}
        vm_fn = _fake_vm(venues)
        r = OS_MOD.run_tick(venue_map_fn=vm_fn, wl_path=self.tmp / "nope.json",
                            pos_path=self.tmp / "nope2.json", fb_path=self.tmp / "nope3.json",
                            store_dir=self.store_dir, marker_path=self.tmp / ".prune",
                            ts=1000)
        # empty universe (no watchlist/positions/faded-bounce files) -> nothing sampled;
        # exercised for real universe coverage in the injected-universe test below
        self.assertEqual(r["universe"], [])

    def test_run_tick_samples_every_universe_member(self):
        universe_tickers = ["A", "B", "C"]
        wl = self.tmp / "watchlist.json"
        wl.write_text(json.dumps({"tokens": [
            {"ticker": tk, "thesis": {"status": "WATCH"}} for tk in universe_tickers]}))
        venues = {tk: {"binance": _venue_ok(100, 1000)} for tk in universe_tickers}
        r = OS_MOD.run_tick(venue_map_fn=_fake_vm(venues), wl_path=wl,
                            pos_path=self.tmp / "nope.json", fb_path=self.tmp / "nope2.json",
                            store_dir=self.store_dir, marker_path=self.tmp / ".prune", ts=1000)
        self.assertEqual(set(r["universe"]), set(universe_tickers))
        self.assertEqual(r["sampled"], 3)
        for tk in universe_tickers:
            self.assertEqual(len(OS_MOD._read_rows(tk, store_dir=self.store_dir)), 1)


# ---------------------------------------------------------------------------
# DoD — redenomination test: a 1000x multiplier step produces the marker row.
# ---------------------------------------------------------------------------
class TestRedenomination(_TmpBase):
    def test_1000x_step_in_oi_usd_oi_raw_ratio_produces_marker_row(self):
        # tick 1: ratio (oi_usd/oi_raw) = 10.0/1 = 10
        OS_MOD.sample_one("A", ts=1000, venue_map_fn=_fake_vm({"A": {"binance": _venue_ok(1.0, 10.0)}}),
                          store_dir=self.store_dir)
        # tick 2: same raw units but the venue redenominated the contract 1000x -> USD/raw
        # ratio steps 1000x (e.g. contract multiplier changed, raw count unchanged but the
        # true USD value is now 1000x what a naive raw*old_price would say)
        OS_MOD.sample_one("A", ts=1900, venue_map_fn=_fake_vm({"A": {"binance": _venue_ok(1.0, 10000.0)}}),
                          store_dir=self.store_dir)
        rows = OS_MOD._read_rows("A", store_dir=self.store_dir)
        markers = [r for r in rows if r["status"] == "redenomination"]
        self.assertEqual(len(markers), 1)
        self.assertAlmostEqual(markers[0]["step"], 1000.0)

    def test_normal_organic_drift_produces_no_marker(self):
        OS_MOD.sample_one("A", ts=1000, venue_map_fn=_fake_vm({"A": {"binance": _venue_ok(100.0, 1000.0)}}),
                          store_dir=self.store_dir)
        OS_MOD.sample_one("A", ts=1900, venue_map_fn=_fake_vm({"A": {"binance": _venue_ok(105.0, 1055.0)}}),
                          store_dir=self.store_dir)
        rows = OS_MOD._read_rows("A", store_dir=self.store_dir)
        self.assertFalse(any(r["status"] == "redenomination" for r in rows))

    def test_first_ever_tick_never_fires_a_marker_nothing_to_compare(self):
        OS_MOD.sample_one("A", ts=1000, venue_map_fn=_fake_vm({"A": {"binance": _venue_ok(1.0, 10000.0)}}),
                          store_dir=self.store_dir)
        rows = OS_MOD._read_rows("A", store_dir=self.store_dir)
        self.assertFalse(any(r["status"] == "redenomination" for r in rows))


# ---------------------------------------------------------------------------
# DoD — prune test: rows older than 90d removed, newer intact, file still valid JSONL.
# ---------------------------------------------------------------------------
class TestPrune(_TmpBase):
    def test_prune_removes_old_rows_keeps_new_ones_valid_jsonl(self):
        now = time.time()
        old_ts = int(now - 100 * 86400)
        new_ts = int(now - 1 * 86400)
        OS_MOD.sample_one("A", ts=old_ts, venue_map_fn=_fake_vm({"A": {"binance": _venue_ok(1, 1)}}),
                          store_dir=self.store_dir)
        OS_MOD.sample_one("A", ts=new_ts, venue_map_fn=_fake_vm({"A": {"binance": _venue_ok(2, 2)}}),
                          store_dir=self.store_dir)
        result = OS_MOD.prune_old_rows(retention_days=90, now_ts=now, store_dir=self.store_dir)
        self.assertEqual(result["pruned_files"], 1)
        rows = OS_MOD._read_rows("A", store_dir=self.store_dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ts"], new_ts)
        # file still valid JSONL — every line parses
        p = OS_MOD._store_path("A", store_dir=self.store_dir)
        for line in p.read_text().splitlines():
            json.loads(line)

    def test_prune_is_noop_when_nothing_is_old(self):
        now = time.time()
        OS_MOD.sample_one("A", ts=int(now), venue_map_fn=_fake_vm({"A": {"binance": _venue_ok(1, 1)}}),
                          store_dir=self.store_dir)
        result = OS_MOD.prune_old_rows(retention_days=90, now_ts=now, store_dir=self.store_dir)
        self.assertEqual(result["pruned_files"], 0)
        self.assertEqual(len(OS_MOD._read_rows("A", store_dir=self.store_dir)), 1)

    def test_maybe_prune_marker_gates_weekly_cadence(self):
        marker = self.tmp / ".prune_marker"
        now = time.time()
        # no marker -> due
        r1 = OS_MOD.maybe_prune(marker_path=marker, now_ts=now, store_dir=self.store_dir)
        self.assertFalse(r1["skipped"])
        self.assertTrue(marker.exists())
        # marker just written -> not due
        r2 = OS_MOD.maybe_prune(marker_path=marker, now_ts=now, store_dir=self.store_dir)
        self.assertTrue(r2["skipped"])
        # marker older than the interval -> due again
        old = now - (OS_MOD.PRUNE_INTERVAL_DAYS + 1) * 86400
        import os
        os.utime(marker, (old, old))
        r3 = OS_MOD.maybe_prune(marker_path=marker, now_ts=now, store_dir=self.store_dir)
        self.assertFalse(r3["skipped"])


# ---------------------------------------------------------------------------
# DoD — universe test: cap enforcement order (live theses > positions > candidates).
# ---------------------------------------------------------------------------
class TestBuildUniverse(_TmpBase):
    def _write(self, name, obj):
        p = self.tmp / name
        p.write_text(json.dumps(obj))
        return p

    def test_union_of_three_tiers_dedupes(self):
        wl = self._write("wl.json", {"tokens": [
            {"ticker": "AAA", "thesis": {"status": "WATCH"}},
            {"ticker": "BBB", "thesis": {"status": "RETIRED"}},   # excluded (terminal)
            {"ticker": "CCC"},                                     # no thesis at all -> excluded
        ]})
        pos = self._write("pos.json", {"positions": [{"ticker": "AAA"}, {"ticker": "DDD"}]})
        fb = self._write("fb.json", {"data": {"candidates": [{"ticker": "EEE"}, {"ticker": "AAA"}]}})
        universe = OS_MOD.build_universe(wl_path=wl, pos_path=pos, fb_path=fb, cap=30)
        self.assertEqual(universe, ["AAA", "DDD", "EEE"])   # AAA counted once, priority order

    def test_cap_enforcement_prioritizes_live_theses_then_positions_then_candidates(self):
        wl = self._write("wl.json", {"tokens": [
            {"ticker": f"T{i}", "thesis": {"status": "WATCH"}} for i in range(5)]})
        pos = self._write("pos.json", {"positions": [{"ticker": f"P{i}"} for i in range(5)]})
        fb = self._write("fb.json", {"data": {"candidates": [{"ticker": f"C{i}"} for i in range(5)]}})
        universe = OS_MOD.build_universe(wl_path=wl, pos_path=pos, fb_path=fb, cap=7)
        self.assertEqual(len(universe), 7)
        self.assertEqual(universe[:5], [f"T{i}" for i in range(5)])
        self.assertEqual(universe[5:], [f"P{i}" for i in range(2)])   # only 2 of 5 positions fit
        self.assertFalse(any(u.startswith("C") for u in universe))    # candidates crowded out entirely

    def test_missing_files_degrade_to_empty_tiers_never_raise(self):
        universe = OS_MOD.build_universe(wl_path=self.tmp / "nope.json",
                                         pos_path=self.tmp / "nope2.json",
                                         fb_path=self.tmp / "nope3.json")
        self.assertEqual(universe, [])

    def test_closed_thesis_status_also_excluded(self):
        wl = self._write("wl.json", {"tokens": [
            {"ticker": "AAA", "thesis": {"status": "CLOSED"}},
            {"ticker": "BBB", "thesis": {"status": "ARMED"}}]})
        universe = OS_MOD.build_universe(wl_path=wl, pos_path=self.tmp / "nope.json",
                                         fb_path=self.tmp / "nope2.json")
        self.assertEqual(universe, ["BBB"])


# ---------------------------------------------------------------------------
# launchd plist / install_launchd.sh wiring
# ---------------------------------------------------------------------------
class TestLaunchdPlist(unittest.TestCase):
    def test_plist_file_exists_and_names_the_right_script_and_interval(self):
        p = ROOT / "ops" / "com.crimedesk.oi-sampler.plist"
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("ops/oi_sampler.py", text)
        self.assertIn("<integer>900</integer>", text)
        self.assertIn("com.crimedesk.oi-sampler", text)

    def test_plist_lints_clean(self):
        import shutil
        import subprocess
        if not shutil.which("plutil"):
            self.skipTest("plutil not available on this platform")
        p = ROOT / "ops" / "com.crimedesk.oi-sampler.plist"
        r = subprocess.run(["plutil", "-lint", str(p)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_install_launchd_glob_picks_up_the_new_plist(self):
        text = (ROOT / "ops" / "install_launchd.sh").read_text()
        self.assertIn("com.crimedesk.*.plist", text)   # the glob that auto-includes any new job


if __name__ == "__main__":
    unittest.main(verbosity=2)
