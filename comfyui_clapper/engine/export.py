"""Stitch the selected takes of a project into one video with ffmpeg. Pure Python (no torch)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .project import Project, ProjectError, folder_name

FFMPEG = shutil.which("ffmpeg") or os.path.expanduser("~/.local/bin/ffmpeg")
FPS = 24
AUDIO_FADE = 0.03  # seconds of fade-out/in at each join, to avoid clicks


def export_plan(project: Project, *, partial: bool = True, use_upscaled: bool = True) -> list[dict]:
    """One segment per scene: which take, and which frame range of it to keep.

    With partial=True, stops at the first scene that has no finished selected take (a later scene
    continues from it, so skipping one would leave a jump) and exports the ready scenes before it.
    """
    segments = []
    for index, scene in enumerate(project.scenes):
        take = project.data["takes"].get(scene.get("selected_take") or "")
        if not take or take.get("status") != "done":
            if partial and segments:
                break
            raise ProjectError(f"scene {index + 1} has no finished selected take")
        meta = json.loads((project.root / take["dir"] / "meta.json").read_text(encoding="utf-8"))
        frames = int(meta["frames"])
        start = int(take.get("context_frames") or 0)  # carried-over frames already shown by the previous scene
        end = frames
        if take.get("join") == "bridge":
            end -= int(meta.get("spec", {}).get("bridge_to", {}).get("frames", 0))  # next scene shows these
        if end - start < 1:
            raise ProjectError(f"scene {index + 1}: nothing left after trimming overlaps")
        from .upscale import best_upscale

        up = best_upscale(take) if use_upscaled else None
        if up and not (project.root / up["file"]).is_file():
            up = None
        segments.append({"scene": scene["id"], "scene_number": index + 1, "take": take["id"],
                         "take_number": take.get("number"),
                         "file": str(project.root / (up["file"] if up else f"{take['dir']}/take.mp4")),
                         "upscaled": (f"{up['method']} {up['size']}" if up else None),
                         "width": int(up["width"] if up else meta.get("width") or take.get("width") or 416),
                         "height": int(up["height"] if up else meta.get("height") or take.get("height") or 736),
                         "start_frame": start, "end_frame": end, "seconds": (end - start) / FPS})
    if not segments:
        raise ProjectError("the project has no scenes")
    return segments


WARNINGS = {
    "continuity_changed": "was made from a different take of the scene before it — expect a visible jump where it starts",
    "parent_missing": "continues from a scene that has no take",
    "prompt_changed": "has a newer prompt than its selected take (the video shows the older version)",
    "refs_changed": "has different references than its selected take (the video shows the older version)",
}


def export_check(project: Project) -> dict:
    """What an export would contain: ready scenes, where it stops, and warnings for the included scenes."""
    ready = 0
    for scene in project.scenes:
        take = project.data["takes"].get(scene.get("selected_take") or "")
        if not take or take.get("status") != "done":
            break
        ready += 1
    seconds = sum(s["seconds"] for s in export_plan(project, partial=True)) if ready else 0.0
    warnings = []
    for index, scene in enumerate(project.scenes[:ready]):
        start = sum(s["seconds"] for s in export_plan(project, partial=True)[:index]) if index else 0.0
        for badge in project.scene_status(scene["id"]):
            if badge in WARNINGS:
                warnings.append({"scene": index + 1, "badge": badge, "at_seconds": round(start, 1),
                                 "message": f"Scene {index + 1} {WARNINGS[badge]}"})
    missing = [i + 1 for i, s in enumerate(project.scenes) if i >= ready]
    plan = export_plan(project, partial=True) if ready else []
    upscaled = [s["scene_number"] for s in plan if s["upscaled"]]
    size = export_size(plan, "auto") if plan else None
    return {"ready": ready, "total": len(project.scenes), "complete": ready == len(project.scenes),
            "seconds": round(seconds, 2), "missing": missing, "warnings": warnings,
            "upscaled": upscaled, "not_upscaled": [s["scene_number"] for s in plan if not s["upscaled"]],
            "size": size}


EXPORT_SIZES = ("auto", "original", "720p", "1080p")


def export_size(segments: list[dict], size: str) -> list[int]:
    """auto = the largest scene (upscaled scenes set it; the others are resized to match);
    720p / 1080p = that short side; original = the first scene's size."""
    if size == "original":
        return [segments[0]["width"], segments[0]["height"]]
    if size in ("720p", "1080p"):
        # rounded exactly like an upscale, so takes upscaled to this size need no resize (scales down too)
        short = 720 if size == "720p" else 1080
        base = min(segments, key=lambda seg: seg["width"] * seg["height"])
        k = short / min(base["width"], base["height"])
        w, h = round(base["width"] * k), round(base["height"] * k)
        return [w - w % 2, h - h % 2]
    big = max(segments, key=lambda s: s["width"] * s["height"])
    return [big["width"], big["height"]]


def export_video(project: Project, *, name: str | None = None, partial: bool = False, size: str = "auto",
                 use_upscaled: bool = True) -> Path:
    if size not in EXPORT_SIZES:
        raise ProjectError(f"size must be one of {EXPORT_SIZES}")
    segments = export_plan(project, partial=partial, use_upscaled=use_upscaled and size != "original")
    width, height = export_size(segments, size)
    out_dir = project.root / "exports"
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # File names that work on every system: the project's folder-style name (letters, digits, dashes).
    out = out_dir / f"{folder_name(name or project.data['name'])}-{stamp}.mp4"

    cmd = [FFMPEG, "-v", "error", "-y"]
    for seg in segments:
        cmd += ["-i", seg["file"]]
    parts, labels = [], []
    for i, seg in enumerate(segments):
        t0, t1 = seg["start_frame"] / FPS, seg["end_frame"] / FPS
        dur = t1 - t0
        scale = "" if (seg["width"], seg["height"]) == (width, height) else f",scale={width}:{height}:flags=lanczos,setsar=1"
        parts.append(f"[{i}:v]trim=start_frame={seg['start_frame']}:end_frame={seg['end_frame']},setpts=PTS-STARTPTS{scale}[v{i}]")
        fades = []
        if i > 0:
            fades.append(f"afade=t=in:st=0:d={AUDIO_FADE}")
        if i < len(segments) - 1:
            fades.append(f"afade=t=out:st={max(0.0, dur - AUDIO_FADE):.4f}:d={AUDIO_FADE}")
        chain = ",".join([f"atrim=start={t0:.6f}:end={t1:.6f}", "asetpts=PTS-STARTPTS", *fades])
        parts.append(f"[{i}:a]{chain}[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(segments)}:v=1:a=1[v][a]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-r", str(FPS),
            "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(out)]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise ProjectError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[-1500:]}")
    manifest = {"project": project.data["name"], "created": stamp, "file": out.name, "width": width, "height": height,
                "segments": segments,
                "total_seconds": round(sum(s["seconds"] for s in segments), 3),
                "scenes_exported": len(segments), "scenes_total": len(project.scenes)}
    out.with_suffix(".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return out
