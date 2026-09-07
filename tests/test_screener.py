#!/usr/bin/env python3
"""screener — BNB-chain structural-fingerprint discovery net (Phase 2).

Run:  python3 tests/test_screener.py

Coingecko's category endpoint is free-tier rate-limited, so `scanned` may be 0 on
a 429. The suite asserts the envelope/shape always, the perp_universe (from the
reliable Binance/Bybit exchange-info), and the candidate contract only when coins
were actually scanned.
SPEC-123: entirely live (CoinGecko + Binance/Bybit) — gated behind CRIMEDESK_LIVE_TESTS=1,
skipped by default.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
SCR = ROOT / "capabilities" / "screener.py"
sys.path.insert(0, str(ROOT / "tests"))
from live_gate import LIVE, SKIP_REASON  # noqa: E402
sys.path.insert(0, str(ROOT / "capabilities"))
import screener as screener_mod  # noqa: E402
CAND_KEYS = {"ticker", "score", "why", "venues", "mc", "fdv_mc", "circ_ratio",
             "vol", "ch7", "ch30", "on_watchlist"}


def run(*flags):
    return subprocess.run([sys.executable, str(SCR), *flags],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=90)


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestScreener(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = json.loads(run("--pages", "1", "--json").stdout)

    def test_shape(self):
        s = self.s
        self.assertEqual({"mode", "scanned", "perp_universe", "min_score",
                          "baseline_age_h", "candidates", "status"} | ({"reason"} if s.get("reason") else set()),
                         set(s))
        self.assertEqual(s["mode"], "pump")
        self.assertIsInstance(s["candidates"], list)
        self.assertIn(s["status"], ("OK", "NOTOK"))

    def test_perp_universe_populated(self):
        # Binance/Bybit exchange-info are reliable even when Coingecko 429s.
        pu = self.s["perp_universe"]
        self.assertGreater(pu["binance"] + pu["bybit"], 50)

    def test_candidate_contract_when_scanned(self):
        if self.s["scanned"] == 0:
            self.skipTest("Coingecko rate-limited (scanned=0)")
        for m in self.s["candidates"]:
            self.assertTrue(CAND_KEYS.issubset(m), m)
            self.assertGreaterEqual(m["score"], self.s["min_score"])
            self.assertIsInstance(m["why"], list)

    def test_accumulation_mode(self):
        s = json.loads(run("--mode", "accumulation", "--pages", "1", "--json").stdout)
        self.assertEqual(s["mode"], "accumulation")

    def test_human_non_json(self):
        out = run("--pages", "1", "--no-color").stdout
        self.assertIn("Screener", out)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)


def _coin(sym="fake", mc=50_000_000, vol=20_000_000, id_="fake-coin"):
    return {"symbol": sym, "id": id_, "market_cap": mc, "total_volume": vol,
            "circulating_supply": 100, "total_supply": 1000,
            "fully_diluted_valuation": 5_000_000,
            "price_change_percentage_7d_in_currency": 10.0,
            "price_change_percentage_30d_in_currency": 20.0}


class TestScreenerNotokEnvelope(unittest.TestCase):
    """SPEC-137 — a missing baseline or a scanned==0 fetch failure must be a LOUD
    NOTOK, never indistinguishable from a real "swept the universe, found nothing".
    Offline-deterministic: the data layer (perp lists / markets / vol baseline /
    watchlist) is monkeypatched, no network."""

    def setUp(self):
        self._orig = (screener_mod.binance_perps, screener_mod.bybit_perps,
                      screener_mod.markets, screener_mod.load_vol_baseline,
                      screener_mod.save_vol_baseline, screener_mod._watchlist)
        screener_mod.binance_perps = lambda: {"FAKE"}
        screener_mod.bybit_perps = lambda: set()
        screener_mod.save_vol_baseline = lambda coins: None
        screener_mod._watchlist = lambda: set()

    def tearDown(self):
        (screener_mod.binance_perps, screener_mod.bybit_perps,
         screener_mod.markets, screener_mod.load_vol_baseline,
         screener_mod.save_vol_baseline, screener_mod._watchlist) = self._orig

    def test_baseline_absent_is_loud_notok(self):
        screener_mod.markets = lambda pages: ([_coin()], [], None, None)
        screener_mod.load_vol_baseline = lambda: ({}, None, "baseline_missing")
        s = screener_mod.build_screen(mode="pump", min_score=0, pages=1, top=25)
        self.assertEqual(s["status"], "NOTOK")
        self.assertEqual(s["reason"], "baseline_missing")
        self.assertIsNone(s["baseline_age_h"])
        # the failure marker must be visible WITHOUT relying on candidates alone
        self.assertIn("status", s)
        self.assertNotEqual(s["status"], "OK")

    def test_baseline_stale_is_loud_notok(self):
        screener_mod.markets = lambda pages: ([_coin()], [], None, None)
        screener_mod.load_vol_baseline = lambda: ({}, None, "baseline_stale_120h")
        s = screener_mod.build_screen(mode="pump", min_score=0, pages=1, top=25)
        self.assertEqual(s["status"], "NOTOK")
        self.assertEqual(s["reason"], "baseline_stale_120h")

    def test_scanned_zero_is_fetch_failed_notok_even_with_good_baseline(self):
        screener_mod.markets = lambda pages: ([], [], None, None)
        screener_mod.load_vol_baseline = lambda: ({"fake-coin": 1e6}, 10.0, None)
        s = screener_mod.build_screen(mode="pump", min_score=0, pages=1, top=25)
        self.assertEqual(s["scanned"], 0)
        self.assertEqual(s["status"], "NOTOK")
        self.assertEqual(s["reason"], "fetch_failed")
        self.assertEqual(s["candidates"], [])

    def test_baseline_present_scanned_nonzero_zero_matches_is_clean_none(self):
        # min_score impossibly high → zero matches, but this must NOT read as a failure
        screener_mod.markets = lambda pages: ([_coin()], [], None, None)
        screener_mod.load_vol_baseline = lambda: ({"fake-coin": 1e6}, 10.0, None)
        s = screener_mod.build_screen(mode="pump", min_score=99, pages=1, top=25)
        self.assertEqual(s["status"], "OK")
        self.assertNotIn("reason", s)
        self.assertGreater(s["scanned"], 0)
        self.assertEqual(s["candidates"], [])
        self.assertIsNotNone(s["baseline_age_h"])
        self.assertNotIn("fetch_errors", s)

    def test_scanned_zero_surfaces_fetch_errors_with_url_and_status(self):
        # SPEC-160 #3: scanned==0 must never be a bare "fetch_failed" label — the failing
        # URL+status rides in the output so a dead/changed/rate-limited endpoint is
        # diagnosable without re-deriving it from stderr.
        screener_mod.markets = lambda pages: (
            [], [{"url": "https://api.coingecko.com/api/v3/coins/markets?page=1", "status": 429}],
            "coingecko_429", None)
        screener_mod.load_vol_baseline = lambda: ({"fake-coin": 1e6}, 10.0, None)
        s = screener_mod.build_screen(mode="pump", min_score=0, pages=1, top=25)
        self.assertEqual(s["status"], "NOTOK")
        self.assertEqual(s["reason"], "fetch_failed")
        self.assertIn("fetch_errors", s)
        self.assertEqual(len(s["fetch_errors"]), 1)
        self.assertEqual(s["fetch_errors"][0]["status"], 429)
        self.assertIn("coins/markets", s["fetch_errors"][0]["url"])


class TestScreenerMarketsCapturesFetchErrors(unittest.TestCase):
    """SPEC-160 #3 — markets() itself: an HTTPError records {url, status:<int code>}; any
    other exception records {url, status:<str(e)>}. Offline via a monkeypatched get().
    SPEC-187: markets() now returns a 4-tuple (rows, errors, degraded, cache_age_h); these
    tests run with no on-disk cache present (a fresh tmp CACHE path) so a failure never
    gets silently backfilled — `degraded` is asserted explicitly per case."""

    def setUp(self):
        self._orig_get = screener_mod.get
        self._orig_cache = screener_mod.MARKETS_CACHE
        self._tmpdir = TemporaryDirectory()
        screener_mod.MARKETS_CACHE = Path(self._tmpdir.name) / "markets_cache.json"
        self._no_sleep = lambda s: None   # never actually wait in tests

    def tearDown(self):
        screener_mod.get = self._orig_get
        screener_mod.MARKETS_CACHE = self._orig_cache
        self._tmpdir.cleanup()

    def test_http_error_records_url_and_int_status(self):
        import urllib.error

        def fake_get(url, timeout=20):
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        screener_mod.get = fake_get
        rows, errors, degraded, cache_age_h = screener_mod.markets(1, sleep_fn=self._no_sleep)
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["status"], 429)
        self.assertIn("coins/markets", errors[0]["url"])
        self.assertEqual(degraded, "coingecko_429")   # no cache available -> still flagged
        self.assertIsNone(cache_age_h)

    def test_non_http_exception_records_url_and_str_status(self):
        def fake_get(url, timeout=20):
            raise TimeoutError("timed out")
        screener_mod.get = fake_get
        rows, errors, degraded, cache_age_h = screener_mod.markets(1, sleep_fn=self._no_sleep)
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("timed out", errors[0]["status"])
        self.assertEqual(degraded, "coingecko_error")
        self.assertIsNone(cache_age_h)

    def test_success_yields_rows_and_no_errors(self):
        screener_mod.get = lambda url, timeout=20: [_coin()]
        rows, errors, degraded, cache_age_h = screener_mod.markets(1, sleep_fn=self._no_sleep)
        self.assertEqual(len(rows), 1)
        self.assertEqual(errors, [])
        self.assertIsNone(degraded)
        self.assertIsNone(cache_age_h)

    def test_multi_page_partial_failure_still_returns_successful_rows(self):
        calls = {"n": 0}

        def fake_get(url, timeout=20):
            calls["n"] += 1
            if calls["n"] == 1:
                return [_coin()]
            import urllib.error
            raise urllib.error.HTTPError(url, 500, "Internal Server Error", None, None)
        screener_mod.get = fake_get
        rows, errors, degraded, cache_age_h = screener_mod.markets(2, sleep_fn=self._no_sleep)
        # no cache present -> the page-1 success is NOT overwritten by an (absent) cache
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["status"], 500)
        self.assertEqual(degraded, "coingecko_error")
        self.assertIsNone(cache_age_h)


class TestScreenerMarketsBackoffAndCache(unittest.TestCase):
    """SPEC-174 #5 — exponential backoff on 429 (retries within the page before it counts
    as a failure). SPEC-187 — the on-disk cache fallback is used at ANY age (not gated to
    a 15-min TTL — a rate limit shouldn't zero the layer), loudly flagged as
    `screener_degraded` + age-stamped via `markets_cache_age_h` in build_screen's output."""

    def setUp(self):
        self._orig_get = screener_mod.get
        self._orig_cache = screener_mod.MARKETS_CACHE
        self._tmpdir = TemporaryDirectory()
        screener_mod.MARKETS_CACHE = Path(self._tmpdir.name) / "markets_cache.json"
        self.sleeps = []

    def tearDown(self):
        screener_mod.get = self._orig_get
        screener_mod.MARKETS_CACHE = self._orig_cache
        self._tmpdir.cleanup()

    def test_429_retries_with_exponential_backoff_then_succeeds(self):
        import urllib.error
        calls = {"n": 0}

        def fake_get(url, timeout=20):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
            return [_coin()]
        screener_mod.get = fake_get
        rows, errors, degraded, cache_age_h = screener_mod.markets(1, sleep_fn=self.sleeps.append)
        self.assertEqual(len(rows), 1)
        self.assertEqual(errors, [])
        self.assertIsNone(degraded)
        self.assertIsNone(cache_age_h)
        self.assertEqual(calls["n"], 3)
        # exponential: each retry delay strictly doubles the previous one
        self.assertEqual(len(self.sleeps), 2)
        self.assertAlmostEqual(self.sleeps[1], self.sleeps[0] * 2, places=6)

    def test_429_exhausting_retries_is_one_error_not_one_per_attempt(self):
        import urllib.error

        def fake_get(url, timeout=20):
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        screener_mod.get = fake_get
        rows, errors, degraded, cache_age_h = screener_mod.markets(1, sleep_fn=lambda s: None)
        self.assertEqual(len(errors), 1)
        self.assertEqual(degraded, "coingecko_429")
        self.assertIsNone(cache_age_h)

    def test_non_429_error_never_retried(self):
        import urllib.error
        calls = {"n": 0}

        def fake_get(url, timeout=20):
            calls["n"] += 1
            raise urllib.error.HTTPError(url, 500, "Internal Server Error", None, None)
        screener_mod.get = fake_get
        screener_mod.markets(1, sleep_fn=lambda s: None)
        self.assertEqual(calls["n"], 1)   # no backoff on a non-429 status

    def test_successful_fetch_writes_the_cache(self):
        screener_mod.get = lambda url, timeout=20: [_coin()]
        screener_mod.markets(1, now=1_000_000.0, sleep_fn=lambda s: None)
        cached = json.loads(screener_mod.MARKETS_CACHE.read_text())
        self.assertEqual(cached["ts"], 1_000_000.0)
        self.assertEqual(len(cached["rows"]), 1)

    def test_fresh_cache_backfills_on_a_429_after_retries_exhausted(self):
        import urllib.error
        screener_mod.MARKETS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        screener_mod.MARKETS_CACHE.write_text(json.dumps(
            {"ts": 1_000_000.0, "rows": [_coin(), _coin()]}))

        def fake_get(url, timeout=20):
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        screener_mod.get = fake_get
        rows, errors, degraded, cache_age_h = screener_mod.markets(
            1, now=1_000_000.0 + 300, sleep_fn=lambda s: None)   # 5 min old -> fresh
        self.assertEqual(len(rows), 2)
        self.assertEqual(degraded, "coingecko_429")
        self.assertAlmostEqual(cache_age_h, 300 / 3600, places=2)   # rounded to 2dp

    def test_stale_cache_still_backfills_with_age_stamp_and_suffix(self):
        """SPEC-187: was `test_stale_cache_is_not_used` — a cache older than
        MARKETS_CACHE_TTL_S (15min) used to be discarded entirely, zeroing `rows` on any
        CoinGecko outage that outlasted 15 minutes. Now it still backfills — age-stamped
        via `cache_age_h`, `degraded` suffixed `_stale_cache` so the staleness is never
        hidden — rather than dropping the sweep to zero rows."""
        import urllib.error
        screener_mod.MARKETS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        screener_mod.MARKETS_CACHE.write_text(json.dumps(
            {"ts": 1_000_000.0, "rows": [_coin(), _coin()]}))

        def fake_get(url, timeout=20):
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        screener_mod.get = fake_get
        rows, errors, degraded, cache_age_h = screener_mod.markets(
            1, now=1_000_000.0 + 3600, sleep_fn=lambda s: None)   # 1h old -> stale
        self.assertEqual(len(rows), 2)   # still backfilled, never zeroed
        self.assertEqual(degraded, "coingecko_429_stale_cache")
        self.assertAlmostEqual(cache_age_h, 1.0, places=4)

    def test_no_cache_at_all_still_zeroes_rows_and_stays_undecorated(self):
        """The one case that still legitimately zeroes `rows`: no cache file exists at
        all — there is genuinely nothing to fall back to (§3, no data really is no data)."""
        import urllib.error

        def fake_get(url, timeout=20):
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        screener_mod.get = fake_get
        rows, errors, degraded, cache_age_h = screener_mod.markets(
            1, now=1_000_000.0, sleep_fn=lambda s: None)
        self.assertEqual(rows, [])
        self.assertEqual(degraded, "coingecko_429")
        self.assertIsNone(cache_age_h)


class TestBuildScreenSurfacesDegraded(unittest.TestCase):
    """SPEC-174 #5 — build_screen surfaces `screener_degraded` when markets() fell back
    to cache/empty on a rate limit, never a silent-looking sweep."""

    def setUp(self):
        self._orig_markets = screener_mod.markets
        self._orig_binance = screener_mod.binance_perps
        self._orig_bybit = screener_mod.bybit_perps
        self._orig_baseline = screener_mod.load_vol_baseline
        self._orig_save = screener_mod.save_vol_baseline
        self._orig_watchlist = screener_mod._watchlist
        screener_mod.binance_perps = lambda: set()
        screener_mod.bybit_perps = lambda: set()
        screener_mod.load_vol_baseline = lambda: ({}, None, "baseline_missing")
        screener_mod.save_vol_baseline = lambda coins: None
        screener_mod._watchlist = lambda: set()

    def tearDown(self):
        screener_mod.markets = self._orig_markets
        screener_mod.binance_perps = self._orig_binance
        screener_mod.bybit_perps = self._orig_bybit
        screener_mod.load_vol_baseline = self._orig_baseline
        screener_mod.save_vol_baseline = self._orig_save
        screener_mod._watchlist = self._orig_watchlist

    def test_degraded_flag_surfaced_when_cache_backfilled(self):
        screener_mod.markets = lambda pages: ([_coin()], [{"url": "x", "status": 429}],
                                              "coingecko_429", 0.08)
        out = screener_mod.build_screen(pages=1)
        self.assertEqual(out["screener_degraded"], "coingecko_429")
        self.assertEqual(out["markets_cache_age_h"], 0.08)
        self.assertEqual(out["scanned"], 1)   # cache backfilled -> not a fetch_failed NOTOK

    def test_no_degraded_key_on_a_clean_sweep(self):
        screener_mod.markets = lambda pages: ([_coin()], [], None, None)
        out = screener_mod.build_screen(pages=1)
        self.assertNotIn("screener_degraded", out)
        self.assertNotIn("markets_cache_age_h", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
