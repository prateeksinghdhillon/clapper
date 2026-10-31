"""Routes for the assistant (/clapper/api/assistant/*) and the GPU manager (/clapper/api/gpu).

A turn runs in a background thread (one at a time: there is one GPU); the page polls the chat for
progress. Before the local model is ejected its conversation cache is saved, and it is restored
when the same chat loads the model again, so the conversation is not re-read after every render.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from . import gpu
from .api import _body, _client, _err, _guarded, _lock, _ok, _open, _view, routes
from .engine import assistant, director, llm
from .engine.project import Project, ProjectError

log = logging.getLogger("clapper")
G = gpu.GPU_MANAGER
RUNS: dict[str, dict] = {}             # chat key -> live state of its current turn
_turn_lock = threading.Lock()          # one assistant turn at a time
_cache = {"saved": None, "active": None}  # which chat's conversation cache is on disk / last used the model
ERRORS = (llm.LLMError, gpu.GPUError, ProjectError, OSError, KeyError, ValueError)


def _key(project: Project, chat_id: str) -> str:
    return f"{project.root}::{chat_id}"


def _provider(settings: dict | None = None) -> dict:
    settings = settings or llm.load_settings()
    return settings["providers"].get(settings["active"]) or settings["providers"]["local"]


# ------------------------------------------------------------------ conversation cache across ejects

def _save_cache(runner: llm.LocalRunner) -> None:
    active = _cache["active"]
    if not active:
        return
    root, chat_id = active
    project = Project.open(root)
    chat = assistant.load_chat(project, chat_id)
    provider = llm.load_settings()["providers"]["local"]
    runner.prime(assistant.build_messages(project, chat, provider, with_snapshot=False), assistant.TOOLS)
    saved = runner.save(provider.get("slot_file") or "clapper-assistant.bin")
    _cache["saved"] = (root, chat_id, chat.get("summarized_upto", 0))
    log.info("Clapper: saved the assistant's conversation cache (%s tokens)", saved)


G.before_eject.append(_save_cache)


def _ensure_local(project: Project, chat: dict, state: dict | None = None) -> None:
    runner = G.runner()
    if not runner.loaded():
        if state is not None:
            state.update(phase="loading")
        G.ensure_llm()
        saved = _cache["saved"]
        if saved == (str(project.root), chat["id"], chat.get("summarized_upto", 0)):
            try:  # a mismatch is harmless: the server just re-reads the conversation
                runner.restore(llm.load_settings()["providers"]["local"].get("slot_file") or "clapper-assistant.bin")
            except llm.LLMError as exc:
                log.warning("Clapper: could not restore the assistant's cache: %s", exc)
    _cache["active"] = (str(project.root), chat["id"])


# ------------------------------------------------------------------ views

def _chat_view(project: Project, chat: dict) -> dict:
    key = _key(project, chat["id"])
    run = RUNS.get(key)
    settings = llm.load_settings()
    provider = _provider(settings)
    messages = [{k: v for k, v in m.items() if k != "snapshot"} for m in chat["messages"]]
    return {"id": chat["id"], "title": chat.get("title") or "New chat", "messages": messages,
            "proposals": chat.get("proposals", {}), "summary": chat.get("summary", ""),
            "summarized_upto": chat.get("summarized_upto", 0),
            "context": {"tokens": chat.get("context_tokens", 0), "limit": int(provider.get("context") or 0),
                        "compact_at": assistant.COMPACT_AT, "last_request": chat.get("last_request")},
            "provider": {"id": settings["active"], "name": provider.get("name") or provider.get("model"),
                         "kind": provider.get("kind"), "vision": bool(provider.get("vision"))},
            "run": {k: v for k, v in run.items() if k not in ("chat", "stop")} if run else None}


def _live_chat(project: Project, chat_id: str) -> dict:
    """The chat object a running turn is working on (so edits are not lost), or the one on disk."""
    run = RUNS.get(_key(project, chat_id))
    if run and run.get("running"):
        return run["chat"]
    return assistant.load_chat(project, chat_id)


def _writer(root, loop):
    def write(fn):
        async def locked():
            async with _lock:
                fn(Project.open(root))
        asyncio.run_coroutine_threadsafe(locked(), loop).result(120)
    return write


# ------------------------------------------------------------------ settings and GPU

@routes.get("/clapper/api/assistant/settings")
async def assistant_settings(request):
    return _ok(llm.public_settings(llm.load_settings()))


@routes.post("/clapper/api/assistant/settings")
async def assistant_settings_update(request):
    try:
        return _ok(llm.public_settings(llm.update_settings(await _body(request))))
    except llm.LLMError as exc:
        return _err(exc)


@routes.get("/clapper/api/gpu")
async def gpu_status(request):
    loop = asyncio.get_running_loop()
    return _ok(await loop.run_in_executor(None, G.status))


@routes.post("/clapper/api/gpu")
async def gpu_action(request):
    """{mode: auto|manual} and/or {action: load|eject|free_comfy}."""
    body = await _body(request)
    loop = asyncio.get_running_loop()
    try:
        if body.get("mode"):
            llm.update_settings({"gpu_mode": body["mode"]})
        action = body.get("action")
        if action == "load":
            await loop.run_in_executor(None, G.load_llm)
        elif action == "eject":
            await loop.run_in_executor(None, lambda: G.eject_llm("ejected from the GPU menu"))
        elif action == "free_comfy":
            await loop.run_in_executor(None, G.free_comfy)
        elif action:
            raise ProjectError(f"unknown action {action!r}")
        return _ok(await loop.run_in_executor(None, G.status))
    except ERRORS as exc:
        return _err(exc)


# ------------------------------------------------------------------ chats

@routes.get("/clapper/api/assistant/chats")
async def chats(request):
    try:
        project = _open(request.query["path"], request)
        return _ok({"chats": assistant.list_chats(project, request["clapper_profile"])})
    except ERRORS as exc:
        return _err(exc)


@routes.post("/clapper/api/assistant/chats")
async def chats_action(request):
    """{path, action: new | rename (id, title) | delete (id)}."""
    body = await _body(request)
    try:
        project = _open(body["path"], request)
        action = body.get("action", "new")
        if action == "new":
            chat = assistant.new_chat(project, request["clapper_profile"])
            return _ok(_chat_view(project, chat))
        if RUNS.get(_key(project, body["id"]), {}).get("running"):
            raise ProjectError("the assistant is answering in this chat — wait or stop it first")
        if action == "rename":
            chat = assistant.load_chat(project, body["id"])
            chat["title"] = str(body.get("title", ""))[:80]
            assistant.save_chat(project, chat)
            return _ok(_chat_view(project, chat))
        if action == "delete":
            assistant.delete_chat(project, body["id"])
            return _ok({"deleted": body["id"]})
        raise ProjectError(f"unknown action {action!r}")
    except ERRORS as exc:
        return _err(exc)


@routes.get("/clapper/api/assistant/chat")
async def chat_get(request):
    try:
        project = _open(request.query["path"], request)
        return _ok(_chat_view(project, _live_chat(project, request.query["id"])))
    except ERRORS as exc:
        return _err(exc)


@routes.post("/clapper/api/assistant/send")
async def chat_send(request):
    """{path, id, text, images: [reference names], thinking: bool}: start a turn; poll /assistant/chat."""
    body = await _body(request)
    try:
        project = _open(body["path"], request)
        chat = assistant.load_chat(project, body["id"])
        key = _key(project, chat["id"])
        if RUNS.get(key, {}).get("running"):
            raise ProjectError("the assistant is still answering in this chat")
        text = str(body.get("text", "")).strip()
        if not text:
            raise ProjectError("type a message")
        items = project.data["library"]["items"]
        images = [items[n]["file"] for n in body.get("images") or [] if items.get(n, {}).get("kind") == "image"]
        harder = bool(body.get("thinking"))
        review = project.data["takes"].get(body.get("take") or "")
        if body.get("take") and (not review or review.get("status") != "done"):
            raise ProjectError("that take is not finished")
    except ERRORS as exc:
        return _err(exc)

    provider = _provider()
    # "think harder" = the provider's thinking budget for this turn; otherwise its thinking mode (off / auto / budget)
    budget, mode = int(provider.get("thinking_budget") or 2048), provider.get("thinking", False)
    thinking = budget if harder or (mode and mode != "auto") else mode
    root, loop = project.root, asyncio.get_running_loop()
    state = {"running": True, "phase": "waiting", "text": "", "reasoning": 0, "tool": None, "error": None,
             "started": time.time(), "chat": chat, "stop": False}
    RUNS[key] = state

    def before_request():
        if provider.get("kind") == "local":
            _ensure_local(Project.open(root), chat, state)

    def worker():
        with _turn_lock:
            try:
                note = ""
                if review:  # the frames go with the message, so the model always sees what it reviews
                    p = Project.open(root)
                    images.extend(assistant.extract_frames(p, review))
                    index = next((i for i, s in enumerate(p.scenes) if s["id"] == review["scene"]), -1)
                    note = (f"[Attached: 4 frames, start to end, of scene {index + 1} Take {review.get('number')}. "
                            f"Compare them with the scene text it was rendered from; say first what already matches, "
                            f"then what differs. Propose an edit only if something important differs.\n"
                            f"Scene text:\n{review.get('scene_text', '')}]")
                agent = assistant.Assistant(provider, open=lambda: Project.open(root), write=_writer(root, loop),
                                            before_request=before_request, update=lambda s: state.update(s),
                                            stopped=lambda: state["stop"])
                agent.run_turn(chat, text, images=images, thinking=thinking, note=note)
            except ERRORS as exc:
                state["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001 — report, never crash the server
                log.exception("Clapper assistant turn failed")
                state["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                state.update(running=False, phase="done")
                try:
                    assistant.save_chat(Project.open(root), chat)
                except OSError:
                    pass

    threading.Thread(target=worker, name="clapper-assistant", daemon=True).start()
    return _ok(_chat_view(project, chat))


@routes.post("/clapper/api/assistant/stop")
async def chat_stop(request):
    body = await _body(request)
    try:
        project = _open(body["path"], request)
        run = RUNS.get(_key(project, body["id"]))
        if run and run.get("running"):
            run["stop"] = True
        return _ok({"stopping": bool(run and run.get("running"))})
    except ERRORS as exc:
        return _err(exc)


@routes.post("/clapper/api/assistant/compact")
async def chat_compact(request):
    body = await _body(request)
    loop = asyncio.get_running_loop()
    try:
        project = _open(body["path"], request)
        chat = assistant.load_chat(project, body["id"])
        if RUNS.get(_key(project, chat["id"]), {}).get("running"):
            raise ProjectError("the assistant is answering — wait for it first")
        provider = _provider()

        def work():
            with _turn_lock:
                agent = assistant.Assistant(provider, open=lambda: Project.open(project.root), write=None,
                                            before_request=lambda: provider.get("kind") == "local" and _ensure_local(project, chat))
                return agent.compact(project, chat)
        done = await loop.run_in_executor(None, work)
        return _ok({"compacted": done, "chat": _chat_view(project, chat)})
    except ERRORS as exc:
        return _err(exc)


# ------------------------------------------------------------------ proposals

def _render(project: Project, scene_ids):
    client = _client()
    if scene_ids is None:
        queued = _guarded(director.queue_rest)(project, client)
        return f"{len(queued)} take(s) queued"
    queued: dict[str, str] = {}
    for sid in sorted(scene_ids, key=project.scene_index):
        index = project.scene_index(sid)
        parent = queued.get(project.scenes[index - 1]["id"]) if index > 0 else None
        take = _guarded(director.queue_take)(project, sid, client, parent_take_id=parent, allow_pending_parent=bool(parent))
        queued[sid] = take["id"]
    return f"{len(queued)} take(s) queued"


@routes.post("/clapper/api/assistant/proposal")
async def proposal_action(request):
    """{path, id, proposal, action: apply | reject}."""
    body = await _body(request)
    loop = asyncio.get_running_loop()
    try:
        async with _lock:
            project = _open(body["path"], request)
            chat = _live_chat(project, body["id"])
            prop = chat.get("proposals", {}).get(body["proposal"])
            if prop is None:
                raise ProjectError("unknown proposal")
            if prop["status"] != "pending":
                raise ProjectError(f"this proposal was already {prop['status']}")
            if body.get("action") == "reject":
                prop["status"] = "rejected"
                event = f"The user rejected: {prop['summary']}."
            else:
                try:
                    if prop["tool"] == "render_scenes":
                        prop["result"] = await loop.run_in_executor(None, lambda: assistant.apply_proposal(project, prop, render=_render))
                    else:
                        prop["result"] = assistant.apply_proposal(project, prop)
                except (ProjectError, RuntimeError) as exc:
                    prop["error"] = str(exc)
                    raise
                prop["status"] = "applied"
                event = f"The user applied: {prop['summary']}."
            chat["messages"].append({"role": "event", "text": event, "proposal": prop["id"], "t": assistant._now()})
            assistant.save_chat(project, chat)
            project = Project.open(project.root)
        return _ok({"project": _view(project), "chat": _chat_view(project, chat)})
    except ERRORS as exc:
        return _err(exc)


# ------------------------------------------------------------------ reference cards

@routes.post("/clapper/api/assistant/describe_ref")
async def describe_ref(request):
    """{path, name}: the model looks at a reference image once; returns a card for the user to review."""
    body = await _body(request)
    loop = asyncio.get_running_loop()
    try:
        project = _open(body["path"], request)
        provider = _provider()

        def work():
            with _turn_lock:
                if provider.get("kind") == "local":
                    G.ensure_llm()
                return assistant.describe_reference(provider, project, str(body["name"]))
        return _ok({"card": await loop.run_in_executor(None, work)})
    except ERRORS as exc:
        return _err(exc)
