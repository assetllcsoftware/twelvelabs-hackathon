"""Per-frame YOLO instance-segmentation worker (Fargate one-shot).

Phase D.6. Runs the trained Ultralytics YOLO-seg models from the sister
``pld-yolo`` project against the same frames the Marengo frame-embed worker
already extracted. Writing into a brand-new ``frame_detections`` table keeps
this orthogonal to the existing search path: we only ever JOIN against
detections at render time.

Pipeline:

  1. Wait until the frame-embed worker has written rows for ``S3_KEY`` (poll
     ``embeddings`` for ``kind='frame'``). They share the digest-based S3
     thumbnail prefix, so once the rows exist we know the JPEGs do too.
  2. For every model in ``YOLO_MODELS``:
       - Download the ``.pt`` from S3 to ephemeral storage (cached across
         models per task).
       - For each frame thumbnail in S3:
           - Download to a scratch dir.
           - ``model.predict(...)`` with ``retina_masks=True``.
           - Convert each mask to a polygon in normalized ``[0, 1]`` and
             collect bbox + class + confidence.
       - Upsert into ``frame_detections`` (DELETE-then-INSERT per
         (s3_key, frame_index, model_name) so re-runs are idempotent).
  3. Flip ``videos.status`` -> 'detections_ready' as a soft signal.

We deliberately do NOT re-extract frames — we sample the exact same set
the embed worker wrote so each search hit's ``thumb_s3_key`` has at most
one detection record per model, and the UI can overlay polygons on the
thumb without any resampling.
"""
from __future__ import annotations

import gc
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import psycopg


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("yolo_detect_worker")


# ---------------------------------------------------------------------------
# Config (env)
# ---------------------------------------------------------------------------

REGION = os.environ["AWS_REGION"]
BUCKET = os.environ["S3_BUCKET"]
S3_KEY = os.environ["S3_KEY"]
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
YOLO_MODELS_JSON = os.environ.get("YOLO_MODELS", "[]")
IMG_SIZE = int(os.environ.get("YOLO_IMGSZ", "640"))
CONF_THRESH = float(os.environ.get("YOLO_CONF", "0.10"))
IOU_THRESH = float(os.environ.get("YOLO_IOU", "0.5"))
APPROX_EPS_PX = float(os.environ.get("YOLO_APPROX_EPS_PX", "1.5"))
WAIT_FOR_FRAMES_SEC = int(os.environ.get("YOLO_WAIT_FOR_FRAMES_SEC", "900"))
WAIT_POLL_SEC = int(os.environ.get("YOLO_WAIT_POLL_SEC", "20"))
MIN_CONTOUR_POINTS = 3

DEFAULT_PALETTE = [
    "#ff8c00",  # safety orange
    "#00e0ff",  # cyan
    "#ff5cc6",  # magenta
    "#a4ff5c",  # lime
    "#ffd166",  # gold
    "#9b8cff",  # violet
]


@dataclass(frozen=True)
class ModelSpec:
    name: str
    s3_key: str
    version: str
    classes: dict[int, str]              # {class_id: class_name}
    colors: dict[int, str]               # {class_id: "#rrggbb"}


def _parse_models() -> list[ModelSpec]:
    raw = json.loads(YOLO_MODELS_JSON or "[]")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(
            "YOLO_MODELS env var is empty/invalid. Expected a JSON array of "
            "{name, s3_key, classes:{id:name}, colors?:{id:hex}}."
        )
    out: list[ModelSpec] = []
    palette_iter = iter(DEFAULT_PALETTE * 4)
    for entry in raw:
        if not isinstance(entry, dict):
            raise RuntimeError(f"YOLO_MODELS entry not a dict: {entry!r}")
        name = str(entry["name"])
        s3_key = str(entry["s3_key"])
        version = str(entry.get("version", "v1"))
        classes = {int(k): str(v) for k, v in (entry.get("classes") or {}).items()}
        if not classes:
            raise RuntimeError(f"YOLO_MODELS entry {name!r} has no classes")
        colors_raw = entry.get("colors") or {}
        colors = {int(k): str(v) for k, v in colors_raw.items()}
        for cid in classes:
            if cid not in colors:
                colors[cid] = next(palette_iter)
        out.append(ModelSpec(name=name, s3_key=s3_key, version=version,
                             classes=classes, colors=colors))
    return out


# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------

s3 = boto3.client("s3", region_name=REGION)
secrets = boto3.client("secretsmanager", region_name=REGION)


def _db_url() -> str:
    payload = json.loads(
        secrets.get_secret_value(SecretId=DB_SECRET_ARN)["SecretString"]
    )
    return payload["url"]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _wait_for_frames(conn) -> list[dict[str, Any]]:
    """Block until frame-embed-worker has written its rows. Returns the
    distinct ``(frame_index, timestamp_sec, thumb_s3_key)`` triples that
    we should run YOLO over.
    """
    deadline = time.time() + WAIT_FOR_FRAMES_SEC
    last_n = -1
    while True:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT frame_index, MIN(timestamp_sec), MIN(thumb_s3_key) "
                "FROM embeddings "
                "WHERE s3_key = %s AND kind = 'frame' AND thumb_s3_key IS NOT NULL "
                "GROUP BY frame_index ORDER BY frame_index",
                (S3_KEY,),
            )
            rows = cur.fetchall()
        if rows:
            frames = [
                {
                    "frame_index": int(r[0]),
                    "timestamp_sec": float(r[1]),
                    "thumb_s3_key": str(r[2]),
                }
                for r in rows
            ]
            logger.info("frames ready: %d distinct rows", len(frames))
            return frames
        if time.time() >= deadline:
            raise RuntimeError(
                f"timed out after {WAIT_FOR_FRAMES_SEC}s waiting for frame "
                f"rows for s3_key={S3_KEY!r}"
            )
        if last_n != 0:
            logger.info("no frame rows yet; sleeping %ds", WAIT_POLL_SEC)
            last_n = 0
        time.sleep(WAIT_POLL_SEC)


def _replace_detections(
    conn,
    *,
    frame_index: int,
    model_name: str,
    rows: list[tuple[Any, ...]],
) -> None:
    """Atomic per-(frame, model) replacement.

    The single statement uses a CTE so a re-run with zero detections still
    deletes any stale ones from a previous run.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM frame_detections "
            "WHERE s3_key = %s AND frame_index = %s AND model_name = %s",
            (S3_KEY, frame_index, model_name),
        )
        if not rows:
            return
        cur.executemany(
            """
            INSERT INTO frame_detections
                (s3_key, frame_index, timestamp_sec, thumb_s3_key,
                 model_name, model_version,
                 class_id, class_name, confidence, bbox_xyxy, polygon_xy)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )


def _flip_status(conn) -> None:
    """Set videos.status -> detections_ready. Best-effort: column accepts
    free-form text, so even a fresh schema is happy with this. We don't
    overwrite ready/frames_ready/clips_ready with a downgrade because the
    column already encodes pipeline progress.
    """
    sql = (
        "UPDATE videos "
        "SET status = CASE "
        "  WHEN status IN ('ready', 'detections_ready') THEN status "
        "  ELSE 'detections_ready' "
        "END, updated_at = now() "
        "WHERE s3_key = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (S3_KEY,))


# ---------------------------------------------------------------------------
# Frame I/O + mask -> polygon conversion
# ---------------------------------------------------------------------------


def _download_thumb(thumb_key: str, dest_dir: Path) -> Path:
    """Pull a JPEG to disk under a name YOLO will accept (it parses suffix
    for video/image dispatch).
    """
    name = Path(thumb_key).name
    if not name:
        name = "frame.jpg"
    out_path = dest_dir / name
    s3.download_file(BUCKET, thumb_key, str(out_path))
    return out_path


def _download_model(model: ModelSpec, dest_dir: Path) -> Path:
    out_path = dest_dir / f"{model.name}.pt"
    if out_path.exists():
        return out_path
    logger.info("download model %s -> %s", model.s3_key, out_path)
    s3.download_file(BUCKET, model.s3_key, str(out_path))
    return out_path


def _mask_to_polygon(mask, *, frame_w: int, frame_h: int) -> list[float] | None:
    """Convert a binary mask (HxW uint8 / 0..1) to a flat normalized polygon.

    Mirrors :func:`pld-yolo/scripts/02_convert_to_yolo.py::mask_to_polygons`
    but keeps only the largest contour per instance — Ultralytics already
    gives us one mask per instance, so any second contour is noise.
    """
    import cv2
    import numpy as np

    if mask is None:
        return None
    arr = mask
    if arr.dtype != "uint8":
        arr = (arr > 0.5).astype("uint8")
    if arr.shape[0] != frame_h or arr.shape[1] != frame_w:
        arr = cv2.resize(arr, (frame_w, frame_h), interpolation=cv2.INTER_NEAREST)
    contours, _ = cv2.findContours(arr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    if len(contour) < MIN_CONTOUR_POINTS:
        return None
    eps = max(0.5, APPROX_EPS_PX)
    contour = cv2.approxPolyDP(contour, eps, closed=True)
    if len(contour) < MIN_CONTOUR_POINTS:
        return None
    flat: list[float] = []
    for pt in contour.reshape(-1, 2):
        x, y = float(pt[0]), float(pt[1])
        flat.append(max(0.0, min(1.0, x / float(frame_w))))
        flat.append(max(0.0, min(1.0, y / float(frame_h))))
    return flat


def _bbox_xyxy_norm(box, *, frame_w: int, frame_h: int) -> list[float]:
    x1, y1, x2, y2 = (float(v) for v in box[:4])
    return [
        max(0.0, min(1.0, x1 / float(frame_w))),
        max(0.0, min(1.0, y1 / float(frame_h))),
        max(0.0, min(1.0, x2 / float(frame_w))),
        max(0.0, min(1.0, y2 / float(frame_h))),
    ]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _process_model(
    *,
    model_spec: ModelSpec,
    weights_path: Path,
    frames: list[dict[str, Any]],
    work_dir: Path,
    conn,
) -> tuple[int, int]:
    """Run one model over all frames, replacing detections atomically per
    (frame, model). Returns ``(frames_with_detections, total_rows_written)``.
    """
    from ultralytics import YOLO

    logger.info("loading %s from %s", model_spec.name, weights_path)
    model = YOLO(str(weights_path))

    frames_with_dets = 0
    total_rows = 0
    frames_dir = work_dir / model_spec.name / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    for f in frames:
        thumb_key = f["thumb_s3_key"]
        frame_index = f["frame_index"]
        timestamp_sec = f["timestamp_sec"]

        try:
            local = _download_thumb(thumb_key, frames_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("download failed thumb=%s: %s", thumb_key, exc)
            _replace_detections(
                conn,
                frame_index=frame_index,
                model_name=model_spec.name,
                rows=[],
            )
            conn.commit()
            continue

        try:
            results = model.predict(
                source=str(local),
                imgsz=IMG_SIZE,
                conf=CONF_THRESH,
                iou=IOU_THRESH,
                retina_masks=True,
                save=False,
                save_txt=False,
                verbose=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("yolo predict failed thumb=%s: %s", thumb_key, exc)
            _replace_detections(
                conn,
                frame_index=frame_index,
                model_name=model_spec.name,
                rows=[],
            )
            conn.commit()
            continue
        finally:
            try:
                local.unlink()
            except OSError:
                pass

        rows: list[tuple[Any, ...]] = []
        for r in results:
            img = r.orig_img
            if img is None:
                continue
            frame_h, frame_w = img.shape[:2]
            masks = None
            classes = None
            confs = None
            boxes = None
            if r.masks is not None:
                masks = r.masks.data.cpu().numpy()
            if r.boxes is not None:
                classes = r.boxes.cls.cpu().numpy().astype(int).tolist()
                confs = r.boxes.conf.cpu().numpy().astype(float).tolist()
                boxes = r.boxes.xyxy.cpu().numpy().astype(float).tolist()
            if masks is None or boxes is None or classes is None or confs is None:
                continue

            for mask, cls_id, conf, box in zip(masks, classes, confs, boxes):
                cls_name = model_spec.classes.get(int(cls_id))
                if cls_name is None:
                    continue
                poly = _mask_to_polygon(mask, frame_w=frame_w, frame_h=frame_h)
                if poly is None:
                    continue
                bbox = _bbox_xyxy_norm(box, frame_w=frame_w, frame_h=frame_h)
                rows.append(
                    (
                        S3_KEY,
                        frame_index,
                        timestamp_sec,
                        thumb_key,
                        model_spec.name,
                        model_spec.version,
                        int(cls_id),
                        cls_name,
                        float(conf),
                        bbox,
                        poly,
                    )
                )

        _replace_detections(
            conn,
            frame_index=frame_index,
            model_name=model_spec.name,
            rows=rows,
        )
        conn.commit()
        if rows:
            frames_with_dets += 1
            total_rows += len(rows)

    # Free any tensors / cached graphs before loading the next model.
    del model
    gc.collect()
    try:
        import torch

        if hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass

    return frames_with_dets, total_rows


def main() -> int:
    started = time.time()
    models = _parse_models()
    logger.info(
        "yolo-detect start s3_key=%s models=%s imgsz=%d conf=%.2f",
        S3_KEY,
        [m.name for m in models],
        IMG_SIZE,
        CONF_THRESH,
    )

    db_url = _db_url()
    work = Path(tempfile.mkdtemp(prefix="yolo-detect-"))
    weights_dir = work / "weights"
    weights_dir.mkdir(parents=True, exist_ok=True)

    try:
        with psycopg.connect(db_url, sslmode="require") as conn:
            frames = _wait_for_frames(conn)

            for spec in models:
                weights_path = _download_model(spec, weights_dir)
                f_with, total = _process_model(
                    model_spec=spec,
                    weights_path=weights_path,
                    frames=frames,
                    work_dir=work,
                    conn=conn,
                )
                logger.info(
                    "model=%s frames_with_detections=%d total_rows=%d",
                    spec.name,
                    f_with,
                    total,
                )

            try:
                _flip_status(conn)
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("status flip failed (non-fatal): %s", exc)

        elapsed = time.time() - started
        logger.info(
            "yolo-detect done s3_key=%s frames=%d models=%d elapsed=%.1fs",
            S3_KEY,
            len(frames),
            len(models),
            elapsed,
        )
        return 0
    finally:
        try:
            shutil.rmtree(work)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
