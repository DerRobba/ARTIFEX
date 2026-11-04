/* ARTIFEX – G-code layer preview (top view, 2D canvas). */
(function () {
  "use strict";
  const COLORS = {
    "Outer wall": "#ff6b4a", "Inner wall": "#ffb347", "Solid infill": "#a46bff", "Sparse infill": "#4ea4ff",
    "Support": "#3ccf7a", "Skirt": "#8a93a6", "Brim": "#2ec4c4",
  };

  function parse(text) {
    const layers = [];
    let cur = null, x = 0, y = 0, type = "", z = 0;
    const lines = text.split("\n");
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      const c0 = ln.charCodeAt(0);
      if (c0 === 59) { // ';'
        if (ln.startsWith(";LAYER_CHANGE")) { cur = { z: 0, segs: [] }; layers.push(cur); }
        else if (ln.startsWith(";Z:") && cur) cur.z = parseFloat(ln.slice(3));
        else if (ln.startsWith(";TYPE:")) type = ln.slice(6).trim();
        continue;
      }
      if (c0 !== 71) continue; // 'G'
      if (!(ln.startsWith("G1 ") || ln.startsWith("G0 "))) continue;
      let nx = x, ny = y, e = 0;
      const parts = ln.split(" ");
      for (let k = 1; k < parts.length; k++) {
        const p = parts[k], v = parseFloat(p.slice(1));
        switch (p[0]) { case "X": nx = v; break; case "Y": ny = v; break; case "E": e = v; break; case "Z": z = v; break; case ";": k = parts.length; break; }
      }
      if (cur && e > 0 && (nx !== x || ny !== y)) cur.segs.push(x, y, nx, ny, type);
      x = nx; y = ny;
    }
    return layers.filter((l) => l.segs.length);
  }

  class LayerView {
    constructor(canvas, slider, label) {
      this.canvas = canvas; this.slider = slider; this.label = label; this.layers = []; this.bed = [235, 235];
      slider.addEventListener("input", () => this.draw());
      new ResizeObserver(() => this.draw()).observe(canvas);
    }
    async load(url) {
      this.label.textContent = "lädt…";
      const r = await fetch(url, { cache: "no-store" });
      if (!r.ok) throw new Error("G-Code " + r.status);
      this.layers = parse(await r.text());
      let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
      for (const L of this.layers) for (let i = 0; i < L.segs.length; i += 5) {
        minx = Math.min(minx, L.segs[i], L.segs[i + 2]); maxx = Math.max(maxx, L.segs[i], L.segs[i + 2]);
        miny = Math.min(miny, L.segs[i + 1], L.segs[i + 3]); maxy = Math.max(maxy, L.segs[i + 1], L.segs[i + 3]);
      }
      this.box = [minx, miny, maxx, maxy];
      this.slider.max = Math.max(1, this.layers.length);
      this.slider.value = Math.max(1, Math.round(this.layers.length / 2));
      this.draw();
    }
    draw() {
      const c = this.canvas, dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = c.clientWidth * dpr | 0, h = c.clientHeight * dpr | 0;
      if (!w || !h) return;
      c.width = w; c.height = h;
      const g = c.getContext("2d");
      g.clearRect(0, 0, w, h);
      if (!this.layers.length) return;
      const idx = Math.min(this.layers.length, +this.slider.value) - 1;
      const [minx, miny, maxx, maxy] = this.box;
      const pad = 16 * dpr;
      const s = Math.min((w - 2 * pad) / Math.max(maxx - minx, 1), (h - 2 * pad) / Math.max(maxy - miny, 1));
      const ox = (w - (maxx - minx) * s) / 2, oy = (h - (maxy - miny) * s) / 2;
      const X = (x) => ox + (x - minx) * s, Y = (y) => h - (oy + (y - miny) * s);
      g.lineCap = "round";
      // previous layer faint
      if (idx > 0) this._layer(g, this.layers[idx - 1], X, Y, Math.max(1, 0.4 * s), "rgba(255,255,255,0.08)");
      this._layer(g, this.layers[idx], X, Y, Math.max(1, 0.42 * s), null);
      this.label.textContent = `${idx + 1}/${this.layers.length} · Z ${this.layers[idx].z.toFixed(2)}`;
    }
    _layer(g, L, X, Y, lw, fixed) {
      g.lineWidth = lw;
      let last = null;
      for (let i = 0; i < L.segs.length; i += 5) {
        const col = fixed || COLORS[L.segs[i + 4]] || "#ddd";
        if (col !== last) { if (last) g.stroke(); g.beginPath(); g.strokeStyle = col; last = col; }
        g.moveTo(X(L.segs[i]), Y(L.segs[i + 1])); g.lineTo(X(L.segs[i + 2]), Y(L.segs[i + 3]));
      }
      if (last) g.stroke();
    }
  }
  window.ArtifexLayers = LayerView;
})();
