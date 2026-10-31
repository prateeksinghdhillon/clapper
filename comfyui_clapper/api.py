"""HTTP API + web page for Clapper, mounted on ComfyUI's own server under /clapper/.

Handlers that talk to ComfyUI's /prompt run in a worker thread: calling our own server
synchronously from its event loop would deadlock it.

Every /clapper path except the page, its static files and /clapper/api/auth/* needs a logged-in
profile (session cookie); a profile only sees the projects it owns (engine/profiles.py).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from aiohttp import web
from server import PromptServer

from . import gpu, live, sysstats
from .engine import director, estimates, finish as finish_engine, media as media_tools, profiles, registry, repeats, tools, upscale
from .engine.comfy_client import ComfyClient
from .engine.export import delete_export, export_check, export_video, list_exports
from .engine.project import ASSET_ROLES, Project, ProjectError, delete_project_folder, disk_usage, folder_name
from .engine.settings import DEFAULT_SETTINGS, SPEED_PROFILES, VIDEO_MODEL_MODES

WEB_ROOT = Path(__file__).parent / "web"
HOME = Path.home().resolve()
routes = PromptServer.instance.routes
_lock = asyncio.Lock()  # one writer at a time per process
_profiles = profiles.Store()
_recent = repeats.RepeatGuard()  # checked and recorded under _lock
COOKIE = "clapper_session"
PUBLIC = {"/clapper", "/clapper/", "/clapper/api/auth/me", "/clapper/api/auth/login",
          "/clapper/api/auth/logout", "/clapper/api/auth/setup"}
live.install()
gpu.install()


@web.middleware
async def _require_profile(request, handler):
    path = request.path
    if (path == "/clapper" or path.startswith("/clapper/")) and path not in PUBLIC and not path.startswith("/clapper/static/"):
        pid = _profiles.session(request.cookies.get(COOKIE))
        if not pid:
            return web.json_response({"error": "log in first", "login": True}, status=401)
        request["clapper_profile"] = pid
    return await handler(request)


PromptServer.instance.app.middlewares.append(_require_profile)


def _client() -> ComfyClient:
    args = __import__("comfy.cli_args", fromlist=["args"]).args
    return ComfyClient(f"http://127.0.0.1:{args.port}")


def _guarded(queue_fn):
    """Queue renders only if the GPU may be used for them (manual GPU mode with the assistant loaded refuses)."""
    def run(*args, **kwargs):
        try:
            gpu.GPU_MANAGER.check_render()
        except gpu.GPUError as exc:
            raise ProjectError(str(exc)) from None
        return queue_fn(*args, **kwargs)
    return run


# ------------------------------------------------------------------ registry

_registry = registry.known
_register = registry.register


def _mine(request) -> list[str]:
    """Known projects owned by the logged-in profile."""
    pid = request["clapper_profile"]
    return [p for p in _registry() if _profiles.owner(p) == pid]


def _safe_project_path(raw: str) -> Path:
    path = Path(os.path.expanduser(str(raw))).resolve()
    if HOME not in path.parents and path != HOME:
        raise ProjectError("projects must live inside your home folder")
    return path


def _open(raw: str, request) -> Project:
    path = _safe_project_path(raw)
    if str(path) not in _registry() or _profiles.owner(path) != request["clapper_profile"]:
        raise ProjectError("unknown project — open it first")
    return Project.open(path)


def _view(project: Project) -> dict:
    data = project.snapshot()
    data["path"] = str(project.root)
    data["badges"] = {s["id"]: project.scene_status(s["id"]) for s in project.scenes}
    data["effective_settings"] = director.project_settings(project)
    compiled = {}
    for scene in project.scenes:
        try:
            c = project.compile_prompt(scene)
            compiled[scene["id"]] = {"prompt": c["prompt"], "labels": c["labels"], "start": c["start"],
                                     "mode": project.scene_mode(scene), "error": None}
        except ProjectError as exc:
            compiled[scene["id"]] = {"prompt": None, "labels": {}, "start": None, "mode": None, "error": str(exc)}
    data["compiled"] = compiled
    data["timing"] = estimates.timing(project)
    data["asset_roles"] = list(ASSET_ROLES)
    data["video_model_modes"] = list(VIDEO_MODEL_MODES)
    data["video_aspect"] = director.video_aspect(project)
    # takes made outside a scene (adopted) have no scene_text: the text they were made from, from their prompt
    for tid, t in data["takes"].items():
        if t.get("scene_text") is None and t.get("status") == "done":
            key = (str(project.root), tid)
            if key not in _TAKE_TEXT:
                from .engine.assistant import take_text
                _TAKE_TEXT[key] = take_text(project, t)
            t["prompt_text"] = _TAKE_TEXT[key]
    return data


_TAKE_TEXT: dict = {}   # (project, take) → recovered text (a take's prompt never changes)


def _ok(payload: dict | list) -> web.Response:
    return web.json_response(payload, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def _err(exc: Exception, status: int = 400) -> web.Response:
    return web.json_response({"error": str(exc)}, status=status)


async def _body(request: web.Request) -> dict:
    try:
        return await request.json()
    except ValueError:
        return {}


# ------------------------------------------------------------------ page

@routes.get("/clapper")
async def page_redirect(request):
    raise web.HTTPFound("/clapper/")


@routes.get("/clapper/")
async def page(request):
    return web.FileResponse(WEB_ROOT / "index.html", headers={"Cache-Control": "no-cache"})


@routes.get("/clapper/static/{name}")
async def static(request):
    name = request.match_info["name"]
    target = (WEB_ROOT / name).resolve()
    if target.parent != WEB_ROOT.resolve() or not target.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(target, headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------ projects

# ------------------------------------------------------------------ profiles

def _set_session(resp: web.Response, token: str) -> web.Response:
    resp.set_cookie(COOKIE, token, max_age=profiles.SESSION_DAYS * 86400, httponly=True, samesite="Lax", path="/clapper")
    return resp


def _me(pid: str) -> dict:
    return {"profile": pid, "name": (_profiles.get(pid) or {}).get("name", pid)}


@routes.get("/clapper/api/auth/me")
async def auth_me(request):
    pid = _profiles.session(request.cookies.get(COOKIE))
    if pid:
        return _ok(_me(pid))
    return web.json_response({"error": "log in first", "login": True, "no_profiles": not _profiles.profiles()}, status=401)


@routes.post("/clapper/api/auth/login")
async def auth_login(request):
    """{profile, password}. 409 while another profile is logged in and active."""
    body = await _body(request)
    pid = str(body.get("profile", "")).strip().lower()
    loop = asyncio.get_running_loop()
    try:
        token = await loop.run_in_executor(None, lambda: _profiles.login(
            pid, str(body.get("password", "")), agent=request.headers.get("User-Agent", ""),
            replace=request.cookies.get(COOKIE)))
    except profiles.InUse as exc:
        return web.json_response({"error": str(exc), "in_use": True}, status=409)
    except profiles.ProfileError as exc:
        await asyncio.sleep(1)  # slow down password guessing
        return _err(exc, 401)
    return _set_session(_ok(_me(pid)), token)


@routes.post("/clapper/api/auth/setup")
async def auth_setup(request):
    """{profile, name, password}: create the first profile (only while none exist); it gets every known project."""
    body = await _body(request)
    pid = str(body.get("profile", "")).strip().lower()
    password = str(body.get("password", ""))
    loop = asyncio.get_running_loop()
    try:
        def create():
            _profiles.add(pid, password, name=str(body.get("name", "")), projects=_registry(), only_if_empty=True)
            return _profiles.login(pid, password, agent=request.headers.get("User-Agent", ""))
        token = await loop.run_in_executor(None, create)
    except profiles.ProfileError as exc:
        return _err(exc)
    return _set_session(_ok(_me(pid)), token)


@routes.post("/clapper/api/auth/password")
async def auth_password(request):
    """{current, new}: change the logged-in profile's password; its other devices are logged out."""
    body = await _body(request)
    loop = asyncio.get_running_loop()
    try:
        ended = await loop.run_in_executor(None, lambda: _profiles.change_password(
            request.cookies.get(COOKIE), str(body.get("current", "")), str(body.get("new", ""))))
    except profiles.ProfileError as exc:
        await asyncio.sleep(1)  # slow down password guessing
        return _err(exc)
    return _ok({"ok": True, "other_sessions_ended": ended})


@routes.post("/clapper/api/auth/logout")
async def auth_logout(request):
    """Ends the profile's session on every device, so another profile can log in."""
    _profiles.logout(request.cookies.get(COOKIE))
    resp = _ok({"ok": True})
    resp.del_cookie(COOKIE, path="/clapper")
    return resp


# ------------------------------------------------------------------ projects

@routes.get("/clapper/api/projects")
async def list_projects(request):
    out = []
    for raw in _mine(request):
        try:
            p = Project.open(raw)
            out.append({"path": raw, "name": p.data["name"], "scenes": len(p.scenes),
                        "takes": len(p.data["takes"]), "updated": p.data["updated"], "usage": disk_usage(raw)})
        except Exception as exc:  # noqa: BLE001 — show broken entries instead of hiding them
            out.append({"path": raw, "error": str(exc)})
    return _ok({"projects": out, "profiles": list(SPEED_PROFILES), "defaults": DEFAULT_SETTINGS})


def _tilde(path: str | Path) -> str:
    path = Path(path)
    try:
        return "~/" + str(path.resolve().relative_to(HOME)) if path.resolve() != HOME else "~"
    except ValueError:
        return str(path)


@routes.get("/clapper/api/locations")
async def locations(request):
    loc = registry.locations(_mine(request))
    return _ok({"default": loc["default"], "choices": [{"path": p, "label": _tilde(p), "exists": Path(p).is_dir()}
                                                      for p in loc["choices"]]})


@routes.post("/clapper/api/locations/default")
async def set_default_location(request):
    body = await _body(request)
    try:
        path = _safe_project_path(body["path"])
        registry.set_default_location(path)
        return _ok({"default": str(path)})
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.get("/clapper/api/folders")
async def folders(request):
    """Subfolders of a folder inside the home folder (hidden ones skipped), for the folder picker."""
    try:
        path = _safe_project_path(request.query.get("path") or str(HOME))
        if not path.is_dir():
            raise ProjectError("that folder does not exist")
        items = []
        for entry in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            is_project = os.path.isfile(os.path.join(entry.path, "project.json"))
            if is_project and _profiles.owner(entry.path) not in (None, request["clapper_profile"]):
                continue
            items.append({"name": entry.name, "path": entry.path, "is_project": is_project})
        parent = str(path.parent) if path != HOME else None
        return _ok({"path": str(path), "label": _tilde(path), "parent": parent, "is_project": (path / "project.json").is_file(),
                    "folders": items[:500]})
    except (ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/folders/new")
async def new_folder(request):
    body = await _body(request)
    try:
        name = str(body["name"]).strip()
        if not name or name.startswith(".") or "/" in name or name in ("..",):
            raise ProjectError("use a plain folder name")
        path = _safe_project_path(Path(body["parent"]) / name)
        path.mkdir(parents=False, exist_ok=True)
        return _ok({"path": str(path)})
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.get("/clapper/api/projects/target")
async def project_target(request):
    """Where a new project with this name would go in this location, and whether that folder is free."""
    try:
        parent = _safe_project_path(request.query["location"])
        path = parent / folder_name(request.query.get("name", ""))
        state = "project" if (path / "project.json").is_file() else "busy" if path.exists() and any(path.iterdir()) else "free"
        if state == "project" and _profiles.owner(path) not in (None, request["clapper_profile"]):
            state = "taken"
        return _ok({"path": str(path), "label": _tilde(path), "state": state})
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/projects")
async def create_or_open_project(request):
    """{path, name} or {location, name}: create a project (or open the one already there)."""
    body = await _body(request)
    try:
        if body.get("location"):
            path = _safe_project_path(Path(body["location"]) / folder_name(body.get("name", "")))
        else:
            path = _safe_project_path(body["path"])
        if path.exists() and not (path / "project.json").is_file() and any(path.iterdir()):
            raise ProjectError(f"{_tilde(path)} already exists and is not a Clapper project — choose another name or folder")
        pid = request["clapper_profile"]
        if _profiles.owner(path) not in (None, pid):
            raise ProjectError(f"{_tilde(path)} belongs to another profile — choose another name or folder")
        async with _lock:
            project = Project.open(path) if (path / "project.json").exists() else Project.create(path, body.get("name") or path.name)
            _register(path)
            _profiles.assign(path, pid)
            registry.remember_location(path.parent)
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.get("/clapper/api/project")
async def get_project(request):
    try:
        return _ok(_view(_open(request.query["path"], request)))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.get("/clapper/api/project/usage")
async def project_usage(request):
    try:
        project = _open(request.query["path"], request)
        loop = asyncio.get_running_loop()
        return _ok(await loop.run_in_executor(None, lambda: disk_usage(project.root)))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/project/delete")
async def project_delete(request):
    """{path, confirm_name, keep_files}: remove from the list, and unless keep_files, delete the folder for good."""
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            if body.get("confirm_name", "").strip() != project.data["name"].strip():
                raise ProjectError("the typed name does not match the project name")
            busy = [t for t in project.data["takes"].values() if t["status"] in ("queued", "rendering")]
            if busy:
                raise ProjectError(f"{len(busy)} take(s) of this project are rendering or waiting — stop them first")
            jobs = [j for j in (project.data.get("jobs") or {}).values() if j["status"] in ("queued", "running")]
            if jobs:   # images, speech or upscales would write into the deleted folder
                raise ProjectError(f"{len(jobs)} image, speech or upscale job(s) of this project are running or waiting — cancel them first")
            freed = disk_usage(project.root)["total"]
            if not body.get("keep_files"):
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda: delete_project_folder(project.root))
            registry.unregister(project.root)
            _profiles.assign(project.root, None)
        return _ok({"deleted_files": not body.get("keep_files"), "freed": 0 if body.get("keep_files") else freed})
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/project/update")
async def update_project(request):
    """Project-level fields: name, style, start_image, settings (overrides), defaults (reference names)."""
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            for key in ("name", "style", "start_image", "settings", "notes"):
                if key in body:
                    project.data[key] = body[key]
            if "defaults" in body:
                project.data["library"]["defaults"] = list(body["defaults"])
            if "settings_changes" in body:   # only what the user changed, on top of the project's current settings
                from .engine.settings import merge
                project.data["settings"] = merge(project.data.get("settings") or {}, body["settings_changes"] or {})
            if body.get("default_toggle"):   # one name on or off: the rest of the list as it is now, not as the page last saw it
                name, defaults = body["default_toggle"], project.data["library"]["defaults"]
                if body.get("on") and name not in defaults:
                    defaults.append(name)
                elif not body.get("on") and name in defaults:
                    defaults.remove(name)
            project.save()
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/ref")
async def upload_ref(request):
    """multipart: path, name, kind, description, default ("1"), start ("1"), file."""
    try:
        reader = await request.multipart()
        fields, tmp = {}, None
        async for part in reader:
            if part.name == "file":
                suffix = Path(part.filename or "upload.bin").suffix.lower()
                tmp = Path(os.path.expanduser("~/.clapper/upload" + suffix))
                tmp.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "wb") as fh:
                    while chunk := await part.read_chunk():
                        fh.write(chunk)
            else:
                fields[part.name] = await part.text()
        if tmp is None:
            raise ProjectError("no file uploaded")
        async with _lock:
            project = _open(fields["path"], request)
            project.add_reference(fields["name"], tmp, fields.get("kind", "image"),
                                  description=fields.get("description", ""), default=fields.get("default") == "1")
            if fields.get("start") == "1":
                project.data["start_image"] = fields["name"]
                project.save()
        tmp.unlink(missing_ok=True)
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError, ValueError) as exc:
        return _err(exc)


@routes.post("/clapper/api/ref/update")
async def update_ref(request):
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            project.update_reference(body["name"], **{k: body[k] for k in ("description", "definition", "card", "card_status") if k in body})
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


# ------------------------------------------------------------------ library (assets)

async def _receive_files(request) -> tuple[dict, list[tuple[Path, str]]]:
    """multipart form -> (fields, [(temp file, original name)])."""
    reader = await request.multipart()
    fields, files = {}, []
    tmp_dir = Path(os.path.expanduser("~/.clapper/uploads"))
    tmp_dir.mkdir(parents=True, exist_ok=True)
    async for part in reader:
        if part.name == "file":
            original = Path(part.filename or "upload.bin").name
            tmp = tmp_dir / f"{os.getpid()}-{len(files)}-{os.urandom(4).hex()}{Path(original).suffix.lower()}"
            with open(tmp, "wb") as fh:
                while chunk := await part.read_chunk():
                    fh.write(chunk)
            files.append((tmp, original))
        else:
            fields[part.name] = await part.text()
    return fields, files


@routes.post("/clapper/api/asset")
async def upload_asset(request):
    """multipart: path, role, title (one file only), one or more `file` parts. Images, audio (mp3, wav, m4a, ogg,
    flac, …) and video (mp4, mov, webm)."""
    files = []
    try:
        fields, files = await _receive_files(request)
        if not files:
            raise ProjectError("no file uploaded")
        loop = asyncio.get_running_loop()
        async with _lock:
            project = _open(fields["path"], request)
            added = []
            for tmp, original in files:
                title = fields.get("title") if len(files) == 1 and fields.get("title") else Path(original).stem
                asset = await loop.run_in_executor(None, lambda: media_tools.add_with_info(
                    project, tmp, role=fields.get("role") or "other", title=title,
                    origin={"type": "upload", "source": original}))
                added.append(asset["id"])
        return _ok({"project": _view(project), "added": added})
    except (KeyError, ProjectError, OSError, ValueError) as exc:
        return _err(exc)
    finally:
        for tmp, _ in files:
            tmp.unlink(missing_ok=True)


@routes.post("/clapper/api/asset/update")
async def update_asset(request):
    """id + any of title, role, tags, notes."""
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            project.update_asset(body["id"], **{k: body[k] for k in ("title", "role", "tags", "notes", "transcript", "look") if k in body})
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/asset/name")
async def name_asset(request):
    """id, name (@name), definition, default (bool), start (bool: make it the project's start image)."""
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            project.name_asset(body["id"], body["name"].strip().lstrip("@"), definition=body.get("definition", ""),
                               description=body.get("description", ""), default=bool(body.get("default")))
            if body.get("start"):
                project.data["start_image"] = body["name"].strip().lstrip("@")
                project.save()
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/asset/remove")
async def remove_asset(request):
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            project.remove_asset(body["id"])
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/ref/remove")
async def remove_ref(request):
    """Remove an @name; its library item stays."""
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            project.remove_reference(body["name"], force=bool(body.get("force")))
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/asset/frame")
async def grab_frame(request):
    """take_id, seconds: save that frame of a finished take to the library."""
    body = await _body(request)
    try:
        loop = asyncio.get_running_loop()
        async with _lock:
            project = _open(body["path"], request)
            asset = await loop.run_in_executor(None, lambda: media_tools.grab_frame(
                project, body["take_id"], float(body.get("seconds", 0)), title=body.get("title", "")))
        return _ok({"project": _view(project), "asset": asset})
    except (KeyError, ProjectError, OSError, ValueError) as exc:
        return _err(exc)


# ------------------------------------------------------------------ tools (image, speech, user workflows)

@routes.get("/clapper/api/tools")
async def list_tools(request):
    """The tools, their forms, and whether each can run on this ComfyUI (nodes and model files present)."""
    try:
        loop = asyncio.get_running_loop()
        found, problems = await loop.run_in_executor(None, tools.all_tools)
        avail = await loop.run_in_executor(None, lambda: tools.availability(found, _client().object_info))
        return _ok({"tools": [{**t.describe(), **avail[t.id]} for t in found], "problems": problems})
    except (OSError, ProjectError) as exc:
        return _err(exc)


@routes.post("/clapper/api/tool/run")
async def run_tool(request):
    """path, tool, inputs {…}, count (1–8), role, seed, title → one job per result."""
    body = await _body(request)
    try:
        loop = asyncio.get_running_loop()
        async with _lock:
            project = _open(body["path"], request)
            jobs = await loop.run_in_executor(None, lambda: _guarded(tools.queue_tool)(
                project, _client(), body["tool"], body.get("inputs") or {}, count=int(body.get("count") or 1),
                role=body.get("role"), seed=body.get("seed"), title=body.get("title", ""),
                version_of=body.get("version_of") or None))
        return _ok({"jobs": [j["id"] for j in jobs], "project": _view(project)})
    except (KeyError, ProjectError, OSError, ValueError, RuntimeError) as exc:
        return _err(exc)


@routes.post("/clapper/api/upscale")
async def upscale_takes(request):
    """path, take_ids [..] or selected: true (every scene's selected take without that upscale yet),
    method sharpen|redetail, size 720p|1080p, strength light|strong."""
    body = await _body(request)
    try:
        loop = asyncio.get_running_loop()
        async with _lock:
            project = _open(body["path"], request)
            method, size, strength = body.get("method", "sharpen"), body.get("size", "1080p"), body.get("strength", "light")
            if body.get("selected"):
                label = method if method == "sharpen" else f"redetail-{strength}"
                ids = []
                for scene in project.scenes:
                    take = project.data["takes"].get(scene.get("selected_take") or "")
                    if take and take.get("status") == "done" and f"{label}-{size}" not in (take.get("upscales") or {}):
                        ids.append(take["id"])
            else:
                ids = list(body["take_ids"])
            if not ids:
                raise ProjectError("nothing to upscale: every selected take already has this version")
            jobs = []
            for tid in ids:
                jobs.append(await loop.run_in_executor(None, lambda tid=tid: _guarded(upscale.queue_upscale)(
                    project, _client(), tid, method=method, size=size, strength=strength, seed=body.get("seed"))))
        return _ok({"jobs": [j["id"] for j in jobs], "project": _view(project)})
    except (KeyError, ProjectError, OSError, ValueError, RuntimeError) as exc:
        return _err(exc)


@routes.post("/clapper/api/upscale/use")
async def upscale_use(request):
    """path, take_id, version (a key of take.upscales, or null = the original): which one export uses."""
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            take = project.data["takes"][body["take_id"]]
            version = body.get("version")
            if version is not None and version not in (take.get("upscales") or {}):
                raise ProjectError("unknown upscaled version")
            take["upscaled"] = version
            take["upscale_original"] = version is None   # chosen on purpose: a later upscale does not replace it
            project.save()
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/upscale/delete")
async def upscale_delete(request):
    """path, take_id, version: delete that upscaled file (the take itself stays)."""
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            upscale.delete_version(project, body["take_id"], body["version"])
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/tool/cancel")
async def cancel_tool(request):
    """path, job_id (or all: true): stop running tool jobs, take waiting ones out of the queue."""
    import nodes as comfy_nodes

    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            jobs = project.data.get("jobs") or {}
            active = [j for j in jobs.values() if j["status"] in ("queued", "running")]
            targets = active if body.get("all") else [j for j in active if j["id"] == body.get("job_id")]
            if not targets:
                raise ProjectError("that job is not running or waiting")
            queue = PromptServer.instance.prompt_queue
            running, _pending = queue.get_current_queue()
            running_ids = {item[1] for item in running}
            for job in targets:
                pid = job.get("prompt_id")
                if pid in running_ids:
                    comfy_nodes.interrupt_processing()
                elif pid:
                    queue.delete_queue_item(lambda item, pid=pid: item[1] == pid)
                job.update(status="cancelled", error="cancelled by you")
            project.save()
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/tool/dismiss")
async def dismiss_tool(request):
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            if body.get("finished"):
                for jid in [j["id"] for j in (project.data.get("jobs") or {}).values() if j["status"] not in ("queued", "running")]:
                    try:
                        tools.dismiss(project, jid)
                    except ProjectError:   # results on disk not collected yet: kept
                        pass
            else:
                tools.dismiss(project, body["job_id"])
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


# ------------------------------------------------------------------ cast & world (entities)

@routes.post("/clapper/api/entity")
async def entity_action(request):
    """action: create (kind, name, description, from_asset) | update (id, name, description, voice, default) |
    main (id, asset) | add (asset, id or null, look) | remove (id)."""
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            action = body.get("action")
            if action == "create":
                e = project.create_entity(body["kind"], body.get("name", ""), description=body.get("description", ""),
                                          from_asset=body.get("from_asset") or None)
                return _ok({"entity": e["id"], "project": _view(project)})
            if action == "update":
                project.update_entity(body["id"], **{k: body[k] for k in ("name", "description", "voice", "default") if k in body})
            elif action == "main":
                project.set_entity_main(body["id"], body["asset"])
            elif action == "add":
                project.add_to_entity(body["asset"], body.get("id"), look=body.get("look", ""))
            elif action == "remove":
                note = project.remove_entity(body["id"], force=bool(body.get("force")))
                return _ok({**_view(project), "note": note})
            else:
                raise ProjectError(f"unknown entity action {action!r}")
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/entity/generate")
async def entity_generate(request):
    """id, action first|look|angle|place, text, place (for place), count, seed, aspect, prompt (optional override)."""
    body = await _body(request)
    try:
        loop = asyncio.get_running_loop()
        async with _lock:
            project = _open(body["path"], request)
            jobs = await loop.run_in_executor(None, lambda: _guarded(tools.queue_entity)(
                project, _client(), body["id"], body["action"], body.get("text", ""), place_id=body.get("place"),
                count=int(body.get("count") or 2), seed=body.get("seed"), aspect=body.get("aspect"),
                prompt=body.get("prompt") or None))
        return _ok({"jobs": [j["id"] for j in jobs], "project": _view(project)})
    except (KeyError, ProjectError, OSError, ValueError, RuntimeError) as exc:
        return _err(exc)


@routes.post("/clapper/api/entity/preview")
async def entity_preview(request):
    """The prompt an entity action would use (to show and edit before generating)."""
    body = await _body(request)
    try:
        project = _open(body["path"], request)
        inputs, _ = tools.entity_inputs(project, body["id"], body["action"], body.get("text", "") or "…",
                                        place_id=body.get("place"), aspect=body.get("aspect"))
        return _ok({"prompt": inputs["prompt"], "images": inputs["images"], "aspect": inputs["aspect"]})
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


# ------------------------------------------------------------------ scenes

@routes.post("/clapper/api/scene")
async def scene_action(request):
    """action: add (text, seconds, join, refs, title, after, start_image, audio_clips) | update (id, fields…) | move (id, index) | remove (id)."""
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            action = body.get("action")
            if action == "add":
                project.add_scene(text=body.get("text", ""), seconds=body.get("seconds"), join=body.get("join", "continue"),
                                  refs=body.get("refs"), title=body.get("title", ""), after=body.get("after"),
                                  start_image=body.get("start_image"), audio_clips=body.get("audio_clips"))
            elif action == "update":
                fields = {k: body[k] for k in ("title", "text", "seconds", "join", "refs", "start_image", "audio_clips") if k in body}
                project.update_scene(body["id"], **fields)
            elif action == "move":
                project.move_scene(body["id"], int(body["index"]))
            elif action == "remove":
                project.remove_scene(body["id"])
            else:
                raise ProjectError(f"unknown scene action {action!r}")
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError, StopIteration) as exc:
        return _err(exc)


# ------------------------------------------------------------------ takes

@routes.post("/clapper/api/render")
async def render(request):
    """Refuses the same scene with the same seed/overrides/parent take again within a few seconds (409)."""
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            key = repeats.render_key(project.root, body["scene_id"], seed=body.get("seed"),
                                     overrides=body.get("overrides"), parent_take=body.get("parent_take"))
            _recent.check(key, "a take of this scene was queued a moment ago — wait a few seconds to queue "
                               "another with the same settings, or give it a different seed")
            loop = asyncio.get_running_loop()
            take = await loop.run_in_executor(None, lambda: _guarded(director.queue_take)(
                project, body["scene_id"], _client(), seed=body.get("seed"), overrides=body.get("overrides"),
                parent_take_id=body.get("parent_take")))
            _recent.done(key)
        return _ok({"take": take, "project": _view(project)})
    except repeats.Repeated as exc:
        return _err(exc, 409)
    except (KeyError, ProjectError, OSError, RuntimeError) as exc:
        return _err(exc)


@routes.post("/clapper/api/render_rest")
async def render_rest(request):
    """Queue every remaining scene (first scene without a finished take → end), chained in order."""
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            loop = asyncio.get_running_loop()
            queued = await loop.run_in_executor(None, lambda: _guarded(director.queue_rest)(
                project, _client(), from_scene=body.get("from_scene"), overrides=body.get("overrides")))
        return _ok({"queued": [t["id"] for t in queued], "project": _view(project)})
    except (KeyError, ProjectError, OSError, RuntimeError) as exc:
        return _err(exc)


@routes.post("/clapper/api/requeue")
async def requeue(request):
    """Refuses re-queueing the same take again within a few seconds (409)."""
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            key = repeats.requeue_key(project.root, body["take_id"])
            _recent.check(key, "this take was re-queued a moment ago")
            loop = asyncio.get_running_loop()
            take = await loop.run_in_executor(None, lambda: _guarded(director.requeue)(project, body["take_id"], _client()))
            _recent.done(key)
        return _ok({"take": take, "project": _view(project)})
    except repeats.Repeated as exc:
        return _err(exc, 409)
    except (KeyError, ProjectError, OSError, RuntimeError) as exc:
        return _err(exc)


@routes.post("/clapper/api/select")
async def select(request):
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            project.select_take(body["scene_id"], body["take_id"])
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/take/note")
async def take_note(request):
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            fields = {k: body[k] for k in ("note", "rating") if k in body}
            project.update_take(body["take_id"], **fields)
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/refresh")
async def refresh(request):
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            loop = asyncio.get_running_loop()
            changed = await loop.run_in_executor(None, lambda: director.refresh(project, _client()))
            changed += await loop.run_in_executor(None, lambda: tools.collect(project, _client()))
            for take in project.data["takes"].values():  # remember how long finished takes took
                if take["status"] == "done" and not take.get("s_per_it") and take.get("prompt_id"):
                    stats = live.LIVE.result_for(take["prompt_id"])
                    if stats and stats.get("s_per_it"):
                        project.update_take(take["id"], render_seconds=take.get("render_seconds") or stats["seconds"],
                                            s_per_it=stats["s_per_it"], steps=stats["steps"])
            queue = await loop.run_in_executor(None, lambda: _client().queue_state())
        return _ok({"changed": changed, "queue": {"running": len(queue.get("queue_running", [])),
                                                  "pending": len(queue.get("queue_pending", []))},
                    "project": _view(project)})
    except (KeyError, ProjectError, OSError, RuntimeError) as exc:
        return _err(exc)


@routes.get("/clapper/api/export/check")
async def export_check_route(request):
    try:
        return _ok(export_check(_open(request.query["path"], request)))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/take/restore_prompt")
async def restore_prompt(request):
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            project.restore_take_prompt(body["take_id"])
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/export")
async def export(request):
    body = await _body(request)
    try:
        project = _open(body["path"], request)
        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(None, lambda: export_video(project, partial=bool(body.get("partial")),
                                                                   size=body.get("size") or "auto",
                                                                   use_upscaled=body.get("use_upscaled", True) is not False,
                                                                   use_finish=body.get("use_finish", True) is not False))
        manifest = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
        return _ok({"file": str(out.relative_to(project.root)), "manifest": manifest})
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/finish")
async def finish_set(request):
    """path, finish {scene_volume, tracks[], overlays[]}: music, sound and overlays applied at export."""
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            finish_engine.set_finish(project, body.get("finish") or {})
        return _ok(_view(project))
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/finish/title")
async def finish_title(request):
    """path, text, style (bold/regular/serif), color, box (#rrggbbaa or null), size → a title-card image in the library."""
    body = await _body(request)
    try:
        loop = asyncio.get_running_loop()
        async with _lock:
            project = _open(body["path"], request)
            asset = await loop.run_in_executor(None, lambda: finish_engine.title_card(
                project, body.get("text", ""), style=body.get("style", "bold"), color=body.get("color") or "#ffffff",
                box=body.get("box"), size=int(body.get("size") or 96)))
        return _ok({"asset": asset, "project": _view(project)})
    except (KeyError, ProjectError, OSError, ValueError) as exc:
        return _err(exc)


@routes.get("/clapper/api/exports")
async def exports_list(request):
    try:
        project = _open(request.query["path"], request)
        return _ok({"exports": list_exports(project)})
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.post("/clapper/api/exports/delete")
async def exports_delete(request):
    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            delete_export(project, body["file"])
        return _ok({"exports": list_exports(project)})
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.get("/clapper/api/system")
async def system(request):
    return _ok(sysstats.read())


def _descendants(project: Project, take_id: str) -> list[str]:
    """Queued/rendering takes that continue (directly or through a chain) from take_id."""
    out, frontier = [], {take_id}
    active = [t for t in project.data["takes"].values() if t["status"] in ("queued", "rendering")]
    changed = True
    while changed:
        changed = False
        for t in active:
            if t["id"] not in out and t.get("parent_take") in frontier:
                out.append(t["id"]); frontier.add(t["id"]); changed = True
    return out


@routes.post("/clapper/api/stop")
async def stop(request):
    """Stop takes: {take_id} stops that take and every queued take that continues from it; {all: true} stops
    every queued/rendering take of the project. Running jobs are interrupted, waiting ones leave the queue."""
    import nodes as comfy_nodes

    body = await _body(request)
    try:
        async with _lock:
            project = _open(body["path"], request)
            active = {t["id"]: t for t in project.data["takes"].values() if t["status"] in ("queued", "rendering")}
            if body.get("all"):
                targets = list(active)
            else:
                if body["take_id"] not in active:
                    raise ProjectError("that take is not rendering or waiting")
                targets = [body["take_id"], *_descendants(project, body["take_id"])]
            queue = PromptServer.instance.prompt_queue
            running, _pending = queue.get_current_queue()
            running_ids = {item[1] for item in running}
            for tid in targets:
                pid = active[tid].get("prompt_id")
                if pid in running_ids:
                    comfy_nodes.interrupt_processing()
                elif pid:
                    queue.delete_queue_item(lambda item, pid=pid: item[1] == pid)
                project.update_take(tid, status="interrupted", error="stopped by you")
        return _ok({"stopped": targets, "project": _view(project)})
    except (KeyError, ProjectError, OSError) as exc:
        return _err(exc)


@routes.get("/clapper/api/live")
async def live_state(request):
    """Current render: phase, step/total, seconds per step, elapsed, ETA; plus recent finished renders."""
    snap = live.LIVE.snapshot()
    try:
        running, pending = PromptServer.instance.prompt_queue.get_current_queue()
        snap["queue"] = {"running": len(running), "pending": len(pending)}
    except Exception:  # noqa: BLE001
        snap["queue"] = None
    return _ok(snap)


# ------------------------------------------------------------------ media

@routes.get("/clapper/thumb")
async def thumb(request):
    """A small cached JPEG of a project video's frame: ?path=…&file=…&t=seconds."""
    try:
        project = _open(request.query["path"], request)
        t = float(request.query.get("t", 0.5))
        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(None, lambda: media_tools.poster(project, request.query["file"], t))
        return web.FileResponse(out, headers={"Cache-Control": "max-age=86400"})
    except (KeyError, ProjectError, OSError, ValueError):
        raise web.HTTPNotFound() from None


@routes.get("/clapper/media")
async def media(request):
    try:
        project = _open(request.query["path"], request)
        target = (project.root / request.query["file"]).resolve()
        if project.root.resolve() not in target.parents or not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target)
    except (KeyError, ProjectError):
        raise web.HTTPNotFound() from None
