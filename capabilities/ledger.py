#!/usr/bin/env python3
"""ledger.py — per-signature outcome scoreboard (SPEC 40).

Makes the §9 base-rate gate enforceable: every closed/retired thesis is one JSONL
record under state/ledger.jsonl; `stats` aggregates hit%/R per signature, live-filled
only (SPEC-139: replay quarantined, unfilled commits excluded), and flags `sizeable`
only at filled n≥10 AND total_r≥+5 AND avg_r>0. Below that a signature is a
hypothesis, not an edge.

Usage:
  python3 capabilities/ledger.py record --ticker VELVET --direction SHORT \
      --signature stage5_short --outcome stopped --pnl-r -1.0 \
      --commit-ts 2026-06-08 --close-ts 2026-06-09 --json
  python3 capabilities/ledger.py stats [--signature stage5_short] --json
  python3 capabilities/ledger.py backfill --json     # seed from retired watchlist theses
"""
import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO        = Path(__file__).resolve().parent.parent
LEDGER_PATH = REPO / "state" / "ledger.jsonl"
WL_PATH     = REPO / "config" / "watchlist.json"


def _tier_events_path():
    """SPEC-169: append-only tier-change log (`ts, signature, from, to, n_filled,
    total_r, trailing_10_r`) — `stats()` appends a row whenever a signature's computed
    tier differs from the last one recorded. ops/discovery_tick.sh tails new lines to
    fire the ntfy TIER class hook; ledger.py itself makes no network call.

    Derived from `LEDGER_PATH.parent` (not a fixed module constant) so every existing
    test that redirects `LEDGER_PATH` to a temp dir gets an isolated tier_events.jsonl
    alongside it for free — no test file needs a second monkeypatch to stay offline."""
    return LEDGER_PATH.parent / "tier_events.jsonl"

# §6 setup names + the catch-all. Anything else is a typo, not a new edge.
# SPEC-110: unlock_cliff_fade (§6 unlock-cliff fade) + spring_reclaim_long (§6 BASED-type
# post-blowoff spring long) added — existing names untouched.
# SPEC-168: faded_bounce (§0 item 1 — the user's PRIMARY edge, SPEC-120 `scan
# mode=faded_bounce`) added as first-class — it was silently collapsing into
# `discretionary`, which must never GO, corrupting both buckets.
SIGNATURES = ("trap_formation_long", "mindshare_top_short",
              "blowoff_top_short", "stage5_short",
              "unlock_cliff_fade", "spring_reclaim_long", "faded_bounce", "discretionary")
OUTCOMES   = ("stopped", "tp1", "tp2", "retired_unfilled", "zone_blown", "closed_manual")

# outcomes that count toward hit% (a decided position); retired_unfilled is
# signal-only — it counts in n but neither hits nor misses. closed_manual (SPEC-69:
# user discretionary close while every kill line was intact) likewise counts in n
# WITH its realized pnl_r but is neither hit nor miss — it says nothing about whether
# the committed stops work, so it must not pollute the kill-discipline stats.
_HITS   = ("tp1", "tp2")
_MISSES = ("stopped", "zone_blown")

# SPEC-139: a record is FILLED iff it has a numeric pnl_r or an outcome in this set;
# retired_unfilled/zone_blown never count as filled (they never got a real fill), even
# if a pnl_r somehow ended up on the row. 74% of live commits were unfilled — the §9
# gates are defined on filled trades only, so `stats` must split commits from fills.
_FILLED_OUTCOMES   = {"tp1", "tp2", "tp3", "stopped", "closed_manual"}
_UNFILLED_OUTCOMES = {"retired_unfilled", "zone_blown"}

# §9 thresholds (grill 2026-08-06 rewrite of CLAUDE.md §9): the GO gate is judged on
# LIVE **filled** records only — n>=10, total_r>=+5, avg_r>0. hit_pct is no longer a
# sizeable factor (dropped along with the old n>=10/hit%>50/total_r>0 gate).
SIZEABLE_N, SIZEABLE_TOTAL_R = 10, 5.0

# SPEC-169 (grill 2026-08-28 post-GO management): a `go`-tier signature whose
# trailing-10 total_r has decayed to at/below this line gets the `decaying` flag
# (still GO-sized, not yet demoted — demotion needs trailing_10_r < 0).
DECAYING_TRAILING_10_R = 1.0

# SPEC-150: the ledger is scoped to DESK-ORIGINATED calls only — the user also trades
# outside the desk (2026-08-19: "I take trades outside of what the desk tells me to
# do") and those are deliberately NOT recorded here (his call). Every row defaults
# `scope: "desk"`; an `off_desk` row (if one is ever recorded) is excluded from every
# desk-scoped aggregate unless explicitly requested. `stats()` is the desk's report
# card, never the user's P&L.
SCOPES = ("desk", "off_desk")

# SPEC-150: replaces the old subset-computed kill-switch. At desk-originated FILLED
# n=30, if no signature has printed GO (SIZEABLE_N/SIZEABLE_TOTAL_R above) and
# cumulative desk-scoped filled R is negative, the desk drops to tracking+veto only —
# it halts the DESK's calls, never the user's trading (a checkpoint computed on a
# subset of his trading cannot gate his trading).
DESK_CHECKPOINT_N = 30

# SPEC-139 data hygiene: a same (ticker, signature, outcome) row recorded within this
# many seconds of an existing one is very likely the same close logged twice (the TLM
# blowoff_top_short tp1 duplicate) — warn, never silently collapse (data loss risk).
_DUP_WINDOW_SECONDS = 30


def _is_filled(rec):
    """SPEC-139: filled iff a numeric pnl_r or a filled-set outcome; retired_unfilled/
    zone_blown are NEVER filled regardless of pnl_r — they tested nothing."""
    outcome = rec.get("outcome")
    if outcome in _UNFILLED_OUTCOMES:
        return False
    if outcome in _FILLED_OUTCOMES:
        return True
    return rec.get("pnl_r") is not None


# SPEC-90: the side each signature trades. `discretionary` is direction-neutral.
# SPEC-110: unlock_cliff_fade (§6) is a SHORT (fades the pre-cliff markup into the unlock);
# spring_reclaim_long (§6, BASED-type post-blowoff spring) is a LONG.
# SPEC-168: faded_bounce is a Cat-B SHORT (off-ATH >=40% + post-dump bounce + faded
# attention + no operator floor, §0 item 1).
_SHORT_SIGS = ("mindshare_top_short", "blowoff_top_short", "stage5_short",
               "unlock_cliff_fade", "faded_bounce")
_LONG_SIGS  = ("trap_formation_long", "spring_reclaim_long")


def _sig_side(sig):
    """SHORT / LONG / None (direction-neutral) for a canonical signature."""
    if sig in _SHORT_SIGS:
        return "SHORT"
    if sig in _LONG_SIGS:
        return "LONG"
    return None


def _is_watch_signature(sig):
    """SPEC-172: a `*_watch` signature is signal-only tripwire coverage (a WATCH-tier
    thesis, never a trade) — case-insensitive so a raw --signature typed in any case
    is still recognized before it ever reaches the heuristic."""
    return bool(sig) and str(sig).strip().lower().endswith("_watch")


def _is_watch_row(rec):
    """A row is WATCH-tier if its (verbatim) signature ends in `_watch` OR its recorded
    direction is literally `WATCH` — either one is enough on its own (a corrupted legacy
    row may carry a real trade signature under direction=='WATCH', or vice versa)."""
    return _is_watch_signature(rec.get("signature")) or (rec.get("direction") or "").upper() == "WATCH"


def _raw_canon_strict(s):
    """Pure name→enum resolution (no direction guard), same rules as `_raw_canon` but
    returns None instead of the `discretionary` catch-all when NOTHING matches — lets a
    caller (SPEC-110: the record CLI) distinguish a real heuristic hit from silently
    falling into the catch-all, so an unrecognized --signature can fail loudly instead of
    corrupting the §9 base-rate record under `discretionary`."""
    if s in SIGNATURES:
        return s
    if "blowoff" in s:
        return "blowoff_top_short"
    # SPEC-90: distribution/markdown/squeezer-breakdown SHORT semantics → stage5_short.
    # `squeezer` (a chronic squeezer being shorted on its breakdown) is short-side and must
    # be caught BEFORE the long "squeeze" branch; `distributing` (≠ "distribution") too.
    if ("stage5" in s or "stage-5" in s or "stage 5" in s or "stage45" in s or "stage4" in s
            or "distribution" in s or "distributing" in s or "markdown" in s
            or "squeezer" in s or "breakdown_short" in s):
        return "stage5_short"
    if "neg_funding" in s or "trap" in s or "squeeze" in s or "accumulation" in s:
        return "trap_formation_long"   # §4 neg-funding gate / trap-formation LONG
    # SPEC-168: checked BEFORE mindshare/catb — a faded_bounce setup string may also
    # contain "cat_b" (it IS a Cat-B setup), so faded_bounce must win the match first.
    if "faded_bounce" in s or "faded-bounce" in s or "faded bounce" in s:
        return "faded_bounce"          # §0 item 1 — off-ATH bounce, faded attention SHORT
    if "catb" in s or "cat_b" in s or "cat-b" in s or "mindshare" in s:
        return "mindshare_top_short"   # Cat-B distribution-top SHORT
    if "unlock_cliff" in s or "unlock-cliff" in s or "cliff_fade" in s:
        return "unlock_cliff_fade"     # §6 unlock-cliff fade
    if "spring_reclaim" in s or "spring reclaim" in s:
        return "spring_reclaim_long"   # §6 BASED-type post-blowoff spring long
    return None


def _raw_canon(s):
    """Pure name→enum resolution (no direction guard) — legacy always-resolves behavior
    (free-text auto-classification callers like replay/thesis/backfill rely on this never
    raising); the catch-all is `discretionary`."""
    return _raw_canon_strict(s) or "discretionary"


def canon_signature(raw, direction=None):
    """Map a free-form setup name (watchlist `setup` strings + the SPEC-59 setup keys) onto
    the enum. The SPEC-59 keys (blowoff/catb_top/trap_long/neg_funding_gate/stage45_short)
    map here so replay (SPEC 62) + thesis signatures (SPEC 63) bucket consistently.

    SPEC-90 direction guard: when `direction` is known, the result can NEVER contradict it —
    a SHORT record never lands in `trap_formation_long`, a LONG never in a `*_short` bucket
    (a free-form "...squeezer...short" once flipped a SHORT win onto the LONG base-rate). On a
    cross-direction resolution, fall back to the correct-side default (SHORT→stage5_short,
    LONG→trap_formation_long), never the opposite side. With no direction (query-time lookups)
    behavior is the legacy pure-name resolution."""
    sig = _raw_canon((raw or "").lower())
    d = (direction or "").upper()
    if d in ("LONG", "SHORT"):
        side = _sig_side(sig)
        if side is not None and side != d:
            return "trap_formation_long" if d == "LONG" else "stage5_short"
    return sig


def record_cli_signature(rec, raw_signature, allow_new=False):
    """SPEC-110: the `record` CLI's explicit --signature handling — distinct from the
    free-text auto-classification `canon_signature()` does elsewhere (replay/thesis/
    backfill), where silently landing on `discretionary` is correct behavior for
    unstructured text. Here the operator TYPED a specific signature name, so an unknown
    one must fail loudly (never silently rebucket to `discretionary` and corrupt the §9
    base-rate record) unless `allow_new` is explicitly passed.

    A raw string that resolves via a REAL heuristic bucket (not the catch-all) still works
    without --allow-new — e.g. "distributing_squeezer_breakdown_short" -> stage5_short —
    preserving existing desk usage. Only a genuine unknown (falls through every heuristic)
    requires --allow-new.

    SPEC-172: two cases NEVER touch the heuristic, regardless of `allow_new`:
    `--allow-new` itself means VERBATIM (the operator typed a specific name and the
    heuristic must not "helpfully" rebucket it — "squeeze_exhaust_watch" once landed
    under trap_formation_long because `_raw_canon_strict` matched "squeeze" first), and
    any `*_watch` signature (or a WATCH-direction row) — a watch-tier thesis is signal-only
    tripwire coverage, never a trade, and must never corrupt a trade signature's base rate
    even when the desk didn't pass --allow-new."""
    s = (raw_signature or "").strip().lower()
    direction = rec.get("direction")
    if allow_new or _is_watch_signature(s) or (direction or "").upper() == "WATCH":
        return record({**rec, "signature": raw_signature}, allow_new=True)
    matched = _raw_canon_strict(s)
    if matched is None and s != "discretionary":
        raise ValueError(
            f"unknown signature {raw_signature!r}; known: {list(SIGNATURES)} or pass --allow-new")
    signature = canon_signature(raw_signature, direction=direction)
    return record({**rec, "signature": signature}, allow_new=allow_new)


def _load():
    if not LEDGER_PATH.exists():
        return []
    recs = []
    for ln in LEDGER_PATH.read_text().splitlines():
        ln = ln.strip()
        if ln:
            try:
                recs.append(json.loads(ln))
            except ValueError:
                continue            # a corrupt line never takes the ledger down
    return recs


def _near_dup(rec, existing):
    """SPEC-139: True if `existing` already holds a same (ticker, signature, outcome)
    row within _DUP_WINDOW_SECONDS of `rec`'s recorded_ts — the TLM double-record."""
    key = (rec.get("ticker"), rec.get("signature"), rec.get("outcome"))
    ts = rec.get("recorded_ts")
    if not ts:
        return False
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    for r in existing:
        if (r.get("ticker"), r.get("signature"), r.get("outcome")) != key:
            continue
        rts = r.get("recorded_ts")
        if not rts:
            continue
        try:
            rt = datetime.strptime(rts, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
        if abs((t - rt).total_seconds()) <= _DUP_WINDOW_SECONDS:
            return True
    return False


def record(rec, allow_new=False):
    """Validate + append one outcome record. Returns {recorded, record[, dup_warning]}.
    `allow_new` (SPEC-110): bypass the known-signature whitelist — the CLI's explicit
    --allow-new escape hatch for a genuinely new signature; never the default."""
    sig = rec.get("signature")
    if sig not in SIGNATURES and not allow_new:
        raise ValueError(f"signature must be one of {SIGNATURES}, got {sig!r} "
                         f"(canon_signature() maps free-form setup names; pass allow_new for a new one)")
    if rec.get("outcome") not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {rec.get('outcome')!r}")
    if not rec.get("ticker"):
        raise ValueError("ticker is required")
    if rec.get("pnl_r") is not None:
        rec["pnl_r"] = float(rec["pnl_r"])
    # SPEC-150: every row is scoped ("desk" default — off_desk is never inferred, only
    # explicit) and carries desk_disagreed (default False — "the desk granted permission").
    rec.setdefault("scope", "desk")
    if rec["scope"] not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}, got {rec['scope']!r}")
    rec.setdefault("desk_disagreed", False)
    rec["desk_disagreed"] = bool(rec["desk_disagreed"])
    rec.setdefault("recorded_ts", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    dup = _near_dup(rec, _load())          # SPEC-139: warn, never silently collapse
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    out = {"recorded": True, "record": rec}
    if dup:
        out["dup_warning"] = (f"possible duplicate: {rec.get('ticker')}/{sig}/{rec.get('outcome')} "
                              f"recorded within {_DUP_WINDOW_SECONDS}s of an existing row")
    return out


def _agg(recs):
    """(n, hit_pct, avg_r, total_r) for a record set."""
    n = len(recs)
    decided = [r for r in recs if r["outcome"] in _HITS + _MISSES]
    hits = sum(1 for r in decided if r["outcome"] in _HITS)
    hit_pct = round(100.0 * hits / len(decided), 1) if decided else 0.0
    rs = [r["pnl_r"] for r in recs if r.get("pnl_r") is not None]
    total_r = round(sum(rs), 2) if rs else 0.0
    avg_r = round(sum(rs) / len(rs), 2) if rs else None
    return n, hit_pct, avg_r, total_r


def _by_source(recs):
    """SPEC 63: split a signature's records by row source (live = unset / replay / backfill)
    so the §9 gate never silently mixes a backtest or a seed with booked live R."""
    buckets = {}
    for r in recs:
        buckets.setdefault(r.get("source") or "live", []).append(r)
    out = {}
    for src, rs in sorted(buckets.items()):
        n, hit_pct, avg_r, total_r = _agg(rs)
        out[src] = {"n": n, "hit_pct": hit_pct, "avg_r": avg_r, "total_r": total_r}
    return out


def _close_sort_key(rec):
    """Ascending close_ts; missing/unparseable close_ts sorts LAST (stable) — a row with
    no close date must never masquerade as older evidence in the trailing-10 window."""
    ts = rec.get("close_ts")
    try:
        return (0, datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) if ts else (1, datetime.min)
    except (ValueError, TypeError):
        return (1, datetime.min)


def _filled_sorted_by_close(filled):
    return sorted(filled, key=_close_sort_key)


def _window_r(rows):
    """(total_r, avg_r) over a list of rows' pnl_r, ignoring rows with no pnl_r."""
    rs = [r["pnl_r"] for r in rows if r.get("pnl_r") is not None]
    total = round(sum(rs), 2) if rs else 0.0
    avg = (sum(rs) / len(rs)) if rs else None
    return total, avg


def _tier_state(signature, filled_sorted):
    """SPEC-169: tier/trailing_10_r/tier_flag for one signature, computed from its FILLED
    rows in close_ts order.

    `trailing_10_r` = total R of the last 10 filled rows (whatever exists over 1-9; null
    if zero filled).

    `tier` walks the filled history forward rebuilding the promote/demote hysteresis:
    `ever_go` latches TRUE the first time any length->=10 PREFIX of the history clears the
    §9 bars (total_r>=+5, avg_r>0); once latched, `demoted` latches TRUE whenever the
    then-current trailing-10 window goes negative, and clears only when a later trailing-10
    window itself re-clears the full GO bars (>=+5 total AND avg>0) — a trailing-10 merely
    back above zero is not enough. Final tier: `go` iff the ALL-TIME record is sizeable AND
    not demoted; `demoted` iff the demoted latch is still set; else `hypothesis`.
    `discretionary` is pinned to `hypothesis` regardless (§9: it must never GO)."""
    n = len(filled_sorted)
    trailing_10_r = None
    if n:
        trailing_10_r, _ = _window_r(filled_sorted[-10:])

    if signature == "discretionary":
        return "hypothesis", trailing_10_r, None

    ever_go = False
    demoted = False
    for i in range(1, n + 1):
        cum_total, cum_avg = _window_r(filled_sorted[:i])
        if i >= SIZEABLE_N and cum_total >= SIZEABLE_TOTAL_R and cum_avg is not None and cum_avg > 0:
            ever_go = True
        if ever_go and i >= SIZEABLE_N:
            win_total, win_avg = _window_r(filled_sorted[max(0, i - 10):i])
            if demoted:
                if win_total >= SIZEABLE_TOTAL_R and win_avg is not None and win_avg > 0:
                    demoted = False
            elif win_total < 0:
                demoted = True

    _, _, avg_r, total_r = _agg(filled_sorted)
    sizeable = bool(n >= SIZEABLE_N and total_r >= SIZEABLE_TOTAL_R
                    and avg_r is not None and avg_r > 0)
    if sizeable and not demoted:
        tier = "go"
    elif demoted:
        tier = "demoted"
    else:
        tier = "hypothesis"
    tier_flag = ("decaying" if (tier == "go" and trailing_10_r is not None
                                and trailing_10_r <= DECAYING_TRAILING_10_R) else None)
    return tier, trailing_10_r, tier_flag


def _last_recorded_tier(signature):
    """Most recent tier_events row for `signature`, or None."""
    p = _tier_events_path()
    if not p.exists():
        return None
    last = None
    for ln in p.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("signature") == signature:
            last = r
    return last


def _record_tier_event(signature, to_tier, n_filled, total_r, trailing_10_r):
    """Append a tier_events row iff `to_tier` differs from the last recorded tier for this
    signature (default prior state: hypothesis — every signature starts there). Returns
    the appended event dict, or None when nothing changed."""
    prev = _last_recorded_tier(signature)
    from_tier = prev["to"] if prev else "hypothesis"
    if from_tier == to_tier:
        return None
    ev = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "signature": signature, "from": from_tier, "to": to_tier,
          "n_filled": n_filled, "total_r": total_r, "trailing_10_r": trailing_10_r}
    p = _tier_events_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(ev) + "\n")
    return ev


def _row(recs, include_replay=False, signature=None):
    """Aggregate one signature's records into the stats row (SPEC-139: headline numbers
    — n_commits/n_filled/fill_rate/hit_pct/avg_r/total_r/sizeable — compute from LIVE
    records only; replay is quarantined to `by_source` (always) and, if asked for, a
    separate `replay` block — never summed into the headline)."""
    live = [r for r in recs if (r.get("source") or "live") == "live"]
    filled = [r for r in live if _is_filled(r)]
    n_commits = len(live)
    n_filled = len(filled)
    fill_rate = round(n_filled / n_commits, 3) if n_commits else None
    _, hit_pct, avg_r, total_r = _agg(filled)
    sizeable = bool(n_filled >= SIZEABLE_N and total_r >= SIZEABLE_TOTAL_R
                    and avg_r is not None and avg_r > 0)
    tier, trailing_10_r, tier_flag = _tier_state(signature, _filled_sorted_by_close(filled))
    row = {
        "n_commits": n_commits, "n": n_commits,          # `n` kept as an n_commits alias
        "n_filled": n_filled, "fill_rate": fill_rate,
        "hit_pct": hit_pct, "avg_r": avg_r, "total_r": total_r,
        "by_source": _by_source(recs),
        "last_5": [{"ticker": r["ticker"], "outcome": r["outcome"],
                    "pnl_r": r.get("pnl_r"), "source": r.get("source") or "live"}
                   for r in reversed(live[-5:])],
        "sizeable": sizeable,
        "tier": tier, "trailing_10_r": trailing_10_r, "tier_flag": tier_flag,
    }
    if include_replay:
        replay_recs = [r for r in recs if r.get("source") == "replay"]
        rn, rhit, ravg, rtotal = _agg(replay_recs)
        row["replay"] = {"n": rn, "hit_pct": rhit, "avg_r": ravg, "total_r": rtotal}
    return row


def _counterfactual_rescore_gap(recs):
    """SPEC-147 req 5: how many `source: counterfactual` rows in `recs` still lack
    `rescored_ts` — a counterfactual row written before the unified fill rule (SPEC-147)
    landed, never re-scored under it. `stats` must say so rather than silently blending a
    rescored and a pre-rescore population (they can disagree on pnl_r for the same call)."""
    cf = [r for r in recs if r.get("source") == "counterfactual"]
    unrescored = sum(1 for r in cf if not r.get("rescored_ts"))
    return {"counterfactual_n": len(cf), "counterfactual_unrescored_n": unrescored,
           "counterfactual_fully_rescored": unrescored == 0}


def _population_agg(recs):
    """n_filled/hit_pct/avg_r/total_r for a LIVE FILLED population (SPEC-150: the shape
    shared by the desk-agreed/desk-disagreed split)."""
    filled = [r for r in recs if (r.get("source") or "live") == "live" and _is_filled(r)]
    n, hit_pct, avg_r, total_r = _agg(filled)
    return {"n_filled": n, "hit_pct": hit_pct, "avg_r": avg_r, "total_r": total_r}


def _desk_disagreed_split(recs):
    """SPEC-150 req 2: desk-agreed vs desk-disagreed populations, each aggregated the
    same way. `rules_change` fires only once the disagreed population has its own
    filled n>=10 (the §9 base-rate bar applied to the override population) AND it
    outperforms the agreed population on avg_r — the number that settles whether the
    desk's rules or the user's overrides are right, and the desk must be willing to
    lose that argument."""
    agreed = _population_agg([r for r in recs if not r.get("desk_disagreed")])
    disagreed = _population_agg([r for r in recs if r.get("desk_disagreed")])
    rules_change = bool(disagreed["n_filled"] >= SIZEABLE_N and disagreed["avg_r"] is not None
                        and (agreed["avg_r"] is None or disagreed["avg_r"] > agreed["avg_r"]))
    return {"agreed": agreed, "disagreed": disagreed, "rules_change": rules_change}


def _desk_checkpoint(signatures, n_filled, total_r):
    """SPEC-150 req 3: replaces the old subset-computed kill-switch. `checkpoint` is
    "HALT_CALLS" only once desk-scoped filled n reaches DESK_CHECKPOINT_N with no
    signature having printed GO (`sizeable`) and cumulative filled R negative — else
    None (never a surprise: `progress` always reports where the count stands)."""
    any_go = any(row.get("sizeable") for row in signatures.values())
    progress = {"n_filled": n_filled, "threshold": DESK_CHECKPOINT_N,
               "n_remaining": max(0, DESK_CHECKPOINT_N - n_filled)}
    checkpoint = ("HALT_CALLS" if (n_filled >= DESK_CHECKPOINT_N and not any_go and total_r < 0)
                 else None)
    return checkpoint, progress


def _earned_signatures(signatures):
    """SPEC-150 req 4 / SPEC-149 tier: signatures with live filled n>=1 AND total_r>0 —
    the ledger's earned set (today: trap_formation_long, stage5_short). A signature with
    zero fills or a negative/zero total_r never appears."""
    return sorted(sig for sig, row in signatures.items()
                 if (row.get("n_filled") or 0) >= 1 and (row.get("total_r") or 0) > 0)


def _watch_bucket(watch_recs):
    """SPEC-172 req (b): WATCH-tier rows never feed a trade signature's base rate, but
    they aren't discarded — grouped by their own (verbatim) signature under a `watch`
    bucket in `stats`, n/tickers only (hit%/R aren't meaningful on signal-only rows)."""
    by_sig = {}
    for r in watch_recs:
        by_sig.setdefault(r.get("signature") or "watch", []).append(r)
    return {sig: {"n": len(rs), "tickers": sorted({r.get("ticker") for r in rs if r.get("ticker")})}
           for sig, rs in sorted(by_sig.items())}


def stats(signature=None, include_replay=False, include_off_desk=False):
    """Aggregate per signature; with `signature`, one full row + its (live) records.
    `include_replay` (SPEC-139): surface a per-signature `replay` aggregate alongside the
    live-only headline — it is display-only and never feeds `sizeable` or the totals.
    `include_off_desk` (SPEC-150): every aggregate is scoped to `scope: "desk"` rows by
    default (a row with no `scope` field — pre-SPEC-150 — counts as desk); pass True to
    also fold in `off_desk` rows (never the default — this is the desk's report card).
    SPEC-172: WATCH-tier rows (`_is_watch_row`) are split out FIRST — they never enter
    any per-signature trade aggregate (n_commits included), live only under the `watch`
    bucket."""
    all_recs = _load()
    if not include_off_desk:
        all_recs = [r for r in all_recs if (r.get("scope") or "desk") == "desk"]
    watch_recs = [r for r in all_recs if _is_watch_row(r)]
    recs = [r for r in all_recs if not _is_watch_row(r)]
    watch_bucket = _watch_bucket(watch_recs)
    if signature is not None:
        mine = [r for r in recs if r.get("signature") == signature]
        if not mine:
            return {"signature": signature, "n_commits": 0, "n": 0, "n_filled": 0,
                    "fill_rate": None, "hit_pct": 0.0, "avg_r": None, "total_r": 0.0,
                    "last_5": [], "sizeable": False, "records": [],
                    "tier": "hypothesis", "trailing_10_r": None, "tier_flag": None,
                    "watch": watch_bucket,
                    **_counterfactual_rescore_gap([])}
        row = _row(mine, include_replay=include_replay, signature=signature)
        row["signature"] = signature
        row["records"] = [r for r in mine if (r.get("source") or "live") == "live"]
        row["watch"] = watch_bucket
        row.update(_counterfactual_rescore_gap(mine))
        return row
    by_sig = {}
    for r in recs:
        by_sig.setdefault(r.get("signature", "discretionary"), []).append(r)
    signatures = {sig: _row(rs, include_replay=include_replay, signature=sig)
                 for sig, rs in sorted(by_sig.items())}
    # SPEC-169: append a tier_events row for every signature whose freshly-computed tier
    # differs from the last one on record — the durable "fill #10 printed GO" trail.
    # ntfy delivery is NOT done here (ledger.py makes no network call, house style) —
    # ops/discovery_tick.sh tails new tier_events lines and fires the TIER-class page.
    tier_events_appended = [ev for ev in
        (_record_tier_event(sig, row["tier"], row["n_filled"], row["total_r"], row["trailing_10_r"])
         for sig, row in signatures.items()) if ev]
    # SPEC-139/150: cumulative LIVE filled n and total_r across every (desk-scoped)
    # signature — the desk_checkpoint's own inputs.
    live_n_filled = sum(row["n_filled"] for row in signatures.values())
    live_total_r = round(sum(row["total_r"] for row in signatures.values()), 2)
    desk_checkpoint, desk_checkpoint_progress = _desk_checkpoint(signatures, live_n_filled, live_total_r)
    return {"total_records": len(recs) + len(watch_recs),
            "signatures": signatures,
            "watch": watch_bucket,
            "summary": {"live_n_filled": live_n_filled, "live_total_r": live_total_r,
                       "desk_disagreed_split": _desk_disagreed_split(recs),
                       "desk_checkpoint": desk_checkpoint,
                       "desk_checkpoint_progress": desk_checkpoint_progress,
                       "earned_signatures": _earned_signatures(signatures),
                       "tier_events_appended": tier_events_appended,
                       **_counterfactual_rescore_gap(recs)}}


# ── backfill — seed from retired theses already in the watchlist ─────────────

def _infer_outcome(state, thesis):
    """Best-effort outcome from the free-text state string of a dead thesis."""
    s = (state or "").lower()
    if re.search(r"\bstop(ped)?\b.*(hit|printed|breach|through)|\bstopped\b", s):
        return "stopped"
    if "zone blown" in s or "blew the zone" in s or "ran past" in s:
        return "zone_blown"
    if re.search(r"\btp2?\s*(banked|hit|printed)|banked tp2", s):
        return "tp2"
    if re.search(r"\btp1?\s*(banked|hit|printed)|banked", s):
        return "tp1"
    return "retired_unfilled"


def backfill(wl_path=None):
    """Seed the ledger from dead theses (status RETIRED/PASS) in the watchlist.
    Idempotent: a (ticker, commit_ts) already in the ledger is never re-seeded.
    pnl_r stays null — backfilled rows are signal-history, not booked R."""
    wl_path = Path(wl_path) if wl_path else WL_PATH
    raw = json.loads(wl_path.read_text())
    tokens = raw.get("tokens", raw) if isinstance(raw, dict) else raw
    existing = {(r.get("ticker"), r.get("commit_ts")) for r in _load()}

    seeded = []
    for tok in tokens:
        th = tok.get("thesis") or {}
        if (th.get("status") or "").upper() not in ("RETIRED", "PASS"):
            continue
        ticker = tok.get("ticker", "?")
        commit_ts = th.get("committed_ts")
        if (ticker, commit_ts) in existing:
            continue
        state = tok.get("state", "")
        rec = {
            "ticker": ticker,
            "direction": (th.get("direction") or "?").upper(),
            "signature": canon_signature(th.get("setup"), th.get("direction")),
            "entry": None, "stop": th.get("stop"),
            "tp": th.get("tp") or th.get("tps") or [],
            "outcome": _infer_outcome(state, th),
            "pnl_r": None,
            "commit_ts": commit_ts, "close_ts": None,
            "notes": f"backfill: {state[:140]}",
            "source": "backfill",
        }
        record(rec)
        seeded.append(ticker)
    return {"seeded": len(seeded), "tickers": seeded}


# ── SPEC-162: open-commit sweep — feed the paper-track scorer from ACTIVE commits ──
# `backfill()` above only ever seeds RETIRED/PASS theses — an actively-committed thesis
# (PENDING/ARMED/OPEN/LIVE) has NO ledger row at all until someone retires it, so the
# paper-track scorer (counterfactual.backfill) never sees it. `commit_open` +
# `sweep_open_commits` close that gap: every committed thesis gets an OPEN row
# (outcome=None, pnl_r=None, source=live) the moment it's committed, carrying a geometry
# SNAPSHOT so scoring survives a later board rewrite/re-anchor/retirement.

def _geometry_snapshot(th):
    """Verbatim copy of the committed-thesis fields the paper-track scorer needs — the
    row becomes self-contained (req 3: counterfactual reads THIS before ever touching
    the watchlist)."""
    return {
        "direction": th.get("direction"),
        "entry_zone": th.get("entry_zone"),
        "stop": th.get("stop"),
        "tp": th.get("tp") or th.get("tps"),
        "entry_mode": th.get("entry_mode"),
        "legs": th.get("legs"),
    }


def _has_scoreable_geometry(th):
    """req 1: top-level entry_zone+stop+tp (the same `top_scoreable` triple
    counterfactual.backfill checks), OR at least one leg (SPEC-155) carrying
    (entry|entry_zone)+stop. False means this commit is UNSCOREABLE — recorded anyway
    (never refused outright) so the no-geometry cost stays a visible, countable fact."""
    if th.get("entry_zone") and th.get("stop") is not None and bool(th.get("tp") or th.get("tps")):
        return True
    legs = th.get("legs")
    if isinstance(legs, list):
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            has_entry = leg.get("entry_zone") or leg.get("entry") is not None
            if has_entry and leg.get("stop") is not None:
                return True
    return False


def commit_open(thesis_row):
    """SPEC-162 req 1: write ONE open ledger row for `thesis_row` (a watchlist token,
    `{"ticker", "thesis": {...}}`) — `outcome=None, pnl_r=None, source="live"` plus an
    inline `geometry` snapshot. Pure ledger write; never touches the watchlist. Raises
    ValueError on a missing ticker/committed_ts — the caller (the sweep, or a human via
    the CLI) is expected to only call this on an actually-committed thesis."""
    ticker = (thesis_row.get("ticker") or "").upper()
    if not ticker:
        raise ValueError("ticker is required")
    th = thesis_row.get("thesis") or {}
    committed_ts = th.get("committed_ts")
    if not committed_ts:
        raise ValueError("thesis.committed_ts is required")
    rec = {
        "ticker": ticker,
        "direction": (th.get("direction") or "?").upper(),
        "signature": canon_signature(th.get("signature") or th.get("setup") or "",
                                     direction=th.get("direction")),
        "outcome": None,
        "pnl_r": None,
        "source": "live",
        "scope": "desk",
        "desk_disagreed": False,
        "commit_ts": committed_ts,
        "close_ts": None,
        "geometry": _geometry_snapshot(th),
        "unscoreable": not _has_scoreable_geometry(th),
        "notes": "",
        "recorded_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return {"recorded": True, "record": rec}


def _open_live_rows(recs=None):
    """Existing OPEN live rows (source=live, outcome=None — never a resolved/retired
    row, whatever its pnl_r)."""
    recs = _load() if recs is None else recs
    return [r for r in recs if (r.get("source") or "live") == "live" and r.get("outcome") is None]


def _retire_orphaned_open_rows(stale_keys):
    """Rewrite the ledger, marking every OPEN row whose (ticker, commit_ts) is in
    `stale_keys` as `outcome:"retired_unfilled", notes:"window_re-anchored"` — the
    standing re-anchor policy (CLAUDE.md §0.5) resets `committed_ts` on every
    arm/reconciliation, which would otherwise orphan the row forever (`no_geometry`,
    silently, since the exact-ts join can never match again). Every other line passes
    through byte-identical; a corrupt line is preserved untouched."""
    if not stale_keys or not LEDGER_PATH.exists():
        return
    out_lines = []
    for ln in LEDGER_PATH.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            out_lines.append(ln)
            continue
        key = (r.get("ticker"), r.get("commit_ts"))
        if (key in stale_keys and r.get("outcome") is None
                and (r.get("source") or "live") == "live"):
            r["outcome"] = "retired_unfilled"
            r["notes"] = ((r.get("notes") or "") + " window_re-anchored").strip()
        out_lines.append(json.dumps(r))
    LEDGER_PATH.write_text("\n".join(out_lines) + "\n")


def sweep_open_commits(wl_path=None):
    """SPEC-162 req 2: diff `config/watchlist.json` against existing OPEN ledger rows
    and `commit_open` any (ticker, committed_ts) not yet recorded — the orchestrator's
    manual step disappears. A re-timestamped `committed_ts` for a ticker that already
    has an open row is a RE-ANCHOR (standing policy), not a new thesis: every stale open
    row for that ticker is retired (`_retire_orphaned_open_rows`) and a fresh one is
    written for the new timestamp — so re-arms never double-count in `n_commits`.

    Idempotent (req 4): a (ticker, committed_ts) that already has a matching open row is
    skipped — a second run with no watchlist changes creates nothing. Best-effort: a
    missing/malformed watchlist degrades to a no-op, never raises (this runs on a
    schedule, unattended)."""
    wl_path = Path(wl_path) if wl_path else WL_PATH
    try:
        raw = json.loads(wl_path.read_text())
    except (OSError, ValueError):
        return {"created": [], "retired": [], "skipped": []}
    tokens = raw.get("tokens", raw) if isinstance(raw, dict) else raw

    open_by_ticker = {}
    for r in _open_live_rows():
        open_by_ticker.setdefault(r.get("ticker"), []).append(r)

    created, retired, skipped, stale_keys = [], [], [], set()
    for tok in tokens or []:
        th = tok.get("thesis") or {}
        committed_ts = th.get("committed_ts")
        if not committed_ts:
            continue
        ticker = (tok.get("ticker") or "").upper()
        existing = open_by_ticker.get(ticker, [])
        if any(r.get("commit_ts") == committed_ts for r in existing):
            skipped.append(ticker)
            continue
        for r in existing:                     # every OTHER open row is now orphaned
            stale_keys.add((r.get("ticker"), r.get("commit_ts")))
            retired.append({"ticker": ticker, "commit_ts": r.get("commit_ts")})
        commit_open({"ticker": ticker, "thesis": th})
        created.append({"ticker": ticker, "commit_ts": committed_ts})

    _retire_orphaned_open_rows(stale_keys)
    return {"created": created, "retired": retired, "skipped": skipped}


# ── SPEC-69 one-off migration — the H row predates the closed_manual outcome ──

def migrate_spec69():
    """Recategorize the single pre-SPEC-69 row: ticker H, closed 2026-06-12, recorded as
    `stopped` pnl_r 0.0 with an apology in the notes (the outcome didn't exist yet) →
    `closed_manual`. Targeted by ticker + close date + the 'SPEC-69' marker the orchestrator
    left in the notes; idempotent (a migrated row no longer matches); every other row is
    rewritten byte-identical."""
    if not LEDGER_PATH.exists():
        return {"migrated": 0, "reason": "no ledger"}
    migrated = 0
    out_lines = []
    for ln in LEDGER_PATH.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            out_lines.append(ln)        # corrupt line passes through untouched
            continue
        if (r.get("ticker") == "H" and r.get("outcome") == "stopped"
                and (r.get("close_ts") or "").startswith("2026-06-12")
                and "SPEC-69" in (r.get("notes") or "")):
            r["outcome"] = "closed_manual"
            r["notes"] = (r["notes"] + " [SPEC-69 migration: stopped → closed_manual, "
                          "manual close with all kill lines intact]")
            migrated += 1
            out_lines.append(json.dumps(r))
        else:
            out_lines.append(ln)
    if migrated:
        LEDGER_PATH.write_text("\n".join(out_lines) + "\n")
    return {"migrated": migrated}


# ── SPEC-90 one-off migration — repair direction-flipped signatures ──────────

def migrate_spec90():
    """Repair rows whose committed `signature` contradicts the trade `direction` — the
    canon_signature direction guard didn't exist when they were written. A SHORT row sitting
    in a LONG signature (the BLESS breakdown-short win that landed in `trap_formation_long`),
    or vice-versa, is re-canonicalized to the correct-side default. Idempotent: a repaired
    row's signature now agrees with its direction and no longer matches. Every other row is
    rewritten byte-identical."""
    if not LEDGER_PATH.exists():
        return {"migrated": 0, "reason": "no ledger"}
    migrated = 0
    out_lines = []
    for ln in LEDGER_PATH.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            out_lines.append(ln)        # corrupt line passes through untouched
            continue
        d = (r.get("direction") or "").upper()
        sig = r.get("signature")
        side = _sig_side(sig)
        if d in ("LONG", "SHORT") and side is not None and side != d:
            fixed = canon_signature(sig, direction=d)
            r["signature"] = fixed
            r["notes"] = ((r.get("notes") or "")
                          + f" [SPEC-90 migration: {sig} → {fixed}, direction-flip repair]").strip()
            migrated += 1
            out_lines.append(json.dumps(r))
        else:
            out_lines.append(ln)
    if migrated:
        LEDGER_PATH.write_text("\n".join(out_lines) + "\n")
    return {"migrated": migrated}


# ── SPEC-110 one-off migration — re-tag the M unlock-cliff-fade record ───────────

SPEC110_RETAG_TARGETS = (
    # (ticker, commit_ts, from_signature, to_signature) — a record must match ALL FOUR
    # fields before it moves; anything else is untouched. One-shot, hand-enumerated (not
    # a heuristic re-scan) — the whole point is this must never move an unrelated row.
    ("M", "2026-07-03", "discretionary", "unlock_cliff_fade"),
)


def migrate_spec110():
    """Re-tag the 2026-07-07-discovered M record: `ledger record --signature
    unlock_cliff_fade` silently normalized to `discretionary` (the taxonomy didn't admit
    the §6 unlock-cliff-fade setup yet), so the n=2/2 hypothesis-tier win is invisible to
    `stats`'s §9 base-rate gate. Idempotent — a moved row no longer matches its own
    (ticker, commit_ts, signature) key. Every other row is rewritten byte-identical."""
    if not LEDGER_PATH.exists():
        return {"migrated": 0, "reason": "no ledger"}
    migrated = 0
    out_lines = []
    for ln in LEDGER_PATH.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            out_lines.append(ln)        # corrupt line passes through untouched
            continue
        hit = next((t for t in SPEC110_RETAG_TARGETS
                   if r.get("ticker") == t[0] and r.get("commit_ts") == t[1]
                   and r.get("signature") == t[2]), None)
        if hit:
            _, _, old_sig, new_sig = hit
            r["signature"] = new_sig
            r["notes"] = ((r.get("notes") or "")
                          + f" [SPEC-110 migration: {old_sig} → {new_sig}]").strip()
            migrated += 1
            out_lines.append(json.dumps(r))
        else:
            out_lines.append(ln)
    if migrated:
        LEDGER_PATH.write_text("\n".join(out_lines) + "\n")
    return {"migrated": migrated}


# ── SPEC-168 one-off migration — retag pre-existing faded_bounce rows ────────

def _faded_bounce_wl_keys(wl_path):
    """(ticker, committed_ts) pairs for every watchlist thesis (tokens/retired/parked)
    tagged `signature: faded_bounce` — the join key `retag_from_watchlist` matches
    ledger rows against."""
    raw = json.loads(wl_path.read_text())
    buckets = []
    if isinstance(raw, dict):
        for key in ("tokens", "retired", "parked"):
            buckets.extend(raw.get(key) or [])
    else:
        buckets = raw
    keys = set()
    for tok in buckets:
        th = tok.get("thesis") or {}
        if (th.get("signature") or "").lower() == "faded_bounce":
            ticker = (tok.get("ticker") or "").upper()
            committed_ts = th.get("committed_ts")
            if ticker and committed_ts:
                keys.add((ticker, committed_ts))
    return keys


def retag_from_watchlist(wl_path=None, apply=False):
    """SPEC-168 req 3: for every ledger row (live AND counterfactual, any source)
    whose `signature == "discretionary"` and whose (ticker, commit_ts) matches a
    watchlist thesis carrying `signature: faded_bounce`, rewrite signature ->
    faded_bounce. Dry-run by default (`apply=False`): computes and returns the would-be
    retag list without writing. `apply=True` backs up the ledger first (`.bak-spec168`)
    then writes. Idempotent: an already-retagged row's signature is no longer
    `discretionary` so a second run retags nothing further."""
    wl_path = Path(wl_path) if wl_path else WL_PATH
    faded_keys = _faded_bounce_wl_keys(wl_path)
    if not LEDGER_PATH.exists():
        return {"retagged": [], "applied": False, "reason": "no ledger"}

    retagged = []
    out_lines = []
    for ln in LEDGER_PATH.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            out_lines.append(ln)        # corrupt line passes through untouched
            continue
        key = ((r.get("ticker") or "").upper(), r.get("commit_ts"))
        if r.get("signature") == "discretionary" and key in faded_keys:
            r["signature"] = "faded_bounce"
            r["notes"] = ((r.get("notes") or "")
                          + " [SPEC-168 migration: discretionary -> faded_bounce]").strip()
            retagged.append({"ticker": r.get("ticker"), "commit_ts": r.get("commit_ts"),
                             "source": r.get("source") or "live"})
        out_lines.append(json.dumps(r))

    applied = False
    if apply and retagged:
        bak = LEDGER_PATH.parent / (LEDGER_PATH.name + ".bak-spec168")
        shutil.copy2(LEDGER_PATH, bak)
        LEDGER_PATH.write_text("\n".join(out_lines) + "\n")
        applied = True
    return {"retagged": retagged, "applied": applied}


# ── SPEC-172 req (c) — migrate rows heuristic-mapped into a trade signature before the fix

_TRADE_SIGNATURES = tuple(s for s in SIGNATURES if s != "discretionary")


def _watchlist_signature_by_ticker(wl_path):
    """{TICKER: thesis.signature} for every watchlist entry that names one — the join
    source `migrate_watch_rows` re-tags a mistagged WATCH row from."""
    try:
        raw = json.loads(Path(wl_path).read_text())
    except (OSError, ValueError):
        return {}
    tokens = raw.get("tokens", raw) if isinstance(raw, dict) else (raw or [])
    out = {}
    for tok in tokens or []:
        if not isinstance(tok, dict):
            continue
        sig = (tok.get("thesis") or {}).get("signature")
        ticker = (tok.get("ticker") or "").upper()
        if ticker and sig:
            out[ticker] = sig
    return out


def migrate_watch_rows(wl_path=None, apply=False):
    """SPEC-172 req (c): find ledger rows with `direction == "WATCH"` whose stored
    `signature` is a real trade signature (the pre-fix `record_cli_signature` heuristic
    bug's fingerprint — e.g. "squeeze_exhaust_watch" landing under trap_formation_long)
    and re-tag them from the matching watchlist entry's `thesis.signature`. Dry-run by
    default; `apply=True` rewrites `LEDGER_PATH` in place. Always returns the full
    `mistagged` list, found or fixed. A row with no matching/no-signature watchlist entry
    is left alone (nothing to re-tag it TO) but is NOT reported as mistagged — silence
    here would otherwise read as "nothing wrong" when it's really "can't fix it yet"; the
    caller can diff `mistagged` against a raw WATCH-row count to see the gap."""
    wl_path = Path(wl_path) if wl_path else WL_PATH
    thesis_sig = _watchlist_signature_by_ticker(wl_path)
    if not LEDGER_PATH.exists():
        return {"dry_run": not apply, "mistagged": []}
    mistagged = []
    out_lines = []
    for ln in LEDGER_PATH.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            out_lines.append(ln)          # corrupt line passes through untouched
            continue
        if (r.get("direction") or "").upper() == "WATCH" and r.get("signature") in _TRADE_SIGNATURES:
            new_sig = thesis_sig.get((r.get("ticker") or "").upper())
            if new_sig and new_sig != r.get("signature"):
                mistagged.append({"ticker": r.get("ticker"), "from": r.get("signature"), "to": new_sig})
                if apply:
                    r["signature"] = new_sig
        out_lines.append(json.dumps(r))
    if apply and mistagged:
        LEDGER_PATH.write_text("\n".join(out_lines) + "\n")
    return {"dry_run": not apply, "mistagged": mistagged}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="per-signature outcome scoreboard (SPEC 40)")
    ap.add_argument("action", choices=["record", "stats", "backfill", "commit", "sweep",
                                        "migrate-spec69", "migrate-spec90", "migrate-spec110",
                                        "migrate-watch", "retag"])
    ap.add_argument("--from-watchlist", dest="from_watchlist", action="store_true",
                    help="SPEC-168: retag's join source — currently the only supported "
                         "mode (kept explicit so a future non-watchlist retag source "
                         "can't be invoked by accident)")
    ap.add_argument("--apply", action="store_true",
                    help="SPEC-168: write the retag (default is dry-run, prints only)")
    ap.add_argument("--wl-path", dest="wl_path", default=None,
                    help="SPEC-162: watchlist path for commit/sweep (defaults to "
                         "config/watchlist.json)")
    ap.add_argument("--ticker")
    ap.add_argument("--direction")
    ap.add_argument("--signature")
    # nargs="?" (not store_true): the orchestrator's [--flag {key}] invoke templates always
    # fill a present key with its value (SPEC-89 pull5 --onchain precedent) — a bare
    # store_true flag would choke on that trailing token.
    ap.add_argument("--allow-new", dest="allow_new", nargs="?", const="1", default="",
                    help="SPEC-110: persist an unrecognized --signature verbatim instead of "
                         "failing loudly")
    ap.add_argument("--entry", type=float)
    ap.add_argument("--stop", type=float)
    ap.add_argument("--tp", help="comma-separated TP levels, e.g. 0.30,0.184")
    ap.add_argument("--outcome", choices=OUTCOMES)
    ap.add_argument("--pnl-r", dest="pnl_r", type=float)
    ap.add_argument("--commit-ts", dest="commit_ts")
    ap.add_argument("--close-ts", dest="close_ts")
    ap.add_argument("--notes", default="")
    ap.add_argument("--include-replay", dest="include_replay", action="store_true",
                    help="SPEC-139: also surface per-signature replay aggregates "
                         "(display-only, never feeds sizeable/headline totals)")
    ap.add_argument("--scope", choices=SCOPES, default="desk",
                    help="SPEC-150: desk-originated (default) or off_desk")
    ap.add_argument("--desk-disagreed", dest="desk_disagreed", action="store_true",
                    help="SPEC-150: the desk would NOT have taken this trade (user override)")
    ap.add_argument("--include-off-desk", dest="include_off_desk", action="store_true",
                    help="SPEC-150: fold off_desk rows into stats (never the default — "
                         "this is the desk's report card)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        if args.action == "record":
            tps = [float(x) for x in args.tp.split(",")] if args.tp else []
            rec = {
                "ticker": (args.ticker or "").upper(), "direction": (args.direction or "?").upper(),
                "entry": args.entry, "stop": args.stop, "tp": tps,
                "outcome": args.outcome, "pnl_r": args.pnl_r,
                "commit_ts": args.commit_ts, "close_ts": args.close_ts,
                "notes": args.notes, "scope": args.scope, "desk_disagreed": args.desk_disagreed,
            }
            # SPEC-110: an explicitly-typed --signature goes through the strict CLI
            # validator (fails loudly on an unknown name unless --allow-new); omitting
            # --signature keeps the legacy behavior of an unset signature.
            if args.signature:
                out = record_cli_signature(rec, args.signature, allow_new=bool(args.allow_new))
            else:
                out = record({**rec, "signature": None})
        elif args.action == "stats":
            out = stats(signature=canon_signature(args.signature) if args.signature else None,
                       include_replay=args.include_replay, include_off_desk=args.include_off_desk)
        elif args.action == "commit":
            # SPEC-162: --ticker's CURRENT committed thesis, read straight off the
            # watchlist (the same source of truth the sweep diffs against).
            if not args.ticker:
                raise ValueError("commit requires --ticker")
            wl_path = Path(args.wl_path) if args.wl_path else WL_PATH
            wl = json.loads(wl_path.read_text())
            tok = next((t for t in wl.get("tokens", [])
                       if (t.get("ticker") or "").upper() == args.ticker.upper()), None)
            if tok is None or not tok.get("thesis"):
                raise ValueError(f"{args.ticker}: no committed thesis on the watchlist")
            out = commit_open(tok)
        elif args.action == "sweep":
            out = sweep_open_commits(wl_path=args.wl_path)
        elif args.action == "migrate-spec69":
            out = migrate_spec69()
        elif args.action == "migrate-spec90":
            out = migrate_spec90()
        elif args.action == "migrate-spec110":
            out = migrate_spec110()
        elif args.action == "migrate-watch":
            out = migrate_watch_rows(wl_path=args.wl_path, apply=args.apply)
        elif args.action == "retag":
            if not args.from_watchlist:
                raise ValueError("retag requires --from-watchlist")
            out = retag_from_watchlist(wl_path=args.wl_path, apply=args.apply)
        else:
            out = backfill()
    except (ValueError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    print(json.dumps(out, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
