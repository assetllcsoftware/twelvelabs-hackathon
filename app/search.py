"""Cloud-side video search: Bedrock query embedding + pgvector ANN + refinement.

Mirrors the local ``scripts/embed/_lib.py`` + ``scripts/embed/serve.py`` code
paths so the in-prod search behaves the same as the laptop search:

  1. Embed the user's query (text / image / text+image) via the Marengo
     cross-region inference profile.
  2. Pull a candidate pool from Postgres ordered by ``embedding <=> query``
     (cosine distance, HNSW-backed). Pool is ~4x ``top_k`` so refinement and
     dedupe have material to work with.
  3. For every clip in the pool, snap its ``timestamp_sec`` to the highest-
     scoring frame whose own ``timestamp_sec`` falls inside the clip's
     ``[start_sec, end_sec]`` window — this is what makes the preview jump
     to the actual matching moment instead of the clip midpoint.
  4. Dedupe near-duplicate hits within ``dedupe_window_sec`` of each other in
     the same video. We walk in score-descending order so the survivor is
     always the strongest.
  5. Attach a presigned URL with ``#t=<timestamp>`` and (if available) a
     presigned URL for the matching frame thumbnail.

The DB-empty path returns ``[]`` instead of raising so a fresh-cluster hit
on ``/api/search/text`` returns an empty 200 — useful before the Lambdas
have populated anything.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Optional

import boto3

import db as portal_db

logger = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.environ["S3_BUCKET"]
# Cross-region inference profile id. ``us.`` covers us-east-1 / us-east-2 /
# us-west-2. Override with MARENGO_INFERENCE_ID if you redeploy elsewhere.
MARENGO_INFERENCE_ID = os.getenv(
    "MARENGO_INFERENCE_ID", "us.twelvelabs.marengo-embed-3-0-v1:0"
)
# Presigned URL TTL. 1h matches the rest of the portal.
PRESIGN_EXPIRES_SECONDS = 3600
# How many raw candidates to pull from PG before refinement + dedupe. The
# only way frame-snap can help a clip is if both the clip and at least one
# frame from inside its window land in this pool, so this number trades
# precision for query latency.
DEFAULT_CANDIDATE_POOL = 80
# Inside one video, hits within this many seconds of each other are folded
# together (we keep the highest-scoring one). Mirrors rank_results().
DEFAULT_DEDUPE_WINDOW_SEC = 3.0

_bedrock = None
_s3 = None


def _bedrock_client():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _bedrock


def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=AWS_REGION)
    return _s3


def _invoke(body: dict) -> list[float]:
    """Call Marengo via the cross-region inference profile and return a 512-d vector.

    Raises ``RuntimeError`` if Bedrock returns no embeddings (zero-length
    ``data`` array). Lets boto3's ``ClientError`` propagate so the caller can
    map it to an HTTP status — mostly that means a 4xx for AccessDenied or
    ValidationException and a 5xx for ServiceUnavailable.
    """
    br = _bedrock_client()
    resp = br.invoke_model(modelId=MARENGO_INFERENCE_ID, body=json.dumps(body))
    payload = json.loads(resp["body"].read().decode("utf-8"))
    data = payload.get("data") or []
    if not data:
        raise RuntimeError(
            f"Bedrock returned empty embedding payload (modelId={MARENGO_INFERENCE_ID})"
        )
    return data[0]["embedding"]


def embed_text(text: str) -> list[float]:
    return _invoke({"inputType": "text", "text": {"inputText": text}})


def embed_image_bytes(image_bytes: bytes) -> list[float]:
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return _invoke(
        {
            "inputType": "image",
            "image": {"mediaSource": {"base64String": encoded}},
        }
    )


def embed_text_image_bytes(text: str, image_bytes: bytes) -> list[float]:
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return _invoke(
        {
            "inputType": "text_image",
            "text_image": {
                "inputText": text,
                "mediaSource": {"base64String": encoded},
            },
        }
    )


def _vector_literal(vec) -> str:
    """Serialize a 512-d float vector to pgvector's text input format.

    Skipping pgvector.psycopg.register_vector() avoids a chicken-and-egg
    problem: the pool opens before migrations run, so the ``vector`` type
    OID isn't registered yet on those connections. String literals + an
    explicit ``::vector`` cast in SQL sidestep that entirely.
    """
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


def _candidate_pool(query_vec, *, pool_size: int) -> list[dict[str, Any]]:
    if not portal_db.is_enabled():
        return []
    v = _vector_literal(query_vec)
    # Restrict clip rows to the 'visual' embedding option so a video that
    # also has audio + transcription clips doesn't get 3x the surface area
    # in the candidate pool. Frames have a single embedding per row so
    # they're always allowed through. This mirrors the local
    # `build_segment_matrix(clip_options=("visual",))` behaviour.
    sql = """
        SELECT
            s3_key,
            kind,
            embedding_option,
            segment_index,
            frame_index,
            start_sec,
            end_sec,
            timestamp_sec,
            thumb_s3_key,
            1 - (embedding <=> %s::vector) AS score
        FROM embeddings
        WHERE kind = 'frame' OR embedding_option = 'visual'
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    pool = portal_db.get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (v, v, pool_size))
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "s3_key": r[0],
                "kind": r[1],
                "embedding_option": r[2],
                "segment_index": r[3],
                "frame_index": r[4],
                "start_sec": float(r[5]),
                "end_sec": float(r[6]),
                "timestamp_sec": float(r[7]),
                "thumb_s3_key": r[8],
                "score": float(r[9]),
            }
        )
    return out


def _refine_and_dedupe(
    candidates: list[dict[str, Any]],
    *,
    top_k: int,
    dedupe_window_sec: float,
) -> list[dict[str, Any]]:
    """Snap clip results to the best frame in their window, then dedupe.

    Same algorithm as ``scripts/embed/_lib.py::rank_results``, ported to
    operate on rows that already carry a ``score`` (because pgvector ranked
    them) instead of an in-memory matrix.
    """
    # Build a per-video index of frame candidates, sorted by timestamp so we
    # can early-exit on out-of-window frames.
    frames_by_video: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for c in candidates:
        if c["kind"] == "frame":
            frames_by_video.setdefault(c["s3_key"], []).append(
                (c["timestamp_sec"], c)
            )
    for v in frames_by_video.values():
        v.sort(key=lambda x: x[0])

    out: list[dict[str, Any]] = []
    seen_windows: dict[str, list[float]] = {}

    for c in candidates:  # already sorted by score DESC by the SQL ORDER BY
        timestamp = c["timestamp_sec"]
        thumb = c["thumb_s3_key"]
        # frame_index defaults to the candidate's own frame_index (set on
        # frame rows, NULL on clip rows); refinement may overwrite it with
        # the snapped frame's index.
        frame_index = c.get("frame_index")
        refined = False

        if c["kind"] == "clip" and c["end_sec"] > c["start_sec"]:
            best_score = -2.0
            best_ts: Optional[float] = None
            best_thumb: Optional[str] = None
            best_idx: Optional[int] = None
            for fts, fc in frames_by_video.get(c["s3_key"], []):
                if fts < c["start_sec"] - 1e-3:
                    continue
                if fts > c["end_sec"] + 1e-3:
                    break
                if fc["score"] > best_score:
                    best_score = fc["score"]
                    best_ts = fts
                    best_thumb = fc["thumb_s3_key"]
                    best_idx = fc.get("frame_index")
            if best_ts is not None:
                timestamp = best_ts
                thumb = best_thumb
                if best_idx is not None:
                    frame_index = best_idx
                refined = True

        # Walking score DESC means the first survivor in any window wins.
        windows = seen_windows.setdefault(c["s3_key"], [])
        if any(abs(t - timestamp) <= dedupe_window_sec for t in windows):
            continue
        windows.append(timestamp)

        out.append(
            {
                "score": c["score"],
                "kind": c["kind"],
                "s3_key": c["s3_key"],
                "segment_index": c["segment_index"],
                "frame_index": frame_index,
                "start_sec": c["start_sec"],
                "end_sec": c["end_sec"],
                "timestamp_sec": timestamp,
                "embedding_option": c["embedding_option"],
                "thumb_s3_key": thumb,
                "refined_from_frame": refined,
            }
        )
        if len(out) >= top_k:
            break

    return out


def _fetch_pegasus_index(
    s3_keys: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Pull every clip_descriptions row for the videos in this result page.

    Returns ``{s3_key: [row, ...]}`` sorted by ``start_sec``. Empty when
    the table doesn't exist yet (fresh cluster pre-D.5) or when no rows
    match. We swallow the relation-missing error so the search keeps
    working in environments where the migration hasn't run.
    """
    if not s3_keys or not portal_db.is_enabled():
        return {}
    pool = portal_db.get_pool()
    sql = """
        SELECT s3_key, start_sec, end_sec, prompt_id, message, model_id
        FROM clip_descriptions
        WHERE s3_key = ANY(%s)
        ORDER BY s3_key, start_sec, prompt_id
    """
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (s3_keys,))
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("clip_descriptions lookup failed: %s", exc)
        return {}

    out: dict[str, list[dict[str, Any]]] = {}
    for s3_key, start, end, prompt_id, message, model_id in rows:
        out.setdefault(s3_key, []).append(
            {
                "start_sec": float(start),
                "end_sec": float(end),
                "prompt_id": prompt_id,
                "message": message,
                "model_id": model_id,
            }
        )
    return out


def _find_pegasus_hit(
    candidates: list[dict[str, Any]],
    *,
    start_sec: float,
    end_sec: float,
    timestamp_sec: float,
    overlap_tolerance: float = 0.25,
) -> dict[str, Any] | None:
    """Pick the best clip_descriptions row for this result.

    Mirrors :func:`scripts.pegasus._lib.find_clip_text` so cloud and local
    UIs render the same text for the same row.

    Preference order:
      1. Inspector preset, then summary preset, then the rest.
      2. Exact ``(start_sec, end_sec)`` match within tolerance.
      3. Otherwise the description whose window contains
         ``timestamp_sec``.
      4. Otherwise the description with the largest overlap.
    """
    if not candidates:
        return None

    preset_priority = {"inspector": 0, "summary": 1}
    ranked = sorted(
        candidates,
        key=lambda c: preset_priority.get(c.get("prompt_id", ""), 99),
    )

    # 1. exact range match
    for c in ranked:
        if (
            abs(c["start_sec"] - start_sec) <= overlap_tolerance
            and abs(c["end_sec"] - end_sec) <= overlap_tolerance
        ):
            return c

    # 2. window contains the result's timestamp
    for c in ranked:
        if c["start_sec"] - overlap_tolerance <= timestamp_sec <= c["end_sec"] + overlap_tolerance:
            return c

    # 3. largest overlap with [start_sec, end_sec]
    best = None
    best_overlap = 0.0
    for c in ranked:
        ov = max(0.0, min(c["end_sec"], end_sec) - max(c["start_sec"], start_sec))
        if ov > best_overlap:
            best_overlap = ov
            best = c
    return best


def _attach_pegasus(
    results: list[dict[str, Any]],
    index: dict[str, list[dict[str, Any]]],
) -> None:
    for r in results:
        bucket = index.get(r["s3_key"], [])
        hit = _find_pegasus_hit(
            bucket,
            start_sec=float(r.get("start_sec", 0.0)),
            end_sec=float(r.get("end_sec", 0.0)),
            timestamp_sec=float(r.get("timestamp_sec", 0.0)),
        )
        if not hit:
            continue
        inherited = (
            r.get("kind") == "frame"
            or abs(hit["start_sec"] - float(r.get("start_sec", 0.0))) > 0.25
            or abs(hit["end_sec"] - float(r.get("end_sec", 0.0))) > 0.25
        )
        r["pegasus"] = {
            "text": hit["message"],
            "preset": hit["prompt_id"],
            "model": hit["model_id"],
            "clip_start_sec": hit["start_sec"],
            "clip_end_sec": hit["end_sec"],
            "inherited": bool(inherited),
        }


DEFAULT_DETECTION_PALETTE = [
    "#ff8c00",
    "#00e0ff",
    "#ff5cc6",
    "#a4ff5c",
    "#ffd166",
    "#9b8cff",
]


def _palette_color(idx: int) -> str:
    return DEFAULT_DETECTION_PALETTE[idx % len(DEFAULT_DETECTION_PALETTE)]


# YOLO_MODELS is the same JSON the yolo-detect worker consumes — Terraform
# pipes it into the portal task definition so the API can surface UI hints
# (mask_only, palette overrides) without re-deploying the worker.
_YOLO_MODELS_RAW = os.environ.get("YOLO_MODELS", "[]")


def _yolo_model_meta() -> dict[str, dict[str, Any]]:
    """Parse YOLO_MODELS into ``{name: {mask_only, classes, colors}}``.

    Returns an empty dict on parse failure so the portal still serves
    detections (just without per-model UI hints).
    """
    try:
        raw = json.loads(_YOLO_MODELS_RAW or "[]")
    except (TypeError, ValueError):
        logger.warning("YOLO_MODELS env var is not valid JSON; ignoring")
        return {}
    if not isinstance(raw, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue
        out[name] = {
            "mask_only": bool(entry.get("mask_only", False)),
            "classes": {
                str(k): str(v) for k, v in (entry.get("classes") or {}).items()
            },
            "colors": {
                str(k): str(v) for k, v in (entry.get("colors") or {}).items()
            },
        }
    return out


_MODEL_META = _yolo_model_meta()


def _model_is_mask_only(model_name: str) -> bool:
    meta = _MODEL_META.get(model_name)
    return bool(meta and meta.get("mask_only"))


def _fetch_detections_index(
    s3_keys: list[str],
    frame_indexes_by_key: dict[str, set[int]],
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Pull frame_detections rows for the (s3_key, frame_index) pairs we
    actually need to render. Returns a map keyed by ``(s3_key, frame_index)``.

    We swallow the relation-missing error so the search keeps working when
    the migration hasn't run yet (fresh cluster pre-D.6).
    """
    if not s3_keys or not portal_db.is_enabled():
        return {}
    pool = portal_db.get_pool()
    sql = """
        SELECT s3_key, frame_index, model_name, model_version,
               class_id, class_name, confidence, bbox_xyxy, polygon_xy
        FROM frame_detections
        WHERE s3_key = ANY(%s)
        ORDER BY s3_key, frame_index, model_name, confidence DESC
    """
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (s3_keys,))
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("frame_detections lookup failed: %s", exc)
        return {}

    out: dict[tuple[str, int], list[dict[str, Any]]] = {}
    color_index: dict[str, str] = {}
    next_color = 0
    for r in rows:
        key = (str(r[0]), int(r[1]))
        wanted = frame_indexes_by_key.get(key[0])
        if wanted is not None and key[1] not in wanted:
            continue
        class_name = str(r[5])
        model_name = str(r[2])
        if class_name not in color_index:
            color_index[class_name] = _palette_color(next_color)
            next_color += 1
        mask_only = _model_is_mask_only(model_name)
        # Mask-only detections (e.g. power lines) are noise without a polygon
        # — the bbox alone is one giant rectangle covering the whole frame.
        # Skip them entirely so the UI never shows a phantom box.
        polygon = list(r[8]) if r[8] is not None else []
        if mask_only and len(polygon) < 6:
            continue
        out.setdefault(key, []).append(
            {
                "model_name": model_name,
                "model_version": str(r[3]),
                "class_id": int(r[4]),
                "class_name": class_name,
                "confidence": float(r[6]),
                "bbox_xyxy": list(r[7]) if r[7] is not None else [],
                "polygon_xy": polygon,
                "color": color_index[class_name],
                "mask_only": mask_only,
            }
        )
    return out


def _attach_detections(
    results: list[dict[str, Any]],
    index: dict[tuple[str, int], list[dict[str, Any]]],
) -> None:
    if not index:
        for r in results:
            r["detections"] = []
            r["detection_classes"] = []
        return
    for r in results:
        idx = r.get("frame_index")
        if idx is None:
            r["detections"] = []
            r["detection_classes"] = []
            continue
        dets = index.get((r["s3_key"], int(idx)), [])
        r["detections"] = dets
        r["detection_classes"] = sorted({d["class_name"] for d in dets})


def detection_classes() -> dict[str, Any]:
    """Public catalogue endpoint payload for the UI's toggle bar.

    Walks the entire ``frame_detections`` table once (it's small) so the UI
    can pre-paint the chip strip even before the user runs a search. Falls
    back to an empty list when the migration hasn't run.
    """
    if not portal_db.is_enabled():
        return {"classes": [], "models": [], "status": "disabled"}
    pool = portal_db.get_pool()
    sql = """
        SELECT class_name, model_name, COUNT(*) AS n
        FROM frame_detections
        GROUP BY class_name, model_name
        ORDER BY n DESC
    """
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("detection_classes lookup failed: %s", exc)
        return {"classes": [], "models": [], "status": "missing"}

    class_counts: dict[str, int] = {}
    class_models: dict[str, set[str]] = {}
    model_counts: dict[str, int] = {}
    for class_name, model_name, n in rows:
        class_counts[class_name] = class_counts.get(class_name, 0) + int(n)
        class_models.setdefault(class_name, set()).add(model_name)
        model_counts[model_name] = model_counts.get(model_name, 0) + int(n)

    ordered_classes = sorted(class_counts.items(), key=lambda kv: -kv[1])
    classes_payload = []
    for idx, (cname, count) in enumerate(ordered_classes):
        classes_payload.append(
            {
                "class_name": cname,
                "color": _palette_color(idx),
                "count": int(count),
                "models": sorted(class_models.get(cname, set())),
            }
        )
    models_payload = [
        {
            "model_name": m,
            "count": int(c),
            "mask_only": _model_is_mask_only(m),
        }
        for m, c in sorted(model_counts.items(), key=lambda kv: -kv[1])
    ]
    return {
        "classes": classes_payload,
        "models": models_payload,
        "status": "ok",
    }


def _enrich(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach presigned URLs (with #t fragment), pre-generated Pegasus text,
    and YOLO detections."""
    s3 = _s3_client()
    for r in results:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": r["s3_key"]},
            ExpiresIn=PRESIGN_EXPIRES_SECONDS,
        )
        r["presigned_url"] = f"{url}#t={r['timestamp_sec']:.2f}"
        thumb_key = r.get("thumb_s3_key")
        if thumb_key:
            r["thumb_url"] = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": S3_BUCKET, "Key": thumb_key},
                ExpiresIn=PRESIGN_EXPIRES_SECONDS,
            )
        else:
            r["thumb_url"] = None

    s3_keys = sorted({r["s3_key"] for r in results})
    pegasus_index = _fetch_pegasus_index(s3_keys)
    _attach_pegasus(results, pegasus_index)

    frame_indexes_by_key: dict[str, set[int]] = {}
    for r in results:
        idx = r.get("frame_index")
        if idx is None:
            continue
        frame_indexes_by_key.setdefault(r["s3_key"], set()).add(int(idx))
    detections_index = _fetch_detections_index(s3_keys, frame_indexes_by_key)
    _attach_detections(results, detections_index)
    return results


def search(
    query_vec: list[float],
    *,
    top_k: int = 10,
    pool_size: Optional[int] = None,
    dedupe_window_sec: float = DEFAULT_DEDUPE_WINDOW_SEC,
) -> list[dict[str, Any]]:
    """Public entry point. Returns up to ``top_k`` enriched results."""
    if pool_size is None:
        pool_size = max(DEFAULT_CANDIDATE_POOL, top_k * 4)
    candidates = _candidate_pool(query_vec, pool_size=pool_size)
    if not candidates:
        return []
    ranked = _refine_and_dedupe(
        candidates, top_k=top_k, dedupe_window_sec=dedupe_window_sec
    )
    return _enrich(ranked)


def stats() -> dict[str, Any]:
    """Cheap status block for the Search UI's HUD."""
    base = {
        "model": MARENGO_INFERENCE_ID,
        "region": AWS_REGION,
        "bucket": S3_BUCKET,
    }
    if not portal_db.is_enabled():
        return {**base, "status": "disabled", "videos": 0, "clips": 0, "frames": 0}
    pool = portal_db.get_pool()
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM videos")
            n_videos = int(cur.fetchone()[0])
            cur.execute(
                "SELECT count(*) FILTER (WHERE kind = 'clip'),"
                "       count(*) FILTER (WHERE kind = 'frame') "
                "FROM embeddings"
            )
            n_clips, n_frames = cur.fetchone()
            cur.execute(
                "SELECT s3_key,"
                "       count(*) FILTER (WHERE kind = 'clip')  AS clips,"
                "       count(*) FILTER (WHERE kind = 'frame') AS frames "
                "FROM embeddings "
                "GROUP BY s3_key ORDER BY 1"
            )
            by_video = [
                {"s3_key": row[0], "clips": int(row[1]), "frames": int(row[2])}
                for row in cur.fetchall()
            ]
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "error", "detail": f"{exc.__class__.__name__}: {exc}"}
    return {
        **base,
        "status": "ok",
        "videos": n_videos,
        "clips": int(n_clips),
        "frames": int(n_frames),
        "by_video": by_video,
    }
