#!/usr/bin/env python3
"""SPEC-115 — DEX execution detector: visible sells by tracked-cluster wallets.

§8's "the SELL is off-chain/invisible" is a CEX-only truth; a DEX sell IS visible
execution (direction, size, pool, tx hash, timestamp all on the public ledger).
This suite is offline-deterministic throughout: size_swap_hits/exploit_dump_flag
are pure; discover_fresh_children/build_execution are exercised via injected
transfers_fn/nonce_fn/quote_fn stubs — no network, no quota spend.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import dex_execution as DX

POOL = "0x238a358808379702088667322f80ac48bad5e6c4"   # router-1 (shared plumbing, still a valid venue)
POOL2 = "0xb300000b72deaeb607a12d5f54773d1c19c7028d"   # router-2 (apparatus-tier)
TRACKED_ADDR = "0xaaaa000000000000000000000000000000aaaa"
CHILD_ADDR = "0xbbbb000000000000000000000000000000bbbb"
CONTRACT = "0xtoken00000000000000000000000000000000"
CHAIN = "binance-smart-chain"


def _tx(from_addr, to_addr, value, h, ts):
    return {"from_address": from_addr, "to_address": to_addr, "value_decimal": value,
            "transaction_hash": h, "block_timestamp": ts}


class KnownDexAddressesTests(unittest.TestCase):
    def test_includes_fingerprinted_routers(self):
        addrs = DX.known_dex_addresses()
        # router-1 is labelled pancakeswap in known_entities.json (DEX_HINTS match)
        self.assertIn(POOL, addrs)
        # router-2 is tagged kind=='router' in tracked_wallets.json
        self.assertIn(POOL2, addrs)


class SizeSwapHitsTests(unittest.TestCase):
    def test_tracked_wallet_sell_sized_off_stable_leg(self):
        outs = [_tx(TRACKED_ADDR, POOL, 1_000_000, "0xh1", "2026-07-08T11:42:00.000Z")]
        ins = []
        quote_ins = [_tx(POOL, TRACKED_ADDR, 412_000, "0xh1", "2026-07-08T11:42:00.000Z")]
        quote_outs = []
        hits = DX.size_swap_hits(TRACKED_ADDR, outs, ins, quote_ins, quote_outs, {POOL})
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual(h["direction"], DX.SELL)
        self.assertEqual(h["usd"], 412_000)
        self.assertEqual(h["pool"], POOL)
        self.assertEqual(h["tx_hash"], "0xh1")
        self.assertEqual(h["timestamp"], "2026-07-08T11:42:00.000Z")

    def test_buy_side_direction(self):
        outs = []
        ins = [_tx(POOL, TRACKED_ADDR, 500_000, "0xh2", "2026-07-08T12:00:00.000Z")]
        quote_ins = []
        quote_outs = [_tx(TRACKED_ADDR, POOL, 200_000, "0xh2", "2026-07-08T12:00:00.000Z")]
        hits = DX.size_swap_hits(TRACKED_ADDR, outs, ins, quote_ins, quote_outs, {POOL})
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["direction"], DX.BUY)
        self.assertEqual(hits[0]["usd"], 200_000)

    def test_below_threshold_no_hit(self):
        outs = [_tx(TRACKED_ADDR, POOL, 1_000, "0xh3", "2026-07-08T11:42:00.000Z")]
        quote_ins = [_tx(POOL, TRACKED_ADDR, 1_000, "0xh3", "2026-07-08T11:42:00.000Z")]
        hits = DX.size_swap_hits(TRACKED_ADDR, outs, [], quote_ins, [], {POOL},
                                 cfg={**DX.DEFAULT_CFG, "min_usd": 50_000})
        self.assertEqual(hits, [])

    def test_no_quote_leg_match_no_hit(self):
        # transfer to a dex address with NO correlated quote-leg tx — never guess a size
        outs = [_tx(TRACKED_ADDR, POOL, 1_000_000, "0xh4", "2026-07-08T11:42:00.000Z")]
        hits = DX.size_swap_hits(TRACKED_ADDR, outs, [], [], [], {POOL})
        self.assertEqual(hits, [])

    def test_not_a_dex_address_no_hit(self):
        outs = [_tx(TRACKED_ADDR, "0xrandom", 1_000_000, "0xh5", "2026-07-08T11:42:00.000Z")]
        quote_ins = [_tx("0xrandom", TRACKED_ADDR, 900_000, "0xh5", "2026-07-08T11:42:00.000Z")]
        hits = DX.size_swap_hits(TRACKED_ADDR, outs, [], quote_ins, [], {POOL})
        self.assertEqual(hits, [])


class DiscoverFreshChildrenTests(unittest.TestCase):
    def test_young_nonce_recipient_is_a_child(self):
        txs = [_tx(TRACKED_ADDR, CHILD_ADDR, 50_000, "0xf1", "2026-07-01T00:00:00.000Z")]
        transfers_fn = lambda addr, contract, ck, days: (txs, "test", False)
        nonce_fn = lambda addr: 3   # young
        children = DX.discover_fresh_children(TRACKED_ADDR, "TRACKED-LABEL", CONTRACT, CHAIN,
                                              transfers_fn, nonce_fn)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["address"], CHILD_ADDR)
        self.assertEqual(children[0]["parent"], TRACKED_ADDR)
        self.assertEqual(children[0]["parent_label"], "TRACKED-LABEL")

    def test_old_nonce_recipient_is_not_a_child(self):
        txs = [_tx(TRACKED_ADDR, CHILD_ADDR, 50_000, "0xf1", "2026-07-01T00:00:00.000Z")]
        transfers_fn = lambda addr, contract, ck, days: (txs, "test", False)
        nonce_fn = lambda addr: 5000   # not fresh
        children = DX.discover_fresh_children(TRACKED_ADDR, "TRACKED-LABEL", CONTRACT, CHAIN,
                                              transfers_fn, nonce_fn)
        self.assertEqual(children, [])

    def test_provider_failure_is_empty_not_exception(self):
        def boom(addr, contract, ck, days):
            raise RuntimeError("provider down")
        children = DX.discover_fresh_children(TRACKED_ADDR, "L", CONTRACT, CHAIN, boom, lambda a: 1)
        self.assertEqual(children, [])


class ExploitDumpFlagTests(unittest.TestCase):
    def test_large_inflow_before_sell_flags(self):
        sell_hit = {"timestamp": "2026-07-08T12:00:00.000Z"}
        ins = [_tx("0xexploit", CHILD_ADDR, 2_000_000, "0xin1", "2026-07-08T10:00:00.000Z")]
        flag = DX.exploit_dump_flag(sell_hit, ins, float_supply=100_000_000)
        self.assertIsNotNone(flag)
        self.assertEqual(flag["tx_hash"], "0xin1")
        self.assertAlmostEqual(flag["pct_float"], 2.0)

    def test_small_inflow_does_not_flag(self):
        sell_hit = {"timestamp": "2026-07-08T12:00:00.000Z"}
        ins = [_tx("0xexploit", CHILD_ADDR, 100, "0xin1", "2026-07-08T10:00:00.000Z")]
        flag = DX.exploit_dump_flag(sell_hit, ins, float_supply=100_000_000)
        self.assertIsNone(flag)

    def test_inflow_after_sell_does_not_flag(self):
        sell_hit = {"timestamp": "2026-07-08T10:00:00.000Z"}
        ins = [_tx("0xexploit", CHILD_ADDR, 2_000_000, "0xin1", "2026-07-08T12:00:00.000Z")]
        flag = DX.exploit_dump_flag(sell_hit, ins, float_supply=100_000_000)
        self.assertIsNone(flag)

    def test_no_float_supply_never_flags(self):
        sell_hit = {"timestamp": "2026-07-08T12:00:00.000Z"}
        ins = [_tx("0xexploit", CHILD_ADDR, 2_000_000, "0xin1", "2026-07-08T10:00:00.000Z")]
        flag = DX.exploit_dump_flag(sell_hit, ins, float_supply=None)
        self.assertIsNone(flag)


class FormatExecutionLineTests(unittest.TestCase):
    def test_tracked_sell_line(self):
        hit = {"direction": DX.SELL, "usd": 412_000, "pool": POOL, "tx_hash": "0xabc",
               "timestamp": "2026-07-08T11:42:00.000Z", "sender": TRACKED_ADDR,
               "attribution": {"kind": "tracked", "cluster": "XTOKEN", "parent": None}}
        line = DX.format_execution_line(hit)
        self.assertTrue(line.startswith("EXECUTION:"))
        self.assertIn("SOLD", line)
        self.assertIn("$412K", line)
        self.assertIn("cluster: XTOKEN", line)
        self.assertIn(POOL, line)
        self.assertIn("0xabc", line)

    def test_fresh_child_line_cites_parent(self):
        hit = {"direction": DX.SELL, "usd": 100_000, "pool": POOL, "tx_hash": "0xabc",
               "timestamp": "2026-07-08T11:42:00.000Z", "sender": CHILD_ADDR,
               "attribution": {"kind": "fresh_child", "cluster": "XTOKEN", "parent": TRACKED_ADDR}}
        line = DX.format_execution_line(hit)
        self.assertIn("fresh child of", line)
        self.assertIn(TRACKED_ADDR, line)

    def test_exploit_dump_flag_rendered(self):
        hit = {"direction": DX.SELL, "usd": 100_000, "pool": POOL, "tx_hash": "0xabc",
               "timestamp": "2026-07-08T11:42:00.000Z", "sender": CHILD_ADDR, "attribution": None,
               "exploit_dump": {"tx_hash": "0xin1", "pct_float": 2.0, "timestamp": "2026-07-08T10:00:00.000Z"}}
        line = DX.format_execution_line(hit)
        self.assertIn("EXPLOIT_DUMP_PATTERN?", line)


class BuildExecutionTests(unittest.TestCase):
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

    def test_tracked_wallet_sell_hit_end_to_end(self):
        txs = {TRACKED_ADDR: [_tx(TRACKED_ADDR, POOL, 1_000_000, "0xh1", "2026-07-08T11:42:00.000Z")]}
        quote = {TRACKED_ADDR: {"USDT": [_tx(POOL, TRACKED_ADDR, 412_000, "0xh1", "2026-07-08T11:42:00.000Z")]}}
        transfers_fn, nonce_fn, quote_fn = self._stubs(txs, {}, quote)
        out = DX.build_execution("LABX", contract=CONTRACT, chain_key=CHAIN,
                                 tracked={TRACKED_ADDR: "XTOKEN-CLUSTER"}, dex_addrs={POOL},
                                 transfers_fn=transfers_fn, nonce_fn=nonce_fn, quote_fn=quote_fn)
        self.assertTrue(out["available"])
        self.assertEqual(out["n_hits"], 1)
        h = out["hits"][0]
        self.assertEqual(h["direction"], DX.SELL)
        self.assertEqual(h["usd"], 412_000)
        self.assertEqual(h["attribution"]["kind"], "tracked")
        self.assertEqual(h["attribution"]["cluster"], "XTOKEN-CLUSTER")
        self.assertIn("EXECUTION:", out["lines"][0])

    def test_fresh_child_sell_attributed_to_parent_cluster(self):
        txs = {
            TRACKED_ADDR: [_tx(TRACKED_ADDR, CHILD_ADDR, 50_000, "0xfund", "2026-07-01T00:00:00.000Z")],
            CHILD_ADDR: [_tx(CHILD_ADDR, POOL2, 300_000, "0xh2", "2026-07-08T09:00:00.000Z")],
        }
        quote = {CHILD_ADDR: {"USDT": [_tx(POOL2, CHILD_ADDR, 150_000, "0xh2", "2026-07-08T09:00:00.000Z")]}}
        transfers_fn, nonce_fn, quote_fn = self._stubs(txs, {CHILD_ADDR: 2}, quote)
        out = DX.build_execution("LABX", contract=CONTRACT, chain_key=CHAIN,
                                 tracked={TRACKED_ADDR: "XTOKEN-CLUSTER"}, dex_addrs={POOL2},
                                 transfers_fn=transfers_fn, nonce_fn=nonce_fn, quote_fn=quote_fn)
        self.assertTrue(out["available"])
        hit = next(h for h in out["hits"] if h["sender"] == CHILD_ADDR)
        self.assertEqual(hit["attribution"]["kind"], "fresh_child")
        self.assertEqual(hit["attribution"]["cluster"], "XTOKEN-CLUSTER")
        self.assertEqual(hit["attribution"]["parent"], TRACKED_ADDR)

    def test_exploit_dump_via_extra_wallet_tip(self):
        exploited = "0xcccc000000000000000000000000000000cccc"
        txs = {
            exploited: [
                _tx("0xexploiter", exploited, 2_000_000, "0xin1", "2026-07-08T10:00:00.000Z"),
                _tx(exploited, POOL, 2_000_000, "0xh3", "2026-07-08T11:00:00.000Z"),
            ]
        }
        quote = {exploited: {"USDT": [_tx(POOL, exploited, 900_000, "0xh3", "2026-07-08T11:00:00.000Z")]}}
        transfers_fn, nonce_fn, quote_fn = self._stubs(txs, {}, quote)
        out = DX.build_execution("LABX", contract=CONTRACT, chain_key=CHAIN, tracked={},
                                 dex_addrs={POOL}, extra_wallets={exploited: None},
                                 transfers_fn=transfers_fn, nonce_fn=nonce_fn, quote_fn=quote_fn,
                                 float_supply=100_000_000)
        self.assertTrue(out["available"])
        self.assertEqual(out["n_hits"], 1)
        self.assertIn("exploit_dump", out["hits"][0])
        self.assertIn("EXPLOIT_DUMP_PATTERN?", out["lines"][0])

    def test_provider_unavailable_never_a_clean_bill(self):
        def boom(addr, contract, ck, days):
            raise RuntimeError("provider down")
        out = DX.build_execution("LABX", contract=CONTRACT, chain_key=CHAIN,
                                 tracked={TRACKED_ADDR: "X"}, dex_addrs={POOL},
                                 transfers_fn=boom, nonce_fn=lambda a: None, quote_fn=lambda a, c, d: {})
        self.assertFalse(out["available"])
        self.assertIn("reason", out)

    def test_below_threshold_swap_is_no_hit(self):
        txs = {TRACKED_ADDR: [_tx(TRACKED_ADDR, POOL, 100, "0xh1", "2026-07-08T11:42:00.000Z")]}
        quote = {TRACKED_ADDR: {"USDT": [_tx(POOL, TRACKED_ADDR, 100, "0xh1", "2026-07-08T11:42:00.000Z")]}}
        transfers_fn, nonce_fn, quote_fn = self._stubs(txs, {}, quote)
        out = DX.build_execution("LABX", contract=CONTRACT, chain_key=CHAIN,
                                 tracked={TRACKED_ADDR: "X"}, dex_addrs={POOL},
                                 transfers_fn=transfers_fn, nonce_fn=nonce_fn, quote_fn=quote_fn,
                                 cfg={**DX.DEFAULT_CFG, "min_usd": 50_000})
        self.assertTrue(out["available"])
        self.assertEqual(out["n_hits"], 0)

    def test_untracked_token_is_unavailable(self):
        out = DX.build_execution("NOPETOKEN_XYZ")
        self.assertFalse(out["available"])


class RotationFreshnessThirdLegTests(unittest.TestCase):
    """SPEC-115 point 3: a qualifying DEX-execution sell by a tracked wallet or fresh
    child, while the tracked top holder's last_out_ts is frozen, is a third independent
    rotation-evidence leg that flips FROZEN -> ROTATED — reusing rotation_freshness's
    own verdict core, not a re-derivation."""

    def test_frozen_plus_dex_sell_flips_rotated_via_default_leg(self):
        import rotation_freshness as RF

        fake_hit = {"direction": DX.SELL, "usd": 300_000, "pool": POOL2, "tx_hash": "0xh2",
                    "timestamp": "2026-07-08T09:00:00.000Z", "sender": CHILD_ADDR,
                    "attribution": {"kind": "fresh_child", "cluster": "XTOKEN-CLUSTER",
                                    "parent": TRACKED_ADDR}}
        orig_build = DX.build_execution
        DX.build_execution = lambda ticker: {"available": True, "ticker": ticker,
                                             "n_hits": 1, "hits": [fake_hit],
                                             "lines": [DX.format_execution_line(fake_hit)]}
        try:
            dex_leg = RF._default_dex_leg("LABX")
        finally:
            DX.build_execution = orig_build

        self.assertTrue(dex_leg["available"])
        self.assertTrue(dex_leg["suspect"])
        self.assertEqual(dex_leg["n_sells"], 1)

        r = RF.classify_freshness("2026-07-06T00:00:00.000Z",
                                  {"available": True, "dump_legs": 0}, {"available": False},
                                  dex=dex_leg, now=1_784_000_000.0, live_short_thesis=True)
        self.assertEqual(r["verdict"], "ROTATED")
        self.assertIn("DEX EXECUTION", r["line"])
        self.assertIn("fresh child of", r["line"])


if __name__ == "__main__":
    unittest.main()
