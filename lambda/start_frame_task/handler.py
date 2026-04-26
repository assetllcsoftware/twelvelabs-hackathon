"""Kick off a Fargate frame-embed worker for a freshly-uploaded video.

Triggered by the same EventBridge rule as ``start_clip_embed`` so clip and
frame pipelines run in parallel. We do not touch Postgres here — the worker
itself will write rows. This keeps the Lambda zip dependency-free and the
cold-start very fast (no VPC attachment).
"""
from __future__ import annotations

import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ["AWS_REGION"]
BUCKET = os.environ["S3_BUCKET"]
ECS_CLUSTER = os.environ["ECS_CLUSTER"]
ECS_TASK_DEFINITION = os.environ["ECS_TASK_DEFINITION"]
ECS_SUBNETS = [s.strip() for s in os.environ["ECS_SUBNETS"].split(",") if s.strip()]
ECS_SECURITY_GROUP = os.environ["ECS_SECURITY_GROUP"]
WORKER_CONTAINER_NAME = os.environ["WORKER_CONTAINER_NAME"]

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}

ecs = boto3.client("ecs", region_name=REGION)


def _is_video_key(key: str) -> bool:
    if not key or key.endswith("/"):
        return False
    _, ext = os.path.splitext(key)
    return ext.lower() in VIDEO_EXTENSIONS


def lambda_handler(event: dict, context) -> dict:
    detail = event.get("detail") or {}
    bucket = (detail.get("bucket") or {}).get("name")
    key = (detail.get("object") or {}).get("key") or ""

    if bucket != BUCKET:
        logger.info("skip: bucket %s != %s", bucket, BUCKET)
        return {"skipped": "bucket-mismatch"}
    if not _is_video_key(key):
        logger.info("skip: %s is not a video", key)
        return {"skipped": "not-a-video", "key": key}

    logger.info("dispatch frame-embed worker for s3://%s/%s", bucket, key)

    response = ecs.run_task(
        cluster=ECS_CLUSTER,
        taskDefinition=ECS_TASK_DEFINITION,
        launchType="FARGATE",
        platformVersion="LATEST",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": ECS_SUBNETS,
                "securityGroups": [ECS_SECURITY_GROUP],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": WORKER_CONTAINER_NAME,
                    "environment": [
                        {"name": "S3_KEY", "value": key},
                    ],
                }
            ]
        },
        propagateTags="TASK_DEFINITION",
    )
    tasks = [t["taskArn"] for t in response.get("tasks", [])]
    failures = response.get("failures", [])
    if failures:
        logger.warning("run_task failures: %s", failures)
    return {"s3_key": key, "tasks": tasks, "failures": failures}
