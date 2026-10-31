"""Finish: music and sound tracks, overlays and title cards, applied at export. Pure Python (no torch).

`project.data["finish"]`:
    scene_volume   the takes' own sound (1.0 = as rendered)
    tracks[]       {id, asset, start, volume, fade_in, fade_out, loop, duck, trim} — library sounds on the timeline
    overlays[]     {id, asset, start, end, position, x, y, width, opacity, fade} — library images over the picture
Times are seconds of the exported video; `end: null` = to the end. Positions: top-left, top, top-right, left,
center, right, bottom-left, bottom, bottom-right, or custom (x, y = the overlay's centre, 0–1).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from .project import Project, ProjectError, _new_id

POSITIONS = ("top-left", "top", "top-right", "left", "center", "right", "bottom-left", "bottom", "bottom-right", "custom")
MARGIN = 0.04            # of the shorter side
DUCK = "threshold=0.04:ratio=6:attack=25:release=450:makeup=1"


def _num(value, default, lo, hi) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = float(default)
    return max(lo, min(hi, v))


def normalise(project: Project, data: dict) -> dict:
    """Check and clean a finish dict from the interface / API."""
    out = {"scene_volume": _num(data.get("scene_volume", 1.0), 1.0, 0.0, 2.0), "tracks": [], "overlays": []}
    for t in data.get("tracks") or []:
        a = project.asset(t.get("asset", ""))
        if a["kind"] not in ("audio", "video"):
            raise ProjectError(f"{a['title']} has no sound to use as a track")
        out["tracks"].append({"id": t.get("id") or _new_id("k"), "asset": a["id"], "start": _num(t.get("start"), 0, 0, 36000),
                              "volume": _num(t.get("volume"), 0.6, 0, 2), "fade_in": _num(t.get("fade_in"), 1.0, 0, 30),
                              "fade_out": _num(t.get("fade_out"), 2.0, 0, 30), "loop": bool(t.get("loop")),
                              "duck": bool(t.get("duck", True)), "trim": _num(t.get("trim"), 0, 0, 36000)})
    for o in data.get("overlays") or []:
        a = project.asset(o.get("asset", ""))
        if a["kind"] != "image":
            raise ProjectError(f"{a['title']} is not an image")
        pos = o.get("position") or "bottom-right"
        if pos not in POSITIONS:
            raise ProjectError(f"position must be one of {POSITIONS}")
        end = o.get("end")
        out["overlays"].append({"id": o.get("id") or _new_id("o"), "asset": a["id"], "start": _num(o.get("start"), 0, 0, 36000),
                                "end": None if end in (None, "") else _num(end, 0, 0, 36000), "position": pos,
                                "x": _num(o.get("x"), 0.5, 0, 1), "y": _num(o.get("y"), 0.5, 0, 1),
                                "width": _num(o.get("width"), 0.25, 0.02, 1), "opacity": _num(o.get("opacity"), 1, 0, 1),
                                "fade": _num(o.get("fade"), 0.4, 0, 10)})
    return out


def set_finish(project: Project, data: dict) -> dict:
    project.data["finish"] = normalise(project, data)
    project.save()
    return project.data["finish"]


def finish_of(project: Project) -> dict:
    f = project.data.get("finish") or {}
    return {"scene_volume": f.get("scene_volume", 1.0), "tracks": list(f.get("tracks") or []), "overlays": list(f.get("overlays") or [])}


def is_empty(f: dict) -> bool:
    return not f["tracks"] and not f["overlays"] and abs(f["scene_volume"] - 1.0) < 1e-6


# ------------------------------------------------------------------ title cards

_SCRIPTS = [(r"[ऀ-ॿ]", "hi"), (r"[ঀ-৿]", "bn"), (r"[஀-௿]", "ta"), (r"[؀-ۿ]", "ar"),
            (r"[぀-ヿ]", "ja"), (r"[一-鿿]", "zh-cn"), (r"[가-힯]", "ko"), (r"[Ѐ-ӿ]", "ru"),
            (r"[฀-๿]", "th")]
STYLES = {"bold": "sans:bold", "regular": "sans", "serif": "serif:bold", "rounded": "sans:bold:rounded"}


def _font_file(pattern: str) -> str | None:
    if not shutil.which("fc-match"):
        return None
    out = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True, timeout=10)
    path = out.stdout.strip()
    return path if path and Path(path).is_file() else None


def _script_of(ch: str) -> str | None:
    for rx, lang in _SCRIPTS:
        if re.match(rx, ch):
            return lang
    return None


def _runs(line: str) -> list[tuple[str | None, str]]:
    """Split a line into runs of one script (None = Latin and common characters such as digits and spaces).
    Spaces and punctuation stay with the run before them, so words keep their font."""
    runs: list[tuple[str | None, str]] = []
    for ch in line:
        lang = _script_of(ch)
        if lang is None and runs and not (ch.isalpha() or ch.isdigit()):
            lang = runs[-1][0]
        if runs and runs[-1][0] == lang:
            runs[-1] = (lang, runs[-1][1] + ch)
        else:
            runs.append((lang, ch))
    return runs


def title_card(project: Project, text: str, *, style: str = "bold", color: str = "#ffffff", box: str | None = "#000000a0",
               size: int = 96, title: str = "") -> dict:
    """Render text as a transparent PNG (sharp at 1080p) and add it to the library as an overlay.
    Each run of text is drawn with a system font that has its script (Latin, Devanagari, CJK, Arabic, …)."""
    from PIL import Image, ImageDraw, ImageFont

    from . import media

    text = unicodedata.normalize("NFC", (text or "").strip())
    if not text:
        raise ProjectError("type the text of the title card")
    size = int(_num(size, 96, 16, 400))
    base = STYLES.get(style, "sans:bold")
    fonts: dict = {}

    def font(lang):
        if lang not in fonts:
            path = _font_file(base + (f":lang={lang}" if lang else ""))
            fonts[lang] = (ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size), path)
        return fonts[lang][0]

    def rgba(value: str | None):
        if not value:
            return None
        v = value.lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", v):
            raise ProjectError(f"colour must be #rrggbb or #rrggbbaa, not {value!r}")
        return tuple(int(v[i:i + 2], 16) for i in range(0, len(v), 2)) + ((255,) if len(v) == 6 else ())

    lines = [_runs(line) for line in text.split("\n")]
    if re.search(r"[\u3040-\u30FF]", text):   # kana present: kanji belong to the Japanese font
        lines = [[("ja" if lang == "zh-cn" else lang, chunk) for lang, chunk in line] for line in lines]
        lines = [[(lang, "".join(c for l2, c in grp)) for lang, grp in __import__("itertools").groupby(line, key=lambda r: r[0])] for line in lines]
    ascent = max(font(lang).getmetrics()[0] for line in lines for lang, _ in line) if any(lines) else size
    descent = max(font(lang).getmetrics()[1] for line in lines for lang, _ in line) if any(lines) else size // 4
    line_h, gap = ascent + descent, int(size * 0.18)
    widths = [sum(font(lang).getlength(chunk) for lang, chunk in line) for line in lines]
    pad = int(size * 0.45) if box else int(size * 0.16)
    w = int(max(widths or [size])) + 2 * pad
    h = len(lines) * line_h + (len(lines) - 1) * gap + 2 * pad
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = rgba(box)
    if fill:
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=int(size * 0.35), fill=fill)
    for n, (line, lw) in enumerate(zip(lines, widths)):
        x = (w - lw) / 2
        y = pad + n * (line_h + gap) + ascent       # baseline
        for lang, chunk in line:
            f = font(lang)
            if not fill:  # no box: a soft outline keeps the text readable on any picture
                d.text((x, y), chunk, font=f, anchor="ls", fill=(0, 0, 0, 200), stroke_width=max(2, size // 18), stroke_fill=(0, 0, 0, 200))
            d.text((x, y), chunk, font=f, anchor="ls", fill=rgba(color))
            x += f.getlength(chunk)
    used = sorted({Path(p).name for _, p in fonts.values() if p}) or ["default"]
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "title.png"
        img.save(out)
        return media.add_with_info(project, out, kind="image", role="overlay", title=title or text.replace("\n", " ")[:50],
                                   origin={"type": "title", "text": text, "style": style, "color": color, "box": box, "size": size,
                                           "fonts": used})


# ------------------------------------------------------------------ the ffmpeg part

def _xy(o: dict) -> tuple[str, str]:
    m = f"min(main_w,main_h)*{MARGIN}"
    pos = o["position"]
    if pos == "custom":
        return f"main_w*{o['x']:.4f}-overlay_w/2", f"main_h*{o['y']:.4f}-overlay_h/2"
    x = "(main_w-overlay_w)/2" if pos in ("top", "center", "bottom") else (m if "left" in pos else f"main_w-overlay_w-{m}")
    y = "(main_h-overlay_h)/2" if pos in ("left", "center", "right") else (m if pos.startswith("top") else f"main_h-overlay_h-{m}")
    return x, y


def filters(project: Project, f: dict, *, first_input: int, width: int, height: int, total: float,
            video: str = "v", audio: str = "a") -> tuple[list[str], list[str], str, str]:
    """ffmpeg inputs and filter parts that put the finish on top of the joined video.

    Returns (input args, filter parts, video label, audio label)."""
    inputs: list[str] = []
    parts: list[str] = []
    n = first_input
    vlabel, alabel = video, audio
    root = project.root
    for i, o in enumerate(f["overlays"]):
        a = project.assets.get(o["asset"])
        if not a:
            continue
        start = min(o["start"], total)
        end = total if o["end"] is None else min(max(o["end"], start + 0.05), total)
        if end - start < 0.05:
            continue
        inputs += ["-loop", "1", "-framerate", "24", "-t", f"{total:.3f}", "-i", str(root / a["file"])]
        ow = max(2, int(round(width * o["width"] / 2)) * 2)
        chain = [f"[{n}:v]format=rgba", f"scale={ow}:-2"]
        fd = min(o["fade"], (end - start) / 2)
        if fd > 0:
            chain += [f"fade=t=in:st={start:.3f}:d={fd:.3f}:alpha=1", f"fade=t=out:st={end - fd:.3f}:d={fd:.3f}:alpha=1"]
        if o["opacity"] < 1:
            chain.append(f"colorchannelmixer=aa={o['opacity']:.3f}")
        parts.append(",".join(chain) + f"[ov{i}]")
        x, y = _xy(o)
        out = f"vo{i}"
        parts.append(f"[{vlabel}][ov{i}]overlay=x='{x}':y='{y}':enable='between(t,{start:.3f},{end:.3f})':eof_action=pass[{out}]")
        vlabel, n = out, n + 1
    tracks = [t for t in f["tracks"] if t["asset"] in project.assets]
    duck = [t for t in tracks if t["duck"]]
    parts.append(f"[{alabel}]aformat=sample_rates=48000:channel_layouts=stereo,volume={f['scene_volume']:.3f}[asv]")
    scene = "asv"
    if duck:
        parts.append(f"[{scene}]asplit={len(duck) + 1}[amain]" + "".join(f"[asc{i}]" for i in range(len(duck))))
        scene = "amain"
    mix = [scene]
    di = 0
    for i, t in enumerate(tracks):
        a = project.assets[t["asset"]]
        if t["start"] >= total:
            continue
        if t["loop"]:
            inputs += ["-stream_loop", "-1"]
        inputs += ["-i", str(root / a["file"])]
        play = total - t["start"]
        chain = [f"[{n}:a]aformat=sample_rates=48000:channel_layouts=stereo", f"atrim=start={t['trim']:.3f}:duration={play:.3f}",
                 "asetpts=PTS-STARTPTS"]
        if t["fade_in"] > 0:
            chain.append(f"afade=t=in:st=0:d={min(t['fade_in'], play):.3f}")
        if t["fade_out"] > 0:
            src_len = (a.get("duration") or play) - t["trim"] if not t["loop"] else play
            chain.append(f"afade=t=out:st={max(0.0, min(play, src_len) - t['fade_out']):.3f}:d={t['fade_out']:.3f}")
        chain.append(f"volume={t['volume']:.3f}")
        if t["start"] > 0:
            chain.append(f"adelay=delays={int(t['start'] * 1000)}:all=1")
        label = f"tr{i}"
        parts.append(",".join(chain) + f"[{label}]")
        if t["duck"]:
            parts.append(f"[{label}][asc{di}]sidechaincompress={DUCK}[trd{i}]")
            label, di = f"trd{i}", di + 1
        mix.append(label)
        n += 1
    while di < len(duck):  # a ducked track that starts after the end: keep the split outputs used
        parts.append(f"[asc{di}]anullsink")
        di += 1
    if len(mix) > 1:
        parts.append("".join(f"[{m}]" for m in mix) + f"amix=inputs={len(mix)}:duration=first:dropout_transition=0:normalize=0[afin]")
        alabel = "afin"
    else:
        alabel = scene
    return inputs, parts, vlabel, alabel
