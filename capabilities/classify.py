#!/usr/bin/env python3
"""classify.py — State-machine classifier: maps live data + trigger logs onto each
committed thesis and returns exactly ONE of CONFIRMS / TRIGGERS / BREAKS per token.

Precedence (highest first): BREAKS > TRIGGERS > CONFIRMS.

Usage:
  python3 scripts/classify.py                    # full board (sorted BREAKS->TRIGGERS->CONFIRMS)
  python3 scripts/classify.py --breaks           # only BREAKS + TRIGGERS
  python3 scripts/classify.py BILL               # single token detail
  python3 scripts/classify.py BILL --json        # machine-readable JSON
  python3 scripts/classify.py --migrate          # bootstrap thesis blocks from state+regime
  python3 scripts/classify.py --migrate --dry-run  # preview without writing
  python3 scripts/classify.py --arm BILL         # print arm_setup.py command(s) for this thesis
"""
import json, re, sys, time, glob, shutil
from datetime import datetime, timezone
from pathlib import Path

# ── paths ────────────────────────────────────────────────────────────────────
REPO      = Path(__file__).resolve().parent.parent
STATE_DIR = REPO / "state"

# ── import regime_flip helpers (no duplication) ──────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from regime_flip import (
    live_perp, classify as rf_classify, fetch,
    memo_direction, memo_zone, memo_funding_sign,
    sign_of, FLAT_BAND, DEEP_NEG, LIQ_GATE_M,
    load_venue_snapshot,
)
from arm_setup import TIME_STOP
from desk import FIRE_RE
import inbox   # SPEC 45: unconsumed surveillance alerts ride along on every board row
import oi_mc as OM   # SPEC-122: OI/MC "perp-casino" flag
import oi_construction as OC   # SPEC-180: the oic: compact board field
import aster_listing as AL   # SPEC-136: execution-venue (Aster) fillability veto
import thesis as TH   # SPEC-146: the ONE typed thesis-geometry parser + WL_PATH owner
import ledger as LG   # SPEC-149: canon_signature + the ledger's earned-set for `tier`

WL_PATH = TH.WL_PATH   # SPEC-146 req 4: thesis.py owns the path; classify re-exports it
                       # (board callers + tests still read classify.WL_PATH)

# ── constants ─────────────────────────────────────────────────────────────────
VERDICT_BREAKS   = "BREAKS"
VERDICT_TRIGGERS = "TRIGGERS"
VERDICT_WATCH_ARMED = "WATCH-ARMED"   # SPEC 77: a monitor-only WATCH trip-wire crossed — NOT a
                                      # committed-position move; kept distinct from BREAKS/TRIGGERS
VERDICT_CONFIRMS = "CONFIRMS"

# Canonical set of every verdict `classify` can emit. Exported so tests (and any
# downstream reader) derive their expected-verdict set from the engine rather than
# re-hardcoding a literal that drifts (SPEC 86 — WATCH-ARMED was missing from the
# test copy, reddening every premerge whenever a watch-leg name was armed).
VERDICTS = frozenset({
    VERDICT_BREAKS,
    VERDICT_TRIGGERS,
    VERDICT_WATCH_ARMED,
    VERDICT_CONFIRMS,
})

# ── classify/board config block ───────────────────────────────────────────────
DRIFT_PCT = 8.0   # SPEC-91: a committed thesis is STALE when live price has run >= this %
                  # past its WHOLE committed structure (nearest anchor, or beyond the furthest
                  # TP once all printed). Distances are measured relative to the anchor price.

# Keyword map for --migrate setup detection
SETUP_KEYWORDS = {
    "blowoff":      "blowoff_top",
    "blowoff_top":  "blowoff_top",
    "stage-5":      "stage5",
    "stage5":       "stage5",
    "stage 5":      "stage5",
    "squeeze-fuel": "squeeze_fuel",
    "squeeze_fuel": "squeeze_fuel",
    "trap-formation": "squeeze_fuel",
    "scalp":        "scalp",
}

# FIRE_RE from desk.py already imported — but the spec wants us to recognise
# sub-categories of fire lines for TRIGGERS vs BREAKS.
ENTRY_RE    = re.compile(r"🔴 ENTRY|🎯|✅ ARMED|✅ STAGE")
BREAK_RE    = re.compile(r"🚨 INVALIDATED|🚨🚨|⚠ FUNDING-GUARD|⏰ TIME-STOP|RE-GATED")


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_watchlist():
    try:
        raw = json.loads(WL_PATH.read_text())
    except Exception as e:
        return [], str(e)
    tokens = raw.get("tokens", raw) if isinstance(raw, dict) else raw
    return tokens, None


# SPEC-146: direction/entry_zone/committed_epoch/time_stop used to be four independently
# re-derived readers here (thesis_direction, thesis_entry_zone, thesis_committed_epoch,
# thesis_time_stop) — now a single `thesis.parse(tok)` call. Callers hold onto the
# returned Thesis and read `.direction` / `.zone` / `.committed_epoch` / `.time_stop_h`
# directly (grep for `TH.parse(` below).


_LINE_TS_RE = re.compile(r'(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}:\d{2}))?')

def _line_epoch(ln, file_mtime):
    """Best-effort timestamp for a log line: a full date in the line, else the
    file's mtime (covers heartbeat lines like '[HH:MM:SS]' that carry no date)."""
    m = _LINE_TS_RE.search(ln)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2) or '00:00:00'}",
                                     "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            pass
    return file_mtime

def scan_trigger_logs(ticker, since_epoch=None):
    """Scan state/*_trigger.log (and /tmp/*.log) for actionable lines.
    Skips lines older than since_epoch — a fire from a PRIOR thesis must not
    classify the current one (e.g. a 2026-05-28 KITE break vs a 2026-06-02 thesis).
    Returns (entry_lines, break_lines) as lists of (path, line) tuples.
    Matches logs whose filename stem contains the ticker as a whole token
    (delimited by '_', '-', start, or end of stem) — avoids 'h' matching 'kite_short'."""
    ticker_lc = ticker.lower()
    # compile a pattern: ticker appears as a standalone word in the filename stem
    _ticker_re = re.compile(r'(?<![a-z0-9])' + re.escape(ticker_lc) + r'(?![a-z0-9])')
    entry_lines = []
    break_lines  = []

    patterns = list(STATE_DIR.glob("*.log")) + [Path(p) for p in glob.glob("/tmp/*.log")]
    for path in patterns:
        stem = path.stem.lower()  # e.g. "kite_short_trigger" — NOT "h_short_trigger"
        if not _ticker_re.search(stem):
            continue
        try:
            content = path.read_text(errors="replace").splitlines()
            mtime = path.stat().st_mtime
        except Exception:
            continue
        for ln in content:
            if since_epoch is not None and _line_epoch(ln, mtime) < since_epoch:
                continue  # stale fire from a prior thesis — ignore
            if ENTRY_RE.search(ln):
                entry_lines.append((str(path), ln.strip()))
            elif BREAK_RE.search(ln):
                break_lines.append((str(path), ln.strip()))

    return entry_lines, break_lines


# ─────────────────────────────────────────────────────────────────────────────
# SPEC 64 — surveillance dead-man switch
#
# The nonce-surveil launchd agent was unloaded for NINE DAYS and nothing surfaced it;
# the Designer kept reading "surveillance armed" off a board whose watcher was dead. The
# board must be physically unreadable without seeing the watcher is dead: when the surveil
# layer's newest observable tick is older than 2× its launchd cadence, every board row's
# reason is prefixed `[SURVEIL STALE Nh]` and ONE HIGH inbox event fires per stale-episode.
# ─────────────────────────────────────────────────────────────────────────────
NONCE_LOG          = STATE_DIR / "nonce_alerts.log"      # ESCALATION/SWEEP_ERROR lines
SURVEIL_HEARTBEAT  = STATE_DIR / "surveil.heartbeat"     # quiet-tick heartbeat (surveil.sh)
BOARD_BASELINE     = STATE_DIR / "board_last.json"       # board_tick's baseline (SPEC 46)
DEADMAN_CURSOR     = STATE_DIR / "surveil_deadman.json"  # once-per-episode fire dedup
SURVEIL_CADENCE_H  = 0.25     # 15-min launchd cadence (ops/com.crimedesk.nonce-surveil.plist)
SURVEIL_STALE_MULT = 2        # stale when age > 2× cadence (= 30 min)


def _newest_log_epoch(path):
    """Newest ISO timestamp across a log file's lines, or None when absent/empty."""
    try:
        if not path.exists():
            return None
        lines = path.read_text(errors="replace").splitlines()
        mtime = path.stat().st_mtime
    except OSError:
        return None
    newest = None
    for ln in lines:
        if not _LINE_TS_RE.search(ln):
            continue
        ep = _line_epoch(ln, mtime)
        if newest is None or ep > newest:
            newest = ep
    return newest


def surveil_status(now=None):
    """Dead-man read on the nonce-surveil layer: age since its newest observable tick —
    an ESCALATION line in nonce_alerts.log OR a quiet heartbeat. stale when that age
    exceeds 2× the launchd cadence. last_epoch None (the layer never ran / no state) is
    stale-by-absence but carries no episode to bound an inbox fire (triage reports it)."""
    now = now if now is not None else time.time()
    epochs = [e for e in (_newest_log_epoch(NONCE_LOG), _newest_log_epoch(SURVEIL_HEARTBEAT))
              if e is not None]
    last = max(epochs) if epochs else None
    thr = SURVEIL_CADENCE_H * SURVEIL_STALE_MULT
    if last is None:
        return {"age_h": None, "stale": True, "last_epoch": None, "threshold_h": thr}
    age_h = (now - last) / 3600
    return {"age_h": round(age_h, 1), "stale": age_h > thr, "last_epoch": last,
            "threshold_h": thr}


def board_tick_status(now=None):
    """Same dead-man read on board_tick's baseline (state/board_last.json mtime). The
    file existing == the agent ticked at least once (≈loaded); absent → not run, left to
    `triage` agent-health to report MISSING rather than flagged stale here."""
    now = now if now is not None else time.time()
    try:
        if not BOARD_BASELINE.exists():
            return {"age_h": None, "stale": False, "last_epoch": None}
        last = BOARD_BASELINE.stat().st_mtime
    except OSError:
        return {"age_h": None, "stale": False, "last_epoch": None}
    age_h = (now - last) / 3600
    return {"age_h": round(age_h, 1),
            "stale": age_h > SURVEIL_CADENCE_H * SURVEIL_STALE_MULT, "last_epoch": last}


def _deadman_prefix(sv, bt):
    bits = []
    if sv["stale"]:
        bits.append(f"[SURVEIL STALE {sv['age_h']:g}h]" if sv["age_h"] is not None
                    else "[SURVEIL STALE ?h]")
    if bt["stale"]:
        bits.append(f"[BOARD-TICK STALE {bt['age_h']:g}h]")
    return (" ".join(bits) + " ") if bits else ""


def _fire_deadman_event(kind, status, now_iso):
    """Fire ONE HIGH inbox event per stale-episode, deduped on last_epoch via a cursor
    file — never per-board-read spam. No last_epoch (layer never ran) → no episode → no
    event. A write/inbox failure degrades silently (the prefix already warns the board)."""
    if status["last_epoch"] is None:
        return False
    try:
        cur = json.loads(DEADMAN_CURSOR.read_text()) if DEADMAN_CURSOR.exists() else {}
    except (OSError, ValueError):
        cur = {}
    if cur.get(kind) == status["last_epoch"]:
        return False
    age_lbl = f"{status['age_h']:g}h" if status["age_h"] is not None else "?"
    try:
        inbox.append_event(ts=now_iso, ticker=None, source=kind, severity="HIGH",
                           msg=f"{kind} watcher SILENT {age_lbl} (>2× cadence) — dead-man "
                               f"fired: alerts are NOT being generated, restore the agent")
    except Exception:
        return False
    cur[kind] = status["last_epoch"]
    try:
        DEADMAN_CURSOR.parent.mkdir(parents=True, exist_ok=True)
        tmp = DEADMAN_CURSOR.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur))
        tmp.replace(DEADMAN_CURSOR)
    except OSError:
        pass
    return True


def annotate_unlocks(rows, now=None, horizon_days=7, catalysts=None):
    """SPEC-95: surface an `⏰ UNLOCK T-Nd` flag on any board row whose ticker has an unlock
    within `horizon_days` (read offline from the sweep-populated config/catalysts.json).
    A READ that prefixes the reason — never overrides the verdict (§0.5). Mutates rows in
    place; any failure degrades silently (the board must never break on the flag)."""
    try:
        import unlocks as U
        cats = U._load_catalysts() if catalysts is None else catalysts
    except Exception:  # noqa: BLE001
        return
    for r in rows:
        try:
            flag = U.unlock_flag_for(r.get("ticker"), now, horizon_days, cats)
        except Exception:  # noqa: BLE001
            flag = None
        if flag:
            r["reason"] = f"{flag} | " + r.get("reason", "")


def annotate_freshness(rows, now=None, state_dir=None):
    """SPEC-98: when a distribution (SHORT) thesis is live, the board row's reason carries
    the persisted rotation-freshness verdict (`distribution: FRESH|FROZEN|ROTATED — …`),
    produced by the last onchain/brief/verify read (rotation_freshness state file). A READ
    that appends to the reason — never overrides the verdict (§0.5). Stale state
    (> board_max_age_h) is not echoed; any failure degrades silently."""
    now = now if now is not None else time.time()
    try:
        import rotation_freshness as RF
        max_age = RF.load_cfg().get("board_max_age_h", 24) * 3600
    except Exception:  # noqa: BLE001
        return
    for r in rows:
        try:
            if (r.get("direction") or "").upper() != "SHORT" or not r.get("thesis_present"):
                continue
            st = RF.read_state(r.get("ticker") or "", state_dir=state_dir)
            if not st or not st.get("line"):
                continue
            if st.get("ts") is not None and (now - st["ts"]) > max_age:
                continue
            r["reason"] = r.get("reason", "") + " | " + st["line"]
        except Exception:  # noqa: BLE001 — the board must never break on the flag
            continue


def _thesis_is_live(status):
    s = (status or "").upper()
    return s.startswith("ACTIVE") or s == "PENDING"


def cluster_heat_for(ticker, clusters=None, tokens=None):
    """SPEC-99: §7 operator-heat from the auto-built operator graph
    (config/operator_clusters.json) — "crime coins sharing an MM are ONE position at
    multiplied size". Returns None when the ticker has no cluster, no cluster-mates on the
    watchlist, or operator_clusters.json is missing/empty (degrades silently, never an
    error — this is a READ, never a verdict override, §0.5). A live thesis (ACTIVE*/PENDING)
    on this ticker AND a cluster-mate additionally carries the "not a hedge" warning when
    the directions oppose (long+short on cluster-mates is not a hedge on operator-event
    risk)."""
    try:
        import operator_graph as OG
    except Exception:  # noqa: BLE001
        return None
    if clusters is None:
        clusters = OG._load_json(OG.OUT_PATH, {}).get("clusters", {})
    if not clusters:
        return None
    ticker = (ticker or "").upper()
    name, members = OG.cluster_for_ticker(ticker, clusters)
    if not name:
        return None
    if tokens is None:
        tokens, _err = load_watchlist()
    thesis_by = {}
    for t in tokens or []:
        tk = (t.get("ticker") or "").upper()
        th = t.get("thesis") or {}
        thesis_by[tk] = {"status": (th.get("status") or "").upper(),
                          "direction": (th.get("direction") or "").upper()}
    mates = sorted(m for m in members if m != ticker and m in thesis_by)
    if not mates:
        return None
    note = (f"⚠ cluster {name}: {'+'.join([ticker] + mates)} "
            f"— §7 one-position rule / combined operator-risk")
    my = thesis_by.get(ticker, {})
    live_mates = [m for m in mates if _thesis_is_live(thesis_by.get(m, {}).get("status"))]
    not_a_hedge = False
    if _thesis_is_live(my.get("status")) and live_mates:
        opp = [m for m in live_mates
               if thesis_by.get(m, {}).get("direction")
               and thesis_by.get(m, {}).get("direction") != my.get("direction")]
        if opp:
            note += (f" | long+short on cluster-mates ({ticker} vs {'+'.join(opp)}) "
                     "is NOT a hedge on operator-event risk")
            not_a_hedge = True
        else:
            note += f" | live thesis also on {'+'.join(live_mates)} — combined risk, not independent"
    return {"cluster": name, "members": members, "mates": mates, "note": note,
            "not_a_hedge_warning": not_a_hedge}


def annotate_cluster_heat(rows, clusters=None, tokens=None):
    """SPEC-99: append cluster_heat_for's note to each board row whose ticker shares an
    auto-built operator cluster with another watchlist name. A READ that appends to the
    reason — never overrides the verdict (§0.5); any failure degrades silently."""
    try:
        import operator_graph as OG
    except Exception:  # noqa: BLE001
        return
    if clusters is None:
        clusters = OG._load_json(OG.OUT_PATH, {}).get("clusters", {})
    if not clusters:
        return
    if tokens is None:
        tokens, _err = load_watchlist()
    for r in rows:
        try:
            ch = cluster_heat_for(r.get("ticker"), clusters=clusters, tokens=tokens)
        except Exception:  # noqa: BLE001
            ch = None
        if ch:
            r["reason"] = r.get("reason", "") + " | " + ch["note"]


def retire_flag_for(tok, row=None, now=None):
    """SPEC-140: mechanical staleness flag for one committed thesis — null when healthy,
    else a machine-readable reason. Advisory only (§0.5): retirement itself stays a
    Designer commit; this only makes staleness impossible to miss. Checked in order —
    the first that fires wins: time_stop_elapsed > zone_blown_unfilled >
    stale_unresolvable > stale_14d."""
    th = tok.get("thesis") or {}
    if not th:
        return None
    status = (th.get("status") or "").upper()
    if status in ("RETIRED", "PASS"):
        return None
    now = now if now is not None else time.time()
    row = row or {}
    p = TH.parse(tok)   # SPEC-146: the one typed geometry parse for this token

    # 1. time_stop_elapsed
    if p.committed_epoch is not None and p.time_stop_h is not None:
        if (now - p.committed_epoch) / 3600 >= p.time_stop_h:
            return "time_stop_elapsed"

    # 2. zone_blown_unfilled — reuse the price-leg's own entered_zone/stop_breached read
    # (already computed for the verdict layer, no re-fetch) plus the live price already
    # on the row: a committed ARMED/PENDING zone the window never traded into, while
    # price already ran past it — the same threshold classify_token's ZONE_BLOWN branch
    # uses ("ran past entry" / "cascaded past").
    direction = p.direction
    entry_zone = p.zone   # already sorted (lo, hi) — SPEC-146
    price_leg = row.get("price_leg")
    live_px = (row.get("live") or {}).get("price")
    if (status in ("ARMED", "PENDING") and entry_zone and price_leg
            and not price_leg.get("stop_breached") and not price_leg.get("entered_zone")
            and live_px is not None):
        lo, hi = entry_zone
        if ((direction == "SHORT" and live_px > hi * 1.03)
                or (direction == "LONG" and live_px < lo * 0.97)):
            return "zone_blown_unfilled"

    # 3/4. staleness — committed_ts age vs the log-scan TRIGGERS/BREAKS history already
    # used to classify (scan_trigger_logs; no new event store).
    committed_epoch = p.committed_epoch
    if th.get("committed_ts") and committed_epoch is None:
        return "stale_unresolvable"      # SPEC-113 legacy row — undatable, stale by definition
    if committed_epoch is None:
        return None                      # never committed a timestamp at all — nothing to age
    if (now - committed_epoch) / 86400 >= 14:
        entry_lines, break_lines = scan_trigger_logs(tok.get("ticker", "?"), since_epoch=committed_epoch)
        if not entry_lines and not break_lines:
            return "stale_14d"
    return None


def annotate_retire_flags(rows, tokens=None, now=None):
    """SPEC-140: `retire_flag` per board row (null when healthy) — the mechanical half of
    the CLAUDE.md §0.5 standing retire-policy (a 65-row board hides the signal in the
    graveyard). A READ, advisory only; never overrides verdict/reason (§0.5). Mutates rows
    in place; any per-row failure degrades that row to retire_flag: None."""
    if tokens is None:
        tokens, _err = load_watchlist()
    by_ticker = {(t.get("ticker") or "").upper(): t for t in (tokens or [])}
    for r in rows:
        try:
            tok = by_ticker.get((r.get("ticker") or "").upper())
            r["retire_flag"] = retire_flag_for(tok, r, now=now) if tok else None
        except Exception:  # noqa: BLE001 — the board must never break on the flag
            r["retire_flag"] = None


def _board_battlefield(perp_vol_24h, oi_mc_flag):
    """SPEC-177: the board-scope battlefield read — `perp_vol_24h` reuses `live["vol_m"]`
    already resolved onto every row (zero new fetch); the spot leg + leverage_state's
    OI-history/kline series need a per-ticker fetch this board-wide annotation
    deliberately does NOT make (a per-row spot-venue resolution + 2-window OI-history
    fetch on every board tick would multiply the sweep's network cost by the row count —
    `brief.py`'s single-ticker deep read resolves both legs live instead). Spot-null
    still yields a correct `perp_led` read via the heavy-OI/MC-flag branch (req 1); it
    never manufactures a `spot_led`/`mixed` read it can't support."""
    ratio, verdict = OM.battlefield_verdict(perp_vol_24h, None, oi_mc_flag)
    leverage_state = {
        "leverage_4h": OM.leverage_state_for_window(None, None, None, 8.0, "4h"),
        "leverage_48h": OM.leverage_state_for_window(None, None, None, 15.0, "48h"),
    }
    return (round(ratio, 4) if ratio is not None else None), verdict, leverage_state


def annotate_oi_mc(rows, fetch_mc=None):
    """SPEC-122: OI/MC "perp-casino" flag — the ledger's worst signature (mindshare/
    blowoff-top short, -26.3R/324) is the vertical-perp-on-no-float profile OI/MC names
    on sight. Uses the primary-venue OI already resolved onto `r["live"]` (the same
    cross-venue read the funding/squeeze signals aggregate — SPEC 13/44 `live_perp`), so
    this is purely a market-cap fetch + a division, never a second OI call. A READ that
    adds fields — never overrides the verdict (§0.5); any failure degrades every row to
    nulls, never a fabricated flag (§3).

    SPEC-177: also attaches `perp_spot_ratio` / `battlefield` / `leverage_state` — see
    `_board_battlefield` for the board-scope reuse/degrade rationale."""
    fetch_mc = fetch_mc or OM.fetch_market_cap
    for r in rows:
        r["oi_mc_ratio"] = None
        r["oi_mc_flag"] = None
        r["oi_mc_caveat"] = None
        live = r.get("live") or {}
        perp_vol_24h = float(live["vol_m"]) * 1e6 if live.get("vol_m") is not None else None
        r["perp_spot_ratio"], r["battlefield"], r["leverage_state"] = _board_battlefield(
            perp_vol_24h, None)
        oi = live.get("oi")
        price = live.get("price")
        if oi is None or price is None:
            continue
        try:
            mc_usd = fetch_mc(r.get("ticker"))
        except Exception:  # noqa: BLE001 — a dead MC source degrades this row only
            mc_usd = None
        try:
            om = OM.build_oi_mc(float(oi) * float(price), mc_usd,
                                direction=(r.get("direction") or "").upper() or None)
        except Exception:  # noqa: BLE001 — never break the board on this flag
            continue
        r["oi_mc_ratio"] = om["oi_mc_ratio"]
        r["oi_mc_flag"] = om["oi_mc_flag"]
        r["oi_mc_caveat"] = om["oi_mc_caveat"]
        r["perp_spot_ratio"], r["battlefield"], r["leverage_state"] = _board_battlefield(
            perp_vol_24h, om["oi_mc_flag"])


def annotate_oi_construction_compact(rows):
    """SPEC-180 req 5: the ONE compact `oic:` board field — printed ONLY when
    non-UNKNOWN or a noteworthy degradation, never boards of UNKNOWN noise (the row
    simply carries no `oic` key otherwise, never a null placeholder).

    Board scope has no chip_state/lock_info source (same cost-scoping decision as
    `annotate_oi_mc`'s battlefield read) — `oi_types` stays empty here, so this
    reduces to reusing the row's own already-computed `battlefield` read and is
    genuinely UNKNOWN-and-therefore-absent by construction today. A caller with a
    deeper per-ticker `oi_construction.build_oi_construction` read can inject the
    full envelope via `r['oic_envelope']` and this picks it up unchanged."""
    for r in rows:
        env = r.get("oic_envelope")
        if env is None:
            env = {"verdict": "UNKNOWN", "degraded": [], "oi_types": [],
                   "battlefield": {"battlefield": r.get("battlefield")},
                   "venue_roles": {"exit": {}}}
        line = OC.compact_line(env)
        if line is not None:
            r["oic"] = line


def annotate_aster_listed(rows, tokens=None, symbols_fn=None):
    """SPEC-136: tag every row `aster_listed: true|false|null` — the FILLABILITY gate,
    distinct from the cross-venue OI/funding SIGNAL (§0.6.3b: never conflate the two —
    COTI printed a clean regime-flip signal and got committed with a real trigger, but
    Aster carries no COTI market). One live/cached exchangeInfo fetch for the whole
    board, never per-ticker. A watchlist entry's explicit `aster_listed` (COTI/LQTY/
    DRIFT — verified un-Aster-listed names) overrides the live probe. `false` tags the
    row's reason `SIGNAL-ONLY (no Aster market)` — advisory only, never touches verdict
    (§0.5); `true` leaves the row byte-identical; `null` (fetch failed) is left
    untagged here (board_tick pages it with a distinct venue-unverified tag instead of
    suppressing — a transient network blip must never look like 'not listed')."""
    if tokens is None:
        tokens, _err = load_watchlist()
    by_ticker = {(t.get("ticker") or "").upper(): t for t in (tokens or [])}
    symbols = (symbols_fn or AL.fetch_aster_symbols)()
    for r in rows:
        tok = by_ticker.get((r.get("ticker") or "").upper()) or {}
        override = tok.get("aster_listed")
        if override is not None:
            al = bool(override)
        else:
            al = AL.aster_listed(r.get("ticker"), symbols)
        r["aster_listed"] = al
        if al is False:
            r["reason"] = r.get("reason", "") + " | SIGNAL-ONLY (no Aster market)"


# ─────────────────────────────────────────────────────────────────────────────
# SPEC-149 — operator_not_done: the §0.6 counterparty read becomes a LIVE veto field
#
# The grill (2026-08-19 Q12/Q13) settled that the desk's on-chain counterparty read
# cannot time or size an entry — its proven skill is knowing when the operator is NOT
# finished. That is a veto, not a permission slip: `true` renders the row's verdict
# with a loud ⛔ VETO prefix, `false` means the structure event fired and the short
# unlocks, `unknown` (a stale or unreadable read) blocks nothing (§3). Recomputed every
# board tick from the freshness stack (operator_veto.sweep -> rotation_freshness state
# + breakdown-hold + squeeze-cadence), never from a stored commit-time verdict.
# ─────────────────────────────────────────────────────────────────────────────

def annotate_operator_veto(rows, now=None, state_dir=None, fire=True, freshness_fn=None,
                           breakdown_hold_fn=None, squeeze_legs_fn=None, event_fn=None):
    """SPEC-149 req 1-4: `operator_not_done` true|false|"unknown" on every row, via the
    headless operator_veto.sweep (req 6 — decoupled from any live-SHORT-thesis gate).
    `true` prefixes the row's reason `⛔ VETO — <reason> | ...` — advisory only, never
    changes `verdict` (§0.5: the desk never silently blocks the user, grill Q6). A
    true->false transition fires ONE HIGH inbox event (the entry unlocking, req 4).
    squeeze_legs_fn defaults to `[]` (no live price_structure fetch per board tick —
    the per-name squeeze-cadence leg is opt-in via an injected fn) so the board sweep
    stays fast; callers wanting the live cadence read pass their own squeeze_legs_fn."""
    try:
        import operator_veto as OV
    except Exception:  # noqa: BLE001 — the board must never break on this flag
        return
    tickers = [r.get("ticker") for r in rows if r.get("ticker")]
    if not tickers:
        return
    try:
        results = OV.sweep(tickers, now=now, state_dir=state_dir, fire=fire,
                           freshness_fn=freshness_fn, breakdown_hold_fn=breakdown_hold_fn,
                           squeeze_legs_fn=squeeze_legs_fn or (lambda t: []),
                           event_fn=event_fn)
    except Exception:  # noqa: BLE001
        return
    for r in rows:
        v = results.get((r.get("ticker") or "").upper())
        if not v:
            continue
        r["operator_not_done"] = v["operator_not_done"]
        r["operator_veto_reason"] = v["reason"]
        if v["operator_not_done"] is True:
            r["reason"] = f"⛔ VETO — {v['reason']} | " + r.get("reason", "")


# ─────────────────────────────────────────────────────────────────────────────
# SPEC-149 req 5 — tier: tradeable | tracking
#
# Derived, never hand-set: `tradeable` iff the row's canonical signature has earned
# fills on the ledger (live filled n>=1 AND total_R>0 — today trap_formation_long,
# stage5_short) AND the name is Aster-listed AND the §7 liquidity gate passes. A
# tracked name never becomes a trade merely by being on screen (CLAUDE.md "working
# set" grill).
# ─────────────────────────────────────────────────────────────────────────────

def earned_signatures():
    """Signatures with live filled n>=1 and total_R>0 — the ledger's earned set.
    SPEC-150 req 4: reads `ledger.stats()`'s own `summary.earned_signatures` (desk-scoped
    only — off_desk rows never earn a signature its promotion) fresh every call (no
    caching — a promotion/demotion must show up immediately). Degrades to an empty set
    on any read failure (never a fabricated 'earned' signature, §3)."""
    try:
        stats = LG.stats()
    except Exception:  # noqa: BLE001
        return set()
    return set((stats.get("summary") or {}).get("earned_signatures") or [])


def annotate_tier(rows, earned=None):
    """SPEC-149 req 5: `tier: tradeable|tracking` on every board row. Reads `signature`/
    `aster_listed`/`live.vol_m` already on the row (classify_token + annotate_aster_listed
    must have run first) — no extra fetch."""
    earned = earned_signatures() if earned is None else earned
    for r in rows:
        sig = r.get("signature")
        vol_m = (r.get("live") or {}).get("vol_m")
        liq_pass = vol_m is not None and vol_m >= LIQ_GATE_M
        tradeable = bool(sig and sig in earned and r.get("aster_listed") is True and liq_pass)
        r["tier"] = "tradeable" if tradeable else "tracking"


def annotate_deadman(rows, now=None, fire=True):
    """Prefix each board row's reason with a dead-man tag when the surveil/board_tick
    watcher is stale, fire the once-per-episode HIGH inbox event(s), and return the board
    meta block. Mutates rows in place. The Designer cannot read a board without seeing a
    dead watcher (SPEC 64)."""
    now = now if now is not None else time.time()
    sv = surveil_status(now)
    bt = board_tick_status(now)
    prefix = _deadman_prefix(sv, bt)
    if prefix:
        for r in rows:
            r["reason"] = prefix + r.get("reason", "")
    if fire:
        now_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if sv["stale"]:
            _fire_deadman_event("nonce_surveil", sv, now_iso)
        if bt["stale"]:
            _fire_deadman_event("board_tick", bt, now_iso)
    return {"surveil_age_h": sv["age_h"], "surveil_stale": sv["stale"],
            "board_tick_age_h": bt["age_h"], "board_tick_stale": bt["stale"],
            "cadence_h": SURVEIL_CADENCE_H}


# ─────────────────────────────────────────────────────────────────────────────
# SPEC 39 — price leg of the committed thesis
#
# The thesis is mostly PRICE fields (stop/tp/entry_zone); a funding-only read
# implements a fraction of the contract. classify compares the high/low RANGE
# since thesis commit against those fields — a stop printed on a wick IS a
# break, even if price came back (the position is stopped).
# ─────────────────────────────────────────────────────────────────────────────

# kline reach per venue (candles per request); commit older than the reach at the
# chosen interval → trailing-24h fallback rather than a silently-truncated window
_KLINE_LIMIT = {"binance": 1500, "bybit": 1000}


def _klines_binance(sym, start_ms, iv_min=60):
    iv = f"{iv_min}m" if iv_min < 60 else "1h"
    d = fetch(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}"
              f"&interval={iv}&startTime={start_ms}&limit=1500")
    try:
        return [{"ts": k[0] / 1000.0, "high": float(k[2]), "low": float(k[3])}
                for k in d] or None
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def _klines_bybit(sym, start_ms, iv_min=60):
    d = fetch(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}"
              f"&interval={iv_min}&start={start_ms}&limit=1000")
    try:
        rows = d["result"]["list"]          # bybit returns newest-first
        return [{"ts": float(r[0]) / 1000.0, "high": float(r[2]), "low": float(r[3])}
                for r in reversed(rows)] or None
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def _ticker_hl(sym, venue):
    """24h high/low from the venue ticker — the fallback window."""
    if venue == "bybit":
        d = fetch(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}")
        try:
            x = d["result"]["list"][0]
            return float(x["highPrice24h"]), float(x["lowPrice24h"])
        except (TypeError, KeyError, IndexError, ValueError):
            return None
    d = fetch(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={sym}")
    try:
        return float(d["highPrice"]), float(d["lowPrice"])
    except (TypeError, KeyError, ValueError):
        return None


def price_window_range(ticker, venue="binance", since_epoch=None):
    """High/low printed since thesis commit, from the thesis's primary venue.

    SPEC 54 — evaluates from committed_ts FORWARD, never a trailing window dressed up
    as one: intraday bars (15m) for the first 24h, 1h beyond; bars opening BEFORE the
    commit are dropped (a straddling bar carries pre-commit prints). A fresh commit
    with no closed bars yet → None (price leg stays SILENT — falling to the 24h ticker
    here is exactly the false-fire this spec kills). The trailing-24h ticker is used
    ONLY when there is no committed_ts or the commit is beyond the kline reach, tagged
    `trailing_24h_fallback`. Returns {"high","low","window","candles"} or None."""
    sym = f"{ticker}USDT"
    if since_epoch is not None:
        age_h = (time.time() - since_epoch) / 3600
        iv_min = 15 if age_h <= 24 else 60
        reach_h = _KLINE_LIMIT.get(venue, 1000) * iv_min / 60
        if age_h <= reach_h:
            kl = (_klines_bybit if venue == "bybit" else _klines_binance)(
                sym, int(since_epoch * 1000), iv_min)
            kl = [k for k in (kl or []) if k["ts"] >= since_epoch]   # drop the straddling bar
            if kl:
                return {"high": max(k["high"] for k in kl),
                        "low": min(k["low"] for k in kl),
                        "window": f"klines:{venue}:{len(kl)}x{iv_min}m", "candles": kl}
            return None   # post-commit window exists but no closed bars / fetch failed → SILENT
    hl = _ticker_hl(sym, venue)
    if hl:
        return {"high": hl[0], "low": hl[1],
                "window": f"trailing_24h_fallback:{venue}", "candles": None}
    return None


def _fmt_ts(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%m-%d %H:%M")


def eval_price_leg(th, direction, rng):
    """Pure: compare the window high/low against the committed stop/tp/entry_zone.

    Direction-aware: a SHORT is stopped when the HIGH prints through the stop,
    banks when the LOW prints through a tp; LONG mirrors. Stop-breach beats a
    tp print (the position is stopped even if a tp also traded); when candles
    are available the reason reports the sequence.

    Returns {verdict: BREAKS|TRIGGERS|None, note, stop_breached, tps_printed,
             entered_zone, high, low, window}."""
    high, low = rng["high"], rng["low"]
    stop   = th.get("stop")
    banked = set(th.get("banked") or [])
    tps    = [t for t in (th.get("tp") or th.get("tps") or []) if t not in banked]
    zone   = th.get("entry_zone")
    status = (th.get("status") or "").upper()

    # SPEC-78: a PENDING thesis whose entry has NEVER printed is not a live position —
    # its committed stop/TP must not be watched. With price on the far side of a stop for
    # a trade that never filled (RIVER 06-17: breakdown SHORT, entry [4.15,4.32] below a
    # 4.80 stop, price drifted UP to 5.07), the old leg read stop_breached every tick and
    # cried a fake BREAKS — the §0.5 noise that masks a real break. Gate the stop/TP on the
    # entry having printed; until then only entry detection drives the verdict. Once the zone
    # prints (or an entered_ts latch is set, or the orchestrator flips status off PENDING),
    # the position is live and the stop/TP are watched exactly as before.
    #
    # Entry = the traded range intersects the committed zone (direction-agnostic). This is
    # what makes a breakdown SHORT (zone BELOW current price) require price to actually fall
    # into the zone, instead of the old SHORT `high >= lo` which read "entered" the moment
    # price was anywhere above the zone low.
    candles = rng.get("candles")
    # SPEC-147: filled/entry_idx now come from the ONE shared fill rule (thesis.fill_of) —
    # also consumed by counterfactual, so a thesis can no longer read "entered" here and
    # "filled at a different price" there. eval_price_leg still only needs filled/entry_idx
    # (the stop/TP sequencing below never priced the entry); `fill` is carried through in
    # the return dict so a caller that DOES need mode/entry_px (or an equivalence test
    # against counterfactual) can read them off the same call.
    fill = TH.fill_of(th, direction, rng)
    entered_raw = bool(fill and fill["filled"])
    entry_idx = fill["entry_idx"] if fill else None
    is_live = bool(th.get("entered_ts")) or status not in ("PENDING",)
    watch_stop = is_live or entered_raw

    # SPEC-79: which high/low does the stop/TP see? A sell-the-breakdown SHORT falls from ABOVE
    # its stop DOWN into the entry zone (and a buy-the-breakout LONG rises from BELOW its stop
    # UP into the zone) — so the window's pre-entry extreme sits on the wrong side of the stop
    # without the position ever being live there. The old aggregate read fired `stop_breached`
    # off that pre-entry print the moment the zone finally traded (false BREAKS instead of the
    # TRIGGERS the entry deserves). When the thesis goes live THIS window (PENDING→entered, no
    # prior entered_ts) and candles resolve the entry bar, evaluate stop/TP only from that bar
    # forward. Without candles the order is unresolvable → keep SPEC-78's conservative
    # full-window read; a position already live (entered_ts/OPEN) watches the full window too.
    seq_candles = candles
    eval_high, eval_low = high, low
    if watch_stop and not is_live and entry_idx is not None:
        seq_candles = candles[entry_idx:]
        eval_high = max(k["high"] for k in seq_candles)
        eval_low = min(k["low"] for k in seq_candles)

    stop_breached = (stop is not None and (
        eval_high >= stop if direction == "SHORT" else eval_low <= stop)) if watch_stop else None
    tps_printed = ([t for t in tps if
                    (eval_low <= t if direction == "SHORT" else eval_high >= t)]
                   if watch_stop else [])
    entered_zone = False
    if zone and status in ("ARMED", "PENDING") and not stop_breached:
        entered_zone = entered_raw

    verdict, note = None, ""
    if stop_breached:
        side = f"window high {high:g} >= stop {stop:g}" if direction == "SHORT" \
            else f"window low {low:g} <= stop {stop:g}"
        seq = ""
        if tps_printed and seq_candles:
            def first_ts(pred):
                return next((k["ts"] for k in seq_candles if pred(k)), None)
            s_ts = first_ts(lambda k: k["high"] >= stop if direction == "SHORT"
                            else k["low"] <= stop)
            tp0 = tps_printed[0]
            t_ts = first_ts(lambda k: k["low"] <= tp0 if direction == "SHORT"
                            else k["high"] >= tp0)
            if s_ts is not None and t_ts is not None:
                seq = (f"; TP {tp0:g} traded {_fmt_ts(t_ts)} then stop breached {_fmt_ts(s_ts)}"
                       if t_ts < s_ts else
                       f"; stop breached {_fmt_ts(s_ts)} before TP {tp0:g} ({_fmt_ts(t_ts)})")
        elif tps_printed:
            seq = f"; TP {tps_printed[0]:g} also printed (order unresolvable from 24h range)"
        verdict = VERDICT_BREAKS
        note = f"price-leg STOP BREACHED: {side} — a wick through the stop IS a break{seq}"
    elif tps_printed or entered_zone:
        verdict = VERDICT_TRIGGERS
        bits = []
        if tps_printed:
            bits.append(f"TP {'/'.join(f'{t:g}' for t in tps_printed)} printed "
                        f"(window {'low ' + format(low, 'g') if direction == 'SHORT' else 'high ' + format(high, 'g')})")
        if entered_zone:
            bits.append(f"entry zone {zone[0]:g}-{zone[1]:g} printed while {status}")
        note = "price-leg: " + "; ".join(bits) + " — stop intact"

    return {"verdict": verdict, "note": note, "stop_breached": stop_breached,
            "tps_printed": tps_printed, "entered_zone": entered_zone,
            "high": high, "low": low, "window": rng["window"], "fill": fill}


# ─────────────────────────────────────────────────────────────────────────────
# SPEC-188 §3 — venue_agreement: does a price-leg event (stop/tp/watch_level breach)
# hold on the ALL-VENUE tape, or just the one/two venues classify's own price leg
# reads off (price_window_range is Binance-primary)? §3's all-venue rule generalizes
# past the discovery-brief case this ticket was filed for (OP 2026-09-02: Bybit alone
# read "stall under prior high" while 11/14 venues printed a higher high) to every
# committed-thesis price event — a stop/tp/watch_level that "broke" on one venue's
# read and nowhere else is a data artifact, not a break. ANNOTATION ONLY (§0.5): the
# verdict itself never changes here — venue_agreement is printed on the event line and
# left for the orchestrator to downgrade PARTIAL/SINGLE reads by rule (CLAUDE.md §3).
# ─────────────────────────────────────────────────────────────────────────────

def venue_agreement_for(ticker, level, direction):
    """FULL = Binance AND the OI size-book venue (venue_map.top_oi_venue) AND Aster
    (execution) all crossed `level` on the live 1h bar; SINGLE = exactly one venue
    crossed; PARTIAL = otherwise. Best-effort: any failure (network, missing modules,
    the level never actually crossed anywhere) returns None — a price-leg VERDICT must
    never be gated on this sweep succeeding (it is a live cross-venue call, not the
    Binance-primary read classify already made to reach the verdict)."""
    if level is None or direction not in ("above", "below"):
        return None
    try:
        import venue_bars as VB
    except Exception:  # noqa: BLE001
        return None
    try:
        r = VB.build_venue_bars(ticker, "1h", 6)
        agr = VB.level_agreement(r, level, direction)
    except Exception:  # noqa: BLE001
        return None
    if agr["n_crossed"] == 0:
        return None
    size_book = None
    try:
        import venue_map as VM
        size_book = VM.build_venue_map(ticker).get("top_oi_venue")
    except Exception:  # noqa: BLE001 — size-book resolution is best-effort only
        pass
    required = {"binance", "aster"} | ({size_book} if size_book else set())
    crossed = set(agr["crossed"])
    if agr["n_crossed"] == 1:
        label = "SINGLE"
    elif required <= crossed:
        label = "FULL"
    else:
        label = "PARTIAL"
    return {"label": label, "n_crossed": agr["n_crossed"], "n_total": agr["n_total"],
            "crossed": agr["crossed"], "closed_beyond": agr["closed_beyond"],
            "size_book_venue": size_book}


def _tape_tag(va):
    return f" (tape: {va['label']} {va['n_crossed']}/{va['n_total']})" if va else ""


# ─────────────────────────────────────────────────────────────────────────────
# SPEC 77 — watch_level: a machine-checkable trip-wire on a WATCH thesis
#
# A WATCH thesis with null stop/tp/entry_zone can only ever return CONFIRMS — the
# price leg (above) never runs for it, so a move it was written to catch happens
# silently (BEAT: 11.57 ATH → −75% while the board printed CONFIRMS). watch_level is
# a monitor-only window read: it does NOT imply a committed/armed position, so a breach
# surfaces as a distinct WATCH-ARMED event, never BREAKS/TRIGGERS (which mean a committed
# position moved — conflating them corrupts the §0.5 state machine).
# ─────────────────────────────────────────────────────────────────────────────
WATCH_ARMED_CURSOR = STATE_DIR / "watch_armed_cursor.json"   # once-per-episode inbox dedup


# SPEC-146: watch_level normalization (formerly _normalize_watch_levels) and the
# board-level invariant sweep (formerly check_watch_level_invariant) now live in
# thesis.py — thesis.parse(tok).watch_levels / thesis.check_board(tokens).

def eval_watch_levels(watch_levels, rng):
    """Pure: which watch_levels did the window cross? `below` trips when low <= price,
    `above` when high >= price (touch counts). Returns the breached subset (in order)."""
    high, low = rng["high"], rng["low"]
    return [w for w in watch_levels
            if (low <= w["price"] if w["dir"] == "below" else high >= w["price"])]


def _watch_armed_note(breached, legacy=False):
    """Compose the reason note for one or more breached watch_levels.

    SPEC-113: `legacy` is True when the window came back `trailing_24h_fallback` (no
    resolvable committed_ts, or the commit predates the kline reach) — the breach could be
    a pre-commit print bleeding through the trailing-24h ticker window (SLX 2026-07-07: a
    freshly-committed 0.1846 exit line read "crossed" off the 07-06 pre-commit low). Tag the
    note so the reader knows to verify live price by hand rather than trust the string."""
    bits = []
    for w in breached:
        tail = f" — {w['note']}" if w.get("note") else ""
        bits.append(f"watch_level {w['price']:g} {w['dir']} crossed{tail}")
    note = "WATCH-ARMED: " + "; ".join(bits)
    if legacy:
        note += " (pre-commit history — verify live price)"
    return note


def fire_watch_armed(results, now=None, fire=True):
    """Fire ONE inbox event per (ticker, watch_level) breach episode, deduped via a cursor
    file — a breached watch_level reads as armed every board tick but only alerts once (SPEC
    77.4: don't nag; once armed the operator's job is to commit it or dismiss). Mirrors the
    dead-man once-per-episode fire. Returns the number of fresh events fired.

    MED severity: a trip-wire is a READ, not a committed-position event (those are HIGH)."""
    if not fire:
        return 0
    now = now if now is not None else time.time()
    now_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        cur = json.loads(WATCH_ARMED_CURSOR.read_text()) if WATCH_ARMED_CURSOR.exists() else {}
    except (OSError, ValueError):
        cur = {}
    fired, changed = 0, False
    for r in results:
        wl = r.get("watch_leg")
        if not wl:
            continue
        for w in wl["breached"]:
            key = f"{r['ticker']}:{w['price']:g}:{w['dir']}"
            if cur.get(key):
                continue   # already armed this episode — no re-fire
            tail = f" — {w['note']}" if w.get("note") else ""
            # SPEC-113: tag a legacy (unresolvable/beyond-reach commit_ts) crossing in the
            # inbox record too — the event still fires (requirement 3 keeps current
            # behavior), only the SPEC-111 push notification is gated on this flag.
            legacy_tail = " (pre-commit history — verify live price)" if wl.get("legacy") else ""
            try:
                inbox.append_event(
                    ts=now_iso, ticker=r["ticker"], source="watch_level", severity="MED",
                    msg=f"WATCH-ARMED: {w['price']:g} {w['dir']} crossed{tail}{legacy_tail} — "
                        f"convert to a committed thesis or dismiss")
            except Exception:
                continue
            cur[key] = now_iso
            fired += 1
            changed = True
    if changed:
        try:
            WATCH_ARMED_CURSOR.parent.mkdir(parents=True, exist_ok=True)
            tmp = WATCH_ARMED_CURSOR.with_suffix(".tmp")
            tmp.write_text(json.dumps(cur))
            tmp.replace(WATCH_ARMED_CURSOR)
        except OSError:
            pass
    return fired


# ─────────────────────────────────────────────────────────────────────────────
# SPEC-142 — funding_watch: a machine-checkable trip-wire on a committed thesis's
# FUNDING leg, the direct mirror of SPEC-77's watch_level for price. "Page me when
# <name>'s funding crosses <level>" (AEON, and the BLESS §6 arming leg — "funding cools
# under +0.02%/4h") had no engine home; it was hand-rolled as ops/interim_funding_watch.sh
# (deleted by this spec's landing) until now. Evaluated on the CROSS-VENUE verified live
# rate (regime_flip.live_perp's already-floor-rejecting funding_4h — never a single-venue
# read, §3) — funding has no lookback-window issue like price klines, so there is no
# committed_ts cut to apply; the current tick's rate is the whole read.
# ─────────────────────────────────────────────────────────────────────────────
FUNDING_WATCH_CURSOR = STATE_DIR / "funding_watch_cursor.json"   # once-per-episode inbox dedup
FUNDING_ALREADY_TRUE_WINDOW_S = 30 * 60   # within this long of commit, a breach reads as
                                          # "already true at commit", never a silent skip


def _normalize_funding_watch(th):
    """Return (entries, caveats). thesis.funding_watch as a list of
    {threshold_4h, op, note} dicts — accepts a single dict or a list. Malformed entries
    are DROPPED but reported in `caveats` (never silently — the SPEC-77 bare-float
    watch_level lesson: a silently-dropped entry is never armed and never known to be
    dead). Absent/null → ([], [])."""
    fw = th.get("funding_watch")
    if not fw:
        return [], []
    if isinstance(fw, dict):
        fw = [fw]
    if not isinstance(fw, (list, tuple)):
        return [], [f"funding_watch malformed: expected list/dict, got {type(fw).__name__}"]
    out, caveats = [], []
    for w in fw:
        if not isinstance(w, dict):
            caveats.append(f"funding_watch entry malformed (expected dict): {w!r}")
            continue
        threshold = w.get("threshold_4h")
        op = str(w.get("op") or "").lower()
        if threshold is None or op not in ("lt", "gt"):
            caveats.append(f"funding_watch entry malformed (needs threshold_4h + op lt|gt): {w!r}")
            continue
        try:
            out.append({"threshold_4h": float(threshold), "op": op, "note": w.get("note") or "",
                       "page_label": TH._clip_page_label(w.get("page_label"), caveats,
                                                          "funding_watch")})
        except (TypeError, ValueError):
            caveats.append(f"funding_watch entry malformed (threshold_4h not numeric): {w!r}")
    return out, caveats


def eval_funding_watch(entries, funding_4h):
    """Pure: which funding_watch entries does the CURRENT live rate cross? `lt` trips when
    funding_4h < threshold, `gt` when funding_4h > threshold. Returns the breached subset
    (in order), or [] when funding_4h is unavailable (never guesses)."""
    if funding_4h is None:
        return []
    out = []
    for w in entries:
        if w["op"] == "lt" and funding_4h < w["threshold_4h"]:
            out.append(w)
        elif w["op"] == "gt" and funding_4h > w["threshold_4h"]:
            out.append(w)
    return out


def _funding_armed_note(breached, funding_4h, already_true_at_commit=False):
    """Compose the reason note for one or more breached funding_watch entries."""
    fh = f"{funding_4h:+.3f}" if funding_4h is not None else "?"
    bits = []
    for w in breached:
        tail = f" — {w['note']}" if w.get("note") else ""
        bits.append(f"funding {fh}%/4h {w['op']} {w['threshold_4h']:+.3f}%{tail}")
    note = "FUNDING-ARMED: " + "; ".join(bits)
    if already_true_at_commit:
        note += " (already true at commit)"
    return note


def fire_funding_armed(results, now=None, fire=True):
    """Fire ONE inbox event per (ticker, funding_watch entry) breach episode, deduped via
    a cursor file — mirrors fire_watch_armed exactly (SPEC-77.4 semantics applied to the
    funding leg). Returns the number of fresh events fired. MED severity: a trip-wire is a
    READ, not a committed-position event (those are HIGH)."""
    if not fire:
        return 0
    now = now if now is not None else time.time()
    now_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        cur = json.loads(FUNDING_WATCH_CURSOR.read_text()) if FUNDING_WATCH_CURSOR.exists() else {}
    except (OSError, ValueError):
        cur = {}
    fired, changed = 0, False
    for r in results:
        fl = r.get("funding_leg")
        if not fl:
            continue
        for w in fl["breached"]:
            key = f"{r['ticker']}:{w['threshold_4h']:g}:{w['op']}"
            if cur.get(key):
                continue   # already armed this episode — no re-fire
            tail = f" — {w['note']}" if w.get("note") else ""
            already_tail = " (already true at commit)" if fl.get("already_true_at_commit") else ""
            try:
                inbox.append_event(
                    ts=now_iso, ticker=r["ticker"], source="funding_watch", severity="MED",
                    msg=f"FUNDING-ARMED: {fl.get('funding_4h')} {w['op']} {w['threshold_4h']:+.3f}%"
                        f"{tail}{already_tail} — convert to a committed thesis or dismiss")
            except Exception:
                continue
            cur[key] = now_iso
            fired += 1
            changed = True
    if changed:
        try:
            FUNDING_WATCH_CURSOR.parent.mkdir(parents=True, exist_ok=True)
            tmp = FUNDING_WATCH_CURSOR.with_suffix(".tmp")
            tmp.write_text(json.dumps(cur))
            tmp.replace(FUNDING_WATCH_CURSOR)
        except OSError:
            pass
    return fired


# ─────────────────────────────────────────────────────────────────────────────
# SPEC-91 — thesis-drift guard
#
# A committed thesis carries price anchors (watch_level[], entry_zone, stop, tp[]) AND a
# commit-time note that the board's reason echoes verbatim. When live price runs BEYOND the
# whole committed structure, the board keeps emitting the stale "still waiting at the old
# level" string and the human misses that the move already happened (LAB 2026-06-30: cascaded
# 17.7→12.26 through all three TPs while the board echoed a 16.69 watch_level). The engine —
# not the human — must detect that the committed levels no longer describe live price and force
# a re-anchor. Drift is a READ: it annotates the reason and fires an inbox event, it does NOT
# change the verdict (§0.5: only a named-invalidation BREAK reframes).
# ─────────────────────────────────────────────────────────────────────────────
THESIS_DRIFT_CURSOR = STATE_DIR / "thesis_drift_cursor.json"   # once-per-episode inbox dedup


def thesis_drift(th, live_price, drift_pct=DRIFT_PCT):
    """Pure: has live price run past the thesis's whole committed structure?

    Returns None when there is no thesis, no live price, or no committed anchors (nothing to
    drift from). Otherwise {stale, live_price, nearest_anchor, nearest_dist_pct, furthest_anchor,
    all_tps_printed, side}. Distances are measured relative to the ANCHOR price.

    STALE = either sub-rule fires:
      A (structure spent): for a directional thesis, EVERY tp printed (live beyond all of them
        in the take-profit direction) AND live is >= drift_pct beyond the FURTHEST tp.
      B (all anchors one side): every committed anchor sits on the SAME side of live AND the
        NEAREST anchor is >= drift_pct away. (Catches a clean gap past the whole structure
        regardless of TP order; an anchor exactly AT live = neither side → not stale.)"""
    if not th or live_price is None:
        return None
    try:
        live_price = float(live_price)
    except (TypeError, ValueError):
        return None
    anchors = TH.parse({"thesis": th}).anchors   # SPEC-146: the one typed anchor collector
    if not anchors:
        return None

    side = str(th.get("direction") or "").upper()
    tps = []
    for t in (th.get("tp") or th.get("tps") or []):
        try:
            tps.append(float(t))
        except (TypeError, ValueError):
            pass

    nearest_anchor = min(anchors, key=lambda a: abs(a - live_price))
    furthest_anchor = max(anchors, key=lambda a: abs(a - live_price))
    nearest_dist_pct = abs(nearest_anchor - live_price) / nearest_anchor * 100 if nearest_anchor else 0.0

    # ── sub-rule A: every TP printed + price beyond the furthest TP by >= drift_pct ──
    all_tps_printed = False
    rule_a = False
    if tps and side in ("SHORT", "LONG"):
        if side == "SHORT":
            all_tps_printed = all(live_price <= t for t in tps)   # TP prints as price falls
            furthest_tp = min(tps)
            beyond_pct = (furthest_tp - live_price) / furthest_tp * 100 if furthest_tp else 0.0
        else:  # LONG — TP prints as price rises
            all_tps_printed = all(live_price >= t for t in tps)
            furthest_tp = max(tps)
            beyond_pct = (live_price - furthest_tp) / furthest_tp * 100 if furthest_tp else 0.0
        rule_a = all_tps_printed and beyond_pct >= drift_pct

    # ── sub-rule B: every anchor on the same side of live + nearest >= drift_pct away ──
    one_side = all(a > live_price for a in anchors) or all(a < live_price for a in anchors)
    rule_b = one_side and nearest_dist_pct >= drift_pct

    return {"stale": bool(rule_a or rule_b), "live_price": live_price,
            "nearest_anchor": nearest_anchor, "nearest_dist_pct": round(nearest_dist_pct, 1),
            "furthest_anchor": furthest_anchor, "all_tps_printed": all_tps_printed,
            "side": side or None}


def _drift_prompt(d):
    """The action prompt that LEADS a stale row's reason (placed before any echoed
    commit-time note, which is tagged [commit-time, stale] downstream)."""
    live, na, nd = d["live_price"], d["nearest_anchor"], d["nearest_dist_pct"]
    fa = d["furthest_anchor"]
    fd = round(abs(fa - live) / fa * 100, 1) if fa else 0.0
    spent = " — all TPs printed" if d.get("all_tps_printed") else ""
    return (f"⏳ STALE-THESIS [DRIFT]: live {live:g} is {nd:g}% past nearest anchor {na:g} / "
            f"{fd:g}% past furthest anchor {fa:g}{spent} — RE-ANCHOR "
            f"(commit-time note below is stale)")


def fire_thesis_drift(results, now=None, fire=True):
    """Fire ONE HIGH inbox event per (ticker, committed_ts) episode when a thesis first goes
    stale, deduped via a cursor file. Re-anchoring (operator bumps committed_ts) starts a new
    episode → the flag clears and can fire again. Returns the number of fresh events fired.
    HIGH severity: a stale committed thesis is action-required, not a passive read."""
    if not fire:
        return 0
    now = now if now is not None else time.time()
    now_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        cur = json.loads(THESIS_DRIFT_CURSOR.read_text()) if THESIS_DRIFT_CURSOR.exists() else {}
    except (OSError, ValueError):
        cur = {}
    fired, changed = 0, False
    for r in results:
        d = r.get("thesis_drift")
        if not d or not d.get("stale"):
            continue
        key = f"{r['ticker']}:{d.get('committed_ts')}"
        if cur.get(key):
            continue   # already fired this episode — no re-nag
        try:
            inbox.append_event(
                ts=now_iso, ticker=r["ticker"], source="thesis_drift", severity="HIGH",
                msg=f"STALE-THESIS [DRIFT]: live {d['live_price']:g} ran past the whole committed "
                    f"structure (nearest anchor {d['nearest_anchor']:g}, {d['nearest_dist_pct']:g}% "
                    f"away) — RE-ANCHOR, the commit-time thesis is stale")
        except Exception:
            continue
        cur[key] = now_iso
        fired += 1
        changed = True
    if changed:
        try:
            THESIS_DRIFT_CURSOR.parent.mkdir(parents=True, exist_ok=True)
            tmp = THESIS_DRIFT_CURSOR.with_suffix(".tmp")
            tmp.write_text(json.dumps(cur))
            tmp.replace(THESIS_DRIFT_CURSOR)
        except OSError:
            pass
    return fired


# ─────────────────────────────────────────────────────────────────────────────
# core classifier
# ─────────────────────────────────────────────────────────────────────────────

def classify_token(tok, live=None):
    """Classify one token. live = result of live_perp(ticker) or None.
    Returns dict: {verdict, reason, signals, thesis_present, ticker}."""
    ticker    = tok.get("ticker", "?")
    th_present = bool(tok.get("thesis"))
    p = TH.parse(tok)   # SPEC-146: the one typed geometry parse for this token
    direction  = p.direction
    entry_zone = p.zone   # already sorted (lo, hi)

    reasons   = []
    verdicts  = []  # BREAKS > TRIGGERS > CONFIRMS — collect all, pick highest

    # ── 1. UNCONDITIONAL time-stop ────────────────────────────────────────────
    # SPEC 54: the committed_ts cut is independent of time_stop_h — a thesis without a
    # time-stop must NOT lose the cut and classify itself on stale (pre-commit) prints
    committed_epoch = p.committed_epoch
    if committed_epoch is not None and p.time_stop_h is not None:
        elapsed_h = (time.time() - committed_epoch) / 3600
        if elapsed_h >= p.time_stop_h:
            verdicts.append(VERDICT_BREAKS)
            reasons.append(f"time-stop: {elapsed_h:.1f}h >= {p.time_stop_h}h horizon")

    # ── 2. Trigger-log scan ───────────────────────────────────────────────────
    entry_lines, break_lines = scan_trigger_logs(ticker, since_epoch=committed_epoch)
    if break_lines:
        verdicts.append(VERDICT_BREAKS)
        reasons.append(f"log BREAK: {break_lines[-1][1][:80]}")
    if entry_lines:
        verdicts.append(VERDICT_TRIGGERS)
        reasons.append(f"log ENTRY/ARMED: {entry_lines[-1][1][:80]}")

    # ── 3. PRICE leg (SPEC 39): range since commit vs stop/tp/entry_zone ─────
    # The committed thesis is mostly price fields; a stop wicked between sessions
    # must surface as BREAKS even when the funding leg reads consistent.
    th = tok.get("thesis") or {}
    th_status = (th.get("status") or "").upper()
    th_dead = th_status in ("RETIRED", "PASS")
    price_leg = None
    watch_leg = None
    # SPEC 77: the watch leg runs even for a WATCH thesis with null committed levels — that
    # is the whole point (the price leg below never fires for it). Both legs share ONE window
    # fetch.
    # SPEC-146: caveats now cover EVERY dropped geometry field (not just watch_level) —
    # a RETIRED/PASS thesis isn't actively read, so it carries no caveats either (matches
    # pre-146 behavior, where a dead thesis never invoked the normalizer at all).
    caveats = [] if th_dead else p.caveats
    watch_levels = [] if th_dead else p.watch_levels
    price_gate = (direction in ("SHORT", "LONG") and not th_dead
                  and (th.get("stop") is not None or th.get("tp") or th.get("tps")
                       or th.get("entry_zone")))
    if price_gate or watch_levels:
        venue = (live or {}).get("primary_venue") or (live or {}).get("venue") or "binance"
        try:
            rng = price_window_range(ticker, venue, committed_epoch)
        except Exception:
            rng = None
        if rng and price_gate:
            price_leg = eval_price_leg(th, direction, rng)
            if price_leg["verdict"]:
                verdicts.append(price_leg["verdict"])
                # SPEC-188 §3: which level fired, and its crossing direction per the
                # documented SHORT/LONG stop/tp/zone convention (eval_price_leg's own
                # "SHORT falls from above INTO the zone / LONG rises from below" rule).
                va_level = va_dir = None
                if price_leg["stop_breached"]:
                    va_level = th.get("stop")
                    va_dir = "above" if direction == "SHORT" else "below"
                elif price_leg["tps_printed"]:
                    va_level = price_leg["tps_printed"][0]
                    va_dir = "below" if direction == "SHORT" else "above"
                elif price_leg["entered_zone"] and entry_zone:
                    va_level = entry_zone[1] if direction == "SHORT" else entry_zone[0]
                    va_dir = "below" if direction == "SHORT" else "above"
                venue_agreement = venue_agreement_for(ticker, va_level, va_dir)
                price_leg["venue_agreement"] = venue_agreement
                reasons.append(price_leg["note"] + _tape_tag(venue_agreement))
        if rng and watch_levels:
            breached = eval_watch_levels(watch_levels, rng)
            if breached:
                # SPEC-113: a `trailing_24h_fallback` window means the commit-time cut
                # (SPEC 54) never engaged — no resolvable committed_ts, or the commit
                # predates the kline reach — so "crossed" may be a pre-commit print.
                legacy = str(rng["window"]).startswith("trailing_24h_fallback")
                # SPEC-188 §3: tape-agreement off the FIRST breached watch_level (a
                # WATCH-ARMED episode is usually one trip-wire; the label is annotation
                # only, never a gate on the WATCH-ARMED verdict itself).
                w0 = breached[0]
                venue_agreement = venue_agreement_for(ticker, w0.get("price"), w0.get("dir"))
                watch_leg = {"breached": breached, "high": rng["high"],
                             "low": rng["low"], "window": rng["window"], "legacy": legacy,
                             "venue_agreement": venue_agreement}
                verdicts.append(VERDICT_WATCH_ARMED)
                reasons.append(_watch_armed_note(breached, legacy=legacy) + _tape_tag(venue_agreement))
    price_fired = bool(price_leg and price_leg["verdict"])

    # ── 3b. FUNDING leg (SPEC-142): a committed funding_watch trip-wire, evaluated on
    # the current CROSS-VENUE verified live rate (live.funding_4h — regime_flip.live_perp
    # already floor-rejects, §3). No window/kline fetch — funding has no lookback issue.
    funding_leg = None
    funding_entries, funding_watch_caveats = ([], []) if th_dead else _normalize_funding_watch(th)
    if funding_entries and live is not None:
        fund_4h = live.get("funding_4h")
        breached = eval_funding_watch(funding_entries, fund_4h)
        if breached:
            # Requirement 4: a condition already true AT commit fires once immediately —
            # never silently skipped just because it predates this tick's "first look".
            already = (committed_epoch is not None
                       and (time.time() - committed_epoch) < FUNDING_ALREADY_TRUE_WINDOW_S)
            funding_leg = {"breached": breached, "funding_4h": fund_4h,
                           "already_true_at_commit": already}
            verdicts.append(VERDICT_WATCH_ARMED)
            reasons.append(_funding_armed_note(breached, fund_4h, already))

    # ── 4. Directional join (regime_flip tag + live data) ────────────────────
    # The rf/funding leg only sets the verdict when the price leg is silent —
    # price-leg BREAKS/TRIGGERS always wins (SPEC 39 precedence rule).
    if live is not None and price_fired:
        _, rf_tag, rf_note = rf_classify(tok, live)
        reasons.append(f"rf={rf_tag} (funding leg, demoted by price-leg verdict): {rf_note[:60]}")
    elif live is not None:
        _, rf_tag, rf_note = rf_classify(tok, live)
        px = live.get("price")
        fund_pi = live.get("funding_pi")
        fund_4h = live.get("funding_4h")          # SPEC 11: normalized %/4h is the canonical funding value

        # REGIME_FLIP tag
        if rf_tag == "REGIME_FLIP":
            live_sign = sign_of(fund_4h) if fund_4h is not None else "?"
            # flip against direction → BREAKS; flip confirming direction → TRIGGERS
            if direction == "SHORT" and live_sign == "pos":
                # funding turned positive: longs trapped = confirms SHORT loading
                verdicts.append(VERDICT_TRIGGERS)
                reasons.append(f"REGIME_FLIP confirms SHORT: funding flipped pos ({fund_4h:+.3f}%/4h)")
            elif direction == "SHORT" and live_sign == "neg":
                # funding deeply neg while SHORT → squeeze veto → BREAKS
                verdicts.append(VERDICT_BREAKS)
                reasons.append(f"REGIME_FLIP against SHORT: deep-neg funding ({fund_4h:+.3f}%/4h) = squeeze fuel, short vetoed")
            elif direction == "LONG" and live_sign == "neg":
                # funding flipped neg: shorts paying = confirms LONG/squeeze
                verdicts.append(VERDICT_TRIGGERS)
                reasons.append(f"REGIME_FLIP confirms LONG: funding flipped neg ({fund_4h:+.3f}%/4h) = squeeze fuel")
            elif direction == "LONG" and live_sign == "pos":
                # funding turned positive while LONG → longs paying → BREAKS
                verdicts.append(VERDICT_BREAKS)
                reasons.append(f"REGIME_FLIP against LONG: funding flipped pos ({fund_4h:+.3f}%/4h) = longs trapped/distribution")
            else:
                # mixed/unknown direction — log as CONFIRMS (flip noted but direction unclear)
                verdicts.append(VERDICT_CONFIRMS)
                reasons.append(f"REGIME_FLIP (direction={direction}): {rf_note[:80]}")

        # ZONE_BLOWN tag
        elif rf_tag == "ZONE_BLOWN" and entry_zone and px is not None:
            lo, hi = entry_zone
            if direction == "SHORT" and px < lo * 0.97:
                # price fell into short zone → TRIGGERS
                verdicts.append(VERDICT_TRIGGERS)
                reasons.append(f"ZONE_BLOWN into SHORT entry: px ${px:g} < zone ${lo:g}-${hi:g}")
            elif direction == "SHORT" and px > hi * 1.03:
                # price ran above zone → BREAKS (ran past entry)
                verdicts.append(VERDICT_BREAKS)
                reasons.append(f"ZONE_BLOWN above SHORT zone: px ${px:g} > ${hi:g} (ran past entry)")
            elif direction == "LONG" and px < lo * 0.97:
                # price fell below long zone → BREAKS
                verdicts.append(VERDICT_BREAKS)
                reasons.append(f"ZONE_BLOWN below LONG zone: px ${px:g} < ${lo:g} (cascaded past)")
            elif direction == "LONG" and px > hi * 1.03:
                # price ran above long zone → TRIGGERS (LONG fired)
                verdicts.append(VERDICT_TRIGGERS)
                reasons.append(f"ZONE_BLOWN above LONG zone: px ${px:g} > ${hi:g} (entry zone printed)")
            else:
                verdicts.append(VERDICT_CONFIRMS)
                reasons.append(f"ZONE_BLOWN: {rf_note[:80]}")

        elif rf_tag in ("CONFIRM", "DUST", "NO_PERP", "FUNDING_UNAVAILABLE", "FUNDING_SUSPECT"):
            # FUNDING_SUSPECT (SPEC 44): funding leg degraded (lone-venue floor) — verdict still
            # CONFIRMS off the rest, but the reason carries the tag so it's not read as a flat CONFIRM.
            # all_floor tag appended compactly so it survives the note truncation (DoD: reason carries it).
            verdicts.append(VERDICT_CONFIRMS)
            floor_tag = " [all_floor]" if live.get("all_floor") else ""
            reasons.append(f"rf={rf_tag}: {rf_note[:80]}{floor_tag}")

    else:
        # no live data — still confirm from existing signals
        if not verdicts:
            verdicts.append(VERDICT_CONFIRMS)
            reasons.append("no live perp data — cannot classify, defaulting CONFIRMS")

    # ── 5. Precedence resolution ─────────────────────────────────────────────
    PRECEDENCE = {VERDICT_BREAKS: 0, VERDICT_TRIGGERS: 1,
                  VERDICT_WATCH_ARMED: 2, VERDICT_CONFIRMS: 3}
    final = min(verdicts, key=lambda v: PRECEDENCE.get(v, 99)) if verdicts else VERDICT_CONFIRMS
    reason = "; ".join(reasons) if reasons else "no signals"

    # ── 5b. THESIS-DRIFT (SPEC-91): has live price run past the whole committed structure?
    # A READ — it does NOT change the verdict (§0.5). When stale, the action prompt LEADS the
    # reason, before any echoed (now-stale) commit-time note, which is tagged [commit-time, stale].
    drift = thesis_drift(th, (live or {}).get("price")) if not th_dead else None
    drift_field = None
    if drift and drift["stale"]:
        drift_field = {**drift, "committed_ts": th.get("committed_ts")}
        reason = f"{_drift_prompt(drift)} | [commit-time, stale] {reason}"

    # ── 6. Inbox alerts (SPEC 45) — surfaced, NEVER a verdict input (§0.5):
    # an on-chain escalation is data for the Designer unless it matches a named
    # invalidation field; the verdict above is already final.
    try:
        ib = inbox.alerts_for(ticker)
        alerts = {"n": ib["n"], "max_severity": ib["max_severity"]}
        if ib["max_severity"] == "HIGH":
            reason += f" | 🔴 {ib['n']} unconsumed HIGH alert(s): {ib['events'][-1]['msg'][:70]}"
    except Exception:
        alerts = {"n": 0, "max_severity": None}

    result = {
        "ticker":        ticker,
        "verdict":       final,
        "reason":        reason,
        "direction":     direction,
        "alerts":        alerts,
        "thesis_present": th_present,
        # SPEC-149: canonical signature (direction-guarded) — the input `tier` derives from.
        "signature":     LG.canon_signature(p.signature, direction) if p.signature else None,
        "price_leg":     ({"high": price_leg["high"], "low": price_leg["low"],
                           "stop_breached": price_leg["stop_breached"],
                           "tps_printed": price_leg["tps_printed"],
                           "entered_zone": price_leg["entered_zone"],
                           "window": price_leg["window"],
                           "venue_agreement": price_leg.get("venue_agreement")} if price_leg else None),
        "watch_leg":     watch_leg,   # SPEC 77: {breached:[{price,dir,note}], high, low, window,
                                      # venue_agreement} | None — venue_agreement is SPEC-188 §3
        "funding_leg":   funding_leg,   # SPEC-142: {breached:[{threshold_4h,op,note}], funding_4h,
                                        # already_true_at_commit} | None
        "funding_watch_caveats": funding_watch_caveats,   # SPEC-142: [] when clean, never silently dropped
        "thesis_drift":  drift_field, # SPEC-91: {stale,live_price,nearest_anchor,...} | None
        "live":          live,
        "caveats":       caveats or None,  # SPEC-146: dropped geometry fields (any type)
    }

    # ── 7. Sector-divergence Cat A prior (SPEC-114) — read-only annotation, never a
    # gate (§0.6): SOLO_PUMP is a manipulation prior, SECTOR_MOVE flags sector beta so
    # a breakout candle doesn't misread as operator escalation. Omitted entirely for
    # tokens with no basket mapping (regression: unmapped rows stay byte-identical).
    chg24 = (live or {}).get("chg24") if live else None
    if chg24 is not None:
        try:
            import sector_divergence as _sd
            tag = _sd.tag_for(ticker, chg24, window_days=1)
            if tag:
                result["sector"] = tag
        except Exception:  # noqa: BLE001 — a prior must never break the classify verdict
            pass

    return result


# ─────────────────────────────────────────────────────────────────────────────
# --migrate: bootstrap thesis blocks
# ─────────────────────────────────────────────────────────────────────────────

def _detect_setup(tok):
    """Guess setup type from state text."""
    memo = (tok.get("state", "") + " " + tok.get("prev_state", "")).lower()
    for kw, st in SETUP_KEYWORDS.items():
        if kw in memo:
            return st
    return "scalp"


def _parse_committed_ts(tok):
    """Best-effort extraction of committed date from memo or regime.ts."""
    memo = tok.get("state", "") + " " + tok.get("prev_state", "")
    # look for YYYY-MM-DD in memo
    m = re.search(r"202\d-\d{2}-\d{2}", memo)
    if m:
        return m.group(0)
    reg = tok.get("regime", {})
    if reg.get("ts"):
        return reg["ts"]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_stops_tps(memo):
    """Extract stop and TP prices from memo text like 'stop $1.13' / 'TPs $7.00/$6.80/$6.56'."""
    stop = None
    tps  = []
    # stop: "stop $X.XX" or "stop: $X.XX"
    m = re.search(r"\bstop\b[:\s]+\$?([\d.]+)", memo, re.I)
    if m:
        try: stop = float(m.group(1))
        except ValueError: pass
    # TPs: "TPs $7.00/$6.80/$6.56" or "TP $X" or "tp1 $X"
    m2 = re.search(r"\btp[s12345]?\b[:\s]*\$?([\d./]+)", memo, re.I)
    if m2:
        for part in m2.group(1).split("/"):
            try: tps.append(float(part.strip().lstrip("$")))
            except ValueError: pass
    return stop, tps


def migrate_thesis(dry_run=False):
    """Bootstrap tokens[].thesis blocks from state+regime. Returns count written."""
    raw = json.loads(WL_PATH.read_text())
    tokens = raw.get("tokens", raw) if isinstance(raw, dict) else raw

    board = []

    for tok in tokens:
        if tok.get("thesis"):
            board.append((tok["ticker"], "SKIP", "thesis block already present"))
            continue

        ticker  = tok.get("ticker", "?")
        # If state starts with [REGIME_FLIP or [ZONE_BLOWN, parse thesis from prev_state
        state_raw = tok.get("state", "")
        prev_state = tok.get("prev_state", "")
        memo_for_thesis = prev_state if (
            state_raw.startswith("[REGIME_FLIP") or state_raw.startswith("[ZONE_BLOWN")
        ) else state_raw

        direction = memo_direction(memo_for_thesis)
        if direction in ("?", "PASS", "MIXED"):
            # stub with _needs_review
            th = {
                "direction":     direction,
                "committed_ts":  _parse_committed_ts(tok),
                "committed_by":  "regime_flip-migration",
                "_needs_review": True,
                "_reason":       f"memo_direction={direction} — ambiguous, review manually",
            }
            board.append((ticker, "STUB", f"direction={direction} (ambiguous)"))
        else:
            zone  = memo_zone(memo_for_thesis)
            stop, tps = _parse_stops_tps(memo_for_thesis)
            setup = _detect_setup(tok)
            ts    = _parse_committed_ts(tok)
            h     = TIME_STOP[setup]

            has_levels = zone is not None or stop is not None or tps
            if not has_levels:
                th = {
                    "direction":     direction,
                    "setup":         setup,
                    "committed_ts":  ts,
                    "time_stop_h":   h,
                    "committed_by":  "regime_flip-migration",
                    "_needs_review": True,
                    "_reason":       "no stop/TP/zone parsed — add levels manually",
                }
                board.append((ticker, "STUB", f"dir={direction} setup={setup} — no levels"))
            else:
                th = {
                    "direction":    direction,
                    "setup":        setup,
                    "committed_ts": ts,
                    "time_stop_h":  h,
                    "committed_by": "regime_flip-migration",
                }
                if zone:
                    th["entry_zone"] = list(zone)
                if stop:
                    th["stop"] = stop
                if tps:
                    th["tps"] = tps
                board.append((ticker, "WRITE", f"dir={direction} setup={setup} zone={zone} stop={stop} tps={tps}"))

        tok["thesis"] = th

    # print review board
    print(f"\n{'DRY-RUN: ' if dry_run else ''}═══ MIGRATE BOARD — {len(board)} tokens ═══\n")
    icons = {"SKIP": "⚪", "STUB": "🟡", "WRITE": "🟢"}
    for tkr, action, detail in board:
        print(f"  {icons.get(action,'·')} {action:5s}  {tkr:9s}  {detail}")

    if not dry_run:
        # backup first
        bak = WL_PATH.parent / (WL_PATH.name + ".bak")
        shutil.copy2(WL_PATH, bak)
        WL_PATH.write_text(json.dumps(raw, indent=2))
        written = sum(1 for _, a, _ in board if a in ("WRITE", "STUB"))
        print(f"\n✍  Wrote {written} thesis block(s) — backup at {bak}")
        return written
    else:
        print(f"\n(dry-run: no files written)")
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# --arm: print arm_setup.py command(s)
# ─────────────────────────────────────────────────────────────────────────────

def arm_commands(ticker, tokens):
    """Print the arm_setup.py command(s) to launch this token's thesis triggers."""
    tok = next((t for t in tokens if t.get("ticker", "").upper() == ticker.upper()), None)
    if tok is None:
        print(f"  {ticker} not found in watchlist"); return

    th = tok.get("thesis")
    if not th:
        print(f"  {ticker}: no thesis block — run --migrate first (or add manually)")
        return

    direction = th.get("direction", "?").lower()
    if direction not in ("short", "long"):
        print(f"  {ticker}: direction '{direction}' not short/long — cannot build arm command"); return

    setup     = th.get("setup", "scalp")
    stop      = th.get("stop")
    tps       = th.get("tps", [])
    entry_zone = th.get("entry_zone")
    fund_guard = th.get("funding_guard")

    parts = [f"python3 scripts/arm_setup.py {ticker.upper()}",
             f"--dir {direction}",
             f"--setup {setup}"]

    # trigger mode
    if entry_zone:
        lvl = entry_zone[0] if direction == "short" else entry_zone[1]
        parts.append(f"--trigger held-break:{lvl}")
    else:
        parts.append("--trigger self-arming")

    if stop:
        parts.append(f"--stop {stop}")
    else:
        print(f"  WARNING: {ticker} thesis has no stop — add --stop <price> manually")
        parts.append("--stop ???")

    if tps:
        parts.append(f"--tp {','.join(str(x) for x in tps)}")
    if fund_guard is not None:
        parts.append(f"--funding-guard {fund_guard}")

    cmd = " \\\n     ".join(parts)
    print(f"\n  # {ticker} — {direction.upper()} ({setup}, {th.get('time_stop_h','?')}h horizon)")
    print(f"  {cmd}")
    print(f"\n  # To run in background:")
    log_name = f"/tmp/arm_{ticker.lower()}_{direction}.log"
    print(f"  nohup {cmd.replace(chr(10),'').replace('     ','')} > {log_name} 2>&1 &")
    print(f"  tail -n 0 -f {log_name} | grep -E '🔴 ENTRY|🎯|INVALIDATED|FUNDING-GUARD|TIME-STOP'")


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

VERDICT_ORDER = {VERDICT_BREAKS: 0, VERDICT_TRIGGERS: 1,
                 VERDICT_WATCH_ARMED: 2, VERDICT_CONFIRMS: 3}
VERDICT_ICON  = {VERDICT_BREAKS: "🔴", VERDICT_TRIGGERS: "🟡",
                 VERDICT_WATCH_ARMED: "👁", VERDICT_CONFIRMS: "🟢"}


def _compact_venue_agreement(va):
    """Drop the raw per-venue `crossed`/`closed_beyond` name arrays — keep the
    label + counts, the only part a verdict/reason ever reads (SPEC-193)."""
    if not va:
        return va
    return {"label": va.get("label"), "n_crossed": va.get("n_crossed"),
            "n_total": va.get("n_total")}


def _compact_leg(leg):
    if not leg:
        return leg
    out = dict(leg)
    if out.get("venue_agreement"):
        out["venue_agreement"] = _compact_venue_agreement(out["venue_agreement"])
    return out


def compact_row(row):
    """SPEC-193: the decision-only board row — same fields the full JSON row carries,
    `reason` UNTRUNCATED, minus `leverage_state` (mostly UNKNOWN placeholders today,
    not a decision key) and the bulky raw venue-name arrays inside price_leg/
    watch_leg's `venue_agreement` (kept as label+counts). Never emits a null
    placeholder key — a field absent from `row` (or explicitly None) is simply
    omitted, matching the existing `oic` convention. `False`/`0` values ARE kept
    (e.g. `retire_flag: false`, `aster_listed: false` are decision-relevant)."""
    out = {
        "ticker": row.get("ticker"), "verdict": row.get("verdict"),
        "reason": row.get("reason"), "direction": row.get("direction"),
        "thesis_present": row.get("thesis_present"),
        "signature": row.get("signature"), "tier": row.get("tier"),
        "operator_not_done": row.get("operator_not_done"),
        "operator_veto_reason": row.get("operator_veto_reason"),
        "retire_flag": row.get("retire_flag"),
        "aster_listed": row.get("aster_listed"),
        "live_price": row.get("live_price"),
        "oic": row.get("oic"),
        "battlefield": row.get("battlefield"),
        "oi_mc_flag": row.get("oi_mc_flag"),
        "oi_mc_caveat": row.get("oi_mc_caveat"),
        "funding_leg": row.get("funding_leg"),
        "funding_watch_caveats": row.get("funding_watch_caveats"),
        "thesis_drift": row.get("thesis_drift"),
        "price_leg": _compact_leg(row.get("price_leg")),
        "watch_leg": _compact_leg(row.get("watch_leg")),
        "caveats": row.get("caveats"),
        "alerts": row.get("alerts"),
    }
    return {k: v for k, v in out.items() if v is not None}


def compact_board(out_rows, board_meta=None):
    """SPEC-193: the compact board envelope — `board_meta` unchanged (already small:
    a handful of scalars + short lists), only the rows are reduced."""
    return {"board": [compact_row(r) for r in out_rows], "meta": board_meta}


def parse_render_mode(args):
    """SPEC-193: pull `--render <mode>` (full|compact, default full) out of the raw
    argv list, returning (mode, remaining_args) with BOTH the flag and its value
    token stripped — so the value token ("compact") never falls through to the bare
    positional TICKER parser below and gets mistaken for a ticker."""
    if "--render" not in args:
        return "full", list(args)
    idx = args.index("--render")
    mode = args[idx + 1] if idx + 1 < len(args) else "full"
    rest = args[:idx] + args[idx + 2:]
    return mode, rest


def main():
    args  = sys.argv[1:]
    as_json   = "--json"      in args
    breaks_only = "--breaks"  in args
    do_migrate  = "--migrate" in args
    dry_run     = "--dry-run" in args
    arm_flag    = "--arm"     in args
    render_mode, args = parse_render_mode(args)

    # --arm <TICKER>
    if arm_flag:
        idx = args.index("--arm")
        arm_ticker = args[idx + 1].upper() if idx + 1 < len(args) else None
        if not arm_ticker:
            print("Usage: classify.py --arm <TICKER>"); sys.exit(1)
        tokens, err = load_watchlist()
        if err:
            print(f"ERROR loading watchlist: {err}"); sys.exit(1)
        arm_commands(arm_ticker, tokens)
        return

    # --migrate [--dry-run]
    if do_migrate:
        migrate_thesis(dry_run=dry_run)
        return

    # parse positional TICKER args (not flags)
    tickers = [a.upper() for a in args if not a.startswith("-")]

    tokens, err = load_watchlist()
    if err:
        print(f"ERROR loading watchlist: {err}"); sys.exit(1)

    if tickers:
        tokens_run = [t for t in tokens if t.get("ticker", "").upper() in tickers]
    else:
        tokens_run = tokens

    # SPEC 49: board runs fetch each venue ONCE (bulk snapshot) instead of ~5 HTTP
    # calls per token; single-ticker runs keep the per-symbol path (snapshot costs
    # more than it saves for one name). Snapshot failure degrades to per-symbol.
    snap_on = False
    if len(tokens_run) >= 3:
        try:
            load_venue_snapshot([f"{t.get('ticker', '?')}USDT" for t in tokens_run])
            snap_on = True
        except Exception:
            snap_on = False

    results = []
    for tok in tokens_run:
        tk = tok.get("ticker", "?")
        try:
            live = live_perp(tk)
        except Exception as ex:
            live = None
        try:
            result = classify_token(tok, live)
        except Exception as ex:
            result = {
                "ticker": tk, "verdict": VERDICT_CONFIRMS,
                "reason": f"classify error: {ex}",
                "direction": "?", "thesis_present": False, "live": None,
            }
        results.append(result)
        if not snap_on:
            time.sleep(0.1)   # rate-limit politeness only matters on the per-symbol path

    results.sort(key=lambda r: (VERDICT_ORDER.get(r["verdict"], 99), r["ticker"]))

    # SPEC 64: the board (full sweep, no positional filter) carries the dead-man switch —
    # stale-surveil prefixes every row's reason + fires one HIGH inbox event per episode.
    board_meta = annotate_deadman(results) if not tickers else None
    # SPEC-95: prefix any row with an unlock ≤7d out with the ⏰ UNLOCK T-Nd flag (board AND
    # single-ticker — a scheduled supply event must never be discovered reactively).
    annotate_unlocks(results)
    # SPEC-98: a live SHORT thesis row echoes the persisted rotation-freshness verdict
    # (FRESH/FROZEN/ROTATED) — the VELVET false-pause read, kept visible on every tick.
    annotate_freshness(results)
    # SPEC-99: a row whose ticker shares an auto-built operator cluster with another
    # watchlist name carries the §7 operator-heat note (cluster-mates are ONE position).
    annotate_cluster_heat(results)
    # SPEC-122: OI/MC "perp-casino" flag — the ledger's worst signature (blowoff-top
    # short, -26.3R/324) named on sight; caveats a SHORT-direction row, never blocks.
    annotate_oi_mc(results)
    # SPEC-180 req 5: the ONE compact oic: field, printed only when non-UNKNOWN or a
    # noteworthy degradation — never boards of UNKNOWN noise.
    annotate_oi_construction_compact(results)
    # SPEC-136: execution-venue (Aster) fillability veto — a page for a trade the user
    # cannot fill is worse than no page (COTI: clean signal, un-executable). One fetch
    # for the whole board, never per-ticker.
    annotate_aster_listed(results, tokens_run)
    # SPEC-149: operator_not_done live veto (⛔ VETO prefix, advisory) + tier
    # (tradeable|tracking, derived from the ledger's earned set + aster_listed + liquidity).
    # Runs for every row regardless of thesis presence/direction — decoupled from any
    # live-SHORT-thesis gate (req 6).
    annotate_operator_veto(results)
    annotate_tier(results)
    # SPEC-140: mechanical retire staleness flag (null when healthy) — the engine half of
    # the CLAUDE.md §0.5 standing retire-policy; advisory only, never the verdict.
    annotate_retire_flags(results, tokens_run)
    if not tickers:
        flagged = [r["ticker"] for r in results if r.get("retire_flag")]
        board_meta["retire_flagged"] = flagged
        board_meta["retire_flagged_count"] = len(flagged)
        # SPEC-149 req 5: tradeable vs tracking counts, surfaced at session-open — a
        # tracked name must never look like a trade merely by being on the board.
        board_meta["tier_tradeable_count"] = sum(1 for r in results if r.get("tier") == "tradeable")
        board_meta["tier_tracking_count"] = sum(1 for r in results if r.get("tier") == "tracking")
        board_meta["operator_veto_count"] = sum(1 for r in results if r.get("operator_not_done") is True)
        # SPEC-146 req 3 (generalizes SPEC-144 req 5): board-level thesis-geometry
        # invariant, surfaced loudly at session-open (the orchestrator's `classify {}`
        # board read) — never silent, never a verdict input.
        geometry_violations = TH.check_board(tokens_run)
        board_meta["thesis_geometry_violations"] = geometry_violations
    # SPEC 77: a breached watch_level fires ONE inbox event per episode (deduped on a cursor)
    # so a WATCH-ARMED trip-wire survives a missed session without nagging every board tick.
    if not tickers:
        fire_watch_armed(results)
        # SPEC-142: mirrors fire_watch_armed for the funding leg.
        fire_funding_armed(results)
        # SPEC-91: a thesis that has drifted past its whole committed structure fires ONE HIGH
        # inbox event per (ticker, committed_ts) episode (re-anchoring resets it).
        fire_thesis_drift(results)

    # ── single ticker detail ──────────────────────────────────────────────────
    if tickers and len(tickers) == 1 and not as_json:
        r   = results[0] if results else None
        if r is None:
            print(f"  {tickers[0]} not found"); return
        tok = next((t for t in tokens if t.get("ticker","").upper() == tickers[0]), {})
        print(f"\n{'═'*60}")
        print(f"  {VERDICT_ICON[r['verdict']]} {r['verdict']}  —  {r['ticker']}  (dir={r['direction']})")
        print(f"{'═'*60}")
        print(f"  reason:  {r['reason']}")
        if r['live']:
            lv = r['live']
            px  = f"${lv['price']:g}" if lv.get('price') else '—'
            chg = f"{lv['chg24']:+.1f}%" if lv.get('chg24') is not None else '?'
            fi  = f"{lv['funding_4h']:+.3f}%/4h" if lv.get('funding_4h') is not None else 'n/a'
            vol = f"${lv['vol_m']:.1f}M" if lv.get('vol_m') is not None else '?'
            print(f"  live:    {px}  {chg}  fund {fi}  vol {vol}")
        print(f"  thesis:  {'present' if r['thesis_present'] else 'absent (memo fallback)'}")
        if r.get("price_leg"):
            pl = r["price_leg"]
            tps = "/".join(f"{t:g}" for t in pl["tps_printed"]) or "—"
            print(f"  price:   window {pl['window']}  high {pl['high']:g} / low {pl['low']:g}"
                  f"  stop_breached={pl['stop_breached']}  tps_printed={tps}")
        if r.get("watch_leg"):
            wlg = r["watch_leg"]
            lvls = ", ".join(f"{w['price']:g} {w['dir']}" + (f" ({w['note']})" if w.get('note') else "")
                             for w in wlg["breached"])
            print(f"  watch:   👁 ARMED — {lvls}  [window {wlg['window']} "
                  f"high {wlg['high']:g} / low {wlg['low']:g}]")
        if r.get("thesis_drift"):
            d = r["thesis_drift"]
            print(f"  drift:   ⏳ STALE — live {d['live_price']:g} {d['nearest_dist_pct']:g}% past "
                  f"nearest {d['nearest_anchor']:g} / furthest {d['furthest_anchor']:g} — RE-ANCHOR")
        print(f"  state:   {tok.get('state','')[:120]}")
        # show trigger log excerpts
        _, break_lines = scan_trigger_logs(r['ticker'])
        entry_lines, _ = scan_trigger_logs(r['ticker'])
        if entry_lines:
            print(f"\n  last ENTRY/ARMED log line:")
            print(f"    {entry_lines[-1][1][:100]}")
        if break_lines:
            print(f"  last BREAK log line:")
            print(f"    {break_lines[-1][1][:100]}")
        print()
        return

    # ── JSON mode ─────────────────────────────────────────────────────────────
    if as_json:
        out = [{"ticker": r["ticker"], "verdict": r["verdict"],
                "reason": r["reason"], "thesis_present": r["thesis_present"],
                "alerts": r.get("alerts"),
                "price_leg": r.get("price_leg"),
                "watch_leg": r.get("watch_leg"),
                "thesis_drift": r.get("thesis_drift"),
                "oi_mc_ratio": r.get("oi_mc_ratio"),
                "oi_mc_flag": r.get("oi_mc_flag"),
                "oi_mc_caveat": r.get("oi_mc_caveat"),
                "perp_spot_ratio": r.get("perp_spot_ratio"),        # SPEC-177
                "battlefield": r.get("battlefield"),                # SPEC-177
                "leverage_state": r.get("leverage_state"),          # SPEC-177
                "retire_flag": r.get("retire_flag"),
                "aster_listed": r.get("aster_listed"),
                "live_price": (r.get("live") or {}).get("price"),
                "funding_leg": r.get("funding_leg"),
                "funding_watch_caveats": r.get("funding_watch_caveats"),
                "signature": r.get("signature"),
                "operator_not_done": r.get("operator_not_done"),
                "operator_veto_reason": r.get("operator_veto_reason"),
                "tier": r.get("tier"),
                "caveats": r.get("caveats")}
               for r in results]
        # SPEC-180 req 5: `oic` key ONLY present when the row actually has a
        # printable compact line — "UNKNOWN rows carry NO oic: field" (never a null
        # placeholder key on every row).
        for out_row, r in zip(out, results):
            if "oic" in r:
                out_row["oic"] = r["oic"]
        if render_mode == "compact":
            # SPEC-193: decision-only rows, no pretty-print indent (every byte here is
            # a prompt-cache write on the orchestrator's session).
            if tickers:
                compact_out = [compact_row(r) for r in out]
                print(json.dumps(compact_out[0] if len(compact_out) == 1 else compact_out))
            else:
                print(json.dumps(compact_board(out, board_meta)))
            return
        if tickers:
            # explicit ticker/subset — preserve the per-ticker contract (object or list)
            print(json.dumps(out[0] if len(out) == 1 else out, indent=2))
        else:
            # SPEC 64: the board carries meta (surveil_age_h + dead-man flags)
            print(json.dumps({"board": out, "meta": board_meta}, indent=2))
        return

    # ── board mode ────────────────────────────────────────────────────────────
    if breaks_only:
        # WATCH-ARMED rides the triage view too — a trip-wire that crossed is actionable (SPEC 77)
        results = [r for r in results if r["verdict"] in
                   (VERDICT_BREAKS, VERDICT_TRIGGERS, VERDICT_WATCH_ARMED)]

    n_b = sum(1 for r in results if r["verdict"] == VERDICT_BREAKS)
    n_t = sum(1 for r in results if r["verdict"] == VERDICT_TRIGGERS)
    n_w = sum(1 for r in results if r["verdict"] == VERDICT_WATCH_ARMED)
    n_c = sum(1 for r in results if r["verdict"] == VERDICT_CONFIRMS)

    print(f"\n{'═'*72}")
    print(f"  CLASSIFY BOARD — {len(results)} tokens   "
          f"{n_b} BREAKS  {n_t} TRIGGERS  {n_w} WATCH-ARMED  {n_c} CONFIRMS   "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if board_meta and (board_meta["surveil_stale"] or board_meta["board_tick_stale"]):
        sv = (f"surveil {board_meta['surveil_age_h']}h" if board_meta["surveil_age_h"] is not None
              else "surveil never-ran") if board_meta["surveil_stale"] else ""
        bt = (f"board_tick {board_meta['board_tick_age_h']}h"
              if board_meta["board_tick_stale"] else "")
        print(f"  🚨 DEAD-MAN: {' · '.join(x for x in (sv, bt) if x)} silent (>2× cadence) "
              f"— alerts NOT being generated, restore the agent")
    if board_meta and board_meta.get("thesis_geometry_violations"):
        bad = ", ".join(v["ticker"] for v in board_meta["thesis_geometry_violations"])
        print(f"  🚨 THESIS GEOMETRY INVARIANT: malformed field(s) dropped on {bad} "
              f"— WATCH-ARMED/thesis_drift/tape sweep are blind on these, fix the thesis")
    print(f"{'═'*72}\n")

    for r in results:
        lv    = r.get("live") or {}
        px    = f"${lv['price']:g}" if lv.get('price') else '—'
        fund  = f"f{lv['funding_4h']:+.3f}%/4h" if lv.get('funding_4h') is not None else ''
        icon  = VERDICT_ICON.get(r["verdict"], "·")
        dir_s = f"[{r['direction']:5s}]"
        # one-line compact
        print(f"  {icon} {r['verdict']:8s} {r['ticker']:9s} {dir_s} {px:10s} {fund:12s} {r['reason'][:65]}")
        if r.get("oic"):   # SPEC-180 req 5: only when the row actually has one
            print(f"      {r['oic']}")

    print()


if __name__ == "__main__":
    main()
