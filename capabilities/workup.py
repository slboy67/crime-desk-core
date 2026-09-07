#!/usr/bin/env python3
"""workup.py — the full §0.6 five-question scan as ONE call (SPEC 60).

The Designer ran 8-10 manual calls per full scan this week (PLAY, ESPORTS, FOLKS). The
assembly is mechanical; one call returns the dossier and the Designer spends their context
on judgment (§0/§0.6 division). `workup` RE-DERIVES NOTHING — it composes the already-merged
capabilities concurrently into the §0.6 five-question shape:

  1. CHIPS           ← onchain  (concentration + tracked tiers + holder flags + unsupported-
                                 chain supply + apparatus balances for watch_balance wallets)
  2. STAGE           ← price_structure full-history (ath_alltime, prior_cycle, squeezes,
                                 range_pos) + 24h change
  3. OI CONSTRUCTION ← live_perp cross-venue funding (normalized %/4h, floor-sentinel) + OI +
                                 oi_sides wash/real + funding-divergence delta
  4. SIZE            ← depth (both venue books: shelves/walls/spoof) + cvd (spot venue/vol/
                                 verdict) + liquidity tier + OI/bracket caps
  5. R:R             ← liq_magnets HVN/LVN + setup_score (SPEC 59, all setups + missing legs)

Plus `flags`: squeeze-history chronic, venue mark divergence >2%, liquidity tier
(dust/scout/full), time-relevant alerts (inbox unconsumed for the ticker).

Every section is degrade-explicit (`available:false` + reason on failure, NEVER a crash that
loses the others) and SOURCED. There is **NO verdict field** — the dossier informs, the
Designer judges. Sections run concurrently → meta.ms ≈ the slow read, not the sum.

  python3 capabilities/workup.py '{"ticker":"ESPORTS"}' --json
"""
import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Composed capabilities — module globals so tests monkeypatch them (house style).
from onchain import build_onchain                  # CHIPS
from price_structure import build_structure        # STAGE
from regime_flip import live_perp                  # OI CONSTRUCTION + gate
from cvd import build_cvd                           # SIZE (spot)
from depth import build_depth                       # SIZE (books)
from liq_magnets import build_magnets               # R:R
from setup_score import score_all                   # R:R (SPEC 59)
from inbox import alerts_for                        # flags

REPO = HERE.parent
SECTION_BUDGET = 18         # per-section wall-clock budget; over it → degrade-explicit
VOL_AUTOPASS_M = 10.0       # §7 liquidity tiers (mirror size.py)
VOL_SCOUT_M = 25.0
MARK_DIVERGENCE_FLAG = 2.0  # venue mid divergence >2% = composite-mark wick risk (§7)
SQUEEZE_CHRONIC_N = 6       # >6 daily squeeze legs in the window = chronic-squeezer corroborator


def oi_sides_read(ticker):
    """Wash/real OI tag via the parts-bin oi_sides (registered under _oldrepo). SPEC-103:
    a WASH tag gains a `liq_corroborator` (liqs' oi_liq_consistency, gated to a rough OI
    delta since oi_sides doesn't expose one) — native addition, the delegated _oldrepo
    script itself is never touched. Best-effort: any failure just omits the field."""
    p = subprocess.run([sys.executable, str(REPO / "_oldrepo" / "scripts" / "oi_sides.py"),
                        ticker, "--json"], capture_output=True, text=True, timeout=SECTION_BUDGET)
    out = json.loads(p.stdout) if p.stdout.strip() else {}
    if out.get("verdict") == "WASH" and out.get("oi_change_pct") is not None:
        try:
            import liqs as LQ
            r = LQ.build_liqs(ticker, oi_delta_pct=out["oi_change_pct"])
            out["liq_corroborator"] = r.get("oi_liq_consistency")
        except Exception:  # noqa: BLE001 — never let the corroborator break the WASH read
            pass
    return out


def _apparatus_balances(ticker):
    """Balances for wallets the config marks watch_balance. Degrades to [] when no config /
    no flagged wallets (SPEC 60 — best-effort, never a section failure)."""
    try:
        cfg = json.loads((REPO / "config" / "tracked_wallets.json").read_text())
    except (OSError, ValueError):
        return []
    rows = cfg.get(ticker.upper()) or cfg.get("tokens", {}).get(ticker.upper()) or []
    if isinstance(rows, dict):
        rows = rows.get("wallets", [])
    return [{"address": w.get("address"), "label": w.get("label"), "watch_balance": True}
            for w in rows if isinstance(w, dict) and w.get("watch_balance")]


# ── the five sections (each degrade-explicit; never raises) ─────────────────────
def _chips(ticker):
    o = build_onchain(ticker)
    conc = o.get("concentration") or {}
    return {"available": True, "source": "onchain",
            "signal": o.get("signal"), "bias": o.get("bias"), "score": o.get("score"),
            "concentration": {"available": bool(conc.get("available")),
                              "top1_pct": conc.get("top1_pct"), "top10_pct": conc.get("top10_pct"),
                              "holder_count": conc.get("holder_count"), "reason": conc.get("reason")},
            "holder_flags": o.get("flags") or {},
            "coverage": o.get("coverage") or {},
            "newly_fired": (o.get("nonces") or {}).get("escalation_fired") or [],
            "apparatus_balances": _apparatus_balances(ticker)}


def _stage(ticker):
    s = build_structure(ticker)
    if s.get("error"):
        return {"available": False, "reason": s["error"], "source": "price_structure"}
    return {"available": True, "source": "price_structure (full-history, SPEC 57)",
            "current_close": s.get("current_close"),
            "ath_alltime": s.get("ath_alltime"), "ath_alltime_date": s.get("ath_alltime_date"),
            "off_ath_alltime_pct": s.get("off_ath_alltime_pct"), "prior_cycle": s.get("prior_cycle"),
            "window_high": s.get("window_high"), "window_low": s.get("window_low"),
            "range_pos": s.get("range_pos"), "off_ath_pct": s.get("off_ath_pct"),
            "squeezes": s.get("squeezes"), "squeeze_pattern": s.get("squeeze_pattern"),
            "structure": s.get("structure"),
            "_squeeze_count": len(s.get("squeezes") or [])}


def _oi_construction(ticker):
    live = live_perp(ticker) or {}
    venues = live.get("venues") or {}
    f4s = [v.get("funding_4h") for v in venues.values() if v.get("funding_4h") is not None]
    divergence = round(max(f4s) - min(f4s), 4) if len(f4s) >= 2 else None
    try:
        osr = oi_sides_read(ticker) or {}
        oi_tag = osr.get("verdict_tag") or osr.get("tag")
    except Exception as e:  # noqa: BLE001 — oi_sides degrade must not sink the section
        oi_tag = None
        oi_err = str(e)[:100]
    else:
        oi_err = None
    return {"available": True, "source": "regime_flip.live_perp + oi_sides",
            "funding_4h": live.get("funding_4h"), "funding_venue": live.get("venue"),
            "all_floor": live.get("all_floor"), "primary_venue": live.get("primary_venue"),
            "oi": live.get("oi"), "chg24": live.get("chg24"),
            "venues": venues, "funding_divergence_4h": divergence,
            "oi_sides_tag": oi_tag, "oi_sides_error": oi_err}


def _size(ticker):
    live = live_perp(ticker) or {}
    vol_m = live.get("vol_m")
    tier = _liquidity_tier(vol_m)
    oi_cap = None
    if live.get("oi") and live.get("price"):
        oi_cap = round(0.03 * float(live["oi"]) * float(live["price"]), 2)
    books = {}
    d = build_depth(ticker)
    for v, vd in (d.get("venues") or {}).items():
        if not vd.get("available"):
            books[v] = {"available": False, "reason": vd.get("reason")}
        else:
            books[v] = {"available": True, "mid": vd.get("mid"),
                        "bid_shelf_below": vd.get("bid_shelf_below"),
                        "ask_wall_above": vd.get("ask_wall_above"),
                        "truncated": vd.get("truncated")}
    cv = build_cvd(ticker)
    return {"available": True, "source": "live_perp gate + depth + cvd",
            "liquidity_tier": tier, "vol_m_24h": vol_m, "oi_cap_usd": oi_cap,
            "books": books,
            "spot_venue": cv.get("spot_venue"), "spot_vol_24h_usd": cv.get("spot_vol_24h_usd"),
            "spot_coverage": cv.get("spot_coverage"), "cvd_verdict": cv.get("verdict"),
            "cvd_reliable": cv.get("reliable")}


def _rr(ticker):
    """R:R map — liq_magnets only here (the setup_score is computed post-assembly from the
    sibling sections, so we reuse their reads instead of re-fetching the engine)."""
    m = build_magnets(ticker)
    return {"available": True, "source": "liq_magnets + setup_score (SPEC 59)",
            "magnets": {"hvns": m.get("hvns"), "lvns": m.get("lvns"),
                        "round_number_magnets": m.get("round_number_magnets")}}


def _derive_signals(out):
    """Map the assembled section fields onto the SPEC-59 signal keys — so setup_score reuses
    the reads workup already made (no extra network, offline-deterministic). Absent fields
    simply leave their legs not-passed."""
    stage = out.get("stage") or {}
    oc = out.get("oi_construction") or {}
    size = out.get("size") or {}
    chips = out.get("chips") or {}
    st = stage.get("structure") or {}
    f4 = oc.get("funding_4h")
    off_ath = stage.get("off_ath_pct")
    return {
        "lower_high": (st.get("read") == "downtrend") or (st.get("lower_highs", 0) >= 4),
        "ath_wick": (off_ath is not None and off_ath >= 0),
        "window_high_wick_pct": (max(0.0, off_ath) if (off_ath or 0) >= 0 else 0.0),
        "parabolic_pct": oc.get("chg24"),
        "oi_sides_tag": oc.get("oi_sides_tag"),
        "multi_sigma_neg": (f4 is not None and f4 <= -0.30 and not oc.get("all_floor")),
        "spot_cvd_up": (size.get("cvd_verdict") == "BULLISH_DIVERGENCE" and bool(size.get("cvd_reliable"))),
        "cex_deposits_firing": bool(chips.get("newly_fired")),
    }


# ── flags (computed from already-fetched sections — no extra reads) ─────────────
def _liquidity_tier(vol_m):
    if vol_m is None:
        return "UNKNOWN"
    if vol_m < VOL_AUTOPASS_M:
        return "DUST"            # AUTO-PASS territory (§7)
    if vol_m < VOL_SCOUT_M:
        return "SCOUT"
    return "FULL"


def _flags(out, ticker):
    flags = {}
    # squeeze-history chronic (corroborator; full-history count from STAGE)
    stage = out.get("stage") or {}
    sq_n = stage.get("_squeeze_count")
    flags["squeeze_chronic"] = bool(sq_n is not None and sq_n > SQUEEZE_CHRONIC_N)
    flags["squeeze_legs_in_window"] = sq_n
    # liquidity tier (from SIZE)
    flags["liquidity_tier"] = (out.get("size") or {}).get("liquidity_tier", "UNKNOWN")
    # venue mark divergence (>2% = composite-mark wick risk, §7) — from SIZE book mids
    mids = [b.get("mid") for b in ((out.get("size") or {}).get("books") or {}).values()
            if isinstance(b, dict) and b.get("mid")]
    if len(mids) >= 2:
        div = (max(mids) - min(mids)) / min(mids) * 100
        flags["venue_mark_divergence_pct"] = round(div, 3)
        flags["venue_mark_divergence_flag"] = div > MARK_DIVERGENCE_FLAG
    else:
        flags["venue_mark_divergence_pct"] = None
        flags["venue_mark_divergence_flag"] = False
    # time-relevant alerts (inbox unconsumed for the ticker)
    try:
        flags["alerts"] = alerts_for(ticker)
    except Exception as e:  # noqa: BLE001
        flags["alerts"] = {"n": 0, "max_severity": None, "events": [], "reason": str(e)[:100]}
    return flags


# ── top-level assembly ──────────────────────────────────────────────────────────
def build_workup(ticker):
    """Compose the §0.6 five-question dossier. Sections run concurrently under a per-section
    budget; each is degrade-explicit. NO verdict field — the Designer judges."""
    ticker = ticker.upper().replace("USDT", "")
    t0 = time.time()
    jobs = {"chips": _chips, "stage": _stage, "oi_construction": _oi_construction,
            "size": _size, "rr": _rr}
    out = {"ticker": ticker}
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {name: ex.submit(fn, ticker) for name, fn in jobs.items()}
        deadline = time.time() + SECTION_BUDGET
        for name, f in futs.items():
            remaining = max(0.0, deadline - time.time())
            try:
                out[name] = f.result(timeout=remaining)
            except FuturesTimeout:
                out[name] = {"available": False,
                             "reason": f"{name} section exceeded {SECTION_BUDGET}s budget"}
            except Exception as e:  # noqa: BLE001 — one dead read never aborts the dossier
                out[name] = {"available": False, "reason": str(e)[:160]}
    # setup_score (SPEC 59) over signals DERIVED from the sibling sections — no re-fetch.
    if isinstance(out.get("rr"), dict) and out["rr"].get("available"):
        scores = score_all(_derive_signals(out))
        out["rr"]["setup_score"] = scores
        out["rr"]["missing_legs"] = {name: r.get("missing") for name, r in scores.items()}
    out["flags"] = _flags(out, ticker)
    # drop the internal helper key now that flags consumed it
    if isinstance(out.get("stage"), dict):
        out["stage"].pop("_squeeze_count", None)
    out["meta"] = {"ms": int((time.time() - t0) * 1000), "sections": list(jobs.keys())}
    return out


def render_human(d):
    print(f"# {d['ticker']} — workup (§0.6 five-question dossier)\n")
    for name in ("chips", "stage", "oi_construction", "size", "rr"):
        sec = d.get(name) or {}
        head = name.upper().replace("_", " ")
        if not sec.get("available"):
            print(f"  {head}: unavailable ({sec.get('reason')})")
            continue
        if name == "chips":
            c = sec.get("concentration", {})
            print(f"  CHIPS:   {sec.get('signal')}  top1 {c.get('top1_pct')}%  top10 {c.get('top10_pct')}%  "
                  f"unreadable {sec.get('coverage', {}).get('unreadable_supply_pct')}%")
        elif name == "stage":
            print(f"  STAGE:   range {sec.get('range_pos')}%  off-ATH(all) {sec.get('off_ath_alltime_pct')}%  "
                  f"squeeze {sec.get('squeeze_pattern')}  prior_cycle={sec.get('prior_cycle')}")
        elif name == "oi_construction":
            print(f"  OI:      fund {sec.get('funding_4h')}%/4h  oi_sides {sec.get('oi_sides_tag')}  "
                  f"divergence {sec.get('funding_divergence_4h')}")
        elif name == "size":
            print(f"  SIZE:    tier {sec.get('liquidity_tier')}  vol ${sec.get('vol_m_24h')}M  "
                  f"oi_cap ${sec.get('oi_cap_usd')}  cvd {sec.get('cvd_verdict')}")
        elif name == "rr":
            armed = [s for s, r in sec.get("setup_score", {}).items() if r.get("verdict") == "ARMED"]
            print(f"  R:R:     armed {armed or 'none'}  magnets {sec.get('magnets', {}).get('round_number_magnets')}")
    f = d.get("flags", {})
    print(f"\n  FLAGS:   liquidity {f.get('liquidity_tier')}  squeeze_chronic={f.get('squeeze_chronic')}  "
          f"mark_div {f.get('venue_mark_divergence_pct')}%  alerts {f.get('alerts', {}).get('n')}")
    print(f"  ({d['meta']['ms']}ms — dossier only, no verdict; the Designer judges)\n")


def main():
    ap = argparse.ArgumentParser(description="§0.6 five-question dossier in one call (SPEC 60)")
    ap.add_argument("payload", help='JSON: {"ticker":X}')
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        req = json.loads(args.payload)
    except ValueError as e:
        print(json.dumps({"error": f"unparseable payload: {e}"})); sys.exit(2)
    ticker = req.get("ticker")
    if not ticker:
        print(json.dumps({"error": "ticker is required"})); sys.exit(2)
    d = build_workup(ticker)
    if args.json:
        print(json.dumps(d))
    else:
        render_human(d)


if __name__ == "__main__":
    main()
