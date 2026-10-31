"""Turns project scenes into queued takes and collects the results. Pure Python (no torch)."""
from __future__ import annotations

import json
import random

from ..h3_temporal import scene_frame_count
from .comfy_client import ComfyClient
from .graph import build_take_graph
from .project import Project, ProjectError
from .settings import DEFAULT_SETTINGS, apply_video_model, fit_resolution, merge


def project_settings(project: Project, overrides: dict | None = None) -> dict:
    return merge(merge(DEFAULT_SETTINGS, project.data.get("settings") or {}), overrides or {})


def canvas_size(project: Project, settings: dict, start: str | None = None) -> tuple[int, int]:
    """Width × height of a fresh scene. Continued scenes keep their parent take's size."""
    res = settings["resolution"]
    aspect = res.get("aspect") or ("start_image" if res.get("mode", "start_image") == "start_image" else "custom")
    multiple = res.get("multiple", 32)
    if aspect == "custom":
        return int(res["width"]), int(res["height"])
    if aspect == "start_image":
        start = start or project.data.get("start_image")
        if not start:
            return int(res["width"]), int(res["height"])
        from PIL import Image

        with Image.open(project.ref_path(start)) as im:
            return fit_resolution(*im.size, res["megapixels"], multiple)
    w, _, h = aspect.partition(":")
    return fit_resolution(int(w) * 1000, int(h) * 1000, res["megapixels"], multiple)


def video_aspect(project: Project) -> str:
    """The project's video shape as an image-tool aspect ("9:16", "16:9", …), for start frames and places."""
    from .tools import ASPECTS as TOOL_ASPECTS

    w, h = canvas_size(project, project_settings(project))
    ratio = w / h
    return min(TOOL_ASPECTS, key=lambda k: abs(TOOL_ASPECTS[k][0] / TOOL_ASPECTS[k][1] - ratio))


def plan_take(project: Project, scene_id: str, *, seed: int | None = None, overrides: dict | None = None,
              parent_take_id: str | None = None, allow_pending_parent: bool = False) -> dict:
    """Everything needed to render one take of a scene, without queueing it."""
    scene = project.scene(scene_id)
    index = project.scene_index(scene_id)
    compiled = project.compile_prompt(scene)
    root = project.root.resolve()

    spec: dict = {
        "ref_images": [str(root / project.data["library"]["items"][n]["file"]) for n in compiled["images"]],
        "ref_audios": [str(root / project.data["library"]["items"][n]["file"]) for n in compiled["audios"]],
    }
    join = scene["join"] if index > 0 else "cut"
    parent = None
    if join in ("continue", "bridge"):
        parent = (project.data["takes"].get(parent_take_id) if parent_take_id
                  else project.previous_selected_take(scene_id))
        ok = {"done", "queued", "rendering"} if allow_pending_parent else {"done"}
        if parent is None or parent.get("status") not in ok:
            raise ProjectError("the previous scene has no finished selected take to continue from")
    # The project's video-model choice, then this take's own overrides on top.
    settings = merge(apply_video_model(project_settings(project), fresh=parent is None), overrides or {})
    start = compiled["start"]
    width, height = canvas_size(project, settings, start)
    spec["ref_image_size"] = settings["references"]["image_size"]
    if start:
        # Presented to the text encoder (it is <Picture 1>); pinned as frame 0 only when the scene does not
        # continue from a previous take: the first scene, or a scene with its own start image (Continuum does
        # the same for the project's start image).
        spec["start_image"] = {"path": str(project.ref_path(start).resolve()), "present": True,
                               "pin": parent is None and (index == 0 or bool(scene.get("start_image")))}
    context = 0
    if parent is not None:
        context = int(settings["continuity"]["frames"])
        spec["continue_from"] = {"take_dir": str(root / parent["dir"]), "method": settings["continuity"]["method"],
                                 "frames": context, "audio": bool(settings["continuity"]["audio"])}
        width, height = parent.get("width", width), parent.get("height", height)
    if join == "bridge":
        nxt = project.scenes[index + 1] if index + 1 < len(project.scenes) else None
        nxt_take = project.data["takes"].get((nxt or {}).get("selected_take") or "")
        if not nxt_take or nxt_take.get("status") != "done":
            raise ProjectError("bridge needs a finished selected take in the next scene")
        spec["bridge_to"] = {"take_dir": str(root / nxt_take["dir"]), "frames": int(settings["continuity"]["frames"]),
                             "audio": bool(settings["continuity"]["audio"])}
    seconds = scene.get("seconds") or settings["scene_seconds"]
    frames = scene_frame_count(seconds, context_frames=context)
    if compiled["clips"]:
        # Clip times are scene time (after any carried frames), so a continued scene's clip at 0.5 s starts
        # half a second after the join.
        spec["audio_guides"] = []
        for clip in compiled["clips"]:
            frame = context + int(round(clip["at"] * 24))
            if frame >= frames - 1:
                raise ProjectError(f"an audio clip starts at {clip['at']:g} s, after the end of the scene")
            spec["audio_guides"].append({"path": str(root / clip["file"]), "frame": frame})
    if join == "bridge":
        frames = scene_frame_count(seconds, context_frames=context + int(settings["continuity"]["frames"]))
    return {"scene": scene, "settings": settings, "compiled": compiled, "spec": spec, "width": width,
            "height": height, "frames": frames, "context": context, "parent": parent, "join": join,
            "seed": seed if seed is not None else random.randrange(1, 2**48)}


def queue_take(project: Project, scene_id: str, client: ComfyClient, **kwargs) -> dict:
    plan = plan_take(project, scene_id, **kwargs)
    scene = plan["scene"]
    take = project.new_take(
        scene_id, seed=plan["seed"], parent_take=(plan["parent"] or {}).get("id"),
        prompt_sha256=plan["compiled"]["sha256"], refs=project.scene_refs(scene), join=plan["join"],
        frames=plan["frames"], context_frames=plan["context"], width=plan["width"], height=plan["height"],
        seconds=scene.get("seconds") or plan["settings"]["scene_seconds"],
        scene_text=scene.get("text", ""), scene_refs=scene.get("refs"), scene_join=scene.get("join"),
        scene_seconds=scene.get("seconds"), scene_start_image=scene.get("start_image"),
        scene_audio_clips=scene.get("audio_clips"), model=plan["settings"].get("model_used"),
        pictures=plan["compiled"].get("pictures"),
        steps=plan["settings"]["sampling"]["steps"])
    take_dir = project.root.resolve() / take["dir"]
    meta = {"project": project.data["name"], "scene": scene_id, "take": take["id"], "prompt": plan["compiled"]["prompt"],
            "labels": plan["compiled"]["labels"], "spec": plan["spec"], "seed": plan["seed"],
            "settings": plan["settings"], "parent_take": take["parent_take"], "join": plan["join"],
            "context_frames": plan["context"]}
    graph = build_take_graph(settings=plan["settings"], prompt=plan["compiled"]["prompt"], width=plan["width"],
                             height=plan["height"], frames=plan["frames"], spec=plan["spec"], seed=plan["seed"],
                             take_dir=str(take_dir), meta=meta)
    try:
        prompt_id = client.submit(graph)
    except Exception as exc:
        project.update_take(take["id"], status="failed", error=str(exc)[:2000])
        raise
    return project.update_take(take["id"], prompt_id=prompt_id)


def queue_rest(project: Project, client: ComfyClient, *, from_scene: str | None = None, **kwargs) -> list[dict]:
    """Queue every scene from the first one without a finished selected take (or from `from_scene`) to the end.

    ComfyUI runs its queue in order, so each queued take can continue from the previous scene's
    still-pending take: its latent is on disk by the time the next job starts.
    """
    scenes = project.scenes
    if from_scene:
        start = project.scene_index(from_scene)
    else:
        start = next((i for i, s in enumerate(scenes)
                      if (project.data["takes"].get(s.get("selected_take") or "") or {}).get("status") != "done"), None)
        if start is None:
            return []
    if any(t["status"] in ("queued", "rendering") and project.scene_index(t["scene"]) >= start
           for t in project.data["takes"].values() if any(s["id"] == t["scene"] for s in scenes)):
        raise ProjectError("some of these scenes are already rendering — wait for them or re-queue later")
    queued, parent_id = [], None
    for scene in scenes[start:]:
        take = queue_take(project, scene["id"], client, parent_take_id=parent_id, allow_pending_parent=True, **kwargs)
        queued.append(take)
        parent_id = take["id"]
    return queued


def refresh(project: Project, client: ComfyClient) -> list[str]:
    """Reconcile queued/rendering takes with ComfyUI (also after a restart). Returns changed take ids."""
    changed = []
    queue = client.queue_state()
    running = {item[1] for item in queue.get("queue_running", [])}
    pending = {item[1] for item in queue.get("queue_pending", [])}
    for take in list(project.data["takes"].values()):
        if take["status"] not in ("queued", "rendering"):
            continue
        pid = take.get("prompt_id")
        meta_path = project.root / take["dir"] / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            extra = {}
            try:  # ComfyUI's history keeps start/finish timestamps (ms): record how long the take took
                entry = client.history(pid) if pid else None
                stamps = {m[0]: m[1].get("timestamp") for m in (entry or {}).get("status", {}).get("messages", [])}
                if stamps.get("execution_start") and stamps.get("execution_success"):
                    extra["render_seconds"] = round((stamps["execution_success"] - stamps["execution_start"]) / 1000, 1)
            except Exception:  # noqa: BLE001 — timing is nice-to-have
                pass
            project.update_take(take["id"], status="done", width=meta["width"], height=meta["height"],
                                frames=meta["frames"], finished=meta.get("saved_utc"), **extra)
            scene = next((s for s in project.scenes if s["id"] == take["scene"]), None)
            if scene is not None and not scene.get("selected_take"):
                project.select_take(scene["id"], take["id"])
            changed.append(take["id"])
            continue
        if pid in running:
            if take["status"] != "rendering":
                project.update_take(take["id"], status="rendering")
                changed.append(take["id"])
            continue
        if pid in pending:
            continue
        entry = client.history(pid) if pid else None
        if entry and entry.get("status", {}).get("status_str") == "error":
            msgs = [m[1] for m in entry["status"].get("messages", []) if m[0] == "execution_error"]
            error = (msgs[-1].get("exception_message") if msgs else "execution error")
            project.update_take(take["id"], status="failed", error=str(error)[:2000])
        else:  # not queued, not running, no result: lost to a restart/shutdown
            project.update_take(take["id"], status="interrupted")
        changed.append(take["id"])
    return changed


def requeue(project: Project, take_id: str, client: ComfyClient) -> dict:
    """Queue a new take with the same scene, seed and parent as an interrupted/failed one."""
    old = project.data["takes"][take_id]
    return queue_take(project, old["scene"], client, seed=old["seed"], parent_take_id=old.get("parent_take"))


__all__ = ["plan_take", "queue_take", "refresh", "requeue", "project_settings"]
