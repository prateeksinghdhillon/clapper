"""Tools: ComfyUI workflows that make library items (images, speech, …). Pure Python (no torch).

A tool turns a form (prompt, library images, a voice sample, …) into one ComfyUI job per result. Each job
writes its files into `jobs/<job-id>/` through the Clapper save nodes; `collect` then moves them into the
project library with their origin (tool, inputs, seed), so every result can be traced and run again.

Built-in tools build their graph in Python. Anyone can add a tool without code: a folder in
`~/.clapper/tools/<id>/` with `workflow.json` (ComfyUI API format) and `tool.json` (see docs/REFERENCE.md,
"Adding your own tool").
"""
from __future__ import annotations

import copy
import json
import os
import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .comfy_client import ComfyClient
from .project import ASSET_ROLES, Project, ProjectError, _new_id, _now

USER_TOOLS_DIR = Path(os.path.expanduser("~/.clapper/tools"))
JOB_STATUSES = ("queued", "running", "done", "failed", "interrupted", "cancelled")
MAX_COUNT = 8

# FLUX.2 sizes near 1 megapixel, multiples of 16.
ASPECTS = {"1:1": (1024, 1024), "9:16": (768, 1344), "16:9": (1344, 768), "3:4": (896, 1184), "4:3": (1184, 896),
           "2:3": (832, 1248), "3:2": (1248, 832)}
TTS_LANGUAGES = ["Auto", "English", "Chinese", "Japanese", "Korean", "French", "German", "Spanish", "Portuguese",
                 "Russian", "Italian"]


@dataclass
class Tool:
    id: str
    kind: str                     # image | audio
    title: str
    description: str
    fields: list[dict]
    build: Callable[[dict, dict], dict]
    default_role: str = "other"
    needs: list[tuple[str, str, str]] = field(default_factory=list)   # (node class, input name, file) that must exist
    nodes: list[str] = field(default_factory=list)                    # node classes that must be installed
    source: str = "built-in"

    def describe(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "description": self.description,
                "fields": self.fields, "default_role": self.default_role, "source": self.source}


# ------------------------------------------------------------------ graph helpers

class _Graph:
    def __init__(self):
        self.g: dict[str, dict] = {}

    def add(self, class_type: str, **inputs) -> str:
        key = str(len(self.g) + 1)
        self.g[key] = {"class_type": class_type, "inputs": inputs}
        return key


# ------------------------------------------------------------------ built-in: FLUX.2 Klein image

KLEIN = {"unet": "flux-2-klein-9b-fp8.safetensors", "clip": "qwen_3_8b_fp8mixed.safetensors",
         "vae": "flux2-vae.safetensors", "lora": "Flux2-Klein-9B-consistency-V2.safetensors"}


def _build_klein(inputs: dict, ctx: dict) -> dict:
    g = _Graph()
    model = [g.add("UNETLoader", unet_name=KLEIN["unet"], weight_dtype="default"), 0]
    refs = [ctx["paths"][a] for a in inputs.get("images") or []]
    strength = float(inputs.get("consistency", 0.0) or 0.0)
    if refs and strength > 0:
        model = [g.add("LoraLoaderModelOnly", model=model, lora_name=KLEIN["lora"], strength_model=strength), 0]
    clip = [g.add("CLIPLoader", clip_name=KLEIN["clip"], type="flux2", device="default"), 0]
    vae = [g.add("VAELoader", vae_name=KLEIN["vae"]), 0]
    cond = [g.add("CLIPTextEncode", clip=clip, text=inputs["prompt"]), 0]
    for path in refs:  # each library image becomes a reference latent (image 1, image 2, … in the prompt)
        img = [g.add("ClapperLoadImage", path=path), 0]
        scaled = [g.add("ImageScaleToTotalPixels", image=img, upscale_method="lanczos", megapixels=1.0, resolution_steps=16), 0]
        latent = [g.add("VAEEncode", pixels=scaled, vae=vae), 0]
        cond = [g.add("ReferenceLatent", conditioning=cond, latent=latent), 0]
    width, height = ASPECTS.get(inputs.get("aspect") or "1:1", ASPECTS["1:1"])
    steps = int(inputs.get("steps") or 4)
    guider = [g.add("BasicGuider", model=model, conditioning=cond), 0]
    sampler = [g.add("KSamplerSelect", sampler_name="euler"), 0]
    sigmas = [g.add("Flux2Scheduler", steps=steps, width=width, height=height), 0]
    noise = [g.add("RandomNoise", noise_seed=int(ctx["seed"])), 0]
    empty = [g.add("EmptyFlux2LatentImage", width=width, height=height, batch_size=1), 0]
    sampled = [g.add("SamplerCustomAdvanced", noise=noise, guider=guider, sampler=sampler, sigmas=sigmas, latent_image=empty), 0]
    decoded = [g.add("VAEDecode", samples=sampled, vae=vae), 0]
    g.add("ClapperSaveImages", images=decoded, out_dir=ctx["out_dir"], prefix="image")
    return g.g


IMAGE_TOOL = Tool(
    id="image", kind="image", title="Image (FLUX.2 Klein)", default_role="character",
    description="Make a new image from a description, or change existing library images: the same character in a new "
                "pose, outfit or place; a place at another time of day; several references combined in one picture.",
    fields=[
        {"key": "prompt", "label": "Describe the image", "type": "longtext", "required": True,
         "placeholder": "Full-body anime girl with short silver hair and a red scarf, standing, neutral white background"},
        {"key": "cast", "label": "Who and where (characters, places, props)", "type": "entities", "max": 4,
         "hint": "Each one adds its main look as a reference image, and its name and description to the prompt — "
                 "write their names in the description: \"Aoi in a winter coat at the Bus stop\"."},
        {"key": "images", "label": "Start from library images (optional, up to 4)", "type": "images", "max": 4,
         "hint": "With images, describe the change: \"the same character, sitting on a bench at night\". Refer to them as image 1, image 2 … in the order chosen."},
        {"key": "aspect", "label": "Shape", "type": "select", "options": list(ASPECTS), "default": "9:16"},
        {"key": "steps", "label": "Steps", "type": "int", "default": 4, "min": 1, "max": 50, "advanced": True,
         "hint": "The Klein model is distilled for 4 steps (about 4 s per image on a 16 GB card); 20 steps took 14× longer with no visible gain."},
        {"key": "consistency", "label": "Consistency LoRA strength (with images)", "type": "float", "default": 0.35,
         "min": 0, "max": 1.5, "step": 0.05, "advanced": True},
    ],
    build=_build_klein,
    needs=[("UNETLoader", "unet_name", KLEIN["unet"]), ("CLIPLoader", "clip_name", KLEIN["clip"]),
           ("VAELoader", "vae_name", KLEIN["vae"])],
    nodes=["EmptyFlux2LatentImage", "Flux2Scheduler", "ReferenceLatent", "ClapperLoadImage", "ClapperSaveImages"],
)


# ------------------------------------------------------------------ built-in: Qwen3-TTS speech

def _build_tts(inputs: dict, ctx: dict) -> dict:
    g = _Graph()
    voice = inputs.get("voice")
    if not voice:
        raise ProjectError("choose a voice sample from the library")
    ref = [g.add("ClapperLoadAudio", path=ctx["paths"][voice]), 0]
    ref_text = (inputs.get("voice_text") or "").strip()
    tts = [g.add("FB_Qwen3TTSVoiceClone", target_text=inputs["text"], model_choice=inputs.get("model") or "1.7B",
                 device="auto", precision="bf16", language=inputs.get("language") or "Auto", ref_audio=ref,
                 ref_text=ref_text, seed=int(ctx["seed"]) % (2**63), max_new_tokens=2048, top_p=0.8, top_k=20,
                 temperature=float(inputs.get("temperature") or 0.9), repetition_penalty=1.05,
                 x_vector_only=not ref_text, attention="auto", unload_model_after_generate=True,
                 custom_model_path=""), 0]
    g.add("ClapperSaveAudio", audio=tts, out_dir=ctx["out_dir"], prefix="speech")
    return g.g


SPEECH_TOOL = Tool(
    id="speech", kind="audio", title="Speech (Qwen3-TTS voice clone)", default_role="speech",
    description="Speak a line in the voice of a sample from the library. Use the result as a sound clip in a scene.",
    fields=[
        {"key": "text", "label": "What is said", "type": "longtext", "required": True,
         "placeholder": "You came back… I knew you would."},
        {"key": "voice", "label": "Voice sample (a few seconds of clean speech)", "type": "audio", "required": True},
        {"key": "voice_text", "label": "What the sample says (optional, improves the likeness)", "type": "text",
         "from_asset": "transcript"},
        {"key": "language", "label": "Language", "type": "select", "options": TTS_LANGUAGES, "default": "Auto"},
        {"key": "temperature", "label": "Expressiveness (temperature)", "type": "float", "default": 0.9, "min": 0.1,
         "max": 1.5, "step": 0.05, "advanced": True},
        {"key": "model", "label": "Model size", "type": "select", "options": ["1.7B", "0.6B"], "default": "1.7B", "advanced": True},
    ],
    build=_build_tts,
    nodes=["FB_Qwen3TTSVoiceClone", "ClapperLoadAudio", "ClapperSaveAudio"],
)

BUILT_IN = [IMAGE_TOOL, SPEECH_TOOL]


# ------------------------------------------------------------------ user tools (workflow.json + tool.json)

def _user_tool(folder: Path) -> Tool:
    spec = json.loads((folder / "tool.json").read_text(encoding="utf-8"))
    workflow = json.loads((folder / "workflow.json").read_text(encoding="utf-8"))
    if "nodes" in workflow and "links" in workflow:
        raise ProjectError(f"{folder.name}: workflow.json must be in API format (ComfyUI: Export (API))")
    kind = spec.get("kind", "image")
    output_node = str(spec.get("output_node", ""))
    if output_node not in workflow:
        raise ProjectError(f"{folder.name}: output_node {output_node!r} is not in workflow.json")
    fields = spec.get("fields") or []
    for f in fields:
        if str(f.get("node")) not in workflow:
            raise ProjectError(f"{folder.name}: field {f.get('key')!r} points at node {f.get('node')!r}, not in workflow.json")

    def build(inputs: dict, ctx: dict) -> dict:
        g = copy.deepcopy(workflow)
        for f in fields:
            node = g[str(f["node"])]["inputs"]
            value = inputs.get(f["key"], f.get("default"))
            if f.get("type") == "images":
                ids = list(value or [])
                for slot, node_id in enumerate(f.get("nodes") or [f["node"]]):
                    if slot < len(ids):
                        g[str(node_id)]["inputs"][f.get("input", "path")] = ctx["paths"][ids[slot]]
                continue
            if f.get("type") in ("image", "audio"):
                value = ctx["paths"][value] if value else ""
            node[f.get("input", "text")] = value
        if spec.get("seed_node"):
            g[str(spec["seed_node"])]["inputs"][spec.get("seed_input", "seed")] = int(ctx["seed"])
        g[output_node]["inputs"]["out_dir"] = ctx["out_dir"]
        return g

    public_fields = [{k: v for k, v in f.items() if k not in ("node", "nodes", "input")} for f in fields]
    return Tool(id=f"user:{folder.name}", kind=kind, title=spec.get("title", folder.name),
                description=spec.get("description", ""), fields=public_fields, build=build,
                default_role=spec.get("default_role", "other"), source=str(folder),
                nodes=sorted({n["class_type"] for n in workflow.values()}))


def all_tools(user_dir: Path | None = None) -> tuple[list[Tool], list[str]]:
    """Built-in tools plus valid user tools; second value: problems with user tool folders."""
    tools, problems = list(BUILT_IN), []
    folder = user_dir or USER_TOOLS_DIR
    if folder.is_dir():
        for sub in sorted(p for p in folder.iterdir() if p.is_dir()):
            try:
                tools.append(_user_tool(sub))
            except (OSError, ValueError, KeyError) as exc:
                problems.append(f"{sub.name}: {exc}")
    return tools, problems


def get_tool(tool_id: str, user_dir: Path | None = None) -> Tool:
    for tool in all_tools(user_dir)[0]:
        if tool.id == tool_id:
            return tool
    raise ProjectError(f"unknown tool {tool_id!r}")


def availability(tools: list[Tool], object_info: Callable[[str], dict]) -> dict[str, dict]:
    """Which tools can run here: required nodes installed and model files present."""
    cache: dict[str, dict] = {}

    def info(node: str) -> dict:
        if node not in cache:
            try:
                cache[node] = object_info(node) or {}
            except Exception:  # noqa: BLE001 — an unreachable ComfyUI means "unknown", shown as missing
                cache[node] = {}
        return cache[node]

    out = {}
    for tool in tools:
        missing = [f"node {n}" for n in tool.nodes if not info(n)]
        for node, name, file in tool.needs:
            options = ((info(node).get("input") or {}).get("required") or {}).get(name, [[]])[0]
            if isinstance(options, list) and file not in options:
                missing.append(f"model {file}")
        out[tool.id] = {"ok": not missing, "missing": missing}
    return out


# ------------------------------------------------------------------ jobs

def _check_inputs(project: Project, tool: Tool, inputs: dict) -> dict[str, str]:
    """Validate the form against the tool's fields; returns {asset id: absolute path} for library inputs."""
    paths: dict[str, str] = {}
    root = project.root.resolve()
    for f in tool.fields:
        value = inputs.get(f["key"])
        if f.get("required") and (value is None or (isinstance(value, str) and not value.strip()) or value == []):
            raise ProjectError(f"{f['label']}: required")
        if f.get("type") in ("images", "image", "audio") and value:
            ids = value if isinstance(value, list) else [value]
            if f.get("max") and len(ids) > f["max"]:
                raise ProjectError(f"{f['label']}: at most {f['max']}")
            want = "image" if f["type"] in ("images", "image") else "audio"
            for aid in ids:
                asset = project.asset(aid)
                if asset["kind"] != want:
                    raise ProjectError(f"{f['label']}: {asset['title']} is not an {want}")
                paths[aid] = str(root / asset["file"])
        if f.get("type") == "entities" and value:
            for eid in value:
                e = project.entity(eid)
                if not e.get("main"):
                    raise ProjectError(f"{e['name']} has no main look yet — make one first")
        if f.get("type") == "select" and value is not None and value not in f["options"]:
            raise ProjectError(f"{f['label']}: choose one of {', '.join(map(str, f['options']))}")
    return paths


def with_cast(project: Project, inputs: dict) -> dict:
    """The image tool's inputs as run: each chosen character / place / prop adds its main look after the user's own
    images (so "image 1" in their text stays their first image), and a line naming it to the prompt."""
    cast = [project.entity(eid) for eid in inputs.get("cast") or []]
    if not cast:
        return inputs
    images = list(inputs.get("images") or [])
    lines = []
    for e in cast:
        if e["main"] not in images:
            images.append(e["main"])
        n = images.index(e["main"]) + 1
        lines.append(f"Image {n} is {e['name']}" + (f": {e['description']}" if e.get("description") else "") + ".")
    if len(images) > 4:
        raise ProjectError("at most 4 images in all (the chosen characters, places and props count as one each)")
    return {**inputs, "images": images, "prompt": " ".join(lines) + " " + (inputs.get("prompt") or "")}


def queue_tool(project: Project, client: ComfyClient, tool_id: str, inputs: dict, *, count: int = 1,
               role: str | None = None, seed: int | None = None, title: str = "", user_dir: Path | None = None,
               version_of: str | None = None, entity: str | None = None, look: str = "") -> list[dict]:
    """Queue `count` jobs (seeds seed, seed+1, …). Their results land in the library when collected.
    `version_of`: a library item the results are new versions of (they join its family).
    `entity` (+ `look`): file the results under that character / place as looks."""
    tool = get_tool(tool_id, user_dir)
    if version_of is not None:
        project.asset(version_of)
    count = max(1, min(MAX_COUNT, int(count or 1)))
    role = role or tool.default_role
    if role not in ASSET_ROLES:
        raise ProjectError(f"role must be one of {ASSET_ROLES}")
    inputs = {f["key"]: inputs.get(f["key"], f.get("default")) for f in tool.fields}
    _check_inputs(project, tool, inputs)
    run_inputs = with_cast(project, inputs)   # the recipe keeps the cast; the graph gets their images and names
    paths = _check_inputs(project, tool, run_inputs)
    if entity is not None:
        project.entity(entity)
    base = int(seed) if seed is not None else random.randrange(1, 2**48)
    jobs = project.data.setdefault("jobs", {})
    queued = []
    for i in range(count):
        job_id = _new_id("j")
        out_dir = project.root.resolve() / "jobs" / job_id
        ctx = {"paths": paths, "out_dir": str(out_dir), "seed": base + i}
        graph = tool.build(run_inputs, ctx)
        name = title.strip()
        if name and count > 1:
            name = f"{name} {i + 1}"
        job = {"id": job_id, "tool": tool.id, "tool_title": tool.title, "kind": tool.kind, "inputs": inputs,
               "seed": base + i, "role": role, "title": name, "status": "queued", "created": _now(),
               "dir": f"jobs/{job_id}", "outputs": []}
        if entity:
            job["entity"], job["look"] = entity, _label(look or title or "look")
        if version_of:
            job["version_of"] = version_of
            src = project.assets.get(version_of) or {}
            if src.get("entity") in project.entities and not entity:   # a version of a character's look is filed under that character
                job["entity"], job["look"] = src["entity"], (src.get("look") or "look") + " (version)"
        from .comfy_client import ComfyTimeout
        try:
            job["prompt_id"] = client.submit(graph)
        except ComfyTimeout as exc:   # may be queued: collect() finds it, or marks it interrupted
            job["prompt_id"] = exc.prompt_id
        except Exception as exc:
            job.update(status="failed", error=str(exc)[:2000])
            jobs[job_id] = job
            project.save()
            raise
        jobs[job_id] = job
        queued.append(job)
    project.save()
    return queued


# ------------------------------------------------------------------ entity actions (look, angle, in a place)

ENTITY_ACTIONS = ("first", "look", "angle", "place", "sheet")
# A turnaround sheet: every side of the character (or object) in one picture, for the video model as a reference.
_SHEET = {
    "character": ("A character turnaround reference sheet of the character from image 1 ({who}). The same character shown four "
                  "times side by side, evenly spaced, full body from head to shoes, standing straight in a neutral pose with arms "
                  "relaxed: front view, three-quarter view, side view, back view. Exactly the same outfit, colours, face, hair and "
                  "art style in every view, even flat lighting, plain light grey background, no text, no labels, no frames."),
    "prop": ("A turnaround reference sheet of the object from image 1 ({who}): the same object shown four times side by side, "
             "evenly spaced — front, side, back and top view — the same size, materials, colours and art style in every view, "
             "even flat lighting, plain light grey background, no text, no labels."),
}
_FIRST = {
    "character": "Original character design, a single character, full body, front view, standing relaxed, plain light grey background, no text. {who}. {extra}",
    "place": "Background art without people: {who}. {extra} No text.",
    "prop": "A single object on a plain light grey background, centred, no text: {who}. {extra}",
    "style": "A style sheet showing the art style: {who}. {extra} No text.",
}
# {who} is the name and the usual description; a change must win over it (the description may say "raincoat" or
# "in the rain"), so the prompt says the change replaces the usual look.
_LOOK = {
    "character": ("The same character as image 1 ({who}), now {change}. Keep the face, hair, body and art style. Full body, "
                  "front view, plain light grey background, no text."),
    "place": "The same place as image 1 ({who}), {change}. Keep the same layout, buildings and art style. No text.",
    "prop": "The same object as image 1 ({who}), {change}. Plain light grey background, no text.",
    "style": "The same art style as image 1 ({who}), {change}. No text.",
}
_ANGLE = {
    "character": "The same character as image 1 ({who}), seen {change}. The same outfit, face, hair and art style, plain light grey background, no text.",
    "place": "The same place as image 1 ({who}), seen {change}. The same art style, no text.",
    "prop": "The same object as image 1 ({who}), seen {change}. Plain light grey background, no text.",
    "style": "The same art style as image 1 ({who}), {change}. No text.",
}


def _label(text: str, limit: int = 48) -> str:
    """A short look label from what the user typed: whole words, at most `limit` characters."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "…"


# What a reference image is for, as told to the image model ("From image 2 take …").
REF_USES = {
    "outfit": "the outfit and clothes only (not the face, hair or body)",
    "pose": "the pose and body position only",
    "hair": "the hairstyle only",
    "accessory": "the accessories only (bag, umbrella, jewellery …)",
    "colours": "the colour palette only",
    "lighting": "the lighting, time of day and weather only",
    "background": "the background and setting only",
    "material": "the materials and textures only",
    "style": "the art style only",
}
REF_DEFAULT = {"character": "outfit", "place": "lighting", "prop": "material", "style": "style"}
MAX_IMAGES = 4   # what the image model takes


def _with_references(project: Project, kind: str, inputs: dict, refs: list[dict] | None) -> tuple[dict, list[str]]:
    """Add reference images after the ones an action already uses (image 1 stays the character / place itself), each
    with what to take from it. Returns the inputs and short labels ("outfit from Ren") for the look's name."""
    if not refs:
        return inputs, []
    images = list(inputs.get("images") or [])
    lines, labels = [], []
    for ref in refs:
        aid = ref.get("asset") if isinstance(ref, dict) else ref
        a = project.asset(aid)
        if a["kind"] != "image":
            raise ProjectError(f"{a['title']} is not an image")
        if aid not in images:
            images.append(aid)
        n = images.index(aid) + 1
        use = str((ref.get("use") if isinstance(ref, dict) else "") or REF_DEFAULT.get(kind, "outfit")).strip()
        phrase = REF_USES.get(use, f"only {use}")
        lines.append(f"From image {n} take {phrase}.")
        if use == "outfit" and kind == "character":
            lines.append("Keep image 1's face, hair and body.")
        owner = project.entities.get(a.get("entity") or "", {}).get("name") or a.get("title") or "a reference"
        labels.append(f"{use if use in REF_USES else 'reference'} from {owner}")
    if len(images) > MAX_IMAGES:
        used = len(images) - len(refs)
        raise ProjectError(f"at most {MAX_IMAGES} images in all: this action already uses {used}, so add at most {MAX_IMAGES - used} reference(s)")
    return {**inputs, "images": images, "prompt": inputs["prompt"].rstrip() + " " + " ".join(lines)}, labels


def entity_inputs(project: Project, entity_id: str, action: str, text: str, *, place_id: str | None = None,
                  aspect: str | None = None, source: str | None = None, refs: list[dict] | None = None) -> tuple[dict, dict]:
    """Image-tool inputs for an entity action, and the job fields that file the results into the entity.
    source: the image to start from (default: its main look) — e.g. an outfit look for a sheet of that outfit.
    refs: [{asset, use}] more images to take something from (an outfit, a pose, the lighting …; see REF_USES)."""
    inputs, extra = _entity_inputs(project, entity_id, action, text, place_id=place_id, aspect=aspect, source=source, refs=refs)
    inputs, labels = _with_references(project, project.entity(entity_id)["kind"], inputs, refs)
    if labels and not (text or "").strip() and action in ("look", "angle", "sheet"):
        extra["look"] = _label(("sheet · " if action == "sheet" else "") + ", ".join(labels))
    return inputs, extra


def _entity_inputs(project: Project, entity_id: str, action: str, text: str, *, place_id: str | None = None,
                   aspect: str | None = None, source: str | None = None, refs: list[dict] | None = None) -> tuple[dict, dict]:
    if action not in ENTITY_ACTIONS:
        raise ProjectError(f"action must be one of {ENTITY_ACTIONS}")
    e = project.entity(entity_id)
    if source:
        src = project.asset(source)
        if src["kind"] != "image":
            raise ProjectError("start from an image")
        e = {**e, "main": source}   # this generation starts from that image instead of the main look
        source_look = src.get("look") if src.get("entity") == entity_id else (src.get("title") or "")
    else:
        source_look = (project.assets.get(e.get("main") or "") or {}).get("look") or ""
    # The description says what the main look shows ("a yellow raincoat", "at dusk in the rain"). It helps when the
    # picture is the main look and nothing changes it; otherwise it fights the change (tested: with it, a new outfit
    # kept the raincoat on top and a sunny version kept the rain), so then only the name and the look's label go in.
    outfit_ref = any(str((r or {}).get("use") or REF_DEFAULT.get(e["kind"], "")) == "outfit" for r in refs or []) and e["kind"] == "character"
    own_picture = not source or source == project.entity(entity_id).get("main")
    who = e["name"] + (f": {e['description']}" if e.get("description") else "")
    if action == "look" or outfit_ref or not own_picture:
        who = e["name"] + (f", {source_look}" if source_look and not own_picture and source_look not in ("default", "design", "look") else "")
    text = (text or "").strip()
    from .director import video_aspect

    kind_aspect = "9:16" if e["kind"] in ("character", "prop") else video_aspect(project)
    if action == "first":
        return ({"prompt": _FIRST[e["kind"]].format(who=who, extra=text).replace("  ", " ").strip(), "images": [],
                 "aspect": aspect or kind_aspect}, {"entity": entity_id, "look": _label(text) or "design"})
    if not e.get("main"):
        raise ProjectError(f"{e['name']} has no main image yet — make or choose one first")
    if not text and action not in ("place", "sheet"):
        if not refs:
            raise ProjectError("describe what changes, or add a reference image to take it from")
        uses = [str((r or {}).get("use") or REF_DEFAULT.get(project.entity(entity_id)["kind"], "")) for r in refs]
        if "outfit" in uses and project.entity(entity_id)["kind"] == "character":   # the reference brings the clothes
            n = uses.index("outfit") + 2   # references follow image 1 (the character)
            text = (f"wearing exactly the clothes, shoes and accessories from image {n} instead of their own (take off everything "
                    "they wore in image 1)")
        else:
            text = "changed as the reference images below show"
    if action == "sheet":
        if e["kind"] not in _SHEET:
            raise ProjectError("a turnaround sheet is for characters and props")
        prompt = _SHEET[e["kind"]].format(who=who) + (f" {text}" if text else "")
        label = "sheet" + (f" · {source_look}" if source_look and source_look not in ("default", "design", "look") else "")
        return ({"prompt": prompt, "images": [e["main"]], "aspect": aspect or "16:9"},
                {"entity": entity_id, "look": _label(label), "sheet": True, "sheet_of": e["main"]})
    if action in ("look", "angle"):
        template = (_LOOK if action == "look" else _ANGLE)[e["kind"]]
        if outfit_ref and action == "angle":   # the clothes come from the reference: image 1 gives only who they are
            template = template.replace("The same outfit, face, hair and art style", "The same face, hair, body and art style as image 1, in the outfit described below")
        return ({"prompt": template.format(who=who, change=text), "images": [e["main"]], "aspect": aspect or kind_aspect},
                {"entity": entity_id, "look": _label(text)})
    place = project.entity(place_id or "")
    if place["kind"] != "place" or not place.get("main"):
        raise ProjectError("choose a place that has a main image")
    prompt = (f"The character from image 1 ({who}) in the place from image 2 ({place['name']}"
              + (f": {place['description']}" if place.get("description") else "") + f"). {text or 'Standing in the scene'}. "
              "The same character design and the same place, the same art style, no text.")
    return ({"prompt": prompt, "images": [e["main"], place["main"]], "aspect": aspect or video_aspect(project)},
            {"uses": [entity_id, place["id"]], "role": "start"})


def queue_entity(project: Project, client: ComfyClient, entity_id: str, action: str, text: str = "", *,
                 place_id: str | None = None, count: int = 2, seed: int | None = None, aspect: str | None = None,
                 prompt: str | None = None, source: str | None = None, refs: list[dict] | None = None) -> list[dict]:
    """Generate from a character / place: its first design, a new look or angle, or it placed in a place.
    `prompt` replaces the built prompt (the references stay)."""
    inputs, extra = entity_inputs(project, entity_id, action, text, place_id=place_id, aspect=aspect, source=source, refs=refs)
    if prompt:
        inputs["prompt"] = prompt
    role = extra.pop("role", None) or project.entity(entity_id)["kind"]
    e = project.entity(entity_id)
    title = (f"{e['name']} — character sheet" if action == "sheet" else
             (f"{e['name']} — {_label(text, 40)}" if text else e["name"]) if action != "place" else
             f"{e['name']} at {project.entity(place_id)['name']}")
    jobs = queue_tool(project, client, "image", inputs, count=count, role=role, seed=seed, title=title)
    for job in jobs:
        job.update(extra)
        job["entity_action"] = action
    project.save()
    return jobs


def _summary(job: dict) -> str:
    text = job["inputs"].get("prompt") or job["inputs"].get("text") or ""
    return " ".join(str(text).split())[:60]


def collect(project: Project, client: ComfyClient) -> list[str]:
    """Move finished jobs' files into the library; mark lost or failed jobs. Returns changed job ids."""
    from . import media

    jobs = project.data.get("jobs") or {}
    active = [j for j in jobs.values() if j["status"] in ("queued", "running")]
    if not active:
        return []
    changed = []
    queue = client.queue_state()
    running = {item[1] for item in queue.get("queue_running", [])}
    pending = {item[1] for item in queue.get("queue_pending", [])}
    for job in active:
        pid = job.get("prompt_id")
        if pid in running:
            if job["status"] != "running":
                job["status"] = "running"
                changed.append(job["id"])
            continue
        if pid in pending:
            continue
        entry = client.history(pid) if pid else None
        status = (entry or {}).get("status", {})
        from .director import history_outcome
        outcome, error = history_outcome(entry)
        if job.get("kind") == "upscale":
            from . import upscale
            # the result file counts even without ComfyUI's history (lost when ComfyUI restarts)
            ok = outcome in ("ok", "lost") and upscale.finish_job(project, job)
            if ok:
                stamps = {m[0]: m[1].get("timestamp") for m in status.get("messages", [])}
                if stamps.get("execution_start") and stamps.get("execution_success"):
                    job["seconds"] = round((stamps["execution_success"] - stamps["execution_start"]) / 1000, 1)
                job.update(status="done", finished=_now())
            elif outcome == "error":
                job.update(status="failed", error=error)
            elif outcome == "stopped":
                job.update(status="interrupted", error="stopped before it finished")
            elif entry:
                job.update(status="failed", error="the upscale finished without writing a video")
            else:
                job["status"] = "interrupted"
            changed.append(job["id"])
            continue
        folder = project.root / job["dir"]
        files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.endswith(".tmp")) if folder.is_dir() else []
        if outcome in ("ok", "lost") and files:   # files on disk are the result, also when ComfyUI's history is gone
            refs = job["inputs"].get("images") or ([job["inputs"]["voice"]] if job["inputs"].get("voice") else [])
            parent = job.get("version_of") or (refs[0] if job["kind"] == "image" and refs else None)
            for n, path in enumerate(files, 1):
                title = job.get("title") or _summary(job) or job["tool_title"]
                if len(files) > 1:
                    title += f" ({n})"
                origin = {"type": "tool", "tool": job["tool"], "tool_title": job["tool_title"], "job": job["id"],
                          "seed": job["seed"], "inputs": job["inputs"]}
                asset = media.add_with_info(project, path, role=job["role"], title=title, origin=origin,
                                            parent=parent if parent in project.assets else None)
                if job.get("entity") in project.entities:
                    project.add_to_entity(asset["id"], job["entity"], look=job.get("look") or "", save=False)
                    if job.get("sheet"):
                        asset["sheet"] = True
                        if job.get("sheet_of"):
                            asset["sheet_of"] = job["sheet_of"]   # the look this sheet shows
                        if not project.entities[job["entity"]].get("sheet"):   # the first sheet becomes its sheet
                            project.set_entity_sheet(job["entity"], asset["id"], save=False)
                if job.get("uses"):
                    asset["uses"] = [e for e in job["uses"] if e in project.entities]
                job["outputs"].append(asset["id"])
            shutil.rmtree(folder, ignore_errors=True)
            stamps = {m[0]: m[1].get("timestamp") for m in status.get("messages", [])}
            if stamps.get("execution_start") and stamps.get("execution_success"):
                job["seconds"] = round((stamps["execution_success"] - stamps["execution_start"]) / 1000, 1)
            job.update(status="done", finished=_now())
        elif outcome == "error":
            job.update(status="failed", error=error)
        elif outcome == "stopped":
            job.update(status="interrupted", error="stopped before it finished")
        elif entry:
            job.update(status="failed", error="the workflow finished without writing a file")
        else:
            job["status"] = "interrupted"
        changed.append(job["id"])
    if changed:
        project.save()
    return changed


def dismiss(project: Project, job_id: str) -> None:
    """Remove a finished, failed or cancelled job from the list (its library items stay)."""
    job = (project.data.get("jobs") or {}).get(job_id)
    if job is None:
        raise ProjectError(f"unknown job {job_id}")
    if job["status"] in ("queued", "running"):
        raise ProjectError("cancel the job first")
    folder = project.root / job["dir"]
    if job.get("kind") != "upscale" and folder.is_dir() and any(p.is_file() and not p.name.endswith(".tmp") for p in folder.iterdir()):
        raise ProjectError("this job has results on disk that are not in the library yet; refresh to collect them")
    if job.get("kind") != "upscale":  # an upscale's folder holds the result next to its take
        shutil.rmtree(folder, ignore_errors=True)
    del project.data["jobs"][job_id]
    project.save()
