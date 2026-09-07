#!/usr/bin/env python3
"""SPEC-119 — claim/distributor top-up tripwire.

Run:  python3 -m unittest tests.test_claim_topup

Fully offline/deterministic: fetch_fn/price_fn/catalyst_fn are injected (no network), and
state persistence is redirected to a tmp dir per test. The BARD-shaped fixture mirrors the
workshop case: a registered claim-distributor wallet funded twice ahead of claims opening.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

spec = importlib.util.spec_from_file_location("claim_topup", ROOT / "capabilities" / "claim_topup.py")
CT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CT)

DISTRIBUTOR = "0xdef0000000000000000000000000000000001"
TEAM_SAFE = "0x1230000000000000000000000000000000001"
UNKNOWN_SENDER = "0x9990000000000000000000000000000000001"
BARD_PRICE = 390_000 / 48_000_000   # ticket example: $390K on 48M tokens

BARD_WALLETS = {
    "tokens": {
        "BARD": {
            "decimals": 18,
            "circulating_supply": 1_000_000_000,
            "contracts": {"binance-smart-chain": "0xcontractbard"},
            "wallets": [
                {"label": "BARD-CLAIM-DISTRIBUTOR", "address": DISTRIBUTOR,
                 "chain": "binance-smart-chain", "kind": "claim-distributor", "tier": "distribution"},
                {"label": "BARD-TEAM-SAFE", "address": TEAM_SAFE,
                 "chain": "binance-smart-chain", "tier": "team"},
            ],
        },
        "NOREG": {
            "decimals": 18,
            "contracts": {"binance-smart-chain": "0xcontractnoreg"},
            "wallets": [
                {"label": "NOREG-SOME-SAFE", "address": "0xaaa0000000000000000000000000000000001",
                 "chain": "binance-smart-chain", "tier": "team"},
            ],
        },
    }
}


def _tx(to=DISTRIBUTOR, frm=UNKNOWN_SENDER, amount=48_000_000, ts="2026-07-10T00:00:00Z", h="0xtx1"):
    return {"to_address": to, "from_address": frm, "value_decimal": amount,
            "block_timestamp": ts, "hash": h}


def _fetch_fn(txs):
    def f(address, contract, chain_key, days, decimals):
        return list(txs), "fixture", False
    return f


def _price_fn(price):
    return lambda ticker: (price, "fixture")


def _catalyst_fn(catalyst):
    return lambda ticker, now: catalyst


class _TmpState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestRegistryGate(_TmpState):
    def test_no_registered_distributor_is_inert(self):
        out = CT.build_topup_state("NOREG", wallets=BARD_WALLETS, state_dir=self.state_dir,
                                   fetch_fn=_fetch_fn([_tx()]))
        self.assertEqual(out["checked"], 0)
        self.assertEqual(out["topups"], [])

    def test_unknown_ticker_is_inert(self):
        out = CT.build_topup_state("NOSUCHTICKER", wallets=BARD_WALLETS, state_dir=self.state_dir)
        self.assertEqual(out["checked"], 0)
        self.assertEqual(out["topups"], [])


class TestTwoTopUpsFire(_TmpState):
    def setUp(self):
        super().setUp()
        self.catalyst = {"type": "cliff", "date": "2026-07-15", "cg_id": "bard"}
        self.txs = [
            _tx(amount=48_000_000, ts="2026-07-10T00:00:00Z", h="0xtx1", frm=TEAM_SAFE),
            _tx(amount=30_000_000, ts="2026-07-11T00:00:00Z", h="0xtx2", frm=UNKNOWN_SENDER),
        ]

    def test_two_topups_above_floor_carry_source_size_float(self):
        out = CT.build_topup_state(
            "BARD", wallets=BARD_WALLETS, state_dir=self.state_dir,
            fetch_fn=_fetch_fn(self.txs), price_fn=_price_fn(BARD_PRICE),
            catalyst_fn=_catalyst_fn(self.catalyst))
        self.assertEqual(out["checked"], 1)
        self.assertEqual(len(out["topups"]), 2)
        first = out["topups"][0]
        self.assertAlmostEqual(first["usd"], 390_000, delta=1)
        self.assertAlmostEqual(first["pct_float"], 4.8, delta=0.01)
        self.assertIn("team safe", first["source_kind"])
        self.assertFalse(first["unscheduled"])
        self.assertEqual(first["catalyst"]["date"], "2026-07-15")
        second = out["topups"][1]
        self.assertEqual(second["source_kind"], "unknown/staging EOA")

    def test_fire_topup_alerts_emits_two_high_inbox_events(self):
        appended = []
        out = CT.fire_topup_alerts(
            tokens=["BARD"], wallets=BARD_WALLETS, state_dir=self.state_dir,
            append=lambda **kw: appended.append(kw),
            fetch_fn=_fetch_fn(self.txs), price_fn=_price_fn(BARD_PRICE),
            catalyst_fn=_catalyst_fn(self.catalyst))
        self.assertEqual(out["fired"], 2)
        self.assertEqual(len(appended), 2)
        for call in appended:
            self.assertEqual(call["severity"], "HIGH")
            self.assertEqual(call["ticker"], "BARD")
        msg = appended[0]["msg"]
        self.assertIn("CLAIM TOP-UP: BARD", msg)
        self.assertIn("390.0K", msg.replace("$", ""))
        self.assertIn("4.8%", msg)
        self.assertIn("team safe", msg)
        self.assertIn("2026-07-15", msg)

    def test_dedup_across_sweeps(self):
        first = CT.build_topup_state(
            "BARD", wallets=BARD_WALLETS, state_dir=self.state_dir, persist=True,
            fetch_fn=_fetch_fn(self.txs), price_fn=_price_fn(BARD_PRICE),
            catalyst_fn=_catalyst_fn(self.catalyst))
        self.assertEqual(len(first["topups"]), 2)
        second = CT.build_topup_state(
            "BARD", wallets=BARD_WALLETS, state_dir=self.state_dir, persist=True,
            fetch_fn=_fetch_fn(self.txs), price_fn=_price_fn(BARD_PRICE),
            catalyst_fn=_catalyst_fn(self.catalyst))
        self.assertEqual(second["topups"], [])  # already-seen txs never re-fire

    def test_recent_annotation_links_calendar_cliff(self):
        CT.build_topup_state(
            "BARD", wallets=BARD_WALLETS, state_dir=self.state_dir, persist=True,
            fetch_fn=_fetch_fn(self.txs), price_fn=_price_fn(BARD_PRICE),
            catalyst_fn=_catalyst_fn(self.catalyst), now=1_752_000_000)
        ann = CT.recent_annotation("BARD", state_dir=self.state_dir, now=1_752_000_000 + 3600)
        self.assertEqual(len(ann), 2)
        self.assertEqual(ann[0]["catalyst"]["date"], "2026-07-15")


class TestUnscheduled(_TmpState):
    def test_no_calendar_entry_flags_unscheduled(self):
        out = CT.build_topup_state(
            "BARD", wallets=BARD_WALLETS, state_dir=self.state_dir,
            fetch_fn=_fetch_fn([_tx(amount=48_000_000)]), price_fn=_price_fn(BARD_PRICE),
            catalyst_fn=_catalyst_fn(None))
        self.assertEqual(len(out["topups"]), 1)
        ev = out["topups"][0]
        self.assertTrue(ev["unscheduled"])
        self.assertIsNone(ev["catalyst"])
        msg = CT._format_msg(ev)
        self.assertIn("UNSCHEDULED", msg)


class TestBelowFloor(_TmpState):
    def test_small_inbound_never_fires(self):
        tiny = _tx(amount=1_000)   # ~$8 @ BARD_PRICE, ~0.0001% float — well under both floors
        out = CT.build_topup_state(
            "BARD", wallets=BARD_WALLETS, state_dir=self.state_dir,
            fetch_fn=_fetch_fn([tiny]), price_fn=_price_fn(BARD_PRICE),
            catalyst_fn=_catalyst_fn(None))
        self.assertEqual(out["topups"], [])


class TestOutboundIsNotATopup(_TmpState):
    def test_outbound_from_distributor_is_ignored(self):
        drain = _tx(to=UNKNOWN_SENDER, frm=DISTRIBUTOR, amount=48_000_000)
        out = CT.build_topup_state(
            "BARD", wallets=BARD_WALLETS, state_dir=self.state_dir,
            fetch_fn=_fetch_fn([drain]), price_fn=_price_fn(BARD_PRICE),
            catalyst_fn=_catalyst_fn(None))
        self.assertEqual(out["topups"], [])


class TestProviderDown(_TmpState):
    def test_provider_failure_reports_unavailable_not_false_quiet(self):
        def broken_fetch(address, contract, chain_key, days, decimals):
            raise RuntimeError("moralis quota exhausted")
        out = CT.build_topup_state(
            "BARD", wallets=BARD_WALLETS, state_dir=self.state_dir,
            fetch_fn=broken_fetch, price_fn=_price_fn(BARD_PRICE), catalyst_fn=_catalyst_fn(None))
        self.assertEqual(out["topups"], [])
        self.assertEqual(len(out["unavailable"]), 1)
        self.assertIn("moralis", out["unavailable"][0]["reason"])
        self.assertEqual(out["unavailable"][0]["address"], DISTRIBUTOR)


class TestUnlocksAnnotation(unittest.TestCase):
    """unlocks.build_unlocks carries the top-up annotation when one is live (injected seam)."""

    def setUp(self):
        uspec = importlib.util.spec_from_file_location("unlocks", ROOT / "capabilities" / "unlocks.py")
        self.unlocks = importlib.util.module_from_spec(uspec)
        uspec.loader.exec_module(self.unlocks)

    def test_build_unlocks_carries_claim_topup_annotation(self):
        fake_event = {"ticker": "BARD", "usd": 390_000, "pct_float": 4.8,
                      "catalyst": {"type": "cliff", "date": "2026-07-15"}, "unscheduled": False}

        def fetcher(cg_id):
            return {"emissions": []}

        def supply_fn(ticker, cg_id):
            return {"cg_id": cg_id, "circulating": 1_000_000_000, "total_supply": 1_000_000_000,
                    "price": BARD_PRICE, "vol_24h": 1_000_000}

        out = self.unlocks.build_unlocks(
            "BARD", fetcher=fetcher, supply_fn=supply_fn, seed=[],
            claim_topup_fn=lambda ticker, now: [fake_event])
        self.assertIn("claim_topup", out)
        self.assertEqual(len(out["claim_topup"]), 1)
        self.assertEqual(out["claim_topup"][0]["catalyst"]["date"], "2026-07-15")

    def test_build_unlocks_claim_topup_defaults_empty_when_none_live(self):
        def fetcher(cg_id):
            return {"emissions": []}

        def supply_fn(ticker, cg_id):
            return {"cg_id": cg_id, "circulating": 1_000_000_000, "total_supply": 1_000_000_000,
                    "price": 0.01, "vol_24h": 1_000_000}

        out = self.unlocks.build_unlocks(
            "NOPE", fetcher=fetcher, supply_fn=supply_fn, seed=[],
            claim_topup_fn=lambda ticker, now: [])
        self.assertEqual(out["claim_topup"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
