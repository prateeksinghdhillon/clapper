"""Import existing work into a project. Pure Python (no torch)."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .project import Project, ProjectError

_HEADER = re.compile(r"^\s*\[\s*(?:(?:chunk|clip)\s*\d+|\d+(?:\.\d+)?\s*(?:s|sec|seconds)?\s*[-–—]\s*\d+(?:\.\d+)?\s*(?:s|sec|seconds)?)\s*\]\s*$", re.I)
_TIME = re.compile(r"(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)?\s*[-–—]\s*(\d+(?:\.\d+)?)", re.I)
_CONTINUATION = re.compile(r"^\s*Continuation of Chunk \d+\.\s*", re.I)


def split_timeline(script: str) -> tuple[str, list[dict]]:
    """(preamble, sections[{header, text, seconds}]) of a Continuum Timeline prompt."""
    preamble, sections, current = [], [], None
    for line in script.splitlines():
        if _HEADER.match(line):
            current = {"header": line.strip(), "lines": []}
            sections.append(current)
        elif current is None:
            preamble.append(line)
        else:
            current["lines"].append(line)
    out = []
    for s in sections:
        m = _TIME.search(s["header"])
        out.append({"header": s["header"], "text": "\n".join(s["lines"]).strip(),
                    "seconds": (float(m.group(2)) - float(m.group(1))) if m else None})
    return "\n".join(preamble).strip(), out


def import_timeline(project: Project, script: str, picture_names: dict[int, str], *,
                    audio_names: dict[int, str] | None = None, strip_continuation: bool = True) -> list[dict]:
    """Preamble -> project style text; each section -> a scene. <Picture N>/<Audio N> -> @names."""
    def to_names(text: str) -> str:
        text = re.sub(r"<Picture\s+(\d+)>", lambda m: "@" + picture_names[int(m.group(1))] if int(m.group(1)) in picture_names else m.group(0), text)
        return re.sub(r"<Audio\s+(\d+)>", lambda m: "@" + (audio_names or {})[int(m.group(1))] if int(m.group(1)) in (audio_names or {}) else m.group(0), text)

    preamble, sections = split_timeline(script)
    if not sections:
        raise ProjectError("no Timeline sections found")
    # "<Picture N> is …" lines become that reference's definition, so a scene that does not use the
    # reference does not carry its definition either. Everything else stays shared style text.
    style_lines = []
    for line in preamble.split("\n"):
        m = re.match(r"^\s*<(Picture|Audio)\s+(\d+)>", line)
        names = picture_names if m and m.group(1) == "Picture" else (audio_names or {})
        if m and int(m.group(2)) in names and names[int(m.group(2))] in project.data["library"]["items"]:
            project.data["library"]["items"][names[int(m.group(2))]]["definition"] = to_names(line.strip())
        else:
            style_lines.append(line)
    project.data["style"] = to_names("\n".join(style_lines))
    project.save()
    scenes = []
    for i, sec in enumerate(sections):
        text = to_names(sec["text"])
        if strip_continuation:  # the Director adds its own continuation; keep the scene text about the scene
            text = _CONTINUATION.sub("", text, count=1)
        scenes.append(project.add_scene(text=text, seconds=sec["seconds"], join="continue" if i else "cut",
                                        title=""))
    return scenes


def adopt_take(project: Project, scene_id: str, source_dir: str | Path, *, parent_take: str | None = None,
               select: bool = True) -> dict:
    """Copy an already-rendered Director take folder (latent + mp4 + meta) into the project."""
    source = Path(source_dir)
    meta = json.loads((source / "meta.json").read_text(encoding="utf-8"))
    scene = project.scene(scene_id)
    compiled = project.compile_prompt(scene)
    take = project.new_take(scene_id, status="done", seed=meta.get("seed"), parent_take=parent_take,
                            prompt_sha256=None if compiled["prompt"] != meta.get("prompt") else compiled["sha256"],
                            refs=project.scene_refs(scene), join=scene["join"] if project.scene_index(scene_id) else "cut",
                            frames=meta["frames"], context_frames=int(meta.get("context_frames") or 0),
                            width=meta["width"], height=meta["height"], imported_from=str(source),
                            finished=meta.get("saved_utc"))
    if meta.get("saved_utc"):  # it was made when it was rendered, not when it was imported
        take = project.update_take(take["id"], created=meta["saved_utc"])
    shutil.copytree(source, project.root / take["dir"], dirs_exist_ok=True)
    if select:
        project.select_take(scene_id, take["id"])
    return take
