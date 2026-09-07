#!/usr/bin/env python3
"""venue_map.py — full cross-venue coverage map for one perp (SPEC-129).

The §0.6.3b read — *across which venues is the OI constructed, and which venue plays
which role (mark-engine / size-book / exit / hedge)* — has no deterministic capability.
`regime_check` covers Binance/Bybit/Bitget/Aster; `hyperliquid` is separate; the DEX long
tail (Lighter, Paradex, Extended, Vest, …) where AMM crews increasingly run their perps
was invisible to the desk. This is one live sweep answering: which venues list this perp,
and where is the book — funding (4h-normalized per SPEC-112), OI in USD, 24h volume.

  python3 capabilities/venue_map.py LAB --json

Venue adapters are `probe_<venue>(ticker) -> {status: ok|not_listed|error, ...}` behind
one interface (the `VENUES` dict) — adding a venue later is one function + one registry
row. `build_venue_map(ticker, venues=VENUES)` fans them out CONCURRENTLY, one thread per
venue, each bounded by `per_venue_timeout` (~6s) so one dead DEX can't stall the sweep
(SPEC-127 lesson) — a venue whose probe raises or exceeds the budget is isolated to that
venue's `status:"error"`, never propagated.

§3 doctrine: a probe error/timeout/malformed response is `status:"error"` for that venue —
NEVER rendered as "not listed" and never a 0 datum. `not_listed` means the venue answered
cleanly and confirmed no market for this ticker (e.g. Binance's `-1121 Invalid symbol`,
Bybit's `retCode 10001`, Extended's all-zero-field 200 for an unlisted market) — a
distinct, positive signal, not an absence-of-evidence guess. Floor prints (the CEX
0.005%-sentinel, `regime_flip._is_floor`; an exact-zero DEX-native rate, the `hyperliquid`
convention) are flagged `is_floor` and excluded from `funding_extreme` (SPEC-19/108).

Reuses `regime_flip.to_4h` / `.binance_interval_min` / `.bybit_interval_min` / `._is_floor`
(the existing normalization/floor layer) and `hyperliquid.resolve`/`.universe`/`.is_floor`
(the existing HL venue source) rather than re-deriving them — new code here is limited to
the venues those modules don't cover (Lighter, Paradex, Extended, Vest) plus the
not-listed/error-distinguishing fetch wrapper regime_flip's `fetch()` doesn't provide (it
discards HTTPError bodies, so it can't tell "invalid symbol" from "timeout").

Live-verified endpoints (2026-07-29): Lighter `orderBookDetails` + `funding-rates`
(filter `exchange:"lighter"` for the venue's OWN rate — the endpoint also carries
reference rates from binance/bybit/hyperliquid for the same symbol), Paradex
`markets/summary` (funding_rate is the quoted 8h amount despite continuous
recalculation — docs.paradex.trade/risk/funding-mechanism), Extended
`api.starknet.extended.exchange/api/v1/info/markets/{M}/stats` (hourly funding,
`openInterest`/`dailyVolume` already USD). Vest's documented endpoint
(`serverprod.vest.exchange/v2`, docs.vest.exchange/vest-api) returned Cloudflare 530
("origin unreachable") at build/verify time — implemented per docs anyway; a dead
origin exercises the same `status:"error"` path as any other outage, never fabricates
a datum, and should self-heal if/when the origin returns. Re-verify live before trusting
Vest reads.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import colors as C
import regime_flip as RF
import hyperliquid as HL

TIMEOUT = 6                 # per-HTTP-call socket timeout
VENUE_TIMEOUT = 6           # per-venue overall probe budget (SPEC-129 req 5)
UA = {"User-Agent": "venue-map/1.0"}


def _get(url, timeout=TIMEOUT):
    """GET -> (data, err). err is None on 2xx; an HTTP status int on HTTPError (`data` is
    still the parsed error body where the venue put a machine-readable reason there — the
    not_listed detectors read it); or the string "error" for anything else (timeout /
    connection failure / non-JSON body)."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = None
        return body, e.code
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None, "error"


def _ok(funding_raw_pct, interval_min, oi_usd=None, vol24h_usd=None, is_floor=False,
       oi_raw=None, mark_price=None):
    """funding_raw_pct = per-interval rate already in PERCENT (not raw decimal fraction).
    `oi_raw` (SPEC-178: base-asset-unit OI, when the venue's own response carries it
    alongside the USD figure — never re-derived by dividing oi_usd/mark_price, which
    would silently fabricate precision the venue never published) and `mark_price` are
    additive/optional — the sampler's redenomination detector needs the raw series
    untainted by this probe's own USD conversion."""
    out = {
        "status": "ok",
        "funding_raw_pi": round(funding_raw_pct, 6),
        "interval_min": interval_min,
        "funding_pi_4h": RF.to_4h(round(funding_raw_pct, 6), interval_min),
        "is_floor": bool(is_floor),
    }
    if oi_usd is not None:
        out["oi_usd"] = round(oi_usd, 2)
    if vol24h_usd is not None:
        out["vol24h_usd"] = round(vol24h_usd, 2)
    if oi_raw is not None:
        out["oi_raw"] = oi_raw
    if mark_price is not None:
        out["mark_price"] = mark_price
    return out


def _not_listed():
    return {"status": "not_listed"}


def _error(reason):
    return {"status": "error", "reason": str(reason)[:160]}


# ---- venue adapters — probe_<venue>(ticker) -> status block ----

def probe_binance(ticker):
    sym = f"{ticker}USDT"
    pi, err = _get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}")
    if err == "error":
        return _error("premiumIndex fetch failed")
    if isinstance(pi, dict) and pi.get("code") == -1121:
        return _not_listed()
    if err is not None or not isinstance(pi, dict) or "lastFundingRate" not in pi:
        return _error(f"premiumIndex malformed (http {err})")
    try:
        funding_raw = float(pi["lastFundingRate"])
        price = float(pi["markPrice"])
    except (KeyError, TypeError, ValueError):
        return _error("premiumIndex unparseable")
    oi_usd = oi_raw = None
    oid, _ = _get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={sym}")
    if isinstance(oid, dict) and oid.get("openInterest") is not None:
        try:
            oi_raw = float(oid["openInterest"])
            oi_usd = oi_raw * price
        except (TypeError, ValueError):
            pass
    vol_usd = None
    t24, _ = _get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={sym}")
    if isinstance(t24, dict) and t24.get("quoteVolume") is not None:
        try:
            vol_usd = float(t24["quoteVolume"])
        except (TypeError, ValueError):
            pass
    interval_min = RF.binance_interval_min(sym)
    return _ok(funding_raw * 100, interval_min, oi_usd, vol_usd, RF._is_floor(funding_raw),
               oi_raw=oi_raw, mark_price=price)


def probe_bybit(ticker):
    sym = f"{ticker}USDT"
    d, err = _get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}")
    if err == "error":
        return _error("tickers fetch failed")
    if err is not None or not isinstance(d, dict):
        return _error(f"tickers http {err}")
    if d.get("retCode") == 10001 or "symbol invalid" in (d.get("retMsg") or "").lower():
        return _not_listed()
    if d.get("retCode") != 0:
        return _error(f"retCode {d.get('retCode')}: {d.get('retMsg')}")
    lst = (d.get("result") or {}).get("list") or []
    if not lst:
        return _not_listed()
    x = lst[0]
    try:
        funding_raw = float(x["fundingRate"])
        oi_usd = float(x["openInterestValue"]) if x.get("openInterestValue") not in (None, "") else None
        oi_raw = float(x["openInterest"]) if x.get("openInterest") not in (None, "") else None
        vol_usd = float(x["turnover24h"]) if x.get("turnover24h") not in (None, "") else None
        mark = float(x["markPrice"]) if x.get("markPrice") not in (None, "") else None
    except (KeyError, TypeError, ValueError):
        return _error("tickers row unparseable")
    interval_min = RF.bybit_interval_min(sym)
    return _ok(funding_raw * 100, interval_min, oi_usd, vol_usd, RF._is_floor(funding_raw),
               oi_raw=oi_raw, mark_price=mark)


def probe_aster(ticker):
    sym = f"{ticker}USDT"
    pi, err = _get(f"https://fapi.asterdex.com/fapi/v1/premiumIndex?symbol={sym}")
    if err == "error":
        return _error("premiumIndex fetch failed")
    if isinstance(pi, dict) and pi.get("code") == -1121:
        return _not_listed()
    if err is not None or not isinstance(pi, dict) or "lastFundingRate" not in pi:
        return _error(f"premiumIndex malformed (http {err})")
    try:
        funding_raw = float(pi["lastFundingRate"])
        price = float(pi["markPrice"])
    except (KeyError, TypeError, ValueError):
        return _error("premiumIndex unparseable")
    oi_usd = oi_raw = None
    oid, _ = _get(f"https://fapi.asterdex.com/fapi/v1/openInterest?symbol={sym}")
    if isinstance(oid, dict) and oid.get("openInterest") is not None:
        try:
            oi_raw = float(oid["openInterest"])
            oi_usd = oi_raw * price
        except (TypeError, ValueError):
            pass
    vol_usd = None
    t24, _ = _get(f"https://fapi.asterdex.com/fapi/v1/ticker/24hr?symbol={sym}")
    if isinstance(t24, dict) and t24.get("quoteVolume") is not None:
        try:
            vol_usd = float(t24["quoteVolume"])
        except (TypeError, ValueError):
            pass
    interval_min = 240   # default 4h; derived from settle-timestamp deltas below if available
    hist, _ = _get(f"https://fapi.asterdex.com/fapi/v1/fundingRate?symbol={sym}&limit=2")
    if isinstance(hist, list) and len(hist) >= 2:
        try:
            ts = sorted(int(h["fundingTime"]) for h in hist)
            if ts[-1] > ts[-2]:
                interval_min = round((ts[-1] - ts[-2]) / 60000)
        except (TypeError, KeyError, ValueError):
            pass
    return _ok(funding_raw * 100, interval_min, oi_usd, vol_usd, RF._is_floor(funding_raw),
               oi_raw=oi_raw, mark_price=price)


def probe_bitget(ticker):
    sym = f"{ticker}USDT"
    base = "https://api.bitget.com/api/v2/mix/market"
    cur, err = _get(f"{base}/current-fund-rate?symbol={sym}&productType=usdt-futures")
    if err == "error":
        return _error("current-fund-rate fetch failed")
    if isinstance(cur, dict) and cur.get("code") not in (None, "00000"):
        if cur.get("code") == "40034":
            return _not_listed()
        return _error(f"bitget code {cur.get('code')}: {cur.get('msg')}")
    if err is not None or not isinstance(cur, dict):
        return _error(f"current-fund-rate http {err}")
    try:
        d = cur["data"][0]
        funding_raw = float(d["fundingRate"])
        interval_min = int(float(d.get("fundingRateInterval") or 8)) * 60
    except (KeyError, IndexError, TypeError, ValueError):
        return _error("current-fund-rate unparseable")
    oi_usd = vol_usd = oi_raw = price = None
    tick, _ = _get(f"{base}/ticker?symbol={sym}&productType=usdt-futures")
    if isinstance(tick, dict) and tick.get("code") == "00000":
        try:
            t = tick["data"][0]
            price = float(t["lastPr"])
            if t.get("holdingAmount"):
                oi_raw = float(t["holdingAmount"])
                oi_usd = oi_raw * price
            if t.get("usdtVolume") not in (None, ""):
                vol_usd = float(t["usdtVolume"])
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    return _ok(funding_raw * 100, interval_min, oi_usd, vol_usd, RF._is_floor(funding_raw),
               oi_raw=oi_raw, mark_price=price)


def probe_hyperliquid(ticker):
    meta = HL.post({"type": "meta"})
    if meta is None:
        return _error("HL meta fetch failed")
    name = HL.coin_for(ticker)
    if name not in HL.universe(meta):
        return _not_listed()
    meta_ctxs = HL.post({"type": "metaAndAssetCtxs"})
    if meta_ctxs is None:
        return _error("HL metaAndAssetCtxs fetch failed")
    v = HL.resolve(ticker, meta_ctxs=meta_ctxs)
    if v is None:
        return _error("HL ctx resolve failed")
    out = {"status": "ok", "funding_raw_pi": v["funding_pi"], "interval_min": v["interval_min"],
           "funding_pi_4h": v["funding_4h"], "is_floor": v["is_floor"]}
    if v.get("oi") is not None:
        out["oi_usd"] = round(v["oi"], 2)
    if v.get("oi_base") is not None:
        out["oi_raw"] = v["oi_base"]
    if v.get("mark") is not None:
        out["mark_price"] = v["mark"]
    if v.get("vol_m") is not None:
        out["vol24h_usd"] = round(v["vol_m"] * 1e6, 2)
    return out


LIGHTER_BASE = "https://mainnet.zklighter.elliot.ai/api/v1"


def probe_lighter(ticker):
    sym = ticker.upper()
    obd, err = _get(f"{LIGHTER_BASE}/orderBookDetails")
    if err == "error" or err is not None:
        return _error("orderBookDetails fetch failed")
    details = (obd or {}).get("order_book_details") or []
    row = next((x for x in details if isinstance(x, dict) and x.get("symbol") == sym), None)
    if row is None:
        return _not_listed()
    try:
        mark = float(row["mark_price"])
        oi_base = float(row["open_interest"])
        vol_usd = (float(row["daily_quote_token_volume"])
                  if row.get("daily_quote_token_volume") is not None else None)
    except (KeyError, TypeError, ValueError):
        return _error("orderBookDetails row unparseable")
    fr, ferr = _get(f"{LIGHTER_BASE}/funding-rates")
    if ferr == "error" or ferr is not None:
        return _error("funding-rates fetch failed")
    rates = (fr or {}).get("funding_rates") or []
    own = next((x for x in rates
               if isinstance(x, dict) and x.get("symbol") == sym and x.get("exchange") == "lighter"), None)
    if own is None or own.get("rate") is None:
        return _error("no lighter-native funding rate for symbol")
    try:
        funding_raw = float(own["rate"])
    except (TypeError, ValueError):
        return _error("funding rate unparseable")
    return _ok(funding_raw * 100, 60, oi_base * mark, vol_usd, funding_raw == 0.0,
               oi_raw=oi_base, mark_price=mark)


def probe_paradex(ticker):
    market = f"{ticker.upper()}-USD-PERP"
    d, err = _get(f"https://api.prod.paradex.trade/v1/markets/summary?market={market}")
    if err == "error":
        return _error("markets/summary fetch failed")
    if isinstance(d, dict) and d.get("error") == "INVALID_REQUEST_PARAMETER":
        return _not_listed()
    if err is not None or not isinstance(d, dict):
        return _error(f"markets/summary http {err}")
    results = d.get("results") or []
    if not results:
        return _not_listed()
    row = results[0]
    try:
        funding_raw = float(row["funding_rate"])
        mark = float(row["mark_price"])
        oi_base = float(row["open_interest"])
        vol_usd = float(row["volume_24h"]) if row.get("volume_24h") not in (None, "") else None
    except (KeyError, TypeError, ValueError):
        return _error("markets/summary row unparseable")
    return _ok(funding_raw * 100, 480, oi_base * mark, vol_usd, funding_raw == 0.0,
               oi_raw=oi_base, mark_price=mark)


def probe_extended(ticker):
    market = f"{ticker.upper()}-USD"
    d, err = _get(f"https://api.starknet.extended.exchange/api/v1/info/markets/{market}/stats")
    if err == "error":
        return _error("markets/stats fetch failed")
    if err is not None or not isinstance(d, dict):
        return _error(f"markets/stats http {err}")
    if d.get("status") != "OK" or not isinstance(d.get("data"), dict):
        return _error("markets/stats malformed")
    row = d["data"]
    try:
        mark = float(row.get("markPrice") or 0)
    except (TypeError, ValueError):
        mark = 0.0
    if mark == 0.0:
        return _not_listed()   # SPEC-129 §3: a genuinely-listed perp never marks at exactly 0
    try:
        funding_raw = float(row["fundingRate"])
        oi_usd = float(row["openInterest"])
        vol_usd = float(row["dailyVolume"]) if row.get("dailyVolume") not in (None, "") else None
    except (KeyError, TypeError, ValueError):
        return _error("markets/stats row unparseable")
    # Extended's `openInterest` is already USD — no separate base-asset field published,
    # so oi_raw stays absent rather than a fabricated oi_usd/mark division.
    return _ok(funding_raw * 100, 60, oi_usd, vol_usd, funding_raw == 0.0, mark_price=mark)


VEST_BASE = "https://serverprod.vest.exchange/v2"


def probe_vest(ticker):
    """Per docs.vest.exchange/vest-api (live-unreachable 2026-07-29, see module docstring)."""
    sym = f"{ticker.upper()}-PERP"
    d, err = _get(f"{VEST_BASE}/ticker/latest?symbols={sym}")
    if err == "error":
        return _error("ticker/latest fetch failed")
    if err is not None:
        return _error(f"ticker/latest http {err}")
    if not isinstance(d, list) or not d:
        return _not_listed()
    row = d[0]
    try:
        funding_raw = float(row["fundingRate"])
        oi_usd = float(row["openInterest"])
        vol_usd = float(row["volume24h"]) if row.get("volume24h") not in (None, "") else None
    except (KeyError, TypeError, ValueError):
        return _error("ticker/latest row unparseable")
    return _ok(funding_raw * 100, 60, oi_usd, vol_usd, funding_raw == 0.0)


def probe_gate(ticker):
    """Gate: one call carries funding+interval+mark; contract_stats carries OI-in-USD
    directly (no volume field on either call — SPEC-176 req 1, vol left unreported)."""
    sym = f"{ticker}_USDT"
    base = "https://api.gateio.ws/api/v4/futures/usdt"
    c, err = _get(f"{base}/contracts/{sym}")
    if err == "error":
        return _error("contracts fetch failed")
    if isinstance(c, dict) and c.get("label") == "CONTRACT_NOT_FOUND":
        return _not_listed()
    if err is not None or not isinstance(c, dict) or "funding_rate" not in c:
        return _error(f"contracts malformed (http {err})")
    try:
        funding_raw = float(c["funding_rate"])
        interval_min = round(int(c["funding_interval"]) / 60)
    except (KeyError, TypeError, ValueError):
        return _error("contracts unparseable")
    mark = None
    try:
        if c.get("mark_price") not in (None, ""):
            mark = float(c["mark_price"])
    except (TypeError, ValueError):
        pass
    cap = None
    try:
        if c.get("funding_rate_limit") not in (None, ""):
            cap = float(c["funding_rate_limit"])
    except (TypeError, ValueError):
        pass
    oi_usd = oi_raw = None
    stats, _ = _get(f"{base}/contract_stats?contract={sym}&interval=5m&limit=1")
    if isinstance(stats, list) and stats:
        try:
            oi_usd = float(stats[0]["open_interest_usd"])
        except (KeyError, TypeError, ValueError, IndexError):
            pass
        try:
            if stats[0].get("open_interest") is not None:
                oi_raw = float(stats[0]["open_interest"])
        except (KeyError, TypeError, ValueError, IndexError):
            pass
    is_floor = cap is not None and abs(abs(funding_raw) - cap) < 1e-9
    return _ok(funding_raw * 100, interval_min, oi_usd, None, is_floor,
               oi_raw=oi_raw, mark_price=mark)


def probe_mexc(ticker):
    """MEXC: ticker gives holdVol (contract units) + amount24 (already USD 24h vol);
    funding_rate gives rate+collectCycle(hours); a third `detail` call resolves
    `contractSize` (varies per symbol — BTC 0.0001, GALA 10 — R1 S3 unit-convention
    trap) to convert holdVol -> USD OI. Missing/failed detail degrades oi_usd to
    absent, never a fabricated value."""
    sym = f"{ticker}_USDT"
    base = "https://contract.mexc.com/api/v1/contract"
    t, err = _get(f"{base}/ticker?symbol={sym}")
    if err == "error":
        return _error("ticker fetch failed")
    if isinstance(t, dict) and t.get("success") is False:
        if t.get("code") == 1001:
            return _not_listed()
        return _error(f"mexc code {t.get('code')}: {t.get('message')}")
    if err is not None or not isinstance(t, dict) or not isinstance(t.get("data"), dict):
        return _error(f"ticker malformed (http {err})")
    row = t["data"]
    try:
        price = float(row["fairPrice"])
        hold_vol = float(row["holdVol"])
        vol_usd = float(row["amount24"]) if row.get("amount24") not in (None, "") else None
    except (KeyError, TypeError, ValueError):
        return _error("ticker row unparseable")
    fr, ferr = _get(f"{base}/funding_rate/{sym}")
    if ferr == "error" or not isinstance(fr, dict) or not isinstance(fr.get("data"), dict):
        return _error("funding_rate fetch failed")
    fdata = fr["data"]
    try:
        funding_raw = float(fdata["fundingRate"])
        interval_min = round(float(fdata["collectCycle"]) * 60)
    except (KeyError, TypeError, ValueError):
        return _error("funding_rate unparseable")
    cap = None
    try:
        if fdata.get("maxFundingRate") not in (None, ""):
            cap = float(fdata["maxFundingRate"])
    except (TypeError, ValueError):
        pass
    oi_usd = oi_raw = None
    d, _ = _get(f"{base}/detail?symbol={sym}")
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        try:
            contract_size = float(d["data"]["contractSize"])
            oi_raw = hold_vol * contract_size   # base-asset units
            oi_usd = oi_raw * price
        except (KeyError, TypeError, ValueError):
            pass
    is_floor = cap is not None and abs(abs(funding_raw) - cap) < 1e-9
    return _ok(funding_raw * 100, interval_min, oi_usd, vol_usd, is_floor,
               oi_raw=oi_raw, mark_price=price)


def probe_kucoin(ticker):
    """KuCoin: one call carries everything — funding+interval+OI(contracts)+multiplier+
    markPrice+24h-USD-turnover. OI USD = contracts × multiplier × mark (R1 S3 trap)."""
    sym = f"{ticker}USDTM"
    d, err = _get(f"https://api-futures.kucoin.com/api/v1/contracts/{sym}")
    if err == "error":
        return _error("contracts fetch failed")
    if isinstance(d, dict) and d.get("code") not in (None, "200000"):
        if d.get("code") == "404000":
            return _not_listed()
        return _error(f"kucoin code {d.get('code')}: {d.get('msg')}")
    if err is not None or not isinstance(d, dict) or not isinstance(d.get("data"), dict):
        return _error(f"contracts malformed (http {err})")
    row = d["data"]
    try:
        funding_raw = float(row["fundingFeeRate"])
        interval_min = round(int(row["fundingRateGranularity"]) / 60000)
        mark = float(row["markPrice"])
        multiplier = float(row["multiplier"])
        oi_contracts = float(row["openInterest"])
        oi_raw = oi_contracts * abs(multiplier)   # base-asset units
        oi_usd = oi_raw * mark
    except (KeyError, TypeError, ValueError):
        return _error("contracts row unparseable")
    vol_usd = None
    try:
        if row.get("turnoverOf24h") not in (None, ""):
            vol_usd = float(row["turnoverOf24h"])
    except (TypeError, ValueError):
        pass
    cap = None
    try:
        if row.get("fundingRateCap") is not None:
            cap = float(row["fundingRateCap"])
    except (TypeError, ValueError):
        pass
    is_floor = cap is not None and abs(abs(funding_raw) - cap) < 1e-9
    return _ok(funding_raw * 100, interval_min, oi_usd, vol_usd, is_floor,
               oi_raw=oi_raw, mark_price=mark)


def probe_okx(ticker):
    """OKX: funding-rate gives rate + settlement timestamps (interval derived, never
    assumed 8h) + cap; open-interest gives oiUsd directly; ticker's volCcy24h (base-asset
    units) needs × last-price for USD (no direct USD-volume field on this venue)."""
    inst = f"{ticker}-USDT-SWAP"
    base = "https://www.okx.com/api/v5"
    fr, err = _get(f"{base}/public/funding-rate?instId={inst}")
    if err == "error":
        return _error("funding-rate fetch failed")
    if isinstance(fr, dict) and fr.get("code") not in (None, "0"):
        if fr.get("code") == "51001":
            return _not_listed()
        return _error(f"okx code {fr.get('code')}: {fr.get('msg')}")
    if err is not None or not isinstance(fr, dict) or not fr.get("data"):
        return _error(f"funding-rate malformed (http {err})")
    try:
        row = fr["data"][0]
        funding_raw = float(row["fundingRate"])
        interval_min = round((int(row["fundingTime"]) - int(row["prevFundingTime"])) / 60000)
    except (KeyError, TypeError, ValueError, IndexError):
        return _error("funding-rate row unparseable")
    cap = None
    try:
        if row.get("maxFundingRate") not in (None, ""):
            cap = float(row["maxFundingRate"])
    except (TypeError, ValueError):
        pass
    vol_usd = last = None
    tick, _ = _get(f"{base}/market/ticker?instId={inst}")
    if isinstance(tick, dict) and tick.get("code") == "0" and tick.get("data"):
        try:
            t = tick["data"][0]
            last = float(t["last"])
            vol_ccy = float(t["volCcy24h"]) if t.get("volCcy24h") not in (None, "") else None
            if vol_ccy is not None:
                vol_usd = vol_ccy * last
        except (KeyError, TypeError, ValueError, IndexError):
            pass
    oi_usd = oi_raw = None
    oi, _ = _get(f"{base}/public/open-interest?instId={inst}")
    if isinstance(oi, dict) and oi.get("code") == "0" and oi.get("data"):
        try:
            oi_usd = float(oi["data"][0]["oiUsd"])
        except (KeyError, TypeError, ValueError, IndexError):
            pass
        try:
            if oi["data"][0].get("oiCcy") not in (None, ""):
                oi_raw = float(oi["data"][0]["oiCcy"])
        except (KeyError, TypeError, ValueError, IndexError):
            pass
    is_floor = cap is not None and abs(abs(funding_raw) - cap) < 1e-9
    return _ok(funding_raw * 100, interval_min, oi_usd, vol_usd, is_floor,
               oi_raw=oi_raw, mark_price=last)


def probe_htx(ticker):
    """HTX: swap_funding_rate gives the rate only (no interval field); swap_contract_info
    gives `settlement_period` (hours) for the interval; swap_open_interest gives `value`
    (OI, USD) AND `trade_turnover` (24h volume, USD) in one call. No published cap field
    found -> is_floor left False rather than guessed."""
    code = f"{ticker}-USDT"
    base = "https://api.hbdm.com/linear-swap-api/v1"
    fr, err = _get(f"{base}/swap_funding_rate?contract_code={code}")
    if err == "error":
        return _error("swap_funding_rate fetch failed")
    if isinstance(fr, dict) and fr.get("status") == "error":
        if fr.get("err_code") in (1332, 1014):
            return _not_listed()
        return _error(f"htx err {fr.get('err_code')}: {fr.get('err_msg')}")
    if err is not None or not isinstance(fr, dict) or not isinstance(fr.get("data"), dict):
        return _error(f"swap_funding_rate malformed (http {err})")
    try:
        funding_raw = float(fr["data"]["funding_rate"])
    except (KeyError, TypeError, ValueError):
        return _error("swap_funding_rate unparseable")
    interval_min = 480  # 8h default; overridden below when settlement_period resolves
    ci, _ = _get(f"{base}/swap_contract_info?contract_code={code}")
    if isinstance(ci, dict) and ci.get("status") == "ok" and isinstance(ci.get("data"), list) and ci["data"]:
        try:
            interval_min = round(float(ci["data"][0]["settlement_period"]) * 60)
        except (KeyError, TypeError, ValueError, IndexError):
            pass
    oi_usd = vol_usd = oi_raw = None
    oid, _ = _get(f"{base}/swap_open_interest?contract_code={code}")
    if isinstance(oid, dict) and oid.get("status") == "ok" and isinstance(oid.get("data"), list) and oid["data"]:
        row = oid["data"][0]
        try:
            oi_usd = float(row["value"])
        except (KeyError, TypeError, ValueError):
            pass
        try:
            if row.get("amount") not in (None, ""):
                oi_raw = float(row["amount"])   # coin units
        except (TypeError, ValueError):
            pass
        try:
            if row.get("trade_turnover") not in (None, ""):
                vol_usd = float(row["trade_turnover"])
        except (TypeError, ValueError):
            pass
    return _ok(funding_raw * 100, interval_min, oi_usd, vol_usd, False, oi_raw=oi_raw)


def probe_bingx(ticker):
    """BingX — verify-first per SPEC-176 req 4 (zero probes in R1; live-verified keyless
    2026-08-31: premiumIndex/openInterest/ticker all keyless, structured not-listed via
    code 109425). openInterest and ticker.quoteVolume are already USD, no conversion."""
    sym = f"{ticker}-USDT"
    base = "https://open-api.bingx.com/openApi/swap/v2/quote"
    pi, err = _get(f"{base}/premiumIndex?symbol={sym}")
    if err == "error":
        return _error("premiumIndex fetch failed")
    if isinstance(pi, dict) and pi.get("code") not in (0, None):
        if pi.get("code") == 109425:
            return _not_listed()
        return _error(f"bingx code {pi.get('code')}: {pi.get('msg')}")
    if err is not None or not isinstance(pi, dict) or not isinstance(pi.get("data"), dict):
        return _error(f"premiumIndex malformed (http {err})")
    row = pi["data"]
    try:
        funding_raw = float(row["lastFundingRate"])
        interval_min = round(float(row["fundingIntervalHours"]) * 60)
    except (KeyError, TypeError, ValueError):
        return _error("premiumIndex unparseable")
    cap = None
    try:
        if row.get("maxFundingRate") not in (None, ""):
            cap = float(row["maxFundingRate"])
    except (TypeError, ValueError):
        pass
    oi_usd = None
    oid, _ = _get(f"{base}/openInterest?symbol={sym}")
    if isinstance(oid, dict) and oid.get("code") == 0 and isinstance(oid.get("data"), dict):
        try:
            oi_usd = float(oid["data"]["openInterest"])
        except (KeyError, TypeError, ValueError):
            pass
    vol_usd = None
    tick, _ = _get(f"{base}/ticker?symbol={sym}")
    if isinstance(tick, dict) and tick.get("code") == 0 and isinstance(tick.get("data"), dict):
        try:
            if tick["data"].get("quoteVolume") not in (None, ""):
                vol_usd = float(tick["data"]["quoteVolume"])
        except (TypeError, ValueError):
            pass
    is_floor = cap is not None and abs(abs(funding_raw) - cap) < 1e-9
    return _ok(funding_raw * 100, interval_min, oi_usd, vol_usd, is_floor)


VENUES = {
    "binance": probe_binance,
    "bybit": probe_bybit,
    "aster": probe_aster,
    "bitget": probe_bitget,
    "hyperliquid": probe_hyperliquid,
    "lighter": probe_lighter,
    "paradex": probe_paradex,
    "extended": probe_extended,
    "vest": probe_vest,
    "gate": probe_gate,
    "mexc": probe_mexc,
    "kucoin": probe_kucoin,
    "okx": probe_okx,
    "htx": probe_htx,
    "bingx": probe_bingx,
}


def _safe_probe(fn, ticker):
    """One venue's crash/malformed return must never take down the sweep (SPEC-127 lesson)."""
    try:
        r = fn(ticker)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))
    if not isinstance(r, dict) or r.get("status") not in ("ok", "not_listed", "error"):
        return _error("malformed probe result")
    return r


def build_venue_map(ticker, venues=None, per_venue_timeout=VENUE_TIMEOUT):
    """Pure(ish) compute — fans `venues` (default VENUES) out concurrently, one thread per
    venue, each bounded by `per_venue_timeout`. `venues` is injectable for tests/reuse."""
    ticker = ticker.upper().replace("USDT", "")
    probes = venues if venues is not None else VENUES
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, len(probes))) as ex:
        futs = {name: ex.submit(_safe_probe, fn, ticker) for name, fn in probes.items()}
        for name, f in futs.items():
            try:
                results[name] = f.result(timeout=per_venue_timeout)
            except FuturesTimeout:
                results[name] = _error(f"timeout >{per_venue_timeout}s")
            except Exception as e:  # noqa: BLE001
                results[name] = _error(str(e))
    return _compose(ticker, results)


def _compose(ticker, results):
    ok = {v: r for v, r in results.items() if r.get("status") == "ok"}
    oi_by_venue = {v: r["oi_usd"] for v, r in ok.items() if r.get("oi_usd") is not None}
    total_oi = round(sum(oi_by_venue.values()), 2) if oi_by_venue else 0.0
    vol_by_venue = {v: r["vol24h_usd"] for v, r in ok.items() if r.get("vol24h_usd") is not None}
    total_vol24h = round(sum(vol_by_venue.values()), 2) if vol_by_venue else 0.0

    venues_out = {}
    for v, r in results.items():
        block = dict(r)
        if v in oi_by_venue and total_oi > 0:
            block["oi_share_pct"] = round(oi_by_venue[v] / total_oi * 100, 2)
        venues_out[v] = block

    top_oi_venue = max(oi_by_venue, key=oi_by_venue.get) if oi_by_venue else None
    oi_top_share_pct = (round(oi_by_venue[top_oi_venue] / total_oi * 100, 2)
                        if top_oi_venue and total_oi > 0 else None)

    # SPEC-108 convention: the most-extreme (max |value|) NON-floor print, already
    # 4h-normalized by the adapter (SPEC-112) — never compares raw prints across intervals.
    extreme_candidates = {v: r["funding_pi_4h"] for v, r in ok.items()
                          if r.get("funding_pi_4h") is not None and not r.get("is_floor")}
    funding_extreme = None
    if extreme_candidates:
        ev = max(extreme_candidates, key=lambda k: abs(extreme_candidates[k]))
        funding_extreme = {"venue": ev, "pi_4h": extreme_candidates[ev]}

    return {
        "ticker": ticker,
        "venues": venues_out,
        "n_listed": len(ok),
        "total_oi_usd": total_oi,
        "total_vol24h_usd": total_vol24h,
        "top_oi_venue": top_oi_venue,
        "oi_top_share_pct": oi_top_share_pct,
        "funding_extreme": funding_extreme,
    }


def render_human(r):
    print(f"# {r['ticker']} — venue coverage ({r['n_listed']} listed)\n")
    for v, b in sorted(r["venues"].items()):
        if b["status"] == "ok":
            oi = f"${b['oi_usd']:,.0f} ({b.get('oi_share_pct', 0):.1f}%)" if b.get("oi_usd") is not None else "—"
            floor = " [FLOOR]" if b.get("is_floor") else ""
            print(f"- {v}: {b['funding_pi_4h']:+.4f}%/4h (raw {b['funding_raw_pi']:+.4f}%/"
                 f"{b['interval_min']}m){floor} oi={oi}")
        elif b["status"] == "not_listed":
            print(f"- {v}: not listed")
        else:
            print(f"- {v}: ERROR ({b.get('reason', '?')})")
    if r["top_oi_venue"]:
        print(f"\nTotal OI: ${r['total_oi_usd']:,.0f} — top venue {r['top_oi_venue']} "
             f"({r['oi_top_share_pct']}%)")
    else:
        print("\nTotal OI: $0 (no venue reported OI)")
    if r["funding_extreme"]:
        fe = r["funding_extreme"]
        print(f"Funding extreme: {fe['venue']} {fe['pi_4h']:+.4f}%/4h")
    else:
        print("Funding extreme: none (all floored / unavailable)")


def main():
    ap = argparse.ArgumentParser(description="Full cross-venue coverage map for one perp (§0.6.3b)")
    ap.add_argument("ticker")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)
    r = build_venue_map(args.ticker)
    if args.json:
        print(json.dumps(r))
    else:
        render_human(r)


if __name__ == "__main__":
    main()
