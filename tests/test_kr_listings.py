#!/usr/bin/env python3
"""SPEC-153 — Korean listing/delisting tripwire (Upbit + Bithumb, keyless).

Trades the UB/CAP listing-pump class the user's manual feed caught 2026-08-06, now
automated at 15-min cadence: a listing NOTICE precedes the listing itself, so the
notice is the earliest tradeable signal (KR_LISTING_NOTICE). The inverse — a delisting
notice (거래지원 종료) — is early warning of single-venue-isolation risk on tracked
names (KR_DELISTING_NOTICE). A market-code diff against a persisted baseline catches
listings the notice layer missed (KR_LISTED); a caution-flag flip
(CONCENTRATION_OF_SMALL_ACCOUNTS etc.) is a crime-desk fingerprint straight from the
exchange (KR_FLAG_CHANGE).

Fixtures below are shaped from the real Upbit/Bithumb responses, captured 2026-08-20
(EVAL-free-apis-round2.md gap 4 desk-side verification) — no live network in this file.
Offline-deterministic: fetch_fn / tracked_tickers_fn / perp_listed_fn are all injected.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))


def _load(name, alias=None):
    spec = importlib.util.spec_from_file_location(alias or name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


KR = _load("kr_listings")

# ── fixtures (captured 2026-08-20, shapes match the live Upbit/Bithumb responses) ──

MARKET_ALL_BASELINE = [
    {"market": "KRW-BTC", "korean_name": "비트코인", "english_name": "Bitcoin",
     "market_event": {"warning": False, "caution": {"CONCENTRATION_OF_SMALL_ACCOUNTS": False,
                                                      "PRICE_FLUCTUATIONS": False}}},
    {"market": "KRW-ETH", "korean_name": "이더리움", "english_name": "Ethereum",
     "market_event": {"warning": False, "caution": {"CONCENTRATION_OF_SMALL_ACCOUNTS": False,
                                                      "PRICE_FLUCTUATIONS": False}}},
]

MARKET_ALL_NEW_LISTING = MARKET_ALL_BASELINE + [
    {"market": "KRW-XPIN", "korean_name": "엑스핀", "english_name": "XPIN",
     "market_event": {"warning": False, "caution": {}}},
]

MARKET_ALL_FLAG_FLIP = [
    MARKET_ALL_BASELINE[0],
    {"market": "KRW-ETH", "korean_name": "이더리움", "english_name": "Ethereum",
     "market_event": {"warning": False, "caution": {"CONCENTRATION_OF_SMALL_ACCOUNTS": True,
                                                      "PRICE_FLUCTUATIONS": False}}},
]

ANNOUNCEMENTS_EMPTY = {"data": {"list": []}}

ANNOUNCEMENTS_DELISTING_TRACKED = {
    "data": {"list": [
        {"id": 9001, "title": "[거래지원 종료] 유비(UB) 마켓 거래지원 종료 안내",
         "listed_at": "2026-08-20T09:00:00+09:00"},
    ]}
}

ANNOUNCEMENTS_LISTING_NO_SYMBOL = {
    "data": {"list": [
        {"id": 9002, "title": "신규 거래지원 안내", "listed_at": "2026-08-20T09:05:00+09:00"},
    ]}
}

BITHUMB_NOTICES_EMPTY = {"data": []}

BITHUMB_NOTICES_LISTING = {
    "data": [
        {"id": 5001, "title": "[안내] 마켓 추가 안내 (XPIN)", "date": "2026-08-20T08:00:00+09:00"},
    ]
}


def _tracked_fn(tickers):
    return lambda: list(tickers)


def _perp_fn(known):
    return lambda symbol: known.get(symbol.upper())


class TestPureHelpers(unittest.TestCase):
    def test_krw_market_to_symbol(self):
        self.assertEqual(KR.krw_market_to_symbol("KRW-BTC"), "BTC")
        self.assertIsNone(KR.krw_market_to_symbol("BTC-ETH"))
        self.assertIsNone(KR.krw_market_to_symbol(None))

    def test_classify_notice_title_listing(self):
        self.assertEqual(KR.classify_notice_title("유비(UB) 마켓 거래지원 개시 안내"), "LISTING")

    def test_classify_notice_title_delisting_wins_over_listing_keywords(self):
        self.assertEqual(KR.classify_notice_title("[거래지원 종료] 유비(UB) 마켓 거래지원 종료 안내"),
                         "DELISTING")

    def test_classify_notice_title_no_match(self):
        self.assertIsNone(KR.classify_notice_title("점검 안내"))

    def test_extract_symbol_from_title(self):
        self.assertEqual(KR.extract_symbol_from_title("유비(UB) 마켓 거래지원 개시 안내"), "UB")
        self.assertIsNone(KR.extract_symbol_from_title("신규 거래지원 안내"))

    def test_diff_new_listings(self):
        baseline = KR.parse_market_all(MARKET_ALL_BASELINE)
        fresh = KR.parse_market_all(MARKET_ALL_NEW_LISTING)
        self.assertEqual(KR.diff_new_listings(baseline, fresh), ["KRW-XPIN"])

    def test_diff_flag_changes(self):
        baseline = KR.parse_market_all(MARKET_ALL_BASELINE)
        fresh = KR.parse_market_all(MARKET_ALL_FLAG_FLIP)
        changes = KR.diff_flag_changes(baseline, fresh)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["market"], "KRW-ETH")
        self.assertEqual(changes[0]["flag"], "CONCENTRATION_OF_SMALL_ACCOUNTS")
        self.assertFalse(changes[0]["old"])
        self.assertTrue(changes[0]["new"])


class TestSweepDoD(unittest.TestCase):
    """Each test below is one bullet of the SPEC-153 '## Test expectation' DoD."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_run_seeds_zero_events(self):
        r = KR.sweep(state_dir=self.state_dir,
                     fetch_market_all=lambda: MARKET_ALL_BASELINE,
                     fetch_announcements=lambda: ANNOUNCEMENTS_EMPTY,
                     fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                     tracked_tickers_fn=_tracked_fn([]),
                     perp_listed_fn=_perp_fn({}))
        self.assertTrue(r["first_run"])
        self.assertEqual(r["events"], [])
        self.assertEqual(r["degraded_sources"], [])

    def test_new_market_yields_one_kr_listed_event_with_perp_crossref(self):
        KR.sweep(state_dir=self.state_dir,
                fetch_market_all=lambda: MARKET_ALL_BASELINE,
                fetch_announcements=lambda: ANNOUNCEMENTS_EMPTY,
                fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                tracked_tickers_fn=_tracked_fn([]),
                perp_listed_fn=_perp_fn({}))
        r = KR.sweep(state_dir=self.state_dir,
                    fetch_market_all=lambda: MARKET_ALL_NEW_LISTING,
                    fetch_announcements=lambda: ANNOUNCEMENTS_EMPTY,
                    fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                    tracked_tickers_fn=_tracked_fn([]),
                    perp_listed_fn=_perp_fn({"XPIN": True}))
        listed = [e for e in r["events"] if e["class"] == "KR_LISTED"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["ticker"], "XPIN")
        self.assertIs(listed[0]["perp_listed"], True)

    def test_delisting_notice_on_tracked_name_is_high(self):
        KR.sweep(state_dir=self.state_dir,
                fetch_market_all=lambda: MARKET_ALL_BASELINE,
                fetch_announcements=lambda: ANNOUNCEMENTS_EMPTY,
                fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                tracked_tickers_fn=_tracked_fn(["UB"]),
                perp_listed_fn=_perp_fn({}))
        r = KR.sweep(state_dir=self.state_dir,
                    fetch_market_all=lambda: MARKET_ALL_BASELINE,
                    fetch_announcements=lambda: ANNOUNCEMENTS_DELISTING_TRACKED,
                    fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                    tracked_tickers_fn=_tracked_fn(["UB"]),
                    perp_listed_fn=_perp_fn({"UB": True}))
        de = [e for e in r["events"] if e["class"] == "KR_DELISTING_NOTICE"]
        self.assertEqual(len(de), 1)
        self.assertEqual(de[0]["severity"], "HIGH")
        self.assertEqual(de[0]["ticker"], "UB")
        self.assertTrue(de[0]["tracked"])
        self.assertTrue(de[0]["page"])

    def test_cf403_on_announcements_degrades_but_sources_1_and_3_still_run(self):
        KR.sweep(state_dir=self.state_dir,
                fetch_market_all=lambda: MARKET_ALL_BASELINE,
                fetch_announcements=lambda: ANNOUNCEMENTS_EMPTY,
                fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                tracked_tickers_fn=_tracked_fn([]),
                perp_listed_fn=_perp_fn({}))

        def _blocked():
            raise RuntimeError("HTTP 403: Cloudflare")

        r = KR.sweep(state_dir=self.state_dir,
                    fetch_market_all=lambda: MARKET_ALL_NEW_LISTING,
                    fetch_announcements=_blocked,
                    fetch_bithumb=lambda: BITHUMB_NOTICES_LISTING,
                    tracked_tickers_fn=_tracked_fn([]),
                    perp_listed_fn=_perp_fn({"XPIN": True}))
        self.assertEqual(len(r["degraded_sources"]), 1)
        self.assertEqual(r["degraded_sources"][0]["source"], "upbit_announcements")
        # source 1 (market diff) and source 3 (bithumb) still produced events —
        # never a silently smaller sweep.
        classes = {e["class"] for e in r["events"]}
        self.assertIn("KR_LISTED", classes)
        self.assertIn("KR_LISTING_NOTICE", classes)

    def test_caution_flag_flip_is_med_no_page(self):
        KR.sweep(state_dir=self.state_dir,
                fetch_market_all=lambda: MARKET_ALL_BASELINE,
                fetch_announcements=lambda: ANNOUNCEMENTS_EMPTY,
                fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                tracked_tickers_fn=_tracked_fn([]),
                perp_listed_fn=_perp_fn({}))
        r = KR.sweep(state_dir=self.state_dir,
                    fetch_market_all=lambda: MARKET_ALL_FLAG_FLIP,
                    fetch_announcements=lambda: ANNOUNCEMENTS_EMPTY,
                    fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                    tracked_tickers_fn=_tracked_fn([]),
                    perp_listed_fn=_perp_fn({}))
        fc = [e for e in r["events"] if e["class"] == "KR_FLAG_CHANGE"]
        self.assertEqual(len(fc), 1)
        self.assertEqual(fc[0]["severity"], "MED")
        self.assertFalse(fc[0]["page"])

    def test_unmapped_symbol_is_discovery_candidate_not_a_crash(self):
        KR.sweep(state_dir=self.state_dir,
                fetch_market_all=lambda: MARKET_ALL_BASELINE,
                fetch_announcements=lambda: ANNOUNCEMENTS_EMPTY,
                fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                tracked_tickers_fn=_tracked_fn([]),
                perp_listed_fn=_perp_fn({}))
        r = KR.sweep(state_dir=self.state_dir,
                    fetch_market_all=lambda: MARKET_ALL_BASELINE,
                    fetch_announcements=lambda: ANNOUNCEMENTS_LISTING_NO_SYMBOL,
                    fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                    tracked_tickers_fn=_tracked_fn([]),
                    perp_listed_fn=_perp_fn({}))
        self.assertEqual(len(r["events"]), 1)
        ev = r["events"][0]
        self.assertEqual(ev["class"], "KR_LISTING_NOTICE")
        self.assertIsNone(ev["ticker"])
        self.assertTrue(ev["unmapped"])

    def test_repeat_sweep_with_no_change_is_quiet(self):
        for _ in range(2):
            r = KR.sweep(state_dir=self.state_dir,
                        fetch_market_all=lambda: MARKET_ALL_BASELINE,
                        fetch_announcements=lambda: ANNOUNCEMENTS_EMPTY,
                        fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                        tracked_tickers_fn=_tracked_fn([]),
                        perp_listed_fn=_perp_fn({}))
        self.assertEqual(r["events"], [])

    def test_notice_not_replayed_on_next_sweep(self):
        KR.sweep(state_dir=self.state_dir,
                fetch_market_all=lambda: MARKET_ALL_BASELINE,
                fetch_announcements=lambda: ANNOUNCEMENTS_EMPTY,
                fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                tracked_tickers_fn=_tracked_fn(["UB"]),
                perp_listed_fn=_perp_fn({}))
        r1 = KR.sweep(state_dir=self.state_dir,
                     fetch_market_all=lambda: MARKET_ALL_BASELINE,
                     fetch_announcements=lambda: ANNOUNCEMENTS_DELISTING_TRACKED,
                     fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                     tracked_tickers_fn=_tracked_fn(["UB"]),
                     perp_listed_fn=_perp_fn({}))
        self.assertEqual(len(r1["events"]), 1)
        r2 = KR.sweep(state_dir=self.state_dir,
                     fetch_market_all=lambda: MARKET_ALL_BASELINE,
                     fetch_announcements=lambda: ANNOUNCEMENTS_DELISTING_TRACKED,
                     fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                     tracked_tickers_fn=_tracked_fn(["UB"]),
                     perp_listed_fn=_perp_fn({}))
        self.assertEqual(r2["events"], [])


class TestStatus(unittest.TestCase):
    def test_status_before_any_sweep(self):
        with tempfile.TemporaryDirectory() as d:
            r = KR.status(state_dir=Path(d))
            self.assertTrue(r["ok"])
            self.assertEqual(r["market_count"], 0)

    def test_status_after_sweep_reflects_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            KR.sweep(state_dir=Path(d),
                    fetch_market_all=lambda: MARKET_ALL_BASELINE,
                    fetch_announcements=lambda: ANNOUNCEMENTS_EMPTY,
                    fetch_bithumb=lambda: BITHUMB_NOTICES_EMPTY,
                    tracked_tickers_fn=_tracked_fn([]),
                    perp_listed_fn=_perp_fn({}))
            r = KR.status(state_dir=Path(d))
            self.assertEqual(r["market_count"], len(MARKET_ALL_BASELINE))


if __name__ == "__main__":
    unittest.main()
