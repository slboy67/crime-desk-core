#!/usr/bin/env python3
"""oi_construction.py — SPEC-179: OI-type decomposition + venue roles.

CLAUDE.md §0.6 item 3 (how OI is CONSTRUCTED — the part the framework calls the heart
of the counterparty read) was computed nowhere: no arb-vs-directional decomposition,
no mechanical venue-role read. This capability closes that gap.

  python3 capabilities/oi_construction.py LAB --json

**The output may only VETO** ("this OI is not squeeze fuel"), never manufacture
confidence — UNKNOWN is first-class and always carries its reason. Every role/type
resolves to a value / SPLIT / NONE_DETECTED / UNKNOWN.

## Boundaries (hard)
CALLS the keyless layer live (venue_map, oi_mc.build_battlefield, cvd.resolve_spot_venue,
and — SPEC-182 — stake_schedule via `fetch_lock_info`/`config/lock_registry.json`, all
keyless RPC, never Moralis); the OI-funding elasticity floor (SPEC-182) reads the
SPEC-178 sampler store (`state/oi_samples/`) live, also a local read, never a fetch.
CONSUMES on-chain from EXISTING desk state (never initiates a fresh Moralis call) — the
chip-control (`chip_state`) and operator-deposit-rail (`flow_confirmed`) reads are
INJECTED by the caller from state it already has (board rows, `verify_wallet` runs
elsewhere); `lock_info` is caller-injectable too but defaults to the live
`fetch_lock_info` wiring when the caller passes nothing. Writes ONLY
`state/venue_roles/<TICKER>.json` (plus the local `state/lock_info_cache.json` cache) —
`config/venue_roles.json` is the CURATED file and is read-only for every code path here
(SPEC-191 #4).

## OI types (G1) — mandatory evidence + supporting signals
Five types, each `evaluate_<type>(...)` a pure function: FUNDING_FARM, VESTING_HEDGE,
OPERATOR_AMM, MM_INVENTORY, CROSS_VENUE_FUNDING_ARB. Mandatory-alone is NEVER enough to
assert (FUNDING_FARM's carry-viability-only degrades to `farm_viable_unconfirmed`,
still UNKNOWN); VESTING_HEDGE/OPERATOR_AMM/MM_INVENTORY/CROSS_VENUE_FUNDING_ARB gate
HARD on their mandatory signal (missing it -> NOT_ASSERTED, no amount of supporting
tape overrides that — the "missing-lock fixture with perfect §4-B tape" case).

**Contamination rule (G1)**: a suspected OPERATOR_AMM (asserted) downgrades
account-ratio-divergence evidence for every OTHER type on the same name — the
top/account-ratio split OPERATOR_AMM itself produces would otherwise double-count as
"evidence" for FUNDING_FARM. `build_oi_construction` enforces this by withholding
`account_ratio_divergence` from `evaluate_funding_farm` whenever OPERATOR_AMM asserts.

## Venue roles (G2)
`resolve_mark_engine`/`resolve_size_book`/`resolve_exit`/`resolve_hedge` — pure,
each consuming already-fetched inputs (`venue_map`'s own composed fields for
size_book/hedge; an injectable index-constituents dict for mark_engine; an
injectable flow-confirmed dict + `cvd.resolve_spot_venue` for exit).

## Roll-up (G3)
`roll_up_verdict`: `ARB_DOMINATED` when a non-directional type reads `dominant`;
`MIXED` when anything asserted (OPERATOR_AMM asserting forces >= MIXED on its own);
`DIRECTIONAL` only when every instrument RAN (asserted-or-not-asserted) and none
asserted; else `UNKNOWN`.
"""
import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import venue_map as VM       # noqa: E402
import oi_mc as OM           # noqa: E402
import cvd as CVD            # noqa: E402
import stake_schedule as SS  # noqa: E402 — keyless RPC only, no Moralis (SPEC-182)

VENUE_ROLES_PATH = ROOT / "config" / "venue_roles.json"   # SPEC-191 #4: CURATED, read-only
VENUE_ROLES_STATE_DIR = ROOT / "state" / "venue_roles"     # SPEC-191 #4: live snapshots live here
OI_SAMPLES_DIR = ROOT / "state" / "oi_samples"
LOCK_REGISTRY_PATH = ROOT / "config" / "lock_registry.json"
LOCK_INFO_CACHE_PATH = ROOT / "state" / "lock_info_cache.json"

# TTLs (hours unless noted) — Boundaries section
TTL_ROLES_H = 4
TTL_EXIT_FLOW_DAYS = 14

# Elasticity floor (SPEC-182 — req 1)
ELASTICITY_WINDOW_S = 3 * 86400   # 3 days lookback
ELASTICITY_BUCKET_MIN = 240       # 4h coverage buckets (matches the common funding interval)
ELASTICITY_MIN_SETTLEMENTS = 3
ELASTICITY_MIN_COVERAGE_PCT = 70.0

FARM_APR_HURDLE_PCT = 10.0     # rule of thumb: ~+0.01%/8h ~= 10.9% APR is roughly the
                                # floor where farm OI starts arriving (R1 S1)
SIZE_BOOK_MIN_SHARE_PCT = 40.0
SIZE_BOOK_SPLIT_BAND_PCT = 15.0
MARK_ENGINE_MIN_WEIGHT_PCT = 20.0

NON_DIRECTIONAL_TYPES = ("FUNDING_FARM", "VESTING_HEDGE", "MM_INVENTORY",
                         "CROSS_VENUE_FUNDING_ARB")


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _evidence(pairs):
    return [{"signal": k, "detail": v} for k, v in pairs if v]


# ---------------------------------------------------------------------------
# OI types (G1)
# ---------------------------------------------------------------------------

def evaluate_funding_farm(funding_pi_4h=None, elasticity_confirmed=None,
                          account_ratio_divergence=None, basis_behaving=None):
    """Mandatory: carry viability (|APR| >= FARM_APR_HURDLE_PCT in the funding-paid
    direction). Supporting: >=1 of {elasticity, account-ratio divergence, basis
    behaving}. Viability ALONE (no supporting signal) degrades to UNKNOWN
    (`farm_viable_unconfirmed`) — it is an economic bound, not an identification."""
    base = {"type": "FUNDING_FARM", "side": None, "share_read": "unknown", "evidence": [],
           "mandatory_met": False}
    if funding_pi_4h is None:
        return {**base, "verdict": "UNKNOWN", "reason": "funding unavailable"}
    apr_pct = funding_pi_4h * 6 * 365   # %/4h -> %/day (x6) -> %/year (x365)
    side = "short" if funding_pi_4h > 0 else "long"
    if abs(apr_pct) < FARM_APR_HURDLE_PCT:
        return {**base, "side": side, "verdict": "NOT_ASSERTED",
               "reason": f"carry not viable (~{apr_pct:.1f}% APR)"}
    supporting = _evidence((("oi_funding_elasticity", elasticity_confirmed),
                            ("account_ratio_divergence", account_ratio_divergence),
                            ("basis_behaving", basis_behaving)))
    if not supporting:
        return {**base, "side": side, "mandatory_met": True, "verdict": "UNKNOWN",
               "reason": "farm_viable_unconfirmed"}
    share = "dominant" if len(supporting) >= 2 else "material"
    return {**base, "side": side, "share_read": share, "evidence": supporting,
           "mandatory_met": True, "verdict": "ASSERTED"}


def evaluate_vesting_hedge(lock_cliff=None, carry_violation=None, perp_discount=None,
                           pristine_chain=None, hl_direct_observation=None):
    """Mandatory: on-chain lock contract w/ cliff ahead (`stake_schedule`, injected by
    the caller as `lock_cliff`). Missing lock -> NOT_ASSERTED regardless of how
    perfect the supporting tape is (the mandatory gate — no amount of §4-B pattern
    substitutes for the contract). Self-retires: the caller stops passing `lock_cliff`
    once the cliff has passed."""
    base = {"type": "VESTING_HEDGE", "side": "short", "share_read": "unknown",
           "evidence": [], "mandatory_met": False}
    if not lock_cliff:
        return {**base, "verdict": "NOT_ASSERTED",
               "reason": "no on-chain lock contract with a cliff ahead"}
    supporting = _evidence((("carry_violation", carry_violation),
                            ("perp_discount", perp_discount),
                            ("pristine_chain", pristine_chain),
                            ("hl_direct_observation", hl_direct_observation)))
    share = ("dominant" if hl_direct_observation else
            "material" if len(supporting) >= 2 else "minor" if supporting else "unknown")
    return {**base, "share_read": share, "evidence": supporting, "mandatory_met": True,
           "verdict": "ASSERTED", "cliff_date": lock_cliff, "on_carry_decay": bool(carry_violation)}


def evaluate_operator_amm(chip_control=None, deep_neg_at_highs=None, no_free_spot=None,
                          no_lock_contract=None, squeeze_cadence=None, quota_dead=False):
    """Mandatory: chip control (§2 Cat-A read from EXISTING desk state, injected).
    quota-dead / chip_control unreadable -> UNKNOWN, unassertable, never fabricated."""
    base = {"type": "OPERATOR_AMM", "side": "long", "share_read": "unknown", "evidence": [],
           "mandatory_met": False}
    if quota_dead or chip_control is None:
        return {**base, "verdict": "UNKNOWN",
               "reason": "chip-control read unassertable (quota-dead)" if quota_dead
               else "chip-control read unavailable"}
    if not chip_control:
        return {**base, "verdict": "NOT_ASSERTED", "reason": "no chip-control evidence"}
    supporting = _evidence((("deep_neg_at_highs", deep_neg_at_highs),
                            ("no_free_spot", no_free_spot),
                            ("no_lock_contract", no_lock_contract),
                            ("squeeze_cadence", squeeze_cadence)))
    share = "dominant" if len(supporting) >= 3 else "material" if supporting else "minor"
    return {**base, "share_read": share, "evidence": supporting, "mandatory_met": True,
           "verdict": "ASSERTED"}


def evaluate_mm_inventory(top_book_neutral=None, oi_insensitive_to_funding=None,
                          oi_stable_under_vol=None):
    """Mandatory: top-book neutrality (top-trader ratios ~=1.00 through a trend, or
    HTX `locked_ratio` elevated — injected as `top_book_neutral`)."""
    base = {"type": "MM_INVENTORY", "side": "both", "share_read": "unknown", "evidence": [],
           "mandatory_met": False}
    if top_book_neutral is None:
        return {**base, "verdict": "UNKNOWN", "reason": "top-book neutrality unreadable"}
    if not top_book_neutral:
        return {**base, "verdict": "NOT_ASSERTED", "reason": "top book not neutral"}
    supporting = _evidence((("oi_insensitive_to_funding", oi_insensitive_to_funding),
                            ("oi_stable_under_vol", oi_stable_under_vol)))
    return {**base, "share_read": "material" if supporting else "minor", "evidence": supporting,
           "mandatory_met": True, "verdict": "ASSERTED"}


def evaluate_cross_venue_arb(dispersion=None, oi_elevated_both=None, price_inelastic=None,
                             paired_unwind=None):
    """Mandatory: persistent cross-venue funding dispersion, computed from the desk's
    own normalized funding table (injected as `dispersion`, a bool). Sets
    `overstated_by_cross_venue` on the aggregate (the ~2x double-count flag)."""
    base = {"type": "CROSS_VENUE_FUNDING_ARB", "side": "both", "share_read": "unknown",
           "evidence": [], "mandatory_met": False}
    if dispersion is None:
        return {**base, "verdict": "UNKNOWN", "reason": "cross-venue funding dispersion unreadable"}
    if not dispersion:
        return {**base, "verdict": "NOT_ASSERTED", "reason": "no persistent cross-venue dispersion"}
    supporting = _evidence((("oi_elevated_both_venues", oi_elevated_both),
                            ("price_inelastic", price_inelastic),
                            ("paired_unwind", paired_unwind)))
    share = "dominant" if len(supporting) >= 2 else "material" if supporting else "minor"
    return {**base, "share_read": share, "evidence": supporting, "mandatory_met": True,
           "verdict": "ASSERTED", "overstated_by_cross_venue": share in ("material", "dominant")}


def roll_up_verdict(oi_types):
    """ARB_DOMINATED = a non-directional type read dominant. MIXED = anything
    asserted (OPERATOR_AMM asserting forces >= MIXED regardless of the other reads —
    it "forces its own printed line" per G3). DIRECTIONAL only when every instrument
    RAN (asserted-or-not-asserted, i.e. its mandatory signal was actually readable)
    and none asserted. else UNKNOWN (nothing ran)."""
    asserted = [t for t in oi_types if t.get("verdict") == "ASSERTED"]
    ran = [t for t in oi_types if t.get("verdict") in ("ASSERTED", "NOT_ASSERTED")]
    if any(t["type"] in NON_DIRECTIONAL_TYPES and t.get("share_read") == "dominant"
          for t in asserted):
        return "ARB_DOMINATED"
    operator_amm = next((t for t in oi_types if t["type"] == "OPERATOR_AMM"), None)
    if operator_amm and operator_amm.get("verdict") == "ASSERTED":
        return "MIXED"
    if asserted:
        return "MIXED"
    if ran and len(ran) == len(oi_types):
        return "DIRECTIONAL"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Venue roles (G2) — pure resolvers over already-fetched inputs
# ---------------------------------------------------------------------------

def resolve_mark_engine(constituents=None, anchor="aster", threshold_pct=MARK_ENGINE_MIN_WEIGHT_PCT):
    """`constituents`: {venue: weight_pct} (already-fetched index composition, e.g.
    Aster `fapi/v3/indexreferences`). UNKNOWN when unpublished everywhere."""
    if not constituents:
        return {"venues": [], "anchor": anchor, "weights": {}, "verdict": "UNKNOWN",
               "reason": "index constituents unpublished everywhere"}
    venues = sorted((v for v, w in constituents.items() if w >= threshold_pct),
                    key=lambda v: -constituents[v])
    verdict = "SPLIT" if len(venues) > 1 else (venues[0] if venues else "NONE_DETECTED")
    return {"venues": venues, "anchor": anchor, "weights": dict(constituents), "verdict": verdict}


def resolve_size_book(oi_share_by_venue=None, errored_plausible=False,
                      min_share_pct=SIZE_BOOK_MIN_SHARE_PCT, split_band_pct=SIZE_BOOK_SPLIT_BAND_PCT):
    """`oi_share_by_venue`: {venue: oi_share_pct} — `venue_map`'s own per-venue field,
    zero new fetch. A plausible-size venue erroring mid-sweep -> UNKNOWN (a partial
    sweep can't crown a winner)."""
    if errored_plausible:
        return {"venue": None, "share_pct": None, "verdict": "UNKNOWN",
               "reason": "a plausible-size venue errored — partial sweep can't crown"}
    if not oi_share_by_venue:
        return {"venue": None, "share_pct": None, "verdict": "UNKNOWN",
               "reason": "no venue reported OI"}
    ranked = sorted(oi_share_by_venue.items(), key=lambda kv: -kv[1])
    top_v, top_s = ranked[0]
    if top_s < min_share_pct:
        return {"venue": top_v, "share_pct": top_s, "verdict": "UNKNOWN",
               "reason": f"top share {top_s:.1f}% below the {min_share_pct:.0f}% crowning floor"}
    if len(ranked) > 1 and (top_s - ranked[1][1]) <= split_band_pct:
        return {"venue": top_v, "share_pct": top_s, "verdict": "SPLIT",
               "runner_up": ranked[1][0], "runner_up_share_pct": ranked[1][1]}
    return {"venue": top_v, "share_pct": top_s, "verdict": top_v}


def resolve_exit(flow_confirmed=None, depth_venue=None, depth_vol_usd=None,
                 max_flow_age_days=TTL_EXIT_FLOW_DAYS):
    """`flow_confirmed`: {"venue":…, "age_days":…} — a tracked/operator deposit rail
    terminating at a venue, injected from EXISTING state (never a fresh Moralis
    call). Beats DEPTH_INFERRED (`cvd.resolve_spot_venue`'s deepest real spot book).
    No flow + no spot -> UNKNOWN (print: perp-only red flag)."""
    if flow_confirmed and flow_confirmed.get("age_days") is not None \
            and flow_confirmed["age_days"] <= max_flow_age_days:
        return {"venue": flow_confirmed["venue"], "grade": "FLOW_CONFIRMED",
               "flow_age_h": round(flow_confirmed["age_days"] * 24, 1)}
    if depth_venue:
        return {"venue": depth_venue, "grade": "DEPTH_INFERRED", "flow_age_h": None,
               "vol24h_usd": depth_vol_usd}
    return {"venue": None, "grade": "UNKNOWN", "flow_age_h": None,
           "reason": "no flow + no spot — perp-only red flag"}


def _funding_is_outlier(venues, extreme):
    """Sign flip or >=2x magnitude vs the pack median (excluding the extreme itself
    and floor prints) -> a genuine outlier. None when unreadable (too few venues)."""
    if not extreme:
        return None
    vals = [b.get("funding_pi_4h") for b in (venues or {}).values()
           if b.get("status") == "ok" and not b.get("is_floor")
           and b.get("funding_pi_4h") is not None]
    ev = extreme.get("pi_4h")
    others = [v for v in vals if v != ev]
    if not others:
        return None
    others_sorted = sorted(others)
    med = others_sorted[len(others_sorted) // 2]
    if med == 0:
        return ev != 0
    return (ev * med < 0) or (abs(ev) >= 2 * abs(med))


def resolve_hedge(is_outlier=None, elevated_oi_share=False, contrarian_basis=False):
    """Mandatory: a persistent funding outlier vs the pack (sign or >=2x magnitude,
    `venue_map.funding_extreme` + `_funding_is_outlier`). +1 of {elevated OI share,
    contrarian basis}. NONE_DETECTED is a normal answer, distinct from UNKNOWN."""
    if is_outlier is None:
        return {"verdict": "UNKNOWN", "reason": "funding-outlier read unavailable"}
    if not is_outlier:
        return {"verdict": "NONE_DETECTED"}
    evidence = _evidence((("elevated_oi_share", elevated_oi_share),
                          ("contrarian_basis", contrarian_basis)))
    if evidence:
        return {"verdict": "ASSERTED", "evidence": evidence}
    return {"verdict": "UNKNOWN", "reason": "funding outlier present but no corroborating signal"}


def compute_gating_ok(roles_as_of_ts, now_ts, ttl_roles_h=TTL_ROLES_H):
    """`gating_ok=false` when the roles block (the veto-bearing block) is stale — a
    stale read prints as context, never gates (governing principle, SPEC-180 G4)."""
    if roles_as_of_ts is None or now_ts is None:
        return False
    return (now_ts - roles_as_of_ts) <= ttl_roles_h * 3600


# ---------------------------------------------------------------------------
# mark_engine constituents — the one genuinely new live endpoint (G2)
# ---------------------------------------------------------------------------

_EXCHANGE_TO_VENUE = {"binance": "binance", "bybit": "bybit", "okex": "okx", "okx": "okx",
                     "gateio": "gate", "gate": "gate", "kucoin": "kucoin", "mexc": "mexc",
                     "bitget": "bitget", "coinbase": "coinbase", "huobi": "htx", "htx": "htx"}


def _http_get(url, timeout=8):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "oi-construction/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None


def fetch_mark_constituents(ticker, anchor="aster"):
    """Aster's own index composition (`fapi/v3/indexreferences` — NOT the v1 clone
    path, G2) first; Binance's `fapi/v1/constituents` as the fallback (size-book
    venue's own index endpoint, per R2). Weight fields are fractions (0-1) on both —
    converted to percent. None when unpublished/unlisted on both."""
    sym = f"{ticker.upper()}USDT"
    for url in (f"https://fapi.asterdex.com/fapi/v3/indexreferences?symbol={sym}",
               f"https://fapi.binance.com/fapi/v1/constituents?symbol={sym}"):
        d = _http_get(url)
        if not isinstance(d, dict):
            continue
        rows = d.get("references") or d.get("constituents")
        if not rows:
            continue
        out = {}
        for row in rows:
            try:
                ex = _EXCHANGE_TO_VENUE.get((row.get("exchange") or "").lower(), row.get("exchange"))
                w = float(row["weight"]) * 100
                out[ex] = round(out.get(ex, 0) + w, 4)
            except (KeyError, TypeError, ValueError):
                continue
        if out:
            return out
    return None


# ---------------------------------------------------------------------------
# Elasticity floor (req 1) — joins state/oi_samples/<SYM>.jsonl to funding
# settlements. A "settlement" is detected as a change in a venue's own
# funding_pi_4h between consecutive stored `status:"ok"` rows (the sampler
# schema carries no interval_min field, so the settlement itself — not a
# derived interval — is the join key). Coverage is measured in fixed-size
# time buckets across the window (sampler-uptime proxy), independent of the
# settlement count.
# ---------------------------------------------------------------------------

def _read_oi_sample_rows(ticker, store_dir=None):
    p = (store_dir or OI_SAMPLES_DIR) / f"{ticker.upper()}.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _elasticity_from_store(ticker, window=ELASTICITY_WINDOW_S, store_dir=None,
                           now_ts=None, bucket_min=ELASTICITY_BUCKET_MIN):
    """SPEC-182 req 1. Returns {elasticity_confirmed, n_settlements,
    coverage_pct, degraded_reason}. Floor: n_settlements>=3 AND coverage>=70%,
    else elasticity_confirmed=None (no file / <3 settlements -> "no_history";
    enough settlements but coverage <70% -> "gappy"). Respects redenomination
    markers — a venue's settlements are counted only in its own segment AFTER
    its last marker (raw-unit OI is not comparable across one)."""
    now_ts = now_ts if now_ts is not None else time.time()
    rows = _read_oi_sample_rows(ticker, store_dir=store_dir)
    if not rows:
        return {"elasticity_confirmed": None, "n_settlements": 0,
               "coverage_pct": 0.0, "degraded_reason": "no_history"}

    window_start = now_ts - window
    bucket_s = bucket_min * 60
    total_buckets = max(1, math.ceil(window / bucket_s))

    by_venue = {}
    for r in rows:
        by_venue.setdefault(r.get("venue"), []).append(r)

    covered_buckets = set()
    settlements = []
    for venue, vrows in by_venue.items():
        vrows.sort(key=lambda r: r.get("ts", 0))
        last_marker_ts = None
        for r in vrows:
            if r.get("status") == "redenomination":
                last_marker_ts = r.get("ts")
        ok_rows = [r for r in vrows if r.get("status") == "ok"
                  and window_start <= r.get("ts", -1) < now_ts
                  and (last_marker_ts is None or r.get("ts", 0) > last_marker_ts)]
        for r in ok_rows:
            bucket = int((r["ts"] - window_start) // bucket_s)
            if 0 <= bucket < total_buckets:
                covered_buckets.add(bucket)
        prev = None
        for r in ok_rows:
            if prev is not None and r.get("funding_pi_4h") is not None \
                    and prev.get("funding_pi_4h") is not None \
                    and r["funding_pi_4h"] != prev["funding_pi_4h"] \
                    and r.get("oi_raw") is not None and prev.get("oi_raw") is not None:
                d_oi = r["oi_raw"] - prev["oi_raw"]
                d_funding_abs = abs(r["funding_pi_4h"]) - abs(prev["funding_pi_4h"])
                concordant = d_oi != 0 and d_funding_abs != 0 and (d_oi > 0) == (d_funding_abs > 0)
                settlements.append({"venue": venue, "ts": r["ts"], "concordant": concordant})
            prev = r

    n_settlements = len(settlements)
    coverage_pct = round(100.0 * len(covered_buckets) / total_buckets, 2)

    if n_settlements < ELASTICITY_MIN_SETTLEMENTS:
        return {"elasticity_confirmed": None, "n_settlements": n_settlements,
               "coverage_pct": coverage_pct, "degraded_reason": "no_history"}
    if coverage_pct < ELASTICITY_MIN_COVERAGE_PCT:
        return {"elasticity_confirmed": None, "n_settlements": n_settlements,
               "coverage_pct": coverage_pct, "degraded_reason": "gappy"}
    concordant_n = sum(1 for s in settlements if s["concordant"])
    return {"elasticity_confirmed": concordant_n > n_settlements / 2,
           "n_settlements": n_settlements, "coverage_pct": coverage_pct,
           "degraded_reason": None}


# ---------------------------------------------------------------------------
# lock_info wiring (req 2) — a small ticker->contract registry resolves WHICH
# lock contract to read; stake_schedule (keyless RPC, no Moralis) reads it.
# Cached until the cliff passes so a resolved lock isn't re-scanned
# (chunked eth_getLogs) on every call.
# ---------------------------------------------------------------------------

def _load_lock_registry(path=None):
    p = path or LOCK_REGISTRY_PATH
    try:
        d = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {(e.get("ticker") or "").upper(): e for e in (d.get("registry") or []) if e.get("ticker")}


def _load_lock_cache(path=None):
    p = path or LOCK_INFO_CACHE_PATH
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_lock_cache(cache, path=None):
    p = path or LOCK_INFO_CACHE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=1))


def fetch_lock_info(ticker, now_ts=None, registry=None, build_schedule_fn=None,
                    cache=None, cache_path=None, registry_path=None):
    """Returns (lock_info|None, attempted). No registry entry for `ticker` ->
    (None, False) — not an attempt, same as any other uninjected desk-state
    signal (no degraded row). A registry entry present -> stake_schedule is
    read (cached until the cached cliff_date passes) -> (info, True) on a
    resolved cliff, (None, True) when stake_schedule finds nothing (the
    caller appends the degraded row for that case)."""
    now_ts = now_ts if now_ts is not None else time.time()
    reg = registry if registry is not None else _load_lock_registry(registry_path)
    entry = reg.get(ticker.upper())
    if not entry:
        return None, False

    key = ticker.upper()
    cache = cache if cache is not None else _load_lock_cache(cache_path)
    cached = cache.get(key)
    if cached and cached.get("cliff_date"):
        try:
            cliff_ts = datetime.strptime(cached["cliff_date"], "%Y-%m-%d") \
                .replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            cliff_ts = None
        if cliff_ts is not None and now_ts < cliff_ts:
            return cached.get("lock_info"), True

    bs_fn = build_schedule_fn or SS.build_schedule
    try:
        result = bs_fn(entry["contract"], entry.get("chain", "bsc"))
    except Exception:  # noqa: BLE001 — a dead RPC degrades, never raises upstream
        return None, True

    cliffs = result.get("cliffs") or []
    if not cliffs:
        cache[key] = {"cliff_date": None, "lock_info": None, "as_of": _iso(now_ts)}
        _save_lock_cache(cache, cache_path)
        return None, True

    cliff = min(cliffs, key=lambda c: c["date"])
    info = {"has_lock": True, "cliff_date": cliff["date"]}
    cache[key] = {"cliff_date": cliff["date"], "lock_info": info, "as_of": _iso(now_ts)}
    _save_lock_cache(cache, cache_path)
    return info, True


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def build_oi_construction(ticker, now_ts=None, venue_map_fn=None, battlefield_fn=None,
                          spot_resolver=None, mark_constituents_fn=None,
                          flow_confirmed=None, chip_state=None, lock_info=None,
                          top_book_neutral=None, cross_venue_dispersion=None,
                          roles_as_of_ts=None, elasticity_fn=None, lock_info_fn=None):
    """The full envelope (G3 contract). Every live call is best-effort/degrade-explicit
    (§3) — a dead leg appends to `degraded` and the rest of the read proceeds.
    `flow_confirmed`/`chip_state`/`lock_info` are the injection points for EXISTING
    desk state (never fetched here — Boundaries section)."""
    now_ts = now_ts if now_ts is not None else time.time()
    degraded = []

    vm_fn = venue_map_fn or VM.build_venue_map
    try:
        vm = vm_fn(ticker) or {}
    except Exception as e:  # noqa: BLE001
        vm = {}
        degraded.append({"instrument": "venue_map", "reason": str(e)[:160]})
    venues = vm.get("venues") or {}

    oi_share_by_venue = {v: b["oi_share_pct"] for v, b in venues.items()
                         if b.get("status") == "ok" and b.get("oi_share_pct") is not None}
    errored_plausible = any(b.get("status") == "error" for b in venues.values())
    size_book = resolve_size_book(oi_share_by_venue, errored_plausible=errored_plausible)

    constituents = None
    mc_fn = mark_constituents_fn if mark_constituents_fn is not None else fetch_mark_constituents
    try:
        constituents = mc_fn(ticker) if mc_fn else None
    except Exception as e:  # noqa: BLE001
        degraded.append({"instrument": "mark_engine", "reason": str(e)[:160]})
    mark_engine = resolve_mark_engine(constituents)

    spot_venue = spot_vol = None
    resolver = spot_resolver or CVD.resolve_spot_venue
    try:
        spot_venue, spot_vol, _ = resolver(ticker)
    except Exception as e:  # noqa: BLE001
        degraded.append({"instrument": "cvd.resolve_spot_venue", "reason": str(e)[:160]})
    exit_role = resolve_exit(flow_confirmed=flow_confirmed, depth_venue=spot_venue,
                             depth_vol_usd=spot_vol)

    fe = vm.get("funding_extreme")
    is_outlier = _funding_is_outlier(venues, fe)
    hedge = resolve_hedge(is_outlier=is_outlier)

    roles_ts = roles_as_of_ts if roles_as_of_ts is not None else now_ts
    venue_roles = {"mark_engine": mark_engine, "size_book": size_book, "exit": exit_role,
                   "hedge": hedge, "as_of": _iso(roles_ts)}

    bf_fn = battlefield_fn or OM.build_battlefield
    try:
        battlefield = bf_fn(perp_vol_24h=vm.get("total_vol24h_usd"), spot_vol_24h=spot_vol)
    except Exception as e:  # noqa: BLE001
        battlefield = {"battlefield": "UNKNOWN"}
        degraded.append({"instrument": "battlefield", "reason": str(e)[:160]})

    resolved_lock_info = lock_info
    if lock_info is None:
        li_fn = lock_info_fn or fetch_lock_info
        try:
            resolved_lock_info, lock_attempted = li_fn(ticker, now_ts=now_ts)
        except Exception as e:  # noqa: BLE001
            resolved_lock_info, lock_attempted = None, True
            degraded.append({"instrument": "lock_info", "reason": str(e)[:160]})
        if lock_attempted and resolved_lock_info is None:
            degraded.append({"instrument": "lock_info",
                             "reason": "no resolvable on-chain lock contract"})

    cs = chip_state or {}
    operator_amm = evaluate_operator_amm(
        chip_control=cs.get("chip_control") if chip_state is not None else None,
        deep_neg_at_highs=cs.get("deep_neg_at_highs"), no_free_spot=cs.get("no_free_spot"),
        no_lock_contract=resolved_lock_info is None, squeeze_cadence=cs.get("squeeze_cadence"),
        quota_dead=bool(cs.get("quota_dead")))
    if cs.get("quota_dead"):
        degraded.append({"instrument": "OPERATOR_AMM (chip control)", "reason": "quota-dead"})

    # G1 contamination rule: a suspected (asserted) OPERATOR_AMM downgrades
    # account-ratio-divergence evidence for every OTHER type on this name.
    contaminated = operator_amm["verdict"] == "ASSERTED"
    ei = cs.get("elasticity_inputs") or {}
    elasticity_confirmed = ei.get("elasticity_confirmed")
    if elasticity_confirmed is None:
        ef = elasticity_fn or _elasticity_from_store
        try:
            store_result = ef(ticker, now_ts=now_ts)
        except Exception as e:  # noqa: BLE001
            store_result = {}
            degraded.append({"instrument": "oi_funding_elasticity", "reason": str(e)[:160]})
        elasticity_confirmed = store_result.get("elasticity_confirmed")
        if store_result.get("degraded_reason"):
            degraded.append({"instrument": "oi_funding_elasticity",
                             "reason": store_result["degraded_reason"]})
    funding_farm = evaluate_funding_farm(
        funding_pi_4h=fe.get("pi_4h") if fe else None,
        elasticity_confirmed=elasticity_confirmed,
        account_ratio_divergence=None if contaminated else ei.get("account_ratio_divergence"),
        basis_behaving=ei.get("basis_behaving"))
    if contaminated and ei.get("account_ratio_divergence"):
        funding_farm.setdefault("evidence", [])
        degraded.append({"instrument": "FUNDING_FARM (account-ratio)",
                         "reason": "downgraded — OPERATOR_AMM suspected on this name"})

    li = resolved_lock_info or {}
    vesting_hedge = evaluate_vesting_hedge(
        lock_cliff=li.get("cliff_date"), carry_violation=li.get("carry_violation"),
        perp_discount=li.get("perp_discount"), pristine_chain=li.get("pristine_chain"),
        hl_direct_observation=li.get("hl_direct_observation"))

    mm_inventory = evaluate_mm_inventory(top_book_neutral=top_book_neutral)
    cross_arb = evaluate_cross_venue_arb(dispersion=cross_venue_dispersion)

    oi_types = [funding_farm, vesting_hedge, operator_amm, mm_inventory, cross_arb]
    verdict = roll_up_verdict(oi_types)

    total_oi_usd = vm.get("total_oi_usd")
    overstated = any(t.get("overstated_by_cross_venue") for t in oi_types)
    gating_ok = compute_gating_ok(roles_ts, now_ts)

    return {
        "ticker": ticker.upper(), "as_of": _iso(now_ts),
        "battlefield": battlefield, "venue_roles": venue_roles, "oi_types": oi_types,
        "verdict": verdict,
        "aggregate": {"total_oi_usd": total_oi_usd, "overstated_by_cross_venue": overstated},
        "degraded": degraded, "gating_ok": gating_ok,
    }


_TYPE_ABBR = {"FUNDING_FARM": "FF", "VESTING_HEDGE": "VH", "OPERATOR_AMM": "OA",
             "MM_INVENTORY": "MM", "CROSS_VENUE_FUNDING_ARB": "CA"}
_SHARE_ABBR = {"minor": "min", "material": "mat", "dominant": "dom", "unknown": "unk"}
_SHARE_RANK = {"dominant": 3, "material": 2, "minor": 1, "unknown": 0}


def compact_line(envelope):
    """SPEC-180 req 5 — the ONE compact board field, e.g.
    'oic: MIXED(VH·mat·short) · perp_led · exit:bitget(flow)'. Printed ONLY when the
    verdict is not UNKNOWN or a degradation is noteworthy — never boards of UNKNOWN
    noise. None (no field at all — never a null placeholder) when there's nothing
    worth a line."""
    if not envelope:
        return None
    verdict = envelope.get("verdict")
    degraded = envelope.get("degraded") or []
    if verdict in (None, "UNKNOWN") and not degraded:
        return None
    bits = []
    asserted = [t for t in (envelope.get("oi_types") or []) if t.get("verdict") == "ASSERTED"]
    if asserted:
        top = max(asserted, key=lambda t: _SHARE_RANK.get(t.get("share_read"), 0))
        abbr = _TYPE_ABBR.get(top["type"], top["type"][:2])
        share = _SHARE_ABBR.get(top.get("share_read"), "unk")
        bits.append(f"{verdict}({abbr}·{share}·{top.get('side')})")
    else:
        bits.append(str(verdict) if verdict not in (None, "UNKNOWN") else "UNKNOWN(degraded)")
    bf = (envelope.get("battlefield") or {}).get("battlefield")
    if bf and bf != "UNKNOWN":
        bits.append(bf)
    exit_role = (envelope.get("venue_roles") or {}).get("exit") or {}
    if exit_role.get("venue"):
        grade_abbr = "flow" if exit_role.get("grade") == "FLOW_CONFIRMED" else "depth"
        bits.append(f"exit:{exit_role['venue']}({grade_abbr})")
    return "oic: " + " · ".join(bits)


def load_curated_venue_roles(ticker, path=None):
    """config/venue_roles.json is the CURATED file — hand-verified roles only (e.g. the
    SKYAI/Bitget exit-book fact), read-only for every code path (SPEC-191 #4). Returns
    the ticker's curated row (list of hand-verified snapshot dicts) or [] when absent —
    never raises on a missing/malformed file."""
    p = path or VENUE_ROLES_PATH
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    row = data.get((ticker or "").upper())
    return row if isinstance(row, list) else []


def load_latest_venue_roles_snapshot(ticker, state_dir=None):
    """Most recent LIVE snapshot for `ticker` from the untracked per-ticker state file
    (state/venue_roles/<TICKER>.json, SPEC-191 #4) — None when there is none yet."""
    d = state_dir or VENUE_ROLES_STATE_DIR
    p = d / f"{(ticker or '').upper()}.json"
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    hist = (data or {}).get((ticker or "").upper()) if isinstance(data, dict) else None
    return hist[-1] if hist else None


def resolved_venue_roles(ticker, curated_path=None, state_dir=None):
    """Curated (config/venue_roles.json) takes priority over the latest live snapshot
    (state/venue_roles/<TICKER>.json) — SPEC-191 #4's read-merge. A curated row with no
    live snapshot behind it still resolves (curated is the ceiling of confidence, not a
    tiebreaker); no curated row falls through to the live snapshot; neither present is
    an explicit `{}` (never a fabricated role)."""
    curated_rows = load_curated_venue_roles(ticker, path=curated_path)
    if curated_rows:
        return {"source": "curated", "venue_roles": curated_rows[-1]}
    snap = load_latest_venue_roles_snapshot(ticker, state_dir=state_dir)
    if snap is not None:
        return {"source": "snapshot", "venue_roles": snap}
    return {"source": None, "venue_roles": {}}


def save_venue_roles_snapshot(ticker, venue_roles, path=None):
    """Append-friendly history for drift detection (G2 — 'Snapshots: per-name roles +
    timestamps written to state/venue_roles/<TICKER>.json'). One row per call; never
    overwrites prior history for other tickers or prior ticks of the same ticker.

    SPEC-191 #4: the default target moved to the untracked per-ticker state file (like
    state/oi_samples/<SYM>.jsonl) — config/venue_roles.json is the CURATED file and is
    NEVER a live-write default. `path` stays overridable (tests, or a caller that wants
    the legacy shared-file-keyed-by-ticker shape)."""
    p = path or (VENUE_ROLES_STATE_DIR / f"{ticker.upper()}.json")
    try:
        data = json.loads(p.read_text()) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    hist = data.setdefault(ticker.upper(), [])
    hist.append(venue_roles)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1))
    return {"ticker": ticker.upper(), "snapshots": len(hist)}


def main():
    ap = argparse.ArgumentParser(description="SPEC-179 oi_construction — OI decomposition + venue roles")
    ap.add_argument("ticker")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save-roles", action="store_true",
                    help="append this run's venue_roles to state/venue_roles/<TICKER>.json "
                         "(config/venue_roles.json is curated/read-only, never a write target)")
    args = ap.parse_args()
    r = build_oi_construction(args.ticker)
    if args.save_roles:
        save_venue_roles_snapshot(args.ticker, r["venue_roles"])
    if args.json:
        print(json.dumps({"ok": True, "data": r, "meta": {}}))
    else:
        print(f"# {r['ticker']} oi_construction — verdict {r['verdict']} "
             f"(gating_ok={r['gating_ok']})")
        print(f"  battlefield: {r['battlefield'].get('battlefield')}")
        vr = r["venue_roles"]
        print(f"  mark_engine: {vr['mark_engine']['verdict']}  size_book: {vr['size_book']['verdict']}  "
             f"exit: {vr['exit']['grade']}({vr['exit'].get('venue')})  hedge: {vr['hedge']['verdict']}")
        for t in r["oi_types"]:
            print(f"  {t['type']}: {t['verdict']} ({t.get('share_read', '?')}) — {t.get('reason', '')}")
        if r["degraded"]:
            print(f"  degraded: {r['degraded']}")


if __name__ == "__main__":
    main()
