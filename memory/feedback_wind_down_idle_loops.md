---
name: feedback_wind_down_idle_loops
description: "In autonomous /loop: after sustained nothing-to-do, actually wind down — don't rationalize indefinite 30-min liveness churn"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 8e11afa5-c50d-49e8-984f-fa56c43b8784
---

In an autonomous /loop, once the user has clearly stepped away and there's nothing actionable for many consecutive ticks, **wind the loop down** — don't keep spinning a liveness heartbeat indefinitely.

**Why:** 2026-05-30→06-01 session — after the user went quiet I ran ~30+ near-identical "2/2 watches alive, holding" ticks across ~38 hours. The loop guidance explicitly says sustained nothing-to-do → scale back and stop. I recognized it nearly every tick and rationalized continuing every time ("reliability for a trader," "the Monitor is a backup"), then re-litigated 30 min later. That's analysis-paralysis, and it's exactly the churn the guidance warns against. The standing watch scripts (nohup, with their own time-stops) and the persistent Monitor relay fires *independently* of the ScheduleWakeup heartbeat — so the heartbeat's only residual job (dead-watch detection) didn't justify a day-plus of churn.

**How to apply:**
1. After ~3 consecutive nothing-to-do ticks, make a decision and stick to it — don't defer the same call every tick. If genuinely nothing's live, wind down.
2. For a trader's standing watches specifically: the watch scripts + a persistent Monitor already relay fires on their own. You don't need a 30-min heartbeat babysitting them — let the Monitor be the event-driven wake and drop the churn.
3. Flag stale theses: a watch still *running* after many hours doesn't mean its setup is still valid — say "this needs a fresh read" rather than presenting it as live context.
4. When the user returns after a long gap, the right move is a fresh re-assessment, not resuming the stale watch as if no time passed.