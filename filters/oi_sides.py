"""Filter for `_oldrepo/scripts/oi_sides.py` — SPEC-174 #6: label the OI-change window.

`oi_change_pct` is computed over `--period x --limit` (default 5m x 48 = 4h) — the
orchestrator's own invoke template for this capability never wires `--period`/`--limit`
through, so every orchestrator-routed call uses those defaults: a FIXED 4h window, always.
`brief.perp.oi_chg_pct` (native, from a DIFFERENT `_oldrepo` script — perp_analyser.py,
`openInterestHist?period=1h&limit=48`, a fixed 48h window) is a different number over a
different window with the same unlabelled name — the MANTRA scout-sweep confusion
(+8.2% vs +57%, neither carrying its window). This filter never touches the delegated
script (guardrail); it relabels the already-correct value on the way out, additively —
`oi_change_pct` stays present unchanged for any existing consumer.
"""
import json


def run(stdout, args):
    # a JSON-decode failure is left to raise naturally — orchestrator.main()'s existing
    # "{cap} output not parseable" path already handles that identically to the
    # no-filter case (json.loads(raw)), no need to duplicate that contract here.
    out = json.loads(stdout)
    if isinstance(out, dict) and out.get("oi_change_pct") is not None:
        out["oi_chg_pct_4h"] = out["oi_change_pct"]
    return out
