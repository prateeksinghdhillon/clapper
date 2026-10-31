"""Small ffmpeg/ffprobe helpers for library files. Pure Python (no torch)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .project import Project, ProjectError

FFMPEG = shutil.which("ffmpeg") or os.path.expanduser("~/.local/bin/ffmpeg")
FFPROBE = shutil.which("ffprobe") or os.path.expanduser("~/.local/bin/ffprobe")


def probe(path: str | Path) -> dict:
    """{'duration': seconds} for audio/video, {'width', 'height'} for images and video; {} if unknown."""
    path = Path(path)
    info: dict = {}
    try:
        out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                              "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return info
    duration = (data.get("format") or {}).get("duration")
    if duration not in (None, "N/A"):
        try:
            info["duration"] = round(float(duration), 3)
        except ValueError:
            pass
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and stream.get("width"):
            info["width"], info["height"] = int(stream["width"]), int(stream["height"])
            break
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        info.pop("duration", None)  # still images report a nominal duration
    return info


def poster(project: Project, rel: str, seconds: float = 0.5, width: int = 360) -> Path:
    """A small JPEG of one frame of a project video, cached in .cache/thumbs/ (made once per file version)."""
    import hashlib

    src = (project.root / rel).resolve()
    if project.root.resolve() not in src.parents or not src.is_file():
        raise ProjectError("unknown video")
    key = hashlib.sha1(f"{rel}|{src.stat().st_mtime_ns}|{seconds:.2f}|{width}".encode()).hexdigest()[:20]
    out = project.root / ".cache" / "thumbs" / f"{key}.jpg"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jpg")
    for at in (seconds, 0.0):  # very short clips: fall back to the first frame
        proc = subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1",
                               "-vf", f"scale={width}:-2", "-q:v", "4", str(tmp)], capture_output=True, timeout=60)
        if proc.returncode == 0 and tmp.is_file() and tmp.stat().st_size:
            tmp.replace(out)
            return out
    raise ProjectError("could not read a frame from the video")


def add_with_info(project: Project, source: str | Path, **kwargs) -> dict:
    """Add a library file and record its duration / size."""
    asset = project.add_asset(source, save=False, **kwargs)
    asset.update(probe(project.root / asset["file"]))
    project.save()
    return asset


def grab_frame(project: Project, take_id: str, seconds: float, *, title: str = "") -> dict:
    """Save one frame of a finished take to the library (role 'frame')."""
    take = project.data["takes"].get(take_id)
    if not take or take.get("status") != "done":
        raise ProjectError("only a finished take has frames to save")
    video = project.root / take["dir"] / "take.mp4"
    if not video.is_file():
        raise ProjectError("this take has no video file")
    seconds = max(0.0, float(seconds))
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "frame.png"
        proc = subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{seconds:.3f}", "-i", str(video), "-frames:v", "1", str(out)],
                              capture_output=True, timeout=60)
        if proc.returncode != 0 or not out.is_file():
            raise ProjectError(f"could not read a frame at {seconds:.2f} s: {proc.stderr.decode(errors='replace')[-400:]}")
        index = project.scene_index(take["scene"]) + 1 if any(s["id"] == take["scene"] for s in project.scenes) else "?"
        return add_with_info(project, out, kind="image", role="frame",
                             title=title or f"Scene {index} take {take.get('number', '?')} at {seconds:.2f} s",
                             origin={"type": "frame", "take": take_id, "seconds": round(seconds, 3)})
