#!/usr/bin/env python3
"""SPEC 51 — watchlist on-chain mapping is an INVARIANT: sweep mode + auto-onboard.

Run:  python3 tests/test_onboard_sweep.py

User pain (verbatim): pulling a coin says "onchain unmapped and then it has to do it
while i have to wait and the entry window sometimes disappears". Contract under test:
  - onboard '{"all":true}' maps every watchlist name missing from tracked_wallets in
    one call; idempotent on re-run; a per-token failure never aborts the sweep;
  - board_tick runs the sweep cheaply each tick: pure set-difference first — ZERO
    network calls when nothing is unmapped;
  - brief on an unmapped ticker triggers the mechanical onboard inline AND still
    returns the perp legs; onboard failure surfaces as UNMAPPED(onboard_failed:<why>),
    never a bare "unmapped" where nothing was attempted;
  - the SPEC-47 hard guard stands: candidates stay unclassified.
All seams monkeypatched — offline, deterministic.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, sub="capabilities"):
    spec = importlib.util.spec_from_file_location(name, ROOT / sub / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OB = _load("onboard")

CG = {"cg_id": "x", "low_confidence": False, "name": "X",
      "contracts": {"binance-smart-chain": "0xabc0000000000000000000000000000000000001"}}


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig = (OB.WALLETS, OB.WL_PATH, OB._cg_resolve, OB._cg_search,
                      OB._holders, OB._decimals, OB._seed_baseline)
        OB.WALLETS = d / "tracked_wallets.json"
        OB.WL_PATH = d / "watchlist.json"
        OB.WALLETS.write_text(json.dumps(
            {"rpcs": {}, "tokens": {"OLD": {"contracts": {}, "wallets": []}}}))
        OB.WL_PATH.write_text(json.dumps(
            {"tokens": [{"ticker": "OLD"}, {"ticker": "AAA"},
                        {"ticker": "BBB"}, {"ticker": "CCC"}]}))
        OB._cg_resolve = lambda tk, cg_id=None: dict(CG)
        OB._cg_search = lambda tk: []
        OB._holders = lambda c, ch: {"available": False, "reason": "none"}
        OB._decimals = lambda cg_id, plat: 18
        OB._seed_baseline = lambda tk: {"baseline_seeded": True}

    def tearDown(self):
        (OB.WALLETS, OB.WL_PATH, OB._cg_resolve, OB._cg_search,
         OB._holders, OB._decimals, OB._seed_baseline) = self._orig
        self.dir.cleanup()

    def cfg(self):
        return json.loads(OB.WALLETS.read_text())


class TestSweep(_Tmp):
    def test_sweep_maps_all_unmapped_one_call(self):
        self.assertEqual(OB.unmapped_tickers(), ["AAA", "BBB", "CCC"])
        out = OB.build_sweep()
        self.assertEqual(out["mapped"], ["AAA", "BBB", "CCC"])
        self.assertEqual(out["failed"], [])
        self.assertTrue(set(["AAA", "BBB", "CCC"]) <= set(self.cfg()["tokens"]))

    def test_sweep_idempotent(self):
        OB.build_sweep()
        out2 = OB.build_sweep()
        self.assertEqual(out2["swept"], 0)          # nothing left unmapped
        self.assertEqual(OB.unmapped_tickers(), [])

    def test_per_token_failure_does_not_abort(self):
        def resolve(tk, cg_id=None):
            if tk == "BBB":
                raise RuntimeError("rate limited")
            return dict(CG)
        OB._cg_resolve = resolve
        out = OB.build_sweep()
        self.assertEqual(out["mapped"], ["AAA", "CCC"])
        self.assertEqual(out["failed"], ["BBB"])
        bbb = next(r for r in out["results"] if r["ticker"] == "BBB")
        self.assertEqual(bbb["status"], "failed")

    def test_sweep_candidates_stay_unclassified(self):
        OB._holders = lambda c, ch: {"available": True, "holders": [
            {"address": "0x1111111111111111111111111111111111111111", "percent": 10.0,
             "tag": None, "is_contract": False, "is_locked": False, "is_burn": False}]}
        OB.build_sweep()
        for tk in ("AAA", "BBB", "CCC"):
            for w in self.cfg()["tokens"][tk]["wallets"]:
                self.assertEqual(w["tier"], "unclassified")


# attrs patched on the SHARED `onboard` module (board_tick/brief `import onboard` hits
# sys.modules — unlike the isolated _load("onboard") instance) — must be restored
_SHARED_ATTRS = ("WALLETS", "WL_PATH", "_cg_resolve", "_cg_search", "_holders",
                 "_decimals", "_seed_baseline", "build_sweep", "build_onboard",
                 "unmapped_tickers")


class _SharedOnboardPatch(_Tmp):
    def patch_shared(self, mod):
        self._shared = mod
        self._shared_orig = {a: getattr(mod, a) for a in _SHARED_ATTRS}
        mod.WALLETS = OB.WALLETS
        mod.WL_PATH = OB.WL_PATH
        mod._cg_resolve = lambda tk, cg_id=None: dict(CG)
        mod._cg_search = lambda tk: []
        mod._holders = lambda c, ch: {"available": False, "reason": "none"}
        mod._decimals = lambda cg_id, plat: 18
        mod._seed_baseline = lambda tk: {"baseline_seeded": True}

    def tearDown(self):
        for a, v in getattr(self, "_shared_orig", {}).items():
            setattr(self._shared, a, v)
        super().tearDown()


class TestBoardTickSweep(_SharedOnboardPatch):
    def setUp(self):
        super().setUp()
        self.BT = _load("board_tick", sub="ops")
        d = Path(self.dir.name)
        self._bt_orig = (self.BT.BASELINE_PATH, self.BT.LOCK_PATH, self.BT.ERR_PATH,
                         self.BT.NOTIFY_STATE_PATH)
        self.BT.BASELINE_PATH = d / "board_last.json"
        self.BT.LOCK_PATH = d / "board_tick.lock"
        self.BT.ERR_PATH = d / "board_tick.err"
        self.BT.NOTIFY_STATE_PATH = d / "notify_sent.json"   # SPEC-111: isolate the dedup state too
        self.patch_shared(self.BT.onboard)

    def tearDown(self):
        (self.BT.BASELINE_PATH, self.BT.LOCK_PATH, self.BT.ERR_PATH,
         self.BT.NOTIFY_STATE_PATH) = self._bt_orig
        super().tearDown()

    def test_tick_maps_unmapped_then_zero_network(self):
        rows = [{"ticker": "AAA", "verdict": "CONFIRMS", "price_leg": None}]
        out1 = self.BT.tick(classify_fn=lambda: rows, notify_fn=lambda t, m: None)
        self.assertEqual(out1["onboard_sweep"]["unmapped"], 3)
        self.assertIn("AAA", self.cfg()["tokens"])
        # second tick: nothing unmapped → the sweep body must never run (zero network)
        def deny():
            raise AssertionError("sweep ran with nothing unmapped")
        self.BT.onboard.build_sweep = deny
        out2 = self.BT.tick(classify_fn=lambda: rows, notify_fn=lambda t, m: None)
        self.assertEqual(out2["onboard_sweep"], {"unmapped": 0})

    def test_sweep_failure_never_kills_the_tick(self):
        def boom():
            raise RuntimeError("coingecko down")
        self.BT.onboard.build_sweep = boom
        rows = [{"ticker": "AAA", "verdict": "CONFIRMS", "price_leg": None}]
        out = self.BT.tick(classify_fn=lambda: rows, notify_fn=lambda t, m: None)
        self.assertIn("error", out["onboard_sweep"])
        self.assertTrue(self.BT.BASELINE_PATH.exists())     # tick itself completed


class TestBriefAutoOnboard(_SharedOnboardPatch):
    def setUp(self):
        super().setUp()
        self.BR = _load("brief")
        self.patch_shared(self.BR.onboard)
        # quiet, deterministic layers
        self.BR.load_watchlist = lambda: ([{"ticker": "AAA", "state": "watch"}], None)
        self.BR.live_perp = lambda tk: None
        self.BR.classify_token = lambda tok, live: {"verdict": "CONFIRMS", "reason": "r",
                                                    "thesis_present": False, "direction": "?"}
        self.BR.build_analyse = lambda tk: {"verdict": "WATCH", "price": 1.0}
        self.BR.build_depth = lambda tk, v=None: {"venues": {}}
        self.BR.build_onchain = lambda tk: {"signal": "UNTRACKED", "bias": "?", "score": 0,
                                            "nonces": {}, "concentration": {}}
        self.BR.fetch_aster_symbols = lambda: None   # SPEC-136: unknown — no live network in tests

    def test_brief_triggers_mapping_and_keeps_perp(self):
        calls = []
        self.BR.onboard.build_onboard = lambda tk, **kw: (calls.append(tk) or {"ok": True})
        b = self.BR.build_brief("AAA")
        self.assertEqual(calls, ["AAA"])                    # mapping attempted inline
        self.assertTrue(b["perp"]["available"])             # perp legs never blocked

    def test_brief_onboard_failure_is_explicit_not_bare_unmapped(self):
        self.BR.onboard.build_onboard = lambda tk, **kw: {"ok": False, "reason": "resolve_ambiguous"}
        b = self.BR.build_brief("AAA")
        self.assertTrue(b["perp"]["available"])
        self.assertIn("UNMAPPED(onboard_failed:resolve_ambiguous)",
                      json.dumps(b["onchain"]))

    def test_brief_mapped_ticker_skips_onboard(self):
        def deny(tk, **kw):
            raise AssertionError("onboard attempted on a mapped ticker")
        self.BR.onboard.build_onboard = deny
        b = self.BR.build_brief("OLD")                      # OLD is tracked in the fixture
        self.assertTrue(b["perp"]["available"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
