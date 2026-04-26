"""Frame extraction + embedding worker (Fargate one-shot).

Triggered by the ``start_frame_task`` Lambda which runs us via
``ecs.run_task()`` and overrides the ``S3_KEY`` env var with the source
video's key. We:

  1. Download the source from S3 to ephemeral storage.
  2. Run ffmpeg at ``FPS`` Hz, scaled to ``WIDTH``px wide, into a scratch dir.
  3. For each frame: call Bedrock ``invoke_model`` (sync image embedding via
     the cross-region inference profile) and PUT the JPEG to S3 under
     ``embeddings/frames/<digest>/frame_NNNNN.jpg`` — same key shape that the
     portal's ``app/search.py`` presigns.
  4. Open one Postgres connection and upsert all the frame rows in a single
     transaction (one ``INSERT ... ON CONFLICT`` per row keeps it idempotent).
  5. Flip ``videos.status`` to ``frames_ready`` (or ``ready`` if clips already
     landed).

We use a thread pool for the embed+upload step because both calls are
network-bound; the Marengo sync API is happy at 8-16 concurrent invocations
on a single account.
"""
from __future__ import annotations

import base64
import concurrent.futures as cf
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import boto3
import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("frame_embed_worker")

REGION = os.environ["AWS_REGION"]
BUCKET = os.environ["S3_BUCKET"]
S3_KEY = os.environ["S3_KEY"]
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
MARENGO_INFERENCE_ID = os.environ.get(
    "MARENGO_INFERENCE_ID", "us.twelvelabs.marengo-embed-3-0-v1:0"
)
FPS = float(os.environ.get("FPS", "1.0"))
WIDTH = int(os.environ.get("WIDTH", "720"))
QUALITY = int(os.environ.get("QUALITY", "4"))
PARALLEL = int(os.environ.get("PARALLEL", "8"))
THUMB_PREFIX = os.environ.get("THUMB_PREFIX", "embeddings/frames").strip("/")

s3 = boto3.client("s3", region_name=REGION)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
secrets = boto3.client("secretsmanager", region_name=REGION)


def _db_url() -> str:
    secret = json.loads(
        secrets.get_secret_value(SecretId=DB_SECRET_ARN)["SecretString"]
    )
    return secret["url"]


def _digest(s3_key: str) -> str:
    return hashlib.sha256(s3_key.encode("utf-8")).hexdigest()[:24]


def _l2_normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(float(x) * float(x) for x in vec))
    if n == 0.0:
        return [float(x) for x in vec]
    return [float(x) / n for x in vec]


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _embed_image(image_bytes: bytes) -> list[float]:
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    response = bedrock.invoke_model(
        modelId=MARENGO_INFERENCE_ID,
        body=json.dumps(
            {
                "inputType": "image",
                "image": {"mediaSource": {"base64String": encoded}},
            }
        ),
    )
    payload = json.loads(response["body"].read().decode("utf-8"))
    data = payload.get("data") or []
    if not data:
        raise RuntimeError("Bedrock returned an empty embedding payload")
    return data[0]["embedding"]


def _process_frame(idx_path_ts: tuple[int, Path, float], digest: str) -> dict[str, Any]:
    idx, frame_path, ts = idx_path_ts
    with open(frame_path, "rb") as fh:
        img = fh.read()
    embedding = _embed_image(img)
    thumb_key = f"{THUMB_PREFIX}/{digest}/{frame_path.name}"
    s3.put_object(
        Bucket=BUCKET,
        Key=thumb_key,
        Body=img,
        ContentType="image/jpeg",
    )
    return {
        "frame_index": idx,
        "timestamp_sec": round(ts, 3),
        "thumb_s3_key": thumb_key,
        "embedding": embedding,
    }


def _extract_frames(video_path: Path, out_dir: Path) -> list[tuple[int, Path, float]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "frame_%05d.jpg"
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-y",
        "-i", str(video_path),
        "-vf", f"fps={FPS},scale={WIDTH}:-2",
        "-q:v", str(QUALITY),
        str(pattern),
    ]
    logger.info("ffmpeg: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    files = sorted(out_dir.glob("frame_*.jpg"))
    step = 1.0 / FPS if FPS > 0 else 1.0
    # Same convention as scripts/embed/_lib.py: frame N maps to (N-1)/fps.
    # Real-world drift versus PTS is well under a second for our 1 fps target,
    # which is the resolution of timestamp_sec anyway.
    return [(i, p, i * step) for i, p in enumerate(files)]


def _upsert_video(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO videos (s3_key, bucket, model_id, status)
            VALUES (%s, %s, %s, 'frame_embedding')
            ON CONFLICT (s3_key) DO UPDATE SET
                bucket = EXCLUDED.bucket,
                model_id = COALESCE(videos.model_id, EXCLUDED.model_id),
                status = CASE
                    WHEN videos.status IN ('clips_ready', 'ready') THEN videos.status
                    ELSE 'frame_embedding'
                END
            """,
            (S3_KEY, BUCKET, MARENGO_INFERENCE_ID),
        )


def _upsert_frames(conn, records: list[dict[str, Any]]) -> int:
    sql = """
        INSERT INTO embeddings (
            s3_key, kind, embedding_option,
            segment_index, frame_index,
            start_sec, end_sec, timestamp_sec,
            thumb_s3_key, embedding
        )
        VALUES (%s, 'frame', 'frame', NULL, %s, %s, %s, %s, %s, %s::vector)
        ON CONFLICT (s3_key, kind, embedding_option,
                     COALESCE(segment_index, -1), COALESCE(frame_index, -1))
        DO UPDATE SET
            start_sec = EXCLUDED.start_sec,
            end_sec = EXCLUDED.end_sec,
            timestamp_sec = EXCLUDED.timestamp_sec,
            thumb_s3_key = EXCLUDED.thumb_s3_key,
            embedding = EXCLUDED.embedding
    """
    upserted = 0
    with conn.cursor() as cur:
        for r in records:
            emb = _vec_literal(_l2_normalize(r["embedding"]))
            cur.execute(
                sql,
                (
                    S3_KEY,
                    r["frame_index"],
                    r["timestamp_sec"],
                    r["timestamp_sec"],
                    r["timestamp_sec"],
                    r["thumb_s3_key"],
                    emb,
                ),
            )
            upserted += 1
    return upserted


def _flip_status(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE videos SET status = CASE
                WHEN status = 'clips_ready' THEN 'ready'
                ELSE 'frames_ready'
            END
            WHERE s3_key = %s
            """,
            (S3_KEY,),
        )


def main() -> int:
    started = time.time()
    digest = _digest(S3_KEY)
    logger.info(
        "frame-embed start s3_key=%s digest=%s fps=%s width=%s parallel=%s",
        S3_KEY, digest, FPS, WIDTH, PARALLEL,
    )

    tmp = Path(tempfile.mkdtemp(prefix="frames-"))
    try:
        local = tmp / Path(S3_KEY).name
        logger.info("download s3://%s/%s -> %s", BUCKET, S3_KEY, local)
        s3.download_file(BUCKET, S3_KEY, str(local))

        thumbs_dir = tmp / "thumbs"
        frames = _extract_frames(local, thumbs_dir)
        logger.info("extracted %d frames", len(frames))
        if not frames:
            logger.error("ffmpeg produced no frames; aborting")
            return 1

        records: list[dict[str, Any]] = []
        with cf.ThreadPoolExecutor(max_workers=PARALLEL) as ex:
            futures = {ex.submit(_process_frame, f, digest): f for f in frames}
            for fu in cf.as_completed(futures):
                f = futures[fu]
                try:
                    records.append(fu.result())
                except Exception as exc:
                    logger.exception("embed/upload failed for frame %s: %s", f[0], exc)
        records.sort(key=lambda r: r["frame_index"])
        logger.info("embedded+uploaded %d/%d frames", len(records), len(frames))

        if not records:
            logger.error("no frames embedded successfully; aborting")
            return 2

        with psycopg.connect(_db_url(), sslmode="require") as conn:
            _upsert_video(conn)
            n = _upsert_frames(conn, records)
            _flip_status(conn)
            conn.commit()
        logger.info("upserted %d frame rows into embeddings", n)

        elapsed = time.time() - started
        logger.info("frame-embed done in %.1fs s3_key=%s", elapsed, S3_KEY)
        return 0
    finally:
        try:
            shutil.rmtree(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
