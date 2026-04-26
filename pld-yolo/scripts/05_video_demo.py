"""Sample N evenly-spaced frames from a video, downsize, and run YOLO-seg.

Designed for quick visual sanity checks while training (or after) on CPU.
Outputs:
  runs/<name>/frames/        downsized JPEG frames (input to model)
  runs/<name>/preds/         frames with predicted masks/boxes overlaid
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"

# BGR (because OpenCV)
NAMED_COLORS = {
    "orange":     (0, 140, 255),   # safety/road-sign orange (#FF8C00)
    "yellow":     (0, 255, 255),
    "red":        (0,   0, 255),
    "cyan":       (255, 255, 0),
    "blue":       (255,   0, 0),
    "magenta":    (255,   0, 255),
    "lime":       (0,   255,  0),
    "white":      (255, 255, 255),
    "amber":      (0,  200, 240),   # softer than pure yellow, easier on the eye
    # Tron-ish electric tones for HUD/scope demos.
    "tron-cyan":  (255, 230,  50),  # bright slightly-cooled cyan
    "tron-blue":  (255, 170,  60),  # punchier neon blue (was 130/30, too dim)
}


def parse_color(spec: str) -> tuple[int, int, int]:
    spec = spec.strip().lower()
    if spec in NAMED_COLORS:
        return NAMED_COLORS[spec]
    parts = spec.split(",")
    if len(parts) == 3:
        try:
            b, g, r = (int(p) for p in parts)
            return (max(0, min(255, b)), max(0, min(255, g)), max(0, min(255, r)))
        except ValueError:
            pass
    raise SystemExit(f"unrecognized color {spec!r}")


def parse_color_spec(
    spec: str,
) -> tuple[int, int, int] | dict[int, tuple[int, int, int]]:
    """Parse either a single color ('orange', '0,140,255') or per-class
    ('0=orange,1=cyan'). Returns either a BGR tuple or {class_id: BGR}."""
    if "=" in spec:
        out: dict[int, tuple[int, int, int]] = {}
        for pair in spec.split(","):
            k, _, v = pair.partition("=")
            out[int(k.strip())] = parse_color(v)
        return out
    return parse_color(spec)


def render_masks(
    img: np.ndarray,
    masks: np.ndarray,
    color_spec: tuple[int, int, int] | dict[int, tuple[int, int, int]],
    alpha: float,
    outline_px: int,
    class_ids: list[int] | None = None,
) -> np.ndarray:
    """Overlay binary masks on `img`. `color_spec` is either a single BGR tuple
    (all masks same color) or a {class_id: BGR} dict (per-class color)."""
    if masks is None or len(masks) == 0:
        return img
    h, w = img.shape[:2]
    if class_ids is None:
        class_ids = [0] * len(masks)

    # Group masks by color so we render one solid layer per color.
    by_color: dict[tuple[int, int, int], np.ndarray] = {}
    fallback = (0, 140, 255) if isinstance(color_spec, dict) else color_spec
    for m, cid in zip(masks, class_ids):
        if m.shape[:2] != (h, w):
            m = cv2.resize(m.astype(np.uint8), (w, h),
                           interpolation=cv2.INTER_NEAREST)
        else:
            m = m.astype(np.uint8)
        if isinstance(color_spec, dict):
            color = color_spec.get(cid, fallback)
        else:
            color = color_spec
        union = by_color.setdefault(color, np.zeros((h, w), dtype=np.uint8))
        union |= (m > 0).astype(np.uint8)

    out = img.copy()
    for color, union in by_color.items():
        color_layer = np.zeros_like(img)
        color_layer[:] = color
        mask_3 = np.repeat(union[:, :, None], 3, axis=2).astype(bool)
        blended = cv2.addWeighted(out, 1 - alpha, color_layer, alpha, 0)
        out[mask_3] = blended[mask_3]
        if outline_px > 0:
            contours, _ = cv2.findContours(union, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_NONE)
            cv2.drawContours(out, contours, -1, color, outline_px, cv2.LINE_AA)
    return out


def draw_corner_brackets(
    img: np.ndarray,
    xyxy: tuple[int, int, int, int],
    color: tuple[int, int, int],
    thickness: int = 2,
    length_frac: float = 0.2,
    min_length: int = 8,
    antialias: bool = False,
) -> None:
    """Draw 4 L-shaped corner markers (HUD/scope style) at the corners of xyxy.

    Each corner gets two short line segments (horizontal + vertical) of length
    ~length_frac * min(box_w, box_h), with a floor of min_length pixels so they
    stay visible on tiny boxes. ``antialias=False`` (default) keeps lines pixel-
    sharp like a real HUD; AA softens edges and looks smudgy at small sizes.
    """
    x1, y1, x2, y2 = xyxy
    bw, bh = x2 - x1, y2 - y1
    L = max(min_length, int(min(bw, bh) * length_frac))
    L = min(L, max(1, min(bw, bh) // 2))  # never overshoot the box midline
    line_type = cv2.LINE_AA if antialias else cv2.LINE_8
    # top-left
    cv2.line(img, (x1, y1), (x1 + L, y1), color, thickness, line_type)
    cv2.line(img, (x1, y1), (x1, y1 + L), color, thickness, line_type)
    # top-right
    cv2.line(img, (x2, y1), (x2 - L, y1), color, thickness, line_type)
    cv2.line(img, (x2, y1), (x2, y1 + L), color, thickness, line_type)
    # bottom-left
    cv2.line(img, (x1, y2), (x1 + L, y2), color, thickness, line_type)
    cv2.line(img, (x1, y2), (x1, y2 - L), color, thickness, line_type)
    # bottom-right
    cv2.line(img, (x2, y2), (x2 - L, y2), color, thickness, line_type)
    cv2.line(img, (x2, y2), (x2, y2 - L), color, thickness, line_type)


def find_latest_weights() -> Path | None:
    cands = []
    for run in RUNS_DIR.glob("*/weights"):
        for nm in ("best.pt", "last.pt"):
            p = run / nm
            if p.exists():
                cands.append(p)
                break
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def center_crop_to_aspect(frame: np.ndarray, target_aspect: float) -> np.ndarray:
    """Center-crop to match `target_aspect = W/H` (e.g. 1.5 for 3:2)."""
    h, w = frame.shape[:2]
    cur = w / h
    if abs(cur - target_aspect) < 1e-3:
        return frame
    if cur > target_aspect:
        new_w = int(round(h * target_aspect))
        x0 = (w - new_w) // 2
        return frame[:, x0:x0 + new_w]
    new_h = int(round(w / target_aspect))
    y0 = (h - new_h) // 2
    return frame[y0:y0 + new_h, :]


def sample_and_resize(
    video: Path,
    out_dir: Path,
    n_frames: int,
    target_w: int,
    target_h: int,
) -> list[Path]:
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        cap.release()
        raise RuntimeError(f"could not read frame count from {video}")
    indices = np.linspace(0, total - 1, n_frames).round().astype(int).tolist()
    out_dir.mkdir(parents=True, exist_ok=True)
    target_aspect = target_w / target_h
    written: list[Path] = []
    for i, idx in enumerate(indices):
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            print(f"[warn] failed to read frame {idx}", file=sys.stderr)
            continue
        frame = center_crop_to_aspect(frame, target_aspect)
        frame = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
        p = out_dir / f"frame_{i:03d}_t{idx:06d}.jpg"
        cv2.imwrite(str(p), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        written.append(p)
    cap.release()
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", required=True)
    ap.add_argument("--weights", default=None,
                    help="Default: latest best.pt/last.pt under runs/.")
    ap.add_argument("--n-frames", type=int, default=16)
    ap.add_argument("--target-w", type=int, default=540,
                    help="Resize frames to this width (after center-crop). PLDM is 540x360.")
    ap.add_argument("--target-h", type=int, default=360,
                    help="Resize frames to this height (after center-crop). PLDM is 540x360.")
    ap.add_argument("--imgsz", type=int, default=480)
    ap.add_argument("--conf", type=float, default=0.10)
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--device", default=None)
    ap.add_argument("--name", default="video-demo")
    ap.add_argument("--show-boxes", action="store_true",
                    help="Draw boxes too. Default: masks only (no boxes/labels).")
    ap.add_argument("--mask-color", default="orange",
                    help="Single color (e.g. 'orange', 'yellow', '0,140,255') "
                         "OR per-class spec like '0=orange,1=cyan'. Built-ins: "
                         "orange, yellow, red, cyan, magenta, lime, white.")
    ap.add_argument("--mask-alpha", type=float, default=0.55,
                    help="Mask opacity, 0..1. Default 0.55 = strong but background visible.")
    ap.add_argument("--mask-outline", type=int, default=2,
                    help="Outline thickness in pixels (0 disables).")
    ap.add_argument("--bracket-classes", default="",
                    help="Comma-separated class IDs to overlay HUD-style "
                         "corner brackets on (e.g. '0' for insulator only). "
                         "Empty disables brackets.")
    ap.add_argument("--bracket-color", default="yellow",
                    help="Color for corner brackets. Same syntax as --mask-color "
                         "(named or 'B,G,R').")
    ap.add_argument("--bracket-thickness", type=int, default=2)
    ap.add_argument("--bracket-length-frac", type=float, default=0.2,
                    help="Bracket leg length as fraction of min(box_w, box_h).")
    args = ap.parse_args()

    video = Path(args.video).resolve()
    if not video.exists():
        raise SystemExit(f"video not found: {video}")

    weights = Path(args.weights) if args.weights else find_latest_weights()
    if weights is None or not weights.exists():
        raise SystemExit("no weights found. Train first or pass --weights.")
    print(f"weights:  {weights}")
    print(f"video:    {video}")

    out_root = RUNS_DIR / args.name
    frames_dir = out_root / "frames"
    preds_dir = out_root / "preds"

    print(f"\nSampling {args.n_frames} frames, "
          f"center-crop to {args.target_w}x{args.target_h} ({args.target_w/args.target_h:.2f}:1) ...")
    frame_paths = sample_and_resize(
        video, frames_dir, args.n_frames, args.target_w, args.target_h
    )
    print(f"  wrote {len(frame_paths)} frames -> {frames_dir}")

    from ultralytics import YOLO

    color_spec = parse_color_spec(args.mask_color)
    if isinstance(color_spec, dict):
        print(f"mask colors:    {color_spec}  (alpha={args.mask_alpha}, outline={args.mask_outline}px)")
    else:
        print(f"mask color BGR: {color_spec}  (alpha={args.mask_alpha}, outline={args.mask_outline}px)")

    bracket_classes: set[int] = set()
    bracket_color: tuple[int, int, int] = (0, 0, 0)
    if args.bracket_classes.strip():
        bracket_classes = {
            int(x.strip()) for x in args.bracket_classes.split(",") if x.strip()
        }
        bracket_color = parse_color(args.bracket_color)
        print(f"brackets on:    classes {sorted(bracket_classes)}  "
              f"color={bracket_color}  thickness={args.bracket_thickness}px")

    model = YOLO(str(weights))
    print(f"\nRunning inference at imgsz={args.imgsz}, conf={args.conf} ...")
    results = model.predict(
        source=str(frames_dir),
        imgsz=args.imgsz,
        conf=args.conf,
        iou=args.iou,
        device=args.device,
        save=False,
        save_txt=False,
        retina_masks=True,
        verbose=False,
    )
    preds_dir.mkdir(parents=True, exist_ok=True)
    for r in results:
        img = r.orig_img.copy()
        masks = None
        class_ids = None
        if r.masks is not None:
            masks = r.masks.data.cpu().numpy()  # [N, H, W] in 0..1
            if r.boxes is not None:
                class_ids = r.boxes.cls.cpu().numpy().astype(int).tolist()
        rendered = render_masks(
            img, masks, color_spec, args.mask_alpha, args.mask_outline,
            class_ids=class_ids,
        )
        if r.boxes is not None and (args.show_boxes or bracket_classes):
            xyxys = r.boxes.xyxy.cpu().numpy().astype(int)
            cids = r.boxes.cls.cpu().numpy().astype(int).tolist()
            box_fallback = (0, 140, 255) if isinstance(color_spec, dict) else color_spec
            for xyxy, cid in zip(xyxys, cids):
                if args.show_boxes:
                    bc = (color_spec.get(cid, box_fallback)
                          if isinstance(color_spec, dict) else color_spec)
                    cv2.rectangle(rendered, (xyxy[0], xyxy[1]),
                                  (xyxy[2], xyxy[3]), bc, 2)
                if cid in bracket_classes:
                    draw_corner_brackets(
                        rendered, tuple(xyxy.tolist()), bracket_color,
                        thickness=args.bracket_thickness,
                        length_frac=args.bracket_length_frac,
                    )
        out_path = preds_dir / Path(r.path).name
        cv2.imwrite(str(out_path), rendered, [cv2.IMWRITE_JPEG_QUALITY, 92])

    print(f"\nDone. Look at:\n  {preds_dir}/")
    if frame_paths:
        sample = preds_dir / frame_paths[len(frame_paths) // 2].name
        print(f"e.g.  xdg-open {sample}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
