#!/usr/bin/env bash
# Board tick — standing classify loop with verdict-delta alerts (SPEC 46).
# One tick: run the board, diff vs state/board_last.json, push deltas to the
# SPEC-45 inbox + a macOS notification. Lockfile + skip-on-failure live in
# ops/board_tick.py. Wire to launchd via ops/com.crimedesk.board-tick.plist.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
mkdir -p state

python3 ops/board_tick.py >> state/board_tick.out 2>> state/board_tick.err

# SPEC-95: auto-populate config/catalysts.json from the public unlock feed on a cadence
# (don't make a human type unlock dates). Time-gated to once / ~12h so the per-token CoinGecko
# reads never hammer the API on the 15-min board tick. Best-effort; never breaks the tick.
marker="state/.unlock_sweep_last"
if [ ! -f "$marker" ] || [ "$(find "$marker" -mmin +720 2>/dev/null)" ]; then
  python3 capabilities/unlocks.py --sweep --json >> state/unlock_sweep.out 2>> state/unlock_sweep.err \
    && touch "$marker" || true
fi
