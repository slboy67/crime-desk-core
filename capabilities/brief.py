#!/usr/bin/env python3
"""brief.py — one-call full-stack token read (SPEC 30).

A bare ticker drop should return the WHOLE picture in one call. Before this, the
Designer hand-assembled `classify` + `analyse` + `depth bitget` + `depth binance` +
`onchain` every time — and skipped pieces (the SKYAI single-venue blind spot: stopping
at a Binance-funding CONFIRMS and missing the Bitget exit book, memory:
feedback_skyai_exit_liquidity_is_bitget). `brief` makes completeness MECHANICAL.

It RE-DERIVES NOTHING — it composes the already-MERGED capabilities concurrently:

  state    ← classify   (verdict vs the committed thesis + the thesis levels)
  perp     ← analyse    (verdict/direction/funding/oi/cvd/price)
  books    ← depth      (Aster execution-venue book FIRST, then Bitget + Binance cross-check — SPEC-85)
  onchain  ← onchain    (nonce signal + SPEC-24 gated fires + SPEC-28 staging/execution +
                         SPEC-72 fired-safe → operator-aggregator dex_swap_sell resolution + concentration)

Every layer is degrade-explicit: a failed/slow layer returns {available:false, reason},
NEVER a silent null and never a crash that loses the other (good) layers. Each layer is
budgeted (SPEC-1b 90s); the layers run concurrently so latency ≈ the slow layer (analyse),
not the sum.

It RESPECTS the state machine (§0.5): brief is a READ, not a commit. It surfaces all the
data but `state.verdict` stays authoritative — a CONFIRMS does not become a reframe just
because the brief shows fresh data.

  python3 capabilities/brief.py SKYAI --json
  python3 capabilities/brief.py SKYAI --venue bitget --json
"""
import argparse
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Composed capabilities — imported as module globals so they stay monkeypatchable in tests.
from classify import load_watchlist, classify_token, live_perp   # state layer
from analyse import build_analyse                                # perp layer
from depth import build_depth                                    # books layer
from onchain import build_onchain                                # on-chain layer
import onboard                                                   # SPEC 51: mapping invariant
import regime_flip as RF                                         # SPEC-71: per-venue surface helpers
import setup_score as SS                                         # SPEC-83: defended_fade wall-dynamics
import hyperliquid as HL                                         # SPEC-84: read-only HL venue surface
import oi_mc as OM                                                # SPEC-122: OI/MC "perp-casino" flag
import aster_listing as AL                                        # SPEC-136: execution-venue fillability veto
from aster_listing import fetch_aster_symbols                     # bare name — monkeypatchable in tests
import thesis as TH                                               # SPEC-146: the ONE typed geometry parser
from perpfinder import build_perpfinder                           # SPEC-159: venue-breadth layer
import risk_card as RC                                             # SPEC-169: the risk-card line
import venue_bars as VB                                            # SPEC-188: all-venue OHLC sweep
from venue_bars import build_venue_bars, level_agreement           # bare names — monkeypatchable in tests
import oi_construction as OC                                        # SPEC-193: reuse the compact oic: line

LAYER_BUDGET = 90          # SPEC-1b: per-layer wall-clock budget; over it → degrade-explicit
_THESIS_RAW_KEYS = ("triggers", "invalidation")   # fields thesis.Thesis doesn't model
VENUE_BREADTH_BUDGET = 10  # SPEC-159 req 4: best-effort, fetched last, never slows wired reads
# SPEC-172: the post-main-loop surfaces (defended_fade/unlocks/venue_breadth/risk_card) are
# mostly offline/local reads — risk_card is the one real network leg (venue equity + a
# maxsize book walk) — so they get a tighter shared budget than the primary LAYER_BUDGET.
POST_LAYER_BUDGET = 20
WIRED_VENUES = {"bybit", "binance", "bitget", "aster", "hyperliquid"}   # SPEC-159 problem statement
VENUE_BARS_BUDGET = 20     # SPEC-188: 14-venue sweep, observed <=3s live but network-bound


def _bounded(name, fn, args, budget):
    """Run fn(*args) under a hard wall-clock `budget`, in an ABANDONED background thread
    on timeout — never blocks the caller past `budget` even if fn is still running.

    SPEC-172: the pre-existing `with ThreadPoolExecutor(...) as ex:` pattern looks bounded
    (each future is read via `.result(timeout=...)`), but `with`'s `__exit__` calls
    `ex.shutdown(wait=True)` — which blocks until EVERY submitted thread finishes,
    including ones already reported as timed-out inside the loop. A single hung network
    call (e.g. a stalled Aster signed request) silently re-imposed its own wall-clock on
    the entire brief regardless of the budget passed to `.result()` — the actual cause of
    the 170s+ `brief` timeouts on GALA/SKR. Not using `with` + `shutdown(wait=False)` here
    lets the abandoned thread finish (or not) on its own time, unread.

    Returns (result, ms_elapsed, timed_out)."""
    t0 = time.time()
    ex = ThreadPoolExecutor(max_workers=1)
    timed_out = False
    try:
        result = ex.submit(fn, *args).result(timeout=budget)
    except FuturesTimeout:
        timed_out = True
        result = {"available": False, "reason": f"{name} exceeded {budget}s budget"}
    except Exception as e:  # noqa: BLE001
        result = {"available": False, "reason": f"{name} error: {str(e)[:140]}"}
    finally:
        ex.shutdown(wait=False)
    return result, int((time.time() - t0) * 1000), timed_out


# ── SPEC-71: ONE shared live-funding resolution per brief ──────────────────────
# The BILL incident: the state leg (classify → regime_flip.live_perp, floor-aware) and the
# perp leg (analyse → perp_analyser's Bybit print) used DIFFERENT resolvers and asserted two
# different live funding verdicts in one payload — the headline showed a venue's ±0.005 floor
# placeholder while the real, trigger-qualifying Binance rate sat in the state leg. Both legs
# now read the SAME live_perp resolution (resolved once, passed as a future).

def _resolve_live(ticker):
    """The shared resolution — regime_flip.live_perp (SPEC 13/44: cross-venue, floor-demoting,
    more-vetoing-real-venue-wins). Never raises; None = no resolution (legs degrade)."""
    try:
        return live_perp(ticker)
    except Exception:  # noqa: BLE001
        return None


def _live_of(live_fut):
    """Resolve the shared future inside a layer thread; tolerate absence/failure."""
    if live_fut is None:
        return None
    try:
        return live_fut.result(timeout=LAYER_BUDGET)
    except Exception:  # noqa: BLE001
        return None


def _surface_bitget(ticker):
    """SPEC-71 req 2: bitget surfaced in funding_by_venue even when the resolver never
    consulted it (live_perp pulls secondaries LAZILY). Display-only — never feeds the
    venue selection. None on any failure (the surface is best-effort)."""
    try:
        v = RF._venue_bitget(f"{ticker}USDT")
        if not v:
            return None
        pi = round(v["funding_raw"] * 100, 6)
        return {"funding_4h": RF.to_4h(pi, v["interval_min"]), "funding_pi": pi,
                "interval_min": v["interval_min"], "is_floor": RF._is_floor(v["funding_raw"]),
                "surfaced_only": True}
    except Exception:  # noqa: BLE001
        return None


def _surface_hyperliquid(ticker):
    """SPEC-84: surface Hyperliquid in funding_by_venue when it lists the name (the 2/26 overlap,
    e.g. TNSR/CHIP). Display-only cross-check — HL never feeds the venue selection or any verdict.
    None when HL doesn't list the name OR on any fetch failure → the surface omits HL entirely
    (graceful absence; the other venues stay byte-identical, the brief never blocks)."""
    try:
        v = HL.resolve(ticker)
        if not v:
            return None
        return {"funding_4h": v.get("funding_4h"), "funding_pi": v.get("funding_pi"),
                "interval_min": v.get("interval_min"), "is_floor": bool(v.get("is_floor")),
                "oi": v.get("oi"), "vol_m": v.get("vol_m"),
                "mark": v.get("mark"), "oracle": v.get("oracle"), "premium": v.get("premium"),
                "surfaced_only": True}
    except Exception:  # noqa: BLE001
        return None


def _thesis_display(tok):
    """SPEC-146: the committed-thesis display, sourced from the ONE typed parser rather
    than an ad-hoc key-projection (formerly `{k: th.get(k) for k in _THESIS_KEYS}` —
    which read a nonexistent `th["time_stop"]` key instead of the real `time_stop_h`,
    always None, and never surfaced watch_level at all). None when no thesis is present."""
    th = tok.get("thesis") or {}
    if not th:
        return None
    p = TH.parse(tok)
    out = {
        "direction": p.direction,
        "entry_zone": list(p.zone) if p.zone else None,
        "stop": p.stop,
        "tp": p.tps,
        "time_stop": p.time_stop_h,
        "watch_level": p.watch_levels,
        "signature": p.signature,
    }
    for k in _THESIS_RAW_KEYS:
        out[k] = th.get(k)
    return out


# ── layers (each returns a degrade-explicit dict; never raises) ────────────────
def _state_layer(ticker, live_fut=None):
    """classify verdict vs the committed thesis, plus the thesis levels so the read is
    anchored to the commitment. A name with no thesis → thesis_present:false (discovery)."""
    tokens, err = load_watchlist()
    if err:
        return {"available": False, "reason": f"watchlist: {err}"}
    # SPEC-99: §7 operator-heat from the auto-built operator graph — surfaced regardless of
    # whether this ticker itself has a thesis (a discovery-mode read still wants to know it
    # shares an MM with a live watchlist name).
    try:
        import classify as CL
        cluster_heat = CL.cluster_heat_for(ticker, tokens=tokens)
    except Exception:  # noqa: BLE001
        cluster_heat = None
    tok = next((t for t in tokens if t.get("ticker", "").upper() == ticker), None)
    # SPEC-136: fillability gate — distinct from the cross-venue signal layer above.
    # Never narrow the SIGNAL to Aster (§0.6.3b); this only answers "can the user fill."
    try:
        override = (tok or {}).get("aster_listed")
        aster_listed = bool(override) if override is not None else \
            AL.aster_listed(ticker, fetch_aster_symbols())
    except Exception:  # noqa: BLE001 — a dead venue probe degrades to unknown, never False
        aster_listed = None
    if tok is None:
        return {"available": True, "verdict": None, "reason": "not on watchlist",
                "thesis_present": False, "direction": None, "thesis": None,
                "cluster_heat": cluster_heat, "aster_listed": aster_listed}
    live = _live_of(live_fut)   # SPEC-71: the SHARED resolution (same one the perp leg reads)
    r = classify_token(tok, live)
    # SPEC 45: ride the full unconsumed events along (classify rows carry the summary;
    # the single-ticker brief has room for the event bodies)
    try:
        import inbox
        alerts = inbox.alerts_for(ticker)
    except Exception:  # noqa: BLE001
        alerts = {"n": 0, "max_severity": None, "events": []}
    reason = r.get("reason")
    if aster_listed is False and reason is not None:
        reason = reason + " | SIGNAL-ONLY (no Aster market)"
    return {
        "available": True,
        "verdict": r.get("verdict"),
        "reason": reason,
        "thesis_present": bool(r.get("thesis_present")),
        "direction": r.get("direction"),
        "thesis": _thesis_display(tok),
        "alerts": alerts,
        "cluster_heat": cluster_heat,
        "aster_listed": aster_listed,
    }


def _perp_layer(ticker, live_fut=None):
    a = build_analyse(ticker)
    out = {
        "available": True,
        "verdict": a.get("verdict"),
        "direction": a.get("direction"),
        "tier": a.get("tier"),
        "funding_4h": a.get("funding_4h"),
        "funding_unavailable": bool(a.get("funding_unavailable")),
        "funding_venue": a.get("funding_venue"),
        "floor_suspect": bool(a.get("floor_suspect")),
        "funding_by_venue": {},
        "oi_chg_pct": a.get("oi_chg_pct"),
        "oi_chg_pct_48h": a.get("oi_chg_pct_48h"),   # SPEC-174 #6: the window, labelled
        "near_ath": bool(a.get("near_ath")),
        "cvd_verdict": a.get("cvd_verdict"),
        "cvd_detail": a.get("cvd_detail"),   # SPEC-191 #1: {window_min, spot_notional_usd, venues_used, spot_cvd, perp_cvd}
        "price": a.get("price"),
        "onchain_status": a.get("onchain"),
        "nonce_signal": a.get("nonce_signal"),
    }
    # SPEC-71: the funding verdict mirrors the SHARED resolution (the same one the state
    # leg classifies against) — one payload can never assert two different live rates, and
    # a floor placeholder never headlines while a real print exists (live_perp guarantees).
    live = _live_of(live_fut)
    if live and live.get("funding_4h") is not None:
        out["funding_4h"] = live["funding_4h"]
        out["funding_venue"] = live.get("venue") or a.get("funding_venue")
        out["funding_unavailable"] = False
        out["floor_suspect"] = bool(live.get("funding_suspect"))   # lone floor = data failure (§3)
    vens = (live or {}).get("venues")
    if isinstance(vens, dict) and vens:
        fbv = {k: {"funding_4h": v.get("funding_4h"), "funding_pi": v.get("funding_pi"),
                   "interval_min": v.get("interval_min"), "is_floor": bool(v.get("is_floor"))}
               for k, v in vens.items()}
        if "bitget" not in fbv:
            bg = _surface_bitget(ticker)
            if bg:
                fbv["bitget"] = bg
        if "hyperliquid" not in fbv:                    # SPEC-84: HL surfaced for overlap names; omit otherwise
            hl = _surface_hyperliquid(ticker)
            if hl:
                fbv["hyperliquid"] = hl
        out["funding_by_venue"] = fbv
    # SPEC-174 #3: vol24h_usd (the size-venue 24h turnover already resolved onto `live` —
    # regime_flip.live_perp's primary-venue vol_m, no second fetch) + mc_usd (CoinGecko,
    # cached via pull5.coingecko_layer) — an unmapped/off-board name previously had neither
    # surfaced (the scout had to derive them by hand from price_structure.vol_daily_m +
    # CoinGecko). mc_usd degrades to null + an explicit reason, never silently absent.
    out["vol24h_usd"] = (round(float(live["vol_m"]) * 1e6, 2)
                         if (live and live.get("vol_m") is not None) else None)
    mc_usd = mc_unavailable_reason = None
    try:
        mc_usd = OM.fetch_market_cap(ticker)
        if mc_usd is None:
            mc_unavailable_reason = "coingecko lookup unavailable (unmapped ticker or fetch failure)"
    except Exception:  # noqa: BLE001 — a dead MC source degrades, never breaks the perp layer
        mc_unavailable_reason = "coingecko lookup unavailable (unmapped ticker or fetch failure)"
    out["mc_usd"] = mc_usd
    out["mc_unavailable_reason"] = mc_unavailable_reason

    # SPEC-122: OI/MC "perp-casino" flag — reuses the primary-venue OI already resolved onto
    # `live` (the same cross-venue read the funding/squeeze signals aggregate, no second OI
    # call) + the mc_usd just fetched above. Any failure degrades to nulls, never a
    # fabricated flag.
    oi_mc_ratio = oi_mc_flag = oi_mc_caveat = None
    if live and live.get("oi") is not None and live.get("price") is not None:
        try:
            om = OM.build_oi_mc(float(live["oi"]) * float(live["price"]), mc_usd,
                                direction=(out.get("direction") or "").upper() or None)
            oi_mc_ratio, oi_mc_flag, oi_mc_caveat = (om["oi_mc_ratio"], om["oi_mc_flag"],
                                                     om["oi_mc_caveat"])
        except Exception:  # noqa: BLE001 — never let this flag break the perp layer
            pass
    # SPEC-177: battlefield verdict — both legs already fetched by this point (perp:
    # `live["vol_m"]`, the same primary-venue 24h volume the funding/squeeze signals
    # use; spot: `a["spot_vol_24h_usd"]` off build_analyse's own cvd.resolve_spot_venue
    # call, req 5) — zero new fetches for the ratio. leverage_state stays UNKNOWN here:
    # its ΔOI/range-hold series needs an OI-history store this call site doesn't have
    # (SPEC-178's sampler is the intended future source; `OM.fetch_leverage_window`
    # exists as tested infra for whichever call site wires it in first).
    perp_vol_24h = float(live["vol_m"]) * 1e6 if (live and live.get("vol_m") is not None) else None
    spot_vol_24h = a.get("spot_vol_24h_usd")
    ratio, verdict = OM.battlefield_verdict(perp_vol_24h, spot_vol_24h, oi_mc_flag)
    out["perp_spot_ratio"] = round(ratio, 4) if ratio is not None else None
    out["battlefield"] = verdict
    out["leverage_state"] = {
        "leverage_4h": OM.leverage_state_for_window(None, None, None, 8.0, "4h"),
        "leverage_48h": OM.leverage_state_for_window(None, None, None, 15.0, "48h"),
    }

    out["oi_mc_ratio"] = oi_mc_ratio
    out["oi_mc_flag"] = oi_mc_flag
    out["oi_mc_caveat"] = oi_mc_caveat
    # SPEC-180 req 5: brief renders the FULL oi_construction block — zero extra
    # fetch, `a` (build_analyse's output) already computed it (SPEC-180 req 1's
    # bounded, best-effort sweep).
    out["oi_construction"] = a.get("oi_construction")
    return out


def _books_layer(ticker, venue=None):
    """depth for EACH venue (omit venue = Aster FIRST, then Bitget + Binance — the whole point)."""
    d = build_depth(ticker, venue)
    books = {}
    for vname, v in (d.get("venues") or {}).items():
        if not v.get("available"):
            books[vname] = {"available": False, "reason": v.get("reason")}
        else:
            books[vname] = {
                "available": True,
                "mid": v.get("mid"),
                "bid_shelf_below": v.get("bid_shelf_below"),
                "ask_wall_above": v.get("ask_wall_above"),
                "truncated": v.get("truncated"),
                "deepest_level_seen": v.get("deepest_level_seen"),
            }
    return {"available": True, "venues": books}


def _defended_fade_layer(ticker, books, perp):
    """SPEC-83 — surface the wall-fade tell the desk kept eyeballing: record the top-of-book
    defended wall and report whether it's GROWING (the operator-capping-while-distributing
    signature) across recent briefs, plus the fade geometry. Best-effort over the books already
    fetched (no extra venue call) — `wall_growing` needs ≥2 briefs to light up. Read-only; the
    full ARM still wants the structure/empty-beyond legs (run setup_score for the verdict)."""
    if not books or not books.get("available"):
        return {"available": False, "reason": "no book"}
    venues = books.get("venues") or {}
    v = next((venues.get(n) for n in ("bitget", "binance")
              if (venues.get(n) or {}).get("available")), None)
    if not v:
        return {"available": False, "reason": "no available venue book"}
    mid = v.get("mid")
    aw = v.get("ask_wall_above") or {}
    bs = v.get("bid_shelf_below") or {}
    truncated = bool(v.get("truncated"))
    out = {"available": True, "mid": mid, "truncated": truncated}
    f4 = (perp or {}).get("funding_4h")
    ts = time.time()
    if aw.get("price") and aw.get("notional_usd") is not None and mid:
        hist = SS.record_wall_snapshot(ticker, "ask", mid, aw["notional_usd"], ts)
        growing = SS.derive_wall_growing(hist)
        sig = {"wall_growing": growing, "empty_beyond": not truncated,
               "stall_lower_high": False, "wall_price": aw["price"], "funding_4h": f4,
               "spoof_prone": bool(aw.get("spoof_prone"))}
        sc = SS.score_setup("defended_fade_short", sig)
        out["ask"] = {"price": aw["price"], "notional_usd": aw["notional_usd"],
                      "wall_growing": growing, "snapshots": len(hist),
                      "spoof_prone": bool(aw.get("spoof_prone")),
                      "verdict": sc["verdict"], "add_triggers": sc["add_triggers"],
                      "geometry": sc.get("geometry"),
                      "note": ("wall thickening into the ceiling = operator capping while "
                               "distributing — the fade entry (SPEC-83)" if growing else
                               "wall not (yet) growing across briefs; need ≥2 briefs at the level")}
    if bs.get("price") and bs.get("notional_usd") is not None and mid:
        histb = SS.record_wall_snapshot(ticker, "bid", mid, bs["notional_usd"], ts)
        out["bid"] = {"price": bs["price"], "notional_usd": bs["notional_usd"],
                      "wall_growing": SS.derive_wall_growing(histb), "snapshots": len(histb)}
    return out


def _liqs_layer(ticker):
    """SPEC-103: cross-check the window's OI move against liquidation ground truth (liq
    prints can't be faked the way aggregate OI can — memory:
    feedback_aggregate_oi_faked_via_double_open). Feeds analyse()'s already-computed
    oi_chg_pct in as the OI-delta input (no second OI fetch). Bounded by the same
    LAYER_BUDGET every other brief layer respects — degrades to unavailable on timeout,
    never adds latency beyond the existing per-layer budget."""
    try:
        import liqs as LQ
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"liqs module unavailable: {str(e)[:80]}"}
    try:
        a = build_analyse(ticker)
    except Exception:  # noqa: BLE001 — a failed analyse() read just means no OI-delta input
        a = {}
    try:
        return LQ.build_liqs(ticker, oi_delta_pct=a.get("oi_chg_pct"))
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"liqs error: {str(e)[:120]}"}


def _phase_layer(ticker):
    """SPEC-104: the Wyckoff cycle read (memory: feedback_wyckoff_cycle_first_breakout_gate
    — classify the cycle BEFORE trusting any breakout/breakdown candle). Bounded by the same
    LAYER_BUDGET every other brief layer respects; thin/short history degrades to
    unavailable without blocking (never guesses a cycle off too little data)."""
    try:
        import phase as PH
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"phase module unavailable: {str(e)[:80]}"}
    try:
        return PH.build_phase(ticker)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"phase error: {str(e)[:120]}"}


def _unlocks_layer(ticker, now=None):
    """SPEC-95: surface an upcoming unlock (≤7d) as a flag in the brief. Offline read over the
    sweep-populated config/catalysts.json (no network in the brief path) — degrade-explicit."""
    try:
        import unlocks as U
        cats = U._load_catalysts()
        flag = U.unlock_flag_for(ticker, now, U.HORIZON_DAYS, cats)
        nxt = U.next_catalyst_for(ticker, now, cats)
        return {"available": True, "flag": flag, "within_horizon": bool(flag),
                "next_unlock": nxt}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"unlocks error: {str(e)[:120]}"}


def _onchain_layer(ticker):
    o = build_onchain(ticker)
    nonces = o.get("nonces") or {}
    # SPEC 24: the actionable fires are the token-out-recency-GATED confirmed ones.
    gated_fired = nonces.get("escalation_fired") or []
    # SPEC 28: a CEX/DEX-routed escalation = EXECUTION (the dump firing); an internal
    # operator sink = STAGING (apparatus loading). Derived from the gated fires' dest_kind.
    kinds = [f.get("dest_kind") for f in gated_fired]
    if any(k in ("cex-execution", "dex-execution") for k in kinds):
        sve = "execution"
    elif any(k == "staging-internal" for k in kinds):
        sve = "staging"
    else:
        sve = None
    conc = o.get("concentration") or {}
    return {
        "available": True,
        "signal": o.get("signal"),
        "bias": o.get("bias"),
        "score": o.get("score"),
        "newly_fired": gated_fired,
        "staging_vs_execution": sve,
        "distribution": o.get("distribution"),   # SPEC-72: resolved fired-safe → aggregator flow
        "recent_distribution": o.get("recent_distribution"),   # SPEC-73: 24h distribution-memory surface
        "top_holder_distribution": o.get("top_holder_distribution"),   # SPEC-89: top-holder verify sweep
        "distribution_checked": o.get("distribution_checked"),         # SPEC-89: QUIET verified vs nonce-blind
        "signal_caveat": o.get("signal_caveat"),                       # SPEC-89: top-holder distribution caveat
        "distribution_freshness": o.get("distribution_freshness"),     # SPEC-98: FRESH/FROZEN/ROTATED
        "concentration": {
            "available": bool(conc.get("available")),
            "top1_pct": conc.get("top1_pct"),
            "top10_pct": conc.get("top10_pct"),
            "holder_count": conc.get("holder_count"),
            "reason": conc.get("reason"),
        },
    }


def _venue_breadth_http(url, timeout=8):
    """Bare keyless GET -> parsed JSON, or None on any failure — the direct-OI fetchers
    below are all best-effort (SPEC-191 #2), never raise past this."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "crime-desk-venue-breadth/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def _direct_oi_binance_like(base_url, sym):
    """Binance/Aster `/fapi/v1/openInterest` — BASE-quantity OI (live-verified: Binance
    BTCUSDT 113,288 BTC). The caller converts to USD with the row's own mark price."""
    d = _venue_breadth_http(f"{base_url}/fapi/v1/openInterest?symbol={sym}USDT")
    try:
        return {"qty": float(d["openInterest"]), "usd": None}
    except (KeyError, TypeError, ValueError):
        return None


def _direct_oi_bingx(sym):
    """BingX `openInterest` is ALREADY USD notional — live-verified 2026-09 by
    cross-checking against Binance's OI ratio (BingX BTC-USDT printed 1.458B; read as a
    BTC quantity that's 13x total supply, impossible — it's dollars)."""
    d = _venue_breadth_http(f"https://open-api.bingx.com/openApi/swap/v2/quote/openInterest?symbol={sym}-USDT")
    try:
        return {"qty": None, "usd": float(((d or {}).get("data") or {})["openInterest"])}
    except (KeyError, TypeError, ValueError):
        return None


def _direct_oi_htx(sym):
    """HTX linear-swap-api's own `value` field is already USD notional (its `amount`
    field is the base-qty equivalent, live-verified: BTC-USDT amount=29,359.114,
    value=$2.38B) — no mark multiplication needed."""
    d = _venue_breadth_http(f"https://api.hbdm.com/linear-swap-api/v1/swap_open_interest"
                            f"?contract_code={sym}-USDT")
    try:
        row = (d or {}).get("data")[0]
        return {"qty": None, "usd": float(row["value"])}
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def _direct_oi_mexc(sym):
    """MEXC's OI is contract-denominated (`holdVol` contracts x `contractSize` = base
    qty, live-verified BTC_USDT contractSize=0.0001) — two keyless calls, then the
    caller converts to USD with the row's own mark."""
    detail = _venue_breadth_http(f"https://contract.mexc.com/api/v1/contract/detail?symbol={sym}_USDT")
    ticker = _venue_breadth_http(f"https://contract.mexc.com/api/v1/contract/ticker?symbol={sym}_USDT")
    try:
        size = float((detail or {})["data"]["contractSize"])
        hold = float((ticker or {})["data"]["holdVol"])
        return {"qty": hold * size, "usd": None}
    except (KeyError, TypeError, ValueError):
        return None


# SPEC-191 #2: keyless direct-OI endpoints for venues perpfinder's aggregator doesn't
# carry OI for (its own `fieldSupport` marks these unsupported/temporarily_unavailable —
# SPEC-159 problem statement). Keyed lowercase; dispatch is case-insensitive on the
# perpfinder venue name.
DIRECT_OI_FETCHERS = {
    "binance": lambda sym: _direct_oi_binance_like("https://fapi.binance.com", sym),
    "aster": lambda sym: _direct_oi_binance_like("https://fapi.asterdex.com", sym),
    "bingx": _direct_oi_bingx,
    "htx": _direct_oi_htx,
    "mexc": _direct_oi_mexc,
}


def _resolve_direct_oi(venue, sym, mark_price):
    """USD OI for one venue via its own keyless endpoint, or None (unmapped venue, dead
    endpoint, or a base-qty venue with no mark to convert with — never fabricated).
    Unit discipline (SPEC-174 #4): a base-qty read is multiplied by `mark_price` here
    exactly once; a venue that already reports USD (BingX/HTX) passes through untouched
    — never both, never re-multiplied by a second venue's price."""
    fn = DIRECT_OI_FETCHERS.get((venue or "").lower())
    if fn is None:
        return None
    try:
        r = fn(sym)
    except Exception:  # noqa: BLE001
        return None
    if not r:
        return None
    if r.get("usd") is not None:
        return r["usd"]
    if r.get("qty") is not None and isinstance(mark_price, (int, float)):
        return r["qty"] * mark_price
    return None


def _venue_breadth_layer(ticker):
    """SPEC-159: PerpFinder's funding-rates matrix (SPEC-151) as the wide-and-shallow
    complement to venue_map's deep 9-venue read (SPEC-129) — kills the "brief only sees
    0-2 books" blind spot (CASHCAT 2026-08-25: brief saw Bybit+HL only while the matrix
    showed 9 venues, Hyperliquid the real size book at $30.4M OI vs Bybit's $14.1M).

    Doctrine guard (SPEC-151 unchanged): PerpFinder's rate is 1h-normalized — it can
    NEVER feed a verdict, §5 veto, floor detection, or funding_leg. It is display-only
    here, labeled `normalized_rates: true`.

    SPEC-187: the old `rate1h` key held perpfinder's `funding_pi_4h` value — a
    misleading name (it read like a raw hourly print) that masked a real units bug
    upstream (perpfinder.py was missing a fraction->percent step, 100x too small; fixed
    at the source). Now surfaced under three explicit keys so the units are legible
    without cross-referencing perpfinder.py: `funding_pi_4h` (the SPEC-112-normalized
    %/4h print, same transform regime_flip.to_4h uses everywhere else), `rate_raw_pi`
    (the untouched raw fraction), `interval_min` (the interval that print is
    normalized against). Display/reconciliation only — still never a verdict input.

    Best-effort, fetched LAST (req 4): a perpfinder timeout/error/shape_drift/absent-ticker
    degrades to {available:false, reason} and never blocks or slows the wired reads. The
    wall-clock budget is enforced by the caller's `_bounded()` (SPEC-172) — this function
    makes the call directly, no nested executor/timeout of its own.
    """
    try:
        r = build_perpfinder("funding", ticker=ticker)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"perpfinder error: {str(e)[:120]}"}
    if not r.get("ok"):
        return {"available": False, "reason": r.get("reason") or "perpfinder call failed"}
    rows = r.get("rows") or []
    if not rows:
        return {"available": False, "reason": "not_in_matrix"}

    venues = [{"venue": row.get("venue"), "oi": row.get("oi"), "price": row.get("price"),
               "funding_pi_4h": row.get("funding_pi_4h"),
               "rate_raw_pi": row.get("rate_raw_pi"), "interval_min": row.get("interval_min"),
               "oi_source": ("perpfinder" if isinstance(row.get("oi"), (int, float)) else None)}
              for row in rows]
    # SPEC-191 #2: direct-OI fallback for any venue perpfinder left null — best-effort,
    # never blocks the rest of the layer on a dead direct endpoint.
    sym = (ticker or "").upper().replace("USDT", "")
    for v in venues:
        if v["oi"] is None:
            try:
                usd = _resolve_direct_oi(v["venue"], sym, v["price"])
            except Exception:  # noqa: BLE001
                usd = None
            if usd is not None:
                v["oi"] = usd
                v["oi_source"] = "direct"
    total_oi = sum(v["oi"] for v in venues if isinstance(v["oi"], (int, float)))
    for v in venues:
        v["oi_share_pct"] = (round(v["oi"] / total_oi * 100, 2)
                             if isinstance(v["oi"], (int, float)) and total_oi > 0 else None)
    venues.sort(key=lambda v: (v["oi"] is None, -(v["oi"] or 0)))   # oi desc, null-oi last (req 6)

    # SPEC-174 #4: PerpFinder's per-venue `oi` on the funding-rates matrix is ALREADY USD
    # notional, not base-asset quantity — live-verified 2026-08-28 (BTC/Binance oi=$8.396B;
    # read as token units that would be 8.4 BILLION BTC, impossible against a ~19.8M supply).
    # The old `v["oi"] * v["price"]` double-counted price and corrupted every total (H:
    # total $1.13M vs its own Bybit row $8.29M; MANTRA total $12K vs venue_map's $5.2M) —
    # `total_oi_usd` is just the USD sum already computed as `total_oi` above.
    total_oi_usd = total_oi
    size_book = venues[0]["venue"] if venues and venues[0]["oi"] is not None else None
    wired_oi = sum(v["oi"] for v in venues if isinstance(v["oi"], (int, float))
                   and (v["venue"] or "").lower() in WIRED_VENUES)
    wired_oi_share_pct = round(wired_oi / total_oi * 100, 2) if total_oi > 0 else None
    desk_blind_share_pct = (round(100 - wired_oi_share_pct, 2)
                            if wired_oi_share_pct is not None else None)
    return {
        "available": True, "source": "perpfinder", "normalized_rates": True,
        "venues": venues, "total_oi_usd": total_oi_usd, "n_venues": len(venues),
        "size_book": size_book, "wired_oi_share_pct": wired_oi_share_pct,
        "desk_blind_share_pct": desk_blind_share_pct,
    }


def _risk_card_layer(ticker, thesis_display, equity_arg=None):
    """SPEC-169: the risk-card line, built off the committed thesis geometry when one is
    present (direction / mid of entry_zone / stop / signature) — else discretionary/no
    geometry, which still renders a line (tier=hypothesis, notional unknown). Best-effort:
    any resolver failure (dead venue, missing config) degrades individual fields to
    unknown/None inside risk_card itself, never raises out of this layer."""
    try:
        th = thesis_display or {}
        entry_zone = th.get("entry_zone")
        entry = (sum(entry_zone) / 2.0) if entry_zone and len(entry_zone) == 2 else None
        return RC.live_risk_line(ticker, th.get("signature") or "discretionary",
                                 th.get("direction"), entry=entry, stop=th.get("stop"),
                                 equity_arg=equity_arg)
    except Exception as e:  # noqa: BLE001 — a risk-card failure must never break the brief
        return {"available": False, "reason": f"risk_card error: {str(e)[:120]}"}


def _venue_bars_layer(ticker):
    """SPEC-188 §2: the all-venue 1h OHLC sweep + cross-venue dispersion, printed
    UNCONDITIONALLY (never gated on a thesis) — the deterministic engine layer behind
    the §3 all-venue structure rule (the user's directive was repeated 2026-09-01 and
    2026-09-02 because hand discipline failed both times). Best-effort: any failure of
    the 14-venue sweep degrades to {available:false, reason}, never silent (§3), never
    blocks the rest of the brief."""
    try:
        r = build_venue_bars(ticker, "1h", 6)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"venue_bars error: {str(e)[:140]}"}
    if not r.get("n_available"):
        return {"available": False, "reason": "no venue returned bars"}
    r["available"] = True
    return r


# SPEC-188 §2: which venues does the desk name in the tape-agreement checklist line —
# the size book (dominant OI, cross-checked separately) is intentionally excluded here;
# these four are the ones §3's directive named by name (Binance/Aster/Kraken/Coinbase).
_TAPE_AGREEMENT_CHECK_VENUES = ("binance", "aster", "kraken", "coinbase")


def _committed_levels(thesis_display):
    """[(label, price, direction)] off the committed thesis geometry for the
    tape-agreement sweep. Direction mirrors classify.py's own eval_price_leg
    SHORT/LONG stop/tp convention (SHORT stop above/tp below entry, LONG mirrors);
    an entry_zone bound has no single typed direction (the zone can be approached
    from either side depending on setup) so it's left None — the caller resolves it
    against the live bar's median close. watch_level direction is already carried on
    the field."""
    if not thesis_display:
        return []
    direction = (thesis_display.get("direction") or "").upper()
    out = []
    if thesis_display.get("stop") is not None:
        out.append(("stop", thesis_display["stop"], "above" if direction == "SHORT" else "below"))
    for i, tp in enumerate(thesis_display.get("tp") or []):
        if tp is not None:
            out.append((f"tp{i+1}", tp, "below" if direction == "SHORT" else "above"))
    zone = thesis_display.get("entry_zone")
    if zone and len(zone) == 2:
        out.append(("entry_zone_lo", zone[0], None))
        out.append(("entry_zone_hi", zone[1], None))
    for w in thesis_display.get("watch_level") or []:
        if w.get("price") is not None:
            out.append(("watch_level", w["price"], w.get("dir")))
    return out


def _tape_agreement_lines(vb, thesis_display):
    """SPEC-188 §2: one line per committed level that ANY venue crossed in the last `n`
    bars — `0.0972 prior-high: crossed 11/14 (closed beyond 9/14) · Binance ✓ · Aster ✗ ·
    Kraken ✗ · Coinbase ✗`. Union of crossings across every fetched bar (not just the
    live one) — a level that traded through on bar N-2 and held since still deserves the
    line. A level never crossed by any venue is silently omitted (nothing to report)."""
    bars = vb.get("bars") or []
    if not bars:
        return []
    ref_close = bars[-1].get("median_c")
    lines = []
    seen_prices = set()
    for label, price, direction in _committed_levels(thesis_display):
        if price is None or price in seen_prices:
            continue
        seen_prices.add(price)
        d = direction or ("above" if (ref_close is not None and price >= ref_close) else "below")
        crossed, closed = set(), set()
        n_total = 0
        for b in bars:
            agr = level_agreement(vb, price, d, bar_ts=b["ts"])
            crossed |= set(agr["crossed"])
            closed |= set(agr["closed_beyond"])
            n_total = max(n_total, agr["n_total"])
        if not crossed:
            continue
        checks = " · ".join(f"{v.title()} {'✓' if v in crossed else '✗'}"
                            for v in _TAPE_AGREEMENT_CHECK_VENUES)
        lines.append(f"{price:g} {label}: crossed {len(crossed)}/{n_total} "
                     f"(closed beyond {len(closed)}/{n_total}) · {checks}")
    return lines


def _headline(ticker, state, perp, books, onchain, defended_fade=None, unlocks=None, liqs=None, phase=None,
              venue_breadth=None):
    """One-line synthesis the Designer can echo. Robust to any degraded layer."""
    bits = []
    # SPEC-104: the Wyckoff cycle-first read leads (before any candle-signal bit below) —
    # "if the desk can't name the cycle, there is no breakout trade" (memory:
    # feedback_wyckoff_cycle_first_breakout_gate).
    if phase:
        try:
            import phase as PH
            bits.append(PH.one_liner(phase))
        except Exception:  # noqa: BLE001
            pass
    # SPEC-95: a scheduled unlock ≤7d out leads the headline — the highest-EV thing to know ahead.
    if unlocks and unlocks.get("available") and unlocks.get("flag"):
        bits.append(unlocks["flag"])
    if state.get("verdict"):
        bits.append(state["verdict"])
    elif state.get("thesis_present") is False:
        bits.append("NO-THESIS (discovery read)")
    # SPEC-99: §7 operator-heat — cluster-mate live theses belong in the one-line synthesis,
    # not buried in the JSON (the "long+short on cluster-mates is not a hedge" trap).
    if state.get("cluster_heat") and state["cluster_heat"].get("note"):
        bits.append(state["cluster_heat"]["note"])
    # SPEC-103: OI/liq ground-truth cross-check — a material OI move that's liq-silent
    # (double-open/internal-transfer fingerprint) belongs in the one-line synthesis.
    if liqs:
        try:
            import liqs as LQ
            line = LQ.one_liner(liqs)
        except Exception:  # noqa: BLE001
            line = None
        if line:
            bits.append(line)
    if perp.get("available"):
        d = perp.get("direction") or perp.get("verdict") or "?"
        f = perp.get("funding_4h")
        if isinstance(f, (int, float)):
            ftxt = f"funding {f:+.3f}%/4h"
            if perp.get("funding_venue"):
                ftxt += f" ({perp['funding_venue']})"          # SPEC-71: the venue actually used
            if perp.get("floor_suspect"):
                ftxt += " ⚠floor-suspect"                      # SPEC-71: floor alone ≠ flat (§3)
        elif perp.get("funding_unavailable"):
            ftxt = "funding n/a"
        else:
            ftxt = "funding ?"
        bits.append(f"{d} / {ftxt}")
    if onchain.get("available") and onchain.get("signal"):
        sig = onchain["signal"]
        if onchain.get("staging_vs_execution"):
            sig += f"({onchain['staging_vs_execution']})"
        bits.append(f"on-chain {sig}")
        # SPEC-89: a top holder DISTRIBUTING under a non-distributing headline signal must surface
        # so a one-call read can't show QUIET while the operator dumps (the BLESS/H false-quiet).
        if onchain.get("signal_caveat") and sig.split("(")[0] not in ("DISTRIBUTING", "STAGING"):
            bits.append("⚠ top-holder DISTRIBUTING (verify_wallet)")
        # SPEC-72: the aggregator that is actually executing the distribution (the dex_swap_sell
        # USDT leg) is the headline — a tracked-safe → aggregator flow must never read as silence.
        agg = (onchain.get("distribution") or {}).get("aggregator")
        if agg and agg.get("mode") == "dex_swap_sell":
            usd = agg.get("usd_in") or agg.get("amount_in")
            if isinstance(usd, (int, float)) and usd > 0:
                mag = f"~${usd/1e6:.2f}m" if usd >= 1e6 else f"~${usd/1e3:.0f}k"
                bits.append(f"aggregator dex_swap_sell {mag} {agg.get('asset') or 'USDT'}")
            else:
                bits.append(f"aggregator dex_swap_sell {agg.get('asset') or 'USDT'}")
        # SPEC-73: DORMANT-now but distributed-recently — surface the 24h memory line so a brief
        # read hours after the dump still says the operator distributed (not silence). The live
        # DISTRIBUTING/STAGING headline above already covers the firing case, so only add when not.
        rd = onchain.get("recent_distribution")
        if rd and rd.get("total_usd") and sig.split("(")[0] not in ("DISTRIBUTING", "STAGING"):
            usd = rd["total_usd"]
            mag = f"~${usd/1e6:.1f}M" if usd >= 1e6 else f"~${usd/1e3:.0f}K"
            dz = len(rd.get("drained_to_zero") or [])
            extra = f", {dz} drained to zero" if dz else ""
            bits.append(f"recent-distribution {mag}/{rd.get('window_h', 24)}h "
                        f"({rd.get('n_safes')} safes{extra})")
        # SPEC-98: rotation-aware freshness — a ROTATED (false-pause) or FROZEN (bank/exit)
        # read on the distribution clock belongs in the one-line synthesis (VELVET-DWF).
        fr = onchain.get("distribution_freshness")
        if fr and fr.get("line") and fr.get("verdict") in ("ROTATED", "FROZEN"):
            bits.append(fr["line"])
    # the Bitget bid — the real exit book on a cluster name (never let it be the missing piece)
    if books.get("available"):
        bg = (books.get("venues") or {}).get("bitget")
        if bg and bg.get("available") and bg.get("bid_shelf_below"):
            px = bg["bid_shelf_below"].get("price")
            if px is not None:
                bits.append(f"Bitget bid {px}")
    # SPEC-83: a GROWING ask wall = operator capping while distributing — the wall-fade tell
    df = defended_fade or {}
    ask = df.get("ask") if df.get("available") else None
    if ask and ask.get("wall_growing"):
        bits.append(f"ask-wall GROWING {ask['price']} (${ask['notional_usd']:,.0f}) — defended-fade")
    # SPEC-159 req 3: the §0.6.3b prompt, automated — a size book living mostly off the desk's
    # wired venues belongs in the one-line synthesis, not buried in the JSON.
    vb = venue_breadth or {}
    if vb.get("available"):
        blind = vb.get("desk_blind_share_pct")
        if isinstance(blind, (int, float)) and blind > 30:
            bits.append(f"⚠ {blind:.0f}% of OI off-desk (size book: {vb.get('size_book')})")
    return " — ".join(str(b) for b in bits) if bits else f"{ticker}: no data"


def _ensure_mapped(ticker):
    """SPEC 51: a brief on an unmapped watchlist name runs the mechanical onboard inline
    FIRST (the Designer must never see a bare "unmapped" where nothing was attempted).
    Bounded: any failure is returned as a tag for the on-chain layer, never raised —
    the perp read is never blocked on mapping."""
    try:
        if ticker in set(json.loads(onboard.WALLETS.read_text()).get("tokens", {})):
            return None                                   # mapped — nothing to do
    except Exception:  # noqa: BLE001 — unreadable config = the onchain layer's problem
        return None
    try:
        r = onboard.build_onboard(ticker)
        if r.get("ok"):
            return {"onboarded": True}
        out = {"onboarded": False, "why": r.get("reason", "unknown")}
        if r.get("candidates"):
            out["candidates"] = r["candidates"]
        return out
    except Exception as ex:  # noqa: BLE001
        return {"onboarded": False, "why": f"exception:{str(ex)[:60]}"}


def build_brief(ticker, venue=None, equity_arg=None):
    """Compose state + perp + BOTH books + on-chain into one distilled envelope.
    Layers run concurrently under a per-layer budget; each is degrade-explicit.
    `equity_arg` (SPEC-169): fallback equity $ for the risk-card line when the venue
    read (SPEC-170) is unavailable — never a remembered/default dollar figure.

    SPEC-172: every sub-read carries its OWN wall-clock budget and a slow one can never
    silently eat the others — `layer_ms`/`timed_out` (in `meta`/top-level, req a+b) make
    that auditable, and `_bounded()`'s abandon-don't-wait executor makes a stalled network
    call time out for REAL (see `_bounded`'s docstring for the `with`-block bug this
    replaces) instead of quietly re-imposing its own timeout on the whole brief."""
    ticker = ticker.upper().replace("USDT", "")
    mapping = _ensure_mapped(ticker)
    out = {}
    layer_ms, timed_out = {}, []

    # SPEC-71: live funding resolved ONCE; the state and perp legs read this same future
    ex = ThreadPoolExecutor(max_workers=7)
    f_live = ex.submit(_resolve_live, ticker)
    jobs = {
        "state": (_state_layer, (ticker, f_live)),
        "perp": (_perp_layer, (ticker, f_live)),
        "books": (_books_layer, (ticker, venue)),
        "onchain": (_onchain_layer, (ticker,)),
        "liqs": (_liqs_layer, (ticker,)),     # SPEC-103: OI/liq ground-truth cross-check
        "phase": (_phase_layer, (ticker,)),   # SPEC-104: Wyckoff cycle-first gate
    }
    t0s = {name: time.time() for name in jobs}
    futs = {name: ex.submit(fn, *a) for name, (fn, a) in jobs.items()}
    for name, f in futs.items():
        try:
            out[name] = f.result(timeout=LAYER_BUDGET)
        except FuturesTimeout:
            out[name] = {"available": False,
                         "reason": f"{name} layer exceeded {LAYER_BUDGET}s budget"}
            timed_out.append(name)
        except Exception as e:  # noqa: BLE001
            out[name] = {"available": False, "reason": f"{name} error: {str(e)[:140]}"}
        layer_ms[name] = int((time.time() - t0s[name]) * 1000)
    ex.shutdown(wait=False)   # SPEC-172: never wait on an abandoned/timed-out thread

    if mapping and not mapping.get("onboarded"):
        # SPEC 51: mapping was ATTEMPTED and failed — say so explicitly on the layer
        out["onchain"]["reason"] = f"UNMAPPED(onboard_failed:{mapping['why']})"
        # SPEC-175: a bare "resolve_ambiguous" doesn't say WHAT was ambiguous — surface
        # the candidate set (max 5) so a KORU-style non-crypto match self-disqualifies
        # here instead of downstream.
        if mapping.get("candidates"):
            out["onchain"]["resolve_candidates"] = [
                {"id": c.get("id"), "name": c.get("name"), "chain": c.get("chain"),
                 "mc_rank": c.get("market_cap_rank"), "not_crypto": bool(c.get("not_crypto"))}
                for c in mapping["candidates"][:5]
            ]

    # SPEC-172: the remaining surfaces (defended_fade/unlocks/venue_breadth/risk_card) each
    # depend on the layers above, so they run concurrently AFTER that block — each still
    # under its own hard budget, and a hang in one never blocks the others or the return.
    post_jobs = {
        # each entry: (fn, args, own budget) — risk_card is the one real network leg
        # (venue equity + a maxsize book walk); the rest are local/offline reads kept on
        # a short budget purely as a safety net.
        "defended_fade": (_defended_fade_layer, (ticker, out.get("books"), out.get("perp")), 15),
        "unlocks": (_unlocks_layer, (ticker,), 15),
        "venue_breadth": (_venue_breadth_layer, (ticker,), VENUE_BREADTH_BUDGET),
        "risk_card": (_risk_card_layer,
                     (ticker, (out.get("state") or {}).get("thesis"), equity_arg), POST_LAYER_BUDGET),
        "venue_bars": (_venue_bars_layer, (ticker,), VENUE_BARS_BUDGET),   # SPEC-188 §2
    }
    ex2 = ThreadPoolExecutor(max_workers=len(post_jobs))
    post_futs = {name: (ex2.submit(_bounded, name, fn, a, budget), time.time(), budget)
                for name, (fn, a, budget) in post_jobs.items()}
    for name, (f, t0, budget) in post_futs.items():
        try:
            result, ms, to = f.result(timeout=budget + 5)
        except Exception as e:  # noqa: BLE001 — _bounded itself never raises; belt+braces
            result, ms, to = {"available": False, "reason": f"{name} error: {str(e)[:140]}"}, \
                int((time.time() - t0) * 1000), True
        out[name] = result
        layer_ms[name] = ms
        if to:
            timed_out.append(name)
    ex2.shutdown(wait=False)

    # SPEC-188 §2: tape-agreement lines need BOTH venue_bars (just resolved above) and the
    # committed thesis (resolved earlier in the main loop's state layer) — computed here,
    # not as its own concurrent job, since it's a pure in-memory fold over two already-
    # fetched results (no network of its own).
    vbars = out.get("venue_bars") or {}
    if vbars.get("available"):
        vbars["tape_agreement"] = _tape_agreement_lines(vbars, (out.get("state") or {}).get("thesis"))

    out["headline"] = _headline(ticker, out["state"], out["perp"], out["books"], out["onchain"],
                                out.get("defended_fade"), out.get("unlocks"), out.get("liqs"),
                                out.get("phase"), out.get("venue_breadth"))
    out["timed_out"] = timed_out
    out["meta"] = {"layer_ms": layer_ms}
    return {"ticker": ticker, **out}


def _funding_dispersion_line(fbv):
    """One line summarizing per-venue funding — the SPEC-193 compact replacement for the
    full `funding_by_venue` dict (which the orchestrator never needs venue-by-venue, only
    the dispersion picture)."""
    if not fbv:
        return None
    bits = []
    for venue in sorted(fbv):
        v = fbv[venue] or {}
        f4 = v.get("funding_4h")
        flag = "†" if v.get("is_floor") else ""
        bits.append(f"{venue}:{f4:+.3f}%{flag}" if f4 is not None else f"{venue}:n/a")
    return " · ".join(bits)


def _book_line(v):
    """One line per venue book — mid + the two defended shelves, no raw depth levels."""
    if not v or not v.get("available"):
        return f"unavailable ({(v or {}).get('reason') or '?'})"
    bits = [f"mid {v['mid']:g}" if v.get("mid") is not None else "mid ?"]
    aw = v.get("ask_wall_above") or {}
    bs = v.get("bid_shelf_below") or {}
    if aw.get("price") is not None:
        bits.append(f"ask-wall {aw['price']:g} (${(aw.get('notional_usd') or 0):,.0f})")
    if bs.get("price") is not None:
        bits.append(f"bid-shelf {bs['price']:g} (${(bs.get('notional_usd') or 0):,.0f})")
    if v.get("truncated"):
        bits.append("truncated")
    return "  ".join(bits)


def compact_brief(b):
    """SPEC-193: the ≤3KB decision-only render — same decision keys `render_human`/full
    JSON carry (state verdict+reason UNTRUNCATED+thesis params, headline, oic line,
    risk-card line, venue-breadth size_book/blind%, one-line funding dispersion, one
    line per venue book, venue_bars dispersion summary + tape-agreement lines) with the
    bulk dropped (raw depth levels, raw OHLC bar arrays, per-venue kline payloads,
    `meta.layer_ms`). `state.thesis` is passed through UNCHANGED — restable params
    (entry/stop/tp/legs/watch_level) must survive byte-for-byte."""
    state = b.get("state") or {}
    perp = b.get("perp") or {}
    onchain = b.get("onchain") or {}
    books = b.get("books") or {}
    vbreadth = b.get("venue_breadth") or {}
    vbars = b.get("venue_bars") or {}
    risk = b.get("risk_card") or {}

    out = {"ticker": b.get("ticker"), "headline": b.get("headline"),
          "timed_out": b.get("timed_out") or None}

    out["state"] = {k: v for k, v in {
        "available": state.get("available"), "verdict": state.get("verdict"),
        "reason": state.get("reason"), "thesis_present": state.get("thesis_present"),
        "direction": state.get("direction"), "thesis": state.get("thesis"),
    }.items() if v is not None}

    oic_line = OC.compact_line(perp.get("oi_construction"))
    out["perp"] = {k: v for k, v in {
        "available": perp.get("available"), "verdict": perp.get("verdict"),
        "direction": perp.get("direction"), "tier": perp.get("tier"),
        "funding_4h": perp.get("funding_4h"), "funding_venue": perp.get("funding_venue"),
        "floor_suspect": perp.get("floor_suspect") or None,
        "funding_dispersion": _funding_dispersion_line(perp.get("funding_by_venue")),
        "oi_chg_pct": perp.get("oi_chg_pct"), "near_ath": perp.get("near_ath") or None,
        "cvd_verdict": perp.get("cvd_verdict"), "price": perp.get("price"),
        "oic": oic_line, "battlefield": perp.get("battlefield"),
        "oi_mc_flag": perp.get("oi_mc_flag"), "oi_mc_caveat": perp.get("oi_mc_caveat"),
    }.items() if v is not None}

    conc = (onchain.get("concentration") or {})
    out["onchain"] = {k: v for k, v in {
        "available": onchain.get("available"), "signal": onchain.get("signal"),
        "bias": onchain.get("bias"), "score": onchain.get("score"),
        "n_newly_fired": len(onchain.get("newly_fired") or []) or None,
        "staging_vs_execution": onchain.get("staging_vs_execution"),
        "distribution_freshness": onchain.get("distribution_freshness"),
        "signal_caveat": onchain.get("signal_caveat"),
        "resolve_candidates": onchain.get("resolve_candidates"),
        "reason": onchain.get("reason"),
        "top1_pct": conc.get("top1_pct"), "top10_pct": conc.get("top10_pct"),
        "holder_count": conc.get("holder_count"),
    }.items() if v is not None}

    out["books"] = {name: _book_line(v) for name, v in (books.get("venues") or {}).items()}

    if risk.get("line"):
        out["risk_card"] = {"line": risk["line"]}
    elif risk.get("reason"):
        out["risk_card"] = {"available": False, "reason": risk["reason"]}

    if vbreadth.get("available"):
        out["venue_breadth"] = {k: v for k, v in {
            "size_book": vbreadth.get("size_book"), "n_venues": vbreadth.get("n_venues"),
            "wired_oi_share_pct": vbreadth.get("wired_oi_share_pct"),
            "desk_blind_share_pct": vbreadth.get("desk_blind_share_pct"),
        }.items() if v is not None}
    elif vbreadth.get("reason"):
        out["venue_breadth"] = {"available": False, "reason": vbreadth["reason"]}

    if vbars.get("available"):
        # SPEC-193: the summary line only needs the RECENT dispersion trend, not every
        # historical bar `render_tape_line` would otherwise stringify — last 3 bars.
        recent = dict(vbars, bars=(vbars.get("bars") or [])[-3:])
        out["venue_bars"] = {"summary": VB.render_tape_line(recent),
                             "n_available": vbars.get("n_available"),
                             "n_total": vbars.get("n_total"),
                             "dominant_tape": vbars.get("dominant_tape"),
                             "execution_venue": vbars.get("execution_venue"),
                             "tape_agreement": vbars.get("tape_agreement") or []}
    elif vbars.get("reason"):
        out["venue_bars"] = {"available": False, "reason": vbars["reason"]}

    return {k: v for k, v in out.items() if v is not None}


def render_human(b):
    print(f"# {b['ticker']} — brief (state + perp + books + on-chain)\n")
    print(f"  {b['headline']}\n")
    st = b["state"]
    print(f"  STATE:   {st.get('verdict') or '—'}  ({'thesis' if st.get('thesis_present') else 'no thesis'}) — {st.get('reason','')}")
    perp = b["perp"]
    if perp.get("available"):
        fv = f" ({perp['funding_venue']})" if perp.get("funding_venue") else ""
        flag = " ⚠floor-suspect" if perp.get("floor_suspect") else ""
        print(f"  PERP:    {perp.get('direction')}  fund {perp.get('funding_4h')}{fv}{flag}  oi {perp.get('oi_chg_pct')}  px {perp.get('price')}")
        for vn, vd in (perp.get("funding_by_venue") or {}).items():
            f4 = vd.get("funding_4h")
            print(f"    fund {vn}: {f4:+.4f}%/4h" if isinstance(f4, (int, float)) else f"    fund {vn}: n/a",
                  "[floor]" if vd.get("is_floor") else "")
    else:
        print(f"  PERP:    unavailable ({perp.get('reason')})")
    bk = b["books"]
    if bk.get("available"):
        for v, vd in bk["venues"].items():
            if vd.get("available"):
                bid = (vd.get("bid_shelf_below") or {}).get("price")
                print(f"  BOOK {v}: mid {vd.get('mid')}  bid-shelf {bid}  truncated={vd.get('truncated')}")
            else:
                print(f"  BOOK {v}: unavailable ({vd.get('reason')})")
    else:
        print(f"  BOOKS:   unavailable ({bk.get('reason')})")
    oc = b["onchain"]
    if oc.get("available"):
        c = oc.get("concentration", {})
        print(f"  ONCHAIN: {oc.get('signal')} ({oc.get('staging_vs_execution')})  top1 {c.get('top1_pct')}%  top10 {c.get('top10_pct')}%")
    else:
        print(f"  ONCHAIN: unavailable ({oc.get('reason')})")
    df = b.get("defended_fade") or {}
    ask = df.get("ask") if df.get("available") else None
    if ask:
        grow = "GROWING" if ask.get("wall_growing") else "flat"
        geo = ask.get("geometry") or {}
        print(f"  FADE:    ask-wall {ask['price']} (${ask['notional_usd']:,.0f}) {grow} "
              f"[{ask.get('snapshots')} snaps] {ask.get('verdict')}"
              + (f"  entry {geo.get('entry')}/stop {geo.get('stop')}" if geo.get("entry") else ""))
    rc = b.get("risk_card") or {}
    if rc.get("line"):
        print(f"  RISK:    {rc['line']}")
    # SPEC-188 §2: the all-venue tape block — printed UNCONDITIONALLY, never silent on
    # unavailability (§3 zero-print rule: a missing sweep must say so, not go quiet).
    vb = b.get("venue_bars") or {}
    if vb.get("available"):
        print(" ", VB.render_tape_line(vb))
        dt = vb.get("dominant_tape")
        size_book = (b.get("venue_breadth") or {}).get("size_book")
        if dt or size_book:
            bits = []
            if dt:
                bits.append(f"dominant tape {dt['venue']} (${dt['turnover_24h_usd']:,.0f}/24h)")
            if size_book:
                bits.append(f"size book (OI) {size_book}")
            bits.append(f"execution {vb.get('execution_venue')}")
            worst = max((bar for bar in vb.get("bars") or []),
                       key=lambda bar: bar.get("high_spread_pct") or 0, default=None)
            if worst and worst.get("high_spread_pct"):
                bits.append(f"high-spread max {worst['max_h_venue']}/{worst['min_h_venue']} "
                           f"{worst['high_spread_pct']:+.1f}%")
            print("  " + " · ".join(bits))
        for line in vb.get("tape_agreement") or []:
            print("  ", line)
    else:
        print(f"  tape: UNAVAILABLE ({vb.get('reason', '?')})")
    # SPEC-180 req 5: brief renders the FULL oi_construction block (not the compact
    # board field — that's classify's job).
    oic = (b.get("perp") or {}).get("oi_construction")
    if oic:
        print(f"  OIC:     verdict {oic.get('verdict')}  gating_ok={oic.get('gating_ok')}")
        vr = oic.get("venue_roles") or {}
        me, sb, ex, hg = (vr.get("mark_engine") or {}, vr.get("size_book") or {},
                         vr.get("exit") or {}, vr.get("hedge") or {})
        print(f"      mark_engine {me.get('verdict')}  size_book {sb.get('verdict')}  "
             f"exit {ex.get('grade')}({ex.get('venue')})  hedge {hg.get('verdict')}")
        for t in oic.get("oi_types") or []:
            if t.get("verdict") != "UNKNOWN":
                print(f"      {t['type']}: {t['verdict']} ({t.get('share_read', '?')})")
        if oic.get("degraded"):
            print(f"      degraded: {oic['degraded']}")
    print()


def main():
    ap = argparse.ArgumentParser(description="One-call full-stack token read (SPEC 30)")
    ap.add_argument("ticker")
    ap.add_argument("--venue", default=None, help="aster|bitget|binance (omit = all, Aster first)")
    ap.add_argument("--equity", type=float, default=None,
                    help="SPEC-169: fallback equity $ for the risk-card line when the "
                         "venue read (SPEC-170) is unavailable")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--render", choices=["full", "compact"], default="full",
                    help="SPEC-193: 'compact' drops raw bars/depth/kline payloads, "
                         "keeps every decision key (reason UNTRUNCATED)")
    a = ap.parse_args()
    b = build_brief(a.ticker, a.venue, equity_arg=a.equity)
    if a.render == "compact":
        # SPEC-193: compact is a JSON-only mode — always machine-consumed, never
        # piped through the TTY render (which expects the full shape).
        print(json.dumps(compact_brief(b)))
    elif a.json:
        print(json.dumps(b))
    else:
        render_human(b)


if __name__ == "__main__":
    main()
