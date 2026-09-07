#!/usr/bin/env python3
"""SPEC-90 — canon_signature must never flip a trade across direction.

A live BLESS breakdown-SHORT win recorded with signature
"distributing_squeezer_breakdown_short" canonicalized to `trap_formation_long`
(a LONG signature, via the "squeeze" branch) — crediting a SHORT win to a LONG
bucket and corrupting the §9 base-rate that gates sizing. These tests pin the
direction guard, the new short-side mapping, and the one-shot repair migration.
All offline — ledger path redirected to a temp dir.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capabilities"))
import ledger as LG


class CanonDirectionGuard(unittest.TestCase):
    # ── the core bug: SHORT must never become a LONG signature ──────────────
    def test_squeezer_breakdown_short_maps_to_stage5_short(self):
        # the literal contaminating string — maps short-side even direction-blind
        self.assertEqual(
            LG.canon_signature("distributing_squeezer_breakdown_short"),
            "stage5_short")

    def test_short_freeform_never_canon_to_trap_formation_long(self):
        # any free-form that would otherwise hit the LONG "squeeze" branch, with a
        # SHORT direction, must fall back to the correct-direction default.
        self.assertNotEqual(
            LG.canon_signature("shorts_squeezed_out_breakdown", direction="SHORT"),
            "trap_formation_long")
        self.assertEqual(
            LG.canon_signature("shorts_squeezed_out_breakdown", direction="SHORT"),
            "stage5_short")

    def test_explicit_long_sig_with_short_direction_flips(self):
        # contradictory record (SHORT trade tagged with the LONG enum) → short default
        self.assertEqual(
            LG.canon_signature("trap_formation_long", direction="SHORT"),
            "stage5_short")

    def test_long_never_canon_to_a_short_signature(self):
        self.assertEqual(
            LG.canon_signature("blowoff_top", direction="LONG"),
            "trap_formation_long")
        self.assertEqual(
            LG.canon_signature("mindshare_top_short", direction="LONG"),
            "trap_formation_long")

    def test_legit_short_unaffected(self):
        self.assertEqual(
            LG.canon_signature("blowoff", direction="SHORT"), "blowoff_top_short")
        self.assertEqual(
            LG.canon_signature("stage45_short", direction="SHORT"), "stage5_short")

    def test_legit_long_unaffected(self):
        self.assertEqual(
            LG.canon_signature("neg_funding_gate", direction="LONG"),
            "trap_formation_long")

    def test_discretionary_is_direction_neutral(self):
        # no keyword match → discretionary, no flip in either direction
        self.assertEqual(LG.canon_signature("???", direction="SHORT"), "discretionary")
        self.assertEqual(LG.canon_signature("???", direction="LONG"), "discretionary")

    def test_no_direction_preserves_legacy_behavior(self):
        # query-time lookups (stats) pass no direction → pure name resolution
        self.assertEqual(LG.canon_signature("trap_formation_long"), "trap_formation_long")
        self.assertEqual(LG.canon_signature("mindshare"), "mindshare_top_short")


class _LedgerTmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = LG.LEDGER_PATH
        LG.LEDGER_PATH = Path(self._tmp.name) / "ledger.jsonl"

    def tearDown(self):
        LG.LEDGER_PATH = self._orig
        self._tmp.cleanup()


class RecordRespectsDirection(_LedgerTmp):
    def test_short_breakdown_record_buckets_stage5_not_trap_long(self):
        LG.record({"ticker": "BLESS", "direction": "SHORT",
                   "signature": LG.canon_signature(
                       "distributing_squeezer_breakdown_short", direction="SHORT"),
                   "outcome": "tp2", "pnl_r": 2.5})
        st = LG.stats()
        self.assertIn("stage5_short", st["signatures"])
        self.assertNotIn("trap_formation_long", st["signatures"])
        self.assertEqual(st["signatures"]["stage5_short"]["n"], 1)
        self.assertEqual(st["signatures"]["stage5_short"]["hit_pct"], 100.0)


class MigrateSpec90(_LedgerTmp):
    def test_migrate_moves_contaminated_short_win_to_stage5(self):
        # the contaminated row exactly as it landed: SHORT win in trap_formation_long
        LG.record({"ticker": "BLESS", "direction": "SHORT",
                   "signature": "trap_formation_long", "outcome": "tp2", "pnl_r": 2.5})
        # a genuine LONG in trap_formation_long must NOT move
        LG.record({"ticker": "FIDA", "direction": "LONG",
                   "signature": "trap_formation_long", "outcome": "tp1", "pnl_r": 1.2})

        res = LG.migrate_spec90()
        self.assertEqual(res["migrated"], 1)

        st = LG.stats()
        self.assertEqual(st["signatures"]["trap_formation_long"]["n"], 1)   # only FIDA left
        self.assertEqual(st["signatures"]["stage5_short"]["n"], 1)          # BLESS landed
        self.assertEqual(st["signatures"]["stage5_short"]["hit_pct"], 100.0)
        self.assertEqual(st["signatures"]["trap_formation_long"]["last_5"][0]["ticker"],
                         "FIDA")

    def test_migrate_idempotent(self):
        LG.record({"ticker": "BLESS", "direction": "SHORT",
                   "signature": "trap_formation_long", "outcome": "tp2", "pnl_r": 2.5})
        self.assertEqual(LG.migrate_spec90()["migrated"], 1)
        self.assertEqual(LG.migrate_spec90()["migrated"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
