#!/usr/bin/env python3
"""Create tiny local sample MP4s for examples/comfy_batch_manifest.jsonl."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SAMPLES = [
    ("steps8_seed101.mp4", "#201040", "steps=8 seed=101"),
    ("steps8_seed202.mp4", "#402010", "steps=8 seed=202"),
    ("steps16_seed101.mp4", "#104020", "steps=16 seed=101"),
    ("steps16_seed202.mp4", "#404010", "steps=16 seed=202"),
]


def main() -> int:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is required to create sample media")
    media_dir = Path(__file__).resolve().parent / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    for filename, color, text in SAMPLES:
        output = media_dir / filename
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=256x256:d=1:r=12",
                "-vf",
                f"drawtext=text='{text}':fontcolor=white:fontsize=18:x=(w-text_w)/2:y=(h-text_h)/2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(output),
            ],
            check=True,
        )
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
