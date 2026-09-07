---
name: feedback-mm-vs-team-distinction
description: "Cross-token operator wallet = same MM (cross-mandate), not necessarily same project team. Don't conflate."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 7d189419-ea54-42ce-9123-4ae76f85face
---

When a single hot wallet appears across multiple Cat A tokens (e.g., `0x11fc12b988933966688d33b70651b5f2f450963c` across LAB + SkyAI + BILL), the most likely explanation is **same market maker running multiple books under separate mandates** — NOT same project team operating multiple tokens. Default to the MM interpretation, not the team-conspiracy interpretation, unless there's evidence beyond wallet overlap.

**Why:** In the Cat A 70%/30% MM/project profit-share structure (CLAUDE.md Section 5), the MM is the active operator who receives token inventory from the project. Active MMs in this niche operate from a small cluster of reusable wallets and rotate through them. So one wallet across N tokens = N separate MM contracts with the same MM firm, not N tokens with shared cap tables or founders. Conflated this twice in the 2026-05-14 BILL analysis (memory + Telegram thesis), claiming "same crew runs LAB+SkyAI+BILL" — user correctly pushed back. The actual finding is "same MM books LAB+SkyAI+BILL". Project teams, cap tables, legal entities and founders differ across the three tokens; only the MM is shared.

**How to apply:**
- When writing up Cat A theses, **say "MM" not "operator" / "crew" / "team"** when the evidence is a shared wallet across tokens. Reserve "team" for evidence of shared cap-table / shared founder / shared multisig / shared VC backers.
- Cross-token contagion runs through MM events (exchange action against MM addresses, MM reputational hit), NOT through project events (single-token founder doxx like Vova Sadkov / LAB does NOT cascade to BILL).
- VC backing is NOT invalidated by sharing a Cat A MM. Coinbase Ventures + Polychain backed BILL the project, not the MM. A VC-backed project can hire a Cat A MM and that does not retroactively make the VCs complicit.
- Trade thesis is mostly unchanged because the MM IS the active price manipulator. But scope claims accordingly: "BILL's MM is in active distribution" not "BILL is a confirmed Cat A operator-run token controlled by the LAB crew".
- Headline risk asymmetry: a LAB-project-team event (doxx, legal action) hits LAB only. A BILL-project-team event (VC denial, project response) hits BILL only. An MM-event (exchange freeze, deposit-address blacklist) hits all three. Size short conviction by which kind of event you're betting on.
- When tempted to call cross-token wallet overlap "the same operator", first ask: do I have evidence beyond wallet overlap? If no → write "same MM". If yes (shared founders/multisig/cap table) → write "same operator/team".
