#!/bin/sh
# loris_ingest — pull the CRIME DESK panel snapshot from the clipboard into state/
#
# Flow: on loris.tools, click "copy json" in the CRIME DESK panel (ops/loris-desk-filter.user.js),
# then run this in the desk repo:  ./ops/loris_ingest.sh
# The orchestrator session reads state/loris_discover.json (clean JSON, §1 discipline).
#
# The snapshot is a DISCOVERY prior only: funding is loris' 8h-normalized print converted to ~%/4h
# (verify per-interval on venue before any verdict — §3), and oi_chg24_pct is single-source.

set -e
cd "$(dirname "$0")/.."
out="state/loris_discover.json"

pbpaste > "$out.tmp"

python3 - "$out.tmp" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    sys.exit(f"clipboard is not valid JSON ({e}) — click 'copy json' in the CRIME DESK panel first")
if 'candidates' not in d or 'generated_at' not in d:
    sys.exit("clipboard JSON is not a CRIME DESK snapshot (missing candidates/generated_at)")
n = len(d['candidates'])
sigs = {}
for c in d['candidates']:
    s = c.get('signature')
    if s: sigs[s] = sigs.get(s, 0) + 1
print(f"ok: {d.get('view')} snapshot {d['generated_at']} — {n} candidates", sigs or '')
PY

mv "$out.tmp" "$out"
echo "written: $out"
