# oi_sides — per-side OI + wash fingerprint

## Purpose
Per-side OI read + 对敲 (self-trade/wash) fingerprint for one ticker — pure perp, no
config dependency. The §6 blowoff-short WASH pre-check: don't short pinned chop that's
operator self-dealing.

## Contract
```
oi_sides '{"ticker":"SKYAI"}'
```
Out: `{ticker, wash_score, verdict, oi_change_pct, oi_chg_pct_4h, ...}`.

- `oi_change_pct` — the raw `_oldrepo` field, unchanged (kept for existing consumers).
- `oi_chg_pct_4h` (SPEC-174 #6) — `filters/oi_sides.py` relabels the SAME value with its
  window: the script's `--period`/`--limit` default to `5m`x`48` = 4h, and the
  orchestrator's invoke template never overrides them, so every orchestrator-routed call
  is a fixed 4h window. Distinct from `brief.perp.oi_chg_pct_48h` (a DIFFERENT script,
  `perp_analyser.py`, a DIFFERENT fixed 48h window) and `triage`'s `oi_chg_pct_24h` — three
  genuinely different OI-change reads that used to share one unlabelled name (the MANTRA
  scout-sweep confusion: +8.2% here vs +57% there, no way to tell which window was which).

## Gotchas
- §6: if verdict = WASH, don't short the pinned chop — wait for the wash→REAL
  transition before the blowoff-top short.
- Lives in `_oldrepo/scripts/` (registered orphan) — wired as-is per the guardrail
  against modifying _oldrepo logic; `filters/oi_sides.py` relabels the window on the way
  out without touching the delegated script.
- A wash read is OI-construction intel (§0.6.3): pinned + washed = the operator is
  recruiting, not exiting.
