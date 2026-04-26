"""Convert PLDM raw images + ground-truth masks to YOLO-seg format.

Input layout (from the SnorkerHeng/PLD-UAV release):

    data/raw/PLDM/
        train/
            aug_data/<rot>_<flip>/<id>.jpg              # scale 1.0
            aug_gt/<rot>_<flip>/<id>.png                # filled binary mask
            aug_data_scale_{0.5,1.5}/<rot>_<flip>/...
            aug_gt_scale_{0.5,1.5}/<rot>_<flip>/...
        test/<id>.jpg                                   # 50 unaugmented images
        test_gt/<id>.mat                                # BSDS Boundaries map

Output:  data/yolo_pldm/{images,labels}/{train,val}/...

Each connected component in the mask becomes one ``power_line`` instance,
written as a YOLO-seg polygon line:

    0 x1 y1 x2 y2 x3 y3 ...     (coords normalized to [0, 1])

Default mode trains on the 237 unaugmented originals and validates on the 50
test images. Pass ``--use-augmented`` to train on all 237 * 16 * 3 = 11,376
augmented variants (val stays at the 50 test originals). The native
train/test split is preserved so val never shares scenes with train.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
import scipy.io as sio
from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "PLDM"
OUT = ROOT / "data" / "yolo_pldm"

CLASS_ID = 0

MIN_INSTANCE_AREA_PX = 6
MIN_CONTOUR_AREA_PX = 4.0
SIMPLIFY_EPSILON_PX = 0.75
MAX_POINTS_PER_POLY = 256

# Rotations/flips and scale subdirs found in train/.
ROT_FLIP_DIRS = [
    f"{rot}_{flip}"
    for rot in (0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0)
    for flip in (0, 1)
]
SCALE_SUFFIXES = ["", "_scale_0.5", "_scale_1.5"]


def load_png_mask(path: Path) -> np.ndarray | None:
    """Load a filled binary mask PNG -> (H, W) uint8 with values {0, 1}."""
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.ndim == 3:
        if img.shape[2] == 4:
            img = img[..., 3]
        else:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return (img > 127).astype(np.uint8)


def load_mat_mask(path: Path, dilate_iter: int = 1) -> np.ndarray | None:
    """Load a BSDS-format .mat ground truth -> (H, W) uint8 with values {0, 1}.

    The PLDM .mat files store ``groundTruth[0,0][0,0]['Boundaries']`` as a
    transposed (W, H) uint8 binary edge map. We transpose to (H, W) and
    optionally dilate so YOLO sees a stroke rather than a 1-pixel skeleton.
    """
    try:
        d = sio.loadmat(str(path))
    except Exception as e:
        print(f"[warn] could not load {path.name}: {e}", file=sys.stderr)
        return None
    try:
        boundaries = d["groundTruth"][0, 0][0, 0]["Boundaries"]
    except Exception as e:
        print(f"[warn] unexpected .mat structure in {path.name}: {e}", file=sys.stderr)
        return None
    mask = np.asarray(boundaries).T.astype(np.uint8)
    mask = (mask > 0).astype(np.uint8)
    if dilate_iter > 0:
        mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=dilate_iter)
    return mask


def mask_to_polygons(mask: np.ndarray, img_w: int, img_h: int) -> list[list[float]]:
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_TC89_L1)
    polys: list[list[float]] = []
    for c in contours:
        if cv2.contourArea(c) < MIN_CONTOUR_AREA_PX and len(c) < 6:
            continue
        approx = cv2.approxPolyDP(c, SIMPLIFY_EPSILON_PX, closed=True).reshape(-1, 2)
        if len(approx) < 3:
            continue
        if len(approx) > MAX_POINTS_PER_POLY:
            idx = np.linspace(0, len(approx) - 1, MAX_POINTS_PER_POLY).astype(int)
            approx = approx[idx]
        norm = approx.astype(np.float64).copy()
        norm[:, 0] /= img_w
        norm[:, 1] /= img_h
        norm = np.clip(norm, 0.0, 1.0)
        polys.append(norm.flatten().tolist())
    return polys


def mask_to_yolo_lines(mask: np.ndarray) -> list[str]:
    h, w = mask.shape
    n_labels, labels = cv2.connectedComponents(mask, connectivity=8)
    lines: list[str] = []
    for cid in range(1, n_labels):
        comp = (labels == cid).astype(np.uint8)
        if int(comp.sum()) < MIN_INSTANCE_AREA_PX:
            continue
        for poly in mask_to_polygons(comp, w, h):
            coords = " ".join(f"{v:.6f}" for v in poly)
            lines.append(f"{CLASS_ID} {coords}")
    return lines


def link_or_copy(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src.resolve(), dst)
    except OSError:
        shutil.copy2(src, dst)


def gather_train_pairs(use_augmented: bool) -> list[tuple[Path, Path, str]]:
    """Return list of (image_path, mask_path, unique_stem) for the train split."""
    pairs: list[tuple[Path, Path, str]] = []
    for scale in (SCALE_SUFFIXES if use_augmented else [""]):
        img_root = RAW / "train" / f"aug_data{scale}"
        gt_root = RAW / "train" / f"aug_gt{scale}"
        if not img_root.exists() or not gt_root.exists():
            continue
        rot_dirs = ROT_FLIP_DIRS if use_augmented else ["0.0_0"]
        for rd in rot_dirs:
            img_dir = img_root / rd
            gt_dir = gt_root / rd
            if not img_dir.exists() or not gt_dir.exists():
                continue
            for img_p in sorted(img_dir.glob("*.jpg")):
                gt_p = gt_dir / f"{img_p.stem}.png"
                if not gt_p.exists():
                    continue
                # Make a unique stem so images from different aug variants
                # don't collide in the flat output dir.
                tag_scale = scale.replace("_scale_", "s") or "s1.0"
                unique_stem = f"{img_p.stem}__{rd}{tag_scale}"
                pairs.append((img_p, gt_p, unique_stem))
    return pairs


def gather_val_pairs() -> list[tuple[Path, Path, str]]:
    """Return list of (image_path, mat_mask_path, stem) for the val split."""
    img_dir = RAW / "test"
    gt_dir = RAW / "test_gt"
    pairs: list[tuple[Path, Path, str]] = []
    if not img_dir.exists() or not gt_dir.exists():
        return pairs
    for img_p in sorted(img_dir.glob("*.jpg")):
        gt_p = gt_dir / f"{img_p.stem}.mat"
        if not gt_p.exists():
            continue
        pairs.append((img_p, gt_p, img_p.stem))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--use-augmented", action="store_true",
                    help="Train on all 11,376 augmented variants (default: 237 originals).")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--limit-train", type=int, default=0,
                    help="Optional cap on number of train images (0 = no limit).")
    ap.add_argument("--val-dilate", type=int, default=1,
                    help="Pixels of dilation applied to val .mat boundary maps.")
    args = ap.parse_args()

    if not RAW.exists():
        print(f"[error] {RAW} not found. Run scripts/01_download.py first.", file=sys.stderr)
        return 1

    train_pairs = gather_train_pairs(args.use_augmented)
    val_pairs = gather_val_pairs()

    if not train_pairs:
        print("[error] no train pairs found.", file=sys.stderr)
        return 1
    if not val_pairs:
        print("[error] no val pairs found.", file=sys.stderr)
        return 1

    if args.limit_train and args.limit_train < len(train_pairs):
        rng = random.Random(args.seed)
        train_pairs = rng.sample(train_pairs, args.limit_train)

    print(f"train pairs: {len(train_pairs)}    val pairs: {len(val_pairs)}")

    if OUT.exists():
        shutil.rmtree(OUT)
    for sub in ("images/train", "images/val", "labels/train", "labels/val"):
        (OUT / sub).mkdir(parents=True, exist_ok=True)

    n_train_obj = 0
    n_val_obj = 0
    n_skipped = 0

    for img_p, gt_p, stem in tqdm(train_pairs, desc="convert train"):
        mask = load_png_mask(gt_p)
        if mask is None:
            n_skipped += 1
            continue
        lines = mask_to_yolo_lines(mask)
        img_dst = OUT / "images" / "train" / f"{stem}{img_p.suffix}"
        lbl_dst = OUT / "labels" / "train" / f"{stem}.txt"
        link_or_copy(img_p, img_dst)
        lbl_dst.write_text("\n".join(lines) + ("\n" if lines else ""))
        n_train_obj += len(lines)

    for img_p, gt_p, stem in tqdm(val_pairs, desc="convert val"):
        mask = load_mat_mask(gt_p, dilate_iter=args.val_dilate)
        if mask is None:
            n_skipped += 1
            continue
        # Sanity: val mask shape must match the image shape.
        im = cv2.imread(str(img_p))
        if im is None:
            n_skipped += 1
            continue
        if mask.shape[:2] != im.shape[:2]:
            mask = cv2.resize(mask, (im.shape[1], im.shape[0]),
                              interpolation=cv2.INTER_NEAREST)
        lines = mask_to_yolo_lines(mask)
        img_dst = OUT / "images" / "val" / f"{stem}{img_p.suffix}"
        lbl_dst = OUT / "labels" / "val" / f"{stem}.txt"
        link_or_copy(img_p, img_dst)
        lbl_dst.write_text("\n".join(lines) + ("\n" if lines else ""))
        n_val_obj += len(lines)

    summary = {
        "use_augmented": args.use_augmented,
        "seed": args.seed,
        "n_train_images": len(train_pairs),
        "n_val_images": len(val_pairs),
        "n_train_instances": n_train_obj,
        "n_val_instances": n_val_obj,
        "n_skipped": n_skipped,
    }
    (OUT / "dataset_stats.json").write_text(json.dumps(summary, indent=2))

    # Write an absolute-path data.yaml so ultralytics doesn't resolve it
    # against its (possibly stale) global ``datasets_dir`` setting.
    yaml_text = (
        f"# Auto-generated by 02_convert_to_yolo.py -- DO NOT EDIT BY HAND.\n"
        f"path: {OUT.resolve()}\n"
        f"train: images/train\n"
        f"val: images/val\n\n"
        f"names:\n"
        f"  0: power_line\n"
    )
    (OUT / "data.yaml").write_text(yaml_text)

    print("\n=== conversion summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"\nWrote dataset to: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
