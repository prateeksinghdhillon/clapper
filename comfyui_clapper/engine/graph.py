"""Builds ComfyUI API-format graphs for one take. Pure Python (no torch)."""
from __future__ import annotations

import json


def build_take_graph(*, settings: dict, prompt: str, width: int, height: int, frames: int,
                     spec: dict, seed: int, take_dir: str, meta: dict) -> dict:
    g: dict[str, dict] = {}
    n = 0

    def add(class_type: str, **inputs) -> str:
        nonlocal n
        n += 1
        g[str(n)] = {"class_type": class_type, "inputs": inputs}
        return str(n)

    m = settings["models"]
    model = [add("UNETLoader", unet_name=m["unet"], weight_dtype=m.get("weight_dtype", "default")), 0]
    ff = settings["patches"].get("chunk_feed_forward", {})
    if ff.get("enabled"):
        model = [add("MiniMaxChunkFeedForward", model=model, chunks=int(ff.get("chunks", 2)),
                     seq_threshold=int(ff.get("seq_threshold", 4096))), 0]
    for lora in settings.get("loras", []):
        if lora.get("enabled", True):
            model = [add("LoraLoaderModelOnly", model=model, lora_name=lora["name"],
                         strength_model=float(lora.get("strength", 1.0))), 0]
    spectrum = settings["patches"].get("spectrum", {})
    if spectrum.get("enabled"):
        params = {k: v for k, v in spectrum.items() if k != "enabled"}
        model = [add("SpectrumApplyMiniMaxH3", model=model, enabled=True, **params), 0]

    clip = [add("CLIPLoader", clip_name=m["clip"], type="minimax"), 0]
    vae = [add("VAELoader", vae_name=m["video_vae"]), 0]
    audio_vae = [add("VAELoader", vae_name=m["audio_vae"]), 0]

    cond = add("ClapperSceneConditioning", clip=clip, vae=vae, audio_vae=audio_vae, prompt=prompt,
               width=int(width), height=int(height), frames=int(frames),
               spec=json.dumps(spec, ensure_ascii=False))
    s = settings["sampling"]
    noise = [add("RandomNoise", noise_seed=int(seed)), 0]
    sampler = [add("KSamplerSelect", sampler_name=s["sampler"]), 0]
    sigmas = [add("BasicScheduler", model=model, scheduler=s["scheduler"], steps=int(s["steps"]),
                  denoise=float(s.get("denoise", 1.0))), 0]
    guider = [add("BasicGuider", model=model, conditioning=[cond, 0]), 0]
    sampled = [add("SamplerCustomAdvanced", noise=noise, guider=guider, sampler=sampler, sigmas=sigmas,
                   latent_image=[cond, 1]), 0]
    add("ClapperSaveTake", samples=sampled, target=[cond, 1], vae=vae, audio_vae=audio_vae,
        take_dir=str(take_dir), meta=json.dumps(meta, ensure_ascii=False))
    return g
