---
name: project_onchain_is_the_spine_perp_came_second
description: "The desk's ORIGINAL/core purpose is on-chain wallet-movement surveillance (mega-safe nonces, flows, distribution) — the perp layer was added second. On-chain is the spine, not an add-on; prioritize on-chain instrumentation accordingly."
metadata:
  node_type: memory
  type: project
---

The crime desk was originally conceived as an **on-chain scanner that reports wallet movements** (mega-safe nonces firing, CEX deposits, distribution/bid-pull surveillance on Cat A operator wallets). The **perp layer (funding/OI/classify/regime_flip) came second.** (User stated 2026-06-03.)

**Why this matters / How to apply:**
- The current codebase *looks* perp-first — `classify`/`regime_flip` are native and fast, while the on-chain path (`analyse`→`safe_audit`) times out to UNAVAILABLE. That is an artifact of what happens to work, **NOT** the intended priority. Do not infer "perp is the core" from the code.
- On-chain is the **spine**. When on-chain is blind, the desk is failing at its primary job, not degrading a secondary feature. Treat `onchain:"UNAVAILABLE"` on a Cat A name as a P0, not a footnote — see [[feedback_live_top_signal_is_nonce_not_getlogs_audit]].
- Architecturally: the dedicated **whole-coin on-chain scanner** (holders + safe lifetime history via `bscscan.py` + flows + VC-entity overlap + live `nonce_watch`) is the centerpiece capability to build/keep healthy. The perp `analyse`/board is the fast overlay on top.
- All the on-chain parts already exist in the bin (holders.py, safe_audit.py, nonce_watch.py, flows.py, vc_entity_watch.py, bscscan.py) — they were just never assembled into one orchestrated `onchain_scan` capability, and `bscscan.py` (the BSC reliability fix) is unused. Building that assembly is restoring the desk's original purpose, not adding a feature.
