#!/usr/bin/env python3
"""SPEC-89 — the on-chain QUIET signal must not hide a DISTRIBUTING top holder.

The nonce/signal layer reads the staging/nonce layer, not the actual token transfers,
so it reports QUIET/DORMANT for names whose top holders are actively distributing (the
BLESS 0x73d8 escrow + H 0x28e2ea cases: onchain QUIET, verify_wallet DISTRIBUTING).
This suite locks:
  - `_top_holder_distribution` verify_wallets the top holders, aggregates the verdicts,
    and is Moralis-quota-aware (degrades to checked:False, never burns calls on empty);
  - `build_onchain` flips the headline off bare QUIET when a top holder is distributing,
    sets `distribution_checked` + `signal_caveat` + a `top_holder_distribution` block;
  - `brief._onchain_layer` surfaces the block (a one-call read can't show QUIET while
    the operator dumps);
  - the `verify_wallet` orchestrator invoke maps `{ticker,wallet}` (the desk's natural
    naming) → `<address> --token <TKR>` (the SPEC-89 wiring bug);
  - `analyse`'s §4 long gate flips to PASS/TRAP when a top holder distributes under
    deep-neg funding (NOT squeeze-fuel).

Deterministic: verify_wallet, concentration, the nonce spine, the heavy layers, and the
Moralis quota meter are all monkeypatched — no network.
"""
import importlib.util
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))
import onchain as O   # noqa: E402
import brief as B     # noqa: E402

spec = importlib.util.spec_from_file_location("analyse_cap", ROOT / "capabilities" / "analyse.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)


def conc(holders, available=True):
    return {"available": available, "source": "goplus", "chain": "binance-smart-chain",
            "holder_count": 1000, "top1_pct": 40.0, "top10_pct": 70.0, "holders": holders}


def H(addr, pct, tag=None, is_burn=False, is_contract=False):
    return {"address": addr, "percent": pct, "tag": tag, "is_burn": is_burn,
            "is_contract": is_contract, "is_locked": False}


def vw(verdict, last_out="2026-06-22T10:00:00Z", dests=None, distributing=None):
    return {"available": True, "verdict": verdict, "last_out_ts": last_out,
            "distributing": (verdict == "DISTRIBUTING") if distributing is None else distributing,
            "sell_destinations": dests or [{"address": "0xdex", "label": "PancakeRouter",
                                            "dest_kind": "dex-execution", "amount": 5e5, "count": 9}]}


class TopHolderDistributionUnit(unittest.TestCase):
    def setUp(self):
        self._q = O.moralis_quota.status
        O.moralis_quota.status = lambda *a, **k: {"exhausted": False, "calls": 0, "prefix": ""}

    def tearDown(self):
        O.moralis_quota.status = self._q

    def test_distributing_top_holder_detected(self):
        calls = []

        def fake(addr, token, **k):
            calls.append(addr.lower())
            return vw("DISTRIBUTING") if addr.lower() == "0x73d8" else vw("DORMANT")
        r = O._top_holder_distribution("BLESS", conc([H("0x73d8", 30.0), H("0xaaa", 10.0)]),
                                       verify_fn=fake)
        self.assertTrue(r["checked"])
        self.assertTrue(r["distributing"])
        top = next(h for h in r["holders"] if h["address"].lower() == "0x73d8")
        self.assertEqual(top["verdict"], "DISTRIBUTING")
        self.assertEqual(top["last_out_ts"], "2026-06-22T10:00:00Z")
        self.assertTrue(top.get("sell_destinations"))
        self.assertIn("0x73d8", calls)

    def test_all_clean_holders_not_distributing(self):
        r = O._top_holder_distribution("X", conc([H("0xaaa", 30.0), H("0xbbb", 10.0)]),
                                       verify_fn=lambda a, t, **k: vw("DORMANT"))
        self.assertTrue(r["checked"])
        self.assertFalse(r["distributing"])

    def test_quota_exhausted_degrades_without_burning_calls(self):
        O.moralis_quota.status = lambda *a, **k: {"exhausted": True, "calls": 99999, "prefix": "[QUOTA 100%]"}
        calls = []
        r = O._top_holder_distribution("X", conc([H("0x73d8", 30.0)]),
                                       verify_fn=lambda a, t, **k: calls.append(a) or vw("DISTRIBUTING"))
        self.assertFalse(r["checked"])
        self.assertFalse(r["distributing"])
        self.assertEqual(calls, [])

    def test_no_holders_no_calls(self):
        calls = []
        r = O._top_holder_distribution("X", conc([]),
                                       verify_fn=lambda a, t, **k: calls.append(a) or vw("DISTRIBUTING"))
        self.assertFalse(r["checked"])
        self.assertEqual(calls, [])

    def test_burn_and_exchange_tagged_holders_skipped(self):
        calls = []

        def fake(addr, token, **k):
            calls.append(addr.lower())
            return vw("DORMANT")
        O._top_holder_distribution(
            "X", conc([H("0xdead", 50.0, is_burn=True),
                       H("0xcex", 20.0, tag="Binance: Hot Wallet"),
                       H("0xpool", 15.0, tag="PancakeSwap V2"),
                       H("0xsafe", 12.0)]),
            verify_fn=fake)
        self.assertEqual(calls, ["0xsafe"])   # burn + CEX + DEX tags filtered out


class BuildOnchainHeadlineFlip(unittest.TestCase):
    """A QUIET nonce signal + a DISTRIBUTING top holder must NOT read bare QUIET."""

    def setUp(self):
        self._orig = (O.build_nonce_state, O._vc_overlap, O._safe_history_and_flows,
                      O._concentration, O._verify_wallet, O._fired_safe_addresses,
                      O.moralis_quota.status)
        O.build_nonce_state = lambda t, *a, **k: {"tracked": True, "signal": "QUIET", "score": 0,
                                                  "ms": 5, "escalation_fired": [], "newly_fired": []}
        O._vc_overlap = lambda t: {"available": False}
        O._safe_history_and_flows = lambda t, **k: ({"available": False}, {"available": False})
        O._fired_safe_addresses = lambda *a, **k: []
        O.moralis_quota.status = lambda *a, **k: {"exhausted": False, "calls": 0, "prefix": ""}

    def tearDown(self):
        (O.build_nonce_state, O._vc_overlap, O._safe_history_and_flows,
         O._concentration, O._verify_wallet, O._fired_safe_addresses,
         O.moralis_quota.status) = self._orig

    def test_quiet_signal_flips_when_top_holder_distributing(self):
        O._concentration = lambda t: conc([H("0x73d8", 30.0), H("0xaaa", 10.0)])
        O._verify_wallet = lambda a, t, **k: vw("DISTRIBUTING") if a.lower() == "0x73d8" else vw("DORMANT")
        o = O.build_onchain("BLESS")
        self.assertNotEqual(o["signal"], "QUIET")
        self.assertEqual(o["signal"], "DISTRIBUTING")
        self.assertTrue(o["distribution_checked"])
        self.assertTrue(o["top_holder_distribution"]["distributing"])
        self.assertTrue(o.get("signal_caveat"))

    def test_quiet_stays_quiet_when_holders_clean(self):
        O._concentration = lambda t: conc([H("0xaaa", 30.0), H("0xbbb", 10.0)])
        O._verify_wallet = lambda a, t, **k: vw("DORMANT")
        o = O.build_onchain("X")
        self.assertEqual(o["signal"], "QUIET")
        self.assertTrue(o["distribution_checked"])
        self.assertFalse(o["top_holder_distribution"]["distributing"])
        self.assertIsNone(o.get("signal_caveat"))

    def test_brief_surfaces_top_holder_distribution(self):
        O._concentration = lambda t: conc([H("0x73d8", 30.0)])
        O._verify_wallet = lambda a, t, **k: vw("DISTRIBUTING")
        layer = B._onchain_layer("BLESS")
        self.assertEqual(layer["signal"], "DISTRIBUTING")
        self.assertTrue(layer["top_holder_distribution"]["distributing"])
        self.assertTrue(layer.get("signal_caveat"))


class VerifyWalletInvokeWiring(unittest.TestCase):
    """SPEC-89 item 5 — `{ticker,wallet}` (desk naming) must map to `<address> --token <TKR>`."""

    def setUp(self):
        import orchestrator as ORC
        self.ORC = ORC
        self.caps = ORC.load_caps()

    def test_registry_has_alias_or_desk_keys(self):
        vw_cap = self.caps["verify_wallet"]
        aliases = vw_cap.get("aliases") or {}
        # ticker→token and wallet→address must be mapped
        self.assertEqual(aliases.get("ticker"), "token")
        self.assertEqual(aliases.get("wallet"), "address")

    def test_desk_args_fill_to_valid_command(self):
        vw_cap = self.caps["verify_wallet"]
        args = {"ticker": "BLESS", "wallet": "0x73d8"}
        args = self.ORC.apply_aliases(vw_cap, args)
        cmd = self.ORC.fill(vw_cap["invoke"], args)
        self.assertIn("0x73d8", cmd)
        self.assertIn("--token BLESS", cmd)
        self.assertNotIn("--token --json", cmd)   # the SPEC-89 bug signature

    def test_legacy_address_token_still_works(self):
        vw_cap = self.caps["verify_wallet"]
        args = self.ORC.apply_aliases(vw_cap, {"address": "0xabc", "token": "ESPORTS"})
        cmd = self.ORC.fill(vw_cap["invoke"], args)
        self.assertIn("0xabc", cmd)
        self.assertIn("--token ESPORTS", cmd)


class AnalyseSec4Gate(unittest.TestCase):
    """SPEC-89 item 4 — a distributing top holder under deep-neg funding flips the §4 long to PASS."""

    def _perp_long(self, fr_4h, oi_chg):
        return {"ticker": "X", "score": 40, "bias": "LONG", "phase": "trap",
                "metrics": {"fr_4h": fr_4h, "oi_chg": oi_chg, "turnover": 500e6,
                            "turnover_bybit": 300e6, "turnover_binance": 200e6, "price": 1.0},
                "reasons": []}

    def test_converge_distributing_top_holder_flips_long_to_pass(self):
        d, tier, notes, *_ = A.converge(
            self._perp_long(-0.50, 30), {}, cvd={"verdict": "BULLISH_DIVERGENCE"},
            struct={"near_ath": False}, top_holder_distributing=True)
        self.assertIn("PASS", d)
        self.assertTrue(any("trap" in n.lower() or "distribut" in n.lower() for n in notes))

    def test_converge_long_survives_when_holders_clean(self):
        d, tier, notes, *_ = A.converge(
            self._perp_long(-0.50, 30), {}, cvd={"verdict": "BULLISH_DIVERGENCE"},
            struct={"near_ath": False}, top_holder_distributing=False)
        self.assertIn("LONG", d)

    def test_build_analyse_regression_distributing_top_holder(self):
        orig = (A.run_json, A.run_whales, A.build_nonce_state, A.run_cvd,
                A._top_holder_distribution_check)
        try:
            A.run_whales = lambda *a, **k: {"verdict": "NO_DATA"}
            A.build_nonce_state = lambda t, *a, **k: {"tracked": True, "signal": "QUIET",
                                                      "score": 0, "ms": 5, "escalation_fired": []}
            A.run_cvd = lambda t, minutes=30: {"verdict": "BULLISH_DIVERGENCE"}
            table = {"perp_analyser.py": self._perp_long(-0.50, 30),
                     "intraday.py": {"near_ath": False}, "oi_sides.py": {}}
            A.run_json = lambda script, ticker, extra=None, timeout=120: table.get(script, {})
            # deep-neg + OI rising + bullish CVD would be a LONG — but a distributing top holder = TRAP
            A._top_holder_distribution_check = lambda t: {"checked": True, "distributing": True}
            a = A.build_analyse("X")
            self.assertEqual(a["verdict"], "PASS")
            self.assertTrue(a.get("top_holder_distribution", {}).get("distributing"))
        finally:
            (A.run_json, A.run_whales, A.build_nonce_state, A.run_cvd,
             A._top_holder_distribution_check) = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
