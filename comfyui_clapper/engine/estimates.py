"""Render-time estimates for the Settings screen. Pure Python (no torch).

A take's time is modelled as `overhead + steps × seconds_per_step`, where seconds per step grow with the
amount of video (width × height × frames). The defaults were measured on an RTX 5060 Ti 16 GB at 416×736
(docs/DESIGN.md); once a project has finished takes, their own timings replace them.
"""
from __future__ import annotations

from statistics import median

from .project import Project

REF_PIXELS = 416 * 736
REF_FRAMES = 124

# Measured: FL2VA 13.6 s/step at 124 frames, ≈5.5 min per first scene; Ref2VA 4 steps 114 s, 20 steps 396 s.
DEFAULTS = {
    "fl2va": {"s_per_step": 13.6, "overhead": 58.0},
    "ref2va": {"s_per_step": 17.6, "overhead": 44.0},
}


def _model_of(take: dict) -> str:
    return take.get("model") or "fl2va"


def timing(project: Project | None = None) -> dict:
    """Per model: seconds per step (normalised to 416×736×124 frames), overhead, how many takes it is based on."""
    out = {m: {**v, "takes": 0, "measured_here": False, "steps_seen": []} for m, v in DEFAULTS.items()}
    if project is None:
        return out
    by_model: dict[str, list[tuple[float, float]]] = {}
    steps_seen: dict[str, set[int]] = {}
    for t in project.data.get("takes", {}).values():
        if t.get("status") != "done" or not t.get("render_seconds") or not t.get("steps"):
            continue
        size = (t.get("width") or 416) * (t.get("height") or 736) * (t.get("frames") or REF_FRAMES)
        norm = size / (REF_PIXELS * REF_FRAMES)
        if t.get("s_per_it"):
            per_step = float(t["s_per_it"]) / norm
            overhead = max(0.0, float(t["render_seconds"]) - float(t["s_per_it"]) * int(t["steps"]))
        else:
            d = DEFAULTS[_model_of(t)]
            per_step = max(1.0, (float(t["render_seconds"]) - d["overhead"]) / int(t["steps"]) / norm)
            overhead = d["overhead"]
        by_model.setdefault(_model_of(t), []).append((per_step, overhead))
        steps_seen.setdefault(_model_of(t), set()).add(int(t["steps"]))
    for model, rows in by_model.items():
        if model in out and rows:
            out[model] = {"s_per_step": round(median(r[0] for r in rows), 2), "overhead": round(median(r[1] for r in rows), 1),
                          "takes": len(rows), "measured_here": True, "steps_seen": sorted(steps_seen.get(model, ()))}
    return out


def estimate_seconds(t: dict, *, steps: int, width: int, height: int, frames: int) -> float:
    norm = (width * height * frames) / (REF_PIXELS * REF_FRAMES)
    return t["overhead"] + steps * t["s_per_step"] * norm
