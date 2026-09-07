# perpfinder — keyless multi-venue breadth API, second-source only (SPEC-151)

```
python3 orchestrator.py perpfinder '{"mode":"funding","ticker":"HEMI"}'
python3 orchestrator.py perpfinder '{"mode":"liqs"}'
python3 orchestrator.py perpfinder '{"mode":"slippage","ticker":"BTC","size":1000,"side":"buy"}'
```

## Doctrine — read this before wiring it into anything (§3, non-negotiable)

PerpFinder is an **aggregator — breadth and second source ONLY**. Its funding prints are
**1h-NORMALIZED** (`rate1h`), so placeholder-floor detection (SPEC-19/108) is impossible on
them: **they may never feed verdict-gating funding selection.** Venue-native prints (and
Velo) stay authoritative. Every funding row carries `normalized:true`.

This capability is NOT wired into `venue_map`, `scan mode=oi_surge`, or `regime_check`
(SPEC-151 explicit non-goal — no follow-on yet). **SPEC-159** is the one exception: `brief`
composes this capability's `funding` mode into a `venue_breadth` block (wide-and-shallow,
every venue PerpFinder covers), fetched LAST and best-effort so it never blocks/slows
`brief`'s wired reads. That wiring is display/breadth-only and does not relax the doctrine
above — see [brief.md](brief.md) §SPEC-159. `venue_map` (SPEC-129, the deep 9-venue read)
is unaffected and stays the authoritative venue-role read.

## Why this one, when loris was rejected (EVAL-loris.md §2)

Keyless, free, CORS-open, self-describing (`schemaVersion` in every body), explicit rate
limits, explicit invitation to use with attribution. No secret in the repo (SPEC-P4 survives).

## Live-verified shape (2026-08-25, SPEC-158 — the schema DRIFTED from the 2026-08-19/20
build: build against THIS, the SPEC-151 addenda are superseded where they conflict)

- Keyless, `HTTP 200`, no auth headers. Base `https://perpfinder.com/api/data/`.
- `funding-rates`: **2253 symbols × 27 venues in one call** — the desk's whole book plus the
  DEX tail (Lighter, Paradex, GRVT, Hibachi, Bluefin, Orderly, Apex Omni, Aevo, dYdX, GMX, …).
  Top-level container is `rows` (was `symbols`), each row keyed `symbol` (was `asset`) with a
  per-venue map at `exchanges` (was `venues`); each venue entry carries `rate1h`/`oi`/`price`
  together. `fieldSupport` moved to top-level `meta.fieldSupport`, keyed by venue name, and
  covers **only `oi`/`price`** (`observed`|`unsupported`|`temporarily_unavailable`) — it does
  NOT cover `rate1h`, so this capability surfaces it as a venue-level lookup per row, never a
  per-symbol fact. `nullSemantics` also moved under `meta` and is preserved verbatim (null =
  not carried by the venue feed; 0 = true numeric zero — matches SPEC-129 rule 4).
  **SPEC-174 #4: `oi` on this endpoint is already USD notional, not base-asset quantity**
  (live-verified 2026-08-28 — BTC/Binance `oi=8,396,617,769.53`; as BTC units that would be
  8.4 billion BTC against a ~19.8M supply). Never multiply it by `price` again — that bug
  corrupted `brief`'s `venue_breadth.total_oi_usd` (H: total $1.13M vs its own Bybit row
  $8.29M; MANTRA total $12K vs `venue_map`'s $5.2M).
- `open-interest` and `volume` are **venue-level aggregates**, not per-asset — `byExchange` /
  `exchanges` respectively (shape unchanged since SPEC-151). They cannot answer "what is
  HEMI's OI on Gate" (the funding matrix already carries that, per-asset per-venue).
- `oi-long-short` is **NOW POPULATED** (live-verified 2026-08-25, was empty at SPEC-151 time).
  Top-level container is `protocols` (not `rows`), each entry carrying `longOI`/`shortOI`/
  `totalOI`/`longPct`; top-level `totalLong`/`totalShort` too. Still renders
  `ok:true, rows:[], empty_reason` on the rare call where `protocols` itself comes back empty.
- `slippage` is **majors-only** (`BTC, ETH, SOL, XRP, BNB, DOGE, HYPE`) — useless for the
  desk's micro-cap book. The live-ladder `depth` capability stays the only §7 size-to-exit
  input for desk names.
- `liquidations` is a genuine multi-venue liq **event stream**, sourced from Coinalyze per
  their own docs — breadth/corroboration next to `liqs.py`'s OKX-native ground truth
  (SPEC-103), never a replacement for it.
- `funding-history` is **BTC/ETH only** — useless for the desk's book; `regime_check`'s own
  funding history stays the per-name source.
- No `openapi.json` / `api-manifest` (both 404) — the contract is unversioned but
  self-describing (`schemaVersion` in the body is the real version signal). **The wire shape
  has now drifted once already (SPEC-151 → SPEC-158) with schemaVersion staying `1` through
  the drift** — schemaVersion is NOT a reliable drift signal; the shape-drift guard below is.
- No rate-limit response headers are exposed. The documented caps (40/min funding·oi·volume,
  30/min slippage) are respected conservatively via the TTL cache, never by polling in a loop.

## Shape-drift guard (SPEC-158)

`build_perpfinder` never renders a silent empty universe. Each mode's normalizer reports the
size of its correctly-keyed top-level container **before** any `ticker`/`venues` filtering; if
that raw count is zero AND the response body is non-trivially sized (>`SHAPE_DRIFT_BODY_BYTES`,
1000 bytes), the call fails loud instead of rendering `rows: []`:

```
{"ok": false, "mode": "funding", "reason": "shape_drift",
 "shape_drift": {"body_type": "dict", "first_keys": [...], "body_bytes": N, "detail": "..."}}
```

A `ticker` that legitimately matches nothing in a real universe (e.g. a typo) is NOT drift —
the raw-count check runs before the ticker filter, so it stays nonzero and the call renders
`rows: []` normally, same as before. A genuinely small/empty body (e.g. `oi_long_short` on a
call where `protocols` really is `[]`) stays under the byte floor and is not flagged either —
only a real ~KB+ body yielding zero rows is treated as a parser/fixture mismatch.

## Contract

`build_perpfinder(mode, ticker=None, venues=None, size=None, side=None, days=None)` →

SPEC-174: CLI `mode` is now optional, defaulting to `funding` — `perpfinder.py --json` (no
positional) no longer argparse-errors.

Always: `{ok, mode, meta: {source:"perpfinder", updatedAt, dataStatus, schemaVersion,
attribution, normalized? (funding only), warning? (schemaVersion drift)}}`.

The shape-specific key(s) DIFFER per mode (no uniform `rows: [...]` — the wire shapes genuinely
differ, see live-verified shape above):

| mode | shape key(s) | notes |
|---|---|---|
| `funding` | `rows: [{asset, venue, funding_pi_4h, rate_raw_pi, interval_min, normalized:true, oi, price, field_support}]`, `venues_covered` | `ticker`/`venues` filter the matrix; `field_support` is a venue-level `{oi, price}` lookup (never rate1h) |
| `oi` | `byExchange: [{name, oi}]`, `total_oi` | venue-level; `oi` preserves `null` |
| `oi_long_short` | `rows: [{slug, name, long_oi, short_oi, total_oi, long_pct}]`, `total_long`, `total_short` (or `rows: [], empty_reason` on a zero-protocol call, or `rows: [], not_listed:true` when `ticker` matches no protocol) | DEX-PROTOCOL aggregate table (slug/name, e.g. "gmx"/"GMX"), not a per-token matrix; `ticker` filters to a protocol whose slug/name matches it (SPEC-174 #2 — previously silently ignored, every caller got the full unfiltered table) |
| `volume` | `exchanges: [{name, volume24h, symbol_count, top_symbols}]`, `venues_covered` | venue-level |
| `liqs` | `events: [{exchange, symbol, side, size_usd, price, timestamp}]` | `ticker`/`venues` filter |
| `slippage` | `rows: [{venue, total_bps, vwap, spread}]` sorted by `total_bps` asc | requires `ticker`+`size`+`side`; majors-only |
| `funding_history` | `dataset_start`, `maturity`, `history` | requires `ticker` (defaults BTC) + `days`; majors-only |

Failure: `{ok:false, mode, reason}` — never a partial render (§3). `mode` not in the seven
above, or `slippage` missing `ticker`/`size`/`side`, fails loudly before any network call
(SPEC-120 addendum convention). A parsed-but-effectively-empty non-trivial body fails loudly
too, `reason: "shape_drift"` with a `shape_drift` diagnostic block (see guard above, SPEC-158).

**SPEC-187 fix — `funding_pi_4h` units:** `rate1h` (the raw wire field) is a fraction, the
same convention every venue-direct `fundingRate` print in this codebase uses (e.g. Binance/
Bybit raw `-0.003` == `-0.3%`) — `funding_pi_4h` used to be a bare `rate1h * 4`, which
skipped the fraction→percent step `regime_flip.py` applies to every other funding read
(`funding_pi = funding_raw * 100`, THEN `to_4h`), leaving every `funding_pi_4h` print ~100x
too small. It now runs the same two-step transform (`regime_flip.to_4h`, imported directly —
one canonical implementation, not a parallel literal), with `interval_min` fixed at 60 since
`rate1h` is documented as already hour-normalized (see Doctrine above). `rate_raw_pi` (the
untouched raw fraction) and `interval_min` are now surfaced per row too — display/
reconciliation only, the doctrine above is unchanged (never a verdict input).

## Rate-limit citizenship

Every GET is cached in `state/perpfinder_cache.json`, TTL `config/perpfinder.json:cache_ttl_s`
(default 120s), keyed on `mode` + request params. A repeat call inside the TTL never touches
the wire. A `429` gets exactly **one** retry honoring `Retry-After` (capped at 5s so a bad/huge
header can never hang a run), then fails loudly — never a poll loop inside one invocation. Any
other HTTP error, timeout, or malformed JSON body is an immediate loud failure (no retry).

## Attribution

`meta.attribution = "Data by PerpFinder (perpfinder.com)"` on every successful response —
PerpFinder's stated condition for free use.
