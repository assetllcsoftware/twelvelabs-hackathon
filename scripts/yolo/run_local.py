"""Local YOLO inference CLI.

Reads the same cached frame thumbnails the local Marengo pipeline writes
(``data/embeddings/thumbs/<digest>/frame_NNNNN.jpg``), runs every
configured model against them, and writes per-video detection JSONs to
``data/yolo/<digest>.json`` so the local FastAPI portal can overlay
polygons on its search results.

Idempotent: re-running with no flags only inserts new frames / models;
pass ``--force`` to recompute every (frame, model). Use ``--limit N`` to
process only the first N videos and ``--models pldm-power-line`` to scope
to a single checkpoint.

Because torch + ultralytics are intentionally **not** in the
energy-hackathon Pipfile, run this from the sister project's pipenv,
which already has them::

    cd /home/bryce/projects/pld-yolo
    pipenv run python /home/bryce/projects/energy-hackathon/scripts/yolo/run_local.py

(Or set ``PIPENV_PIPFILE=.../pld-yolo/Pipfile`` and run from this repo.)
The script resolves all paths relative to ``__file__`` so the working
directory is irrelevant.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

# Allow running as ``python /path/to/run_local.py`` from the pld-yolo pipenv
# (no parent package) as well as ``python -m scripts.yolo.run_local`` from
# this repo's pipenv.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.yolo import _lib as ylib  # noqa: E402  (path bootstrap above)


def _ensure_thumbs(s3_key: str, frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter the frame list down to those whose thumb file actually exists.

    Mirrors the index used by ``build_segment_matrix`` — the *array index*
    is the ``frame_index`` we key detections by on lookup.
    """
    out: list[dict[str, Any]] = []
    thumb_dir = ylib.thumb_dir_for(s3_key)
    for idx, frame in enumerate(frames):
        thumb_name = frame.get("thumb_name")
        if not thumb_name:
            continue
        path = thumb_dir / thumb_name
        if not path.exists():
            continue
        out.append(
            {
                "frame_index": idx,
                "timestamp_sec": float(frame.get("timestamp_sec", 0.0)),
                "thumb_path": path,
                "thumb_rel": f"{ylib.digest_for(s3_key)}/{thumb_name}",
            }
        )
    return out


def _run_one_model(
    model,
    spec: ylib.ModelSpec,
    frames: list[dict[str, Any]],
    *,
    imgsz: int,
    conf: float,
    iou: float,
    eps_px: float,
) -> dict[int, list[dict[str, Any]]]:
    """Run one already-loaded ``ultralytics.YOLO`` over the given frames.

    Returns ``{frame_index: [detection, ...]}``. Frames that produce zero
    detections are still represented (with an empty list) so callers can
    tell the model *was* run on them.
    """
    out: dict[int, list[dict[str, Any]]] = {}

    if not frames:
        return out

    print(
        f"  running {spec.name} ({spec.weights.name}) over {len(frames)} frame(s)",
        flush=True,
    )

    todo = frames
    sources = [str(f["thumb_path"]) for f in todo]
    started = time.time()
    # ultralytics.predict tolerates a list of file paths and returns a
    # parallel list of Results (one per source).
    results = model.predict(
        source=sources,
        imgsz=imgsz,
        conf=conf,
        iou=iou,
        retina_masks=True,
        save=False,
        save_txt=False,
        verbose=False,
    )
    if len(results) != len(todo):
        print(
            f"    warning: {spec.name} returned {len(results)} results for "
            f"{len(todo)} sources; skipping mismatched run",
            file=sys.stderr,
        )
        return out

    for frame, r in zip(todo, results):
        frame_index = frame["frame_index"]
        rows: list[dict[str, Any]] = []
        img = getattr(r, "orig_img", None)
        if img is None:
            out[frame_index] = rows
            continue
        frame_h, frame_w = img.shape[:2]
        boxes = r.boxes
        masks = r.masks
        if boxes is None or masks is None:
            out[frame_index] = rows
            continue
        cls_arr = boxes.cls.cpu().numpy().astype(int).tolist()
        conf_arr = boxes.conf.cpu().numpy().astype(float).tolist()
        xyxy_arr = boxes.xyxy.cpu().numpy().astype(float).tolist()
        masks_arr = masks.data.cpu().numpy()
        for mask, cls_id, c, box in zip(masks_arr, cls_arr, conf_arr, xyxy_arr):
            cls_name = (spec.classes or {}).get(int(cls_id))
            if cls_name is None:
                # Model produced a class id we don't know about. Don't
                # silently drop -- name it by id so the operator notices.
                cls_name = f"class_{cls_id}"
            poly = ylib.mask_to_polygon(
                mask, frame_w=frame_w, frame_h=frame_h, eps_px=eps_px
            )
            if poly is None:
                continue
            bbox = ylib.bbox_xyxy_norm(box, frame_w=frame_w, frame_h=frame_h)
            rows.append(
                {
                    "model_name": spec.name,
                    "model_version": spec.version,
                    "class_id": int(cls_id),
                    "class_name": cls_name,
                    "confidence": float(c),
                    "bbox_xyxy": bbox,
                    "polygon_xy": poly,
                    "thumb_rel": frame["thumb_rel"],
                    "timestamp_sec": frame["timestamp_sec"],
                }
            )
        out[frame_index] = rows

    elapsed = time.time() - started
    n_dets = sum(len(v) for v in out.values())
    print(
        f"    {spec.name}: {n_dets} detection(s) over {len(todo)} frame(s) "
        f"in {elapsed:.1f}s",
        flush=True,
    )
    return out


def _process_video(
    *,
    s3_key: str,
    frames: list[dict[str, Any]],
    models: list[ylib.ModelSpec],
    args: argparse.Namespace,
) -> bool:
    """Run all configured models for one video. Returns True iff anything
    was written to disk."""
    if not frames:
        print(f"  no thumbs cached for {s3_key} -- skipping")
        return False

    cache_path = ylib.yolo_cache_path_for(s3_key)
    if cache_path.exists() and not args.force:
        print(
            f"  cache exists ({cache_path.relative_to(ylib.REPO_ROOT)}); "
            "pass --force to recompute"
        )
        return False

    from ultralytics import YOLO  # type: ignore[import-not-found]

    all_frames: dict[int, list[dict[str, Any]]] = {f["frame_index"]: [] for f in frames}
    for spec in models:
        # Surfacing this print early helps when YOLO's first-load delay
        # makes the run look hung.
        print(f"  loading weights {spec.weights}")
        model = YOLO(str(spec.weights))
        per_frame = _run_one_model(
            model,
            spec,
            frames,
            imgsz=args.imgsz,
            conf=args.conf,
            iou=args.iou,
            eps_px=args.approx_eps_px,
        )
        for fi, dets in per_frame.items():
            all_frames.setdefault(fi, [])
            all_frames[fi].extend(dets)
        # Free torch graph + cached buffers between models so a 4-GB box
        # can still hold both.
        del model
        try:
            import gc

            gc.collect()
            import torch  # type: ignore[import-not-found]

            if hasattr(torch, "cuda") and torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    # Drop empty frame entries to keep the JSON small. Frames that produced
    # zero detections are still considered "processed" because the cache
    # file exists for the video; running this video again is gated on
    # --force.
    merged = {fi: dets for fi, dets in all_frames.items() if dets}
    out_path = ylib.save_video_detections(s3_key=s3_key, models=models, frames=merged)
    print(
        f"  wrote {sum(len(v) for v in merged.values())} detection(s) "
        f"across {len(merged)} frame(s) -> {out_path.relative_to(ylib.REPO_ROOT)}"
    )
    return True


def _load_overrides(path: Path | None) -> list[dict[str, Any]] | None:
    if path is None:
        return None
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON array of model entries")
    return raw


def _select_videos(
    only_keys: Iterable[str] | None,
) -> list[dict[str, Any]]:
    keep = set(only_keys) if only_keys else None
    out: list[dict[str, Any]] = []
    for video in ylib.iter_cached_videos_with_frames():
        if keep is not None and video["s3_key"] not in keep:
            continue
        out.append(video)
    return out


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--pld-yolo-dir",
        type=Path,
        default=None,
        help="Path to the sister pld-yolo project (default: ../pld-yolo).",
    )
    ap.add_argument(
        "--models",
        type=Path,
        default=None,
        help=(
            "Optional JSON file overriding the default model list. Each "
            "entry: {name, run|weights, classes:{id:name}, colors?:{id:hex}}."
        ),
    )
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="Run only the named model(s). Repeatable.",
    )
    ap.add_argument(
        "--video",
        action="append",
        default=[],
        metavar="S3_KEY",
        help="Restrict to specific s3 keys. Repeatable.",
    )
    ap.add_argument(
        "--limit", type=int, default=None, help="Process at most N videos."
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Recompute every (frame, model). Default: only fill gaps.",
    )
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.10)
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument(
        "--approx-eps-px",
        type=float,
        default=1.5,
        help="cv2.approxPolyDP epsilon (px) for mask -> polygon conversion.",
    )
    args = ap.parse_args(argv)

    overrides = _load_overrides(args.models)
    models = ylib.discover_models(
        pld_yolo_dir=args.pld_yolo_dir,
        overrides=overrides,
        only=args.only or None,
    )
    print("yolo: using models")
    for m in models:
        rel = m.weights
        try:
            rel = m.weights.relative_to(ylib.DEFAULT_PLD_YOLO_DIR)
        except ValueError:
            pass
        classes = ", ".join(f"{k}={v}" for k, v in (m.classes or {}).items())
        print(f"  {m.name:30s} {classes}")
        print(f"    weights: {rel}")
    print()

    videos = _select_videos(args.video or None)
    if not videos:
        print(
            "yolo: no cached frames found under "
            f"{ylib.FRAMES_CACHE_DIR.relative_to(ylib.REPO_ROOT)}/. "
            "Run `pipenv run python -m scripts.embed.embed_videos` first.",
            file=sys.stderr,
        )
        return 1
    if args.limit is not None:
        videos = videos[: args.limit]

    print(f"yolo: {len(videos)} video(s) queued")
    started = time.time()
    written = 0
    for i, video in enumerate(videos, start=1):
        s3_key = video["s3_key"]
        frames = _ensure_thumbs(s3_key, video.get("frames", []) or [])
        print(
            f"[{i}/{len(videos)}] {s3_key}  ({len(frames)} thumbs)",
            flush=True,
        )
        if _process_video(
            s3_key=s3_key, frames=frames, models=models, args=args
        ):
            written += 1
    elapsed = time.time() - started
    print(
        f"\nyolo: done. {written} video(s) updated, "
        f"{len(videos) - written} unchanged, in {elapsed:.1f}s."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
