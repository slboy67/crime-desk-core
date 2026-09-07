---
name: feedback_alerts_are_signals_positions_json_is_truth
description: "User trades MANUALLY off arm_setup alerts. arm_setup/Monitors emit signals (🔴 ENTRY etc.) — they place NO orders. config/positions.json is the ONLY source of truth for actual exposure. Never infer a position from a running watcher process."
metadata:
  node_type: memory
  type: feedback
---

The user **manually executes** trades off the desk's alerts. `arm_setup.py` and the Monitors are pure **signal generators** — `🔴 ENTRY` / `🎯 TP` / `🚨 INVALIDATED` are notifications to the human, not order placements. A fired ENTRY alert means "here is a signal you may act on," NOT "you are now in this position."

**Why:** I once read a running `arm_setup` process (held-break below trigger for hours) as "you have a live blind position" — wrong. The user was flat. A signal firing ≠ a fill.

**How to apply:**
- **`config/positions.json` is the single source of truth for open exposure.** If it's empty, the desk is flat — full stop, regardless of what watcher processes are running.
- When an alert fires, report it as **"signal fired — your call"**, never "you're in / you may be in / you're blind to a position."
- Never infer a position from `ps`/running `arm_setup`/log lines. Those are watchers.
- When the user says they took (or closed) a trade, **log/update it in `positions.json`** (entry, size, stop, risk_pct, operator cluster) — that's how operator-heat (§7) and the board stay accurate. Don't assume a fill happened; wait for them to say so.
- Backtest/closed-thesis records: mark signal-only outcomes as hypothetical ("would have …"), not realized P&L, unless a real fill was logged in positions.json.
