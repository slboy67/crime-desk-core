#!/usr/bin/env python3
# PROTOTYPE — throwaway, not desk machinery. Answers ONE question:
# "What should the actual-liquidity (resting clusters) view look like for a board ticker?"
# Three radically different variants on one route, switchable via ?variant= + floating bar:
#   A "Ladder"  — price-axis-first: per-venue ladders side by side, armed levels as rules
#   B "Map"     — chart-first: merged %-from-mid staircase, venues overlaid, levels as markers
#   C "Verdict" — text-first: the engine's distilled per-venue reads + level-distance strip
# Run: python3 ops/prototype_depth_viewer.py  → http://localhost:8787/?ticker=TAKE&variant=A
# Read-only: shells to `orchestrator.py depth` and reads config/watchlist.json. No writes.
import json
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent
PORT = 8787


def get_depth(ticker):
    out = subprocess.run(
        ["python3", str(ROOT / "orchestrator.py"), "depth", json.dumps({"ticker": ticker})],
        capture_output=True, text=True, timeout=90, cwd=ROOT)
    d = json.loads(out.stdout)
    return d.get("data", d)


def get_rawbook(ticker):
    # full Binance perp book (raw ladder) — the true staircase, vs the engine's distilled read
    url = f"https://fapi.binance.com/fapi/v1/depth?symbol={ticker.upper()}USDT&limit=1000"
    with urllib.request.urlopen(url, timeout=15) as r:
        d = json.loads(r.read())
    return {"bids": [[float(p), float(q)] for p, q in d.get("bids", [])],
            "asks": [[float(p), float(q)] for p, q in d.get("asks", [])]}


def get_levels(ticker):
    wl = json.loads((ROOT / "config" / "watchlist.json").read_text())
    for r in wl.get("tokens", []):
        if r.get("ticker") == ticker.upper():
            th = r.get("thesis") or {}
            return {
                "watch_level": th.get("watch_level") or [],
                "stop": th.get("stop"), "entry_zone": th.get("entry_zone"),
                "tp": th.get("tp") or [], "direction": th.get("direction"),
            }
    return {"watch_level": [], "stop": None, "entry_zone": None, "tp": [], "direction": None}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/data":
            tk = (q.get("ticker") or ["TAKE"])[0].upper()
            try:
                raw = None
                try:
                    raw = get_rawbook(tk)
                except Exception:
                    pass
                body = json.dumps({"depth": get_depth(tk), "levels": get_levels(tk),
                                   "raw": raw, "ticker": tk})
                self._send(200, body, "application/json")
            except Exception as ex:
                self._send(500, json.dumps({"error": str(ex)}), "application/json")
        else:
            self._send(200, PAGE, "text/html")

    def _send(self, code, body, ctype):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>PROTOTYPE depth viewer</title>
<style>
  body{margin:0;font:13px/1.45 -apple-system,Menlo,monospace;background:#0d1017;color:#cdd3e0}
  .top{display:flex;gap:10px;align-items:center;padding:10px 16px;border-bottom:1px solid #232a38;position:sticky;top:0;background:#0d1017}
  input{background:#161c28;border:1px solid #2a3446;color:#e6ebf5;padding:5px 9px;border-radius:6px;width:90px;font:inherit;text-transform:uppercase}
  button{background:#1d2635;border:1px solid #2a3446;color:#cdd3e0;padding:5px 10px;border-radius:6px;cursor:pointer;font:inherit}
  .tag{font-size:11px;color:#7d8798}.bid{color:#4cc38a}.ask{color:#e5484d}.warn{color:#f0b429}
  .wrap{padding:16px;max-width:1200px;margin:0 auto}
  .bar{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);display:flex;gap:10px;align-items:center;background:#e6ebf5;color:#0d1017;padding:7px 14px;border-radius:999px;box-shadow:0 4px 18px #0009;font-weight:600;z-index:9}
  .bar button{background:none;border:none;color:#0d1017;font-size:15px;cursor:pointer;padding:0 4px}
  .cols{display:flex;gap:14px;align-items:flex-start}
  .venue{flex:1;background:#131926;border:1px solid #232a38;border-radius:10px;padding:10px}
  .row{display:flex;align-items:center;gap:6px;height:20px}
  .sz{height:12px;border-radius:2px;min-width:2px}
  .lvl{border-top:1px dashed #f0b429;position:relative;margin:2px 0}
  .lvl span{position:absolute;right:0;top:-9px;font-size:10px;color:#f0b429;background:#131926;padding:0 4px}
  svg{width:100%;background:#131926;border:1px solid #232a38;border-radius:10px}
  .card{background:#131926;border:1px solid #232a38;border-radius:10px;padding:12px;margin-bottom:12px}
  .strip{position:relative;height:64px;background:#161c28;border-radius:8px;margin-top:8px}
  .strip .m{position:absolute;top:0;bottom:0;width:2px;background:#e6ebf5}
  .strip .p{position:absolute;top:6px;font-size:10px;transform:translateX(-50%)}
  .kv{display:flex;justify-content:space-between;padding:2px 0}
  h3{margin:4px 0 8px;font-size:13px} h2{margin:0 0 4px;font-size:15px}
</style></head><body>
<div class="top">
  <b>PROTOTYPE · depth viewer</b>
  <input id="tk" value="TAKE"><button onclick="load()">load</button>
  <span class="tag" id="status">…</span>
</div>
<div class="wrap" id="app"></div>
<div class="bar"><button onclick="cycle(-1)">←</button><span id="vlabel"></span><button onclick="cycle(1)">→</button></div>
<script>
const VARIANTS=[["A","Ladder — price-axis per venue"],["B","Map — merged %-from-mid staircase"],["C","Verdict — engine reads + level strip"]];
let DATA=null;
const P=new URLSearchParams(location.search);
let cur=P.get("variant")||"A";
document.getElementById("tk").value=(P.get("ticker")||"TAKE").toUpperCase();
function setUrl(){P.set("variant",cur);P.set("ticker",document.getElementById("tk").value.toUpperCase());history.replaceState(null,"","?"+P)}
function cycle(d){const i=VARIANTS.findIndex(v=>v[0]===cur);cur=VARIANTS[(i+d+VARIANTS.length)%VARIANTS.length][0];setUrl();render()}
addEventListener("keydown",e=>{if(["INPUT","TEXTAREA"].includes(document.activeElement.tagName))return;if(e.key==="ArrowLeft")cycle(-1);if(e.key==="ArrowRight")cycle(1)});
async function load(){setUrl();document.getElementById("status").textContent="pulling live books…";
  const r=await fetch("/api/data?ticker="+document.getElementById("tk").value.toUpperCase());DATA=await r.json();
  document.getElementById("status").textContent=DATA.error?("error: "+DATA.error):"live";render()}
function venues(){const v=DATA?.depth?.venues||{};return Object.entries(v).filter(([,x])=>x&&x.available)}
function allLevels(){const L=DATA?.levels||{};const out=[];(L.watch_level||[]).forEach(w=>out.push({p:w.price,label:"WATCH "+(w.dir||""),cls:"warn"}));
  if(L.stop)out.push({p:L.stop,label:"STOP",cls:"ask"});(L.tp||[]).forEach((t,i)=>out.push({p:t,label:"TP"+(i+1),cls:"bid"}));
  if(Array.isArray(L.entry_zone))L.entry_zone.forEach(e=>out.push({p:e,label:"ENTRY",cls:"bid"}));return out.filter(x=>x.p)}
function fmt(x){return x==null?"—":(+x).toPrecision(5)}
function usd(x){return x==null?"—":"$"+Math.round(x).toLocaleString()}

function render(){
  document.getElementById("vlabel").textContent=cur+" — "+VARIANTS.find(v=>v[0]===cur)[1];
  const app=document.getElementById("app");
  if(!DATA){app.innerHTML="<p class='tag'>load a ticker…</p>";return}
  if(cur==="A")app.innerHTML=A();else if(cur==="B")app.innerHTML=B();else app.innerHTML=C();
}

// ── Variant A: per-venue price ladders, armed levels as dashed rules ──
function A(){
  const lv=allLevels();
  return "<div class='cols'>"+venues().map(([name,v])=>{
    const rows=[];
    const items=[["ask",v.ask_wall_above],["mid",{price:v.mid}],["bid",v.bid_shelf_below]];
    const maxN=Math.max(v.ask_wall_above?.notional_usd||0,v.bid_shelf_below?.notional_usd||0,1);
    let html="<div class='venue'><h2>"+name+"</h2><div class='tag'>mid "+fmt(v.mid)+" · spread "+(v.spread_pct??"—")+"%</div>";
    const pts=[["ASK WALL",v.ask_wall_above,"ask"],["","MID","mid"],["BID SHELF",v.bid_shelf_below,"bid"]];
    html+="<div style='margin-top:8px'>";
    // asks then levels then bids, ordered by price desc
    const entries=[];
    if(v.ask_wall_above)entries.push({p:v.ask_wall_above.price,n:v.ask_wall_above.notional_usd,cls:"ask",tag:(v.ask_wall_above.spoof_prone?"⚠spoof ":"")+v.ask_wall_above.levels+"lv "+v.ask_wall_above.dist_pct+"%"});
    entries.push({p:v.mid,n:0,cls:"",tag:"mid"});
    if(v.bid_shelf_below)entries.push({p:v.bid_shelf_below.price,n:v.bid_shelf_below.notional_usd,cls:"bid",tag:(v.bid_shelf_below.spoof_prone?"⚠spoof ":"")+v.bid_shelf_below.levels+"lv "+v.bid_shelf_below.dist_pct+"%"});
    lv.forEach(L=>entries.push({p:L.p,n:0,cls:L.cls,tag:L.label,isLvl:true}));
    entries.sort((a,b)=>b.p-a.p);
    entries.forEach(e=>{
      if(e.isLvl){html+="<div class='lvl'><span>"+e.tag+" "+fmt(e.p)+"</span></div>";return}
      const w=e.n?Math.max(4,120*e.n/maxN):0;
      html+="<div class='row'><span style='width:78px' class='"+e.cls+"'>"+fmt(e.p)+"</span>"+(e.n?"<div class='sz "+e.cls+"' style='background:currentColor;width:"+w+"px'></div><span class='tag'>"+usd(e.n)+" · "+e.tag+"</span>":"<span class='tag'>"+e.tag+"</span>")+"</div>";
    });
    html+="</div>";
    if(v.truncation_note)html+="<div class='tag warn' style='margin-top:6px'>⚠ "+v.truncation_note+"</div>";
    return html+"</div>";}).join("")+"</div>";
}

// ── Variant B: TRUE cumulative staircase from the raw Binance ladder ──
function B(){
  const lv=allLevels();const raw=DATA.raw;
  const vs=venues();const mid=vs.length?vs[0][1].mid:(raw?(raw.bids[0][0]+raw.asks[0][0])/2:1);
  if(!raw)return "<p class='tag warn'>raw Binance ladder unavailable for this ticker — flip to A/C for the engine read.</p>";
  const W=1100,Hh=440,cx=W/2,span=0.12;
  const x=p=>cx+((p-mid)/mid)/span*(W/2-30);
  // cumulative $ walking away from mid, clipped to ±span
  function stair(levels,side){
    let cum=0;const pts=[];
    for(const [p,qty] of levels){
      if(Math.abs((p-mid)/mid)>span)break;
      cum+=p*qty;pts.push([p,cum]);
    }
    return pts;
  }
  const bids=stair(raw.bids),asks=stair(raw.asks);
  const maxC=Math.max(bids.at(-1)?.[1]||1,asks.at(-1)?.[1]||1);
  const y=c=>Hh-46-(c/maxC)*(Hh-110);
  function path(pts){
    if(!pts.length)return "";
    let d=`M ${x(pts[0][0])} ${y(0)}`;let prevY=y(0);
    for(const [p,c] of pts){d+=` L ${x(p)} ${prevY} L ${x(p)} ${y(c)}`;prevY=y(c);}
    return d;
  }
  let lvl="";
  lv.forEach(L=>{if(Math.abs((L.p-mid)/mid)>span)return;const X=x(L.p);
    lvl+=`<line x1='${X}' y1='22' x2='${X}' y2='${Hh-40}' stroke='#f0b429' stroke-dasharray='5,4'/><text x='${X}' y='16' fill='#f0b429' font-size='10' text-anchor='middle'>${L.label} ${fmt(L.p)}</text>`});
  const bTot=bids.at(-1)?.[1]||0,aTot=asks.at(-1)?.[1]||0;
  return `<svg viewBox='0 0 ${W} ${Hh}'>
    <path d='${path(bids)}' fill='none' stroke='#4cc38a' stroke-width='2'/>
    <path d='${path(asks)}' fill='none' stroke='#e5484d' stroke-width='2'/>
    <line x1='${cx}' y1='22' x2='${cx}' y2='${Hh-34}' stroke='#e6ebf5'/>
    <text x='${cx}' y='${Hh-16}' fill='#e6ebf5' font-size='11' text-anchor='middle'>mid ${fmt(mid)}</text>
    <text x='30' y='${Hh-16}' fill='#4cc38a' font-size='11'>← bids · cum ${usd(bTot)} within −${span*100}%</text>
    <text x='${W-30}' y='${Hh-16}' fill='#e5484d' font-size='11' text-anchor='end'>asks · cum ${usd(aTot)} within +${span*100}% →</text>
    ${lvl}
  </svg>
  <p class='tag'>TRUE staircase (raw Binance perp ladder, every resting order): height = cumulative $ walking away from mid.
  A tall vertical jump = a wall/shelf AT that price. A long flat run = AIR (price travels fast there). Dashed gold = your armed levels.</p>`;
}

// ── Variant C: engine verdict cards + level-distance strip, no chart ──
function C(){
  const vs=venues();const lv=allLevels();const mid=vs.length?vs[0][1].mid:1;
  let cards=vs.map(([name,v])=>{
    const rows=[["mid",fmt(v.mid)],["spread",(v.spread_pct??"—")+"%"],
      ["bid shelf",v.bid_shelf_below?fmt(v.bid_shelf_below.price)+" · "+usd(v.bid_shelf_below.notional_usd)+" · "+v.bid_shelf_below.dist_pct+"%"+(v.bid_shelf_below.spoof_prone?" ⚠spoof":""):"—"],
      ["ask wall",v.ask_wall_above?fmt(v.ask_wall_above.price)+" · "+usd(v.ask_wall_above.notional_usd)+" · +"+v.ask_wall_above.dist_pct+"%"+(v.ask_wall_above.spoof_prone?" ⚠spoof":""):"—"]];
    return "<div class='card'><h2>"+name+"</h2>"+rows.map(([k,val])=>"<div class='kv'><span class='tag'>"+k+"</span><span>"+val+"</span></div>").join("")+
      (v.truncation_note?"<div class='tag warn'>⚠ "+v.truncation_note+"</div>":"")+"</div>";}).join("");
  // distance strip: mid at center, levels + shelves plotted by % distance
  const span=0.12;const pos=p=>50+((p-mid)/mid)/span*48;
  let marks="";
  vs.forEach(([name,v])=>{[v.bid_shelf_below,v.ask_wall_above].forEach(s=>{if(!s)return;const pct=pos(s.price);if(pct<0||pct>100)return;
    marks+="<div class='p "+(s.side==="bid"?"bid":"ask")+"' style='left:"+pct+"%;top:34px'>"+name[0]+"·"+usd(s.notional_usd)+"</div>"})});
  lv.forEach(L=>{const pct=pos(L.p);if(pct<0||pct>100)return;marks+="<div class='p warn' style='left:"+pct+"%'>"+L.label+"</div>"});
  return "<div class='card'><h3>where your armed levels sit vs real money (±12% of mid)</h3><div class='strip'><div class='m' style='left:50%'></div>"+marks+"</div>"+
    "<div class='tag' style='margin-top:6px'>top row = your levels · bottom row = venue shelves/walls (first letter = venue)</div></div>"+cards;
}
load();
</script></body></html>"""

if __name__ == "__main__":
    print(f"PROTOTYPE depth viewer → http://localhost:{PORT}/?ticker=TAKE&variant=A  (Ctrl-C to stop)")
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
