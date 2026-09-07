# page_grammar — lock-screen grammar for every pushed page (SPEC-141, v2 SPEC-161)

## Purpose
Internal helper (`ops/page_grammar.py`, pure functions, no I/O) — the user is live on
the ntfy phone channel and pages were reading as engineer-speak (`verdict CONFIRMS→
BREAKS`, no price/level/action; `TAG STALE-THESIS: live 0.53 is 12% past nearest anchor
0.60`, desk jargon; wallet balances as `1.90324e+06 → 1.90252e+06 (-718.135)`). Every
pushed page now follows one grammar: **TICKER · PRICE · WHAT HAPPENED · DO THIS**.

**SPEC-161 (2026-08-25, "I'm still getting a lot of unreadable ntfys"):** the root cause
was `note` — free-text analyst marginalia (multi-clause prose, dates, §-refs, em-dashes)
on `watch_level`/`funding_watch` entries — getting interpolated VERBATIM into the page
body, then hard-truncated. **`note` is now NEVER read by any `page_*` function.** Bodies
are COMPOSED only from structured fields (ticker, live price, level+direction, thesis
numbers) plus the new optional `page_label` (≤40 chars, on a `watch_level`/
`funding_watch` entry) — validated and truncated at **commit-read time**
(`thesis._clip_page_label`, called from `thesis._parse_watch_level_element` and
`classify._normalize_funding_watch`), never inside `page_grammar.py` itself. When
present, `page_label` is the ONLY prose a body may carry, and it always appears whole
(already ≤40 chars by the time a page function sees it). When absent, the body is the
plain mechanical line + a canned generic action phrase (`convert to entry or dismiss`)
— never a fabricated instruction.

## Contract
Title = severity emoji + ticker + event word. Body composes to ≤90 chars, `BODY_MAX=120`
is a hard backstop that must **never cut a number** — `_finalize_body` drops the
trailing clause at the last clean separator instead of a mid-token slice. Bodies are
built off the committed thesis (`entry_zone`/`stop`/`tp`/`direction`) and the breach's
structured fields, never invented at page time. No usable thesis field → `read board`.

```python
import page_grammar as PG
PG.page_triggers(ticker, price, thesis)      # 🟢 T TRIGGERED
PG.page_breaks(ticker, price, thesis)        # 🔴 T BREAKS
PG.page_stop_breach(ticker, price, thesis)   # ⛔ T STOP — SPEC-161: "STOP: T 1 >= S — close short (entry E)"
PG.page_watch_armed(ticker, price, breach, thesis=None)   # 👁 T ARMED, or 🟢 T TPn if page_label contains "tp"
PG.page_funding_armed(ticker, funding_4h, breach)  # 👁 T ARMED (SPEC-142, funding cross)
PG.page_drift(ticker, drift)                 # ⚠️ T DRIFTED
PG.page_tape(ticker, raw_msg)                # ⚠️ T TAPE
```
Each returns `(title, body)`.

**SPEC-161 req 3 — imperative ACT shapes** (numbers first, action verb leads):
- `page_stop_breach`: `STOP: GALA 0.00212 ≥ 0.00211 — close short (entry 0.00198)`
  (`≥`/`close short` for a SHORT thesis, `≤`/`close long` for LONG; entry is
  `thesis.entry` if set, else the `entry_zone` bound nearest the stop).
- `page_watch_armed`, TP-flavored (`breach.page_label` contains "tp", case-insensitive):
  `TP1: GALA 0.00165 tagged — <page_label>`. The `TP1`/`TP2`/… TAG is derived
  MECHANICALLY — the breached price's position in `thesis.tp` — never parsed out of the
  label text; falls back to `TP1` when the level doesn't match a committed TP.
- `page_watch_armed`, otherwise (armed-entry cross): `ENTRY? CASHCAT 0.00177 < 0.178 —
  <page_label or "convert to entry or dismiss">; stop 0.1955` (the `; stop …` suffix
  needs the new `thesis` param — board_tick.py passes `thesis_by_ticker.get(ticker)`).

Human-number helpers (no scientific notation, ever):
```python
PG.fmt_price(x)                 # natural precision — "0.0412", not "4.12e-02"
PG.fmt_compact(n)                # "1.90M", "1.50K"
PG.fmt_balance_change(prev, cur) # "1.90M → 1.90M (-718 · -0.04%)"
```

## Callers
- `ops/board_tick.py` `tick()` — builds every push title/body through these functions;
  `_priority_for(cls, level)` maps the same event class to the ntfy `Priority` header
  (BREAKS/stop-breach → urgent, TRIGGERS/watch-cross → high, else default).
- `ops/tape_watch.py` `_pattern_reads` — composes human verdict phrases (never raw
  detector-type tokens like `operator_profit_take`); `page_tape` titles + terminates
  whatever it returns.
- `ops/balance_surveil.py` `assess_wallet` — the balance-drop message uses
  `fmt_balance_change` instead of raw `:g` formatting (the scientific-notation bug).
- `ops/surveil.sh` — the nonce-fired and balance-drained pages both now pass `urgent`
  as `ops/notify.sh`'s 3rd arg; the balance-drop summary uses a compact-unit formatter
  inline (bash can't import a Python module mid-pipeline).

## ops/notify.sh (SPEC-141 addition)
Optional 3rd arg `<priority>` (`urgent|high|default`) forwards as the ntfy `Priority:`
header; absent = today's behavior (no header). The osascript leg is unaffected (macOS
notifications have no priority concept).

See docs/board_tick.md for the full push-delivery pipeline. Tests:
`tests/test_page_grammar.py` (pure functions), `tests/test_spec141_page_grammar.py`
(notify.sh priority header, board_tick wiring, balance_surveil human numbers).
