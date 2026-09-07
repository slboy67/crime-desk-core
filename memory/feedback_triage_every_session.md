---
name: feedback-triage-every-session
description: Run scripts/triage.py (Mode C compact crime sweep) at least once at the start of every session in this project
metadata: 
  node_type: memory
  type: feedback
  originSessionId: f1c24a74-6d40-46cb-b214-327247fa4e65
---

At least once per session in this project, run `python3 scripts/triage.py` (the Mode C compact crime triage across the full watchlist) and surface the output to the user. Do not require the user to ask.

**Why:** User directive 2026-05-14: "Do this for every session atleast once" — issued after building the triage script. Tied to the broader principle ("under" mentor chat, same day) of building repeatable machines and eliminating recurring manual yapping. The user shouldn't have to re-ask "what's happening on the watchlist" each session — the answer should be on-screen by default.

**How to apply:**
- Run `python3 scripts/triage.py` (no args = full watchlist) at session start, OR at the first user message that touches trading/watchlist context if a SessionStart hook isn't catching it.
- Surface the output as a short "Watchlist heartbeat" section before whatever the user is asking about. If they asked a specific token question, lead with the answer to their question, then append the triage as a secondary block.
- If the user is doing non-trading work in this folder (config edits, infrastructure), skip — the heartbeat is about live trading state.
- Don't double-run: once per session is enough unless the user asks for a refresh or 60-90 min have elapsed (per [[project_scripts_tooling]] / Section 13 refresh cadence).
- If the SessionStart hook fires it automatically (see settings.local.json), you don't need to re-run it — just reference the output that's already in context.
- See [[project_scripts_tooling]] for the full automation stack (pull5, regime_check, watch_wallets, triage, etc.).
