# inbox — surveillance event ingestion (SPEC 45)

## Purpose
`ops/surveil.sh` + launchd write nonce escalations to `state/nonce_alerts.log`; before
this capability nothing read it — the desk's only push channel dead-ended in a log file.
CLAUDE.md §8 calls a dormant mega-safe lighting up "the biggest Stage-5 escalation" and
notes the cascade outruns the technical breakdown. `inbox` makes those events reach the
board: `classify` rows carry a per-ticker alert summary, `brief` surfaces the event
bodies, and an ack cursor keeps consumed events out of the way.

## Sources
- `state/nonce_alerts.log` — surveil.sh lines. `<ts> ESCALATION xN <envelope>` becomes
  one HIGH event per alerted ticker; `<ts> SWEEP_ERROR …` becomes one LOW ops event.
  The `<envelope>` is parsed tolerant of BOTH shapes (SPEC 64): the orchestrator-wrapped
  `{"ok":..,"data":{"alerts":[…]}}` AND the **raw** `onchain_board` JSON
  `{"scanned":N,"alerts":[…],"board":[…]}`. The restored multi-alert sweep logged the
  raw form; the wrapped-only read found 0 alerts in it → the 9-day silent death (8
  ESCALATION alerts, 0 unconsumed). `_extract_alerts` now checks `data.alerts` then
  top-level `alerts`.
- `state/inbox_events.jsonl` — normalized events appended by other producers via
  `inbox.append_event(ts, ticker, source, severity, msg)` (SPEC 46's `board_tick` writes
  verdict-delta events here).

Cursor: `state/inbox_cursor.json` `{"through_ts": …}` — events at or before the cursor
are consumed. The write is atomic (tmp + rename).

## Contract
```
inbox '{}'                                       → {unconsumed:[{ts,ticker,source,severity,msg}], n}
inbox '{"ticker":"LAB"}'                         → {n, max_severity, events}
inbox '{"op":"ack","through_ts":"<ISO>"}'        → {acked_through, remaining}
```

## Example
```
$ python3 orchestrator.py inbox '{}'
{"ok": true, "data": {"unconsumed": [{"ts": "2026-06-10T08:00:00Z", "ticker": "LAB",
  "source": "nonce_surveil", "severity": "HIGH",
  "msg": "dormant safe FIRED [team-safe-2(4→7)] kind=execution — §8 bid-pull/top"}], "n": 1}, …}
```

## Board integration
Every `classify` row gains `alerts: {n, max_severity}`; when `max_severity` is HIGH a
short note is appended to `reason`. **The verdict is never overridden** — §0.5
state-machine purity: an on-chain escalation is data for the Designer unless it matches
a named `invalidation` field. The engine surfaces; the Designer judges.

## Gotchas
- `ack` is inclusive: events with `ts <= through_ts` are consumed.
- Severity enum is `LOW | MED | HIGH`; `append_event` coerces anything else to MED.
- Unparseable log lines are skipped silently (the log is append-only shell output) —
  a malformed producer line cannot take the inbox down.
- ISO timestamps sort lexically; producers must write UTC `…Z` stamps (surveil.sh does).
