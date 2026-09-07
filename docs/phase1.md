# Phase 1b–1e — regime_check · price_structure · liq_magnets · pull5

The four remaining heavy noisy CLIs, native-ported following the `triage` template:
`build_*()` pure compute → human render default → `--json` machine path → registered
`filter:null`. All read live venue data; all are in `capabilities/`. One shared
test (`tests/test_phase1.py`) and this doc.

---

## regime_check — cross-venue funding/OI table

Funding + OI for one ticker across Binance/Bybit/Aster, regime per §2, divergence
(§8), and funding/OI z-scores vs the token's OWN ~30–40D history.

```
python3 orchestrator.py regime_check '{"ticker":"LAB"}'
```
```json
{ "ticker":"LAB",
  "funding": { "binance": {"latest":-0.60,"history":[...7],"regime":"NEGATIVE deepening … trap-formation loading",
                           "z":{"latest":-0.60,"mean":...,"std":...,"z":-1.8,"n":119}},
               "bybit": {...}, "aster": {...} },
  "oi_z":   { "binance": {"today":...,"z":...,"n":29}, "bybit": {...} },
  "oi_24h": { "binance": {"first":...,"last":...,"trend_pct":-12.3}, "bybit": {...}, "aster": {"current":...} },
  "divergence": {"binance":-0.60,"bybit":-0.75,"delta_pct":-0.15} }   // null if < 0.05% apart
```
- Funding is per-interval %. `regime` strings encode the §2 transitions (POS→NEG FLIP,
  NEGATIVE deepening, POSITIVE cooling, …). **Distinct from `regime_flip`** (that's the
  single live-vs-stored drift used by the board; this is the full multi-venue table).
- z-scores are relative-extremeness vs the token's own distribution (n≥10 to report);
  crypto funding is fat-tailed — read |z|≥2 as relative, not Gaussian probability.

## price_structure — Layer 4 daily structure

```
python3 orchestrator.py price_structure '{"ticker":"LAB","days":90,"squeeze_pct":15}'
```
Key contract fields: `range_pos` (0=ATL,100=ATH), `off_ath_pct`/`off_atl_pct`,
`ath`/`atl` + dates + `days_since_*`, `squeezes[]` + `squeeze_pattern`
(`diminishing`=Stage-5 exit fingerprint / `growing`=trap accelerating / `mixed` / `none`),
`vol_profile` (green/red M$, red_green_ratio, read=cascade|bounce|balanced),
`structure` (lower/higher highs/lows, read=downtrend|uptrend|mixed), `compression`
(tightness + wedge bool), `young_listing`. Unlisted ticker → `{"error": ...}`.

## liq_magnets — Layer 3 magnet zones (volume-profile proxy)

```
python3 orchestrator.py liq_magnets '{"ticker":"LAB","days":14,"interval":"15m","buckets":100}'
```
Contract: `current`, `range_lo`/`range_hi`, `hvns[]` (each `{p_lo,p_hi,p_peak,volume,
volume_m,bucket_count,dist_pct}` sorted by volume), `upside_magnets`/`downside_magnets`
(peak prices split by current), `round_numbers[]` (`{price,dist_pct,implicit_magnet}`).
**Not** a Coinglass replica — approximate; verify trade-critical levels on the real chart.

## pull5 — unified multi-layer pull (§14)

Aggregates the layers into ONE object (composes the `build_*` fns above + Coingecko):
```
python3 orchestrator.py pull5 '{"ticker":"LAB"}'
python3 orchestrator.py pull5 '{"ticker":"BILL","cg_id":"billions-network"}'
python3 orchestrator.py pull5 '{"ticker":"LAB","onchain":"1"}'   # add the slow on-chain layer
```
```json
{ "ticker":"LAB",
  "layer0_coingecko": {name,symbol,categories,contracts,market_cap,fdv,fdv_mc_ratio,
                       fdv_mc_redflag,pct_circulating,supply_locked_flag,price,ath,atl,chg_24h/7d/30d,vol_24h},
  "layer2_regime":    { …regime_check… },
  "layer3_magnets":   { …liq_magnets… },
  "layer4_structure": { …price_structure… },
  "layer1_onchain":   null }     // null unless onchain arg passed (slow RPC, opt-in)
```
- On-chain is **opt-in** (pass any truthy `onchain`) because the wallet sweep is slow/
  often RPC-degraded; the Designer normally calls the `onchain` capability separately.
- Layer 5 (catalyst/sentiment) is manual by design — not fetched.

---

## Optional-arg plumbing

These capabilities use the orchestrator's optional-group syntax `[--flag {key}]` — the
flag is emitted only when `key` is present in args, dropped otherwise (so omitting
`days`/`interval`/`cg_id`/`onchain` falls back to the script default). See
`docs/orchestrator.md`.

## Tests

`tests/test_phase1.py`: per-capability out-contract on LAB, optional-arg handling,
human-view-non-JSON, pull5 aggregation through the orchestrator, registry integrity.
