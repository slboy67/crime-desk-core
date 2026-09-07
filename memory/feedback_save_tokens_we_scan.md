---
name: save-tokens-we-scan
description: "When the user asks for a check/scan on ANY token — Cat A or not, verdict PASS or BUY — save a brief project memory so the token persists across sessions and is recognized on follow-up. Memory is the cross-session connector; otherwise every session starts blind to prior coverage."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: e94ca703-b256-430c-897b-c7124651d8f5
---

When user requests a scan/check/analysis on a token, **save a project memory** for that token even if the current verdict is PASS. Otherwise the next session has no record and the user has to re-explain context, OR an early-stage signal we flagged in session N gets missed in session N+1 when the move has fully played out.

**Why:** FIDA 2026-05-21 — user said "I asked you to scan it in an earlier session." Zero memory references to FIDA, no watchlist entry, no project memory. Couldn't reconstruct what we did or flag prior signals. By the time it surfaced again, FIDA had pumped +69%/24h, +141%/60d — the clean early-stage entry (when the squeeze-fuel pattern would have been forming) was gone. The framework signals are clean now but the trade is late. If a memory had captured the early scan, the next session would have surfaced FIDA proactively when momentum confirmed.

**How to apply:**

When user requests `check $X` / `scan $X` / `look at $X`:

1. **Always save a project memory** with token name, current price, key observations, verdict, and a forward-trigger (what would change the verdict).
2. **For Cat A tokens that pass the liquidity gate**: also add them to `config/watchlist.json` so they appear in future `/triage` runs automatically.
3. **For non-watchlist / non-Cat-A tokens**: project memory is enough — don't bloat the watchlist with tokens that don't fit the framework, but DO retain the analysis.
4. **Memory naming**: `project_<ticker>_thesis.md` (e.g. `project_fida_thesis.md`) — slug for grep-ability.
5. **Include the trigger that would re-engage**: "price reclaims $X" / "wallet Y fires" / "funding flips negative" — so the next session knows what to watch.
6. **If session's verdict is "early-stage signal, not entry-ready yet"** → especially important to save, because that's the exact case where missing it on follow-up costs a missed clean entry.

**Anti-pattern to avoid:** treating each scan as a one-shot disposable analysis. Memory persistence is what compounds the framework's edge across days/weeks — without it, every session is the user's first.

**Archival rule (added 2026-05-29, the MEMORY.md-over-limit fix):** per-session/dated thesis snapshots are indexed in MEMORY.md only while LIVE. **On resolution (PASS played out, trade closed, setup invalidated/aged), DROP the index line but KEEP the `project_<ticker>_thesis.md` file on disk** for grep — removing the index line is NOT deleting the token (this rule and the save-rule above are compatible). Run an archival pass at session start whenever the harness fires the "MEMORY.md over limit" size warning: trim oversized index entries to ≤200-char pointers (detail already lives in the linked file) and archive resolved snapshots. Without this the 90+-entry index re-breaches the 24.4KB load limit within days and the tail truncates silently. (2026-05-29: index hit 29KB → trimmed 8 bloated entries + archived 9 resolved snapshots back under limit.)

Related: [[feedback-triage-every-session]], [[feedback-stay-strict-on-confluence]], [[feedback-full-analysis-always]]
