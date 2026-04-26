"""Train a YOLO-seg model on the hand-labeled Airpelago demo data.

This is the *second* YOLO in the project; the first (PLDM) lives under
``data/yolo_pldm/`` and is trained by ``scripts/03_train.py``. The two
datasets/models are kept entirely separate.

Key differences vs. 03_train.py:
  - 2 real classes (insulator, pole), so ``single_cls=False``.
  - Inputs are 960x540 (16:9), so ``imgsz=640`` is a closer fit than 480.
  - Insulators/poles are upright structural objects: no vertical flip,
    only mild rotation.
  - ``mixup`` and very high ``copy_paste`` would smear hard structural
    silhouettes, so the heavy preset here is more conservative than PLDM's.
  - More epochs (50) by default since the labeled set is tiny.

Usage:
    pipenv run python scripts/08_train_airpelago.py
    pipenv run python scripts/08_train_airpelago.py --heavy-aug --epochs 80
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_CFG = ROOT / "data" / "airpelago" / "yolo" / "data.yaml"
RUNS_DIR = ROOT / "runs"
MIN_TRAIN_IMAGES = 8  # below this, training is a smoke test, not a model


def count_train_images(yolo_root: Path) -> int:
    train_dir = yolo_root / "images" / "train"
    if not train_dir.exists():
        return 0
    return sum(1 for _ in train_dir.glob("*.jpg"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="yolo26s-seg.pt",
                    help="Pretrained backbone. Falls back to yolo11s-seg.pt "
                         "if yolo26 weights aren't bundled.")
    ap.add_argument("--data", default=str(DATA_CFG))
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--workers", type=int, default=4,
                    help="Dataloader workers. Set to 0 when running alongside "
                         "another training job to avoid thread thrash.")
    ap.add_argument("--device", default=None,
                    help='e.g. "0", "0,1", "cpu". Defaults to ultralytics auto.')
    ap.add_argument("--name", default="airpelago-yolo26s-seg")
    ap.add_argument("--patience", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--heavy-aug", action="store_true",
                    help="More aggressive augmentation. Useful past ~50 labeled "
                         "frames.")
    ap.add_argument("--no-pretrained", action="store_true",
                    help="Train from scratch (rarely a good idea on small data).")
    ap.add_argument("--allow-tiny", action="store_true",
                    help=f"Skip the {MIN_TRAIN_IMAGES}-image safety check.")
    ap.add_argument("--resume", action="store_true",
                    help="Resume from runs/<name>/weights/last.pt (keeps "
                         "optimizer state, epoch counter, lr schedule). All "
                         "other args except --device are taken from the "
                         "original run.")
    args = ap.parse_args()

    data_cfg = Path(args.data).resolve()
    if not data_cfg.exists():
        print(f"[error] data config not found: {data_cfg}", file=sys.stderr)
        print("        Run scripts/07_labelme_to_yolo.py first.", file=sys.stderr)
        return 1

    yolo_root = data_cfg.parent
    n_train = count_train_images(yolo_root)
    print(f"data:           {data_cfg}")
    print(f"train images:   {n_train}")
    if n_train < MIN_TRAIN_IMAGES and not args.allow_tiny:
        print(f"[error] only {n_train} train images. Pass --allow-tiny to "
              "force a smoke-test run, or label more frames first.",
              file=sys.stderr)
        return 1

    # Augmentations:
    #   - flipud=0  -> poles point up; vertical flips are unrealistic.
    #   - degrees   -> small only; structures are near-vertical/horizontal.
    #   - mixup=0   -> mixing two transmission scenes blurs hard silhouettes.
    #   - copy_paste mild on heavy preset (insulators paste OK; poles less so).
    if args.heavy_aug:
        aug = dict(
            degrees=10.0, translate=0.1, scale=0.5,
            fliplr=0.5, flipud=0.0,
            mosaic=1.0, mixup=0.0, copy_paste=0.2,
            hsv_h=0.015, hsv_s=0.7, hsv_v=0.4,
            erasing=0.2,
        )
    else:
        aug = dict(
            degrees=5.0, translate=0.05, scale=0.3,
            fliplr=0.5, flipud=0.0,
            mosaic=0.8, mixup=0.0, copy_paste=0.0,
            hsv_h=0.01, hsv_s=0.5, hsv_v=0.3,
        )

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
        # When resume=True, ultralytics replays the args from the original run
        # (epochs, imgsz, augmentation, etc.). Don't pass them again.
        model.train(resume=True, device=args.device)
        return 0

    model_arg = args.model
    if args.no_pretrained:
        # Loading the .yaml gives an untrained backbone of the same shape.
        model_arg = args.model.replace(".pt", ".yaml")

    print(f"\nmodel:          {model_arg}")
    print(f"epochs:         {args.epochs}")
    print(f"imgsz:          {args.imgsz}")
    print(f"batch:          {args.batch}")
    print(f"heavy-aug:      {args.heavy_aug}")
    print(f"output:         {RUNS_DIR / args.name}\n")

    model = YOLO(model_arg)
    model.train(
        data=str(data_cfg),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        workers=args.workers,
        device=args.device,
        project=str(RUNS_DIR),
        name=args.name,
        seed=args.seed,
        patience=args.patience,
        # Two real classes -- DON'T collapse to single_cls like PLDM does.
        single_cls=False,
        # Standard loss weights -- the PLDM script bumps box=5 because lines
        # are skinny and box loss alone is a poor signal there. Insulators and
        # poles are well-shaped objects, so default weighting is fine.
        box=7.5,
        cls=0.5,
        dfl=1.5,
        **aug,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
