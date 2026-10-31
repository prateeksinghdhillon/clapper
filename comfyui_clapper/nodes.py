"""ComfyUI graph nodes used by Clapper.

The Director builds and queues these graphs itself; users normally never wire
them by hand. Masked AV continuation is adapted from ComfyUI-H3-Continuum
(MIT, (c) 2026 ukr8b3g-cmyk), v3/masked_continuation.py and v2/h3_builder.py.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import torch
from PIL import Image

import comfy.model_management
import comfy.nested_tensor
import node_helpers
from comfy_extras.nodes_audio import vae_decode_audio
from comfy_extras.nodes_minimax_h3 import _encode_ref_audio, _resize

from .h3_temporal import audio_latent_t, context_slots, video_latent_t

CANVAS_MULTIPLE = 32
REF_IMAGE_SHORT_EDGE = 2048
LATENT_FILE = "latent.safetensors"
FFMPEG = shutil.which("ffmpeg") or os.path.expanduser("~/.local/bin/ffmpeg")


# ---------------------------------------------------------------- loading helpers

def _load_image(path: str) -> torch.Tensor:
    with Image.open(path) as im:
        rgb = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    return torch.from_numpy(rgb)[None]  # [1, H, W, 3]


def _load_audio(path: str) -> dict:
    import av

    with av.open(path) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout=stream.layout.name, rate=stream.rate)
        chunks = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray())
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray())
        rate = stream.rate
    data = np.concatenate(chunks, axis=-1).astype(np.float32)  # [channels, samples]
    return {"waveform": torch.from_numpy(data)[None], "sample_rate": int(rate)}


def _load_take_latent(take_dir: str) -> tuple[torch.Tensor, torch.Tensor]:
    from safetensors.torch import load_file

    tensors = load_file(str(Path(take_dir) / LATENT_FILE))
    return tensors["video"], tensors["audio"]


def _empty_av_latent(width: int, height: int, frames: int) -> dict:
    device = comfy.model_management.intermediate_device()
    video = torch.zeros((1, 24, video_latent_t(frames), height // 16, width // 16), device=device)
    audio = torch.zeros((1, 32, 2, audio_latent_t(frames)), device=device)
    return {"samples": comfy.nested_tensor.NestedTensor((video, audio))}


def _tail_context(take_dir: str, context_frames: int, width: int, height: int):
    video, audio = _load_take_latent(take_dir)
    slots = context_slots(context_frames)
    if tuple(video.shape[-2:]) != (height // 16, width // 16):
        raise ValueError(
            f"parent take {take_dir} is {video.shape[-1] * 16}x{video.shape[-2] * 16}; "
            f"this scene is {width}x{height} — continuation needs the same size"
        )
    ticks = audio_latent_t(context_frames)
    return video[:, :, -slots:].contiguous(), audio[..., -ticks:].contiguous()


def _head_context(take_dir: str, context_frames: int):
    video, audio = _load_take_latent(take_dir)
    return video[:, :, : context_slots(context_frames)].contiguous(), audio[..., : audio_latent_t(context_frames)].contiguous()


# ---------------------------------------------------------------- scene conditioning

class ClapperSceneConditioning:
    """Prompt + start image + per-scene references + continuation -> conditioning and target latent."""

    CATEGORY = "Clapper"
    RETURN_TYPES = ("CONDITIONING", "LATENT", "STRING")
    RETURN_NAMES = ("positive", "latent", "info")
    FUNCTION = "build"
    DESCRIPTION = "Builds one Clapper take's conditioning. `spec` is JSON written by the Director."

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "clip": ("CLIP",),
                "vae": ("VAE",),
                "audio_vae": ("VAE",),
                "prompt": ("STRING", {"multiline": True}),
                "width": ("INT", {"default": 416, "min": 32, "max": 4096, "step": 32}),
                "height": ("INT", {"default": 736, "min": 32, "max": 4096, "step": 32}),
                "frames": ("INT", {"default": 124, "min": 5, "max": 3600, "step": 17}),
                "spec": ("STRING", {"multiline": True, "default": "{}"}),
            }
        }

    def build(self, clip, vae, audio_vae, prompt, width, height, frames, spec):
        spec = json.loads(spec or "{}")
        info = []
        presentation = []   # what the vision-language text encoder sees, in <Picture/Audio N> order
        ref_blocks = []     # reference latents for the DiT
        keyframes = []

        start = spec.get("start_image") or {}
        if start.get("path"):
            img = _resize(_load_image(start["path"])[:1], width, height, "disabled")
            if start.get("present", True):
                presentation.append({"type": "image", "data": img})
            if start.get("pin", True):
                keyframes.append({"resolved_frame_index": 0, "latent": vae.encode(img)})
            info.append(f"start image: pin={start.get('pin', True)} present={start.get('present', True)}")

        size_mode = spec.get("ref_image_size", "match")
        for path in spec.get("ref_images") or []:
            img = _load_image(path)
            h, w = img.shape[1], img.shape[2]
            if size_mode == "match":
                scale = min(1.0, math.sqrt((width * height) / (w * h)))
            else:
                scale = min(1.0, REF_IMAGE_SHORT_EDGE / min(w, h))
            tw = max(CANVAS_MULTIPLE, round(w * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            th = max(CANVAS_MULTIPLE, round(h * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            resized = _resize(img[:1], tw, th, "disabled")
            presentation.append({"type": "image", "data": resized})
            ref_blocks.append({"kind": "image", "latent_h": th // 16, "latent_w": tw // 16, "latent": vae.encode(resized)})
        for path in spec.get("ref_audios") or []:
            audio_latent, ref_t = _encode_ref_audio(audio_vae, _load_audio(path))
            presentation.append({"type": "audio"})
            ref_blocks.append({"kind": "audio", "ref_audio_t": ref_t, "audio_latent": audio_latent})
        if ref_blocks:
            info.append(f"references: {sum(b['kind'] == 'image' for b in ref_blocks)} image(s), "
                        f"{sum(b['kind'] == 'audio' for b in ref_blocks)} audio")

        latent = _empty_av_latent(width, height, frames)
        cont = spec.get("continue_from") or {}
        if cont.get("take_dir"):
            ctx_frames = int(cont.get("frames", 22))
            video_ctx, audio_ctx = _tail_context(cont["take_dir"], ctx_frames, width, height)
            method = cont.get("method", "masked")
            if method == "masked":
                latent = _masked_prefix_latent(latent, video_ctx, audio_ctx if cont.get("audio", True) else None, ctx_frames)
                keyframes = [k for k in keyframes if k["resolved_frame_index"] != 0]
            elif method == "keyframe":
                keyframes = [k for k in keyframes if k["resolved_frame_index"] != 0]
                kf = {"resolved_frame_index": 0, "latent": video_ctx}
                if cont.get("audio", True):
                    kf["audio_latent"] = audio_ctx
                keyframes.append(kf)
            else:
                raise ValueError(f"unknown continuation method {method!r}")
            info.append(f"continue: {method}, {ctx_frames} frames from {Path(cont['take_dir']).name}")

        bridge = spec.get("bridge_to") or {}
        if bridge.get("take_dir"):
            ctx_frames = int(bridge.get("frames", 22))
            video_ctx, audio_ctx = _head_context(bridge["take_dir"], ctx_frames)
            kf = {"resolved_frame_index": frames - ctx_frames, "latent": video_ctx}
            if bridge.get("audio", True):
                kf["audio_latent"] = audio_ctx
            keyframes.append(kf)
            info.append(f"bridge: last {ctx_frames} frames pinned to start of {Path(bridge['take_dir']).name}")

        if presentation and (ref_blocks or any(p["type"] != "image" for p in presentation)):
            tokens = clip.tokenize(prompt, minimax_ref_items=presentation)
        else:
            tokens = clip.tokenize(prompt, images=[p["data"] for p in presentation])
        cond = clip.encode_from_tokens_scheduled(tokens)
        values = {}
        if ref_blocks:
            values["minimax_refs"] = ref_blocks
        if keyframes:
            values["minimax_keyframes"] = keyframes
        if values:
            cond = node_helpers.conditioning_set_values(cond, values)
        info.append(f"{width}x{height}, {frames} frames ({frames / 24:.2f}s)")
        return (cond, latent, "\n".join(info))


def _masked_prefix_latent(target: dict, video_ctx: torch.Tensor, audio_ctx, ctx_frames: int) -> dict:
    video, audio = target["samples"].unbind()
    slots = context_slots(ctx_frames)
    if video.shape[2] <= slots:
        raise ValueError("scene is too short to hold the continuation context")
    video = video.clone()
    audio = audio.clone()
    video[:, :, :slots] = video_ctx.to(video.device, video.dtype)
    video_mask = torch.ones((video.shape[0], 1, *video.shape[2:]), device=video.device)
    video_mask[:, :, :slots] = 0.0
    audio_mask = torch.ones((audio.shape[0], 1, *audio.shape[2:]), device=audio.device)
    if audio_ctx is not None:
        ticks = audio_ctx.shape[-1]
        audio[..., :ticks] = audio_ctx.to(audio.device, audio.dtype)
        audio_mask[..., :ticks] = 0.0
    out = dict(target)
    out["samples"] = comfy.nested_tensor.NestedTensor((video, audio))
    out["noise_mask"] = comfy.nested_tensor.NestedTensor((video_mask, audio_mask))
    out["clapper_prefix"] = {"slots": slots, "audio_ticks": 0 if audio_ctx is None else int(audio_ctx.shape[-1])}
    return out


# ---------------------------------------------------------------- save take

class ClapperSaveTake:
    """Restores a masked prefix, saves raw latents, decodes, and writes the take's mp4 + meta."""

    CATEGORY = "Clapper"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("take_dir",)
    FUNCTION = "save"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "samples": ("LATENT",),
                "target": ("LATENT",),
                "vae": ("VAE",),
                "audio_vae": ("VAE",),
                "take_dir": ("STRING", {"default": ""}),
                "meta": ("STRING", {"multiline": True, "default": "{}"}),
            }
        }

    def save(self, samples, target, vae, audio_vae, take_dir, meta):
        from safetensors.torch import save_file

        started = time.time()
        out_dir = Path(take_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        video, audio = samples["samples"].unbind()
        prefix = target.get("clapper_prefix")
        if prefix:  # re-apply the protected prefix exactly (sampler integration can round it)
            t_video, t_audio = target["samples"].unbind()
            video = video.clone()
            video[:, :, : prefix["slots"]] = t_video[:, :, : prefix["slots"]].to(video.device, video.dtype)
            if prefix["audio_ticks"]:
                audio = audio.clone()
                audio[..., : prefix["audio_ticks"]] = t_audio[..., : prefix["audio_ticks"]].to(audio.device, audio.dtype)

        save_file({"video": video.detach().float().cpu().contiguous(),
                   "audio": audio.detach().float().cpu().contiguous()}, str(out_dir / LATENT_FILE))

        images = vae.decode(video)
        if images.ndim == 5:
            images = images.reshape(-1, *images.shape[-3:])
        decoded = vae_decode_audio(audio_vae, {"samples": comfy.nested_tensor.NestedTensor((video, audio))})
        wav_path = out_dir / "audio.wav"
        _write_wav(wav_path, decoded["waveform"][0], decoded["sample_rate"])
        mp4_path = out_dir / "take.mp4"
        _write_mp4(mp4_path, images, wav_path)

        info = json.loads(meta or "{}")
        info.update({
            "frames": int(images.shape[0]),
            "width": int(images.shape[2]),
            "height": int(images.shape[1]),
            "prefix": prefix or None,
            "files": {"video": mp4_path.name, "audio": wav_path.name, "latent": LATENT_FILE},
            "saved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "save_seconds": round(time.time() - started, 2),
        })
        tmp = out_dir / "meta.json.tmp"
        tmp.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, out_dir / "meta.json")
        return {"ui": {"text": [str(out_dir)]}, "result": (str(out_dir),)}


def _write_wav(path: Path, waveform: torch.Tensor, rate: int) -> None:
    data = waveform.detach().float().cpu().clamp(-1, 1).numpy()  # [C, L]
    pcm = (data.T * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(data.shape[0])
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm.tobytes())


def _write_mp4(path: Path, images: torch.Tensor, wav_path: Path) -> None:
    frames = (images.detach().float().cpu().clamp(0, 1) * 255.0).round().to(torch.uint8).numpy()
    n, h, w, _ = frames.shape
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", "24",
           "-i", "-", "-i", str(wav_path), "-c:v", "libx264", "-crf", "16", "-preset", "medium",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", str(path)]
    proc = subprocess.run(cmd, input=frames.tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[-800:]}")


NODE_CLASS_MAPPINGS = {
    "ClapperSceneConditioning": ClapperSceneConditioning,
    "ClapperSaveTake": ClapperSaveTake,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "ClapperSceneConditioning": "Clapper · Scene Conditioning",
    "ClapperSaveTake": "Clapper · Save Take",
}
