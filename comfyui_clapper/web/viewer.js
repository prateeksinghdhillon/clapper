/* Clapper gallery viewer: full screen, next / previous, zoom and pan, and 2–3 images side by side to compare.
   openViewer(items, index): items are {file, kind ("image" | "video"), title, sub, asset?, take?}.
   Any grid can feed it: tiles with data-vfile (see viewItemsFrom) get a ⛶ button, and open on a second click
   (once selected) or a double click. */

const viewer = { items: [], panes: [], active: 0, link: true, el: null };
const ZOOM_MIN = 1, ZOOM_MAX = 12;

function openViewer(items, index = 0) {
  if (!items.length) return;
  viewer.items = items;
  viewer.panes = [newPane(index)];
  viewer.active = 0;
  if (!viewer.el) buildViewer();
  viewer.el.hidden = false;
  document.body.classList.add("viewer-open");
  renderViewer();
}

function closeViewer() {
  if (!viewer.el) return;
  viewer.el.hidden = true;
  document.body.classList.remove("viewer-open");
  viewer.el.querySelectorAll("video").forEach((v) => v.pause());
}

const newPane = (idx) => ({ idx: Math.max(0, Math.min(viewer.items.length - 1, idx)), s: 1, x: 0, y: 0 });
const wrapIdx = (i) => (i + viewer.items.length) % viewer.items.length;

function buildViewer() {
  const el = h("div", { id: "viewer", class: "viewer", hidden: true });
  document.body.append(el);
  viewer.el = el;
  document.addEventListener("keydown", (e) => {
    if (el.hidden || /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) return;
    const k = e.key;
    if (k === "Escape") closeViewer();
    else if (k === "ArrowRight") step(+1);
    else if (k === "ArrowLeft") step(-1);
    else if (k === "+" || k === "=") zoomActive(1.5);
    else if (k === "-") zoomActive(1 / 1.5);
    else if (k === "0") fitAll();
    else if (k === "1" || k === "2" || k === "3") setPaneCount(Number(k));
    else if (k === "Tab" && viewer.panes.length > 1) { viewer.active = (viewer.active + 1) % viewer.panes.length; renderViewer(); }
    else return;
    e.preventDefault();
  });
}

function setPaneCount(n) {
  const max = window.innerWidth < 700 ? 2 : 3;   // phones: two, one above the other
  n = Math.max(1, Math.min(n, max, viewer.items.length));
  const first = viewer.panes[0].idx;
  while (viewer.panes.length < n) viewer.panes.push(newPane(wrapIdx(viewer.panes[viewer.panes.length - 1].idx + 1)));
  viewer.panes.length = n;
  viewer.panes[0].idx = first;
  viewer.active = Math.min(viewer.active, n - 1);
  renderViewer();
}

function step(d, paneIndex = viewer.active) {
  const p = viewer.panes[paneIndex];
  p.idx = wrapIdx(p.idx + d);
  Object.assign(p, viewer.link && viewer.panes.length > 1 ? {} : { s: 1, x: 0, y: 0 });
  if (viewer.panes.length === 1) Object.assign(p, { s: 1, x: 0, y: 0 });
  renderViewer();
}

function fitAll() { viewer.panes.forEach((p) => Object.assign(p, { s: 1, x: 0, y: 0 })); applyTransforms(); }

function zoomActive(f) { const p = viewer.panes[viewer.active]; zoomAt(p, f, 0, 0); }

// Zoom pane p by factor f around (cx, cy), measured from the pane's centre; linked panes follow.
function zoomAt(p, f, cx, cy) {
  const s = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, p.s * f));
  const k = s / p.s;
  p.x = cx - (cx - p.x) * k; p.y = cy - (cy - p.y) * k; p.s = s;
  if (s === 1) { p.x = 0; p.y = 0; }
  follow(p);
  applyTransforms();
}

function follow(src) {
  if (!viewer.link) return;
  for (const p of viewer.panes) if (p !== src) Object.assign(p, { s: src.s, x: src.x, y: src.y });
}

function applyTransforms() {
  viewer.el.querySelectorAll(".vw-pane").forEach((paneEl, i) => {
    const p = viewer.panes[i]; const media = paneEl.querySelector(".vw-media");
    if (media) media.style.transform = `translate(${p.x}px, ${p.y}px) scale(${p.s})`;
    paneEl.classList.toggle("zoomed", p.s > 1);
    const z = paneEl.querySelector(".vw-zoom"); if (z) z.textContent = `${Math.round(p.s * 100)}%`;
  });
}

function renderViewer() {
  const el = viewer.el; const items = viewer.items; const multi = viewer.panes.length > 1;
  const act = viewer.panes[viewer.active]; const cur = items[act.idx];
  const bar = h("div", { class: "vw-bar" },
    h("span", { class: "vw-count" }, multi ? `${viewer.panes.length} side by side` : `${act.idx + 1} / ${items.length}`),
    h("b", { class: "vw-title" }, multi ? "" : cur.title || ""),
    h("span", { class: "spacer" }),
    h("div", { class: "seg vw-panes", title: "How many images side by side (keys 1, 2, 3)" },
      ...[1, 2, 3].map((n) => h("button", { class: viewer.panes.length === n ? "on" : "", disabled: n > items.length || (n === 3 && window.innerWidth < 700),
        onclick: () => setPaneCount(n) }, n === 1 ? "▢" : n === 2 ? "▢▢" : "▢▢▢"))),
    multi ? h("label", { class: "switch vw-link", title: "Zoom and move all images together, to compare the same spot" },
      h("input", { type: "checkbox", checked: viewer.link, onchange: (e) => { viewer.link = e.target.checked; if (viewer.link) follow(viewer.panes[viewer.active]); applyTransforms(); } }), "Zoom together") : null,
    h("button", { class: "icon", title: "Zoom out (−)", onclick: () => zoomActive(1 / 1.5) }, "−"),
    h("button", { class: "icon", title: "Zoom in (+)", onclick: () => zoomActive(1.5) }, "+"),
    h("button", { title: "Fit (0)", onclick: fitAll }, "Fit"),
    !multi && cur.asset ? h("button", { title: "Open its details in the Studio", onclick: () => { closeViewer(); if (state.ws !== "studio") showWs("studio"); select("asset", cur.asset); } }, "Details") : null,
    !multi && cur.take ? h("button", { title: "Open this take", onclick: () => { closeViewer(); if (state.ws !== "studio") showWs("studio"); select("take", cur.take); } }, "Details") : null,
    !multi ? h("a", { class: "btn", href: media(cur.file), download: (cur.title || "image").replace(/[^\w.-]+/g, "_") + "." + cur.file.split(".").pop(), title: "Download" }, "⤓") : null,
    h("button", { class: "icon vw-close", title: "Close (Esc)", onclick: closeViewer }, "✕"));
  const panes = h("div", { class: "vw-panes-box n" + viewer.panes.length }, ...viewer.panes.map((p, i) => paneEl(p, i)));
  const strip = h("div", { class: "vw-strip" }, ...items.map((it, i) => h("button", {
    class: "vw-thumb" + (i === act.idx ? " on" : "") + (viewer.panes.some((p) => p.idx === i) ? " shown" : ""), title: it.title || "",
    onclick: () => { act.idx = i; if (!multi) Object.assign(act, { s: 1, x: 0, y: 0 }); renderViewer(); } },
    it.kind === "video" ? posterImg(it.file, 0.1) : h("img", { src: media(it.file), loading: "lazy", alt: "" }))));
  el.replaceChildren(bar, panes, strip);
  applyTransforms();
  el.querySelector(".vw-thumb.on")?.scrollIntoView({ block: "nearest", inline: "center" });
}

function paneEl(p, i) {
  const it = viewer.items[p.idx]; const multi = viewer.panes.length > 1;
  const mediaEl = it.kind === "video"
    ? h("video", { class: "vw-media", src: media(it.file), controls: !multi, loop: true, playsinline: true, preload: "metadata", draggable: false })
    : h("img", { class: "vw-media", src: media(it.file), alt: "", draggable: false });
  const stage = h("div", { class: "vw-stage" }, mediaEl);
  const pane = h("div", { class: "vw-pane" + (multi && i === viewer.active ? " active" : "") },
    stage,
    h("button", { class: "vw-nav prev", title: "Previous (←)", onclick: (e) => { e.stopPropagation(); viewer.active = i; step(-1, i); } }, "‹"),
    h("button", { class: "vw-nav next", title: "Next (→)", onclick: (e) => { e.stopPropagation(); viewer.active = i; step(+1, i); } }, "›"),
    h("div", { class: "vw-cap" }, multi ? h("b", {}, `${p.idx + 1} / ${viewer.items.length} · ${it.title || ""}`) : null, it.sub ? h("span", {}, it.sub) : null,
      h("span", { class: "vw-zoom" }, "100%")),
    multi && it.kind === "video" ? h("button", { class: "vw-play", title: "Play / pause all", onclick: (e) => { e.stopPropagation(); playAll(); } }, "▶︎ ❚❚") : null);
  attachGestures(pane, stage, i);
  return pane;
}

function playAll() {
  const vids = [...viewer.el.querySelectorAll("video.vw-media")];
  const go = vids.some((v) => v.paused);
  vids.forEach((v) => { if (go) { v.currentTime = 0; v.play().catch(() => {}); } else v.pause(); });
}

// Wheel / pinch zoom around the pointer, drag to move, double click to toggle 2×, swipe for next / previous.
function attachGestures(pane, stage, i) {
  const p = viewer.panes[i];
  const centre = (e) => { const r = stage.getBoundingClientRect(); return [e.clientX - r.left - r.width / 2, e.clientY - r.top - r.height / 2]; };
  stage.addEventListener("wheel", (e) => {
    e.preventDefault(); viewer.active = i; pane.parentNode.querySelectorAll(".vw-pane").forEach((x, j) => x.classList.toggle("active", j === i && viewer.panes.length > 1));
    const [cx, cy] = centre(e); zoomAt(p, e.deltaY < 0 ? 1.2 : 1 / 1.2, cx, cy);
  }, { passive: false });
  stage.addEventListener("dblclick", (e) => { const [cx, cy] = centre(e); if (p.s > 1) { Object.assign(p, { s: 1, x: 0, y: 0 }); follow(p); applyTransforms(); } else zoomAt(p, 2.5, cx, cy); });
  const pts = new Map(); let last = null, pinch = null, swipe = null;
  stage.addEventListener("pointerdown", (e) => {
    if (e.target.closest("video") && e.offsetY > e.target.clientHeight - 48) return;   // a video's own controls
    e.preventDefault();   // no native image drag or text selection: it would cancel the pointer and stop the move
    stage.setPointerCapture(e.pointerId); pts.set(e.pointerId, [e.clientX, e.clientY]);
    if (viewer.active !== i) { viewer.active = i; pane.parentNode.querySelectorAll(".vw-pane").forEach((x, j) => x.classList.toggle("active", j === i && viewer.panes.length > 1)); }
    if (pts.size === 2) { const [a, b] = [...pts.values()]; pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), s: p.s }; swipe = null; }
    else { last = [e.clientX, e.clientY]; swipe = p.s === 1 ? { x: e.clientX, t: Date.now() } : null; }
  });
  stage.addEventListener("pointermove", (e) => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, [e.clientX, e.clientY]);
    if (pinch && pts.size === 2) {
      const [a, b] = [...pts.values()]; const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      const r = stage.getBoundingClientRect(); const mx = (a[0] + b[0]) / 2 - r.left - r.width / 2, my = (a[1] + b[1]) / 2 - r.top - r.height / 2;
      zoomAt(p, (pinch.s * d / pinch.d) / p.s, mx, my);
    } else if (last && p.s > 1) {
      p.x += e.clientX - last[0]; p.y += e.clientY - last[1]; last = [e.clientX, e.clientY]; follow(p); applyTransforms();
    }
  });
  const up = (e) => {
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (swipe && pts.size === 0) {
      const dx = e.clientX - swipe.x;
      if (Math.abs(dx) > 60 && Date.now() - swipe.t < 600) step(dx < 0 ? +1 : -1, i);
    }
    if (!pts.size) { last = null; swipe = null; }
  };
  stage.addEventListener("pointerup", up); stage.addEventListener("pointercancel", up);
}

// ------------------------------------------------------------------ wiring into the Studio's grids

// The images (and videos) of the grid a tile is in, in the order shown.
function viewItemsFrom(grid) {
  return [...grid.querySelectorAll("[data-vfile]")].map((el) => ({ file: el.dataset.vfile, kind: el.dataset.vkind || "image",
    title: el.dataset.vtitle || "", sub: el.dataset.vsub || "", asset: el.dataset.vasset || null, take: el.dataset.vtake || null }));
}
function openFromTile(tile) {
  const grid = tile.closest(".grid, .strip, .sheet-list, #edTakes") || tile.parentNode;
  const items = viewItemsFrom(grid);
  const idx = [...grid.querySelectorAll("[data-vfile]")].indexOf(tile);
  openViewer(items, Math.max(0, idx));
}
// Tiles get a ⛶ button; a double click also opens the viewer.
function decorateViewTiles(root) {
  root.querySelectorAll("[data-vfile]:not([data-vdeco])").forEach((tile) => {
    tile.dataset.vdeco = "1";
    const z = h("span", { class: "tile-view", role: "button", title: "View large — zoom, next / previous, compare" }, "⛶");
    z.addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); openFromTile(tile); });
    (tile.querySelector(".thumb-box, .thumb") || tile).append(z);
  });
}
// A double click opens the viewer. The first click of the pair selects the tile and re-draws the Studio (the
// panel opens and the grid moves), so the browser's own dblclick rarely fires: two clicks on the same picture
// within 450 ms count, wherever that picture is drawn now.
let lastTileClick = null;
document.addEventListener("click", (e) => {
  const tile = e.target.closest?.("[data-vfile]");
  if (!tile || tile.closest("#viewer, dialog") || e.target.closest(".tile-view")) { lastTileClick = null; return; }
  const now = Date.now(), file = tile.dataset.vfile;
  // a click on the picture that is already selected (its details are open) opens it large
  const selected = typeof studio !== "undefined" && studio.sel && (studio.sel.id === tile.dataset.vasset || studio.sel.id === tile.dataset.vtake);
  if (selected && state.ws === "studio") {
    e.stopPropagation(); e.preventDefault(); lastTileClick = null;
    openFromTile(tile);
    return;
  }
  if (lastTileClick && lastTileClick.file === file && now - lastTileClick.t < 450) {
    lastTileClick = null;
    const fresh = [...document.querySelectorAll("[data-vfile]")].find((t) => t.dataset.vfile === file && t.isConnected) || tile;
    setTimeout(() => openFromTile(fresh), 0);   // after the Studio has re-drawn
  } else lastTileClick = { file, t: now };
}, true);
// Mark a tile as viewable (used by the Studio when it draws tiles).
function viewable(tile, it) {
  if (!it || !it.file) return tile;
  Object.assign(tile.dataset, { vfile: it.file, vkind: it.kind || "image", vtitle: it.title || "", vsub: it.sub || "" });
  if (it.asset) tile.dataset.vasset = it.asset;
  if (it.take) tile.dataset.vtake = it.take;
  return tile;
}
// (not in pickers: they are modal dialogs, which always stay above the viewer)
new MutationObserver(() => { for (const id of ["studioMain", "studioPanel", "edTakes"]) { const r = document.getElementById(id); if (r) decorateViewTiles(r); } })
  .observe(document.body, { childList: true, subtree: true });
