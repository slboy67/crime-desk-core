#!/usr/bin/env python3
"""SPEC-117 — drip/DCA-pattern seller detector: metronomic micro-selling evades the
per-swap floor (SPEC-115). ESPORTS sold ~$530K via micro-swaps invisible to any single-
swap USD floor; this fingerprints the cumulative pattern per wallet instead.

Offline-deterministic throughout: fingerprint_drip/channel_executions_* are pure;
build_drip is exercised via injected transfers_fn/nonce_fn/quote_fn stubs.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import drip_seller as DR

POOL = "0x238a358808379702088667322f80ac48bad5e6c4"       # router-1 (shared plumbing)
CEX_ADDR = "0xacd03d601e5bb1b275bb94076ff46ed9d753435a"     # Binance 1 (known_entities.json)
WALLET = "0xdddd000000000000000000000000000000dddd"
CHILD_ADDR = "0xbbbb000000000000000000000000000000bbbb"
TRACKED_ADDR = "0xaaaa000000000000000000000000000000aaaa"
CONTRACT = "0xtoken00000000000000000000000000000000"
CHAIN = "binance-smart-chain"


def _tx(from_addr, to_addr, value, h, ts):
    return {"from_address": from_addr, "to_address": to_addr, "value_decimal": value,
            "transaction_hash": h, "block_timestamp": ts}


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


BASE_TS = 1_784_000_000.0   # 2026-07-08-ish


def _regular_execs(n, gap_sec, usd_each, start=BASE_TS, channel="pool", channel_kind="dex-swap"):
    return [{"usd": usd_each, "timestamp": _iso(start + i * gap_sec), "channel": channel,
             "channel_kind": channel_kind} for i in range(n)]


class FingerprintDripTests(unittest.TestCase):
    def test_esports_shaped_drip_fires(self):
        # 60 micro-swaps, ~48min apart, ~$8833 each (well under SPEC-115's $50K floor),
        # cumulative ~$530K over ~48h, one pool.
        n, gap, each = 60, 2880, 530_000 / 60
        execs = _regular_execs(n, gap, each)
        for e in execs:
            self.assertLess(e["usd"], 50_000)   # proves the complementarity vs SPEC-115's floor
        hit = DR.fingerprint_drip(WALLET, DR.SELL, POOL, "dex-swap", execs)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["verdict"], "DRIP_SELLER")
        self.assertEqual(hit["tx_count"], 60)
        self.assertAlmostEqual(hit["cumulative_usd"], 530_000, delta=1)
        self.assertEqual(hit["median_gap_sec"], gap)
        self.assertGreater(hit["run_rate_usd_per_day"], 0)

    def test_drip_buyer_mirror(self):
        n, gap, each = 8, 3600, 10_000
        execs = _regular_execs(n, gap, each)
        hit = DR.fingerprint_drip(WALLET, DR.BUY, POOL, "dex-swap", execs,
                                  cfg={**DR.DEFAULT_CFG, "cumulative_floor_usd": 50_000})
        self.assertIsNotNone(hit)
        self.assertEqual(hit["verdict"], "DRIP_BUYER")

    def test_irregular_human_selling_no_hit(self):
        # same count as the ESPORTS fixture, but wildly irregular gaps/sizes.
        gaps = [60, 9000, 300, 15000, 500, 200, 20000, 100, 1200, 30000] * 6
        sizes = [1000, 42000, 800, 38000, 1500, 900, 45000, 700, 2200, 40000] * 6
        ts = BASE_TS
        execs = []
        for g, s in zip(gaps, sizes):
            ts += g
            execs.append({"usd": s, "timestamp": _iso(ts), "channel": "pool", "channel_kind": "dex-swap"})
        hit = DR.fingerprint_drip(WALLET, DR.SELL, POOL, "dex-swap", execs)
        self.assertIsNone(hit)

    def test_below_cumulative_floor_no_hit(self):
        n, gap, each = 6, 3600, 1_000   # regular cadence/sizes but only $6K total
        execs = _regular_execs(n, gap, each)
        hit = DR.fingerprint_drip(WALLET, DR.SELL, POOL, "dex-swap", execs)
        self.assertIsNone(hit)

    def test_below_min_n_no_hit(self):
        n, gap, each = 3, 3600, 20_000   # regular + big enough $ but too few txs
        execs = _regular_execs(n, gap, each)
        hit = DR.fingerprint_drip(WALLET, DR.SELL, POOL, "dex-swap", execs)
        self.assertIsNone(hit)


class ChannelExecutionsTests(unittest.TestCase):
    def test_dex_swap_executions_no_floor(self):
        outs = [_tx(WALLET, POOL, 1000, "0xh1", _iso(BASE_TS))]
        quote_ins = [_tx(POOL, WALLET, 900, "0xh1", _iso(BASE_TS))]
        execs = DR.channel_executions_dex(WALLET, outs, [], quote_ins, [], {POOL})
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]["usd"], 900)
        self.assertEqual(execs[0]["channel"], POOL)
        self.assertEqual(execs[0]["channel_kind"], "dex-swap")

    def test_cex_deposit_executions_priced(self):
        outs = [_tx(WALLET, CEX_ADDR, 500, "0xh2", _iso(BASE_TS))]
        execs = DR.channel_executions_cex(WALLET, outs, {CEX_ADDR}, price=2.0)
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]["usd"], 1000.0)
        self.assertEqual(execs[0]["channel_kind"], "cex-deposit")

    def test_cex_deposit_unpriced_has_no_usd(self):
        outs = [_tx(WALLET, CEX_ADDR, 500, "0xh2", _iso(BASE_TS))]
        execs = DR.channel_executions_cex(WALLET, outs, {CEX_ADDR}, price=None)
        self.assertIsNone(execs[0]["usd"])


class FormatDripLineTests(unittest.TestCase):
    def test_swap_drip_line(self):
        n, gap, each = 61, 2880, 530_000 / 61
        execs = _regular_execs(n, gap, each)
        hit = DR.fingerprint_drip(WALLET, DR.SELL, POOL, "dex-swap", execs)
        hit["attribution"] = None
        line = DR.format_drip_line(hit)
        self.assertTrue(line.startswith("EXECUTION (drip):"))
        self.assertIn("sold", line)
        self.assertIn("$530K", line)
        self.assertIn("61 micro-swaps", line)
        self.assertIn(f"via pool {POOL}", line)
        self.assertIn("run-rate", line)
        self.assertNotIn("positioning cadence", line)

    def test_cex_deposit_drip_line_distinguishes_positioning(self):
        n, gap, each = 8, 3600, 10_000
        execs = _regular_execs(n, gap, each, channel=CEX_ADDR, channel_kind="cex-deposit")
        hit = DR.fingerprint_drip(WALLET, DR.SELL, CEX_ADDR, "cex-deposit", execs,
                                  cfg={**DR.DEFAULT_CFG, "cumulative_floor_usd": 50_000})
        hit["attribution"] = None
        line = DR.format_drip_line(hit)
        self.assertIn(f"via CEX deposit {CEX_ADDR}", line)
        self.assertIn("positioning cadence, not executed", line)
        self.assertIn("micro-deposits", line)


class BuildDripTests(unittest.TestCase):
    def _stubs(self, txs_by_wallet, nonce_by_wallet, quote_by_wallet):
        def transfers_fn(addr, contract, ck, days):
            if addr not in txs_by_wallet:
                raise RuntimeError("no data for wallet")
            return txs_by_wallet[addr], "test", False

        def nonce_fn(addr):
            return nonce_by_wallet.get(addr)

        def quote_fn(addr, ck, days):
            return quote_by_wallet.get(addr, {})

        return transfers_fn, nonce_fn, quote_fn

    def test_tracked_wallet_drip_hit_end_to_end(self):
        n, gap, each = 60, 2880, 530_000 / 60
        txs, quote_asset = [], []
        for i in range(n):
            h = f"0xh{i}"
            ts = _iso(BASE_TS + i * gap)
            txs.append(_tx(TRACKED_ADDR, POOL, each, h, ts))
            quote_asset.append(_tx(POOL, TRACKED_ADDR, each, h, ts))
        transfers_fn, nonce_fn, quote_fn = self._stubs(
            {TRACKED_ADDR: txs}, {}, {TRACKED_ADDR: {"USDT": quote_asset}})
        out = DR.build_drip("LABX", contract=CONTRACT, chain_key=CHAIN,
                            tracked={TRACKED_ADDR: "XTOKEN-CLUSTER"}, dex_addrs={POOL}, cex_addrs=set(),
                            transfers_fn=transfers_fn, nonce_fn=nonce_fn, quote_fn=quote_fn)
        self.assertTrue(out["available"])
        self.assertEqual(out["n_hits"], 1)
        h = out["hits"][0]
        self.assertEqual(h["verdict"], "DRIP_SELLER")
        self.assertEqual(h["attribution"]["kind"], "tracked")
        self.assertIn("EXECUTION (drip):", out["lines"][0])

    def test_fresh_child_cex_deposit_drip(self):
        fund = [_tx(TRACKED_ADDR, CHILD_ADDR, 50_000, "0xfund", _iso(BASE_TS - 100 * 3600))]
        n, gap, each = 8, 3600, 10_000
        deposit_txs = [_tx(CHILD_ADDR, CEX_ADDR, each, f"0xd{i}", _iso(BASE_TS + i * gap)) for i in range(n)]
        txs_by_wallet = {TRACKED_ADDR: fund, CHILD_ADDR: fund + deposit_txs}
        transfers_fn, nonce_fn, quote_fn = self._stubs(txs_by_wallet, {CHILD_ADDR: 2}, {})
        out = DR.build_drip("LABX", contract=CONTRACT, chain_key=CHAIN,
                            tracked={TRACKED_ADDR: "XTOKEN-CLUSTER"}, dex_addrs=set(), cex_addrs={CEX_ADDR},
                            price=2.0, transfers_fn=transfers_fn, nonce_fn=nonce_fn, quote_fn=quote_fn,
                            cfg={**DR.DEFAULT_CFG, "cumulative_floor_usd": 50_000})
        self.assertTrue(out["available"])
        hit = next(h for h in out["hits"] if h["channel_kind"] == "cex-deposit")
        self.assertEqual(hit["verdict"], "DRIP_SELLER")
        self.assertEqual(hit["attribution"]["kind"], "fresh_child")
        self.assertEqual(hit["attribution"]["parent"], TRACKED_ADDR)

    def test_provider_unavailable_never_a_clean_bill(self):
        def boom(addr, contract, ck, days):
            raise RuntimeError("provider down")
        out = DR.build_drip("LABX", contract=CONTRACT, chain_key=CHAIN,
                            tracked={TRACKED_ADDR: "X"}, dex_addrs={POOL}, cex_addrs=set(),
                            transfers_fn=boom, nonce_fn=lambda a: None, quote_fn=lambda a, c, d: {})
        self.assertFalse(out["available"])
        self.assertIn("reason", out)

    def test_untracked_token_is_unavailable(self):
        out = DR.build_drip("NOPETOKEN_XYZ")
        self.assertFalse(out["available"])


class RotationFreshnessFourthLegTests(unittest.TestCase):
    """SPEC-117 point 4: a DRIP_SELLER hit from a tracked wallet or fresh child counts as
    rotation-evidence exactly like SPEC-115's large-swap leg (FROZEN -> ROTATED input)."""

    def test_frozen_plus_drip_sell_flips_rotated_via_default_leg(self):
        import rotation_freshness as RF

        fake_hit = {"verdict": "DRIP_SELLER", "wallet": CHILD_ADDR, "direction": DR.SELL,
                    "channel": POOL, "channel_kind": "dex-swap", "cumulative_usd": 300_000,
                    "tx_count": 40, "median_gap_sec": 2000, "first_ts": _iso(BASE_TS),
                    "last_ts": _iso(BASE_TS + 40 * 2000), "run_rate_usd_per_day": 150_000,
                    "attribution": {"kind": "fresh_child", "cluster": "XTOKEN-CLUSTER", "parent": TRACKED_ADDR}}
        orig_build = DR.build_drip
        DR.build_drip = lambda ticker, **kw: {"available": True, "ticker": ticker, "n_hits": 1,
                                              "hits": [fake_hit], "lines": [DR.format_drip_line(fake_hit)]}
        try:
            drip_leg = RF._default_drip_leg("LABX")
        finally:
            DR.build_drip = orig_build

        self.assertTrue(drip_leg["available"])
        self.assertTrue(drip_leg["suspect"])
        self.assertEqual(drip_leg["n_hits"], 1)

        r = RF.classify_freshness("2026-07-06T00:00:00.000Z",
                                  {"available": True, "dump_legs": 0}, {"available": False},
                                  drip=drip_leg, now=1_784_000_000.0, live_short_thesis=True)
        self.assertEqual(r["verdict"], "ROTATED")
        self.assertIn("DRIP", r["line"])
        self.assertIn("fresh child of", r["line"])


if __name__ == "__main__":
    unittest.main()
