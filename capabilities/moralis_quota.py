#!/usr/bin/env python3
"""moralis_quota.py — SPEC 66: per-UTC-day Moralis call meter.

The Moralis free-tier quota dies SILENTLY mid-session (memory:
reference_moralis_free_daily_quota_gates_onchain — flow reads die mid-session on the
daily quota, which resets ~00:00 UTC). This meter is the desk's gas gauge: every live
Moralis call increments a per-UTC-day counter in state/moralis_quota.json; capabilities
read `calls_today()` to surface `moralis_calls_today`, and at >80% of the configured
daily budget `with_prefix()` prepends `[QUOTA n%]` to their reason/notes so the wall is
visible BEFORE it's hit. At 100% the existing per-section unavailable/degrade pattern
takes over (the meter only warns; it does not itself block a read).

Pure helpers — the clock (`now`) and the state path are injectable so tests run offline.

SPEC-123: the 2026-07-14 incident (premerge x3 + a coder TDD loop consumed the whole
free-plan day) exposed that this meter counted calls but not WHO made them — "84 calls
today" cannot answer "who spent the quota" when the plan was already exhausted. Every
record_call() now attributes to a `caller` (default: the running script's own name, e.g.
'verify_wallet' for capabilities/verify_wallet.py — zero plumbing needed at any call site)
with a CU estimate, stored under `by_caller` in the state file alongside the flat total.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
QUOTA_PATH = STATE / "moralis_quota.json"

# Moralis free tier is ~40k compute-units/day; a tokentx/txlist page is several CU, so a
# conservative call budget keeps the warning ahead of the real wall. Override with the
# MORALIS_DAILY_BUDGET env var (ops can tune without a code change).
DEFAULT_BUDGET = int(os.environ.get("MORALIS_DAILY_BUDGET", "2000"))
WARN_PCT = 80   # at/above this % of budget the [QUOTA n%] prefix fires

# erc20/transfers (the one live Moralis endpoint this codebase calls) costs ~10-20 CU per
# Moralis's own pricing (_oldrepo/scripts/moralis.py docstring); midpoint estimate used when
# a call site doesn't know its own weight.
DEFAULT_CU_PER_CALL = 15


def _utc_day(now=None):
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


def _read(state_path):
    try:
        return json.loads(Path(state_path).read_text())
    except Exception:   # missing / corrupt → cold start
        return {}


def calls_today(now=None, state_path=QUOTA_PATH):
    """Calls recorded for the current UTC day (0 if the file is cold or the day rolled over)."""
    d = _read(state_path)
    return int(d.get("calls", 0)) if d.get("day") == _utc_day(now) else 0


def record_call(now=None, state_path=QUOTA_PATH, n=1, caller=None, cu=None):
    """Increment today's counter (resetting on a UTC-day boundary) and return the new total.

    Attributes the call to `caller` (defaults to the running script's own name via
    sys.argv[0], e.g. 'verify_wallet' for capabilities/verify_wallet.py) with a CU estimate
    (`cu`, defaults to DEFAULT_CU_PER_CALL) — SPEC-123: state/moralis_quota.json must answer
    'what spent the quota today' from the file alone, not just carry a flat total.
    """
    day = _utc_day(now)
    d = _read(state_path)
    stale = d.get("day") != day
    count = (0 if stale else int(d.get("calls", 0))) + n
    cu_each = DEFAULT_CU_PER_CALL if cu is None else cu
    cu_total = (0 if stale else int(d.get("cu_estimate", 0))) + cu_each * n
    who = caller or Path(sys.argv[0]).stem
    by_caller = {} if stale else dict(d.get("by_caller", {}))
    entry = dict(by_caller.get(who) or {"calls": 0, "cu_estimate": 0})
    entry["calls"] += n
    entry["cu_estimate"] += cu_each * n
    by_caller[who] = entry
    p = Path(state_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"day": day, "calls": count, "cu_estimate": cu_total,
                                 "by_caller": by_caller}))
    except Exception:
        pass   # the meter must NEVER break the read it is metering
    return count


def status(budget=None, now=None, state_path=QUOTA_PATH):
    """The gas-gauge read: {calls, budget, pct, warn, exhausted, prefix}.

    pct is integer-rounded down. warn fires at/above WARN_PCT; exhausted at/above 100%.
    prefix is '[QUOTA n%]' once warn fires, else '' (so callers can blindly prepend it).
    """
    budget = budget or DEFAULT_BUDGET
    calls = calls_today(now=now, state_path=state_path)
    pct = int(calls * 100 // budget) if budget > 0 else 0
    warn = pct >= WARN_PCT
    return {
        "calls": calls,
        "budget": budget,
        "pct": pct,
        "warn": warn,
        "exhausted": pct >= 100,
        "prefix": f"[QUOTA {pct}%]" if warn else "",
    }


def with_prefix(text, budget=None, now=None, state_path=QUOTA_PATH):
    """Prepend the '[QUOTA n%]' marker to `text` iff the warning threshold is live."""
    st = status(budget=budget, now=now, state_path=state_path)
    return f"{st['prefix']} {text}" if st["prefix"] else text


if __name__ == "__main__":
    print(json.dumps(status(), indent=2))
