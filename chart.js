/* Canvas candlestick chart: zoom (wheel/pinch), scroll (drag), crosshair, volume, overlays,
   sub-panes, support/resistance, trend lines (auto + user drawn), entry/SL/target levels, fibonacci. No dependencies. */
(function () {
  const C = { up: "#26d07c", down: "#ff5c6c", grid: "#1b2431", text: "#8b98a9", bg: "#0a0e14", acc: "#f7931a", blue: "#4cc2ff" };
  const PALETTE = ["#f7931a", "#4cc2ff", "#c792ea", "#ffd166", "#06d6a0", "#ef476f", "#8ecae6"];
  const AXIS_W = 66, TIME_H = 22, SUB_H = 110;

  const fmtP = (p) => (p == null || !isFinite(p)) ? "" : (Math.abs(p) >= 1000 ? p.toFixed(1) : Math.abs(p) >= 1 ? p.toFixed(3) : p.toPrecision(4));
  const pad2 = (n) => String(n).padStart(2, "0");
  function fmtT(ms, daily) {
    const d = new Date(ms);
    return daily ? `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
      : `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  }

  class CandleChart {
    constructor(box) {
      this.box = box;
      this.cv = document.createElement("canvas");
      box.appendChild(this.cv);
      this.ctx = this.cv.getContext("2d");
      this.d = { candles: [], overlays: {}, sr: null, tl: null, fib: null };
      this.levels = []; this.user = []; this.pending = null; this.drawMode = false;
      this.view = { n: 120, off: 0 }; this.cross = null; this.pointers = new Map(); this.sub = [];
      this._bind();
      this.ro = new ResizeObserver(() => this.resize());
      this.ro.observe(box);
      this.resize();
    }
    destroy() { this.ro.disconnect(); this.cv.remove(); }
    setData(res) {
      this.d = { candles: res.candles || [], overlays: res.overlays || {}, sr: res.support_resistance, tl: res.trend_lines, fib: res.fibonacci };
      this.daily = (this.d.candles.length > 1 && this.d.candles[1].t - this.d.candles[0].t >= 86400000);
      this.sub = Object.entries(this.d.overlays).filter(([, o]) => o.pane === "sub").map(([k]) => k).slice(0, 2);
      this.view.n = Math.min(Math.max(this.view.n, 20), Math.max(this.d.candles.length, 20));
      this.view.off = 0; this.user = []; this.draw();
    }
    setLevels(levels) { this.levels = levels || []; this.draw(); }
    setDrawMode(on) { this.drawMode = on; this.pending = null; this.cv.style.cursor = on ? "crosshair" : "default"; }
    clearUser() { this.user = []; this.pending = null; this.draw(); }
    resize() {
      const r = this.box.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
      this.W = Math.max(r.width, 200); this.H = Math.max(r.height, 200);
      this.cv.width = this.W * dpr; this.cv.height = this.H * dpr;
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0); this.draw();
    }
    _range() {
      const n = this.d.candles.length, cnt = Math.min(this.view.n, n);
      const e = n - Math.max(0, Math.min(this.view.off, n - cnt)); return [e - cnt, e];
    }
    _bind() {
      const cv = this.cv, pos = (ev) => { const r = cv.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; };
      cv.addEventListener("wheel", (ev) => {
        ev.preventDefault();
        const f = ev.deltaY > 0 ? 1.12 : 0.89, n = this.d.candles.length;
        this.view.n = Math.round(Math.min(Math.max(this.view.n * f, 15), Math.max(n, 15))); this.draw();
      }, { passive: false });
      cv.addEventListener("pointerdown", (ev) => {
        cv.setPointerCapture(ev.pointerId); this.pointers.set(ev.pointerId, pos(ev));
        this.dragX = pos(ev).x; this.dragOff = this.view.off; this.moved = false; this.pinch = null;
      });
      cv.addEventListener("pointermove", (ev) => {
        const p = pos(ev); this.cross = p;
        if (this.pointers.has(ev.pointerId)) this.pointers.set(ev.pointerId, p);
        if (this.pointers.size === 2) {
          const [a, b] = [...this.pointers.values()], d = Math.hypot(a.x - b.x, a.y - b.y);
          if (this.pinch) { this.view.n = Math.round(Math.min(Math.max(this.view.n * this.pinch / d, 15), Math.max(this.d.candles.length, 15))); }
          this.pinch = d; this.moved = true;
        } else if (this.pointers.size === 1) {
          const bw = (this.W - AXIS_W) / Math.min(this.view.n, this.d.candles.length || 1);
          const dx = p.x - this.dragX;
          if (Math.abs(dx) > 3) this.moved = true;
          this.view.off = Math.max(0, Math.round(this.dragOff + dx / bw));
        }
        this.draw();
      });
      const up = (ev) => {
        this.pointers.delete(ev.pointerId); this.pinch = null;
        if (!this.moved && this.drawMode && ev.type === "pointerup") this._addPoint(pos(ev));
      };
      cv.addEventListener("pointerup", up); cv.addEventListener("pointercancel", up);
      cv.addEventListener("pointerleave", () => { this.cross = null; this.draw(); });
      cv.addEventListener("dblclick", () => { this.view.off = 0; this.view.n = 120; this.draw(); });
    }
    _addPoint(p) {
      if (!this.geo) return;
      const [s, e] = this._range(), bw = this.geo.bw;
      const i = Math.min(e - 1, Math.max(s, Math.floor((p.x - this.geo.left) / bw) + s));
      const pt = { i, p: this.geo.yToP(p.y) };
      if (!this.pending) this.pending = pt; else { this.user.push([this.pending, pt]); this.pending = null; }
      this.draw();
    }
    draw() {
      const g = this.ctx, W = this.W, H = this.H, cs = this.d.candles;
      g.clearRect(0, 0, W, H); g.fillStyle = C.bg; g.fillRect(0, 0, W, H);
      if (!cs.length) { g.fillStyle = C.text; g.fillText("No data", 20, 30); return; }
      const [s, e] = this._range(), vis = cs.slice(s, e), n = e - s;
      const subH = this.sub.length * SUB_H, top = 8, priceH = H - TIME_H - subH - top, left = 0, plotW = W - AXIS_W, bw = plotW / n;
      let lo = Math.min(...vis.map((c) => c.l)), hi = Math.max(...vis.map((c) => c.h)); const padv = (hi - lo) * 0.06 || hi * 0.01; lo -= padv; hi += padv;
      const volMax = Math.max(...vis.map((c) => c.v)) || 1;
      const yP = (p) => top + (hi - p) / (hi - lo) * priceH, x = (i) => left + (i - s + 0.5) * bw;
      this.geo = { left, bw, yToP: (y) => hi - (y - top) / priceH * (hi - lo) };
      // grid + price axis
      g.font = "11px ui-monospace,monospace"; g.textBaseline = "middle"; g.lineWidth = 1;
      for (let k = 0; k <= 6; k++) {
        const p = lo + (hi - lo) * k / 6, y = yP(p);
        g.strokeStyle = C.grid; g.beginPath(); g.moveTo(0, y); g.lineTo(plotW, y); g.stroke();
        g.fillStyle = C.text; g.textAlign = "left"; g.fillText(fmtP(p), plotW + 6, y);
      }
      // time axis
      g.textAlign = "center"; g.textBaseline = "top";
      const step = Math.max(1, Math.round(n / Math.max(2, Math.floor(plotW / 110))));
      for (let i = s; i < e; i += step) { g.fillStyle = C.text; g.fillText(fmtT(cs[i].t, this.daily), x(i), H - TIME_H + 5); }
      // volume
      for (let i = s; i < e; i++) {
        const c = cs[i], h = c.v / volMax * priceH * 0.16;
        g.fillStyle = c.c >= c.o ? "#26d07c30" : "#ff5c6c30"; g.fillRect(x(i) - bw * 0.35, top + priceH - h, bw * 0.7, h);
      }
      g.save(); g.beginPath(); g.rect(0, top, plotW, priceH); g.clip();
      this._fib(g, yP, plotW); this._sr(g, yP, plotW); this._lines(g, x, yP, s, e, plotW);
      // candles
      for (let i = s; i < e; i++) {
        const c = cs[i], up = c.c >= c.o, col = up ? C.up : C.down, cx = x(i);
        g.strokeStyle = col; g.fillStyle = col; g.beginPath(); g.moveTo(cx, yP(c.h)); g.lineTo(cx, yP(c.l)); g.stroke();
        const y1 = yP(Math.max(c.o, c.c)), y2 = yP(Math.min(c.o, c.c)); g.fillRect(cx - bw * 0.35, y1, Math.max(bw * 0.7, 1), Math.max(y2 - y1, 1));
      }
      this._overlays(g, x, yP, s, e);
      this._levels(g, yP, plotW);
      g.restore();
      // sub panes
      this.sub.forEach((name, k) => this._subpane(g, name, k, s, e, x, plotW, top + priceH + k * SUB_H));
      // last price tag
      const last = cs[cs.length - 1].c, ly = yP(last);
      if (ly > top && ly < top + priceH) { g.fillStyle = last >= cs[cs.length - 1].o ? C.up : C.down; g.fillRect(plotW, ly - 9, AXIS_W, 18); g.fillStyle = "#000"; g.textAlign = "left"; g.textBaseline = "middle"; g.fillText(fmtP(last), plotW + 6, ly); }
      this._cross(g, s, e, x, plotW, top, priceH, lo, hi);
    }
    _fib(g, yP, w) {
      if (!this.d.fib) return; g.setLineDash([2, 4]); g.font = "10px ui-monospace,monospace";
      Object.entries(this.d.fib).forEach(([k, p], i) => {
        const y = yP(p); g.strokeStyle = "#c792ea66"; g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke();
        g.fillStyle = "#c792ea"; g.textAlign = "left"; g.textBaseline = "bottom"; g.fillText(`fib ${k}`, 4, y - 1);
      }); g.setLineDash([]);
    }
    _sr(g, yP, w) {
      const sr = this.d.sr; if (!sr) return; g.setLineDash([6, 4]); g.font = "10px ui-monospace,monospace";
      [["support", C.up], ["resistance", C.down]].forEach(([k, col]) => (sr[k] || []).forEach((z) => {
        const y = yP(z.price); g.strokeStyle = col + "88"; g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke();
        g.fillStyle = col; g.textAlign = "right"; g.textBaseline = "bottom"; g.fillText(`${k[0].toUpperCase()} ${fmtP(z.price)} ×${z.touches || ""}`, w - 4, y - 1);
      })); g.setLineDash([]);
    }
    _lines(g, x, yP, s, e, w) {
      const ext = (a, b) => { const dx = x(b.i) - x(a.i), sl = dx ? (yP(b.p) - yP(a.p)) / dx : 0; return [x(a.i), yP(a.p), w, yP(a.p) + sl * (w - x(a.i))]; };
      const seg = (a, b, col, dash) => { const [x1, y1, x2, y2] = ext(a, b); g.strokeStyle = col; g.setLineDash(dash || []); g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke(); g.setLineDash([]); };
      const tl = this.d.tl || {};
      ["support", "resistance"].forEach((k) => { const t = tl[k]; if (t && t.i1 != null) seg({ i: t.i1, p: t.p1 }, { i: t.i2, p: t.p2 }, k === "support" ? "#26d07caa" : "#ff5c6caa", [3, 3]); });
      this.user.forEach(([a, b]) => seg(a, b, C.blue)); g.lineWidth = 1;
      if (this.pending) { g.fillStyle = C.blue; g.beginPath(); g.arc(x(this.pending.i), yP(this.pending.p), 4, 0, 7); g.fill(); }
    }
    _overlays(g, x, yP, s, e) {
      let ci = 0; g.lineWidth = 1.3;
      Object.entries(this.d.overlays).forEach(([name, o]) => {
        if (o.pane !== "price") return;
        const dir = o.series.direction;
        Object.entries(o.series).forEach(([key, arr]) => {
          if (key === "direction" || key === "width") return;
          const col = PALETTE[ci++ % PALETTE.length]; g.strokeStyle = col; g.beginPath(); let on = false;
          for (let i = s; i < e; i++) {
            const v = arr[i]; if (v == null) { on = false; continue; }
            if (name === "supertrend" && dir) g.strokeStyle = dir[i] === 1 ? C.up : C.down;
            on ? g.lineTo(x(i), yP(v)) : g.moveTo(x(i), yP(v)); on = true;
          } g.stroke();
        });
      }); g.lineWidth = 1;
    }
    _levels(g, yP, w) {
      g.setLineDash([8, 3]); g.font = "11px ui-monospace,monospace";
      this.levels.forEach((l) => {
        if (l.price == null || !isFinite(l.price)) return; const y = yP(l.price);
        g.strokeStyle = l.color; g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke();
        g.fillStyle = l.color; g.textAlign = "left"; g.textBaseline = "bottom"; g.fillText(`${l.label} ${fmtP(l.price)}`, 6, y - 2);
      }); g.setLineDash([]);
    }
    _subpane(g, name, k, s, e, x, plotW, y0) {
      const o = this.d.overlays[name], H = SUB_H - 8; let lo = Infinity, hi = -Infinity;
      const keys = Object.keys(o.series).filter((q) => q !== "direction" && q !== "width");
      keys.forEach((key) => { for (let i = s; i < e; i++) { const v = o.series[key][i]; if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } } });
      if (name === "rsi" || name === "stoch") { lo = Math.min(lo, 0); hi = Math.max(hi, 100); }
      if (!isFinite(lo)) return; if (hi === lo) { hi += 1; lo -= 1; }
      const y = (v) => y0 + 4 + (hi - v) / (hi - lo) * H;
      g.strokeStyle = C.grid; g.beginPath(); g.moveTo(0, y0); g.lineTo(plotW + AXIS_W, y0); g.stroke();
      g.fillStyle = C.text; g.font = "11px ui-monospace,monospace"; g.textAlign = "left"; g.textBaseline = "top"; g.fillText(name.toUpperCase(), 6, y0 + 4);
      const guides = name === "rsi" ? [30, 70] : name === "stoch" ? [20, 80] : name === "adx" ? [25] : [];
      g.setLineDash([3, 3]); guides.forEach((v) => { g.strokeStyle = "#ffffff22"; g.beginPath(); g.moveTo(0, y(v)); g.lineTo(plotW, y(v)); g.stroke(); }); g.setLineDash([]);
      let ci = 0; const bw = plotW / (e - s);
      keys.forEach((key) => {
        const arr = o.series[key];
        if (key === "hist") { for (let i = s; i < e; i++) { const v = arr[i]; if (v == null) continue; g.fillStyle = v >= 0 ? "#26d07c88" : "#ff5c6c88"; g.fillRect(x(i) - bw * 0.3, Math.min(y(0), y(v)), bw * 0.6, Math.abs(y(v) - y(0)) || 1); } return; }
        g.strokeStyle = PALETTE[ci++ % PALETTE.length]; g.beginPath(); let on = false;
        for (let i = s; i < e; i++) { const v = arr[i]; if (v == null) { on = false; continue; } on ? g.lineTo(x(i), y(v)) : g.moveTo(x(i), y(v)); on = true; } g.stroke();
      });
      g.fillStyle = C.text; g.textAlign = "left"; g.textBaseline = "middle"; g.fillText(fmtP(hi), plotW + 6, y(hi) + 6); g.fillText(fmtP(lo), plotW + 6, y(lo) - 6);
    }
    _cross(g, s, e, x, plotW, top, priceH, lo, hi) {
      const c = this.cross, cs = this.d.candles; if (!c || c.x > plotW || c.y < 0) return;
      const bw = plotW / (e - s), i = Math.min(e - 1, Math.max(s, Math.floor(c.x / bw) + s)), cd = cs[i];
      g.strokeStyle = "#ffffff44"; g.setLineDash([4, 4]); g.beginPath(); g.moveTo(x(i), 0); g.lineTo(x(i), this.H - TIME_H); g.moveTo(0, c.y); g.lineTo(plotW, c.y); g.stroke(); g.setLineDash([]);
      if (c.y >= top && c.y <= top + priceH) { const p = hi - (c.y - top) / priceH * (hi - lo); g.fillStyle = "#2a3647"; g.fillRect(plotW, c.y - 9, AXIS_W, 18); g.fillStyle = "#fff"; g.textAlign = "left"; g.textBaseline = "middle"; g.fillText(fmtP(p), plotW + 6, c.y); }
      g.fillStyle = "#2a3647"; g.fillRect(Math.min(x(i) - 50, plotW - 100), this.H - TIME_H, 100, TIME_H - 2); g.fillStyle = "#fff"; g.textAlign = "center"; g.textBaseline = "middle"; g.fillText(fmtT(cd.t, this.daily), Math.min(x(i), plotW - 50), this.H - TIME_H / 2);
      g.textAlign = "left"; g.textBaseline = "top"; g.fillStyle = cd.c >= cd.o ? C.up : C.down;
      g.fillText(`O ${fmtP(cd.o)}  H ${fmtP(cd.h)}  L ${fmtP(cd.l)}  C ${fmtP(cd.c)}  V ${cd.v.toFixed(2)}`, 8, 10);
    }
  }
  window.CandleChart = CandleChart;
})();
