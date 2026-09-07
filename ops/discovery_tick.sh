#!/usr/bin/env bash
# discovery_tick.sh — roadmap Phase 0c: put the DARK detectors on a cadence.
#
# Everything here already existed as a working capability with ZERO scheduled caller:
#   - scan --mode faded_bounce : the user's PRIMARY edge (CLAUDE.md §0.1), previously only
#     run when a human remembered at session-open.
#   - local_index --tick       : the SPEC-102 indexer whose docstring says "Ops wires the
#     schedule separately" — nothing ever did, so the index never ingested and the Moralis
#     quota strategy (GOAL G2) was void in practice.
#   - distribution_radar / accumulation_radar : discovery sweeps with no loop.
#
# Cadence-gated internally (marker files) so one launchd/cron entry can carry several
# different periods. Every leg is best-effort: a failing leg logs and never blocks the rest.
# Pages go through ops/notify.sh, routed `desk` (SPEC-157: desktop only, no ntfy phone
# leg — a name surfaced by a radar sweep and not on the board is desk-homework, not a
# user decision).
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
mkdir -p state
LOG="state/discovery_tick.log"
ts() { date -u +%FT%TZ; }
log() { echo "$(ts) $*" >> "$LOG"; }

# due <marker> <minutes> — true when the marker is missing or older than N minutes
due() {
  local m="state/.$1" mins="$2"
  [ ! -f "$m" ] || [ -n "$(find "$m" -mmin +"$mins" 2>/dev/null)" ]
}
touch_marker() { touch "state/.$1"; }

# ── 1. FADED-BOUNCE sweep (primary edge) — every 6h ─────────────────────────────
if due fb_sweep_last 360; then
  out="$(python3 capabilities/scan.py --mode faded_bounce --json 2>>state/discovery_tick.err)"
  if [ -n "$out" ]; then
    printf '%s' "$out" > state/faded_bounce_latest.json
    n="$(printf '%s' "$out" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); d=d.get("data",d)
    rows=d.get("candidates") or d.get("faded_bounce") or d.get("rows") or []
    print(len(rows))
except Exception: print("ERR")' 2>/dev/null || echo ERR)"
    log "faded_bounce candidates=$n"
    if [ "$n" != "0" ] && [ "$n" != "ERR" ] && [ -n "$n" ]; then
      names="$(printf '%s' "$out" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); d=d.get("data",d)
    rows=d.get("candidates") or d.get("faded_bounce") or d.get("rows") or []
    print(", ".join(str(r.get("ticker","?")) for r in rows[:6]))
except Exception: print("see state/faded_bounce_latest.json")' 2>/dev/null)"
      bash ops/notify.sh "crime-desk — FADED-BOUNCE sweep" "$n candidate(s): $names — primary-edge sweep, read before committing" "" "desk" || true
    fi
    touch_marker fb_sweep_last
  else
    log "faded_bounce EMPTY OUTPUT (capability failed — see discovery_tick.err)"
  fi
fi

# ── 1b. OI-SURGE net (SPEC-148) — every 6h ──────────────────────────────────────
# Cross-sectional discovery: retires the manual loris.tools/markets browse. Pages only on a
# high-conviction hit (>=3 signals firing, or a fresh Aster/Bitget listing) — this net must
# not become spam.
if due oi_surge_last 360; then
  out="$(python3 capabilities/scan.py --mode oi_surge --json 2>>state/discovery_tick.err)"
  if [ -n "$out" ]; then
    printf '%s' "$out" > state/oi_surge_latest.json
    hits="$(printf '%s' "$out" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); d=d.get("data",d)
    rows=d.get("candidates") or []
    hits=[r for r in rows if len(r.get("flags") or []) >= 3
          or (r.get("new_listing") and r["new_listing"].get("venue") in ("aster","bitget"))]
    print(", ".join(f'"'"'{r.get("ticker","?")}[{"+".join(r.get("flags") or [])}]'"'"' for r in hits[:6]))
except Exception: print("")' 2>/dev/null)"
    log "oi_surge ok hits=${hits:-none}"
    [ -n "$hits" ] && { bash ops/notify.sh "crime-desk — OI-SURGE net" "$hits" "" "desk" || true; }
    touch_marker oi_surge_last
  else
    log "oi_surge EMPTY OUTPUT (capability failed — see discovery_tick.err)"
  fi
fi

# ── 1c. KR LISTINGS tripwire (SPEC-153) — every 15min ───────────────────────────
# Listing pumps move in minutes, not hours — 6h would miss the trade entirely. Pages
# only KR_LISTING_NOTICE/KR_DELISTING_NOTICE on a tracked-or-perp-listed symbol
# (kr_listings.py's own `page` flag); everything else (KR_LISTED, KR_FLAG_CHANGE,
# untracked notices) rides the JSON feed inbox-only. A degraded source (e.g. the
# Upbit announcements Cloudflare 403) is logged LOUD, never a silent smaller sweep.
if due kr_listings_last 15; then
  out="$(python3 capabilities/kr_listings.py sweep --json 2>>state/discovery_tick.err)"
  if [ -n "$out" ]; then
    printf '%s' "$out" > state/kr_listings_latest.json
    summary="$(printf '%s' "$out" | python3 -c 'import sys,json
try:
    d = json.load(sys.stdin)
    first_run = d.get("first_run")
    degraded = [x.get("source") for x in (d.get("degraded_sources") or [])]
    events = d.get("events") or []
    paged = [e for e in events if e.get("page")]
    print("first_run={} events={} paged={} degraded={}".format(
        first_run, len(events), len(paged), degraded))
    for e in paged:
        print("PAGE", e.get("class"), e.get("ticker"), "-", e.get("msg"))
except Exception as ex: print("ERR", ex)' 2>/dev/null)"
    log "kr_listings ${summary:-ERR}"
    page_lines="$(printf '%s' "$summary" | grep '^PAGE ' | sed 's/^PAGE //')"
    if [ -n "$page_lines" ]; then
      bash ops/notify.sh "crime-desk — KR listing/delisting tripwire" "$page_lines" "high" "desk" || true
    fi
    touch_marker kr_listings_last
  else
    log "kr_listings EMPTY OUTPUT (capability failed — see discovery_tick.err)"
  fi
fi

# ── 2. DISTRIBUTION radar — every 12h ───────────────────────────────────────────
if due dist_radar_last 720; then
  out="$(python3 capabilities/distribution_radar.py --json 2>>state/discovery_tick.err)"
  if [ -n "$out" ]; then
    printf '%s' "$out" > state/distribution_radar_latest.json
    hi="$(printf '%s' "$out" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); d=d.get("data",d)
    rows=d.get("alerts") or d.get("candidates") or d.get("rows") or []
    hits=[r for r in rows if str(r.get("severity","")).upper()=="HIGH" or str(r.get("tier","")).upper()=="HIGH"]
    print("|".join(f'"'"'{r.get("ticker","?")}:{r.get("label") or r.get("address","")[:10]}'"'"' for r in hits[:5]))
except Exception: print("")' 2>/dev/null)"
    log "distribution_radar ok high=${hi:-none}"
    [ -n "$hi" ] && { bash ops/notify.sh "crime-desk — DISTRIBUTION radar" "HIGH: $hi" "" "desk" || true; }
    touch_marker dist_radar_last
  else
    log "distribution_radar EMPTY OUTPUT"
  fi
fi

# ── 3. ACCUMULATION radar — every 12h (G1: hypothesis-tier, never sized) ────────
if due accum_radar_last 720; then
  out="$(python3 capabilities/accumulation_radar.py --json 2>>state/discovery_tick.err)"
  if [ -n "$out" ]; then
    printf '%s' "$out" > state/accumulation_radar_latest.json
    imm="$(printf '%s' "$out" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); d=d.get("data",d)
    rows=d.get("candidates") or d.get("alerts") or d.get("rows") or []
    hits=[r for r in rows if str(r.get("tier","")).upper()=="IMMINENT"]
    print(", ".join(str(r.get("ticker","?")) for r in hits[:5]))
except Exception: print("")' 2>/dev/null)"
    log "accumulation_radar ok imminent=${imm:-none}"
    [ -n "$imm" ] && { bash ops/notify.sh "crime-desk — ACCUMULATION radar" "IMMINENT: $imm — G1 hypothesis-tier, scout size only" "" "desk" || true; }
    touch_marker accum_radar_last
  else
    log "accumulation_radar EMPTY OUTPUT"
  fi
fi

# ── 3b. COUNTERFACTUAL scoring (SPEC-81) — every 12h ────────────────────────────
# The desk's paper track record: scores every committed thesis against FORWARD klines and
# writes ledger rows tagged source=counterfactual. This is the only way the desk accumulates
# per-signature evidence when the user is NOT trading (user directive 2026-08-19: "gather data
# while I'm away"). Never merged with live rows — the §9 GO gate still counts FILLED trades only.
# Note: theses committed WITHOUT entry/stop/tp geometry are skipped (`no_geometry`) — a
# paramless WATCH is unscoreable, which is why every commit must carry restable params.
if due counterfactual_last 720; then
  out="$(python3 capabilities/counterfactual.py '{"action":"backfill"}' --json 2>>state/discovery_tick.err)"
  if [ -n "$out" ]; then
    printf '%s' "$out" > state/counterfactual_latest.json
    log "counterfactual $out"
    touch_marker counterfactual_last
  else
    log "counterfactual EMPTY OUTPUT"
  fi
fi

# ── 3c. OPERATOR VETO sweep (SPEC-149) — every 1h ───────────────────────────────
# The §0.6 counterparty read as a LIVE veto: operator_not_done recomputed for every
# watchlist ticker, decoupled from any live-SHORT-thesis gate (previously reachable
# only through onchain.py's `_freshness_layer`, which never ran on a cadence). A
# true->false transition fires ONE HIGH inbox event — the entry unlocking.
if due operator_veto_last 60; then
  out="$(python3 capabilities/operator_veto.py --json 2>>state/discovery_tick.err)"
  if [ -n "$out" ]; then
    printf '%s' "$out" > state/operator_veto_latest.json
    log "operator_veto sweep ok"
    touch_marker operator_veto_last
  else
    log "operator_veto EMPTY OUTPUT (capability failed — see discovery_tick.err)"
  fi
fi

# ── 3d. TIER-EVENTS page (SPEC-169) — every 1h ──────────────────────────────────
# ledger.py's `stats` appends state/tier_events.jsonl whenever a signature's computed
# tier differs from the last one on record (hypothesis->go, go->demoted, demoted->go) —
# this is the durable "fill #10 printed GO" trail. ledger.py itself makes no network
# call (house style): this tick calls `stats` (the write happens as its side effect),
# then pages any tier_events lines new since the last tick, class TIER.
if due tier_events_last 60; then
  python3 capabilities/ledger.py stats --json > state/ledger_stats_latest.json 2>>state/discovery_tick.err
  seen_marker="state/.tier_events_lines_seen"
  total_lines=0
  [ -f state/tier_events.jsonl ] && total_lines="$(wc -l < state/tier_events.jsonl | tr -d ' ')"
  prev_seen=0
  [ -f "$seen_marker" ] && prev_seen="$(cat "$seen_marker" 2>/dev/null || echo 0)"
  if [ "$total_lines" -gt "$prev_seen" ]; then
    tail -n "+$((prev_seen + 1))" state/tier_events.jsonl | while IFS= read -r line; do
      [ -z "$line" ] && continue
      msg="$(printf '%s' "$line" | python3 -c 'import sys, json
try:
    d = json.load(sys.stdin)
    print(f"{d.get(\"signature\")}: {d.get(\"from\")} -> {d.get(\"to\")} "
          f"(n={d.get(\"n_filled\")}, {d.get(\"total_r\")}R, trailing-10 {d.get(\"trailing_10_r\")}R)")
except Exception:
    pass' 2>/dev/null)"
      if [ -n "$msg" ]; then
        bash ops/notify.sh "crime-desk — TIER change" "$msg" "high" "desk" || true
        log "tier_event: $msg"
      fi
    done
    echo "$total_lines" > "$seen_marker"
  fi
  touch_marker tier_events_last
fi

# ── 3f. VENUE ACCOUNT read (SPEC-170) — max_leverage daily, sync every tick ─────
# READ-ONLY (ADR-0001: the desk reads the venue, never executes). A missing/placeholder
# Aster key degrades to `{"ok":false,"error":"aster_key_missing"}` — logged, never fatal
# to the rest of the tick.
if due venue_max_lev_last 1440; then
  out="$(python3 capabilities/venue_account.py max_leverage --write --json 2>>state/discovery_tick.err)"
  log "venue_account max_leverage --write: ${out:-EMPTY}"
  touch_marker venue_max_lev_last
fi

sync_out="$(python3 capabilities/venue_account.py sync --dry-run --json 2>>state/discovery_tick.err)"
if [ -n "$sync_out" ]; then
  printf '%s' "$sync_out" > state/venue_sync_latest.json
  log "venue_account sync --dry-run: $sync_out"
  flags="$(printf '%s' "$sync_out" | python3 -c 'import sys, json
try:
    d = json.load(sys.stdin)
    d = d.get("data", d)
    closed = d.get("closed_on_venue") or []
    untracked = [u.get("ticker") for u in (d.get("untracked") or [])]
    lines = []
    if closed:
        lines.append("CLOSED_ON_VENUE: " + ", ".join(closed))
    if untracked:
        lines.append("UNTRACKED: " + ", ".join(untracked))
    print("\n".join(lines))
except Exception:
    pass' 2>/dev/null)"
  if [ -n "$flags" ]; then
    bash ops/notify.sh "crime-desk — POSITIONS drift" "$flags" "high" "desk" || true
    log "venue_account sync flags: $flags"
  fi
else
  log "venue_account sync --dry-run EMPTY OUTPUT (capability failed — see discovery_tick.err)"
fi

# ── 4. LOCAL INDEX ingest (SPEC-102) — every 12h, LAST ON PURPOSE ───────────────
# Ingests the whole tracked set and can run for many minutes; it must never delay the
# discovery legs above (measured 2026-08-07: it alone outlasted a 10-min foreground run).
if due local_index_last 720; then
  if python3 capabilities/local_index.py --tick --json >> state/local_index_tick.out 2>>state/discovery_tick.err; then
    log "local_index tick ok"
    touch_marker local_index_last
  else
    log "local_index tick FAILED (see discovery_tick.err)"
  fi
fi

exit 0
