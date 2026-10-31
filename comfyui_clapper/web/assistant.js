/* Clapper assistant: chat panel, proposals, GPU menu, assistant settings. Uses helpers from app.js. */
"use strict";

// ------------------------------------------------------------------ header: status pill, popovers

const gpu = { timer: null, status: null };
const HOLDER = { assistant: ["GPU · AI", "llm"], h3: ["GPU · Video", "h3"], free: ["GPU · Free", "free"] };
const BUSY = { freeing: "Freeing GPU…", loading: "Loading assistant…", ejecting: "Ejecting…" };

function closePops() { for (const id of ["statusMenu", "appMenu"]) $(id).hidden = true; }
function togglePop(id, anchor) {
  const pop = $(id), open = pop.hidden;
  closePops();
  if (!open) return;
  pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  pop.style.top = `${r.bottom + 6}px`;
  if (innerWidth <= 640) { pop.style.left = "8px"; pop.style.right = "8px"; }  // a full-width sheet on phones
  else { pop.style.right = "auto"; pop.style.left = `${Math.max(8, Math.min(r.right - pop.offsetWidth, innerWidth - pop.offsetWidth - 8))}px`; }
}
$("statusPill").onclick = (e) => { e.stopPropagation(); togglePop("statusMenu", $("statusPill")); if (!$("statusMenu").hidden) pollGpu(); };
$("menuBtn").onclick = (e) => { e.stopPropagation(); togglePop("appMenu", $("menuBtn")); };
document.addEventListener("click", (e) => { if (!e.target.closest(".pop")) closePops(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePops(); });
addEventListener("resize", closePops);

// Panels (chat, scene editor) start below the header, whatever its height.
new ResizeObserver(() => document.documentElement.style.setProperty("--top-h", `${$("stickyTop").offsetHeight}px`)).observe($("stickyTop"));
// on tablets the workspace tabs are a row under the header: side panels start below it, so the tabs stay usable
const navOffset = () => { const n = $("wsNav"); const row = n && getComputedStyle(n).position === "sticky" && n.offsetHeight < 120;
  document.documentElement.style.setProperty("--nav-h", row ? `${n.offsetHeight}px` : "0px"); };
if ($("wsNav")) { new ResizeObserver(navOffset).observe($("wsNav")); window.addEventListener("resize", navOffset); }

/** One pill for everything the header used to show: rendering progress, GPU owner, waiting jobs, load. */
function renderStatus() {
  if (!state.me) return;
  const pill = $("statusPill"); pill.hidden = false;
  const cur = state.live?.current, q = state.live?.queue, s = gpu.status;
  let text, cls;
  if (cur) { text = cur.total ? `Rendering ${cur.step}/${cur.total}` : `Rendering · ${cur.phase}`; cls = "render"; }
  else if (s && BUSY[s.state]) { text = BUSY[s.state]; cls = "busy llm"; }
  else if (s) { [text, cls] = HOLDER[s.holder] || ["GPU", ""]; }
  else { text = "Status"; cls = ""; }
  if (q && q.pending) text += ` · +${q.pending}`;
  pill.className = `status-pill ${cls}` + (s?.mode === "manual" ? " manual" : "");
  $("statusText").textContent = text;
  const sys = state.sys;
  $("statusMini").textContent = sys && sys.gpu ? `GPU ${Math.round(sys.gpu.percent)}% · VRAM ${(sys.gpu.vram_used / 1024 ** 3).toFixed(1)}/${Math.round(sys.gpu.vram_total / 1024 ** 3)} GB` : "";
  $("queueLine").textContent = q ? (q.running || q.pending ? `ComfyUI queue: ${q.running} running, ${q.pending} waiting` : "ComfyUI queue: empty") : "";
}

function gpuStart() { pollGpu(); }
function gpuStop() { clearTimeout(gpu.timer); gpu.status = null; }
async function pollGpu() {
  clearTimeout(gpu.timer);
  if (!state.me) return;
  try { gpu.status = await call("/gpu"); renderGpu(); } catch (e) { /* server busy or restarting */ }
  gpu.timer = setTimeout(pollGpu, $("statusMenu").hidden ? 6000 : 2000);
}
function renderGpu() {
  const s = gpu.status; if (!s) return;
  const busy = BUSY[s.state];
  renderStatus();
  const llm = s.llm;
  $("gpuState").innerHTML = [
    `<div><b>Assistant model</b> ${llm.reachable ? (llm.loaded ? "<span class='ok'>loaded</span>" : "not loaded") : "<span class='bad'>server not running</span>"}<br><span class='hint'>${llm.name || llm.model}</span></div>`,
    `<div><b>ComfyUI</b> ${s.comfy.running ? "rendering" : s.comfy.models ? `${s.comfy.models} model(s) in memory` : "idle, nothing loaded"}${s.comfy.pending ? ` · ${s.comfy.pending} waiting` : ""}</div>`,
    s.mode === "auto" ? "<div class='hint'>Auto: the assistant loads when you ask it something and is ejected when a render is queued.</div>"
      : "<div class='hint'>Manual: load and eject the assistant yourself. Clapper refuses to render while it is loaded.</div>",
  ].join("");
  document.querySelectorAll("#gpuMode button").forEach((b) => b.classList.toggle("on", b.dataset.v === s.mode));
  $("gpuLoad").disabled = !llm.reachable || llm.loaded || !!busy || s.comfy.running > 0;
  $("gpuEject").disabled = !llm.loaded || !!busy;
  $("gpuFree").disabled = !s.comfy.models || s.comfy.running > 0 || !!busy;
}
async function gpuDo(body, doing) {
  await guard(async () => {
    if (doing) { $("statusText").textContent = doing; $("statusPill").classList.add("busy"); }
    gpu.status = await call("/gpu", body); renderGpu();
  });
}
document.querySelectorAll("#gpuMode button").forEach((b) => (b.onclick = () => gpuDo({ mode: b.dataset.v })));
$("gpuLoad").onclick = () => gpuDo({ action: "load" }, "Loading assistant…");
$("gpuEject").onclick = () => gpuDo({ action: "eject" }, "Ejecting…");
$("gpuFree").onclick = () => gpuDo({ action: "free_comfy" }, "Freeing GPU…");

// ------------------------------------------------------------------ chat panel

const chat = { id: null, data: null, timer: null, stick: true, lastRun: false };

function chatReset() {
  clearTimeout(chat.timer); chat.id = null; chat.data = null;
  $("chatPanel").hidden = true; $("chatMsgs").replaceChildren(); document.body.classList.remove("chat-open");
}
function openChat() {
  if (!state.path) return;
  $("chatPanel").hidden = false; document.body.classList.add("chat-open");
  chatRefsChanged(); loadChats();
}
function closeChat() { $("chatPanel").hidden = true; document.body.classList.remove("chat-open"); clearTimeout(chat.timer); }
$("assistantBtn").onclick = () => ($("chatPanel").hidden ? openChat() : closeChat());
$("chatClose").onclick = closeChat;

async function loadChats(selectId) {
  const r = await guard(() => call(`/assistant/chats?path=${encodeURIComponent(state.path)}`));
  if (!r) return;
  const sel = $("chatSelect"); sel.replaceChildren();
  for (const c of r.chats) sel.append(h("option", { value: c.id }, `${c.title} · ${fmtWhen(c.updated)}`));
  const want = selectId || chat.id || r.chats[0]?.id;
  if (!want) return newChat();
  sel.value = want; await showChat(want);
}
async function newChat() {
  const d = await guard(() => call("/assistant/chats", { path: state.path, action: "new" }));
  if (d) { chat.id = d.id; await loadChats(d.id); $("chatInput").focus(); }
}
$("chatNew").onclick = newChat;
$("chatSelect").onchange = () => showChat($("chatSelect").value);
$("chatRename").onclick = () => guard(async () => {
  const title = prompt("Chat name", chat.data?.title || ""); if (title === null) return;
  await call("/assistant/chats", { path: state.path, action: "rename", id: chat.id, title }); loadChats(chat.id);
});
$("chatDelete").onclick = () => guard(async () => {
  if (!confirm("Delete this chat? Proposals you already applied stay in the project.")) return;
  await call("/assistant/chats", { path: state.path, action: "delete", id: chat.id }); chat.id = null; loadChats();
});
$("chatCompact").onclick = () => guard(async () => {
  showStatus("Summarising older messages…");
  const r = await call("/assistant/compact", { path: state.path, id: chat.id });
  toast(r.compacted ? "Older messages summarised" : "Nothing to summarise yet"); renderChat(r.chat);
});

async function showChat(id) {
  chat.id = id; chat.stick = true;
  const d = await guard(() => call(`/assistant/chat?path=${encodeURIComponent(state.path)}&id=${id}`));
  if (d) renderChat(d);
}

function showStatus(text) { const s = $("chatStatus"); s.hidden = !text; s.textContent = text || ""; }

const PHASES = { waiting: "Waiting for the previous answer to finish…", starting: "Starting…", loading: "Loading the assistant model (freeing the GPU first)…",
  thinking: "Reading…", writing: "Writing…", tool: "Working…", compacting: "Summarising older messages…" };
const TOOL_LABELS = { get_scene: "Read a scene", view_reference: "Looked at a reference", view_take: "Looked at a take", update_notes: "Updated the story notes",
  save_reference_card: "Saved a reference card" };

function renderChat(d) {
  chat.data = d;
  const box = $("chatMsgs");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.replaceChildren();
  $("chatProvider").textContent = d.provider.name + (d.provider.vision ? " · sees images" : "");
  const pct = d.context.limit ? Math.min(100, (100 * d.context.tokens) / d.context.limit) : 0;
  $("chatCtx").querySelector("i").style.width = `${pct}%`;
  $("chatCtx").querySelector("b").textContent = d.context.limit ? `${Math.round(d.context.tokens / 1000)}k / ${Math.round(d.context.limit / 1000)}k` : "";
  $("chatCtx").className = "ctx" + (pct > 100 * d.context.compact_at ? " warn" : "");
  const lr = d.context.last_request;
  $("chatCtx").title = "How full the model's context window is. At 60% older messages are summarised automatically."
    + (lr ? `\nLast request: ${lr.cache_n ?? 0} tokens from cache, ${lr.prompt_n} read${lr.predicted_per_second ? `, writing ${lr.predicted_per_second.toFixed(0)} tokens/s` : ""}` : "");

  const results = {}; for (const m of d.messages) if (m.role === "tool") results[m.tool_call_id] = m;
  if (!d.messages.length) box.append(h("div", { class: "chat-empty" },
    h("b", {}, "What are we making?"),
    h("p", {}, "Describe a video idea and I'll split it into scenes with prompts, references and lengths. Or ask me to rewrite a scene, check a take, or explain a setting."),
    h("p", { class: "hint" }, "I propose changes as cards; nothing changes until you press Apply.")));
  d.messages.forEach((m, i) => {
    if (i === d.summarized_upto && i > 0) box.append(h("div", { class: "chat-divider" }, "Messages above are summarised for the model (still kept here)"));
    if (m.role === "user") box.append(h("div", { class: "msg user" }, md(m.text), (m.images || []).length ? h("div", { class: "thumbs" }, m.images.map((f) => h("img", { src: media(f) }))) : null));
    else if (m.role === "assistant") {
      const el = h("div", { class: "msg bot" });
      if (m.reasoning) el.append(h("details", { class: "reasoning" }, h("summary", {}, "Reasoning"), h("pre", {}, m.reasoning)));
      if (m.text) el.append(md(m.text));
      for (const c of m.tool_calls || []) el.append(toolLine(c, results[c.id], d));
      if (el.childNodes.length) box.append(el);
    } else if (m.role === "attachment") {
      box.append(h("div", { class: "thumbs small" }, (m.images || []).map((f) => h("img", { src: media(f), title: m.text }))));
    } else if (m.role === "event") box.append(h("div", { class: "chat-event" }, m.text.replace(/^The user /, "You ")));
    else if (m.role === "notice") box.append(h("div", { class: "chat-event" + (m.kind === "error" ? " bad" : "") }, (m.kind === "error" ? "⚠ The answer failed: " : "ℹ ") + m.text));
  });

  const run = d.run;
  const running = !!(run && run.running);
  if (running && run.text) box.append(h("div", { class: "msg bot live" }, md(run.text)));
  if (running) showStatus((PHASES[run.phase] || "Working…") + (run.phase === "tool" && run.tool ? ` ${TOOL_LABELS[run.tool] || run.tool}` : "")
    + (run.reasoning ? ` (reasoning ${Math.round(run.reasoning / 4)} tokens)` : "") + ` · ${fmtTime((Date.now() / 1000) - run.started)}`);
  else showStatus(run && run.error ? `⚠ ${run.error}` : "");
  $("chatStatus").classList.toggle("error", !running && !!(run && run.error));
  const pending = Object.values(d.proposals).filter((p) => p.status === "pending");
  if (!running && pending.length > 1) box.append(h("div", { class: "row apply-all" },
    h("button", { class: "primary", ...busyAttr("applyAll"), onclick: () => applyAll(pending) }, `Apply all ${pending.length} proposals`)));
  $("chatSend").hidden = running; $("chatStop").hidden = !running;
  $("chatInput").disabled = false;
  if (nearBottom || chat.stick) { box.scrollTop = box.scrollHeight; chat.stick = false; }

  clearTimeout(chat.timer);
  if (running) chat.timer = setTimeout(pollChat, 700);
  if (chat.lastRun && !running) { refresh(false); pollGpu(); }
  chat.lastRun = running;
}

async function pollChat() {
  if (!chat.id || $("chatPanel").hidden) return;
  try { renderChat(await call(`/assistant/chat?path=${encodeURIComponent(state.path)}&id=${chat.id}`)); }
  catch (e) { chat.timer = setTimeout(pollChat, 2000); }
}

function toolLine(callInfo, result, d) {
  const prop = result && result.proposal ? d.proposals[result.proposal] : null;
  if (prop) return proposalCard(prop);
  let args = {}; try { args = JSON.parse(callInfo.arguments || "{}"); } catch (e) { /* shown as is */ }
  const what = callInfo.name === "view_reference" ? `Looked at @${String(args.name || "").replace(/^@/, "")}`
    : callInfo.name === "get_scene" ? `Read scene ${args.scene}`
    : callInfo.name === "view_take" ? `Looked at scene ${args.scene}${args.take ? ` Take ${args.take}` : ""}`
    : callInfo.name === "save_reference_card" ? `Saved a card for @${String(args.name || "").replace(/^@/, "")}`
    : TOOL_LABELS[callInfo.name] || callInfo.name;
  const err = result && result.text.startsWith("ERROR");
  return h("div", { class: "tool-line" + (err ? " err" : ""), title: result ? result.text.slice(0, 600) : "" },
    err ? "⚠ " : "· ", what, err ? h("span", { class: "hint" }, ` — ${result.text.replace(/^ERROR: /, "").slice(0, 160)}`) : null);
}

function proposalCard(p) {
  const cls = { pending: "info", applied: "ok", rejected: "muted" }[p.status] || "muted";
  const card = h("div", { class: `proposal ${p.status}` },
    h("div", { class: "p-head" }, h("b", {}, p.summary), h("span", { class: `badge ${cls}` }, p.status)));
  for (const dt of p.details || []) {
    const item = h("div", { class: "p-item" }, h("div", { class: "p-label" }, dt.label), dt.meta ? h("div", { class: "hint" }, dt.meta) : null);
    if (dt.before !== undefined && dt.before !== "" && dt.before !== dt.after) {
      const long = (dt.before + dt.after).length > 160;
      if (long) item.append(h("details", {}, h("summary", {}, "Before"), h("pre", { class: "before" }, dt.before)));
      else item.append(h("div", { class: "p-short" }, h("del", {}, dt.before), " → "));
    }
    item.append(h("pre", { class: "after" }, dt.after || "(empty)"));
    card.append(item);
  }
  if (p.error) card.append(h("div", { class: "hint bad" }, "⚠ " + p.error));
  if (p.status === "pending") card.append(h("div", { class: "row" },
    h("button", { class: "primary", ...busyAttr(`proposal:${p.id}`), onclick: () => decide(p, "apply") }, p.tool === "render_scenes" ? "Render" : "Apply"),
    h("button", { ...busyAttr(`proposal:${p.id}`), onclick: () => decide(p, "reject") }, "Reject")));
  return card;
}

async function decide(p, action) {
  await once(`proposal:${p.id}`, async () => {
    if (action === "apply" && p.tool === "render_scenes" && !confirm(`${p.summary}?\n\nThe GPU switches to video generation (the assistant is ejected).`)) return;
    const body = { path: state.path, id: chat.id, proposal: p.id, action };
    let r;
    try { r = await call("/assistant/proposal", body); } catch (e) {
      // the user edited what this card changes after it was made: applying it would undo that edit
      if (action !== "apply" || !/changed after this card was made/.test(e.message)) throw e;
      if (!confirm(`${e.message.replace(/^not applied: /, "").split(", and applying")[0]}.\n\nApply the card anyway and replace your change?`)) return;
      r = await call("/assistant/proposal", { ...body, force: true });
    }
    setProject(r.project); renderChat(r.chat);
    if (action === "apply") toast(p.tool === "render_scenes" ? "Queued" : "Applied");
    if (p.tool === "render_scenes") { pollLive(); pollGpu(); }
  });
}
async function applyAll(list) {
  await once("applyAll", async () => {
    for (const p of list.filter((x) => x.tool !== "render_scenes")) {
      const ok = await once(`proposal:${p.id}`, async () => { const r = await call("/assistant/proposal", { path: state.path, id: chat.id, proposal: p.id, action: "apply" }); setProject(r.project); renderChat(r.chat); return true; });
      if (!ok) break;
    }
  });
}

async function send(text, thinking, extra = {}) {
  await once("send", async () => {
    text = (text ?? $("chatInput").value).trim();
    if (!text || !chat.id) return;
    const d = await call("/assistant/send", { path: state.path, id: chat.id, text, thinking: thinking ?? $("chatThink").checked, ...extra });
    $("chatInput").value = ""; chat.stick = true; renderChat(d); pollChat(); loadChatsTitle();
  });
}
async function loadChatsTitle() {
  const opt = $("chatSelect").selectedOptions[0];
  if (opt && chat.data && /^New chat/.test(opt.textContent)) setTimeout(() => loadChats(chat.id), 1500);
}
$("chatSend").dataset.once = "send";
$("chatSend").onclick = () => send();
$("chatStop").onclick = () => guard(() => call("/assistant/stop", { path: state.path, id: chat.id }));
$("chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });

function chatRefsChanged() {
  if ($("chatPanel").hidden || !state.project) return;
  const items = state.project.library?.items || {};
  $("chatRefs").replaceChildren(...Object.entries(items).map(([name, item]) =>
    h("span", { class: "chip", title: (item.description || "") + " — click to insert", onclick: () => insertAtCursor2($("chatInput"), "@" + name) },
      "@" + name, h("span", { class: "k" }, item.kind))));
}
function insertAtCursor2(ta, text) {
  const s = ta.selectionStart ?? ta.value.length, e = ta.selectionEnd ?? s, before = ta.value.slice(0, s);
  const pad = before && !/\s$/.test(before) ? " " : "";
  ta.value = before + pad + text + " " + ta.value.slice(e); ta.focus();
  ta.selectionStart = ta.selectionEnd = s + pad.length + text.length + 1;
}

// Entry points used by app.js
async function askAssistant(text, sendNow, extra) {
  if ($("chatPanel").hidden) { openChat(); await new Promise((r) => setTimeout(r, 400)); }
  for (let i = 0; i < 20 && !chat.id; i++) await new Promise((r) => setTimeout(r, 150));
  if (sendNow) send(text, undefined, extra); else { $("chatInput").value = text; $("chatInput").focus(); }
}
function planWithAssistant() {
  askAssistant("I want to make a video about: \n\nLength: about 30 seconds. Format: vertical 9:16. Look: \nPlease plan the scenes.", false);
}
$("edAsk").onclick = () => {
  const scene = sceneById(state.sceneId); if (!scene) return;
  askAssistant(`Scene ${scenes().indexOf(scene) + 1}: `, false);
};

// Minimal markdown: paragraphs, lists, **bold**, `code`, ``` blocks.
function md(text) {
  const el = h("div", { class: "md" });
  const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  const parts = String(text || "").split(/```[a-z]*\n?/);
  parts.forEach((part, i) => {
    if (i % 2) { el.append(h("pre", {}, part.replace(/\n$/, ""))); return; }
    let list = null;
    for (const block of part.split(/\n{2,}/)) {
      for (const line of block.split("\n")) {
        const item = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
        if (item) { if (!list) { list = h("ul"); el.append(list); } list.append(h("li", { html: inline(item[1]) })); continue; }
        list = null;
        if (!line.trim()) continue;
        const head = line.match(/^#{1,4}\s+(.*)$/);
        el.append(h("p", { html: head ? `<b>${inline(head[1])}</b>` : inline(line) }));
      }
      list = null;
    }
  });
  return el;
}

// ------------------------------------------------------------------ reference cards (Library)

function refCard(name, item) {
  const ta = h("textarea", { rows: 4, placeholder: "What it looks like, for the assistant (not sent to H3). ✨ Describe lets the assistant look at the image." });
  ta.value = item.card || "";
  const status = h("span", { class: "hint" }, item.card ? (item.card_status === "confirmed" ? "checked by you" : "written by the assistant — check it") : "");
  const describe = h("button", { title: "The assistant looks at the image and writes a card; you check it before saving" }, "✨ Describe");
  describe.onclick = () => guard(async () => {
    describe.disabled = true; describe.textContent = "Looking…"; status.textContent = "the assistant is looking (may load the model first)…";
    try {
      const r = await call("/assistant/describe_ref", { path: state.path, name });
      ta.value = r.card; status.textContent = "new card — check it, then Save";
    } finally { describe.disabled = false; describe.textContent = "✨ Describe"; pollGpu(); }
  });
  const save = h("button", { onclick: () => guard(async () => {
    setProject(await call("/ref/update", { path: state.path, name, card: ta.value, card_status: "confirmed" }));
    status.textContent = "checked by you"; toast(`@${name} card saved`);
  }) }, "Save card");
  return h("div", { class: "card-box" }, h("label", {}, "Card (for the assistant) ", status, ta), h("div", { class: "row" }, describe, save));
}

// ------------------------------------------------------------------ assistant settings

let aiDraft = null;
$("aiSettingsBtn").onclick = $("mAiSettings").onclick = async () => {
  closePops();
  const s = await guard(() => call("/assistant/settings")); if (!s) return;
  aiDraft = s; renderAiSettings(); $("aiDlg").showModal();
};
$("aiAdd").onclick = () => {
  const id = "p" + Math.random().toString(16).slice(2, 8);
  aiDraft.providers[id] = { kind: "openai", name: "Online model", base_url: "https://api.openai.com/v1", model: "", api_key: "", context: 128000, vision: false, _new: true };
  renderAiSettings();
};
// Base URLs of common OpenAI-compatible services. Their model names are suggestions that providers retire over time:
// "Load list" shows what the provider offers now. Vision depends on the model ("Test connection" checks it).
const AI_PRESETS = [
  ["OpenAI", "https://api.openai.com/v1", "gpt-4.1-mini", 1047576, true],
  ["OpenRouter", "https://openrouter.ai/api/v1", "openai/gpt-4.1-mini", 1047576, true],
  ["Groq", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b", 131072, false],
  ["Mistral", "https://api.mistral.ai/v1", "mistral-medium-latest", 131072, true],
  ["DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat", 65536, false],
  ["Together", "https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo", 131072, false],
  ["Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", 1048576, true],
  ["Anthropic", "https://api.anthropic.com/v1", "claude-sonnet-5-5", 200000, true],
  ["LM Studio (another computer)", "http://192.168.1.10:1234/v1", "", 32768, false],
  ["Ollama (another computer)", "http://192.168.1.10:11434/v1", "", 32768, false],
];
function renderAiSettings() {
  const body = $("aiBody"); body.replaceChildren();
  body.append(h("p", { class: "hint" }, "The assistant works with the local model or with any OpenAI-compatible service (OpenAI, OpenRouter, Groq, LM Studio, Ollama, vLLM …). API keys stay on the computer running ComfyUI."));
  for (const [id, p] of Object.entries(aiDraft.providers)) {
    const f = (label, key, opts = {}) => {
      const input = h("input", { type: opts.type || "text", value: p[key] ?? "", placeholder: opts.placeholder || "" });
      input.oninput = () => (p[key] = opts.type === "number" ? Number(input.value) : input.value);
      return h("label", {}, label, input);
    };
    const vision = h("input", { type: "checkbox" }); vision.checked = !!p.vision; vision.onchange = () => (p.vision = vision.checked);
    const active = h("input", { type: "radio", name: "aiActive" }); active.checked = aiDraft.active === id; active.onchange = () => (aiDraft.active = id);
    const mode = p.thinking === "auto" ? "auto" : p.thinking ? "always" : "off";
    const think = h("select", {}, [["off", "Off — answer directly"], ["auto", "Auto — think when planning a video"], ["always", "Always"]]
      .map(([v, label]) => h("option", { value: v, selected: v === mode }, label)));
    think.onchange = () => (p.thinking = think.value === "off" ? false : think.value === "auto" ? "auto" : (p.thinking_budget || 2048));
    const preset = p.kind === "local" ? null : h("select", {}, h("option", { value: "" }, "Fill in from a preset…"), ...AI_PRESETS.map(([n], i) => h("option", { value: i }, n)));
    if (preset) preset.onchange = () => {
      const pr = AI_PRESETS[Number(preset.value)]; if (!pr) return;
      Object.assign(p, { name: pr[0], base_url: pr[1], model: p.model || pr[2], context: pr[3], vision: pr[4] });
      renderAiSettings();
    };
    const testOut = h("span", { class: "hint test-out" });
    // the provider's current models (a typed name may be retired or misspelled)
    const modelList = h("select", { hidden: true });
    const pickModel = h("label", {}, "Pick a model", h("div", { class: "row" },
      h("button", { type: "button", onclick: () => guard(async () => {
        const { api_key_set, api_key_hint, _new, ...rest } = p;
        const r = await call("/assistant/models", { id, provider: rest });
        if (!r.models.length) { toast("This provider does not list its models — type the name"); return; }
        modelList.replaceChildren(h("option", { value: "" }, `${r.models.length} models…`), ...r.models.map((m) => h("option", { value: m, selected: m === p.model }, m)));
        modelList.hidden = false;
      }) }, "Load list"), modelList));
    modelList.onchange = () => { if (modelList.value) { p.model = modelList.value; renderAiSettings(); } };
    const effort = p.kind === "local" ? null : h("select", {}, [["", "Default"], ["low", "Low"], ["medium", "Medium"], ["high", "High"]]
      .map(([v, l]) => h("option", { value: v, selected: (p.reasoning_effort || "") === v }, l)));
    if (effort) effort.onchange = () => (p.reasoning_effort = effort.value || null);
    const test = h("button", { onclick: () => guard(async () => {
      test.disabled = true; testOut.className = "hint test-out"; testOut.textContent = p.kind === "local" ? "Testing (may load the model first)…" : "Testing…";
      try {
        const { api_key_set, api_key_hint, _new, ...rest } = p;
        const r = await call("/assistant/test", { id, provider: rest });
        const c = r.checks || {}, bad = Object.entries(c).filter(([, v]) => !v.ok);
        const NAMES = { listed: "model list", tools: "tool calling (needed for proposals)", images: "pictures" };
        testOut.className = "hint test-out " + (bad.length ? "bad" : "ok");
        testOut.replaceChildren(
          h("div", {}, `✓ answered in ${r.seconds} s: “${r.reply}”` + (r.usage ? "" : " · sends no token counts (estimated instead)") + (r.reasoning ? " · shows its reasoning" : "")),
          ...["listed", "tools", "images"].filter((k) => c[k]).map((k) => h("div", {}, (c[k].ok ? "✓ " : "✗ ") + NAMES[k] + (c[k].detail ? `: ${c[k].detail}` : ""))));
        if (c.images && !c.images.ok && /cannot see pictures/.test(c.images.detail)) { p.vision = false; vision.checked = false; }
      } catch (e) { testOut.className = "hint test-out bad"; testOut.textContent = "✗ " + e.message; throw Object.assign(e, { silent: true }); }
      finally { test.disabled = false; }
    }) }, "Test connection");
    body.append(h("section", { class: "settings-section provider" },
      h("div", { class: "block-head" }, h("label", { class: "switch" }, active, h("b", {}, p.name || id), h("span", { class: "hint" }, p.kind === "local" ? " · this computer, shares the GPU" : " · online")),
        p.kind !== "local" ? h("button", { class: "ghost danger", onclick: () => { p.remove = true; renderAiSettings(); } }, p.remove ? "will be removed" : "Remove") : null),
      h("div", { class: "settings-grid" },
        f("Name", "name"), f("Model", "model", { placeholder: p.kind === "local" ? "llama-swap model name" : "e.g. gpt-4.1" }),
        p.kind !== "local" ? pickModel : null,
        f("Base URL (…/v1)", "base_url"),
        p.kind === "local" ? f("llama-swap URL", "swap_url") : f(`API key${p.api_key_hint ? ` (saved: ${p.api_key_hint})` : ""}`, "api_key", { type: "password", placeholder: p.api_key_set ? "leave empty to keep" : "" }),
        p.kind === "local" ? f("Start command (run when llama-swap is not running)", "start_command", { placeholder: "systemctl --user start llama-swap" }) : null,
        f("Context (tokens)", "context", { type: "number" }),
        p.kind !== "local" ? h("label", { title: "Some plans limit how many tokens one request (or one minute) may use, e.g. Groq's free tier: 8000 for gpt-oss-120b. Clapper learns it from the first refusal; then it sends the prompt guide in brief and waits between requests when needed." },
          "Request limit (tokens, optional)", (() => { const i = h("input", { type: "number", value: p.request_limit ?? "", placeholder: "learned automatically" }); i.oninput = () => (p.request_limit = i.value ? Number(i.value) : null); return i; })()) : null,
        p.kind === "local" ? h("label", {}, "Thinking", think) : null,
        p.kind === "local" ? f("Thinking budget (tokens; also used by “think harder”)", "thinking_budget", { type: "number" }) : null,
        h("label", { class: "switch" }, vision, "can see images"),
        effort ? h("label", { title: "For reasoning models (OpenAI o-series and gpt-5, gpt-oss, …): how hard they think when planning or writing scenes" }, "Reasoning effort", effort) : null,
        preset ? h("label", {}, "Preset", preset) : null),
      h("div", { class: "row" }, test, testOut)));
  }
  addPasswordEyes(body);
}
$("aiSave").onclick = () => guard(async () => {
  const providers = {};
  for (const [id, p] of Object.entries(aiDraft.providers)) {
    const { api_key_set, api_key_hint, _new, ...rest } = p;
    providers[id] = rest;
  }
  await call("/assistant/settings", { active: aiDraft.active, providers });
  $("aiDlg").close(); toast("Assistant settings saved");
  if (chat.id) showChat(chat.id);
});
