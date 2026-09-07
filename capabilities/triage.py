#!/usr/bin/env python3
"""triage.py — Mode C compact crime-triage board across the watchlist (NATIVE).

Two paths, one source of truth:
  - build_board(tickers=None) -> list[dict]   PURE compute. No printing. The JSON contract.
  - render_human(board)                         the colored one-glance cards (default TTY view).

  python3 capabilities/triage.py            # human cards (full watchlist)
  python3 capabilities/triage.py BILL LAB   # subset
  python3 capabilities/triage.py --json     # JSON array (the orchestrator/machine path)

Per-token JSON contract (see docs/triage.md):
  {ticker, category, price, chg24, range_pct, vol_m, funding_pi, funding_venue, funding_range,
   funding_suspect, funding_raw_pi, funding_interval_min, oi_chg_pct, oi_chg_pct_24h,
   ls_ratio, signals[], direction(short|long|neutral), tier(live|watch|dust), memo}
  oi_chg_pct_24h (SPEC-174 #6) is the SAME value as oi_chg_pct with its window labelled
  in the key (the source field is literally named oi_pct_24h) — additive, never breaking.

Pulls Binance/Bybit/Bitget/Aster funding + Binance/Bybit OI 24h + Binance 24h ticker +
top-trader L/S. Reads config/watchlist.json. Flag logic is unchanged from the
parts-bin original — only compute and render are separated. funding_pi is the most-extreme
non-floor venue print, normalized to **%/4h-equivalent** (SPEC-112) — a 1h-interval venue is
scaled (`* 240/interval_min`) before it's compared or rendered, so it never sits in the same
column as a raw 4h print. The un-normalized print + its interval ride along as funding_raw_pi /
funding_interval_min.
"""
import argparse
import json
import subprocess
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import colors as C
import regime_flip as RF
import oi_mc as OM
import cvd as CVD   # SPEC-177: cvd.resolve_spot_venue is the battlefield ratio's spot leg

REPO = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = REPO / "config" / "watchlist.json"
UA = "Mozilla/5.0 (triage.py)"

# SPEC-108: floor sentinel in pct-space (fund_latest is already *100, unlike regime_flip's
# raw fraction) — same sentinel, different units.
_FLOOR_PCT = RF.FUNDING_FLOOR_RAW * 100
_FLOOR_BAND_PCT = RF.FUNDING_FLOOR_BAND * 100


def _is_floor_pct(v):
    return v is not None and abs(abs(v) - _FLOOR_PCT) <= _FLOOR_BAND_PCT


def fetch(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError):
        return None


def _live_funding_pct(venue, ticker):
    """SPEC 18: LIVE predicted funding rate (%/interval) for the venue — NOT the last settled
    print. Bybit: tickers.fundingRate; Binance/Aster: premiumIndex.lastFundingRate. Returns
    None on any fetch/parse failure (caller degrades to the settled head)."""
    s = f"{ticker}USDT"
    try:
        if venue == "bybit":
            d = fetch(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={s}")
            lst = d.get("result", {}).get("list", []) if isinstance(d, dict) else []
            return float(lst[0]["fundingRate"]) * 100 if lst and lst[0].get("fundingRate") not in (None, "") else None
        host = "fapi.binance.com" if venue == "binance" else "fapi.asterdex.com"
        d = fetch(f"https://{host}/fapi/v1/premiumIndex?symbol={s}")
        if isinstance(d, dict) and d.get("lastFundingRate") not in (None, ""):
            return float(d["lastFundingRate"]) * 100
    except (KeyError, ValueError, TypeError, IndexError):
        return None
    return None


def venue_pull(ticker):
    """Pull funding + OI + ticker + L/S for one ticker across Binance / Bybit / Aster."""
    out = {"ticker": ticker, "binance": {}, "bybit": {}, "aster": {}}

    # SPEC 18: `fund_latest` must be the LIVE PREDICTED rate (premiumIndex.lastFundingRate),
    # NOT the last SETTLED print — the settled head lags and hid a §5-vetoed short (EDEN
    # settled +0.005% vs live −1.72%). The settled history stays as the series only.
    bin_fund = fetch(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={ticker}USDT&limit=7")
    settled_head = None
    if bin_fund:
        rates = [float(x["fundingRate"]) * 100 for x in bin_fund[-7:]]
        out["binance"]["fund_hist"] = rates
        settled_head = rates[-1] if rates else None
    out["binance"]["fund_latest"] = _live_funding_pct("binance", ticker)
    if out["binance"]["fund_latest"] is None:        # live unavailable → degrade to settled head
        out["binance"]["fund_latest"] = settled_head
    # SPEC-112: interval so the board can normalize to 4h-equivalent before comparing/rendering
    # (Binance funding is hourly on many pairs, not the 4h/8h default).
    out["binance"]["interval_min"] = RF.binance_interval_min(f"{ticker}USDT")

    bin_oi = fetch(f"https://fapi.binance.com/futures/data/openInterestHist?symbol={ticker}USDT&period=1h&limit=24")
    if bin_oi and len(bin_oi) >= 2:
        oi_first = float(bin_oi[0]["sumOpenInterest"])
        oi_last = float(bin_oi[-1]["sumOpenInterest"])
        out["binance"]["oi_pct_24h"] = (oi_last / oi_first - 1) * 100 if oi_first else None
    if bin_oi:
        # SPEC-122: sumOpenInterestValue is already OI priced in USDT — no extra fetch needed
        # for the oi_mc ratio (the same cross-venue-selected primary-venue OI read).
        try:
            out["binance"]["oi_usd"] = float(bin_oi[-1]["sumOpenInterestValue"])
        except (KeyError, TypeError, ValueError):
            pass

    bin_t = fetch(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={ticker}USDT")
    if bin_t:
        out["binance"]["px"] = float(bin_t["lastPrice"])
        out["binance"]["ch24"] = float(bin_t["priceChangePercent"])
        out["binance"]["hi24"] = float(bin_t["highPrice"])
        out["binance"]["lo24"] = float(bin_t["lowPrice"])
        out["binance"]["qvol24"] = float(bin_t["quoteVolume"])

    bin_ls = fetch(f"https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol={ticker}USDT&period=1h&limit=1")
    if bin_ls:
        out["binance"]["ls"] = float(bin_ls[-1]["longShortRatio"])

    # SPEC 18: Bybit latest = LIVE predicted (tickers.fundingRate), not settled funding/history.
    out["bybit"]["fund_latest"] = _live_funding_pct("bybit", ticker)
    if out["bybit"]["fund_latest"] is None:           # live unavailable → degrade to settled head
        by_fund = fetch(f"https://api.bybit.com/v5/market/funding/history?category=linear&symbol={ticker}USDT&limit=7")
        if by_fund and by_fund.get("retCode") == 0:
            items = by_fund.get("result", {}).get("list", [])
            rates = [float(x["fundingRate"]) * 100 for x in items][::-1]
            out["bybit"]["fund_latest"] = rates[-1] if rates else None
    out["bybit"]["interval_min"] = RF.bybit_interval_min(f"{ticker}USDT")

    by_oi = fetch(f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={ticker}USDT&intervalTime=1h&limit=24")
    if by_oi and by_oi.get("retCode") == 0:
        items = by_oi.get("result", {}).get("list", [])
        if len(items) >= 2:
            oi_first = float(items[-1]["openInterest"])
            oi_last = float(items[0]["openInterest"])
            out["bybit"]["oi_pct_24h"] = (oi_last / oi_first - 1) * 100 if oi_first else None

    # SPEC-112: reuse regime_flip's Aster fetcher — it derives interval_min from the last two
    # settle timestamps (Aster has no fundingInfo endpoint like Binance/Bybit).
    av = RF._venue_aster(f"{ticker}USDT")
    if av and av.get("funding_raw") is not None:
        out["aster"]["fund_latest"] = round(av["funding_raw"] * 100, 6)
        out["aster"]["interval_min"] = av.get("interval_min", 240)

    # SPEC-108: Bitget joins the cross-venue funding set (never a single-venue print) —
    # reuses regime_flip's existing Bitget fetcher, no new vendor plumbing.
    out["bitget"] = {}
    bg = RF._venue_bitget(f"{ticker}USDT")
    if bg and bg.get("funding_raw") is not None:
        out["bitget"]["fund_latest"] = round(bg["funding_raw"] * 100, 6)
        out["bitget"]["interval_min"] = bg.get("interval_min", 240)

    # SPEC-177: spot leg for the battlefield perp/spot ratio — the REAL spot venue
    # (req 5, never Binance-centric); perp leg reuses `qvol24` already fetched above,
    # zero new fetch for that half. Any resolve failure degrades to None (§3).
    try:
        _, spot_vol24, _ = CVD.resolve_spot_venue(ticker)
        out["spot_vol_24h_usd"] = spot_vol24
    except Exception:  # noqa: BLE001 — a dead spot resolver degrades this leg only
        out["spot_vol_24h_usd"] = None

    return out


def _select_funding(data):
    """SPEC-108: funding_pi = the most-extreme NON-floor print across bybit/binance/bitget/aster
    (a floor print is a data failure, never a datum — §3), compared and rendered as a
    **4h-equivalent** (SPEC-112) so a 1h venue never sits in the same column as a raw 4h print.
    The floor check runs on the RAW per-interval value BEFORE normalization — a floor scaled up
    from a 1h interval would otherwise clear the floor band and be mistaken for a real read.
    Returns (funding_pi_4h, funding_venue, funding_range_4h[min, max], funding_suspect)."""
    all_vals, non_floor = {}, {}
    for venue in ("bybit", "binance", "bitget", "aster"):
        v = data.get(venue) or {}
        val = v.get("fund_latest")
        if val is None:
            continue
        all_vals[venue] = val
        if not _is_floor_pct(val):
            interval_min = v.get("interval_min") or 240
            non_floor[venue] = RF.to_4h(val, interval_min)

    if non_floor:
        venue = max(non_floor, key=lambda k: abs(non_floor[k]))
        vals = list(non_floor.values())
        return non_floor[venue], venue, [min(vals), max(vals)], False
    if all_vals:
        return None, None, None, True   # every reporting venue floored — data failure, not flat
    return None, None, None, False      # no venue reported anything


def _compute_flags(data):
    """The verbose signal flags (unchanged logic from the parts-bin render_token)."""
    b = data.get("binance", {})
    y = data.get("bybit", {})
    bin_f, by_f = b.get("fund_latest"), y.get("fund_latest")
    bin_oi, by_oi = b.get("oi_pct_24h"), y.get("oi_pct_24h")
    ch = b.get("ch24")
    fund_div = abs(bin_f - by_f) if (bin_f is not None and by_f is not None) else None

    flags = []
    if bin_oi is not None and bin_oi <= -5:
        flags.append(f"BIN-OI-FLUSH ({bin_oi:+.1f}%)")
    if by_oi is not None and by_oi <= -5:
        flags.append(f"BYB-OI-FLUSH ({by_oi:+.1f}%)")
    if bin_oi is not None and bin_oi >= 5 and ch is not None and ch <= -3:
        flags.append(f"BIN-OI-UP-ON-DROP ({bin_oi:+.1f}% / {ch:+.1f}%)  TRAP CANDIDATE")
    if by_f is not None and by_f <= -0.10:
        flags.append(f"BYB-FUND-DEEP-NEG ({by_f:+.3f}%)  TRAP-FORMATION")
    if bin_f is not None and bin_f >= 0.10:
        flags.append(f"BIN-FUND-HOT ({bin_f:+.3f}%)  longs paying carry")
    if fund_div is not None and fund_div > 0.04:
        flags.append(f"FUND-DIVERGENCE Δ{fund_div:.3f}%  pull per-venue heatmaps")
    if b.get("ls") is not None:
        if b["ls"] >= 1.30:
            flags.append(f"L/S {b['ls']:.2f} retail-long")
        elif b["ls"] <= 0.90:
            flags.append(f"L/S {b['ls']:.2f} retail-short")
    return flags


def _short_flag(f):
    """Compress a verbose flag string to a chip (unchanged mapping)."""
    f = f.split("(")[0].strip()
    return {
        "BIN-OI-FLUSH": "OI-flush", "BYB-OI-FLUSH": "OI-flush",
        "BIN-OI-UP-ON-DROP": "trap?", "BYB-FUND-DEEP-NEG": "deep-neg-fund",
        "BIN-FUND-HOT": "fund-hot", "FUND-DIVERGENCE": "venue-div",
    }.get(f, f.lower().replace("l/s ", "L/S"))


def _short_state(state):
    """Trim the long watchlist memo to a compact header tag."""
    if not state:
        return ""
    for sep in (".", ";", "—", " - "):
        if sep in state:
            state = state.split(sep)[0]
            break
    state = state.strip()
    return state[:34] + ("…" if len(state) > 34 else "")


def _direction(state, flags):
    s = (state or "").upper()
    fl = " ".join(flags).upper()
    if "SHORT" in s or "LONGS PAYING" in fl:
        return "short"
    if "LONG" in s or "TRAP-FORMATION" in fl:
        return "long"
    if "TRAP CANDIDATE" in fl:
        return "short"
    return "neutral"


def _tier(vol_m, nflags):
    if vol_m < 10:
        return "dust"
    if nflags >= 2:
        return "live"
    if nflags == 1:
        return "watch"
    return "dust"


def _build_row(data, meta):
    """One token → the JSON contract dict (pure)."""
    b = data.get("binance", {})
    y = data.get("bybit", {})
    px, ch, qvol = b.get("px"), b.get("ch24"), b.get("qvol24")
    lo, hi = b.get("lo24"), b.get("hi24")
    funding_pi, funding_venue, funding_range, funding_suspect = _select_funding(data)
    # SPEC-112: raw per-interval print + its interval alongside the 4h-normalized funding_pi,
    # same convention as brief/classify — the row carries both so a reader can see the raw print.
    funding_raw_pi, funding_interval_min = None, None
    if funding_venue is not None:
        venue_data = data.get(funding_venue) or {}
        funding_raw_pi = venue_data.get("fund_latest")
        funding_interval_min = venue_data.get("interval_min") or 240

    range_pct = None
    if px is not None and lo and hi and hi > lo:
        range_pct = round((px - lo) / (hi - lo) * 100)

    flags = _compute_flags(data)
    vol_m = round((qvol or 0) / 1e6, 1)
    memo = meta.get("state", "")
    direction = _direction(memo, flags)
    row = {
        "ticker": meta["ticker"],
        "category": meta.get("category", "?"),
        "price": px,
        "chg24": round(ch, 1) if ch is not None else None,
        "range_pct": range_pct,
        "vol_m": vol_m,
        "funding_pi": round(funding_pi, 4) if funding_pi is not None else None,
        "funding_venue": funding_venue,
        "funding_range": [round(funding_range[0], 4), round(funding_range[1], 4)]
                         if funding_range is not None else None,
        "funding_suspect": funding_suspect,
        "funding_raw_pi": round(funding_raw_pi, 6) if funding_raw_pi is not None else None,
        "funding_interval_min": funding_interval_min,
        "oi_chg_pct": round(b.get("oi_pct_24h")) if b.get("oi_pct_24h") is not None else None,
        # SPEC-174 #6: additive alias, window labelled in the key (source field is
        # literally named oi_pct_24h) — distinct from brief.perp's 48h and oi_sides' 4h
        # OI-change reads, the MANTRA scout-sweep confusion (neither used to be labelled).
        "oi_chg_pct_24h": round(b.get("oi_pct_24h")) if b.get("oi_pct_24h") is not None else None,
        "ls_ratio": round(b["ls"], 2) if b.get("ls") is not None else None,
        "signals": [_short_flag(f) for f in flags],
        "direction": direction,
        "tier": _tier(vol_m, len(flags)),
        "memo": memo,
    }

    # SPEC-122: OI/MC "perp-casino" flag — read-only, never blocks (§7/§0.6). MC fetch is
    # best-effort (CoinGecko via oi_mc.fetch_market_cap) and skipped entirely when there's
    # no OI to divide (no wasted network call); any failure degrades the whole block to
    # nulls, never a fabricated ratio (§3).
    oi_usd = b.get("oi_usd")
    row["oi_mc_ratio"] = row["oi_mc_flag"] = row["oi_mc_caveat"] = None
    if oi_usd is not None:
        try:
            mc_usd = OM.fetch_market_cap(meta["ticker"])
            om = OM.build_oi_mc(oi_usd, mc_usd, direction=direction.upper() if direction else None)
            row["oi_mc_ratio"] = om["oi_mc_ratio"]
            row["oi_mc_flag"] = om["oi_mc_flag"]
            row["oi_mc_caveat"] = om["oi_mc_caveat"]
        except Exception:  # noqa: BLE001 — the board must never break on this flag
            pass

    # SPEC-177: battlefield verdict — perp leg reuses `qvol` (Binance 24h, already
    # fetched above); spot leg reuses `spot_vol_24h_usd` (venue_pull's cvd.resolve_spot_venue
    # call, req 5). leverage_state stays UNKNOWN here: its ΔOI/range-hold series needs an
    # OI-history store this board sweep doesn't have (SPEC-178's sampler is the intended
    # future source).
    try:
        ratio, verdict = OM.battlefield_verdict(qvol, data.get("spot_vol_24h_usd"),
                                                row["oi_mc_flag"])
        row["perp_spot_ratio"] = round(ratio, 4) if ratio is not None else None
        row["battlefield"] = verdict
    except Exception:  # noqa: BLE001 — the board must never break on this flag
        row["perp_spot_ratio"] = None
        row["battlefield"] = "UNKNOWN"
    row["leverage_state"] = {
        "leverage_4h": OM.leverage_state_for_window(None, None, None, 8.0, "4h"),
        "leverage_48h": OM.leverage_state_for_window(None, None, None, 15.0, "48h"),
    }

    # SPEC-114: sector-divergence Cat A prior, read-only annotation (never a gate, §0.6).
    # Omitted entirely when the ticker has no basket mapping — unmapped rows stay
    # byte-identical (regression).
    if ch is not None:
        try:
            import sector_divergence as _sd
            tag = _sd.tag_for(meta["ticker"], ch, window_days=1)
            if tag:
                row["sector"] = tag
        except Exception:  # noqa: BLE001 — a prior must never break the board row
            pass
    return row


# ── SPEC 64: launchd agent health — one glance, every scan ──────────────────────
# The nonce-surveil agent was unloaded for 9 days unnoticed. triage meta now reports
# whether each standing agent is loaded, so a dead watcher is visible on every board.
AGENT_LABELS = {
    "coder_dispatch": "com.crimedesk.coder-dispatch",
    "nonce_surveil":  "com.crimedesk.nonce-surveil",
    "board_tick":     "com.crimedesk.board-tick",
}


def _launchctl_loaded(label):
    """True if the launchd agent is loaded, False if not, None if launchctl is
    unavailable (non-macOS / no binary) — surfaced as 'unknown', never a false MISSING.
    `launchctl list <label>` exits 0 when the job is loaded, non-zero otherwise."""
    try:
        proc = subprocess.run(["launchctl", "list", label],
                              capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0


def agent_health(probe=None):
    """{coder_dispatch, nonce_surveil, board_tick} → loaded | MISSING | unknown.
    `probe` is injectable for tests; defaults to the launchctl read."""
    probe = probe or _launchctl_loaded
    out = {}
    for key, label in AGENT_LABELS.items():
        st = probe(label)
        out[key] = "loaded" if st is True else "MISSING" if st is False else "unknown"
    return out


def _resolve_items(tickers=None):
    wl = json.loads(WATCHLIST_PATH.read_text())
    items = wl["tokens"]
    if tickers:
        wanted = [t.upper() for t in tickers]
        by_ticker = {x["ticker"].upper(): x for x in items}
        items = [by_ticker.get(t, {"ticker": t, "category": "?", "state": "ad-hoc scan"})
                 for t in wanted]
    return items


def build_board(tickers=None):
    """Pure compute: list of contract dicts, sorted (signals desc, then vol desc)."""
    items = _resolve_items(tickers)
    rows = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(venue_pull, x["ticker"]): x for x in items}
        for fut in as_completed(futures):
            meta = futures[fut]
            try:
                rows.append(_build_row(fut.result(), meta))
            except Exception as e:  # noqa: BLE001 — degrade one token, never the board
                rows.append({
                    "ticker": meta["ticker"], "category": meta.get("category", "?"),
                    "price": None, "chg24": None, "range_pct": None, "vol_m": 0.0,
                    "funding_pi": None, "oi_chg_pct": None, "ls_ratio": None,
                    "signals": [], "direction": "neutral", "tier": "dust",
                    "memo": meta.get("state", ""), "error": str(e),
                })
    rows.sort(key=lambda r: (-len(r["signals"]), -(r["vol_m"] or 0)))
    return rows


def compact_row(row):
    """SPEC-193: drop `leverage_state` (mostly UNKNOWN placeholders today, not a
    decision key — same call as classify/brief's compact) and any None-valued key;
    everything else on a triage row is already scalar/short-list, no further bulk to
    cut. `memo`/`signals` (the decision text) pass through UNCHANGED."""
    return {k: v for k, v in row.items() if k != "leverage_state" and v is not None}


def compact_board(rows, meta=None):
    return {"board": [compact_row(r) for r in rows], "meta": meta}


def render_human(board):
    """The colored one-glance cards (default view) — rendered from the contract dicts."""
    print(C.c("═══ CRIME TRIAGE ═══", "bold", "cyan")
          + C.c(f"  {len(board)} tokens · Binance+Bybit+Aster", "grey"))
    print(C.c("🔴 live(2+ sig)  🟠 watch(1)  ⚪ dust/skip   green=long red=short", "grey") + "\n")

    for r in board:
        dir_style = {"long": ("green",), "short": ("red",), "neutral": ("white",)}[r["direction"]]
        state = _short_state(r["memo"])
        tk = f"{r['ticker']:<8}"
        head = (f"{C.dot(r['tier'])} {C.c(tk, 'bold', *dir_style)} "
                f"{C.c('Cat ' + str(r['category']), 'grey')}  {C.c(state, *dir_style)}")
        if r["price"] is None:
            print(head + "\n   " + C.c("⚠ no Binance perp data", "grey") + "\n")
            continue
        ch, pos, vol_m = r["chg24"], r["range_pct"], r["vol_m"]
        ch_txt = C.sign_color(f"{ch:+.1f}%", ch)
        px_txt = C.c(f"${r['price']:.4f}", "bold")
        rng = f"{C.bar(pos)}{('%d%%' % pos) if pos is not None else ''}"
        line2 = f"   {px_txt}  {ch_txt}  {rng}  {C.c('vol $%.0fM' % vol_m, 'grey')}"

        f_show = r["funding_pi"]
        if f_show is not None:
            f_txt = C.sign_color(f"{f_show:+.3f}%/4h", f_show, flip=True)
            iv = r.get("funding_interval_min")
            raw = r.get("funding_raw_pi")
            if iv and iv != 240 and raw is not None:
                f_txt += C.c(f" (raw {raw:+.3f}%/{iv // 60 or 1}h)", "grey")
            rng = r.get("funding_range")
            if r.get("funding_venue") and rng:
                f_txt += C.c(f"({r['funding_venue'][:3]}|rng {rng[0]:+.2f}..{rng[1]:+.2f})", "grey")
        elif r.get("funding_suspect"):
            f_txt = C.c("SUSPECT(floor)", "grey")
        else:
            f_txt = C.c("—", "grey")
        oi = r["oi_chg_pct"]
        oi_txt = C.sign_color(f"{C.arrow(oi)}{oi:+.0f}%", oi) if oi is not None else C.c("·", "grey")
        ls = r["ls_ratio"]
        if ls is not None:
            ls_style = "green" if ls >= 1.30 else "red" if ls <= 0.90 else "grey"
            ls_txt = C.c(f"L/S {ls:.2f}", ls_style)
        else:
            ls_txt = C.c("L/S —", "grey")
        line3 = f"   fund {f_txt}  OI {oi_txt}  {ls_txt}"

        if r["signals"]:
            line4 = "   " + " ".join(C.c(s, "yellow") for s in r["signals"])
        else:
            line4 = f"   {C.c('· no signals', 'grey')}"
        print("\n".join([head, line2, line3, line4]) + "\n")

    print(C.c("─── ranking (signals, then vol) ───", "grey"))
    for r in board:
        tk = f"{r['ticker']:<8}"
        meta = f"{len(r['signals'])} sig · ${r['vol_m']:.0f}M"
        print(f"  {C.dot(r['tier'])} {C.c(tk, 'bold')} {C.c(meta, 'grey')}")


def main():
    p = argparse.ArgumentParser(description="Mode C compact crime triage")
    p.add_argument("tickers", nargs="*", help="optional ticker subset")
    p.add_argument("--json", action="store_true", help="emit JSON array (machine path)")
    p.add_argument("--color", action="store_true", help="force ANSI color on")
    p.add_argument("--no-color", action="store_true", help="force ANSI color off")
    p.add_argument("--render", choices=["full", "compact"], default="full",
                   help="SPEC-193: 'compact' drops leverage_state + null keys")
    args = p.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)

    if not WATCHLIST_PATH.exists():
        print(f"watchlist not found: {WATCHLIST_PATH}", file=sys.stderr)
        sys.exit(1)

    board = build_board(args.tickers or None)
    if args.json:
        # SPEC 64: {board, meta} envelope — meta.agents reports launchd watcher health
        meta = {"agents": agent_health()}
        if args.render == "compact":
            print(json.dumps(compact_board(board, meta)))
        else:
            print(json.dumps({"board": board, "meta": meta}))  # stdout = JSON ONLY
    else:
        render_human(board)
        h = agent_health()
        bad = {k: v for k, v in h.items() if v != "loaded"}
        if bad:
            print(C.c("\n  🚨 AGENTS: " + ", ".join(f"{k}={v}" for k, v in bad.items())
                      + "  (a MISSING watcher generates no alerts)", "bold", "red"))


if __name__ == "__main__":
    main()
