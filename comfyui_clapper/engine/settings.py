"""Generation settings.

Defaults are a proven fast-draft setup taken from a ComfyUI-H3-Continuum production workflow
(FL2VA int8 + Turbo 8-step LoRA at 20 steps, res_multistep/simple, 0.3 MP, 22-frame masked AV
continuation), so drafts start at a known speed/quality point. Every value can be changed per
project (Settings) or per take."""
from __future__ import annotations

import copy
import math

DEFAULT_SETTINGS: dict = {
    "models": {
        "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "weight_dtype": "default",
        "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "video_vae": "minimax_h3_video_vae_fp16.safetensors",
        "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    },
    "loras": [
        {"name": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors", "strength": 1.0, "enabled": True},
    ],
    "patches": {
        "chunk_feed_forward": {"enabled": True, "chunks": 2, "seq_threshold": 4096},
        "spectrum": {"enabled": False},
    },
    "sampling": {"sampler": "res_multistep", "scheduler": "simple", "steps": 20, "denoise": 1.0},
    "resolution": {"mode": "start_image", "megapixels": 0.30, "multiple": 32, "width": 416, "height": 736},
    "continuity": {"frames": 22, "method": "masked", "audio": True},
    "references": {"image_size": "match"},
    "scene_seconds": 5.0,
}

SPEED_PROFILES: dict = {
    # The defaults above, unchanged.
    "continuum_current": {},
    # Match the Turbo LoRA's distilled step count (README recommendation). Untested here yet.
    "turbo_matched": {"sampling": {"steps": 8}},
}


def merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def fit_resolution(src_w: int, src_h: int, megapixels: float, multiple: int = 32) -> tuple[int, int]:
    """Same result as ComfyUI's ImageScaleToTotalPixels(megapixels, resolution_steps=multiple)."""
    scale = math.sqrt(megapixels * 1_000_000 / (src_w * src_h))
    w = max(multiple, round(src_w * scale / multiple) * multiple)
    h = max(multiple, round(src_h * scale / multiple) * multiple)
    return int(w), int(h)
