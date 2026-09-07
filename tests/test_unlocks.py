#!/usr/bin/env python3
"""SPEC-95 — auto-populated unlock calendar + pre-unlock (T-3d/T-1d) alerts.

Offline-deterministic: the public feed (DefiLlama emissions) and the canonical
supply lookup (CoinGecko via pull5) are injected, so no test touches the network.
A fixed `now` (epoch) drives every date/leg assertion.
"""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

import unlocks as U  # noqa: E402

# 2026-07-01T00:00:00Z — the BEAT/Audiera unlock day (the motivating miss).
NOW = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()

# Canonical CoinGecko supply for Audiera (the 288M circ / NOT the GoPlus 612k bug).
BEAT_SUPPLY = {"cg_id": "audiera", "circulating": 288016666.0,
               "total_supply": 1_000_000_000.0, "price": 2.92, "vol_24h": 18_009_711.0}

BEAT_SEED = [{"cg_id": "audiera", "ticker": "BEAT", "date": "2026-07-01",
              "amount": 21_240_000, "type": "cliff", "source": "manual:public-tracker",
              "detail": "Audiera July-1 cliff"}]


def _supply_beat(ticker, cg_id=None):
    return dict(BEAT_SUPPLY)


class SizeEvent(unittest.TestCase):
    def test_uses_canonical_circulating_not_single_chain(self):
        """The BEAT bug: GoPlus read 612k on BSC -> nonsense 3431%. Sizing MUST use the
        288M canonical circulating supply (CoinGecko) and live price."""
        sized = U.size_event(21_240_000, BEAT_SUPPLY)
        self.assertAlmostEqual(sized["pct_circulating"], 7.37, places=1)
        self.assertNotEqual(round(sized["pct_circulating"]), 3431)
        # ~$60M off the live price, ~3.4x the 24h turnover
        self.assertAlmostEqual(sized["usd_notional"] / 1e6, 62.0, delta=1.0)
        self.assertAlmostEqual(sized["vs_daily_volume"], 3.44, places=1)

    def test_degrades_without_supply(self):
        sized = U.size_event(21_240_000, {"_error": "no supply"})
        self.assertIsNone(sized["pct_circulating"])
        self.assertIsNone(sized["usd_notional"])


class BuildUnlocks(unittest.TestCase):
    def test_beat_audiera_from_seed_sized_canonically(self):
        """DoD #1: unlocks BEAT -> Audiera July-1: 21.24M / ~7.4% / ~$60M cliff,
        pct off 288M canonical circ. DefiLlama doesn't cover Audiera (404); the seed
        fallback supplies the date; sizing is the canonical CG read."""
        out = U.build_unlocks("BEAT", now=NOW,
                              fetcher=lambda slug: U.NOT_FOUND,
                              supply_fn=_supply_beat, seed=BEAT_SEED)
        self.assertEqual(out["cg_id"], "audiera")        # keyed off cg_id, not bare ticker
        self.assertTrue(out["coverage"])
        ev = out["events"][0]
        self.assertEqual(ev["date"], "2026-07-01")
        self.assertEqual(ev["amount"], 21_240_000)
        self.assertEqual(ev["type"], "cliff")
        self.assertAlmostEqual(ev["pct_circulating"], 7.37, places=1)
        self.assertTrue(ev["source"])

    def test_source_down_no_seed_is_coverage_false_not_no_unlock(self):
        """DoD #5: source down -> coverage:false, never crashes, never 'no unlock'."""
        out = U.build_unlocks("ZZZ", now=NOW,
                              fetcher=lambda slug: None,     # network error
                              supply_fn=lambda t, c=None: {"_error": "x"}, seed=[])
        self.assertFalse(out["coverage"])
        self.assertEqual(out["events"], [])

    def test_source_up_no_data_is_uncovered_not_no_unlock(self):
        """DoD #7: a token the source has nothing for is UNCOVERED (looked, found
        nothing), distinct from source-down — never silently read as 'no unlock'."""
        out = U.build_unlocks("ZZZ", now=NOW,
                              fetcher=lambda slug: U.NOT_FOUND,
                              supply_fn=lambda t, c=None: {"_error": "x"}, seed=[])
        self.assertTrue(out["coverage"])
        self.assertTrue(out["uncovered"])
        self.assertEqual(out["events"], [])

    def test_defillama_events_parsed_and_sized(self):
        payload = {"metadata": {"token": "coingecko:river", "events": [
            {"timestamp": int(datetime(2026, 7, 4, tzinfo=timezone.utc).timestamp()),
             "noOfTokens": [1_000_000], "unlockType": "cliff", "description": "cliff"},
        ]}}
        out = U.build_unlocks("RIVER", cg_id="river", now=NOW,
                              fetcher=lambda slug: payload,
                              supply_fn=lambda t, c=None: {"cg_id": "river",
                                                           "circulating": 100_000_000,
                                                           "price": 0.5, "vol_24h": 1_000_000},
                              seed=[])
        self.assertTrue(out["coverage"])
        ev = out["events"][0]
        self.assertEqual(ev["date"], "2026-07-04")
        self.assertEqual(ev["pct_circulating"], 1.0)
        self.assertEqual(ev["usd_notional"], 500_000)


class Flag(unittest.TestCase):
    def test_format_flag_t_minus(self):
        ev = {"date": "2026-07-02", "pct_circulating": 7.4, "usd_notional": 60_000_000,
              "type": "cliff", "vs_daily_volume": 3.0}
        flag = U.format_flag(ev, NOW)
        self.assertIn("⏰ UNLOCK T-1d", flag)
        self.assertIn("7.4%", flag)
        self.assertIn("$60M", flag)
        self.assertIn("cliff", flag)
        self.assertIn("3× daily vol", flag)

    def test_flag_for_within_horizon_only(self):
        cats = {"catalysts": [
            {"ticker": "AAA", "date": "2026-07-04", "type": "cliff",
             "pct_circulating": 5.0, "usd_notional": 1e7},
            {"ticker": "BBB", "date": "2026-08-30", "type": "unlock", "pct_supply": 3},
        ]}
        self.assertIsNotNone(U.unlock_flag_for("AAA", NOW, 7, cats))
        self.assertIn("T-3d", U.unlock_flag_for("AAA", NOW, 7, cats))
        self.assertIsNone(U.unlock_flag_for("BBB", NOW, 7, cats))   # 60d out
        self.assertIsNone(U.unlock_flag_for("CCC", NOW, 7, cats))   # not present


class Catalysts(unittest.TestCase):
    def test_merge_preserves_manual_dedupes_auto(self):
        cal = {"catalysts": [
            {"ticker": "SOON", "date": "2026-05-23", "type": "unlock", "detail": "manual"},
        ]}
        new = [{"ticker": "BEAT", "cg_id": "audiera", "date": "2026-07-01", "type": "cliff",
                "source": "unlocks-auto", "fetched_ts": "2026-07-01T00:00:00Z"}]
        cal, added, refreshed = U.merge_catalysts(cal, new)
        self.assertEqual(added, 1)
        tickers = {c["ticker"] for c in cal["catalysts"]}
        self.assertIn("SOON", tickers)     # manual preserved
        self.assertIn("BEAT", tickers)
        # re-merging the same auto entry refreshes in place, never duplicates
        cal, added2, refreshed2 = U.merge_catalysts(cal, new)
        self.assertEqual(added2, 0)
        self.assertEqual(refreshed2, 1)
        self.assertEqual(sum(1 for c in cal["catalysts"] if c["ticker"] == "BEAT"), 1)


class Alerts(unittest.TestCase):
    def _cat(self, days):
        d = datetime.fromtimestamp(NOW, tz=timezone.utc).date()
        from datetime import timedelta
        return {"ticker": "AAA", "cg_id": "aaa", "type": "cliff",
                "date": (d + timedelta(days=days)).isoformat(),
                "pct_circulating": 7.0, "usd_notional": 6e7, "vs_daily_volume": 3}

    def test_t3_leg_fires_at_three_days(self):
        cats = {"catalysts": [self._cat(3)]}
        due = U.due_alerts(cats, NOW, set())
        self.assertEqual([d["leg"] for d in due], [3])

    def test_t1_leg_fires_at_one_day(self):
        cats = {"catalysts": [self._cat(1)]}
        due = U.due_alerts(cats, NOW, set())
        self.assertEqual([d["leg"] for d in due], [1])

    def test_far_out_does_not_fire(self):
        cats = {"catalysts": [self._cat(5)]}
        self.assertEqual(U.due_alerts(cats, NOW, set()), [])

    def test_dedup_once_per_event(self):
        cats = {"catalysts": [self._cat(3)]}
        first = U.due_alerts(cats, NOW, set())
        fired = {d["key"] for d in first}
        self.assertEqual(U.due_alerts(cats, NOW, fired), [])   # already fired -> silent

    def test_fire_appends_once(self):
        cats = {"catalysts": [self._cat(1)]}
        sink = []
        fired = set()

        def append(ts, ticker, source, severity, msg):
            sink.append({"ticker": ticker, "severity": severity, "msg": msg})

        n1 = U.fire_unlock_alerts(now=NOW, cats=cats, fired=fired, append=append)
        n2 = U.fire_unlock_alerts(now=NOW, cats=cats, fired=fired, append=append)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)           # cursor-deduped
        self.assertEqual(len(sink), 1)
        self.assertIn("UNLOCK", sink[0]["msg"])


class ClassifySurfacing(unittest.TestCase):
    def test_annotate_unlocks_prefixes_reason(self):
        import classify as CL
        cats = {"catalysts": [{"ticker": "AAA", "date": "2026-07-04", "type": "cliff",
                               "pct_circulating": 7.4, "usd_notional": 6e7,
                               "vs_daily_volume": 3}]}
        rows = [{"ticker": "AAA", "reason": "CONFIRMS thesis"},
                {"ticker": "BBB", "reason": "nothing"}]
        CL.annotate_unlocks(rows, now=NOW, catalysts=cats)
        self.assertIn("⏰ UNLOCK", rows[0]["reason"])
        self.assertIn("CONFIRMS thesis", rows[0]["reason"])
        self.assertNotIn("⏰ UNLOCK", rows[1]["reason"])


if __name__ == "__main__":
    unittest.main()
