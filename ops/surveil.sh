#!/usr/bin/env bash
# On-chain nonce surveillance tick — runs the cheap board sweep, logs + notifies on
# ESCALATION (a dormant tracked safe's nonce fired = §8 bid-pull/top signal).
# The capability persists each token's baseline, so each tick diffs against the last.
# Wire to launchd/cron for a durable cadence (see ops/com.crimedesk.nonce-surveil.plist).
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
mkdir -p state

ts="$(date -u +%FT%TZ)"

# SPEC-95: fire any due T-3d/T-1d pre-unlock alerts (cursor-deduped) into the inbox so a
# scheduled supply event is known BEFORE the date. Best-effort — never blocks the nonce tick.
python3 capabilities/unlocks.py --fire-alerts --json >> state/surveil.err 2>&1 || true

# SPEC-119: claim/distributor top-up tripwire — same cadence, no new loop. Best-effort —
# never blocks the nonce tick.
python3 capabilities/claim_topup.py --op fire-alerts --json >> state/surveil.err 2>&1 || true

out="$(python3 orchestrator.py onchain_board '{}' 2>>state/surveil.err || true)"

# count ESCALATION alerts from the envelope
n="$(printf '%s' "$out" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); print(len(d.get("data",{}).get("alerts",[])))
except Exception:
    print("ERR")' 2>/dev/null || echo ERR)"

if [ "$n" = "ERR" ]; then
  echo "$ts SWEEP_ERROR (see state/surveil.err)" >> state/nonce_alerts.log
elif [ "$n" != "0" ]; then
  # SPEC 66: the LOG line is ALWAYS written (the record is complete); only the PAGE is
  # throttled. page_gate keeps state/page_cooldowns.json — a wallet that paged within the
  # cooldown logs but does not re-notify unless it's a tier upgrade / re-emergence.
  echo "$ts ESCALATION x$n $out" >> state/nonce_alerts.log
  decision="$(printf '%s' "$out" | python3 ops/page_gate.py gate 2>>state/surveil.err | head -1 || echo PAGE)"
  if [ "$decision" != "PAGE" ]; then
    echo "$ts page suppressed (cooldown) x$n" >> state/nonce_alerts.log
    exit 0
  fi
  # SPEC-163: compose the page body + decide ACT vs DESK via ops/safe_page.py (one
  # source of truth, shared with board_tick's own ACT/DESK rule via ops/route_for.py) —
  # a fired safe on a ticker you hold or are committed to (thesis ARMED/LIVE) is
  # genuinely ACT; any other ticker is DESK (desktop + inbox only, no phone). A parse
  # failure degrades to ACT — never risk silencing a real page.
  page="$(printf '%s' "$out" | python3 ops/safe_page.py fired 2>>state/surveil.err || true)"
  route="$(printf '%s\n' "$page" | sed -n '1p')"
  summary="$(printf '%s\n' "$page" | sed -n '2p')"
  if [ -z "$route" ] || [ -z "$summary" ]; then
    route="act"
    summary="escalation (see state/nonce_alerts.log)"
  fi
  # Delivery via ops/notify.sh (grill 2026-08-06): desktop toast + the CRIMEDESK_NTFY_URL
  # phone leg — an ESCALATION at 4am must reach the lock screen, not an empty room.
  # SPEC-141: a safe firing is the cascade-outruns-the-session case -> urgent priority.
  bash ops/notify.sh "crime-desk — safe FIRED" "$summary" "urgent" "$route" || true
else
  # heartbeat (quiet) — comment out if the log gets noisy
  echo "$ts quiet" >> state/surveil.heartbeat
fi

# SPEC-145 req 4: a coverage COLLAPSE (an RPC chain fully unreadable, or >25% of the tracked
# set unreadable this sweep) is itself news — a half-blind sweep reads identically to a
# genuinely quiet one unless this pages too. Same `$out` envelope as the ESCALATION check
# above, no extra RPC/API cost. Pages directly (no page_gate throttle — that gate is keyed
# on fired-wallet identity/tier, not coverage; balance_surveil's HIGH path below does the
# same un-throttled page for the same reason).
cov_n="$(printf '%s' "$out" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); print(len(d.get("data",{}).get("coverage_alerts",[])))
except Exception:
    print(0)' 2>/dev/null || echo 0)"
if [ "$cov_n" != "0" ] && [ "$cov_n" != "ERR" ]; then
  cov_summary="$(printf '%s' "$out" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin).get("data",{})
    print(" | ".join(a.get("msg","coverage collapse") for a in d.get("coverage_alerts",[]))[:180])
except Exception:
    print("surveillance coverage collapse (see state/nonce_alerts.log)")' 2>/dev/null || echo 'coverage collapse')"
  echo "$ts COVERAGE_COLLAPSE x$cov_n $cov_summary" >> state/nonce_alerts.log
  # SPEC-163 req 2: an ops-health message ("surveillance is half-blind"), never a
  # per-ticker trade decision -> always desk (desktop + inbox only), never the phone.
  bash ops/notify.sh "crime-desk — surveillance BLIND" "$cov_summary" "" "desk" || true
fi

# SPEC-126: contract-wallet balance-delta surveillance — nonce_surveil (above) is permanently
# blind to Gnosis Safe proxies (a Safe's own account nonce never bumps). Runs every tracked
# token; each tick cross-checks + diffs BALANCE-mode wallets and fires its own inbox event
# (source balance_surveil) via the SAME inbox producer API other post-nonce monitors use.
bal_out="$(python3 ops/balance_surveil.py tick --all --json 2>>state/surveil.err || echo '{}')"
bal_n="$(printf '%s' "$bal_out" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(sum(len(r.get("fired", [])) for r in d.values()))
except Exception:
    print(0)' 2>/dev/null || echo 0)"
if [ "$bal_n" != "0" ] && [ "$bal_n" != "ERR" ]; then
  # SPEC-163: route (ACT/DESK) from the shared compose-and-route helper — one source of
  # truth; degrades to ACT on a parse failure (never silence a real page).
  page="$(printf '%s' "$bal_out" | python3 ops/safe_page.py drained 2>>state/surveil.err || true)"
  route="$(printf '%s\n' "$page" | sed -n '1p')"
  [ -z "$route" ] && route="act"
  # SPEC-141: compact units, never scientific notation (a raw %g on a 6-7 digit delta
  # renders as "7.18135e+05" — meaningless on a lock screen).
  # SPEC-164: `fired` is already materiality-gated (sub_threshold/exchange_churn never
  # land here — see ops/balance_surveil.py PAGEABLE_KINDS), so every item here is a real
  # page. The TITLE must still be honest per-kind: "DRAINED" is reserved for
  # drained_to_zero — a balance_drop/outbound_drip mislabeled DRAINED was the SPEC-164
  # incident (SLX -0.09%, TAG -0.05%, TAKE -0.19% all paged "contract safe DRAINED").
  bal_title="$(printf '%s' "$bal_out" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    kinds=set()
    for r in d.values():
        kinds |= {f.get("kind") for f in r.get("fired", [])}
    if "drained_to_zero" in kinds:
        print("crime-desk — contract safe DRAINED")
    elif "outbound_drip" in kinds and len(kinds) == 1:
        print("crime-desk — contract wallet OUTBOUND-DRIP")
    else:
        print("crime-desk — contract wallet OUTBOUND")
except Exception:
    print("crime-desk — contract wallet OUTBOUND")' 2>/dev/null || echo 'crime-desk — contract wallet OUTBOUND')"
  bal_summary="$(printf '%s' "$bal_out" | python3 -c 'import sys,json

def fmt(n):
    n = float(n)
    sign = "-" if n < 0 else ""
    an = abs(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if an >= div:
            return "%s%.2f%s" % (sign, an / div, suf)
    return "%s%.0f" % (sign, an)

_TAG = {"drained_to_zero": "DRAINED", "outbound_drip": "DRIP", "balance_drop": "OUT"}
try:
    d=json.load(sys.stdin)
    parts=[]
    for ticker,r in d.items():
        for f in r.get("fired", []):
            tag = _TAG.get(f.get("kind"), "OUT")
            parts.append("$%s: %s %s -%s" % (ticker, f.get("label","?"), tag, fmt(f.get("delta", 0))))
    print(" | ".join(parts)[:180])
except Exception:
    print("balance drop (see state/balance_baseline_*.json)")' 2>/dev/null || echo 'balance drop')"
  # SPEC-141: a materiality-gated contract-wallet outbound is the cascade-outruns-the-
  # session case -> urgent.
  bash ops/notify.sh "$bal_title" "$bal_summary" "urgent" "$route" || true
fi
