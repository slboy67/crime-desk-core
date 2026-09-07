#!/usr/bin/env python3
"""SPEC 43 — `stake_schedule` capability: unlock cliffs + catalysts from a lock contract.

Run:  python3 -m unittest tests.test_stake_schedule

Native port of _oldrepo/scripts/stake_schedule.py (RIVER unlock methodology:
event-log scan → primary event by topic[0] frequency → heuristic uint256 decode →
forward unlock schedule by week → CLIFF dates >5% of locked supply → optional
config/catalysts.json write). The parts-bin script prose-prints; the port emits ONE
JSON object on --json with the contract:
  {contract, chain, primary_event, locked_total,
   schedule:[{week, amount, pct_of_locked}], cliffs:[{date, amount, pct}],
   catalyst_written: bool}

All RPC calls are mocked (module-level `rpc`) — offline-deterministic. The real RPC
pool must put the archive endpoint (bsc-mainnet.public.blastapi.io) FIRST for bsc
(memory/reference_bsc_archive_rpc.md).
"""
import contextlib
import importlib.util
import io
import json
import shlex
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SS = _load("stake_schedule", ROOT / "capabilities" / "stake_schedule.py")
ORCH = _load("orchestrator", ROOT / "orchestrator.py")

NOW = int(time.time())
LATEST_BLOCK = 1_000_000
CONTRACT = "0x" + "ab" * 20
TOKEN = "0x" + "cd" * 20
LOCK_TOPIC = "0x" + "11" * 32

# proposed registry invoke template (mirrors the capabilities.json entry returned
# by the coder) — round-tripped through the orchestrator's own fill()
INVOKE = ("python3 capabilities/stake_schedule.py {contract} [--chain {chain}] "
          "[--token {token}] [--window {window}] [--catalyst {catalyst}] --json")


def _slot(v):
    return format(v, "064x")


def _dt_to_ts(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _encode_unlock_steps(times, cumulative):
    """ABI-encode the LIVE getUnlockSchedules() shape (SPEC-167): ONE dynamic array
    of (uint256 time, uint256 cumulativeAmount) 2-word tuples — one head offset,
    then a length word, then `length` inline (time, cumulativeAmount) pairs. This
    is what the real DEBIT escrow returns (verified against
    handoffs/fixtures/debit_escrow_getUnlockSchedules.hex) — NOT two independent
    uint256[] arrays with two head offsets (that was the SPEC-165 assumption the
    live contract disproves: its second head word, 0x30, isn't word-aligned)."""
    n = len(times)
    assert n == len(cumulative)
    head = _slot(32)   # single offset: points right after this one head word
    body = _slot(n) + "".join(_slot(t) + _slot(c) for t, c in zip(times, cumulative))
    return "0x" + head + body


def _lock_log(amount_wei, end_ts, block=LATEST_BLOCK - 100):
    """A Stake/Lock-style event: data = [amount, endTime]."""
    return {
        "topics": [LOCK_TOPIC, "0x" + "00" * 12 + "ee" * 20],
        "data": "0x" + _slot(amount_wei) + _slot(end_ts),
        "blockNumber": hex(block),
    }


# Lock fixture: 3 future unlocks 7d apart → 3 distinct ISO weeks.
# total = 10_000 tokens; 5% cliff threshold = 500 → 9000 and 600 are cliffs, 400 is not.
LOCK_LOGS = [
    _lock_log(9000 * 10**18, NOW + 30 * 86400),
    _lock_log(400 * 10**18, NOW + 37 * 86400),
    _lock_log(600 * 10**18, NOW + 44 * 86400),
]

# Transfer-ish event: amounts only, no endTime → no future unlocks decodable
NOLOCK_LOGS = [
    {"topics": ["0x" + "22" * 32], "data": "0x" + _slot(5 * 10**18),
     "blockNumber": hex(LATEST_BLOCK - 50)},
]


def _fake_rpc(logs):
    """Offline rpc stub: blockNumber, chunked getLogs (all logs on first chunk),
    block timestamps, balanceOf."""
    state = {"getlogs_calls": 0}

    def rpc(chain, method, params):
        if method == "eth_blockNumber":
            return hex(LATEST_BLOCK)
        if method == "eth_getLogs":
            state["getlogs_calls"] += 1
            return logs if state["getlogs_calls"] == 1 else []
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(NOW - 3600)}
        if method == "eth_call":
            return _slot(1000 * 10**18)  # balanceOf for token-mode
        raise AssertionError(f"unexpected rpc method {method}")
    return rpc


class TestStakeSchedule(unittest.TestCase):
    def setUp(self):
        self._rpc = SS.rpc

    def tearDown(self):
        SS.rpc = self._rpc

    # ── DoD: known lock-contract fixture returns cliffs ────────────────────
    def test_lock_contract_returns_cliffs_and_schedule(self):
        SS.rpc = _fake_rpc(LOCK_LOGS)
        d = SS.build_schedule(CONTRACT, "bsc", window=1)
        self.assertEqual(d["contract"], CONTRACT)
        self.assertEqual(d["chain"], "binance-smart-chain")
        self.assertEqual(d["primary_event"], LOCK_TOPIC)
        self.assertAlmostEqual(d["locked_total"], 10_000.0, places=2)
        # schedule: 3 distinct weeks, each with week/amount/pct_of_locked
        self.assertEqual(len(d["schedule"]), 3)
        for row in d["schedule"]:
            self.assertIn("week", row)
            self.assertIn("amount", row)
            self.assertIn("pct_of_locked", row)
        self.assertAlmostEqual(sum(r["amount"] for r in d["schedule"]), 10_000.0, places=2)
        # cliffs: 9000 (90%) and 600 (6%) clear the 5% gate; 400 (4%) does not
        self.assertEqual(len(d["cliffs"]), 2)
        pcts = sorted(round(c["pct"], 1) for c in d["cliffs"])
        self.assertEqual(pcts, [6.0, 90.0])
        for c in d["cliffs"]:
            self.assertIn("date", c)
            self.assertIn("amount", c)
        self.assertFalse(d["catalyst_written"])

    # ── DoD: non-lock contract → clean {cliffs:[]}, not a crash ────────────
    def test_no_events_contract_clean_empty(self):
        SS.rpc = _fake_rpc([])
        d = SS.build_schedule(CONTRACT, "bsc", window=1)
        self.assertEqual(d["cliffs"], [])
        self.assertEqual(d["schedule"], [])
        self.assertEqual(d["locked_total"], 0)
        self.assertIsNone(d["primary_event"])
        self.assertFalse(d["catalyst_written"])

    def test_non_lock_events_no_endtime_clean_empty(self):
        # contract emits events but none carry a future timestamp slot → no cliffs
        SS.rpc = _fake_rpc(NOLOCK_LOGS)
        d = SS.build_schedule(CONTRACT, "bsc", window=1)
        self.assertEqual(d["cliffs"], [])
        self.assertEqual(d["schedule"], [])
        self.assertEqual(d["primary_event"], "0x" + "22" * 32)

    # ── archive RPC wiring (memory/reference_bsc_archive_rpc.md) ───────────
    def test_bsc_archive_rpc_first_in_pool(self):
        pool = SS.RPC_POOL["binance-smart-chain"]
        self.assertEqual(pool[0], "https://bsc-mainnet.public.blastapi.io")

    # ── catalyst write path ─────────────────────────────────────────────────
    def test_catalyst_write_and_dedupe(self):
        SS.rpc = _fake_rpc(LOCK_LOGS)
        with tempfile.TemporaryDirectory() as td:
            cat_file = Path(td) / "catalysts.json"
            old = SS.CATALYST_FILE
            try:
                SS.CATALYST_FILE = cat_file
                d = SS.build_schedule(CONTRACT, "bsc", window=1, catalyst="TESTX")
                self.assertTrue(d["catalyst_written"])
                cal = json.loads(cat_file.read_text())
                ours = [c for c in cal["catalysts"] if c["ticker"] == "TESTX"]
                self.assertEqual(len(ours), 2)
                self.assertEqual(ours[0]["type"], "unlock")
                # second run: duplicates are not re-added
                SS.rpc = _fake_rpc(LOCK_LOGS)
                SS.build_schedule(CONTRACT, "bsc", window=1, catalyst="TESTX")
                cal2 = json.loads(cat_file.read_text())
                self.assertEqual(
                    len([c for c in cal2["catalysts"] if c["ticker"] == "TESTX"]), 2)
            finally:
                SS.CATALYST_FILE = old

    # ── orchestrator-style round-trip on the proposed invoke template ──────
    def test_orchestrator_roundtrip_cli_json(self):
        args = {"contract": CONTRACT, "chain": "bsc", "window": 1}
        cmd = ORCH.fill(INVOKE, args)
        self.assertIn(CONTRACT, cmd)
        self.assertIn("--json", cmd)
        self.assertNotIn("--token", cmd)      # optional group dropped
        self.assertNotIn("--catalyst", cmd)
        argv = shlex.split(cmd)[1:]           # strip the leading "python3"
        SS.rpc = _fake_rpc(LOCK_LOGS)
        old_argv = sys.argv
        buf = io.StringIO()
        try:
            sys.argv = argv
            with contextlib.redirect_stdout(buf):
                SS.main()
        finally:
            sys.argv = old_argv
        out = buf.getvalue().strip()
        d = json.loads(out)                   # exactly ONE JSON object
        self.assertEqual(len(d["cliffs"]), 2)
        self.assertEqual(d["chain"], "binance-smart-chain")

    # ── usage error: bad contract → JSON error + nonzero exit ──────────────
    def test_bad_contract_usage_error(self):
        old_argv = sys.argv
        buf = io.StringIO()
        try:
            sys.argv = ["stake_schedule.py", "nothex", "--json"]
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    SS.main()
        finally:
            sys.argv = old_argv
        self.assertNotEqual(cm.exception.code, 0)
        d = json.loads(buf.getvalue().strip())
        self.assertIn("error", d)

    # ── SPEC-165 problem 2: UUPS escrow adapter (getUnlockSchedules() etc) ──
    # DEBIT's escrow 0x1088b932…aa74: getUnlockSchedules() returns 48 cumulative
    # monthly steps; getAvailableAmount/withdrawnAmount/startTime are simple
    # uint256 getters; owner-mutators (setUnlockSchedules/setStartTime/
    # emergencyWithdraw/upgradeToAndCall) exist → mutable:true.
    def test_escrow_adapter_decodes_48_steps_and_first_cliff(self):
        FIRST = _dt_to_ts("2026-09-18")
        times = [FIRST + i * 30 * 86400 for i in range(48)]
        cumulative = [(i + 1) * 1_000_000 * 10**18 for i in range(48)]   # 48M total, even steps
        raw = _encode_unlock_steps(times, cumulative)
        code = "0x600160006000" + "".join(
            "63" + s[2:] for s in SS.MUTATOR_SELECTORS.values()) + "5b"

        def rpc(chain, method, params):
            if method == "eth_blockNumber":
                return hex(LATEST_BLOCK)
            if method == "eth_call":
                data = params[0]["data"]
                if data == SS.ESCROW_GETUNLOCKSCHEDULES_SELECTOR:
                    return raw
                if data == SS.ESCROW_GETAVAILABLEAMOUNT_SELECTOR:
                    return "0x" + _slot(500_000 * 10**18)
                if data == SS.ESCROW_WITHDRAWNAMOUNT_SELECTOR:
                    return "0x" + _slot(0)
                if data == SS.ESCROW_STARTTIME_SELECTOR:
                    return "0x" + _slot(FIRST)
                if data == SS.OWNER_SELECTOR:
                    return "0x" + "00" * 12 + "aa" * 20
                raise AssertionError(f"unexpected selector {data}")
            if method == "eth_getCode":
                return code
            raise AssertionError(f"unexpected rpc method {method}")
        SS.rpc = rpc
        # float_supply 18M → each 1M step is ~5.56% of float, clears the 5% cliff gate
        d = SS.build_schedule(CONTRACT, "bsc", window=1, float_supply=18_000_000)
        self.assertEqual(d["mode"], "escrow")
        self.assertEqual(len(d["schedule"]), 48)
        self.assertAlmostEqual(d["locked_total"], 48_000_000.0, places=2)
        self.assertEqual(d["cliffs"][0]["date"], "2026-09-18")
        self.assertTrue(d["mutable"])
        self.assertEqual(d["owner_safes"], ["0x" + "aa" * 20])
        self.assertIn("setUnlockSchedules", d["mutators"])
        self.assertIn("upgradeToAndCall", d["mutators"])
        self.assertTrue(d["schedule_hash"])
        self.assertIsNone(d.get("escrow_adapter_reason"))

    def test_escrow_schedule_hash_changes_when_schedule_changes(self):
        def make_rpc(cum0):
            times = [_dt_to_ts("2026-09-18") + i * 30 * 86400 for i in range(3)]
            cumulative = [cum0 * (i + 1) * 10**18 for i in range(3)]
            raw = _encode_unlock_steps(times, cumulative)

            def rpc(chain, method, params):
                if method == "eth_blockNumber":
                    return hex(LATEST_BLOCK)
                if method == "eth_call":
                    data = params[0]["data"]
                    if data == SS.ESCROW_GETUNLOCKSCHEDULES_SELECTOR:
                        return raw
                    return "0x" + _slot(0)
                if method == "eth_getCode":
                    return "0x00"
                raise AssertionError(method)
            return rpc
        SS.rpc = make_rpc(1_000_000)
        d1 = SS.build_schedule(CONTRACT, "bsc", window=1)
        SS.rpc = make_rpc(2_000_000)
        d2 = SS.build_schedule(CONTRACT, "bsc", window=1)
        self.assertNotEqual(d1["schedule_hash"], d2["schedule_hash"])
        self.assertFalse(d1["mutable"])   # no mutator selectors in the "0x00" bytecode

    # ── SPEC-165: missing escrow selectors degrade to the current behavior ──
    def test_missing_escrow_selectors_degrades_to_event_scan_with_reason(self):
        # getUnlockSchedules() reverts ("0x") -> falls back to the existing lock-mode
        # event-log scan; the fixture's LOCK_LOGS still decode normally, plus a
        # `escrow_adapter_reason` names why the adapter didn't apply.
        def rpc(chain, method, params):
            if method == "eth_blockNumber":
                return hex(LATEST_BLOCK)
            if method == "eth_call":
                return "0x"      # no escrow selectors on this contract
            if method == "eth_getLogs":
                rpc.calls = getattr(rpc, "calls", 0) + 1
                return LOCK_LOGS if rpc.calls == 1 else []
            if method == "eth_getBlockByNumber":
                return {"timestamp": hex(NOW - 3600)}
            raise AssertionError(method)
        SS.rpc = rpc
        d = SS.build_schedule(CONTRACT, "bsc", window=1)
        self.assertEqual(d["mode"], "lock")
        self.assertEqual(len(d["cliffs"]), 2)          # same as the plain lock-mode fixture
        self.assertEqual(d["escrow_adapter_reason"], "getUnlockSchedules_not_present")

    # ── SPEC-167: decode the LIVE getUnlockSchedules() shape ────────────────
    def test_escrow_decodes_live_debit_fixture(self):
        # Real `eth_call` return captured verbatim against the DEBIT escrow
        # (handoffs/fixtures/debit_escrow_getUnlockSchedules.hex, SPEC-167). The
        # SPEC-165 fixture assumed two independent uint256[] arrays; the live
        # return is actually ONE dynamic array of (time, cumulativeAmount) pairs —
        # its second head word (0x30) isn't word-aligned, so it can't be a second
        # ABI offset. This is the exact shape the old decoder crashed on
        # (`int('', 16)`).
        raw = (ROOT / "handoffs" / "fixtures" /
               "debit_escrow_getUnlockSchedules.hex").read_text().strip()

        def rpc(chain, method, params):
            if method == "eth_call":
                data = params[0]["data"]
                if data == SS.ESCROW_GETUNLOCKSCHEDULES_SELECTOR:
                    return raw
                return "0x" + _slot(0)
            if method == "eth_getCode":
                return "0x00"
            raise AssertionError(method)
        SS.rpc = rpc
        d = SS.build_schedule(CONTRACT, "bsc", window=1, decimals=18)
        self.assertEqual(d["mode"], "escrow")
        self.assertIsNone(d.get("escrow_adapter_reason"))
        self.assertEqual(len(d["schedule"]), 48)
        self.assertEqual(d["schedule"][0]["date"], "2026-09-18")
        self.assertAlmostEqual(d["schedule"][0]["amount"], 1_908_333.0, places=1)
        self.assertAlmostEqual(d["schedule"][1]["amount"], 1_908_333.0, places=1)
        self.assertAlmostEqual(d["schedule"][2]["amount"], 1_908_333.0, places=1)
        self.assertAlmostEqual(d["locked_total"], 82_780_000.0, delta=1.0)
        self.assertTrue(d["schedule_hash"])

    def test_escrow_tried_first_even_with_token_flag(self):
        # SPEC-167 problem 2: --token must not skip the escrow probe.
        times = [_dt_to_ts("2026-09-18") + i * 30 * 86400 for i in range(3)]
        cumulative = [1_000_000 * (i + 1) * 10**18 for i in range(3)]
        raw = _encode_unlock_steps(times, cumulative)

        def rpc(chain, method, params):
            if method == "eth_call":
                data = params[0]["data"]
                if data == SS.ESCROW_GETUNLOCKSCHEDULES_SELECTOR:
                    return raw
                return "0x" + _slot(0)
            if method == "eth_getCode":
                return "0x00"
            raise AssertionError(method)
        SS.rpc = rpc
        d = SS.build_schedule(CONTRACT, "bsc", window=1, token=TOKEN)
        self.assertEqual(d["mode"], "escrow")

    def test_token_mode_carries_escrow_adapter_reason_when_not_present(self):
        # SPEC-167 problem 2: a `--token` call whose escrow probe comes back empty
        # must still carry `escrow_adapter_reason` — `skipped:token_mode` is not
        # acceptable, the adapter must actually have been tried.
        def rpc(chain, method, params):
            if method == "eth_blockNumber":
                return hex(LATEST_BLOCK)
            if method == "eth_call":
                data = params[0]["data"]
                if data == SS.ESCROW_GETUNLOCKSCHEDULES_SELECTOR:
                    return "0x"
                return "0x" + _slot(1000 * 10**18)   # balanceOf
            if method == "eth_getLogs":
                return []
            raise AssertionError(method)
        SS.rpc = rpc
        d = SS.build_schedule(CONTRACT, "bsc", window=1, token=TOKEN)
        self.assertEqual(d["mode"], "token")
        self.assertEqual(d["escrow_adapter_reason"], "getUnlockSchedules_not_present")

    def test_escrow_undecodable_raw_is_loud_with_hex_length(self):
        # SPEC-167 problem 3: any decode exception -> a loud
        # getUnlockSchedules_undecodable:<detail> naming the raw hex length, never
        # a silent [].
        bad_raw = "0x" + _slot(31)   # head offset 31 -> not word-aligned -> raises
        def rpc(chain, method, params):
            if method == "eth_call":
                return bad_raw
            raise AssertionError(method)
        SS.rpc = rpc
        result = SS._try_escrow_adapter(CONTRACT, "bsc", 18, None, NOW)
        self.assertFalse(result["available"])
        self.assertTrue(result["reason"].startswith("getUnlockSchedules_undecodable:"))
        self.assertIn("hex_len=64", result["reason"])

    # ── token-mode (passive vesting: Transfer-from scan) ────────────────────
    def test_token_mode_releases_as_cliffs(self):
        # one historical release of 200 tokens vs balance 1000 → 20% ≥ 5% = cliff
        rel = [{
            "topics": [SS.TRANSFER_TOPIC,
                       "0x" + "00" * 12 + CONTRACT[2:],
                       "0x" + "00" * 12 + "ff" * 20],
            "data": "0x" + _slot(200 * 10**18),
            "blockNumber": hex(LATEST_BLOCK - 10),
        }]
        SS.rpc = _fake_rpc(rel)
        d = SS.build_schedule(CONTRACT, "bsc", window=1, token=TOKEN)
        self.assertEqual(d["mode"], "token")
        self.assertEqual(len(d["cliffs"]), 1)
        self.assertAlmostEqual(d["cliffs"][0]["amount"], 200.0, places=2)
        self.assertAlmostEqual(d["cliffs"][0]["pct"], 20.0, places=2)
        self.assertFalse(d["catalyst_written"])


if __name__ == "__main__":
    unittest.main()
