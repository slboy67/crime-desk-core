#!/usr/bin/env python3
"""oi_mc.py — SPEC-122: OI/MC ratio as a "perp-casino" flag.

The ledger (SPEC-40) shows the desk's single worst signature is mindshare/blowoff-top
short: n=324, hit 32%, total -26.3R — vertical-candle perp pumps on low-MC names where
the game is entirely in the derivatives (shorts get chopped on the vertical, longs get
caught in the OI unwind). Surfaced live 2026-07-10 (EVAA, via CT analyst DoubleEdge):
**OI/MC ratio**. EVAA ran at 252% OI/MC — open interest 2.5x the entire market cap =
there's almost no float, the whole move is a perp game. That one ratio instantly
separates a perp-manipulation casino (avoid) from a name with a real on-chain float
(tradeable).

  oi_mc_ratio = cross-venue OI (USD) / circulating market cap (USD)
    < 0.5    -> normal       (on-chain float dominates; on-chain reads meaningful)
    0.5-1.5  -> PERP_HEAVY   (derivatives large vs float — weight perp, discount on-chain)
    > 1.5    -> PERP_CASINO  (OI dominates MC — vertical/chop/unwind profile, the -26R zone)

PERP_CASINO does NOT hard-block a trade (an operator-timed OI-unwind short can still
work) — it attaches an explicit caveat to a SHORT-direction row so the desk never takes
one of these blind (memory: feedback_lead_with_the_disqualifier_dont_make_user_extract_it
— EVAA was the case). Cross-references liqs' SUSPECT_FAKE OI verdict when both fire (the
OI itself may be faked, not just heavy).

§3: a missing/zero OI or MC input is a data FAILURE, not a datum — degrades to
`oi_mc_ratio: null`, never a fabricated `normal`/`PERP_CASINO`.

  python3 capabilities/oi_mc.py EVAA --oi-usd 50000000 --mc-usd 19800000 --direction SHORT --json
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
CONFIG_PATH = ROOT / "config" / "oi_mc.json"

DEFAULT_CFG = {
    "perp_heavy_min": 0.5,     # ratio >= this (and <= perp_casino_min) -> PERP_HEAVY
    "perp_casino_min": 1.5,    # ratio > this -> PERP_CASINO
}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        d = json.loads(CONFIG_PATH.read_text())
        cfg.update({k: v for k, v in d.items() if k in DEFAULT_CFG})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def compute_ratio(oi_usd, mc_usd):
    """cross-venue OI (USD) / circulating MC (USD). None when either input is missing or
    non-positive (§3: a missing/zero input is a data failure, never a fabricated ratio)."""
    if oi_usd is None or mc_usd is None:
        return None
    try:
        oi_usd = float(oi_usd)
        mc_usd = float(mc_usd)
    except (TypeError, ValueError):
        return None
    if oi_usd < 0 or mc_usd <= 0:
        return None
    return oi_usd / mc_usd


def flag_for_ratio(ratio, cfg=None):
    """normal | PERP_HEAVY | PERP_CASINO, or None when ratio is None (a missing input
    must never read as a false 'normal')."""
    if ratio is None:
        return None
    cfg = dict(DEFAULT_CFG, **(cfg or {}))
    if ratio > cfg["perp_casino_min"]:
        return "PERP_CASINO"
    if ratio >= cfg["perp_heavy_min"]:
        return "PERP_HEAVY"
    return "normal"


def caveat_for(ratio, flag, direction, liq_verdict=None):
    """The explicit short-discipline caveat (req 3) — fires ONLY on PERP_CASINO + a
    SHORT-direction row (the blowoff-short -26R signature). Never blocks/vetoes the
    trade — the operator-timed OI-unwind short can still work — it just renders so the
    desk never takes one of these blind."""
    if flag != "PERP_CASINO" or (direction or "").upper() != "SHORT":
        return None
    msg = (f"⚠ PERP_CASINO (OI/MC {ratio * 100:.0f}%) — blowoff-short is the "
           "−26R signature, timing-bet only, not a setup")
    if liq_verdict == "SUSPECT_FAKE":
        msg += " [+ liqs SUSPECT_FAKE — the OI build itself may be faked]"
    return msg


def build_oi_mc(oi_usd, mc_usd, direction=None, liq_verdict=None, cfg=None):
    """The full envelope one call site wires in: {oi_usd, mc_usd, oi_mc_ratio, oi_mc_flag,
    oi_mc_caveat}. Pure — no I/O."""
    ratio = compute_ratio(oi_usd, mc_usd)
    flag = flag_for_ratio(ratio, cfg=cfg)
    return {
        "oi_usd": oi_usd,
        "mc_usd": mc_usd,
        "oi_mc_ratio": round(ratio, 4) if ratio is not None else None,
        "oi_mc_flag": flag,
        "oi_mc_caveat": caveat_for(ratio, flag, direction, liq_verdict=liq_verdict),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SPEC-177 — battlefield verdict (perp_led/spot_led/mixed/UNKNOWN) + leverage_state
#
# The Cartel framework's primary classifier — is this name's game on the perp or the
# spot — was not computed anywhere. Both halves of the perp/spot volume ratio are
# already fetched elsewhere in the desk (cvd.resolve_spot_venue 24h USD; venue_map
# per-venue vol24h_usd / a venue's own 24h ticker) — `build_battlefield` is a PURE
# composition over those already-resolved numbers plus the existing `build_oi_mc`
# envelope, alongside `leverage_state` (has leverage entered or left, over TWO
# windows). No I/O in this section except the clearly-marked `fetch_leverage_window`
# best-effort helper, which callers use optionally and which degrades every field to
# None on any failure — never raises, never blocks a verdict.
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_BF_CFG = {
    "perp_led_min": 4.0,       # perp/spot 24h-vol ratio >= this -> perp_led
    "spot_led_max": 2.0,       # ratio <= this (AND oi_mc "normal") -> spot_led
    "delta_oi_sig_4h": 8.0,    # |ΔOI%| significance floor, 4h window
    "delta_oi_sig_48h": 15.0,  # |ΔOI%| significance floor, 48h window
}


def load_bf_cfg():
    cfg = dict(DEFAULT_BF_CFG)
    try:
        d = json.loads(CONFIG_PATH.read_text())
        cfg.update({k: v for k, v in d.items() if k in DEFAULT_BF_CFG})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def compute_perp_spot_ratio(perp_vol_24h, spot_vol_24h):
    """None-degrading (§3): a missing/non-positive leg never fabricates a ratio."""
    if perp_vol_24h is None or spot_vol_24h is None:
        return None
    try:
        perp_vol_24h = float(perp_vol_24h)
        spot_vol_24h = float(spot_vol_24h)
    except (TypeError, ValueError):
        return None
    if spot_vol_24h <= 0 or perp_vol_24h < 0:
        return None
    return perp_vol_24h / spot_vol_24h


def battlefield_verdict(perp_vol_24h, spot_vol_24h, oi_mc_flag, cfg=None):
    """(ratio, verdict) — perp_led | spot_led | mixed | UNKNOWN. Asymmetric (SPEC-177
    req 1): perp_led fires on EITHER a high ratio OR a heavy OI/MC flag alone (no real
    spot venue IS the perp_led condition, not a missing input); spot_led requires BOTH
    legs present and low; UNKNOWN only when both legs are unknowable."""
    cfg = dict(DEFAULT_BF_CFG, **(cfg or {}))
    ratio = compute_perp_spot_ratio(perp_vol_24h, spot_vol_24h)
    oi_heavy = oi_mc_flag in ("PERP_HEAVY", "PERP_CASINO")
    if (ratio is not None and ratio >= cfg["perp_led_min"]) or oi_heavy:
        return ratio, "perp_led"
    if ratio is None and oi_mc_flag is None:
        return ratio, "UNKNOWN"
    if ratio is not None and oi_mc_flag == "normal" and ratio <= cfg["spot_led_max"]:
        return ratio, "spot_led"
    return ratio, "mixed"


def _range_held(closes, swing_point, direction):
    """CLOSE-only range-hold (SPEC-177 req 2): wick-throughs never break hold, only a
    CLOSE beyond the defining swing point does. direction 'long' -> swing_point is a
    swing LOW (a close below it breaks hold); 'short' -> swing HIGH (a close above it
    breaks hold). None when the inputs can't answer the question."""
    if not closes or swing_point is None:
        return None
    if direction == "short":
        return not any(c > swing_point for c in closes)
    return not any(c < swing_point for c in closes)


LEVERAGE_STATES = ("RESET_CONSTRUCTIVE", "MOVE_DONE", "LOADING", "TREND_FEEDING",
                   "WASH_PINNED", "UNKNOWN")


def leverage_state_for_window(delta_oi_pct, range_held, oi_sides_tag, threshold_pct, window_label):
    """One window's leverage_state verdict (SPEC-177 req 2). `oi_sides_tag == "WASH"`
    overrides every other read (fake directional OI, distrust the aggregate build).
    Sub-threshold |ΔOI%| has no verdict (state: None) — insufficient move to classify,
    not a fabricated read. Missing series -> UNKNOWN with a named reason (§3)."""
    if oi_sides_tag == "WASH":
        return {"state": "WASH_PINNED", "window": window_label, "delta_oi_pct": delta_oi_pct,
                "range_held": range_held, "reason": "oi_sides tag WASH overrides all other reads"}
    if delta_oi_pct is None or range_held is None:
        return {"state": "UNKNOWN", "window": window_label, "delta_oi_pct": delta_oi_pct,
                "range_held": range_held, "reason": "insufficient series (delta_oi_pct/range_held missing)"}
    if abs(delta_oi_pct) < threshold_pct:
        return {"state": None, "window": window_label, "delta_oi_pct": delta_oi_pct,
                "range_held": range_held, "reason": "sub-threshold ΔOI"}
    if delta_oi_pct <= -threshold_pct:
        state = "RESET_CONSTRUCTIVE" if range_held else "MOVE_DONE"
    else:
        state = "LOADING" if range_held else "TREND_FEEDING"
    return {"state": state, "window": window_label, "delta_oi_pct": delta_oi_pct,
            "range_held": range_held, "reason": None}


def build_battlefield(oi_usd=None, mc_usd=None, perp_vol_24h=None, spot_vol_24h=None,
                      direction=None, liq_verdict=None, leverage_4h=None, leverage_48h=None,
                      cfg=None):
    """Pure — no I/O. `leverage_4h`/`leverage_48h` are each either None (insufficient
    series -> UNKNOWN) or a dict `{delta_oi_pct, range_held, oi_sides_tag}` the caller
    has already resolved (e.g. via `fetch_leverage_window`). Composes the existing
    `build_oi_mc` envelope, the battlefield verdict, both leverage_state windows, and
    the framing-only annotations (SPEC-177 req 4 — never a veto/gate)."""
    bf_cfg = dict(DEFAULT_BF_CFG, **(cfg or {})) if cfg else load_bf_cfg()
    om = build_oi_mc(oi_usd, mc_usd, direction=direction, liq_verdict=liq_verdict, cfg=cfg)
    ratio, verdict = battlefield_verdict(perp_vol_24h, spot_vol_24h, om["oi_mc_flag"], cfg=bf_cfg)

    def _lev(inputs, window_label, threshold_key):
        if inputs is None:
            return {"state": "UNKNOWN", "window": window_label, "delta_oi_pct": None,
                    "range_held": None, "reason": "insufficient series"}
        return leverage_state_for_window(inputs.get("delta_oi_pct"), inputs.get("range_held"),
                                         inputs.get("oi_sides_tag"), bf_cfg[threshold_key],
                                         window_label)

    leverage_state = {
        "leverage_4h": _lev(leverage_4h, "4h", "delta_oi_sig_4h"),
        "leverage_48h": _lev(leverage_48h, "48h", "delta_oi_sig_48h"),
    }

    annotations = []
    states = {leverage_state["leverage_4h"]["state"], leverage_state["leverage_48h"]["state"]}
    if "TREND_FEEDING" in states:
        annotations.append("chasing the first spike is trash — the reset+next-OI-expansion is the trade.")
    if verdict == "perp_led" and "RESET_CONSTRUCTIVE" in states:
        annotations.append("RESET_CONSTRUCTIVE on a perp-led name — the next-OI-expansion is the entry, "
                           "not the reset itself.")

    return {
        "perp_vol_24h": perp_vol_24h, "spot_vol_24h": spot_vol_24h,
        "perp_spot_ratio": round(ratio, 4) if ratio is not None else None,
        "battlefield": verdict,
        "oi_mc": om,
        "leverage_state": leverage_state,
        "annotations": annotations,
    }


def commit_time_annotation(battlefield, entry_kind):
    """SPEC-177 req 4 bullet 1 — a commit-time flag when a thesis entry on a
    `perp_led` name is a spot-level retest with no perp-structure trigger. Framing
    only, never a veto/gate; None when the condition doesn't apply."""
    if battlefield == "perp_led" and entry_kind == "spot_level_retest":
        return "spot-natured entry on perp-led name"
    return None


def _http_get(url, timeout=8):
    """Minimal JSON GET for the leverage-window fetch helper below — mirrors the
    `regime_flip.fetch`/`price_structure.fetch` pattern already used elsewhere in the
    desk (no new HTTP-client dependency)."""
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "oi-mc-battlefield/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None


def fetch_leverage_window(ticker, period, kline_interval, limit, swing_point, direction):
    """Best-effort I/O (SPEC-177): ΔOI% + closes-only range-hold for ONE window, via
    Binance's keyless `futures/data/openInterestHist` (same endpoint `triage.py`
    already uses for its 24h ΔOI read) + `klines` (close only, price_structure's own
    fetch shape). Any failure degrades a field to None (-> the caller's
    `leverage_state_for_window` reads UNKNOWN) — never raises, no fabricated read."""
    sym = f"{ticker.upper()}USDT"
    delta_oi_pct = None
    oi_hist = _http_get(
        f"https://fapi.binance.com/futures/data/openInterestHist?symbol={sym}&period={period}&limit={limit}")
    if isinstance(oi_hist, list) and len(oi_hist) >= 2:
        try:
            first = float(oi_hist[0]["sumOpenInterest"])
            last = float(oi_hist[-1]["sumOpenInterest"])
            delta_oi_pct = round((last / first - 1) * 100, 2) if first else None
        except (KeyError, TypeError, ValueError):
            pass
    range_held = None
    klines = _http_get(
        f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval={kline_interval}&limit={limit}")
    if isinstance(klines, list) and klines:
        try:
            closes = [float(k[4]) for k in klines]
            range_held = _range_held(closes, swing_point, direction)
        except (TypeError, ValueError, IndexError):
            pass
    return {"delta_oi_pct": delta_oi_pct, "range_held": range_held}


def fetch_market_cap(ticker, cg_id=None):
    """Live circulating MC (USD) via pull5.coingecko_layer — the same canonical CoinGecko
    source unlocks.py sizes off (never a single-chain on-chain total_supply). None on any
    resolution/fetch failure (§3 degrade, never a fabricated MC)."""
    try:
        from pull5 import coingecko_layer
        cg = coingecko_layer(ticker, cg_id=cg_id)
        if not isinstance(cg, dict) or cg.get("_error"):
            return None
        mc = cg.get("market_cap")
        return float(mc) if mc else None
    except Exception:  # noqa: BLE001 — a dead MC source degrades, never crashes the caller
        return None


def main():
    ap = argparse.ArgumentParser(description="SPEC-122 oi_mc — OI/MC perp-casino flag")
    ap.add_argument("ticker")
    ap.add_argument("--oi-usd", type=float, default=None, help="cross-venue OI in USD")
    ap.add_argument("--mc-usd", type=float, default=None,
                    help="circulating market cap in USD (omit to fetch live via CoinGecko)")
    ap.add_argument("--direction", default=None, help="SHORT|LONG — gates the caveat")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    mc_usd = args.mc_usd if args.mc_usd is not None else fetch_market_cap(args.ticker)
    r = build_oi_mc(args.oi_usd, mc_usd, direction=args.direction)
    data = {"ticker": args.ticker.upper(), **r}
    if args.json:
        print(json.dumps({"ok": True, "data": data, "meta": {}}))
    else:
        print(f"# {args.ticker.upper()} OI/MC")
        print(f"  oi ${args.oi_usd:,.0f}" if args.oi_usd is not None else "  oi n/a")
        print(f"  mc ${mc_usd:,.0f}" if mc_usd is not None else "  mc n/a")
        print(f"  ratio {r['oi_mc_ratio']}  flag {r['oi_mc_flag']}")
        if r["oi_mc_caveat"]:
            print(f"  {r['oi_mc_caveat']}")


if __name__ == "__main__":
    main()
