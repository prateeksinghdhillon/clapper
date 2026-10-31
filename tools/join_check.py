#!/usr/bin/env python3
"""Stitch two takes like the Director export does and measure the join.

usage: join_check.py TAKE_A TAKE_B OUT_DIR
Writes OUT_DIR/joined.mp4 and OUT_DIR/join_strip.png (last 3 frames of A | first 3 kept frames of B),
and prints the mean absolute pixel difference across the join vs. inside each take.
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

FFMPEG = "ffmpeg"


def frames(mp4: Path, w: int, h: int) -> np.ndarray:
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(mp4), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)


def main(a: str, b: str, out: str) -> None:
    a, b, out = Path(a), Path(b), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    ma, mb = (json.loads((p / "meta.json").read_text()) for p in (a, b))
    ctx = int(mb.get("context_frames", 0))
    fa, fb = frames(a / "take.mp4", ma["width"], ma["height"]), frames(b / "take.mp4", mb["width"], mb["height"])
    kept = fb[ctx:]
    diff = lambda x, y: float(np.abs(x.astype(np.int16) - y.astype(np.int16)).mean())  # noqa: E731
    across = diff(fa[-1], kept[0])
    inside_a = np.mean([diff(fa[i], fa[i + 1]) for i in range(len(fa) - 10, len(fa) - 1)])
    inside_b = np.mean([diff(kept[i], kept[i + 1]) for i in range(0, 9)])
    overlap = np.mean([diff(fa[-ctx + i], fb[i]) for i in range(ctx)]) if ctx else float("nan")
    print(f"frame-to-frame change: across join {across:.2f} | last frames of A {inside_a:.2f} | first kept frames of B {inside_b:.2f}")
    print(f"carried overlap: B's first {ctx} frames vs A's last {ctx} frames, mean abs diff {overlap:.2f} (0 = identical)")
    strip = np.concatenate([*fa[-3:], np.full((ma["height"], 8, 3), 255, np.uint8), *kept[:3]], axis=1)
    Image.fromarray(strip).save(out / "join_strip.png")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(a / "take.mp4"), "-i", str(b / "take.mp4"), "-filter_complex",
                    f"[0:v]setpts=PTS-STARTPTS[v0];[1:v]trim=start_frame={ctx},setpts=PTS-STARTPTS[v1];"
                    f"[0:a]asetpts=PTS-STARTPTS[a0];[1:a]atrim=start={ctx / 24:.6f},asetpts=PTS-STARTPTS[a1];"
                    "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]", "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out / "joined.mp4")], check=True)
    print(f"wrote {out / 'joined.mp4'} and {out / 'join_strip.png'}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
