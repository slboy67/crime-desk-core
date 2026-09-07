---
name: intraday-ath-label
description: "The intraday.py \"Recent ATH\" field is a 14-day period high, NOT the token's all-time high — always cross-check Layer 0 true ATH and label local highs correctly"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 13acafc4-cf7a-4888-9e44-0fac07a67b22
---

`scripts/intraday.py` (Layer 4b) prints a section "## ATH structure (Section 6 short trigger #1+2)" with a line "Recent ATH: <price>". That value is the **intraday/14-day period high**, NOT the token's all-time high. Do not call it "ATH" or "fresh ATH" in analysis.

**Why:** On 2026-05-16 I repeatedly called RIVER's $7.923 14-day period high a "fresh ATH" and said "ATH wick 3.47% formed = Section 6 trigger #1 satisfied." User corrected: RIVER's real ATH is $87.73 (2026-01-26) — the token is -77% off ATH. Layer 0 of every pull5 shows the true ATH; I ignored it and trusted the intraday script's mislabeled field.

**Why it matters for the trade, not just semantics:**
- A *true ATH* wick = rejection in clean price discovery, zero overhead supply.
- A *local-cycle-high* wick (post-ATH drawdown token) sits under stacked trapped bags from every higher price — overhead supply structure is completely different.
- Section 6 short trigger #1 literally says "ATH wick clearly formed." A local-high wick does NOT satisfy it. It can still count toward trigger #2 (lower-high on retest), but conflating the two inflates the confluence score.

**FIXED IN CODE 2026-05-21:** `intraday.py` now (a) labels the period high "LOCAL high (period, NOT true ATH)" and the section "## Local-high structure" whenever the high isn't within 3% of true ATH; (b) pulls the TRUE ATH from Coingecko — resolving ticker→id via the CONTRACT ADDRESS in tracked_wallets.json (authoritative), falling back to symbol-search guarded by a price-sanity check (Coingecko current price within 50% of the perp last price) to catch ticker collisions; (c) prints "% from ATH" and explicitly says "Section 6 blowoff trigger #1 (fresh ATH) does NOT apply" when far below ATH; (d) refuses to print a number it can't verify (BSB → "true ATH unavailable, verify manually") rather than showing a wrong one. The recurrence risk is largely closed, but still sanity-read the output.

**How to apply (still true):**
- For Section 6 trigger #1, only credit "ATH wick" when price is genuinely at/near the true ATH (tool now labels this). Otherwise it's a local-high rejection feeding trigger #2 only.
- If the tool shows "⚠ symbol-matched, UNVERIFIED token" or "true ATH unavailable", treat the ATH as unknown and confirm before any ATH language.
- Note (2026-05-21): our tracked "EDEN" resolves to **OpenEden**, true ATH $1.31 (2025-09-30) — the $0.095 "fresh ATH" in older notes was a local cycle high, not the true ATH.

Related: [[feedback-full-analysis-always]] — full picture means reading Layer 0, not just the layer that caught attention.
