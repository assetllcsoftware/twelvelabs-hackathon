"""Train a YOLO-seg model on the converted PLDM dataset.

Defaults to ``yolo26s-seg.pt`` (Ultralytics, released Jan 2026). Override with
``--model yolo11s-seg.pt`` if the YOLO26 weights are not yet available in your
``ultralytics`` install.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Use the auto-generated absolute-path YAML written by 02_convert_to_yolo.py.
# Falls back to configs/pldm.yaml if the user wants to hand-edit.
DATA_CFG = ROOT / "data" / "yolo_pldm" / "data.yaml"
DATA_CFG_FALLBACK = ROOT / "configs" / "pldm.yaml"
RUNS_DIR = ROOT / "runs"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="yolo26s-seg.pt")
    ap.add_argument("--data", default=str(DATA_CFG if DATA_CFG.exists() else DATA_CFG_FALLBACK))
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--imgsz", type=int, default=640,
                    help="Higher (e.g. 960) helps thin lines but is slower.")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--device", default=None,
                    help='e.g. "0", "0,1", or "cpu". Defaults to ultralytics auto.')
    ap.add_argument("--name", default="pldm-yolo26s-seg")
    ap.add_argument("--patience", type=int, default=25)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--heavy-aug", action="store_true",
                    help="Aggressive augmentation preset for use with --use-augmented "
                         "data conversion (anti-overfit when train set is large).")
    ap.add_argument("--resume", action="store_true",
                    help="Resume from runs/<name>/weights/last.pt (preserves "
                         "epoch counter, optimizer state, lr schedule). All "
                         "other args except --device are taken from the "
                         "original run.")
    args = ap.parse_args()

    from ultralytics import YOLO

    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    if args.resume:
        last_pt = RUNS_DIR / args.name / "weights" / "last.pt"
        if not last_pt.exists():
            print(f"[error] cannot resume; no checkpoint at {last_pt}",
                  file=sys.stderr)
            return 1
        print(f"resuming from:  {last_pt}\n")
        model = YOLO(str(last_pt))
        model.train(resume=True, device=args.device)
        return 0

    # Augmentations: power lines are line-symmetric and mostly orientation
    # invariant -- rotations and flips are safe and helpful. The heavy preset
    # is intended for the ~11k augmented dataset.
    if args.heavy_aug:
        aug = dict(
            degrees=30.0, translate=0.15, scale=0.6,
            fliplr=0.5, flipud=0.5,                      # vertical flips OK for wires
            mosaic=1.0, mixup=0.15, copy_paste=0.3,
            hsv_h=0.02, hsv_s=0.8, hsv_v=0.5,
            erasing=0.4,
        )
    else:
        aug = dict(
            degrees=15.0, translate=0.1, scale=0.4,
            fliplr=0.5, flipud=0.0,
            mosaic=1.0,
        )

    model = YOLO(args.model)
    model.train(
        data=args.data,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        project=str(RUNS_DIR),
        name=args.name,
        seed=args.seed,
        patience=args.patience,
        # Mask loss weight bumped up since lines are skinny -- box loss alone
        # is a poor signal here.
        box=5.0,
        cls=0.5,
        dfl=1.5,
        single_cls=True,
        **aug,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
