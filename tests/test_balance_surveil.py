#!/usr/bin/env python3
"""SPEC-126 — balance-delta surveillance for CONTRACT tracked wallets.

Run:  python3 tests/test_balance_surveil.py

The DEXE 2026-07-21 incident: the committed tripwire was "page on first outbound from any
DEXE-STAGED-HOP2-* safe" — but those wallets are Gnosis Safe PROXIES. A Safe executes via
execTransaction from a signer EOA, so the safe's own account nonce never changes; nonce_surveil
is permanently blind to it. The safes fired 15:43-16:00 UTC (625K DEXE, 510,218 -> 138,908 on
hop2-A) and NOTHING PAGED. This module adds per-wallet mode: EOAs keep nonce surveillance
(untouched, in onchain.py/wallet_state.py — this module never imports or mutates them); contract
wallets get token-balance-delta surveillance instead, riding the SAME inbox producer API
(inbox.append_event) every other post-nonce surveillance monitor uses (funding_surveil,
tape_watch, board_tick).

Offline-deterministic: balance reads + contract probes are INJECTED (no network), clock
injected, tmp baseline state path.
"""
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

spec = importlib.util.spec_from_file_location("balance_surveil", ROOT / "ops" / "balance_surveil.py")
BS = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BS)

import onchain as O  # noqa: E402 — the exact module object BS.O references (same sys.modules entry)


def _wallet(label="SAFE-A", address="0x076b2d185c4da214ac92ad004311216ab9401399",
            chain="binance-smart-chain", surveil=None):
    w = {"label": label, "address": address, "chain": chain, "tier": "distribution"}
    if surveil is not None:
        w["surveil"] = surveil
    return w


def _read(value, available=True, cross_checked=True, agree=True):
    return {"available": available, "value": value, "cross_checked": cross_checked, "agree": agree}


class TestAssessWallet(unittest.TestCase):
    def test_first_tick_seeds_no_event(self):
        w = _wallet()
        ev, entry = BS.assess_wallet(w, None, lambda *a, **k: _read(510218))
        self.assertIsNone(ev)
        self.assertEqual(entry, {"balance": 510218, "fail_count": 0})

    def test_balance_drop_fires_high_with_delta(self):
        w = _wallet()
        prev = {"balance": 510218, "fail_count": 0}
        ev, entry = BS.assess_wallet(w, prev, lambda *a, **k: _read(138908))
        self.assertIsNotNone(ev)
        self.assertEqual(ev["severity"], "HIGH")
        self.assertEqual(ev["kind"], "balance_drop")
        self.assertAlmostEqual(ev["delta"], 510218 - 138908)
        self.assertEqual(ev["prev_balance"], 510218)
        self.assertEqual(ev["balance"], 138908)
        self.assertIn("510.22K", ev["msg"])   # SPEC-141: compact human units, not the raw int
        self.assertIn("138.91K", ev["msg"])
        self.assertEqual(entry, {"balance": 138908, "fail_count": 0})

    def test_unchanged_balance_no_event(self):
        w = _wallet()
        prev = {"balance": 510218, "fail_count": 0}
        ev, entry = BS.assess_wallet(w, prev, lambda *a, **k: _read(510218))
        self.assertIsNone(ev)
        self.assertEqual(entry["balance"], 510218)

    def test_balance_increase_no_event(self):
        w = _wallet()
        prev = {"balance": 100.0, "fail_count": 0}
        ev, entry = BS.assess_wallet(w, prev, lambda *a, **k: _read(150.0))
        self.assertIsNone(ev)
        self.assertEqual(entry["balance"], 150.0)

    def test_dust_decrease_no_event(self):
        w = _wallet()
        prev = {"balance": 100.0, "fail_count": 0}
        ev, entry = BS.assess_wallet(w, prev, lambda *a, **k: _read(100.0 - BS.DUST / 2))
        self.assertIsNone(ev)

    def test_single_rpc_zero_disagreement_is_med_not_page(self):
        w = _wallet()
        prev = {"balance": 138908, "fail_count": 0}
        # cross_checked True but the two sources DISAGREE (one read 0, the other didn't)
        ev, entry = BS.assess_wallet(w, prev, lambda *a, **k: _read(0, cross_checked=True, agree=False))
        self.assertIsNotNone(ev)
        self.assertEqual(ev["severity"], "MED")
        self.assertEqual(ev["kind"], "data_quality")
        # the unconfirmed zero must NOT overwrite the trusted baseline
        self.assertEqual(entry["balance"], 138908)

    def test_single_source_zero_unconfirmed_is_med_not_page(self):
        w = _wallet()
        prev = {"balance": 138908, "fail_count": 0}
        ev, entry = BS.assess_wallet(w, prev, lambda *a, **k: _read(0, cross_checked=False, agree=None))
        self.assertIsNotNone(ev)
        self.assertEqual(ev["kind"], "data_quality")
        self.assertEqual(entry["balance"], 138908)

    def test_cross_confirmed_zero_still_fires_high(self):
        w = _wallet()
        prev = {"balance": 138908, "fail_count": 0}
        ev, entry = BS.assess_wallet(w, prev, lambda *a, **k: _read(0, cross_checked=True, agree=True))
        self.assertIsNotNone(ev)
        self.assertEqual(ev["severity"], "HIGH")
        self.assertEqual(entry["balance"], 0)

    def test_consecutive_failures_med_surveillance_blind(self):
        w = _wallet()
        prev = None
        events = []
        for _ in range(BS.FAIL_THRESHOLD):
            ev, prev = BS.assess_wallet(w, prev, lambda *a, **k: _read(None, available=False))
            events.append(ev)
        self.assertTrue(all(e is None for e in events[:-1]))
        self.assertIsNotNone(events[-1])
        self.assertEqual(events[-1]["severity"], "MED")
        self.assertEqual(events[-1]["kind"], "surveillance_blind")

    def test_failure_then_recovery_resets_fail_count(self):
        w = _wallet()
        ev, entry = BS.assess_wallet(w, None, lambda *a, **k: _read(None, available=False))
        self.assertEqual(entry["fail_count"], 1)
        ev, entry = BS.assess_wallet(w, entry, lambda *a, **k: _read(500.0))
        self.assertIsNone(ev)   # first successful read after failures = seed, not a fire
        self.assertEqual(entry, {"balance": 500.0, "fail_count": 0})


class TestMaterialityGate(unittest.TestCase):
    """SPEC-164: SLX -0.09%, TAG -0.05%, TAKE -0.19% (all exchange-proxy/top-holder churn)
    fired HIGH 'contract-wallet OUTBOUND' and paged 'contract safe DRAINED' (urgent) — none
    was a drain. A delta only pages once it clears BAL_MIN_PCT/BAL_MIN_USD; below that it's
    a LOW inbox record, never HIGH, never paged."""

    def _now(self):
        return datetime(2026, 8, 26, 0, 0, 0, tzinfo=timezone.utc)

    def test_09pct_delta_is_low_no_page(self):
        w = _wallet()
        prev = {"balance": 1_000_000.0, "fail_count": 0}
        ev, entry = BS.assess_wallet(w, prev, lambda *a, **k: _read(999_100.0), now=self._now())
        self.assertIsNotNone(ev)
        self.assertEqual(ev["severity"], "LOW")
        self.assertEqual(ev["kind"], "sub_threshold")
        self.assertNotIn(ev["kind"], BS.PAGEABLE_KINDS)
        self.assertIn("[SUB-THRESHOLD]", ev["msg"])

    def test_8pct_delta_is_high_and_pageable(self):
        w = _wallet()
        prev = {"balance": 1_000_000.0, "fail_count": 0}
        ev, entry = BS.assess_wallet(w, prev, lambda *a, **k: _read(920_000.0), now=self._now())
        self.assertIsNotNone(ev)
        self.assertEqual(ev["severity"], "HIGH")
        self.assertEqual(ev["kind"], "balance_drop")
        self.assertIn(ev["kind"], BS.PAGEABLE_KINDS)
        self.assertIn("OUTBOUND", ev["msg"])

    def test_balance_to_zero_is_drained_label(self):
        w = _wallet()
        prev = {"balance": 138908.0, "fail_count": 0}
        ev, entry = BS.assess_wallet(w, prev, lambda *a, **k: _read(0, cross_checked=True, agree=True),
                                      now=self._now())
        self.assertIsNotNone(ev)
        self.assertEqual(ev["kind"], "drained_to_zero")
        self.assertIn("DRAINED", ev["msg"])
        self.assertIn(ev["kind"], BS.PAGEABLE_KINDS)

    def test_exchange_labeled_wallet_big_drop_no_page(self):
        w = _wallet(label="BINANCE-WALLET-PROXY")
        prev = {"balance": 1_000_000.0, "fail_count": 0}
        ev, entry = BS.assess_wallet(w, prev, lambda *a, **k: _read(700_000.0), now=self._now())
        self.assertIsNotNone(ev)
        self.assertEqual(ev["kind"], "exchange_churn")
        self.assertIn("EXCHANGE-CHURN", ev["msg"])
        self.assertNotIn(ev["kind"], BS.PAGEABLE_KINDS)   # 30% is material but must never page

    def test_thirty_subthreshold_ticks_fire_one_drip(self):
        w = _wallet()
        prev = {"balance": 1_000_000.0, "fail_count": 0}
        events = []
        bal = 1_000_000.0
        now = self._now()
        for _ in range(30):
            bal *= (1 - 0.0015)   # -0.15% per tick, well under the 2% single-tick gate
            ev, prev = BS.assess_wallet(w, prev, (lambda v: (lambda *a, **k: _read(v)))(bal), now=now)
            events.append(ev)
        drip_events = [e for e in events if e and e["kind"] == "outbound_drip"]
        self.assertEqual(len(drip_events), 1)
        self.assertIn("OUTBOUND-DRIP", drip_events[0]["msg"])
        self.assertIn(drip_events[0]["kind"], BS.PAGEABLE_KINDS)
        # everything else stayed sub-threshold, never HIGH balance_drop
        self.assertTrue(all(e["kind"] in ("sub_threshold", "outbound_drip") for e in events if e))

    def test_usd_bound_can_fire_below_pct_threshold(self):
        # whale wallet: 0.5% is way under BAL_MIN_PCT, but at $1/token that's $500K >> $25K
        w = _wallet()
        w["_price_usd"] = 1.0
        prev = {"balance": 100_000_000.0, "fail_count": 0}
        ev, entry = BS.assess_wallet(w, prev, lambda *a, **k: _read(99_500_000.0), now=self._now())
        self.assertIsNotNone(ev)
        self.assertEqual(ev["kind"], "balance_drop")
        self.assertIn(ev["kind"], BS.PAGEABLE_KINDS)


class TestSurveilMode(unittest.TestCase):
    def test_explicit_override_wins_no_probe_call(self):
        calls = []

        def probe_fn(addr, chain):
            calls.append(addr)
            return {"is_contract": False}

        w = _wallet(surveil="balance")
        self.assertEqual(BS.surveil_mode(w, probe_fn), "balance")
        self.assertEqual(calls, [])   # explicit mode never consults the probe

    def test_autodetect_contract_is_balance(self):
        w = _wallet()
        mode = BS.surveil_mode(w, lambda addr, chain: {"is_contract": True})
        self.assertEqual(mode, "balance")

    def test_autodetect_eoa_is_nonce(self):
        w = _wallet()
        mode = BS.surveil_mode(w, lambda addr, chain: {"is_contract": False})
        self.assertEqual(mode, "nonce")

    def test_invalid_override_falls_back_to_autodetect(self):
        w = _wallet(surveil="bogus")
        mode = BS.surveil_mode(w, lambda addr, chain: {"is_contract": True})
        self.assertEqual(mode, "balance")

    def test_both_mode_is_explicit_passthrough(self):
        w = _wallet(surveil="both")
        self.assertEqual(BS.surveil_mode(w, lambda *a: {"is_contract": False}), "both")


class TestGetCodeCached(unittest.TestCase):
    """SPEC-126 req: getCode detection cached (no per-tick getCode) — reuses onchain.py's
    existing probe_contract disk cache (SPEC-67), redirected to a tmp file for the test."""

    def setUp(self):
        self._orig_rpc = O._rpc
        self._orig_cache_path = O._PROBE_CACHE_PATH
        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self._tmp.write(b"{}")
        self._tmp.close()
        O._PROBE_CACHE_PATH = Path(self._tmp.name)
        self.calls = []

        def counting_rpc(chain_key, method, params, timeout=12):
            if method == "eth_getCode":
                self.calls.append(1)
                return "0x60806040"
            return "0x"
        O._rpc = counting_rpc

    def tearDown(self):
        O._rpc = self._orig_rpc
        O._PROBE_CACHE_PATH = self._orig_cache_path
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_second_probe_hits_cache_not_rpc(self):
        addr = "0x076b2d185c4da214ac92ad004311216ab9401399"
        w = _wallet(address=addr)
        BS.surveil_mode(w, O.probe_contract)
        BS.surveil_mode(w, O.probe_contract)
        BS.surveil_mode(w, O.probe_contract)
        self.assertEqual(len(self.calls), 1)


class TestRunTick(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg_path = Path(self.tmpdir) / "tracked_wallets.json"
        self.state_path = Path(self.tmpdir) / "balance_baseline_DEXE.json"
        import json
        self.cfg_path.write_text(json.dumps({
            "rpcs": {"binance-smart-chain": "https://example/rpc"},
            "tokens": {
                "DEXE": {
                    "decimals": 18,
                    "contracts": {"binance-smart-chain": "0x6e88056e8376ae7709496ba64d37fa2f8015ce3e"},
                    "wallets": [
                        _wallet("DEXE-STAGED-HOP2-A",
                                "0x076b2d185c4da214ac92ad004311216ab9401399", surveil="balance"),
                        _wallet("DEXE-EOA-EXECUTOR", "0xc9fb9ca2b7a08577839aea21905a607fae340d8b",
                                surveil="nonce"),
                    ],
                }
            },
        }))

    def _run(self, balances, emit_events):
        def balance_fn(address, contract, chain, decimals):
            return _read(balances[address.lower()])

        def emit_fn(ts, ticker, source, severity, msg):
            emit_events.append((ts, ticker, source, severity, msg))

        return BS.run_tick("DEXE", wallets_path=self.cfg_path, state_path=self.state_path,
                            now=None, probe_fn=lambda *a, **k: {"is_contract": True},
                            balance_fn=balance_fn, emit_fn=emit_fn)

    def test_nonce_mode_wallet_skipped_entirely(self):
        events = []
        out = self._run({"0x076b2d185c4da214ac92ad004311216ab9401399": 510218}, events)
        self.assertEqual(out["checked"], 1)   # only the balance-mode wallet

    def test_seed_tick_emits_nothing(self):
        events = []
        self._run({"0x076b2d185c4da214ac92ad004311216ab9401399": 510218}, events)
        self.assertEqual(events, [])

    def test_second_tick_fires_high_via_emit_fn(self):
        events = []
        self._run({"0x076b2d185c4da214ac92ad004311216ab9401399": 510218}, events)
        self._run({"0x076b2d185c4da214ac92ad004311216ab9401399": 138908}, events)
        self.assertEqual(len(events), 1)
        ts, ticker, source, severity, msg = events[0]
        self.assertEqual(ticker, "DEXE")
        self.assertEqual(source, "balance_surveil")
        self.assertEqual(severity, "HIGH")
        self.assertIn("510.22K", msg)   # SPEC-141: compact human units, not the raw int
        self.assertIn("138.91K", msg)

    def test_result_lists_fired_entries(self):
        events = []
        self._run({"0x076b2d185c4da214ac92ad004311216ab9401399": 510218}, events)
        out = self._run({"0x076b2d185c4da214ac92ad004311216ab9401399": 138908}, events)
        self.assertEqual(len(out["fired"]), 1)
        self.assertEqual(out["fired"][0]["kind"], "balance_drop")

    def test_subthreshold_and_exchange_never_land_in_fired(self):
        import json
        cfg_path = Path(self.tmpdir) / "tracked_wallets_2.json"
        state_path = Path(self.tmpdir) / "balance_baseline_SLX.json"
        cfg_path.write_text(json.dumps({
            "tokens": {
                "SLX": {
                    "decimals": 18,
                    "contracts": {"binance-smart-chain": "0xslxtoken"},
                    "wallets": [
                        _wallet("SLX-SAFE", "0x076b2d185c4da214ac92ad004311216ab9401399",
                                surveil="balance"),
                        _wallet("BINANCE-WALLET-PROXY", "0xc9fb9ca2b7a08577839aea21905a607fae340d8b",
                                surveil="balance"),
                    ],
                }
            },
        }))

        def balance_fn(address, contract, chain, decimals):
            return _read(balances[address.lower()])

        def emit_fn(ts, ticker, source, severity, msg):
            events.append((ts, ticker, source, severity, msg))

        events = []
        balances = {"0x076b2d185c4da214ac92ad004311216ab9401399": 16_980_000.0,
                     "0xc9fb9ca2b7a08577839aea21905a607fae340d8b": 1_000_000.0}
        BS.run_tick("SLX", wallets_path=cfg_path, state_path=state_path, now=None,
                    probe_fn=lambda *a, **k: {"is_contract": True},
                    balance_fn=balance_fn, emit_fn=emit_fn)   # seed tick

        # SLX-SAFE: -15.7K of 16.98M ≈ -0.09% (the real incident); BINANCE-WALLET-PROXY: -30%
        balances = {"0x076b2d185c4da214ac92ad004311216ab9401399": 16_980_000.0 - 15_700.0,
                     "0xc9fb9ca2b7a08577839aea21905a607fae340d8b": 700_000.0}
        out = BS.run_tick("SLX", wallets_path=cfg_path, state_path=state_path, now=None,
                          probe_fn=lambda *a, **k: {"is_contract": True},
                          balance_fn=balance_fn, emit_fn=emit_fn)
        self.assertEqual(out["fired"], [])
        kinds = {e["kind"] for e in out["events"]}
        self.assertEqual(kinds, {"sub_threshold", "exchange_churn"})


if __name__ == "__main__":
    unittest.main()
