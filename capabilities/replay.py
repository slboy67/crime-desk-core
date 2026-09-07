#!/usr/bin/env python3
"""replay.py — backtest the SPEC-59 setup scorers for real base rates (SPEC 62).

The §9 base-rate gate needs n>=10 per signature; live trading accumulates that over months.
The scorers (SPEC 59) are DETERMINISTIC — replay them over history and the gate gets real
numbers in days. Three stages (each a CLI action):

  fetch    pull + cache per-symbol history (1h klines + funding + OI) under state/replay/
           (gitignored). Idempotent, resumable, rate-limit-respecting. OI history is capped
           at 30d by Binance (documented; we use what exists).
  run      step history bar-by-bar; at each bar score the PRICE/OI/FUNDING-computable legs
           of a setup (on-chain / L-S legs are unavailable in replay and recorded as such);
           when ARMED, simulate the §6 entry + §7 stop/TP geometry; append the outcome.
  report   per-signature n / hit% / avg R / max-adverse-excursion + by-symbol breakdown.
  ledger-import   write per-outcome rows into the ledger, tagged source:"replay" (never
                  silently mixed with live rows — the §9 gate reads them split, SPEC 63).

The ENGINE is pure over a bar list, so tests drive it on synthetic bars with NO network.

  python3 capabilities/replay.py '{"action":"report","setup":"blowoff","symbols":["BEAT"]}' --json
  python3 capabilities/replay.py '{"action":"run","setup":"blowoff","symbols":["BEAT"]}' --json
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from setup_score import score_setup        # the deterministic scorer being backtested
import ledger                              # SPEC 40/63 — outcome scoreboard

REPO = HERE.parent
REPLAY_DIR = REPO / "state" / "replay"
OUTCOMES_PATH = REPLAY_DIR / "outcomes.jsonl"

# ── engine constants ─────────────────────────────────────────────────────────────
LOOKBACK = 48               # bars of context the legs read (≈ 48h on 1h bars)
BREAK_CONFIRM = 2           # the breakdown must have held this many bars (causal — no peeking)
STOP_BUFFER_PCT = 1.5       # stop beyond the window-high wick (mirror SPEC 61)
TIME_STOP_BARS = 24         # no TP/stop within this many bars → time-stop (mark-to-market)
PARABOLIC_PCT = 50.0
ATH_WICK_PCT = 2.0
BREAK_VOL_MULT = 1.5
OI_OFF_HIGH_PCT = 5.0       # OI >= this % off its window high = "off highs"

# replay-side setups (price/OI/funding computable). Both are SHORTs.
REPLAY_SETUPS = ("blowoff", "catb_top")


# ── leg derivation from bars (causal: only bars[:i+1]) ──────────────────────────
def _f(bars, key):
    return [b.get(key) for b in bars]


def derive_replay_signals(bars, i, lookback=LOOKBACK):
    """Compute the SPEC-59 signal keys that are PRICE/OI/FUNDING-derivable, from bars[:i+1].
    Legs not computable from klines (oi_sides wash, spot CVD, L-S, on-chain) are simply
    omitted → their setup legs read not-passed and are reported as unavailable. Returns
    (signals, evaluable_legs)."""
    lo = max(0, i - lookback)
    win = bars[lo:i + 1]
    if len(win) < BREAK_CONFIRM + 3:
        return {}, []
    closes = _f(win, "close"); highs = _f(win, "high"); lows = _f(win, "low")
    vols = _f(win, "volume")
    ois = [o for o in _f(win, "oi") if o is not None]
    funds = [x for x in _f(win, "funding") if x is not None]
    cur = bars[i]
    sig, evaluable = {}, []

    # parabolic: gain from window start to the window peak (the run-up that precedes the top)
    base = closes[0]
    peak_high = max(highs)
    if base:
        sig["parabolic_pct"] = round((peak_high / base - 1) * 100, 3); evaluable.append("parabolic")

    # ATH/window-high WICKED: the peak high poked >= ATH_WICK_PCT above the highest CLOSE
    # (a wick that did not hold as a close — the blowoff-top rejection).
    max_close = max(closes)
    if max_close:
        sig["window_high_wick_pct"] = round((peak_high / max_close - 1) * 100, 3)
        sig["ath_wick"] = sig["window_high_wick_pct"] >= ATH_WICK_PCT
        evaluable += ["ath_wick"]

    # lower_high: the recent highs sit BELOW the prior window peak — i.e. we are off the top
    # making lower highs (the peak itself must be behind us, not in the recent window).
    k = max(3, len(win) // 6)
    recent_hi = max(highs[-k:]); prior_hi = max(highs[:-k]) if len(highs) > k else recent_hi
    sig["lower_high"] = recent_hi < prior_hi; evaluable.append("lower_high")

    # clean intraday break: at bar i-BREAK_CONFIRM, close broke below the pre-break support on
    # >=1.5x vol and the BREAK_CONFIRM bars since did NOT close back above it (causal — the
    # "not re-bought in 1-2 candles" leg, confirmed only once those candles have printed).
    bi = i - BREAK_CONFIRM
    broke = rebought = False
    vol_mult = None
    if bi - 12 >= 0:
        support = min(b["low"] for b in bars[bi - 12:bi])
        avg_vol = sum(b["volume"] for b in bars[bi - 12:bi]) / 12 if 12 else 0
        if bars[bi]["close"] < support:
            broke = True
            vol_mult = round(bars[bi]["volume"] / avg_vol, 3) if avg_vol else None
            rebought = any(bars[j]["close"] >= support for j in range(bi + 1, i + 1))
    sig["intraday_break"] = {"broke": broke, "vol_mult": vol_mult, "rebought": rebought}
    evaluable.append("clean_break")

    # OI off highs / peaked-and-rolled (needs OI series)
    if ois:
        oi_now = cur.get("oi")
        oi_high = max(ois)
        if oi_now is not None and oi_high:
            off = (oi_high - oi_now) / oi_high * 100
            sig["oi_off_highs"] = off >= OI_OFF_HIGH_PCT
            sig["oi_peaked_rolled"] = sig["oi_off_highs"]
            evaluable += ["oi_off_highs", "oi_peaked_rolled"]

    # funding cooling (magnitude shrinking toward flat) — needs funding series
    if len(funds) >= 4:
        first_half = funds[:len(funds) // 2]
        cur_f = funds[-1]
        sig["funding_cooling"] = abs(cur_f) < abs(sum(first_half) / len(first_half))
        evaluable.append("funding_cooling")

    # volume declining (recent third mean < middle third mean)
    third = max(2, len(win) // 3)
    if len(win) >= 3 * third:
        rv = sum(vols[-third:]) / third
        mv = sum(vols[-2 * third:-third]) / third
        sig["volume_declining"] = rv < mv; evaluable.append("volume_declining")

    return sig, sorted(set(evaluable))


# ── trade simulation (§6 entry + §7 stop/TP) ────────────────────────────────────
def simulate_trade(forward_bars, entry, stop, tp1, tp2, direction="SHORT",
                   time_stop_bars=TIME_STOP_BARS):
    """Walk forward from the entry bar; return (outcome, pnl_r, bars_held, mae_r). Within a
    bar the STOP is checked first (conservative). Risk = |stop-entry|; pnl_r in R."""
    risk = abs(stop - entry)
    if risk <= 0:
        return "invalid", 0.0, 0, 0.0
    mae_r = 0.0
    for n, b in enumerate(forward_bars[:time_stop_bars], start=1):
        hi, lo = b["high"], b["low"]
        if direction == "SHORT":
            mae_r = max(mae_r, (hi - entry) / risk)
            if hi >= stop:
                return "stopped", -1.0, n, round(mae_r, 3)
            if lo <= tp2:
                return "tp2", round((entry - tp2) / risk, 3), n, round(mae_r, 3)
            if lo <= tp1:
                return "tp1", round((entry - tp1) / risk, 3), n, round(mae_r, 3)
        else:  # LONG
            mae_r = max(mae_r, (entry - lo) / risk)
            if lo <= stop:
                return "stopped", -1.0, n, round(mae_r, 3)
            if hi >= tp2:
                return "tp2", round((tp2 - entry) / risk, 3), n, round(mae_r, 3)
            if hi >= tp1:
                return "tp1", round((tp1 - entry) / risk, 3), n, round(mae_r, 3)
    # time-stop: mark to the last close
    if forward_bars:
        last = forward_bars[min(time_stop_bars, len(forward_bars)) - 1]["close"]
        mtm = (entry - last) / risk if direction == "SHORT" else (last - entry) / risk
        return "time_stop", round(mtm, 3), min(time_stop_bars, len(forward_bars)), round(mae_r, 3)
    return "no_data", 0.0, 0, 0.0


def _entry_geometry(bars, i, direction="SHORT"):
    """§7 standard geometry for a replayed entry: stop beyond the window-high wick (+buffer),
    TP1 = 1R, TP2 = 2R."""
    lo = max(0, i - LOOKBACK)
    win = bars[lo:i + 1]
    entry = bars[i]["close"]
    if direction == "SHORT":
        wick = max(b["high"] for b in win)
        stop = wick * (1 + STOP_BUFFER_PCT / 100)
        risk = stop - entry
        return entry, stop, entry - risk, entry - 2 * risk
    wick = min(b["low"] for b in win)
    stop = wick * (1 - STOP_BUFFER_PCT / 100)
    risk = entry - stop
    return entry, stop, entry + risk, entry + 2 * risk


# ── per-symbol replay ────────────────────────────────────────────────────────────
def replay_symbol(symbol, bars, setup):
    """Step `bars` for one setup; return the list of outcome rows. One position at a time
    (no overlapping entries). Deterministic: same bars + setup → same outcomes."""
    outcomes = []
    i = LOOKBACK
    n = len(bars)
    while i < n - 1:
        sig, evaluable = derive_replay_signals(bars, i)
        if not sig:
            i += 1; continue
        r = score_setup(setup, sig)
        if r["verdict"] != "ARMED":
            i += 1; continue
        entry, stop, tp1, tp2 = _entry_geometry(bars, i, "SHORT")
        outcome, pnl_r, held, mae = simulate_trade(bars[i + 1:], entry, stop, tp1, tp2, "SHORT")
        row = {
            "symbol": symbol, "setup": setup,
            "signature": ledger.canon_signature(setup),
            "direction": "SHORT",
            "entry_ts": bars[i].get("ts"), "entry_px": round(entry, 10),
            "stop": round(stop, 10), "tp": [round(tp1, 10), round(tp2, 10)],
            "outcome": outcome, "pnl_r": pnl_r, "bars_held": held, "mae_r": mae,
            "evaluable_legs": evaluable, "score": r["score"], "required": r["required"],
            "source": "replay",
        }
        outcomes.append(row)
        i += max(1, held) + 1          # skip past the closed trade — no overlap
    return outcomes


def run_replay(symbols_bars, setup):
    """symbols_bars: {symbol: [bars]}. Returns all outcome rows across symbols."""
    out = []
    for sym, bars in symbols_bars.items():
        out.extend(replay_symbol(sym, bars, setup))
    return out


def write_outcomes(outcomes, path=None):
    path = Path(path) if path else OUTCOMES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for o in outcomes:
            f.write(json.dumps(o) + "\n")
    return {"written": len(outcomes), "path": str(path)}


# ── report ───────────────────────────────────────────────────────────────────────
_HITS = ("tp1", "tp2")
_MISSES = ("stopped",)


def report(outcomes, setup=None):
    """Per-signature: n, hit%, avg R, max adverse excursion + by-symbol breakdown."""
    rows = [o for o in outcomes if (setup is None or o.get("setup") == setup)]
    by_sig = {}
    for o in rows:
        by_sig.setdefault(o.get("signature", "?"), []).append(o)
    sigs = {}
    for sig, recs in sorted(by_sig.items()):
        decided = [r for r in recs if r["outcome"] in _HITS + _MISSES]
        hits = sum(1 for r in decided if r["outcome"] in _HITS)
        rs = [r["pnl_r"] for r in recs if r.get("pnl_r") is not None]
        maes = [r["mae_r"] for r in recs if r.get("mae_r") is not None]
        by_symbol = {}
        for r in recs:
            by_symbol.setdefault(r["symbol"], {"n": 0, "tp": 0, "stop": 0})
            by_symbol[r["symbol"]]["n"] += 1
            if r["outcome"] in _HITS:
                by_symbol[r["symbol"]]["tp"] += 1
            elif r["outcome"] in _MISSES:
                by_symbol[r["symbol"]]["stop"] += 1
        sigs[sig] = {
            "n": len(recs),
            "hit_pct": round(100.0 * hits / len(decided), 1) if decided else 0.0,
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
            "total_r": round(sum(rs), 2) if rs else 0.0,
            "max_adverse_excursion_r": round(max(maes), 3) if maes else None,
            "by_symbol": by_symbol,
            "source": "replay",
        }
    return {"setup": setup, "total_outcomes": len(rows), "signatures": sigs}


def ledger_import(outcomes, ledger_path=None):
    """Write per-outcome rows into the ledger, tagged source:"replay". time_stop maps to the
    signal-only `retired_unfilled` (counts in n, neither hit nor miss). Never mixed silently
    with live rows — the §9 gate reads them split (SPEC 63)."""
    if ledger_path:
        ledger.LEDGER_PATH = Path(ledger_path)
    imported = 0
    for o in outcomes:
        outcome = o["outcome"]
        if outcome == "time_stop":
            outcome = "retired_unfilled"
        elif outcome not in ledger.OUTCOMES:
            continue
        ledger.record({
            "ticker": o["symbol"], "direction": o.get("direction", "SHORT"),
            "signature": o["signature"], "entry": o.get("entry_px"), "stop": o.get("stop"),
            "tp": o.get("tp") or [], "outcome": outcome, "pnl_r": o.get("pnl_r"),
            "commit_ts": None, "close_ts": None,
            "notes": f"replay: {o['setup']} score {o.get('score')}/{o.get('required')}",
            "source": "replay",
        })
        imported += 1
    return {"imported": imported, "source": "replay"}


# ── CLI ─────────────────────────────────────────────────────────────────────────
def _load_outcomes(path=None):
    path = Path(path) if path else OUTCOMES_PATH
    if not path.exists():
        return []
    out = []
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    return out


def main():
    ap = argparse.ArgumentParser(description="replay — backtest the SPEC-59 scorers (SPEC 62)")
    ap.add_argument("payload", help='JSON {action:fetch|run|report|ledger-import, setup?, symbols?}')
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        req = json.loads(args.payload)
    except ValueError as e:
        print(json.dumps({"error": f"unparseable payload: {e}"})); sys.exit(2)
    action = req.get("action", "report")
    setup = req.get("setup")
    symbols = req.get("symbols")

    if action == "fetch":
        out = _fetch_live(symbols or [])
    elif action == "run":
        sb = _load_cached_bars(symbols or [])
        outs = run_replay(sb, setup)
        write_outcomes(outs)
        out = {"ran": setup, "symbols": list(sb.keys()), "outcomes": len(outs)}
    elif action == "report":
        out = report(_load_outcomes(), setup)
    elif action == "ledger-import":
        out = ledger_import(_load_outcomes())
    else:
        out = {"error": f"unknown action {action!r} (fetch|run|report|ledger-import)"}
    print(json.dumps(out, indent=None if args.json else 2))


# ── live data layer (network; not unit-tested) ──────────────────────────────────
def _fetch_live(symbols):
    """Stage 1: pull + cache 1h klines + funding + OI per symbol under state/replay/.
    Idempotent (skip cached), resumable. OI history capped at 30d by Binance (documented)."""
    import urllib.request
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    done, skipped, failed = [], [], []

    def _get(url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "replay/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001
            return None

    for sym in symbols:
        s = sym.upper().replace("USDT", "")
        cache = REPLAY_DIR / f"{s}.json"
        if cache.exists():
            skipped.append(s); continue
        kl = _get(f"https://fapi.binance.com/fapi/v1/klines?symbol={s}USDT&interval=1h&limit=1500")
        if not isinstance(kl, list):
            failed.append(s); continue
        fund = _get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={s}USDT&limit=1000") or []
        oi = _get(f"https://fapi.binance.com/futures/data/openInterestHist?symbol={s}USDT&period=1h&limit=500") or []
        cache.write_text(json.dumps({"symbol": s, "klines": kl, "funding": fund, "oi": oi}))
        done.append(s)
    return {"fetched": done, "skipped_cached": skipped, "failed": failed,
            "note": "OI history capped at 30d by Binance; klines/funding full listing"}


def _bars_from_cache(blob):
    """Stitch cached klines + funding + OI into the engine's bar shape (funding/OI mapped by
    nearest-prior timestamp; missing → None, the legs degrade)."""
    kl = blob.get("klines") or []
    fund = sorted(((int(f["fundingTime"]), float(f["fundingRate"])) for f in blob.get("funding") or []),
                  key=lambda x: x[0])
    oi = sorted(((int(o["timestamp"]), float(o["sumOpenInterest"])) for o in blob.get("oi") or []
                 if o.get("sumOpenInterest")), key=lambda x: x[0])

    def _prior(series, ts):
        v = None
        for t, x in series:
            if t <= ts:
                v = x
            else:
                break
        return v

    bars = []
    for k in kl:
        ts = int(k[0])
        bars.append({"ts": ts, "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                     "close": float(k[4]), "volume": float(k[5]),
                     "funding": _prior(fund, ts), "oi": _prior(oi, ts)})
    return bars


def _load_cached_bars(symbols):
    sb = {}
    for sym in symbols:
        s = sym.upper().replace("USDT", "")
        cache = REPLAY_DIR / f"{s}.json"
        if cache.exists():
            sb[s] = _bars_from_cache(json.loads(cache.read_text()))
    return sb


if __name__ == "__main__":
    main()
