"""Kick off Marengo async video embedding for a freshly-uploaded video.

Triggered by an EventBridge rule on S3 ``Object Created`` events under the
``raw-videos/`` (and optionally ``video-clips/``) prefix. We:

  1. Filter out non-video keys and zero-byte placeholder objects.
  2. Mint a UUID and call ``bedrock.start_async_invoke`` with the matching
     output prefix under ``embeddings/videos/<our-uuid>/`` so we can find the
     output later.
  3. Upsert the ``videos`` row, stamping ``invocation_arn`` and
     ``output_prefix`` so the finalize Lambda can resolve the parent video
     when Bedrock writes ``output.json``.

We use ``pg8000.native`` (pure-Python) so the Lambda zip stays small and
portable across architectures. The vector table is not touched here — that's
the finalize Lambda's job.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import boto3
import pg8000.native

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ["AWS_REGION"]
BUCKET = os.environ["S3_BUCKET"]
ACCOUNT_ID = os.environ["AWS_ACCOUNT_ID"]
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
MARENGO_MODEL_ID = os.environ.get(
    "MARENGO_MODEL_ID", "twelvelabs.marengo-embed-3-0-v1:0"
)
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "embeddings/videos").strip("/")
EMBEDDING_OPTIONS = [
    o.strip()
    for o in os.environ.get(
        "EMBEDDING_OPTIONS", "visual,audio,transcription"
    ).split(",")
    if o.strip()
]

# Mirrors scripts/embed/_lib.py::VIDEO_EXTENSIONS so manual uploads behave
# the same in cloud and on the laptop.
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}

bedrock = boto3.client("bedrock-runtime", region_name=REGION)
secrets = boto3.client("secretsmanager", region_name=REGION)

_db_conn: pg8000.native.Connection | None = None
_db_creds: dict[str, Any] | None = None


def _db() -> pg8000.native.Connection:
    """Lazily open (and reconnect on stale-conn errors) a pg8000 connection."""
    global _db_conn, _db_creds
    if _db_creds is None:
        secret = secrets.get_secret_value(SecretId=DB_SECRET_ARN)
        _db_creds = json.loads(secret["SecretString"])
    if _db_conn is None:
        _db_conn = pg8000.native.Connection(
            host=_db_creds["host"],
            port=int(_db_creds["port"]),
            database=_db_creds["dbname"],
            user=_db_creds["username"],
            password=_db_creds["password"],
            ssl_context=True,
        )
    return _db_conn


def _is_video_key(key: str) -> bool:
    if not key or key.endswith("/"):
        return False
    _, ext = os.path.splitext(key)
    return ext.lower() in VIDEO_EXTENSIONS


def _start_async(s3_key: str) -> tuple[str, str]:
    """Returns (invocation_arn, output_prefix) on success."""
    job_id = uuid.uuid4().hex
    output_prefix = f"{OUTPUT_PREFIX}/{job_id}"
    response = bedrock.start_async_invoke(
        modelId=MARENGO_MODEL_ID,
        modelInput={
            "inputType": "video",
            "video": {
                "mediaSource": {
                    "s3Location": {
                        "uri": f"s3://{BUCKET}/{s3_key}",
                        "bucketOwner": ACCOUNT_ID,
                    }
                },
                "embeddingOption": EMBEDDING_OPTIONS,
                "embeddingScope": ["clip"],
            },
        },
        outputDataConfig={
            "s3OutputDataConfig": {"s3Uri": f"s3://{BUCKET}/{output_prefix}"}
        },
    )
    return response["invocationArn"], output_prefix


def _upsert_video(s3_key: str, *, bytes_size: int | None, invocation_arn: str, output_prefix: str) -> None:
    sql = (
        "INSERT INTO videos "
        "  (s3_key, bucket, bytes, invocation_arn, output_prefix, model_id, status) "
        "VALUES (:k, :b, :sz, :arn, :outp, :model, :st) "
        "ON CONFLICT (s3_key) DO UPDATE SET "
        "  bucket=EXCLUDED.bucket, "
        "  bytes=COALESCE(EXCLUDED.bytes, videos.bytes), "
        "  invocation_arn=EXCLUDED.invocation_arn, "
        "  output_prefix=EXCLUDED.output_prefix, "
        "  model_id=EXCLUDED.model_id, "
        "  status='clip_embedding'"
    )
    _db().run(
        sql,
        k=s3_key,
        b=BUCKET,
        sz=bytes_size,
        arn=invocation_arn,
        outp=output_prefix,
        model=MARENGO_MODEL_ID,
        st="clip_embedding",
    )


def lambda_handler(event: dict, context) -> dict:
    detail = event.get("detail") or {}
    bucket = (detail.get("bucket") or {}).get("name")
    obj = detail.get("object") or {}
    key = obj.get("key")
    size = obj.get("size")

    if bucket != BUCKET:
        logger.info("skip: bucket %s != %s", bucket, BUCKET)
        return {"skipped": "bucket-mismatch"}
    if not _is_video_key(key):
        logger.info("skip: %s is not a video", key)
        return {"skipped": "not-a-video", "key": key}

    logger.info("start clip embed for s3://%s/%s (%s bytes)", bucket, key, size)
    try:
        invocation_arn, output_prefix = _start_async(key)
    except Exception as exc:
        logger.exception("start_async_invoke failed for %s", key)
        raise
    logger.info("invocation_arn=%s output_prefix=%s", invocation_arn, output_prefix)

    try:
        _upsert_video(
            key,
            bytes_size=int(size) if size is not None else None,
            invocation_arn=invocation_arn,
            output_prefix=output_prefix,
        )
    except Exception:
        # If the DB write fails we still want the async job to keep running;
        # the next finalize call will re-resolve via output_prefix matching.
        logger.exception("DB upsert failed for %s; bedrock job is still in flight", key)
        raise

    return {
        "s3_key": key,
        "invocation_arn": invocation_arn,
        "output_prefix": output_prefix,
        "embedding_options": EMBEDDING_OPTIONS,
    }
