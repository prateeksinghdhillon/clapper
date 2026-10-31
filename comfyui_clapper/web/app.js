/* Clapper web UI — plain JS, no build step. */
"use strict";

const API = "/clapper/api";
const $ = (id) => document.getElementById(id);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
};

const BADGES = {
  ok: ["ok", "take ready"],
  no_take: ["muted", "no take yet"],
  rendering: ["info", "rendering…"],
  prompt_changed: ["warn", "prompt edited since take"],
  refs_changed: ["warn", "references changed since take"],
  continuity_changed: ["warn", "previous scene changed"],
  parent_missing: ["bad", "previous scene has no take"],
  prompt_error: ["bad", "prompt error"],
};
const JOIN_LABEL = { continue: "↪ continue", bridge: "⇄ bridge", cut: "✂ cut", image: "▣ from image", fresh: "✦ fresh" };

const state = { me: null, project: null, path: null, sceneId: null, profiles: [], defaults: null, polling: null, queue: null, live: null, liveTimer: null };

const fmtSize = (n) => { if (n == null) return "—"; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return `${n.toFixed(i < 2 ? 0 : 1)} ${u[i]}`; };
const fmtWhen = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  return d.toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
};
const takeLabel = (t) => `Take ${t.number ?? "?"}`;
const fmtTime = (s) => { if (s == null) return "—"; s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60); return m ? `${m}:${String(s % 60).padStart(2, "0")}` : `${s}s`; };

// ------------------------------------------------------------------ helpers

async function call(path, body, method) {
  const opts = body instanceof FormData ? { method: "POST", body }
    : body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : { method: method || "GET" };
  const res = await fetch(API + path, opts);
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (res.status === 401 && data.login) {
    if (state.me) showLogin("Your session has ended — log in again.");
    throw Object.assign(new Error(data.error), { silent: true });
  }
  if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

let toastTimer;
function toast(msg, error = false) {
  const t = $("toast");
  t.textContent = msg; t.className = "toast" + (error ? " error" : ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), error ? 7000 : 3000);
}
async function guard(fn) { try { return await fn(); } catch (e) { if (!e.silent) { toast(e.message, true); console.error(e); } } }

// Actions that queue GPU work or are not idempotent run once at a time per key: a second click (double
// click, Enter held down) is ignored until the server has answered. Buttons tagged with data-once=key stay
// disabled meanwhile; buttons rebuilt by a re-render get the same via busyAttr(key).
const inFlight = new Set();
const isBusy = (key) => inFlight.has(key);
const busyAttr = (key) => ({ "data-once": key, disabled: inFlight.has(key) });
function setBusy(key, on) {
  if (on) inFlight.add(key); else inFlight.delete(key);
  document.querySelectorAll("[data-once]").forEach((b) => { if (b.dataset.once === key) b.disabled = on; });
}
async function once(key, fn) {
  if (inFlight.has(key)) return undefined;
  setBusy(key, true);
  try { return await guard(fn); } finally { setBusy(key, false); }
}

const media = (file) => `/clapper/media?path=${encodeURIComponent(state.path)}&file=${encodeURIComponent(file)}`;
const takeVideo = (take) => media(`${take.dir}/take.mp4`);
const scenes = () => state.project?.scenes || [];
const takes = () => Object.values(state.project?.takes || {});
const takesFor = (sid) => takes().filter((t) => t.scene === sid).sort((a, b) => (a.number ?? 0) - (b.number ?? 0) || a.created.localeCompare(b.created));
const sceneById = (sid) => scenes().find((s) => s.id === sid);
const library = () => state.project?.library?.items || {};
const effective = () => state.project?.effective_settings || state.defaults;
const sceneSeconds = (s) => s.seconds || effective()?.scene_seconds || 5;
const firstLine = (text) => {
  const body = (text || "").match(/integrated_multimodal_description:\s*(?:\[Shot \d+\]\s*)?([^\n]+)/i);
  return (body ? body[1] : (text || "").replace(/^[a-z_]+:\s*/i, "")).replace(/\[Shot \d+\]\s*/g, "").trim().split("\n")[0].slice(0, 110);
};

function setProject(data) {
  state.project = data; state.path = data.path;
  $("projectTitle").textContent = data.name;
  renderBoard();
  if (state.sceneId && sceneById(state.sceneId)) renderEditor(); else closeEditor();
  syncPolling(); chatRefsChanged();
}

// ------------------------------------------------------------------ profile login

function showLogin(message = "", noProfiles = false) {
  state.me = null; state.project = null; state.path = null; state.sceneId = null; state.pendingFields = null;
  clearTimeout(saveTimer); clearInterval(state.polling); state.polling = null; clearTimeout(state.liveTimer); clearTimeout(sysTimer);
  for (const id of ["homeView", "projectView", "topActions", "menuBtn", "liveBar", "drawer", "statusPill", "statusMenu", "appMenu", "chatPanel"]) $(id).hidden = true;
  gpuStop(); chatReset();
  document.querySelectorAll("dialog[open]").forEach((d) => d.close());
  $("sysStats").replaceChildren(); $("projectTitle").textContent = "";
  $("loginView").hidden = false;
  $("loginForm").hidden = noProfiles; $("setupForm").hidden = !noProfiles;
  $("loginMsg").textContent = message; $("loginMsg").className = "target" + (message ? " warn" : "");
  try { $("loginId").value = $("loginId").value || localStorage.getItem("clapper.profile") || ""; } catch (e) { /* storage blocked */ }
  (noProfiles ? $("setupId") : $("loginId").value ? $("loginPass") : $("loginId")).focus();
}

function enterApp(me) {
  state.me = me;
  try { localStorage.setItem("clapper.profile", me.profile); } catch (e) { /* storage blocked */ }
  $("loginView").hidden = true; $("menuBtn").hidden = false;
  $("whoName").textContent = me.name; $("whoId").textContent = `profile ${me.profile}`;
  $("avatar").textContent = (me.name || me.profile).trim().charAt(0).toUpperCase();
  $("menuBtn").title = `${me.name} — menu`;
  const p = new URLSearchParams(location.search).get("project");
  pollSystem(); gpuStart(); showHome();
  if (p) openProject(p);
}

$("loginForm").onsubmit = async (e) => {
  e.preventDefault();
  const msg = $("loginMsg"); $("loginBtn").disabled = true;
  try {
    const me = await call("/auth/login", { profile: $("loginId").value.trim(), password: $("loginPass").value });
    $("loginPass").value = ""; enterApp(me);
  } catch (err) { msg.textContent = err.message; msg.className = "target bad"; }
  finally { $("loginBtn").disabled = false; }
};

$("setupForm").onsubmit = async (e) => {
  e.preventDefault();
  const msg = $("setupMsg");
  try {
    if ($("setupPass").value !== $("setupPass2").value) throw new Error("The two passwords are different");
    const me = await call("/auth/setup", { profile: $("setupId").value.trim(), name: $("setupName").value.trim(), password: $("setupPass").value });
    enterApp(me);
  } catch (err) { msg.textContent = err.message; msg.className = "target bad"; }
};

$("logoutBtn").onclick = () => guard(async () => {
  await flushSave();
  await call("/auth/logout", {});
  history.replaceState(null, "", "/clapper/");
  showLogin("Logged out. Clapper is free for another profile.");
});

$("mPassword").onclick = () => {
  for (const id of ["pwCurrent", "pwNew", "pwNew2"]) $(id).value = "";
  $("pwMsg").textContent = ""; $("appMenu").hidden = true;
  $("pwDlg").showModal(); $("pwCurrent").focus();
};
$("pwForm").onsubmit = async (e) => {
  e.preventDefault();
  const msg = $("pwMsg");
  try {
    if ($("pwNew").value !== $("pwNew2").value) throw new Error("The two new passwords are different");
    const r = await call("/auth/password", { current: $("pwCurrent").value, new: $("pwNew").value });
    $("pwDlg").close();
    toast(r.other_sessions_ended ? `Password changed; ${r.other_sessions_ended} other device(s) logged out` : "Password changed");
  } catch (err) { msg.textContent = err.message; msg.className = "target bad"; }
};

// ------------------------------------------------------------------ routing

function showHome() {
  if (!state.me) return;
  history.replaceState(null, "", "/clapper/");
  state.project = null; state.path = null; state.sceneId = null; state.usage = null;
  $("homeView").hidden = false; $("projectView").hidden = true; $("topActions").hidden = true; document.body.classList.remove("in-project");
  $("projectTitle").textContent = ""; closeEditor(); chatReset(); syncPolling(); loadProjects(); loadLocations();
}

async function openProject(path) {
  await guard(async () => {
    const data = await call(`/project?path=${encodeURIComponent(path)}`);
    history.replaceState(null, "", `/clapper/?project=${encodeURIComponent(path)}`);
    if (state.path !== path) chatReset();
    $("homeView").hidden = true; $("projectView").hidden = false; $("topActions").hidden = false; document.body.classList.add("in-project");
    setProject(data); refresh(true); loadUsage();
  });
}

// ------------------------------------------------------------------ home

async function loadProjects() {
  const data = await guard(() => call("/projects"));
  if (!data) return;
  state.profiles = data.profiles; state.defaults = data.defaults;
  const list = $("projectList"); list.replaceChildren();
  if (!data.projects.length) list.append(h("p", { class: "muted" }, "No projects yet. Create one on the left."));
  for (const p of data.projects) {
    list.append(h("div", { class: "card project-item", onclick: () => !p.error && openProject(p.path) },
      h("h2", {}, p.name || "(broken project)"),
      p.error ? h("p", { class: "hint" }, p.error) : h("p", { class: "muted" }, `${p.scenes} scenes · ${p.takes} takes · updated ${fmtWhen(p.updated)}`),
      h("div", { class: "path" }, p.path),
      p.error ? null : h("div", { class: "row" }, h("span", { class: "size" }, `${fmtSize(p.usage?.total)} on disk`),
        h("button", { class: "ghost danger", onclick: (e) => { e.stopPropagation(); openProjectDialog(p.path, p.name); } }, "Storage / delete…"))));
  }
}

// New project: name + location (no typing paths on a phone)
const OTHER = "__other__";
let locationChoices = [];
async function loadLocations(select) {
  const data = await guard(() => call("/locations"));
  if (!data) return;
  locationChoices = data.choices;
  const sel = $("newLocation"); sel.replaceChildren();
  for (const c of data.choices) sel.append(h("option", { value: c.path }, c.label + (c.path === data.default ? "  (default)" : "") + (c.exists ? "" : "  — will be created")));
  sel.append(h("option", { value: OTHER }, "Other folder…"));
  let want = select;
  try { want = want || localStorage.getItem("clapper.location"); } catch (e) { /* storage blocked */ }
  sel.value = data.choices.some((c) => c.path === want) ? want : data.default;
  sel.dataset.last = sel.value;
  updateTarget();
}
let targetTimer = null;
function updateTarget() {
  clearTimeout(targetTimer);
  targetTimer = setTimeout(async () => {
    const t = $("newTarget"), btn = $("createBtn"), name = $("newName").value.trim(), typed = $("newPath").value.trim();
    t.className = "target";
    if (typed) { t.innerHTML = `Will use the typed folder: <b>${typed}</b>`; btn.disabled = false; return; }
    if (!name) { t.textContent = "Type a name — the folder is named after it."; btn.disabled = true; return; }
    const loc = $("newLocation").value;
    if (!loc || loc === OTHER) return;
    try {
      const r = await call(`/projects/target?location=${encodeURIComponent(loc)}&name=${encodeURIComponent(name)}`);
      btn.disabled = r.state === "busy";
      btn.textContent = r.state === "project" ? "Open project" : "Create project";
      if (r.state === "free") t.innerHTML = `Will be saved in <b>${r.label}</b>`;
      else if (r.state === "project") { t.className = "target warn"; t.innerHTML = `A project with this name is already in <b>${r.label}</b> — it will be opened.`; }
      else if (r.state === "taken") { btn.disabled = true; t.className = "target bad"; t.innerHTML = `<b>${r.label}</b> belongs to another profile — choose another name or place.`; }
      else { t.className = "target bad"; t.innerHTML = `<b>${r.label}</b> already exists and is not a Clapper project — choose another name or place.`; }
    } catch (e) { t.className = "target bad"; t.textContent = e.message; btn.disabled = true; }
  }, 250);
}
$("newName").addEventListener("input", updateTarget);
$("newPath").addEventListener("input", updateTarget);
$("newLocation").addEventListener("change", () => {
  const sel = $("newLocation");
  if (sel.value === OTHER) { sel.value = sel.dataset.last || ""; openFolderPicker(sel.dataset.last); return; }
  sel.dataset.last = sel.value;
  try { localStorage.setItem("clapper.location", sel.value); } catch (e) { /* storage blocked */ }
  updateTarget();
});
$("createBtn").onclick = () => guard(async () => {
  const name = $("newName").value.trim(), typed = $("newPath").value.trim();
  if (!name && !typed) throw new Error("Give the project a name");
  const body = typed ? { path: typed, name: name || undefined } : { location: $("newLocation").value, name };
  const data = await call("/projects", body);
  $("newName").value = ""; $("newPath").value = "";
  await openProject(data.path);
});

// Folder picker
const picker = { path: null, parent: null };
async function browse(path) {
  await guard(async () => {
    const r = await call(`/folders${path ? `?path=${encodeURIComponent(path)}` : ""}`);
    picker.path = r.path; picker.parent = r.parent;
    $("fdPath").textContent = r.label;
    $("fdUp").disabled = !r.parent;
    const list = $("fdList"); list.replaceChildren();
    if (!r.folders.length) list.append(h("div", { class: "empty" }, "No folders here. Use this folder, or make a new one."));
    for (const f of r.folders) list.append(h("button", { onclick: () => browse(f.path) }, "📁 " + f.name, f.is_project ? h("span", { class: "tag" }, "Clapper project") : null));
    $("fdUse").disabled = r.is_project;
    $("fdUse").title = r.is_project ? "This is a project folder; choose the folder that contains it" : "";
  });
}
function openFolderPicker(start) { $("fdDefault").checked = false; browse(start && start !== OTHER ? start : null); $("folderDlg").showModal(); }
$("fdUp").onclick = () => picker.parent && browse(picker.parent);
$("fdNew").onclick = () => guard(async () => {
  const name = prompt("New folder name"); if (!name) return;
  const r = await call("/folders/new", { parent: picker.path, name: name.trim() });
  await browse(r.path);
});
$("fdUse").onclick = () => guard(async () => {
  const path = picker.path;
  if ($("fdDefault").checked) await call("/locations/default", { path });
  $("folderDlg").close();
  try { localStorage.setItem("clapper.location", path); } catch (e) { /* storage blocked */ }
  await loadLocations(path);
  if (![...$("newLocation").options].some((o) => o.value === path)) {
    $("newLocation").insertBefore(h("option", { value: path }, path.replace(/^\/home\/[^/]+/, "~")), $("newLocation").lastChild);
    $("newLocation").value = path; $("newLocation").dataset.last = path;
  }
  updateTarget();
});

$("homeBtn").onclick = showHome;

// ------------------------------------------------------------------ storyboard

function badgeEls(ids) {
  const list = ids.length ? ids : ["ok"];
  return list.map((b) => { const [cls, label] = BADGES[b] || ["muted", b]; return h("span", { class: `badge ${cls}` }, label); });
}

function thumbFor(take, num, join) {
  const box = h("div", { class: "thumb" });
  if (take && take.status === "done") {
    const v = h("video", { src: takeVideo(take) + "#t=0.1", muted: true, playsinline: true, preload: "metadata", loop: true });
    v.muted = true;
    box.addEventListener("mouseenter", () => v.play().catch(() => {}));
    box.addEventListener("mouseleave", () => { v.pause(); });
    box.append(v);
  } else {
    box.append(take ? `take ${take.status}` : "no take yet");
    const cur = state.live?.current;
    if (take && cur && take.prompt_id === cur.prompt_id) {
      const pct = cur.total ? (100 * cur.step / cur.total) : 0;
      const detail = cur.total ? `step ${cur.step}/${cur.total}` + (cur.s_per_it ? ` · ${cur.s_per_it.toFixed(1)} s/step` : "") : fmtTime(cur.phase_elapsed);
      box.append(h("div", { class: "progress" }, h("div", {}, h("b", {}, cur.phase)), h("div", {}, detail + (cur.eta != null ? ` · ~${fmtTime(cur.eta)} left` : "")),
        h("div", { class: "live-track" }, h("div", { class: "live-fill" + (cur.total ? "" : " indeterminate"), style: `width:${pct}%` }))));
    } else if (take && take.status === "queued") {
      box.append(h("div", { class: "progress" }, "waiting in the ComfyUI queue"));
    }
  }
  if (num) box.append(h("span", { class: "num" }, num));
  if (join) box.append(h("span", { class: "join" }, JOIN_LABEL[join] || join));
  return box;
}

function renderBoard() {
  const board = $("board"); board.replaceChildren();
  const list = scenes();
  const total = list.reduce((sum, s) => sum + sceneSeconds(s), 0);
  const done = list.filter((s) => (state.project.takes[s.selected_take] || {}).status === "done").length;
  $("boardStats").innerHTML = `<b>${list.length}</b> scenes · <b>${total.toFixed(1)}s</b> planned · <b>${done}/${list.length}</b> have a selected take` +
    (state.usage ? ` · <span class="disk" id="diskStat" title="Takes ${fmtSize(state.usage.parts.takes)} · exports ${fmtSize(state.usage.parts.exports)} · library ${fmtSize(state.usage.parts.refs)} · history ${fmtSize(state.usage.parts.history)}"><b>${fmtSize(state.usage.total)}</b> on disk</span>` : "");
  $("diskStat")?.addEventListener("click", () => openProjectDialog(state.path, state.project.name));
  const firstMissing = list.findIndex((s) => (state.project.takes[s.selected_take] || {}).status !== "done");
  const busy = takes().some((t) => t.status === "queued" || t.status === "rendering");
  const rest = firstMissing < 0 ? 0 : list.length - firstMissing;
  const timed = takes().filter((t) => t.render_seconds > 30);
  const avg = timed.length ? timed.reduce((a, t) => a + t.render_seconds, 0) / timed.length : 360;
  const btn = $("renderRestBtn");
  btn.hidden = !rest || busy;
  btn.textContent = `Render remaining ${rest} scene${rest === 1 ? "" : "s"} (≈${Math.round(rest * avg / 60)} min)`;
  btn.title = `Queues scenes ${firstMissing + 1}–${list.length} in order; each continues from the one before. You can still retake any of them later.`;

  list.forEach((scene, i) => {
    if (i > 0) board.append(h("div", { class: "gap" }, h("button", { title: "Insert a scene here", onclick: () => addScene(list[i - 1].id) }, "+")));
    const take = state.project.takes[scene.selected_take];
    const running = takesFor(scene.id).find((t) => t.status === "queued" || t.status === "rendering");
    const card = h("div", { class: "scene-card" + (scene.id === state.sceneId ? " active" : ""), onclick: () => openEditor(scene.id) },
      thumbFor(running && !take ? running : take, i + 1, sceneMode(scene)),
      h("div", { class: "scene-meta" },
        h("div", { class: "scene-name" }, scene.title || firstLine(scene.text) || "Untitled scene"),
        h("div", { class: "scene-sub" }, `${sceneSeconds(scene)}s · ` + (take ? `${takeLabel(take)} of ${takesFor(scene.id).length} selected` : `${takesFor(scene.id).length} take(s)`) +
          ((scene.audio_clips || []).length ? ` · ♪ ${scene.audio_clips.length}` : "")),
        h("div", { class: "badges" }, badgeEls(state.project.badges[scene.id] || []))));
    board.append(card);
  });
  if (list.length) board.append(h("div", { class: "gap" }));
  board.append(h("button", { class: "add-end", onclick: () => addScene(list.length ? list[list.length - 1].id : null) }, list.length ? "+ Add scene at the end" : "+ Add the first scene"));
  if (!list.length) board.append(h("div", { class: "gap" }), h("button", { class: "add-end ai-plan", onclick: () => planWithAssistant() },
    h("b", {}, "✨ Plan the video with the assistant"), h("span", {}, "Describe your idea; it proposes the scenes, prompts and references")));
}

async function addScene(after) {
  await guard(async () => {
    const data = await call("/scene", { path: state.path, action: "add", after, text: "", join: "continue" });
    const idx = after ? data.scenes.findIndex((s) => s.id === after) + 1 : data.scenes.length - 1;
    state.sceneId = data.scenes[idx].id;
    setProject(data); openEditor(state.sceneId);
  });
}

$("renderRestBtn").dataset.once = "renderRest";
$("renderRestBtn").onclick = () => once("renderRest", async () => {
  if (!confirm($("renderRestBtn").title + "\n\nStart now?")) return;
  const data = await call("/render_rest", { path: state.path });
  toast(`${data.queued.length} take(s) queued`); setProject(data.project);
});

// ------------------------------------------------------------------ scene editor

let saveTimer = null;
function openEditor(sid) { state.sceneId = sid; $("drawer").hidden = false; renderEditor(); renderBoard(); }
function closeEditor() { flushSave(); state.sceneId = null; $("drawer").hidden = true; document.querySelectorAll(".scene-card.active").forEach((c) => c.classList.remove("active")); }
$("drawerClose").onclick = closeEditor;
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("drawer").hidden && !document.querySelector("dialog[open]")) closeEditor(); });

function scheduleSave(fields) {
  state.pendingFields = { ...(state.pendingFields || {}), ...fields };
  clearTimeout(saveTimer); saveTimer = setTimeout(flushSave, 700);
}
async function flushSave() {
  clearTimeout(saveTimer);
  const fields = state.pendingFields; state.pendingFields = null;
  if (!fields || !state.sceneId || !state.path) return;
  const data = await guard(() => call("/scene", { path: state.path, action: "update", id: state.sceneId, ...fields }));
  if (data) { state.project = data; renderBoard(); renderEditor(); }
}

function renderBadges() {
  const scene = sceneById(state.sceneId); if (!scene) return;
  $("edBadges").replaceChildren(...badgeEls(state.project.badges[scene.id] || []));
}

function renderCompiled() {
  const c = state.project.compiled[state.sceneId] || {};
  $("edCompiled").textContent = c.error ? `⚠ ${c.error}` : c.prompt;
  $("edCompiledBox").querySelector("summary").textContent = c.error ? "Prompt sent to H3 — ⚠ has an error" : "Prompt sent to H3";
}

function activeRefs(scene) { return scene.refs === null || scene.refs === undefined ? state.project.library.defaults : scene.refs; }

function renderEditor() {
  const scene = sceneById(state.sceneId); if (!scene) return closeEditor();
  const index = scenes().indexOf(scene);
  $("edEyebrow").textContent = `Scene ${index + 1} of ${scenes().length}`;
  if (document.activeElement !== $("edTitle")) $("edTitle").value = scene.title || "";
  if (document.activeElement !== $("edText")) $("edText").value = scene.text || "";
  if (document.activeElement !== $("edSeconds")) $("edSeconds").value = scene.seconds || "";
  $("edSeconds").placeholder = effective()?.scene_seconds || 5;
  const mode = sceneMode(scene);
  document.querySelectorAll("#edStart button").forEach((b) => {
    b.classList.toggle("on", b.dataset.v === mode);
    b.disabled = index === 0 && (b.dataset.v === "continue" || b.dataset.v === "bridge");
  });
  const images = Object.entries(library()).filter(([, it]) => it.kind === "image");
  $("edStartPick").hidden = mode !== "image";
  const startName = (state.project.compiled[scene.id] || {}).start || (scene.start_image || "");
  $("edStartImage").replaceChildren(...(images.length ? images.map(([n]) => h("option", { value: n, selected: n === startName }, "@" + n))
    : [h("option", { value: "" }, "no image references yet — name one in the Library")]));
  renderBadges(); renderCompiled(); renderClips(scene);

  // references
  const lib = library(); const active = activeRefs(scene); const usesDefaults = scene.refs === null || scene.refs === undefined;
  $("edUseDefaults").checked = usesDefaults;
  const start = state.project.start_image;
  $("edRefs").replaceChildren(...Object.entries(lib).filter(([n]) => n !== start).map(([name, item]) =>
    h("span", { class: "chip" + (active.includes(name) ? " on" : ""), title: item.description || "",
      onclick: () => toggleRef(name) }, "@" + name, h("span", { class: "k" }, item.kind))));
  const labels = (state.project.compiled[scene.id] || {}).labels || {};
  $("edRefsHint").textContent = (start ? `@${start} is the start image (always <Picture 1>). ` : "") +
    (Object.keys(labels).length ? "In this scene: " + Object.entries(labels).map(([n, l]) => `@${n} → ${l}`).join(", ") : "No references in this scene.");
  $("edInsertChips").replaceChildren(...[start, ...active].filter(Boolean).map((name) =>
    h("span", { class: "chip", title: "Insert at cursor", onclick: () => insertAtCursor($("edText"), "@" + name) }, "@" + name)));

  // parent choices
  const prev = scenes()[index - 1];
  const parentSel = $("edParent"); parentSel.replaceChildren(h("option", { value: "" }, prev ? "previous scene's selected take" : "(first scene)"));
  if (prev) for (const t of takesFor(prev.id).filter((t) => t.status === "done")) parentSel.append(h("option", { value: t.id }, `${takeLabel(t)}${t.id === prev.selected_take ? " (selected)" : ""} · ${fmtWhen(t.created)}`));
  parentSel.disabled = !prev || scene.join === "cut";

  // takes
  const list = takesFor(scene.id);
  $("edTakesHint").textContent = list.length ? `${list.length} take(s) — the selected one is used in the video` : "";
  $("edTakes").replaceChildren(...(list.length ? list.map((t) => takeCard(scene, t)) : [h("p", { class: "hint" }, "No takes yet. Render one above.")]));
  syncRenderBtn();
}

function sceneMode(scene) {
  const c = (state.project.compiled || {})[scene.id];
  if (c && c.mode) return c.mode;
  const index = scenes().indexOf(scene);
  if (index > 0 && (scene.join === "continue" || scene.join === "bridge")) return scene.join;
  const start = scene.start_image === undefined || scene.start_image === null ? state.project.start_image : scene.start_image;
  return start && (index === 0 || scene.start_image) ? "image" : "fresh";
}

function setSceneMode(mode) {
  const scene = sceneById(state.sceneId); if (!scene) return;
  const images = Object.entries(library()).filter(([, it]) => it.kind === "image").map(([n]) => n);
  let fields;
  if (mode === "continue" || mode === "bridge") fields = { join: mode, start_image: null };
  else if (mode === "fresh") fields = { join: "cut", start_image: "" };
  else {
    const current = (state.project.compiled[scene.id] || {}).start || state.project.start_image || images[0];
    if (!current) { toast("Give an image an @name in the Library first, then choose it here.", true); return; }
    fields = { join: "cut", start_image: current };
  }
  Object.assign(scene, fields); scheduleSave(fields); flushSave();
}

function renderClips(scene) {
  const clips = scene.audio_clips || [];
  const secs = sceneSeconds(scene);
  $("edClips").replaceChildren(...clips.map((c, i) => {
    const a = assets()[c.asset];
    if (!a) return h("div", { class: "clip-row bad" }, "⚠ this clip is no longer in the library", h("button", { class: "icon", onclick: () => saveClips(clips.filter((_, j) => j !== i)) }, "✕"));
    const at = h("input", { type: "number", min: 0, step: 0.1, value: c.at, title: "Seconds into the scene" });
    at.onchange = () => saveClips(clips.map((x, j) => (j === i ? { ...x, at: Math.max(0, Number(at.value) || 0) } : x)));
    const end = (c.at || 0) + (a.duration || 0);
    return h("div", { class: "clip-row" },
      h("div", { class: "clip-title" }, h("b", {}, a.title), h("span", { class: "hint" }, a.duration ? ` ${fmtDur(a.duration)}` : "")),
      h("audio", { src: media(a.file), controls: true, preload: "none" }),
      h("label", { class: "at" }, "starts at", at, "s"),
      a.duration && end > secs ? h("span", { class: "hint warn" }, `ends at ${end.toFixed(1)} s — the scene is ${secs} s, so the clip is cut; make the scene longer?`) : null,
      h("button", { class: "icon", title: "Remove from this scene", onclick: () => saveClips(clips.filter((_, j) => j !== i)) }, "✕"));
  }));
  const audio = Object.values(assets()).filter((a) => a.kind === "audio");
  $("edClipAdd").replaceChildren(h("option", { value: "" }, audio.length ? "+ Add a sound clip from the library…" : "+ Add sound clips in the Library first"),
    ...audio.map((a) => h("option", { value: a.id }, `${a.title}${a.duration ? " · " + fmtDur(a.duration) : ""} (${ROLE_LABEL[a.role] || a.role})`)));
  $("edClipAdd").disabled = !audio.length;
}

function saveClips(clips) {
  const scene = sceneById(state.sceneId); if (!scene) return;
  scene.audio_clips = clips; scheduleSave({ audio_clips: clips }); flushSave(); renderClips(scene);
}

function syncRenderBtn() {
  const key = `render:${state.sceneId}`;
  $("edRender").dataset.once = key;
  $("edRender").disabled = isBusy(key) || !!(state.project?.compiled[state.sceneId] || {}).error;
}

function takeCard(scene, take) {
  const selected = scene.selected_take === take.id;
  const statusCls = { done: "ok", failed: "bad", interrupted: "warn", queued: "info", rendering: "info" }[take.status] || "muted";
  const info = h("div", { class: "take-info" },
    h("div", { class: "take-head" }, h("b", {}, takeLabel(take)), h("span", { class: `badge ${statusCls}` }, take.status), selected ? h("span", { class: "badge ok" }, "selected") : null),
    h("div", { class: "take-dates" },
      h("div", {}, h("span", { class: "k" }, "Created "), fmtWhen(take.created)),
      take.finished ? h("div", {}, h("span", { class: "k" }, "Finished "), fmtWhen(take.finished)) : null),
    h("div", { class: "hint" }, `seed ${take.seed}` + (take.parent_take ? ` · from ${parentLabel(take.parent_take)}` : "")),
    take.render_seconds ? h("div", { class: "take-stats" }, `rendered in ${fmtTime(take.render_seconds)}` + (take.s_per_it ? ` · ${take.s_per_it.toFixed(1)} s/step × ${take.steps}` : "")) : null,
    take.error ? h("div", { class: "hint", title: take.error }, "⚠ " + take.error.slice(0, 120)) : null,
    h("div", { class: "row" },
      take.status === "done" && !selected ? h("button", { class: "primary", onclick: () => selectTake(scene.id, take.id) }, "Use this take") : null,
      take.status === "done" ? h("button", { onclick: () => play(take, `Scene ${scenes().indexOf(scene) + 1} · ${takeLabel(take)}`) }, "Play") : null,
      take.status === "done" ? h("button", { title: "The assistant looks at frames of this take and compares them with the scene", onclick: () =>
        askAssistant(`Review scene ${scenes().indexOf(scene) + 1} ${takeLabel(take)}: does it match the scene? What should change?`, true, { take: take.id }) }, "✨ Review") : null,
      take.scene_text !== undefined && (take.scene_text !== scene.text || JSON.stringify(take.scene_refs ?? null) !== JSON.stringify(scene.refs ?? null))
        ? h("button", { title: "Put this take's text and references back into the scene editor", onclick: () => restorePrompt(take.id) }, "Restore its prompt") : null,
      take.status === "interrupted" || take.status === "failed" ? h("button", { ...busyAttr(`requeue:${take.id}`), onclick: () => requeue(take.id) }, "Re-queue") : null,
      take.status === "queued" || take.status === "rendering" ? h("button", { class: "danger", ...busyAttr("stop"), onclick: () => stopTake(take.id) }, take.status === "queued" ? "Cancel" : "Stop") : null));
  return h("div", { class: "take" + (selected ? " selected" : "") }, thumbFor(take), info);
}

function parentLabel(tid) {
  const p = state.project.takes[tid]; if (!p) return tid;
  const idx = scenes().findIndex((s) => s.id === p.scene);
  return `scene ${idx + 1} ${takeLabel(p)}`;
}

function insertAtCursor(ta, text) {
  const s = ta.selectionStart ?? ta.value.length, e = ta.selectionEnd ?? s;
  const before = ta.value.slice(0, s), pad = before && !/\s$/.test(before) ? " " : "";
  ta.value = before + pad + text + " " + ta.value.slice(e);
  ta.focus(); ta.selectionStart = ta.selectionEnd = s + pad.length + text.length + 1;
  scheduleSave({ text: ta.value });
}

function toggleRef(name) {
  const scene = sceneById(state.sceneId);
  const current = [...activeRefs(scene)];
  const next = current.includes(name) ? current.filter((n) => n !== name) : [...current, name];
  scene.refs = next; scheduleSave({ refs: next }); renderEditor(); flushSave();
}

$("edUseDefaults").onchange = (e) => {
  const scene = sceneById(state.sceneId);
  const refs = e.target.checked ? null : [...state.project.library.defaults];
  scene.refs = refs; scheduleSave({ refs }); renderEditor(); flushSave();
};
$("edTitle").oninput = (e) => scheduleSave({ title: e.target.value });
$("edText").oninput = (e) => scheduleSave({ text: e.target.value });
$("edSeconds").oninput = (e) => scheduleSave({ seconds: e.target.value ? Number(e.target.value) : null });
document.querySelectorAll("#edStart button").forEach((b) => (b.onclick = () => setSceneMode(b.dataset.v)));
$("edStartImage").onchange = () => { const v = $("edStartImage").value; if (!v) return; const scene = sceneById(state.sceneId); scene.start_image = v; scene.join = "cut"; scheduleSave({ start_image: v, join: "cut" }); flushSave(); };
$("edClipAdd").onchange = () => {
  const id = $("edClipAdd").value; $("edClipAdd").value = ""; if (!id) return;
  const scene = sceneById(state.sceneId);
  saveClips([...(scene.audio_clips || []), { asset: id, at: 0.5 }]);
};

$("edRender").onclick = () => {
  const sid = state.sceneId;
  once(`render:${sid}`, async () => {
    await flushSave();
    const body = { path: state.path, scene_id: sid };
    if ($("edSeed").value) body.seed = Number($("edSeed").value);
    if ($("edSteps").value) body.overrides = { sampling: { steps: Number($("edSteps").value) } };
    if ($("edParent").value) body.parent_take = $("edParent").value;
    const data = await call("/render", body);
    toast(`${takeLabel(data.take)} queued`);
    setProject(data.project);
  }).then(syncRenderBtn);
};

$("edRemove").onclick = () => guard(async () => {
  const scene = sceneById(state.sceneId);
  if (!confirm(`Remove scene ${scenes().indexOf(scene) + 1} from the timeline? Its takes stay saved on disk.`)) return;
  const data = await call("/scene", { path: state.path, action: "remove", id: scene.id });
  state.sceneId = null; setProject(data);
});

async function restorePrompt(tid) {
  if (!confirm("Replace the scene's current text and references with this take's version?")) return;
  await guard(async () => { setProject(await call("/take/restore_prompt", { path: state.path, take_id: tid })); toast("Prompt restored"); });
}
async function selectTake(sid, tid) { await guard(async () => setProject(await call("/select", { path: state.path, scene_id: sid, take_id: tid }))); }
async function requeue(tid) { await once(`requeue:${tid}`, async () => { const d = await call("/requeue", { path: state.path, take_id: tid }); toast("Re-queued"); setProject(d.project); }); }

let playing = null;
function play(take, title) {
  playing = take;
  $("playerTitle").textContent = title;
  $("playerVideo").src = takeVideo(take);
  $("playerInfo").textContent = `${take.width}×${take.height} · ${take.frames} frames · seed ${take.seed} · created ${fmtWhen(take.created)}` + (take.finished ? ` · finished ${fmtWhen(take.finished)}` : "");
  $("playerDlg").showModal(); $("playerVideo").play().catch(() => {});
}

$("playerFrame").onclick = () => once("frame", async () => {
  if (!playing) return;
  const v = $("playerVideo"); v.pause();
  const d = await call("/asset/frame", { path: state.path, take_id: playing.id, seconds: v.currentTime });
  setProject(d.project); toast(`Frame at ${v.currentTime.toFixed(1)} s saved to the Library — give it an @name to start a scene from it`);
});

// ------------------------------------------------------------------ polling

function syncPolling() {
  clearInterval(state.polling); state.polling = null;
  clearTimeout(state.liveTimer); pollLive();
  if (!state.path) return;
  const busy = takes().some((t) => t.status === "queued" || t.status === "rendering");
  state.polling = setInterval(() => refresh(false), busy ? 4000 : 20000);
}

async function refresh(showErrors) {
  if (!state.path) return;
  try {
    const data = await call("/refresh", { path: state.path });
    if (data.changed.length) loadUsage();
    if (data.changed.length) {
      const finished = data.changed.map((id) => data.project.takes[id]).filter((t) => t && t.status === "done").length;
      if (finished) toast(`${finished} take(s) finished`);
    }
    if (!document.activeElement || !["edText", "edTitle", "edSeconds"].includes(document.activeElement.id) || data.changed.length) setProject(data.project);
    else { state.project = data.project; renderBoard(); }
  } catch (e) { if (showErrors) toast(e.message, true); }
}

async function pollLive() {
  clearTimeout(state.liveTimer);
  if (!state.me) return;
  let busy = false;
  try {
    const res = await fetch(API + "/live");
    if (res.status === 401) return showLogin("Your session has ended — log in again.");
    const live = await res.json();
    const hadCurrent = !!state.live?.current;
    state.live = live; busy = !!live.current || !!(live.queue && (live.queue.running || live.queue.pending));
    renderLive(); renderStatus();
    if (state.project && (live.current || hadCurrent)) { renderBoard(); if (state.sceneId) renderTakesOnly(); }
    if (hadCurrent && !live.current && state.path) refresh(false);
  } catch (e) { /* server restarting */ }
  state.liveTimer = setTimeout(pollLive, busy ? 1500 : 8000);
}

function liveTake(cur) {
  for (const t of takes()) if (t.prompt_id === cur.prompt_id) {
    const idx = scenes().findIndex((s) => s.id === t.scene);
    return { take: t, label: idx >= 0 ? `Scene ${idx + 1}` : "Scene" };
  }
  return null;
}

function renderLive() {
  const cur = state.live?.current; const bar = $("liveBar");
  if (!cur) { bar.hidden = true; return; }
  bar.hidden = false;
  const who = liveTake(cur);
  const parts = [who ? `<b>${who.label}</b>` : `<b>ComfyUI job</b>`, cur.phase];
  if (cur.total) parts.push(`step ${cur.step}/${cur.total}`);
  if (cur.s_per_it) parts.push(`${cur.s_per_it.toFixed(1)} s/step`);
  parts.push(`${fmtTime(cur.elapsed)} elapsed`);
  if (cur.eta != null) parts.push(`~${fmtTime(cur.eta)} left in this phase`);
  const q = state.live.queue; if (q && q.pending) parts.push(`${q.pending} more waiting`);
  $("liveText").innerHTML = parts.join(" · ");
  const mine = takes().filter((t) => t.status === "queued" || t.status === "rendering");
  $("stopBtn").hidden = !who;
  $("stopBtn").dataset.take = who ? who.take.id : "";
  const dependents = who ? mine.filter((t) => t.id !== who.take.id).length : 0;
  $("stopBtn").title = dependents ? `Stop this take and the ${dependents} queued scene(s) after it` : "Stop this take";
  $("stopAllBtn").hidden = mine.length < 2;
  const fill = $("liveFill");
  fill.classList.toggle("indeterminate", !cur.total);
  fill.style.width = cur.total ? `${(100 * cur.step / cur.total).toFixed(1)}%` : "30%";
}

function renderTakesOnly() {
  const scene = sceneById(state.sceneId); if (!scene) return;
  const list = takesFor(scene.id);
  if (list.some((t) => t.status === "queued" || t.status === "rendering")) $("edTakes").replaceChildren(...list.map((t) => takeCard(scene, t)));
}

async function stopTake(takeId) {
  await once("stop", async () => {
    const t = state.project.takes[takeId]; if (!t) return;
    const later = takes().filter((x) => (x.status === "queued" || x.status === "rendering") && x.id !== takeId && x.created > t.created).length;
    const msg = t.status === "queued" ? "Cancel this waiting take?" : "Stop this take now? The work done on it so far is lost.";
    if (!confirm(msg + (later ? `\n\nQueued scenes that continue from it are cancelled too.` : ""))) return;
    const d = await call("/stop", { path: state.path, take_id: takeId }); toast(`Stopped ${d.stopped.length} take(s)`); setProject(d.project); pollLive();
  });
}
$("stopBtn").dataset.once = $("stopAllBtn").dataset.once = "stop";
$("stopBtn").onclick = () => $("stopBtn").dataset.take && stopTake($("stopBtn").dataset.take);
$("stopAllBtn").onclick = () => once("stop", async () => {
  if (!confirm("Stop every take of this project that is rendering or waiting?")) return;
  const d = await call("/stop", { path: state.path, all: true }); toast(`Stopped ${d.stopped.length} take(s)`); setProject(d.project); pollLive();
});

// ------------------------------------------------------------------ system stats

const GB = 1024 ** 3;
function statBox(label, value, pct, extra, tempC, tempLimits = [75, 85]) {
  const level = pct >= 95 ? "hot" : pct >= 85 ? "warn" : "";
  const tLevel = tempC == null ? "" : tempC >= tempLimits[1] ? "hot" : tempC >= tempLimits[0] ? "warn" : "";
  return h("div", { class: `stat ${level}` },
    h("div", { class: "k" }, h("span", {}, label), extra ? h("span", { class: "x" }, extra) : null),
    h("div", { class: "v" }, value, tempC != null ? h("span", { class: `t ${tLevel}` }, `  ${Math.round(tempC)}°C`) : null),
    h("div", { class: "bar" }, h("i", { style: `width:${Math.min(100, pct).toFixed(0)}%` })));
}
let sysTimer = null;
async function pollSystem() {
  clearTimeout(sysTimer);
  if (!state.me) return;
  try {
    const res = await fetch(API + "/system");
    if (res.status === 401) return showLogin("Your session has ended — log in again.");
    const s = await res.json();
    const boxes = [
      statBox("CPU", `${Math.round(s.cpu.percent)}%`, s.cpu.percent, `${s.cpu.cores} threads`, s.cpu.temp, [80, 90]),
      statBox("RAM", `${(s.ram.used / GB).toFixed(1)}/${Math.round(s.ram.total / GB)} GB`, s.ram.percent),
    ];
    if (s.gpu) {
      boxes.push(statBox("GPU", `${s.gpu.percent}%`, s.gpu.percent, `${Math.round(s.gpu.power)}/${Math.round(s.gpu.power_limit)} W`, s.gpu.temp, [78, 86]));
      boxes.push(statBox("VRAM", `${(s.gpu.vram_used / GB).toFixed(1)}/${Math.round(s.gpu.vram_total / GB)} GB`, 100 * s.gpu.vram_used / s.gpu.vram_total));
    }
    $("sysStats").replaceChildren(...boxes);
    $("sysStats").title = s.gpu ? `${s.gpu.name} · updates every 2 s` : "updates every 2 s";
    state.sys = s; renderStatus();
  } catch (e) { /* server restarting */ }
  sysTimer = setTimeout(pollSystem, 2000);
}

async function loadUsage() {
  if (!state.path) return;
  try { state.usage = await call(`/project/usage?path=${encodeURIComponent(state.path)}`); if (state.project) renderBoard(); } catch (e) { /* ignore */ }
}

const USAGE_PARTS = [["takes", "Takes", "var(--accent)"], ["exports", "Exports", "var(--ok)"], ["refs", "Library", "var(--warn)"], ["history", "History", "var(--info)"], ["other", "Other", "var(--muted)"]];
async function openProjectDialog(path, name) {
  await guard(async () => {
    const u = await call(`/project/usage?path=${encodeURIComponent(path)}`);
    $("pdTitle").textContent = `${name} — storage`;
    $("pdPath").textContent = path;
    $("pdBar").replaceChildren(...USAGE_PARTS.filter(([k]) => u.parts[k]).map(([k, , color]) =>
      h("i", { style: `width:${(100 * u.parts[k] / Math.max(1, u.total)).toFixed(2)}%;background:${color}`, title: `${k} ${fmtSize(u.parts[k])}` })));
    $("pdLegend").replaceChildren(h("span", { style: "--c:transparent" }, h("b", {}, `${fmtSize(u.total)} total`), ` in ${u.files} files`),
      ...USAGE_PARTS.filter(([k]) => u.parts[k]).map(([k, label, color]) => h("span", { style: `--c:${color}` }, `${label} ${fmtSize(u.parts[k])}`)));
    $("pdFree").textContent = fmtSize(u.total);
    $("pdName").textContent = `"${name}"`;
    $("pdConfirm").value = "";
    document.querySelector('input[name="pdMode"][value="files"]').checked = true;
    const busy = (state.path === path ? takes() : []).filter((t) => t.status === "queued" || t.status === "rendering").length;
    $("pdBusy").textContent = busy ? `${busy} take(s) are rendering or waiting — stop them before deleting.` : "";
    const sync = () => {
      const mode = document.querySelector('input[name="pdMode"]:checked').value;
      $("pdDelete").textContent = mode === "files" ? `Delete for good (${fmtSize(u.total)})` : "Remove from list";
      $("pdDelete").disabled = $("pdConfirm").value.trim() !== name.trim() || busy > 0;
    };
    $("pdConfirm").oninput = sync;
    document.querySelectorAll('input[name="pdMode"]').forEach((r) => (r.onchange = sync));
    sync();
    $("pdDelete").onclick = () => guard(async () => {
      const keep = document.querySelector('input[name="pdMode"]:checked').value === "list";
      $("pdDelete").disabled = true;
      const r = await call("/project/delete", { path, confirm_name: $("pdConfirm").value, keep_files: keep });
      $("projectDlg").close();
      toast(r.deleted_files ? `Project deleted — ${fmtSize(r.freed)} freed` : "Removed from the list; files kept");
      showHome();
    });
    $("projectDlg").showModal();
  });
}
$("storageBtn").onclick = () => openProjectDialog(state.path, state.project.name);

// ------------------------------------------------------------------ logs

const logs = { timer: null, lines: [], userScrolled: false };
$("logsBtn").onclick = $("mLogs").onclick = () => { closePops(); $("logsPanel").hidden = !$("logsPanel").hidden; if (!$("logsPanel").hidden) loadLogs(); else clearTimeout(logs.timer); };
$("mLibrary").onclick = () => { closePops(); $("libraryBtn").click(); };
$("mSettings").onclick = () => { closePops(); $("settingsBtn").click(); };
$("mStorage").onclick = () => { closePops(); $("storageBtn").click(); };
$("logsClose").onclick = () => { $("logsPanel").hidden = true; clearTimeout(logs.timer); };
$("logsFilter").onchange = drawLogs; $("logsSearch").oninput = drawLogs;
$("logsCopy").onclick = () => navigator.clipboard?.writeText($("logsText").innerText).then(() => toast("Log copied"));
$("logsText").addEventListener("scroll", () => {
  const el = $("logsText"); if (el.scrollHeight - el.scrollTop - el.clientHeight > 40) $("logsFollow").checked = false;
});

async function loadLogs() {
  clearTimeout(logs.timer);
  try {
    const res = await fetch("/internal/logs/raw"); const data = await res.json();
    const out = [];
    for (const e of data.entries || []) {
      const text = String(e.m || "").replace(/\x1b\[[0-9;]*m/g, "");
      for (const piece of text.split("\n")) {
        if (!piece) continue;
        const parts = piece.split("\r").filter(Boolean);           // tqdm rewrites its line with \r
        const last = parts[parts.length - 1] || "";
        if (piece.startsWith("\r") && out.length && /\d+\/\d+ \[/.test(out[out.length - 1].m)) out[out.length - 1] = { t: e.t, m: last };
        else out.push({ t: e.t, m: last });
      }
    }
    logs.lines = out.slice(-1500); drawLogs();
  } catch (err) { $("logsText").textContent = "Could not read the log: " + err.message; }
  if (!$("logsPanel").hidden) logs.timer = setTimeout(loadLogs, 2000);
}

function drawLogs() {
  const filter = $("logsFilter").value, q = $("logsSearch").value.toLowerCase();
  const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const rows = logs.lines.filter((l) => {
    const m = l.m.toLowerCase();
    if (filter === "warn" && !/warn|error|exception|traceback|failed/.test(m)) return false;
    if (filter === "progress" && !/\d+\/\d+ \[|prompt executed|got prompt|requested to load/.test(m)) return false;
    return !q || m.includes(q);
  }).slice(-600);
  $("logsText").innerHTML = rows.map((l) => {
    const m = l.m; const cls = /error|exception|traceback|failed/i.test(m) ? "le" : /warn/i.test(m) ? "lw" : /\d+\/\d+ \[/.test(m) ? "lp" : "";
    return `<span class="lt">${esc((l.t || "").slice(11, 19))}</span> <span class="${cls}">${esc(m)}</span>`;
  }).join("\n");
  if ($("logsFollow").checked) { const el = $("logsText"); el.scrollTop = el.scrollHeight; }
}

// ------------------------------------------------------------------ library

document.querySelectorAll("dialog [data-close]").forEach((b) => (b.onclick = () => b.closest("dialog").close()));
$("playerDlg").addEventListener("close", () => $("playerVideo").pause());
$("libraryBtn").onclick = () => { renderLibrary(); $("libraryDlg").showModal(); };

// ------------------------------------------------------------------ library

const assets = () => state.project?.assets || {};
const assetNames = (id) => Object.entries(library()).filter(([, it]) => it.asset === id).map(([n]) => n);
const ROLE_LABEL = { character: "Character", place: "Place", prop: "Prop", style: "Style", start: "Start image", frame: "Frame",
  voice: "Voice sample", speech: "Speech line", sound: "Sound effect", music: "Music", clip: "Clip", other: "Other" };
const LIB_FILTERS = [["all", "All"], ["image", "Images"], ["audio", "Sound"], ["video", "Video"], ["named", "@ References"]];
const fmtDur = (s) => (s == null ? "" : s < 60 ? `${s.toFixed(1)} s` : fmtTime(s));
let libFilter = "all";

function assetMeta(a) {
  const bits = [];
  if (a.duration) bits.push(fmtDur(a.duration));
  if (a.width) bits.push(`${a.width}×${a.height}`);
  const o = a.origin || {};
  if (o.type === "frame") bits.push(`frame of ${parentLabel(o.take)} at ${Number(o.seconds).toFixed(1)} s`);
  else if (o.type === "tool") bits.push(`made with ${o.tool}`);
  return bits.join(" · ");
}

function roleSelect(value, onchange) {
  const sel = h("select", {}, (state.project.asset_roles || Object.keys(ROLE_LABEL)).map((r) => h("option", { value: r, selected: r === value }, ROLE_LABEL[r] || r)));
  sel.onchange = () => onchange(sel.value);
  return sel;
}

function assetPreview(a) {
  const src = media(a.file);
  if (a.kind === "image") return h("img", { src, alt: a.title, loading: "lazy" });
  if (a.kind === "audio") return h("div", { class: "audio-prev" }, h("span", { class: "big-ico" }, a.role === "music" ? "♫" : a.role === "voice" || a.role === "speech" ? "🗣" : "♪"), h("audio", { src, controls: true, preload: "none" }));
  return h("video", { src, controls: true, preload: "metadata", playsinline: true });
}

function namedBox(name, item) {
  const defaults = state.project.library.defaults; const start = state.project.start_image;
  const ta = h("textarea", { rows: 3, placeholder: `How H3 should understand @${name}, e.g. "@${name} is …". Added only to scenes that use it.` });
  ta.value = item.definition || "";
  ta.onchange = () => guard(async () => { setProject(await call("/ref/update", { path: state.path, name, definition: ta.value })); toast(`@${name} definition saved`); });
  return h("div", { class: "named" },
    h("div", { class: "name" }, "@" + name, name === start ? h("span", { class: "badge info", title: "start image of the first scene" }, "▶ start") : null,
      defaults.includes(name) ? h("span", { class: "badge ok", title: "used by scenes without their own list" }, "★ default") : null),
    h("label", {}, "Definition (sent to H3)", ta),
    item.kind === "image" ? h("details", { class: "card-fold", open: !!(item.card && item.card_status !== "confirmed") },
      h("summary", {}, "Card for the assistant" + (item.card ? (item.card_status === "confirmed" ? " ✓" : " · check it") : "")), refCard(name, item)) : null,
    h("div", { class: "row" },
      name !== start ? h("button", { onclick: () => toggleDefault(name) }, defaults.includes(name) ? "Unset ★" : "Set ★") : null,
      item.kind === "image" ? h("button", { onclick: () => setStart(name === start ? null : name) }, name === start ? "Unset ▶" : "Set ▶") : null,
      h("button", { class: "ghost danger", title: "The item stays in the library", onclick: () => guard(async () => {
        if (!confirm(`Remove the name @${name}? The file stays in the library.`)) return;
        setProject(await call("/ref/remove", { path: state.path, name })); renderLibrary();
      }) }, "Remove @name")));
}

function nameForm(a) {
  const suggestion = a.title.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "_").replace(/^[^a-z]+/, "").replace(/_+$/, "").slice(0, 32);
  const input = h("input", { placeholder: "name, e.g. hero", value: suggestion });
  const def = h("input", { type: "checkbox" }); def.checked = a.kind === "image" && ["character", "style", "prop", "place"].includes(a.role);
  const go = h("button", { class: "primary", onclick: () => guard(async () => {
    setProject(await call("/asset/name", { path: state.path, id: a.id, name: input.value.trim(), default: def.checked }));
    renderLibrary(); toast(`@${input.value.trim()} ready to use in scenes`);
  }) }, "Name it");
  const box = h("div", { class: "name-form", hidden: true }, h("div", { class: "row" }, h("span", {}, "@"), input, go),
    h("label", { class: "switch" }, def, "use in every scene by default"));
  const open = h("button", { onclick: () => { box.hidden = !box.hidden; if (!box.hidden) input.focus(); } }, "Give it an @name");
  return [open, box];
}

function renderLibrary() {
  $("styleText").value = state.project.style || "";
  $("notesText").value = state.project.notes || "";
  $("libFilter").replaceChildren(...LIB_FILTERS.map(([v, label]) => h("span", { class: "chip" + (libFilter === v ? " on" : ""), onclick: () => { libFilter = v; renderLibrary(); } }, label)));
  const roleSel = $("libRole"); if (!roleSel.options.length) roleSel.replaceChildren(...(state.project.asset_roles || Object.keys(ROLE_LABEL)).map((r) => h("option", { value: r }, ROLE_LABEL[r] || r)));
  const list = Object.values(assets()).filter((a) => libFilter === "all" || (libFilter === "named" ? assetNames(a.id).length : a.kind === libFilter))
    .sort((x, y) => (assetNames(y.id).length - assetNames(x.id).length) || (y.created || "").localeCompare(x.created || ""));
  $("libGrid").replaceChildren(...(list.length ? list.map((a) => {
    const names = assetNames(a.id);
    const title = h("input", { class: "title", value: a.title, title: "Title (for you)" });
    title.onchange = () => guard(async () => setProject(await call("/asset/update", { path: state.path, id: a.id, title: title.value })));
    const [nameBtn, nameBox] = names.length || a.kind === "video" ? [null, null] : nameForm(a);
    return h("div", { class: "lib-item" + (names.length ? " named-item" : "") }, assetPreview(a), title,
      h("div", { class: "row meta" }, roleSelect(a.role, (r) => guard(async () => { setProject(await call("/asset/update", { path: state.path, id: a.id, role: r })); renderLibrary(); })),
        h("span", { class: "hint" }, assetMeta(a))),
      ...names.map((n) => namedBox(n, library()[n])),
      nameBox,
      h("div", { class: "row" }, nameBtn,
        names.length ? null : h("button", { class: "ghost danger", title: "Move the file to the project's trash folder", onclick: () => guard(async () => {
          if (!confirm(`Remove "${a.title}" from the library?`)) return;
          setProject(await call("/asset/remove", { path: state.path, id: a.id })); renderLibrary();
        }) }, "Remove")));
  }) : [h("p", { class: "hint empty" }, libFilter === "all" ? "The library is empty. Add images, sounds or clips with + Add files, or drop them here." : "Nothing here yet.")]));
}

async function uploadFiles(files) {
  if (!files.length) return;
  const fd = new FormData();
  fd.append("path", state.path); fd.append("role", $("libRole").value || "other");
  for (const f of files) fd.append("file", f);
  await once("upload", async () => {
    toast(`Adding ${files.length} file(s)…`);
    const d = await call("/asset", fd);
    setProject(d.project); renderLibrary(); toast(`${d.added.length} added to the library`);
  });
}
$("libFiles").onchange = () => { const f = [...$("libFiles").files]; $("libFiles").value = ""; uploadFiles(f); };
$("libraryDlg").addEventListener("dragover", (e) => { e.preventDefault(); $("libraryDlg").classList.add("drop"); });
$("libraryDlg").addEventListener("dragleave", (e) => { if (e.target === $("libraryDlg")) $("libraryDlg").classList.remove("drop"); });
$("libraryDlg").addEventListener("drop", (e) => { e.preventDefault(); $("libraryDlg").classList.remove("drop"); uploadFiles([...(e.dataTransfer?.files || [])]); });

async function projectUpdate(fields) { const d = await guard(() => call("/project/update", { path: state.path, ...fields })); if (d) { setProject(d); renderLibrary(); } return d; }
const toggleDefault = (name) => { const d = state.project.library.defaults; projectUpdate({ defaults: d.includes(name) ? d.filter((n) => n !== name) : [...d, name] }); };
const setStart = (name) => projectUpdate({ start_image: name });
$("saveStyle").onclick = async () => { if (await projectUpdate({ style: $("styleText").value })) toast("Style text saved"); };
$("saveNotes").onclick = async () => { if (await projectUpdate({ notes: $("notesText").value })) toast("Story notes saved"); };

// ------------------------------------------------------------------ settings

const SAMPLERS = ["res_multistep", "euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "uni_pc", "lcm", "ddim"];
const SCHEDULERS = ["simple", "sgm_uniform", "karras", "exponential", "normal", "beta", "linear_quadratic"];
let draft = null;

$("settingsBtn").onclick = () => { draft = structuredClone(effective()); renderSettings(); $("settingsDlg").showModal(); };
$("settingsReset").onclick = () => { draft = structuredClone(state.defaults); renderSettings(); };
$("settingsSave").onclick = async () => { if (await projectUpdate({ settings: draft })) { $("settingsDlg").close(); toast("Settings saved — used by the next takes"); } };

function field(label, value, onchange, opts = {}) {
  let input;
  if (opts.options) { input = h("select", {}, opts.options.map((o) => h("option", { value: o, selected: String(o) === String(value) }, o))); }
  else if (opts.check) { input = h("input", { type: "checkbox" }); input.checked = !!value; }
  else input = h("input", { type: opts.type || "number", value: value ?? "", step: opts.step, min: opts.min, max: opts.max });
  input.onchange = () => onchange(opts.check ? input.checked : opts.options ? (isNaN(Number(input.value)) ? input.value : Number(input.value)) : opts.type === "text" ? input.value : Number(input.value));
  return opts.check ? h("label", { class: "switch" }, input, label) : h("label", {}, label, input);
}

function renderSettings() {
  const s = draft; const body = $("settingsBody"); body.replaceChildren();
  s.video_model = s.video_model || structuredClone(state.defaults.video_model);
  const VM = { fl2va: "FL2VA for every scene (proven default)", ref2va: "Ref2VA for every scene (one model, 4 steps)",
    ref2va_fresh: "Ref2VA for fresh scenes, FL2VA when continuing" };
  const vmSel = h("select", {}, Object.entries(VM).map(([v, label]) => h("option", { value: v, selected: s.video_model.mode === v }, label)));
  vmSel.onchange = () => { s.video_model.mode = vmSel.value; renderSettings(); };
  const r = s.video_model.ref2va;
  body.append(h("section", { class: "settings-section" }, h("h3", {}, "Video model"),
    h("label", {}, "Which H3 model renders the scenes", vmSel),
    h("p", { class: "hint" }, "Ref2VA makes a scene from references alone, from a start image, or continuing the previous scene, so one model can make the whole video. With its Turbo LoRA at 4 steps a 5-second scene takes about 2 minutes on a 16 GB card. The model, LoRA and steps below are used only when Ref2VA renders; the Sampling, LoRA and model sections further down are for FL2VA."),
    s.video_model.mode === "fl2va" ? null : h("div", { class: "settings-grid" },
      field("Ref2VA model", r.unet, (v) => (r.unet = v), { type: "text" }),
      field("Ref2VA LoRA (empty = none)", (r.loras[0] || {}).name || "", (v) => (r.loras = v ? [{ name: v, strength: (r.loras[0] || {}).strength ?? 1, enabled: true }] : []), { type: "text" }),
      field("Ref2VA steps", r.steps, (v) => (r.steps = v), { min: 1, max: 60 }))));
  body.append(h("section", { class: "settings-section" }, h("h3", {}, "Quick presets"),
    h("div", { class: "profile-row" },
      h("button", { onclick: () => { s.sampling.steps = 20; renderSettings(); } }, "Continuum current (20 steps)"),
      h("button", { onclick: () => { s.sampling.steps = 8; renderSettings(); } }, "Turbo matched (8 steps)")),
    h("p", { class: "hint" }, "Your current Continuum workflow runs the 8-step Turbo LoRA at 20 steps. 8 steps is about 2.5× faster sampling; compare the results before switching.")));
  body.append(h("section", { class: "settings-section" }, h("h3", {}, "Sampling"), h("div", { class: "settings-grid" },
    field("Steps", s.sampling.steps, (v) => (s.sampling.steps = v), { min: 1, max: 60 }),
    field("Sampler", s.sampling.sampler, (v) => (s.sampling.sampler = v), { options: SAMPLERS }),
    field("Scheduler", s.sampling.scheduler, (v) => (s.sampling.scheduler = v), { options: SCHEDULERS }),
    field("Default scene length (s)", s.scene_seconds, (v) => (s.scene_seconds = v), { step: 0.5, min: 1, max: 15 }))));
  body.append(h("section", { class: "settings-section" }, h("h3", {}, "Size"), h("div", { class: "settings-grid" },
    field("Megapixels (from the start image's shape)", s.resolution.megapixels, (v) => (s.resolution.megapixels = v), { options: [0.3, 0.45, 0.6, 0.8] }),
    field("Width when no start image", s.resolution.width, (v) => (s.resolution.width = v), { step: 32 }),
    field("Height when no start image", s.resolution.height, (v) => (s.resolution.height = v), { step: 32 }))));
  body.append(h("section", { class: "settings-section" }, h("h3", {}, "Continuity between scenes"), h("div", { class: "settings-grid" },
    field("Carried frames (5 fast · 22 balanced · 39 strong)", s.continuity.frames, (v) => (s.continuity.frames = v), { options: [5, 22, 39] }),
    field("Method", s.continuity.method, (v) => (s.continuity.method = v), { options: ["masked", "keyframe"] }),
    field("Carry sound across scenes", s.continuity.audio, (v) => (s.continuity.audio = v), { check: true }),
    field("Reference image size", s.references.image_size, (v) => (s.references.image_size = v), { options: ["match", "max"] }))));
  const loraBox = h("div");
  s.loras.forEach((l, i) => loraBox.append(h("div", { class: "lora-row" },
    (() => { const c = h("input", { type: "checkbox" }); c.checked = l.enabled !== false; c.onchange = () => (l.enabled = c.checked); return c; })(),
    (() => { const t = h("input", { type: "text", value: l.name }); t.onchange = () => (l.name = t.value); return t; })(),
    (() => { const n = h("input", { type: "number", step: 0.05, value: l.strength }); n.onchange = () => (l.strength = Number(n.value)); return n; })(),
    h("button", { class: "icon", title: "Remove", onclick: () => { s.loras.splice(i, 1); renderSettings(); } }, "✕"))));
  body.append(h("section", { class: "settings-section" }, h("h3", {}, "LoRAs"), loraBox,
    h("button", { onclick: () => { s.loras.push({ name: "", strength: 1, enabled: true }); renderSettings(); } }, "+ Add LoRA")));
  body.append(h("section", { class: "settings-section" }, h("h3", {}, "Model & speed patches"), h("div", { class: "settings-grid" },
    field("Diffusion model", s.models.unet, (v) => (s.models.unet = v), { type: "text" }),
    field("Text encoder", s.models.clip, (v) => (s.models.clip = v), { type: "text" }),
    field("Chunk FeedForward (lower VRAM)", s.patches.chunk_feed_forward.enabled, (v) => (s.patches.chunk_feed_forward.enabled = v), { check: true }),
    field("FeedForward chunks", s.patches.chunk_feed_forward.chunks, (v) => (s.patches.chunk_feed_forward.chunks = v), { min: 1, max: 16 }))));
}

// ------------------------------------------------------------------ export

$("exportBtn").onclick = () => guard(async () => {
  const c = await call(`/export/check?path=${encodeURIComponent(state.path)}`);
  const body = $("exportBody"); body.replaceChildren();
  const go = $("exportGo");
  if (!c.ready) {
    body.append(h("p", { class: "note" }, "Scene 1 has no finished take yet, so there is nothing to export."));
    go.hidden = true;
  } else {
    go.hidden = false;
    body.append(h("div", { class: "export-summary" },
      h("div", { class: "big" }, c.complete ? `All ${c.total} scenes · ${c.seconds.toFixed(1)} s` : `Scenes 1–${c.ready} of ${c.total} · ${c.seconds.toFixed(1)} s`),
      h("p", { class: "muted" }, "Each scene uses its selected take. Carried-over frames are trimmed so nothing plays twice.")));
    if (!c.complete) body.append(h("p", { class: "note" },
      `The full video is not ready: scene${c.missing.length > 1 ? "s" : ""} ${c.missing.length > 1 ? c.missing[0] + "–" + c.missing[c.missing.length - 1] : c.missing[0]} ${c.missing.length > 1 ? "have" : "has"} no finished take yet. `,
      `You can export the ${c.ready} ready scene${c.ready > 1 ? "s" : ""} now as a partial video, or render the rest first.`));
    if (c.warnings.length) body.append(h("div", {}, h("b", {}, "Check before exporting"),
      h("ul", { class: "warn-list" }, c.warnings.map((w) => h("li", {}, `${w.message} (at ${w.at_seconds.toFixed(1)} s).`)))));
    const label = c.complete ? "Export full video" : `Export partial video (${c.ready} scene${c.ready > 1 ? "s" : ""})`;
    go.dataset.once = "export"; go.disabled = isBusy("export");
    go.textContent = isBusy("export") ? "Exporting…" : label;
    go.onclick = () => once("export", async () => {
      go.textContent = "Exporting…";
      try {
        const data = await call("/export", { path: state.path, partial: !c.complete });
        $("exportDlg").close();
        const m = data.manifest;
        $("playerTitle").textContent = m.scenes_exported < m.scenes_total ? "Partial export" : "Exported video";
        $("playerVideo").src = media(data.file);
        $("playerInfo").innerHTML = `${m.total_seconds.toFixed(1)} s · scenes 1–${m.scenes_exported} of ${m.scenes_total}<br>Saved in the project: <code>${data.file}</code> · <a href="${media(data.file)}" download>download</a>`;
        $("playerDlg").showModal();
      } finally { go.textContent = label; }
    });
  }
  $("exportDlg").showModal();
});

// ------------------------------------------------------------------ show / hide passwords

const EYE = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.1 10.1 0 0 1 12 19C5 19 1 12 1 12a18.5 18.5 0 0 1 5.06-5.94M9.9 4.24A9.1 9.1 0 0 1 12 4c7 0 11 7 11 7a18.6 18.6 0 0 1-2.16 3.19M14.12 14.12a3 3 0 1 1-4.24-4.24M1 1l22 22"/></svg>';
function addPasswordEyes(root = document) {
  for (const input of root.querySelectorAll('input[type="password"]:not([data-eye])')) {
    input.dataset.eye = "1";
    const eye = h("button", { type: "button", class: "pw-eye", title: "Show password", "aria-label": "Show password", html: EYE });
    eye.onclick = () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      eye.innerHTML = show ? EYE_OFF : EYE;
      eye.title = show ? "Hide password" : "Show password";
      eye.setAttribute("aria-label", eye.title);
      input.focus();
    };
    const wrap = h("span", { class: "pw-wrap" });
    input.replaceWith(wrap);
    wrap.append(input, eye);
  }
}
function hidePasswords(root) {  // a dialog or form that is closed or reset starts hidden again
  for (const input of root.querySelectorAll("input[data-eye]")) {
    input.type = "password";
    const eye = input.nextElementSibling;
    if (eye) { eye.innerHTML = EYE; eye.title = "Show password"; eye.setAttribute("aria-label", eye.title); }
  }
}
addPasswordEyes();
$("pwDlg").addEventListener("close", () => hidePasswords($("pwDlg")));

// ------------------------------------------------------------------ boot

(async () => {
  try {
    const res = await fetch(API + "/auth/me"); const data = await res.json();
    if (res.ok) enterApp(data); else showLogin("", !!data.no_profiles);
  } catch (e) { showLogin("Cannot reach ComfyUI — reload the page once it is running."); }
})();
