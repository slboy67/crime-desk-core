"""Filter for the legacy `analyse.py` human banner → clean JSON.

`_oldrepo/scripts/analyse.py` is the combined perp+onchain verdict engine. It has
NO `--json` mode — it prints a colored human report. This filter distills the only
fields the Designer needs from that report into the analyse out-contract:

    {ticker, verdict, tier, direction}

Mapping (from analyse.py's banner `  🟢  VERDICT: {direction}  [{tier}]  🟢`):
  - direction : the FULL banner string  ("LONG", "SHORT (loading)", "PASS (CVD: hedge-trap)", …)
  - tier      : the bracketed tier      ("STRONG" | "MILD" | "WATCH" | "CAUTION" | "PASS")
  - verdict   : the coarse call distilled from direction (LONG | SHORT | WATCH | PASS)
  - ticker    : from the report header `# TICKER — COMBINED ANALYSIS`, falling back to args.

This is the bridge form (ARCHITECTURE.md §3). End-state: fold a native `--json`
into analyse.py and drop this filter. Until then the orchestrator routes through here.
"""
import re

ANSI = re.compile(r"\x1b\[[0-9;]*m")
HEADER = re.compile(r"^#\s*([A-Z0-9]+)\s*[—-]\s*COMBINED ANALYSIS", re.MULTILINE)
BANNER = re.compile(r"VERDICT:\s*(.+?)\s*\[\s*(.+?)\s*\]")


def _coarse(direction):
    d = direction.upper()
    if "LONG" in d:
        return "LONG"
    if "SHORT" in d:
        return "SHORT"
    if "WATCH" in d or "CAUTION" in d:
        return "WATCH"
    return "PASS"


def run(stdout, args):
    text = ANSI.sub("", stdout or "")

    m = BANNER.search(text)
    if not m:
        raise ValueError("no VERDICT banner found in analyse output")
    direction = m.group(1).strip()
    tier = m.group(2).strip()

    h = HEADER.search(text)
    ticker = h.group(1) if h else str(args.get("ticker", "")).upper().replace("USDT", "")

    return {
        "ticker": ticker,
        "verdict": _coarse(direction),
        "tier": tier,
        "direction": direction,
    }
