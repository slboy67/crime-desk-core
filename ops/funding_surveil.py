#!/usr/bin/env python3
"""funding_surveil.py — SPEC-93: standing extreme-negative-funding monitor (squeeze-loading radar).

`scan` already does the cross-sectional read over the whole perp universe and buckets the
deep-neg LONG side — but it is PULL-ONLY. This is the standing monitor (the funding mirror of
ops/surveil.sh + page_gate for on-chain nonces): a launchd-cadenced tick that watches the
deep-neg band continuously and PAGES when a name crosses into it. IN (deep-neg −2.5%/4h at a
fresh ATH + OI +41%, real $14M spot) only surfaced because the user dropped the ticker — this
is the layer that would have surfaced it unprompted.

The whole value is the §4 disambiguation baked into the alert: deep-neg ALONE is the coin-flip
the §4/§5 gate exists to filter, so every alert ships the OI-context decode + the §5 short-veto,
never a bare funding number:
  deep-neg + OI rising  = §4 squeeze-LONG candidate (the IN signature)
  deep-neg + OI flat/falling = hedge / OTC-distribution trap (reason #4, veto-context)

Reuses (minimal new surface):
  - scan        — the universe fetch + SPEC-11 to_4h normalization (the .sh feeds its longs[])
  - regime_flip — live_perp() does the cross-venue verify + floor rejection (§3): the verified
                  rate is the most-negative NON-floor venue; a lone-floor/zero print is SUSPECT
                  and rejected, never a datum ([[feedback_funding_0005_is_placeholder_not_flat]]).
                  DEEP_NEG (−0.30%/4h) is the §5 short-veto line.
  - inbox       — append_event() is the producer API onto the board feed.
  - page_gate   — the surveil.sh PAGE throttle (the .sh wires it; the baseline diff below is the
                  primary dedup — a name sitting in the band does not re-nag).

Pure decision functions over an injectable clock + state path + perp_fn → tests run offline.

SPEC-96 (the location layer): deep-neg + OI-rising is a §4 coin-flip in isolation. Each
candidate is classified by PRICE LOCATION (the TAIKO +126% discriminator):
  near a HELD low + basing + OI rising = SQUEEZE-LONG (TAIKO-type, HIGH) — the take
  near a HIGH + OI rising              = AMBIGUOUS (hedge/AMM/blowoff — the IN case, MED)
  near a low but NOT basing (fresh cascade) = FORMING (watch for a base — the LAB case, WATCH)
  OI flat/falling                     = no load (de-prioritize)
The price snapshot comes from price_structure (live_price_ctx) or is injected offline.

CLI (funding_surveil.sh wiring):
  python3 ops/funding_surveil.py tick --scan-json <file|-> [--perp-json <file|->] \
      [--price-json <file|->] [--state <path>] [--log <path>] [--now <iso>]
    reads a `scan --json` envelope (or bare {longs:[...]}); without --perp-json it cross-venue
    verifies each candidate live via regime_flip.live_perp; without --price-json it reads the
    price-location snapshot live via price_structure. Prints {paged:[...], alerts:[...]}.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "capabilities"))

# Reuse the §5 short-veto line + the cross-venue/floor-resolved live read (do not redefine them).
from regime_flip import DEEP_NEG, live_perp  # noqa: E402

STATE = ROOT / "state"
BASELINE_PATH = STATE / "funding_baseline.json"
LOG_PATH = STATE / "funding_alerts.log"

# ── tiered thresholds (documented defaults; %/4h, never annualized) ──────────────
BAND_ENTER = -1.0      # ≤ this enters the WATCH band (squeeze-loading radar)
DEEP_EXTREME = -2.0    # ≤ this is the HIGH tier (the clamp region — IN was −2.5)
# OI direction (mirrors analyse's §4 `oi_chg > 10` oi_rising gate — the long-gate threshold):
OI_RISE_PCT = 10.0     # OI up ≥ this % vs baseline = rising (loading, not hedging)
OI_FALL_PCT = -10.0    # OI down ≤ this % = falling (hedge / distribution unwind)
# Liquidity gate (§7): <~$10M/24h = auto-PASS (DUST), $10–25M = scout-only, ≥$25M = tradeable.
DUST_VOL_M = 10.0
SCOUT_VOL_M = 25.0

# ── SPEC-96: price-LOCATION thresholds (%; documented defaults) ───────────────────
# The deep-neg + OI-rising signature is a §4 coin-flip in isolation (trapped-shorts that
# squeeze vs delta-neutral hedge/arb that never does). The TAIKO +126% (2026-07-01) taught the
# screenable discriminator: PRICE LOCATION. At/just-above a HELD capitulation low + basing =
# shorts are directional at a bottom → high-conviction squeeze-LONG; near a HIGH = ambiguous
# (AMM-farm / vesting-hedge / blowoff — the IN case). [[feedback_atl_shortsqueeze_is_the_deepneg_oi_loading_tell]]
NEAR_LOW_PCT = 25.0        # ≤ this % above the recent major low/ATL = near a low
NEAR_HIGH_PCT = 25.0       # ≤ this % below the recent major high/ATH = near a high
BASING_RANGE_PCT = 25.0    # recent ~3d high/low range < this % = coiling (tight), not cascading
LOW_HELD_BOUNCE_PCT = 2.0  # price bounced ≥ this % off the low = held (not a fresh-breaking low)

# location tags (the §4 coin-flip resolved into an actionable classification)
TAG_SQUEEZE_LONG = "SQUEEZE-LONG (high conviction, TAIKO-type)"
TAG_AMBIGUOUS = "AMBIGUOUS (hedge/AMM/blowoff — needs gate)"
TAG_FORMING = "FORMING (watch for a base)"
TAG_NO_LOAD = "no load"


# ── pure classifiers ────────────────────────────────────────────────────────────
def oi_context(oi_chg_pct):
    """OI direction bucket vs the persisted baseline. None (first sight / no baseline) = unknown."""
    if oi_chg_pct is None:
        return "unknown"
    if oi_chg_pct >= OI_RISE_PCT:
        return "rising"
    if oi_chg_pct <= OI_FALL_PCT:
        return "falling"
    return "flat"


def liq_tier(turnover_m):
    if turnover_m is None:
        return "unknown"
    if turnover_m < DUST_VOL_M:
        return "DUST"
    if turnover_m < SCOUT_VOL_M:
        return "scout"
    return "tradeable"


def decode(funding_4h, oi_ctx):
    """The §4 disambiguation baked into the alert. Returns (label, severity, is_long_candidate).

    deep-neg + OI rising = §4 squeeze-LONG candidate (the IN signature, loading not hedging).
    deep-neg + OI flat/falling = hedge / OTC-distribution trap (reason #4 — you = exit liquidity).
    OI unknown (first sight, no baseline) = coin-flip, OI-context pending (§4)."""
    if oi_ctx == "rising":
        return ("§4 squeeze-LONG candidate", "HIGH", True)
    if oi_ctx in ("flat", "falling"):
        return ("hedge/distribution trap (veto-context, §4 reason #4)", "MED", False)
    return ("OI-context pending — funding alone is a coin-flip (§4)", "MED", False)


# ── SPEC-96: price-LOCATION classifiers (resolve the §4 deep-neg+OI coin-flip) ────
def pct_above_low(price, low):
    """% the current price sits above the recent major low / ATL. None if unusable."""
    if price is None or low is None or low <= 0:
        return None
    return round((price - low) / low * 100.0, 1)


def pct_below_high(price, high):
    """% the current price sits below the recent major high / ATH. None if unusable."""
    if price is None or high is None or high <= 0:
        return None
    return round((high - price) / high * 100.0, 1)


def low_held(price, low, new_lows=False, bounce_pct=LOW_HELD_BOUNCE_PCT):
    """The low HELD = price bounced ≥bounce_pct off it AND no fresh new lows in the window
    (not sitting on a fresh-breaking low)."""
    pal = pct_above_low(price, low)
    if pal is None:
        return False
    return pal >= bounce_pct and not new_lows


def is_near_held_low(price, low, new_lows=False, near_pct=NEAR_LOW_PCT,
                     bounce_pct=LOW_HELD_BOUNCE_PCT):
    """Within near_pct% above a low that HELD → shorts here are directional-at-a-bottom."""
    pal = pct_above_low(price, low)
    if pal is None:
        return False
    return pal <= near_pct and low_held(price, low, new_lows=new_lows, bounce_pct=bounce_pct)


def is_near_high(price, high, near_pct=NEAR_HIGH_PCT):
    """Within near_pct% below a high → the ambiguous (hedge/AMM/blowoff) location."""
    pbh = pct_below_high(price, high)
    if pbh is None:
        return False
    return pbh <= near_pct


def is_basing(range_3d_pct, new_lows=False, range_pct=BASING_RANGE_PCT):
    """Coiling: recent ~3d range tight AND no new lows (not still cascading)."""
    if range_3d_pct is None:
        return False
    return range_3d_pct < range_pct and not new_lows


def price_context(raw):
    """Compute the SPEC-96 location flags from a raw price snapshot. None if unusable.

    raw: {price, low (14-30d window low/ATL), high (window high/ATH), range_3d_pct, new_lows}
    — the price_structure-shaped snapshot (live: live_price_ctx wraps price_structure)."""
    if not raw:
        return None
    price = raw.get("price")
    low = raw.get("low")
    high = raw.get("high")
    new_lows = bool(raw.get("new_lows"))
    range_3d = raw.get("range_3d_pct")
    return {
        "price": price,
        "pct_above_low": pct_above_low(price, low),
        "pct_below_high": pct_below_high(price, high),
        "near_held_low": is_near_held_low(price, low, new_lows=new_lows),
        "near_high": is_near_high(price, high),
        "basing": is_basing(range_3d, new_lows=new_lows),
        "new_lows": new_lows,
        "range_3d_pct": range_3d,
    }


def classify_location(oi_ctx, pc):
    """Resolve the §4 deep-neg+OI coin-flip by PRICE LOCATION (the TAIKO +126% discriminator).
    Returns (tag, severity, is_squeeze_long).

      near a HELD low + basing + OI rising     → SQUEEZE-LONG (shorts directional at a held
                                                  bottom, sellers exhausted — TAIKO-type)   HIGH
      near a low but NOT basing (fresh cascade) → FORMING (watch for a base — the LAB case)  WATCH
      near a HIGH + OI rising                    → AMBIGUOUS (hedge/AMM/blowoff — the IN case) MED
      OI flat/falling                           → no load (de-prioritize)                     LOW
    """
    if oi_ctx != "rising":
        return (TAG_NO_LOAD, "LOW", False)
    if not pc:
        return (TAG_AMBIGUOUS, "MED", False)   # OI loading but no price context → needs the gate
    if pc.get("near_held_low"):
        if pc.get("basing"):
            return (TAG_SQUEEZE_LONG, "HIGH", True)
        return (TAG_FORMING, "WATCH", False)
    if pc.get("near_high"):
        return (TAG_AMBIGUOUS, "MED", False)
    return (TAG_AMBIGUOUS, "MED", False)


def live_price_ctx(ticker):
    """Live price-location snapshot for classify_location — wraps price_structure (Binance
    Futures dailies, 30d window fits the §-14-30d low). Returns the raw price_ctx shape or None."""
    try:
        import price_structure as ps
        s = ps.build_structure(ticker, days=30)
    except Exception:  # noqa: BLE001 — a dead price read drops the location layer, not the tick
        return None
    if not s or s.get("error"):
        return None
    return {
        "price": s.get("current_close"),
        "low": s.get("window_low"),
        "high": s.get("window_high"),
        "range_3d_pct": s.get("range_3d_pct"),
        "new_lows": s.get("days_since_atl") == 0,
    }


# ── enrichment (one verified, cross-venue-confirmed band hit) ─────────────────────
def _venue_note(lp):
    """Short non-floor/other-venue annotation for the alert copy."""
    canon = lp.get("venue")
    parts = []
    for name, v in (lp.get("venues") or {}).items():
        if name == canon:
            continue
        f4 = v.get("funding_4h")
        tag = " floor" if v.get("is_floor") else ""
        if f4 is not None:
            parts.append(f"{name} {f4:+.2f}{tag}")
    extra = ("; " + ", ".join(parts)) if parts else ""
    return f"{canon}, non-floor{extra}"


def enrich_hit(cand, lp, oi_chg_pct=None, near_ath=None, price_raw=None):
    """Cross-venue-verify + enrich one scan `longs[]` row. Returns the alert dict, or None if the
    candidate is REJECTED (no perp / lone-floor SUSPECT / all-floor flat / cross-venue downgrade
    out of the band). The verified rate is the most-negative NON-floor venue (from live_perp).

    SPEC-96: when a `price_raw` snapshot is supplied, the §4 coin-flip is resolved by PRICE
    LOCATION (near a held low + basing = SQUEEZE-LONG; near a high = AMBIGUOUS; near a low but
    fresh cascade = FORMING; OI not rising = no load) — the location tag then drives the
    alert severity + is_long_candidate. Without a snapshot the SPEC-93 OI-only decode stands."""
    if not lp:
        return None
    if lp.get("funding_suspect") or lp.get("all_floor"):
        return None                                  # §3: a floor/placeholder is a data FAILURE
    f4 = lp.get("funding_4h")
    if f4 is None or f4 > BAND_ENTER:                # cross-venue confirms it's not in the band
        return None

    oi_ctx = oi_context(oi_chg_pct)
    label, severity, is_long = decode(f4, oi_ctx)
    tier = liq_tier(cand.get("turnover_m"))
    band = "HIGH" if f4 <= DEEP_EXTREME else "WATCH"
    short_veto = f4 <= DEEP_NEG                       # §5: every deep-neg hit is a short-veto

    # ── SPEC-96 location layer: resolve the coin-flip by price location when we have a snapshot ──
    pc = price_context(price_raw)
    loc_tag, loc_sev, is_squeeze_long = classify_location(oi_ctx, pc)
    if pc is not None:                                # location drives the verdict when available
        severity = loc_sev
        is_long = is_squeeze_long

    # ── inbox copy: carries the decode/tag, not just the number (req 4/6) ──
    oi_str = (f"OI {oi_chg_pct:+.0f}%" if oi_chg_pct is not None else "OI Δ pending")
    vol_str = f"${cand.get('turnover_m', 0):.0f}M"
    vol_tag = {"DUST": "DUST (untradeable)", "scout": f"{vol_str} (scout)",
               "tradeable": vol_str, "unknown": vol_str}[tier]
    ath_str = " near_ath." if near_ath else ""
    veto = "§5 short-veto" + (" · DUST" if tier == "DUST" else "")
    if pc is not None:                               # SPEC-96 copy: carries the location tag
        loc_detail = ""
        pal = pc.get("pct_above_low")
        pbh = pc.get("pct_below_high")
        if pc.get("near_held_low"):
            base_word = "basing" if pc.get("basing") else "not-basing (fresh cascade)"
            off = f"{pal:.0f}% off held low" if pal is not None else "off held low"
            loc_detail = f" + {base_word} {off}"
        elif pc.get("near_high"):
            loc_detail = f" + {pbh:.0f}% below high" if pbh is not None else " + near high"
        verdict = loc_tag
    else:
        loc_detail = ""
        verdict = label
    msg = (f"DEEP-NEG: {cand['ticker']} {f4:+.2f}%/4h ({_venue_note(lp)}) + {oi_str}{loc_detail} "
           f"→ {verdict} · {veto} · spot {vol_tag}.{ath_str} Run brief/tape before sizing.")

    hit = {
        "ticker": cand["ticker"], "funding_4h": f4, "scan_funding_4h": cand.get("funding_4h"),
        "venue": lp.get("venue"), "oi": lp.get("oi"), "oi_chg_pct": oi_chg_pct,
        "oi_context": oi_ctx, "band": band, "tier": tier, "turnover_m": cand.get("turnover_m"),
        "near_ath": bool(near_ath), "is_long_candidate": is_long, "short_veto": short_veto,
        "severity": severity, "label": label, "msg": msg,
        "location_tag": (loc_tag if pc is not None else None),
        "is_squeeze_long": (is_squeeze_long if pc is not None else False),
    }
    if pc is not None:
        hit.update({"pct_above_low": pc.get("pct_above_low"),
                    "pct_below_high": pc.get("pct_below_high"),
                    "near_held_low": pc.get("near_held_low"), "basing": pc.get("basing"),
                    "near_high": pc.get("near_high")})
    return hit


# ── change-detection (the dedup that stops a band-sitter re-nagging) ──────────────
def _decide_page(hit, prev):
    """Page only when (a) NEW name, (b) materially deepens (WATCH→HIGH / crosses DEEP_EXTREME),
    or (c) OI-context flips TO rising. A name sitting in the band logs but does not re-page."""
    if prev is None:
        return True, "new"
    prev_f = prev.get("funding_4h")
    if prev_f is not None and prev_f > DEEP_EXTREME >= hit["funding_4h"]:
        return True, "deepened"
    if hit["oi_context"] == "rising" and prev.get("oi_context") != "rising":
        return True, "oi-flip-rising"
    return False, "in-band-no-change"


def assess(cands, perp_fn, baseline, now, near_ath_fn=None, price_ctx_fn=None):
    """Pure tick: verify + enrich + diff each scan candidate against the persisted baseline.

    cands: scan `longs[]` rows. perp_fn: ticker -> live_perp dict (injectable; default live).
    baseline: {ticker: {funding_4h, oi, oi_context, tier, first_seen}}.
    price_ctx_fn: ticker -> raw price snapshot (SPEC-96 location layer; injectable, default
    live_price_ctx). Returns (alerts, new_baseline). alerts carry page/reason + location_tag;
    DUST never pages. Does NOT mutate the input baseline. A name that leaves the band (or fails
    verify) is dropped from the baseline so its next genuine re-entry pages as `new`."""
    new_baseline = {}
    alerts = []
    now_iso = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for cand in cands:
        tk = cand["ticker"]
        if cand.get("funding_4h") is None or cand["funding_4h"] > BAND_ENTER:
            continue                                 # scan thresh may be looser than the band
        try:
            lp = perp_fn(tk)
        except Exception:  # noqa: BLE001 — a dead read drops the candidate, never the tick
            lp = None
        prev = baseline.get(tk)
        prev_oi = prev.get("oi") if prev else None
        oi = lp.get("oi") if lp else None
        oi_chg_pct = None
        if prev_oi and oi is not None and prev_oi > 0:
            oi_chg_pct = round((oi - prev_oi) / prev_oi * 100.0, 1)

        near_ath = None
        if near_ath_fn:
            try:
                near_ath = near_ath_fn(tk)
            except Exception:  # noqa: BLE001
                near_ath = None

        price_raw = None
        if price_ctx_fn:
            try:
                price_raw = price_ctx_fn(tk)
            except Exception:  # noqa: BLE001 — a dead price read drops the location layer, not the tick
                price_raw = None

        hit = enrich_hit(cand, lp, oi_chg_pct=oi_chg_pct, near_ath=near_ath, price_raw=price_raw)
        if hit is None:
            continue                                 # rejected: floor/suspect/downgrade — not tracked

        page, reason = _decide_page(hit, prev)
        if hit["tier"] == "DUST":                    # DUST is logged/tracked but never paged
            page, reason = False, "dust"
        hit["page"], hit["reason"] = page, reason
        alerts.append(hit)
        new_baseline[tk] = {"funding_4h": hit["funding_4h"], "oi": hit["oi"],
                            "oi_context": hit["oi_context"], "tier": hit["tier"],
                            "first_seen": (prev or {}).get("first_seen", now_iso),
                            "last_seen": now_iso}
    return alerts, new_baseline


# ── persistence + tick driver ────────────────────────────────────────────────────
def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return default


def _write_json(path, obj):
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2))
        tmp.replace(p)
    except Exception:  # noqa: BLE001 — persistence must never break the sweep
        pass


def run_tick(cands, perp_fn, state_path=BASELINE_PATH, log_path=LOG_PATH,
             now=None, emit_fn=None, near_ath_fn=None, price_ctx_fn=None):
    """One full monitor tick: load baseline → assess → write log (always) → fire inbox events for
    the PAGED hits → persist baseline. emit_fn(ts,ticker,source,severity,msg) defaults to
    inbox.append_event. price_ctx_fn is the SPEC-96 location layer. Returns {paged, alerts}."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    baseline = _read_json(state_path, {})
    alerts, new_baseline = assess(cands, perp_fn, baseline, now, near_ath_fn=near_ath_fn,
                                  price_ctx_fn=price_ctx_fn)

    # the LOG line is ALWAYS written (the record is complete); only the PAGE is change-gated.
    lines = []
    for a in alerts:
        mark = "PAGE" if a["page"] else "log"
        lines.append(f"{now_iso} {mark} [{a['reason']}] {a['msg']}")
    if not alerts:
        lines.append(f"{now_iso} quiet (no in-band deep-neg names)")
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with Path(log_path).open("a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        pass

    if emit_fn is None:
        import inbox
        emit_fn = inbox.append_event

    paged = []
    for a in alerts:
        if not a["page"]:
            continue
        paged.append(a["ticker"])
        try:
            emit_fn(now_iso, a["ticker"], "funding-surveil", a["severity"], a["msg"])
        except Exception:  # noqa: BLE001 — a dead producer must not abort the tick
            pass

    _write_json(state_path, new_baseline)
    return {"paged": paged, "alerts": alerts}


def _load_input(arg):
    if arg in (None, ""):
        return None
    text = sys.stdin.read() if arg == "-" else Path(arg).read_text()
    return json.loads(text or "{}")


def _cands_from_scan(env):
    """Accept either a `scan --json` envelope ({longs:[...]} or orchestrator-wrapped
    {data:{longs:[...]}}) or a bare {longs:[...]}."""
    if not isinstance(env, dict):
        return []
    if isinstance(env.get("data"), dict) and isinstance(env["data"].get("longs"), list):
        return env["data"]["longs"]
    if isinstance(env.get("longs"), list):
        return env["longs"]
    return []


def _cli_tick(args):
    scan_env = _load_input(args.scan_json)
    cands = _cands_from_scan(scan_env)
    perp_table = _load_input(args.perp_json) if args.perp_json else None
    if perp_table is not None:
        perp_fn = lambda tk: perp_table.get(tk)  # noqa: E731 — offline injected reads
    else:
        perp_fn = live_perp
    price_table = _load_input(args.price_json) if args.price_json else None
    if price_table is not None:
        price_ctx_fn = lambda tk: price_table.get(tk)  # noqa: E731 — offline injected snapshots
    else:
        price_ctx_fn = live_price_ctx            # SPEC-96: live price-location layer (price_structure)
    now = (datetime.strptime(args.now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
           if args.now else None)
    state = Path(args.state) if args.state else BASELINE_PATH
    log = Path(args.log) if args.log else LOG_PATH
    result = run_tick(cands, perp_fn, state_path=state, log_path=log, now=now,
                      price_ctx_fn=price_ctx_fn)
    print(json.dumps(result))
    return 0


def main():
    ap = argparse.ArgumentParser(description="SPEC-93 extreme-neg-funding monitor")
    sub = ap.add_subparsers(dest="cmd")
    t = sub.add_parser("tick", help="run one monitor tick")
    t.add_argument("--scan-json", default="-", help="scan --json envelope (file or - for stdin)")
    t.add_argument("--perp-json", default=None,
                   help="offline: {ticker: live_perp} injected (skip live cross-venue verify)")
    t.add_argument("--price-json", default=None,
                   help="offline: {ticker: price_ctx} injected (skip live price_structure read)")
    t.add_argument("--state", default=None, help="baseline state path")
    t.add_argument("--log", default=None, help="alert log path")
    t.add_argument("--now", default=None, help="ISO ts (offline determinism)")
    args = ap.parse_args()
    if args.cmd == "tick":
        return _cli_tick(args)
    ap.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
