"""Shared helpers for the local YOLO pipeline.

This module is split out so two very different consumers can both import it
cheaply:

* ``scripts.yolo.run_local`` runs from the *pld-yolo* pipenv, which has
  torch + ultralytics + cv2.
* ``scripts.embed.serve`` runs from the *energy-hackathon* pipenv (no
  torch / cv2). It only ever calls :func:`load_detections` /
  :func:`detection_classes_summary`.

So this module:

* uses **only the standard library** at import time;
* lazy-imports cv2 / numpy from inside :func:`mask_to_polygon`;
* does **not** import ``scripts.embed._lib`` because that module pulls
  in boto3 (which the pld-yolo pipenv doesn't have).

Cache layout written by :func:`save_video_detections` and read by
:func:`load_detections`::

    data/yolo/<digest>.json
    {
      "s3_key":      "raw-videos/pipeline_vegetation001.mp4",
      "version":     1,
      "generated_at": 1714083600.0,
      "models":      [{"name": ..., "version": "v1", "weights": "...", "classes": {...}}],
      "frames": {
          "0":  [ {detection}, ... ],
          "1":  [ {detection}, ... ],
          ...
      }
    }

Each detection dict matches the cloud schema:
``{model_name, model_version, class_id, class_name, confidence,
   bbox_xyxy: [x1,y1,x2,y2] in [0,1], polygon_xy: [x0,y0,x1,y1,...] in [0,1]}``.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
EMBEDDINGS_DIR = DATA_DIR / "embeddings"
FRAMES_CACHE_DIR = EMBEDDINGS_DIR / "frames"
FRAMES_THUMB_DIR = EMBEDDINGS_DIR / "thumbs"
YOLO_CACHE_DIR = DATA_DIR / "yolo"

CACHE_VERSION = 1
DEFAULT_PLD_YOLO_DIR = (REPO_ROOT.parent / "pld-yolo").resolve()

# Same palette the cloud worker uses, so result cards look the same in
# both UIs once the masks are in place.
DEFAULT_PALETTE = [
    "#ff8c00",  # safety orange
    "#00e0ff",  # cyan
    "#ff5cc6",  # magenta
    "#a4ff5c",  # lime
    "#ffd166",  # gold
    "#9b8cff",  # violet
]

MIN_CONTOUR_POINTS = 3


# ---------------------------------------------------------------------------
# Path / cache helpers
# ---------------------------------------------------------------------------


def digest_for(s3_key: str) -> str:
    """sha256[:24] of the s3 key. Mirrors ``scripts.embed._lib`` exactly so
    the YOLO cache lines up with the existing thumb / frame caches."""
    return hashlib.sha256(s3_key.encode("utf-8")).hexdigest()[:24]


def thumb_dir_for(s3_key: str) -> Path:
    return FRAMES_THUMB_DIR / digest_for(s3_key)


def yolo_cache_path_for(s3_key: str) -> Path:
    YOLO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return YOLO_CACHE_DIR / f"{digest_for(s3_key)}.json"


def iter_cached_frames_files() -> Iterator[Path]:
    if not FRAMES_CACHE_DIR.exists():
        return
    yield from sorted(FRAMES_CACHE_DIR.glob("*.json"))


def iter_cached_videos_with_frames() -> Iterator[dict[str, Any]]:
    """Stream the same per-video frame caches that ``scripts.embed`` writes.

    Each dict has at least: ``s3_key``, ``frames: [{timestamp_sec, thumb_name,
    embedding}, ...]``. We do **not** depend on ``scripts.embed._lib`` so this
    can run in a torch-only env.
    """
    for path in iter_cached_frames_files():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not data.get("s3_key") or not isinstance(data.get("frames"), list):
            continue
        yield data


def load_detections(s3_key: str) -> Optional[dict[str, Any]]:
    """Return the raw cache dict for ``s3_key`` or ``None`` if no cache yet."""
    path = yolo_cache_path_for(s3_key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_video_detections(
    *,
    s3_key: str,
    models: list["ModelSpec"],
    frames: dict[int, list[dict[str, Any]]],
) -> Path:
    """Write the per-video detections JSON atomically."""
    path = yolo_cache_path_for(s3_key)
    payload = {
        "s3_key": s3_key,
        "version": CACHE_VERSION,
        "generated_at": time.time(),
        "models": [m.summary() for m in models],
        "frames": {str(k): v for k, v in frames.items()},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    os.replace(tmp, path)
    return path


def detections_for_frame(
    cache: Optional[dict[str, Any]], frame_index: int
) -> list[dict[str, Any]]:
    if not cache:
        return []
    frames = cache.get("frames") or {}
    out = frames.get(str(frame_index))
    if isinstance(out, list):
        return out
    return []


def detection_classes_summary() -> dict[str, Any]:
    """Aggregate every cached detection into the catalogue shape the UI
    needs for its master + per-class toggle bar.

    Returns ``{"classes": [{"name", "model", "color", "count"}, ...],
    "models": [...], "status": "ok"|"empty"}``.
    """
    YOLO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    counts: dict[tuple[str, str], int] = {}
    color_for: dict[tuple[str, str], str] = {}
    models: dict[str, dict[str, Any]] = {}

    palette = list(DEFAULT_PALETTE)
    palette_idx = 0

    for path in sorted(YOLO_CACHE_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for spec in data.get("models", []) or []:
            name = spec.get("name") or "unknown"
            models.setdefault(
                name,
                {
                    "name": name,
                    "version": spec.get("version", "v1"),
                    "classes": dict(spec.get("classes", {}) or {}),
                    "mask_only": bool(spec.get("mask_only", False)),
                },
            )
            for cid, cname in (spec.get("classes") or {}).items():
                key = (name, str(cname))
                color_for.setdefault(
                    key,
                    (spec.get("colors", {}) or {}).get(str(cid))
                    or (spec.get("colors", {}) or {}).get(int(cid) if str(cid).isdigit() else cid)
                    or palette[palette_idx % len(palette)],
                )
                if key not in counts:
                    palette_idx += 1
                counts.setdefault(key, 0)
        for _frame_index, dets in (data.get("frames") or {}).items():
            for d in dets or []:
                key = (d.get("model_name") or "unknown", d.get("class_name") or "unknown")
                counts[key] = counts.get(key, 0) + 1
                if key not in color_for:
                    color_for[key] = palette[palette_idx % len(palette)]
                    palette_idx += 1

    classes = [
        {
            "name": cname,
            "model": mname,
            "color": color_for.get((mname, cname), DEFAULT_PALETTE[0]),
            "count": n,
        }
        for (mname, cname), n in sorted(
            counts.items(), key=lambda kv: (-kv[1], kv[0][1], kv[0][0])
        )
    ]
    return {
        "classes": classes,
        "models": list(models.values()),
        "status": "ok" if classes else "empty",
    }


# ---------------------------------------------------------------------------
# Model spec resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """One YOLO checkpoint to run.

    ``classes`` is the *trained* mapping (``class_id -> name``) and must
    match what the model emits — we don't try to remap or filter at
    runtime, only use it to attach the ``class_name`` for each detection.
    ``colors`` is per-class, falls back to :data:`DEFAULT_PALETTE` when a
    class id is missing.

    ``mask_only`` controls how the UI renders the model's detections:
    when ``True`` we draw only the polygon (no bbox, no per-detection
    label). Useful for long thin instances like power-line masks where an
    axis-aligned bbox is meaningless and clutters the frame.
    """

    name: str
    weights: Path
    version: str = "v1"
    classes: dict[int, str] = None  # type: ignore[assignment]
    colors: dict[int, str] = None  # type: ignore[assignment]
    mask_only: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "weights": str(self.weights),
            "classes": {str(k): v for k, v in (self.classes or {}).items()},
            "colors": {str(k): v for k, v in (self.colors or {}).items()},
            "mask_only": bool(self.mask_only),
        }


# Hardcoded defaults that match the trained runs in pld-yolo. Override via
# the --models CLI flag (a JSON file) when class lists or run dirs change.
DEFAULT_MODELS: list[dict[str, Any]] = [
    {
        "name": "pldm-power-line",
        "run": "pldm-subset2k-heavy",
        "classes": {0: "power_line"},
        "colors": {0: "#ff8c00"},
        # Power-line masks are thin diagonal stripes; an axis-aligned
        # bbox + label adds visual noise without any extra information.
        "mask_only": True,
    },
    {
        "name": "airpelago-insulator-pole",
        "run": "airpelago-yolo26s-seg",
        "classes": {0: "insulator", 1: "pole"},
        "colors": {0: "#00e0ff", 1: "#ff5cc6"},
    },
]


def _resolve_weights(run_dir: Path) -> Optional[Path]:
    for name in ("best.pt", "last.pt"):
        cand = run_dir / "weights" / name
        if cand.exists():
            return cand
    return None


def discover_models(
    *,
    pld_yolo_dir: Path | None = None,
    overrides: list[dict[str, Any]] | None = None,
    only: Iterable[str] | None = None,
) -> list[ModelSpec]:
    """Resolve :data:`DEFAULT_MODELS` (or *overrides*) into ModelSpecs.

    Skips entries whose weights file is missing — but raises if **all** of
    them are missing, since a typo in ``--pld-yolo-dir`` would otherwise
    look like a successful no-op.
    """
    pld_yolo_dir = (pld_yolo_dir or DEFAULT_PLD_YOLO_DIR).resolve()
    catalogue = list(overrides or DEFAULT_MODELS)
    keep: set[str] | None = set(only) if only else None

    out: list[ModelSpec] = []
    skipped: list[str] = []
    for entry in catalogue:
        name = str(entry["name"])
        if keep is not None and name not in keep:
            continue
        explicit = entry.get("weights")
        if explicit:
            weights = Path(explicit).expanduser()
            if not weights.is_absolute():
                weights = (pld_yolo_dir / weights).resolve()
        else:
            run_dir = pld_yolo_dir / "runs" / str(entry["run"])
            weights = _resolve_weights(run_dir) or Path()
        if not weights or not weights.exists():
            skipped.append(f"{name} (looked for {weights or '<unset>'})")
            continue
        classes = {int(k): str(v) for k, v in (entry.get("classes") or {}).items()}
        colors = {int(k): str(v) for k, v in (entry.get("colors") or {}).items()}
        out.append(
            ModelSpec(
                name=name,
                weights=weights,
                version=str(entry.get("version", "v1")),
                classes=classes,
                colors=colors,
                mask_only=bool(entry.get("mask_only", False)),
            )
        )

    if skipped:
        print(f"yolo: skipped {len(skipped)} model(s):", file=sys.stderr)
        for line in skipped:
            print(f"  - {line}", file=sys.stderr)
    if not out:
        raise RuntimeError(
            "No YOLO weights found. Pass --pld-yolo-dir or --models <json>."
        )
    return out


# ---------------------------------------------------------------------------
# Mask -> normalized polygon
# ---------------------------------------------------------------------------


def mask_to_polygon(
    mask, *, frame_w: int, frame_h: int, eps_px: float = 1.5
) -> list[float] | None:
    """Convert a binary mask (H x W, 0/1 or 0..1 float) to a flat polygon
    normalized to ``[0, 1]``. Returns ``None`` when the mask is empty or
    too small to produce a usable contour.

    Lazy-imports cv2 + numpy so importers that only need
    :func:`load_detections` don't pay the import cost (and don't need the
    libs installed).
    """
    import cv2
    import numpy as np

    if mask is None:
        return None
    arr = mask
    if hasattr(arr, "cpu"):
        arr = arr.cpu().numpy()
    arr = np.asarray(arr)
    if arr.dtype != np.uint8:
        arr = (arr > 0.5).astype(np.uint8)
    if arr.shape[0] != frame_h or arr.shape[1] != frame_w:
        arr = cv2.resize(arr, (frame_w, frame_h), interpolation=cv2.INTER_NEAREST)
    contours, _ = cv2.findContours(arr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    if len(contour) < MIN_CONTOUR_POINTS:
        return None
    contour = cv2.approxPolyDP(contour, max(0.5, eps_px), closed=True)
    if len(contour) < MIN_CONTOUR_POINTS:
        return None
    flat: list[float] = []
    for pt in contour.reshape(-1, 2):
        x, y = float(pt[0]), float(pt[1])
        flat.append(max(0.0, min(1.0, x / float(frame_w))))
        flat.append(max(0.0, min(1.0, y / float(frame_h))))
    return flat


def bbox_xyxy_norm(box, *, frame_w: int, frame_h: int) -> list[float]:
    x1, y1, x2, y2 = (float(v) for v in box[:4])
    return [
        max(0.0, min(1.0, x1 / float(frame_w))),
        max(0.0, min(1.0, y1 / float(frame_h))),
        max(0.0, min(1.0, x2 / float(frame_w))),
        max(0.0, min(1.0, y2 / float(frame_h))),
    ]
