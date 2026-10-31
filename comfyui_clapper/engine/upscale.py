"""Upscaling selected takes (Finish stage). Pure Python (no torch).

Two methods, both queued as jobs on a take (`project.jobs`, kind "upscale"):

- **sharpen** — an image upscale model (Real-ESRGAN ×2, 4x-UltraSharp ×4) on every frame, then an exact
  resize to the target size. No diffusion; the take's sound is kept as-is. Seconds to a minute per take.
- **redetail** — the MiniMax H3 latent upscaler on the take's saved latents, then a short low-noise H3 pass
  in tiles (MMH3 Split Upscale) that draws real detail at the new size. Minutes per take. The take's
  original sound is kept (the pass would otherwise re-generate it).

The result is `takes/<scene>/<take>/upscaled/<method>-<size>.mp4` and is recorded on the take as
`upscaled`; export uses it when present.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .comfy_client import ComfyClient
from .project import Project, ProjectError, _new_id, _now
from .settings import DEFAULT_SETTINGS, merge

METHODS = ("sharpen", "redetail")
SIZES = {"720p": 720, "1080p": 1080}          # short side of the result
STRENGTHS = {"light": "0.6316, 0.3158, 0.0000", "strong": "0.9035, 0.6316, 0.3158, 0.0000"}
UPSCALE_MODELS = {2: "RealESRGAN_x2plus.pth", 4: "4x-UltraSharp.pth"}
LATENT_UPSCALER = "minimax_h3_latent_upscaler_3d_fp16.safetensors"


def target_size(width: int, height: int, size: str) -> tuple[int, int]:
    """Keep the aspect; the short side becomes 720 or 1080; both sides even (multiple of 32 for redetail)."""
    short = SIZES[size]
    scale = short / min(width, height)
    if scale < 1:
        raise ProjectError(f"the take is already larger than {size}")
    w, h = round(width * scale), round(height * scale)
    return w - w % 2, h - h % 2


def _graph_sharpen(take_dir: Path, out_path: Path, width: int, height: int, size: str) -> dict:
    tw, th = target_size(width, height, size)
    # Real-ESRGAN ×2, then an exact lanczos resize. 4x-UltraSharp at 1080p took 4.4× longer for no visible gain.
    factor = 2
    g = {
        "1": {"class_type": "ClapperLoadVideoFrames", "inputs": {"path": str(take_dir / "take.mp4")}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": UPSCALE_MODELS[factor]}},
        "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "ImageScale", "inputs": {"image": ["3", 0], "upscale_method": "lanczos", "width": tw,
                                                     "height": th, "crop": "disabled"}},
        "5": {"class_type": "ClapperSaveVideo", "inputs": {"images": ["4", 0], "audio_path": str(take_dir / "audio.wav"),
                                                           "out_path": str(out_path)}},
    }
    return g


def _graph_redetail(project: Project, take: dict, take_dir: Path, out_path: Path, size: str, strength: str,
                    seed: int) -> dict:
    meta = json.loads((take_dir / "meta.json").read_text(encoding="utf-8"))
    width, height = int(meta["width"]), int(meta["height"])
    tw, th = target_size(width, height, size)
    tw, th = tw - tw % 32, th - th % 32
    settings = merge(DEFAULT_SETTINGS, meta.get("settings") or {})
    r = settings["video_model"]["ref2va"]
    spec = dict(meta.get("spec") or {})
    # Re-sample with the take's own prompt and references, without continuation or pinned frames: the
    # upscaled latent itself carries the picture.
    spec.pop("continue_from", None)
    spec.pop("bridge_to", None)
    spec.pop("audio_guides", None)
    if spec.get("start_image"):
        spec["start_image"] = {**spec["start_image"], "pin": False}
    g: dict[str, dict] = {}

    def add(cls: str, **inputs) -> str:
        key = str(len(g) + 1)
        g[key] = {"class_type": cls, "inputs": inputs}
        return key

    model = [add("UNETLoader", unet_name=r["unet"], weight_dtype="default"), 0]
    model = [add("MiniMaxChunkFeedForward", model=model, chunks=2, seq_threshold=4096), 0]
    for lora in r.get("loras") or []:
        model = [add("LoraLoaderModelOnly", model=model, lora_name=lora["name"], strength_model=float(lora.get("strength", 1))), 0]
    clip = [add("CLIPLoader", clip_name=settings["models"]["clip"], type="minimax"), 0]
    vae = [add("VAELoader", vae_name=settings["models"]["video_vae"]), 0]
    audio_vae = [add("VAELoader", vae_name=settings["models"]["audio_vae"]), 0]
    cond = add("ClapperSceneConditioning", clip=clip, vae=vae, audio_vae=audio_vae, prompt=meta["prompt"],
               width=tw, height=th, frames=int(meta["frames"]), spec=json.dumps(spec, ensure_ascii=False))
    av = [add("ClapperLoadTakeLatent", take_dir=str(take_dir)), 0]
    sep = add("LTXVSeparateAVLatent", av_latent=av)
    up = [add("MinimaxH3LatentUpscaler3D", latent=[sep, 0], model_name=LATENT_UPSCALER, mode="target dimensions",
              **{"mode.width": tw, "mode.height": th}, align=32, enable_temporal_chunking=True, force_unload=True,
              device="cuda", precision="fp16"), 0]
    joined = [add("LTXVConcatAVLatent", video_latent=up, audio_latent=[sep, 1]), 0]
    temporal = [add("MMH3TemporalSplitParamsV10", chunk_frames=73, temporal_overlap_frames=22, anchor_strength=0.999,
                    motion_anchor_frames="22", identity_anchor_frames=24), 0]
    spatial = [add("MMH3SpatialSplitParamsV10", tile_width=512, tile_height=512, overlap_ratio=0.25, fade_ratio=0.5,
                   min_tile_size=256, seam_denoise=0.7), 0]
    noise = [add("RandomNoise", noise_seed=int(seed)), 0]
    sampler = [add("KSamplerSelect", sampler_name="euler"), 0]
    sigmas = [add("ManualSigmas", sigmas=STRENGTHS[strength]), 0]
    refined = [add("MMH3SplitUpscale", model=model, conditioning=[cond, 0], latent=joined, noise=noise, sampler=sampler,
                   sigmas=sigmas, cfg=1.0, seam_polish="auto", color_match=True, temporal_split_param=temporal,
                   spatial_split_param=spatial), 0]
    sep2 = add("LTXVSeparateAVLatent", av_latent=refined)
    images = [add("VAEDecode", samples=[sep2, 0], vae=vae), 0]
    add("ClapperSaveVideo", images=images, audio_path=str(take_dir / "audio.wav"), out_path=str(out_path))
    return g


def queue_upscale(project: Project, client: ComfyClient, take_id: str, *, method: str = "sharpen",
                  size: str = "1080p", strength: str = "light", seed: int | None = None) -> dict:
    take = project.data["takes"].get(take_id)
    if not take or take.get("status") != "done":
        raise ProjectError("only a finished take can be upscaled")
    if method not in METHODS:
        raise ProjectError(f"method must be one of {METHODS}")
    if size not in SIZES:
        raise ProjectError(f"size must be one of {tuple(SIZES)}")
    if strength not in STRENGTHS:
        raise ProjectError(f"strength must be one of {tuple(STRENGTHS)}")
    active = [j for j in (project.data.get("jobs") or {}).values()
              if j.get("kind") == "upscale" and j.get("take") == take_id and j["status"] in ("queued", "running")]
    if active:
        raise ProjectError("this take is already being upscaled")
    take_dir = project.root.resolve() / take["dir"]
    width, height = int(take.get("width") or 0), int(take.get("height") or 0)
    if not (width and height):
        meta = json.loads((take_dir / "meta.json").read_text(encoding="utf-8"))
        width, height = int(meta["width"]), int(meta["height"])
    label = method if method == "sharpen" else f"redetail-{strength}"
    out_rel = f"{take['dir']}/upscaled/{label}-{size}.mp4"
    out_path = project.root.resolve() / out_rel
    seed = int(seed) if seed is not None else random.randrange(1, 2**48)
    if method == "sharpen":
        graph = _graph_sharpen(take_dir, out_path, width, height, size)
    else:
        if not (take_dir / "latent.safetensors").is_file():
            raise ProjectError("re-detail needs the take's saved latents (takes adopted from elsewhere have none); use Sharpen")
        graph = _graph_redetail(project, take, take_dir, out_path, size, strength, seed)
    job_id = _new_id("j")
    tw, th = target_size(width, height, size)
    job = {"id": job_id, "tool": f"upscale:{method}", "tool_title": "Upscale", "kind": "upscale", "take": take_id,
           "inputs": {"method": method, "size": size, "strength": strength}, "seed": seed, "status": "queued",
           "created": _now(), "dir": str(Path(out_rel).parent), "out": out_rel, "title": "", "outputs": [],
           "width": tw - (tw % 32 if method == "redetail" else 0), "height": th - (th % 32 if method == "redetail" else 0)}
    job["prompt_id"] = client.submit(graph)
    project.data.setdefault("jobs", {})[job_id] = job
    project.save()
    return job


def finish_job(project: Project, job: dict) -> bool:
    """Record a finished upscale on its take. True when the file is there."""
    out = project.root / job["out"]
    if not out.is_file():
        return False
    take = project.data["takes"].get(job["take"])
    if take is not None:
        versions = take.setdefault("upscales", {})
        key = Path(job["out"]).stem
        versions[key] = {"file": job["out"], "method": job["inputs"]["method"], "size": job["inputs"]["size"],
                         "strength": job["inputs"].get("strength"), "width": job.get("width"), "height": job.get("height"),
                         "created": _now(), "job": job["id"]}
        take["upscaled"] = key   # the version export uses
    return True


def delete_version(project: Project, take_id: str, version: str) -> None:
    """Delete one upscaled version of a take (file and record); the export falls back to the original."""
    take = project.data["takes"].get(take_id)
    if not take or version not in (take.get("upscales") or {}):
        raise ProjectError("unknown upscaled version")
    rec = take["upscales"].pop(version)
    (project.root / rec["file"]).unlink(missing_ok=True)
    if take.get("upscaled") == version:
        take["upscaled"] = None
    project.save()


def best_upscale(take: dict) -> dict | None:
    key = take.get("upscaled")
    return (take.get("upscales") or {}).get(key) if key else None
