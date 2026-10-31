#!/usr/bin/env python3
"""Clapper command line.

  clapper.py take --prompt-file P [--start-image IMG] [--ref IMG ...] [--ref-audio WAV ...]
              [--continue-from TAKE_DIR] [--method masked|keyframe] [--seconds 5]
              [--profile continuum_current] [--steps N] [--seed N] --out TAKE_DIR
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comfyui_clapper.engine.comfy_client import ComfyClient  # noqa: E402
from comfyui_clapper.engine.graph import build_take_graph  # noqa: E402
from comfyui_clapper.engine.settings import DEFAULT_SETTINGS, SPEED_PROFILES, fit_resolution, merge  # noqa: E402
from comfyui_clapper.h3_temporal import scene_frame_count  # noqa: E402
from comfyui_clapper.engine import director, importer, media, profiles, registry  # noqa: E402
from comfyui_clapper.engine.export import export_video  # noqa: E402
from comfyui_clapper.engine.project import ASSET_ROLES, Project, ProjectError  # noqa: E402
from comfyui_clapper.engine.settings import VIDEO_MODEL_MODES  # noqa: E402


def cmd_take(args: argparse.Namespace) -> int:
    settings = merge(DEFAULT_SETTINGS, SPEED_PROFILES[args.profile])
    if args.steps:
        settings = merge(settings, {"sampling": {"steps": args.steps}})
    if args.unet:
        settings = merge(settings, {"models": {"unet": args.unet}})
    if args.lora is not None:
        loras = []
        for item in args.lora:
            if item.lower() == "none":
                continue
            name, _, strength = item.partition(":")
            loras.append({"name": name, "strength": float(strength or 1.0), "enabled": True})
        settings["loras"] = loras
    prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()

    if args.start_image:
        from PIL import Image
        with Image.open(args.start_image) as im:
            width, height = fit_resolution(*im.size, settings["resolution"]["megapixels"])
    else:
        width, height = settings["resolution"]["width"], settings["resolution"]["height"]

    spec: dict = {"ref_images": [str(Path(p).resolve()) for p in args.ref],
                  "ref_audios": [str(Path(p).resolve()) for p in args.ref_audio],
                  "ref_image_size": settings["references"]["image_size"]}
    ctx = 0
    if args.start_image:
        spec["start_image"] = {"path": str(Path(args.start_image).resolve()),
                               "pin": not args.continue_from, "present": True}
    if args.continue_from:
        ctx = settings["continuity"]["frames"]
        spec["continue_from"] = {"take_dir": str(Path(args.continue_from).resolve()),
                                 "method": args.method or settings["continuity"]["method"],
                                 "frames": ctx, "audio": settings["continuity"]["audio"]}
        parent = json.loads((Path(args.continue_from) / "meta.json").read_text())
        width, height = parent["width"], parent["height"]
    guides = []
    for item in args.anchor_audio:
        path, _, at = item.rpartition("@") if "@" in item else (item, "", "0")
        guides.append({"path": str(Path(path).resolve()), "frame": int(round(float(at) * 24))})
    if guides:
        spec["audio_guides"] = guides
    frames = scene_frame_count(args.seconds, context_frames=ctx)
    seed = args.seed if args.seed is not None else random.randrange(1, 2**48)
    out = Path(args.out).resolve()
    meta = {"prompt": prompt, "spec": spec, "seed": seed, "settings": settings, "profile": args.profile,
            "context_frames": ctx, "requested_seconds": args.seconds}
    graph = build_take_graph(settings=settings, prompt=prompt, width=width, height=height, frames=frames,
                             spec=spec, seed=seed, take_dir=str(out), meta=meta)
    client = ComfyClient(args.url)
    print(f"take: {width}x{height}, {frames} frames ({frames / 24:.2f}s, context {ctx}), "
          f"steps {settings['sampling']['steps']}, seed {seed}")
    started = time.time()
    prompt_id = client.submit(graph)
    print(f"queued {prompt_id}")
    client.wait(prompt_id, on_poll=lambda t: print(f"  … {t:.0f}s", end="\r", flush=True))
    print(f"done in {time.time() - started:.0f}s -> {out}")
    return 0


def _open(args) -> Project:
    return Project.open(args.project)


def cmd_new(args) -> int:
    p = Project.create(args.project, args.name)
    registry.register(p.root)
    print(f"created {p.root}")
    return 0


def cmd_ref(args) -> int:
    p = _open(args)
    p.add_reference(args.name, args.file, args.kind, description=args.description or "", default=args.default)
    if args.start:
        p.data["start_image"] = args.name
        p.save()
    print(f"@{args.name} ({args.kind}) added" + (" as start image" if args.start else "") + (", default" if args.default else ""))
    return 0


def cmd_scene(args) -> int:
    p = _open(args)
    text = Path(args.text_file).read_text(encoding="utf-8").strip() if args.text_file else (args.text or "")
    refs = None if args.refs is None else [r.lstrip("@") for r in args.refs.split(",") if r.strip()]
    start = None if args.start_image is None else ("" if args.start_image.lower() == "none" else args.start_image.lstrip("@"))
    clips = None
    if args.audio_clip is not None:
        clips = []
        for item in args.audio_clip:
            if item.lower() == "none":
                continue
            aid, _, at = item.partition("@")
            clips.append({"asset": aid, "at": float(at or 0)})
    if args.update:
        fields = {k: v for k, v in (("text", text if (args.text_file or args.text) else None), ("seconds", args.seconds),
                                     ("join", args.join), ("title", args.title), ("start_image", start),
                                     ("audio_clips", clips)) if v is not None}
        if args.refs is not None:
            fields["refs"] = refs
        scene = p.update_scene(_scene_id(p, args.update), **fields)
    else:
        scene = p.add_scene(text=text, seconds=args.seconds, join=args.join or "continue", refs=refs,
                            title=args.title or "", after=_scene_id(p, args.after) if args.after else None,
                            start_image=start, audio_clips=clips)
    print(f"scene {p.scene_index(scene['id']) + 1} = {scene['id']}")
    return 0


def cmd_asset(args) -> int:
    p = _open(args)
    if args.action == "add":
        for f in args.files:
            a = media.add_with_info(p, f, role=args.role, title=args.title if len(args.files) == 1 and args.title else "")
            extra = f"{a['duration']:.2f} s" if a.get("duration") else (f"{a.get('width')}x{a.get('height')}" if a.get("width") else "")
            print(f"{a['id']}  {a['kind']:5s} {a['role']:9s} {a['title']}  {extra}")
    elif args.action == "list":
        for a in p.assets.values():
            names = " ".join("@" + n for n in p.asset_names(a["id"]))
            extra = f"{a['duration']:.2f} s" if a.get("duration") else ""
            print(f"{a['id']}  {a['kind']:5s} {a['role']:9s} {a['title']}  {extra}  {names}  [{a['origin'].get('type')}]")
    elif args.action == "name":
        if len(args.files) != 2:
            raise ProjectError("usage: asset PROJECT name ID NAME")
        aid, name = args.files[0], args.files[1].lstrip("@")
        p.name_asset(aid, name, definition=args.definition or "", default=args.default)
        print(f"{aid} is now @{name}")
    elif args.action == "remove":
        if len(args.files) != 1:
            raise ProjectError("usage: asset PROJECT remove ID")
        p.remove_asset(args.files[0])
        print(f"removed {args.files[0]} (file moved to trash/)")
    return 0


def cmd_frame(args) -> int:
    p = _open(args)
    a = media.grab_frame(p, args.take, args.seconds)
    print(f"{a['id']}  frame {a['width']}x{a['height']}  {a['title']}")
    return 0


def cmd_video_model(args) -> int:
    p = _open(args)
    settings = p.data.setdefault("settings", {})
    settings.setdefault("video_model", {})["mode"] = args.mode
    p.save()
    print(f"video model: {args.mode}")
    return 0


def _scene_id(p: Project, ref: str) -> str:
    return p.scenes[int(ref) - 1]["id"] if ref.isdigit() else ref


def cmd_render(args) -> int:
    p = _open(args)
    client = ComfyClient(args.url)
    overrides = json.loads(args.overrides) if args.overrides else None
    for ref in args.scenes:
        take = director.queue_take(p, _scene_id(p, ref), client, seed=args.seed, overrides=overrides)
        print(f"scene {ref}: queued take {take['id']} ({take['frames']} frames, seed {take['seed']})")
    if args.wait:
        pending = {t["id"] for t in p.data["takes"].values() if t["status"] in ("queued", "rendering")}
        started = time.time()
        while pending:
            time.sleep(5)
            director.refresh(p, client)
            pending = {t for t in pending if p.data["takes"][t]["status"] in ("queued", "rendering")}
            print(f"  … {time.time() - started:.0f}s, {len(pending)} pending", end="\r", flush=True)
        print()
        return cmd_status(args)
    return 0


def cmd_render_rest(args) -> int:
    p = _open(args)
    overrides = json.loads(args.overrides) if args.overrides else None
    queued = director.queue_rest(p, ComfyClient(args.url), from_scene=_scene_id(p, args.from_scene) if args.from_scene else None,
                                 overrides=overrides)
    print(f"queued {len(queued)} take(s): " + ", ".join(t["id"] for t in queued) if queued else "nothing to render")
    return 0


def cmd_refresh(args) -> int:
    p = _open(args)
    from comfyui_clapper.engine import tools

    client = ComfyClient(args.url)
    changed = director.refresh(p, client)
    jobs = tools.collect(p, client)
    print(f"{len(changed)} take(s) updated" + (f", {len(jobs)} job(s) updated" if jobs else ""))
    return cmd_status(args)


def cmd_select(args) -> int:
    p = _open(args)
    p.select_take(_scene_id(p, args.scene), args.take)
    return cmd_status(args)


def cmd_status(args) -> int:
    p = _open(args)
    print(f"{p.data['name']}  ({len(p.scenes)} scenes)")
    for i, s in enumerate(p.scenes, 1):
        takes = p.takes_for(s["id"])
        badges = ", ".join(p.scene_status(s["id"])) or "ok"
        sel = s.get("selected_take") or "-"
        print(f"  {i:>2}. {s['id']}  {s['join']:<8} takes={len(takes):<2} selected={sel:<10} [{badges}]  {s.get('title') or s['text'][:50]!r}")
        for t in takes:
            mark = "*" if t["id"] == s.get("selected_take") else " "
            print(f"        {mark} Take {t.get('number', '?'):<3} {t['id']}  {t['status']:<11} created {t.get('created', '')[:16].replace('T', ' ')}"
                  f" finished {(t.get('finished') or '-')[:16].replace('T', ' ')} seed={t.get('seed')} parent={t.get('parent_take') or '-'}"
                  + (f"  error={t.get('error')[:80]}" if t.get("error") else ""))
    return 0


def cmd_import_timeline(args) -> int:
    p = _open(args)
    pictures = dict((int(k), v.lstrip("@")) for k, v in (m.split("=", 1) for m in args.picture))
    audios = dict((int(k), v.lstrip("@")) for k, v in (m.split("=", 1) for m in args.audio))
    scenes = importer.import_timeline(p, Path(args.prompt_file).read_text(encoding="utf-8"), pictures, audio_names=audios)
    print(f"imported {len(scenes)} scenes; style text {len(p.data['style'])} chars")
    return cmd_status(args)


def cmd_adopt(args) -> int:
    p = _open(args)
    take = importer.adopt_take(p, _scene_id(p, args.scene), args.take_dir, parent_take=args.parent)
    print(f"adopted {args.take_dir} as {take['id']}")
    return cmd_status(args)


def cmd_upscale(args) -> int:
    from comfyui_clapper.engine import upscale

    p = _open(args)
    client = ComfyClient(args.url)
    ids = []
    for ref in args.scenes or [str(i + 1) for i in range(len(p.scenes))]:
        take_id = p.scene(_scene_id(p, ref)).get("selected_take")
        if take_id:
            ids.append(take_id)
    jobs = [upscale.queue_upscale(p, client, tid, method=args.method, size=args.size, strength=args.strength) for tid in ids]
    print(f"queued {len(jobs)} upscale(s): " + ", ".join(j["id"] for j in jobs) + " — run `refresh` when they are done")
    return 0


def cmd_export(args) -> int:
    out = export_video(_open(args), partial=args.partial, size=args.size, use_upscaled=not args.no_upscaled)
    print(f"exported {out}")
    return 0


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def cmd_usage(args) -> int:
    from comfyui_clapper.engine.project import disk_usage
    u = disk_usage(_open(args).root)
    print(f"{_size(u['total'])} in {u['files']} files: " + ", ".join(f"{k} {_size(v)}" for k, v in u["parts"].items() if v))
    return 0


def cmd_delete(args) -> int:
    from comfyui_clapper.engine.project import delete_project_folder, disk_usage
    p = _open(args)
    if args.confirm != p.data["name"]:
        print(f"refusing: --confirm must be the exact project name ({p.data['name']!r})")
        return 1
    if any(t["status"] in ("queued", "rendering") for t in p.data["takes"].values()):
        print("refusing: takes of this project are rendering or waiting — stop them first")
        return 1
    size = disk_usage(p.root)["total"]
    if not args.keep_files:
        delete_project_folder(p.root)
    registry.unregister(p.root)
    print("removed from the list" + ("" if args.keep_files else f" and deleted {p.root} ({_size(size)} freed)"))
    return 0


def _password(args) -> str:
    if args.password:
        return args.password
    import getpass
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Again: "):
        raise profiles.ProfileError("the passwords do not match")
    return first


def cmd_profile(args) -> int:
    store = profiles.Store()
    try:
        if args.action == "add":
            store.add(args.id, _password(args), name=args.name or "")
            print(f"profile {args.id} created")
        elif args.action == "password":
            ended = store.set_password(args.id, _password(args))
            print(f"password of {args.id} changed; {ended} logged-in session(s) of {args.id} ended")
        elif args.action == "remove":
            freed = store.remove(args.id)
            print(f"profile {args.id} removed; {len(freed)} project(s) now belong to no profile")
        elif args.action == "assign":
            for folder in args.projects:
                path = Path(folder).expanduser().resolve()
                Project.open(path)
                registry.register(path)
                store.assign(path, args.id)
                print(f"{path} -> {args.id}")
        elif args.action == "unassign":
            for folder in args.projects:
                store.assign(folder, None)
                print(f"{Path(folder).expanduser().resolve()} -> no profile")
        elif args.action == "logout":
            print(f"{store.release(args.id)} session(s) ended")
        else:  # list
            active = store.active()
            for pid, prof in store.profiles().items():
                state = ""
                if active and active["profile"] == pid:
                    state = (f"  [logged in on {active['devices']} device(s), last active {int(active['idle_seconds'])}s ago"
                             + (", holds Clapper]" if active["holds"] else ", idle]"))
                print(f"{pid:<16} {prof['name']:<20} {len(prof['projects'])} project(s){state}")
                for path in prof["projects"]:
                    print(f"    {path}")
            owned = {p for prof in store.profiles().values() for p in prof["projects"]}
            loose = [p for p in registry.known() if p not in owned]
            if loose:
                print("no profile:")
                for path in loose:
                    print(f"    {path}")
    except (profiles.ProfileError, OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="clapper")
    ap.add_argument("--url", default="http://127.0.0.1:8188")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("take", help="render one take")
    t.add_argument("--prompt-file", required=True)
    t.add_argument("--start-image")
    t.add_argument("--ref", action="append", default=[])
    t.add_argument("--ref-audio", action="append", default=[])
    t.add_argument("--continue-from")
    t.add_argument("--method", choices=["masked", "keyframe"])
    t.add_argument("--seconds", type=float, default=5.0)
    t.add_argument("--profile", default="continuum_current", choices=sorted(SPEED_PROFILES))
    t.add_argument("--steps", type=int)
    t.add_argument("--unet", help="diffusion model file, e.g. a Ref2VA model for a fresh scene")
    t.add_argument("--lora", action="append", help="NAME[:STRENGTH]; repeat for several; 'none' for no LoRA "
                   "(replaces the default LoRA list)")
    t.add_argument("--anchor-audio", action="append", default=[], metavar="FILE[@SECONDS]",
                   help="guide the scene's soundtrack to be this clip from SECONDS (default 0)")
    t.add_argument("--seed", type=int)
    t.add_argument("--out", required=True)

    def proj(name, **kw):
        sp = sub.add_parser(name, **kw)
        sp.add_argument("project", help="project folder")
        return sp

    s = proj("new", help="create a project")
    s.add_argument("--name", required=True)
    s = proj("ref", help="add a reference to the project library")
    s.add_argument("name")
    s.add_argument("file")
    s.add_argument("--kind", choices=["image", "audio"], default="image")
    s.add_argument("--description")
    s.add_argument("--default", action="store_true", help="use in every scene that has no own list")
    s.add_argument("--start", action="store_true", help="use as the start image (<Picture 1>)")
    s = proj("scene", help="add or update a scene")
    s.add_argument("--text")
    s.add_argument("--text-file")
    s.add_argument("--seconds", type=float)
    s.add_argument("--join", choices=["continue", "cut", "bridge"])
    s.add_argument("--refs", help="comma list of @names; empty string = none; omit = project defaults")
    s.add_argument("--title")
    s.add_argument("--after", help="insert after scene N / id")
    s.add_argument("--update", help="update scene N / id instead of adding")
    s.add_argument("--start-image", help="@name of an image reference to start this scene from, or 'none' to start "
                   "from the references only (use with --join cut)")
    s.add_argument("--audio-clip", action="append", metavar="ASSET_ID[@SECONDS]",
                   help="audio library item that becomes the scene's soundtrack from SECONDS (scene time); repeat; "
                   "'none' clears")
    s = proj("asset", help="library items: add files, list, give an @name, remove")
    s.add_argument("action", choices=["add", "list", "name", "remove"])
    s.add_argument("files", nargs="*", help="add: files; name/remove: the item id (name: then the @name)")
    s.add_argument("--role", choices=ASSET_ROLES, default="other")
    s.add_argument("--title")
    s.add_argument("--definition")
    s.add_argument("--default", action="store_true", help="name: use in every scene that has no own list")
    s = proj("frame", help="save a frame of a finished take to the library")
    s.add_argument("take", help="take id")
    s.add_argument("seconds", type=float)
    s = proj("video-model", help="which H3 model renders the scenes")
    s.add_argument("mode", choices=VIDEO_MODEL_MODES, help="fl2va (default) | ref2va | ref2va_fresh (Ref2VA for "
                   "scenes that start fresh, FL2VA for continued ones)")
    s = proj("render", help="queue a take for each given scene")
    s.add_argument("scenes", nargs="+")
    s.add_argument("--seed", type=int)
    s.add_argument("--overrides", help="JSON settings overrides, e.g. '{\"sampling\":{\"steps\":8}}'")
    s.add_argument("--wait", action="store_true")
    s = proj("render-rest", help="queue every remaining scene, chained in order")
    s.add_argument("--from-scene", help="start at scene N / id instead of the first scene without a take")
    s.add_argument("--overrides")
    proj("usage", help="disk space used by the project")
    s = proj("delete", help="delete the project folder for good (or --keep-files to only remove it from the list)")
    s.add_argument("--confirm", required=True, help="the exact project name")
    s.add_argument("--keep-files", action="store_true")
    proj("refresh", help="collect finished takes, detect interrupted ones")
    proj("status", help="show scenes, takes and badges")
    s = proj("select", help="select a take for a scene")
    s.add_argument("scene")
    s.add_argument("take")
    s = proj("import-timeline", help="turn a Continuum Timeline prompt into scenes")
    s.add_argument("prompt_file")
    s.add_argument("--picture", action="append", default=[], help="N=name, e.g. 1=opening")
    s.add_argument("--audio", action="append", default=[], help="N=name")
    s = proj("adopt", help="add an already-rendered take folder to a scene")
    s.add_argument("scene")
    s.add_argument("take_dir")
    s.add_argument("--parent", help="take id this take continued from")
    s = proj("export", help="stitch the selected takes into one video")
    s.add_argument("--partial", action="store_true", help="export the ready scenes even if later ones have no take")
    s.add_argument("--size", choices=["auto", "original", "720p", "1080p"], default="auto",
                   help="auto = the largest scene (upscaled takes set it); others are resized to match")
    s.add_argument("--no-upscaled", action="store_true", help="use the original takes even where an upscale exists")
    s = proj("upscale", help="upscale the selected takes of the given scenes (all scenes if none given)")
    s.add_argument("scenes", nargs="*")
    s.add_argument("--method", choices=["sharpen", "redetail"], default="sharpen")
    s.add_argument("--size", choices=["720p", "1080p"], default="1080p")
    s.add_argument("--strength", choices=["light", "strong"], default="light", help="redetail only")

    pr = sub.add_parser("profile", help="profiles: who may log in to the web interface and which projects they see")
    psub = pr.add_subparsers(dest="action", required=True)
    psub.add_parser("list", help="profiles, their projects, who is logged in")
    s = psub.add_parser("add", help="create a profile")
    s.add_argument("id", help="login id: lowercase letters, digits, - and _")
    s.add_argument("--name", help="display name")
    s.add_argument("--password", help="omit to be asked")
    s = psub.add_parser("password", help="change a profile's password")
    s.add_argument("id")
    s.add_argument("--password", help="omit to be asked")
    s = psub.add_parser("remove", help="delete a profile (its projects stay on disk, owned by nobody)")
    s.add_argument("id")
    s = psub.add_parser("assign", help="give project folders to a profile (moves them from any other profile)")
    s.add_argument("id")
    s.add_argument("projects", nargs="+")
    s = psub.add_parser("unassign", help="take project folders away from their profile")
    s.add_argument("projects", nargs="+")
    s = psub.add_parser("logout", help="end the sessions of a profile (or everyone), freeing Clapper")
    s.add_argument("id", nargs="?")

    args = ap.parse_args()
    return {"profile": cmd_profile, "import-timeline": cmd_import_timeline, "adopt": cmd_adopt, "export": cmd_export, "take": cmd_take, "new": cmd_new, "ref": cmd_ref, "scene": cmd_scene, "render": cmd_render,
            "refresh": cmd_refresh, "usage": cmd_usage, "delete": cmd_delete, "render-rest": cmd_render_rest, "status": cmd_status, "select": cmd_select,
            "asset": cmd_asset, "frame": cmd_frame, "video-model": cmd_video_model, "upscale": cmd_upscale}[args.cmd](args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
