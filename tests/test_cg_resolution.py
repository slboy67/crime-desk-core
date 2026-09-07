#!/usr/bin/env python3
"""SPEC 36 — ticker→coingecko-id resolution must pick the LIVE token on symbol collisions.

Run:  python3 tests/test_cg_resolution.py

`coingecko_layer` took the FIRST symbol match (arbitrary coingecko order), so SIREN resolved
to the defunct 2021 `siren` (rank 6229, $0.003) instead of `siren-2` (rank 80, $1.08, the real
$783M perp-listed token) — silently corrupting identity/MC/ATH/contract on every collision
ticker. Fix: rank candidates by market_cap_rank and sanity-check the resolved price vs the live
perp price (>5x divergence → re-resolve / flag low_confidence). Network mocked.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


P5 = _load("pull5")

# the real coingecko /search collision: both list symbol SIREN; ranks differ by ~80x
SIREN_COINS = [
    {"id": "siren", "name": "SIREN", "symbol": "SIREN", "market_cap_rank": 6229},
    {"id": "siren-2", "name": "Siren", "symbol": "SIREN", "market_cap_rank": 80},
]


def _detail(cid, price, platforms):
    return {"name": cid, "symbol": "SIREN", "categories": [], "platforms": platforms,
            "market_data": {"current_price": {"usd": price}, "market_cap": {"usd": 1},
                            "fully_diluted_valuation": {"usd": 1}, "circulating_supply": 1,
                            "total_supply": 1, "ath": {"usd": 0}, "ath_date": {"usd": None},
                            "atl": {"usd": 0}, "atl_date": {"usd": None}}}


class TestRanking(unittest.TestCase):
    def test_ranked_best_is_lowest_market_cap_rank(self):
        ranked = P5._rank_cg_candidates(SIREN_COINS, "SIREN")
        self.assertEqual(ranked[0]["id"], "siren-2")           # rank 80 beats 6229

    def test_no_rank_sorts_last(self):
        coins = [{"id": "a", "symbol": "X", "market_cap_rank": None},
                 {"id": "b", "symbol": "X", "market_cap_rank": 1500}]
        self.assertEqual(P5._rank_cg_candidates(coins, "X")[0]["id"], "b")

    def test_widely_separated_ranks_not_ambiguous(self):
        self.assertFalse(P5._rank_ambiguous(P5._rank_cg_candidates(SIREN_COINS, "SIREN")))

    def test_close_ranks_are_ambiguous(self):
        coins = [{"id": "a", "symbol": "X", "market_cap_rank": 100},
                 {"id": "b", "symbol": "X", "market_cap_rank": 120}]
        self.assertTrue(P5._rank_ambiguous(P5._rank_cg_candidates(coins, "X")))


class TestPriceDivergence(unittest.TestCase):
    def test_dead_token_price_diverges_from_perp(self):
        self.assertTrue(P5._price_diverges(0.003, 1.06))       # 350x off → wrong token
        self.assertFalse(P5._price_diverges(1.08, 1.06))       # matches → right token
        self.assertFalse(P5._price_diverges(0, 1.06))          # missing → can't judge
        self.assertFalse(P5._price_diverges(1.0, None))


class TestKnownIdMapFixed(unittest.TestCase):
    def test_siren_known_id_points_to_the_live_token(self):
        # the hardcoded map short-circuits search → it MUST point at siren-2, not the dead siren
        self.assertEqual(P5.KNOWN_CG_IDS.get("SIREN"), "siren-2")


class TestResolutionIntegration(unittest.TestCase):
    def setUp(self):
        self._fetch, self._perp, self._known = P5.fetch, P5._perp_price, P5.KNOWN_CG_IDS
        P5.KNOWN_CG_IDS = {}                  # force the search path (bypass the curated map)

    def tearDown(self):
        P5.fetch, P5._perp_price, P5.KNOWN_CG_IDS = self._fetch, self._perp, self._known

    def _wire(self, perp):
        P5._perp_price = lambda t: perp

        def fake_fetch(url):
            if "/search" in url:
                return {"coins": SIREN_COINS}
            if "siren-2" in url:
                return _detail("siren-2", 1.08, {"binance-smart-chain": "0x997a58129890bbda032231a52ed1ddc845fc18e1"})
            if "/coins/siren?" in url or "/coins/siren/" in url or url.rstrip("/").endswith("/siren"):
                return _detail("siren", 0.003, {"ethereum": "0xdeadbeef"})
            return {"_error": "unexpected url"}
        P5.fetch = fake_fetch

    def test_siren_resolves_to_siren2_not_the_dead_token(self):
        self._wire(perp=1.06)
        out = P5.coingecko_layer("SIREN")
        self.assertEqual(out["cg_id"], "siren-2")
        self.assertEqual(out.get("resolved_id"), "siren-2")
        self.assertAlmostEqual(out["price"], 1.08, places=3)
        self.assertIn("binance-smart-chain", out["contracts"])
        self.assertFalse(out.get("low_confidence"))            # rank+price clearly disambiguate

    def test_divergence_reresolves_or_flags(self):
        # even if ranking somehow led with the dead token, the perp sanity check must not let a
        # 350x-divergent price through as a confident read.
        coins_dead_first = [SIREN_COINS[0], SIREN_COINS[1]]    # dead 'siren' listed first
        P5._perp_price = lambda t: 1.06

        def fake_fetch(url):
            if "/search" in url:
                # force a tie so the dead token could win on order, exercising the price guard
                return {"coins": [{"id": "siren", "symbol": "SIREN", "market_cap_rank": 80},
                                  {"id": "siren-2", "symbol": "SIREN", "market_cap_rank": 81}]}
            if "siren-2" in url:
                return _detail("siren-2", 1.08, {"binance-smart-chain": "0x997a"})
            return _detail("siren", 0.003, {"ethereum": "0xdead"})
        P5.fetch = fake_fetch
        out = P5.coingecko_layer("SIREN")
        # the 0.003 token must NOT be returned as a confident match: either re-resolved to the
        # price-sane siren-2, or flagged low_confidence.
        if out["cg_id"] == "siren":
            self.assertTrue(out.get("low_confidence"))
        else:
            self.assertEqual(out["cg_id"], "siren-2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
