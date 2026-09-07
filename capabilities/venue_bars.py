#!/usr/bin/env python3
"""venue_bars.py — all-venue 1h OHLC sweep + cross-venue dispersion (SPEC-188).

The user has twice told the desk (2026-09-01, 2026-09-02) to check EVERY venue before
making a structure statement (lower-high, stall, breakdown-hold, sweep, level-crossed) —
both times the orchestrator read one or two venues (usually Bybit/Binance) by hand and
got it wrong (OP 2026-09-02 13:00Z: Bybit read a "stall under the prior high", the
14-venue sweep showed a higher high on 11/14). This makes the sweep a deterministic
engine layer `brief` prints every time, so hand-discipline is no longer load-bearing.

  python3 capabilities/venue_bars.py OP --interval 1h --n 6 --json

Fourteen venues, keyless, fanned out CONCURRENTLY (one thread per venue, `VENUE_TIMEOUT`
each, whole sweep bounded by the caller): Binance, Bybit, OKX, Bitget, KuCoin, Gate,
MEXC, HTX, BingX, Hyperliquid, Aster, Kraken Futures, BloFin, Coinbase INTX. Endpoints +
symbol conventions verified live 2026-09-02 (see the spec's prototype). A venue that
fails (HTTP error / timeout / malformed body) returns `available:false, reason:<...>` —
loud, never dropped (§3 zero-print rule) — exactly the `venue_map.py` (SPEC-129)
adapter-isolation discipline, reused here for bars instead of funding/OI.

`turnover_24h_usd` is fetched best-effort via each venue's own 24h-ticker endpoint
(reusing the parsing already proven in `venue_map.py`'s probes where the same field is
read there) — four venues (Gate/Kraken/BloFin/Coinbase) have no cheap 24h-USD field
verified live and are left `None` rather than guessed; `dominant_tape` is computed over
whichever venues DID report one.

Only `interval="1h"` is verified against every venue's own granularity convention (the
spec's worked example and DoD are all 1h); other intervals are passed through per-venue
best-effort and not guaranteed correct.
"""
import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import colors as C

TIMEOUT = 8                  # per-HTTP-call socket timeout (spec: per-venue budget <=8s)
VENUE_TIMEOUT = 8            # per-venue overall probe budget
UA = {"User-Agent": "venue-bars/1.0"}
INTERVAL_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}
EXECUTION_VENUE = "aster"    # §7: fills happen on Aster


def _get(url, data=None, hdr=None, timeout=TIMEOUT):
    """GET (or POST when `data` is given) -> (body, err). err is None on 2xx; an HTTP
    status int on HTTPError; the string "error" for anything else (timeout / connection
    failure / non-JSON body). Mirrors venue_map._get's not_listed/error split, extended
    with an optional POST body for Hyperliquid's single `/info` endpoint."""
    try:
        req = urllib.request.Request(url, data=data, headers=hdr or UA)
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


def _bar(ts, o, h, l, c, quote_vol=None):
    return {"ts": int(ts), "o": float(o), "h": float(h), "l": float(l), "c": float(c),
            "quote_vol": (float(quote_vol) if quote_vol is not None else None)}


def _ok(bars, turnover_24h_usd=None):
    return {"available": True, "bars": bars, "turnover_24h_usd": turnover_24h_usd}


def _error(reason):
    return {"available": False, "reason": str(reason)[:160]}


# ── venue adapters — probe_<venue>(ticker, interval, n) -> {available, bars[, reason]} ──

def probe_binance(ticker, interval="1h", n=6):
    sym = f"{ticker}USDT"
    k, err = _get(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval={interval}&limit={n+1}")
    if err == "error":
        return _error("klines fetch failed")
    if err is not None or not isinstance(k, list):
        return _error(f"klines malformed (http {err})")
    try:
        bars = [_bar(x[0] // 1000, x[1], x[2], x[3], x[4], x[7]) for x in k]
    except (IndexError, TypeError, ValueError):
        return _error("klines unparseable")
    turnover = None
    t24, _ = _get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={sym}")
    if isinstance(t24, dict) and t24.get("quoteVolume") is not None:
        try:
            turnover = float(t24["quoteVolume"])
        except (TypeError, ValueError):
            pass
    return _ok(bars, turnover)


def probe_bybit(ticker, interval="1h", n=6):
    sym = f"{ticker}USDT"
    iv = {"1h": "60", "4h": "240", "1d": "D"}.get(interval, "60")
    d, err = _get(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval={iv}&limit={n+1}")
    if err == "error":
        return _error("kline fetch failed")
    if err is not None or not isinstance(d, dict) or d.get("retCode") != 0:
        return _error(f"kline http {err} / retCode {(d or {}).get('retCode')}")
    lst = (d.get("result") or {}).get("list") or []
    try:
        bars = [_bar(int(x[0]) // 1000, x[1], x[2], x[3], x[4], x[6]) for x in reversed(lst)]
    except (IndexError, TypeError, ValueError):
        return _error("kline unparseable")
    turnover = None
    tick, _ = _get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}")
    if isinstance(tick, dict) and tick.get("retCode") == 0:
        rows = ((tick.get("result") or {}).get("list") or [])
        if rows and rows[0].get("turnover24h") not in (None, ""):
            try:
                turnover = float(rows[0]["turnover24h"])
            except (TypeError, ValueError):
                pass
    return _ok(bars, turnover)


def probe_okx(ticker, interval="1h", n=6):
    inst = f"{ticker}-USDT-SWAP"
    bar = {"1h": "1H", "4h": "4H", "1d": "1D"}.get(interval, "1H")
    d, err = _get(f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar={bar}&limit={n+1}")
    if err == "error":
        return _error("candles fetch failed")
    if err is not None or not isinstance(d, dict) or d.get("code") != "0":
        return _error(f"candles http {err} / code {(d or {}).get('code')}")
    rows = d.get("data") or []
    try:
        bars = [_bar(int(x[0]) // 1000, x[1], x[2], x[3], x[4], x[7]) for x in reversed(rows)]
    except (IndexError, TypeError, ValueError):
        return _error("candles unparseable")
    turnover = None
    tick, _ = _get(f"https://www.okx.com/api/v5/market/ticker?instId={inst}")
    if isinstance(tick, dict) and tick.get("code") == "0" and tick.get("data"):
        try:
            t = tick["data"][0]
            last = float(t["last"])
            vol_ccy = float(t["volCcy24h"]) if t.get("volCcy24h") not in (None, "") else None
            if vol_ccy is not None:
                turnover = vol_ccy * last
        except (KeyError, TypeError, ValueError, IndexError):
            pass
    return _ok(bars, turnover)


def probe_bitget(ticker, interval="1h", n=6):
    sym = f"{ticker}USDT"
    gran = {"1h": "1H", "4h": "4H", "1d": "1D"}.get(interval, "1H")
    d, err = _get(f"https://api.bitget.com/api/v2/mix/market/candles?symbol={sym}&productType=USDT-FUTURES&granularity={gran}&limit={n+1}")
    if err == "error":
        return _error("candles fetch failed")
    if err is not None or not isinstance(d, dict) or d.get("code") != "00000":
        return _error(f"candles http {err} / code {(d or {}).get('code')}")
    rows = d.get("data") or []
    try:
        bars = [_bar(int(x[0]) // 1000, x[1], x[2], x[3], x[4], x[6]) for x in rows]
    except (IndexError, TypeError, ValueError):
        return _error("candles unparseable")
    turnover = None
    tick, _ = _get(f"https://api.bitget.com/api/v2/mix/market/ticker?symbol={sym}&productType=usdt-futures")
    if isinstance(tick, dict) and tick.get("code") == "00000" and tick.get("data"):
        try:
            t = tick["data"][0]
            if t.get("usdtVolume") not in (None, ""):
                turnover = float(t["usdtVolume"])
        except (KeyError, TypeError, ValueError, IndexError):
            pass
    return _ok(bars, turnover)


def probe_kucoin(ticker, interval="1h", n=6):
    sym = f"{ticker}USDTM"
    gran = {"1h": 60, "4h": 240, "1d": 1440}.get(interval, 60)
    step = INTERVAL_SECONDS.get(interval, 3600)
    since_ms = (int(time.time()) - (n + 2) * step) * 1000
    d, err = _get(f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={sym}&granularity={gran}&from={since_ms}")
    if err == "error":
        return _error("kline fetch failed")
    if err is not None or not isinstance(d, dict) or d.get("code") != "200000":
        return _error(f"kline http {err} / code {(d or {}).get('code')}")
    rows = d.get("data") or []
    try:
        bars = [_bar(int(x[0]) // 1000, x[1], x[2], x[3], x[4], x[6]) for x in rows][-(n + 1):]
    except (IndexError, TypeError, ValueError):
        return _error("kline unparseable")
    turnover = None
    c, _ = _get(f"https://api-futures.kucoin.com/api/v1/contracts/{sym}")
    if isinstance(c, dict) and c.get("code") == "200000" and isinstance(c.get("data"), dict):
        try:
            if c["data"].get("turnoverOf24h") not in (None, ""):
                turnover = float(c["data"]["turnoverOf24h"])
        except (TypeError, ValueError):
            pass
    return _ok(bars, turnover)


def probe_gate(ticker, interval="1h", n=6):
    sym = f"{ticker}_USDT"
    d, err = _get(f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract={sym}&interval={interval}&limit={n+1}")
    if err == "error":
        return _error("candlesticks fetch failed")
    if isinstance(d, dict) and d.get("label"):
        return _error(f"gate {d.get('label')}")
    if err is not None or not isinstance(d, list):
        return _error(f"candlesticks malformed (http {err})")
    try:
        bars = [_bar(x["t"], x["o"], x["h"], x["l"], x["c"], x.get("sum")) for x in d]
    except (KeyError, TypeError, ValueError):
        return _error("candlesticks unparseable")
    # SPEC-176: Gate has no verified cheap 24h-USD field on either the contracts or
    # contract_stats endpoint — left None rather than guessed (matches venue_map's probe_gate).
    return _ok(bars, None)


def probe_mexc(ticker, interval="1h", n=6):
    sym = f"{ticker}_USDT"
    iv = {"1h": "Min60", "4h": "Min240", "1d": "Day1"}.get(interval, "Min60")
    step = INTERVAL_SECONDS.get(interval, 3600)
    now = int(time.time())
    d, err = _get(f"https://contract.mexc.com/api/v1/contract/kline/{sym}?interval={iv}&start={now-(n+2)*step}&end={now}")
    if err == "error":
        return _error("kline fetch failed")
    if isinstance(d, dict) and d.get("success") is False:
        return _error(f"mexc code {d.get('code')}: {d.get('message')}")
    if err is not None or not isinstance(d, dict) or not isinstance(d.get("data"), dict):
        return _error(f"kline malformed (http {err})")
    row = d["data"]
    try:
        times = row["time"]
        bars = [_bar(times[i], row["open"][i], row["high"][i], row["low"][i], row["close"][i],
                     (row.get("amount") or [None] * len(times))[i]) for i in range(len(times))][-(n + 1):]
    except (KeyError, IndexError, TypeError, ValueError):
        return _error("kline unparseable")
    turnover = None
    t, _ = _get(f"https://contract.mexc.com/api/v1/contract/ticker?symbol={sym}")
    if isinstance(t, dict) and t.get("success") and isinstance(t.get("data"), dict):
        try:
            if t["data"].get("amount24") not in (None, ""):
                turnover = float(t["data"]["amount24"])
        except (TypeError, ValueError):
            pass
    return _ok(bars, turnover)


def probe_htx(ticker, interval="1h", n=6):
    code = f"{ticker}-USDT"
    period = {"1h": "60min", "4h": "4hour", "1d": "1day"}.get(interval, "60min")
    d, err = _get(f"https://api.hbdm.com/linear-swap-ex/market/history/kline?contract_code={code}&period={period}&size={n+1}")
    if err == "error":
        return _error("history/kline fetch failed")
    if isinstance(d, dict) and d.get("status") == "error":
        return _error(f"htx err {d.get('err_code')}: {d.get('err_msg')}")
    if err is not None or not isinstance(d, dict) or not isinstance(d.get("data"), list):
        return _error(f"history/kline malformed (http {err})")
    try:
        bars = [_bar(x["id"], x["open"], x["high"], x["low"], x["close"], x.get("trade_turnover"))
                for x in reversed(d["data"])]
    except (KeyError, TypeError, ValueError):
        return _error("history/kline unparseable")
    turnover = None
    oid, _ = _get(f"https://api.hbdm.com/linear-swap-api/v1/swap_open_interest?contract_code={code}")
    if isinstance(oid, dict) and oid.get("status") == "ok" and isinstance(oid.get("data"), list) and oid["data"]:
        try:
            if oid["data"][0].get("trade_turnover") not in (None, ""):
                turnover = float(oid["data"][0]["trade_turnover"])
        except (TypeError, ValueError):
            pass
    return _ok(bars, turnover)


def probe_bingx(ticker, interval="1h", n=6):
    sym = f"{ticker}-USDT"
    d, err = _get(f"https://open-api.bingx.com/openApi/swap/v3/quote/klines?symbol={sym}&interval={interval}&limit={n+1}")
    if err == "error":
        return _error("klines fetch failed")
    if isinstance(d, dict) and d.get("code") not in (0, None):
        return _error(f"bingx code {d.get('code')}: {d.get('msg')}")
    if err is not None or not isinstance(d, dict) or not isinstance(d.get("data"), list):
        return _error(f"klines malformed (http {err})")
    try:
        bars = [_bar(x["time"] // 1000, x["open"], x["high"], x["low"], x["close"], x.get("volume"))
                for x in reversed(d["data"])]
    except (KeyError, TypeError, ValueError):
        return _error("klines unparseable")
    turnover = None
    t, _ = _get(f"https://open-api.bingx.com/openApi/swap/v2/quote/ticker?symbol={sym}")
    if isinstance(t, dict) and t.get("code") == 0 and isinstance(t.get("data"), dict):
        try:
            if t["data"].get("quoteVolume") not in (None, ""):
                turnover = float(t["data"]["quoteVolume"])
        except (TypeError, ValueError):
            pass
    return _ok(bars, turnover)


def probe_hyperliquid(ticker, interval="1h", n=6):
    step = INTERVAL_SECONDS.get(interval, 3600)
    now = int(time.time())
    body = json.dumps({"type": "candleSnapshot",
                       "req": {"coin": ticker.upper(), "interval": interval,
                              "startTime": (now - (n + 2) * step) * 1000, "endTime": now * 1000}}).encode()
    d, err = _get("https://api.hyperliquid.xyz/info", body, {"Content-Type": "application/json"})
    if err == "error":
        return _error("candleSnapshot fetch failed")
    if err is not None or not isinstance(d, list):
        return _error(f"candleSnapshot malformed (http {err})")
    if not d:
        return _error("no candles returned (unlisted or empty)")
    try:
        bars = [_bar(x["t"] // 1000, x["o"], x["h"], x["l"], x["c"], x.get("v")) for x in d][-(n + 1):]
    except (KeyError, TypeError, ValueError):
        return _error("candleSnapshot unparseable")
    turnover = None
    try:
        import hyperliquid as HL
        v = HL.resolve(ticker)
        if v and v.get("vol_m") is not None:
            turnover = float(v["vol_m"]) * 1e6
    except Exception:  # noqa: BLE001 — turnover is best-effort only
        pass
    return _ok(bars, turnover)


def probe_aster(ticker, interval="1h", n=6):
    sym = f"{ticker}USDT"
    k, err = _get(f"https://fapi.asterdex.com/fapi/v1/klines?symbol={sym}&interval={interval}&limit={n+1}")
    if err == "error":
        return _error("klines fetch failed")
    if isinstance(k, dict) and k.get("code") == -1121:
        return _error("not listed on Aster")
    if err is not None or not isinstance(k, list):
        return _error(f"klines malformed (http {err})")
    try:
        bars = [_bar(x[0] // 1000, x[1], x[2], x[3], x[4], x[7]) for x in k]
    except (IndexError, TypeError, ValueError):
        return _error("klines unparseable")
    turnover = None
    t24, _ = _get(f"https://fapi.asterdex.com/fapi/v1/ticker/24hr?symbol={sym}")
    if isinstance(t24, dict) and t24.get("quoteVolume") is not None:
        try:
            turnover = float(t24["quoteVolume"])
        except (TypeError, ValueError):
            pass
    return _ok(bars, turnover)


KRAKEN_SYMBOL_ALIASES = {"BTC": "XBT"}   # Kraken's own convention (PF_XBTUSD, never PF_BTCUSD)


def probe_kraken(ticker, interval="1h", n=6):
    step = INTERVAL_SECONDS.get(interval, 3600)
    now = int(time.time())
    kt = KRAKEN_SYMBOL_ALIASES.get(ticker.upper(), ticker.upper())
    inst = f"PF_{kt}USD"
    iv = {"1h": "1h", "4h": "4h", "1d": "1d"}.get(interval, "1h")
    d, err = _get(f"https://futures.kraken.com/api/charts/v1/trade/{inst}/{iv}?from={now-(n+2)*step}&to={now}")
    if err == "error":
        return _error("charts fetch failed")
    if err is not None or not isinstance(d, dict) or not isinstance(d.get("candles"), list):
        return _error(f"charts malformed (http {err})")
    try:
        bars = [_bar(int(x["time"]) // 1000, x["open"], x["high"], x["low"], x["close"])
                for x in d["candles"]][-(n + 1):]
    except (KeyError, TypeError, ValueError):
        return _error("charts unparseable")
    if not bars:
        return _error("no candles returned (unlisted or empty)")
    # No verified cheap 24h-USD turnover field on Kraken Futures' charts/tickers surface —
    # left None rather than guessed (candle `volume` is base-asset contract units).
    return _ok(bars, None)


def probe_blofin(ticker, interval="1h", n=6):
    inst = f"{ticker}-USDT"
    bar = {"1h": "1H", "4h": "4H", "1d": "1D"}.get(interval, "1H")
    d, err = _get(f"https://openapi.blofin.com/api/v1/market/candles?instId={inst}&bar={bar}&limit={n+1}")
    if err == "error":
        return _error("candles fetch failed")
    if err is not None or not isinstance(d, dict) or d.get("code") != "0":
        return _error(f"candles http {err} / code {(d or {}).get('code')}")
    rows = d.get("data") or []
    if not rows:
        return _error("no candles returned (unlisted or empty)")
    try:
        bars = [_bar(int(x[0]) // 1000, x[1], x[2], x[3], x[4], x[7] if len(x) > 7 else None)
                for x in reversed(rows)]
    except (IndexError, TypeError, ValueError):
        return _error("candles unparseable")
    # No verified cheap 24h-USD ticker field checked for BloFin — left None rather than guessed.
    return _ok(bars, None)


def probe_coinbase(ticker, interval="1h", n=6):
    step = INTERVAL_SECONDS.get(interval, 3600)
    now = int(time.time())
    gran = {"1h": "ONE_HOUR", "4h": "FOUR_HOUR", "1d": "ONE_DAY"}.get(interval, "ONE_HOUR")
    prod = f"{ticker.upper()}-PERP-INTX"
    d, err = _get(f"https://api.coinbase.com/api/v3/brokerage/market/products/{prod}/candles"
                 f"?granularity={gran}&start={now-(n+2)*step}&end={now}")
    if err == "error":
        return _error("candles fetch failed")
    if err is not None or not isinstance(d, dict) or not isinstance(d.get("candles"), list):
        return _error(f"candles malformed (http {err})")
    rows = d["candles"]
    if not rows:
        return _error("no candles returned (unlisted or empty)")
    try:
        bars = [_bar(int(x["start"]), x["open"], x["high"], x["low"], x["close"])
                for x in reversed(rows)][-(n + 1):]
    except (KeyError, TypeError, ValueError):
        return _error("candles unparseable")
    # candle `volume` is base-asset units, not USD — no verified 24h-USD field, left None.
    return _ok(bars, None)


VENUES = {
    "binance": probe_binance,
    "bybit": probe_bybit,
    "okx": probe_okx,
    "bitget": probe_bitget,
    "kucoin": probe_kucoin,
    "gate": probe_gate,
    "mexc": probe_mexc,
    "htx": probe_htx,
    "bingx": probe_bingx,
    "hyperliquid": probe_hyperliquid,
    "aster": probe_aster,
    "kraken": probe_kraken,
    "blofin": probe_blofin,
    "coinbase": probe_coinbase,
}


def _safe_probe(fn, ticker, interval, n):
    """One venue's crash/malformed return must never take down the sweep (venue_map's
    SPEC-127 lesson, reused here)."""
    try:
        r = fn(ticker, interval, n)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))
    if not isinstance(r, dict) or "available" not in r:
        return _error("malformed probe result")
    return r


def build_venue_bars(ticker, interval="1h", n=6, venues=None, per_venue_timeout=VENUE_TIMEOUT, now=None):
    """Fans `venues` (default VENUES) out concurrently, one thread per venue, each bounded
    by `per_venue_timeout`. `venues`/`now` are injectable for tests (offline-deterministic,
    no network on the test path — see tests/test_venue_bars.py)."""
    ticker = ticker.upper().replace("USDT", "")
    probes = venues if venues is not None else VENUES
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, len(probes))) as ex:
        futs = {name: ex.submit(_safe_probe, fn, ticker, interval, n) for name, fn in probes.items()}
        for name, f in futs.items():
            try:
                results[name] = f.result(timeout=per_venue_timeout)
            except FuturesTimeout:
                results[name] = _error(f"timeout >{per_venue_timeout}s")
            except Exception as e:  # noqa: BLE001
                results[name] = _error(str(e))
    return _compose(ticker, interval, n, results, now if now is not None else time.time())


def _compose(ticker, interval, n, results, now):
    step = INTERVAL_SECONDS.get(interval, 3600)
    live_ts = int(now // step * step)

    venues_out = {}
    for name, r in results.items():
        if r.get("available"):
            bars = sorted((r.get("bars") or []), key=lambda b: b["ts"])
            for b in bars:
                b["live"] = (b["ts"] == live_ts)
            venues_out[name] = {"venue": name, "available": True, "bars": bars,
                                "turnover_24h_usd": r.get("turnover_24h_usd")}
        else:
            venues_out[name] = {"venue": name, "available": False,
                                "reason": r.get("reason") or "unknown"}

    all_ts = sorted({b["ts"] for v in venues_out.values() if v["available"] for b in v["bars"]})
    bars_out = []
    for ts in all_ts:
        highs, lows, closes = {}, {}, {}
        for name, v in venues_out.items():
            if not v["available"]:
                continue
            for b in v["bars"]:
                if b["ts"] == ts:
                    highs[name] = b["h"]
                    lows[name] = b["l"]
                    closes[name] = b["c"]
        if not highs:
            continue
        med_h, med_l, med_c = statistics.median(highs.values()), statistics.median(lows.values()), statistics.median(closes.values())
        max_h_v = max(highs, key=highs.get)
        min_h_v = min(highs, key=highs.get)
        max_l_v = max(lows, key=lows.get)
        min_l_v = min(lows, key=lows.get)
        bars_out.append({
            "ts": ts, "live": (ts == live_ts), "n_venues": len(highs),
            "median_h": round(med_h, 8), "median_l": round(med_l, 8), "median_c": round(med_c, 8),
            "high_spread_pct": round((highs[max_h_v] - highs[min_h_v]) / med_h * 100, 4) if med_h else None,
            "low_spread_pct": round((lows[max_l_v] - lows[min_l_v]) / med_l * 100, 4) if med_l else None,
            "close_spread_pct": round((max(closes.values()) - min(closes.values())) / med_c * 100, 4) if med_c else None,
            "max_h_venue": max_h_v, "min_h_venue": min_h_v,
            "max_l_venue": max_l_v, "min_l_venue": min_l_v,
        })

    n_total = len(venues_out)
    n_available = sum(1 for v in venues_out.values() if v["available"])

    turnovers = {name: v["turnover_24h_usd"] for name, v in venues_out.items()
                if v["available"] and v.get("turnover_24h_usd") is not None}
    dominant_tape = None
    if turnovers:
        dv = max(turnovers, key=turnovers.get)
        dominant_tape = {"venue": dv, "turnover_24h_usd": round(turnovers[dv], 2)}

    return {
        "ticker": ticker, "interval": interval, "n": n,
        "venues": venues_out, "n_total": n_total, "n_available": n_available,
        "bars": bars_out, "dominant_tape": dominant_tape,
        "execution_venue": EXECUTION_VENUE,
    }


def level_agreement(result, level, direction, bar_ts=None):
    """Which venues' bar crossed `level`? `direction` = "above" (bar high >= level,
    testing a resistance/breakout) or "below" (bar low <= level, testing a
    support/breakdown). `bar_ts` defaults to the live bar. `closed_beyond` = the bar's
    CLOSE is past the level (the stronger "held" read vs a mere wick-cross).

    Returns {crossed:[...], closed_beyond:[...], n_crossed, n_total,
             execution_venue_crossed, dominant_tape_crossed}."""
    venues = result.get("venues") or {}
    if bar_ts is None:
        step = INTERVAL_SECONDS.get(result.get("interval", "1h"), 3600)
        bar_ts = int(time.time() // step * step)
    crossed, closed_beyond = [], []
    n_total = 0
    for name, v in venues.items():
        if not v.get("available"):
            continue
        n_total += 1
        bar = next((b for b in v.get("bars") or [] if b["ts"] == bar_ts), None)
        if bar is None:
            continue
        if direction == "above":
            if bar["h"] >= level:
                crossed.append(name)
            if bar["c"] >= level:
                closed_beyond.append(name)
        else:
            if bar["l"] <= level:
                crossed.append(name)
            if bar["c"] <= level:
                closed_beyond.append(name)
    dominant = (result.get("dominant_tape") or {}).get("venue")
    return {
        "crossed": sorted(crossed), "closed_beyond": sorted(closed_beyond),
        "n_crossed": len(crossed), "n_total": n_total,
        "execution_venue_crossed": EXECUTION_VENUE in crossed,
        "dominant_tape_crossed": bool(dominant) and dominant in crossed,
    }


def render_tape_line(result):
    """The compact `tape (1h, N venues) ...` line brief prints (SPEC-188 §2)."""
    if not result.get("bars"):
        return f"tape ({result.get('interval','1h')}, {result.get('n_available',0)} venues) — no bars"
    bits = []
    for b in result["bars"]:
        label = time.strftime("%H:%MZ", time.gmtime(b["ts"])) + ("*" if b["live"] else "")
        hsp = f"{b['high_spread_pct']:+.1f}%" if b.get("high_spread_pct") is not None else "?"
        lsp = f"{b['low_spread_pct']:+.1f}%" if b.get("low_spread_pct") is not None else "?"
        bits.append(f"{label} h {b['median_h']:g}±{hsp} l {b['median_l']:g}±{lsp}")
    header = f"tape ({result.get('interval','1h')}, {result.get('n_available',0)} venues) "
    return header + "  ".join(bits)


def render_human(r):
    print(f"# {r['ticker']} — venue bars ({r['n_available']}/{r['n_total']} venues, {r['interval']})\n")
    print(" ", render_tape_line(r))
    dt = r.get("dominant_tape")
    if dt:
        print(f"  dominant tape {dt['venue']} (${dt['turnover_24h_usd']:,.0f}/24h)  execution {r['execution_venue']}")
    for name, v in sorted(r["venues"].items()):
        if v["available"]:
            last = v["bars"][-1] if v["bars"] else None
            tag = " [live]" if last and last.get("live") else ""
            print(f"  - {name}: {len(v['bars'])} bars"
                 + (f", last h/l/c {last['h']:g}/{last['l']:g}/{last['c']:g}{tag}" if last else ""))
        else:
            print(f"  - {name}: unavailable ({v.get('reason', '?')})")


def main():
    ap = argparse.ArgumentParser(description="All-venue OHLC sweep + cross-venue dispersion (SPEC-188)")
    ap.add_argument("ticker")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)
    r = build_venue_bars(args.ticker, args.interval, args.n)
    if args.json:
        print(json.dumps(r))
    else:
        render_human(r)


if __name__ == "__main__":
    main()
