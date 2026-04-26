"""Convert labelme JSON polygons to YOLO-seg labels.

Input layout (default)::

    data/airpelago/
        frames/<stem>.jpg
        frames/<stem>.json     # written by labelme next to each frame
        classes.txt            # __ignore__, _background_, then real classes

Output layout::

    data/airpelago/yolo/
        images/{train,val}/<stem>.jpg
        labels/{train,val}/<stem>.txt
        data.yaml              # absolute paths, class names

Design notes:
  - "__ignore__" and "_background_" are stripped before assigning class IDs.
  - Train/val split is per-image, deterministic (seed=42), default 90/10.
  - Pass --no-holdout for "fit-the-demo" mode: every labeled image goes into
    train, and a deterministic ~val_frac sample is mirrored into val so
    ultralytics can still compute mAP / pick best.pt. Train and val overlap.
  - Polygons must have >= 3 points. Rectangles, circles, lines etc. are skipped
    with a warning -- this script is segmentation-only on purpose.
  - Coords are normalized to [0, 1] using the image dims labelme stored in JSON.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "data" / "airpelago"


def load_classes(classes_file: Path) -> list[str]:
    raw = [
        ln.strip()
        for ln in classes_file.read_text().splitlines()
        if ln.strip()
    ]
    real = [c for c in raw if c not in ("__ignore__", "_background_")]
    if not real:
        raise SystemExit(f"no real classes in {classes_file}")
    return real


def find_pairs(frames_dir: Path) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for img in sorted(frames_dir.glob("*.jpg")):
        js = img.with_suffix(".json")
        if js.exists():
            pairs.append((img, js))
    return pairs


def polygon_to_yolo_line(
    points: list[list[float]],
    class_id: int,
    w: int,
    h: int,
) -> str | None:
    if len(points) < 3:
        return None
    coords = []
    for x, y in points:
        nx = max(0.0, min(1.0, x / w))
        ny = max(0.0, min(1.0, y / h))
        coords.extend([f"{nx:.6f}", f"{ny:.6f}"])
    return f"{class_id} " + " ".join(coords)


def convert(
    pairs: list[tuple[Path, Path]],
    classes: list[str],
    out_root: Path,
    val_frac: float,
    seed: int,
    no_holdout: bool = False,
) -> dict[str, int]:
    cls_to_id = {c: i for i, c in enumerate(classes)}

    rng = random.Random(seed)
    indices = list(range(len(pairs)))
    rng.shuffle(indices)
    n_val = max(1, round(len(indices) * val_frac)) if pairs else 0
    val_set = set(indices[:n_val])

    for sub in ("images/train", "images/val", "labels/train", "labels/val"):
        d = out_root / sub
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    counts = {"train_imgs": 0, "val_imgs": 0, "skipped": 0,
              "polys": 0, "non_poly_shapes": 0, "unknown_class": 0}

    # In no-holdout mode every image goes to train; the val subset is mirrored
    # alongside it so train and val overlap. Useful for fit-the-demo runs where
    # we don't care about generalization, only that best.pt picks something.
    for i, (img, js) in enumerate(pairs):
        if no_holdout:
            splits = ["train"] + (["val"] if i in val_set else [])
        else:
            splits = ["val" if i in val_set else "train"]
        try:
            data = json.loads(js.read_text())
        except Exception as e:
            print(f"[warn] {js.name}: bad json ({e}), skipping", file=sys.stderr)
            counts["skipped"] += 1
            continue

        w = int(data.get("imageWidth") or 0)
        h = int(data.get("imageHeight") or 0)
        if not w or not h:
            print(f"[warn] {js.name}: missing image size, skipping",
                  file=sys.stderr)
            counts["skipped"] += 1
            continue

        lines: list[str] = []
        for shape in data.get("shapes", []):
            label = shape.get("label", "").strip()
            stype = shape.get("shape_type", "polygon")
            if stype != "polygon":
                counts["non_poly_shapes"] += 1
                continue
            if label not in cls_to_id:
                counts["unknown_class"] += 1
                print(f"[warn] {js.name}: unknown label '{label}'",
                      file=sys.stderr)
                continue
            ln = polygon_to_yolo_line(
                shape.get("points", []), cls_to_id[label], w, h
            )
            if ln is None:
                continue
            lines.append(ln)
            counts["polys"] += 1

        for split in splits:
            img_dst = out_root / "images" / split / img.name
            lbl_dst = out_root / "labels" / split / f"{img.stem}.txt"
            try:
                img_dst.symlink_to(img.resolve())
            except (OSError, FileExistsError):
                shutil.copy2(img, img_dst)
            lbl_dst.write_text("\n".join(lines) + ("\n" if lines else ""))
            counts[f"{split}_imgs"] += 1

    yaml = (out_root / "data.yaml")
    yaml_text = (
        f"# auto-generated by scripts/07_labelme_to_yolo.py\n"
        f"path: {out_root.resolve()}\n"
        f"train: images/train\n"
        f"val: images/val\n"
        f"names:\n"
        + "".join(f"  {i}: {c}\n" for i, c in enumerate(classes))
    )
    yaml.write_text(yaml_text)

    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(DEFAULT_ROOT),
                    help="Project root containing frames/ and classes.txt.")
    ap.add_argument("--frames-dir", default=None,
                    help="Override location of frames+JSONs (default: <root>/frames).")
    ap.add_argument("--classes", default=None,
                    help="Override classes.txt path (default: <root>/classes.txt).")
    ap.add_argument("--out", default=None,
                    help="Override output dir (default: <root>/yolo).")
    ap.add_argument("--val-frac", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-holdout", action="store_true",
                    help="Fit-the-demo mode: every labeled image goes into "
                         "train, and a small val_frac sample is mirrored into "
                         "val (overlapping with train) so ultralytics can "
                         "still compute mAP and pick best.pt.")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    frames_dir = Path(args.frames_dir).resolve() if args.frames_dir else root / "frames"
    classes_file = Path(args.classes).resolve() if args.classes else root / "classes.txt"
    out_root = Path(args.out).resolve() if args.out else root / "yolo"

    if not frames_dir.exists():
        raise SystemExit(f"frames dir not found: {frames_dir}")
    if not classes_file.exists():
        raise SystemExit(f"classes file not found: {classes_file}")

    classes = load_classes(classes_file)
    pairs = find_pairs(frames_dir)
    if not pairs:
        raise SystemExit(f"no labelme JSONs found in {frames_dir}")

    print(f"classes:     {classes}")
    print(f"pairs found: {len(pairs)}")
    print(f"mode:        {'no-holdout (val overlaps train)' if args.no_holdout else f'{int((1-args.val_frac)*100)}/{int(args.val_frac*100)} disjoint split'}")
    counts = convert(pairs, classes, out_root, args.val_frac, args.seed,
                     no_holdout=args.no_holdout)
    print()
    print("=== conversion summary ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print(f"\nWrote dataset to: {out_root}")
    print(f"Train YOLO with:  pipenv run python scripts/03_train.py "
          f"--data {out_root}/data.yaml --name airpelago-yolo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
