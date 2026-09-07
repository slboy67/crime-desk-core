#!/usr/bin/env python3
"""backtest_accumulation.py — accumulation-edge backtest harness (SPEC-100, the §9 GO/NO-GO gate).

`GOAL-close-gaps.md` G1: the accumulation-radar long edge (SPEC-76, the SIREN chip-control case,
the 0xaDFffc33 accumulator pattern) is n=1 and unbacktested — §9's hard gate (n≥10, hit%>50,
positive edge) blocks trading it at size. This harness measures whether verified accumulation →
perp construction → markup leads price with tradeable timing, and prints the literal GO/NO-GO.

Two event feeds:
  1. **manual** — `config/backtest_accumulation_events.json`, desk-verified cases sourced from
     memory (SIREN, XPIN, the 0xaDFffc33 cluster). Runs even before wide enumeration is affordable.
  2. **historical replay** — `source_historical_events` walks a candidate wallet's transfer
     history (SPEC-97 provider seam) and reuses `accumulation_radar.diff_holdings` (the SAME
     SPEC-76 NEW/GROWN criteria production uses) day-by-day to find the first date the radar
     WOULD have fired.

Per event: forward return @ +3d/+7d/+14d/+30d, MFE/MAE, time-to-first-+20%-leg, whether a
§6-style base+trigger entry existed after the signal. Aggregate: n, hit% (+7d return>0 AND
MFE>=2x MAE), avg 7d edge, lead-time distribution, and the literal §9 GO/NO-GO line. n<10 always
prints HYPOTHESIS-TIER, never GO. A markdown report lands in `reports/` for an auditable desk
decision. The desk does NOT size the edge until this prints GO.

  python3 capabilities/backtest_accumulation.py '{}' --json
"""
import argparse
import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from accumulation_radar import diff_holdings   # SPEC-76 — the SAME NEW/GROWN criteria, reused
from onchain import token_transfers            # SPEC-97 provider seam

MANUAL_EVENTS_PATH = ROOT / "config" / "backtest_accumulation_events.json"
REPORTS_DIR = ROOT / "reports"

HORIZONS = (3, 7, 14, 30)          # forward-return checkpoints, in days
DAY_BARS = 24                      # 1h bars per day (native desk bar, per counterfactual.py)
HIT_MFE_MULT = 2.0                 # hit needs MFE >= this multiple of MAE
MARKUP_LEG_PCT = 20.0              # "first +20% leg" = markup start
BASE_PULLBACK_PCT = 5.0            # §6-style base = a pullback of at least this % from a local high
ACCUM_WINDOW_DAYS = 7              # rolling baseline window for the historical replay (matches ACCUM_DAYS)

GATE_MIN_N = 10                    # §9 base-rate gate
GATE_MIN_HITPCT = 50.0
HIT_DEFINITION = f"+7d return > 0 AND MFE >= {HIT_MFE_MULT}x MAE"


# ───────────────────────── shared helpers ─────────────────────────
def _parse_ts(ts):
    """ISO-8601 (…Z) | epoch number | None → epoch seconds | None."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _iso(epoch_s):
    if epoch_s is None:
        return None
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ───────────────────────── 2a. historical replay of the SPEC-76 criteria ─────────────────────────
def replay_accumulation_signal(transfers, wallet, contract, window_days=ACCUM_WINDOW_DAYS):
    """Walk a wallet's inbound transfer history for `contract` day-by-day and find the first date
    the SPEC-76 radar criteria (`accumulation_radar.diff_holdings`: a NEW position, or one GROWN
    >= GROWTH_PCT vs a `window_days`-old rolling baseline) would have fired. Reuses production's
    own criteria function — do NOT reimplement the NEW/GROWN threshold here. Returns the epoch
    seconds of the day the signal fires, or None if it never does."""
    w = (wallet or "").lower()
    c = (contract or "").lower()
    rows = []
    for t in transfers or []:
        if (t.get("to_address") or "").lower() != w:
            continue
        if (t.get("address") or "").lower() != c:
            continue
        ts = _parse_ts(t.get("block_timestamp"))
        if ts is None:
            continue
        try:
            val = float(t.get("value_decimal") or 0)
        except (TypeError, ValueError):
            val = 0.0
        rows.append((ts, val))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    days = sorted({int(ts // 86400) for ts, _ in rows})
    for day in days:
        cutoff = day * 86400 + 86400              # end of this UTC day
        base_cutoff = cutoff - window_days * 86400
        holds_now = sum(v for ts, v in rows if ts < cutoff)
        holds_base = sum(v for ts, v in rows if ts < base_cutoff)
        baseline = {c: {"in_amount": holds_base}} if holds_base > 0 else {}
        diff = diff_holdings(baseline, {c: {"in_amount": holds_now}})
        if diff:
            return cutoff - 1
    return None


def _default_history_fn(wallet, contract, chain):
    txs, _src, _partial = token_transfers(wallet, contract, chain)
    return txs


def source_historical_events(candidates, tokentx_fn=None, window_days=ACCUM_WINDOW_DAYS):
    """Req 2a: for each candidate {wallet, contract, token, chain[, cluster]}, replay the SPEC-76
    criteria over its transfer history and emit an event at the first-fire date. Candidates with
    no signal in-window are skipped. Provider failures degrade to skip (never fabricate)."""
    tokentx_fn = tokentx_fn or _default_history_fn
    events = []
    for cand in candidates:
        wallet, contract = cand["wallet"], cand["contract"]
        token = cand.get("token") or contract[:10]
        chain = cand.get("chain") or "binance-smart-chain"
        try:
            transfers = tokentx_fn(wallet, contract, chain)
        except Exception:  # noqa: BLE001 — degrade-explicit, never fabricate a signal
            transfers = []
        sig_ts = replay_accumulation_signal(transfers, wallet, contract, window_days=window_days)
        if sig_ts is None:
            continue
        events.append({"token": token, "wallet": wallet, "contract": contract, "chain": chain,
                       "cluster": cand.get("cluster"), "signal_ts": sig_ts,
                       "signal_date": _iso(sig_ts), "source": "historical-replay"})
    return events


# ───────────────────────── 2b. manual fixtures ─────────────────────────
def load_manual_events(path=None):
    """Req 2b: the desk-verified fixtures file (SIREN, XPIN, 0xaDFffc33 cluster names …). Each
    entry needs {token, signal_date}; `signal_ts` is derived. Missing/unparsable file → []
    (the harness still runs on whatever feed it has)."""
    p = Path(path) if path else MANUAL_EVENTS_PATH
    try:
        data = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return []
    out = []
    for e in data.get("events", []):
        sig_ts = _parse_ts(e.get("signal_date"))
        if sig_ts is None:
            continue
        out.append({**e, "signal_ts": sig_ts, "source": e.get("source") or "manual"})
    return out


# ───────────────────────── 3. per-event outcome metrics ─────────────────────────
def find_base_trigger(win, entry_ref):
    """Req 3 (§6-style entry existence): after the signal, price must PULL BACK >=
    BASE_PULLBACK_PCT from a running local high (the base) and then RECLAIM that high (the
    trigger) — the same base+trigger shape §6 requires before an entry is live."""
    local_high = entry_ref
    in_pullback = False
    for b in win:
        c = b.get("close")
        if c is None:
            continue
        if not in_pullback:
            if c > local_high:
                local_high = c
            elif c <= local_high * (1 - BASE_PULLBACK_PCT / 100.0):
                in_pullback = True
        else:
            if c >= local_high:
                return True
    return False


def compute_outcome(bars):
    """Req 3: forward-return + MFE/MAE + time-to-markup + entry_existed metrics from the signal
    bar forward, on 1h bars (`bars[0]` = the signal/entry bar). Returns None on empty/bad input —
    the caller records the event as unscoreable, never fabricates zeros."""
    if not bars:
        return None
    entry_ref = bars[0].get("close") or bars[0].get("open")
    if not entry_ref:
        return None
    out = {"entry_ref": entry_ref}
    for d in HORIZONS:
        idx = d * DAY_BARS
        px = bars[idx].get("close") if idx < len(bars) else None
        out[f"return_{d}d_pct"] = round((px - entry_ref) / entry_ref * 100.0, 2) if px else None
    highs = [b["high"] for b in bars if b.get("high") is not None]
    lows = [b["low"] for b in bars if b.get("low") is not None]
    out["mfe_pct"] = round((max(highs) - entry_ref) / entry_ref * 100.0, 2) if highs else None
    out["mae_pct"] = round((entry_ref - min(lows)) / entry_ref * 100.0, 2) if lows else None
    thresh = entry_ref * (1 + MARKUP_LEG_PCT / 100.0)
    ttm = None
    for i, b in enumerate(bars):
        if (b.get("close") or 0) >= thresh:
            ttm = round(i / DAY_BARS, 2)
            break
    out["time_to_markup_days"] = ttm
    out["entry_existed"] = find_base_trigger(bars, entry_ref)
    return out


def is_hit(outcome):
    """Req 4 hit definition: +7d return > 0 AND MFE >= HIT_MFE_MULT x MAE. None (not evaluable,
    e.g. missing +7d data) is distinct from False — never silently counted as a miss."""
    if not outcome:
        return None
    r7, mfe, mae = outcome.get("return_7d_pct"), outcome.get("mfe_pct"), outcome.get("mae_pct")
    if r7 is None or mfe is None or mae is None:
        return None
    return bool(r7 > 0 and mfe >= HIT_MFE_MULT * mae)


# ───────────────────────── req 3 (cluster cascade) ─────────────────────────
def _pearson(a, b):
    n = len(a)
    if n < 2:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return None
    return cov / (va ** 0.5 * vb ** 0.5)


def co_cascade_correlation(events_scored):
    """Req 3: for cluster names (shared `cluster` tag), Pearson-correlate their forward-return
    series (the HORIZONS checkpoints) — the G1 'do cluster names cascade together' question.
    Clusters with <2 comparable (fully-scored) events return corr:None (not a fake 0)."""
    clusters = {}
    for e in events_scored:
        cl = e.get("cluster")
        if cl:
            clusters.setdefault(cl, []).append(e)
    out = {}
    for cl, evs in clusters.items():
        series = []
        for e in evs:
            o = e.get("outcome") or {}
            s = [o.get(f"return_{d}d_pct") for d in HORIZONS]
            if not any(v is None for v in s):
                series.append(s)
        if len(series) < 2:
            out[cl] = {"n": len(evs), "corr": None}
            continue
        rs = [r for r in (_pearson(a, b) for a, b in itertools.combinations(series, 2)) if r is not None]
        out[cl] = {"n": len(evs), "corr": round(sum(rs) / len(rs), 3) if rs else None}
    return out


# ───────────────────────── 4. aggregate + the literal §9 GO/NO-GO ─────────────────────────
def aggregate(events_scored):
    """Req 4: n / hit% / avg 7d edge / lead-time distribution + the literal §9 gate. n<GATE_MIN_N
    is ALWAYS HYPOTHESIS-TIER (never GO, regardless of hit%/edge on the small sample)."""
    scoreable = [e for e in events_scored if e.get("outcome")]
    n = len(scoreable)
    hits = [h for h in (is_hit(e["outcome"]) for e in scoreable) if h is not None]
    n_hit_evaluable = len(hits)
    hit_pct = round(100.0 * sum(hits) / n_hit_evaluable, 1) if n_hit_evaluable else None
    edges = [e["outcome"]["return_7d_pct"] for e in scoreable if e["outcome"].get("return_7d_pct") is not None]
    avg_edge = round(sum(edges) / len(edges), 2) if edges else None
    leads = [e["outcome"]["time_to_markup_days"] for e in scoreable if e["outcome"].get("time_to_markup_days") is not None]
    lead_dist = {"n": len(leads),
                 "median_days": round(sorted(leads)[len(leads) // 2], 2) if leads else None,
                 "min_days": round(min(leads), 2) if leads else None,
                 "max_days": round(max(leads), 2) if leads else None}

    fail = []
    if n < GATE_MIN_N:
        fail.append(f"n={n} < {GATE_MIN_N}")
    if hit_pct is None or hit_pct <= GATE_MIN_HITPCT:
        fail.append(f"hit%={hit_pct} <= {GATE_MIN_HITPCT}")
    if avg_edge is None or avg_edge <= 0:
        fail.append(f"avg_edge={avg_edge} <= 0")

    if n < GATE_MIN_N:
        verdict = "HYPOTHESIS-TIER"
        line = f"HYPOTHESIS-TIER (insufficient n): {'; '.join(fail)}"
    elif not fail:
        verdict = "GO"
        line = f"GO: n={n} hit%={hit_pct} avg_edge={avg_edge}%"
    else:
        verdict = "NO-GO"
        line = f"NO-GO: {'; '.join(fail)}"

    return {"n": n, "n_hit_evaluable": n_hit_evaluable, "hit_pct": hit_pct,
            "avg_edge_pct": avg_edge, "lead_time_days": lead_dist,
            "hit_definition": HIT_DEFINITION, "verdict": verdict, "verdict_line": line}


# ───────────────────────── 5. report artifact ─────────────────────────
def write_report(events_scored, agg, date_str=None, path=None):
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = Path(path) if path else (REPORTS_DIR / f"backtest_accumulation_{date_str}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    median_lead = agg["lead_time_days"]["median_days"]
    median_lead_str = f"{median_lead}d" if median_lead is not None else "n/a"
    lines = [f"# Accumulation-edge backtest — {date_str}", "",
             f"**{agg['verdict_line']}**", "",
             f"n={agg['n']} · hit%={agg['hit_pct']} (`{agg['hit_definition']}`) · "
             f"avg 7d edge={agg['avg_edge_pct']}% · median lead={median_lead_str}",
             "",
             "| token | source | signal_date | 3d | 7d | 14d | 30d | MFE | MAE | lead(d) | base+trigger | hit |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for e in events_scored:
        o = e.get("outcome") or {}
        hit = is_hit(o) if o else None
        lines.append("| " + " | ".join(str(x) for x in [
            e.get("token"), e.get("source"), e.get("signal_date"),
            o.get("return_3d_pct"), o.get("return_7d_pct"), o.get("return_14d_pct"), o.get("return_30d_pct"),
            o.get("mfe_pct"), o.get("mae_pct"), o.get("time_to_markup_days"),
            o.get("entry_existed"), hit]) + " |")
    out_path.write_text("\n".join(lines) + "\n")
    return str(out_path)


# ───────────────────────── composed harness ─────────────────────────
def _default_bars_provider(token, signal_ts):
    """Live 1h klines from `signal_ts` forward (Binance; price-only, no Moralis — G2-independent).
    Not unit-tested (network); tests inject a deterministic bars_provider."""
    import urllib.request
    sym = (token or "").upper().replace("USDT", "")
    start_ms = int(signal_ts * 1000) if signal_ts else None
    url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}USDT&interval=1h&limit=1000"
           + (f"&startTime={start_ms}" if start_ms else ""))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "backtest_accumulation/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            kl = json.loads(r.read())
        if not isinstance(kl, list):
            return None
        return [{"ts": k[0], "open": float(k[1]), "high": float(k[2]),
                 "low": float(k[3]), "close": float(k[4])} for k in kl]
    except Exception:  # noqa: BLE001
        return None


def build_backtest(manual_path=None, historical_candidates=None, tokentx_fn=None,
                    bars_provider=None, window_days=ACCUM_WINDOW_DAYS, write=True, date_str=None,
                    report_path=None):
    """Req 1: the composed harness. manual fixtures + (optional) historical replay candidates →
    per-event outcome metrics → aggregate + literal §9 GO/NO-GO → markdown report."""
    bars_provider = bars_provider or _default_bars_provider
    events = load_manual_events(manual_path)
    if historical_candidates:
        events += source_historical_events(historical_candidates, tokentx_fn=tokentx_fn, window_days=window_days)

    scored = []
    for e in events:
        try:
            bars = bars_provider(e["token"], e["signal_ts"])
        except Exception:  # noqa: BLE001 — a dead price feed degrades one event, not the batch
            bars = None
        scored.append({**e, "outcome": compute_outcome(bars) if bars else None})

    agg = aggregate(scored)
    cascades = co_cascade_correlation(scored)
    out_path = write_report(scored, agg, date_str=date_str, path=report_path) if write else None
    return {"events": scored, "aggregate": agg, "co_cascade": cascades, "report_path": out_path}


# ───────────────────────── CLI ─────────────────────────
def main():
    ap = argparse.ArgumentParser(description="backtest_accumulation — SPEC-100 §9 GO/NO-GO gate")
    ap.add_argument("payload", nargs="?", default="{}", help='JSON {manual_path?, write?}')
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        req = json.loads(args.payload)
    except ValueError as e:
        print(json.dumps({"error": f"unparseable payload: {e}"})); sys.exit(2)
    r = build_backtest(manual_path=req.get("manual_path"), write=req.get("write", True))
    print(json.dumps(r, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
