#!/usr/bin/env python3
"""setup_score.py — the §6 setup checklists as deterministic box-counters (SPEC 59).

The desk hand-counted "4 of 5 boxes" on every name this week (BEAT blowoff, FOLKS
Cat-B fade, PLAY trap-long, ID §4 gate). Counting the boxes is mechanical; what to
DO with the count is judgment (ARCHITECTURE §0.3 / invariant 2). This capability does
ONLY the counting — scores, never trade calls.

Each setup is a named checklist whose legs map to existing engine reads. The scorer
is pure over a flat normalized `signals` snapshot (one value per leg input); the live
`gather_signals(ticker)` assembles that snapshot from the real capabilities
(price_structure, regime_flip live_perp, oi_sides, tape, cvd, onchain, SPEC-57
full-history squeeze count). Tests inject `signals` directly — no network.

Output per setup:
  {setup, score, required, legs:{name:{pass,value,source}}, vetoes:[], missing:[],
   verdict: "ARMED|FORMING|VETOED|ABSENT"}

verdict:
  VETOED  — a hard veto fired (regardless of score)
  ARMED   — score >= required and no veto
  FORMING — 0 < score < required
  ABSENT  — no leg passed

For "ALL required" setups (blowoff, trap_long/neg_funding_gate) required == #legs, so
ARMED means every box ticked. NO trade recommendation is ever emitted.

  python3 capabilities/setup_score.py '{"ticker":"BEAT","setup":"blowoff"}' --json
  python3 capabilities/setup_score.py '{"setup":"catb_top","signals":{...}}' --json   # offline
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:                                   # §5 deep-neg short-veto line, single-sourced
    from regime_flip import DEEP_NEG as DEEP_NEG_4H
except Exception:                      # noqa: BLE001 — keep the scorer importable offline
    DEEP_NEG_4H = -0.30

_OI_DROP_LEGS = {"oi_off_or_funding_cooling", "oi_peaked_rolled", "oi_drop_ls_unfreeze"}


def oi_construction_annotation(signals, fired_leg_names):
    """SPEC-180 req 2 — ANNOTATION only, never a gate/score change. When an
    injected `signals['oi_construction']` envelope is present and one of the §6
    OI-drop/peaked-rolled legs FIRED, name two things the raw box-count can't see:
    (1) a FUNDING_FARM type reading `on_carry_decay` means the "drop" may be a farm
    unwind on funding normalization, not organic squeeze fuel leaving; (2)
    `overstated_by_cross_venue` means the aggregate OI feeding that read is
    double-counted by cross-venue arb — treat the peaked/rolled read as an upper
    bound, not raw. Returns a list of note strings (possibly empty), never mutates
    `signals`."""
    oic = signals.get("oi_construction") if isinstance(signals, dict) else None
    if not oic or not (fired_leg_names & _OI_DROP_LEGS):
        return []
    notes = []
    for t in oic.get("oi_types") or []:
        if t.get("type") == "FUNDING_FARM" and t.get("on_carry_decay"):
            notes.append("⚠ oi_construction: the OI drop may be a FUNDING_FARM unwind on "
                        "funding normalization, not organic squeeze fuel leaving (§6).")
            break
    agg = oic.get("aggregate") or {}
    if agg.get("overstated_by_cross_venue"):
        notes.append("⚠ oi_construction: aggregate OI is overstated_by_cross_venue (arb "
                    "double-counting) — read 'OI peaked/rolled' as an upper bound, not raw.")
    return notes

# ── SPEC-82 graded SCOUT tier ───────────────────────────────────────────────────
# A FORMING setup within SCOUT_DELTA legs of ARMED is surfaced as SCOUT (scout-actionable
# now; the missing legs are add_triggers, not entry gates). Confluence is the AVOID-filter,
# not the entry trigger (orchestrator/user 2026-06-19, replay-confirmed blind entry = −26R).
SCOUT_DELTA = 1               # SCOUT = score >= required - 1 (one leg short of ARMED)

# ── SPEC-83 defended_fade geometry + wall-dynamics ──────────────────────────────
FADE_STOP_BUFFER_PCT = 0.5    # stop sits this % BEYOND the wall, into the vacuum (defined risk)
WALL_HISTORY_MAX = 8          # rolling defended-wall snapshots kept per ticker/side
WALL_HOLD_BAND_PCT = 1.5      # price must stay within this % of the level = "the wall is tested"
WALL_HISTORY = HERE.parent / "state" / "wall_history.json"   # gitignored runtime state

# ── §6 deterministic thresholds ────────────────────────────────────────────────
PARABOLIC_PCT = 50.0          # +50% / <48h = parabolic (§6 blowoff)
ATH_WICK_PCT = 2.0            # ATH/window-high wicked >= 2%
BREAK_VOL_MULT = 1.5         # clean intraday break on >= 1.5x vol
LS_DROP_LO, LS_DROP_HI = 15.0, 25.0   # top L/S drops 15-25% (§6 catb_top)

# ── SPEC-74 volume-climax confirmation (RELATIVE primary / absolute MC-gated) ────
# Primary metric is RELATIVE: 4h quote-vol peak >= N x the coin's trailing baseline,
# evaluated ONLY while parabolic (the heuristic is "only matters when pumping hard").
# BSB hit ~10-25x; VELVET similar. Tunable, not hardcoded inline.
VOL_CLIMAX_MULT = 8.0          # peak 4h vol >= 8x trailing baseline = climax
VOL_ROLLOVER_FRAC = 0.5        # climax-then-collapse: latest bar <= 50% of the peak
# Cap-gated ABSOLUTE tier (a trader heuristic; the bands are ~5-10x too high for the
# desk's micro-caps so they only apply to coins large enough to plausibly reach them).
ABS_MC_GATE_USD = 150_000_000  # below this MC/OI the absolute bands NEVER apply (micro-cap)
ABS_BANDS_USD = {              # quote-volume topping bands (lo, hi) per interval
    "4h": (300_000_000, 700_000_000),
    "1h": (150_000_000, 300_000_000),
    "15m": (50_000_000, 100_000_000),
}


def _b(signals, key):
    return bool(signals.get(key))


def volume_climax(signals):
    """SPEC-74 — normalized volume-climax CONFIRMATION leg for the blowoff/Stage-5 top.

    RELATIVE tier (primary, micro-cap): 4h quote-vol peak >= VOL_CLIMAX_MULT x trailing
    baseline, evaluated ONLY while parabolic, AND climax-then-collapse (latest bar rolled
    over to <= VOL_ROLLOVER_FRAC of the peak) — the top tell is the spike *and* the drop,
    not the peak alone, so we don't fire mid-pump.

    ABSOLUTE tier (second, cap-gated): the raw $300-700M/4h topping bands only apply when
    MC/OI is large enough to plausibly reach them (ABS_MC_GATE_USD). The bands NEVER gate a
    micro-cap — those use the relative tier only.

    Confirmation only: this raises confidence on an already-forming blowoff; it never by
    itself arms or blocks a short (it is NOT a counted leg). Volumes in USD quote-volume.
    """
    s = signals or {}
    parabolic = _leg_parabolic(s)[0]
    baseline = s.get("vol_4h_baseline")
    peak = s.get("vol_4h_peak")
    cur = s.get("vol_4h_current")

    ratio = round(peak / baseline, 2) if (baseline and peak) else None
    rollover = bool(peak and cur is not None and cur <= peak * VOL_ROLLOVER_FRAC)
    rel_fires = bool(parabolic and ratio is not None and ratio >= VOL_CLIMAX_MULT and rollover)

    mc = s.get("mc_usd") or s.get("oi_usd")
    abs_eligible = mc is not None and mc >= ABS_MC_GATE_USD
    lo_4h = ABS_BANDS_USD["4h"][0]
    abs_fires = bool(abs_eligible and parabolic and peak is not None and peak >= lo_4h)

    fires = rel_fires or abs_fires
    tier = "relative" if rel_fires else ("absolute" if abs_fires else None)
    return {
        "fires": fires,
        "tier": tier,
        "parabolic": parabolic,
        "ratio": ratio,
        "rollover": rollover,
        "relative_fires": rel_fires,
        "absolute_eligible": abs_eligible,
        "absolute_fires": abs_fires,
        "values": {"vol_4h_baseline": baseline, "vol_4h_peak": peak, "vol_4h_current": cur,
                   "mc_usd": mc},
        "thresholds": {"relative_mult": VOL_CLIMAX_MULT, "rollover_frac": VOL_ROLLOVER_FRAC,
                       "abs_mc_gate_usd": ABS_MC_GATE_USD, "abs_band_4h_lo_usd": lo_4h},
        "source": "price_structure/live 4h quote-vol: relative peak/baseline + rollover; "
                  "absolute band MC-gated (SPEC-74, §6 confirmation only)",
    }


# ── leg evaluators: each returns (pass: bool, value, source: str) ───────────────
# value is whatever the Designer should see to audit the leg in one glance.

def _leg_parabolic(s):
    v = s.get("parabolic_pct")
    return (v is not None and v >= PARABOLIC_PCT, v,
            "price_structure/live: max gain over trailing <48h (%)")


def _leg_ath_wick(s):
    v = s.get("window_high_wick_pct")
    return (v is not None and v >= ATH_WICK_PCT, v,
            "price_structure.window_high: wick poke above window-high (%)")


def _leg_lower_high(s):
    return (_b(s, "lower_high"), s.get("lower_high"),
            "price_structure.structure: lower-high present")


def _leg_clean_break(s):
    ib = s.get("intraday_break") or {}
    ok = bool(ib.get("broke")) and (ib.get("vol_mult") or 0) >= BREAK_VOL_MULT \
        and not ib.get("rebought")
    return (ok, ib, "tape: intraday-level break on >=1.5x vol NOT re-bought in 1-2 candles")


def _leg_oi_off_or_funding_cooling(s):
    ok = _b(s, "oi_off_highs") or _b(s, "funding_cooling")
    return (ok, {"oi_off_highs": s.get("oi_off_highs"), "funding_cooling": s.get("funding_cooling")},
            "live: OI off highs OR funding cooling")


def _leg_ath_wick_bool(s):
    return (_b(s, "ath_wick"), s.get("ath_wick"), "price_structure: ATH wick")


def _leg_ls_drop(s):
    v = s.get("ls_top_drop_pct")
    return (v is not None and LS_DROP_LO <= v <= LS_DROP_HI, v,
            f"classify/L-S: top L/S drop in {LS_DROP_LO:g}-{LS_DROP_HI:g}% (%)")


def _leg_oi_peaked_rolled(s):
    return (_b(s, "oi_peaked_rolled"), s.get("oi_peaked_rolled"),
            "live: OI peaked-and-rolled")


def _leg_volume_declining(s):
    return (_b(s, "volume_declining"), s.get("volume_declining"),
            "price_structure: volume declining")


def _leg_funding_cooling(s):
    return (_b(s, "funding_cooling"), s.get("funding_cooling"),
            "live: funding cooling toward flat")


def _leg_multi_sigma_neg(s):
    return (_b(s, "multi_sigma_neg"), s.get("multi_sigma_neg"),
            "live.funding_4h: multi-sigma-neg (floor-sentinel, non-floor venue)")


def _leg_oi_spiking_real(s):
    ok = _b(s, "oi_spiking") and s.get("oi_sides_tag") != "WASH"
    return (ok, {"oi_spiking": s.get("oi_spiking"), "oi_sides_tag": s.get("oi_sides_tag")},
            "live OI rising AND oi_sides REAL (not WASH)")


def _leg_price_trigger(s):
    return (_b(s, "price_trigger"), s.get("price_trigger"),
            "tape: magnet sweep-and-reclaim (fake_breakdown_wick)")


def _leg_spot_cvd_up(s):
    return (_b(s, "spot_cvd_up"), s.get("spot_cvd_up"),
            "cvd: spot CVD diverging UP from perp (reliable only)")


def _leg_funding_phase_match(s):
    return (_b(s, "funding_phase_match"), s.get("funding_phase_match"),
            "classify: funding signature matches phase (§3)")


def _leg_oi_drop_ls_unfreeze(s):
    return (_b(s, "oi_drop_ls_unfreeze"), s.get("oi_drop_ls_unfreeze"),
            "live: sharp OI drop with L/S unfreezing")


def _leg_cex_deposits_firing(s):
    return (_b(s, "cex_deposits_firing"), s.get("cex_deposits_firing"),
            "onchain: CEX deposits firing")


def _leg_breakdown_not_bought_back(s):
    return (_b(s, "breakdown_not_bought_back"), s.get("breakdown_not_bought_back"),
            "tape/price: breakdown candle on volume not bought back")


# ── SPEC-83 defended_fade legs (the wall-fade LEVEL read) ───────────────────────
def _leg_wall_growing(s):
    return (_b(s, "wall_growing"), s.get("wall_growing"),
            "depth wall-history: defended wall NOTIONAL rising across snapshots WHILE price "
            "tests the level (operator capping while distributing) — not a static snapshot")


def _leg_empty_beyond(s):
    return (_b(s, "empty_beyond"), s.get("empty_beyond"),
            "depth/liq_magnets: vacuum past the wall (stop-above-empty) — uneconomical for the "
            "operator to squeeze into nothing [[feedback_safe_short_is_stop_above_empty_liquidity]]")


def _leg_stall_lower_high(s):
    return (_b(s, "stall_lower_high"), s.get("stall_lower_high"),
            "price_structure/tape: failing to break the ceiling, lower-highs forming")


def _leg_stall_higher_low(s):
    return (_b(s, "stall_higher_low"), s.get("stall_higher_low"),
            "price_structure/tape: holding the floor, higher-lows forming (long mirror)")


# ── vetoes: fires(signals) -> {"kind","reason"} | None ──────────────────────────
# kind "hard"  → suppresses entirely (verdict VETOED regardless of score).
# kind "swing" → blocks a SWING entry only; SCOUT/scalp still allowed (an ARMED score
#                is demoted to SCOUT, surfaced via `swing_vetoes`). [[feedback_squeezer_scalp_not_no_trade]]
def _veto_wash(s):
    if s.get("oi_sides_tag") == "WASH":
        return {"kind": "hard",
                "reason": "oi_sides WASH — fake directional OI, distrust the aggregate build (§4)"}
    return None


def _veto_squeeze_chronic(s):
    if _b(s, "squeeze_chronic"):
        d = s.get("squeeze_detail")
        return {"kind": "swing",
                "reason": ("squeeze-history chronic (>1 leg/10d over 60d) — scalp/scout-only, "
                           "never a swing short; the token's own pattern overrides the framework"
                           + (f" [{d}]" if d else ""))}
    return None


def _veto_deepneg_short(s):
    """§5 HARD short veto: funding ≤ the deep-neg line = you'd pay carry into crowded shorts
    (squeeze fuel). Applied to SHORT setups only; longs are never funding-vetoed (§5)."""
    f4 = s.get("funding_4h")
    if f4 is not None and f4 <= DEEP_NEG_4H:
        return {"kind": "hard",
                "reason": (f"§5 deep-neg funding {f4:+.3f}%/4h ≤ {DEEP_NEG_4H:+.2f}%/4h — "
                           "SHORT vetoed (carry + short-liqs stacked above = squeeze fuel)")}
    return None


def _veto_unsafe_fade(s):
    """SPEC-83 HARD safety veto for defended_fade: no vacuum past the wall means the fade stop
    would sit IN the squeeze fuel — the squeeze location isn't safe, so don't fade it at all
    (not even scout). [[feedback_safe_short_is_stop_above_empty_liquidity]]"""
    if not _b(s, "empty_beyond"):
        return {"kind": "hard",
                "reason": ("empty_beyond false — a liquidity cluster sits beyond the wall; the fade "
                           "stop would be IN the squeeze fuel (§7 stop-above-empty, unsafe)")}
    return None


def _veto_dex_mark(s):
    """§7 HARD veto: a composite-index / DEX-pool-weighted mark can liquidate you off a venue you
    aren't trading. Fires only when the read flags it explicitly (signal-gated)."""
    if _b(s, "dex_mark_heavy"):
        return {"kind": "hard",
                "reason": "DEX-pool mark-weight >30% — composite mark can liquidate off-venue (§7)"}
    return None


def _veto_liquidity_gate(s):
    """§7 HARD liquidity gate: sub-threshold exit liquidity = auto-PASS (the exit can't absorb)."""
    if _b(s, "illiquid"):
        return {"kind": "hard",
                "reason": "liquidity gate — sub-threshold 24h vol / MC; exit can't absorb (§7 auto-PASS)"}
    return None


# ── the checklists ──────────────────────────────────────────────────────────────
# required: None => ALL legs required (len). vetoes: hard-stop predicates.
SETUPS = {
    "blowoff": {
        "side": "short",
        "required": None,   # ALL
        "legs": [
            ("parabolic", _leg_parabolic),
            ("ath_wick", _leg_ath_wick),
            ("lower_high", _leg_lower_high),
            ("clean_break", _leg_clean_break),
            ("oi_off_or_funding_cooling", _leg_oi_off_or_funding_cooling),
        ],
        "vetoes": [_veto_wash, _veto_squeeze_chronic, _veto_deepneg_short],
    },
    "catb_top": {
        "side": "short",
        "required": 4,
        "legs": [
            ("ath_wick", _leg_ath_wick_bool),
            ("lower_high", _leg_lower_high),
            ("ls_top_drop", _leg_ls_drop),
            ("oi_peaked_rolled", _leg_oi_peaked_rolled),
            ("volume_declining", _leg_volume_declining),
            ("funding_cooling", _leg_funding_cooling),
        ],
        "vetoes": [_veto_deepneg_short],
    },
    "trap_long": {
        "side": "long",
        "required": None,   # ALL (§4 gate)
        "legs": [
            ("multi_sigma_neg", _leg_multi_sigma_neg),
            ("oi_spiking_real", _leg_oi_spiking_real),
            ("price_trigger", _leg_price_trigger),
            ("spot_cvd_up", _leg_spot_cvd_up),
        ],
        "vetoes": [_veto_wash],
    },
    "stage45_short": {
        "side": "short",
        "required": 4,
        "legs": [
            ("funding_phase_match", _leg_funding_phase_match),
            ("oi_drop_ls_unfreeze", _leg_oi_drop_ls_unfreeze),
            ("cex_deposits_firing", _leg_cex_deposits_firing),
            ("lower_high", _leg_lower_high),
            ("breakdown_not_bought_back", _leg_breakdown_not_bought_back),
        ],
        "vetoes": [_veto_squeeze_chronic, _veto_deepneg_short],
    },
    # SPEC-83 — the size-able squeezer entry: fade the defended extreme with stop above empty
    # liquidity. ARMS on the LEVEL read alone (no cascade); the cascade legs ride as add_legs
    # (the ADD, not the gate). entry=level / stop=beyond-wall (into the vacuum).
    "defended_fade_short": {
        "side": "short",
        "required": None,   # ALL 3 level legs
        "geometry": "short",
        "legs": [
            ("wall_growing", _leg_wall_growing),
            ("empty_beyond", _leg_empty_beyond),
            ("stall_lower_high", _leg_stall_lower_high),
        ],
        "add_legs": [       # cascade confirmations = size-up triggers, NOT required
            ("multi_sigma_neg", _leg_multi_sigma_neg),
            ("breakdown_not_bought_back", _leg_breakdown_not_bought_back),
            ("cex_deposits_firing", _leg_cex_deposits_firing),
        ],
        "vetoes": [_veto_wash, _veto_squeeze_chronic, _veto_deepneg_short,
                   _veto_unsafe_fade, _veto_dex_mark, _veto_liquidity_gate],
    },
    "defended_fade_long": {  # the floor mirror — long the defended floor, stop below empty
        "side": "long",
        "required": None,
        "geometry": "long",
        "legs": [
            ("wall_growing", _leg_wall_growing),
            ("empty_beyond", _leg_empty_beyond),
            ("stall_higher_low", _leg_stall_higher_low),
        ],
        "add_legs": [
            ("price_trigger", _leg_price_trigger),
            ("spot_cvd_up", _leg_spot_cvd_up),
            ("multi_sigma_neg", _leg_multi_sigma_neg),
        ],
        "vetoes": [_veto_wash, _veto_unsafe_fade, _veto_dex_mark, _veto_liquidity_gate],
    },
}
# §4 alias — same checklist under both names.
SETUPS["neg_funding_gate"] = SETUPS["trap_long"]

ALL_SETUPS = ["blowoff", "catb_top", "trap_long", "neg_funding_gate", "stage45_short",
              "defended_fade_short", "defended_fade_long"]

# The top/blowoff detectors that carry the SPEC-74 volume-climax confirmation field.
# (trap_long is a LONG accumulation gate — climax-collapse is not its tell.)
TOP_SETUPS = {"blowoff", "catb_top", "stage45_short"}

# SPEC-104: legs that fire ON a breakout/breakdown candle — these setups gain cycle_gate.
_CYCLE_GATE_LEGS = {"breakdown_not_bought_back", "price_trigger"}


def _fade_geometry(side, wall_price):
    """SPEC-83 — entry = the level (the defended wall/floor); stop = beyond it, INTO the vacuum
    (above the wall for a short, below the floor for a long). Defined-risk poke."""
    if wall_price is None:
        return {"entry": None, "stop": None, "side": side,
                "note": "wall_price unavailable — geometry pending live book read"}
    buf = FADE_STOP_BUFFER_PCT / 100.0
    stop = wall_price * (1 + buf) if side == "short" else wall_price * (1 - buf)
    return {"entry": round(wall_price, 10), "stop": round(stop, 10), "side": side,
            "stop_buffer_pct": FADE_STOP_BUFFER_PCT,
            "note": "entry=defended level; stop beyond the wall into the vacuum (§7)"}


def derive_wall_growing(snapshots, hold_band_pct=WALL_HOLD_BAND_PCT):
    """SPEC-83 — is the defended wall GROWING while price tests the level? True only when the
    wall notional rises strictly across snapshots AND price stays within hold_band_pct of the
    first level (the wall is being tested, not drifting away). Notional rising while price walks
    off the level is NOT a fade signal (the wall isn't capping a contested level). Pure."""
    snaps = [x for x in (snapshots or [])
             if x and x.get("notional") is not None and x.get("price")]
    if len(snaps) < 2:
        return False
    notls = [x["notional"] for x in snaps]
    pxs = [x["price"] for x in snaps]
    growing = all(b > a for a, b in zip(notls, notls[1:]))
    base = pxs[0]
    held = bool(base) and all(abs(p - base) / base * 100 <= hold_band_pct for p in pxs)
    return bool(growing and held)


def record_wall_snapshot(ticker, side, price, notional, ts,
                         max_keep=WALL_HISTORY_MAX, path=WALL_HISTORY):
    """Append a {price, notional, ts} snapshot to the rolling per-ticker/side wall history and
    return the trimmed list (newest last). Best-effort persistence under state/ (gitignored,
    like the nonce/accum baselines); on any I/O failure it still returns the in-memory list so
    a live read never blocks on disk. The book is a snapshot per call — this is what lets
    `derive_wall_growing` see the wall THICKENING across calls (SPEC-83's enabling data)."""
    path = Path(path)
    key = ticker.upper().replace("USDT", "")
    db = {}
    try:
        db = json.loads(path.read_text())
    except Exception:  # noqa: BLE001 — missing/corrupt history starts fresh
        db = {}
    if not isinstance(db, dict):
        db = {}
    series = db.setdefault(key, {}).setdefault(side, [])
    if not isinstance(series, list):
        series = []
    series.append({"price": price, "notional": notional, "ts": ts})
    series = series[-max_keep:]
    db[key][side] = series
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(db))
    except Exception:  # noqa: BLE001 — persistence is best-effort; never block a live read
        pass
    return series


def read_wall_history(ticker, side="ask", path=WALL_HISTORY):
    """Read-only: the rolling wall snapshots for a ticker/side (newest last); [] if none."""
    try:
        db = json.loads(Path(path).read_text())
        series = db.get(ticker.upper().replace("USDT", ""), {}).get(side, [])
        return series if isinstance(series, list) else []
    except Exception:  # noqa: BLE001
        return []


def grade_verdict(score, required, hard_vetoes, swing_vetoes):
    """SPEC-82 pure grader → (verdict, tier).

    VETOED  — a HARD veto fired (suppresses entirely, regardless of score).
    ARMED   — score >= required, no veto (the size-up / press tier).
    SCOUT   — within SCOUT_DELTA legs of ARMED (near-armed, scout-actionable now), OR an
              ARMED score demoted by a SWING veto (scalp/scout-allowed, not a swing).
    FORMING — 0 < score < required - SCOUT_DELTA (below threshold → stays quiet, no spam).
    ABSENT  — no leg passed.
    tier: "armed"|"scout"|None (None for FORMING/ABSENT/VETOED) — ledger reads it split.
    """
    if hard_vetoes:
        return "VETOED", None
    scout_floor = max(1, required - SCOUT_DELTA)
    if score >= required:
        base, tier = "ARMED", "armed"
    elif score >= scout_floor:
        base, tier = "SCOUT", "scout"
    elif score > 0:
        base, tier = "FORMING", None
    else:
        base, tier = "ABSENT", None
    if swing_vetoes and base == "ARMED":
        return "SCOUT", "scout"          # a swing-vetoed full-confluence short = scalp, not swing
    return base, tier


def score_setup(name, signals):
    """Score ONE setup over the normalized `signals` snapshot. Pure, deterministic."""
    spec = SETUPS.get(name)
    if spec is None:
        return {"setup": name, "error": f"unknown setup {name!r} (one of {sorted(SETUPS)})"}
    signals = signals or {}
    legs, missing = {}, []
    score = 0
    for leg_name, fn in spec["legs"]:
        passed, value, source = fn(signals)
        legs[leg_name] = {"pass": bool(passed), "value": value, "source": source}
        if passed:
            score += 1
        else:
            missing.append(leg_name)
    required = spec["required"] if spec["required"] is not None else len(spec["legs"])

    # add_legs (SPEC-83): cascade confirmations that are NOT counted/required — the absent
    # ones become size-up add_triggers; the present ones are confirmed_adds.
    add_legs = {}
    absent_adds = []
    for leg_name, fn in spec.get("add_legs", []):
        passed, value, source = fn(signals)
        add_legs[leg_name] = {"pass": bool(passed), "value": value, "source": source}
        if not passed:
            absent_adds.append(leg_name)

    hard_vetoes, swing_vetoes = [], []
    for v in spec["vetoes"]:
        res = v(signals)
        if res:
            (hard_vetoes if res["kind"] == "hard" else swing_vetoes).append(res["reason"])

    verdict, tier = grade_verdict(score, required, hard_vetoes, swing_vetoes)
    # add_triggers = missing required legs + absent cascade adds (what confirms / sizes it
    # up); empty once VETOED/ABSENT (nothing to size into).
    add_triggers = [] if verdict in ("VETOED", "ABSENT") else (missing + absent_adds)

    out = {"setup": name, "score": score, "required": required, "legs": legs,
           "vetoes": hard_vetoes, "swing_vetoes": swing_vetoes, "missing": missing,
           "add_triggers": add_triggers, "verdict": verdict, "tier": tier}
    if add_legs:
        out["add_legs"] = add_legs
    # SPEC-180 req 2: annotation-only oi_construction context on the §6 OI-drop
    # counters (never a gate/score change).
    fired_leg_names = {ln for ln, l in legs.items() if l["pass"]}
    oic_notes = oi_construction_annotation(signals, fired_leg_names)
    if oic_notes:
        out["oi_construction_notes"] = oic_notes
    if spec.get("geometry"):
        out["geometry"] = _fade_geometry(spec["geometry"], signals.get("wall_price"))
    if name in TOP_SETUPS:
        # SPEC-74: a CONFIRMATION field, not a counted leg — it raises confidence on an
        # already-forming top; it never changes score/required (cannot arm/block alone).
        out["volume_climax"] = volume_climax(signals)
    # SPEC-104: breakout/breakdown-triggered setups gain a cycle_gate field — candle-signal
    # + Wyckoff cycle agree -> pass-through; disagree -> an explicit CYCLE_CONFLICT the
    # orchestrator cannot miss. A WARNING, not a hard veto — never changes score/verdict.
    leg_names = {leg_name for leg_name, _fn in spec["legs"]}
    if (leg_names & _CYCLE_GATE_LEGS) and signals.get("cycle"):
        try:
            import phase as PH
            out["cycle_gate"] = PH.cycle_gate_for(spec["side"], signals["cycle"])
        except Exception:  # noqa: BLE001 — never let the gate break the score
            pass
    return out


def score_all(signals):
    """Score every setup. (trap_long and its neg_funding_gate alias both reported.)"""
    return {name: score_setup(name, signals) for name in ALL_SETUPS}


# ── live assembly (best-effort; tests never hit this path) ──────────────────────
def gather_signals(ticker):
    """Assemble the normalized signals snapshot from the real capabilities. Network —
    each read degrades to absent on failure (the leg then reads not-pass). Returns
    (signals, sources_meta)."""
    tk = ticker.upper().replace("USDT", "")
    s = {}
    meta = {}

    def _try(label, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — one dead read must not abort the snapshot
            meta[label] = {"available": False, "reason": str(e)[:120]}
            return None

    ps = _try("price_structure", lambda: __import__("price_structure").build_structure(tk))
    if ps and not ps.get("error"):
        meta["price_structure"] = {"available": True}
        st = ps.get("structure") or {}
        s["lower_high"] = (st.get("read") == "downtrend") or (st.get("lower_highs", 0) >= 4)
        s["window_high_wick_pct"] = round(max(0.0, ps.get("off_ath_pct") or 0.0), 4) \
            if (ps.get("off_ath_pct") or 0) >= 0 else 0.0
        s["ath_wick"] = (ps.get("off_ath_pct") or -1) >= 0
        vp = ps.get("vol_profile") or {}
        s["volume_declining"] = (vp.get("read") == "cascade")
        s["squeeze_chronic"] = len(ps.get("squeezes") or []) > 6  # corroborator; SPEC-57 count below

    live = _try("live", lambda: __import__("regime_flip").live_perp(tk))
    if live:
        meta["live"] = {"available": True}
        f4 = live.get("funding_4h")
        s["multi_sigma_neg"] = (f4 is not None and f4 <= -0.30 and not live.get("all_floor"))
        s["parabolic_pct"] = live.get("chg24")

    oi = _try("oi_sides", lambda: _run_oi_sides(tk))
    if oi:
        meta["oi_sides"] = {"available": True}
        s["oi_sides_tag"] = oi.get("verdict_tag") or oi.get("tag")

    cv = _try("cvd", lambda: __import__("cvd").build_cvd(tk))
    if cv:
        meta["cvd"] = {"available": True}
        s["spot_cvd_up"] = cv.get("verdict") == "BULLISH_DIVERGENCE" and bool(cv.get("reliable"))

    tp = _try("tape", lambda: __import__("tape").build_tape(tk))
    if tp and tp.get("available"):
        meta["tape"] = {"available": True}
        s["price_trigger"] = any(p.get("type") == "fake_breakdown_wick"
                                 for p in tp.get("patterns") or [])

    # SPEC-104: the Wyckoff cycle read — feeds cycle_gate on breakout/breakdown-triggered
    # setups. Thin/short history (phase's own degrade) just means no cycle_gate is attached.
    ph = _try("phase", lambda: __import__("phase").build_phase(tk))
    if ph and ph.get("available"):
        meta["phase"] = {"available": True}
        s["cycle"] = ph.get("cycle")

    vc = _try("vol_4h", lambda: _fetch_vol_4h(tk))
    if vc:
        meta["vol_4h"] = {"available": True}
        s.update(vc)   # vol_4h_baseline / vol_4h_peak / vol_4h_current

    wall = _try("defended_fade", lambda: _gather_wall(tk, s))
    if wall:
        meta["defended_fade"] = {"available": True}
        s.update(wall)
    return s, meta


def _gather_wall(ticker, s):
    """SPEC-83 — assemble the defended_fade LEVEL read live: record the top-of-book wall, derive
    `wall_growing` from the rolling history, the `empty_beyond` vacuum (liq_magnets proxy), the
    stall/lower-high structure, and the wall_price for geometry. Best-effort; network. The short
    reads the ask wall above; the long reads the bid shelf below. Both sides recorded each call."""
    import time
    depth = __import__("depth").build_depth(ticker)
    venues = depth.get("venues") or {}
    v = next((x for name in ("bitget", "binance")
              for x in [venues.get(name)] if x and x.get("available")), None)
    if not v:
        return {}
    out = {}
    ts = time.time()
    aw = v.get("ask_wall_above") or {}
    bs = v.get("bid_shelf_below") or {}
    mid = v.get("mid")
    mags = None
    try:
        mags = __import__("liq_magnets").build_magnets(ticker.upper().replace("USDT", "") + "USDT")
    except Exception:  # noqa: BLE001
        mags = None
    truncated = bool(v.get("truncated"))

    # SHORT side: defended ask wall above + vacuum above it
    if aw.get("price") and aw.get("notional_usd") is not None and mid:
        hist = record_wall_snapshot(ticker, "ask", mid, aw["notional_usd"], ts)
        out["wall_growing"] = derive_wall_growing(hist)
        out["wall_price"] = aw["price"]
        # empty_beyond: no upside magnet cluster sitting above the wall (defer if book truncated)
        ups = (mags or {}).get("upside_magnets") or []
        cluster_above = any((m.get("price") or 0) > aw["price"] * 1.001 for m in ups)
        out["empty_beyond"] = (not cluster_above) and not truncated
        out["spoof_prone"] = bool(aw.get("spoof_prone"))
    out["stall_lower_high"] = bool(s.get("lower_high"))
    out["stall_higher_low"] = bool(s.get("ath_wick")) and not s.get("lower_high")  # rough up-bias proxy
    out["_wall_bid"] = {"price": bs.get("price"), "notional": bs.get("notional_usd")}  # diag
    return out


def _fetch_vol_4h(ticker, peak_lookback=12):
    """SPEC-74 — Binance-futures 4h quote-volume: trailing-7d-median baseline, the recent
    peak, and the latest closed bar. Best-effort; network. Returns {} on any failure."""
    import statistics
    import urllib.request

    sym = ticker.upper().replace("USDT", "")
    url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}USDT"
           f"&interval=4h&limit=60")
    with urllib.request.urlopen(url, timeout=15) as r:
        kl = json.loads(r.read())
    if not isinstance(kl, list) or len(kl) < 10:
        return {}
    qv = [float(k[7]) for k in kl]          # quote-volume per 4h bar, oldest->newest
    trailing = qv[:-1]                       # exclude the most recent (the in-progress top)
    baseline = statistics.median(trailing[-42:]) if trailing else None   # ~7d of 4h bars
    recent = qv[-peak_lookback:]
    peak = max(recent) if recent else None
    cur = qv[-1]
    out = {"vol_4h_current": cur}
    if baseline:
        out["vol_4h_baseline"] = baseline
    if peak is not None:
        out["vol_4h_peak"] = peak
    return out


def _run_oi_sides(ticker):
    """Shell out to the parts-bin oi_sides (registered under _oldrepo) for the wash tag."""
    import subprocess
    p = subprocess.run([sys.executable, str(HERE.parent / "_oldrepo" / "scripts" / "oi_sides.py"),
                        ticker, "--json"], capture_output=True, text=True, timeout=30)
    return json.loads(p.stdout) if p.stdout.strip() else None


# ── CLI ─────────────────────────────────────────────────────────────────────────
def _emit(setup, signals, only):
    if only:
        return {only: score_setup(only, signals)}
    return score_all(signals)


def main():
    ap = argparse.ArgumentParser(description="§6 setup checklists as box-counters (SPEC 59)")
    ap.add_argument("payload", help='JSON: {"ticker":X,"setup":?} OR {"setup":?,"signals":{...}}')
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        req = json.loads(args.payload)
    except ValueError as e:
        print(json.dumps({"error": f"unparseable payload: {e}"})); sys.exit(2)
    setup = req.get("setup")
    if setup and setup not in SETUPS:
        print(json.dumps({"error": f"unknown setup {setup!r} (one of {sorted(SETUPS)})"})); sys.exit(2)
    if "signals" in req:                       # offline / fixture path — no network
        signals = req["signals"]
        meta = {"source": "signals (injected)"}
    else:
        ticker = req.get("ticker")
        if not ticker:
            print(json.dumps({"error": "need ticker (live) or signals (offline)"})); sys.exit(2)
        signals, meta = gather_signals(ticker)
    out = _emit(setup, signals, setup)
    out["_meta"] = meta
    print(json.dumps(out, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
