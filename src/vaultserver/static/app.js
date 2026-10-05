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
$("#btn-logout").onclick = async () => { await api("POST", "/api/logout"); location.hash = ""; showLogin(); };

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
      row.onclick = () => f.kind === "note" ? go(`#/note/${enc(f.path)}`) : window.open(`/api/file/${enc(f.path)}`, "_blank");
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

// ------------------------------------------------------------------ Dialoge

function dialog(title, fields, okLabel = "OK") {
  return new Promise((resolve) => {
    const dlg = $("#dlg"), form = $("#dlg-form");
    form.innerHTML = `<h3>${esc(title)}</h3>` + fields.map((f) => {
      const id = `f-${f.name}`;
      let input;
      if (f.type === "select") input = `<select id="${id}" name="${f.name}">${f.options.map((o) => `<option ${o === f.value ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
      else if (f.type === "textarea") input = `<textarea id="${id}" name="${f.name}" placeholder="${esc(f.placeholder || "")}">${esc(f.value || "")}</textarea>`;
      else input = `<input type="text" id="${id}" name="${f.name}" value="${esc(f.value || "")}" placeholder="${esc(f.placeholder || "")}">`;
      return `<div class="row"><label for="${id}">${esc(f.label)}${f.required ? " *" : ""}</label>${input}</div>`;
    }).join("") + `<div class="btns"><button value="cancel" formnovalidate>Abbrechen</button><button class="primary" value="ok">${esc(okLabel)}</button></div>`;
    dlg.onclose = () => {
      if (dlg.returnValue !== "ok") return resolve(null);
      const out = {}; fields.forEach((f) => (out[f.name] = form.elements[f.name].value.trim())); resolve(out);
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
    ["Rohtext öffnen", () => window.open(`/api/file/${enc(path)}`, "_blank")],
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

$("#btn-recent").onclick = () => go("#/recent");
$("#btn-lint").onclick = () => go("#/lint");
$("#btn-tasks").onclick = () => go("#/tasks");
$("#btn-more").onclick = (ev) => { ev.stopPropagation(); menu(ev, [
  ["Änderungen", () => go("#/recent")], ["Aufgaben", () => go("#/tasks")], ["Prüfung", () => go("#/lint")],
  ["Abmelden", () => $("#btn-logout").click()],
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
