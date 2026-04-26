"""Run a trained YOLO-seg model on images / videos and save annotated output.

Examples:
    python scripts/04_predict.py --source data/yolo_pldm/images/val
    python scripts/04_predict.py --source path/to/uav_clip.mp4 --weights runs/pldm-yolo26s-seg/weights/best.pt
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"


def find_latest_weights() -> Path | None:
    """Pick the most recently modified ``best.pt`` (or fall back to ``last.pt``)
    under ``runs/``. Useful while training is still in progress.
    """
    candidates: list[Path] = []
    for run in RUNS_DIR.glob("*/weights"):
        for name in ("best.pt", "last.pt"):
            p = run / name
            if p.exists():
                candidates.append(p)
                break  # prefer best.pt over last.pt within a run
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--weights", default=None,
                    help="Path to .pt file. Default: most recent best.pt/last.pt under runs/.")
    ap.add_argument("--source", required=True,
                    help="Path to image, video, directory, or glob.")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--device", default=None)
    ap.add_argument("--name", default="predict")
    args = ap.parse_args()

    weights = Path(args.weights) if args.weights else find_latest_weights()
    if weights is None or not weights.exists():
        raise SystemExit("no weights found. Train first via scripts/03_train.py "
                         "or pass --weights explicitly.")
    print(f"Using weights: {weights}")

    from ultralytics import YOLO

    model = YOLO(str(weights))
    model.predict(
        source=args.source,
        imgsz=args.imgsz,
        conf=args.conf,
        iou=args.iou,
        device=args.device,
        save=True,
        save_txt=False,
        project=str(ROOT / "runs"),
        name=args.name,
        exist_ok=True,
        line_width=2,
    )
    print(f"Saved predictions under {ROOT / 'runs' / args.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
