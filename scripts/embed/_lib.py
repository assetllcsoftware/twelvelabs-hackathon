"""Shared helpers for the local Marengo embedding scripts.

Everything we need to: list videos in S3, kick off Marengo async video jobs,
poll them, fetch the resulting `output.json`, run sync text / image / text+image
embeddings, cache results on disk, and rebuild a numpy similarity matrix from
the cache.

No Postgres, no FastAPI, no notebook. Configured purely via environment
variables so the same code works on a laptop and (later) inside a Lambda or
ECS task.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

import boto3


REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "data" / "embeddings"
FRAMES_CACHE_DIR = CACHE_DIR / "frames"
FRAMES_THUMB_DIR = CACHE_DIR / "thumbs"

MARENGO_MODEL_ID = "twelvelabs.marengo-embed-3-0-v1:0"
# Cross-region inference profile ids. start_async_invoke wants the foundation
# model id; invoke_model wants the inference profile id.
MARENGO_INFERENCE_PROFILES: dict[str, str] = {
    "us-east-1": "us.twelvelabs.marengo-embed-3-0-v1:0",
    "eu-west-1": "eu.twelvelabs.marengo-embed-3-0-v1:0",
    "ap-northeast-2": "apac.twelvelabs.marengo-embed-3-0-v1:0",
}

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
DEFAULT_VIDEO_PREFIXES = ("raw-videos/", "video-clips/")
DEFAULT_OUTPUT_PREFIX = "embeddings/videos"
# Mirrors AWS's documented Marengo 3.0 default for video. Transcription pulls
# narration into the same 512-d space, which is what makes text→video search
# actually work for inspection-style footage with voiceover.
DEFAULT_EMBEDDING_OPTIONS = ("visual", "audio", "transcription")


@dataclass
class Config:
    region: str
    bucket: str
    inference_id: str
    account_id: str
    output_prefix: str = DEFAULT_OUTPUT_PREFIX


class ConfigError(RuntimeError):
    """Raised when env vars or AWS reachability needed to embed/search are missing."""


def die(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(key)
    return value if value else default


def load_config() -> Config:
    """Resolve env-driven config. Raises ConfigError on bad inputs.

    CLIs typically wrap this with ``load_config_or_die()`` so a friendly
    message ends up on stderr; the FastAPI server catches the exception and
    surfaces it as HTTP 500.
    """
    region = _env("AWS_REGION") or _env("AWS_DEFAULT_REGION") or "us-east-1"
    bucket = _env("S3_BUCKET")
    if not bucket:
        raise ConfigError(
            "S3_BUCKET env var is required. "
            "Try: export S3_BUCKET=$(terraform -chdir=infra output -raw bucket_name)"
        )
    inference_id = _env("MARENGO_INFERENCE_ID") or MARENGO_INFERENCE_PROFILES.get(region or "")
    if not inference_id:
        raise ConfigError(
            f"No Marengo inference profile mapped for region {region!r}. "
            "Set MARENGO_INFERENCE_ID explicitly."
        )
    try:
        account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    except Exception as exc:  # noqa: BLE001 — surface boto3/credential errors as ConfigError
        raise ConfigError(
            f"Could not call STS GetCallerIdentity ({exc.__class__.__name__}: {exc}). "
            "Are AWS credentials sourced? `set -a; source ./.aws-demo.env; set +a; unset AWS_PROFILE`"
        ) from exc
    return Config(region=region, bucket=bucket, inference_id=inference_id, account_id=account_id)


def load_config_or_die() -> Config:
    """CLI helper: load config or exit with a friendly stderr message."""
    try:
        return load_config()
    except ConfigError as exc:
        die(str(exc))
        raise  # unreachable, satisfies type checkers


def s3_client(region: str):
    return boto3.client("s3", region_name=region)


def bedrock_client(region: str):
    return boto3.client("bedrock-runtime", region_name=region)


def cache_path_for_key(s3_key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(s3_key.encode("utf-8")).hexdigest()[:24]
    return CACHE_DIR / f"{digest}.json"


def frames_cache_path_for(s3_key: str) -> Path:
    FRAMES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(s3_key.encode("utf-8")).hexdigest()[:24]
    return FRAMES_CACHE_DIR / f"{digest}.json"


def frames_thumb_dir_for(s3_key: str) -> Path:
    digest = hashlib.sha256(s3_key.encode("utf-8")).hexdigest()[:24]
    out = FRAMES_THUMB_DIR / digest
    out.mkdir(parents=True, exist_ok=True)
    return out


def list_video_keys(s3, bucket: str, prefixes: Iterable[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in prefixes:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                if Path(key).suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                out.append(
                    {
                        "key": key,
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                    }
                )
    return out


def start_video_embedding(
    bedrock,
    *,
    bucket: str,
    account_id: str,
    video_key: str,
    output_prefix: str,
    embedding_options: Iterable[str] = ("visual",),
    embedding_scope: Iterable[str] = ("clip",),
) -> tuple[str, str]:
    job_id = uuid.uuid4().hex
    output_dir = f"{output_prefix.rstrip('/')}/{job_id}"
    response = bedrock.start_async_invoke(
        modelId=MARENGO_MODEL_ID,
        modelInput={
            "inputType": "video",
            "video": {
                "mediaSource": {
                    "s3Location": {
                        "uri": f"s3://{bucket}/{video_key}",
                        "bucketOwner": account_id,
                    }
                },
                "embeddingOption": list(embedding_options),
                "embeddingScope": list(embedding_scope),
            },
        },
        outputDataConfig={
            "s3OutputDataConfig": {"s3Uri": f"s3://{bucket}/{output_dir}"},
        },
    )
    return response["invocationArn"], output_dir


def wait_for_async_output(
    bedrock,
    s3,
    *,
    bucket: str,
    output_dir: str,
    invocation_arn: str,
    poll_seconds: int = 8,
    on_status: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    while True:
        response = bedrock.get_async_invoke(invocationArn=invocation_arn)
        status = response["status"]
        if on_status:
            on_status(status)
        if status == "Completed":
            break
        if status in {"Failed", "Expired"}:
            raise RuntimeError(
                f"async invocation ended with {status}: {response.get('failureMessage', '')}"
            )
        time.sleep(poll_seconds)

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=output_dir):
        for obj in page.get("Contents", []) or []:
            if obj["Key"].endswith("output.json"):
                body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                return json.loads(body)
    raise RuntimeError(f"no output.json found under s3://{bucket}/{output_dir}/")


def invoke_text_embedding(bedrock, inference_id: str, text: str) -> list[dict[str, Any]]:
    return _invoke(bedrock, inference_id, {"inputType": "text", "text": {"inputText": text}})


def invoke_image_embedding(bedrock, inference_id: str, image_path: str) -> list[dict[str, Any]]:
    return _invoke(
        bedrock,
        inference_id,
        {
            "inputType": "image",
            "image": {"mediaSource": {"base64String": _b64(image_path)}},
        },
    )


def invoke_image_embedding_bytes(
    bedrock, inference_id: str, image_bytes: bytes
) -> list[dict[str, Any]]:
    """Same as invoke_image_embedding but takes raw bytes (no file roundtrip)."""
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return _invoke(
        bedrock,
        inference_id,
        {
            "inputType": "image",
            "image": {"mediaSource": {"base64String": encoded}},
        },
    )


def invoke_text_image_embedding(
    bedrock, inference_id: str, text: str, image_path: str
) -> list[dict[str, Any]]:
    return _invoke(
        bedrock,
        inference_id,
        {
            "inputType": "text_image",
            "text_image": {
                "inputText": text,
                "mediaSource": {"base64String": _b64(image_path)},
            },
        },
    )


def _b64(path: str) -> str:
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("utf-8")


def _invoke(bedrock, model_id: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    response = bedrock.invoke_model(modelId=model_id, body=json.dumps(body))
    payload = json.loads(response["body"].read().decode("utf-8"))
    return payload.get("data", [])


def save_video_cache(s3_key: str, raw_output: dict[str, Any]) -> Path:
    path = cache_path_for_key(s3_key)
    payload = {
        "s3_key": s3_key,
        "model": MARENGO_MODEL_ID,
        "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "segments": raw_output.get("data", []),
    }
    path.write_text(json.dumps(payload))
    return path


def iter_cached_videos() -> Iterator[dict[str, Any]]:
    if not CACHE_DIR.exists():
        return
    for path in sorted(CACHE_DIR.glob("*.json")):
        try:
            yield json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue


def save_frames_cache(s3_key: str, frames: list[dict[str, Any]], *, fps: float) -> Path:
    path = frames_cache_path_for(s3_key)
    payload = {
        "s3_key": s3_key,
        "model": MARENGO_MODEL_ID,
        "kind": "frames",
        "fps": fps,
        "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "frames": frames,
    }
    path.write_text(json.dumps(payload))
    return path


def iter_cached_frames() -> Iterator[dict[str, Any]]:
    if not FRAMES_CACHE_DIR.exists():
        return
    for path in sorted(FRAMES_CACHE_DIR.glob("*.json")):
        try:
            yield json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue


# ---------------------------------------------------------------------------
# Frame extraction (ffmpeg). Local-only utility; not used at search time.
# ---------------------------------------------------------------------------


def extract_frames_with_ffmpeg(
    video_path: str | Path,
    out_dir: str | Path,
    *,
    fps: float = 1.0,
    width: int = 720,
    quality: int = 4,
) -> list[tuple[float, Path]]:
    """Extract frames at ``fps`` Hz, scaled to ``width`` px wide, into ``out_dir``.

    Returns a list of (timestamp_sec, frame_path) sorted by timestamp.
    Existing frames are reused if already present.
    """
    import subprocess

    video_path = Path(video_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pattern = out_dir / "frame_%05d.jpg"
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"fps={fps},scale={width}:-2",
        "-q:v",
        str(quality),
        str(pattern),
    ]
    # Skip ffmpeg if frames already exist for this fps. Cheap idempotency.
    existing = sorted(out_dir.glob("frame_*.jpg"))
    if not existing:
        subprocess.run(cmd, check=True)
        existing = sorted(out_dir.glob("frame_*.jpg"))

    # ffmpeg's fps filter emits frame N at time (N - 0.5) / fps for the first
    # frame, but in practice setting a target fps maps frame_00001 -> 0/fps,
    # frame_00002 -> 1/fps, etc. for input streams without B-frames at the
    # boundary. Close enough for seek-to-moment UX; a user-visible drift of
    # <0.5s within a 6s clip is invisible.
    out: list[tuple[float, Path]] = []
    step = 1.0 / fps if fps > 0 else 1.0
    for idx, p in enumerate(existing):
        out.append((idx * step, p))
    return out


def build_segment_matrix(
    *,
    include_frames: bool = True,
    clip_options: tuple[str, ...] = ("visual",),
):
    """Read every cached video (and frame) embedding and return (matrix, meta).

    Matrix is L2-normalized so a dot product against a normalized query vector
    yields cosine similarity directly.

    ``clip_options`` filters which clip-level ``embeddingOption`` rows
    contribute to the matrix. Defaults to ``("visual",)`` because mixing
    audio + transcription unfairly weighted any video that happened to be
    embedded under all three options vs. ones cached as visual-only —
    transcription/audio vectors would 3x the row count for narrated
    videos and dominate the rankings. The dropped options are still
    persisted in ``data/embeddings/*.json`` and can be re-enabled by
    passing e.g. ``("visual", "audio", "transcription")``.

    Each ``meta`` row carries:
      - kind: "clip" | "frame"
      - s3_key, timestamp_sec, start_sec, end_sec
      - segment_index / frame_index
      - embedding_option (clips: visual/audio/transcription; frames: "frame")
      - frame_thumb_rel: relative path (POSIX) under data/embeddings/thumbs/
        for frames; absent for clips. Servers can map this to a static URL.
    """
    import numpy as np

    rows: list[list[float]] = []
    meta: list[dict[str, Any]] = []
    allowed_clip_options = set(clip_options) if clip_options else None

    for video in iter_cached_videos():
        for idx, segment in enumerate(video.get("segments", []) or []):
            embedding = segment.get("embedding")
            if not embedding or len(embedding) != 512:
                continue
            option = segment.get("embeddingOption", "visual")
            if allowed_clip_options is not None and option not in allowed_clip_options:
                continue
            start = float(segment.get("startSec", 0.0))
            end = float(segment.get("endSec", 0.0))
            rows.append(embedding)
            meta.append(
                {
                    "kind": "clip",
                    "s3_key": video["s3_key"],
                    "segment_index": idx,
                    "frame_index": None,
                    "start_sec": start,
                    "end_sec": end,
                    # Default seek point for a clip is its midpoint; this gets
                    # overridden by the search layer when frame embeddings let
                    # us pick a more precise moment within [start, end].
                    "timestamp_sec": (start + end) / 2.0 if end > start else start,
                    "embedding_option": segment.get("embeddingOption", "visual"),
                    "embedding_scope": segment.get("embeddingScope", "clip"),
                }
            )

    if include_frames:
        for video in iter_cached_frames():
            s3_key = video["s3_key"]
            digest = hashlib.sha256(s3_key.encode("utf-8")).hexdigest()[:24]
            for idx, frame in enumerate(video.get("frames", []) or []):
                embedding = frame.get("embedding")
                if not embedding or len(embedding) != 512:
                    continue
                ts = float(frame.get("timestamp_sec", 0.0))
                thumb_name = frame.get("thumb_name")
                rows.append(embedding)
                meta.append(
                    {
                        "kind": "frame",
                        "s3_key": s3_key,
                        "segment_index": None,
                        "frame_index": idx,
                        "start_sec": ts,
                        "end_sec": ts,
                        "timestamp_sec": ts,
                        "embedding_option": "frame",
                        "embedding_scope": "frame",
                        "frame_thumb_rel": (
                            f"{digest}/{thumb_name}" if thumb_name else None
                        ),
                    }
                )

    if not rows:
        return np.zeros((0, 512), dtype="float32"), meta

    matrix = np.asarray(rows, dtype="float32")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms, meta


def normalize(vec):
    import numpy as np

    arr = np.asarray(vec, dtype="float32")
    norm = float(np.linalg.norm(arr))
    if norm == 0:
        return arr
    return arr / norm


def presigned_get(s3, bucket: str, key: str, expires: int = 3600) -> str:
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires
    )


# ---------------------------------------------------------------------------
# Search-time ranking. Shared by the CLI (search.py) and FastAPI app (serve.py)
# so they always agree on what "top match" means.
# ---------------------------------------------------------------------------


def _index_frames_by_video(meta: list[dict[str, Any]]):
    """Group meta indices for kind=="frame" rows by s3_key.

    Returns ``{s3_key: list[(timestamp_sec, matrix_index)]}`` sorted by
    timestamp_sec ascending.
    """
    out: dict[str, list[tuple[float, int]]] = {}
    for i, m in enumerate(meta):
        if m.get("kind") != "frame":
            continue
        out.setdefault(m["s3_key"], []).append((float(m["timestamp_sec"]), i))
    for v in out.values():
        v.sort()
    return out


def rank_results(
    matrix,
    meta: list[dict[str, Any]],
    query_vec,
    *,
    top_k: int = 10,
    dedupe_window_sec: float = 3.0,
) -> list[dict[str, Any]]:
    """Rank ``matrix`` rows by cosine vs ``query_vec`` and shape them for the UI.

    Behaviours:
      * Both clip rows and frame rows participate in the ranking. Frame rows
        give precise seek points; clip rows give broader (audio/transcription)
        signal.
      * For every clip in the result list we look at the frame embeddings of
        the same video that fall inside ``[clip.start_sec, clip.end_sec]`` and
        pick the highest-scoring one against the query. That frame's
        ``timestamp_sec`` (and thumbnail) overrides the clip's seek point so
        the preview jumps to the actual matching moment.
      * After refinement we dedupe hits that fall within ``dedupe_window_sec``
        of each other inside the same video, keeping the higher-scoring hit.
    """
    import numpy as np

    if matrix.shape[0] == 0:
        return []

    q = normalize(query_vec)
    scores = matrix @ q
    order = np.argsort(-scores)

    frames_by_video = _index_frames_by_video(meta)

    seen_windows: dict[str, list[float]] = {}
    out: list[dict[str, Any]] = []

    for idx in order:
        i = int(idx)
        m = meta[i]
        score = float(scores[i])
        s3_key = m["s3_key"]
        kind = m.get("kind", "clip")

        timestamp = float(m.get("timestamp_sec", m.get("start_sec", 0.0)))
        start_sec = float(m.get("start_sec", timestamp))
        end_sec = float(m.get("end_sec", timestamp))
        thumb_rel: Optional[str] = m.get("frame_thumb_rel")
        refined_from_frame = False

        if kind == "clip" and end_sec > start_sec:
            best_frame_score = -2.0
            best_frame_ts: Optional[float] = None
            best_frame_thumb: Optional[str] = None
            for ts, midx in frames_by_video.get(s3_key, []):
                if ts < start_sec - 0.001:
                    continue
                if ts > end_sec + 0.001:
                    break
                fscore = float(scores[midx])
                if fscore > best_frame_score:
                    best_frame_score = fscore
                    best_frame_ts = ts
                    best_frame_thumb = meta[midx].get("frame_thumb_rel")
            if best_frame_ts is not None:
                timestamp = best_frame_ts
                thumb_rel = best_frame_thumb
                refined_from_frame = True

        # Dedupe: drop hits within ``dedupe_window_sec`` of an already-kept
        # hit from the same video. Because we're walking sorted by score
        # descending, the kept hit is always the highest-scoring one in its
        # window.
        windows = seen_windows.setdefault(s3_key, [])
        if any(abs(ts - timestamp) <= dedupe_window_sec for ts in windows):
            continue
        windows.append(timestamp)

        out.append(
            {
                "score": score,
                "kind": kind,
                "s3_key": s3_key,
                "segment_index": m.get("segment_index"),
                "frame_index": m.get("frame_index"),
                "start_sec": start_sec,
                "end_sec": end_sec,
                "timestamp_sec": timestamp,
                "embedding_option": m.get("embedding_option"),
                "refined_from_frame": refined_from_frame,
                "thumb_rel": thumb_rel,
            }
        )

        if len(out) >= top_k:
            break

    return out
