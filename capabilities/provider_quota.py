#!/usr/bin/env python3
"""provider_quota.py — SPEC-97: per-provider daily call meter (generalizes SPEC 66).

moralis_quota.py metered exactly one provider. The provider seam (onchain.token_transfers)
now has several — Etherscan-V2 on ETH today, the SPEC-101 Bitquery client and SPEC-102
local indexer on BSC next — and each has its own daily wall. Same contract as SPEC 66:
a per-UTC-day counter in state/<provider>_quota.json, an env-overridable budget
(<PROVIDER>_DAILY_BUDGET), and the `[QUOTA n%]` warn prefix at 80%. The meter only
warns; it never blocks the read it is metering.

Delegates the counter/threshold mechanics to moralis_quota (single source of truth);
`moralis` keeps its historical state file and budget so SPEC-66 behavior is unchanged.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import moralis_quota

STATE = moralis_quota.STATE

# Conservative default call budgets per provider. Etherscan free tier is ~100k/day @ 5 rps;
# 20k keeps the desk far from the wall (the recurring board/radar load is the quota killer,
# not ad-hoc lookbacks). Override per provider with <PROVIDER>_DAILY_BUDGET.
DEFAULT_BUDGETS = {"etherscan": 20_000, "bitquery": 800}   # SPEC-101: 800 = headroom under 1k/day free
FALLBACK_BUDGET = 2_000
WARN_PCT = moralis_quota.WARN_PCT


def state_path_for(provider):
    """moralis keeps its SPEC-66 state file; every other provider gets its own."""
    if provider == "moralis":
        return moralis_quota.QUOTA_PATH
    return STATE / f"{provider}_quota.json"


def budget_for(provider):
    """Env-overridable per-provider daily budget (<PROVIDER>_DAILY_BUDGET), read at call
    time so ops can tune without a restart."""
    env = os.environ.get(f"{provider.upper()}_DAILY_BUDGET")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    if provider == "moralis":
        return moralis_quota.DEFAULT_BUDGET
    return DEFAULT_BUDGETS.get(provider, FALLBACK_BUDGET)


def calls_today(provider, now=None, state_path=None):
    return moralis_quota.calls_today(now=now, state_path=state_path or state_path_for(provider))


def record_call(provider, now=None, state_path=None, n=1, caller=None, cu=None):
    """Increment `provider`'s counter for the current UTC day; returns the new total.
    caller/cu (SPEC-123) pass straight through to moralis_quota — every provider's state
    file gets the same per-caller + CU-estimate attribution."""
    return moralis_quota.record_call(now=now, state_path=state_path or state_path_for(provider),
                                     n=n, caller=caller, cu=cu)


def status(provider, budget=None, now=None, state_path=None):
    """The gas-gauge read: {calls, budget, pct, warn, exhausted, prefix} for one provider."""
    return moralis_quota.status(budget=budget or budget_for(provider), now=now,
                                state_path=state_path or state_path_for(provider))


def with_prefix(provider, text, budget=None, now=None, state_path=None):
    """Prepend '[QUOTA n%]' to `text` iff `provider`'s warning threshold is live."""
    st = status(provider, budget=budget, now=now, state_path=state_path)
    return f"{st['prefix']} {text}" if st["prefix"] else text


if __name__ == "__main__":
    import json
    prov = sys.argv[1] if len(sys.argv) > 1 else "moralis"
    print(json.dumps(status(prov), indent=2))
