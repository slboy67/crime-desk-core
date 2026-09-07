#!/usr/bin/env python3
"""SPEC 30 — `brief` capability: one-call full-stack token read.

Run:  python3 tests/test_brief.py

`brief` composes the already-MERGED capabilities (classify → state, analyse → perp,
depth → BOTH venue books, onchain → on-chain) into ONE distilled envelope so a bare
ticker never returns a chopped-up partial read (the SKYAI single-venue blind spot,
memory: feedback_skyai_exit_liquidity_is_bitget). It re-derives nothing — pure
composition. Every layer is degrade-explicit (`available:false`+reason, never silent
null). All underlying build fns are mocked here — offline-deterministic.

Also pins the concurrency-safety fix it depends on: `onchain._save_baseline` must be
atomic, because `brief` runs analyse + onchain concurrently and BOTH write the same
per-ticker nonce baseline file.
"""
import importlib.util
import json
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


BR = _load("brief")
ONCHAIN = _load("onchain")


# ── canned layer outputs (shape mirrors the real build fns) ────────────────────
def _fake_watchlist_with_thesis():
    return ([{
        "ticker": "SKYAI", "state": "short-loading watch",
        "thesis": {"direction": "SHORT", "entry_zone": [0.20, 0.21], "stop": 0.225,
                   "tp": [0.18, 0.16], "triggers": ["LH retest"],
                   "invalidation": "reclaim 0.225", "time_stop_h": 72,
                   "committed_ts": "2026-06-05"},
    }], None)


def _fake_classify_token(tok, live=None):
    return {"ticker": tok["ticker"], "verdict": "CONFIRMS",
            "reason": "price within zone, funding flat", "direction": "SHORT",
            "thesis_present": bool(tok.get("thesis")), "live": live}


def _fake_analyse(ticker, days=90):
    return {"ticker": ticker, "verdict": "WATCH", "direction": "SHORT-loading-WATCH",
            "tier": "watch", "funding_4h": 0.004, "funding_venue": "binance",
            "funding_unavailable": False, "oi_chg_pct": -3, "oi_chg_pct_48h": -3,
            "near_ath": False,
            "cvd_verdict": "distribution", "price": 0.1834, "onchain": "OK",
            "nonce_signal": "QUIET", "notes": []}


def _fake_depth(ticker, venue=None):
    venues = {
        "bitget": {"available": True, "mid": 0.1865, "best_bid": 0.1864, "best_ask": 0.1866,
                   "bid_shelf_below": {"price": 0.1865, "notional_usd": 120000, "dist_pct": -0.2,
                                       "round_number": None},
                   "ask_wall_above": {"price": 0.19, "notional_usd": 80000, "dist_pct": 1.8,
                                      "round_number": 0.19},
                   "truncated": False, "deepest_level_seen": {"bid": 0.18, "ask": 0.19}},
        "binance": {"available": True, "mid": 0.1834, "best_bid": 0.1833, "best_ask": 0.1835,
                    "bid_shelf_below": {"price": 0.1834, "notional_usd": 95000, "dist_pct": -0.1,
                                        "round_number": None},
                    "ask_wall_above": None,
                    "truncated": True, "deepest_level_seen": {"bid": 0.182, "ask": 0.185}},
    }
    if venue:
        venues = {venue.lower(): venues[venue.lower()]}
    return {"ticker": ticker, "venues": venues, "note": "read-only"}


def _fake_onchain(ticker, depth="fast"):
    return {"ticker": ticker, "bias": "LEAN BEARISH (distribution loading)", "score": -30,
            "signal": "ESCALATION",
            "nonces": {"tracked": True, "signal": "ESCALATION", "score": -30,
                       "baseline_seeded": True,
                       "newly_fired": [{"label": "GATE-DEPOSIT", "dest_kind": "cex-execution"}],
                       "escalation_fired": [{"label": "GATE-DEPOSIT", "tier": "distribution",
                                             "nonce_prev": 10, "nonce_now": 11,
                                             "dest_kind": "cex-execution"}]},
            "concentration": {"available": True, "top1_pct": 22.5, "top10_pct": 71.0,
                              "holder_count": 25430},
            "safe_history": {"available": True}, "recent_flows": {"available": True},
            "vc_overlap": {"available": False}, "coverage": {}}


def _fake_perpfinder_empty(mode, ticker=None, **kw):
    """Default venue-breadth stub for tests that don't care about SPEC-159 — the symbol
    is simply absent from the matrix, so the layer degrades to not_in_matrix and every
    other section of the brief stays byte-identical to pre-SPEC-159 behavior."""
    return {"ok": True, "mode": mode, "rows": [], "meta": {}}


def _fake_risk_card_layer(ticker, thesis_display, equity_arg=None):
    """SPEC-172: `_risk_card_layer` is the one real network leg left unmocked pre-SPEC-172
    (venue equity + a maxsize exit-book walk) — every test here ran it for real against
    the live Aster key sitting in this worktree's config/secrets.json, costing the whole
    suite ~35s and making it flaky/non-offline. Stubbed here like every other layer."""
    return {"available": False, "reason": "stub"}


def _fake_build_venue_bars(ticker, interval="1h", n=6):
    """SPEC-188: default stub — the 14-venue sweep is a real network layer; tests that
    care about its shape override BR.build_venue_bars themselves (mirrors the
    build_perpfinder/risk_card stub pattern above)."""
    raise RuntimeError("stub: no network in tests")


def _patch_all(funded=True):
    """Install the canned layer fns onto the brief module; return a restore fn."""
    saved = {k: getattr(BR, k) for k in
             ("load_watchlist", "classify_token", "live_perp",
              "build_analyse", "build_depth", "build_onchain", "fetch_aster_symbols",
              "build_perpfinder", "_risk_card_layer", "build_venue_bars", "DIRECT_OI_FETCHERS")}
    BR.load_watchlist = _fake_watchlist_with_thesis
    BR.classify_token = _fake_classify_token
    BR.live_perp = lambda t: {"price": 0.1834, "funding_4h": 0.004}
    BR.build_analyse = _fake_analyse
    BR.build_depth = _fake_depth
    BR.build_onchain = _fake_onchain
    BR.fetch_aster_symbols = lambda: None   # SPEC-136: unknown — no live network in tests
    BR.build_perpfinder = _fake_perpfinder_empty   # SPEC-159: default = not_in_matrix
    BR._risk_card_layer = _fake_risk_card_layer    # SPEC-172: no live venue calls in tests
    BR.build_venue_bars = _fake_build_venue_bars   # SPEC-188: no live network in tests
    BR.DIRECT_OI_FETCHERS = {}   # SPEC-191 #2: no live network in tests unless a test opts in

    def restore():
        for k, v in saved.items():
            setattr(BR, k, v)
    return restore


class TestEnvelopeShape(unittest.TestCase):
    def setUp(self):
        self._restore = _patch_all()

    def tearDown(self):
        self._restore()

    def test_top_level_keys(self):
        b = BR.build_brief("SKYAI")
        for k in ("ticker", "state", "perp", "books", "onchain", "headline"):
            self.assertIn(k, b, f"missing top-level key {k}")
        self.assertEqual(b["ticker"], "SKYAI")
        self.assertIsInstance(b["headline"], str)
        self.assertTrue(b["headline"])

    def test_state_section_carries_verdict_and_thesis(self):
        st = BR.build_brief("SKYAI")["state"]
        self.assertTrue(st["available"])
        self.assertEqual(st["verdict"], "CONFIRMS")
        self.assertTrue(st["thesis_present"])
        # committed levels surfaced so the read is anchored to the commitment
        th = st["thesis"]
        for k in ("direction", "entry_zone", "stop", "tp", "triggers", "invalidation", "time_stop"):
            self.assertIn(k, th, f"thesis missing {k}")
        self.assertEqual(th["direction"], "SHORT")

    def test_perp_section_fields(self):
        perp = BR.build_brief("SKYAI")["perp"]
        self.assertTrue(perp["available"])
        for k in ("verdict", "direction", "tier", "funding_4h", "funding_venue",
                  "oi_chg_pct", "near_ath", "cvd_verdict", "price"):
            self.assertIn(k, perp)
        self.assertEqual(perp["price"], 0.1834)

    def test_perp_oi_chg_pct_window_labelled(self):
        # SPEC-174 #6: the raw oi_chg_pct is a fixed 48h window (perp_analyser.py's
        # openInterestHist period=1h&limit=48) — the label rides alongside it, additive.
        perp = BR.build_brief("SKYAI")["perp"]
        self.assertIn("oi_chg_pct_48h", perp)
        self.assertEqual(perp["oi_chg_pct_48h"], perp["oi_chg_pct"])

    def test_onchain_section_spec24_and_spec28(self):
        oc = BR.build_brief("SKYAI")["onchain"]
        self.assertTrue(oc["available"])
        self.assertEqual(oc["signal"], "ESCALATION")
        # SPEC 24: newly_fired is the token-out-gated CONFIRMED list (escalation_fired)
        self.assertEqual(len(oc["newly_fired"]), 1)
        # SPEC 28: a CEX-routed escalation = execution
        self.assertEqual(oc["staging_vs_execution"], "execution")
        # concentration top1/top10 surfaced
        self.assertEqual(oc["concentration"]["top1_pct"], 22.5)
        self.assertEqual(oc["concentration"]["top10_pct"], 71.0)


class TestPerpVolMcFields(unittest.TestCase):
    """SPEC-174 #3 — brief.perp fills vol24h_usd (the size-venue 24h turnover already
    resolved onto live_perp's primary-venue vol_m, no second fetch) and mc_usd (CoinGecko,
    cached; degrades to null + an explicit reason on failure, never silently absent) — an
    unmapped/off-board name previously had neither surfaced at all."""

    def setUp(self):
        self._restore = _patch_all()
        self._orig_fetch_mc = BR.OM.fetch_market_cap
        BR.live_perp = lambda t: {"price": 0.1834, "funding_4h": 0.004, "vol_m": 12.5,
                                  "oi": 1_000_000}

    def tearDown(self):
        self._restore()
        BR.OM.fetch_market_cap = self._orig_fetch_mc

    def test_vol24h_usd_derived_from_primary_venue_vol_m(self):
        BR.OM.fetch_market_cap = lambda t, cg_id=None: 5_000_000.0
        perp = BR.build_brief("SKYAI")["perp"]
        self.assertEqual(perp["vol24h_usd"], 12_500_000.0)

    def test_mc_usd_surfaced_when_available(self):
        BR.OM.fetch_market_cap = lambda t, cg_id=None: 5_000_000.0
        perp = BR.build_brief("SKYAI")["perp"]
        self.assertEqual(perp["mc_usd"], 5_000_000.0)
        self.assertIsNone(perp["mc_unavailable_reason"])
        self.assertIsNotNone(perp["oi_mc_ratio"])   # OI+price present -> ratio computed too

    def test_mc_usd_unavailable_degrades_with_reason_never_silently_absent(self):
        BR.OM.fetch_market_cap = lambda t, cg_id=None: None
        perp = BR.build_brief("SKYAI")["perp"]
        self.assertIsNone(perp["mc_usd"])
        self.assertIsNotNone(perp["mc_unavailable_reason"])

    def test_missing_vol_m_degrades_vol24h_usd_to_none(self):
        BR.live_perp = lambda t: {"price": 0.1834, "funding_4h": 0.004}   # no vol_m
        BR.OM.fetch_market_cap = lambda t, cg_id=None: 5_000_000.0
        perp = BR.build_brief("SKYAI")["perp"]
        self.assertIsNone(perp["vol24h_usd"])


def _fake_onchain_distribution(ticker, depth="fast"):
    """SPEC-72: build_onchain output when a fired safe → operator aggregator distribution
    was resolved (the BSB QUIET-14.75M → XTOKEN-BILL-AGGREGATOR $2.87M case)."""
    return {"ticker": ticker,
            "bias": "DISTRIBUTING (tracked safe QUIET-14.75M → operator aggregator — §8 Stage-5)",
            "score": -30, "signal": "DISTRIBUTING",
            "nonces": {"tracked": True, "signal": "DISTRIBUTING", "score": -30,
                       "baseline_seeded": True,
                       "newly_fired": [{"label": "QUIET-14.75M", "dest_kind": "staging-internal"}],
                       "escalation_fired": [{"label": "QUIET-14.75M", "tier": "distribution",
                                             "dest_kind": "staging-internal"}]},
            "distribution": {"resolved": True, "staging": True, "distributing": True,
                             "flows": [{"safe_label": "QUIET-14.75M", "safe_address": "0xcbd3c6",
                                        "verdict": "DISTRIBUTING", "net_token_out": -750000,
                                        "dest": "0xa8bdd6", "dest_label": None,
                                        "dest_kind": "staging-internal"}],
                             "aggregator": {"address": "0x1ab497", "label": "XTOKEN-BILL-AGGREGATOR",
                                            "mode": "dex_swap_sell", "usd_in": 2870000,
                                            "amount_in": 2870000, "asset": "USDT", "n_settlements": 104}},
            "concentration": {"available": True, "top1_pct": 43.0, "top10_pct": 70.0,
                              "holder_count": 1000},
            "safe_history": {"available": True}, "recent_flows": {"available": True},
            "vc_overlap": {"available": False}, "coverage": {}}


class TestSpec72BriefSurfacesDistribution(unittest.TestCase):
    """SPEC-72: the brief must surface a resolved safe→aggregator distribution — a non-DORMANT
    on-chain signal, the fired safe in newly_fired, and the dex_swap_sell USDT magnitude in the
    headline — never DORMANT / NEUTRAL-CONSTRUCTIVE while Stage-5 distribution executes."""

    def setUp(self):
        self._restore = _patch_all()
        BR.build_onchain = _fake_onchain_distribution

    def tearDown(self):
        self._restore()

    def test_onchain_layer_carries_distribution(self):
        oc = BR.build_brief("BSB")["onchain"]
        self.assertTrue(oc["available"])
        self.assertEqual(oc["signal"], "DISTRIBUTING")
        self.assertEqual(oc["staging_vs_execution"], "staging")
        self.assertEqual(len(oc["newly_fired"]), 1)
        self.assertEqual(oc["distribution"]["aggregator"]["usd_in"], 2870000)

    def test_headline_names_aggregator_dex_swap_sell_magnitude(self):
        h = BR.build_brief("BSB")["headline"]
        self.assertIn("DISTRIBUTING", h)
        self.assertIn("aggregator dex_swap_sell", h)
        self.assertIn("USDT", h)
        self.assertIn("2.87", h)
        self.assertNotIn("DORMANT", h)


def _fake_onchain_recent_distribution(ticker, depth="fast"):
    """SPEC-73: build_onchain output read T+8h after the BSB $5.1M/24h wave — the LIVE signal
    has reverted to DORMANT (fires aged past the 6h window) but the 24h distribution-memory
    surface still carries the recent operator exit."""
    return {"ticker": ticker,
            "bias": "RECENT-DISTRIBUTION ~$5.1M/24h (2 safes, 1 drained to zero) — "
                    "§8 Stage-5 distributed recently (live signal DORMANT)",
            "score": 10, "signal": "DORMANT",
            "nonces": {"tracked": True, "signal": "DORMANT", "score": 10,
                       "baseline_seeded": True, "newly_fired": [], "escalation_fired": []},
            "distribution": {"resolved": True, "staging": True, "distributing": True, "live": False,
                             "flows": [{"safe_label": "QUIET-14.75M", "drained_to_zero": False,
                                        "distributed_usd": 2870000},
                                       {"safe_label": "QUIET-29M", "drained_to_zero": True,
                                        "distributed_usd": 2270000}],
                             "aggregator": {"label": "XTOKEN-BILL-AGGREGATOR", "mode": "dex_swap_sell",
                                            "usd_in": 2870000, "asset": "USDT"}},
            "recent_distribution": {"window_h": 24, "total_usd": 5140000, "n_safes": 2,
                                    "drained_to_zero": ["QUIET-29M"],
                                    "last_fire_ts": "2026-06-15T14:54:00.000Z"},
            "concentration": {"available": True, "top1_pct": 43.0, "top10_pct": 70.0,
                              "holder_count": 1000},
            "safe_history": {"available": True}, "recent_flows": {"available": True},
            "vc_overlap": {"available": False}, "coverage": {}}


class TestSpec73BriefSurfacesRecentDistribution(unittest.TestCase):
    """SPEC-73: a brief read hours after a distribution wave must still surface the 24h
    distribution-memory line + a non-NEUTRAL bias — DORMANT-now but distributed-recently is a
    real state, never plain NEUTRAL-CONSTRUCTIVE."""

    def setUp(self):
        self._restore = _patch_all()
        BR.build_onchain = _fake_onchain_recent_distribution

    def tearDown(self):
        self._restore()

    def test_onchain_layer_carries_recent_distribution(self):
        oc = BR.build_brief("BSB")["onchain"]
        self.assertTrue(oc["available"])
        self.assertEqual(oc["signal"], "DORMANT")               # live signal reverted
        rd = oc["recent_distribution"]
        self.assertIsNotNone(rd)
        self.assertEqual(rd["total_usd"], 5140000)
        self.assertEqual(rd["n_safes"], 2)
        self.assertEqual(rd["drained_to_zero"], ["QUIET-29M"])
        # the bias is explicit, never plain NEUTRAL-CONSTRUCTIVE
        self.assertIn("RECENT-DISTRIBUTION", oc["bias"])
        self.assertNotIn("NEUTRAL-CONSTRUCTIVE", oc["bias"])

    def test_headline_names_recent_distribution_magnitude(self):
        h = BR.build_brief("BSB")["headline"]
        self.assertIn("recent-distribution", h.lower())
        self.assertIn("5.1M", h)
        self.assertIn("drained to zero", h)


class TestBothVenueBooks(unittest.TestCase):
    def setUp(self):
        self._restore = _patch_all()

    def tearDown(self):
        self._restore()

    def test_no_venue_arg_returns_both_books(self):
        books = BR.build_brief("SKYAI")["books"]
        self.assertTrue(books["available"])
        self.assertIn("bitget", books["venues"])
        self.assertIn("binance", books["venues"])
        # the whole point: the Bitget exit book is never silently dropped on a cluster name
        bg = books["venues"]["bitget"]
        self.assertTrue(bg["available"])
        self.assertEqual(bg["bid_shelf_below"]["price"], 0.1865)
        for k in ("mid", "bid_shelf_below", "ask_wall_above", "truncated", "deepest_level_seen"):
            self.assertIn(k, bg)

    def test_explicit_venue_arg_restricts(self):
        books = BR.build_brief("SKYAI", venue="bitget")["books"]
        self.assertIn("bitget", books["venues"])
        self.assertNotIn("binance", books["venues"])

    def test_headline_echoes_bitget_bid(self):
        b = BR.build_brief("SKYAI")
        self.assertIn("0.1865", b["headline"])


class TestPerLayerDegradeExplicit(unittest.TestCase):
    """A failed layer = available:false + reason — never a silent null, never a crash
    that loses the other (good) layers. This is the recurring partial-read failure mode."""

    def setUp(self):
        self._restore = _patch_all()

    def tearDown(self):
        self._restore()

    def test_perp_layer_error_degrades_but_others_survive(self):
        def _boom(*a, **k):
            raise RuntimeError("perp analyser down")
        BR.build_analyse = _boom
        b = BR.build_brief("SKYAI")
        self.assertFalse(b["perp"]["available"])
        self.assertIn("reason", b["perp"])
        # the other layers still return
        self.assertTrue(b["books"]["available"])
        self.assertTrue(b["onchain"]["available"])

    def test_onchain_layer_error_degrades(self):
        def _boom(*a, **k):
            raise RuntimeError("moralis 401")
        BR.build_onchain = _boom
        oc = BR.build_brief("SKYAI")["onchain"]
        self.assertFalse(oc["available"])
        self.assertIn("reason", oc)

    def test_books_layer_error_degrades(self):
        def _boom(*a, **k):
            raise RuntimeError("venue timeout")
        BR.build_depth = _boom
        bk = BR.build_brief("SKYAI")["books"]
        self.assertFalse(bk["available"])
        self.assertIn("reason", bk)

    def test_partial_book_one_venue_down(self):
        def _one_down(ticker, venue=None):
            return {"ticker": ticker, "venues": {
                "bitget": {"available": True, "mid": 0.1865,
                           "bid_shelf_below": {"price": 0.1865}, "ask_wall_above": None,
                           "truncated": False, "deepest_level_seen": {}},
                "binance": {"available": False, "reason": "empty book"}}}
        BR.build_depth = _one_down
        books = BR.build_brief("SKYAI")["books"]
        self.assertTrue(books["venues"]["bitget"]["available"])
        self.assertFalse(books["venues"]["binance"]["available"])
        self.assertIn("reason", books["venues"]["binance"])


class TestThesisAbsentPath(unittest.TestCase):
    def setUp(self):
        self._restore = _patch_all()

    def tearDown(self):
        self._restore()

    def test_no_committed_thesis_is_discovery_read(self):
        # token present but no thesis block
        BR.load_watchlist = lambda: ([{"ticker": "NEWNAME", "state": ""}], None)
        BR.classify_token = lambda tok, live=None: {
            "ticker": tok["ticker"], "verdict": "CONFIRMS", "reason": "no thesis",
            "direction": "?", "thesis_present": False, "live": live}
        b = BR.build_brief("NEWNAME")
        self.assertFalse(b["state"]["thesis_present"])
        # brief STILL returns perp + books + onchain (a discovery read)
        self.assertTrue(b["perp"]["available"])
        self.assertTrue(b["books"]["available"])
        self.assertTrue(b["onchain"]["available"])

    def test_ticker_not_on_watchlist_still_returns_read(self):
        BR.load_watchlist = lambda: ([{"ticker": "OTHER"}], None)
        b = BR.build_brief("GHOST")
        self.assertFalse(b["state"]["thesis_present"])
        self.assertTrue(b["perp"]["available"])
        self.assertTrue(b["books"]["available"])


class TestSaveBaselineAtomic(unittest.TestCase):
    """brief runs analyse + onchain concurrently; both write state/nonce_baseline_<T>.json.
    The write must be atomic so concurrent writers never produce a torn (unparseable) file."""

    def test_concurrent_save_baseline_never_corrupts(self):
        ticker = "ZZBRIEFTEST"
        path = ONCHAIN._baseline_path(ticker)
        try:
            rows_a = [{"address": "0xaaa", "nonce": 5}, {"address": "0xbbb", "nonce": 9}]
            rows_b = [{"address": "0xaaa", "nonce": 6}, {"address": "0xbbb", "nonce": 10}]
            errors = []

            def hammer(rows):
                for _ in range(50):
                    try:
                        ONCHAIN._save_baseline(ticker, rows, 1000.0, 1000.0)
                        # read it back — must always parse
                        json.loads(path.read_text())
                    except Exception as e:  # noqa: BLE001
                        errors.append(repr(e))

            threads = [threading.Thread(target=hammer, args=(r,))
                       for r in (rows_a, rows_b, rows_a, rows_b)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [], f"torn/unreadable baseline under concurrency: {errors[:3]}")
            # final file is valid and well-formed
            final = json.loads(path.read_text())
            self.assertIn("nonces", final)
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


# ── SPEC-159: brief absorbs the PerpFinder venue-breadth read ──────────────────────────
# CASHCAT-shaped fixture (9 venues, Hyperliquid the dominant/size-book OI, MEXC null-oi) —
# mirrors the real 2026-08-25 incident: brief saw Bybit+HL only while the matrix had 9.
CASHCAT_BREADTH_FIXTURE = {
    "ok": True, "mode": "funding",
    "rows": [
        {"asset": "CASHCAT", "venue": "Bybit", "funding_pi_4h": 0.00221, "normalized": True,
         "oi": 14104626.97, "price": 0.20059, "field_support": None},
        {"asset": "CASHCAT", "venue": "Binance", "funding_pi_4h": 0.0018, "normalized": True,
         "oi": 8000000.0, "price": 0.2006, "field_support": None},
        {"asset": "CASHCAT", "venue": "Bitget", "funding_pi_4h": 0.0017, "normalized": True,
         "oi": 5000000.0, "price": 0.2005, "field_support": None},
        {"asset": "CASHCAT", "venue": "Aster", "funding_pi_4h": 0.0019, "normalized": True,
         "oi": 2000000.0, "price": 0.2007, "field_support": None},
        {"asset": "CASHCAT", "venue": "Hyperliquid", "funding_pi_4h": 0.00196, "normalized": True,
         "oi": 30385822.02, "price": 0.20085, "field_support": None},
        {"asset": "CASHCAT", "venue": "MEXC", "funding_pi_4h": 0.0011, "normalized": True,
         "oi": None, "price": 0.2012, "field_support": {"oi": "unsupported", "price": "observed"}},
        {"asset": "CASHCAT", "venue": "Gate", "funding_pi_4h": 0.0009, "normalized": True,
         "oi": 1000000.0, "price": 0.201, "field_support": None},
        {"asset": "CASHCAT", "venue": "KuCoin", "funding_pi_4h": 0.0008, "normalized": True,
         "oi": 500000.0, "price": 0.2009, "field_support": None},
        {"asset": "CASHCAT", "venue": "OKX", "funding_pi_4h": 0.0015, "normalized": True,
         "oi": 3000000.0, "price": 0.2008, "field_support": None},
    ],
    "venues_covered": 9, "meta": {"normalized": True},
}

# Mostly-off-desk fixture: the size book (MEXC) and most of the OI sit on venues outside
# the desk's wired set — desk_blind_share_pct must clear 30 and the headline must warn.
BLIND_BREADTH_FIXTURE = {
    "ok": True, "mode": "funding",
    "rows": [
        {"asset": "GHOST", "venue": "Bybit", "funding_pi_4h": 0.001, "normalized": True,
         "oi": 5000000.0, "price": 1.0, "field_support": None},
        {"asset": "GHOST", "venue": "MEXC", "funding_pi_4h": 0.0005, "normalized": True,
         "oi": 20000000.0, "price": 1.0, "field_support": None},
        {"asset": "GHOST", "venue": "Gate", "funding_pi_4h": 0.0004, "normalized": True,
         "oi": 10000000.0, "price": 1.0, "field_support": None},
    ],
    "venues_covered": 3, "meta": {"normalized": True},
}


class TestSpec159VenueBreadth(unittest.TestCase):
    def setUp(self):
        self._restore = _patch_all()

    def tearDown(self):
        self._restore()

    def test_cashcat_shaped_fixture_size_book_shares_and_sort(self):
        BR.build_perpfinder = lambda mode, ticker=None, **kw: CASHCAT_BREADTH_FIXTURE
        vb = BR.build_brief("SKYAI")["venue_breadth"]
        self.assertTrue(vb["available"])
        self.assertEqual(vb["source"], "perpfinder")
        self.assertTrue(vb["normalized_rates"])
        self.assertEqual(vb["n_venues"], 9)
        # size_book = largest-OI venue = Hyperliquid
        self.assertEqual(vb["size_book"], "Hyperliquid")
        # sorted OI desc, null-oi (MEXC) last
        venue_order = [v["venue"] for v in vb["venues"]]
        self.assertEqual(venue_order[0], "Hyperliquid")
        self.assertEqual(venue_order[-1], "MEXC")
        mexc = next(v for v in vb["venues"] if v["venue"] == "MEXC")
        self.assertIsNone(mexc["oi"])
        self.assertIsNone(mexc["oi_share_pct"])
        # SPEC-187: every venue carries the display-only funding_pi_4h under its own
        # explicit name (was mislabeled "rate1h")
        hl = next(v for v in vb["venues"] if v["venue"] == "Hyperliquid")
        self.assertEqual(hl["funding_pi_4h"], 0.00196)
        self.assertIsNotNone(vb["total_oi_usd"])
        self.assertIsNotNone(vb["wired_oi_share_pct"])
        self.assertIsNotNone(vb["desk_blind_share_pct"])
        self.assertAlmostEqual(vb["wired_oi_share_pct"] + vb["desk_blind_share_pct"], 100.0, places=2)

    def test_total_oi_usd_is_the_straight_oi_sum_never_multiplied_by_price(self):
        """SPEC-174 #4 regression: PerpFinder's `oi` is already USD notional (live-verified
        2026-08-28 — BTC/Binance oi=$8.396B, which as token units would be 8.4B BTC against
        a ~19.8M supply). The old `oi * price` double-counted price and corrupted every
        total (H: total $1.13M vs its own Bybit row $8.29M)."""
        BR.build_perpfinder = lambda mode, ticker=None, **kw: CASHCAT_BREADTH_FIXTURE
        vb = BR.build_brief("SKYAI")["venue_breadth"]
        expected = sum(r["oi"] for r in CASHCAT_BREADTH_FIXTURE["rows"]
                       if r["oi"] is not None)   # straight sum, NOT sum(oi * price)
        self.assertAlmostEqual(vb["total_oi_usd"], expected, places=2)
        # the pre-fix formula would have produced a wildly different (much smaller) number
        wrong = sum(r["oi"] * r["price"] for r in CASHCAT_BREADTH_FIXTURE["rows"]
                    if r["oi"] is not None)
        self.assertNotAlmostEqual(vb["total_oi_usd"], wrong, places=2)

    def test_symbol_absent_from_matrix_block_omitted_not_in_matrix(self):
        BR.build_perpfinder = lambda mode, ticker=None, **kw: {"ok": True, "mode": mode, "rows": []}
        vb = BR.build_brief("SKYAI")["venue_breadth"]
        self.assertFalse(vb["available"])
        self.assertEqual(vb["reason"], "not_in_matrix")

    def test_provider_error_degrades_rest_of_brief_untouched(self):
        baseline = BR.build_brief("SKYAI")   # default stub (_patch_all) = not_in_matrix
        del baseline["venue_breadth"]
        BR.build_perpfinder = lambda mode, ticker=None, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        b = BR.build_brief("SKYAI")
        vb = b.pop("venue_breadth")
        self.assertFalse(vb["available"])
        self.assertIn("perpfinder error", vb["reason"])
        # every other section is byte-identical to a brief that never touched perpfinder
        self.assertEqual(b["state"], baseline["state"])
        self.assertEqual(b["perp"], baseline["perp"])
        self.assertEqual(b["books"], baseline["books"])
        self.assertEqual(b["onchain"], baseline["onchain"])

    def test_ok_false_from_perpfinder_degrades_gracefully(self):
        BR.build_perpfinder = lambda mode, ticker=None, **kw: {"ok": False, "mode": mode,
                                                                 "reason": "shape_drift"}
        vb = BR.build_brief("SKYAI")["venue_breadth"]
        self.assertFalse(vb["available"])
        self.assertEqual(vb["reason"], "shape_drift")

    def test_headline_warns_when_desk_blind_share_over_30(self):
        BR.build_perpfinder = lambda mode, ticker=None, **kw: BLIND_BREADTH_FIXTURE
        b = BR.build_brief("SKYAI")
        self.assertGreater(b["venue_breadth"]["desk_blind_share_pct"], 30)
        self.assertIn("off-desk", b["headline"])
        self.assertIn(b["venue_breadth"]["size_book"], b["headline"])

    def test_headline_silent_when_desk_blind_share_under_30(self):
        BR.build_perpfinder = lambda mode, ticker=None, **kw: CASHCAT_BREADTH_FIXTURE
        b = BR.build_brief("SKYAI")
        if b["venue_breadth"]["desk_blind_share_pct"] <= 30:
            self.assertNotIn("off-desk", b["headline"])

    def test_rate1h_never_reaches_perp_verdict_or_funding_leg_paths(self):
        """Doctrine guard: PerpFinder's rate is 1h-normalized and must never feed a verdict,
        §5 veto, floor detection, or funding_leg — by construction, `_perp_layer` never
        touches `build_perpfinder` at all, so none of its funding fields (the old `rate1h`
        name, nor its SPEC-187 replacements funding_pi_4h/rate_raw_pi) can appear in the
        perp/state sections regardless of what the venue_breadth fixture contains."""
        BR.build_perpfinder = lambda mode, ticker=None, **kw: CASHCAT_BREADTH_FIXTURE
        b = BR.build_brief("SKYAI")
        self.assertNotIn("rate1h", b["perp"])
        self.assertNotIn("rate1h", json.dumps(b["state"]))
        self.assertNotIn("rate1h", json.dumps(b["perp"]))
        self.assertNotIn("funding_pi_4h", json.dumps(b["state"]))
        self.assertNotIn("funding_pi_4h", json.dumps(b["perp"]))
        self.assertNotIn("rate_raw_pi", json.dumps(b["state"]))
        self.assertNotIn("rate_raw_pi", json.dumps(b["perp"]))


# SPEC-187 — ACE-shaped fixture: Bybit rate_raw_pi=-0.002575 (a fraction, same convention
# as every venue-direct fundingRate print) -> funding_pi_4h=-1.03%/4h via the SAME transform
# regime_flip.to_4h uses (raw*100, then *4 for a 60min interval) — reproduces the live
# 2026-09-01 incident where venue_breadth showed a bare, un-percent-scaled "rate1h -0.008838"
# against the funding layer's own -1.026%/4h for the same venue/moment.
ACE_BREADTH_FIXTURE = {
    "ok": True, "mode": "funding",
    "rows": [
        {"asset": "ACE", "venue": "Bybit", "funding_pi_4h": -1.03,
         "rate_raw_pi": -0.002575, "interval_min": 60, "normalized": True,
         "oi": 4200000.0, "price": 1.5, "field_support": None},
    ],
    "venues_covered": 1, "meta": {"normalized": True},
}


class TestSpec187VenueBreadthUnitsReconcileWithRegimeFlip(unittest.TestCase):
    def setUp(self):
        self._restore = _patch_all()

    def tearDown(self):
        self._restore()

    def test_ace_shaped_fixture_shows_normalized_percent_not_bare_fraction(self):
        BR.build_perpfinder = lambda mode, ticker=None, **kw: ACE_BREADTH_FIXTURE
        vb = BR.build_brief("ACE")["venue_breadth"]
        bybit = next(v for v in vb["venues"] if v["venue"] == "Bybit")
        self.assertEqual(bybit["funding_pi_4h"], -1.03)
        self.assertEqual(bybit["rate_raw_pi"], -0.002575)
        self.assertEqual(bybit["interval_min"], 60)
        # the reconciliation: venue_breadth's normalized print is the exact number
        # regime_flip.to_4h would produce from the same raw fraction/interval — one
        # canonical SPEC-112 transform, not a diverging parallel one.
        expected = BR.RF.to_4h(round(bybit["rate_raw_pi"] * 100, 6), bybit["interval_min"])
        self.assertAlmostEqual(bybit["funding_pi_4h"], expected)


# ── SPEC-191 #2: direct-OI fallback for venues perpfinder doesn't cover ─────────────
# OP-shaped fixture: Binance's `oi` is null in the perpfinder body even though Binance
# ran 2.5x Bybit's turnover in reality — the exact 2026-09-02 incident.
OP_BREADTH_FIXTURE_BINANCE_NULL = {
    "ok": True, "mode": "funding",
    "rows": [
        {"asset": "OP", "venue": "Bybit", "funding_pi_4h": 0.001, "normalized": True,
         "oi": 4_000_000.0, "price": 0.90, "field_support": None},
        {"asset": "OP", "venue": "Binance", "funding_pi_4h": 0.0012, "normalized": True,
         "oi": None, "price": 0.90, "field_support": {"oi": "unsupported", "price": "observed"}},
    ],
    "venues_covered": 2, "meta": {"normalized": True},
}


class TestSpec191VenueBreadthDirectOI(unittest.TestCase):
    def setUp(self):
        self._restore = _patch_all()   # DIRECT_OI_FETCHERS starts {} — tests opt individual venues in

    def tearDown(self):
        self._restore()

    def test_binance_null_oi_direct_fallback_qty_times_mark_flips_size_book(self):
        BR.build_perpfinder = lambda mode, ticker=None, **kw: OP_BREADTH_FIXTURE_BINANCE_NULL
        # Binance's direct fetcher reports a BASE quantity (10M OP) — the layer must
        # convert with the row's own mark price (0.90), never take the qty as USD.
        BR.DIRECT_OI_FETCHERS["binance"] = lambda sym: {"qty": 10_000_000.0, "usd": None}
        vb = BR.build_brief("SKYAI")["venue_breadth"]
        binance = next(v for v in vb["venues"] if v["venue"] == "Binance")
        self.assertEqual(binance["oi_source"], "direct")
        self.assertAlmostEqual(binance["oi"], 10_000_000.0 * 0.90)
        bybit = next(v for v in vb["venues"] if v["venue"] == "Bybit")
        self.assertEqual(bybit["oi_source"], "perpfinder")
        # Binance's direct-computed USD ($9M) now beats Bybit's perpfinder OI ($4M)
        self.assertEqual(vb["size_book"], "Binance")
        self.assertAlmostEqual(vb["total_oi_usd"], 9_000_000.0 + 4_000_000.0)

    def test_direct_endpoint_dead_stays_null_never_fabricated(self):
        BR.build_perpfinder = lambda mode, ticker=None, **kw: OP_BREADTH_FIXTURE_BINANCE_NULL
        BR.DIRECT_OI_FETCHERS["binance"] = lambda sym: None
        vb = BR.build_brief("SKYAI")["venue_breadth"]
        binance = next(v for v in vb["venues"] if v["venue"] == "Binance")
        self.assertIsNone(binance["oi"])
        self.assertIsNone(binance["oi_source"])
        self.assertEqual(vb["size_book"], "Bybit")

    def test_direct_fetcher_raising_degrades_that_venue_only(self):
        BR.build_perpfinder = lambda mode, ticker=None, **kw: OP_BREADTH_FIXTURE_BINANCE_NULL

        def boom(sym):
            raise RuntimeError("timeout")
        BR.DIRECT_OI_FETCHERS["binance"] = boom
        vb = BR.build_brief("SKYAI")["venue_breadth"]
        binance = next(v for v in vb["venues"] if v["venue"] == "Binance")
        self.assertIsNone(binance["oi"])
        bybit = next(v for v in vb["venues"] if v["venue"] == "Bybit")
        self.assertEqual(bybit["oi"], 4_000_000.0)   # the rest of the sweep is untouched

    def test_usd_native_venue_passes_through_without_multiplying_by_mark(self):
        """BingX/HTX already report USD notional — must never be re-multiplied by price
        (the SPEC-174 #4 double-count bug this spec explicitly guards against)."""
        fixture = {
            "ok": True, "mode": "funding",
            "rows": [
                {"asset": "OP", "venue": "BingX", "funding_pi_4h": 0.001, "normalized": True,
                 "oi": None, "price": 0.90, "field_support": None},
            ],
            "venues_covered": 1, "meta": {"normalized": True},
        }
        BR.build_perpfinder = lambda mode, ticker=None, **kw: fixture
        BR.DIRECT_OI_FETCHERS["bingx"] = lambda sym: {"qty": None, "usd": 1_234_567.0}
        vb = BR.build_brief("SKYAI")["venue_breadth"]
        bingx = next(v for v in vb["venues"] if v["venue"] == "BingX")
        self.assertEqual(bingx["oi"], 1_234_567.0)
        self.assertEqual(bingx["oi_source"], "direct")


# ── SPEC-191 #3: the risk-card line reads equity from the venue, loudly ────────────
class TestSpec191RiskCardLoudEquity(unittest.TestCase):
    """`_risk_card_layer` delegates to risk_card.live_risk_line -> resolve_equity, which
    is where the real historical bug lived (venue_account.equity()'s nested `data` shape
    silently never won). Exercised here through brief's own layer function, not a stub,
    so a regression in the wiring (not just risk_card.py's own unit tests) is caught."""

    def setUp(self):
        import types
        import sys as _sys
        import risk_card as RC
        self._fake = types.ModuleType("venue_account")
        self._sys = _sys
        self._RC = RC
        self._orig_maxsize = RC.resolve_maxsize_exit_usd
        RC.resolve_maxsize_exit_usd = lambda *a, **kw: None   # no live network in this test

    def tearDown(self):
        self._sys.modules.pop("venue_account", None)
        self._RC.resolve_maxsize_exit_usd = self._orig_maxsize

    def test_venue_equity_present_line_carries_risk_cap(self):
        self._fake.equity = lambda: {"ok": True, "data": {"perp_total_value_usd": 175.15}}
        self._sys.modules["venue_account"] = self._fake
        row = BR._risk_card_layer("ZTEST", {"direction": "LONG"}, equity_arg=None)
        # 175.15 * 5% = $8.76 (risk_usd) — the line rounds to whole dollars ($9)
        self.assertAlmostEqual(row["risk_usd"], 8.76, places=2)
        self.assertIn("risk cap 5% eq = $9 at stop", row["line"])

    def test_venue_equity_failing_names_the_reason(self):
        def _boom():
            raise RuntimeError("dead key")
        self._fake.equity = _boom
        self._sys.modules["venue_account"] = self._fake
        row = BR._risk_card_layer("ZTEST", {"direction": "LONG"}, equity_arg=None)
        self.assertIn("equity=unknown:", row["line"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
