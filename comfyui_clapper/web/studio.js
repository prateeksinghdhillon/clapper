// Clapper workspaces: Studio (cast, world and every file), Scenes (storyboard, in app.js) and Finish.
// Loaded after app.js and uses its helpers (h, $, call, guard, once, media, state, …).

// ------------------------------------------------------------------ workspaces

const WS = ["studio", "scenes", "finish"];
const wsFromHash = () => { const w = (location.hash || "").replace("#", "").split("/")[0]; return WS.includes(w) ? w : null; };
state.ws = wsFromHash() || (() => { try { return localStorage.getItem("clapper.ws") || "scenes"; } catch (e) { return "scenes"; } })();

function showWs(ws) {
  if (ws === "assistant") { $("assistantBtn").click(); return; }
  state.ws = ws;
  try { localStorage.setItem("clapper.ws", ws); } catch (e) { /* storage blocked */ }
  if (location.hash !== "#" + ws) history.replaceState(null, "", location.pathname + location.search + "#" + ws);
  document.querySelectorAll("#wsNav button[data-ws]").forEach((b) => b.classList.toggle("on", b.dataset.ws === ws));
  for (const w of WS) $("ws" + w[0].toUpperCase() + w.slice(1)).hidden = w !== ws;
  document.body.dataset.ws = ws;
  if (ws !== "scenes" && typeof closeEditor === "function" && state.sceneId) closeEditor();
  if (ws !== "studio") document.body.classList.remove("panel-open");
  renderWorkspace(true);
  window.scrollTo({ top: 0 });
}
document.querySelectorAll("#wsNav button[data-ws]").forEach((b) => (b.onclick = () => showWs(b.dataset.ws)));
window.addEventListener("hashchange", () => { const w = wsFromHash(); if (w && w !== state.ws) showWs(w); });

// Re-render the visible workspace after project changes, without clobbering a field being typed in.
let lastSig = { studio: "", finish: "" };
function wsSignature(ws) {
  const p = state.project;
  const jobsSig = Object.values(p.jobs || {}).map((j) => j.id + j.status).join();
  if (ws === "studio") return JSON.stringify([Object.keys(p.assets || {}).length, p.entities, p.library, jobsSig,
    p.scenes.map((s) => [s.refs, s.start_image, s.selected_take, (s.text || "").length]), Object.values(p.takes).map((t) => t.status + (t.upscaled || "")),
    Object.values(p.assets || {}).map((a) => [a.title, a.role, a.entity, a.look, a.transcript])]);
  return JSON.stringify([p.scenes.map((s) => s.selected_take), Object.values(p.takes).map((t) => [t.status, t.upscaled, Object.keys(t.upscales || {})]), jobsSig,
    mixBusy ? "" : p.finish || {}]);   // the finish too: a change made elsewhere is shown (not while this page is saving its own)
}
function renderWorkspace(force) {
  if (!state.project) return;
  const ws = state.ws;
  if (ws !== "studio" && ws !== "finish") return;
  const sig = wsSignature(ws);
  if (!force && sig === lastSig[ws]) { if (ws === "studio") renderStudioLive(); return; }   // nothing changed: keep forms as they are
  lastSig[ws] = sig;
  const el = $("ws" + ws[0].toUpperCase() + ws.slice(1));
  const typing = !force && el && el.contains(document.activeElement) && /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (ws === "studio") { if (typing) renderStudioLive(); else renderStudio(); }
  if (ws === "finish" && !typing) { if (!force) studio.exports = null; renderFinish(); }   // re-check what each export is out of date with
}
// Names the older code calls.
function renderLibrary() { renderWorkspace(); }
function renderJobs() { renderWorkspace(); }

// ------------------------------------------------------------------ studio state

const ENTITY_KINDS = [["character", "Characters", "Character", "👤"], ["place", "Places", "Place", "🏞"], ["prop", "Props", "Prop", "🎒"], ["style", "Styles", "Style", "🎨"]];
const FILE_CATS = [
  ["files", "All files", () => true],
  ["images", "Images", (a) => a.kind === "image"],
  ["starts", "Start images & frames", (a) => a.kind === "image" && (a.role === "start" || a.role === "frame")],
  ["voices", "Voices", (a) => a.kind === "audio" && a.role === "voice"],
  ["lines", "Speech lines", (a) => a.kind === "audio" && a.role === "speech"],
  ["sound", "Music & effects", (a) => a.kind === "audio" && (a.role === "music" || a.role === "sound" || a.role === "other")],
  ["clips", "Video clips", (a) => a.kind === "video"],
  ["refs", "@ References", (a) => assetNames(a.id).length > 0],
];
const SCENE_CATS = [["takes", "Scene takes"], ["exports", "Exports"]];
const studio = { cat: "character", page: null, entity: null, sel: null, search: "", made: new Set(), newKind: null, exports: null };
try { Object.assign(studio, JSON.parse(localStorage.getItem("clapper.studio") || "{}"), { made: new Set(), exports: null, sel: null }); } catch (e) { /* ignore */ }
const saveStudio = () => { try { localStorage.setItem("clapper.studio", JSON.stringify({ cat: studio.cat })); } catch (e) { /* ignore */ } };

const entities = () => state.project?.entities || {};
const looksOf = (eid) => Object.values(assets()).filter((a) => a.entity === eid).sort((a, b) => (a.created || "").localeCompare(b.created || ""));
const mainOf = (e) => assets()[e.main];
const entityKind = (k) => ENTITY_KINDS.find((x) => x[0] === k) || ENTITY_KINDS[0];
const activeJobs = () => jobs().filter((j) => j.status === "queued" || j.status === "running");
const matches = (text) => !studio.search || String(text || "").toLowerCase().includes(studio.search.toLowerCase());
const assetText = (a) => [a.title, a.look, a.role, a.description, ...(assetNames(a.id) || []), a.origin?.inputs?.prompt, a.origin?.inputs?.text, a.transcript].join(" ");

function usedIn(name) {
  if (!name) return [];
  const re = new RegExp("@" + name + "\\b");
  const lib = state.project.library;
  // the scene's own references, or the project's defaults when it uses them; scene 1 opens on the project start image
  return scenes().map((s, i) => ({ s, n: i + 1 })).filter(({ s, n }) => re.test(s.text || "") || (s.refs ?? lib.defaults).includes(name)
    || s.start_image === name || (n === 1 && s.start_image == null && state.project.start_image === name));
}
function clipsUsing(aid) { return scenes().map((s, i) => ({ s, n: i + 1 })).filter(({ s }) => (s.audio_clips || []).some((c) => c.asset === aid)); }

// ------------------------------------------------------------------ studio: layout

function renderStudio() {
  if (!state.project) return;
  renderCats();
  if (studio.page === "new") renderNewPage();
  else if (studio.page === "entity" && entities()[studio.entity]) renderEntityPage(entities()[studio.entity]);
  else { studio.page = null; renderCollection(); }
  renderPanel();
  $("studio").classList.toggle("has-panel", !!studio.sel);
}

// While a field is focused: refresh only things that change by themselves (jobs, new results).
function renderStudioLive() {
  document.querySelectorAll("[data-live=jobs]").forEach((el) => el.replaceWith(jobTiles(el.dataset.scope || "", el.dataset.entity || "")));
}

function renderCats() {
  const count = (fn) => Object.values(assets()).filter(fn).length;
  const ent = (k) => Object.values(entities()).filter((e) => e.kind === k).length;
  const item = (key, label, n, ico) => h("button", { class: "cat" + (studio.page !== "new" && studio.cat === key && studio.page !== "entity" ? " on" : "") + (studio.page === "entity" && entities()[studio.entity]?.kind === key ? " on" : ""),
    onclick: () => { studio.cat = key; studio.page = null; studio.sel = null; saveStudio(); renderStudio(); } }, ico ? h("span", { class: "ci" }, ico) : null, h("span", { class: "cl" }, label), n != null ? h("span", { class: "cn" }, n) : null);
  $("studioCats").replaceChildren(
    h("div", { class: "cat-group" }, "Cast & world"),
    ...ENTITY_KINDS.map(([k, label, , ico]) => item(k, label, ent(k), ico)),
    h("div", { class: "cat-group" }, "Files"),
    ...FILE_CATS.map(([k, label, fn]) => item(k, label, count(fn))),
    h("div", { class: "cat-group" }, "From scenes"),
    item("takes", "Scene takes", takes().filter((t) => t.status === "done").length),
    item("exports", "Exports", studio.exports ? studio.exports.length : null));
}

function crumbs(...parts) {
  return h("div", { class: "crumbs" }, ...parts.flatMap((p, i) => [i ? h("span", { class: "sep" }, "›") : null,
    p.onclick ? h("button", { class: "link", onclick: p.onclick }, p.label) : h("b", {}, p.label)]));
}

// ------------------------------------------------------------------ collections

function renderCollection() {
  const main = $("studioMain");
  const ek = ENTITY_KINDS.find((x) => x[0] === studio.cat);
  if (ek) return main.replaceChildren(entityCollection(ek));
  if (studio.cat === "takes") return main.replaceChildren(takesCollection());
  if (studio.cat === "exports") { main.replaceChildren(exportsCollection()); if (!studio.exports) loadExports(); return; }
  const cat = FILE_CATS.find((x) => x[0] === studio.cat) || FILE_CATS[0];
  const list = Object.values(assets()).filter(cat[2]).filter((a) => matches(assetText(a))).sort((a, b) => (b.created || "").localeCompare(a.created || ""));
  main.replaceChildren(
    h("div", { class: "coll-head" }, h("h3", {}, cat[1]), h("span", { class: "hint" }, `${list.length} item${list.length === 1 ? "" : "s"}`)),
    jobTiles("files"),
    list.length ? h("div", { class: "grid" }, list.map(assetTile))
      : h("div", { class: "empty-state" }, h("p", {}, studio.search ? "Nothing matches your search." : "Nothing here yet."),
        h("div", { class: "row" }, h("button", { class: "primary", onclick: () => openNew() }, "＋ New"), h("span", { class: "hint" }, "or drop files here, or ⬆ Upload"))));
}

function assetTile(a) {
  const names = assetNames(a.id);
  const e = entities()[a.entity];
  return viewIf(a, h("button", { class: "tile" + (studio.sel?.id === a.id ? " on" : ""), onclick: () => select("asset", a.id), title: a.title },
    tileThumb(a),
    h("span", { class: "tile-t" }, a.title),
    h("span", { class: "tile-m" }, names.length ? h("span", { class: "at" }, "@" + names[0]) : null,
      e ? h("span", {}, `${entityKind(e.kind)[3]} ${e.name}`) : h("span", {}, ROLE_LABEL[a.role] || a.role),
      a.duration ? h("span", {}, fmtDur(a.duration)) : null)));
}
// Images and video clips open in the gallery viewer (⛶, or a double click).
function viewIf(a, tile) {
  if (!a || (a.kind !== "image" && a.kind !== "video")) return tile;
  const e = entities()[a.entity];
  return viewable(tile, { file: a.file, kind: a.kind, title: a.look && e ? `${e.name} · ${a.look}` : a.title, asset: a.id,
    sub: [e ? e.name : ROLE_LABEL[a.role] || a.role, a.width ? `${a.width}×${a.height}` : ""].filter(Boolean).join(" · ") });
}

function tileThumb(a) {
  if (!a) return h("span", { class: "thumb-box empty" }, "—");
  if (a.kind === "image") return h("span", { class: "thumb-box" }, h("img", { src: media(a.file), loading: "lazy", alt: "" }));
  if (a.kind === "video") return h("span", { class: "thumb-box" }, posterImg(a.file, 0.1), h("span", { class: "dur" }, a.duration ? fmtDur(a.duration) : "▶"));
  return h("span", { class: "thumb-box audio" }, h("span", { class: "big-ico" }, a.role === "music" ? "♫" : a.role === "voice" || a.role === "speech" ? "🗣" : "♪"), a.duration ? h("span", { class: "dur" }, fmtDur(a.duration)) : null);
}

// Jobs still running, as tiles (scope: files | entity id | made).
function jobTiles(scope, entityId) {
  const list = activeJobs().filter((j) => j.kind !== "upscale" && (scope === "entity" ? j.entity === entityId || (j.uses || [])[0] === entityId : scope === "made" ? studio.made.has(j.id) : !j.entity));
  const failed = jobs().filter((j) => j.kind !== "upscale" && (j.status === "failed" || j.status === "interrupted") && (scope === "entity" ? j.entity === entityId || (j.uses || [])[0] === entityId : scope === "made" ? studio.made.has(j.id) : !j.entity)).slice(0, 3);
  const cur = state.live?.current;
  const box = h("div", { class: "grid jobs-grid", "data-live": "jobs", "data-scope": scope, "data-entity": entityId || "" },
    ...list.map((j) => h("div", { class: "tile pending" },
      h("span", { class: "thumb-box" }, h("span", { class: "spin" }), h("span", { class: "hint" }, j.status === "running" ? (cur && cur.prompt_id === j.prompt_id && cur.total ? `step ${cur.step}/${cur.total}` : "making…") : "waiting")),
      h("span", { class: "tile-t" }, j.title || (j.inputs.prompt || j.inputs.text || "").slice(0, 60)),
      h("span", { class: "tile-m" }, h("button", { class: "link danger", onclick: (ev) => { ev.stopPropagation(); once(`cancel:${j.id}`, async () => setProject(await call("/tool/cancel", { path: state.path, job_id: j.id }))); } }, "Cancel")))),
    ...failed.map((j) => h("div", { class: "tile failed" },
      h("span", { class: "thumb-box" }, h("span", { class: "big-ico" }, "⚠")),
      h("span", { class: "tile-t", title: j.error || "" }, `${j.status}: ${(j.error || "").slice(0, 70)}`),
      h("span", { class: "tile-m" }, h("button", { class: "link", onclick: () => guard(async () => { setProject(await call("/tool/dismiss", { path: state.path, job_id: j.id })); }) }, "Dismiss")))));
  if (!list.length && !failed.length) box.hidden = true;
  return box;
}

// ------------------------------------------------------------------ cast & world

function entityCollection([kind, label, one, ico]) {
  const list = Object.values(entities()).filter((e) => e.kind === kind).filter((e) => matches(e.name + " " + e.description + " " + e.ref));
  return h("div", {},
    h("div", { class: "coll-head" }, h("h3", {}, label), h("span", { class: "hint" }, ENTITY_HINT[kind])),
    h("div", { class: "grid entity-grid" },
      ...list.map((e) => {
        const looks = looksOf(e.id);
        const busy = activeJobs().filter((j) => j.entity === e.id).length;
        return h("button", { class: "tile entity", onclick: () => openEntity(e.id) }, tileThumb(mainOf(e)),
          h("span", { class: "tile-t" }, e.name),
          h("span", { class: "tile-m" }, h("span", { class: "at" }, "@" + e.ref), h("span", {}, `${looks.length} look${looks.length === 1 ? "" : "s"}`), busy ? h("span", { class: "badge info" }, `making ${busy}`) : null));
      }),
      h("button", { class: "tile add", onclick: () => openNew(kind) }, h("span", { class: "thumb-box" }, h("span", { class: "big-ico" }, "＋")), h("span", { class: "tile-t" }, `New ${one.toLowerCase()}`), h("span", { class: "tile-m" }, "design it, or start from an image"))));
}
const ENTITY_HINT = {
  character: "Each character keeps its description, its looks (outfits, poses, angles) and its voice. Scenes use its main look as @name.",
  place: "Locations and backgrounds, with their variations (time of day, weather, angles).",
  prop: "Objects that must look the same in every scene.",
  style: "Art-style sheets the scenes follow.",
};

function openEntity(eid) { studio.page = "entity"; studio.entity = eid; studio.sel = null; renderStudio(); window.scrollTo({ top: 0 }); }

// Reference images for an action: take only the outfit / pose / lighting … from each (another character's look,
// any library image). They come after image 1 (the character itself), so the prompt can name them.
const REF_USE_LABEL = { outfit: "its outfit", pose: "its pose", hair: "its hairstyle", accessory: "its accessories", colours: "its colours",
  lighting: "its lighting & weather", background: "its background", material: "its materials", style: "its art style" };
const REF_DEFAULT_USE = { character: "outfit", place: "lighting", prop: "material", style: "style" };
function refsRow(e, changed) {
  const refs = entityAct.refs;
  const max = entityAct.action === "place" ? 2 : 3;   // the image model takes 4 in all
  const box = h("div", { class: "refs-row" });
  const draw = () => box.replaceChildren(...[   // (replaceChildren would print a null: only real nodes)
    h("span", { class: "hint grow-0" }, "References"),
    ...refs.map((r, i) => {
      const a = assets()[r.asset]; if (!a) return null;
      const known = Object.keys(REF_USE_LABEL);
      const use = h("select", {}, ...known.map((k) => h("option", { value: k, selected: r.use === k }, "take " + REF_USE_LABEL[k])),
        h("option", { value: "_other", selected: !known.includes(r.use) }, "take… (type)"));
      const other = h("input", { placeholder: "e.g. the red scarf", value: known.includes(r.use) ? "" : r.use, hidden: known.includes(r.use) });
      use.onchange = () => { if (use.value === "_other") { other.hidden = false; other.focus(); r.use = other.value || "the details"; } else { other.hidden = true; r.use = use.value; } changed(); };
      other.oninput = () => { r.use = other.value || "the details"; changed(); };
      const owner = entities()[a.entity]?.name;
      return h("div", { class: "ref-chip" }, h("span", { class: "ref-thumb", title: a.title }, tileThumb(a)),
        h("div", { class: "ref-body" }, h("span", { class: "hint" }, `image ${i + 2}` + (owner ? ` · ${owner}` : "")), use, other),
        h("button", { class: "icon", title: "Remove", onclick: () => { refs.splice(i, 1); changed(); draw(); } }, "✕"));
    }),
    refs.length < max ? h("button", { class: "ghost", onclick: () => pickAssets({ kind: "image", title: "Choose reference images — another character's outfit, a pose, a lighting mood …", exclude: refs.map((r) => r.asset) },
      (ids) => { for (const id of ids.slice(0, max - refs.length)) refs.push({ asset: id, use: REF_DEFAULT_USE[e.kind] || "outfit" }); changed(); draw(); }) }, "＋ Reference image") : null,
    refs.length ? null : h("span", { class: "hint" }, e.kind === "character" ? "e.g. another character's jacket, or a pose to copy — for one of its own outfits, choose it in Start from (more reliable)" : e.kind === "place" ? "e.g. a photo with the light you want" : "optional"),
    refs.some((r) => r.use === "outfit" && assets()[r.asset]?.entity === e.id) ? h("span", { class: "hint warn" }, `This outfit is one of ${e.name}'s own looks: choosing it in “Start from” keeps it more reliably than a reference.`) : null].filter(Boolean));
  draw();
  return box;
}

// The character sheet: every side in one picture; scenes can send it to the video model instead of the main look.
function sheetRow(e) {
  if (e.kind !== "character" && e.kind !== "prop") return null;
  const sheet = e.sheet && assets()[e.sheet];
  const use = h("select", {}, [["main", "its main look"], ["sheet", "its character sheet (every side)"]].map(([v, l]) => h("option", { value: v, selected: (e.ref_use || "main") === v, disabled: v === "sheet" && !sheet }, l)));
  use.onchange = () => guard(async () => { setProject(await call("/entity", { path: state.path, action: "update", id: e.id, ref_use: use.value })); toast(`Scenes now use ${use.value === "sheet" ? "the character sheet" : "the main look"} as @${e.ref}`); });
  const sheets = Object.values(assets()).filter((a) => a.entity === e.id && a.sheet).sort((x, y) => (x.created || "").localeCompare(y.created || ""));
  return h("div", { class: "sheet-row" },
    h("div", { class: "row" },
      h("label", {}, `@${e.ref} in scenes is`, use),
      h("button", { title: "Front, three-quarter, side and back in one picture — of the main look or any look, with references if you like",
        onclick: () => { entityAct.action = "sheet"; entityAct.prompt = null; renderStudio(); document.querySelector("#studioMain .actions-card")?.scrollIntoView({ behavior: "smooth", block: "center" }); } },
        sheets.length ? "▦ Another character sheet…" : "▦ Make a character sheet…")),
    sheets.length ? h("div", { class: "sheet-list" }, ...sheets.map((x) => viewIf(x, h("button", { class: "sheet-thumb" + (x.id === e.sheet ? " on" : ""),
      title: (x.id === e.sheet ? "The sheet in use. " : "Click to use this sheet. ") + (x.look || x.title),
      onclick: () => x.id === e.sheet ? select("asset", x.id) : guard(async () => { setProject(await call("/entity", { path: state.path, action: "sheet", id: e.id, asset: x.id })); toast(`${x.look || "This sheet"} is now ${e.name}'s character sheet`); }) },
      tileThumb(x), h("span", { class: "sheet-cap" }, (x.id === e.sheet ? "✓ " : "") + (x.look || "sheet").replace(/^sheet · /, "")))))) : null);
}

function renderEntityPage(e) {
  const [kind, label, one, ico] = entityKind(e.kind);
  const looks = looksOf(e.id);
  const main = mainOf(e);
  const name = h("input", { class: "title-input", value: e.name });
  name.onchange = () => guard(async () => setProject(await call("/entity", { path: state.path, action: "update", id: e.id, name: name.value })));
  const desc = h("textarea", { rows: 4, placeholder: DESC_PH[kind] });
  desc.value = e.description || "";
  desc.onchange = () => guard(async () => { setProject(await call("/entity", { path: state.path, action: "update", id: e.id, description: desc.value })); toast("Description saved — new images and scenes use it"); });
  const scenesUsing = usedIn(e.ref);
  const voices = Object.values(assets()).filter((a) => a.kind === "audio" && (a.role === "voice" || a.id === e.voice));
  const voiceSel = h("select", {}, h("option", { value: "" }, voices.length ? "No voice yet" : "Add a voice sample to the library first"),
    ...voices.map((v) => h("option", { value: v.id, selected: v.id === e.voice }, `${v.title}${v.duration ? " · " + fmtDur(v.duration) : ""}`)));
  voiceSel.onchange = () => guard(async () => setProject(await call("/entity", { path: state.path, action: "update", id: e.id, voice: voiceSel.value || null })));
  $("studioMain").replaceChildren(
    crumbs({ label, onclick: () => { studio.cat = kind; studio.page = null; renderStudio(); } }, { label: e.name }),
    h("div", { class: "entity-head" },
      h("button", { class: "cover", onclick: () => main && select("asset", main.id), title: "Main look" }, tileThumb(main)),
      h("div", { class: "entity-info" },
        name,
        h("div", { class: "row meta" }, h("span", { class: "at big" }, "@" + e.ref), h("span", { class: "hint" }, `${one} · ${looks.length} look${looks.length === 1 ? "" : "s"} · ${scenesUsing.length ? "in scene " + scenesUsing.map((x) => x.n).join(", ") : "not in a scene yet"}`)),
        h("label", {}, "Description — what makes it look like itself; added to every image made from it and sent to H3 in scenes that use it", desc),
        h("div", { class: "assist-row" }, (() => {
          const w = h("input", { placeholder: "✨ Tell the assistant what it is or what to change, e.g. “she is 17, always wears red sneakers”" });
          const b = h("button", { ...busyAttr("write-desc"), onclick: () => once("write-desc", async () => {
            if (!w.value.trim()) throw new Error("Say in your own words what to write");
            const r = await call("/assistant/write_description", { path: state.path, kind, words: (desc.value.trim() ? `Current description: ${desc.value.trim()}\nChange or add: ` : "") + w.value, name: e.name, asset: main?.id || null });
            if (desc.value.trim() && !confirm(`Use the assistant's description?\n\n${r.description}`)) return;
            desc.value = r.description;
            setProject(await call("/entity", { path: state.path, action: "update", id: e.id, description: r.description })); toast("Description written by the assistant");
          }) }, "✨ Write it");
          w.onkeydown = (ev) => { if (ev.key === "Enter") b.click(); };
          return [h("label", { class: "grow" }, w), b];
        })()),
        main ? h("div", { class: "row" }, h("button", { class: "ghost small", ...busyAttr("describe"), title: "The assistant looks at the main look and rewrites the description (you can still edit it)",
          onclick: () => once("describe", async () => {
            const r = await call("/assistant/describe_image", { path: state.path, asset: main.id, kind });
            if (desc.value.trim() && !confirm(`Replace the description with the assistant's?\n\n${r.description}`)) return;
            desc.value = r.description;
            setProject(await call("/entity", { path: state.path, action: "update", id: e.id, description: r.description })); toast("Description written from the image");
          }) }, "✨ Describe from its main look")) : null,
        sheetRow(e),
        kind === "character" ? h("label", {}, "Voice (used by 🗣 Speak)", voiceSel) : null,
        h("div", { class: "row" },
          main ? null : h("span", { class: "hint warn" }, "No main look yet — design one below or add an image."),
          h("span", { class: "spacer" }),
          h("button", { class: "ghost danger", onclick: () => guard(async () => {
            if (!confirm(`Remove ${e.name}? Its images stay in the library (Files).`)) return;
            const d = await callRemoving("/entity", { path: state.path, action: "remove", id: e.id }); if (!d) return;
            setProject(d); studio.page = null; renderStudio(); if (d.note) toast(d.note);
          }) }, `Remove ${one.toLowerCase()}`)))),
    entityActions(e),
    h("div", { class: "coll-head" }, h("h3", {}, "Looks"), h("span", { class: "hint" }, e.ref_use === "sheet" && e.sheet
      ? `★ = the main look (new images start from it) · ▦ = the character sheet, used in scenes as @${e.ref}`
      : `★ = the main look, used in scenes as @${e.ref}` + (e.sheet ? " · ▦ = the character sheet" : ""))),
    jobTiles("entity", e.id),
    h("div", { class: "grid" }, ...looks.map((a) => viewIf(a, h("button", { class: "tile" + (studio.sel?.id === a.id ? " on" : ""), onclick: () => select("asset", a.id) }, tileThumb(a),
      h("span", { class: "tile-t" }, (a.id === e.main ? "★ " : "") + (a.id === e.sheet ? "▦ " : "") + (a.look || a.title)),
      h("span", { class: "tile-m" }, a.id === e.main ? h("span", { class: "at" }, "main") : a.sheet ? h("span", { class: "at" }, a.id === e.sheet ? "sheet" : "a sheet") : h("span", { class: "link", onclick: (ev) => { ev.stopPropagation(); guard(async () => setProject(await call("/entity", { path: state.path, action: "main", id: e.id, asset: a.id }))); } }, "Make main"))))),
      h("button", { class: "tile add", onclick: () => pickAssets({ kind: "image", title: `Add images to ${e.name}`, exclude: looks.map((x) => x.id) }, (ids) => guard(async () => {
        for (const id of ids) setProject(await call("/entity", { path: state.path, action: "add", id: e.id, asset: id, look: "look" }));
      })) }, h("span", { class: "thumb-box" }, h("span", { class: "big-ico" }, "＋")), h("span", { class: "tile-t" }, "Add from library"), h("span", { class: "tile-m" }, "images you already have"))));
}
const DESC_PH = {
  character: "e.g. a 17-year-old girl, short indigo bob with a white streak over her left eye, amber eyes, small bandage on her cheek",
  place: "e.g. a small rural bus stop with a wooden shelter and tin roof, a bench, rice fields and mountains behind",
  prop: "e.g. a clear plastic umbrella with a wooden handle",
  style: "e.g. clean cel-shaded 2D anime, crisp line art, soft flat shading",
};

// The action box on an entity page: one place to make more of it.
const entityAct = { action: "look", text: "", place: "", count: 2, prompt: null, for: null };
function entityActions(e) {
  const kind = e.kind;
  // what was typed, asked and written belongs to this character only
  if (entityAct.for !== e.id) Object.assign(entityAct, { text: "", prompt: null, origin: "auto", for: e.id, source: null, refs: [], look: "", ask: "", explain: "" });
  entityAct.refs = entityAct.refs || [];
  const isLook = ["look", "angle", "sheet", "first"].includes(entityAct.action);
  // start from another of its looks (an outfit) instead of the main look: outfits and looks only (not sheets, poses or frames)
  const madeBy = (a) => (state.project.jobs || {})[a.origin?.job]?.entity_action;
  const ownImages = looksOf(e.id).filter((a) => a.kind === "image" && !a.sheet && !["angle", "place", "sheet"].includes(madeBy(a)));
  const seen = {};
  const sourceSel = ownImages.length > 1 ? h("select", {}, h("option", { value: "" }, "its main look"),
    ...ownImages.filter((a) => a.id !== e.main).map((a) => { const label = a.look || a.title; seen[label] = (seen[label] || 0) + 1;
      return h("option", { value: a.id, selected: a.id === entityAct.source }, seen[label] > 1 ? `${label} (${seen[label]})` : label); })) : null;
  if (sourceSel) sourceSel.onchange = () => { entityAct.source = sourceSel.value || null; changed(); };
  const acts = [["look", kind === "character" ? "New outfit / look" : kind === "place" ? "Variation (time, weather)" : "Variation"], ["angle", "New angle / pose"], ...(kind === "character" ? [["place", "Put in a place"]] : []),
    ...(kind === "character" || kind === "prop" ? [["sheet", "▦ Character sheet"]] : []), ["first", e.main ? "More designs" : "Design it"]];
  if (!e.main && entityAct.action !== "first") entityAct.action = "first";
  const ph = { look: kind === "character" ? "wearing a winter coat and a red scarf" : "at night in the rain", angle: kind === "character" ? "from the side, walking" : "from above", place: "sitting on the bench, looking down the road", first: "anything to add to the description (optional)", sheet: "optional, e.g. without the umbrella, hands empty" };
  const askPh = { look: kind === "character" ? "e.g. a cosy winter outfit like Ren's jacket but in red" : kind === "place" ? "e.g. the same stop on a foggy early morning" : "e.g. older and more worn", angle: "e.g. sitting on the steps, looking up at someone", place: "e.g. waiting under the shelter, a bit impatient", sheet: "e.g. a sheet of her winter outfit with empty hands", first: "e.g. a shy 18-year-old boy, school uniform, messy hair" };

  // ✨ the assistant writes it from plain words, looking at image 1 and the references
  const ask = h("input", { value: entityAct.ask, placeholder: askPh[entityAct.action] || "say what you want" });
  ask.oninput = () => { entityAct.ask = ask.value; };
  const explain = h("p", { class: "hint assist-out" }, entityAct.explain || "");
  const askBtn = h("button", { ...busyAttr("img-prompt"), title: "The assistant looks at the images that will be sent and writes the change, a name and the prompt", onclick: () => once("img-prompt", async () => {
    if (!ask.value.trim()) throw new Error("Say in your own words what you want");
    explain.textContent = "✨ The assistant is looking at the images and writing…";
    try {
      const r = await call("/assistant/image_prompt", { path: state.path, id: e.id, request: ask.value, action: entityAct.action, refs: entityAct.refs, source: entityAct.source, place: entityAct.place });
      Object.assign(entityAct, { text: r.change || entityAct.text, look: r.name || entityAct.look, prompt: r.prompt, origin: "assistant",
        explain: "✨ " + (r.explain || "Written by the assistant.") + (r.images_seen ? "" : " (Its model cannot see images: it worked from the descriptions.)") });
      renderStudio();
    } catch (err) { explain.textContent = ""; throw err; }
  }) }, "✨ Write it");
  ask.onkeydown = (ev) => { if (ev.key === "Enter") askBtn.click(); };

  const text = h("input", { value: entityAct.text, placeholder: ph[entityAct.action] });
  text.oninput = () => { entityAct.text = text.value; changed(); };
  const look = h("input", { value: entityAct.look, placeholder: entityAct.text ? entityAct.text.slice(0, 40) : "from “What changes”" });
  look.oninput = () => { entityAct.look = look.value; };
  const places = Object.values(entities()).filter((x) => x.kind === "place" && x.main);
  const placeSel = h("select", {}, places.length ? places.map((p) => h("option", { value: p.id, selected: p.id === entityAct.place }, p.name)) : [h("option", { value: "" }, "make a place first (Places)")]);
  if (!entityAct.place && places[0]) entityAct.place = places[0].id;
  placeSel.onchange = () => { entityAct.place = placeSel.value; changed(); };
  const count = h("select", {}, [1, 2, 4].map((n) => h("option", { value: n, selected: n === entityAct.count }, `${n} result${n > 1 ? "s" : ""}`)));
  count.onchange = () => { entityAct.count = Number(count.value); };
  // which image model makes these (default: the project's, Settings › Image model) and, if it takes one, a negative prompt
  if (!toolState.list) loadTools().then(() => renderStudio()).catch(() => {});
  const ims = toolState.imageModels || [];
  const curModel = ims.find((m) => m.id === (entityAct.model || projectImageModel()));
  const modelSel = h("select", {}, h("option", { value: "" }, `Project's — ${(ims.find((m) => m.id === projectImageModel()) || {}).title || projectImageModel()}`),
    ...ims.map((m) => h("option", { value: m.id, selected: m.id === entityAct.model, disabled: !m.ok }, m.title + (m.ok ? "" : " (not installed)"))));
  modelSel.onchange = () => { entityAct.model = modelSel.value || null; renderStudio(); };
  const neg = h("input", { value: entityAct.negative || "", placeholder: curModel && curModel.negative ? "empty = " + ((effective()?.image || {}).negative || "the model's own: " + curModel.negative) : "" });
  neg.oninput = () => { entityAct.negative = neg.value; };
  const negRow = curModel && curModel.takes_negative ? h("label", { class: "neg-row" }, "Negative prompt — what to avoid (optional)", neg) : null;

  // the prompt that will be sent: always shown, kept up to date while it is automatic
  const ORIGIN = { auto: ["Automatic", "built from the description, what changes and the references — updates as you type"],
    assistant: ["Written by the assistant", "you can still change it"], you: ["Edited by you", "sent exactly as it is"] };
  const promptBox = h("textarea", { rows: 5 });
  promptBox.value = entityAct.prompt || "";
  const originChip = h("span", { class: "chip on origin-" + entityAct.origin }, ORIGIN[entityAct.origin][0]);
  const originHint = h("span", { class: "hint" }, ORIGIN[entityAct.origin][1]);
  const imagesLine = h("p", { class: "hint" });
  const backAuto = h("button", { class: "ghost small", hidden: entityAct.origin === "auto", onclick: () => { Object.assign(entityAct, { prompt: null, origin: "auto", explain: "" }); renderStudio(); } }, "Back to automatic");
  promptBox.oninput = () => { entityAct.prompt = promptBox.value; if (entityAct.origin !== "you") { entityAct.origin = "you"; originChip.textContent = ORIGIN.you[0]; originChip.className = "chip on origin-you"; originHint.textContent = ORIGIN.you[1]; backAuto.hidden = false; } };
  let previewTimer = null;
  const refreshPreview = () => {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      try {
        const r = await call("/entity/preview", { path: state.path, id: e.id, action: entityAct.action, text: entityAct.text, place: entityAct.place, source: entityAct.source, refs: entityAct.refs });
        if (entityAct.origin === "auto") promptBox.value = r.prompt;
        imagesLine.textContent = r.images.length ? "Images sent: " + r.images.map((id, i) => { const a = assets()[id] || {}; const owner = entities()[a.entity]?.name; return `image ${i + 1} = ${owner ? owner + (a.look ? ` (${a.look})` : "") : a.title || id}`; }).join(" · ")
          : "No image is sent: the description and your words only.";
      } catch (err) { imagesLine.textContent = "⚠ " + err.message; }
    }, 350);
  };
  function changed() { refreshPreview(); }   // the automatic prompt (and the list of images sent) follow every change
  refreshPreview();

  const go = h("button", { class: "primary", ...busyAttr("entity-gen"), onclick: () => once("entity-gen", async () => {
    const d = await call("/entity/generate", { path: state.path, id: e.id, action: entityAct.action, text: entityAct.text, place: entityAct.place, count: entityAct.count,
      prompt: entityAct.origin === "auto" ? null : promptBox.value, look: entityAct.look,
      source: entityAct.action === "first" || entityAct.action === "place" ? null : entityAct.source, refs: entityAct.refs,
      model: entityAct.model || null, negative: (curModel && curModel.takes_negative && entityAct.negative) || null });
    Object.assign(entityAct, { text: "", prompt: null, origin: "auto", look: "", ask: "", explain: "" }); setProject(d.project);
    toast(entityAct.action === "place" ? "Making start frames — they appear in Start images & frames"
      : entityAct.action === "sheet" ? "Making the character sheet — it appears under Looks (▦)" : `Making ${d.jobs.length} — they appear under Looks`);
  }) }, "Generate");
  return h("section", { class: "card actions-card" },
    h("div", { class: "seg act-tabs" }, acts.map(([v, l]) => h("button", { class: entityAct.action === v ? "on" : "", disabled: !e.main && v !== "first",
      onclick: () => { Object.assign(entityAct, { action: v, prompt: null, origin: "auto", explain: "" }); renderStudio(); } }, l))),
    h("div", { class: "assist-row" }, h("label", { class: "grow" }, "✨ Ask the assistant — in your own words; it looks at the pictures and fills in everything below", ask), askBtn),
    explain,
    h("div", { class: "act-row" }, entityAct.action === "place" ? h("label", { class: "grow-0" }, "Place", placeSel) : null,
      sourceSel && ["look", "angle", "sheet"].includes(entityAct.action) ? h("label", { class: "grow-0" }, "Start from", sourceSel) : null,
      h("label", { class: "grow" }, entityAct.action === "first" || entityAct.action === "sheet" ? "Extra details" : entityAct.action === "place" ? "What happens there" : "What changes", text),
      isLook ? h("label", { class: "grow-0 look-name", title: "The label of the new images under Looks (e.g. “winter outfit”). Empty: the start of “What changes”." }, "Name of the new look", look) : null),
    refsRow(e, () => changed()),
    h("div", { class: "prompt-box" },
      h("div", { class: "row" }, h("b", {}, "The prompt that will be sent"), originChip, originHint, h("span", { class: "spacer" }), backAuto),
      promptBox, imagesLine),
    negRow,
    h("div", { class: "row go-row" }, h("label", { class: "grow-0" }, "Image model", modelSel), h("label", { class: "grow-0" }, "Results", count), go,
      h("span", { class: "hint" }, entityAct.action === "place" ? "Start frames for scenes (Files › Start images & frames), with the character and the place as references."
        : `${curModel && curModel.id !== "flux2_klein_9b" ? "Slower than FLUX.2 Klein (25 steps)" : "About 4–10 s per image"}; filed under ${e.name}'s looks.`)));
}

// ------------------------------------------------------------------ the panel (one item, in detail)

// On tablets and phones the panel slides over the page; tapping the dimmed page beside it closes it.
document.body.addEventListener("click", (e) => { if (e.target === document.body && document.body.classList.contains("panel-open")) closePanel(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && studio.sel && !document.querySelector("dialog[open]")) closePanel(); });

function select(type, id) { studio.sel = { type, id }; renderStudio(); }
function closePanel() { studio.sel = null; renderStudio(); }

function renderPanel() {
  const p = $("studioPanel");
  const sel = studio.sel;
  if (!sel) { p.hidden = true; p.replaceChildren(); document.body.classList.remove("panel-open"); return; }
  p.hidden = false; document.body.classList.toggle("panel-open", state.ws === "studio");
  const head = (title) => h("div", { class: "panel-head" }, h("button", { class: "icon back", title: "Close", onclick: closePanel }, "←"), h("b", {}, title), h("span", { class: "spacer" }), h("button", { class: "icon", title: "Close", onclick: closePanel }, "✕"));
  if (sel.type === "asset") {
    const a = assets()[sel.id];
    if (!a) { studio.sel = null; p.hidden = true; return; }
    p.replaceChildren(head(a.kind === "image" ? "Image" : a.kind === "audio" ? "Sound" : "Video"), ...assetDetail(a));
  } else if (sel.type === "take") {
    const t = state.project.takes[sel.id];
    if (!t) { studio.sel = null; p.hidden = true; return; }
    p.replaceChildren(head("Scene take"), ...takeDetail(t));
  } else if (sel.type === "export") {
    const x = (studio.exports || []).find((e) => e.file === sel.id);
    if (!x) { studio.sel = null; p.hidden = true; return; }
    p.replaceChildren(head("Export"), ...exportDetail(x));
  }
}

function bigPreview(kind, file) {
  const src = media(file);
  if (kind === "image") return h("button", { class: "big-prev", title: "View large — zoom, next / previous, compare", onclick: () => {
    // the images of the grid this one was opened from (so next / previous walk through them), else just this one
    const tile = [...document.querySelectorAll("#studioMain [data-vfile]")].find((t) => t.dataset.vfile === file);
    if (tile) openFromTile(tile); else openViewer([{ file, kind: "image", title: "" }], 0);
  } }, h("img", { src, alt: "" }), h("span", { class: "tile-view" }, "⛶"));
  if (kind === "audio") return h("div", { class: "big-prev audio" }, h("span", { class: "big-ico" }, "🗣"), h("audio", { src, controls: true, preload: "metadata" }));
  return h("div", { class: "big-prev" }, h("video", { src, controls: true, preload: "metadata", playsinline: true }));
}

function familyOf(a) {
  const all = Object.values(assets());
  let root = a; const seen = new Set();
  while (root.parent && assets()[root.parent] && !seen.has(root.parent)) { seen.add(root.id); root = assets()[root.parent]; }
  const out = [root]; const queue = [root.id];
  while (queue.length) { const id = queue.shift(); for (const x of all) if (x.parent === id && !out.includes(x)) { out.push(x); queue.push(x.id); } }
  // versions are variants of the same kind of thing: a start frame made from a character is not one of its versions
  return out.filter((x) => x.id === a.id || (x.role === a.role && x.kind === a.kind));
}

function assetDetail(a) {
  const names = assetNames(a.id);
  const e = entities()[a.entity];
  const title = h("input", { class: "title-input", value: a.title });
  title.onchange = () => guard(async () => setProject(await call("/asset/update", { path: state.path, id: a.id, title: title.value })));
  const out = [bigPreview(a.kind, a.file), title,
    h("div", { class: "row meta" }, roleSelect(a.role, (r) => guard(async () => setProject(await call("/asset/update", { path: state.path, id: a.id, role: r })))),
      h("span", { class: "hint" }, assetMeta(a)))];
  if (a.kind === "image") {   // what is in it: written by the assistant (or by hand); used for search and by the assistant
    const d = h("textarea", { rows: 3, placeholder: "What is in this image (✨ lets the assistant write it)" });
    d.value = a.description || "";
    d.onchange = () => guard(async () => setProject(await call("/asset/update", { path: state.path, id: a.id, description: d.value })));
    out.push(h("section", { class: "psec" }, h("div", { class: "row" }, h("h4", {}, "What it shows"), h("span", { class: "spacer" }),
      h("button", { class: "ghost small", ...busyAttr("describe"), onclick: () => once("describe", async () => {
        const r = await call("/assistant/describe_image", { path: state.path, asset: a.id, save: true });
        setProject(r.project); d.value = r.description; toast("Described by the assistant");
      }) }, "✨ Describe")), d));
  }
  // where it belongs
  if (a.kind === "image") {
    const options = Object.values(entities());
    const belong = h("select", {}, h("option", { value: "" }, "— not part of a character or place"), ...ENTITY_KINDS.map(([k, label]) => {
      const list = options.filter((x) => x.kind === k); return list.length ? h("optgroup", { label }, list.map((x) => h("option", { value: x.id, selected: x.id === a.entity }, x.name))) : null;
    }).filter(Boolean));
    belong.onchange = () => guard(async () => setProject(await call("/entity", { path: state.path, action: "add", id: belong.value || null, asset: a.id, look: a.look || a.title })));
    const look = h("input", { value: a.look || "", placeholder: "label, e.g. winter outfit" });
    look.onchange = () => guard(async () => setProject(await call("/asset/update", { path: state.path, id: a.id, look: look.value })));
    out.push(h("section", { class: "psec" }, h("h4", {}, "Belongs to"), h("div", { class: "row" }, belong, e ? h("button", { onclick: () => openEntity(e.id) }, "Open") : null),
      e ? h("label", {}, "Look", look) : null,
      e && e.main !== a.id ? h("button", { onclick: () => guard(async () => setProject(await call("/entity", { path: state.path, action: "main", id: e.id, asset: a.id }))) }, `★ Make it ${e.name}'s main look`) : null,
      e && e.main === a.id ? h("p", { class: "hint" }, `★ ${e.name}'s main look` + ((e.ref_use || "main") === "main" ? ` — scenes use it as @${e.ref}.` : ".")) : null,
      e && (e.kind === "character" || e.kind === "prop") && e.sheet !== a.id ? h("button", { onclick: () => guard(async () => { setProject(await call("/entity", { path: state.path, action: "sheet", id: e.id, asset: a.id })); toast(`Now ${e.name}'s character sheet`); }) }, `▦ Use as ${e.name}'s character sheet`) : null,
      e && e.sheet === a.id ? h("p", { class: "hint" }, `▦ ${e.name}'s character sheet` + (e.ref_use === "sheet" ? ` — scenes use it as @${e.ref}.` : " (scenes use the main look; switch it on the character's page).")) : null,
      e && (e.kind === "character" || e.kind === "prop") ? h("button", { ...busyAttr("sheet"), title: "Front, three-quarter, side and back views of this look", onclick: () => once("sheet", async () => {
        const d = await call("/entity/generate", { path: state.path, id: e.id, action: "sheet", count: 1, source: a.id });
        setProject(d.project); toast("Making a character sheet of this look");
      }) }, "▦ Character sheet of this look") : null,
      !e ? h("button", { class: "ghost", onclick: () => openNew(a.role === "place" ? "place" : a.role === "prop" ? "prop" : a.role === "style" ? "style" : "character", { from: a.id }) }, "Make a character / place from it…") : null));
  }
  // versions
  const fam = familyOf(a);
  if (fam.length > 1) out.push(h("section", { class: "psec" }, h("h4", {}, `Versions (${fam.length})`),
    h("div", { class: "strip" }, fam.map((x) => viewIf(x, h("button", { class: "strip-item" + (x.id === a.id ? " on" : ""), title: x.title, onclick: () => select("asset", x.id) }, tileThumb(x)))))));
  // make more from it
  out.push(...makeMore(a, e));
  // reference
  if (a.kind !== "video") out.push(h("section", { class: "psec" }, h("h4", {}, "In scenes"),
    ...names.map((n) => library()[n]?.entity ? h("p", { class: "hint" }, `@${n} — ${entities()[library()[n].entity]?.name || ""}'s main look; its definition is the description.`) : namedBox(n, library()[n])),
    ...(names.length ? [] : a.kind === "image" ? nameForm(a) : []),
    ...names.map((n) => { const u = usedIn(n); return h("p", { class: "hint" }, u.length ? `@${n} is used in scene ${u.map((x) => x.n).join(", ")}.` : `@${n} is not used in a scene yet.`); }),
    a.kind === "audio" ? (() => { const u = clipsUsing(a.id); return h("p", { class: "hint" }, u.length ? `Sound clip in scene ${u.map((x) => x.n).join(", ")}.` : "Add it to a scene as a sound clip in the scene editor."); })() : null));
  out.push(h("div", { class: "row psec" },
    h("a", { class: "btn", href: media(a.file), download: (a.title || a.id).replace(/[^\w.-]+/g, "_") + "." + a.file.split(".").pop() }, "⤓ Download"),
    h("span", { class: "spacer" }),
    names.length ? null : h("button", { class: "ghost danger", onclick: () => guard(async () => {
      if (!confirm(`Remove "${a.title}" from the library?`)) return;
      setProject(await call("/asset/remove", { path: state.path, id: a.id })); closePanel();
    }) }, "Remove")));
  return out;
}

const recipeEdits = {};   // asset id → edited recipe inputs
// Generate again with changes (a new version), change an image, or speak with a voice.
function makeMore(a, e) {
  const sections = [];
  const o = a.origin || {};
  if (o.type === "tool" && o.inputs && o.tool && !o.tool.startsWith("upscale")) {
    const values = recipeEdits[a.id] || (recipeEdits[a.id] = { ...o.inputs });
    const edited = JSON.stringify(values) !== JSON.stringify(o.inputs);
    const tool = (toolState.list || []).find((t) => t.id === o.tool);
    if (!toolState.list) loadTools().then(() => renderPanel()).catch(() => {});
    const count = h("select", {}, [1, 2, 4].map((n) => h("option", { value: n }, `${n}`)));
    sections.push(h("section", { class: "psec recipe" }, h("h4", {}, "Recipe"),
      h("p", { class: "hint" }, `${(o.tool_title || o.tool).replace(/ \(.*\)$/, "")} · seed ${o.seed}. Change anything and make a new version — the original stays.`),
      edited ? h("p", { class: "hint warn" }, "Shown with your changes (not made yet) — not what made this image. ",
        h("button", { class: "ghost small", onclick: () => { delete recipeEdits[a.id]; renderPanel(); } }, "Show the original")) : null,
      ...(tool ? tool.fields.filter((f) => !f.advanced).map((f) => toolField(f, values)) : [h("p", { class: "hint" }, "Loading the tool…")]),
      h("div", { class: "row" }, h("label", { class: "grow-0" }, "Results", count),
        h("button", { class: "primary", ...busyAttr("again"), onclick: () => once("again", async () => {
          const d = await call("/tool/run", { path: state.path, tool: o.tool, inputs: values, count: Number(count.value), role: a.role, title: a.title.replace(/ \d+$/, ""), version_of: a.id });
          delete recipeEdits[a.id]; setProject(d.project); toast(`Making ${d.jobs.length} new version(s)`);
        }) }, "Make new version"))));
  } else if (a.kind === "image") {
    const text = h("input", { placeholder: "e.g. at night with the street lamp on" });
    sections.push(h("section", { class: "psec" }, h("h4", {}, "Change it"),
      h("div", { class: "row" }, text, h("button", { class: "primary", ...busyAttr("change"), onclick: () => once("change", async () => {
        if (!text.value.trim()) throw new Error("Describe the change");
        const d = await call("/tool/run", { path: state.path, tool: "image", inputs: { prompt: `The same picture as image 1, ${text.value.trim()}. Keep everything else the same.`, images: [a.id], aspect: aspectOf(a) }, count: 2, role: a.role, title: a.title, version_of: a.id });
        setProject(d.project); toast("Making 2 versions");
      }) }, "Make 2"))));
  }
  if (a.kind === "audio" && (a.role === "voice" || a.role === "speech")) {
    const t = h("input", { placeholder: "What the sample says (improves the likeness)", value: a.transcript || "" });
    t.onchange = () => guard(async () => { setProject(await call("/asset/update", { path: state.path, id: a.id, transcript: t.value })); toast("Transcript saved"); });
    const line = h("textarea", { rows: 2, placeholder: "A line to say in this voice" });
    const lang = h("select", {}, ["Auto", "English", "Chinese", "Japanese", "Korean", "French", "German", "Spanish", "Portuguese", "Russian", "Italian"].map((l) => h("option", {}, l)));
    sections.push(h("section", { class: "psec" }, h("h4", {}, "🗣 Speak in this voice"),
      a.role === "voice" ? h("label", {}, "Transcript of the sample", t) : null, line,
      h("div", { class: "row" }, h("label", { class: "grow-0" }, "Language", lang), h("button", { class: "primary", ...busyAttr("speak"), onclick: () => once("speak", async () => {
        if (!line.value.trim()) throw new Error("Type a line");
        const d = await call("/tool/run", { path: state.path, tool: "speech", inputs: { text: line.value.trim(), voice: a.id, voice_text: a.transcript || "", language: lang.value }, count: 2, role: "speech", title: line.value.trim().slice(0, 40) });
        setProject(d.project); toast("Speaking 2 takes — they appear in Speech lines");
      }) }, "Speak (2 takes)"))));
  }
  return sections;
}

// ------------------------------------------------------------------ takes and exports in the studio

function takesCollection() {
  const rows = scenes().map((s, i) => ({ s, n: i + 1, list: takesFor(s.id).filter((t) => t.status === "done") })).filter((r) => r.list.length);
  return h("div", {},
    h("div", { class: "coll-head" }, h("h3", {}, "Scene takes"), h("span", { class: "hint" }, "Every finished take, by scene. Save a frame from one to start another scene from it.")),
    ...(rows.length ? rows.map((r) => h("div", { class: "scene-group" }, h("h4", {}, `Scene ${r.n}${r.s.title ? " · " + r.s.title : ""}`),
      h("div", { class: "grid" }, r.list.map((t) => viewable(h("button", { class: "tile" + (studio.sel?.id === t.id ? " on" : ""), onclick: () => select("take", t.id) },
        h("span", { class: "thumb-box" }, posterImg(`${t.dir}/take.mp4`, 0.1)),
        h("span", { class: "tile-t" }, takeLabel(t) + (r.s.selected_take === t.id ? " · selected" : "")),
        h("span", { class: "tile-m" }, h("span", {}, `${((t.frames || 124) / 24).toFixed(1)} s`), Object.keys(t.upscales || {}).length ? h("span", { class: "at" }, `⤢ ${Object.keys(t.upscales).length}`) : null)),
        { file: `${t.dir}/take.mp4`, kind: "video", title: `Scene ${scenes().findIndex((x) => x.id === t.scene) + 1} · ${takeLabel(t)}`, sub: `${t.width}×${t.height} · seed ${t.seed}`, take: t.id })))))
      : [h("div", { class: "empty-state" }, h("p", {}, "No finished takes yet — render scenes in Scenes."))]));
}

function aspectOf(a) {   // keep an image's shape when making versions of it
  if (!a.width || !a.height) return "9:16";
  const shapes = { "1:1": 1, "9:16": 9 / 16, "16:9": 16 / 9, "3:4": 3 / 4, "4:3": 4 / 3, "2:3": 2 / 3, "3:2": 3 / 2 };
  const r = a.width / a.height;
  return Object.keys(shapes).reduce((best, k) => (Math.abs(shapes[k] - r) < Math.abs(shapes[best] - r) ? k : best), "1:1");
}

function takeDetail(t) {
  const sc = sceneById(t.scene); const n = scenes().indexOf(sc) + 1;
  const v = h("video", { src: takeVideo(t), controls: true, playsinline: true, preload: "metadata" });
  return [h("div", { class: "big-prev" }, v),
    h("b", {}, `Scene ${n}${sc?.title ? " · " + sc.title : ""} · ${takeLabel(t)}`),
    h("p", { class: "hint" }, `${t.width}×${t.height} · ${((t.frames || 124) / 24).toFixed(1)} s · seed ${t.seed} · ${t.model || "fl2va"}${sc?.selected_take === t.id ? " · selected" : ""}`),
    h("div", { class: "row" },
      h("button", { onclick: () => once("frame", async () => {
        v.pause();
        const d = await call("/asset/frame", { path: state.path, take_id: t.id, seconds: v.currentTime });
        setProject(d.project); toast(`Frame at ${v.currentTime.toFixed(1)} s saved to Start images & frames`);
      }) }, "Save this frame"),
      h("button", { onclick: () => openUpscale([t.id], `Upscale scene ${n} · ${takeLabel(t)}`) }, "⤢ Upscale…"),
      h("button", { onclick: () => { showWs("scenes"); openEditor(t.scene); } }, "Open scene")),
    takeMadeFrom(t, sc),
    upscaleRow(t) || h("p", { class: "hint" }, "No upscaled versions yet.")];
}

async function loadExports() {
  const d = await guard(() => call(`/exports?path=${encodeURIComponent(state.path)}`));
  if (d) { studio.exports = d.exports; renderWorkspace(true); }
}
function exportsCollection() {
  const list = studio.exports;
  return h("div", {}, h("div", { class: "coll-head" }, h("h3", {}, "Exports"), h("span", { class: "hint" }, "Finished videos. Make new ones in Finish.")),
    !list ? h("p", { class: "hint" }, "Loading…") : list.length ? h("div", { class: "grid" }, list.map((x) => h("button", { class: "tile" + (studio.sel?.id === x.file ? " on" : ""), onclick: () => select("export", x.file) },
      h("span", { class: "thumb-box" }, posterImg(x.file, 0.5)),
      h("span", { class: "tile-t" }, fmtStamp(x.created) || x.file.split("/").pop()),
      h("span", { class: "tile-m" }, h("span", {}, `${(x.seconds || 0).toFixed(1)} s`), x.width ? h("span", {}, `${x.width}×${x.height}`) : null))))
      : h("div", { class: "empty-state" }, h("p", {}, "No exports yet."), h("button", { class: "primary", onclick: () => showWs("finish") }, "Go to Finish")));
}
const fmtStamp = (s) => { const m = /^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)/.exec(s || ""); return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` : ""; };
function exportDetail(x) {
  return [h("div", { class: "big-prev" }, h("video", { src: media(x.file), controls: true, playsinline: true, preload: "metadata" })),
    h("b", {}, fmtStamp(x.created) || x.file),
    h("p", { class: "hint" }, `${(x.seconds || 0).toFixed(1)} s · ${x.width || "?"}×${x.height || "?"} · scenes 1–${x.scenes} of ${x.scenes_total}${x.upscaled?.length ? " · upscaled: " + x.upscaled.join(", ") : ""} · ${fmtSize(x.bytes)}`),
    h("div", { class: "row" }, h("a", { class: "btn primary", href: media(x.file), download: x.file.split("/").pop() }, "⤓ Download"), h("span", { class: "spacer" }),
      h("button", { class: "ghost danger", onclick: () => guard(async () => {
        if (!confirm("Delete this exported video?")) return;
        const d = await call("/exports/delete", { path: state.path, file: x.file }); studio.exports = d.exports; studio.sel = null; renderWorkspace(true);
      }) }, "Delete"))];
}

// ------------------------------------------------------------------ New

function openNew(kind, opts = {}) {
  if (state.ws !== "studio") showWs("studio");
  studio.page = "new"; studio.newKind = kind || studio.newKind || "character"; studio.newOpts = opts; studio.sel = null;
  renderStudio(); window.scrollTo({ top: 0 });
}
// Older callers: openTool(toolId, values) opens New on that tool, filled in.
function openTool(toolId, values = {}) { toolState.current = toolId; toolState.values = { ...values }; openNew("tool:" + toolId); }
$("studioNew").onclick = () => openNew();

function renderNewPage() {
  const kinds = [...ENTITY_KINDS.map(([k, , one, ico]) => ["entity:" + k, `${ico} ${one}`]), ...((toolState.list || []).map((t) => ["tool:" + t.id, (t.kind === "audio" ? "🗣 " : "🖼 ") + t.title.replace(/ \(.*\)$/, "") + (t.ok ? "" : " ⚠")])), ["upload", "⬆ Upload files"]];
  if (!toolState.list) loadTools().then(() => renderStudio()).catch(() => {});
  let cur = studio.newKind || "character";
  if (ENTITY_KINDS.some((x) => x[0] === cur)) cur = "entity:" + cur;
  studio.newKind = cur;
  const body = cur.startsWith("entity:") ? newEntityForm(cur.slice(7)) : cur.startsWith("tool:") ? newToolForm(cur.slice(5)) : uploadForm();
  $("studioMain").replaceChildren(
    crumbs({ label: "Studio", onclick: () => { studio.page = null; renderStudio(); } }, { label: "New" }),
    h("div", { class: "new-kinds" }, h("div", { class: "cat-group" }, "Make"), h("div", { class: "seg wrap" }, kinds.map(([v, label]) => h("button", { class: v === cur ? "on" : "", onclick: () => { studio.newKind = v; if (v.startsWith("tool:")) { toolState.current = v.slice(5); toolState.values = {}; } renderStudio(); } }, label)))),
    body,
    h("div", { class: "coll-head" }, h("h3", {}, "Made here"), h("span", { class: "hint" }, "Results from this page; open one to name it, file it under a character or make versions.")),
    jobTiles("made"),
    h("div", { class: "grid" }, ...[...studio.made].flatMap((jid) => (state.project.jobs?.[jid]?.outputs || []).map((id) => assets()[id]).filter(Boolean)).map(assetTile)));
}

const newForms = {};   // kind → {name, desc, mode, picked, count}: kept while the page re-draws
function newEntityForm(kind) {
  const [, , one] = entityKind(kind);
  const from = studio.newOpts?.from ? assets()[studio.newOpts.from] : null;
  const f = newForms[kind] || (newForms[kind] = { name: from ? from.title.replace(/ \(\d+\)$/, "") : "", desc: from?.origin?.inputs?.prompt?.slice(0, 600) || "",
    mode: from ? "image" : "design", picked: from ? from.id : null, count: 2 });
  const name = h("input", { placeholder: kind === "character" ? "e.g. Aoi" : kind === "place" ? "e.g. Bus stop" : "Name", value: f.name });
  name.oninput = () => { f.name = name.value; };
  const desc = h("textarea", { rows: 4, placeholder: DESC_PH[kind] });
  desc.value = f.desc; desc.oninput = () => { f.desc = desc.value; };
  const mode = { get v() { return f.mode; }, set v(x) { f.mode = x; } };
  let picked = f.picked;
  const count = h("select", {}, [1, 2, 4].map((n) => h("option", { value: n, selected: n === f.count }, `${n} designs`)));
  count.onchange = () => { f.count = Number(count.value); };
  const pickBox = h("div", { class: "row" });
  const describeOut = h("span", { class: "hint" });
  const describe = async (auto) => {
    if (!picked) return;
    describeOut.textContent = "✨ The assistant is looking at the image…";
    try {
      const r = await call("/assistant/describe_image", { path: state.path, asset: picked, kind, auto });
      if (r.skipped) { describeOut.textContent = ""; return; }
      if (r.name && !f.name.trim()) { f.name = r.name; name.value = r.name; }
      f.desc = r.description; desc.value = r.description;
      describeOut.textContent = "✨ Written by the assistant from the image — check it and change anything.";
    } catch (e) { describeOut.textContent = ""; if (!auto) throw e; }
  };
  const drawPick = () => pickBox.replaceChildren(picked ? tileThumb(assets()[picked]) : h("span", { class: "hint" }, "No image chosen"),
    h("button", { onclick: () => pickAssets({ kind: "image", title: "Choose its first image", single: true }, (ids) => {
      picked = f.picked = ids[0]; drawPick();
      if (!f.desc.trim()) guard(() => describe(true));   // nothing written yet: let the assistant describe it right away
    }) }, picked ? "Change…" : "Choose from library…"),
    picked ? h("button", { ...busyAttr("describe"), title: "The assistant looks at the image and writes the name and description", onclick: () => once("describe", () => describe(false)) }, "✨ Describe it") : null,
    describeOut);
  drawPick();
  const modeSeg = h("div", { class: "seg" }, [["design", "Design it now"], ["image", "Start from an image I have"]].map(([v, l]) => h("button", { class: mode.v === v ? "on" : "", onclick: (ev) => { mode.v = v; ev.target.parentNode.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === ev.target)); designRow.hidden = v !== "design"; pickBox.hidden = v !== "image"; } }, l)));
  const designRow = h("div", { class: "row" }, h("label", { class: "grow-0" }, "How many", count), h("span", { class: "hint" }, "≈ 4–10 s per image; you pick the main look afterwards."));
  designRow.hidden = mode.v !== "design"; pickBox.hidden = mode.v !== "image";
  const go = h("button", { class: "primary big", ...busyAttr("new-entity"), onclick: () => once("new-entity", async () => {
    if (!name.value.trim()) throw new Error("Give it a name");
    if (mode.v === "image" && !picked) throw new Error("Choose an image, or design it now");
    const d = await call("/entity", { path: state.path, action: "create", kind, name: name.value.trim(), description: desc.value.trim(), from_asset: mode.v === "image" ? picked : null });
    setProject(d.project);
    studio.newOpts = {}; delete newForms[kind];
    if (mode.v === "design") {
      try {
        const g = await call("/entity/generate", { path: state.path, id: d.entity, action: "first", count: Number(count.value) });
        setProject(g.project);
      } catch (err) {   // it exists now: open it (creating it again would make a second one)
        openEntity(d.entity);
        throw new Error(`${name.value.trim()} was created, but designing failed: ${err.message}. Try "Design it" on its page.`);
      }
    }
    openEntity(d.entity);
  }) }, `Create ${one.toLowerCase()}`);
  const words = h("textarea", { rows: 2, placeholder: kind === "character" ? "e.g. a shy 18-year-old boy who always carries a camera, school uniform, messy dark hair, glasses"
    : kind === "place" ? "e.g. a small ramen shop at night, steamy windows, red lanterns, narrow counter" : kind === "prop" ? "e.g. her grandmother's old film camera, brass and black leather" : "e.g. soft watercolour anime, pastel colours" });
  words.value = f.words || ""; words.oninput = () => { f.words = words.value; };
  const wordsOut = h("span", { class: "hint" });
  const write = h("button", { class: "primary", ...busyAttr("write-desc"), title: "The assistant writes the name and the description from your words (and from the image, if you chose one)",
    onclick: () => once("write-desc", async () => {
      if (!words.value.trim() && !(mode.v === "image" && picked)) throw new Error("Say in your own words what it is");
      wordsOut.textContent = "✨ Writing…";
      try {
        const r = await call("/assistant/write_description", { path: state.path, kind, words: words.value, name: name.value, asset: mode.v === "image" ? picked : null });
        if (r.name && !name.value.trim()) { name.value = r.name; f.name = r.name; }
        desc.value = r.description; f.desc = r.description;
        wordsOut.textContent = "✨ Written by the assistant — change anything you like, then create it.";
      } catch (err) { wordsOut.textContent = ""; throw err; }
    }) }, "✨ Write it");
  return h("section", { class: "card new-form" }, h("h3", {}, `New ${one.toLowerCase()}`),
    h("div", { class: "assist-row" }, h("label", { class: "grow" }, `✨ Tell the assistant about it, in your own words — it writes the name and the description`, words), write),
    wordsOut,
    h("div", { class: "form-grid" }, h("label", {}, "Name — becomes its @name in scenes", name), h("label", { class: "wide" }, "Description — what the image and video models get (written by the assistant, or by you)", desc)),
    modeSeg, designRow, pickBox, h("div", { class: "row" }, go));
}

function newToolForm(toolId) {
  const tool = (toolState.list || []).find((t) => t.id === toolId);
  if (!tool) return h("p", { class: "hint" }, "Loading tools…");
  toolState.current = tool.id;
  const values = toolState.values;
  if (tool.id === "image" && values.aspect == null) values.aspect = state.project.video_aspect || "9:16";   // the video's shape
  const role = h("select", {}, (state.project.asset_roles || Object.keys(ROLE_LABEL)).map((r) => h("option", { value: r, selected: r === tool.default_role }, ROLE_LABEL[r] || r)));
  const count = h("select", {}, [1, 2, 4, 8].map((n) => h("option", { value: n, selected: n === 2 }, String(n))));
  const title = h("input", { placeholder: "optional" });
  const seed = h("input", { type: "number", placeholder: "random" });
  // images: file the results straight into a character / place as looks
  const under = tool.kind === "image" ? h("select", {}, h("option", { value: "" }, "— the library only"),
    ...ENTITY_KINDS.map(([k, label]) => { const list = Object.values(entities()).filter((x) => x.kind === k); return list.length ? h("optgroup", { label }, list.map((x) => h("option", { value: x.id, selected: x.id === values._under }, x.name))) : null; }).filter(Boolean)) : null;
  if (under) under.onchange = () => { values._under = under.value; lookName.hidden = !under.value; };
  const lookName = h("input", { placeholder: "look, e.g. winter outfit", value: values._look || "", hidden: !values._under });
  lookName.oninput = () => { values._look = lookName.value; };
  const go = h("button", { class: "primary big", disabled: !tool.ok, ...busyAttr("tool"), onclick: () => once("tool", async () => {
    const inputs = Object.fromEntries(tool.fields.map((f) => [f.key, values[f.key] ?? f.default ?? null]));
    const body = { path: state.path, tool: tool.id, inputs, count: Number(count.value), role: role.value, title: title.value };
    if (under && under.value) { body.entity = under.value; body.look = lookName.value.trim() || title.value.trim() || "look"; }
    if (seed.value) body.seed = Number(seed.value);
    const d = await call("/tool/run", body);
    d.jobs.forEach((j) => studio.made.add(j));
    setProject(d.project); toast(`${d.jobs.length} queued — results appear below`);
  }) }, "Create");
  return h("section", { class: "card new-form" }, h("h3", {}, tool.title),
    h("p", { class: "hint" }, tool.description, tool.ok ? "" : h("span", { class: "warn" }, ` Cannot run on this ComfyUI: missing ${tool.missing.join(", ")}.`)),
    h("div", { class: "form-grid" }, ...tool.fields.filter((f) => !f.advanced).map((f) => toolField(f, values))),
    tool.fields.some((f) => f.advanced) ? h("details", { class: "adv" }, h("summary", {}, "More options"), h("div", { class: "form-grid" }, ...tool.fields.filter((f) => f.advanced).map((f) => toolField(f, values)), h("label", {}, "Seed", seed))) : null,
    under ? h("div", { class: "row" }, h("label", {}, "Save results under", under), h("label", { class: "grow" }, lookName)) : null,
    h("div", { class: "row tool-run" }, h("label", {}, "Results", count), h("label", {}, "Save as", role), h("label", { class: "grow" }, "Title", title), go));
}

function uploadForm() {
  const role = h("select", {}, (state.project.asset_roles || Object.keys(ROLE_LABEL)).map((r) => h("option", { value: r }, ROLE_LABEL[r] || r)));
  const input = h("input", { type: "file", multiple: true, accept: "image/*,audio/*,video/*,.m4a,.flac,.ogg,.opus" });
  input.onchange = () => { const f = [...input.files]; input.value = ""; uploadFiles(f, role.value); };
  return h("section", { class: "card new-form drop-zone" }, h("h3", {}, "Upload files"),
    h("p", { class: "hint" }, "Images, sounds (mp3, wav, m4a, ogg, flac…) and video clips — or drop them anywhere in the Studio."),
    h("div", { class: "row" }, h("label", {}, "Save as", role), h("label", { class: "btn primary file-label" }, "Choose files…", input)));
}

// New images get a title and "what it shows" from the assistant in the background — when it can see images and
// (for the local model) ComfyUI is not rendering; otherwise ✨ Describe in the image's panel does it later.
async function autoDescribe(ids) {
  let done = 0;
  for (const id of ids) {
    try {
      const r = await call("/assistant/describe_image", { path: state.path, asset: id, auto: true, save: true });
      if (r.skipped) { if (!done) toast(`Not described: ${r.skipped}`); return; }
      setProject(r.project); done++;
    } catch (e) { return; }   // the assistant is not set up: nothing to say on every upload
  }
  if (done) toast(`✨ ${done} image${done > 1 ? "s" : ""} described by the assistant`);
}

async function uploadFiles(files, role) {
  if (!files.length) return;
  const fd = new FormData();
  fd.append("path", state.path); fd.append("role", role || guessRole(files[0]));
  for (const f of files) fd.append("file", f);
  await once("upload", async () => {
    toast(`Adding ${files.length} file(s)…`);
    const d = await call("/asset", fd);
    setProject(d.project); toast(`${d.added.length} added`);
    autoDescribe(d.added.filter((id) => assets()[id]?.kind === "image").slice(0, 8));
    if (d.added.length === 1) { studio.page = null; studio.cat = "files"; select("asset", d.added[0]); }
  });
}
const guessRole = (f) => (f.type.startsWith("audio") ? "voice" : f.type.startsWith("video") ? "clip" : "other");
$("libFiles").onchange = () => { const f = [...$("libFiles").files]; $("libFiles").value = ""; uploadFiles(f); };
$("wsStudio").addEventListener("dragover", (e) => { e.preventDefault(); $("wsStudio").classList.add("drop"); });
$("wsStudio").addEventListener("dragleave", (e) => { if (!$("wsStudio").contains(e.relatedTarget)) $("wsStudio").classList.remove("drop"); });
$("wsStudio").addEventListener("drop", (e) => { e.preventDefault(); $("wsStudio").classList.remove("drop"); uploadFiles([...(e.dataTransfer?.files || [])]); });
let searchTimer;
$("studioSearch").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { studio.search = $("studioSearch").value.trim(); if (studio.page === "new") studio.page = null; renderStudio(); }, 200); };

// A picker for library items (used to add looks and to choose images).
function pickAssets({ kind, title, single, exclude = [] }, done) {
  const dlg = $("pickDlg");
  const chosen = new Set();
  const list = Object.values(assets()).filter((a) => a.kind === kind && !exclude.includes(a.id)).sort((a, b) => (b.created || "").localeCompare(a.created || ""));
  const draw = () => {
    $("pickBody").replaceChildren(list.length ? h("div", { class: "grid" }, list.map((a) => h("button", { class: "tile" + (chosen.has(a.id) ? " on" : ""), onclick: () => {
      if (single) { chosen.clear(); chosen.add(a.id); } else chosen.has(a.id) ? chosen.delete(a.id) : chosen.add(a.id); draw();
    } }, tileThumb(a), h("span", { class: "tile-t" }, a.title)))) : h("p", { class: "hint" }, "No matching items in the library."));
    $("pickGo").disabled = !chosen.size; $("pickGo").textContent = chosen.size > 1 ? `Add ${chosen.size}` : "Choose";
  };
  $("pickTitle").textContent = title; draw();
  $("pickGo").onclick = () => { dlg.close(); done([...chosen]); };
  dlg.showModal();
}

// ------------------------------------------------------------------ Finish

const finishSel = new Set();
let lastExport = null;   // the export just made, shown until another one is made or the project changes
async function renderFinish() {
  const body = $("finishBody");
  const rows = scenes().map((s, i) => ({ s, n: i + 1, take: state.project.takes[s.selected_take] }));
  const ready = rows.filter((r) => r.take && r.take.status === "done");
  for (const id of [...finishSel]) if (!ready.some((r) => r.take.id === id)) finishSel.delete(id);
  const sceneRows = rows.map((r) => {
    const t = r.take; const ok = t && t.status === "done";
    const box = h("input", { type: "checkbox", disabled: !ok, checked: ok && finishSel.has(t.id), onchange: () => { box.checked ? finishSel.add(t.id) : finishSel.delete(t.id); renderFinish(); } });
    return h("div", { class: "fin-row" + (ok ? "" : " not-ready") },
      h("label", { class: "fin-pick" }, box),
      h("button", { class: "fin-thumb", disabled: !ok, onclick: () => ok && play(t, `Scene ${r.n} · ${takeLabel(t)}`) }, ok ? posterImg(`${t.dir}/take.mp4`, 0.1) : h("span", { class: "hint" }, "no take")),
      h("div", { class: "fin-info" }, h("b", {}, `Scene ${r.n}${r.s.title ? " · " + r.s.title : ""}`),
        h("span", { class: "hint" }, ok ? `${takeLabel(t)} · ${((t.frames || 124) / 24).toFixed(1)} s · ${t.width}×${t.height}` : "no finished selected take — render it in Scenes"),
        ok ? upscaleRow(t) : null),
      h("div", { class: "fin-act" }, ok ? h("button", { onclick: () => openUpscale([t.id], `Upscale scene ${r.n}`) }, "⤢ Upscale…") : h("button", { onclick: () => { showWs("scenes"); openEditor(r.s.id); } }, "Open scene")));
  });
  const notUp = ready.filter((r) => !Object.keys(r.take.upscales || {}).length).map((r) => r.take.id);
  const c = await guard(() => call(`/export/check?path=${encodeURIComponent(state.path)}`));
  if (!c) return;
  if (!studio.exports) studio.exports = (await guard(() => call(`/exports?path=${encodeURIComponent(state.path)}`)))?.exports || [];
  const size = h("select", { id: "exportSize" }, [["auto", c.size ? `Largest scene (${c.size[0]}×${c.size[1]})` : "Largest scene"], ["720p", "720p"], ["1080p", "1080p"], ["original", "Original takes, no upscales"]].map(([v, l]) => h("option", { value: v }, l)));
  const result = h("div", {});
  const useMix = h("input", { type: "checkbox", checked: true });
  const showResult = (d) => {
    const m = d.manifest;
    result.replaceChildren(h("div", { class: "card export-done" }, h("b", {}, "Exported"), h("video", { src: media(d.file), controls: true, playsinline: true, preload: "metadata", poster: thumbUrl(d.file, 0.5) }),
      h("p", { class: "hint" }, `${m.total_seconds.toFixed(1)} s · ${m.width}×${m.height} · scenes 1–${m.scenes_exported} of ${m.scenes_total}${m.finish ? ` · ${m.finish.tracks.length} sound track(s), ${m.finish.overlays.length} overlay(s)` : ""}`),
      h("a", { class: "btn primary", href: media(d.file), download: d.file.split("/").pop() }, "⤓ Download")));
  };
  // the export just made: shown while it exists; marked when the project changed since
  if (lastExport && lastExport.path === state.path) {
    const now = studio.exports.find((x) => x.file === lastExport.file);
    if (!now) lastExport = null;
    else { showResult(lastExport); if (now.changed_since?.length) result.append(h("p", { class: "hint warn" }, `Out of date: ${now.changed_since.join("; ")}. Export again for the current version.`)); }
  }
  const label = c.complete ? "Export the video" : `Export scenes 1–${c.ready} (partial)`;
  const go = h("button", { class: "primary big", disabled: !c.ready, ...busyAttr("export"), onclick: () => once("export", async () => {
    go.textContent = "Exporting…";
    try {
      const d = await call("/export", { path: state.path, partial: !c.complete, size: size.value, use_finish: useMix.checked });
      lastExport = { ...d, path: state.path }; studio.exports = null;
      showResult(d); toast("Exported"); loadExports();
    } finally { go.textContent = label; }
  }) }, label);
  body.replaceChildren(
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h3", {}, "1 · Scenes"), h("span", { class: "spacer" }),
        h("button", { class: "ghost small", onclick: () => { ready.forEach((r) => finishSel.add(r.take.id)); renderFinish(); } }, "Select all"),
        h("button", { class: "ghost small", disabled: !finishSel.size, onclick: () => { finishSel.clear(); renderFinish(); } }, "Clear"),
        h("button", { disabled: !finishSel.size && !notUp.length, onclick: () => openUpscale(finishSel.size ? [...finishSel] : notUp, "Upscale scenes") }, finishSel.size ? `⤢ Upscale ${finishSel.size} selected…` : `⤢ Upscale the ${notUp.length} not upscaled…`)),
      h("p", { class: "hint" }, "Finish each scene on its own: upscale it (Sharpen or Re-detail, 720p or 1080p) and choose which version goes into the video. Upscales run one after another."),
      rows.length ? h("div", { class: "fin-list" }, sceneRows) : h("p", { class: "hint" }, "No scenes yet.")),
    mixCard(c.seconds),
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h3", {}, "3 · Export")),
      c.ready ? h("p", {}, c.complete ? `All ${c.total} scenes · ${c.seconds.toFixed(1)} s` : `Scenes 1–${c.ready} of ${c.total} are ready · ${c.seconds.toFixed(1)} s`,
        c.upscaled.length ? ` · upscaled: ${c.upscaled.join(", ")}` : " · none upscaled",
        c.upscaled.length && c.not_upscaled.length ? h("span", { class: "hint" }, ` (scene${c.not_upscaled.length > 1 ? "s" : ""} ${c.not_upscaled.join(", ")} resized to match, softer)`) : null) : h("p", { class: "hint" }, "Scene 1 has no finished take yet."),
      !c.complete && c.ready ? h("p", { class: "hint warn" }, `Scene${c.missing.length > 1 ? "s" : ""} ${c.missing.join(", ")} ${c.missing.length > 1 ? "have" : "has"} no finished take — the export stops before ${c.missing.length > 1 ? "them" : "it"}.`) : null,
      c.warnings.length ? h("ul", { class: "warn-list" }, c.warnings.map((w) => h("li", {}, w.at_seconds == null ? `${w.message}.` : `${w.message} (at ${w.at_seconds.toFixed(1)} s).`))) : null,
      h("div", { class: "row" }, h("label", {}, "Video size", size), h("label", { class: "switch" }, useMix, "With sound & overlays"), go),
      result),
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h3", {}, "4 · Previous exports")),
      studio.exports.length ? h("div", { class: "exp-list" }, studio.exports.map((x) => h("div", { class: "exp-row" },
        h("button", { class: "fin-thumb", onclick: () => playFile(x.file, fmtStamp(x.created) || x.file) }, posterImg(x.file, 0.5)),
        h("div", { class: "fin-info" }, h("b", {}, fmtStamp(x.created) || x.file.split("/").pop()), h("span", { class: "hint" }, `${(x.seconds || 0).toFixed(1)} s · ${x.width || "?"}×${x.height || "?"} · ${fmtSize(x.bytes)}`
          + (x.scenes && x.scenes_total && x.scenes < x.scenes_total ? ` · scenes 1–${x.scenes} of ${x.scenes_total}` : "")
          + (x.upscaled?.length ? " · upscaled " + x.upscaled.join(", ") : "")),
          x.changed_since?.length ? h("span", { class: "hint warn", title: x.changed_since.join("\n") }, `Out of date: ${x.changed_since.slice(0, 2).join("; ")}${x.changed_since.length > 2 ? "…" : ""}`) : null),
        h("div", { class: "fin-act" }, h("button", { onclick: () => playFile(x.file, fmtStamp(x.created) || x.file) }, "▶"), h("a", { class: "btn", href: media(x.file), download: x.file.split("/").pop() }, "⤓"),
          h("button", { class: "icon", title: "Delete", onclick: () => guard(async () => { if (!confirm("Delete this exported video?")) return; const d = await call("/exports/delete", { path: state.path, file: x.file }); studio.exports = d.exports; renderFinish(); }) }, "✕")))))
        : h("p", { class: "hint" }, "No exports yet.")));
  drawTimeline();
}

// ------------------------------------------------------------------ Finish: sound & overlays

let mix = null;          // the editable copy of project.finish
let mixTimer = null, mixRev = 0, mixBusy = false;   // edits made / a save waiting or in flight
const POS_LABEL = { "top-left": "Top left", top: "Top", "top-right": "Top right", left: "Left", center: "Centre", right: "Right",
  "bottom-left": "Bottom left", bottom: "Bottom", "bottom-right": "Bottom right", custom: "Custom (x, y)" };
function mixState() {
  const f = state.project.finish || {};
  const base = JSON.stringify(f);
  // reload when the project's finish changed elsewhere (another device, the CLI) and nothing here is waiting to be saved
  if (!mix || mix.path !== state.path || (!mixBusy && base !== mix.base))
    mix = { path: state.path, base, scene_volume: f.scene_volume ?? 1, tracks: structuredClone(f.tracks || []), overlays: structuredClone(f.overlays || []) };
  return mix;
}
function saveMix(redraw) {
  clearTimeout(mixTimer);
  mixRev++; mixBusy = true;
  mixTimer = setTimeout(() => guard(async () => {
    const { scene_volume, tracks, overlays } = mix, rev = mixRev, m = mix;
    try {
      const d = await call("/finish", { path: state.path, finish: { scene_volume, tracks, overlays } });
      state.project = d;
      // update the rows' own objects in place (the controls on screen keep pointing at them): the ids the server gave,
      // and its cleaned values only if nothing was edited while saving
      const merge = (mine, saved) => saved.forEach((x, i) => { if (!mine[i]) return; if (mixRev === rev) Object.assign(mine[i], x); else mine[i].id ||= x.id; });
      merge(m.tracks, d.finish.tracks); merge(m.overlays, d.finish.overlays);
      if (mixRev === rev) { m.base = JSON.stringify(d.finish); mixBusy = false; }
    } catch (e) { if (mixRev === rev) mixBusy = false; throw e; }
    if (redraw) renderFinish();
  }), 500);
  if (redraw) drawTimeline();
}
const num = (label, value, onchange, opts = {}) => {
  const i = h("input", { type: "number", value, step: opts.step || 0.1, min: opts.min ?? 0, max: opts.max, placeholder: opts.placeholder || "" });
  i.oninput = () => onchange(i.value === "" ? null : Number(i.value));
  return h("label", { class: "mini" }, label, i);
};
const slider = (label, value, onchange, { min = 0, max = 1.5, step = 0.05, pct = true } = {}) => {
  const out = h("b", {}, pct ? `${Math.round(value * 100)}%` : String(value));
  const r = h("input", { type: "range", min, max, step, value });
  r.oninput = () => { out.textContent = pct ? `${Math.round(r.value * 100)}%` : r.value; onchange(Number(r.value)); };
  return h("label", { class: "mini slider" }, h("span", {}, label, " ", out), r);
};

let timelineBox = null, mixTotal = 0;
function drawTimeline() {
  if (!timelineBox) return;
  const total = mixTotal || 1;
  const pct = (s) => `${Math.max(0, Math.min(100, (s / total) * 100)).toFixed(2)}%`;
  const scenesRow = h("div", { class: "tl-row tl-scenes" });
  let t = 0;
  for (const [i, s] of scenes().entries()) {
    const take = state.project.takes[s.selected_take];
    if (!take || take.status !== "done") break;
    const len = ((take.frames || 124) - (take.context_frames || 0)) / 24;
    scenesRow.append(h("span", { class: "tl-scene", style: `left:${pct(t)};width:${pct(len)}`, title: `Scene ${i + 1}` }, String(i + 1)));
    t += len;
  }
  const bar = (cls, start, len, label) => h("span", { class: `tl-bar ${cls}`, style: `left:${pct(start)};width:${pct(Math.max(0.15, len))}`, title: label }, label);
  const tracks = mix.tracks.map((tr) => { const a = assets()[tr.asset] || {}; const len = tr.loop ? total - tr.start : Math.min((a.duration || total) - (tr.trim || 0), total - tr.start); return bar(a.role === "music" ? "music" : "sound", tr.start, len, a.title || "sound"); });
  const ovs = mix.overlays.map((o) => { const a = assets()[o.asset] || {}; const end = o.end ?? total; return bar("overlay", o.start, end - o.start, a.title || "overlay"); });
  const ticks = h("div", { class: "tl-ticks" }, ...Array.from({ length: Math.floor(total / 5) + 1 }, (_, k) => h("span", { style: `left:${pct(k * 5)}` }, `${k * 5}s`)));
  // one lane per track and per overlay, so nothing hides behind something else
  timelineBox.replaceChildren(scenesRow, ...[...tracks, ...ovs].map((b) => h("div", { class: "tl-row lane" }, b)), ticks);
}

function mixCard(total) {
  const m = mixState(); mixTotal = total;
  timelineBox = h("div", { class: "timeline" });
  const trackRows = m.tracks.map((tr, i) => {
    const a = assets()[tr.asset];
    const set = (k, redraw) => (v) => { tr[k] = v; saveMix(redraw); };
    return h("div", { class: "mix-row" },
      h("div", { class: "mix-title" }, h("b", {}, a ? a.title : "missing sound"), h("span", { class: "hint" }, a?.duration ? ` ${fmtDur(a.duration)}` : ""),
        h("span", { class: "spacer" }), h("button", { class: "icon", title: "Remove", onclick: () => { m.tracks.splice(i, 1); saveMix(true); renderFinish(); } }, "✕")),
      a ? h("audio", { src: media(a.file), controls: true, preload: "none" }) : null,
      h("div", { class: "mix-fields" },
        num("Starts at (s)", tr.start, set("start", true)),
        slider("Volume", tr.volume, set("volume")),
        num("Fade in (s)", tr.fade_in, set("fade_in")), num("Fade out (s)", tr.fade_out, set("fade_out")),
        h("label", { class: "switch" }, h("input", { type: "checkbox", checked: tr.loop, onchange: (e) => set("loop", true)(e.target.checked) }), "Loop to the end"),
        h("label", { class: "switch", title: "Lower this track while the scenes' own sound (voices, effects, ambience) is loud" }, h("input", { type: "checkbox", checked: tr.duck, onchange: (e) => set("duck")(e.target.checked) }), "Dip under the scenes' sound")));
  });
  const ovRows = m.overlays.map((o, i) => {
    const a = assets()[o.asset];
    const set = (k, redraw) => (v) => { o[k] = v; saveMix(redraw); };
    const pos = h("select", {}, Object.entries(POS_LABEL).map(([v, l]) => h("option", { value: v, selected: v === o.position }, l)));
    pos.onchange = () => { o.position = pos.value; saveMix(); renderFinish(); };
    return h("div", { class: "mix-row ov" },
      h("div", { class: "mix-title" }, a ? h("img", { class: "ov-thumb", src: media(a.file), alt: "" }) : null, h("b", {}, a ? a.title : "missing image"),
        h("span", { class: "spacer" }), h("button", { class: "icon", title: "Remove", onclick: () => { m.overlays.splice(i, 1); saveMix(true); renderFinish(); } }, "✕")),
      h("div", { class: "mix-fields" },
        h("label", { class: "mini" }, "Position", pos),
        o.position === "custom" ? num("x (0–1)", o.x, set("x"), { step: 0.01, max: 1 }) : null,
        o.position === "custom" ? num("y (0–1)", o.y, set("y"), { step: 0.01, max: 1 }) : null,
        slider("Size (of the width)", o.width, set("width"), { min: 0.05, max: 1, step: 0.01 }),
        slider("Opacity", o.opacity, set("opacity"), { min: 0, max: 1, step: 0.05 }),
        num("From (s)", o.start, set("start", true)), num("Until (s)", o.end, set("end", true), { placeholder: "the end" }),
        num("Fade (s)", o.fade, set("fade"))));
  });
  // title card form
  const tc = { text: "", style: "bold", color: "#ffffff", box: true };
  const tcText = h("textarea", { rows: 2, placeholder: "Title text — a second line is allowed" });
  tcText.oninput = () => (tc.text = tcText.value);
  const tcStyle = h("select", {}, [["bold", "Bold"], ["regular", "Regular"], ["serif", "Serif"]].map(([v, l]) => h("option", { value: v }, l)));
  tcStyle.onchange = () => (tc.style = tcStyle.value);
  const tcColor = h("input", { type: "color", value: "#ffffff" });
  tcColor.oninput = () => (tc.color = tcColor.value);
  const tcBox = h("input", { type: "checkbox", checked: true }); tcBox.onchange = () => (tc.box = tcBox.checked);
  const titleForm = h("details", { class: "adv" }, h("summary", {}, "＋ Title card…"),
    h("div", { class: "mix-fields" }, h("label", { class: "mini wide" }, "Text", tcText), h("label", { class: "mini" }, "Style", tcStyle), h("label", { class: "mini" }, "Colour", tcColor),
      h("label", { class: "switch" }, tcBox, "Dark box behind"),
      h("button", { class: "primary", ...busyAttr("title"), onclick: () => once("title", async () => {
        if (!tc.text.trim()) throw new Error("Type the title text");
        const d = await call("/finish/title", { path: state.path, text: tc.text, style: tc.style, color: tc.color, box: tc.box ? "#000000a0" : null, size: 110 });
        state.project = d.project;
        m.overlays.push({ asset: d.asset.id, start: 0, end: Math.min(3, total || 3), position: "top", width: 0.7, opacity: 1, fade: 0.4 });
        saveMix(true); renderFinish(); toast("Title card added — it is also in the Studio");
      }) }, "Make and add")),
    h("p", { class: "hint" }, "Rendered as a sharp image with a font that has your text's script (Latin, Devanagari, CJK, Arabic, …), then placed like any overlay."));
  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h3", {}, "2 · Sound & overlays"), h("span", { class: "hint" }, "Exact, added at export — nothing is re-rendered")),
    total ? timelineBox : h("p", { class: "hint" }, "The timeline appears when scene 1 has a take."),
    slider("Scenes' own sound", m.scene_volume, (v) => { m.scene_volume = v; saveMix(); }, { min: 0, max: 2 }),
    h("h4", {}, "Music & sound"),
    ...trackRows,
    h("div", { class: "row" }, h("button", { onclick: () => pickAssets({ kind: "audio", title: "Add music or a sound" }, (ids) => {
      for (const id of ids) { const a = assets()[id]; m.tracks.push({ asset: id, start: 0, volume: a?.role === "music" ? 0.5 : 1, fade_in: a?.role === "music" ? 1 : 0, fade_out: a?.role === "music" ? 2 : 0, loop: a?.role === "music", duck: a?.role === "music" }); }
      saveMix(true); renderFinish();
    }) }, "＋ Add music or sound"), h("span", { class: "hint" }, "From the Studio (upload music and effects there). Music loops and dips under the scenes' sound by default.")),
    h("h4", {}, "Overlays"),
    ...ovRows,
    h("div", { class: "row" }, h("button", { onclick: () => pickAssets({ kind: "image", title: "Add an image overlay (logo, product photo…)" }, (ids) => {
      for (const id of ids) m.overlays.push({ asset: id, start: 0, end: null, position: "bottom-right", width: 0.22, opacity: 1, fade: 0.4 });
      saveMix(true); renderFinish();
    }) }, "＋ Add image"), h("span", { class: "hint" }, "Logos and product photos stay exact — they are put on the picture, not generated.")),
    titleForm);
}

// The Style & notes dialog (Scenes).
$("styleBtn").onclick = () => { $("styleText").value = state.project.style || ""; $("notesText").value = state.project.notes || ""; $("styleDlg").showModal(); };
