#!/usr/bin/env python3
"""ops/oi_sampler.py — SPEC-178: desk-run OI sampler.

Bitget/MEXC/KuCoin/Aster/Hyperliquid/Lighter publish NO keyless OI HISTORY (only a
current snapshot — R1 S3), and Binance silently caps its own history at ~30 days.
OI-elasticity (arb-vs-directional discrimination, the heart of the OI-construction
layer §0.6 item 3) is blind exactly where Cat-A OI lives until the desk samples and
stores OI itself.

One tick = for each symbol in the board-driven universe, call
`venue_map.build_venue_map(ticker)` and append one row per venue to
`state/oi_samples/<SYM>.jsonl`. Zero new fetch code — the sampler IS venue_map +
append, inheriting its ok/not_listed/error discipline and per-venue timeouts.

  python3 ops/oi_sampler.py tick --json

Universe (req 2): board-driven, capped at UNIVERSE_CAP — the union of watchlist
tickers carrying a live (non-retired/non-closed) thesis, config/positions.json open
positions, and the most recent faded-bounce sweep's candidates, recomputed every
tick. Over-cap: live theses first, then positions, then candidates (in that priority
order — a ticker counts once, at its highest-priority slot).

Store (req 3): append-only JSONL per symbol, one row per venue per tick:
  {ts, venue, status, oi_raw, oi_usd, mark_price, funding_pi_4h, vol24h_usd}
Raw AND USD both — a venue's contract-size/multiplier redenomination breaks a
raw-unit series in a way a USD-only series would hide, and USD-only hides it the
other direction (a real OI move painted over by a mark-price move). Venue errors
append an explicit `status:"error"` row — a missing tick is a GAP (never
interpolated, never read as flat OI); this module records only what happened, never
fabricates a filler row for a tick it didn't run.

Redenomination marker (req 6): a step-change (beyond REDENOM_STEP_TOLERANCE) in the
oi_usd/oi_raw ratio versus the venue's own last `status:"ok"` row writes a
`status:"redenomination"` marker row instead of silently corrupting the raw series —
consumers must split the series at that point, never read a fake OI cliff.
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "capabilities"))
import venue_map as VM  # noqa: E402

STORE_DIR = ROOT / "state" / "oi_samples"
WATCHLIST_PATH = ROOT / "config" / "watchlist.json"
POSITIONS_PATH = ROOT / "config" / "positions.json"
FADED_BOUNCE_PATH = ROOT / "state" / "faded_bounce_latest.json"
PRUNE_MARKER = ROOT / "state" / ".oi_sampler_prune_last"

UNIVERSE_CAP = 30
RETENTION_DAYS = 90
PRUNE_INTERVAL_DAYS = 7          # weekly, marker-gated like discovery_tick's due() pattern
REDENOM_STEP_TOLERANCE = 3.0     # a >=3x (or <=1/3x) step in the oi_usd/oi_raw ratio vs the
                                  # venue's prior ok row is a redenomination, not organic drift

TERMINAL_THESIS_STATUSES = ("RETIRED", "CLOSED")


# ---------------------------------------------------------------------------
# Universe (req 2)
# ---------------------------------------------------------------------------

def _live_thesis_tickers(wl_path=None):
    """Watchlist tickers carrying a thesis whose status is NOT terminal (RETIRED/
    CLOSED) — 'live' in the CLAUDE.md §0.5 sense. Missing/corrupt file degrades to
    an empty tier, never an exception (§3 — a dead universe leg shrinks the sweep,
    never kills the tick)."""
    try:
        wl = json.loads((wl_path or WATCHLIST_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    toks = wl["tokens"] if isinstance(wl, dict) else wl
    out = []
    for t in toks or []:
        th = t.get("thesis")
        if not th or th.get("status") in TERMINAL_THESIS_STATUSES:
            continue
        tk = (t.get("ticker") or "").upper()
        if tk:
            out.append(tk)
    return out


def _open_position_tickers(pos_path=None):
    try:
        d = json.loads((pos_path or POSITIONS_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [(p.get("ticker") or "").upper() for p in (d.get("positions") or []) if p.get("ticker")]


def _faded_bounce_candidate_tickers(fb_path=None):
    p = fb_path or FADED_BOUNCE_PATH
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    d = d.get("data", d) if isinstance(d, dict) else {}
    rows = d.get("candidates") or []
    return [(r.get("ticker") or "").upper() for r in rows if r.get("ticker")]


def build_universe(wl_path=None, pos_path=None, fb_path=None, cap=UNIVERSE_CAP):
    """Union of the three tiers, recomputed fresh every call (req 2) — never a
    persisted list. Over-cap enforcement order: live theses > positions > candidates;
    a ticker appearing in more than one tier counts once, at its highest-priority
    slot (so a live-thesis name is never pushed out of the cap by a lower-priority
    duplicate)."""
    tiers = (
        _live_thesis_tickers(wl_path),
        _open_position_tickers(pos_path),
        _faded_bounce_candidate_tickers(fb_path),
    )
    seen, out = set(), []
    for tier in tiers:
        for tk in tier:
            if tk and tk not in seen:
                seen.add(tk)
                out.append(tk)
    return out[:cap]


# ---------------------------------------------------------------------------
# Store (req 3) + redenomination detection (req 6)
# ---------------------------------------------------------------------------

def _store_path(ticker, store_dir=None):
    d = store_dir or STORE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{ticker.upper()}.jsonl"


def _read_rows(ticker, store_dir=None):
    p = _store_path(ticker, store_dir=store_dir)
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
            continue   # a corrupt line is dropped on read, never propagated further
    return rows


def _last_ok_row(ticker, venue, store_dir=None):
    """Most recent `status:"ok"` row for (ticker, venue) — the redenomination
    detector's baseline."""
    last = None
    for row in _read_rows(ticker, store_dir=store_dir):
        if row.get("venue") == venue and row.get("status") == "ok":
            last = row
    return last


def _append_rows(ticker, rows, store_dir=None):
    p = _store_path(ticker, store_dir=store_dir)
    with p.open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _redenomination_marker(ticker, venue, ts, prev_row, cur_row):
    """A >=REDENOM_STEP_TOLERANCE (or <=1/tolerance) step in the oi_usd/oi_raw ratio
    vs the venue's own last ok row -> a marker row, not a fabricated flat/cliff read
    in the raw series. None when either row lacks both oi_raw and oi_usd, or the
    step is within tolerance."""
    if not prev_row or not cur_row:
        return None
    try:
        prev_raw, prev_usd = float(prev_row.get("oi_raw") or 0), float(prev_row.get("oi_usd") or 0)
        cur_raw, cur_usd = float(cur_row.get("oi_raw") or 0), float(cur_row.get("oi_usd") or 0)
    except (TypeError, ValueError):
        return None
    if not (prev_raw and prev_usd and cur_raw and cur_usd):
        return None
    prev_ratio, cur_ratio = prev_usd / prev_raw, cur_usd / cur_raw
    if prev_ratio <= 0 or cur_ratio <= 0:
        return None
    step = cur_ratio / prev_ratio
    if step >= REDENOM_STEP_TOLERANCE or step <= 1.0 / REDENOM_STEP_TOLERANCE:
        return {"ts": ts, "venue": venue, "status": "redenomination",
               "prev_ratio": round(prev_ratio, 8), "cur_ratio": round(cur_ratio, 8),
               "step": round(step, 4)}
    return None


def sample_one(ticker, ts=None, venue_map_fn=None, store_dir=None):
    """One symbol's tick: venue_map sweep -> one row per venue -> append, with the
    per-venue redenomination check run BEFORE appending (so it compares against the
    prior tick's row, not this tick's). Returns the rows written."""
    ts = ts if ts is not None else int(time.time())
    vm_fn = venue_map_fn or VM.build_venue_map
    result = vm_fn(ticker)
    rows = []
    for venue, block in (result.get("venues") or {}).items():
        status = block.get("status")
        row = {"ts": ts, "venue": venue, "status": status}
        if status == "ok":
            row["oi_raw"] = block.get("oi_raw")
            row["oi_usd"] = block.get("oi_usd")
            row["mark_price"] = block.get("mark_price")
            row["funding_pi_4h"] = block.get("funding_pi_4h")
            row["vol24h_usd"] = block.get("vol24h_usd")
            marker = _redenomination_marker(ticker, venue, ts,
                                            _last_ok_row(ticker, venue, store_dir=store_dir), row)
            if marker:
                rows.append(marker)
        elif status == "error":
            row["reason"] = block.get("reason")
        rows.append(row)
    _append_rows(ticker, rows, store_dir=store_dir)
    return rows


# ---------------------------------------------------------------------------
# Retention (req 5)
# ---------------------------------------------------------------------------

def _prune_due(marker_path=None, interval_days=PRUNE_INTERVAL_DAYS, now_ts=None):
    m = marker_path or PRUNE_MARKER
    if not m.exists():
        return True
    now_ts = now_ts if now_ts is not None else time.time()
    return (now_ts - m.stat().st_mtime) >= interval_days * 86400


def prune_old_rows(retention_days=RETENTION_DAYS, now_ts=None, store_dir=None):
    """Drop rows older than `retention_days` from every symbol's JSONL, in place.
    The file stays valid JSONL throughout (rewritten only when something changed);
    a malformed line encountered while pruning is dropped, never propagated."""
    now_ts = now_ts if now_ts is not None else time.time()
    cutoff = now_ts - retention_days * 86400
    d = store_dir or STORE_DIR
    if not d.exists():
        return {"pruned_files": 0}
    n_pruned = 0
    for p in sorted(d.glob("*.jsonl")):
        kept, changed = [], False
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                changed = True
                continue
            if row.get("ts", cutoff) >= cutoff:
                kept.append(row)
            else:
                changed = True
        if changed:
            p.write_text("".join(json.dumps(r) + "\n" for r in kept))
            n_pruned += 1
    return {"pruned_files": n_pruned}


def maybe_prune(marker_path=None, retention_days=RETENTION_DAYS, now_ts=None, store_dir=None):
    if not _prune_due(marker_path=marker_path, now_ts=now_ts):
        return {"skipped": True}
    result = prune_old_rows(retention_days=retention_days, now_ts=now_ts, store_dir=store_dir)
    (marker_path or PRUNE_MARKER).parent.mkdir(parents=True, exist_ok=True)
    (marker_path or PRUNE_MARKER).touch()
    return {"skipped": False, **result}


# ---------------------------------------------------------------------------
# Tick orchestration
# ---------------------------------------------------------------------------

def run_tick(cap=UNIVERSE_CAP, ts=None, venue_map_fn=None, wl_path=None, pos_path=None,
            fb_path=None, store_dir=None, marker_path=None):
    universe = build_universe(wl_path=wl_path, pos_path=pos_path, fb_path=fb_path, cap=cap)
    ts = ts if ts is not None else int(time.time())
    sampled, errors = 0, []
    for tk in universe:
        try:
            sample_one(tk, ts=ts, venue_map_fn=venue_map_fn, store_dir=store_dir)
            sampled += 1
        except Exception as e:  # noqa: BLE001 — one symbol's crash never kills the tick
            errors.append({"ticker": tk, "error": str(e)})
    prune_result = maybe_prune(marker_path=marker_path, store_dir=store_dir, now_ts=ts)
    return {"universe": universe, "sampled": sampled, "errors": errors, "ts": ts,
           "prune": prune_result}


def main():
    ap = argparse.ArgumentParser(description="SPEC-178 — desk-run OI sampler")
    ap.add_argument("cmd", choices=["tick"])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    r = run_tick()
    if args.json:
        print(json.dumps(r))
    else:
        print(f"oi_sampler tick: {r['sampled']}/{len(r['universe'])} sampled, "
             f"{len(r['errors'])} error(s), prune={r['prune']}")


if __name__ == "__main__":
    main()
