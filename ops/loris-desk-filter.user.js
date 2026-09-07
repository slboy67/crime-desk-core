// ==UserScript==
// @name         Loris Desk Filter — crime-desk view
// @namespace    crime-desk
// @version      1.2
// @description  Crime-desk view on loris.tools: DISCOVER tab = full universe minus everything the desk doesn't trade (TradFi, majors, stables, thin books), sorted by OI build — the new-crime-coin net. DESK tab = watchlist board. Optional hiding of non-desk rows in native tables.
// @match        https://loris.tools/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

/* Watchlist seeded from config/watchlist.json 2026-07-29 (active + parked). Everything editable in the panel; persists in this browser. */
(function () {
  'use strict';

  const SEED = ['PIEVERSE','UB','EDEN','ZAMA','KITE','COLLECT','HOME','SLX','LAB','CHIP','IDOL','COAI','AIO','AIOT','CLO','DRIFT','RE','JCT','VELVET','H','BEAT','BASED','UAI','BAS','XPIN','ALLO','TAIKO','BLUR','M','Q','LYN','ID','OPG','TAG','BLUAI','0G','GWEI','OGN','US','B3','PUMP','SYN','PARTI','DEXE','AEON','RIVER','BTR','MAGMA','SKYAI'];
  // majors / stables / wrapped / L1 blue-chips the desk never trades (editable via "edit excludes")
  const EXCLUDE_SEED = ['BTC','ETH','SOL','XRP','BNB','DOGE','ADA','LINK','LTC','AVAX','DOT','TRX','SHIB','SUI','HYPE','TON','GRAM','BCH','XLM','NEAR','APT','ICP','ETC','UNI','AAVE','FIL','ARB','OP','ATOM','HBAR','KAS','XMR','ZEC','PEPE','WIF','BONK','FET','TAO','RENDER','INJ','SEI','TIA','JUP','ENA','ONDO','WLD','USDT','USDC','DAI','FDUSD','WBTC','WETH','STETH','BTCUSD','ETHUSD','XAUT','PAXG','MU','SNDK','SPY','CL','SPCX','SKHX',
  // HIP-3 stock/index perps that leak through loris' incomplete asset tagging (curate via "edit excludes")
  'SKDD','GIGADEVICE','MSFU','KR200','TMO','GDX','QXO','CSOL','VRT','TER','BZ','ZHIPU','NVDA','TSLA','AAPL','MSFT','AMZN','GOOGL','META','AMD','PLTR','COIN','HOOD','MSTR','CRCL','OPENAI','SPACEX','XYZ100','NASDAQ100','NAS100','SP500','US500','XAU','XAG','XPD','XPT','WTI','BRENT','NG','HG'];
  const LS = k => 'crimedesk_' + k;
  const get = (k, dflt) => { try { const v = JSON.parse(localStorage.getItem(LS(k))); return v == null ? dflt : v; } catch (e) { return dflt; } };
  const set = (k, v) => localStorage.setItem(LS(k), JSON.stringify(v));

  let DESK = get('tickers', SEED);
  let EXCL = get('excludes', EXCLUDE_SEED);
  let deskSet = new Set(DESK), exclSet = new Set(EXCL);
  let hideOthers = get('hide_others', false);
  let tab = get('tab', 'discover');            // 'discover' | 'desk'
  let newOnly = get('new_only', false);        // discover: hide names already on the watchlist
  let cfg = get('cfg', { minVolM: 5, maxOiM: 300, top: 40 });
  let sortKey = 'oi_24h_change_pct', sortDir = -1;

  // ---------- data: pull the full dataset out of React fiber props ----------
  function getDataset() {
    for (const tbl of document.querySelectorAll('table')) {
      const fk = Object.keys(tbl).find(k => k.startsWith('__reactFiber$'));
      if (!fk) continue;
      let f = tbl[fk], depth = 0;
      while (f && depth < 40) {
        const p = f.memoizedProps;
        if (p && typeof p === 'object') {
          for (const k of Object.keys(p)) {
            const v = p[k];
            if (Array.isArray(v) && v.length > 200 && v[0] && typeof v[0] === 'object' && 'symbol' in v[0] && 'oi_usd' in v[0]) return v;
          }
        }
        f = f.return; depth++;
      }
    }
    return null;
  }

  // ---------- helpers ----------
  const fmtUsd = n => n == null ? '—' : n >= 1e9 ? '$' + (n / 1e9).toFixed(2) + 'B' : n >= 1e6 ? '$' + (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? '$' + (n / 1e3).toFixed(0) + 'K' : '$' + n.toFixed(0);
  const fmtPct = n => n == null ? '—' : (n > 0 ? '+' : '') + n.toFixed(1) + '%';
  const fmtPx = n => n == null ? '—' : n >= 1000 ? '$' + n.toLocaleString('en-US', {maximumFractionDigits: 0}) : n >= 1 ? '$' + n.toFixed(2) : '$' + n.toPrecision(3);
  const f4h = r => r.top_funding_bps == null ? null : r.top_funding_bps / 200; // loris bps are 8h-normalized -> %/4h equivalent

  function signature(r) {
    const f = f4h(r), oi = r.oi_24h_change_pct;
    if (oi == null) return null;
    if (oi >= 10 && f != null && f <= -0.10) return ['SQZ-LOAD', '#22c55e'];   // OI built while shorts pay: trap-formation net (§4 legs 1-2)
    if (oi >= 10 && f != null && f >= 0.10)  return ['CROWD-LONG', '#ef4444']; // OI built while longs pay: distribution watch
    if (oi >= 10) return ['BUILD', '#eab308'];                                // flat funding: possible delta-neutral construction (§0.6.3a)
    return null;
  }

  // ---------- hide non-desk rows in native tables (tables with a "Symbol" header) ----------
  function tick(txt) {
    if (!txt) return null;
    const m = txt.match(/\(([A-Z0-9]{1,12})\)/); if (m) return m[1];
    const t = txt.trim().split(/[\s\n\t]/)[0].toUpperCase();
    if (!/^[A-Z0-9]{1,14}$/.test(t)) return null;
    for (const suf of ['USDT', 'USD']) if (t.endsWith(suf) && t.length > suf.length + 1) return t.slice(0, -suf.length);
    return t;
  }
  function filterNativeTables() {
    document.querySelectorAll('table').forEach(tb => {
      if (tb.id === 'cd-tbl') return; // never filter the desk panel itself
      let ths = [...tb.querySelectorAll('thead th')].map(h => h.innerText.trim().toLowerCase());
      let symIdx = ths.findIndex(h => h === 'symbol');
      if (symIdx < 0 && ths.length === 0 && tb.parentElement) { // header/body split across twin tables
        for (const p of tb.parentElement.querySelectorAll('table')) {
          if (p === tb) continue;
          const h2 = [...p.querySelectorAll('thead th')].map(h => h.innerText.trim().toLowerCase());
          const i2 = h2.findIndex(h => h === 'symbol');
          if (i2 >= 0) { symIdx = i2; break; }
        }
      }
      if (symIdx < 0) return;
      tb.querySelectorAll('tbody tr').forEach(tr => {
        const cell = tr.cells && tr.cells[symIdx];
        const t = tick(cell ? cell.innerText : tr.innerText);
        tr.style.display = (!hideOthers || (t && deskSet.has(t))) ? '' : 'none';
      });
    });
  }

  // ---------- the desk panel ----------
  const css = `
  #cd-panel{position:relative;z-index:40;margin:8px 12px;border:1px solid #333;border-radius:10px;background:#0c0d10;color:#d4d4d8;font:12px/1.45 ui-monospace,Menlo,monospace}
  #cd-head{display:flex;align-items:center;gap:10px;padding:7px 12px;user-select:none;flex-wrap:wrap}
  #cd-head b{color:#f4f4f5;letter-spacing:.06em}
  #cd-head .cd-badge{color:#0c0d10;background:#22c55e;border-radius:4px;padding:0 6px;font-weight:700}
  #cd-head .cd-ctl{margin-left:auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  #cd-panel button{background:#1c1d22;border:1px solid #3a3b42;color:#d4d4d8;border-radius:6px;padding:2px 8px;cursor:pointer;font:inherit}
  #cd-panel button.on{background:#22c55e;color:#0c0d10;border-color:#22c55e}
  #cd-panel button.cd-tab{border-radius:6px 6px 0 0}
  #cd-cfg{display:flex;gap:10px;align-items:center;padding:4px 12px;color:#71717a;flex-wrap:wrap}
  #cd-cfg input[type=number]{width:58px;background:#111;border:1px solid #333;color:#d4d4d8;border-radius:4px;padding:1px 4px;font:inherit}
  #cd-body{padding:0 12px 10px}
  #cd-tbl{width:100%;border-collapse:collapse}
  #cd-tbl th{text-align:right;color:#71717a;font-weight:500;padding:4px 8px;cursor:pointer;white-space:nowrap}
  #cd-tbl th:first-child,#cd-tbl td:first-child{text-align:left}
  #cd-tbl td{padding:3px 8px;text-align:right;border-top:1px solid #1c1d22;white-space:nowrap}
  #cd-tbl a{color:#f4f4f5;text-decoration:none;font-weight:600}
  #cd-tbl a:hover{text-decoration:underline}
  #cd-tbl tr.cd-thin td{opacity:.45}
  #cd-note{color:#52525b;padding-top:6px}
  #cd-panel textarea{width:100%;height:70px;background:#111;border:1px solid #333;color:#d4d4d8;border-radius:6px;margin-top:6px;font:inherit;display:none}
  .cd-up{color:#22c55e}.cd-dn{color:#ef4444}
  .cd-sig{border-radius:4px;padding:0 5px;font-weight:700;color:#0c0d10}
  .cd-wl{color:#818cf8;font-weight:700}`;

  function ensurePanel() {
    if (document.getElementById('cd-panel')) return;
    const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
    const p = document.createElement('div'); p.id = 'cd-panel';
    p.innerHTML = `<div id="cd-head"><b>CRIME DESK</b>
      <button class="cd-tab" id="cd-tab-discover">DISCOVER</button>
      <button class="cd-tab" id="cd-tab-desk">DESK</button>
      <span class="cd-badge" id="cd-count"></span>
      <span class="cd-ctl">
        <button id="cd-copy" title="copy current view as JSON for the desk session (pbpaste)">copy json</button>
        <button id="cd-newonly" title="hide names already on the watchlist">new only</button>
        <button id="cd-hide" title="hide all non-desk rows in the site's own tables">hide others</button>
        <button id="cd-editbtn" title="edit watchlist tickers">edit list</button>
        <button id="cd-editexbtn" title="edit excluded majors/stables">edit excludes</button>
        <button id="cd-collapse">▾</button></span></div>
      <div id="cd-cfg">vol ≥ $<input type="number" id="cd-minvol" min="0"> M&nbsp;·&nbsp;OI ≤ $<input type="number" id="cd-maxoi" min="0"> M&nbsp;·&nbsp;top <input type="number" id="cd-top" min="5" max="200"><span id="cd-univ" style="margin-left:auto"></span></div>
      <div id="cd-body"><div id="cd-tblwrap"></div>
      <textarea id="cd-edit" spellcheck="false"></textarea>
      <textarea id="cd-editex" spellcheck="false"></textarea>
      <div id="cd-note"></div></div>`;
    const main = document.querySelector('main') || document.body;
    main.insertBefore(p, main.firstChild);

    const el = id => document.getElementById(id);
    el('cd-minvol').value = cfg.minVolM; el('cd-maxoi').value = cfg.maxOiM; el('cd-top').value = cfg.top;
    ['cd-minvol','cd-maxoi','cd-top'].forEach(id => el(id).addEventListener('change', () => {
      cfg = { minVolM: +el('cd-minvol').value || 0, maxOiM: +el('cd-maxoi').value || 1e9, top: Math.max(5, +el('cd-top').value || 40) };
      set('cfg', cfg); render();
    }));

    el('cd-collapse').addEventListener('click', () => {
      const b = el('cd-body'), c = el('cd-cfg');
      const open = b.style.display !== 'none';
      b.style.display = c.style.display = open ? 'none' : '';
      el('cd-collapse').textContent = open ? '▸' : '▾';
      set('open', !open);
    });
    if (get('open', true) === false) { el('cd-body').style.display = el('cd-cfg').style.display = 'none'; el('cd-collapse').textContent = '▸'; }

    const syncBtns = () => {
      el('cd-hide').classList.toggle('on', hideOthers);
      el('cd-newonly').classList.toggle('on', newOnly);
      el('cd-tab-discover').classList.toggle('on', tab === 'discover');
      el('cd-tab-desk').classList.toggle('on', tab === 'desk');
      el('cd-newonly').style.display = tab === 'discover' ? '' : 'none';
      el('cd-cfg').style.display = (tab === 'discover' && get('open', true)) ? '' : 'none';
    };
    el('cd-hide').addEventListener('click', () => { hideOthers = !hideOthers; set('hide_others', hideOthers); syncBtns(); filterNativeTables(); });
    el('cd-newonly').addEventListener('click', () => { newOnly = !newOnly; set('new_only', newOnly); syncBtns(); render(); });
    el('cd-tab-discover').addEventListener('click', () => { tab = 'discover'; set('tab', tab); syncBtns(); render(); });
    el('cd-tab-desk').addEventListener('click', () => { tab = 'desk'; set('tab', tab); syncBtns(); render(); });
    syncBtns();

    const bindEditor = (btnId, taId, getList, save) => {
      const ta = el(taId);
      el(btnId).addEventListener('click', () => {
        if (ta.style.display === 'none' || !ta.style.display) { ta.value = getList().join(', '); ta.style.display = 'block'; ta.focus(); }
        else {
          save(ta.value.toUpperCase().split(/[\s,;]+/).filter(Boolean));
          ta.style.display = 'none'; render(); filterNativeTables();
        }
      });
    };
    bindEditor('cd-editbtn', 'cd-edit', () => DESK, l => { DESK = l; deskSet = new Set(l); set('tickers', l); });
    bindEditor('cd-editexbtn', 'cd-editex', () => EXCL, l => { EXCL = l; exclSet = new Set(l); set('excludes', l); });

    el('cd-copy').addEventListener('click', () => {
      const snap = exportJson();
      if (!snap) { el('cd-copy').textContent = 'no data'; setTimeout(() => el('cd-copy').textContent = 'copy json', 1500); return; }
      navigator.clipboard.writeText(JSON.stringify(snap, null, 1)).then(() => {
        el('cd-copy').textContent = `copied ${snap.candidates.length}`;
        setTimeout(() => el('cd-copy').textContent = 'copy json', 2000);
      });
    });
    window.__cdSyncBtns = syncBtns;
  }

  // snapshot of the CURRENT view (all matches, not just displayed top-N) for the desk session
  function exportJson() {
    const data = getDataset();
    if (!data) return null;
    const shape = r => {
      const f = f4h(r), sig = signature(r);
      return { ticker: String(r.symbol).toUpperCase(), price_usd: r.price_usd, px_chg24_pct: r.price_24h_change_pct,
        vol24_usd: r.volume_24h_usd, oi_usd: r.oi_usd, oi_chg24_pct: r.oi_24h_change_pct,
        liq24_usd: r.liquidation_24h_usd, funding_4h_approx: f, funding_src_venue: r.top_funding_exchange || null,
        signature: sig ? sig[0] : null, on_watchlist: deskSet.has(String(r.symbol).toUpperCase()) };
    };
    let candidates;
    if (tab === 'discover') {
      let uni = data.filter(r => r.asset_class == null
        && !exclSet.has(String(r.symbol).toUpperCase())
        && (r.volume_24h_usd || 0) >= cfg.minVolM * 1e6
        && (r.oi_usd || 0) <= cfg.maxOiM * 1e6
        && r.oi_usd != null && r.oi_usd > 0);
      if (newOnly) uni = uni.filter(r => !deskSet.has(String(r.symbol).toUpperCase()));
      uni.sort((a, b) => (b.oi_24h_change_pct ?? -1e9) - (a.oi_24h_change_pct ?? -1e9));
      candidates = uni.map(shape);
    } else {
      const bySym = new Map(data.map(r => [String(r.symbol).toUpperCase(), r]));
      candidates = DESK.map(t => bySym.get(t) ? shape(bySym.get(t)) : { ticker: t, not_listed: true });
    }
    return { source: 'loris.tools /markets/perps (client dataset, 8h-normalized funding converted to ~%/4h)',
      generated_at: new Date().toISOString(), view: tab, cfg: tab === 'discover' ? cfg : null, new_only: newOnly,
      universe: data.length, candidates,
      caveats: ['funding_4h_approx is derived from loris 8h-normalized top print — verify per-interval on venue before any verdict (§3)',
                'oi_chg24_pct is loris-computed, single-source — cross-check before it gates a trade',
                'signature = §4 legs 1-2 prior only, NOT a verdict'] };
  }

  function rowHtml(t, r, wl) {
    const f = f4h(r), sig = signature(r);
    const thin = (r.volume_24h_usd || 0) < 10e6 ? ' class="cd-thin"' : ''; // §7 gate: <$10M dim
    const pc = v => v == null ? '' : v >= 0 ? 'cd-up' : 'cd-dn';
    return `<tr${thin}><td><a href="/funding/coin?symbol=${t.toLowerCase()}" title="funding by venue">${t}</a>${wl ? ' <span class="cd-wl" title="already on watchlist">•wl</span>' : ''}</td>
      <td>${fmtPx(r.price_usd)}</td><td class="${pc(r.price_24h_change_pct)}">${fmtPct(r.price_24h_change_pct)}</td>
      <td>${fmtUsd(r.volume_24h_usd)}</td><td>${fmtUsd(r.oi_usd)}</td>
      <td class="${pc(r.oi_24h_change_pct)}"><b>${fmtPct(r.oi_24h_change_pct)}</b></td>
      <td>${fmtUsd(r.liquidation_24h_usd)}</td>
      <td class="${f==null?'':(f<0?'cd-dn':'cd-up')}">${f==null?'—':(f>0?'+':'')+f.toFixed(3)}<span style="color:#52525b"> ${r.top_funding_exchange||''}</span></td>
      <td>${sig?`<span class="cd-sig" style="background:${sig[1]}">${sig[0]}</span>`:''}</td></tr>`;
  }

  const COLS = [['symbol','Symbol'],['price_usd','Price'],['price_24h_change_pct','Px Δ24h'],['volume_24h_usd','Vol 24h'],['oi_usd','OI'],['oi_24h_change_pct','OI Δ24h'],['liquidation_24h_usd','Liq 24h'],['fund','≈Fund %/4h'],['sig','Signal']];

  function render() {
    const data = getDataset();
    ensurePanel();
    const wrap = document.getElementById('cd-tblwrap'), note = document.getElementById('cd-note'), univ = document.getElementById('cd-univ');
    if (window.__cdSyncBtns) window.__cdSyncBtns();
    if (!data) {
      wrap.innerHTML = '<div style="color:#71717a;padding:8px 0">full dataset not found on this page — "hide others" still works on its tables. open /markets/perps for the board.</div>';
      document.getElementById('cd-count').textContent = '—'; univ.textContent = ''; return;
    }
    const head = '<table id="cd-tbl"><thead><tr>' + COLS.map(c => `<th data-k="${c[0]}">${c[1]}${sortKey===c[0]?(sortDir<0?' ↓':' ↑'):''}</th>`).join('') + '</tr></thead><tbody>';
    let html = head, count = '';

    if (tab === 'discover') {
      // the net: crypto only, no majors/stables, tradable vol, OI below blue-chip territory
      let uni = data.filter(r => r.asset_class == null
        && !exclSet.has(String(r.symbol).toUpperCase())
        && (r.volume_24h_usd || 0) >= cfg.minVolM * 1e6
        && (r.oi_usd || 0) <= cfg.maxOiM * 1e6
        && r.oi_usd != null && r.oi_usd > 0);
      if (newOnly) uni = uni.filter(r => !deskSet.has(String(r.symbol).toUpperCase()));
      uni.sort((a, b) => {
        const av = a[sortKey], bv = b[sortKey];
        if (av == null && bv == null) return 0; if (av == null) return 1; if (bv == null) return -1;
        return (av - bv) * sortDir;
      });
      const shown = uni.slice(0, cfg.top);
      for (const r of shown) html += rowHtml(String(r.symbol).toUpperCase(), r, deskSet.has(String(r.symbol).toUpperCase()));
      count = `${shown.length}/${uni.length}`;
      univ.textContent = `${uni.length} match of ${data.length} · ${data.length - uni.length} cut (TradFi/majors/thin/OI-cap)`;
      note.innerHTML = 'the NET: crypto perps only, majors+stables excluded, vol≥floor, OI≤cap — sorted by OI Δ24h = where positions are being built. <span class="cd-wl">•wl</span> = already on the desk. a hit is a CANDIDATE (§4 gate still applies: OI construction, CVD, structure). funding ≈%/4h from loris\' 8h-normalized print — verify per-interval on venue (§3). dim = vol&lt;$10M (§7).';
    } else {
      const bySym = new Map(data.map(r => [String(r.symbol).toUpperCase(), r]));
      let rows = DESK.map(t => ({ t, r: bySym.get(t) || null }));
      rows.sort((a, b) => {
        const av = a.r ? a.r[sortKey] : null, bv = b.r ? b.r[sortKey] : null;
        if (av == null && bv == null) return 0; if (av == null) return 1; if (bv == null) return -1;
        return (av - bv) * sortDir;
      });
      let listed = 0;
      for (const {t, r} of rows) {
        if (!r) { html += `<tr class="cd-thin"><td>${t}</td><td colspan="8" style="text-align:left;color:#71717a">not listed on loris (no perp data — not a zero)</td></tr>`; continue; }
        listed++; html += rowHtml(t, r, false);
      }
      count = `${listed}/${DESK.length}`;
      univ.textContent = '';
      note.innerHTML = 'the WATCHLIST board, sorted by OI Δ24h. not-listed names shown, never silently dropped. funding ≈%/4h from loris\' 8h-normalized print — verify per-interval on venue (§3). dim = vol&lt;$10M (§7).';
    }

    html += '</tbody></table>';
    wrap.innerHTML = html;
    document.getElementById('cd-count').textContent = count;
    wrap.querySelectorAll('th').forEach(th => th.addEventListener('click', () => {
      const k = th.dataset.k; if (k === 'fund' || k === 'sig' || k === 'symbol') return;
      if (sortKey === k) sortDir *= -1; else { sortKey = k; sortDir = -1; }
      render();
    }));
  }

  // ---------- boot + keep alive across SPA re-renders ----------
  let t0 = null;
  const refresh = () => { render(); filterNativeTables(); };
  const debounced = () => { clearTimeout(t0); t0 = setTimeout(() => { if (!document.getElementById('cd-panel')) { ensurePanel(); render(); } filterNativeTables(); }, 400); };
  const boot = () => {
    refresh();
    new MutationObserver(debounced).observe(document.body, { childList: true, subtree: true });
    setInterval(render, 60000); // loris refreshes ~every minute; re-read the dataset
    let path = location.pathname;
    setInterval(() => { if (location.pathname !== path) { path = location.pathname; setTimeout(refresh, 1500); } }, 500);
  };
  if (document.readyState === 'complete') setTimeout(boot, 1000); else window.addEventListener('load', () => setTimeout(boot, 1000));
})();
