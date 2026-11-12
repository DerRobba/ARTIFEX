/* ARTIFEX – tiny dependency-free WebGL STL viewer (works offline on the Pi). */
(function () {
  "use strict";

  function parseSTL(buf) {
    const dv = new DataView(buf);
    const isBinary = buf.byteLength >= 84 && 84 + dv.getUint32(80, true) * 50 === buf.byteLength;
    if (isBinary) {
      const n = dv.getUint32(80, true);
      const pos = new Float32Array(n * 9);
      for (let i = 0; i < n; i++) {
        const o = 84 + i * 50 + 12;
        for (let k = 0; k < 9; k++) pos[i * 9 + k] = dv.getFloat32(o + k * 4, true);
      }
      return pos;
    }
    const text = new TextDecoder().decode(buf);
    const nums = [];
    const re = /vertex\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)/g;
    let m;
    while ((m = re.exec(text))) nums.push(+m[1], +m[2], +m[3]);
    return new Float32Array(nums);
  }

  function flatNormals(pos) {
    const nrm = new Float32Array(pos.length);
    for (let i = 0; i < pos.length; i += 9) {
      const ax = pos[i + 3] - pos[i], ay = pos[i + 4] - pos[i + 1], az = pos[i + 5] - pos[i + 2];
      const bx = pos[i + 6] - pos[i], by = pos[i + 7] - pos[i + 1], bz = pos[i + 8] - pos[i + 2];
      let nx = ay * bz - az * by, ny = az * bx - ax * bz, nz = ax * by - ay * bx;
      const l = Math.hypot(nx, ny, nz) || 1;
      nx /= l; ny /= l; nz /= l;
      for (let k = 0; k < 3; k++) { nrm[i + k * 3] = nx; nrm[i + k * 3 + 1] = ny; nrm[i + k * 3 + 2] = nz; }
    }
    return nrm;
  }

  // --- minimal mat4 (column major) ---
  function perspective(fovy, aspect, near, far) {
    const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
    return [f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0];
  }
  function lookAt(e, c, up) {
    let zx = e[0] - c[0], zy = e[1] - c[1], zz = e[2] - c[2];
    let l = Math.hypot(zx, zy, zz); zx /= l; zy /= l; zz /= l;
    let xx = up[1] * zz - up[2] * zy, xy = up[2] * zx - up[0] * zz, xz = up[0] * zy - up[1] * zx;
    l = Math.hypot(xx, xy, xz) || 1; xx /= l; xy /= l; xz /= l;
    const yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
    return [xx, yx, zx, 0, xy, yy, zy, 0, xz, yz, zz, 0,
      -(xx * e[0] + xy * e[1] + xz * e[2]), -(yx * e[0] + yy * e[1] + yz * e[2]), -(zx * e[0] + zy * e[1] + zz * e[2]), 1];
  }
  function mul(a, b) {
    const o = new Array(16);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
      let s = 0; for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k]; o[c * 4 + r] = s;
    }
    return o;
  }

  const VS = `attribute vec3 p; attribute vec3 n; uniform mat4 mvp; varying vec3 vn; varying float vz;
    void main(){ vn = n; vz = p.z; gl_Position = mvp * vec4(p,1.0); }`;
  const FS = `precision mediump float; varying vec3 vn; varying float vz; uniform vec3 col; uniform float lit;
    void main(){ if(lit < 0.5){ gl_FragColor = vec4(col,1.0); return; }
      vec3 l1 = normalize(vec3(0.4,-0.6,0.8)); vec3 l2 = normalize(vec3(-0.6,0.5,0.3));
      float d = 0.35 + 0.55*max(dot(vn,l1),0.0) + 0.25*max(dot(vn,l2),0.0);
      gl_FragColor = vec4(col*d,1.0); }`;

  function hexToRgb(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return [0.85, 0.85, 0.85];
    const v = parseInt(m[1], 16);
    return [(v >> 16 & 255) / 255, (v >> 8 & 255) / 255, (v & 255) / 255];
  }

  class Viewer {
    constructor(canvas) {
      this.canvas = canvas;
      const gl = canvas.getContext("webgl", { antialias: true }) || canvas.getContext("experimental-webgl");
      this.gl = gl;
      this.ok = !!gl;
      this.yaw = -0.6; this.pitch = 0.55; this.dist = 300; this.target = [117.5, 117.5, 20];
      this.bed = [235, 235];
      this.color = [0.85, 0.85, 0.85];
      this.count = 0; this.autoRotate = true; this.raf = 0;
      if (!gl) return;
      const sh = (t, s) => { const x = gl.createShader(t); gl.shaderSource(x, s); gl.compileShader(x); return x; };
      const pr = gl.createProgram();
      gl.attachShader(pr, sh(gl.VERTEX_SHADER, VS)); gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, FS));
      gl.linkProgram(pr); gl.useProgram(pr);
      this.pr = pr;
      this.loc = { p: gl.getAttribLocation(pr, "p"), n: gl.getAttribLocation(pr, "n"), mvp: gl.getUniformLocation(pr, "mvp"),
        col: gl.getUniformLocation(pr, "col"), lit: gl.getUniformLocation(pr, "lit") };
      this.pbuf = gl.createBuffer(); this.nbuf = gl.createBuffer(); this.gridBuf = gl.createBuffer(); this.dummyN = gl.createBuffer();
      gl.enable(gl.DEPTH_TEST);
      this._bindInput();
      this._makeGrid();
    }

    setBed(x, y) { this.bed = [x, y]; this._makeGrid(); }
    setColor(hex) { this.color = hexToRgb(hex); this.draw(); }

    _makeGrid() {
      if (!this.gl) return;
      const v = [], [bx, by] = this.bed;
      for (let x = 0; x <= bx + 0.01; x += bx / 10) v.push(x, 0, 0, x, by, 0);
      for (let y = 0; y <= by + 0.01; y += by / 10) v.push(0, y, 0, bx, y, 0);
      this.gridCount = v.length / 3;
      const gl = this.gl;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.gridBuf); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(v), gl.STATIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.dummyN); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(v.length), gl.STATIC_DRAW);
    }

    async load(url) {
      if (!this.ok) return false;
      const r = await fetch(url, { cache: "no-store" });
      if (!r.ok) throw new Error("STL " + r.status);
      const pos = parseSTL(await r.arrayBuffer());
      const gl = this.gl;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.pbuf); gl.bufferData(gl.ARRAY_BUFFER, pos, gl.STATIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.nbuf); gl.bufferData(gl.ARRAY_BUFFER, flatNormals(pos), gl.STATIC_DRAW);
      this.count = pos.length / 3;
      let min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
      for (let i = 0; i < pos.length; i += 3) for (let k = 0; k < 3; k++) {
        if (pos[i + k] < min[k]) min[k] = pos[i + k]; if (pos[i + k] > max[k]) max[k] = pos[i + k];
      }
      this.target = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
      const size = Math.max(max[0] - min[0], max[1] - min[1], max[2] - min[2]);
      this.dist = Math.max(size * 2.2, 60);
      this.start();
      return true;
    }

    clear() { this.count = 0; this.draw(); }

    start() {
      if (this.raf) return;
      const loop = () => {
        this.raf = 0;
        if (!this.canvas.offsetParent) return; // hidden -> stop animating
        if (this.autoRotate) this.yaw += 0.004;
        this.draw();
        this.raf = requestAnimationFrame(loop);
      };
      this.raf = requestAnimationFrame(loop);
    }

    draw() {
      const gl = this.gl; if (!gl) return;
      const c = this.canvas, dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.max(1, c.clientWidth * dpr | 0), h = Math.max(1, c.clientHeight * dpr | 0);
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
      gl.viewport(0, 0, w, h);
      gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      const t = this.target, d = this.dist;
      const eye = [t[0] + d * Math.cos(this.pitch) * Math.sin(this.yaw), t[1] - d * Math.cos(this.pitch) * Math.cos(this.yaw), t[2] + d * Math.sin(this.pitch)];
      const mvp = mul(perspective(0.75, w / h, 1, 5000), lookAt(eye, t, [0, 0, 1]));
      gl.uniformMatrix4fv(this.loc.mvp, false, new Float32Array(mvp));
      // bed grid
      gl.uniform1f(this.loc.lit, 0); gl.uniform3fv(this.loc.col, [0.3, 0.34, 0.42]);
      this._attr(this.gridBuf, this.dummyN); gl.drawArrays(gl.LINES, 0, this.gridCount);
      if (this.count) {
        gl.uniform1f(this.loc.lit, 1); gl.uniform3fv(this.loc.col, this.color);
        this._attr(this.pbuf, this.nbuf); gl.drawArrays(gl.TRIANGLES, 0, this.count);
      }
    }

    _attr(pb, nb) {
      const gl = this.gl;
      gl.bindBuffer(gl.ARRAY_BUFFER, pb); gl.enableVertexAttribArray(this.loc.p); gl.vertexAttribPointer(this.loc.p, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, nb); gl.enableVertexAttribArray(this.loc.n); gl.vertexAttribPointer(this.loc.n, 3, gl.FLOAT, false, 0, 0);
    }

    _bindInput() {
      const c = this.canvas, pts = new Map();
      let pinch = 0;
      c.addEventListener("pointerdown", (e) => { c.setPointerCapture(e.pointerId); pts.set(e.pointerId, [e.clientX, e.clientY]); this.autoRotate = false; });
      c.addEventListener("pointermove", (e) => {
        if (!pts.has(e.pointerId)) return;
        const prev = pts.get(e.pointerId); pts.set(e.pointerId, [e.clientX, e.clientY]);
        if (pts.size === 1) {
          this.yaw -= (e.clientX - prev[0]) * 0.008;
          this.pitch = Math.max(-0.2, Math.min(1.5, this.pitch + (e.clientY - prev[1]) * 0.008));
        } else if (pts.size === 2) {
          const [a, b] = [...pts.values()]; const dd = Math.hypot(a[0] - b[0], a[1] - b[1]);
          if (pinch) this.dist = Math.max(20, Math.min(1500, this.dist * pinch / dd));
          pinch = dd;
        }
        this.draw();
      });
      const up = (e) => { pts.delete(e.pointerId); if (pts.size < 2) pinch = 0; };
      c.addEventListener("pointerup", up); c.addEventListener("pointercancel", up);
      c.addEventListener("wheel", (e) => { e.preventDefault(); this.dist = Math.max(20, Math.min(1500, this.dist * (1 + Math.sign(e.deltaY) * 0.1))); this.draw(); }, { passive: false });
      c.addEventListener("dblclick", () => { this.autoRotate = true; this.start(); });
    }
  }

  window.ArtifexViewer = Viewer;
})();
