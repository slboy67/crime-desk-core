#!/usr/bin/env python3
"""analyse.py — combined perp + on-chain verdict engine (NATIVE).

Native port of the parts-bin analyse.py convergence engine. Pulls the data layers
(perp / on-chain / spot-perp CVD / intraday structure / per-side OI / HL whales) as
subprocesses with clean --json, runs them CONCURRENTLY, and converges to one verdict.

Two behaviours this native version fixes vs the delegated original:

  SPEC 1b — HARD on-chain budget (ONCHAIN_BUDGET=90s). If the on-chain layer doesn't
            return in time it is dropped and the run completes perp-only with an
            explicit `onchain:"UNAVAILABLE"`. The on-chain layer NEVER hangs the run
            or bubbles up as a failure — the perp verdict always returns.

  SPEC 4  — NEG-FUNDING LONG CONFLUENCE GATE (§4). A LONG on negative funding is only
            emitted when the full §4 confluence is present: multi-sigma-neg funding
            AND OI rising AND a price trigger (not a fresh-ATH chase) AND spot-CVD
            diverging UP. Otherwise — incl. deep-neg + fresh-ATH + clean/locked
            on-chain (the OTC vesting-hedge trap) or missing CVD — it returns WATCH
            (spectate), treating on-chain-clean as NEUTRAL, never LONG.

  SPEC 42 — the spot-CVD leg is the NATIVE capabilities/cvd.py (real spot-venue
            resolution: Binance ↔ Bitget by 24h volume, then GeckoTerminal DEX), not
            the Binance-centric _oldrepo script. notes[] name the CVD venue; no spot
            anywhere → cvd_verdict "UNAVAILABLE" + spot_coverage "none" and the §4 leg
            reads UNKNOWN — never a silent fail or a false-negative hedge-trap veto.

  python3 capabilities/analyse.py LAB
  python3 capabilities/analyse.py LAB --json

Output preserves the {ticker, verdict, tier, direction} contract in BOTH paths, plus
reason / onchain status / onchain_ms / confluence legs.
"""
import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OLD = ROOT / "_oldrepo" / "scripts"
sys.path.insert(0, str(HERE))
import colors as C
from onchain import build_nonce_state   # the fast live on-chain read (nonce spine)
import regime_check as RC               # SPEC 25: reuse the SPEC-18 hardened cross-venue funding path
import cvd as CVDM                       # SPEC 42: native spot-CVD with real spot-venue resolution
import oi_mc as OM                       # SPEC-177: battlefield verdict + leverage_state
import oi_construction as OC             # SPEC-180: OI-type decomposition (§4 gate consumer)

ONCHAIN_BUDGET = 30          # SPEC 1b + Phase-2: on-chain read = cheap nonce snapshot, not getLogs
OIC_BUDGET = 20               # SPEC-180: oi_construction sweep (venue_map+cvd), bounded like on-chain


# ---- SPEC 25: hardened cross-venue price/funding backfill ----
# perp_analyser sources price + funding from BYBIT ONLY → both null on thin names (SKYAI) while
# OI (Binance) populates. A null price/funding silently broke the verdict (false "funding NEUTRAL
# → WAIT"). Backfill from the same live-predicted cross-venue path regime_check uses; degrade
# EXPLICITLY (price_unavailable/funding_unavailable) when ALL venues fail — never a silent null.

def _funding_interval_min(venue, s):
    """Funding interval in minutes (for %/4h normalization). Bybit: instruments-info; Binance/Aster:
    delta between two settled fundingTimes. Default 240 (4h, the cluster norm) if unreadable."""
    try:
        if venue == "bybit":
            d = RC.fetch(f"https://api.bybit.com/v5/market/instruments-info?category=linear&symbol={s}")
            return int(d["result"]["list"][0]["fundingInterval"])
        host = "fapi.binance.com" if venue == "binance" else "fapi.asterdex.com"
        d = RC.fetch(f"https://{host}/fapi/v1/fundingRate?symbol={s}&limit=4")
        if isinstance(d, list) and len(d) >= 2:
            return round((d[-1]["fundingTime"] - d[-2]["fundingTime"]) / 60000)
    except (KeyError, ValueError, TypeError, IndexError):
        return None
    return None


def _hardened_funding_4h(sym):
    """Cross-venue LIVE-predicted funding normalized to %/4h; MORE-VETOING (most-negative)
    NON-floor venue wins (SPEC 13/18 + SPEC-71). A ±0.005%/interval floor-sentinel print is a
    placeholder, not a datum (§3) — it is DEMOTED whenever any venue has a real print (a +0.005
    floor must never beat a real +0.02 by being "less positive"). Reuses
    regime_check._live_funding_pct. Returns (fr_4h, venue, floor_state) where floor_state is
    None (real print won) | 'all_floor' (every covered venue floored, ≥2 = corroborated ~0%,
    SPEC 19/44) | 'suspect' (lone floored venue, no corroboration) — or (None, None, None)."""
    s = f"{sym}USDT"
    cands = []
    for v in ("binance", "bybit", "aster"):
        fr = RC._live_funding_pct(v, s)          # %/interval, live predicted (None on fail)
        if fr is None:
            continue
        iv = _funding_interval_min(v, s) or 240
        cands.append((v, round(fr * 240 / iv, 4), RC._is_floor_pct(fr)))   # floor on the RAW print
    if not cands:
        return None, None, None
    real = [c for c in cands if not c[2]]
    if real:
        venue, fr4, _ = min(real, key=lambda x: x[1])   # most-vetoing REAL venue
        return fr4, venue, None
    venue, fr4, _ = min(cands, key=lambda x: x[1])      # every covered venue at the floor
    return fr4, venue, ("all_floor" if len(cands) >= 2 else "suspect")


def _cross_venue_price(sym):
    """Live last price with cross-venue fallback: binance → bybit → aster. Returns (price, venue) |
    (None, None). (SPEC 26 inserts bitget into this chain.)"""
    s = f"{sym}USDT"
    d = RC.fetch(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={s}")
    if isinstance(d, dict) and d.get("price"):
        return float(d["price"]), "binance"
    d = RC.fetch(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={s}")
    try:
        lst = d["result"]["list"]
        if lst and lst[0].get("lastPrice"):
            return float(lst[0]["lastPrice"]), "bybit"
    except (KeyError, TypeError, IndexError):
        pass
    d = RC.fetch(f"https://api.bitget.com/api/v2/mix/market/ticker?symbol={s}&productType=usdt-futures")   # SPEC 26
    try:
        data = d["data"]
        if data and data[0].get("lastPr"):
            return float(data[0]["lastPr"]), "bitget"
    except (KeyError, TypeError, IndexError):
        pass
    d = RC.fetch(f"https://fapi.asterdex.com/fapi/v1/ticker/price?symbol={s}")
    if isinstance(d, dict) and d.get("price"):
        return float(d["price"]), "aster"
    return None, None


def _backfill_perp(perp, ticker):
    """SPEC 25: patch perp['metrics'] with hardened price/funding when perp_analyser (Bybit-only)
    nulls them; set explicit *_unavailable flags when ALL venues fail. Returns the patched perp.

    SPEC-71: perp_analyser's Bybit print is also floor-checked — a floor-sentinel rate is demoted
    to the cross-venue real rate when one exists (the BILL incident: a floor print headlined while
    Binance held the real, trigger-qualifying rate), flagged floor_suspect/all_floor when none
    does. funding_venue is ALWAYS attributed — never null."""
    m = dict(perp.get("metrics", {}) or {})
    if m.get("fr_4h") is None:
        fr4, fv, floor_state = _hardened_funding_4h(ticker)
        if fr4 is not None:
            m["fr_4h"], m["funding_venue"], m["funding_backfilled"] = fr4, fv, True
            if floor_state == "suspect":
                m["floor_suspect"] = True
            elif floor_state == "all_floor":
                m["all_floor"] = True
        else:
            m["funding_unavailable"] = True
    elif RC._is_floor_pct(m.get("funding", m.get("fr_4h"))):
        # the floor sentinel lives on the RAW per-interval print (normalization shifts it)
        fr4, fv, floor_state = _hardened_funding_4h(ticker)
        if fr4 is not None and floor_state is None:
            m["fr_4h"], m["funding_venue"], m["funding_floor_demoted"] = fr4, fv, True
        elif floor_state == "all_floor":
            m["fr_4h"], m["funding_venue"], m["all_floor"] = fr4, fv, True
        else:
            # lone floor (or the cross-venue re-read failed): keep it, but it is a data-failure
            # signal, not a confident flat (§3)
            m.setdefault("funding_venue", "bybit")
            m["floor_suspect"] = True
            if fr4 is not None:
                m["fr_4h"], m["funding_venue"] = fr4, fv
    else:
        m.setdefault("funding_venue", "bybit")   # perp_analyser's live source (SPEC-71: never null)
    if m.get("price") is None:
        px, pv = _cross_venue_price(ticker)
        if px is not None:
            m["price"], m["price_venue"], m["price_backfilled"] = px, pv, True
        else:
            m["price_unavailable"] = True
    perp["metrics"] = m
    return perp
NEG_LONG_TRIGGER = -0.10     # funding %/4h at/below which the §4 neg-funding LONG gate engages
DEEP_NEG = -0.30             # multi-sigma / deep-neg threshold (also the short-side veto cutoff)


def run_json(script, ticker, extra=None, timeout=120):
    cmd = ["python3", str(OLD / script), ticker, "--json"] + (extra or [])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
        return json.loads(out.stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        return {"_timeout": True}
    except Exception as e:  # noqa: BLE001
        return {"_err": str(e)[:160]}


def run_cvd(ticker, minutes=30):
    """SPEC 42 — native spot-CVD layer (replaces the _oldrepo Binance-centric
    cvd_spot_perp leg). Resolves the token's REAL primary spot venue (Binance →
    Bitget → DEX) and degrades EXPLICIT: coverage 'none' → verdict 'UNAVAILABLE'."""
    try:
        return CVDM.build_cvd(ticker, minutes)
    except Exception as e:  # noqa: BLE001
        return {"verdict": None, "_err": str(e)[:160]}


def run_whales(ticker, min_notl=50000):
    cmd = ["python3", str(OLD / "hyperdash_whale_tracker.py"), "scan", ticker,
           "--min", str(min_notl), "--json"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90, cwd=str(ROOT))
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001
        return {"_err": str(e)[:120], "verdict": "NO_DATA"}


def run_oi_construction(ticker):
    """SPEC-180 — the OI-construction envelope for the §4 gate. `build_oi_construction`
    already degrades every leg internally (§3); this wrapper only guards against a
    total crash (matching run_whales/run_cvd's own outer try/except), returning None
    so the gate's `_oic_short_side_read` reads it as UNKNOWN/leg-unchanged."""
    try:
        return OC.build_oi_construction(ticker)
    except Exception:  # noqa: BLE001
        return None


def _top_holder_distribution_check(ticker):
    """SPEC-89 — verify_wallet the top holders so the §4 long gate's 'on-chain not distributing'
    leg is satisfied ONLY by a clean top-holder check, never the nonce-QUIET signal alone (the
    BLESS/H false-quiet long). Lazily imported (onchain pulls verify_wallet → a module-load
    import is circular). Moralis-gated inside `_top_holder_distribution`; any failure degrades to
    checked:False so the gate behaves exactly as before (never a false TRAP). Returns
    {checked, distributing}."""
    try:
        from onchain import _concentration, _top_holder_distribution
        thd = _top_holder_distribution(ticker, _concentration(ticker))
        return {"checked": bool(thd.get("checked")), "distributing": bool(thd.get("distributing"))}
    except Exception:  # noqa: BLE001 — a failed check never blocks or false-traps the verdict
        return {"checked": False, "distributing": False}


def _coarse(direction):
    # Order matters: WATCH/PASS qualifiers (e.g. "WATCH (spectate / pre-unlock short)")
    # must win over the LONG/SHORT substring they may contain.
    d = (direction or "").upper()
    if "WATCH" in d or "CAUTION" in d or "SPECTATE" in d:
        return "WATCH"
    if "PASS" in d:
        return "PASS"
    if "LONG" in d:
        return "LONG"
    if "SHORT" in d:
        return "SHORT"
    return "PASS"


def _oic_short_side_read(oi_construction):
    """SPEC-180 req 1 — the OI-construction decomposition on the SHORT side (the
    shorts whose 'OI rising' would confirm squeeze-fuel loading). Returns
    (verdict, note|None): verdict ∈ ARB_DOMINATED|MIXED_MATERIAL|UNKNOWN — a stale
    (`gating_ok:false`) or genuinely-UNKNOWN read is UNKNOWN, never a fabricated leg
    change (governing principle, SPEC-180 G4: a gap must never block a trade today's
    rules would take)."""
    if not oi_construction or not oi_construction.get("gating_ok") \
            or oi_construction.get("verdict") in (None, "UNKNOWN"):
        return "UNKNOWN", "§4 OI-construction: decomposition unknown/stale — leg unchanged."
    oi_types = oi_construction.get("oi_types") or []
    short_types = [t for t in oi_types if t.get("side") in ("short", "both")]
    if not short_types:
        return "UNKNOWN", "§4 OI-construction: no short-side type ran — leg unchanged."
    short_verdict = OC.roll_up_verdict(short_types)
    if short_verdict == "ARB_DOMINATED":
        return "ARB_DOMINATED", ("⛔ §4 OI-construction: short-side OI reads ARB_DOMINATED "
                                 "(spike = hedging, not loading) — oi_rising leg VOIDED.")
    if short_verdict == "MIXED" and any(
            t.get("share_read") in ("material", "dominant")
            for t in short_types if t.get("type") in OC.NON_DIRECTIONAL_TYPES):
        return "MIXED_MATERIAL", ("⚠ §4 OI-construction: short-side MIXED with a material+ arb "
                                  "signal — leg DOWNGRADED, size down even if §4 otherwise "
                                  "completes.")
    return "UNKNOWN", None


def _neg_funding_long_gate(direction, tier, notes, fr_4h, oi_chg, near_ath, cvd_v,
                           onch_locked, onch_unavailable, top_holder_distributing=False,
                           oi_construction=None):
    """SPEC 4 — gate a neg-funding LONG behind the full §4 confluence.

    Returns (direction, tier, gate_reason|None). Only acts when direction is LONG and
    funding is negative (≤ NEG_LONG_TRIGGER). On any missing leg → WATCH (spectate),
    naming the failed leg(s). on-chain-clean at a fresh-ATH deep-neg is treated as
    NEUTRAL, not bullish (the OTC vesting-hedge trap).

    SPEC-180 req 1: `oi_construction` (the SPEC-179 envelope, optional) VOIDS the
    "OI SPIKING = loading" (`oi_rising`) leg when the short-side decomposition reads
    ARB_DOMINATED (a spike there is hedging, not loading); DOWNGRADES tier (never
    blocks) when MIXED with a material+ arb signal on the short side; UNKNOWN/stale
    leaves the leg exactly as before, annotated."""
    if "LONG" not in direction or fr_4h is None or fr_4h > NEG_LONG_TRIGGER:
        return direction, tier, None

    # SPEC-89: a top holder / cluster escrow actively DISTRIBUTING (verify_wallet) under deep-neg
    # funding is the reason-#4 hedge/distribution TRAP — you are the exit liquidity, NOT trapped-
    # short squeeze fuel. The §4 "on-chain not distributing" leg is satisfied ONLY by a clean
    # top-holder check, never the nonce-QUIET signal alone (the BLESS/H false-quiet long).
    if top_holder_distributing:
        notes.append("⛔ §4 reason-#4 TRAP: a top holder / cluster escrow is DISTRIBUTING "
                     "(verify_wallet) under deep-neg funding — you are the EXIT LIQUIDITY, not "
                     "trapped-short squeeze fuel (§4/§8). PASS, do NOT long the dump.")
        return ("PASS (top-holder distributing)", "PASS",
                "top-holder distribution under deep-neg funding (reason #4 hedge/distribution trap)")

    legs = {
        "multi_sigma_neg_funding": fr_4h <= DEEP_NEG,
        "oi_rising": (oi_chg is not None and oi_chg > 10),
        "price_trigger": not bool(near_ath),          # fresh-ATH chase ≠ sweep&bounce trigger
        "spot_cvd_up": cvd_v == "BULLISH_DIVERGENCE",
    }
    oic_verdict, oic_note = _oic_short_side_read(oi_construction)
    if oic_verdict == "ARB_DOMINATED":
        legs["oi_rising"] = False
    if oic_note:
        notes.append(oic_note)

    failed = [k for k, ok in legs.items() if not ok]
    if not failed:
        notes.append("✅ §4 confluence COMPLETE: multi-sigma-neg funding + OI rising + price trigger "
                     "+ spot-CVD diverging up. Genuine trapped-short squeeze long.")
        if oic_verdict == "MIXED_MATERIAL":
            return direction, ("MILD" if tier == "STRONG" else tier), None
        return direction, tier, None

    human = {"multi_sigma_neg_funding": "funding not multi-sigma-extreme",
             "oi_rising": "OI not rising (flat/falling = hedge, not loading)",
             "price_trigger": "at/near fresh ATH (chase, no swept-magnet bounce)",
             "spot_cvd_up": ("spot-CVD UNAVAILABLE — no spot venue anywhere, leg is UNKNOWN, "
                             "not a bearish read (SPEC 42)" if cvd_v == "UNAVAILABLE"
                             else "no spot-CVD up-divergence" + ("" if cvd_v else " (CVD unavailable)"))}
    reason = "neg-funding LONG blocked (§4 confluence incomplete): " + "; ".join(human[k] for k in failed)

    otc = near_ath and (onch_locked or onch_unavailable)
    if otc:
        notes.append(
            "⛔ §4 OTC vesting-hedge trap: deep-neg funding persisting at a fresh ATH with on-chain "
            "clean/locked/unverified = desks shorting locked supply delta-neutral, NOT trapped shorts. "
            "On-chain-clean here is NEUTRAL, not bullish — they won't cover on a squeeze. "
            "Spectate / pre-unlock SHORT positioning, NOT long.")
        new_dir = "WATCH (spectate / pre-unlock short)"
    else:
        notes.append("⛔ " + reason + ". A neg-funding long is a coin-flip without the full gate (§4); "
                     "the alt default is MM-hedge/OTC distribution (the trap). WATCH until all four legs confirm.")
        new_dir = "WATCH (neg-funding §4 incomplete)"
    return new_dir, "WATCH", reason


def converge(perp, onch, cvd=None, struct=None, whales=None, oi=None, onch_unavailable=False,
             top_holder_distributing=False, battlefield=None, oi_construction=None):
    """Port of the parts-bin converge() + the SPEC-4 neg-funding LONG gate. Returns
    (direction, tier, notes, p_score, o_score, gate_reason). `battlefield` (SPEC-177,
    optional) is a pre-computed `oi_mc.build_battlefield(...)` envelope — converge()
    does no I/O of its own for it, just names the verdict/annotations in the notes.
    `oi_construction` (SPEC-180, optional) is a pre-computed
    `oi_construction.build_oi_construction(...)` envelope, consumed only by the §4
    gate's oi_rising leg (see `_oic_short_side_read`) — same no-I/O-here discipline."""
    p_score = perp.get("score", 0)
    m = perp.get("metrics", {}) or {}
    fr_4h = m.get("fr_4h")
    oi_chg = m.get("oi_chg")
    turnover = m.get("turnover")
    o = onch.get("result") or {}
    o_score = o.get("score", 0) if o else 0
    cvd_v = (cvd or {}).get("verdict")
    w_v = (whales or {}).get("verdict")
    near_ath = bool(struct and struct.get("near_ath"))
    notes = []
    gate_reason = None

    # GATE 0 — liquidity (run FIRST, §6)
    byb_turn = m.get("turnover_bybit") or 0
    bin_turn = m.get("turnover_binance") or 0
    single_venue_pct = m.get("single_venue_pct") or 0
    if turnover is not None and turnover < 10_000_000:
        breakdown = f"(Binance ${bin_turn/1e6:.1f}M + Bybit ${byb_turn/1e6:.1f}M + Aster ${(m.get('turnover_aster') or 0)/1e6:.2f}M)"
        return ("PASS (liquidity)", "PASS",
                [f"Liquidity gate FAIL: 24h aggregate turnover ${turnover:,.0f} < $10M {breakdown} — untradeable for real size. Auto-PASS."],
                p_score, o_score, "liquidity gate fail")
    scout_liq = turnover is not None and turnover < 25_000_000
    if single_venue_pct >= 0.85 and turnover and turnover >= 10_000_000:
        notes.append(f"⚠ Single-venue concentration {single_venue_pct*100:.0f}% — exit slippage risk (§6).")

    # GATE — blowoff-top short (microstructure alone; deep-neg veto applies)
    if struct and struct.get("blowoff_short"):
        if fr_4h is not None and fr_4h <= DEEP_NEG:
            return ("WATCH (blowoff vs deep-neg funding)", "WATCH",
                    [f"⛔ BLOWOFF-SHORT VETOED by deep-neg funding {fr_4h:+.2f}%/4h (~{fr_4h*6:+.1f}%/day carry, short-liqs stack above = squeeze fuel). "
                     "On deep-neg Cat A a breakdown is a bilateral bait-dip into a re-squeeze (OTC-hedgers don't cover), not a clean cascade (§2 veto + §6 reload). WATCH until funding cools toward flat."]
                    + (["⚠ thin liquidity — scout size."] if scout_liq else []),
                    p_score, o_score, "blowoff vetoed by deep-neg funding")
        if fr_4h is not None and fr_4h <= -0.10:
            return ("SHORT (blowoff, scout)", "MILD",
                    [f"⚠ BLOWOFF SHORT — SCOUT ONLY: structure fired but funding {fr_4h:+.2f}%/4h still mildly NEG (squeeze fuel above). Clean blowoff wants flat/positive funding (§2). Scout; bank fast."]
                    + (["⚠ thin liquidity — scout size."] if scout_liq else []),
                    p_score, o_score, "blowoff scout (mild-neg funding)")
        return ("SHORT (blowoff-top)", "STRONG",
                [f"BLOWOFF-TOP SHORT: fresh-ATH wick {struct.get('ath_wick_pct')}% + lower-high + clean breakdown on {struct.get('breakdown_vol_mult')}× volume. §6 setup — cascade outruns on-chain. Stop above the ATH wick."]
                + (["⚠ thin liquidity — scout size."] if scout_liq else []),
                p_score, o_score, None)

    # Convergence matrix
    perp_long = p_score >= 15
    perp_short = p_score <= -15
    onch_exec = bool(o and "EXECUTING" in o.get("phase", ""))
    onch_bear = o_score <= -15 or onch_exec
    onch_locked = o_score >= 10
    onch_neutral = not onch_bear and not onch_locked

    if perp_long and onch_exec:
        direction, tier = "LONG (fast scalp only)", "CAUTION"
        notes.append("CONFLICT: perp squeeze-fuel LONG but on-chain distribution EXECUTING → bilateral. Fast scalp only, take TP1.")
    elif perp_long and (onch_locked or onch_neutral or not o):
        direction = "LONG"
        tier = "STRONG" if (p_score >= 35 and onch_locked) else "MILD"
        notes.append("Perp squeeze-fuel/trap-formation LONG; on-chain not contradicting (locked / neutral / unmapped).")
    elif perp_short and onch_exec:
        direction, tier = "SHORT", "STRONG"
        notes.append("CONVERGENCE: perp distribution-top SHORT + on-chain EXECUTING = Tier-1 Stage-5 short.")
    elif perp_short and onch_bear:
        direction, tier = "SHORT", "MILD"
        notes.append("Perp short + on-chain positioning (loading) = Tier-2. Take TP, don't oversize.")
    elif perp_short and (onch_neutral or onch_locked or not o):
        direction, tier = "SHORT (mild)", "MILD"
        notes.append("Perp short but no on-chain distribution confirmation = Tier-2; bilateral-squeeze risk.")
    elif (not perp_long and not perp_short) and onch_bear:
        if m.get("funding_unavailable"):
            direction, tier = "WATCH (funding unavailable)", "WATCH"
            notes.append("On-chain distribution loading but FUNDING UNAVAILABLE (all venues failed) — cannot "
                         "assess the §5 veto / §6 trigger. This is UNKNOWN, NOT neutral: verify funding "
                         "manually before arming. (SPEC 25 degrade-explicit — a null fetch must not read as flat.)")
        else:
            direction, tier = "SHORT (loading)", "WATCH"
            notes.append("On-chain distribution loading but perp funding NEUTRAL. Short fires when funding flips positive. WAIT.")
    elif (not perp_long and not perp_short) and onch_locked:
        direction, tier = "WATCH (constructive)", "WATCH"
        notes.append("Supply locked + perp neutral = constructive base, no setup.")
    else:
        direction, tier = "PASS", "PASS"
        notes.append("No convergence — perp + on-chain both neutral.")

    # GATE — LONG confirm via spot/perp CVD (§2 "4 reasons")
    if "LONG" in direction:
        if cvd_v == "BEARISH_DIVERGENCE":
            direction, tier = "PASS (CVD: hedge-trap)", "PASS"
            notes.append("⛔ LONG VETOED: spot SELLING while perp pumps = MM-hedge/distribution (reason #4). Exit liquidity.")
        elif cvd_v == "SPOT_REAL_WINDOW_THIN":
            if tier == "STRONG":
                tier = "MILD"
            notes.append("⚠ CVD window thin but 24h spot REAL — long stands UNCONFIRMED; size down.")
        elif cvd_v in ("UNRELIABLE_PERP_DRIVEN", "UNRELIABLE_THIN_SPOT"):
            direction, tier = "PASS (CVD: unreliable)", "PASS"
            notes.append(f"⛔ LONG VETOED: {cvd_v} — can't distinguish real squeeze from MM-hedge into thin/perp-driven spot.")
        elif cvd_v == "BULLISH_DIVERGENCE":
            notes.append("✅ CVD confirms the long: spot BUYING while perp sells = real money (reason #1).")
        elif cvd_v == "UNAVAILABLE":
            if tier == "STRONG":
                tier = "MILD"
            notes.append("⚠ spot-CVD UNAVAILABLE (spot_coverage none — no spot venue anywhere, "
                         "Binance/Bitget/DEX): the CVD leg is UNKNOWN, not bearish (SPEC 42 "
                         "degrade-explicit). Long stands UNCONFIRMED; size down.")
        else:
            if tier == "STRONG":
                tier = "MILD"
            notes.append(f"⚠ CVD does NOT confirm the long ({cvd_v or 'no data'}); size down, scout until spot confirms.")

    # GATE — SPEC 4: neg-funding LONG confluence (§4)
    direction, tier, gate_reason = _neg_funding_long_gate(
        direction, tier, notes, fr_4h, oi_chg, near_ath, cvd_v,
        onch_locked=onch_locked, onch_unavailable=onch_unavailable,
        top_holder_distributing=top_holder_distributing, oi_construction=oi_construction)

    # SPEC-177 req 3: the §4 gate note names the battlefield verdict so a perp_led name
    # can't present a spot-natured read; also carries whatever leverage_state framing
    # (TREND_FEEDING/RESET_CONSTRUCTIVE, req 4 bullets 2-3) the injected battlefield read
    # already computed. Framing only — never a veto/gate (governing principle, SPEC-180 G4).
    if battlefield and battlefield.get("battlefield") not in (None, "UNKNOWN"):
        notes.append(f"battlefield: {battlefield['battlefield']} "
                     f"(perp/spot vol ratio {battlefield.get('perp_spot_ratio')}) — "
                     "a perp-led name wants a perp-structure trigger, not a spot-level retest.")
        notes.extend(battlefield.get("annotations") or [])

    # GATE — SHORT-side funding rule (never short deep-NEG funding)
    if "SHORT" in direction and fr_4h is not None and fr_4h <= DEEP_NEG:
        direction, tier = "PASS (deep-neg funding)", "PASS"
        gate_reason = gate_reason or "short vetoed by deep-neg funding"
        notes.append(f"⛔ SHORT VETOED: funding {fr_4h:+.2f}%/4h deeply NEG (~{fr_4h*6:+.1f}%/day carry, squeeze fuel above). Clean shorts want flat/positive.")
        # SPEC-180 req 3: carry alone sustains the veto (no code change to it) — this
        # is an annotation-only fuel/no-fuel read naming whether the short-side OI
        # reading is genuine directional squeeze fuel or arb-dominated (no fuel).
        fuel_verdict, _ = _oic_short_side_read(oi_construction)
        if fuel_verdict == "ARB_DOMINATED":
            notes.append("  (fuel read: short-side OI is ARB_DOMINATED — no genuine squeeze fuel behind the veto.)")
        elif oi_construction and oi_construction.get("gating_ok") \
                and oi_construction.get("verdict") not in (None, "UNKNOWN"):
            notes.append("  (fuel read: short-side OI decomposition present, not arb-dominated — fuel likely genuine.)")

    if scout_liq and "PASS" not in direction:
        notes.append("Thin liquidity ($10–25M/24h) → scout size only.")

    # Per-side OI wash caveat (informational)
    oi_v = (oi or {}).get("verdict")
    if oi_v == "WASH":
        notes.append(f"⚠ OI is 对敲 WASH ({oi.get('wash_score')}/4): aggregate OI faked — don't read it as a trapped side.")
    elif oi_v == "REAL_DIRECTIONAL":
        notes.append("✅ Per-side OI is REAL directional — squeeze fuel is real.")

    return direction, tier, notes, p_score, o_score, gate_reason


def build_analyse(ticker, days=90):
    """Run the data layers concurrently (on-chain capped at ONCHAIN_BUDGET), converge,
    return the verdict dict. On-chain timeout/error → onchain:'UNAVAILABLE', perp verdict
    still returns (SPEC 1b)."""
    ticker = ticker.upper().replace("USDT", "")

    # On-chain read is the CHEAP nonce snapshot (the §8 live-top signal), not a getLogs
    # lifetime audit — it finishes in ~5s, capped at ONCHAIN_BUDGET (SPEC 5 / SPEC 1b).
    with ThreadPoolExecutor(max_workers=7) as ex:
        f_perp = ex.submit(run_json, "perp_analyser.py", ticker)
        f_onch = ex.submit(build_nonce_state, ticker)
        f_cvd = ex.submit(run_cvd, ticker, 30)   # SPEC 42: native real-venue spot-CVD
        f_struct = ex.submit(run_json, "intraday.py", ticker, None, 60)
        f_oi = ex.submit(run_json, "oi_sides.py", ticker, None, 60)
        f_whales = ex.submit(run_whales, ticker)
        # SPEC-180: oi_construction feeds the §4 gate's oi_rising leg. chip_state/
        # flow_confirmed/lock_info aren't wired at this call site (no existing state
        # source here) — the read is honestly UNKNOWN/leg-unchanged until one is,
        # never fabricated. Bounded like the on-chain read; a slow/dead sweep degrades
        # to None, never blocks the rest of the verdict (G4).
        f_oic = ex.submit(run_oi_construction, ticker)
        perp, cvd, struct, oi, whales = (f_perp.result(), f_cvd.result(),
                                         f_struct.result(), f_oi.result(), f_whales.result())
        try:
            nonce = f_onch.result(timeout=ONCHAIN_BUDGET)
        except (FuturesTimeout, Exception):
            nonce = None
        try:
            oi_construction = f_oic.result(timeout=OIC_BUDGET)
        except (FuturesTimeout, Exception):
            oi_construction = None

    # Map the nonce signal → an onch dict converge() reads unchanged, + the status field.
    nonce_signal = nonce.get("signal") if nonce else None
    newly_fired = (nonce or {}).get("escalation_fired", [])
    onch_ms = nonce.get("ms") if nonce else None
    PHASE = {"ESCALATION": "EXECUTING (mega-safe nonce fired — distribution/bid-pull)",
             "LOADING": "POSITIONING (distribution loading)", "DORMANT": "DORMANT (locked)",
             "QUIET": "neutral", "UNTRACKED": "no wallet map"}
    if nonce is None:
        onchain_status, onch, onch_unavailable = "UNAVAILABLE", {}, True
    elif not nonce.get("tracked"):
        onchain_status, onch, onch_unavailable = "UNMAPPED", {}, False
    else:
        onchain_status, onch_unavailable = "OK", False
        onch = {"result": {"score": nonce["score"], "phase": PHASE.get(nonce_signal, "neutral")}}

    if perp.get("_err") or perp.get("_timeout"):
        return {"ticker": ticker, "verdict": "PASS", "direction": "PASS (perp data unavailable)",
                "tier": "PASS", "reason": "perp analyser unavailable", "onchain": onchain_status,
                "onchain_ms": onch_ms, "nonce_signal": nonce_signal, "newly_fired": newly_fired,
                "notes": [f"perp data error: {perp.get('_err') or 'timeout'}"]}

    perp = _backfill_perp(perp, ticker)   # SPEC 25: hardened cross-venue price/funding (Bybit-only nulls)

    # SPEC-89: only run the (Moralis-heavy) top-holder verify when funding is in the §4 deep-neg
    # LONG region — the exact case where "is the operator distributing?" decides the trade. This
    # bounds the quota cost to the rare deep-neg-long read, and degrades to unchecked otherwise.
    _m = perp.get("metrics", {}) or {}
    _fr = _m.get("fr_4h")
    thd = {"checked": False, "distributing": False}
    if _fr is not None and _fr <= NEG_LONG_TRIGGER and perp.get("score", 0) > 0:
        thd = _top_holder_distribution_check(ticker)

    # SPEC-177: battlefield verdict from data already fetched THIS call — perp leg is
    # the cross-venue turnover perp_analyser already aggregated, spot leg is cvd's own
    # resolve_spot_venue read (req 5), leverage_4h reuses oi_sides' own WASH tag +
    # 4h ΔOI (`oi["verdict"]`/`oi["oi_change_pct"]`, already fetched above) — no new
    # fetch. `range_held` is unavailable here (no swing-point source at this call
    # site), so only a genuine WASH tag resolves to a non-UNKNOWN leverage_4h read.
    _oi_d = oi if isinstance(oi, dict) else {}
    battlefield = OM.build_battlefield(
        perp_vol_24h=_m.get("turnover"), spot_vol_24h=(cvd or {}).get("spot_vol_24h_usd"),
        leverage_4h={"delta_oi_pct": _oi_d.get("oi_change_pct"), "range_held": None,
                    "oi_sides_tag": _oi_d.get("verdict")} if _oi_d else None,
    )

    direction, tier, notes, p_score, o_score, gate_reason = converge(
        perp, onch, cvd=cvd, struct=struct, whales=whales, oi=oi, onch_unavailable=onch_unavailable,
        top_holder_distributing=bool(thd.get("checked") and thd.get("distributing")),
        battlefield=battlefield, oi_construction=oi_construction)

    # SPEC 42 — notes must NAME the venue the spot-CVD came from (or the explicit absence).
    cvd = cvd or {}
    if cvd.get("spot_venue"):
        v24 = cvd.get("spot_vol_24h_usd")
        notes.append(f"CVD source: {cvd['spot_venue']} spot"
                     + (f" (${v24/1e6:.1f}M/24h," if v24 else " (")
                     + f" coverage {cvd.get('spot_coverage')}) — real-venue read, not Binance-assumed.")
    elif cvd.get("spot_coverage") == "none":
        notes.append("⚠ spot-CVD UNAVAILABLE — no spot venue anywhere (Binance/Bitget spot unlisted, "
                     "no resolvable DEX pool). The §4 CVD leg is UNKNOWN — degrade-explicit, never a "
                     "silent gate-fail (SPEC 42).")

    m = perp.get("metrics", {}) or {}
    # SPEC-71: name the floor handling — the desk must see WHY the venue/rate it gets
    # differs from a venue's floor placeholder (or why a floor print is not a flat verdict).
    if m.get("funding_floor_demoted"):
        notes.append(f"⚠ funding floor-print DEMOTED: Bybit live print at the ±0.005 venue-floor "
                     f"sentinel (placeholder, §3) — using the real cross-venue rate "
                     f"{m['fr_4h']:+.3f}%/4h ({m.get('funding_venue')}) instead (SPEC-71).")
    elif m.get("floor_suspect"):
        notes.append("⚠ FLOOR-SUSPECT: the only covered venue prints the ±0.005 floor sentinel — "
                     "a placeholder, NOT a confident flat (§3; it once masked −1.65%/4h). "
                     "Verify funding on a second venue before any funding-gated action.")
    elif m.get("all_floor"):
        notes.append("funding at the venue floor on ≥2 covered venues — corroborated, genuinely "
                     "~0% flat (SPEC 19/44).")
    if nonce_signal == "ESCALATION":
        labels = ", ".join(f"{f['label']}({f['nonce_prev']}→{f['nonce_now']})" for f in newly_fired)
        notes.insert(0, f"🔴 ON-CHAIN ESCALATION: dormant safe nonce fired [{labels}] = distribution/bid-pull "
                        "firing (§8 Stage-5 — the cascade outruns the breakdown). This is the live top signal on a §4 name.")
    if onchain_status == "UNAVAILABLE":
        notes.append("⛔ ON-CHAIN UNAVAILABLE — on a §4/Cat A name this is NOT 'spectate calmly', it's BLIND to the "
                     "exact nonce signal that calls the top (§8). Run wallet_state/onchain on the mega-safes manually.")
    elif onchain_status == "UNMAPPED":
        notes.append("⚠ on-chain UNMAPPED (no tracked wallets) — perp-only; on-chain-clean cannot be assumed.")

    return {
        "ticker": ticker,
        "verdict": _coarse(direction),
        "direction": direction,
        "tier": tier,
        "reason": gate_reason or (notes[0] if notes else None),
        "onchain": onchain_status,
        "onchain_ms": onch_ms,
        "nonce_signal": nonce_signal,
        "newly_fired": newly_fired,
        "top_holder_distribution": thd,   # SPEC-89: {checked, distributing} top-holder verify sweep
        "perp_score": p_score,
        "onchain_score": o_score,
        "funding_4h": m.get("fr_4h"),
        "funding_unavailable": bool(m.get("funding_unavailable")),     # SPEC 25: explicit, never silent null
        "funding_venue": m.get("funding_venue"),                       # SPEC-71: always attributed
        "floor_suspect": bool(m.get("floor_suspect")),                 # SPEC-71: lone floor print = data failure, not flat
        "all_floor": bool(m.get("all_floor")),                         # SPEC-71: corroborated venue-floor flat
        "oi_chg_pct": m.get("oi_chg"),
        # SPEC-174 #6: this OI-change figure is perp_analyser.py's `openInterestHist?
        # period=1h&limit=48` — a FIXED 48h window, never parameterized by any native
        # caller. Additive alias, window labelled in the key (the MANTRA scout-sweep
        # confusion: this read +57% vs oi_sides' 4h-window +8.2%, neither labelled).
        "oi_chg_pct_48h": m.get("oi_chg"),
        "near_ath": bool(struct and struct.get("near_ath")),
        "cvd_verdict": (cvd or {}).get("verdict"),
        "cvd_venue": (cvd or {}).get("spot_venue"),                    # SPEC 42: the REAL spot venue
        "spot_coverage": (cvd or {}).get("spot_coverage"),             # SPEC 42: full|partial|none
        "spot_vol_24h_usd": (cvd or {}).get("spot_vol_24h_usd"),
        "cvd_detail": (cvd or {}).get("cvd_detail"),                   # SPEC-191 #1: adaptive-window detail
        "price": m.get("price"),
        "price_unavailable": bool(m.get("price_unavailable")),
        "price_venue": m.get("price_venue"),
        "notes": notes,
        "oi_construction": oi_construction,   # SPEC-180 req 5: full envelope, brief renders it
    }


def render_human(a):
    vstyle = {"LONG": "green", "SHORT": "red", "WATCH": "yellow", "PASS": "grey"}.get(a["verdict"], "grey")
    bar = "═" * 56
    print(C.c(bar, "bold", vstyle))
    print(C.c(f"  {a['ticker']} — VERDICT: {a['direction']}  [{a['tier']}]", "bold", vstyle))
    print(C.c(bar, "bold", vstyle))
    fr = a.get("funding_4h")
    print(f"Perp score {a.get('perp_score'):+} · on-chain {a['onchain']}"
          + (f" ({a['onchain_ms']}ms)" if a.get("onchain_ms") is not None else "")
          + (f" · funding {fr:+.3f}%/4h" if fr is not None else "")
          + (f" · CVD {a.get('cvd_verdict')}" if a.get("cvd_verdict") else "")
          + (" · near ATH" if a.get("near_ath") else ""))
    if a.get("reason"):
        print(C.c(f"→ {a['reason']}", "bold"))
    print("\n## Convergence read")
    for n in a.get("notes", []):
        print(f"  - {n}")


def main():
    ap = argparse.ArgumentParser(description="Combined perp+onchain verdict engine")
    ap.add_argument("ticker")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)
    a = build_analyse(args.ticker, args.days)
    if args.json:
        print(json.dumps(a))
    else:
        render_human(a)


if __name__ == "__main__":
    main()
