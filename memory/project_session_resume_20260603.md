---
name: project_session_resume_20260603
description: Session resume snapshot (2026-06-03 late) — open positions, watch config, armed setups, and open decisions. Read this on session-open to resume the live desk state instantly.
metadata:
  type: project
---

**Resume snapshot — 2026-06-03 ~late session (after PC restart, load this first).**

**OPEN SHORTS (committed in config/watchlist.json):**
- **ESPORTS** — SHORT entry ~$0.0492, stop $0.0528, TP1 $0.045 / TP2 $0.040 / TP3 $0.035. Was AT TP1 (~$0.045, −16% on day) → bank 1/3, runner to $0.040. Distribution active (0xbb58/0x2609 → Pancake + CEX-deposit secondary wallets 0xc2F8/0x2a50). Healthy. (A-different-MM operator, separate from SKYAI.)
- **SKYAI** — SHORT entry $0.168, stop $0.175, TP $0.15/0.12/0.10. px ~$0.166 (bounced toward entry; funding flat, distribution intact). Add Tier-1 on held break <$0.15. Primary distributor 0xffa8 → Bitget, decelerating (21M→11M/1d) — if it goes quiet = cover cue. XTOKEN-MM cluster (operator-heat with BILL/EDEN/OPN; separate from ESPORTS).

**WATCH LOOP (re-arm with /loop):** board-wide `onchain_board {}` + overlays for ESPORTS (3 wallets), SKYAI (0xffa8/0xc882/0x4982), PIEVERSE pre-arm (DOMINO 0x01b97cea + 0xf89d7b). Baseline ESCALATING=EDEN,BILL,SKYAI; LOADING=LAB.

**ARMED/FORMING SETUPS:**
- **SLX** — trap-formation LONG candidate (deep-neg-ish funding + OI +56% building). NOT gated yet — run confluence (spot-CVD-up + trigger) before sizing.
- **EDEN** — faded-distribution short ARMING: team wallet 0xad11e97f5044db890a75a7d3e51eaa7099d7e7ff deposited ~$500K to Gate at a local high, holds ~$3M more. Still pumping (+16%); arms on the pullback. Deep-neg funding cooling.
- **PLAY** — distribution starting (0x24d0315b/0x68a3067a dumping ~$200K DEX, absorbed). Forming; needs a lower-high to fade.
- **MYX** — −32% cascade, flat funding, OI −37% capitulation. Played-out; fade the bounce not the knife.
- **BEAT** (Audiera, BSC 0xcf3232…) — blowoff-top SHORT forming: controlled float (57 holders), lower-high $1.434 vs ATH $1.529, OI z −1.96 hollow retest, crowd LONG. Vaults 0x4efe47/0xb479c8 NOT distributing yet. User SCALPED it (out). Swing entry = rejection of $1.434 + breakdown + vault distribution. NOT on watchlist.
- **RAVE** (onboarded) — faded corpse, distribution drained/frozen, PASS. Re-arm only if frozen whale 0xf07327/0x2d81 wakes → CEX.
- **OPN** (Opinion, XTOKEN cluster) — active pump, no trade; future blowoff-watch.

**OPEN DECISIONS / GAPS:**
- **Durability:** coder auto-dispatch + watch are TCC-blocked from ~/Documents (launchd "Operation not permitted"). To survive restart, pick: cloud /schedule, grant Full Disk Access to /bin/bash, or move repo out of ~/Documents. UNRESOLVED.
- **Coder queue:** SPEC 18/19/20 DONE (funding live-rate + history-floor + RAVE onboard). **SPEC 21 OPEN** — verify_wallet/radar are SINGLE-CHAIN, miss multi-chain distribution (user's multi-chain reads beat the engine; trust them).
- Today's scalps (separate track): EDEN short +4.2%, BEAT (out). n=2, not a validated signature.

**Style:** two-track — swing-short faded distributions (the edge, see [[feedback_user_edge_faded_distribution_bounces]]) + scalp crime-coin rejections/squeezes (see [[feedback_deepneg_veto_is_swing_not_scalp]]). Orchestrator session only — never edit engine code, file coder tickets ([[feedback_orchestrator_not_coder_two_sessions]]).
