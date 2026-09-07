---
name: token-specific-pattern-overrides-framework
description: "Mechanical Section 6 setup criteria can fire on a token whose historical pattern argues the opposite trade — always count the token's squeeze-event history before treating any pull as terminal."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: e94ca703-b256-430c-897b-c7124651d8f5
---

A Section 6 setup (especially Blowoff-Top Short) can fire 5/5 on mechanical criteria while the **token's history says the framework is mismatched**. Before treating ANY cascade as terminal, check the token's squeeze-event frequency and whether magnitude is diminishing or stable.

**Why:** ESPORTS 2026-05-20 — I called Blowoff-Top Short 5/5 on a -15% pull-off-ATH. User pushed back: "ESPORTS has been doing this since launch." Data confirmed: 8 squeeze events in 60 days (~one every 7.5 days), MIXED magnitude (no diminishing trend), every prior cascade bounced to a new high. The mechanical short criteria were correct but the trade construct was wrong — the token's chronic re-squeeze pattern made it a fast scalp, not a multi-day cascade trade. The bilateral-distribution caveat in Section 6 ("on Cat A the first cascade can be a bait-dip before a re-squeeze") applies HARD on chronic-squeeze tokens but I treated it as standard caveat-text rather than a hard binding.

**How to apply:**
- Before any Blowoff-Top Short verdict, count daily squeeze events (close-to-close > 15%) over the available window in `price_structure.py`.
- If frequency is high (>1 per 10 days) AND magnitude is stable/mixed (not diminishing), the framework's "terminal cascade" assumption is structurally weaker — treat the setup as a FAST SCALP (TP1 only) not multi-day hold.
- If diminishing-returns pattern visible (90% → 28% MYX-style), framework applies normally — terminal cascade more probable.
- Section 6 Blowoff-Top + chronic re-squeeze token = TP at first round magnet or first HVN cluster, do not hold for deep targets unless on-chain layer fires (wallets → CEX).
- This is NOT "loosen the framework on user wins/losses" ([[feedback-stay-strict-on-confluence]]) — it's "apply the token-specific override that the framework itself describes in the caveat text."

**Generalizes to:** LAB-pattern multi-cycle vs ESPORTS-pattern chronic-re-squeeze vs MYX-pattern single-magnum cascade — the squeeze-event histogram tells you which pattern you're in BEFORE you size the trade.

Related: [[feedback-stay-strict-on-confluence]], [[feedback-blowoff-top-short-gap]], [[feedback-cascade-is-the-setup]], [[project-esports-thesis]]
