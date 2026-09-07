#!/usr/bin/env python3
"""SPEC-110 — data hygiene: (A) unlocks auto-fetch must render DefiLlama's `description`
template (never persist a literal `{placeholder}`), (B) ledger.py's known-signature set
must admit the §6 setups (`unlock_cliff_fade`, `spring_reclaim_long`) and fail loudly on
an unlisted --signature instead of silently rebucketing to `discretionary`.

Offline/deterministic throughout — ledger path redirected to a temp dir (same pattern as
tests/test_ledger_direction_guard.py); no network.
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import unlocks as U      # noqa: E402
import ledger as LG      # noqa: E402

NOW = datetime(2026, 7, 7, tzinfo=timezone.utc).timestamp()


# ── A: DefiLlama description-template rendering ──────────────────────────────────

class RenderDefillamaTemplate(unittest.TestCase):
    def test_real_memecore_shaped_payload_renders_no_literal_braces(self):
        # config/catalysts.json:75's real M/memecore row, reproduced as the raw feed shape.
        ts = int(datetime(2026, 7, 3, tzinfo=timezone.utc).timestamp())
        payload = {"metadata": {"token": "coingecko:memecore", "events": [
            {"timestamp": ts, "noOfTokens": [7_638_888.888888889], "unlockType": "cliff",
             "description": "On {timestamp} {tokens[0]} of Team tokens were unlocked"},
        ]}}
        events = U.parse_defillama(payload)
        self.assertEqual(len(events), 1)
        detail = events[0]["detail"]
        self.assertNotIn("{", detail)
        self.assertNotIn("}", detail)
        self.assertIn("7,638,889", detail)          # rendered token amount
        self.assertIn("Jul", detail)                # rendered timestamp (some date rendering)

    def test_unresolvable_placeholder_falls_back_to_composed_detail(self):
        ts = int(datetime(2026, 8, 2, tzinfo=timezone.utc).timestamp())
        payload = {"metadata": {"token": "coingecko:memecore", "events": [
            {"timestamp": ts, "noOfTokens": [1_000_000], "unlockType": "cliff",
             "description": "On {timestamp} {tokens[0]} of {mystery_field} tokens unlocked"},
        ]}}
        events = U.parse_defillama(payload)
        detail = events[0]["detail"]
        self.assertNotIn("{", detail)
        self.assertNotIn("}", detail)
        self.assertIn("1,000,000", detail)
        self.assertIn("2026-08-02", detail)
        self.assertIn("unlock", detail.lower())

    def test_plain_description_passes_through_unchanged(self):
        payload = {"metadata": {"token": "coingecko:river", "events": [
            {"timestamp": int(datetime(2026, 7, 4, tzinfo=timezone.utc).timestamp()),
             "noOfTokens": [1_000_000], "unlockType": "cliff", "description": "cliff"},
        ]}}
        events = U.parse_defillama(payload)
        self.assertEqual(events[0]["detail"], "cliff")

    def test_no_template_literal_ever_reaches_catalysts_json_entry(self):
        ts = int(datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp())
        payload = {"metadata": {"token": "coingecko:memecore", "events": [
            {"timestamp": ts, "noOfTokens": [7_638_888.888888889], "unlockType": "cliff",
             "description": "On {timestamp} {tokens[0]} of Team tokens will be unlocked"},
        ]}}
        out = U.build_unlocks("M", cg_id="memecore", now=NOW,
                              fetcher=lambda slug: payload,
                              supply_fn=lambda t, c=None: {"cg_id": "memecore",
                                                           "circulating": 1e9, "price": 0.01,
                                                           "vol_24h": 1e6},
                              seed=[])
        entry = U._to_catalyst_entry("M", out["events"][0], NOW)
        self.assertNotIn("{", entry["detail"])
        self.assertNotIn("}", entry["detail"])


# ── B: ledger signature taxonomy ──────────────────────────────────────────────────

class _LedgerTmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = LG.LEDGER_PATH
        LG.LEDGER_PATH = Path(self._tmp.name) / "ledger.jsonl"

    def tearDown(self):
        LG.LEDGER_PATH = self._orig
        self._tmp.cleanup()


class SignatureTaxonomy(unittest.TestCase):
    def test_new_signatures_admitted(self):
        self.assertIn("unlock_cliff_fade", LG.SIGNATURES)
        self.assertIn("spring_reclaim_long", LG.SIGNATURES)

    def test_existing_signatures_untouched(self):
        for s in ("trap_formation_long", "mindshare_top_short", "blowoff_top_short",
                  "stage5_short", "discretionary"):
            self.assertIn(s, LG.SIGNATURES)


class CliSignatureValidation(_LedgerTmp):
    def test_known_signature_records_verbatim_not_rebucketed(self):
        out = LG.record_cli_signature(
            {"ticker": "M", "direction": "SHORT", "outcome": "tp1", "pnl_r": 1.0},
            raw_signature="unlock_cliff_fade")
        self.assertEqual(out["record"]["signature"], "unlock_cliff_fade")
        st = LG.stats()
        self.assertIn("unlock_cliff_fade", st["signatures"])
        self.assertNotIn("discretionary", st["signatures"])

    def test_unknown_signature_fails_loudly_without_allow_new(self):
        with self.assertRaises(ValueError) as ctx:
            LG.record_cli_signature(
                {"ticker": "X", "direction": "SHORT", "outcome": "tp1", "pnl_r": 1.0},
                raw_signature="totally_made_up_signature")
        msg = str(ctx.exception)
        self.assertIn("totally_made_up_signature", msg)
        self.assertIn("unlock_cliff_fade", msg)          # names the known set
        self.assertIn("--allow-new", msg)
        # never silently landed in the ledger
        self.assertFalse(LG.LEDGER_PATH.exists())

    def test_unknown_signature_with_allow_new_persists_verbatim(self):
        out = LG.record_cli_signature(
            {"ticker": "X", "direction": "SHORT", "outcome": "tp1", "pnl_r": 1.0},
            raw_signature="totally_made_up_signature", allow_new=True)
        self.assertEqual(out["record"]["signature"], "totally_made_up_signature")
        st = LG.stats()
        self.assertIn("totally_made_up_signature", st["signatures"])

    def test_heuristic_freeform_still_resolves_without_allow_new(self):
        # legacy behavior preserved: a free-form string that hits a real heuristic bucket
        # (not the catch-all) still works without --allow-new.
        out = LG.record_cli_signature(
            {"ticker": "BLESS", "direction": "SHORT", "outcome": "tp2", "pnl_r": 2.0},
            raw_signature="distributing_squeezer_breakdown_short")
        self.assertEqual(out["record"]["signature"], "stage5_short")


class MigrateSpec110(_LedgerTmp):
    def test_migrate_retags_m_discretionary_to_unlock_cliff_fade(self):
        LG.record({"ticker": "M", "direction": "SHORT", "signature": "discretionary",
                   "outcome": "tp2", "pnl_r": 1.28, "commit_ts": "2026-07-03"})
        # an unrelated discretionary row must NOT move
        LG.record({"ticker": "OTHER", "direction": "LONG", "signature": "discretionary",
                   "outcome": "tp1", "pnl_r": 0.5, "commit_ts": "2026-07-03"})

        res = LG.migrate_spec110()
        self.assertEqual(res["migrated"], 1)

        st = LG.stats()
        self.assertEqual(st["signatures"]["unlock_cliff_fade"]["n"], 1)
        self.assertEqual(st["signatures"]["discretionary"]["n"], 1)   # OTHER unaffected

    def test_migrate_idempotent(self):
        LG.record({"ticker": "M", "direction": "SHORT", "signature": "discretionary",
                   "outcome": "tp2", "pnl_r": 1.28, "commit_ts": "2026-07-03"})
        self.assertEqual(LG.migrate_spec110()["migrated"], 1)
        self.assertEqual(LG.migrate_spec110()["migrated"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
