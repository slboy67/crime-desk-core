#!/usr/bin/env python3
"""SPEC-175 — resolver ambiguity surfaces its candidates, not a bare "resolve_ambiguous".

Run:  python3 tests/test_onboard_resolve_candidates.py

Live gap: `brief`'s onchain layer returned `UNMAPPED(onboard_failed:resolve_ambiguous)`
without saying WHAT was ambiguous. The KORU case (a tokenized Direxion 3x ETF passing as
a crypto ticker) would have self-disqualified at the resolve step had the candidates been
printed. Contract under test:
  - `onboard._cg_search` caps candidates at 5 and annotates each with `not_crypto`
    (ETF/stock naming heuristic on the CoinGecko candidate's name);
  - `onboard.build_onboard`'s resolve_ambiguous path logs one WARN line to stderr;
  - `brief.build_brief`'s onchain layer surfaces `resolve_candidates` (max 5,
    {id, name, chain, mc_rank, not_crypto}) when mapping failed ambiguous;
  - a mapping failure with no candidates (resolve_not_found, exceptions, ...) never adds
    the key.
Network seams monkeypatched — offline, deterministic.
"""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OB = _load("onboard")


class TestNonCryptoHeuristic(unittest.TestCase):
    def test_direxion_etf_flagged(self):
        self.assertTrue(OB._looks_non_crypto("Direxion Daily South Korea 3X Bull Shares"))

    def test_ishares_flagged(self):
        self.assertTrue(OB._looks_non_crypto("iShares MSCI Emerging Markets ETF"))

    def test_plain_coin_name_not_flagged(self):
        self.assertFalse(OB._looks_non_crypto("Solstice"))

    def test_none_name_not_flagged(self):
        self.assertFalse(OB._looks_non_crypto(None))


class TestCgSearchCapAndAnnotation(unittest.TestCase):
    def setUp(self):
        self._fetch = None

    def test_capped_at_five_and_annotated(self):
        coins = [{"id": f"koru{i}", "symbol": "KORU", "name": f"Koru Coin {i}",
                  "market_cap_rank": None} for i in range(7)]
        coins[3] = {"id": "direxion-koru", "symbol": "KORU",
                    "name": "Direxion Daily South Korea CI 3X Bull Shares",
                    "market_cap_rank": None}
        import pull5
        orig = pull5.fetch
        pull5.fetch = lambda url: {"coins": coins}
        try:
            out = OB._cg_search("KORU")
        finally:
            pull5.fetch = orig
        self.assertLessEqual(len(out), 5)
        self.assertTrue(any(c["not_crypto"] for c in out))
        self.assertTrue(any(not c["not_crypto"] for c in out))
        for c in out:
            self.assertIn("not_crypto", c)


class TestResolveAmbiguousLogsWarn(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self._orig_wallets = OB.WALLETS
        OB.WALLETS = Path(self.dir.name) / "tracked_wallets.json"
        OB.WALLETS.write_text(json.dumps({"rpcs": {}, "tokens": {}}))
        self._orig_resolve, self._orig_search = OB._cg_resolve, OB._cg_search

    def tearDown(self):
        OB.WALLETS = self._orig_wallets
        OB._cg_resolve, OB._cg_search = self._orig_resolve, self._orig_search
        self.dir.cleanup()

    def test_warn_logged_once_on_ambiguous(self):
        candidates = [{"id": "solstice", "symbol": "SLX", "name": "Solstice",
                       "market_cap_rank": 900, "not_crypto": False}]
        OB._cg_resolve = lambda tk, cg_id=None: {"_error": "fetch failed"}
        OB._cg_search = lambda tk: list(candidates)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = OB.build_onboard("SLX")
        self.assertEqual(out["reason"], "resolve_ambiguous")
        lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("WARN")]
        self.assertEqual(len(lines), 1)
        self.assertIn("SLX", lines[0])


BR = _load("brief")


class TestBriefSurfacesResolveCandidates(unittest.TestCase):
    def setUp(self):
        BR.load_watchlist = lambda: ([{"ticker": "AAA", "state": "watch"}], None)
        BR.live_perp = lambda tk: None
        BR.classify_token = lambda tok, live: {"verdict": "CONFIRMS", "reason": "r",
                                                "thesis_present": False, "direction": "?"}
        BR.build_analyse = lambda tk: {"verdict": "WATCH", "price": 1.0}
        BR.build_depth = lambda tk, v=None: {"venues": {}}
        BR.build_onchain = lambda tk: {"signal": "UNTRACKED", "bias": "?", "score": 0,
                                        "nonces": {}, "concentration": {}}
        BR.fetch_aster_symbols = lambda: None
        # BR.onboard resolves via sys.modules — SHARED with every other test file that
        # does a plain `import onboard` (board_tick, brief itself, ...). Must restore.
        self._orig_build_onboard = BR.onboard.build_onboard

    def tearDown(self):
        BR.onboard.build_onboard = self._orig_build_onboard

    def test_candidates_surfaced_capped_and_shaped(self):
        candidates = [{"id": f"c{i}", "symbol": "AAA", "name": f"Coin {i}",
                       "market_cap_rank": i, "not_crypto": i == 0} for i in range(6)]
        BR.onboard.build_onboard = lambda tk, **kw: {"ok": False, "reason": "resolve_ambiguous",
                                                       "candidates": candidates}
        b = BR.build_brief("AAA")
        rc = b["onchain"]["resolve_candidates"]
        self.assertEqual(len(rc), 5)
        for c in rc:
            self.assertEqual(set(c), {"id", "name", "chain", "mc_rank", "not_crypto"})
        self.assertTrue(rc[0]["not_crypto"])

    def test_no_candidates_key_when_not_ambiguous(self):
        BR.onboard.build_onboard = lambda tk, **kw: {"ok": False, "reason": "resolve_not_found"}
        b = BR.build_brief("AAA")
        self.assertNotIn("resolve_candidates", b["onchain"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
