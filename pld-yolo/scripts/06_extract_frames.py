"""Extract uniformly-sampled frames from a video for hand labeling.

Lives in its own dataset root (default ``data/airpelago/``) so it never gets
mixed up with the PLDM YOLO tree.

Output layout::

    data/airpelago/
        frames/          downsized JPEGs ready to label
        classes.txt      labelme label list (edit if you add classes)
        yolo/            populated later by 07_labelme_to_yolo.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "airpelago"

DEFAULT_LABELS = [
    "__ignore__",   # required by labelme
    "_background_", # required by labelme
    "insulator",
    "pole",
]


def extract(
    video: Path,
    out_dir: Path,
    every_n: int,
    target_w: int,
    target_h: int,
    quality: int,
) -> int:
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        cap.release()
        raise RuntimeError(f"could not read frame count from {video}")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        if idx % every_n == 0:
            h, w = frame.shape[:2]
            if (w, h) != (target_w, target_h):
                # Default: simple resize to target; preserves the source 16:9
                # so don't pass mismatched aspect unless you mean it.
                frame = cv2.resize(
                    frame, (target_w, target_h), interpolation=cv2.INTER_AREA
                )
            name = out_dir / f"frame_{idx:06d}.jpg"
            cv2.imwrite(str(name), frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            written += 1
        idx += 1
    cap.release()
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--every-n", type=int, default=10,
                    help="Keep every Nth frame. 10 @ 25fps -> 2.5 fps sampling.")
    ap.add_argument("--width", type=int, default=960)
    ap.add_argument("--height", type=int, default=540)
    ap.add_argument("--quality", type=int, default=92)
    args = ap.parse_args()

    video = Path(args.video).resolve()
    if not video.exists():
        print(f"video not found: {video}", file=sys.stderr)
        return 1

    out_root = Path(args.out).resolve()
    frames_dir = out_root / "frames"
    classes_file = out_root / "classes.txt"

    print(f"video:       {video}")
    print(f"out dir:     {out_root}")
    print(f"every-n:     {args.every_n}")
    print(f"frame size:  {args.width}x{args.height}")

    n = extract(
        video=video,
        out_dir=frames_dir,
        every_n=args.every_n,
        target_w=args.width,
        target_h=args.height,
        quality=args.quality,
    )
    print(f"wrote {n} frames -> {frames_dir}")

    if not classes_file.exists():
        classes_file.write_text("\n".join(DEFAULT_LABELS) + "\n")
        print(f"wrote default class list -> {classes_file}")
    else:
        print(f"kept existing class list -> {classes_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
