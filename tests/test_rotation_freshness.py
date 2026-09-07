#!/usr/bin/env python3
"""SPEC-98 — rotation-aware distribution freshness: FRESH / FROZEN / ROTATED.

Run:  python3 tests/test_rotation_freshness.py

The VELVET-DWF lesson: DWF rotated supply through FRESH wallets between pump legs, so the
tracked top holder's last_out_ts FROZE while distribution continued — the engine reported
"distribution stopped" (a false pause) exactly when the desk was deciding whether to bank.
Three-state verdict: FRESH (tracked selling) / FROZEN (genuine pause → bank/exit) /
ROTATED (pause is FALSE — dump legs on tape or fresh-wallet→CEX flow continue).

Offline-deterministic: both evidence legs are injected fixtures; state dir is a tmp path;
the inbox event fn is a spy.
"""
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))


def _load(name, alias=None):
    spec = importlib.util.spec_from_file_location(alias or name, ROOT / "capabilities" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


RF = _load("rotation_freshness")

NOW = 1_780_000_000.0


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _bar(force, d_price_pct):
    return {"oi_force": force, "d_price_pct": d_price_pct, "price": 1.0}


def _dump_run(n=3, move=-0.6):
    return [_bar("longs_closing", move) for _ in range(n)]


TAPE_QUIET = {"available": True, "dump_legs": 0, "detail": "no dump legs"}
TAPE_DUMPING = {"available": True, "dump_legs": 2, "detail": "2 dump legs (longs_closing runs)"}
OC_QUIET = {"available": True, "suspect": False, "n_fresh_wallets": 0, "usd_to_cex": 0.0}
OC_ROTATING = {"available": True, "suspect": True, "n_fresh_wallets": 3, "usd_to_cex": 410_000.0,
               "detail": "3 fresh wallets → CEX $410K"}
OC_UNAVAILABLE = {"available": False, "reason": "provider unavailable"}
DEX_UNAVAILABLE = {"available": False, "reason": "not evaluated"}
DEX_QUIET = {"available": True, "suspect": False, "n_sells": 0, "lines": []}
DEX_ROTATING = {"available": True, "suspect": True, "n_sells": 1,
                "lines": ["EXECUTION: 0xbbbb (cluster: XTOKEN, fresh child of 0xaaaa) SOLD "
                          "$412K via pool 0x238a3588, tx 0xabc, 07-08 11:42"]}


class TestClassifyFreshness(unittest.TestCase):
    def test_velvet_shape_is_rotated_not_frozen(self):
        # DoD (a): tracked wallet frozen + fresh-wallet CEX flow present → ROTATED
        r = RF.classify_freshness(_iso(NOW - 30 * 3600), TAPE_QUIET, OC_ROTATING,
                                  now=NOW, live_short_thesis=True)
        self.assertEqual(r["verdict"], "ROTATED")
        self.assertIn("fresh wallets", r["line"])
        self.assertIn("ROTATED", r["line"])
        self.assertIn("$410K", r["line"])
        # the pause is FALSE — never the affirmative bank/exit note
        self.assertNotIn("bank/exit", (r.get("note") or "").lower())

    def test_tape_leg_alone_flips_rotated(self):
        # rotation evidence: EITHER leg flips FROZEN → ROTATED (tape needs no on-chain quota)
        r = RF.classify_freshness(_iso(NOW - 30 * 3600), TAPE_DUMPING, OC_UNAVAILABLE, now=NOW)
        self.assertEqual(r["verdict"], "ROTATED")
        self.assertIn("dump legs", r["line"])

    def test_both_quiet_is_frozen_with_bank_note_on_live_short(self):
        # DoD (b): both legs quiet → FROZEN + the VELVET bank/exit discipline note
        r = RF.classify_freshness(_iso(NOW - 30 * 3600), TAPE_QUIET, OC_QUIET,
                                  now=NOW, live_short_thesis=True)
        self.assertEqual(r["verdict"], "FROZEN")
        self.assertIn("bank/exit", (r.get("note") or "").lower())
        self.assertIn("squeeze", (r.get("note") or "").lower())

    def test_tracked_actively_selling_is_fresh(self):
        # DoD (c): recent tracked outflow → FRESH regardless of the other legs
        r = RF.classify_freshness(_iso(NOW - 2 * 3600), TAPE_QUIET, OC_QUIET, now=NOW)
        self.assertEqual(r["verdict"], "FRESH")
        self.assertIn("FRESH", r["line"])

    def test_provider_absent_frozen_carries_unavailable_caveat(self):
        # DoD (d): provider absent + tape quiet → FROZEN with onchain_leg=unavailable, NO
        # false confidence (no bank note — the pause is NOT confirmed)
        r = RF.classify_freshness(_iso(NOW - 30 * 3600), TAPE_QUIET, OC_UNAVAILABLE,
                                  now=NOW, live_short_thesis=True)
        self.assertEqual(r["verdict"], "FROZEN")
        self.assertEqual(r["onchain_leg"].get("available"), False)
        self.assertIn("unavailable", r["line"])
        self.assertNotIn("bank/exit", (r.get("note") or "").lower())

    def test_no_tracked_ts_and_quiet_is_frozen(self):
        r = RF.classify_freshness(None, TAPE_QUIET, OC_QUIET, now=NOW)
        self.assertEqual(r["verdict"], "FROZEN")

    def test_dex_leg_alone_flips_rotated(self):
        # SPEC-115: a qualifying DEX-execution sell by a tracked wallet/fresh child is the
        # THIRD rotation-evidence leg — flips FROZEN → ROTATED on its own (tape/onchain quiet).
        r = RF.classify_freshness(_iso(NOW - 30 * 3600), TAPE_QUIET, OC_QUIET, dex=DEX_ROTATING,
                                  now=NOW, live_short_thesis=True)
        self.assertEqual(r["verdict"], "ROTATED")
        self.assertIn("DEX EXECUTION", r["line"])
        self.assertNotIn("bank/exit", (r.get("note") or "").lower())

    def test_dex_leg_cited_senior_to_onchain_leg(self):
        # both legs fire — the DEX-execution leg (confirmed) is cited first, ahead of the
        # CEX-inference leg (memory: feedback_predictor_vs_cause_dex_sale_is_execution).
        r = RF.classify_freshness(_iso(NOW - 30 * 3600), TAPE_QUIET, OC_ROTATING, dex=DEX_ROTATING,
                                  now=NOW, live_short_thesis=True)
        self.assertEqual(r["verdict"], "ROTATED")
        dex_pos = r["line"].find("DEX EXECUTION")
        cex_pos = r["line"].find("fresh wallets")
        self.assertGreater(dex_pos, -1)
        self.assertGreater(cex_pos, -1)
        self.assertLess(dex_pos, cex_pos)

    def test_dex_leg_unavailable_adds_caveat_on_frozen(self):
        r = RF.classify_freshness(_iso(NOW - 30 * 3600), TAPE_QUIET, OC_QUIET, dex=DEX_UNAVAILABLE,
                                  now=NOW, live_short_thesis=True)
        self.assertEqual(r["verdict"], "FROZEN")
        self.assertIn("dex_leg=unavailable", r["line"])


class TestTapeLeg(unittest.TestCase):
    def test_dump_legs_counted_from_longs_closing_runs(self):
        bars = (_dump_run(3, -0.6) + [_bar("shorts_opening", 0.1)] * 2
                + _dump_run(4, -0.5) + [_bar("mixed", 0.0)])
        leg = RF.tape_leg_from_bars(bars)
        self.assertTrue(leg["available"])
        self.assertEqual(leg["dump_legs"], 2)

    def test_short_runs_and_small_moves_do_not_count(self):
        bars = (_dump_run(2, -0.9)                      # run too short
                + [_bar("shorts_closing", 0.2)]
                + _dump_run(3, -0.01))                  # net move too small
        leg = RF.tape_leg_from_bars(bars)
        self.assertEqual(leg["dump_legs"], 0)

    def test_no_bars_is_unavailable_not_quiet(self):
        # a missing tape read is NOT "tape quiet" (§3 — never fabricate a clean datum)
        self.assertFalse(RF.tape_leg_from_bars([])["available"])
        self.assertFalse(RF.tape_leg_from_bars(None)["available"])


class TestOnchainLeg(unittest.TestCase):
    """Fresh-wallet → CEX flow scan: senders NOT tracked + young + sized vs float."""

    def setUp(self):
        self.contract = "0xc0ffee"
        self.tracked = {"0xtracked1"}
        self.channels = [{"address": "0xcexhot1", "label": "Binance hot"}]
        self.nonces = {"0xfresh1": 4, "0xfresh2": 9, "0xfresh3": 1, "0xoldmm": 2_000_000}

        def transfers_fn(address, contract, chain_key, days=2):
            rows = [
                {"from_address": "0xfresh1", "to_address": "0xcexhot1", "value_decimal": 900_000.0,
                 "block_timestamp": _iso(NOW - 3600)},
                {"from_address": "0xfresh2", "to_address": "0xcexhot1", "value_decimal": 700_000.0,
                 "block_timestamp": _iso(NOW - 5400)},
                {"from_address": "0xfresh3", "to_address": "0xcexhot1", "value_decimal": 450_000.0,
                 "block_timestamp": _iso(NOW - 7200)},
                {"from_address": "0xtracked1", "to_address": "0xcexhot1", "value_decimal": 500_000.0,
                 "block_timestamp": _iso(NOW - 3000)},      # tracked → excluded (that's the FRESH leg)
                {"from_address": "0xoldmm", "to_address": "0xcexhot1", "value_decimal": 800_000.0,
                 "block_timestamp": _iso(NOW - 3000)},      # high-nonce MM churn → not a fresh wallet
            ]
            return rows, "moralis", False

        self.transfers_fn = transfers_fn
        self.nonce_fn = lambda addr, chain_key="binance-smart-chain": self.nonces.get(addr.lower())

    def test_fresh_wallet_cex_flow_detected(self):
        leg = RF.fresh_wallet_cex_leg("VELVET", self.contract, "binance-smart-chain",
                                      self.tracked, self.channels,
                                      transfers_fn=self.transfers_fn, nonce_fn=self.nonce_fn,
                                      price=0.2, now=NOW)
        self.assertTrue(leg["available"])
        self.assertTrue(leg["suspect"])
        self.assertEqual(leg["n_fresh_wallets"], 3)
        self.assertAlmostEqual(leg["usd_to_cex"], (900_000 + 700_000 + 450_000) * 0.2)

    def test_provider_down_reports_unavailable_not_clean(self):
        def dead(address, contract, chain_key, days=2):
            raise RuntimeError("all providers failed")
        leg = RF.fresh_wallet_cex_leg("VELVET", self.contract, "binance-smart-chain",
                                      self.tracked, self.channels,
                                      transfers_fn=dead, nonce_fn=self.nonce_fn, price=0.2, now=NOW)
        self.assertFalse(leg["available"])

    def test_unsized_flow_is_not_suspect(self):
        # no price and no float share → cannot size vs float → never a confident ROTATED
        leg = RF.fresh_wallet_cex_leg("VELVET", self.contract, "binance-smart-chain",
                                      self.tracked, self.channels,
                                      transfers_fn=self.transfers_fn, nonce_fn=self.nonce_fn,
                                      price=None, now=NOW)
        self.assertFalse(leg["suspect"])


class TestBuildFreshnessAndTransition(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.events = []

        def event_fn(ts, ticker, source, severity, msg):
            self.events.append({"ts": ts, "ticker": ticker, "source": source,
                                "severity": severity, "msg": msg})

        self.event_fn = event_fn

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, tape, onchain, dex=DEX_UNAVAILABLE, live_short=True, now=NOW):
        return RF.build_freshness("VELVET", last_out_ts=_iso(NOW - 30 * 3600),
                                  tape=tape, onchain=onchain, dex=dex, live_short_thesis=live_short,
                                  now=now, state_dir=self.state_dir, event_fn=self.event_fn)

    def test_frozen_to_rotated_fires_one_high_event(self):
        r1 = self._build(TAPE_QUIET, OC_QUIET)
        self.assertEqual(r1["verdict"], "FROZEN")
        self.assertEqual(self.events, [])
        r2 = self._build(TAPE_QUIET, OC_ROTATING, now=NOW + 600)
        self.assertEqual(r2["verdict"], "ROTATED")
        self.assertTrue(r2["transition"])
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["severity"], "HIGH")
        self.assertEqual(self.events[0]["source"], "rotation_freshness")
        self.assertIn("ROTATED", self.events[0]["msg"])
        # steady-state ROTATED → no re-fire every read
        r3 = self._build(TAPE_QUIET, OC_ROTATING, now=NOW + 1200)
        self.assertEqual(len(self.events), 1)
        self.assertFalse(r3["transition"])

    def test_no_event_without_live_thesis(self):
        self._build(TAPE_QUIET, OC_QUIET, live_short=False)
        self._build(TAPE_QUIET, OC_ROTATING, live_short=False, now=NOW + 600)
        self.assertEqual(self.events, [])

    def test_fresh_skips_both_legs_entirely(self):
        # quota discipline: a FRESH tracked leg answers the question — never spend the legs
        called = []

        def boom_tape():
            called.append("tape")
            return TAPE_DUMPING

        def boom_onchain():
            called.append("onchain")
            return OC_ROTATING

        def boom_dex():
            called.append("dex")
            return DEX_ROTATING

        r = RF.build_freshness("VELVET", last_out_ts=_iso(NOW - 3600),
                               tape_fn=boom_tape, onchain_fn=boom_onchain, dex_fn=boom_dex,
                               live_short_thesis=True, now=NOW,
                               state_dir=self.state_dir, event_fn=self.event_fn)
        self.assertEqual(r["verdict"], "FRESH")
        self.assertEqual(called, [])

    def test_state_persisted_for_the_board(self):
        self._build(TAPE_QUIET, OC_ROTATING)
        st = RF.read_state("VELVET", state_dir=self.state_dir)
        self.assertEqual(st["verdict"], "ROTATED")
        self.assertIn("distribution: ROTATED", st["line"])


class TestBoardAndBriefSurface(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_board_reason_carries_verdict_on_live_short_thesis(self):
        RF.build_freshness("VELVET", last_out_ts=_iso(NOW - 30 * 3600),
                           tape=TAPE_QUIET, onchain=OC_ROTATING, dex=DEX_UNAVAILABLE, live_short_thesis=True,
                           now=NOW, state_dir=self.state_dir, event_fn=lambda *a: None)
        CL = _load("classify", alias="classify_rf_t")
        rows = [{"ticker": "VELVET", "direction": "SHORT", "thesis_present": True,
                 "verdict": "CONFIRMS", "reason": "funding leg consistent"},
                {"ticker": "OTHER", "direction": "LONG", "thesis_present": True,
                 "verdict": "CONFIRMS", "reason": "unchanged"}]
        CL.annotate_freshness(rows, state_dir=self.state_dir, now=NOW + 60)
        self.assertIn("distribution: ROTATED", rows[0]["reason"])
        self.assertIn("fresh wallets", rows[0]["reason"])
        self.assertNotIn("distribution:", rows[1]["reason"])   # no distribution thesis → untouched

    def test_stale_state_does_not_annotate(self):
        RF.build_freshness("VELVET", last_out_ts=_iso(NOW - 30 * 3600),
                           tape=TAPE_QUIET, onchain=OC_ROTATING, dex=DEX_UNAVAILABLE, live_short_thesis=True,
                           now=NOW, state_dir=self.state_dir, event_fn=lambda *a: None)
        CL = _load("classify", alias="classify_rf_t2")
        rows = [{"ticker": "VELVET", "direction": "SHORT", "thesis_present": True,
                 "verdict": "CONFIRMS", "reason": "funding leg consistent"}]
        CL.annotate_freshness(rows, state_dir=self.state_dir, now=NOW + 40 * 3600)
        self.assertNotIn("distribution:", rows[0]["reason"])

    def test_brief_onchain_layer_passes_verdict_through(self):
        BR = _load("brief", alias="brief_rf_t")
        fresh_block = {"verdict": "ROTATED",
                       "line": "distribution: ROTATED — top holder frozen but 3 fresh wallets → CEX $410K"}
        orig = BR.build_onchain
        BR.build_onchain = lambda t: {"signal": "QUIET", "bias": "NEUTRAL", "score": 0,
                                      "nonces": {}, "concentration": {},
                                      "distribution_freshness": fresh_block}
        try:
            layer = BR._onchain_layer("VELVET")
        finally:
            BR.build_onchain = orig
        self.assertEqual(layer["distribution_freshness"]["verdict"], "ROTATED")

    def test_brief_headline_carries_rotated_line(self):
        BR = _load("brief", alias="brief_rf_t2")
        onchain = {"available": True, "signal": "QUIET", "newly_fired": [],
                   "distribution_freshness": {
                       "verdict": "ROTATED",
                       "line": "distribution: ROTATED — top holder frozen but 3 fresh wallets → CEX $410K"}}
        h = BR._headline("VELVET", {"available": False}, {"available": False},
                         {"available": False}, onchain)
        self.assertIn("distribution: ROTATED", h)


class TestVerifyWalletSurface(unittest.TestCase):
    def test_attach_freshness_adds_block_and_line(self):
        VW = _load("verify_wallet", alias="verify_wallet_rf_t")
        v = {"available": True, "token": "VELVET", "address": "0xabc", "verdict": "DORMANT",
             "last_out_ts": _iso(NOW - 30 * 3600)}
        block = {"verdict": "ROTATED", "line": "distribution: ROTATED — …", "note": None}
        out = VW.attach_freshness(v, freshness_fn=lambda ticker, last_out_ts: block)
        self.assertEqual(out["distribution_freshness"]["verdict"], "ROTATED")

    def test_attach_freshness_never_raises(self):
        VW = _load("verify_wallet", alias="verify_wallet_rf_t2")
        v = {"available": True, "token": "VELVET", "address": "0xabc", "verdict": "DORMANT",
             "last_out_ts": None}

        def boom(ticker, last_out_ts):
            raise RuntimeError("no network")
        out = VW.attach_freshness(v, freshness_fn=boom)
        self.assertNotIn("distribution_freshness", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
