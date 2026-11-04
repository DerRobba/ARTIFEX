/* ARTIFEX touch UI – vanilla JS single page app. */
(function () {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const state = {
    view: "create", jobs: new Map(), current: null, printer: {}, afs: [], system: null,
    opts: { mode: "auto", size: 0, slot: 0, autoPrint: false },
    shownPrompt: null,
  };

  const IDEAS = [
    "Kleine Eule auf einem Ast", "Schlüsselanhänger mit dem Text „ARTIFEX“", "Handyhalter für den Schreibtisch",
    "Freundlicher Drache, 8 cm hoch", "Stiftehalter 7 cm Durchmesser", "Wandhaken für Kopfhörer",
    "Gedrehte sechseckige Vase", "Box 60 × 40 × 30 mm",
  ];
  const STATUS = {
    queued: ["Wartet", "info"], running: ["Läuft", "run"], ready: ["Bereit", "ok"], printing: ["Druckt", "run"],
    done: ["Gedruckt", "ok"], failed: ["Fehler", "bad"], cancelled: ["Abgebrochen", "warn"],
  };
  const PRINTER_STATE = {
    printing: ["Druckt", "run"], paused: ["Pausiert", "warn"], complete: ["Fertig", "ok"], cancelled: ["Abgebrochen", "warn"],
    error: ["Fehler", "bad"], standby: ["Bereit", "ok"],
  };

  // ---------------------------------------------------------------- API
  async function api(method, url, body) {
    const opt = { method, headers: {} };
    if (body instanceof FormData) opt.body = body;
    else if (body !== undefined) { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
    const r = await fetch(url, opt);
    let data = null;
    try { data = await r.json(); } catch (_) { /* empty */ }
    if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
    return data;
  }
  function toast(msg, kind = "") {
    const t = document.createElement("div");
    t.className = "toast " + kind; t.textContent = msg;
    $("#toasts").appendChild(t);
    setTimeout(() => t.remove(), kind === "bad" ? 7000 : 3500);
  }
  async function act(fn, okMsg) {
    try { const r = await fn(); if (okMsg) toast(okMsg, "ok"); return r; } catch (e) { toast(e.message, "bad"); return null; }
  }
  const printerAction = (action, extra = {}) => act(() => api("POST", "/api/printer", { action, ...extra }));

  // ---------------------------------------------------------------- modal
  function modal(html, onMount) {
    $("#modal").innerHTML = html;
    $("#modal-bg").hidden = false;
    if (onMount) onMount($("#modal"));
  }
  function closeModal() { $("#modal-bg").hidden = true; $("#modal").innerHTML = ""; }
  function confirmBox(text) {
    return new Promise((res) => {
      modal(`<h3>${esc(text)}</h3><div class="btn-row"><button class="btn" data-x="0">Nein</button><button class="btn primary" data-x="1">Ja</button></div>`,
        (m) => m.addEventListener("click", (e) => { const b = e.target.closest("[data-x]"); if (b) { closeModal(); res(b.dataset.x === "1"); } }));
    });
  }
  function numpad(title, initial, max) {
    return new Promise((res) => {
      let v = String(Math.round(initial || 0));
      modal(`<h3>${esc(title)}</h3><div class="numdisplay" id="np-v">${v} °C</div>
        <div class="numpad">${[1, 2, 3, 4, 5, 6, 7, 8, 9, "C", 0, "⌫"].map((k) => `<button class="btn" data-k="${k}">${k}</button>`).join("")}</div>
        <div class="btn-row"><button class="btn" data-k="off">Aus (0)</button><button class="btn" data-k="cancel">Abbrechen</button><button class="btn primary" data-k="ok">Setzen</button></div>`,
        (m) => m.addEventListener("click", (e) => {
          const b = e.target.closest("[data-k]"); if (!b) return;
          const k = b.dataset.k;
          if (k === "ok") { closeModal(); res(Math.min(+v || 0, max)); return; }
          if (k === "off") { closeModal(); res(0); return; }
          if (k === "cancel") { closeModal(); res(null); return; }
          if (k === "C") v = "0"; else if (k === "⌫") v = v.slice(0, -1) || "0"; else v = (v === "0" ? "" : v) + k;
          if (+v > max) v = String(max);
          $("#np-v").textContent = v + " °C";
        }));
    });
  }

  // ---------------------------------------------------------------- navigation
  function show(view) {
    state.view = view;
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + view));
    $$(".rail button").forEach((b) => b.classList.toggle("active", b.dataset.view === view || (view === "job" && b.dataset.view === "jobs")));
    if (view === "jobs") loadJobs();
    if (view === "afs") loadAFS();
    if (view === "system") loadSystem();
    if (view === "job" && viewer) viewer.start();
  }
  $("#rail").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) show(b.dataset.view); });
  $("#btn-back").addEventListener("click", () => show("jobs"));

  // ---------------------------------------------------------------- create view
  function renderIdeas() {
    $("#idea-chips").innerHTML = IDEAS.map((i) => `<button class="chip">${esc(i)}</button>`).join("");
  }
  $("#idea-chips").addEventListener("click", (e) => {
    const b = e.target.closest(".chip"); if (!b) return;
    $("#prompt").value = b.textContent;
  });
  $("#opt-mode").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.opts.mode = b.dataset.v;
    $$("#opt-mode button").forEach((x) => x.classList.toggle("on", x === b));
  });
  $("#opt-size").addEventListener("input", (e) => {
    state.opts.size = +e.target.value;
    $("#size-label").textContent = state.opts.size ? `${state.opts.size} mm` : "Auto";
  });
  $("#opt-autoprint").addEventListener("change", (e) => { state.opts.autoPrint = e.target.checked; });
  function renderSlotPick() {
    $("#opt-slot").innerHTML = state.afs.map((s) =>
      `<button class="slot-dot ${s.index === state.opts.slot ? "on" : ""}" data-i="${s.index}" style="background:${esc(s.color)}" title="${esc(s.name || s.material)}"><i>${s.index + 1} · ${esc(s.material)}</i></button>`).join("");
  }
  $("#opt-slot").addEventListener("click", (e) => {
    const b = e.target.closest("[data-i]"); if (!b) return;
    state.opts.slot = +b.dataset.i; renderSlotPick();
  });
  $("#btn-generate").addEventListener("click", async () => {
    const prompt = $("#prompt").value.trim();
    if (!prompt) { toast("Bitte beschreibe zuerst, was gedruckt werden soll.", "bad"); return; }
    if (window.osk) window.osk.close();
    const job = await act(() => api("POST", "/api/jobs", {
      prompt, mode: state.opts.mode, size_mm: state.opts.size || null, slot: state.opts.slot,
      auto_print: state.opts.autoPrint,
    }));
    if (job) { upsertJob(job); openJob(job.id); $("#prompt").value = ""; }
  });

  // ---------------------------------------------------------------- jobs list
  function upsertJob(j) {
    const prev = state.jobs.get(j.id) || {};
    state.jobs.set(j.id, { ...prev, ...j });
    if (state.view === "jobs") renderJobs();
    if (state.current === j.id) renderJob(true);
    updateBadge();
  }
  function updateBadge() {
    const n = [...state.jobs.values()].filter((j) => j.status === "running" || j.status === "queued").length;
    const b = $("#jobs-badge"); b.hidden = !n; b.textContent = n;
  }
  async function loadJobs() {
    const list = await act(() => api("GET", "/api/jobs?limit=60"));
    if (!list) return;
    for (const j of list) state.jobs.set(j.id, { ...(state.jobs.get(j.id) || {}), ...j });
    renderJobs(); updateBadge();
  }
  function jobTitle(j) { return (j.plan && j.plan.object_name) || j.object_name || j.prompt || "Objekt"; }
  function jobMode(j) { return (j.plan && j.plan.mode) || j.mode; }
  function renderJobs() {
    const jobs = [...state.jobs.values()].sort((a, b) => b.created - a.created);
    if (!jobs.length) { $("#job-list").innerHTML = `<div class="empty">Noch keine Aufträge – leg los unter „Erstellen“.</div>`; return; }
    $("#job-list").innerHTML = jobs.map((j) => {
      const [label, cls] = STATUS[j.status] || [j.status, ""];
      const mode = jobMode(j);
      const meta = j.slice ? `${fmtDur(j.slice.estimated_time_s)} · ${j.slice.filament_g} g` : esc(j.message || "");
      return `<div class="card job" data-id="${j.id}">
        <div class="jt"><span class="jn">${esc(jobTitle(j))}</span><span class="pill ${cls}">${label}</span></div>
        <div class="jp">${esc(j.prompt)}</div>
        ${j.status === "running" || j.status === "queued" ? `<div class="progress"><span style="width:${(j.progress * 100).toFixed(0)}%"></span></div>` : ""}
        <div class="jm">${mode ? (mode === "organic" ? "Organisch" : "Parametrisch") + " · " : ""}${meta}</div></div>`;
    }).join("");
  }
  $("#job-list").addEventListener("click", (e) => { const c = e.target.closest(".job"); if (c) openJob(c.dataset.id); });
  $("#upload").addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f); fd.append("slot", state.opts.slot);
    const job = await act(() => api("POST", "/api/jobs/upload", fd), "Datei hochgeladen");
    e.target.value = "";
    if (job) { upsertJob(job); openJob(job.id); }
  });

  // ---------------------------------------------------------------- job detail
  let viewer = null, layerView = null, loadedModel = null, loadedGcode = null, activeTab = "3d";
  async function openJob(id) {
    state.current = id; loadedModel = null; loadedGcode = null;
    show("job");
    const j = await act(() => api("GET", "/api/jobs/" + id));
    if (j) upsertJob(j);
    renderJob(false);
  }
  function setTab(tab) {
    activeTab = tab;
    $$("#jd-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    $$(".tab-pane").forEach((p) => p.classList.toggle("on", p.dataset.pane === tab));
    const j = state.jobs.get(state.current);
    if (tab === "3d" && viewer) viewer.start();
    if (tab === "layers" && j && j.files.includes("print.gcode") && loadedGcode !== j.id + j.updated) {
      loadedGcode = j.id + j.updated;
      layerView.load(`/api/jobs/${j.id}/files/print.gcode`).catch((e) => toast(e.message, "bad"));
    }
    if (tab === "layers" && layerView) layerView.draw();
    updateEmpty(j);
  }
  $("#jd-tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setTab(b.dataset.tab); });
  function updateEmpty(j) {
    const files = (j && j.files) || [];
    const need = { "3d": "print.stl", image: "image.png", layers: "print.gcode", code: "model.scad" }[activeTab];
    const has = files.includes(need) || (activeTab === "3d" && files.some((f) => f.startsWith("upload")));
    $("#preview-empty").hidden = has;
    $("#preview-empty").textContent = j && (j.status === "running" || j.status === "queued") ? "Wird erzeugt …" : "Nicht verfügbar";
  }

  function renderJob(fromEvent) {
    const j = state.jobs.get(state.current);
    if (!j || state.view !== "job") return;
    const [label, cls] = STATUS[j.status] || [j.status, ""];
    $("#jd-title").textContent = jobTitle(j);
    $("#jd-status").className = "pill " + cls; $("#jd-status").textContent = label;
    $("#jd-progress").style.width = ((j.status === "ready" || j.status === "done" || j.status === "printing") ? 100 : j.progress * 100).toFixed(1) + "%";
    $("#jd-message").textContent = j.message || "";
    $("#jd-error").hidden = !j.error; $("#jd-error").textContent = j.error || "";

    $("#jd-stages").innerHTML = (j.stages || []).map((s) => {
      const t = s.started && s.finished ? `${(s.finished - s.started).toFixed(1)} s` : "";
      return `<div class="stage ${s.state}"><i>${s.state === "done" ? "✓" : s.state === "failed" ? "!" : ""}</i>${esc(s.title)}<small>${t}</small></div>`;
    }).join("");

    // tabs availability
    const files = j.files || [];
    $('#jd-tabs [data-tab="image"]').hidden = !files.includes("image.png");
    $('#jd-tabs [data-tab="code"]').hidden = !files.includes("model.scad");
    $('#jd-tabs [data-tab="layers"]').hidden = !files.includes("print.gcode");
    if ($(`#jd-tabs [data-tab="${activeTab}"]`).hidden) setTab("3d");

    if (files.includes("image.png")) {
      const src = `/api/jobs/${j.id}/files/image.png?v=${j.id}`;
      if ($("#jd-image").getAttribute("src") !== src) $("#jd-image").src = src;
    }
    if (files.includes("model.scad") && j.scad_code != null) $("#jd-code").textContent = j.scad_code;
    const modelFile = files.includes("print.stl") ? "print.stl" : files.includes("source.stl") ? "source.stl" : null;
    const modelKey = modelFile && j.id + modelFile + (j.mesh_stats ? JSON.stringify(j.mesh_stats.size_z) : "");
    if (viewer && modelKey && loadedModel !== modelKey) {
      loadedModel = modelKey;
      const slot = state.afs.find((s) => s.index === ((j.options && j.options.slot) || 0));
      viewer.setColor(slot ? slot.color : "#d9d9d9");
      viewer.load(`/api/jobs/${j.id}/files/${modelFile}?v=${Date.now()}`).catch((e) => toast(e.message, "bad"));
    } else if (viewer && !modelKey) { viewer.clear(); }
    updateEmpty(j);

    // info
    let info = "";
    if (j.plan) {
      info += `<h4>KI-Plan</h4><dl><dt>Modus</dt><dd>${j.plan.mode === "organic" ? "Organisch (Bild → 3D)" : "Parametrisch (OpenSCAD)"}</dd>
        <dt>Quelle</dt><dd>${{ llm: "KI-Modell", heuristic: "Offline-Regeln", user: "Upload" }[j.plan.source] || j.plan.source}${j.used_template ? " · Vorlage" : ""}${j.codegen_attempts > 1 ? ` · ${j.codegen_attempts} Versuche` : ""}</dd></dl>
        ${j.plan.reasoning ? `<p>${esc(j.plan.reasoning)}</p>` : ""}`;
    }
    if (j.mesh_stats) {
      const m = j.mesh_stats;
      info += `<h4 style="margin-top:12px">Modell</h4><dl><dt>Größe</dt><dd>${m.size_x.toFixed(1)} × ${m.size_y.toFixed(1)} × ${m.size_z.toFixed(1)} mm</dd>
        <dt>Volumen</dt><dd>${m.volume_cm3.toFixed(1)} cm³</dd><dt>Überhänge</dt><dd>${(m.overhang_ratio * 100).toFixed(0)} %</dd></dl>`;
    }
    if (j.settings) {
      const s = j.settings;
      info += `<h4 style="margin-top:12px">Druckeinstellungen ${j.settings_source === "ai" ? "(KI)" : "(Regeln)"}</h4><dl>
        <dt>Material</dt><dd>${esc(s.material)} · Slot ${s.afs_slot + 1} · ${s.nozzle_temp}/${s.bed_temp} °C</dd>
        <dt>Schicht</dt><dd>${s.layer_height.toFixed(2)} mm · ${s.perimeters} Wände</dd>
        <dt>Füllung</dt><dd>${Math.round(s.infill_density * 100)} % ${esc(s.infill_pattern)}</dd>
        <dt>Stützen</dt><dd>${s.supports ? "ja" : "nein"}${s.brim_width > 0 ? ` · Brim ${s.brim_width} mm` : ""}</dd></dl>
        ${j.settings_reason ? `<p>${esc(j.settings_reason)}</p>` : ""}`;
    }
    if (j.slice) {
      info += `<h4 style="margin-top:12px">Ergebnis</h4><dl><dt>Druckzeit</dt><dd>ca. ${fmtDur(j.slice.estimated_time_s)}</dd>
        <dt>Filament</dt><dd>${j.slice.filament_g} g · ${(j.slice.filament_mm / 1000).toFixed(2)} m</dd><dt>Schichten</dt><dd>${j.slice.layers}</dd></dl>`;
    }
    $("#jd-info").innerHTML = info || `<p>${esc(j.prompt)}</p>`;
    $("#jd-info").hidden = false;

    // actions
    const a = [];
    const busy = j.status === "running" || j.status === "queued";
    if (["ready", "done", "cancelled", "failed"].includes(j.status) && files.includes("print.gcode")) a.push(`<button class="btn primary" data-a="print">🖨 Jetzt drucken</button>`);
    if (busy) a.push(`<button class="btn danger" data-a="cancel">✕ Abbrechen</button>`);
    if (!busy && j.source === "prompt") a.push(`<button class="btn" data-a="feedback">✎ Ändern</button>`, `<button class="btn" data-a="again">↻ Neu generieren</button>`);
    if (!busy && (files.includes("source.stl") || files.some((f) => f.startsWith("upload")))) a.push(`<button class="btn" data-a="reslice">⚙ Größe & Einstellungen</button>`);
    if (!busy && j.status !== "printing") a.push(`<button class="btn danger" data-a="delete">🗑 Löschen</button>`);
    $("#jd-actions").innerHTML = a.join("");
  }

  $("#jd-actions").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-a]"); if (!b) return;
    const j = state.jobs.get(state.current); if (!j) return;
    const id = j.id;
    switch (b.dataset.a) {
      case "print":
        if (!(await confirmBox("Ist das Druckbett frei und sauber?"))) return;
        { const r = await act(() => api("POST", `/api/jobs/${id}/print`), "Druck gestartet"); if (r) { upsertJob(r); show("printer"); } }
        break;
      case "cancel": { const r = await act(() => api("POST", `/api/jobs/${id}/cancel`)); if (r) upsertJob(r); break; }
      case "delete":
        if (!(await confirmBox("Auftrag löschen?"))) return;
        if (await act(() => api("DELETE", `/api/jobs/${id}`))) { state.jobs.delete(id); show("jobs"); }
        break;
      case "again": {
        const r = await act(() => api("POST", "/api/jobs", { prompt: j.prompt, mode: (j.options && j.options.mode) || "auto",
          size_mm: j.options && j.options.size_mm, slot: (j.options && j.options.slot) || 0 }));
        if (r) { upsertJob(r); openJob(r.id); }
        break;
      }
      case "feedback": feedbackDialog(j); break;
      case "reslice": resliceDialog(j); break;
    }
  });

  function feedbackDialog(j) {
    modal(`<h3>Was soll geändert werden?</h3><p>z. B. „doppelt so hoch“, „Wände dicker“, „mit Loch für eine Schraube“, „die Eule soll lachen“.</p>
      <textarea id="fb-text" maxlength="500"></textarea>
      <div class="btn-row"><button class="btn" id="fb-cancel">Abbrechen</button><button class="btn primary" id="fb-ok">Ändern</button></div>`, (m) => {
      $("#fb-cancel", m).onclick = closeModal;
      $("#fb-ok", m).onclick = async () => {
        const fb = $("#fb-text", m).value.trim(); if (!fb) return;
        closeModal(); if (window.osk) window.osk.close();
        const r = await act(() => api("POST", "/api/jobs", { prompt: j.prompt, feedback: fb, parent_id: j.id,
          mode: (j.plan && j.plan.mode) || "auto", slot: (j.options && j.options.slot) || 0 }));
        if (r) { upsertJob(r); openJob(r.id); }
      };
      setTimeout(() => $("#fb-text", m).focus(), 50);
    });
  }

  function resliceDialog(j) {
    const s = j.settings || {};
    const m0 = j.mesh_stats ? Math.max(j.mesh_stats.size_x, j.mesh_stats.size_y, j.mesh_stats.size_z) : 60;
    const slot = (j.options && j.options.slot) || 0;
    modal(`<h3>Größe & Einstellungen</h3>
      <div class="form-row"><span class="label">Größte Abmessung <b id="rs-size-l">${m0.toFixed(0)} mm</b></span><input type="range" id="rs-size" min="10" max="220" step="1" value="${m0.toFixed(0)}"></div>
      <div class="form-row"><span class="label">Schichthöhe <b id="rs-lh-l">${(s.layer_height || 0.2).toFixed(2)} mm</b></span><input type="range" id="rs-lh" min="0.08" max="0.28" step="0.02" value="${s.layer_height || 0.2}"></div>
      <div class="form-row"><span class="label">Füllung <b id="rs-inf-l">${Math.round((s.infill_density ?? 0.15) * 100)} %</b></span><input type="range" id="rs-inf" min="0" max="100" step="5" value="${Math.round((s.infill_density ?? 0.15) * 100)}"></div>
      <div class="form-row"><span class="label">Wände <b id="rs-per-l">${s.perimeters || 2}</b></span><input type="range" id="rs-per" min="1" max="6" step="1" value="${s.perimeters || 2}"></div>
      <div class="form-row"><label class="toggle"><input type="checkbox" id="rs-sup" ${s.supports ? "checked" : ""}><span></span> Stützstrukturen</label></div>
      <div class="form-row"><label class="toggle"><input type="checkbox" id="rs-brim" ${s.brim_width > 0 ? "checked" : ""}><span></span> Brim (bessere Haftung)</label></div>
      <div class="form-row"><span class="label">Filament</span><div class="slot-pick" id="rs-slot">${state.afs.map((x) => `<button class="slot-dot ${x.index === slot ? "on" : ""}" data-i="${x.index}" style="background:${esc(x.color)}"><i>${x.index + 1} · ${esc(x.material)}</i></button>`).join("")}</div></div>
      <div class="btn-row"><button class="btn" id="rs-cancel">Abbrechen</button><button class="btn primary" id="rs-ok">Neu slicen</button></div>`, (m) => {
      let sel = slot;
      const bind = (id, fmt) => $(id, m).addEventListener("input", (e) => { $(id + "-l", m).textContent = fmt(e.target.value); });
      bind("#rs-size", (v) => `${v} mm`); bind("#rs-lh", (v) => `${(+v).toFixed(2)} mm`); bind("#rs-inf", (v) => `${v} %`); bind("#rs-per", (v) => v);
      $("#rs-slot", m).onclick = (e) => { const b = e.target.closest("[data-i]"); if (!b) return; sel = +b.dataset.i; $$("#rs-slot .slot-dot", m).forEach((x) => x.classList.toggle("on", x === b)); };
      $("#rs-cancel", m).onclick = closeModal;
      $("#rs-ok", m).onclick = async () => {
        const size = +$("#rs-size", m).value;
        const body = {
          size_mm: Math.abs(size - m0) >= 1 ? size : null, slot: sel,
          overrides: {
            layer_height: +$("#rs-lh", m).value, infill_density: +$("#rs-inf", m).value / 100, perimeters: +$("#rs-per", m).value,
            supports: $("#rs-sup", m).checked, brim_width: $("#rs-brim", m).checked ? Math.max(s.brim_width || 0, 5) : 0,
          },
        };
        closeModal();
        const r = await act(() => api("POST", `/api/jobs/${j.id}/reslice`, body));
        if (r) { loadedGcode = null; upsertJob(r); }
      };
    });
  }

  // ---------------------------------------------------------------- printer
  function fmtDur(s) {
    if (s == null || isNaN(s)) return "–";
    s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h} h ${m} min` : `${m} min`;
  }
  function renderPrinter() {
    const p = state.printer || {};
    let [label, cls] = ["offline", "bad"];
    if (p.connected && p.klippy !== "ready") [label, cls] = [`Klipper: ${p.klippy}`, "bad"];
    else if (p.connected) [label, cls] = PRINTER_STATE[p.state] || [p.state || "Bereit", "ok"];
    $("#st-state").className = "pill " + cls; $("#st-state").textContent = label;
    const heat = (h, el, icon) => {
      const e = $(el);
      if (!h) { e.textContent = `${icon} –`; return; }
      e.textContent = `${icon} ${h.temp.toFixed(0)}°${h.target > 0 ? " / " + h.target.toFixed(0) + "°" : ""}`;
      e.classList.toggle("hot", h.target > 0 || h.temp > 50);
    };
    heat(p.extruder, "#st-nozzle", "🔥"); heat(p.bed, "#st-bed", "▭");
    const active = p.state === "printing" || p.state === "paused";
    $("#st-progress").hidden = !active;
    $("#st-progress span").style.width = ((p.progress || 0) * 100).toFixed(1) + "%";

    // printer view
    const tile = (sel, h) => {
      const t = $(sel); if (!h) { $("b", t).textContent = "–"; return; }
      $("b", t).textContent = h.temp.toFixed(0) + " °C"; $("span", t).textContent = h.target > 0 ? `Ziel ${h.target.toFixed(0)} °C` : "Aus";
      t.classList.toggle("hot", h.target > 0);
    };
    tile("#tile-nozzle", p.extruder); tile("#tile-bed", p.bed);
    $("#pc-file").textContent = p.filename ? p.filename.split("/").pop() : "Kein Druck aktiv";
    const pct = (p.progress || 0) * 100;
    $("#pc-bar").style.width = pct.toFixed(1) + "%";
    $("#pc-pct").textContent = pct.toFixed(0) + " %";
    $("#pc-layer").textContent = p.layer ? `${p.layer}/${p.total_layers || "?"}` : "–";
    $("#pc-time").textContent = p.print_duration ? fmtDur(p.print_duration) : "–";
    $("#pc-left").textContent = p.progress > 0.02 && p.print_duration ? fmtDur(p.print_duration / p.progress - p.print_duration) : "–";
    $("#pc-msg").textContent = p.message || p.error || "";
    $("#btn-pause").disabled = p.state !== "printing";
    $("#btn-resume").disabled = p.state !== "paused";
    $("#btn-cancel").disabled = !active;
    $("#filter-state").textContent = p.filter == null ? "nicht konfiguriert" : p.filter > 0 ? `an (${Math.round(p.filter * 100)} %)` : "aus";
    $("#tool-state").textContent = p.tool ? `Aktiv: ${p.tool}` : "–";
    checkPrompts(p);
  }

  let promptShown = null;
  function checkPrompts(p) {
    // AFS manual change or toolhead swap waiting for the user
    let key = null, html = null;
    if (p.afs_pending != null && p.afs_pending >= 0) {
      const slot = state.afs.find((s) => s.index === p.afs_pending);
      key = "afs" + p.afs_pending;
      html = `<h3>Filamentwechsel</h3><p>Bitte Filament aus <b>Slot ${p.afs_pending + 1}</b> einlegen${slot ? ` (${esc(slot.material)}, <span style="color:${esc(slot.color)}">●</span> ${esc(slot.name || "")})` : ""} und bis zum Extruder schieben.</p>
        <div class="btn-row"><button class="btn primary" data-pc="afs_confirm">Eingelegt – weiter</button></div>`;
    } else if (p.tool_pending) {
      key = "tool" + p.tool_pending;
      html = `<h3>Werkzeugwechsel</h3><p>Bitte jetzt den magnetischen Werkzeugkopf <b>${esc(p.tool_pending)}</b> aufsetzen.</p>
        <div class="btn-row"><button class="btn primary" data-pc="tool_confirm">Aufgesetzt – weiter</button></div>`;
    }
    if (key && promptShown !== key) {
      promptShown = key;
      modal(html, (m) => m.addEventListener("click", async (e) => {
        const b = e.target.closest("[data-pc]"); if (!b) return;
        closeModal(); await printerAction(b.dataset.pc);
      }));
    } else if (!key && promptShown) { promptShown = null; closeModal(); }
  }

  $("#view-printer").addEventListener("click", async (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.preheat) { printerAction("preheat", { script: b.dataset.preheat }); toast(`Heize für ${b.dataset.preheat} vor`); return; }
    if (b.dataset.heater) {
      const isBed = b.dataset.heater === "bed"; const h = state.printer[isBed ? "bed" : "extruder"];
      const v = await numpad(isBed ? "Bett-Temperatur" : "Düsen-Temperatur", h ? h.target : 0, isBed ? 110 : 290);
      if (v != null) printerAction(isBed ? "set_bed" : "set_extruder", { value: v });
      return;
    }
    if (b.dataset.tool) { printerAction("tool_change", { script: b.dataset.tool }); return; }
    if (b.dataset.act) {
      if (b.dataset.confirm && !(await confirmBox(b.dataset.confirm))) return;
      printerAction(b.dataset.act);
    }
  });
  $("#btn-estop").addEventListener("click", async () => {
    if (await confirmBox("NOT-AUS auslösen? Alle Motoren und Heizungen werden sofort abgeschaltet.")) printerAction("estop");
  });

  // ---------------------------------------------------------------- AFS
  const COLORS = ["#f2f2f2", "#222222", "#8a8f99", "#d63c3c", "#f08a24", "#f2c12e", "#2fa84f", "#1fb5a8", "#2f6fd6", "#8e44ad", "#e05fa8", "#8b5a2b"];
  async function loadAFS() {
    const s = await act(() => api("GET", "/api/afs"));
    if (s) { state.afs = s; renderAFS(); renderSlotPick(); }
  }
  function renderAFS() {
    const p = state.printer;
    $("#afs-mode").textContent = p.afs_hardware == null ? "Klipper offline" : p.afs_hardware ? "Automatik-Modus" : "Manueller Modus";
    $("#afs-grid").innerHTML = state.afs.map((s) => `<div class="card slot-card ${p.afs_current === s.index ? "active" : ""}">
      <div class="spool" style="background:${esc(s.color)}"></div>
      <h3>Slot ${s.index + 1} · ${esc(s.material)}</h3>
      <div class="sub">${esc(s.name || "—")} ${s.loaded ? "· geladen" : ""}${p.afs_current === s.index ? " · aktiv" : ""}</div>
      <div class="btn-row"><button class="btn small" data-s="${s.index}" data-a="edit">Bearbeiten</button>
      <button class="btn small" data-s="${s.index}" data-a="select">Aktivieren</button></div>
      <div class="btn-row"><button class="btn small" data-s="${s.index}" data-a="load">Laden</button>
      <button class="btn small" data-s="${s.index}" data-a="unload">Entladen</button></div></div>`).join("");
  }
  $("#afs-grid").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-a]"); if (!b) return;
    const i = +b.dataset.s;
    if (b.dataset.a === "edit") return editSlot(state.afs.find((s) => s.index === i));
    if (await act(() => api("POST", `/api/afs/${i}/${b.dataset.a}`), "Befehl gesendet")) loadAFS();
  });
  function editSlot(s) {
    const mats = (state.system && state.system.materials) || ["PLA", "PETG", "ABS", "ASA", "TPU"];
    let color = s.color, mat = s.material;
    modal(`<h3>Slot ${s.index + 1} bearbeiten</h3>
      <span class="label">Material</span><div class="seg" id="es-mat">${mats.map((m) => `<button data-v="${m}" class="${m === mat ? "on" : ""}">${m}</button>`).join("")}</div>
      <span class="label" style="margin-top:12px">Farbe</span><div class="palette" id="es-col">${COLORS.map((c) => `<button data-c="${c}" class="${c === color ? "on" : ""}" style="background:${c}"></button>`).join("")}</div>
      <span class="label">Bezeichnung</span><input type="text" id="es-name" maxlength="40" value="${esc(s.name)}" placeholder="z. B. Galaxy Black">
      <div class="btn-row"><button class="btn" id="es-cancel">Abbrechen</button><button class="btn primary" id="es-ok">Speichern</button></div>`, (m) => {
      $("#es-mat", m).onclick = (e) => { const b = e.target.closest("button"); if (!b) return; mat = b.dataset.v; $$("#es-mat button", m).forEach((x) => x.classList.toggle("on", x === b)); };
      $("#es-col", m).onclick = (e) => { const b = e.target.closest("button"); if (!b) return; color = b.dataset.c; $$("#es-col button", m).forEach((x) => x.classList.toggle("on", x === b)); };
      $("#es-cancel", m).onclick = closeModal;
      $("#es-ok", m).onclick = async () => {
        const r = await act(() => api("PUT", `/api/afs/${s.index}`, { material: mat, color, name: $("#es-name", m).value }));
        closeModal(); if (window.osk) window.osk.close();
        if (r) loadAFS();
      };
    });
  }

  // ---------------------------------------------------------------- system
  async function loadSystem() {
    const s = await act(() => api("GET", "/api/system"));
    if (!s) return;
    state.system = s;
    const p = s.printer || {};
    const item = (title, ok, value) => `<div class="card sys-item"><div class="jt" style="display:flex;justify-content:space-between"><b>${title}</b>
      <span class="pill ${ok ? "ok" : "bad"}">${ok ? "OK" : "Fehler"}</span></div><div class="v">${value}</div></div>`;
    $("#sys-grid").innerHTML = [
      item("Klipper / Moonraker", p.connected && p.klippy === "ready", esc(p.connected ? `Klipper: ${p.klippy}${p.message ? " – " + p.message : ""}` : p.error || "nicht erreichbar")),
      item("KI-Sprachmodell", s.llm.enabled && s.llm.reachable, esc(`${s.llm.model} @ ${s.llm.base_url}`) + (s.llm.enabled ? "" : " (deaktiviert – Offline-Vorlagen aktiv)")),
      item("GPU-Inference-Server", s.inference.reachable, esc(s.inference.url) + (s.inference.info ? `<br>${esc(JSON.stringify(s.inference.info))}` : "<br>z-image-turbo + Hunyuan3D-2.1")),
      item("OpenSCAD", !/unavailable/.test(s.openscad), esc(s.openscad)),
      item("Slicer", true, esc(s.slicer === "artifex" ? "ARTIFEX Slicer (eingebaut)" : "OrcaSlicer CLI")),
      item("ARTIFEX", true, `Version ${esc(s.version)} · Bauraum ${s.build_volume.join(" × ")} mm`),
    ].join("");
  }
  $("#btn-sys-refresh").addEventListener("click", loadSystem);
  $("#view-system").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    if (b.dataset.confirm && !(await confirmBox(b.dataset.confirm))) return;
    printerAction(b.dataset.act);
  });
  $("#btn-fullscreen").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(() => {});
  });

  // ---------------------------------------------------------------- websocket
  let wsRetry = 1000;
  function connectWS() {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.onopen = () => { wsRetry = 1000; };
    ws.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      if (ev.type === "printer") {
        const prevTool = state.printer.tool;
        state.printer = ev.status || {}; renderPrinter();
        if (state.view === "afs") renderAFS();
        if (prevTool !== state.printer.tool) renderTools();
      } else if (ev.type === "job") {
        const prev = state.jobs.get(ev.job.id);
        upsertJob(ev.job);
        if (prev && prev.status !== ev.job.status) {
          if (ev.job.status === "ready") toast(`„${jobTitle(ev.job)}“ ist bereit zum Drucken`, "ok");
          if (ev.job.status === "failed") toast(`„${jobTitle(ev.job)}“: ${ev.job.error || "Fehler"}`, "bad");
        }
      } else if (ev.type === "job_deleted") { state.jobs.delete(ev.id); if (state.view === "jobs") renderJobs(); }
      else if (ev.type === "afs") { state.afs = ev.slots; renderAFS(); renderSlotPick(); }
    };
    ws.onclose = () => { setTimeout(connectWS, wsRetry); wsRetry = Math.min(wsRetry * 2, 10000); };
  }

  const TOOLS = [["fdm", "FDM 0,4"], ["fdm06", "FDM 0,6"], ["laser", "Laser"], ["pen", "Stift"]];
  function renderTools() {
    $("#tool-row").innerHTML = `<span class="pill" id="tool-state">${state.printer.tool ? "Aktiv: " + esc(state.printer.tool) : "–"}</span>` +
      TOOLS.map(([k, l]) => `<button class="btn small" data-tool="${k}">${l}</button>`).join("");
  }

  // ---------------------------------------------------------------- init
  async function init() {
    renderIdeas(); renderTools();
    const canvas = $("#viewer3d");
    viewer = new window.ArtifexViewer(canvas);
    layerView = new window.ArtifexLayers($("#layer-canvas"), $("#layer-slider"), $("#layer-label"));
    window.osk = new window.ArtifexOSK($("#osk"));
    $("#modal-bg").addEventListener("click", (e) => { if (e.target.id === "modal-bg") closeModal(); });
    $("#prompt").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#btn-generate").click(); });
    connectWS();
    await loadAFS();
    loadJobs();
    api("GET", "/api/system").then((s) => {
      state.system = s;
      if (s.build_volume) viewer.setBed(s.build_volume[0], s.build_volume[1]);
    }).catch(() => {});
  }
  init();
})();
