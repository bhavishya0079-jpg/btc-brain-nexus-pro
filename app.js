(() => {
"use strict";
/* ------------------------------------------------------------------ helpers */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 2) => (n == null || !isFinite(n)) ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d });
const pct = (n) => (n == null || !isFinite(n)) ? "—" : `<span class="${n >= 0 ? "up" : "down"}">${n >= 0 ? "+" : ""}${Number(n).toFixed(2)}%</span>`;
const money = (n) => (n == null || !isFinite(n)) ? "—" : `<span class="${n >= 0 ? "up" : "down"}">${fmt(n, 2)}</span>`;
const when = (ms) => ms ? new Date(ms).toLocaleString() : "—";
const DISC = `<div class="disc">Research &amp; analysis only. No real orders are placed and no profit is guaranteed. Backtests describe past data only.</div>`;
let BASE = localStorage.getItem("nexusBase") || window.NEXUS_API_BASE || "/api";
let TOKEN = localStorage.getItem("nexusToken") || "";
const CRYPTO = ["binance", "delta"], STOCK = ["angelone", "groww"];
const TFS = ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"];

async function api(path, { method = "GET", body, params } = {}) {
  let url = BASE + path;
  if (params) { const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null)); if ([...q].length) url += "?" + q; }
  let r;
  try {
    r = await fetch(url, { method, headers: { "Content-Type": "application/json", ...(TOKEN ? { Authorization: "Bearer " + TOKEN } : {}) }, body: body ? JSON.stringify(body) : undefined });
  } catch (e) { throw new Error("Cannot reach backend (" + BASE + "). Check the URL, server and CORS settings."); }
  let data = null; try { data = await r.json(); } catch (e) { /* non-json */ }
  if (r.status === 401 && path !== "/auth/login") { logout(); throw new Error("Session expired"); }
  if (!r.ok) { const er = new Error(data?.error?.message || `HTTP ${r.status}`); er.code = data?.error?.code; throw er; }
  return data;
}
const toast = (msg) => { const t = document.createElement("div"); t.className = "toast"; t.textContent = msg; document.body.appendChild(t); setTimeout(() => t.remove(), 6000); };
const errBox = (e) => `<div class="err">${esc(e.message || e)}${e.code === "not_configured" ? " — add the credentials on the server (see docs/INSTALL.md)." : ""}</div>`;
const busy = async (btn, fn) => { btn.disabled = true; const o = btn.innerHTML; btn.innerHTML = '<span class="spin"></span>'; try { return await fn(); } finally { btn.disabled = false; btn.innerHTML = o; } };
const opts = (arr, sel) => arr.map((v) => `<option ${v === sel ? "selected" : ""}>${esc(v)}</option>`).join("");
const kpi = (l, v) => `<div class="card kpi"><div class="l">${l}</div><div class="v">${v}</div></div>`;
const table = (heads, rows, cls = "") => `<div class="tw"><table class="${cls}"><thead><tr>${heads.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;

function lineChart(cv, pts, color = "#f7931a") {
  const dpr = window.devicePixelRatio || 1, w = cv.clientWidth || 600, h = cv.clientHeight || 180; cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  if (pts.length < 2) return; const ys = pts.map((p) => p.y), lo = Math.min(...ys), hi = Math.max(...ys), sp = hi - lo || 1;
  g.strokeStyle = color; g.lineWidth = 1.6; g.beginPath();
  pts.forEach((p, i) => { const x = i / (pts.length - 1) * (w - 60), y = 8 + (hi - p.y) / sp * (h - 16); i ? g.lineTo(x, y) : g.moveTo(x, y); }); g.stroke();
  g.fillStyle = "#8b98a9"; g.font = "11px monospace"; g.fillText(fmt(hi, 0), w - 56, 14); g.fillText(fmt(lo, 0), w - 56, h - 4);
}

function signalHtml(s) {
  const L = s.levels;
  return `<div class="row"><span class="tag ${s.signal}">${s.signal}</span><span class="pill">confidence ${s.confidence}/90 (not a win probability)</span><span class="pill">score ${fmt(s.combined_score, 1)}</span></div>
  ${L ? `<div class="grid g4"><div class="card kpi"><div class="l">Entry</div><div class="v">${fmt(L.entry, 4)}</div></div><div class="card kpi"><div class="l">Stop loss</div><div class="v down">${fmt(L.stop_loss, 4)}</div></div>
  ${[1, 2, 3].map((i) => `<div class="card kpi"><div class="l">Target ${i} · R/R ${fmt(L.risk_reward?.["target_" + i], 1)}</div><div class="v up">${fmt(L["target_" + i], 4)}</div></div>`).join("")}</div><div class="mut" style="margin:6px 0">${esc(L.basis)}</div>` : ""}
  ${Object.entries(s.timeframes || {}).map(([tf, t]) => `<div class="card" style="margin-top:8px"><b>${tf}</b> · ${esc(t.direction)} · trend ${esc(t.trend)} (${esc(t.trend_strength)}) · RSI ${fmt(t.rsi, 1)} · ADX ${fmt(t.adx, 1)} · vol×${fmt(t.volume_ratio, 2)} · supertrend ${esc(t.supertrend)}<br><span class="mut">${(t.reasons || []).map(esc).join(" · ")}</span></div>`).join("")}
  ${Object.entries(s.skipped_timeframes || {}).map(([k, v]) => `<div class="mut">skipped ${k}: ${esc(v)}</div>`).join("")}
  <div class="disc">${esc(s.disclaimer)}</div>`;
}

/* ------------------------------------------------------------------ pages */
const PAGES = [];
const page = (id, title, render) => PAGES.push({ id, title, render });
let cleanup = [];
const onLeave = (fn) => cleanup.push(fn);

function symbolPicker(el, provider, input) {
  api("/market/symbols", { params: { provider, limit: 300 } }).then((r) => {
    let dl = $("#symlist", el); if (!dl) { dl = document.createElement("datalist"); dl.id = "symlist"; el.appendChild(dl); }
    dl.innerHTML = r.symbols.map((s) => `<option value="${esc(s.symbol)}">`).join(""); input.setAttribute("list", "symlist");
  }).catch(() => { });
}
const DEFSYM = { binance: "BTCUSDT", delta: "BTCUSD", angelone: "NSE:NIFTY", groww: "NSE:NIFTY" };

page("dashboard", "Dashboard", async (el) => {
  el.innerHTML = `<h2>Dashboard</h2>${DISC}<div id="dk" class="grid g4"></div><div class="grid g2" style="margin-top:14px"><div class="card"><h3>AI signal · BTCUSDT</h3><div id="dsig"><span class="spin"></span></div></div><div class="card"><h3>Top movers · Binance</h3><div id="dmov"><span class="spin"></span></div></div></div>`;
  const items = [["binance", "BTCUSDT"], ["binance", "ETHUSDT"], ["delta", "BTCUSD"], ["angelone", "NSE:NIFTY"], ["angelone", "NSE:BANKNIFTY"]];
  const res = await api("/market/compare", { params: { items: items.map((i) => i.join(":")).join(",") } });
  $("#dk", el).innerHTML = res.items.map((t) => t.error ? kpi(`${esc(t.provider)} ${esc(t.symbol)}`, `<span class="mut" style="font-size:12px">${esc(t.error)}</span>`) : kpi(`${esc(t.provider)} · ${esc(t.symbol)}`, `${fmt(t.ltp, 2)} <small>${pct(t.change_pct)}</small>`)).join("");
  api("/analysis/signal", { params: { provider: "binance", symbol: "BTCUSDT", tfs: "15m,1h,4h" } }).then((s) => $("#dsig", el).innerHTML = signalHtml(s)).catch((e) => $("#dsig", el).innerHTML = errBox(e));
  api("/market/movers", { params: { provider: "binance" } }).then((m) => $("#dmov", el).innerHTML = table(["Symbol", "Last", "24h"], [...m.gainers.slice(0, 5), ...m.losers.slice(0, 5)].map((t) => `<tr><td>${esc(t.symbol)}</td><td>${fmt(t.ltp, 4)}</td><td>${pct(t.change_pct)}</td></tr>`))).catch((e) => $("#dmov", el).innerHTML = errBox(e));
});

function marketPage(id, title, providers, market) {
  page(id, title, async (el) => {
    el.innerHTML = `<h2>${title}</h2>${DISC}<div class="row"><select id="mp">${opts(providers)}</select><input id="mq" placeholder="Filter symbol…"><button id="mgo" class="p">Load</button><label class="mut"><input type="checkbox" id="mlive"> live stream</label></div><div id="mout"></div>
    <div class="grid g2" style="margin-top:14px"><div class="card" id="mside"></div><div class="card" id="mdepth"><h3>Market depth</h3><div class="mut">Pick a symbol…</div></div></div>`;
    let es = null; onLeave(() => es && es.close());
    const rows = (ts) => ts.map((t) => `<tr data-s="${esc(t.symbol)}" style="cursor:pointer"><td>${esc(t.symbol)}</td><td>${fmt(t.ltp, 4)}</td><td>${pct(t.change_pct)}</td><td>${fmt(t.high, 4)}</td><td>${fmt(t.low, 4)}</td><td>${fmt(t.volume, 2)}</td></tr>`).join("");
    const load = async () => {
      const p = $("#mp", el).value; $("#mout", el).innerHTML = '<span class="spin"></span>';
      try {
        let ts = (await api("/market/tickers", { params: { provider: p } })).tickers;
        const q = $("#mq", el).value.trim().toUpperCase(); if (q) ts = ts.filter((t) => t.symbol.toUpperCase().includes(q));
        ts.sort((a, b) => (b.quote_volume || b.volume || 0) - (a.quote_volume || a.volume || 0));
        $("#mout", el).innerHTML = `<div class="card">${table(["Symbol", "LTP", "24h / day", "High", "Low", "Volume"], [rows(ts.slice(0, 150))])}</div>`;
        $$("#mout tr[data-s]", el).forEach((tr) => tr.onclick = () => depth(p, tr.dataset.s));
        $("#mside", el).innerHTML = market === "stock" ? '<h3>Sector analysis</h3><span class="spin"></span>' : '<h3>Exchange comparison · BTC</h3><span class="spin"></span>';
        if (market === "stock") api("/market/sectors", { params: { provider: p } }).then((r) => $("#mside", el).innerHTML = "<h3>Sector analysis</h3>" + table(["Sector", "Avg %", "Leaders", "Laggards"], r.sectors.map((s) => `<tr><td>${esc(s.sector)}</td><td>${pct(s.avg_change_pct)}</td><td>${s.leaders.map((x) => esc(x[0])).join(", ")}</td><td>${s.laggards.map((x) => esc(x[0])).join(", ")}</td></tr>`))).catch((e) => $("#mside", el).innerHTML = "<h3>Sector analysis</h3>" + errBox(e));
        else api("/market/compare", { params: { items: "binance:BTCUSDT,delta:BTCUSD" } }).then((r) => $("#mside", el).innerHTML = "<h3>Exchange comparison · BTC</h3>" + table(["Exchange", "Symbol", "LTP", "24h"], r.items.map((t) => `<tr><td>${esc(t.provider)}</td><td>${esc(t.symbol)}</td><td>${t.error ? esc(t.error) : fmt(t.ltp, 2)}</td><td>${pct(t.change_pct)}</td></tr>`)));
      } catch (e) { $("#mout", el).innerHTML = errBox(e); }
    };
    async function depth(p, s) {
      $("#mdepth", el).innerHTML = `<h3>Market depth · ${esc(s)}</h3><span class="spin"></span>`;
      try { const d = await api("/market/depth", { params: { provider: p, symbol: s, levels: 10 } });
        $("#mdepth", el).innerHTML = `<h3>Market depth · ${esc(s)}</h3><div class="grid g2"><div>${table(["Bid", "Size"], d.bids.map((b) => `<tr><td class="up">${fmt(b[0], 4)}</td><td>${fmt(b[1], 4)}</td></tr>`))}</div><div>${table(["Ask", "Size"], d.asks.map((b) => `<tr><td class="down">${fmt(b[0], 4)}</td><td>${fmt(b[1], 4)}</td></tr>`))}</div></div>`;
      } catch (e) { $("#mdepth", el).innerHTML = `<h3>Market depth · ${esc(s)}</h3>` + errBox(e); }
    }
    $("#mgo", el).onclick = (ev) => busy(ev.target, load);
    $("#mlive", el).onchange = (ev) => {
      if (es) { es.close(); es = null; } if (!ev.target.checked) return;
      const syms = $$("#mout tr[data-s]", el).slice(0, 15).map((r) => r.dataset.s);
      es = new EventSource(`${BASE}/stream/tickers?${new URLSearchParams({ provider: $("#mp", el).value, symbols: syms.join(","), token: TOKEN })}`);
      es.onmessage = (m) => JSON.parse(m.data).tickers.forEach((t) => { const r = $(`#mout tr[data-s="${CSS.escape(t.symbol)}"]`, el); if (r) { r.children[1].textContent = fmt(t.ltp, 4); r.children[2].innerHTML = pct(t.change_pct); } });
      es.addEventListener("problem", (m) => toast("Stream: " + JSON.parse(m.data).message));
    };
    load();
  });
}
marketPage("crypto", "Crypto Market", CRYPTO, "crypto");
marketPage("stocks", "Indian Stocks · NSE / BSE", STOCK, "stock");

page("options", "Options", async (el) => {
  el.innerHTML = `<h2>Options Chain</h2>${DISC}<div class="row"><select id="op">${opts(["delta", "angelone", "groww"])}</select><input id="ou" value="BTC" style="width:110px"><select id="oe"></select><button id="oex">Expiries</button><label class="mut"><input type="checkbox" id="od" checked> derive missing greeks (labelled)</label><button id="ogo" class="p">Load chain</button></div><div id="osum"></div><div id="oout"></div><div id="ostrike"></div>`;
  const prov = () => $("#op", el).value;
  $("#op", el).onchange = () => { $("#ou", el).value = prov() === "delta" ? "BTC" : "NIFTY"; };
  $("#oex", el).onclick = (ev) => busy(ev.target, async () => { try { const r = await api("/options/expiries", { params: { provider: prov(), underlying: $("#ou", el).value } }); $("#oe", el).innerHTML = opts(r.expiries); } catch (e) { $("#oout", el).innerHTML = errBox(e); } });
  const leg = (l, side) => l ? (side === "CE" ? [l.oi, l.oi_change, l.volume, l.iv, l.delta, l.gamma, l.theta, l.vega, l.ltp] : [l.ltp, l.vega, l.theta, l.gamma, l.delta, l.iv, l.volume, l.oi_change, l.oi]) : Array(9).fill(null);
  const cell = (v, i, d) => `<td>${v == null ? '<span class="mut">—</span>' : fmt(v, d ?? 2)}</td>`;
  $("#ogo", el).onclick = (ev) => busy(ev.target, async () => {
    try {
      const ch = await api("/options/chain", { params: { provider: prov(), underlying: $("#ou", el).value, expiry: $("#oe", el).value, derive: $("#od", el).checked ? 1 : 0 } });
      const s = ch.summary;
      $("#osum", el).innerHTML = `<div class="grid g4" style="margin-bottom:12px">${kpi("Spot", fmt(s.spot, 2))}${kpi("ATM strike", fmt(s.atm_strike, 2))}${kpi("PCR (OI)", fmt(s.pcr_oi, 2))}${kpi("Max pain", fmt(s.max_pain, 2))}${kpi("ATM straddle", fmt(s.atm_straddle_premium, 2))}${kpi("Days to expiry", fmt(s.days_to_expiry, 2))}${kpi("Resistance (CE OI)", s.resistance_by_ce_oi.map((x) => fmt(x.strike, 0)).join(", ") || "—")}${kpi("Support (PE OI)", s.support_by_pe_oi.map((x) => fmt(x.strike, 0)).join(", ") || "—")}</div><div class="mut">${esc(s.pcr_reading || "")}. ${esc(s.note)} Missing provider fields are shown as “—”, never invented.</div>`;
      const heads = ["OI", "ΔOI", "Vol", "IV%", "Δ", "Γ", "Θ", "Vega", "LTP", "Strike", "LTP", "Vega", "Θ", "Γ", "Δ", "IV%", "Vol", "ΔOI", "OI"];
      $("#oout", el).innerHTML = table(heads, ch.rows.map((r) => `<tr class="${r.strike === s.atm_strike ? "atm" : ""}" data-k="${r.strike}" style="cursor:pointer">${leg(r.CE, "CE").map((v, i) => cell(v, i, [0, 0, 0, 1, 3, 5, 2, 2, 2][i])).join("")}<td><b>${fmt(r.strike, 2)}</b></td>${leg(r.PE, "PE").map((v, i) => cell(v, i, [2, 2, 2, 5, 3, 1, 0, 0, 0][i])).join("")}</tr>`));
      $$("#oout tr[data-k]", el).forEach((tr) => tr.onclick = async () => {
        try { const a = await api("/options/strike", { params: { provider: prov(), underlying: $("#ou", el).value, expiry: $("#oe", el).value, strike: tr.dataset.k } });
          $("#ostrike", el).innerHTML = `<div class="card" style="margin-top:12px"><h3>Strike analysis · ${esc(a.strike)}</h3><pre class="mono mut" style="white-space:pre-wrap">${esc(JSON.stringify(a, null, 2))}</pre></div>`; } catch (e) { $("#ostrike", el).innerHTML = errBox(e); }
      });
    } catch (e) { $("#oout", el).innerHTML = errBox(e); }
  });
  $("#oex", el).click();
});

page("charts", "Charts & AI Analysis", async (el) => {
  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  el.innerHTML = `<h2>Charts &amp; AI Analysis</h2>${DISC}<div class="row"><select id="cp">${opts([...CRYPTO, ...STOCK], q.get("p") || "binance")}</select><input id="cs" value="${esc(q.get("s") || "BTCUSDT")}" style="width:150px"><select id="ct">${opts(TFS, q.get("tf") || "15m")}</select>
  <button id="cgo" class="p">Load</button><button id="cdraw">✏️ Trend line</button><button id="cclr">Clear lines</button><label class="mut"><input type="checkbox" id="cfib"> Fibonacci</label></div>
  <div class="row mut" id="cind">${["ema:20", "ema:50", "sma:200", "bb", "vwap", "supertrend", "rsi", "macd", "adx", "stoch", "atr"].map((i) => `<label><input type="checkbox" value="${i}" ${["ema:20", "ema:50", "rsi"].includes(i) ? "checked" : ""}> ${i.replace(":", " ")}</label>`).join(" ")}</div>
  <div class="chartbox" id="cbox"></div><div class="row" style="margin-top:10px"><span class="mut">Levels:</span><input id="lE" placeholder="Entry" style="width:90px"><input id="lS" placeholder="Stop" style="width:90px"><input id="lT1" placeholder="T1" style="width:90px"><input id="lT2" placeholder="T2" style="width:90px"><input id="lT3" placeholder="T3" style="width:90px"><button id="lgo">Plot</button><button id="lai">Use AI levels</button></div>
  <div class="card" style="margin-top:12px"><h3>Multi-timeframe signal</h3><div class="row"><input id="ctf" value="15m,1h,4h" style="width:140px"><button id="csig">Analyse</button></div><div id="csg" class="mut">Press Analyse.</div></div>`;
  const chart = new CandleChart($("#cbox", el)); onLeave(() => chart.destroy());
  let lastSig = null;
  const sym = () => $("#cs", el).value.trim(), prov = () => $("#cp", el).value;
  $("#cp", el).onchange = () => { $("#cs", el).value = DEFSYM[prov()]; symbolPicker(el, prov(), $("#cs", el)); };
  symbolPicker(el, prov(), $("#cs", el));
  const load = async () => {
    const ind = $$("#cind input:checked", el).map((i) => i.value).join(",");
    try { chart.setData(await api("/chart", { params: { provider: prov(), symbol: sym(), tf: $("#ct", el).value, limit: 500, ind, fib: $("#cfib", el).checked ? 1 : 0 } })); plot(); }
    catch (e) { $("#cbox", el).insertAdjacentHTML("afterend", errBox(e)); setTimeout(() => $(".err", el)?.remove(), 6000); }
  };
  const plot = () => chart.setLevels([["lE", "Entry", "#4cc2ff"], ["lS", "SL", "#ff5c6c"], ["lT1", "T1", "#26d07c"], ["lT2", "T2", "#26d07c"], ["lT3", "T3", "#26d07c"]].map(([id, label, color]) => ({ label, color, price: parseFloat($("#" + id, el).value) })).filter((l) => isFinite(l.price)));
  $("#cgo", el).onclick = (ev) => busy(ev.target, load);
  $("#lgo", el).onclick = plot;
  $("#cdraw", el).onclick = (ev) => { const on = !chart.drawMode; chart.setDrawMode(on); ev.target.className = on ? "p" : ""; };
  $("#cclr", el).onclick = () => chart.clearUser();
  $("#lai", el).onclick = () => { const L = lastSig?.levels; if (!L) return toast("Run the analysis first"); $("#lE", el).value = L.entry; $("#lS", el).value = L.stop_loss; $("#lT1", el).value = L.target_1; $("#lT2", el).value = L.target_2; $("#lT3", el).value = L.target_3; plot(); };
  $("#csig", el).onclick = (ev) => busy(ev.target, async () => {
    try { lastSig = await api("/analysis/signal", { params: { provider: prov(), symbol: sym(), tfs: $("#ctf", el).value } }); $("#csg", el).innerHTML = signalHtml(lastSig); } catch (e) { $("#csg", el).innerHTML = errBox(e); }
  });
  load();
});

page("scanner", "Market Scanner", async (el) => {
  el.innerHTML = `<h2>Market Scanner</h2>${DISC}<div class="row"><select id="sp">${opts([...CRYPTO, ...STOCK])}</select><select id="st">${opts(["high_volume", "breakout", "trend", "rsi", "macd", "ema_cross", "mtf", "custom"])}</select><select id="si">${opts(TFS, "15m")}</select><input id="sn" type="number" value="20" min="1" max="40" style="width:80px" title="top N by volume"><button id="sgo" class="p">Scan</button></div>
  <div class="row"><textarea id="sy" placeholder="Optional symbols, comma separated (default: most active)" style="min-height:44px"></textarea></div>
  <div class="row"><textarea id="spar" style="min-height:70px" placeholder='Params JSON, e.g. {"min_adx":25} or custom: {"indicators":[{"id":"r","type":"rsi","params":{"period":14}}],"conditions":[{"left":"r","op":"<","right":30}]}'>{}</textarea></div>
  <div class="row"><button id="oscan">Options scan (Options page provider)</button></div><div id="sout"></div>`;
  $("#sgo", el).onclick = (ev) => busy(ev.target, async () => {
    try {
      const body = { provider: $("#sp", el).value, scan_type: $("#st", el).value, interval: $("#si", el).value, top: +$("#sn", el).value, symbols: $("#sy", el).value.split(",").map((s) => s.trim()).filter(Boolean), params: JSON.parse($("#spar", el).value || "{}") };
      if (body.scan_type === "mtf") body.mtf_intervals = ["15m", "1h", "4h"];
      const r = await api("/scan", { method: "POST", body });
      $("#sout", el).innerHTML = `<div class="card"><b>${r.matches.length}</b> matches of ${r.scanned} scanned ${r.errors.length ? `· <span class="warn">${r.errors.length} failed</span>` : ""}${table(["Symbol", "Result", "Detail"], r.matches.map((m) => `<tr><td><a href="#charts?p=${body.provider}&s=${encodeURIComponent(m.symbol)}&tf=${body.interval}">${esc(m.symbol)}</a></td><td>${esc(m.signal || fmt(m.price, 4))}</td><td style="text-align:left;white-space:normal">${esc(m.detail || JSON.stringify(m))}</td></tr>`))}${r.errors.map((e) => `<div class="mut">${esc(e.symbol)}: ${esc(e.error)}</div>`).join("")}</div>`;
    } catch (e) { $("#sout", el).innerHTML = errBox(e); }
  });
  $("#oscan", el).onclick = (ev) => busy(ev.target, async () => {
    try { const r = await api("/options/scan", { method: "POST", body: { provider: "delta", underlying: "BTC", kind: "high_oi", params: {} } });
      $("#sout", el).innerHTML = `<div class="card"><h3>Options scan (delta · BTC · high OI)</h3><pre class="mono mut">${esc(JSON.stringify(r.matches.slice(0, 15), null, 1))}</pre></div>`; } catch (e) { $("#sout", el).innerHTML = errBox(e); }
  });
});

page("strategies", "Strategy Builder", async (el) => {
  el.innerHTML = `<h2>Strategy Builder</h2>${DISC}<div class="grid g2"><div class="card"><h3>Strategies</h3><div id="sl"></div></div><div class="card"><h3>Editor (JSON rules)</h3><div class="row"><input id="sname" placeholder="Strategy name" style="flex:1"><button id="sdef">Load template</button></div><textarea id="sspec"></textarea><div class="row" style="margin-top:8px"><button id="sval">Validate</button><button id="ssave" class="p">Save as new</button><button id="supd">Save new version</button><button id="simp">Import file</button><input type="file" id="sfile" class="hide" accept=".json"></div><div id="smsg"></div></div></div><div id="sver"></div>`;
  let cur = null;
  const list = async () => { const r = await api("/strategies"); $("#sl", el).innerHTML = r.strategies.length ? table(["Name", "Active", "Versions", ""], r.strategies.map((s) => `<tr><td>${esc(s.name)}</td><td>v${s.active_version}</td><td>${s.versions}</td><td><button data-o="${s.id}">Open</button> <button class="d" data-d="${s.id}">✕</button></td></tr>`)) : '<span class="mut">No strategies yet.</span>';
    $$("[data-o]", el).forEach((b) => b.onclick = () => open(+b.dataset.o)); $$("[data-d]", el).forEach((b) => b.onclick = async () => { if (confirm("Delete strategy and all versions?")) { await api("/strategies/" + b.dataset.d, { method: "DELETE" }); cur = null; list(); } }); };
  const open = async (id) => { const s = await api("/strategies/" + id); cur = s; $("#sname", el).value = s.name; $("#sspec", el).value = JSON.stringify(s.active_spec, null, 2);
    $("#sver", el).innerHTML = `<div class="card" style="margin-top:14px"><h3>Version history · ${esc(s.name)}</h3>${table(["Version", "Status", "By", "Note", "Created", ""], s.versions.map((v) => `<tr><td>v${v.version}</td><td>${esc(v.status)}${v.version === s.active_version ? " ✓" : ""}</td><td>${esc(v.created_by)}</td><td style="white-space:normal;text-align:left">${esc(v.note || "")}</td><td>${when(v.created_at * 1000)}</td><td><button data-r="${v.version}">Restore</button> <button data-v="${v.version}">View</button></td></tr>`))}</div>
    <div class="card" style="margin-top:14px"><h3>Sandbox test (not saved, never live)</h3><div class="row"><select id="bp">${opts([...CRYPTO, ...STOCK])}</select><input id="bs" value="BTCUSDT" style="width:140px"><button id="bgo" class="p">Run sandbox</button></div><div id="bout"></div></div>`;
    $$("[data-r]", el).forEach((b) => b.onclick = async () => { await api(`/strategies/${id}/restore`, { method: "POST", body: { version: +b.dataset.r } }); toast("Restored as a new version"); open(id); list(); });
    $$("[data-v]", el).forEach((b) => b.onclick = () => { $("#sspec", el).value = JSON.stringify(s.versions.find((v) => v.version === +b.dataset.v).spec, null, 2); });
    $("#bgo", el).onclick = (ev) => busy(ev.target, async () => { try { const r = await api(`/strategies/${id}/sandbox`, { method: "POST", body: { provider: $("#bp", el).value, symbol: $("#bs", el).value, limit: 1500 } });
      $("#bout", el).innerHTML = `<div class="grid g4">${kpi("Trades", r.stats.trades)}${kpi("Win rate", fmt(r.stats.win_rate_pct, 1) + "%")}${kpi("Net profit", money(r.stats.net_profit))}${kpi("Max DD", fmt(r.stats.max_drawdown_pct, 1) + "%")}</div><div class="mut">Sandbox · data: ${r.data_source}</div>`; } catch (e) { $("#bout", el).innerHTML = errBox(e); } });
  };
  const spec = () => JSON.parse($("#sspec", el).value);
  const msg = (h) => $("#smsg", el).innerHTML = h;
  $("#sdef", el).onclick = async () => { $("#sspec", el).value = JSON.stringify(await api("/strategies/default"), null, 2); };
  $("#sval", el).onclick = async () => { try { const r = await api("/strategies/validate", { method: "POST", body: { spec: spec() } }); msg(r.valid ? `<div class="up">✔ Valid</div>` : `<div class="err">${r.errors.map(esc).join("<br>")}</div>`); if (r.warnings?.length) msg($("#smsg", el).innerHTML + `<div class="warn">${r.warnings.map(esc).join("<br>")}</div>`); } catch (e) { msg(errBox(e)); } };
  $("#ssave", el).onclick = async () => { try { const r = await api("/strategies", { method: "POST", body: { name: $("#sname", el).value, spec: spec() } }); msg(`<div class="up">Saved #${r.id}</div>`); await list(); open(r.id); } catch (e) { msg(errBox(e)); } };
  $("#supd", el).onclick = async () => { if (!cur) return msg(errBox("Open a strategy first")); try { const r = await api("/strategies/" + cur.id, { method: "PUT", body: { spec: spec(), note: "edited in UI" } }); msg(`<div class="up">Saved as v${r.version}</div>`); list(); open(cur.id); } catch (e) { msg(errBox(e)); } };
  $("#simp", el).onclick = () => $("#sfile", el).click();
  $("#sfile", el).onchange = async (ev) => { const f = ev.target.files[0]; if (f) { const t = await f.text(); try { JSON.parse(t); $("#sspec", el).value = t; msg('<div class="up">Imported into the editor — validate, then save.</div>'); } catch (e) { msg(errBox("Not valid JSON")); } } };
  $("#sdef", el).click(); list();
});

page("backtest", "Backtesting", async (el) => {
  const strategies = (await api("/strategies")).strategies;
  el.innerHTML = `<h2>Backtesting Engine</h2>${DISC}${strategies.length ? "" : '<div class="err">Create a strategy first.</div>'}<div class="row"><select id="bst" multiple size="3" title="select 2+ to compare">${strategies.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("")}</select><select id="bp">${opts([...CRYPTO, ...STOCK])}</select><input id="bs" value="BTCUSDT" style="width:130px"><select id="bt">${opts(TFS, "15m")}</select><input id="bl" type="number" value="1500" style="width:90px" title="candles"><input id="bc" type="number" value="100000" style="width:110px" title="capital"><input id="bf" type="number" step="0.01" value="0.05" style="width:80px" title="fee % per side"><input id="bsl" type="number" step="0.01" value="0.02" style="width:80px" title="slippage %"></div>
  <div class="row"><button id="brun" class="p">Run backtest</button><button id="bcmp">Compare selected</button></div><div id="bout"></div><div class="card" style="margin-top:14px"><h3>Saved runs</h3><div id="bruns"></div></div>`;
  const args = () => ({ provider: $("#bp", el).value, symbol: $("#bs", el).value, tf: $("#bt", el).value, limit: +$("#bl", el).value, capital: +$("#bc", el).value, fee_pct: +$("#bf", el).value, slippage_pct: +$("#bsl", el).value });
  const sel = () => [...$("#bst", el).selectedOptions].map((o) => +o.value);
  const statsHtml = (s) => `<div class="grid g4">${kpi("Net profit", money(s.net_profit))}${kpi("Return", pct(s.return_pct))}${kpi("Win rate", fmt(s.win_rate_pct, 1) + "%")}${kpi("Profit factor", fmt(s.profit_factor, 2))}${kpi("Max drawdown", fmt(s.max_drawdown_pct, 1) + "%")}${kpi("Trades", s.trades)}${kpi("Realized R/R", fmt(s.risk_reward_realized, 2))}${kpi("Fees paid", fmt(s.fees_paid, 2))}${kpi("Buy &amp; hold", pct(s.buy_and_hold_return_pct))}${kpi("Exposure", fmt(s.exposure_pct, 1) + "%")}</div>`;
  $("#brun", el).onclick = (ev) => busy(ev.target, async () => {
    const id = sel()[0]; if (!id) return toast("Select a strategy");
    try { const r = await api("/backtest", { method: "POST", body: { strategy_id: id, ...args() } });
      $("#bout", el).innerHTML = `${statsHtml(r.stats)}<div class="card" style="margin-top:12px"><h3>Equity curve</h3><canvas id="eq" style="width:100%;height:190px"></canvas></div><div class="card" style="margin-top:12px"><h3>Trade history (last 200)</h3>${table(["Side", "Entry", "Exit", "Entry px", "Exit px", "P&L", "R", "Reason"], r.trades.slice(-200).reverse().map((t) => `<tr><td><span class="tag ${t.side}">${t.side}</span></td><td>${when(t.entry_t)}</td><td>${when(t.exit_t)}</td><td>${fmt(t.entry, 4)}</td><td>${fmt(t.exit, 4)}</td><td>${money(t.pnl)}</td><td>${fmt(t.r_multiple, 2)}</td><td>${esc(t.exit_reason)}</td></tr>`))}</div><div class="disc">${esc(r.stats.disclaimer)} Data source: ${esc(r.data_source)}. ${esc(r.stats.profit_factor_note || "")}</div>`;
      lineChart($("#eq", el), r.equity.map((p) => ({ y: p.equity }))); runs(); } catch (e) { $("#bout", el).innerHTML = errBox(e); }
  });
  $("#bcmp", el).onclick = (ev) => busy(ev.target, async () => {
    try { const ids = sel(); const r = await api("/backtest/compare", { method: "POST", body: { strategy_ids: ids, ...args() } });
      $("#bout", el).innerHTML = `<div class="card"><h3>Strategy comparison</h3>${table(["Strategy", "Net", "Return", "Win%", "PF", "Max DD", "Trades"], r.results.map((x) => `<tr><td>${esc(x.name)}</td><td>${money(x.stats?.net_profit)}</td><td>${pct(x.stats?.return_pct)}</td><td>${fmt(x.stats?.win_rate_pct, 1)}</td><td>${fmt(x.stats?.profit_factor, 2)}</td><td>${fmt(x.stats?.max_drawdown_pct, 1)}%</td><td>${x.stats?.trades ?? esc(x.error || "")}</td></tr>`))}</div>`; } catch (e) { $("#bout", el).innerHTML = errBox(e); }
  });
  const runs = async () => { const r = await api("/backtest/runs"); $("#bruns", el).innerHTML = r.runs.length ? table(["#", "Strategy", "Symbol", "TF", "Net", "Trades", "When"], r.runs.map((x) => `<tr><td>${x.id}</td><td>${x.strategy_id}/v${x.version}</td><td>${esc(x.provider)}:${esc(x.symbol)}</td><td>${esc(x.tf)}</td><td>${money(x.stats.net_profit)}</td><td>${x.stats.trades}</td><td>${when(x.created_at * 1000)}</td></tr>`)) : '<span class="mut">No runs yet.</span>'; };
  runs();
});

page("paper", "Paper Trading", async (el) => {
  el.innerHTML = `<h2>Paper Trading <span class="pill">virtual money only</span></h2>${DISC}<div class="row"><input id="pn" placeholder="Account name" value="Paper 1" style="width:150px"><input id="pc" type="number" value="100000" style="width:120px"><button id="pcr" class="p">Create account</button><select id="pa"></select><button id="prf">Refresh fills</button></div><div id="pout"></div>`;
  const accounts = async (keep) => { const r = await api("/paper/accounts"); $("#pa", el).innerHTML = r.accounts.map((a) => `<option value="${a.id}">${esc(a.name)} · ${fmt(a.equity.equity, 0)}</option>`).join(""); if (keep) $("#pa", el).value = keep; if (r.accounts.length) show(); else $("#pout", el).innerHTML = '<span class="mut">No accounts.</span>'; };
  const aid = () => $("#pa", el).value;
  async function show() {
    try { const r = await api("/paper/accounts/" + aid()), s = r.stats;
      $("#pout", el).innerHTML = `<div class="grid g4">${kpi("Equity", fmt(r.equity.equity, 2))}${kpi("Return", pct(r.return_pct))}${kpi("Realized P&amp;L", money(r.realized_pnl))}${kpi("Unrealized", money(r.unrealized_pnl))}${kpi("Win rate", fmt(s.win_rate_pct, 1) + "%")}${kpi("Profit factor", fmt(s.profit_factor, 2))}${kpi("Closed trades", s.closed_trades)}${kpi("Fees", fmt(s.fees_paid, 2))}</div>
      <div class="card" style="margin-top:12px"><h3>Open a virtual position</h3><div class="row"><select id="xp">${opts([...CRYPTO, ...STOCK])}</select><input id="xs" value="BTCUSDT" style="width:130px"><select id="xd">${opts(["LONG", "SHORT"])}</select><input id="xsl" placeholder="Stop loss" style="width:100px"><input id="xt1" placeholder="Target 1" style="width:100px"><input id="xt2" placeholder="Target 2" style="width:100px"><input id="xr" type="number" value="1" step="0.1" style="width:70px" title="risk % of equity"><button id="xgo" class="p">Open at live price</button></div></div>
      <div class="card" style="margin-top:12px"><h3>Open positions</h3>${table(["Symbol", "Side", "Qty left", "Entry", "Stop", "Targets", "Last", "Unrealized", ""], r.open_positions.map((p) => `<tr><td>${esc(p.provider)}:${esc(p.symbol)}</td><td><span class="tag ${p.side}">${p.side}</span></td><td>${fmt(p.remaining, 6)}</td><td>${fmt(p.entry, 4)}</td><td>${fmt(p.stop_loss, 4)}</td><td>${p.targets.map((t) => fmt(t.price, 2)).join(" / ")}</td><td>${fmt(p.last_price, 4)}</td><td>${money(p.unrealized_pnl)}</td><td><button data-c="${p.id}">Close</button></td></tr>`))}</div>
      <div class="card" style="margin-top:12px"><h3>Closed positions</h3>${table(["Symbol", "Side", "Entry", "Opened", "Closed", "Net P&L", "Exits"], r.closed_positions.slice().reverse().map((p) => `<tr><td>${esc(p.symbol)}</td><td>${p.side}</td><td>${fmt(p.entry, 4)}</td><td>${when(p.opened_at * 1000)}</td><td>${when(p.closed_at * 1000)}</td><td>${money(p.net_pnl)}</td><td>${p.exits.map((x) => esc(x.reason)).join(", ")}</td></tr>`))}</div>`;
      $("#xgo", el).onclick = (ev) => busy(ev.target, async () => { try { const tg = [$("#xt1", el).value, $("#xt2", el).value].filter(Boolean).map(Number); const body = { provider: $("#xp", el).value, symbol: $("#xs", el).value, side: $("#xd", el).value, stop_loss: +$("#xsl", el).value, risk_pct: +$("#xr", el).value, targets: tg.map((p) => ({ price: p, qty_pct: 100 / tg.length })) }; await api(`/paper/accounts/${aid()}/positions`, { method: "POST", body }); show(); } catch (e) { toast(e.message); } });
      $$("[data-c]", el).forEach((b) => b.onclick = async () => { try { await api(`/paper/positions/${b.dataset.c}/close`, { method: "POST" }); show(); } catch (e) { toast(e.message); } });
    } catch (e) { $("#pout", el).innerHTML = errBox(e); }
  }
  $("#pcr", el).onclick = async () => { try { const r = await api("/paper/accounts", { method: "POST", body: { name: $("#pn", el).value, capital: +$("#pc", el).value } }); accounts(r.id); } catch (e) { toast(e.message); } };
  $("#pa", el).onchange = show;
  $("#prf", el).onclick = (ev) => busy(ev.target, async () => { try { const r = await api(`/paper/accounts/${aid()}/refresh`, { method: "POST" }); if (r.fills.length) toast(`${r.fills.length} virtual fill(s)`); if (r.errors.length) toast(r.errors[0].error); show(); } catch (e) { toast(e.message); } });
  accounts();
});

page("alerts", "Alerts", async (el) => {
  const PRESETS = { price: '{"condition":"above","value":70000}', indicator: '{"indicators":[{"id":"r","type":"rsi","params":{"period":14}}],"conditions":[{"left":"r","op":"<","right":30}]}', breakout: '{"lookback":20,"direction":"any"}', volume: '{"period":20,"multiplier":2}', strategy: '{"strategy_id":1}', stop_loss: '{"position_id":1}', target: '{"position_id":1}', custom: '{"indicators":[],"conditions":[]}' };
  el.innerHTML = `<h2>Alerts</h2>${DISC}<div class="card"><div class="row"><input id="an" placeholder="Name" style="width:150px"><select id="ak">${opts(Object.keys(PRESETS))}</select><select id="ap">${opts([...CRYPTO, ...STOCK])}</select><input id="as" value="BTCUSDT" style="width:130px"><select id="at">${opts(TFS, "15m")}</select></div><textarea id="aj" style="min-height:80px"></textarea><div class="row" style="margin-top:8px"><button id="acr" class="p">Create alert</button><button id="arun">Check now</button><button id="anotif">Enable browser notifications</button></div><div id="amsg"></div></div>
  <div class="grid g2" style="margin-top:14px"><div class="card"><h3>Active alerts</h3><div id="al"></div></div><div class="card"><h3>Triggered</h3><div id="ae"></div></div></div>`;
  const setp = () => { $("#aj", el).value = PRESETS[$("#ak", el).value]; }; $("#ak", el).onchange = setp; setp();
  const refresh = async () => { const [a, e] = await Promise.all([api("/alerts"), api("/alerts/events")]);
    $("#al", el).innerHTML = (a.server_notifications ? "" : '<div class="mut">Server notifications (webhook/Telegram) not configured — in-app and browser only.</div>') + table(["Name", "Type", "Symbol", "On", ""], a.alerts.map((x) => `<tr><td>${esc(x.name)}</td><td>${esc(x.kind)}</td><td>${esc(x.symbol || "")}</td><td><input type="checkbox" data-t="${x.id}" ${x.enabled ? "checked" : ""}></td><td><button class="d" data-d="${x.id}">✕</button></td></tr>`));
    $("#ae", el).innerHTML = table(["When", "Message"], e.events.map((x) => `<tr><td>${when(x.created_at * 1000)}</td><td style="white-space:normal;text-align:left">${esc(x.message)}</td></tr>`));
    $$("[data-d]", el).forEach((b) => b.onclick = async () => { await api("/alerts/" + b.dataset.d, { method: "DELETE" }); refresh(); });
    $$("[data-t]", el).forEach((b) => b.onchange = async () => { await api("/alerts/" + b.dataset.t, { method: "PUT", body: { enabled: b.checked } }); });
    api("/alerts/events/seen", { method: "POST" }).then(() => $("#unseen").textContent = "0"); };
  $("#acr", el).onclick = async () => { try { const k = $("#ak", el).value; await api("/alerts", { method: "POST", body: { name: $("#an", el).value || k, kind: k, provider: $("#ap", el).value, symbol: $("#as", el).value, tf: $("#at", el).value, params: JSON.parse($("#aj", el).value) } }); $("#amsg", el).innerHTML = '<div class="up">Created</div>'; refresh(); } catch (e) { $("#amsg", el).innerHTML = errBox(e); } };
  $("#arun", el).onclick = (ev) => busy(ev.target, async () => { const r = await api("/alerts/run", { method: "POST" }); toast(`${r.triggered} alert(s) triggered`); refresh(); });
  $("#anotif", el).onclick = () => enableNotifications();
  refresh();
});

page("learning", "AI Learning", async (el) => {
  const strategies = (await api("/strategies")).strategies;
  el.innerHTML = `<h2>AI Learning System</h2><div class="disc">The AI analyses past trades, proposes rule changes, and tests each one on <b>unseen data</b>. Nothing is applied until <b>you</b> approve it, and only proposals validated out-of-sample can be approved. Even then, confirm in paper trading — many candidates are tried, so a pass can still be luck.</div>
  <div class="row"><select id="ls">${strategies.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("")}</select><select id="lp">${opts([...CRYPTO, ...STOCK])}</select><input id="lsym" value="BTCUSDT" style="width:130px"><select id="lt">${opts(TFS, "15m")}</select><input id="ll" type="number" value="3000" style="width:90px"><button id="lrun" class="p">Analyse &amp; propose</button></div><div id="lout"></div>
  <div class="card" style="margin-top:14px"><h3>Proposals</h3><div id="lprops"></div></div><div class="card" style="margin-top:14px"><h3>Audit history</h3><div id="laud"></div></div>`;
  const verdictCls = (v) => v === "VALIDATED_OUT_OF_SAMPLE" ? "up" : v === "NOT_VALIDATED" ? "down" : "warn";
  const props = async () => {
    const sid = $("#ls", el).value; if (!sid) return;
    const [p, a] = await Promise.all([api("/learning/proposals", { params: { strategy_id: sid } }), api("/audit")]);
    $("#lprops", el).innerHTML = p.proposals.length ? table(["#", "Change", "Verdict", "Status", "Test net", "Test PF", ""], p.proposals.map((x) => { const d = x.detail || {}; return `<tr><td>${x.id}</td><td style="white-space:normal;text-align:left">${esc(x.note)}<div class="mut">${esc(d.rationale || "")}</div></td><td class="${verdictCls(x.verdict)}">${esc(x.verdict)}<div class="mut" style="white-space:normal">${(d.reasons || []).map(esc).join("; ")}</div></td><td>${esc(x.status)}</td><td>${fmt(d.test?.net_profit, 0)}</td><td>${fmt(d.test?.profit_factor, 2)}</td><td>${x.status === "validated" ? `<button class="p" data-a="${x.id}">Approve</button> <button data-j="${x.id}">Reject</button>` : x.status === "applied" ? `<button class="d" data-b="${x.id}">Rollback</button>` : ""}</td></tr>`; })) : '<span class="mut">No proposals yet.</span>';
    $$("[data-a]", el).forEach((b) => b.onclick = async () => { if (!confirm("Apply this validated change as the active version?")) return; try { await api(`/learning/proposals/${b.dataset.a}/approve`, { method: "POST" }); toast("Applied"); props(); } catch (e) { toast(e.message); } });
    $$("[data-j]", el).forEach((b) => b.onclick = async () => { await api(`/learning/proposals/${b.dataset.j}/reject`, { method: "POST" }); props(); });
    $$("[data-b]", el).forEach((b) => b.onclick = async () => { await api(`/learning/proposals/${b.dataset.b}/rollback`, { method: "POST" }); toast("Rolled back"); props(); });
    $("#laud", el).innerHTML = table(["When", "Actor", "Action", "Entity"], a.audit.slice(0, 40).map((x) => `<tr><td>${when(x.created_at * 1000)}</td><td>${esc(x.actor)}</td><td>${esc(x.action)}</td><td>${esc(x.entity || "")} ${esc(x.entity_id || "")}</td></tr>`));
  };
  $("#ls", el).onchange = props;
  $("#lrun", el).onclick = (ev) => busy(ev.target, async () => {
    try { const r = await api("/learning/run", { method: "POST", body: { strategy_id: +$("#ls", el).value, provider: $("#lp", el).value, symbol: $("#lsym", el).value, tf: $("#lt", el).value, limit: +$("#ll", el).value } });
      const sm = (x) => `net ${fmt(x.net_profit, 0)} · PF ${fmt(x.profit_factor, 2)} · win ${fmt(x.win_rate_pct, 0)}% · ${x.trades} trades`;
      $("#lout", el).innerHTML = `<div class="card"><h3>Result · ${esc(r.status)} · ${r.candidates_tested} candidates tested</h3><div>Baseline train: ${sm(r.baseline.train)}</div><div>Baseline test (unseen): ${sm(r.baseline.test)}</div>${(r.analysis?.weaknesses || []).map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join("")}${r.notes.map((n) => `<div class="mut">${esc(n)}</div>`).join("")}</div>`; props();
    } catch (e) { $("#lout", el).innerHTML = errBox(e); }
  });
  props();
});

page("system", "System & Diagnostics", async (el) => {
  el.innerHTML = `<h2>System &amp; Diagnostics</h2><div id="sys"></div>`;
  const load = async () => {
    try { const d = await api("/diagnostics");
      $("#sys", el).innerHTML = `<div class="grid g4">${kpi("Version", esc(d.version))}${kpi("Database", d.database_ok ? '<span class="up">OK</span>' : '<span class="down">PROBLEM</span>')}${kpi("Scheduler", d.scheduler.running ? '<span class="up">running</span>' : '<span class="warn">off</span>')}${kpi("Mode", "read-only")}</div>
      <div class="card" style="margin-top:12px"><h3>Providers</h3>${table(["Provider", "State", "Latency", "Calls", "Errors", "Last error", "Capabilities"], d.providers.map((p) => `<tr><td>${esc(p.label)}</td><td><span class="dot ${p.state}" style="display:inline-block"></span> ${esc(p.state)}</td><td>${p.latency_ms ?? "—"}</td><td>${p.calls}</td><td>${p.errors}</td><td style="white-space:normal;text-align:left">${esc(p.last_error || "")}</td><td>${p.capabilities.join(", ")}</td></tr>`))}</div>
      <div class="card" style="margin-top:12px"><h3>Backup &amp; restore</h3><div class="row"><button id="bk" class="p">Create backup now</button></div>${table(["Backup", "Size", "Created", ""], (await api("/backup")).backups.map((b) => `<tr><td>${esc(b.name)}</td><td>${fmt(b.size / 1024, 0)} KB</td><td>${when(b.created_at * 1000)}</td><td><button class="d" data-r="${esc(b.name)}">Restore</button></td></tr>`))}</div>
      <div class="card" style="margin-top:12px"><h3>Recent provider errors</h3>${table(["When", "Provider", "Message"], d.recent_provider_errors.map((x) => `<tr><td>${when(x.created_at * 1000)}</td><td>${esc(x.provider)}</td><td style="white-space:normal;text-align:left">${esc(x.message)}</td></tr>`))}</div>`;
      $("#bk", el).onclick = async () => { const r = await api("/backup", { method: "POST" }); toast("Created " + r.created); load(); };
      $$("[data-r]", el).forEach((b) => b.onclick = async () => { if (confirm("Restore this backup? A safety snapshot of the current data is taken first.")) { const r = await api("/backup/restore", { method: "POST", body: { name: b.dataset.r } }); toast("Restored. Safety snapshot: " + r.safety_snapshot); load(); } });
    } catch (e) { $("#sys", el).innerHTML = errBox(e); }
  };
  load();
});

/* ------------------------------------------------------------------ shell */
let lastEvent = 0;
async function enableNotifications() {
  if (!("Notification" in window)) return toast("Notifications are not supported in this browser");
  const p = await Notification.requestPermission(); toast(p === "granted" ? "Browser notifications enabled" : "Notifications " + p);
}
async function pollStatus() {
  try { const p = await api("/providers"); $("#provs").innerHTML = p.providers.map((x) => `<span class="pill" title="${esc(x.last_error || x.state)}"><span class="dot ${x.state}"></span>${esc(x.label)}</span>`).join(""); } catch (e) { /* offline */ }
  try {
    const r = await api("/alerts/events", { params: { since_id: lastEvent } });
    $("#unseen").textContent = r.unseen;
    r.events.slice().reverse().forEach((e) => { if (lastEvent) { toast(e.message); if (window.Notification && Notification.permission === "granted") new Notification("BTC BRAIN NEXUS", { body: e.message }); } });
    if (r.events.length) lastEvent = Math.max(lastEvent, ...r.events.map((e) => e.id)); else if (!lastEvent) lastEvent = 0.0001;
  } catch (e) { /* ignore */ }
}
let timer = null;
async function route() {
  cleanup.forEach((f) => { try { f(); } catch (e) { /* ignore */ } }); cleanup = [];
  const id = (location.hash.slice(1).split("?")[0]) || "dashboard", p = PAGES.find((x) => x.id === id) || PAGES[0];
  $$("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.id === p.id)); $("#nav").classList.remove("open");
  const m = $("#main"); m.innerHTML = '<span class="spin"></span>';
  try { await p.render(m); } catch (e) { m.innerHTML = `<h2>${esc(p.title)}</h2>${errBox(e)}`; }
}
function showApp() {
  $("#login").classList.add("hide"); $("#app").classList.remove("hide");
  $("#nav").innerHTML = PAGES.map((p) => `<a href="#${p.id}" data-id="${p.id}">${p.title}</a>`).join("");
  route(); pollStatus(); clearInterval(timer); timer = setInterval(pollStatus, 30000);
}
function logout() { TOKEN = ""; localStorage.removeItem("nexusToken"); clearInterval(timer); $("#app").classList.add("hide"); $("#login").classList.remove("hide"); }
$("#lbase").value = BASE;
$("#lgo").onclick = async (ev) => busy(ev.target, async () => {
  BASE = $("#lbase").value.trim().replace(/\/+$/, "") || "/api"; localStorage.setItem("nexusBase", BASE);
  try { const r = await api("/auth/login", { method: "POST", body: { username: $("#luser").value, password: $("#lpass").value } }); TOKEN = r.token; localStorage.setItem("nexusToken", TOKEN); $("#lerr").classList.add("hide"); showApp(); }
  catch (e) { $("#lerr").textContent = e.message; $("#lerr").classList.remove("hide"); }
});
$("#lpass").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#lgo").click(); });
$("#logout").onclick = logout; $("#burger").onclick = () => $("#nav").classList.toggle("open"); $("#notif").onclick = () => { enableNotifications(); location.hash = "#alerts"; };
window.addEventListener("hashchange", () => { if (TOKEN) route(); });
(async () => { if (TOKEN) { try { await api("/auth/me"); showApp(); return; } catch (e) { /* fallthrough */ } } $("#login").classList.remove("hide"); })();
})();
