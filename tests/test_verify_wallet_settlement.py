#!/usr/bin/env python3
"""SPEC 53 — verify_wallet: detect swap settlements (quote-leg), stop calling
drip-sells "unknown distribution".

Run:  python3 tests/test_verify_wallet_settlement.py

Live miss: three ESPORTS wallets dripped ~200-token clips to one unknown hub → verdict
"DISTRIBUTING / distribution_mode: unknown" — the ~$530K USDT settlement leg (~2,500
micro-clips from one swap counterparty, a live TWAP drip-sell bot) was invisible to the
token-scoped read. Contract under test:
  - token-out clips mirrored by quote-in clips (same tx hash, or cadence/notional) →
    distribution_mode "dex_swap_sell" + settlement{asset, usd_in, n_settlements,
    counterparty, usd_out_after};
  - quote-in WITHOUT token-out → accumulating_quote (surfaced, not verdicted);
  - token-out only → current behavior unchanged;
  - quota/provider failure → settlement{available:false}, verdict untouched.
All transfers fixtures — offline, deterministic.
"""
import importlib.util
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


VW = _load("verify_wallet")

WALLET = "0xaaaa000000000000000000000000000000000001"
HUB = "0xbbbb000000000000000000000000000000000002"
SWAP = "0xcccc000000000000000000000000000000000003"


def tx(frm, to, val, ts, h=None):
    return {"from_address": frm, "to_address": to, "value_decimal": str(val),
            "block_timestamp": ts, "transaction_hash": h}


def token_clips(n=10, start_day=1):
    """n token-out clips of ~200 to one hub, one per hour."""
    return [tx(WALLET, HUB, 200, f"2026-06-0{start_day}T{8 + i % 12:02d}:00:00.000Z", h=f"0xh{i}")
            for i in range(n)]


def quote_clips(n=10, start_day=1, hash_match=False):
    """n USDT-in micro-settlements from one swap counterparty, same cadence."""
    return [tx(SWAP, WALLET, 210.0, f"2026-06-0{start_day}T{8 + i % 12:02d}:05:00.000Z",
               h=(f"0xh{i}" if hash_match else f"0xq{i}"))
            for i in range(n)]


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self._orig = (VW.WALLETS, VW.token_transfers, VW._quote_transfers, VW._live_price, VW.balance_of)
        VW.balance_of = lambda *a, **k: {"available": False}   # SPEC 58: no live RPC in tests
        VW.WALLETS = d / "tracked_wallets.json"
        VW.WALLETS.write_text(json.dumps({"tokens": {"ESPORTS": {
            "decimals": 18,
            "contracts": {"binance-smart-chain": "0xdef0000000000000000000000000000000000009"},
            "wallets": []}}}))
        VW._live_price = lambda tk: (1.0, "live")
        self.token_txs = []
        VW.token_transfers = lambda address, contract, chain_key, days=180, decimals=18: (
            list(self.token_txs), "moralis", False)
        self.quote = {}
        VW._quote_transfers = lambda address, chain_key, days: dict(self.quote)

    def tearDown(self):
        (VW.WALLETS, VW.token_transfers, VW._quote_transfers, VW._live_price, VW.balance_of) = self._orig
        self.dir.cleanup()

    def verify(self):
        return VW.build_verify(WALLET, "ESPORTS")


class TestSwapSell(_Base):
    def test_mirrored_clips_are_dex_swap_sell(self):
        self.token_txs = token_clips(10)
        self.quote = {"USDT": quote_clips(10)}
        v = self.verify()
        self.assertEqual(v["distribution_mode"], "dex_swap_sell")
        s = v["settlement"]
        self.assertTrue(s["available"])
        self.assertEqual(s["mode"], "dex_swap_sell")
        self.assertEqual(s["asset"], "USDT")
        self.assertEqual(s["n_settlements"], 10)
        self.assertAlmostEqual(s["usd_in"], 2100.0)
        self.assertEqual(s["counterparty"], SWAP)

    def test_hash_match_detects_even_few_clips(self):
        self.token_txs = token_clips(3)
        self.quote = {"USDT": quote_clips(3, hash_match=True)}
        v = self.verify()
        self.assertEqual(v["settlement"]["mode"], "dex_swap_sell")
        self.assertTrue(v["settlement"]["hash_matched"])

    def test_usd_out_after_tracks_proceeds_leaving(self):
        self.token_txs = token_clips(10, start_day=1)
        q = quote_clips(10, start_day=1)
        # proceeds leave AFTER the last token-out
        q.append(tx(WALLET, HUB, 1500.0, "2026-06-03T09:00:00.000Z"))
        self.quote = {"USDT": q}
        v = self.verify()
        self.assertAlmostEqual(v["settlement"]["usd_out_after"], 1500.0)


class TestNonSell(_Base):
    def test_token_out_only_unchanged(self):
        self.token_txs = token_clips(10)
        self.quote = {"USDT": []}
        v = self.verify()
        self.assertNotEqual(v["distribution_mode"], "dex_swap_sell")
        self.assertTrue(v["settlement"]["available"])
        self.assertIsNone(v["settlement"]["mode"])
        self.assertFalse(v["accumulating_quote"])

    def test_quote_in_without_token_out_is_accumulating_quote(self):
        # only inbound token history (no outs), but USDT streaming in → loading
        self.token_txs = [tx(HUB, WALLET, 1000, "2026-06-01T08:00:00.000Z")]
        self.quote = {"USDT": quote_clips(8)}
        v = self.verify()
        self.assertTrue(v["accumulating_quote"])
        self.assertNotEqual(v["distribution_mode"], "dex_swap_sell")
        self.assertNotEqual(v["verdict"], "DISTRIBUTING")   # surfaced, not verdicted

    def test_uncorrelated_sparse_quote_not_a_sell(self):
        # 10 token outs but only 2 quote-ins → no cadence match, no hashes
        self.token_txs = token_clips(10)
        self.quote = {"USDT": quote_clips(2)}
        v = self.verify()
        self.assertIsNone(v["settlement"]["mode"])


class TestDegrade(_Base):
    def test_quota_failure_degrades_never_blocks(self):
        self.token_txs = token_clips(10)
        def boom(address, chain_key, days):
            raise VW.MoralisError("quota")
        VW._quote_transfers = boom
        v = self.verify()
        self.assertTrue(v["available"])                     # main verdict path intact
        self.assertEqual(v["verdict"], "DISTRIBUTING")
        self.assertFalse(v["settlement"]["available"])

    def test_no_flows_skips_quote_leg_entirely(self):
        self.token_txs = []
        called = []
        VW._quote_transfers = lambda *a, **k: called.append(1) or {}
        v = self.verify()
        self.assertEqual(called, [])                        # quota discipline
        self.assertFalse(v["settlement"]["available"])

    # ── SPEC-80: a degraded/partial read must NEVER assert a clean DORMANT ──────────
    def test_partial_token_read_no_flows_is_degraded_not_dormant(self):
        # Live miss (0xffa8…): getlogs-partial token read returned n:0 → engine read
        # DORMANT while the full Moralis read was DISTRIBUTING -$33M→Bitget. A partial
        # read cannot ESTABLISH dormancy → surface DEGRADED + partial:true (§3).
        self.token_txs = []
        VW.token_transfers = lambda address, contract, chain_key, days=180, decimals=18: (
            [], "getlogs", True)
        v = self.verify()
        self.assertTrue(v["available"])
        self.assertEqual(v["verdict"], "DEGRADED")
        self.assertNotEqual(v["verdict"], "DORMANT")
        self.assertTrue(v["partial"])

    def test_partial_token_read_with_outs_carries_distributing(self):
        # getlogs-partial but the truncated window already shows token-out clips → carry
        # the known distributor forward, never downgrade to DORMANT off an incomplete read.
        self.token_txs = token_clips(10)
        VW.token_transfers = lambda address, contract, chain_key, days=180, decimals=18: (
            list(self.token_txs), "getlogs", True)
        v = self.verify()
        self.assertEqual(v["verdict"], "DISTRIBUTING")
        self.assertNotEqual(v["verdict"], "DORMANT")
        self.assertTrue(v["partial"])

    def test_full_read_sold_out_long_ago_stays_dormant(self):
        # regression: the SAME stale-out shape as the cited test but with a COMPLETE read
        # (no quota/partial) must still read DORMANT — the guard fires ONLY on a degrade.
        self.token_txs = token_clips(10)
        self.quote = {"USDT": []}            # quote leg succeeds → no degrade
        v = self.verify()
        self.assertFalse(v["partial"])
        self.assertEqual(v["verdict"], "DORMANT")

    def test_full_read_no_flows_is_dormant(self):
        # regression: zero flows on a complete read = genuinely dormant.
        self.token_txs = []
        v = self.verify()
        self.assertFalse(v["partial"])
        self.assertEqual(v["verdict"], "DORMANT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
