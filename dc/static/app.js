"use strict";
// ---------- helpers ----------
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function fmtBytes(b) {
  if (b == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(v < 10 ? 2 : 1)) + " " + u[i];
}
const fmtNum = (n) => (n ?? 0).toLocaleString("en-US");
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleString() : "–");
function fmtDur(s) {
  if (!isFinite(s) || s < 0) return "–";
  s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : m ? `${m} min ${s % 60} s` : `${s} s`;
}
// plural(5, "file") -> "5 files"
const plural = (n, one, many = one + "s") => `${fmtNum(n)} ${n === 1 ? one : many}`;
const files = (n) => plural(n, "file");
const ROLE = { main: "Main", backup: "Backup", other: "Other" };
const PHASE = { walk: "Walking folders", hash: "Computing checksums", verify: "Verifying checksums" };

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-DataCatalog": "1" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) { const err = new Error(data.error || `Error ${r.status}`); err.status = r.status; throw err; }
  return data;
}
function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}
function modal(title, html, buttons = []) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  const foot = $("#modal-foot"); foot.innerHTML = "";
  for (const b of buttons) {
    const el = document.createElement("button");
    el.textContent = b.label; if (b.cls) el.className = b.cls;
    el.onclick = async () => { if (b.onClick) { const keep = await b.onClick(); if (keep === true) return; } closeModal(); };
    foot.appendChild(el);
  }
  $("#modal").classList.remove("hidden");
}
const closeModal = () => $("#modal").classList.add("hidden");
$("#modal-x").onclick = closeModal;
$("#modal").onclick = (e) => { if (e.target.id === "modal") closeModal(); };
const handle = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, 6000); } };

// ---------- state ----------
const state = { status: null, tab: "drives", builtAt: null };
const sel = new Set();           // selected paths (files and folders)
const treeCache = new Map();     // path -> /api/tree response
const expanded = new Set([""]);

const parentOf = (p) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
function coveredBy(p) {          // is the path covered by a selected ancestor?
  let x = p;
  while (x) { x = parentOf(x); if (sel.has(x) && x !== p) return x; if (!x) break; }
  return null;
}
function toggleSel(p, on) {
  if (on) {
    for (const s of Array.from(sel)) if (s.startsWith(p + "/")) sel.delete(s);
    sel.add(p);
  } else sel.delete(p);
  renderSelbar(); rerenderSelectionViews();
}
function renderSelbar() {
  $("#selbar").classList.toggle("hidden", sel.size === 0);
  $("#sel-text").textContent = `Selected: ${plural(sel.size, "item")}`;
}
function rerenderSelectionViews() {
  if (state.tab === "tree") renderTree();
  if (state.tab === "groups") renderGroups();
  if (state.tab === "identical") renderIdentical();
}

// ---------- tabs ----------
function showTab(t) {
  state.tab = t;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  $$(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + t));
  ({ drives: loadDrives, tree: loadTree, groups: loadGroups, identical: loadIdentical,
     quarantine: loadBatches, issues: loadIssues, settings: loadSettings }[t] || (() => {}))();
}
$$("#tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

// ---------- status ----------
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    state.status = s;
    const c = s.catalog;
    $("#disk-chip").innerHTML = c
      ? `<b>${esc(c.volume.name)}</b><span class="muted">${esc(ROLE[c.volume.role] || c.volume.role)} · ${esc(c.root)}</span>`
      : "No drive selected";
    $$("#tabs button").forEach((b) => { if (b.dataset.tab !== "drives") b.disabled = !c; });
    const running = s.job && s.job.running;
    $("#btn-scan").disabled = !c || running;
    $("#btn-verify").disabled = !c || running || !c.files;
    $("#btn-stop").classList.toggle("hidden", !running);
    renderJob(s.job);
    const ib = $("#issues-badge");
    const n = c ? c.integrity_issues : 0;
    ib.textContent = n; ib.classList.toggle("hidden", !n);
    if (s.analysis) {
      const a = s.analysis;
      $("#analysis-info").textContent = a.building ? "Recomputing analysis…" :
        a.built_at ? `${plural(a.groups, "duplicate group")} · ${fmtBytes(a.wasted_bytes)} reclaimable` : "";
      if (a.built_at && a.built_at !== state.builtAt) {
        const first = state.builtAt === null;
        state.builtAt = a.built_at;
        treeCache.clear();
        if (!first || state.tab !== "drives") rerenderAfterAnalysis();
      }
    }
    if (state.tab === "drives") renderSummary();
    setTimeout(refreshStatus, running || (s.analysis && s.analysis.building) ? 1000 : 4000);
  } catch (e) {
    $("#disk-chip").textContent = "No connection to the server";
    setTimeout(refreshStatus, 4000);
  }
}
function rerenderAfterAnalysis() {
  if (state.tab === "tree") loadTree();
  if (state.tab === "identical") loadIdentical();
  if (state.tab === "groups") loadGroups();
}
function renderJob(j) {
  const box = $("#job");
  if (!j) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  let pct = 0, nums = "";
  if (j.phase === "hash" || j.phase === "verify") {
    pct = j.to_hash_bytes ? (100 * j.bytes_hashed) / j.to_hash_bytes : 100;
    const left = j.rate > 0 ? (j.to_hash_bytes - j.bytes_hashed) / j.rate : NaN;
    nums = `${fmtNum(j.files_hashed)}/${files(j.to_hash_files)} · ${fmtBytes(j.bytes_hashed)}/${fmtBytes(j.to_hash_bytes)} · ${fmtBytes(j.rate)}/s · ~${fmtDur(left)} left`;
  } else if (j.phase === "walk") {
    pct = 3; nums = `${files(j.files_seen)} · ${fmtBytes(j.bytes_seen)}`;
  } else {
    pct = 100;
    const st = { done: "finished", stopped: "stopped", failed: "failed" }[j.status] || j.status;
    nums = `${j.mode === "verify" ? "Verification" : "Scan"} ${st} · ${fmtDur(j.elapsed)} · new: ${fmtNum(j.new_files)}, changed: ${fmtNum(j.changed_files)}, missing: ${fmtNum(j.missing_files)}, errors: ${fmtNum(j.errors)}` +
      (j.mode === "verify" ? `, corrupted: ${fmtNum(j.corrupt)}` : "");
  }
  const text = nums + (j.message ? " · " + j.message : "");
  $("#job-phase").textContent = j.running ? PHASE[j.phase] || j.phase : "";
  $("#job-numbers").textContent = text;
  box.title = text;
  $("#job-bar").style.width = Math.min(100, pct).toFixed(1) + "%";
  $("#job-current").textContent = j.running ? j.current : "";
}
$("#btn-scan").onclick = handle(async () => { await api("/api/scan", { mode: "scan" }); refreshNow(); });
$("#btn-verify").onclick = handle(async () => {
  modal("Verify", `<p>Verification re-reads <b>all</b> files
    (${fmtBytes(state.status.catalog.bytes)}) and compares checksums. For 1 TB over USB this takes several hours.
    You can stop it and run it again later.</p>`,
    [{ label: "Cancel" }, { label: "Start", cls: "primary", onClick: handle(async () => { await api("/api/scan", { mode: "verify" }); refreshNow(); }) }]);
});
$("#btn-stop").onclick = handle(async () => { await api("/api/stop", {}); toast("Stopping… the work done so far is saved."); });
function refreshNow() {
  clearTimeout(refreshNow._t);
  refreshNow._t = setTimeout(() => api("/api/status").then((s) => {
    state.status = s; renderJob(s.job);
    const running = !!(s.job && s.job.running);
    $("#btn-scan").disabled = $("#btn-verify").disabled = running;
    $("#btn-stop").classList.toggle("hidden", !running);
    if (state.tab === "drives") renderSummary();
    if (running) refreshNow();
  }), 700);
}

// ---------- drives ----------
function renderSummary() {
  const c = state.status && state.status.catalog;
  const box = $("#summary");
  if (!c) { box.classList.add("hidden"); return; }
  const a = state.status.analysis || {};
  box.classList.remove("hidden");
  box.innerHTML = `
    <div class="stat"><span class="muted">Files</span><b>${fmtNum(c.files)}</b></div>
    <div class="stat"><span class="muted">Data</span><b>${fmtBytes(c.bytes)}</b></div>
    <div class="stat"><span class="muted">Without checksum</span><b>${fmtNum(c.unhashed)}</b></div>
    <div class="stat"><span class="muted">Reclaimable (duplicates)</span><b>${a.wasted_bytes != null ? fmtBytes(a.wasted_bytes) : "…"}</b></div>
    <div class="stat"><span class="muted">Last full scan</span><b>${fmtDate(c.last_full_scan && c.last_full_scan.finished_at)}</b></div>
    <div class="stat"><span class="muted">Last verification</span><b>${fmtDate(c.last_verify && c.last_verify.finished_at)}</b></div>
    <div class="stat"><span class="muted">In quarantine</span><b>${fmtNum(c.quarantined_files)} · ${fmtBytes(c.quarantined_bytes)}</b></div>`;
  if (!c.last_scan) box.innerHTML += `<div class="notice amber" style="flex-basis:100%">This drive has not been scanned yet. Click <b>Scan</b>.
     The first scan of 1 TB over USB takes several hours, but it can be stopped and resumed.</div>`;
}
async function loadDrives() {
  renderSummary();
  const list = $("#drive-list");
  list.innerHTML = `<p class="muted">Detecting drives…</p>`;
  try {
    const { drives } = await api("/api/drives");
    const cur = state.status && state.status.catalog && state.status.catalog.root;
    list.innerHTML = "";
    if (!drives.length) list.innerHTML = `<p class="muted">No drives found. Enter a path manually below.</p>`;
    for (const d of drives) {
      if (d.error) { list.innerHTML += `<div class="card"><p class="notice red">${esc(d.error)}</p></div>`; continue; }
      const used = d.total && d.free != null ? (100 * (d.total - d.free)) / d.total : 0;
      const card = document.createElement("div");
      card.className = "card" + (cur && d.path === cur ? " current" : "");
      const title = d.known ? esc(d.catalog.name) : esc(d.label || d.path || d.device);
      card.innerHTML = `
        <h3>${title} ${d.known ? `<span class="tag blue">${esc(ROLE[d.catalog.role] || "")}</span>` : ""}
          ${d.system ? `<span class="tag">system</span>` : ""} ${!d.mounted ? `<span class="tag amber">not mounted</span>` : ""}</h3>
        <div class="meta">${esc(d.path || d.device)} · ${esc(d.fs)} · ${esc(d.type || "")}${d.model ? " · " + esc(d.model) : ""}
          ${d.label && d.known ? " · label: " + esc(d.label) : ""}</div>
        ${d.total ? `<div class="usage"><div style="width:${used.toFixed(1)}%"></div></div>
          <div class="meta">${fmtBytes(d.total - (d.free || 0))} used of ${fmtBytes(d.total)} · ${fmtBytes(d.free)} free</div>` : ""}
        <div class="actions-box"></div>`;
      const box = card.querySelector(".actions-box");
      if (!d.mounted) {
        box.innerHTML = `<p class="hint">Mount the drive, e.g.:<br><code>${esc(d.mount_hint)}</code><br>then click Refresh.</p>`;
      } else if (d.known) {
        const b = document.createElement("button");
        b.className = "primary"; b.textContent = cur === d.path ? "Open" : "Open";
        if (cur === d.path) { b.textContent = "Opened"; b.disabled = true; }
        b.onclick = handle(async () => { await api("/api/open", { root: d.path }); afterOpen(); });
        box.appendChild(b);
      } else {
        box.appendChild(initForm(d.path, d.label));
      }
      list.appendChild(card);
    }
  } catch (e) { list.innerHTML = `<p class="notice red">${esc(e.message)}</p>`; }
}
function initForm(root, label) {
  const f = document.createElement("form");
  f.innerHTML = `<input name="name" placeholder="Drive name, e.g. Data-A" value="${esc(label || "")}" required>
    <select name="role"><option value="main">Main</option><option value="backup">Backup</option><option value="other">Other</option></select>
    <button class="primary">Initialize this drive</button>
    <span class="hint">Creates a hidden <code>.datacatalog</code> folder with the database on the drive. None of your files are changed.</span>`;
  f.onsubmit = handle(async (e) => {
    e.preventDefault();
    await api("/api/init", { root, name: f.name.value, role: f.role.value });
    closeModal();
    afterOpen();
  });
  return f;
}
function afterOpen() {
  sel.clear(); renderSelbar(); treeCache.clear(); expanded.clear(); expanded.add(""); state.builtAt = null;
  groupsState.offset = 0;
  toast("Drive opened."); api("/api/status").then((s) => { state.status = s; loadDrives(); });
}
$("#btn-refresh-drives").onclick = loadDrives;
$("#btn-manual-open").onclick = handle(async () => { await api("/api/open", { root: $("#manual-path").value.trim() }); afterOpen(); });
$("#btn-manual-init").onclick = () => {
  const root = $("#manual-path").value.trim();
  if (!root) return toast("Enter a path.");
  modal("Initialize drive", `<p>${esc(root)}</p>`, [{ label: "Close" }]);
  $("#modal-body").appendChild(initForm(root, ""));
};

// ---------- duplicate tree ----------
async function fetchTree(path) {
  if (treeCache.has(path)) return treeCache.get(path);
  const q = new URLSearchParams({ path, only_dups: $("#only-dups").checked ? "1" : "0", sort: $("#tree-sort").value });
  const d = await api("/api/tree?" + q);
  treeCache.set(path, d);
  return d;
}
async function loadTree() {
  const body = $("#tree-body");
  try {
    for (const p of Array.from(expanded)) {
      try { await fetchTree(p); } catch (e) { if (e.status === 404) expanded.delete(p); else throw e; }
    }
    renderTree();
  } catch (e) {
    body.innerHTML = `<tr><td colspan="6" class="muted">${esc(e.status === 503 ? "The analysis is being computed…" : e.message)}</td></tr>`;
    if (e.status === 503) setTimeout(() => state.tab === "tree" && loadTree(), 1500);
  }
}
function renderTree() {
  const body = $("#tree-body");
  const root = treeCache.get("");
  if (!root) return;
  const rows = [];
  if (!root.dirs.length && !root.files.length) {
    body.innerHTML = `<tr><td colspan="6" class="muted">${state.status.catalog.unhashed
      ? "No results - some files do not have checksums yet. Finish the scan."
      : "No duplicates found."}</td></tr>`;
    return;
  }
  const walk = (data, depth) => {
    for (const d of data.dirs) {
      rows.push(dirRow(d, depth));
      if (expanded.has(d.path) && treeCache.has(d.path)) walk(treeCache.get(d.path), depth + 1);
    }
    for (const f of data.files) rows.push(fileRow(f, depth));
    if (data.files_total > data.files.length)
      rows.push(`<tr><td></td><td style="padding-left:${depth * 20 + 28}px" class="muted" colspan="5">… and ${files(data.files_total - data.files.length)} more (see the Duplicate groups tab)</td></tr>`);
  };
  walk(root, 0);
  body.innerHTML = rows.join("");
}
function checkCell(p) {
  const cov = coveredBy(p);
  const on = sel.has(p) || !!cov;
  return `<td class="c-check"><input type="checkbox" data-sel="${esc(p)}" ${on ? "checked" : ""} ${cov ? "disabled" : ""}></td>`;
}
function rowCls(p) { return sel.has(p) ? "sel" : coveredBy(p) ? "sel selected-inherited" : ""; }
function dirRow(d, depth) {
  const pct = d.bytes ? (100 * d.out_bytes) / d.bytes : 0;
  const status = d.corrupt ? `<span class="tag red">${fmtNum(d.corrupt)} corrupted</span>`
    : d.fully_redundant ? `<span class="tag green">100% copy</span>`
    : d.unhashed ? `<span class="tag amber">${fmtNum(d.unhashed)} without checksum</span>`
    : d.out_files ? `<span class="tag">partial</span>` : `<span class="tag blue">copies inside</span>`;
  return `<tr class="${rowCls(d.path)}">${checkCell(d.path)}
    <td class="name" style="padding-left:${depth * 20 + 10}px"><span class="twisty" data-tw="${esc(d.path)}">${expanded.has(d.path) ? "▾" : "▸"}</span><span class="icon">📁</span>${esc(d.name)}</td>
    <td class="num">${fmtBytes(d.bytes)}</td>
    <td class="num">${fmtNum(d.dup_files)} / ${fmtNum(d.files)}</td>
    <td class="num"><span class="pct">${fmtBytes(d.out_bytes)} <span class="mini"><div style="width:${pct.toFixed(0)}%"></div></span> ${pct.toFixed(0)}%</span></td>
    <td>${status}</td></tr>`;
}
function fileRow(f, depth) {
  const st = f.corrupt ? `<span class="tag red">suspected corruption</span>`
    : f.copies > 1 ? `<a class="link" data-copies="${esc(f.hash)}">${plural(f.copies, "copy", "copies")}</a>`
    : f.hash ? `<span class="tag">unique</span>` : `<span class="tag amber">no checksum</span>`;
  return `<tr class="${rowCls(f.path)}">${checkCell(f.path)}
    <td class="name" style="padding-left:${depth * 20 + 28}px"><span class="icon">📄</span>${esc(f.name)}</td>
    <td class="num">${fmtBytes(f.size)}</td><td class="num"></td><td class="num"></td><td>${st}</td></tr>`;
}
$("#tree-body").addEventListener("click", handle(async (e) => {
  const tw = e.target.closest("[data-tw]");
  if (tw) {
    const p = tw.dataset.tw;
    if (expanded.has(p)) { for (const x of Array.from(expanded)) if (x === p || x.startsWith(p + "/")) expanded.delete(x); renderTree(); }
    else { expanded.add(p); await fetchTree(p); renderTree(); }
    return;
  }
  const cp = e.target.closest("[data-copies]");
  if (cp) showCopies(cp.dataset.copies);
}));
document.addEventListener("change", (e) => {
  if (e.target.matches("input[data-sel]")) toggleSel(e.target.dataset.sel, e.target.checked);
});
$("#only-dups").onchange = () => { treeCache.clear(); loadTree(); };
$("#tree-sort").onchange = () => { treeCache.clear(); loadTree(); };
async function showCopies(hash) {
  const d = await api("/api/copies?hash=" + encodeURIComponent(hash));
  modal(`Copies of the file (${d.copies.length})`, `<p class="muted">Size: ${fmtBytes(d.copies[0] && d.copies[0].size)}</p><ul>` +
    d.copies.map((c) => `<li>${esc(c.path)} <span class="muted">· ${fmtDate(c.mtime)}</span></li>`).join("") + "</ul>", [{ label: "Close" }]);
}

// ---------- duplicate groups ----------
const groupsState = { offset: 0, limit: 50, data: null };
async function loadGroups() {
  const q = new URLSearchParams({ offset: groupsState.offset, limit: groupsState.limit, min_size: $("#groups-min").value, q: $("#groups-q").value });
  groupsState.data = await api("/api/groups?" + q).catch((e) => { toast(e.message); return null; });
  renderGroups();
}
function renderGroups() {
  const d = groupsState.data; const box = $("#groups-list");
  if (!d) { box.innerHTML = ""; return; }
  $("#groups-info").textContent = d.total ? `Groups ${d.offset + 1}–${d.offset + d.groups.length} of ${fmtNum(d.total)}` : "No duplicates";
  $("#groups-prev").disabled = d.offset === 0;
  $("#groups-next").disabled = d.offset + d.groups.length >= d.total;
  box.innerHTML = d.groups.map((g) => {
    const allSel = g.files.every((f) => sel.has(f.path) || coveredBy(f.path));
    return `<div class="group"><div class="group-head"><b>${fmtBytes(g.size)}</b> × ${g.count}
      <span class="muted">${fmtBytes(g.wasted)} reclaimable</span>
      ${allSel ? `<span class="tag red">all copies selected - one must stay</span>` : ""}
      <div class="spacer"></div><button class="small" data-keepfirst="${esc(g.hash)}">Keep first</button></div>` +
      g.files.map((f) => `<div class="group-file ${sel.has(f.path) || coveredBy(f.path) ? "sel" : ""}">
        <input type="checkbox" data-sel="${esc(f.path)}" ${sel.has(f.path) || coveredBy(f.path) ? "checked" : ""} ${coveredBy(f.path) ? "disabled" : ""}>
        <span class="path">${esc(f.path)}</span><span class="muted">${fmtDate(f.mtime)}</span></div>`).join("") + "</div>";
  }).join("");
}
function keepFirst(paths) { paths.slice(1).forEach((p) => { if (!coveredBy(p)) sel.add(p); }); sel.delete(paths[0]); }
$("#groups-list").addEventListener("click", (e) => {
  const b = e.target.closest("[data-keepfirst]"); if (!b) return;
  const g = groupsState.data.groups.find((x) => x.hash === b.dataset.keepfirst);
  keepFirst(g.files.map((f) => f.path)); renderSelbar(); renderGroups();
});
$("#groups-keep-first").onclick = () => {
  (groupsState.data ? groupsState.data.groups : []).forEach((g) => keepFirst(g.files.map((f) => f.path)));
  renderSelbar(); renderGroups();
};
$("#groups-prev").onclick = () => { groupsState.offset = Math.max(0, groupsState.offset - groupsState.limit); loadGroups(); };
$("#groups-next").onclick = () => { groupsState.offset += groupsState.limit; loadGroups(); };
$("#groups-min").onchange = () => { groupsState.offset = 0; loadGroups(); };
let gqTimer; $("#groups-q").oninput = () => { clearTimeout(gqTimer); gqTimer = setTimeout(() => { groupsState.offset = 0; loadGroups(); }, 350); };

// ---------- identical folders ----------
const identState = { data: null };
async function loadIdentical() {
  try { identState.data = await api("/api/identical?limit=500"); }
  catch (e) {
    $("#ident-list").innerHTML = `<p class="muted">${esc(e.status === 503 ? "The analysis is being computed…" : e.message)}</p>`;
    if (e.status === 503) setTimeout(() => state.tab === "identical" && loadIdentical(), 1500);
    return;
  }
  renderIdentical();
}
function renderIdentical() {
  const d = identState.data; const box = $("#ident-list");
  if (!d) return;
  if (!d.groups.length) { box.innerHTML = `<p class="muted">No identical folders found.</p>`; return; }
  box.innerHTML = d.groups.map((g, i) => `<div class="group"><div class="group-head">
      <b>${plural(g.dirs.length, "folder")}</b> · ${files(g.files)} and ${fmtBytes(g.bytes)} each
      <span class="muted">${fmtBytes(g.wasted)} reclaimable</span><div class="spacer"></div>
      <button class="small" data-identfirst="${i}">Keep first</button></div>` +
    g.dirs.map((p) => `<div class="group-file ${sel.has(p) || coveredBy(p) ? "sel" : ""}">
      <input type="checkbox" data-sel="${esc(p)}" ${sel.has(p) || coveredBy(p) ? "checked" : ""} ${coveredBy(p) ? "disabled" : ""}>
      <span class="path">📁 ${esc(p)}</span></div>`).join("") + "</div>").join("");
}
$("#ident-list").addEventListener("click", (e) => {
  const b = e.target.closest("[data-identfirst]"); if (!b) return;
  keepFirst(identState.data.groups[+b.dataset.identfirst].dirs); renderSelbar(); renderIdentical();
});
$("#ident-keep-first").onclick = () => {
  (identState.data ? identState.data.groups : []).forEach((g) => keepFirst(g.dirs)); renderSelbar(); renderIdentical();
};

// ---------- selection -> quarantine ----------
$("#sel-clear").onclick = () => { sel.clear(); renderSelbar(); rerenderSelectionViews(); };
$("#sel-show").onclick = () => modal("Selected items", "<ul>" + Array.from(sel).sort().map((p) => `<li>${esc(p)}</li>`).join("") + "</ul>", [{ label: "Close" }]);
$("#sel-quarantine").onclick = handle(async () => {
  const paths = Array.from(sel);
  const p = await api("/api/plan", { paths });
  let html = p.movable_files ? `<p class="stat-big">${files(p.movable_files)} · ${fmtBytes(p.movable_bytes)}</p>
    <p>will be moved to quarantine. Each of them has a copy that stays on the drive.</p>`
    : `<p class="stat-big">Nothing to move</p>`;
  if (p.examples.length) html += `<details><summary>Examples (file → kept copy)</summary><ul>` +
    p.examples.map((x) => `<li>${esc(x.path)}<br><span class="muted">kept: ${esc(x.kept_copy || "(empty file)")}</span></li>`).join("") + "</ul></details>";
  if (p.blocked_files) {
    html += `<div class="notice amber"><b>Staying in place: ${files(p.blocked_files)} (${fmtBytes(p.blocked_bytes)})</b> - no copy outside the selection, or another problem.</div><ul>` +
      p.blocked.map((b) => `<li>${esc(b.path)} <span class="muted">· ${esc(b.reason)}</span></li>`).join("") + "</ul>" +
      (p.blocked_files > p.blocked.length ? `<p class="muted">… and ${fmtNum(p.blocked_files - p.blocked.length)} more</p>` : "");
  }
  const btns = [{ label: "Cancel" }];
  if (p.movable_files) btns.push({ label: `Move ${files(p.movable_files)} to quarantine`, cls: "primary", onClick: handle(async () => {
    const r = await api("/api/quarantine", { paths });
    sel.clear(); renderSelbar(); treeCache.clear();
    let msg = `Moved to quarantine: ${files(r.moved_files)} (${fmtBytes(r.moved_bytes)}).`;
    if (r.skipped_count) msg += ` Skipped: ${files(r.skipped_count)} - details below.`;
    if (r.skipped_count) modal("Result", `<p>${esc(msg)}</p><ul>` + r.skipped.map((s) => `<li>${esc(s.path)} <span class="muted">· ${esc(s.reason)}</span></li>`).join("") + "</ul>", [{ label: "OK" }]);
    else toast(msg);
  }) });
  modal("Move to quarantine", html, btns);
});

// ---------- quarantine ----------
async function loadBatches() {
  const { batches } = await api("/api/batches").catch(() => ({ batches: [] }));
  const st = { active: "in quarantine", restored: "restored", purged: "deleted" };
  $("#batch-body").innerHTML = batches.length ? batches.map((b) => `<tr>
    <td>${esc(b.id)}<br><span class="muted">${fmtDate(b.created_at)}</span></td>
    <td class="num">${fmtNum(b.files)}</td><td class="num">${fmtBytes(b.bytes)}</td>
    <td><span class="tag ${b.status === "active" ? "amber" : b.status === "restored" ? "green" : ""}">${st[b.status] || b.status}</span></td>
    <td>${b.status === "active" ? `<button class="small" data-bshow="${esc(b.id)}">Files</button>
      <button class="small" data-brestore="${esc(b.id)}">Restore</button>
      <button class="small danger" data-bpurge="${esc(b.id)}">Delete permanently</button>` : ""}</td></tr>`).join("")
    : `<tr><td colspan="5" class="muted">Quarantine is empty.</td></tr>`;
}
$("#batch-body").addEventListener("click", handle(async (e) => {
  const t = e.target;
  if (t.dataset.bshow) {
    const r = await api("/api/batch?id=" + encodeURIComponent(t.dataset.bshow));
    modal(`Batch ${t.dataset.bshow}`, "<ul>" + r.files.map((f) => `<li>${esc(f.path)} <span class="muted">· ${fmtBytes(f.size)}</span></li>`).join("") + "</ul>", [{ label: "Close" }]);
  } else if (t.dataset.brestore) {
    const r = await api("/api/restore", { batch: t.dataset.brestore });
    toast(`Restored: ${files(r.restored)}.` + (r.problems.length ? ` Problems: ${r.problems.length}` : ""));
    loadBatches();
  } else if (t.dataset.bpurge) {
    const id = t.dataset.bpurge;
    modal("Permanent deletion", `<div class="notice red">The files of batch <b>${esc(id)}</b> will be deleted forever. This cannot be undone.</div>
      <p>If you keep a backup on a second drive, synchronize first. Type <b>DELETE</b> to confirm:</p>
      <input id="purge-confirm" autocomplete="off">`, [
      { label: "Cancel" },
      { label: "Delete permanently", cls: "danger", onClick: handle(async () => {
        const r = await api("/api/purge", { batch: id, confirm: $("#purge-confirm").value.trim() });
        toast(`Permanently deleted: ${files(r.deleted)}.`); loadBatches();
      }) }]);
  }
}));

// ---------- problems ----------
async function loadIssues() {
  const d = await api("/api/issues").catch((e) => { toast(e.message); return null; });
  if (!d) return;
  $("#integrity-body").innerHTML = d.integrity.length ? d.integrity.map((i) => `<tr>
    <td>${esc(i.path)}</td><td class="num">${fmtBytes(i.size)}</td><td>${fmtDate(i.detected_at)}</td>
    <td>${i.good_copy ? `<span class="tag green">${esc(i.good_copy)}</span>` : `<span class="muted">none - check the backup drive</span>`}</td>
    <td>${i.good_copy ? `<button class="small primary" data-irepair="${i.id}" title="Overwrite the file with the good copy; the corrupted version goes to quarantine">Repair from copy</button>` : ""}
      <button class="small" data-iaccept="${i.id}" title="I changed this file on purpose - accept the new content as correct">I changed it</button></td></tr>`).join("")
    : `<tr><td colspan="5"><span class="tag green">No corruption detected</span> <span class="muted">(run Verify to check)</span></td></tr>`;
  $("#missing-box").innerHTML = d.missing_total ? `<p>${files(d.missing_total)}.
    <button class="small" id="btn-forget-missing">Remove these entries from the catalog</button></p><ul>` +
    d.missing.map((m) => `<li>${esc(m.path)} <span class="muted">· ${fmtBytes(m.size)}</span></li>`).join("") + "</ul>"
    : `<p class="muted">None.</p>`;
  $("#errors-box").innerHTML = d.errors.length ? "<ul>" + d.errors.map((e) => `<li>${esc(e.path)} <span class="muted">· ${esc(e.error)}</span></li>`).join("") + "</ul>"
    : `<p class="muted">None.</p>`;
  const fm = $("#btn-forget-missing");
  if (fm) fm.onclick = handle(async () => {
    modal("Remove entries?", `<p>The entries of ${files(d.missing_total)} will be removed from the catalog and recorded in the journal as confirmed deletions.
      During synchronization these files will also be deleted from the backup.</p>`,
      [{ label: "Cancel" }, { label: "Remove entries", cls: "primary", onClick: handle(async () => { await api("/api/forget_missing", {}); loadIssues(); }) }]);
  });
}
$("#integrity-body").addEventListener("click", handle(async (e) => {
  const id = e.target.dataset.iaccept || e.target.dataset.irepair;
  if (!id) return;
  const r = await api("/api/issue/resolve", { id: +id, action: e.target.dataset.irepair ? "repair" : "accept" });
  if (r.repaired) toast(`Repaired from copy ${r.from}. The corrupted version is in ${r.damaged_copy}.`, 6000);
  loadIssues();
}));

// ---------- settings ----------
function loadSettings() {
  const c = state.status && state.status.catalog; if (!c) return;
  $("#set-name").value = c.volume.name; $("#set-role").value = c.volume.role;
  $("#set-excludes").value = (c.excludes || []).join("\n");
  $("#set-include").value = (c.include || []).join("\n");
  $("#set-info").textContent = `Drive ID: ${c.volume.volume_id} · database: ${c.root}/.datacatalog/catalog.db`;
}
$("#btn-save-settings").onclick = handle(async () => {
  const lines = (s) => s.split("\n").map((x) => x.trim()).filter(Boolean);
  state.status = await api("/api/settings", {
    name: $("#set-name").value.trim(), role: $("#set-role").value,
    excludes: lines($("#set-excludes").value), include: lines($("#set-include").value),
  });
  toast("Saved. Scope changes take effect on the next scan.");
});
$("#btn-close-disk").onclick = handle(async () => {
  await api("/api/close", {}); sel.clear(); renderSelbar(); state.builtAt = null; treeCache.clear();
  showTab("drives");
});

// ---------- start ----------
refreshStatus().then(() => showTab("drives"));
