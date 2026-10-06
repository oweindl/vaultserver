// VaultServer – Web-Oberfläche (ohne Build-Schritt)
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const enc = (p) => p.split("/").map(encodeURIComponent).join("/");
const slug = (s) => s.toLowerCase().replace(/[^\p{L}\p{N}_\- ]/gu, "").trim().replace(/ /g, "-");
const fmtDate = (s) => { const d = typeof s === "number" ? new Date(s * 1000) : new Date(s); return d.toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }); };
const kb = (n) => n > 1024 ? `${(n / 1024).toFixed(n > 10240 ? 0 : 1)} KB` : `${n} B`;

const state = { tree: null, open: new Set(JSON.parse(localStorage.getItem("vs-open") || "[]")), note: null, editor: null, dirty: false, me: null };

// ------------------------------------------------------------------ API

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  if (r.status === 401 && !url.startsWith("/api/login")) { showLogin(); const e = new Error("Anmeldung erforderlich"); e.status = 401; throw e; }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) { const e = new Error(data.message || data.error || r.statusText); e.status = r.status; e.data = data; throw e; }
  return data;
}
const get = (u) => api("GET", u);

function toast(msg, ms = 2600) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), ms);
}
function fail(e) {
  if (e.data?.problems?.length) toast(`${e.message}: ${e.data.problems.join("; ")}`, 6000);
  else toast(e.message || String(e), 5000);
}

// ------------------------------------------------------------------ Anmeldung

function showLogin() { $("#login").style.display = "flex"; $("#login-user").focus(); }
$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("POST", "/api/login", { user: $("#login-user").value, password: $("#login-pw").value });
    $("#login").style.display = "none"; $("#login-err").textContent = ""; boot();
  } catch (e) { $("#login-err").textContent = e.message; }
});
async function logout() { await api("POST", "/api/logout"); location.hash = ""; showLogin(); }

async function changePassword() {
  const r = await dialog(`Passwort ändern${state.me?.user ? ` (${state.me.user})` : ""}`, [
    { name: "old", label: "Bisheriges Passwort", type: "password", required: true, autocomplete: "current-password" },
    { name: "new", label: "Neues Passwort (mindestens 10 Zeichen)", type: "password", required: true, autocomplete: "new-password" },
    { name: "repeat", label: "Neues Passwort wiederholen", type: "password", required: true, autocomplete: "new-password" },
  ], "Ändern");
  if (!r) return;
  if (r.new !== r.repeat) { toast("Die beiden neuen Passwörter stimmen nicht überein", 4000); return changePassword(); }
  try {
    await api("POST", "/api/password", { old: r.old, new: r.new });
    toast("Passwort geändert. Andere Sitzungen sind abgemeldet.", 4000);
  } catch (e) { fail(e); }
}

const accountItems = () => [["Einrichtung (Claude/MCP) …", () => go("#/setup")], ["Passwort ändern …", changePassword], ["Abmelden", logout]];
$("#btn-account").onclick = (ev) => { ev.stopPropagation(); menu(ev, accountItems()); };

// ------------------------------------------------------------------ Baum

function buildTree(data) {
  const root = { name: "", path: "", dirs: {}, files: [] };
  const add = (p, item) => {
    const parts = p.split("/"); let n = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const key = parts[i];
      n.dirs[key] ??= { name: key, path: parts.slice(0, i + 1).join("/"), dirs: {}, files: [] };
      n = n.dirs[key];
    }
    n.files.push(item);
  };
  data.notes.forEach((n) => add(n.path, { ...n, kind: "note" }));
  data.attachments.forEach((a) => add(a.path, { ...a, kind: "att" }));
  return root;
}

async function loadTree() {
  state.tree = buildTree(await get("/api/tree"));
  renderTree();
}

function renderTree() {
  const el = $("#tree"); el.innerHTML = "";
  const walk = (node, parent) => {
    Object.values(node.dirs).sort((a, b) => a.name.localeCompare(b.name, "de")).forEach((d) => {
      const open = state.open.has(d.path);
      const row = document.createElement("div");
      row.className = "node dir"; row.dataset.dir = d.path; row.draggable = true;
      row.innerHTML = `<span class="tw">${open ? "▾" : "▸"}</span><span class="ic">📁</span><span>${esc(d.name)}</span>`;
      const kids = document.createElement("div"); kids.className = "children" + (open ? "" : " closed");
      row.onclick = () => {
        const o = !state.open.has(d.path);
        o ? state.open.add(d.path) : state.open.delete(d.path);
        localStorage.setItem("vs-open", JSON.stringify([...state.open]));
        kids.classList.toggle("closed", !o); row.querySelector(".tw").textContent = o ? "▾" : "▸";
      };
      parent.append(row, kids); walk(d, kids);
    });
    node.files.sort((a, b) => a.path.localeCompare(b.path, "de")).forEach((f) => {
      const row = document.createElement("div");
      const name = f.path.split("/").pop();
      row.className = "node " + (f.kind === "note" ? "note" : "att");
      row.dataset.path = f.path; row.draggable = true;
      row.title = f.kind === "note" ? `${f.title}\n${kb(f.size)} · ${fmtDate(f.mtime)}` : `${f.type} · ${kb(f.size)}`;
      row.innerHTML = `<span class="tw"></span><span class="ic">${f.kind === "note" ? "📄" : "📎"}</span><span>${esc(f.kind === "note" ? name.replace(/\.md$/, "") : name)}</span>`;
      row.onclick = () => f.kind === "note" ? go(`#/note/${enc(f.path)}`) : openViewer(f.path);
      parent.append(row);
    });
  };
  walk(state.tree, el);
  markActive();
}

function markActive() {
  document.querySelectorAll("#tree .node.active").forEach((n) => n.classList.remove("active"));
  if (!state.note) return;
  const row = [...document.querySelectorAll("#tree .node.note")].find((n) => n.dataset.path === state.note.path);
  if (row) row.classList.add("active");
}

function revealInTree(path) {
  const parts = path.split("/"); let changed = false;
  for (let i = 1; i < parts.length; i++) { const p = parts.slice(0, i).join("/"); if (!state.open.has(p)) { state.open.add(p); changed = true; } }
  if (changed) { localStorage.setItem("vs-open", JSON.stringify([...state.open])); renderTree(); } else markActive();
}

// Ziehen und Ablegen: Notiz/Ordner auf Ordner verschieben
$("#tree").addEventListener("dragstart", (ev) => {
  const n = ev.target.closest(".node"); if (!n) return;
  ev.dataTransfer.setData("text/plain", n.dataset.path || n.dataset.dir);
});
$("#tree").addEventListener("dragover", (ev) => { const d = ev.target.closest(".node.dir"); if (d) { ev.preventDefault(); d.classList.add("drop"); } });
$("#tree").addEventListener("dragleave", (ev) => ev.target.closest(".node.dir")?.classList.remove("drop"));
$("#tree").addEventListener("drop", async (ev) => {
  const d = ev.target.closest(".node.dir"); if (!d) return;
  ev.preventDefault(); d.classList.remove("drop");
  if (ev.dataTransfer.files.length) { for (const f of ev.dataTransfer.files) await uploadFile(d.dataset.dir, f); return; }
  const src = ev.dataTransfer.getData("text/plain"); if (!src) return;
  const target = `${d.dataset.dir}/${src.split("/").pop()}`;
  if (target === src || !confirm(`„${src}“ nach „${d.dataset.dir}/“ verschieben? Links werden nachgezogen.`)) return;
  await moveItem(src, target);
});

async function moveItem(src, target) {
  try {
    if (state.tree && isDir(src)) { // Ordner: jede Datei einzeln verschieben
      const files = allFiles().filter((p) => p.startsWith(src + "/"));
      for (const f of files) await api("POST", "/api/move", { source: f, target: target + f.slice(src.length) });
    } else {
      const r = await api("POST", "/api/move", { source: src, target });
      toast(`Verschoben${r.updated_notes.length ? `, Links in ${r.updated_notes.length} Notiz(en) angepasst` : ""}`);
    }
    await loadTree();
    if (state.note?.path === src) go(`#/note/${enc(target.endsWith(".md") ? target : target + ".md")}`);
  } catch (e) { fail(e); }
}
const allFiles = () => { const out = []; const w = (n) => { n.files.forEach((f) => out.push(f.path)); Object.values(n.dirs).forEach(w); }; w(state.tree); return out; };
const isDir = (p) => { let n = state.tree; for (const part of p.split("/")) { n = n.dirs[part]; if (!n) return false; } return true; };

async function uploadFile(folder, file) {
  const fd = new FormData(); fd.append("file", file);
  const r = await fetch(`/api/upload?folder=${encodeURIComponent(folder)}`, { method: "POST", body: fd });
  if (!r.ok) return fail(new Error((await r.json()).message || r.statusText));
  toast(`Hochgeladen: ${file.name}`); loadTree();
}

// Kontextmenü
function menu(ev, items) {
  ev.preventDefault();
  const m = $("#menu"); m.innerHTML = "";
  items.forEach(([label, fn]) => { const d = document.createElement("div"); d.textContent = label; d.onclick = () => { hideMenu(); fn(); }; m.append(d); });
  m.style.display = "block";
  m.style.left = Math.min(ev.clientX, innerWidth - 200) + "px"; m.style.top = Math.min(ev.clientY, innerHeight - m.offsetHeight - 8) + "px";
}
const hideMenu = () => ($("#menu").style.display = "none");
document.addEventListener("click", hideMenu);
$("#tree").addEventListener("contextmenu", (ev) => {
  const n = ev.target.closest(".node"); if (!n) return;
  const p = n.dataset.path, d = n.dataset.dir;
  if (d) menu(ev, [
    ["Neue Notiz hier", () => newNote(d)],
    ["Datei hochladen …", () => pickUpload(d)],
    ["Ordner umbenennen/verschieben …", () => askMove(d)],
  ]);
  else menu(ev, [
    ["Öffnen", () => n.click()],
    ["Umbenennen/verschieben …", () => askMove(p)],
    ...(p.endsWith(".md") ? [["Löschen …", () => delNote(p)]] : []),
  ]);
});
function pickUpload(folder) {
  const i = document.createElement("input"); i.type = "file"; i.multiple = true;
  i.onchange = async () => { for (const f of i.files) await uploadFile(folder, f); }; i.click();
}

// ------------------------------------------------------------------ Dateiansicht im Popup

const EXT = (p) => (p.split(".").pop() || "").toLowerCase();
const KIND = {
  image: ["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "avif"],
  pdf: ["pdf"], html: ["html", "htm"],
  video: ["mp4", "webm", "mov", "m4v", "ogv"], audio: ["mp3", "wav", "ogg", "m4a", "flac"],
  text: ["md", "txt", "csv", "json", "yaml", "yml", "xml", "log", "base", "canvas", "css", "js", "py", "sh", "toml", "ini"],
};
const kindOf = (p) => Object.keys(KIND).find((k) => KIND[k].includes(EXT(p))) || "other";

async function openViewer(path) {
  const url = `/api/file/${enc(path)}`, body = $("#v-body"), v = $("#viewer");
  $("#v-name").textContent = path; $("#v-download").href = url; $("#v-tab").href = url;
  body.className = "v-body"; body.innerHTML = "";
  const k = kindOf(path);
  if (k === "image") {
    const img = document.createElement("img"); img.src = url; img.alt = path;
    img.onclick = () => { img.classList.toggle("zoom"); body.classList.toggle("zoomed"); };
    body.append(img);
  } else if (k === "pdf") body.innerHTML = `<iframe src="${url}"></iframe>`;
  else if (k === "html") body.innerHTML = `<iframe src="${url}" sandbox="allow-scripts"></iframe>`;
  else if (k === "video") body.innerHTML = `<video src="${url}" controls autoplay></video>`;
  else if (k === "audio") body.innerHTML = `<audio src="${url}" controls autoplay></audio>`;
  else if (k === "text") {
    try { const t = await (await fetch(url)).text(); const pre = document.createElement("pre"); pre.textContent = t; body.append(pre); }
    catch (e) { body.innerHTML = `<div class="v-info">${esc(e.message)}</div>`; }
  } else body.innerHTML = `<div class="v-info">Keine Vorschau für .${esc(EXT(path))}-Dateien.<br><br><a href="${url}" download>Herunterladen</a></div>`;
  if (!v.open) v.showModal();
}
function closeViewer() { const v = $("#viewer"); if (v.open) v.close(); $("#v-body").innerHTML = ""; }
$("#v-close").onclick = closeViewer;
$("#viewer").addEventListener("close", () => ($("#v-body").innerHTML = ""));  // Video/Audio anhalten
$("#viewer").addEventListener("click", (ev) => { if (ev.target.id === "viewer") closeViewer(); }); // Klick auf den Hintergrund
// Links und eingebettete Bilder in Notizen: Anhänge im Popup statt neuem Tab
$("#content").addEventListener("click", (ev) => {
  if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.button !== 0) return;  // Strg/⌘-Klick: wie gewohnt neuer Tab
  const el = ev.target.closest("a[href^='/api/file/'], img[data-file]");
  if (!el || !el.closest(".md")) return;
  ev.preventDefault();
  const path = el.dataset.file || decodeURIComponent(el.getAttribute("href").slice("/api/file/".length));
  openViewer(path);
});

// ------------------------------------------------------------------ Dialoge

function dialog(title, fields, okLabel = "OK") {
  return new Promise((resolve) => {
    const dlg = $("#dlg"), form = $("#dlg-form");
    form.innerHTML = `<h3>${esc(title)}</h3>` + fields.map((f) => {
      const id = `f-${f.name}`;
      let input;
      if (f.type === "select") input = `<select id="${id}" name="${f.name}">${f.options.map((o) => `<option ${o === f.value ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
      else if (f.type === "textarea") input = `<textarea id="${id}" name="${f.name}" placeholder="${esc(f.placeholder || "")}">${esc(f.value || "")}</textarea>`;
      else if (f.type === "password") input = `<input type="password" id="${id}" name="${f.name}" autocomplete="${f.autocomplete || "off"}" ${f.required ? "required" : ""}>`;
      else input = `<input type="text" id="${id}" name="${f.name}" value="${esc(f.value || "")}" placeholder="${esc(f.placeholder || "")}">`;
      return `<div class="row"><label for="${id}">${esc(f.label)}${f.required ? " *" : ""}</label>${input}</div>`;
    }).join("") + `<div class="btns"><button value="cancel" formnovalidate>Abbrechen</button><button class="primary" value="ok">${esc(okLabel)}</button></div>`;
    dlg.onclose = () => {
      if (dlg.returnValue !== "ok") return resolve(null);
      const out = {}; fields.forEach((f) => { const v = form.elements[f.name].value; out[f.name] = f.type === "password" ? v : v.trim(); }); resolve(out);
    };
    dlg.returnValue = ""; dlg.showModal(); form.querySelector("input,textarea,select")?.focus();
  });
}

async function askMove(src) {
  const r = await dialog("Umbenennen / verschieben", [{ name: "target", label: "Neuer Pfad", value: src }], "Verschieben");
  if (r?.target && r.target !== src) moveItem(src, r.target);
}

async function newNote(folder = "") {
  const r = await dialog("Neue Notiz", [{ name: "path", label: "Pfad (ohne .md)", value: folder ? folder + "/" : "" }], "Anlegen");
  if (!r?.path) return;
  const title = r.path.split("/").pop();
  try {
    const res = await api("PUT", "/api/note", { path: r.path, text: `# ${title}\n\n` });
    await loadTree(); go(`#/note/${enc(res.path)}?edit=1`);
  } catch (e) { fail(e); }
}

async function newEntry(area) {
  const g = await get(`/api/guide?area=${encodeURIComponent(area)}`);
  const a = g.area;
  const fields = [{ name: "title", label: "Titel", required: true }];
  a.required.forEach((k) => {
    const allowed = a.allowed[k.toLowerCase()];
    if (allowed) fields.push({ name: k, label: k, type: "select", options: allowed, value: k.toLowerCase() === "status" ? "offen" : allowed[0], required: true });
    else fields.push({ name: k, label: k + (k === "Akzeptanzkriterien" ? " (eine pro Zeile)" : ""), type: ["Ist", "Soll", "Akzeptanzkriterien"].includes(k) ? "textarea" : "text", required: true });
  });
  fields.push({ name: "__summary", label: "Kurzbeschreibung für die Übersicht", type: "textarea" });
  const r = await dialog(`Neuer Eintrag: ${area}`, fields, "Anlegen");
  if (!r) return;
  const f = {};
  a.required.forEach((k) => { const v = r[k]; f[k] = k === "Akzeptanzkriterien" ? v.split("\n").map((s) => s.replace(/^[-*]\s*(\[ \]\s*)?/, "").trim()).filter(Boolean) : v; });
  try {
    const res = await api("POST", "/api/create", { area, title: r.title, fields: f, summary: r.__summary });
    toast(`${res.path.split("/").pop()} angelegt`); await loadTree(); go(`#/note/${enc(res.path)}`);
  } catch (e) { fail(e); }
}

$("#btn-new").onclick = (ev) => {
  ev.stopPropagation();
  const folder = state.note ? state.note.path.split("/").slice(0, -1).join("/") : "";
  menu(ev, [["Notiz …", () => newNote(folder)], ...(state.me?.areas || []).map((a) => [`${a}-Eintrag …`, () => newEntry(a)])]);
};

async function delNote(path) {
  if (!confirm(`„${path}“ löschen? (bleibt in der Git-Historie)`)) return;
  try {
    const n = await get(`/api/note?path=${encodeURIComponent(path)}&raw=1`);
    await api("DELETE", `/api/note?path=${encodeURIComponent(path)}&base_version=${n.version}`);
    toast("Gelöscht"); await loadTree(); if (state.note?.path === path) go("#/");
  } catch (e) { fail(e); }
}

// ------------------------------------------------------------------ Navigation

function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }
window.addEventListener("hashchange", () => route());
window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });

async function route() {
  if (state.dirty && !confirm("Ungespeicherte Änderungen verwerfen?")) return;
  state.dirty = false; state.editor = null;
  document.body.classList.remove("tree-open");
  const h = decodeURI(location.hash.slice(1) || "/");
  try {
    if (h.startsWith("/note/")) {
      const rest = location.hash.slice("#/note/".length);
      const [pathPart, ...anchorParts] = rest.split("#");
      const [p, qs] = pathPart.split("?");
      const path = p.split("/").map(decodeURIComponent).join("/");
      return showNote(path, anchorParts.join("#"), new URLSearchParams(qs || "").get("edit") === "1");
    }
    if (h.startsWith("/search")) { const q = new URLSearchParams(h.split("?")[1]); return showSearch(q.get("q") || "", q.get("archive") === "1"); }
    if (h === "/lint") return showLint();
    if (h === "/recent") return showRecent();
    if (h === "/tasks") return showTasks();
    if (h === "/setup") return showSetup();
    return showHome();
  } catch (e) { if (e.status !== 401) $("#content").innerHTML = `<div class="banner warn">${esc(e.message)}</div>`; }
}

// ------------------------------------------------------------------ Notiz anzeigen

async function showNote(path, anchor, edit) {
  const n = await get(`/api/note?path=${encodeURIComponent(path)}`);
  state.note = n; revealInTree(path);
  document.title = `${n.title} – VaultServer`;
  if (edit) return editNote();
  const claim = n.claim ? `<div class="banner info">Reserviert von <b>${esc(n.claim.agent)}</b> bis ${fmtDate(n.claim.expires_at)}${n.claim.note ? ` – ${esc(n.claim.note)}` : ""}</div>` : "";
  $("#content").innerHTML = `
    <div class="note-head">
      <div class="path">${esc(path)} · ${kb(new Blob([n.text]).size)}${n.area ? ` <span class="tag">${esc(n.area)}</span>` : ""}</div>
      <div class="actions">
        <button id="b-edit" class="primary" title="Bearbeiten (E)">Bearbeiten</button>
        ${n.area ? `<button id="b-claim">${n.claim ? "Freigeben" : "Reservieren"}</button>` : ""}
        <button id="b-more">…</button>
      </div>
    </div>${claim}
    <article class="md">${n.html}</article>`;
  $("#b-edit").onclick = () => go(`#/note/${enc(path)}?edit=1`);
  $("#b-more").onclick = (ev) => { ev.stopPropagation(); menu(ev, [
    ["Umbenennen/verschieben …", () => askMove(path)],
    ["Rohtext anzeigen", () => openViewer(path)],
    ["Löschen …", () => delNote(path)],
  ]); };
  if ($("#b-claim")) $("#b-claim").onclick = async () => {
    try { await api("POST", "/api/claim", { path, release: !!n.claim }); toast(n.claim ? "Freigegeben" : "Reserviert"); showNote(path); } catch (e) { fail(e); }
  };
  $("#content").querySelectorAll("input.task").forEach((cb) => cb.addEventListener("change", async () => {
    try {
      const r = await api("POST", "/api/task", { path, line: +cb.dataset.line, base_version: state.note.version });
      state.note.version = r.version; toast("Gespeichert");
    } catch (e) { cb.checked = !cb.checked; fail(e); if (e.status === 409) showNote(path); }
  }));
  renderSide(n);
  $("#center").scrollTop = 0;
  if (anchor) scrollToAnchor(anchor);
}

function scrollToAnchor(a) {
  const el = document.getElementById(decodeURIComponent(a)) || document.getElementById(slug(decodeURIComponent(a)));
  if (el) { el.scrollIntoView({ block: "start" }); el.classList.add("flash"); }
}

function renderSide(n) {
  const area = n.area;
  const allowed = {};
  if (area && state.guide?.[area]) Object.assign(allowed, state.guide[area].allowed);
  const props = n.properties.filter((p, i, a) => a.findIndex((q) => q.key_norm === p.key_norm) === i).slice(0, 25);
  const outline = n.outline.filter((o) => o.level > 0);
  $("#side").innerHTML = `
    ${n.violations.length ? `<h4>Regeln</h4><ul>${n.violations.map((v) => `<li class="tag warn" style="display:block;margin:2px 0">${esc(v)}</li>`).join("")}</ul>` : ""}
    <h4>Gliederung</h4>
    ${outline.length ? `<ul class="outline">${outline.map((o) => `<li style="padding-left:${(o.level - 1) * 10}px"><a href="#" data-a="${esc(slug(o.heading))}"><span class="size">${kb(o.size)}</span>${esc(o.heading)}</a></li>`).join("")}</ul>` : `<div class="empty">keine Überschriften</div>`}
    <h4>Eigenschaften</h4>
    ${props.length ? `<table class="props">${props.map((p) => {
      const opts = allowed[p.key_norm];
      const val = opts ? `<select data-key="${esc(p.key)}">${[...new Set([p.value_norm, ...opts])].map((o) => `<option ${o === p.value_norm ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`
        : `${esc(p.value.length > 80 ? p.value.slice(0, 80) + "…" : p.value)}${p.value_norm !== p.value.toLowerCase() ? ` <span class="tag">${esc(p.value_norm)}</span>` : ""}`;
      return `<tr><td>${esc(p.key)}</td><td>${val}</td></tr>`;
    }).join("")}</table>` : `<div class="empty">keine</div>`}
    <h4>Backlinks (${n.backlinks.length})</h4>
    ${n.backlinks.length ? `<ul>${n.backlinks.map((b) => `<li><a href="#/note/${enc(b.path)}">${esc(b.path.split("/").pop().replace(/\.md$/, ""))}</a>${b.heading_path ? `<div class="hist meta">${esc(b.heading_path)}</div>` : ""}</li>`).join("")}</ul>` : `<div class="empty">keine</div>`}
    <h4>Verlauf</h4>
    ${n.history.length ? `<ul class="hist">${n.history.map((h) => `<li>${esc(h.message)}<div class="meta">${esc(h.author)} · ${fmtDate(h.date)} · ${h.commit.slice(0, 7)}</div></li>`).join("")}</ul>` : `<div class="empty">kein Git</div>`}`;
  $("#side").querySelectorAll("a[data-a]").forEach((a) => (a.onclick = (ev) => { ev.preventDefault(); scrollToAnchor(a.dataset.a); }));
  $("#side").querySelectorAll("select[data-key]").forEach((s) => (s.onchange = async () => {
    try { await api("POST", "/api/property", { path: n.path, key: s.dataset.key, value: s.value, base_version: state.note.version }); toast("Gespeichert"); showNote(n.path); }
    catch (e) { fail(e); showNote(n.path); }
  }));
}

// ------------------------------------------------------------------ Editor

function editNote() {
  const n = state.note;
  $("#content").innerHTML = `
    <div class="note-head">
      <div class="path">${esc(n.path)} – bearbeiten</div>
      <div class="actions">
        <input type="text" id="e-msg" placeholder="Änderungsnotiz (optional)" style="width:220px">
        <button id="e-cancel">Abbrechen</button>
        <button id="e-save" class="primary" title="Strg+S">Speichern</button>
      </div>
    </div>
    <div id="e-banner"></div>
    <div class="editor-wrap"><textarea id="e-text"></textarea></div>`;
  const cm = CodeMirror.fromTextArea($("#e-text"), {
    mode: "yaml-frontmatter", lineWrapping: true, lineNumbers: false, indentUnit: 2, tabSize: 2,
    extraKeys: {
      "Enter": "newlineAndIndentContinueMarkdownList",
      "Ctrl-S": () => save(), "Cmd-S": () => save(),
      "Ctrl-Space": (c) => wikiHint(c),
      "Esc": () => $("#e-cancel").click(),
    },
  });
  cm.setValue(n.text); cm.clearHistory(); cm.focus();
  state.editor = cm; state.base = n.version;
  cm.on("change", () => (state.dirty = true));
  cm.on("inputRead", (c, ch) => { if (ch.text[0] === "[" && c.getRange({ line: ch.from.line, ch: ch.from.ch - 1 }, ch.from) === "[") wikiHint(c); });
  $("#e-cancel").onclick = () => { if (!state.dirty || confirm("Änderungen verwerfen?")) { state.dirty = false; go(`#/note/${enc(n.path)}`); } };
  $("#e-save").onclick = () => save();
  renderSide(n);

  async function save(force = false) {
    try {
      const r = await api("PUT", "/api/note", { path: n.path, text: cm.getValue(), base_version: state.base, message: $("#e-msg").value || undefined, force });
      state.dirty = false; toast("Gespeichert"); go(`#/note/${enc(n.path)}`);
      if (r.created) loadTree();
    } catch (e) {
      if (e.status === 409) return showConflict(cm, e.data);
      fail(e);
    }
  }
  editNote.save = save;
}

// Wikilink-Vervollständigung nach "[["
function wikiHint(cm) {
  cm.showHint({
    completeSingle: false,
    hint: async (c) => {
      const cur = c.getCursor(), line = c.getLine(cur.line);
      const start = line.lastIndexOf("[[", cur.ch);
      if (start < 0) return null;
      const q = line.slice(start + 2, cur.ch);
      if (q.includes("]]")) return null;
      const items = await get(`/api/complete?q=${encodeURIComponent(q)}`);
      return {
        list: items.map((i) => ({ text: `${i.link}]]`, displayText: `${i.title}  —  ${i.path}` })),
        from: CodeMirror.Pos(cur.line, start + 2), to: cur,
      };
    },
  });
}

// Konflikt: eigener Stand und aktueller Stand nebeneinander
function showConflict(cm, data) {
  const mine = cm.getValue(), theirs = data.current_text ?? "";
  const a = theirs.split("\n"), b = mine.split("\n");
  const setA = new Set(a), setB = new Set(b);
  const fmt = (lines, other, cls) => lines.map((l) => `<span class="${other.has(l) ? "" : cls}">${esc(l) || " "}</span>`).join("\n");
  $("#e-banner").innerHTML = `
    <div class="banner warn">Die Notiz wurde inzwischen geändert (z. B. von einem Agenten). Links der aktuelle Stand, rechts deiner.
      Übernimm Teile in den Editor und speichere dann auf Basis des aktuellen Stands.</div>
    <div class="conflict"><div><b>Aktuell gespeichert</b><pre>${fmt(a, setB, "del")}</pre></div><div><b>Deine Fassung</b><pre>${fmt(b, setA, "add")}</pre></div></div>
    <p class="actions" style="display:flex;gap:8px;flex-wrap:wrap">
      <button id="c-mine" class="primary">Meine Fassung speichern (überschreibt)</button>
      <button id="c-theirs">Aktuellen Stand in den Editor laden</button>
      <button id="c-close">Schließen</button></p>`;
  state.base = data.current_version;
  $("#c-mine").onclick = () => editNote.save();
  $("#c-theirs").onclick = () => { cm.setValue(theirs); $("#e-banner").innerHTML = ""; };
  $("#c-close").onclick = () => ($("#e-banner").innerHTML = "");
}

// ------------------------------------------------------------------ Suche und Listen

$("#search-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = $("#q").value.trim(); if (q) go(`#/search?q=${encodeURIComponent(q)}${$("#q-archive").checked ? "&archive=1" : ""}`);
});
document.addEventListener("keydown", (ev) => {
  if ((ev.ctrlKey || ev.metaKey) && ev.key === "k") { ev.preventDefault(); $("#q").focus(); $("#q").select(); }
  if (ev.key === "e" && !state.editor && state.note && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && location.hash.startsWith("#/note/")) go(`#/note/${enc(state.note.path)}?edit=1`);
});

async function showSearch(q, archive) {
  $("#q").value = q; $("#q-archive").checked = archive; state.note = null; markActive();
  const hits = await get(`/api/search?q=${encodeURIComponent(q)}&archive=${archive ? 1 : 0}&mode=${state.me?.semantic ? "auto" : "text"}`);
  $("#content").innerHTML = `<div class="results"><h2>${hits.length} Treffer für „${esc(q)}“</h2>${hits.map((h) => {
    const anchor = h.heading_path ? "#" + slug(h.heading_path.split(" > ").pop()) : "";
    const snip = esc(h.snippet).replace(/«/g, "<mark>").replace(/»/g, "</mark>");
    return `<div class="hit"><a class="t" href="#/note/${enc(h.path)}${anchor}">${esc(h.title)}</a>${h.semantic ? `<span class="tag">ähnlich</span>` : ""}
      <div class="hp">${esc(h.path)}${h.heading_path ? " › " + esc(h.heading_path) : ""}</div><div class="snip">${snip}</div></div>`;
  }).join("") || `<p class="empty">Nichts gefunden.</p>`}</div>`;
  $("#side").innerHTML = "";
}

async function showLint() {
  state.note = null; markActive();
  $("#content").innerHTML = `<p class="empty">Prüfe …</p>`;
  const rows = await get("/api/lint");
  const kinds = { "kaputter-link": "Kaputte Links", regel: "Regeln", "doppelte-nummer": "Doppelte Nummern", "fehlt-in-uebersicht": "Fehlt in Übersicht", claim: "Reservierungen" };
  const by = {}; rows.forEach((r) => (by[r.kind] ??= []).push(r));
  $("#content").innerHTML = `<div class="results"><h2>Prüfung: ${rows.length} Hinweise</h2>
    <p style="display:flex;gap:8px;flex-wrap:wrap">${(state.me?.areas || []).map((a) => `<button data-ov="${esc(a)}">Übersicht ${esc(a)} erneuern</button>`).join("")}
    <button data-st="1">Statusblock erneuern</button><button data-cm="1">Commits verknüpfen</button></p>
    ${Object.entries(by).map(([k, list]) => `<h3>${esc(kinds[k] || k)} (${list.length})</h3>${list.map((r) =>
      `<div class="hit"><a href="#/note/${enc(r.path)}">${esc(r.path)}</a>${r.line ? ` <span class="hp">Zeile ${r.line}</span>` : ""}<div class="snip">${esc(r.message)}</div></div>`).join("")}`).join("")
    || `<p>Alles in Ordnung.</p>`}</div>`;
  const run = async (body) => { try { const r = await api("POST", "/api/refresh", body); toast(r.unchanged ? "Unverändert" : r.commit ? `Aktualisiert (${r.commit.slice(0, 7)})` : "Fertig"); } catch (e) { fail(e); } };
  $("#content").querySelectorAll("[data-ov]").forEach((b) => (b.onclick = () => run({ area: b.dataset.ov })));
  $("#content").querySelector("[data-st]").onclick = () => run({ what: "status" });
  $("#content").querySelector("[data-cm]").onclick = () => run({ what: "commits" });
  $("#side").innerHTML = "";
}

async function showRecent() {
  state.note = null; markActive();
  const rows = await get("/api/recent?limit=50");
  $("#content").innerHTML = `<div class="results"><h2>Letzte Änderungen</h2>${rows.map((r) =>
    `<div class="hit">${r.exists === false ? `<span class="t">${esc(r.path)}</span> <span class="tag warn">gelöscht</span>` : `<a class="t" href="#/note/${enc(r.path)}">${esc(r.path)}</a>`}
     <div class="snip">${esc(r.message || "")}</div><div class="hp">${esc(r.author || "")} · ${fmtDate(r.date || r.mtime)}</div></div>`).join("")}</div>`;
  $("#side").innerHTML = "";
}

async function showTasks() {
  state.note = null; markActive();
  const rows = await get("/api/tasks");
  const by = {}; rows.forEach((r) => (by[r.path] ??= []).push(r));
  $("#content").innerHTML = `<div class="results"><h2>Offene Aufgaben (${rows.length})</h2>${Object.entries(by).map(([p, list]) =>
    `<div class="hit"><a class="t" href="#/note/${enc(p)}">${esc(p.replace(/\.md$/, ""))}</a><ul>${list.map((t) => `<li>${esc(t.text)} <span class="hp">${esc(t.heading_path || "")}</span></li>`).join("")}</ul></div>`).join("")}</div>`;
  $("#side").innerHTML = "";
}

async function showHome() {
  state.note = null; markActive(); document.title = "VaultServer";
  const [recent, claims] = await Promise.all([get("/api/recent?limit=12"), get("/api/claims")]);
  $("#content").innerHTML = `<div class="results"><h2>VaultServer</h2>
    ${state.me?.last_error ? `<div class="banner warn">Hintergrund: ${esc(state.me.last_error)}</div>` : ""}
    ${claims.length ? `<h3>Reserviert</h3>${claims.map((c) => `<div class="hit"><a href="#/note/${enc(c.path)}">${esc(c.path)}</a><div class="hp">${esc(c.agent)} bis ${fmtDate(c.expires_at)} ${esc(c.note)}</div></div>`).join("")}` : ""}
    <h3>Zuletzt geändert</h3>${recent.map((r) => `<div class="hit">${r.exists === false ? esc(r.path) : `<a href="#/note/${enc(r.path)}">${esc(r.path)}</a>`}<div class="hp">${esc(r.message || "")} · ${esc(r.author || "")} · ${fmtDate(r.date || r.mtime)}</div></div>`).join("")}</div>`;
  $("#side").innerHTML = "";
}


// ------------------------------------------------------------------ Einrichtung (MCP-Zugänge, Projekte, Anleitungen)

function copyText(text) {
  // navigator.clipboard gibt es nur in sicheren Kontexten (HTTPS/localhost); im LAN über HTTP der alte Weg
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  (document.querySelector("dialog[open]") || document.body).append(ta); ta.select();
  try { document.execCommand("copy"); } catch { toast("Kopieren nicht möglich – bitte markieren"); }
  ta.remove();
}
const codeBlock = (text, id = "") => `<div class="copy"><pre><code${id ? ` id="${id}"` : ""}>${esc(text)}</code></pre><button type="button" data-copy="${esc(text)}">Kopieren</button></div>`;
function wireCopy(root) {
  root.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = async () => {
    await copyText(b.dataset.copy);
    b.textContent = "Kopiert ✓"; setTimeout(() => (b.textContent = "Kopieren"), 1600);  // Toast läge im Dialog verdeckt
  }));
}

const mcpUrl = (project) => `${location.origin}/mcp${project ? `/${project}` : ""}`;
const ALL = "alle Projekte";
const setupTexts = (token = "<TOKEN>", project = null) => {
  const url = mcpUrl(project);
  return {
    claudeCode: `claude mcp add --scope user --transport http vaultserver ${url} \\\n  --header "Authorization: Bearer ${token}"`,
    claudeCodeCheck: `claude mcp list        # vaultserver: ✓ Connected\n# in Claude Code: /mcp  zeigt vaultserver mit den Werkzeugen`,
    desktop: JSON.stringify({ mcpServers: { vaultserver: {
      command: "npx", args: ["-y", "mcp-remote", url, "--allow-http", "--header", "Authorization:${VAULT_AUTH}"],
      env: { VAULT_AUTH: `Bearer ${token}` } } } }, null, 2),
    curl: `curl -s ${url} \\\n  -H "Authorization: Bearer ${token}" \\\n  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \\\n  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`,
    claudeMd: `## Vault (VaultServer)\n\nProjektwissen, Fixliste, Features und Status liegen im Vault und werden **nur über den MCP-Server \`vaultserver\`** gelesen und geändert (nicht über das Obsidian-Plugin, nicht per Dateizugriff).\n\n- Zu Beginn jeder Sitzung \`guide\` aufrufen, bei Arbeit an der Fixliste \`guide(area="Fixliste")\`.\n- Erst \`search\`/\`query\`/\`outline\`, dann \`read\` mit \`section\` – keine großen Dateien komplett lesen.\n- Schreiben mit \`base_version\` aus dem letzten \`read\`; Status mit \`set_property\`, Abschnitte mit \`patch_section\`, neue FIX/Features mit \`create_from_template\`.\n- Eigenschaften enthalten nur kanonische Werte (siehe \`guide\`); Details (Commit, Branch, Test) gehören in „Umsetzung“.\n- Vor längerer Arbeit an einem Eintrag \`claim\`, danach \`release\`. Nachtläufe beginnen mit \`changes_since\`.\n`,
  };
};
// .mcp.json für ein Code-Repo: Projekt-Adresse, Token aus der Umgebung (die Datei darf ins Repo)
const repoJson = (project) => JSON.stringify({ mcpServers: { vaultserver: {
  type: "http", url: mcpUrl(project), headers: { Authorization: "Bearer ${VAULTSERVER_TOKEN}" } } } }, null, 2);
const envTexts = (token = "<TOKEN>") => ({
  linux: `echo 'export VAULTSERVER_TOKEN=${token}' >> ~/.profile   # danach neu anmelden oder: source ~/.profile`,
  windows: `setx VAULTSERVER_TOKEN "${token}"   # danach Terminal und Claude Code neu starten`,
});

function tokenDialog(out, title) {
  const t = setupTexts(out.token, out.project), e = envTexts(out.token), dlg = $("#dlg"), form = $("#dlg-form");
  form.innerHTML = `<h3>${esc(title)}: ${esc(out.name)}</h3>
    <div class="banner warn">Der Token wird <b>nur jetzt</b> angezeigt. Gleich auf dem Rechner eintragen oder sicher ablegen.</div>
    ${out.project ? `<div class="banner info">Dieser Zugang gilt nur für das Projekt <b>${esc(out.project)}</b> – auch über <code>/mcp</code> sieht er nur dieses Projekt.</div>` : ""}
    <div class="setup">
      <h4>Token</h4>${codeBlock(out.token)}
      <h4>Als Umgebungsvariable (für .mcp.json in Code-Repos)</h4>${codeBlock(e.linux)}${codeBlock(e.windows)}
      <h4>Oder direkt in Claude Code (für alle Projekte dieses Rechners)</h4>${codeBlock(t.claudeCode)}
      <h4>Claude Desktop (claude_desktop_config.json)</h4>${codeBlock(t.desktop)}
    </div>
    <div class="btns"><button class="primary" value="ok">Fertig</button></div>`;
  wireCopy(form);
  dlg.onclose = () => { form.innerHTML = ""; showSetup(); };
  dlg.returnValue = ""; dlg.showModal();
}

async function newClient(projects) {
  const r = await dialog("Neuer MCP-Zugang", [
    { name: "name", label: "Name des Rechners (erscheint in den Commits)", placeholder: "z. B. laptop-oliver", required: true },
    { name: "project", label: "Zugriff auf", type: "select", options: [ALL, ...projects.map((p) => p.name)], value: ALL },
  ], "Anlegen");
  if (!r?.name) return;
  try { tokenDialog(await api("POST", "/api/clients", { name: r.name, project: r.project === ALL ? null : r.project }), "Neuer Zugang"); } catch (e) { fail(e); }
}

async function showSetup() {
  state.note = null; markActive(); document.title = "Einrichtung – VaultServer";
  const d = await get("/api/setup");
  const t = setupTexts();
  const ago = (ts) => (ts ? fmtDate(ts) : "–");
  const ro = d.tools.filter((x) => x.readonly), rw = d.tools.filter((x) => !x.readonly);
  const first = d.projects[0]?.name || "";
  const projOptions = (sel) => `<option value="">${ALL}</option>` + d.projects.map((p) => `<option value="${esc(p.name)}" ${p.name === sel ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  $("#content").innerHTML = `<div class="results setup">
    <h2>Einrichtung: VaultServer als Vault für Claude</h2>
    <p>Claude Code und Claude Desktop greifen über den eingebauten <b>MCP-Server</b> auf diesen Vault zu: suchen, gezielt Abschnitte lesen, Einträge anlegen und ändern – jede Änderung wird ein Git-Commit mit dem Namen des Rechners. Jeder Rechner bekommt einen eigenen Zugang (Bearer-Token). Über eine <b>Projekt-Adresse</b> sieht Claude nur einen Ordner des Vaults.</p>

    <h3>1 · Verbindung</h3>
    <table class="kv">
      <tr><th>Ganzer Vault</th><td>${codeBlock(mcpUrl())}</td></tr>
      <tr><th>Ein Projekt</th><td><code>${esc(location.origin)}/mcp/&lt;projekt&gt;</code> – siehe 2</td></tr>
      <tr><th>Transport</th><td>Streamable HTTP (<code>--transport http</code>)</td></tr>
      <tr><th>Anmeldung</th><td>Kopfzeile <code>Authorization: Bearer &lt;Token&gt;</code>, ein Token je Rechner (siehe 3)</td></tr>
      <tr><th>Optional</th><td>Kopfzeile <code>X-VaultServer-Agent: &lt;Werkzeug&gt;</code> – zweiter Teil des Autors in Commits (Standard <code>claude-code</code>)</td></tr>
      <tr><th>Vault</th><td><code>${esc(d.vault)}</code> · Bereiche mit Regeln: ${d.areas.map((a) => `<code>${esc(a)}</code>`).join(", ") || "–"} · Push nach jeder Änderung: ${d.git_push ? "ja" : "nein"}</td></tr>
    </table>
    ${location.protocol === "http:" ? `<div class="banner info">Der Server spricht HTTP ohne TLS – gedacht fürs LAN. Tokens nicht über fremde Netze schicken.</div>` : ""}

    <h3>2 · Projekte</h3>
    <p>Über die Projekt-Adresse sehen alle Werkzeuge nur diesen Ordner: Suche, Liste, Änderungen, Prüfung und <code>guide</code> beziehen sich nur auf das Projekt, Pfade sind <b>relativ</b> zum Projektordner (<code>Fixliste/FIX-001.md</code>), Schreiben außerhalb wird abgelehnt. <code>guide</code> nennt die Einstiegsnotiz des Projekts.</p>
    <table class="list">
      <tr><th>Projekt</th><th>Ordner</th><th>Einstieg</th><th>MCP-Adresse</th></tr>
      ${d.projects.map((p) => `<tr><td><b>${esc(p.name)}</b></td><td>${esc(p.folder)}</td>
        <td>${p.start ? `<a href="#/note/${enc(p.start)}">${esc(p.start.slice(p.folder.length + 1))}</a>` : "–"}</td>
        <td>${codeBlock(mcpUrl(p.name))}</td></tr>`).join("") || `<tr><td colspan="4">Keine Projekte gefunden.</td></tr>`}
    </table>
    <p class="hp">Projekte = Ordner der obersten Ebene. Eigene Namen oder Einstiegsnotizen: Abschnitt <code>[projects]</code> in <code>vaultserver.toml</code>.</p>
    <h4>Pro Code-Repo automatisch das richtige Projekt</h4>
    <ol>
      <li>Im Code-Repo eine Datei <code>.mcp.json</code> anlegen (darf ins Repo, der Token steht nicht drin). Projekt:
        <select id="proj-pick">${d.projects.map((p) => `<option ${p.name === first ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
        ${codeBlock(repoJson(first), "proj-json")}</li>
      <li>Auf jedem Rechner einmal den Token als Umgebungsvariable <code>VAULTSERVER_TOKEN</code> setzen (Befehl erscheint beim Anlegen des Zugangs).</li>
      <li>Claude Code im Repo starten und die Frage nach dem Projekt-Server einmal bestätigen. Im Repo gilt dann die Projekt-Adresse; sie hat Vorrang vor einer allgemeinen Einbindung gleichen Namens.</li>
    </ol>

    <h3>3 · Zugänge (Rechner)</h3>
    <table class="list">
      <tr><th>Name</th><th>Zugriff auf</th><th>Herkunft</th><th>Zuletzt benutzt</th><th></th></tr>
      ${d.clients.map((c) => `<tr><td><b>${esc(c.name)}</b></td>
        <td><select data-proj="${esc(c.id)}" data-name="${esc(c.name)}">${projOptions(c.project)}</select></td>
        <td>${c.source === "config" ? "vaultserver.toml" : `Oberfläche, ${ago(c.created)}`}</td><td>${ago(c.last_seen)}</td>
        <td class="act"><button data-renew="${esc(c.id)}" data-name="${esc(c.name)}">Neuer Token</button> <button data-revoke="${esc(c.id)}" data-name="${esc(c.name)}">Sperren</button></td></tr>`).join("")
        || `<tr><td colspan="5">Noch keine Zugänge.</td></tr>`}
    </table>
    <p><button class="primary" id="btn-new-client">Neuen Zugang anlegen …</button>
      <span class="hp">Ein auf ein Projekt beschränkter Zugang sieht immer nur dieses Projekt, auch über <code>/mcp</code>. „Neuer Token“ ersetzt den alten sofort (z. B. wenn er bekannt geworden ist).</span></p>

    <h3>4 · Claude Code einbinden</h3>
    <ol>
      <li>Für den ganzen Vault auf diesem Rechner (Token aus 3 einsetzen; für ein Projekt die Projekt-Adresse aus 2 nehmen oder die <code>.mcp.json</code>):${codeBlock(t.claudeCode)}</li>
      <li>Prüfen:${codeBlock(t.claudeCodeCheck)}</li>
      <li>Damit Claude den Vault auch benutzt: diesen Block in <code>~/.claude/CLAUDE.md</code> (für alle Projekte) oder in die <code>CLAUDE.md</code> eines Repos:${codeBlock(t.claudeMd)}</li>
      <li>Alte Obsidian-Einbindung entfernen, falls vorhanden: <code>claude mcp remove obsidian</code></li>
    </ol>

    <h3>5 · Claude Desktop (Windows, macOS)</h3>
    <ol>
      <li>Node.js 18 oder neuer installieren (für die Brücke <code>mcp-remote</code>).</li>
      <li>Claude Desktop › Einstellungen › Entwickler › <i>Konfiguration bearbeiten</i> öffnet <code>claude_desktop_config.json</code>
        (Windows <code>%APPDATA%\\Claude\\</code>, macOS <code>~/Library/Application Support/Claude/</code>). Eintragen bzw. unter <code>mcpServers</code> ergänzen (für ein Projekt die Projekt-Adresse einsetzen):${codeBlock(t.desktop)}</li>
      <li>Claude Desktop ganz beenden und neu starten; im Eingabefeld unter „Werkzeuge“ erscheint <b>vaultserver</b>.</li>
    </ol>
    <p class="hp">„Benutzerdefinierten Konnektor hinzufügen“ in den Einstellungen funktioniert hier nicht: Diese Konnektoren werden über die Anthropic-Cloud verbunden und erreichen einen Server im LAN nicht. Deshalb der Weg über die Konfigurationsdatei.</p>

    <h3>6 · claude.ai im Browser und Claude-App auf dem Handy</h3>
    <p>Derzeit nicht möglich. claude.ai verbindet Konnektoren aus dem Internet und braucht dafür eine öffentlich erreichbare HTTPS-Adresse mit OAuth-Anmeldung. VaultServer ist bewusst nur im LAN erreichbar und kennt nur Bearer-Tokens. Möglich wäre es später über HTTPS mit Reverse-Proxy und OAuth – das öffnet den Vault aber nach außen.</p>

    <h3>7 · Andere MCP-Programme und Test</h3>
    <p>Jedes Programm, das MCP über Streamable HTTP mit eigener Kopfzeile kann, verbindet sich mit URL und Token wie oben. Schnelltest von der Kommandozeile:</p>${codeBlock(t.curl)}

    <h3>Werkzeuge (${d.tools.length})</h3>
    <p class="hp">Arbeitsregeln für Agenten liefert das Werkzeug <code>guide</code>.</p>
    <table class="list tools">${[["Lesen", ro], ["Schreiben", rw]].map(([h, list]) =>
      `<tr><th colspan="2">${h} (${list.length})</th></tr>` + list.map((x) => `<tr><td><code>${esc(x.name)}</code></td><td>${esc(x.description)}</td></tr>`).join("")).join("")}</table>
  </div>`;
  const c = $("#content");
  wireCopy(c);
  const pick = $("#proj-pick");
  if (pick) pick.onchange = () => {
    const txt = repoJson(pick.value), box = $("#proj-json").closest(".copy");
    $("#proj-json").textContent = txt; box.querySelector("button").dataset.copy = txt;
  };
  $("#btn-new-client").onclick = () => newClient(d.projects);
  c.querySelectorAll("[data-proj]").forEach((sel) => (sel.onchange = async () => {
    try {
      await api("PUT", `/api/clients/${sel.dataset.proj}`, { project: sel.value || null });
      toast(`„${sel.dataset.name}“: ${sel.value ? `nur Projekt ${sel.value}` : "ganzer Vault"}`);
    } catch (e) { fail(e); showSetup(); }
  }));
  c.querySelectorAll("[data-revoke]").forEach((b) => (b.onclick = async () => {
    if (!confirm(`Zugang „${b.dataset.name}“ sperren? Der Rechner kommt danach nicht mehr an den Vault.`)) return;
    try { await api("DELETE", `/api/clients/${b.dataset.revoke}`); toast(`„${b.dataset.name}“ gesperrt`); showSetup(); } catch (e) { fail(e); }
  }));
  c.querySelectorAll("[data-renew]").forEach((b) => (b.onclick = async () => {
    if (!confirm(`Neuen Token für „${b.dataset.name}“ erzeugen? Der bisherige ist sofort ungültig.`)) return;
    try { tokenDialog(await api("POST", `/api/clients/${b.dataset.renew}/renew`), "Neuer Token"); } catch (e) { fail(e); }
  }));
  $("#side").innerHTML = "";
}

$("#btn-recent").onclick = () => go("#/recent");
$("#btn-lint").onclick = () => go("#/lint");
$("#btn-tasks").onclick = () => go("#/tasks");
$("#btn-more").onclick = (ev) => { ev.stopPropagation(); menu(ev, [
  ["Änderungen", () => go("#/recent")], ["Aufgaben", () => go("#/tasks")], ["Prüfung", () => go("#/lint")], ["Einrichtung", () => go("#/setup")],
  ...accountItems(),
]); };
$("#toggle-tree").onclick = () => document.body.classList.toggle("tree-open");

// ------------------------------------------------------------------ Start

async function boot() {
  try {
    state.me = await get("/api/me");
    state.guide = {};
    const g = await get("/api/guide");
    g.areas.forEach((a) => (state.guide[a.name] = a));
    await loadTree();
    route();
  } catch (e) { if (e.status !== 401) toast(e.message); }
}
boot();
// Baum alle 30 s auffrischen (Änderungen durch Agenten)
setInterval(() => { if (!document.hidden && state.tree) loadTree().catch(() => {}); }, 30000);
