#!/usr/bin/env bash
# Render the ops/com.crimedesk.*.plist TEMPLATES for THIS machine and (re)load them.
#
# The tracked plists carry no machine-specific values: __DESK_ROOT__ is the absolute
# path of this clone and __NTFY_URL__ is the private push topic. launchd does not
# expand $HOME or env vars inside plist strings, so they must be rendered at install.
#
#   bash ops/install_launchd.sh                 render into ~/Library/LaunchAgents + reload
#   bash ops/install_launchd.sh --dry-run DIR   render into DIR only (no launchctl)
#   bash ops/install_launchd.sh --only board-tick,nonce-surveil   subset of jobs
#
# NTFY_URL source (first hit wins): $CRIMEDESK_NTFY_URL, then
# ~/Library/Application Support/crimedesk/ntfy_url (one line). Missing = rendered
# empty, which makes ops/notify.sh skip the phone leg — a warning is printed.
set -euo pipefail

DESK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NTFY_FILE="$HOME/Library/Application Support/crimedesk/ntfy_url"
NTFY_URL="${CRIMEDESK_NTFY_URL:-}"
if [ -z "$NTFY_URL" ] && [ -f "$NTFY_FILE" ]; then
  NTFY_URL="$(tr -d '[:space:]' < "$NTFY_FILE")"
fi

TARGET="$HOME/Library/LaunchAgents"
DRY=0
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; TARGET="$2"; shift 2 ;;
    --only)    ONLY="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
mkdir -p "$TARGET"

[ -n "$NTFY_URL" ] || echo "WARN: no ntfy URL (set CRIMEDESK_NTFY_URL or write $NTFY_FILE) — phone pages disabled" >&2

for tpl in "$DESK_ROOT"/ops/com.crimedesk.*.plist; do
  name="$(basename "$tpl" .plist)"            # com.crimedesk.<job>
  job="${name#com.crimedesk.}"
  if [ -n "$ONLY" ] && ! printf ',%s,' "$ONLY" | grep -q ",$job,"; then continue; fi
  out="$TARGET/$name.plist"
  sed -e "s|__DESK_ROOT__|$DESK_ROOT|g" -e "s|__NTFY_URL__|$NTFY_URL|g" "$tpl" > "$out"
  if grep -q '__[A-Z_]*__' "$out"; then echo "ERR: unrendered placeholder in $out" >&2; exit 1; fi
  if [ "$DRY" = 1 ]; then
    echo "rendered $out"
  else
    launchctl unload "$out" 2>/dev/null || true
    launchctl load "$out"
    echo "loaded $name"
  fi
done
