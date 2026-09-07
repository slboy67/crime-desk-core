#!/usr/bin/env python3
"""SPEC-191 #4 — config/venue_roles.json is the CURATED file, read-only for every code
path; live snapshots move to the untracked state/venue_roles/<TICKER>.json (like
state/oi_samples/). This closes the SPEC-181/SPEC-185 recurrence where a coder branch's
own test run (or a live `--save-roles` tick) left the tracked config file dirty.

Run:  python3 -m unittest tests.test_spec191_venue_roles_isolation -v
"""
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("oi_construction_spec191",
                                              ROOT / "capabilities" / "oi_construction.py")
OC = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OC)

REAL_VENUE_ROLES_PATH = ROOT / "config" / "venue_roles.json"


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class TestConfigVenueRolesNeverWrittenByTheSuite(unittest.TestCase):
    """The literal DoD ask: running the full oi_construction/venue_map test modules
    leaves config/venue_roles.json byte-identical."""

    def test_hash_unchanged_after_oi_construction_and_venue_map_suites(self):
        before = _hash(REAL_VENUE_ROLES_PATH)
        r = subprocess.run([sys.executable, "-m", "unittest",
                            "tests.test_oi_construction", "tests.test_venue_map", "-q"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = _hash(REAL_VENUE_ROLES_PATH)
        self.assertEqual(before, after,
                        "config/venue_roles.json changed while running the "
                        "oi_construction/venue_map suites — it must stay read-only")


class TestSaveVenueRolesSnapshotDefaultTarget(unittest.TestCase):
    """Default `path=None` must resolve under state/venue_roles/, never config/."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state_dir = OC.VENUE_ROLES_STATE_DIR
        OC.VENUE_ROLES_STATE_DIR = Path(self._tmp.name)
        self._before_hash = _hash(REAL_VENUE_ROLES_PATH)

    def tearDown(self):
        OC.VENUE_ROLES_STATE_DIR = self._orig_state_dir
        self._tmp.cleanup()

    def test_default_writes_under_state_dir_not_config(self):
        r = OC.save_venue_roles_snapshot("ZTEST", {"mark_engine": {"verdict": "binance"}})
        self.assertEqual(r["snapshots"], 1)
        written = Path(self._tmp.name) / "ZTEST.json"
        self.assertTrue(written.exists())
        self.assertEqual(_hash(REAL_VENUE_ROLES_PATH), self._before_hash)

    def test_main_save_roles_flag_never_touches_config(self):
        orig_build = OC.build_oi_construction
        OC.build_oi_construction = lambda t: {"ticker": t, "venue_roles": {"mark_engine": {"verdict": "x"}},
                                              "verdict": "UNKNOWN", "gating_ok": False,
                                              "battlefield": {"battlefield": "UNKNOWN"},
                                              "oi_types": [], "degraded": []}
        orig_argv = sys.argv
        sys.argv = ["oi_construction.py", "ZTEST", "--save-roles", "--json"]
        try:
            OC.main()
        finally:
            OC.build_oi_construction = orig_build
            sys.argv = orig_argv
        self.assertEqual(_hash(REAL_VENUE_ROLES_PATH), self._before_hash)
        self.assertTrue((Path(self._tmp.name) / "ZTEST.json").exists())


class TestCuratedVsSnapshotReadMerge(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.curated_path = Path(self._tmp.name) / "curated.json"
        self.state_dir = Path(self._tmp.name) / "state_roles"
        self.state_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_curated_wins_over_snapshot(self):
        self.curated_path.write_text(json.dumps(
            {"SKYAI": [{"exit": {"venue": "bitget"}, "curated": True}]}))
        OC.save_venue_roles_snapshot("SKYAI", {"exit": {"venue": "binance"}},
                                     path=self.state_dir / "SKYAI.json")
        r = OC.resolved_venue_roles("SKYAI", curated_path=self.curated_path, state_dir=self.state_dir)
        self.assertEqual(r["source"], "curated")
        self.assertEqual(r["venue_roles"]["exit"]["venue"], "bitget")

    def test_snapshot_used_when_no_curated_row(self):
        OC.save_venue_roles_snapshot("NOCUR", {"exit": {"venue": "aster"}},
                                     path=self.state_dir / "NOCUR.json")
        r = OC.resolved_venue_roles("NOCUR", curated_path=self.curated_path, state_dir=self.state_dir)
        self.assertEqual(r["source"], "snapshot")
        self.assertEqual(r["venue_roles"]["exit"]["venue"], "aster")

    def test_neither_present_is_empty_not_fabricated(self):
        r = OC.resolved_venue_roles("GHOST", curated_path=self.curated_path, state_dir=self.state_dir)
        self.assertIsNone(r["source"])
        self.assertEqual(r["venue_roles"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
