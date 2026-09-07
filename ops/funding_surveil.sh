#!/usr/bin/env bash
# SPEC-93: extreme-negative-funding surveillance tick — the funding mirror of ops/surveil.sh.
# Runs the deep-neg side of `scan` over the whole perp universe, cross-venue verifies + enriches
# each band hit (§4 OI-context decode + §5 short-veto), diffs against state/funding_baseline.json,
# and PAGES (a) a NEW deep-neg name or (b) one that materially deepens / flips OI to rising. The
# inbox event + LOG line are always written (the record); only the osascript PAGE is throttled
# (page_gate cooldown). Wire to launchd/cron for the cadence (ops/com.crimedesk.funding-surveil.plist).
#
# Offline override (DoD demo / debugging): set FUNDING_SURVEIL_SCAN and (optionally)
# FUNDING_SURVEIL_PERP / FUNDING_SURVEIL_PRICE to files holding a `scan --json` envelope, a
# {ticker: live_perp} table, and a {ticker: price_ctx} table (SPEC-96 location snapshots).
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
mkdir -p state

ts="$(date -u +%FT%TZ)"

# 1) the cross-sectional read — deep-neg LONG side, band ≤ −1.0%/4h, §7 liquidity gate.
if [ -n "${FUNDING_SURVEIL_SCAN:-}" ]; then
  scan_file="$FUNDING_SURVEIL_SCAN"
else
  scan_file="$(mktemp)"
  python3 capabilities/scan.py --side long --thresh 1.0 --min-vol 10 --include-hl --json \
    > "$scan_file" 2>>state/funding_surveil.err || true
fi

perp_arg=()
[ -n "${FUNDING_SURVEIL_PERP:-}" ] && perp_arg=(--perp-json "$FUNDING_SURVEIL_PERP")
[ -n "${FUNDING_SURVEIL_PRICE:-}" ] && perp_arg+=(--price-json "$FUNDING_SURVEIL_PRICE")

# 2) verify + enrich + diff + log + fire inbox events (run_tick). Prints {paged,alerts}.
# ${perp_arg[@]+...} — macOS bash 3.2 treats an EMPTY array expansion as unbound under set -u
# (killed the first live tick 2026-07-02; tests never hit it because the offline seams populate it)
result="$(python3 ops/funding_surveil.py tick --scan-json "$scan_file" ${perp_arg[@]+"${perp_arg[@]}"} \
  --state state/funding_baseline.json --log state/funding_alerts.log \
  2>>state/funding_surveil.err || echo '{"paged":[],"alerts":[]}')"

[ -z "${FUNDING_SURVEIL_SCAN:-}" ] && rm -f "$scan_file"

# 3) throttle the PUSH (notification) via page_gate — the inbox event/log already landed above.
#    Build an onchain_board-shaped envelope keyed by ticker; band tier = dest_kind so a
#    WATCH→HIGH deepen pierces the cooldown (SPEC-93 _DEST_RANK addition).
env="$(printf '%s' "$result" | python3 -c 'import sys,json
try:
    r=json.load(sys.stdin)
except Exception:
    print("{}"); sys.exit()
alerts=[]
for a in r.get("alerts",[]):
    if not a.get("page"): continue
    alerts.append({"ticker":a["ticker"],"escalation_fired":[{
        "address":"FUND:%s"%a["ticker"],"label":"%s %s%%/4h"%(a["ticker"],a["funding_4h"]),
        "dest_kind":a["band"].lower()}]})
print(json.dumps({"data":{"alerts":alerts}}))' 2>/dev/null || echo '{}')"

npaged="$(printf '%s' "$env" | python3 -c 'import sys,json
try: print(len(json.load(sys.stdin).get("data",{}).get("alerts",[])))
except Exception: print(0)' 2>/dev/null || echo 0)"

if [ "$npaged" != "0" ]; then
  # SPEC-154: 24h per-name dedup (not page_gate's 6h wallet-activity default) — a deep-neg
  # name pages at most once/24h at the same severity; a WATCH->HIGH deepen (tier upgrade)
  # still pierces the window.
  decision="$(printf '%s' "$env" | python3 ops/page_gate.py gate --cooldown-hours 24 2>>state/funding_surveil.err | head -1 || echo PAGE)"
  if [ "$decision" = "PAGE" ]; then
    summary="$(printf '%s' "$result" | python3 -c 'import sys,json
try:
    r=json.load(sys.stdin)
    parts=[a["msg"][:120] for a in r.get("alerts",[]) if a.get("page")]
    print(" | ".join(parts)[:200] or "deep-neg (see state/funding_alerts.log)")
except Exception:
    print("deep-neg (see state/funding_alerts.log)")' 2>/dev/null || echo 'deep-neg')"
    # Delivery via ops/notify.sh (2026-08-06): desktop toast + the CRIMEDESK_NTFY_URL phone leg.
    # SPEC-157: names not on the board are DESK by definition — no ntfy POST.
    bash ops/notify.sh "crime-desk — DEEP-NEG funding" "$summary" "" "desk" || true
  else
    echo "$ts page suppressed (cooldown) x$npaged" >> state/funding_alerts.log
  fi
fi
