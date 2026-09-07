#!/usr/bin/env bash
# One-shot: wait for the running coder to release its lock, then kickstart dispatch so
# queued specs go straight in. Detached + nohup'd so it survives the Claude session,
# a terminal close, or a VSCode restart. Uses launchctl kickstart (never a backgrounded
# bash wrapper — that orphans zombies, see memory/feedback_never_nudge_dispatch_via_backgrounded_wrapper).
LOCK="$HOME/Library/Application Support/crimedesk/coder.lock"
LOG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/state/coder_dispatch.log"
for _ in $(seq 1 240); do            # 240 x 30s = 2h ceiling
  if [ ! -f "$LOCK" ] || ! ps -p "$(cut -d' ' -f1 "$LOCK" 2>/dev/null)" >/dev/null 2>&1; then
    sleep 5
    launchctl kickstart -k "gui/$(id -u)/com.crimedesk.coder-dispatch"
    echo "$(date -u +%FT%TZ) auto-kickstart: lock released, dispatching queued specs" >> "$LOG"
    exit 0
  fi
  sleep 30
done
echo "$(date -u +%FT%TZ) auto-kickstart: gave up after 2h, lock still held" >> "$LOG"
