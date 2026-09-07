#!/usr/bin/env python3
"""All-venue 1h OHLC sweep (desk rule CLAUDE.md §3). Interim hand tool until SPEC-188 lands.
Usage: python3 venue_bars.py <TICKER> [n_bars]   -> prints h/l/c per venue for the last n hourly bars (last = live)."""
import json, time, urllib.request, sys
T = (sys.argv[1] if len(sys.argv) > 1 else "OP").upper()
N = int(sys.argv[2]) if len(sys.argv) > 2 else 3

def get(url, data=None, hdr=None):
    req = urllib.request.Request(url, data=data, headers=hdr or {"User-Agent": "desk/1"})
    return json.loads(urllib.request.urlopen(req, timeout=12).read())

now = int(time.time()); h = now // 3600 * 3600
want = [h - i * 3600 for i in range(N - 1, -1, -1)]
lim = N + 1
out = {}

def add(v, rows):  # rows: list of (ts,h,l,c)
    out[v] = {ts: (float(hh), float(ll), float(cc)) for ts, hh, ll, cc in rows if ts in want}

def run(v, fn):
    try:
        add(v, fn())
    except Exception as e:
        out[v] = f"ERR {str(e)[:60]}"

start_ms = (h - N * 3600) * 1000
run("Binance", lambda: [(x[0]//1000, x[2], x[3], x[4]) for x in get(f"https://fapi.binance.com/fapi/v1/klines?symbol={T}USDT&interval=1h&limit={lim}")])
run("Bybit", lambda: [(int(x[0])//1000, x[2], x[3], x[4]) for x in get(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={T}USDT&interval=60&limit={lim}")["result"]["list"]])
run("OKX", lambda: [(int(x[0])//1000, x[2], x[3], x[4]) for x in get(f"https://www.okx.com/api/v5/market/candles?instId={T}-USDT-SWAP&bar=1H&limit={lim}")["data"]])
run("Bitget", lambda: [(int(x[0])//1000, x[2], x[3], x[4]) for x in get(f"https://api.bitget.com/api/v2/mix/market/candles?symbol={T}USDT&productType=USDT-FUTURES&granularity=1H&limit={lim}")["data"]])
run("KuCoin", lambda: [(int(x[0])//1000, x[2], x[3], x[4]) for x in get(f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={T}USDTM&granularity=60&from={start_ms}")["data"]])
run("Gate", lambda: [(int(x["t"]), x["h"], x["l"], x["c"]) for x in get(f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract={T}_USDT&interval=1h&limit={lim}")])
def _mexc():
    k = get(f"https://contract.mexc.com/api/v1/contract/kline/{T}_USDT?interval=Min60&start={h - N*3600}&end={now}")["data"]
    return [(int(t), hh, ll, cc) for t, hh, ll, cc in zip(k["time"], k["high"], k["low"], k["close"])]
run("MEXC", _mexc)
run("HTX", lambda: [(int(x["id"]), x["high"], x["low"], x["close"]) for x in get(f"https://api.hbdm.com/linear-swap-ex/market/history/kline?contract_code={T}-USDT&period=60min&size={lim}")["data"]])
run("BingX", lambda: [(int(x["time"])//1000, x["high"], x["low"], x["close"]) for x in get(f"https://open-api.bingx.com/openApi/swap/v3/quote/klines?symbol={T}-USDT&interval=1h&limit={lim}")["data"]])
def _hl():
    body = json.dumps({"type": "candleSnapshot", "req": {"coin": T, "interval": "1h", "startTime": start_ms, "endTime": now * 1000}}).encode()
    return [(int(x["t"])//1000, x["h"], x["l"], x["c"]) for x in get("https://api.hyperliquid.xyz/info", body, {"Content-Type": "application/json"})]
run("Hyperliquid", _hl)
run("Aster", lambda: [(x[0]//1000, x[2], x[3], x[4]) for x in get(f"https://fapi.asterdex.com/fapi/v1/klines?symbol={T}USDT&interval=1h&limit={lim}")])
run("Kraken", lambda: [(int(x["time"])//1000, x["high"], x["low"], x["close"]) for x in get(f"https://futures.kraken.com/api/charts/v1/trade/PF_{T}USD/1h?from={h - N*3600}&to={now}")["candles"]])
run("BloFin", lambda: [(int(x[0])//1000, x[2], x[3], x[4]) for x in get(f"https://openapi.blofin.com/api/v1/market/candles?instId={T}-USDT&bar=1H&limit={lim}")["data"]])
run("Coinbase", lambda: [(int(x["start"]), x["high"], x["low"], x["close"]) for x in get(f"https://api.coinbase.com/api/v3/brokerage/market/products/{T}-PERP-INTX/candles?granularity=ONE_HOUR&start={h - N*3600}&end={now}")["candles"]])

lab = [time.strftime("%H:%MZ", time.gmtime(t)) + ("*" if t == h else "") for t in want]
print(f"{T} 1h across {len(out)} venues (h / l / c; * = live bar)")
print(f"{'venue':12}" + "".join(f"{l:>26}" for l in lab))
for v, r in out.items():
    if isinstance(r, str):
        print(f"{v:12} {r}"); continue
    print(f"{v:12}" + "".join((f"{r[t][0]:.5f}/{r[t][1]:.5f}/{r[t][2]:.5f}".rjust(26) if t in r else "-".rjust(26)) for t in want))
# dispersion per bar
ok = {v: r for v, r in out.items() if not isinstance(r, str)}
for t, l in zip(want, lab):
    hs = [r[t][0] for r in ok.values() if t in r]; ls = [r[t][1] for r in ok.values() if t in r]
    if len(hs) >= 2:
        mh = sorted(hs)[len(hs)//2]; ml = sorted(ls)[len(ls)//2]
        hi_v = max((v for v in ok if t in ok[v]), key=lambda v: ok[v][t][0]); lo_v = min((v for v in ok if t in ok[v]), key=lambda v: ok[v][t][0])
        print(f"{l:>8} high-spread {100*(max(hs)-min(hs))/mh:.2f}% ({hi_v} top, {lo_v} bottom) · low-spread {100*(max(ls)-min(ls))/ml:.2f}% · n={len(hs)}")
