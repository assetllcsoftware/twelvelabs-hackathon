"""Persist Marengo async clip embeddings into Postgres.

Triggered by the EventBridge rule on S3 ``Object Created`` events whose key
ends with ``/output.json`` under ``embeddings/videos/``. Bedrock writes the
file at ``s3://bucket/<our-output-prefix>/<bedrock-jobId>/output.json`` once
the async invoke is ``Completed``.

We:
  1. Pull the JSON down from S3 (small — KBs).
  2. Extract our-uuid (parts[2] under ``embeddings/videos/<our-uuid>/...``)
     and look up the corresponding ``videos`` row by ``output_prefix``.
  3. L2-normalize each segment vector and upsert it into ``embeddings`` with
     ``kind='clip'``.
  4. Flip ``videos.status`` to ``clips_ready`` (or ``ready`` if frames are
     already in).

Idempotent on the natural-key unique index, so re-deliveries are harmless.
"""
from __future__ import annotations

import json
import logging
import math
import os
from typing import Any

import boto3
import pg8000.native

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ["AWS_REGION"]
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
EMBEDDING_OUTPUT_PREFIX = os.environ.get(
    "EMBEDDING_OUTPUT_PREFIX", "embeddings/videos/"
)

# Optional Pegasus dispatch — empty when the cluster/task hasn't been
# provisioned yet, in which case we just skip the launch step. Lets the
# clip-embed pipeline keep working in environments where D.5 is rolled
# back.
PEGASUS_ECS_CLUSTER = os.environ.get("PEGASUS_ECS_CLUSTER", "").strip()
PEGASUS_TASK_DEFINITION = os.environ.get("PEGASUS_TASK_DEFINITION", "").strip()
PEGASUS_SUBNETS = [
    s.strip() for s in os.environ.get("PEGASUS_SUBNETS", "").split(",") if s.strip()
]
PEGASUS_SECURITY_GROUP = os.environ.get("PEGASUS_SECURITY_GROUP", "").strip()
PEGASUS_CONTAINER_NAME = os.environ.get(
    "PEGASUS_CONTAINER_NAME", "clip-pegasus-worker"
)

s3 = boto3.client("s3", region_name=REGION)
secrets = boto3.client("secretsmanager", region_name=REGION)
ecs = boto3.client("ecs", region_name=REGION)

_db_conn: pg8000.native.Connection | None = None
_db_creds: dict[str, Any] | None = None


def _db() -> pg8000.native.Connection:
    global _db_conn, _db_creds
    if _db_creds is None:
        _db_creds = json.loads(
            secrets.get_secret_value(SecretId=DB_SECRET_ARN)["SecretString"]
        )
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


def _l2_normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(float(x) * float(x) for x in vec))
    if n == 0.0:
        return [float(x) for x in vec]
    return [float(x) / n for x in vec]


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _resolve_video_key(output_prefix: str) -> str | None:
    rows = _db().run(
        "SELECT s3_key FROM videos WHERE output_prefix = :p", p=output_prefix
    )
    if not rows:
        return None
    return rows[0][0]


def _upsert_clip(
    *,
    s3_key: str,
    segment_index: int,
    embedding_option: str,
    start_sec: float,
    end_sec: float,
    embedding_lit: str,
) -> None:
    timestamp = (start_sec + end_sec) / 2.0 if end_sec > start_sec else start_sec
    sql = (
        "INSERT INTO embeddings "
        "  (s3_key, kind, embedding_option, segment_index, frame_index, "
        "   start_sec, end_sec, timestamp_sec, thumb_s3_key, embedding) "
        "VALUES "
        "  (:k, 'clip', :opt, :si, NULL, :s, :e, :ts, NULL, "
        "   CAST(:emb AS vector)) "
        "ON CONFLICT (s3_key, kind, embedding_option, "
        "             COALESCE(segment_index, -1), COALESCE(frame_index, -1)) "
        "DO UPDATE SET start_sec=EXCLUDED.start_sec, "
        "             end_sec=EXCLUDED.end_sec, "
        "             timestamp_sec=EXCLUDED.timestamp_sec, "
        "             embedding=EXCLUDED.embedding"
    )
    _db().run(
        sql,
        k=s3_key,
        opt=embedding_option,
        si=segment_index,
        s=start_sec,
        e=end_sec,
        ts=timestamp,
        emb=embedding_lit,
    )


def _dispatch_pegasus(s3_key: str) -> dict[str, Any]:
    """Kick off the clip-pegasus Fargate task for this video, if configured.

    Designed to be best-effort: any AWS-side problem is logged and the
    finalize Lambda still returns success so the clip-embed pipeline
    doesn't get retried just because Pegasus is unavailable.
    """
    if not (
        PEGASUS_ECS_CLUSTER
        and PEGASUS_TASK_DEFINITION
        and PEGASUS_SUBNETS
        and PEGASUS_SECURITY_GROUP
    ):
        logger.info("pegasus dispatch disabled (no ECS env vars)")
        return {"dispatched": False, "reason": "disabled"}

    try:
        response = ecs.run_task(
            cluster=PEGASUS_ECS_CLUSTER,
            taskDefinition=PEGASUS_TASK_DEFINITION,
            launchType="FARGATE",
            platformVersion="LATEST",
            count=1,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": PEGASUS_SUBNETS,
                    "securityGroups": [PEGASUS_SECURITY_GROUP],
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": PEGASUS_CONTAINER_NAME,
                        "environment": [
                            {"name": "S3_KEY", "value": s3_key},
                        ],
                    }
                ]
            },
            propagateTags="TASK_DEFINITION",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("pegasus run_task failed: %s", exc)
        return {"dispatched": False, "reason": "exception", "error": str(exc)}

    tasks = [t["taskArn"] for t in response.get("tasks", [])]
    failures = response.get("failures", [])
    if failures:
        logger.warning("pegasus run_task partial failures: %s", failures)
    return {"dispatched": True, "tasks": tasks, "failures": failures}


def _flip_status(s3_key: str) -> None:
    """Move a video to ``clips_ready`` (or ``ready`` if frames are already in)."""
    _db().run(
        """
        UPDATE videos SET status = CASE
            WHEN status = 'frames_ready' THEN 'ready'
            ELSE 'clips_ready'
        END
        WHERE s3_key = :k
        """,
        k=s3_key,
    )


def lambda_handler(event: dict, context) -> dict:
    detail = event.get("detail") or {}
    bucket = (detail.get("bucket") or {}).get("name")
    key = (detail.get("object") or {}).get("key") or ""

    if not key.endswith("/output.json"):
        logger.info("skip: %s is not output.json", key)
        return {"skipped": "not-output-json"}
    if not key.startswith(EMBEDDING_OUTPUT_PREFIX):
        logger.info("skip: %s outside %s", key, EMBEDDING_OUTPUT_PREFIX)
        return {"skipped": "outside-prefix"}

    # embeddings/videos/<our-uuid>/<bedrock-id>/output.json
    parts = key.split("/")
    if len(parts) < 4:
        logger.info("skip: unexpected key shape %s", key)
        return {"skipped": "unexpected-shape"}
    our_uuid = parts[2]
    output_prefix = f"embeddings/videos/{our_uuid}"

    s3_key = _resolve_video_key(output_prefix)
    if not s3_key:
        logger.warning(
            "no videos row for output_prefix=%s; the start Lambda may have "
            "failed to write or the row was already cleaned up.",
            output_prefix,
        )
        return {"skipped": "no-video-row", "output_prefix": output_prefix}

    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    output = json.loads(body)
    segments = output.get("data") or []
    logger.info(
        "finalize %s segments for s3_key=%s output=%s",
        len(segments),
        s3_key,
        key,
    )

    upserted = 0
    skipped = 0
    for idx, seg in enumerate(segments):
        emb = seg.get("embedding")
        if not emb or len(emb) != 512:
            skipped += 1
            continue
        emb_norm = _l2_normalize(emb)
        emb_lit = _vec_literal(emb_norm)
        opt = seg.get("embeddingOption", "visual")
        start = float(seg.get("startSec", 0.0))
        end = float(seg.get("endSec", 0.0))
        _upsert_clip(
            s3_key=s3_key,
            segment_index=idx,
            embedding_option=opt,
            start_sec=start,
            end_sec=end,
            embedding_lit=emb_lit,
        )
        upserted += 1

    _flip_status(s3_key)

    pegasus = _dispatch_pegasus(s3_key)

    logger.info(
        "finalize done s3_key=%s upserted=%s skipped=%s pegasus=%s",
        s3_key,
        upserted,
        skipped,
        pegasus,
    )
    return {
        "s3_key": s3_key,
        "output_prefix": output_prefix,
        "segments_total": len(segments),
        "upserted": upserted,
        "skipped": skipped,
        "pegasus": pegasus,
    }
