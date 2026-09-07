#!/usr/bin/env bash
# herdr-desk.sh — build the crime-desk Herdr workspace in one shot.
#
#   bash ops/herdr-desk.sh            # create workspace + tabs, start the desk agent
#   bash ops/herdr-desk.sh --force    # build even if a "Crime Desk" workspace already exists
#
# Layout it builds (in the default Herdr session):
#   Workspace "Crime Desk"  (cwd = repo root)
#     Tab 1 (root)  DESK   — Claude orchestrator session (the Designer; CLAUDE.md rules apply)
#     Tab 2         CODER  — top: tail of state/coder_dispatch.log (launchd auto-dispatch)
#                            bottom: idle shell for ops/premerge.sh <branch> + merges
#     Tab 3         WATCH  — top: tail of state/nonce_alerts.log (safe-FIRED escalations)
#                            middle: tail of state/board_tick.out (15-min classify tick)
#                            bottom: caffeinate -is  (keeps the Mac awake so launchd ticks
#                            survive a closed lid; on battery, -s needs AC — plug in overnight)
#
# The surveillance cadence itself stays on launchd (board-tick / nonce-surveil / funding-surveil
# plists) — Herdr here is the persistent *interactive* layer: detach, close the terminal,
# reattach later with `herdr` (or `herdr --remote` from elsewhere) and the desk is still live.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
FORCE="${1:-}"

jget() {  # jget '<json>' result.workspace.workspace_id
  python3 - "$1" "$2" <<'PY'
import sys, json
d = json.loads(sys.argv[1])
for k in sys.argv[2].split("."):
    d = d[k]
print(d)
PY
}

command -v herdr >/dev/null 2>&1 || { echo "herdr not on PATH — install first: curl -fsSL https://herdr.dev/install.sh | sh"; exit 1; }

# Server must be up for CLI calls. If not, tell the human the one manual step.
if ! herdr session list >/dev/null 2>&1; then
  echo "Herdr server isn't running. Run 'herdr' once to start it (detach with ctrl+b q), then rerun this script."
  exit 1
fi

# Idempotency guard — don't stack duplicate workspaces on rerun.
if [ "$FORCE" != "--force" ] && herdr workspace list 2>/dev/null | grep -qi "Crime Desk"; then
  echo "A 'Crime Desk' workspace already exists. Rerun with --force to build another, or 'herdr' to attach."
  exit 0
fi

echo "▸ creating workspace 'Crime Desk' @ $DIR"
WS_JSON="$(herdr workspace create --cwd "$DIR" --label "Crime Desk" --focus)"
WS="$(jget "$WS_JSON" result.workspace.workspace_id)"
DESK_PANE="$(jget "$WS_JSON" result.root_pane.pane_id)"

# ---- Tab 1: DESK — the orchestrator/Designer Claude session -------------------
echo "▸ starting Claude in the desk pane ($DESK_PANE)"
sleep 2  # let the pane's shell finish starting (agent start needs an idle foreground shell)
if ! herdr agent start desk --kind claude --pane "$DESK_PANE" 2>/dev/null; then
  echo "  agent start failed — falling back to plain 'claude' launch"
  herdr pane run "$DESK_PANE" "claude"
fi

# ---- Tab 2: CODER — dispatch log + premerge shell -----------------------------
echo "▸ building CODER tab"
T2_JSON="$(herdr tab create --workspace "$WS" --cwd "$DIR" --label "Coder" --no-focus)"
T2_TAB="$(jget "$T2_JSON" result.tab.tab_id)"
T2_TOP="$(jget "$T2_JSON" result.root_pane.pane_id)"
S2_JSON="$(herdr pane split "$T2_TOP" --direction down --ratio 0.5 --cwd "$DIR")"
T2_BOT="$(jget "$S2_JSON" result.pane.pane_id)"
sleep 1
touch state/coder_dispatch.log
herdr pane run "$T2_TOP" "tail -n 40 -F state/coder_dispatch.log"
# bottom pane stays an idle shell — that's where you run: ops/premerge.sh <branch>

# ---- Tab 3: WATCH — alert tails + keep-awake ----------------------------------
echo "▸ building WATCH tab"
T3_JSON="$(herdr tab create --workspace "$WS" --cwd "$DIR" --label "Watch" --no-focus)"
T3_TOP="$(jget "$T3_JSON" result.root_pane.pane_id)"
S3_JSON="$(herdr pane split "$T3_TOP" --direction down --ratio 0.5 --cwd "$DIR")"
T3_MID="$(jget "$S3_JSON" result.pane.pane_id)"
S4_JSON="$(herdr pane split "$T3_MID" --direction down --ratio 0.35 --cwd "$DIR")"
T3_BOT="$(jget "$S4_JSON" result.pane.pane_id)"
sleep 1
touch state/nonce_alerts.log state/board_tick.out
herdr pane run "$T3_TOP" "tail -n 20 -F state/nonce_alerts.log"
herdr pane run "$T3_MID" "tail -n 10 -F state/board_tick.out"
herdr pane run "$T3_BOT" "echo 'keeping Mac awake for launchd ticks (ctrl+c to stop)…' && caffeinate -is"

echo
echo "✅ Crime Desk workspace built (workspace $WS)."
echo "   Attach:   herdr        (detach: ctrl+b q — everything keeps running)"
echo "   Remote:   ssh in and run 'herdr', or 'herdr --remote <this-mac>' from another machine"
echo "   Desk tab: Claude is starting — open with your usual session-open (classify board + faded-bounce sweep)."
