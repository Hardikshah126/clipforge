/* ClipForge front-end — vanilla JS */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = async (url, opt = {}) => {
  const r = await fetch(url, { headers: { "Content-Type": "application/json" }, ...opt,
    body: opt.body && typeof opt.body !== "string" && !(opt.body instanceof Blob) ? JSON.stringify(opt.body) : opt.body });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};
const toast = (m, bad) => { const t = $("#toast"); t.textContent = m; t.className = "toast" + (bad ? " bad" : ""); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), bad ? 7000 : 3000); };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = s => { s = Math.max(0, s); const m = Math.floor(s / 60); return `${m}:${(s % 60).toFixed(1).padStart(4, "0")}`; };
const parseT = v => { v = String(v).trim(); if (v.includes(":")) { const [m, s] = v.split(":"); return (+m) * 60 + (+s); } return +v; };

const CSS_FONT = { "Impact": '"Impact CF"', "Segoe UI Black": '"Segoe UI Black CF"', "Space Mono": '"Space Mono CF"', "Instrument Serif": '"Instrument Serif CF"' };
const fontCss = f => CSS_FONT[f] || `"${f}"`;
const SINGLE_PALETTE = ["#FFE600", "#FFFFFF", "#22E06B", "#FFFFFF", "#FF4D6D", "#FFFFFF", "#38E1FF", "#FFFFFF"];
const POS_Y = { upper: 620, center: 960, seam: 960, lower: 1380 };
const AUTO_Y = { split: 960, stream: 960, full: 1380, fit: 1460 };

let STATE = null;          // server state (presets, gameplay, defaults)
let S = null;              // new-project settings
let src = { type: "url" }; // source selection
let upload = null;
let currentJob = null, pollT = null;

/* ---------------------------------------------------------------- init */
async function init() {
  STATE = await api("/api/state");
  const saved = (() => { try { return JSON.parse(localStorage.getItem("cf_settings")); } catch { return null; } })();
  S = { ...STATE.defaults, ...(saved || {}) };
  S.style = { ...STATE.presets[S.preset] || STATE.presets.pop, ...(saved?.style || {}) };
  $("#cookieHint").textContent = STATE.cookies
    ? "cookies.txt found — YouTube downloads will use it."
    : "If YouTube shows “confirm you’re not a bot”, download the video yourself and use Upload a file.";
  buildPresets(); buildFontSelect(); bindNew(); renderGameplay(); syncControls(); startPreview();
  await refreshJobs();
  setInterval(refreshJobs, 4000);
  const h = location.hash.slice(1);
  if (h) openJob(h); else showNew();
}

function persist() { try { localStorage.setItem("cf_settings", JSON.stringify(S)); } catch { } }

/* ---------------------------------------------------------------- new project */
function bindNew() {
  $$("#srcTabs .tab").forEach(b => b.onclick = () => {
    $$("#srcTabs .tab").forEach(x => x.classList.toggle("on", x === b));
    src.type = b.dataset.t;
    $("#srcUrl").classList.toggle("hidden", src.type !== "url");
    $("#srcUpload").classList.toggle("hidden", src.type !== "upload");
  });
  const drop = $("#drop");
  ["dragenter", "dragover"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", ev => ev.dataTransfer.files[0] && doUpload(ev.dataTransfer.files[0]));
  $("#fileIn").onchange = e => e.target.files[0] && doUpload(e.target.files[0]);
  $("#gpIn").onchange = async e => { for (const f of e.target.files) await uploadGameplay(f); e.target.value = ""; };

  $$("#layouts .layout").forEach(b => b.onclick = () => { S.layout = b.dataset.l; syncControls(); });
  $("#avoidBurned").onchange = e => { S.avoid_burned = e.target.checked; persist(); };

  const bindStyle = (id, key, num, out) => {
    const el = $(id);
    el.oninput = () => {
      S.style[key] = el.type === "checkbox" ? el.checked : num ? +el.value : el.value;
      if (out) $(out).textContent = el.value;
      markCustom(); persist(); restartPreview();
    };
  };
  bindStyle("#fFont", "font"); bindStyle("#fSize", "size", 1, "#oSize"); bindStyle("#fColor", "color");
  bindStyle("#fHl", "highlight"); bindStyle("#fOc", "outline"); bindStyle("#fOw", "outline_w", 1, "#oOw");
  bindStyle("#fMw", "max_words", 1, "#oMw"); bindStyle("#fPop", "pop", 1, "#oPop");
  bindStyle("#fUpper", "upper"); bindStyle("#fStrip", "strip_punct");
  $("#fPos").onchange = e => { S.position = e.target.value; persist(); restartPreview(); };

  $("#eCount").oninput = e => { S.count = +e.target.value; persist(); };
  $("#eMin").oninput = e => { S.min_len = +e.target.value; persist(); };
  $("#eMax").oninput = e => { S.max_len = +e.target.value; persist(); };
  $("#eAi").onchange = e => { S.ai_pick = e.target.checked; persist(); };

  $("#realBtn").onclick = realPreview;
  $("#liveBtn").onclick = () => { $("#realPrev").classList.add("hidden"); $("#liveBtn").classList.add("hidden"); };
  $("#goBtn").onclick = start;
  $("#newBtn").onclick = showNew;
  $("#modalX").onclick = () => $("#modal").classList.add("hidden");
  $("#modal").onclick = e => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };
}

function markCustom() { S.style.max_chars = Math.max(10, S.style.max_words * 7); }

function syncControls() {
  $$("#layouts .layout").forEach(b => b.classList.toggle("on", b.dataset.l === S.layout));
  $("#gpBlock").classList.toggle("hidden", S.layout !== "split");
  $("#avoidBurned").checked = !!S.avoid_burned;
  $("#phone").className = "phone " + S.layout;
  const st = S.style;
  $("#fFont").value = st.font; $("#fSize").value = st.size; $("#oSize").textContent = st.size;
  $("#fColor").value = st.color; $("#fHl").value = st.highlight; $("#fOc").value = st.outline;
  $("#fOw").value = st.outline_w; $("#oOw").textContent = st.outline_w;
  $("#fMw").value = st.max_words; $("#oMw").textContent = st.max_words;
  $("#fPop").value = st.pop; $("#oPop").textContent = st.pop;
  $("#fUpper").checked = !!st.upper; $("#fStrip").checked = !!st.strip_punct;
  $("#fPos").value = S.position || "auto";
  $("#eCount").value = S.count; $("#eMin").value = S.min_len; $("#eMax").value = S.max_len; $("#eAi").checked = !!S.ai_pick;
  $$("#presets .preset").forEach(p => p.classList.toggle("on", p.dataset.k === S.preset));
  const g = $$("#gpGrid .gp"); g.forEach(x => x.classList.toggle("on", x.dataset.n === S.gameplay));
  persist(); restartPreview();
}

function buildFontSelect() { $("#fFont").innerHTML = STATE.fonts.map(f => `<option>${esc(f)}</option>`).join(""); }

function buildPresets() {
  $("#presets").innerHTML = Object.entries(STATE.presets).map(([k, p]) => `
    <button class="preset" data-k="${k}"><div class="pv" id="pv_${k}"></div>
    <div class="pt"><b>${esc(p.label)}</b><span>${esc(p.blurb)}</span></div></button>`).join("");
  $$("#presets .preset").forEach(b => b.onclick = () => {
    S.preset = b.dataset.k; S.style = { ...STATE.presets[S.preset] }; syncControls();
  });
  Object.entries(STATE.presets).forEach(([k, p]) => miniLoop($("#pv_" + k), p));
}

/* ---- gameplay */
function renderGameplay() {
  const gp = STATE.gameplay;
  const cards = [`<div class="gp" data-n="mix"><div class="mix">⇄ Mix</div><div class="meta"><b>Mix all</b><span class="muted">${gp.length} videos, rotates</span></div></div>`]
    .concat(gp.map(g => `<div class="gp" data-n="${esc(g.name)}"><img loading="lazy" src="${g.thumb}" alt="">
      <div class="meta"><b title="${esc(g.name)}">${esc(g.name)}</b><span class="muted">${Math.round(g.duration / 60)} min · <span class="${g.height < 720 ? "lowres" : ""}">${g.height}p</span></span></div>
      <button class="x" title="Remove from library">×</button></div>`));
  $("#gpGrid").innerHTML = cards.join("");
  $$("#gpGrid .gp").forEach(c => {
    c.onclick = e => {
      if (e.target.classList.contains("x")) return removeGameplay(c.dataset.n);
      S.gameplay = c.dataset.n; syncControls();
    };
  });
  if (!gp.find(g => g.name === S.gameplay)) S.gameplay = "mix";
  const first = gp.find(g => g.name === S.gameplay) || gp[0];
  $("#phBot").style.backgroundImage = first ? `url("${first.thumb}")` : "";
  syncControls();
}

async function uploadGameplay(f) {
  toast(`Uploading ${f.name}…`);
  try {
    await xhrUpload("/api/gameplay", f, () => { });
    STATE = await api("/api/state"); renderGameplay(); toast(`Added ${f.name}`);
  } catch (e) { toast(e.message, 1); }
}
async function removeGameplay(n) {
  if (!confirm(`Remove “${n}” from the gameplay library? (It's moved to clipforge/data/gameplay-trash, not deleted.)`)) return;
  await api("/api/gameplay/" + encodeURIComponent(n), { method: "DELETE" });
  STATE = await api("/api/state"); renderGameplay();
}

/* ---- upload source */
function xhrUpload(url, file, onProg) {
  return new Promise((res, rej) => {
    const x = new XMLHttpRequest();
    x.open("POST", url);
    x.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    x.upload.onprogress = e => e.lengthComputable && onProg(e.loaded / e.total);
    x.onload = () => { let j = {}; try { j = JSON.parse(x.responseText); } catch { } x.status < 300 ? res(j) : rej(new Error(j.error || "Upload failed")); };
    x.onerror = () => rej(new Error("Upload failed"));
    x.send(file);
  });
}
async function doUpload(f) {
  upload = null;
  $("#upState").classList.remove("hidden");
  $("#upText").textContent = `Uploading ${f.name}…`;
  try {
    const j = await xhrUpload("/api/upload", f, p => { $("#upBar").style.width = (p * 100).toFixed(0) + "%"; });
    upload = j; $("#upBar").style.width = "100%"; $("#upText").textContent = `✓ ${f.name} ready`;
  } catch (e) { $("#upText").textContent = e.message; toast(e.message, 1); }
}

/* ---- start */
async function start() {
  let source;
  if (src.type === "url") {
    const url = $("#urlIn").value.trim();
    if (!url) return toast("Paste a YouTube link first", 1);
    source = { type: "url", url };
  } else {
    if (!upload) return toast("Upload a video first", 1);
    source = { type: "upload", upload_id: upload.upload_id };
  }
  if (S.min_len > S.max_len) return toast("Min length is longer than max length", 1);
  $("#goBtn").disabled = true;
  try {
    const job = await api("/api/jobs", { method: "POST", body: { source, settings: S } });
    $("#urlIn").value = ""; upload = null; $("#upState").classList.add("hidden");
    await refreshJobs(); openJob(job.id);
  } catch (e) { toast(e.message, 1); }
  $("#goBtn").disabled = false;
}

/* ---------------------------------------------------------------- caption live preview */
const SAMPLE = "This is how your captions will look in every single clip".split(" ");
let prevTimer = null;

function styledWord(text, st, k, active, idx, scale) {
  const sp = document.createElement("span");
  sp.className = "w";
  sp.textContent = st.upper ? text.toUpperCase() : text;
  const ow = st.outline_w * scale * 2.1;
  sp.style.cssText += `-webkit-text-stroke:${ow}px ${st.outline};paint-order:stroke fill;`;
  const sh = st.shadow * scale;
  let shadow = sh ? `${sh}px ${sh}px 0 rgba(0,0,0,.45)` : "none";
  sp.style.color = st.color;
  if (st.mode === "neon") shadow = `0 0 ${10 * scale * 3}px ${st.outline}, 0 0 ${4 * scale * 3}px ${st.outline}`;
  if (k === active) {
    if (st.mode === "pop" || st.mode === "neon") { sp.style.color = st.highlight; sp.style.transform = `scale(${st.pop / 100})`; }
    if (st.mode === "box") { sp.style.background = st.highlight; sp.style.borderRadius = ".22em"; sp.style.padding = "0 .16em"; sp.style.webkitTextStroke = "0"; sp.style.transform = `scale(${st.pop / 100})`; }
    if (st.mode === "reveal") { sp.style.color = st.highlight; sp.style.transform = `scale(${st.pop / 100})`; }
    if (st.mode === "single") { sp.style.color = SINGLE_PALETTE[idx % SINGLE_PALETTE.length]; sp.animate([{ transform: "scale(.7)" }, { transform: `scale(${st.pop / 100})` }, { transform: "scale(1)" }], { duration: 170 }); }
  } else {
    if (st.mode === "dim") sp.style.opacity = .5;
    if (st.mode === "reveal" && k > active) sp.style.visibility = "hidden";
  }
  if (k === active && st.mode === "dim") sp.style.color = st.highlight;
  sp.style.textShadow = shadow;
  return sp;
}

function chunkWords(words, st) {
  const out = []; let cur = [];
  words.forEach((w, i) => {
    cur.push(w);
    const len = cur.join(" ").length + (words[i + 1] ? words[i + 1].length + 1 : 0);
    if (cur.length >= st.max_words || len > st.max_chars || /[.,?!]$/.test(w) || i === words.length - 1) { out.push(cur); cur = []; }
  });
  return out;
}

function loopInto(el, st, scale, opts = {}) {
  const groups = chunkWords(SAMPLE, st);
  let g = 0, w = 0, idx = 0;
  const ff = fontCss(st.font);
  const tick = () => {
    const words = groups[g];
    el.innerHTML = "";
    el.style.fontFamily = ff;
    el.style.fontSize = (st.size * scale) + "px";
    el.style.fontStyle = st.font === "Instrument Serif" ? "italic" : "normal";
    if (st.mode === "single") el.appendChild(styledWord(words[w], st, 0, 0, idx, scale));
    else words.forEach((t, k) => el.appendChild(styledWord(t, st, k, w, idx, scale)));
    idx++; w++;
    if (w >= words.length) { w = 0; g = (g + 1) % groups.length; }
  };
  tick();
  return setInterval(tick, opts.ms || 330);
}

const miniTimers = {};
function miniLoop(el, st) { clearInterval(miniTimers[el.id]); miniTimers[el.id] = loopInto(el, { ...st }, 0.2, { ms: 380 }); }

function startPreview() { restartPreview(); }
function restartPreview() {
  if (!S) return;
  clearInterval(prevTimer);
  const cap = $("#phCap");
  const y = S.position && S.position !== "auto" ? POS_Y[S.position] : AUTO_Y[S.layout];
  cap.style.top = (y / 1920 * 100) + "%";
  prevTimer = loopInto(cap, S.style, 238 / 1080);
}

async function realPreview() {
  const b = $("#realBtn"); b.disabled = true; b.textContent = "Rendering…";
  try {
    const j = await api("/api/style-preview", { method: "POST", body: { preset: S.preset, style: S.style, layout: S.layout, position: S.position } });
    const img = $("#realPrev"); img.src = j.url; img.classList.remove("hidden"); $("#liveBtn").classList.remove("hidden");
  } catch (e) { toast(e.message, 1); }
  b.disabled = false; b.textContent = "Render exact preview";
}

/* ---------------------------------------------------------------- jobs list */
async function refreshJobs() {
  try {
    const jobs = await api("/api/jobs");
    $("#jobList").innerHTML = jobs.length ? jobs.map(j => `
      <div class="job-item ${currentJob && currentJob.id === j.id ? "on" : ""}" data-id="${j.id}">
        <b>${esc(j.title)}</b><span><i class="dot ${j.status}"></i>${statusText(j)}</span></div>`).join("")
      : `<p class="muted small" style="padding:0 6px">No projects yet.</p>`;
    $$("#jobList .job-item").forEach(x => x.onclick = () => openJob(x.dataset.id));
  } catch { }
}
const statusText = j => ({ queued: "Queued", working: "Working…", ready: `${j.clips} moments found`, done: `${j.rendered} clips rendered`, error: "Needs attention" }[j.status] || j.status);

function showNew() {
  currentJob = null; clearTimeout(pollT); location.hash = "";
  $("#newView").classList.remove("hidden"); $("#jobView").classList.add("hidden"); refreshJobs();
}

/* ---------------------------------------------------------------- job view */
async function openJob(id) {
  clearTimeout(pollT);
  try { currentJob = await api("/api/jobs/" + id); } catch { return showNew(); }
  location.hash = id;
  $("#newView").classList.add("hidden"); $("#jobView").classList.remove("hidden");
  renderJob(true); refreshJobs(); poll();
}

function poll() {
  clearTimeout(pollT);
  pollT = setTimeout(async () => {
    if (!currentJob) return;
    try {
      const j = await api("/api/jobs/" + currentJob.id);
      const changed = JSON.stringify([j.status, j.log.length, j.outputs, j.clips.length]) !== JSON.stringify([currentJob.status, currentJob.log.length, currentJob.outputs, currentJob.clips.length]);
      const statusChanged = j.status !== currentJob.status || j.clips.length !== currentJob.clips.length;
      currentJob = j;
      if (changed) renderJob(statusChanged);
    } catch { }
    poll();
  }, ["queued", "working"].includes(currentJob.status) ? 1200 : 5000);
}

function renderJob(full) {
  const j = currentJob, busy = ["queued", "working"].includes(j.status);
  $("#jTitle").textContent = j.title || j.source.url || "Upload";
  const info = j.info ? `${j.info.width}×${j.info.height} · ${(j.info.duration / 60).toFixed(1)} min` : "";
  $("#jSub").textContent = [j.source.type === "url" ? j.source.url : "Uploaded file", info].filter(Boolean).join(" · ");
  $("#jStage").textContent = j.status === "error" ? "Something went wrong" : j.status === "ready" ? "Moments ready — review and render" : j.status === "done" ? "All done" : (j.stage || "Queued");
  $("#spin").className = "spinner" + (busy ? "" : j.status === "error" ? " err" : " idle");
  const lg = $("#jLog"); lg.innerHTML = j.log.slice(-40).map(l => `<div>${l.t}  ${esc(l.m)}</div>`).join(""); lg.scrollTop = 1e9;
  $("#jErr").classList.toggle("hidden", !j.error); $("#jErr").textContent = j.error || "";
  $("#retryBtn").classList.toggle("hidden", j.status !== "error");
  $("#retryBtn").onclick = async () => { await api(`/api/jobs/${j.id}/retry`, { method: "POST", body: {} }); openJob(j.id); };
  $("#delBtn").onclick = async () => { if (!confirm("Delete this project? Rendered clips in the export folder are kept.")) return; await api(`/api/jobs/${j.id}/delete`, { method: "POST", body: {} }); showNew(); };

  const has = j.clips.length > 0;
  $("#clipsBlock").classList.toggle("hidden", !has);
  if (!has) return;
  if (full) renderClipEditor();
  renderOutputs();
  if (full) renderFrame();
  $("#renderBtn").disabled = busy;
  $("#renderBtn").textContent = busy ? "Rendering…" : (Object.values(j.outputs).some(o => o.status === "done") ? "Re-render changed clips →" : "Render selected clips →");
}

function renderClipEditor() {
  const j = currentJob, st = j.settings;
  $("#jPreset").innerHTML = Object.entries(STATE.presets).map(([k, p]) => `<option value="${k}" ${k === st.preset ? "selected" : ""}>${esc(p.label)}</option>`).join("");
  $("#jHl").value = st.style.highlight;
  $("#jLayout").value = st.layout;
  $("#jGp").innerHTML = `<option value="mix">Mix all</option>` + STATE.gameplay.map(g => `<option ${g.name === st.gameplay ? "selected" : ""}>${esc(g.name)}</option>`).join("");
  $("#jGpWrap").classList.toggle("hidden", st.layout !== "split");
  $("#jPreset").onchange = e => { const p = STATE.presets[e.target.value]; pushSettings({ preset: e.target.value, style: { ...p, highlight: p.highlight } }); $("#jHl").value = p.highlight; };
  $("#jHl").onchange = e => pushSettings({ style: { ...currentJob.settings.style, highlight: e.target.value } });
  $("#jLayout").onchange = async e => {
    const L = e.target.value;
    $("#jGpWrap").classList.toggle("hidden", L !== "split");
    await pushSettings({ layout: L });
    if (L === "stream" && !currentJob.facecam) await detectFace();
    renderFrame();
  };
  $("#jGp").onchange = e => pushSettings({ gameplay: e.target.value });

  $("#clips").innerHTML = j.clips.map((c, i) => `
    <div class="clip ${c.selected ? "" : "off"}" data-id="${c.id}">
      <input type="checkbox" ${c.selected ? "checked" : ""}>
      <div><input class="ttl input" value="${esc(c.title)}">
        <div class="hook">${esc(c.hook || "")}</div>
        <div class="txt">${esc(c.text)}</div></div>
      <div class="times">
        <div class="tm"><span>Start</span><button data-d="-0.5" data-f="start">−</button><input data-f="start" value="${fmt(c.start)}"><button data-d="0.5" data-f="start">+</button></div>
        <div class="tm"><span>End</span><button data-d="-0.5" data-f="end">−</button><input data-f="end" value="${fmt(c.end)}"><button data-d="0.5" data-f="end">+</button></div>
        <div class="row between"><span class="dur">${(c.end - c.start).toFixed(1)}s</span><button class="btn small ghost pv">Preview frame</button></div>
      </div></div>`).join("");
  $$("#clips .clip").forEach(el => {
    const c = j.clips.find(x => x.id === el.dataset.id);
    const upd = () => {
      el.classList.toggle("off", !c.selected);
      const d = c.end - c.start, dEl = $(".dur", el);
      dEl.textContent = d.toFixed(1) + "s"; dEl.classList.toggle("bad", d < j.settings.min_len - 3 || d > j.settings.max_len + 3);
      $$(".tm input", el).forEach(i => i.value = fmt(c[i.dataset.f]));
      updateSelCount(); pushClips();
    };
    $("input[type=checkbox]", el).onchange = e => { c.selected = e.target.checked; upd(); };
    $(".ttl", el).onchange = e => { c.title = e.target.value; pushClips(); };
    $$(".tm button", el).forEach(b => b.onclick = () => { c[b.dataset.f] = Math.round((c[b.dataset.f] + +b.dataset.d) * 100) / 100; upd(); });
    $$(".tm input", el).forEach(i => i.onchange = () => { const v = parseT(i.value); if (!isNaN(v)) c[i.dataset.f] = v; upd(); });
    $(".pv", el).onclick = async e => {
      const b = e.target; b.disabled = true; b.textContent = "Rendering…";
      try {
        await flushClips();
        const r = await api(`/api/jobs/${j.id}/still`, { method: "POST", body: { clip: c.id, at: Math.min(4, (c.end - c.start) / 3) } });
        $("#modalImg").src = r.url; $("#modal").classList.remove("hidden");
      } catch (err) { toast(err.message, 1); }
      b.disabled = false; b.textContent = "Preview frame";
    };
  });
  updateSelCount();
  $("#renderBtn").onclick = async () => {
    try { await flushClips(); currentJob = await api(`/api/jobs/${j.id}/render`, { method: "POST", body: { clips: currentJob.clips } }); renderJob(false); poll(); refreshJobs(); }
    catch (e) { toast(e.message, 1); }
  };
}

function updateSelCount() {
  const n = currentJob.clips.filter(c => c.selected).length;
  $("#selCount").textContent = `${n} of ${currentJob.clips.length} selected`;
  $("#renderHint").textContent = n ? "" : "Select at least one moment";
}

let clipT = null, pendingClips = null;
function pushClips() { clearTimeout(clipT); pendingClips = true; clipT = setTimeout(flushClips, 500); }
async function flushClips() {
  clearTimeout(clipT);
  if (!pendingClips || !currentJob) return;
  pendingClips = null;
  try { const j = await api(`/api/jobs/${currentJob.id}/update`, { method: "POST", body: { clips: currentJob.clips } }); currentJob.settings = j.settings; }
  catch (e) { toast(e.message, 1); }
}
async function pushSettings(patch) {
  try { const j = await api(`/api/jobs/${currentJob.id}/update`, { method: "POST", body: { settings: patch } }); currentJob.settings = j.settings; toast("Saved — re-render to apply"); }
  catch (e) { toast(e.message, 1); }
}

function renderOutputs() {
  const j = currentJob;
  const sel = j.clips.filter(c => j.outputs[c.id]);
  $("#outCard").classList.toggle("hidden", !sel.length);
  $("#outDir").textContent = j.export_dir || "";
  $("#outs").innerHTML = sel.map(c => {
    const o = j.outputs[c.id];
    const media = o.status === "done"
      ? `<video src="${o.url}?v=${o.v}" poster="${o.thumb}?v=${o.v}" controls preload="none" playsinline></video>`
      : `<div class="ph">${o.status === "error" ? "⚠ " + esc(o.error || "Failed") : o.status === "rendering" ? '<div class="spinner"></div>' : "Queued"}</div>`;
    const meta = o.meta ? `
      <div class="meta-box">
        <div class="meta-lbl"><span>Title</span><button class="cp" data-cp="t" data-id="${c.id}">Copy</button></div>
        <textarea class="meta-t" rows="2" data-id="${c.id}">${esc(o.meta.title)}</textarea>
        <div class="meta-lbl"><span>Description</span><button class="cp" data-cp="d" data-id="${c.id}">Copy</button></div>
        <textarea class="meta-d" rows="6" data-id="${c.id}">${esc(o.meta.description)}</textarea>
      </div>` : (o.status === "done" ? `<div class="muted small meta-wait">${["queued", "working"].includes(j.status) ? "Writing title & description…" : "No title yet — press ↻ Regenerate titles."}</div>` : "");
    return `<div class="out">${media}<div class="m"><b>${esc(c.title)}</b>
      ${o.status === "done" ? `<div class="row"><a class="btn small" href="${o.url}" download>Download</a><span class="muted small">${o.dur}s</span></div>` : ""}
      ${meta}</div></div>`;
  }).join("");
  $$("#outs .cp").forEach(b => b.onclick = async () => {
    const ta = $(`#outs textarea.${b.dataset.cp === "t" ? "meta-t" : "meta-d"}[data-id="${b.dataset.id}"]`);
    try { await navigator.clipboard.writeText(ta.value); } catch { ta.select(); document.execCommand("copy"); }
    b.textContent = "Copied ✓"; setTimeout(() => b.textContent = "Copy", 1500);
  });
  $("#metaBtn").disabled = ["queued", "working"].includes(j.status);
  $("#metaBtn").onclick = async () => {
    try { currentJob = await api(`/api/jobs/${j.id}/meta`, { method: "POST", body: {} }); renderJob(false); poll(); }
    catch (e) { toast(e.message, 1); }
  };
}

init().catch(e => toast("Couldn't reach the ClipForge server: " + e.message, 1));


/* ---------------------------------------------------------------- facecam framing editor */
const OUT_ASPECT = 1080 / 960;
async function detectFace() {
  const b = $("#detectBtn"); b.disabled = true; b.textContent = "Detecting…";
  try { currentJob = await api(`/api/jobs/${currentJob.id}/detect`, { method: "POST", body: {} }); renderFrame(); }
  catch (e) { toast(e.message, 1); }
  b.disabled = false; b.textContent = "↻ Auto-detect again";
}

function renderFrame() {
  const j = currentJob, card = $("#frameCard");
  const on = j.settings.layout === "stream" && j.clips.length;
  card.classList.toggle("hidden", !on);
  if (!on) return;
  $("#detectBtn").onclick = detectFace;
  if (!j.facecam) { $("#frameNote").textContent = "Detecting the facecam…"; return; }
  $("#frameNote").innerHTML = j.facecam.found ? '<span style="color:var(--ok)">✓ Facecam found</span>' : '<span style="color:var(--acc)">No facecam found — place the yellow box by hand</span>';
  const img = $("#frameImg");
  const src = `${j.facecam.frame}?v=${j.facecam.v}`;
  if (img.getAttribute("src") !== src) img.src = src;
  const boxes = { face: { ...j.settings.face_box }, game: { ...j.settings.game_box } };
  const draw = () => {
    for (const [k, el] of [["face", $("#boxFace")], ["game", $("#boxGame")]]) {
      const b = boxes[k];
      Object.assign(el.style, { left: b.x * 100 + "%", top: b.y * 100 + "%", width: b.w * 100 + "%", height: b.h * 100 + "%" });
    }
    const bg = (el, b) => {
      el.style.backgroundImage = `url("${src}")`;
      el.style.backgroundSize = `${100 / b.w}% ${100 / b.h}%`;
      el.style.backgroundPosition = `${b.w >= 1 ? 0 : b.x / (1 - b.w) * 100}% ${b.h >= 1 ? 0 : b.y / (1 - b.h) * 100}%`;
    };
    bg($("#moTop"), boxes.face); bg($("#moBot"), boxes.game);
  };
  const ready = () => {
    const ratio = img.naturalWidth / img.naturalHeight || 16 / 9;   // source W/H
    draw();
    for (const [k, el] of [["face", $("#boxFace")], ["game", $("#boxGame")]]) {
      el.onpointerdown = ev => {
        ev.preventDefault();
        const stage = $("#frameStage").getBoundingClientRect();
        const resize = ev.target.classList.contains("h");
        const b0 = { ...boxes[k] }, x0 = ev.clientX, y0 = ev.clientY;
        el.setPointerCapture(ev.pointerId);
        el.onpointermove = mv => {
          const dx = (mv.clientX - x0) / stage.width, dy = (mv.clientY - y0) / stage.height;
          const b = boxes[k];
          if (resize) {
            let w = Math.max(0.06, Math.min(b0.w + dx, 1 - b0.x));
            let h = w * ratio / OUT_ASPECT;                 // keep 1080:960 pixel aspect
            if (b0.y + h > 1) { h = 1 - b0.y; w = h * OUT_ASPECT / ratio; }
            b.w = w; b.h = h;
          } else {
            b.x = Math.min(Math.max(b0.x + dx, 0), 1 - b.w);
            b.y = Math.min(Math.max(b0.y + dy, 0), 1 - b.h);
          }
          draw();
        };
        el.onpointerup = () => {
          el.onpointermove = el.onpointerup = null;
          currentJob.settings.face_box = boxes.face; currentJob.settings.game_box = boxes.game;
          pushSettings({ face_box: boxes.face, game_box: boxes.game });
        };
      };
    }
  };
  img.complete && img.naturalWidth ? ready() : (img.onload = ready);
}
