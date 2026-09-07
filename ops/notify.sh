#!/usr/bin/env bash
# ops/notify.sh — SPEC-111: best-effort push delivery for watch-level/verdict alerts.
#
#   ops/notify.sh "<title>" "<body>" ["<priority>"]
#
# Delivers a macOS desktop notification (osascript, zero new dependencies) and, if
# CRIMEDESK_NTFY_URL is set, ALSO curl-POSTs to it (ntfy.sh-style — reaches the phone).
# Both legs are best-effort: a failure is logged to state/notify.err, never raised — a
# dead notifier must never break the board tick that calls this. Kill switch:
# CRIMEDESK_NOTIFY=off disables ALL delivery (tests/replay set this so they never notify).
#
# SPEC-141: optional 3rd arg (urgent|high|default) forwards as the ntfy `Priority:`
# header — the osascript leg is unaffected (macOS notifications have no priority
# concept). Arg absent = today's behavior (no Priority header), so existing callers
# are unbroken.
#
# SPEC-157: optional 4th arg `route` (act|desk, default act) — ACT vs DESK page routing.
# `desk` skips the CRIMEDESK_NTFY_URL POST entirely (the phone leg); the osascript
# desktop leg always fires regardless of route — nothing is silenced, only the phone
# POST is gated. Arg absent = today's behavior (act, POST as before).
set -uo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
ERR_LOG="$DIR/state/notify.err"

TITLE="${1:-crime-desk}"
BODY="${2:-}"
PRIORITY="${3:-}"
ROUTE="${4:-act}"

if [ "${CRIMEDESK_NOTIFY:-}" = "off" ]; then
  exit 0
fi

ESCAPED_TITLE="${TITLE//\"/\\\"}"
ESCAPED_BODY="${BODY//\"/\\\"}"

# NOTE: stderr is discarded on the primary attempt, not redirected into ERR_LOG directly —
# a `2>>` redirect creates/touches the file as a side effect of the redirection itself even
# when nothing is written, which would leave a stray empty state/notify.err on every clean
# run (incl. every test invocation). Only the `||` failure branch below ever touches it.
osascript -e "display notification \"$ESCAPED_BODY\" with title \"$ESCAPED_TITLE\"" \
  >/dev/null 2>/dev/null \
  || { mkdir -p "$DIR/state"; echo "$(date -u +%FT%TZ) osascript delivery failed" >>"$ERR_LOG"; }

if [ -n "${CRIMEDESK_NTFY_URL:-}" ] && [ "$ROUTE" != "desk" ]; then
  if [ -n "$PRIORITY" ]; then
    curl -fsS -m 10 -X POST "$CRIMEDESK_NTFY_URL" -H "Title: $TITLE" -H "Priority: $PRIORITY" -d "$BODY" \
      >/dev/null 2>/dev/null \
      || { mkdir -p "$DIR/state"; echo "$(date -u +%FT%TZ) ntfy POST failed" >>"$ERR_LOG"; }
  else
    curl -fsS -m 10 -X POST "$CRIMEDESK_NTFY_URL" -H "Title: $TITLE" -d "$BODY" \
      >/dev/null 2>/dev/null \
      || { mkdir -p "$DIR/state"; echo "$(date -u +%FT%TZ) ntfy POST failed" >>"$ERR_LOG"; }
  fi
fi

exit 0
