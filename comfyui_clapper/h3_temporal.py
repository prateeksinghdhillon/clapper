"""MiniMax H3 frame/latent/audio grid helpers.

Adapted from ComfyUI-H3-Continuum (MIT, (c) 2026 ukr8b3g-cmyk), temporal.py.
Pure Python: safe to import without torch or ComfyUI.
"""
from __future__ import annotations

from fractions import Fraction

FPS = 24
AUDIO_LATENT_FPS = 40
FRAME_PER_TOKEN = (1, 4, 4, 4, 4)  # pixel frames carried by each video latent slot, repeating
_AUDIO_TICKS_PER_FRAME = Fraction(AUDIO_LATENT_FPS, FPS)

CONTEXT_FRAME_OPTIONS = (5, 22, 39)  # Continuum: Fast / Balanced / Strong


def is_valid_frame_count(frames: int) -> bool:
    return frames >= 5 and frames % 17 == 5


def align_frame_count_nearest(frames: int, *, minimum: int = 5) -> int:
    """Nearest valid 17k+5 frame count that is >= minimum (ties round down)."""
    target = max(int(frames), int(minimum), 5)
    k = max(0, (target - 5) // 17)
    low = 5 + 17 * k
    while low < minimum:
        low += 17
    high = low + 17
    return low if abs(target - low) <= abs(high - target) else high


def video_latent_t(frames: int) -> int:
    if not is_valid_frame_count(frames):
        raise ValueError(f"H3 frame count must be 17k+5; got {frames}")
    return 2 if frames <= 5 else ((frames - 5) // 17) * 5 + 2


def pixel_frames_for_latent_t(latent_t: int) -> int:
    return sum(FRAME_PER_TOKEN[i % 5] for i in range(int(latent_t)))


def context_slots(context_frames: int) -> int:
    for slots in range(1, 256):
        frames = pixel_frames_for_latent_t(slots)
        if frames == context_frames:
            return slots
        if frames > context_frames:
            break
    raise ValueError(f"context of {context_frames} frames is not representable; use 5, 22 or 39")


def audio_latent_t(frames: int) -> int:
    return int(round(Fraction(int(frames)) * _AUDIO_TICKS_PER_FRAME))


def scene_frame_count(seconds: float, *, context_frames: int = 0) -> int:
    """Total frames to sample for a scene of `seconds` new content after `context_frames` of carried context."""
    new = max(1, int(round(float(seconds) * FPS)))
    if context_frames:
        return align_frame_count_nearest(context_frames + new, minimum=context_frames + 5)
    return align_frame_count_nearest(new, minimum=5)
