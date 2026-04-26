"""Per-clip Pegasus video-text worker (Fargate one-shot).

Mirrors :mod:`scripts.pegasus.pregenerate` but runs in the cloud:

  1. Look up every clip already written for ``S3_KEY`` (Marengo's
     ``finalize_clip_embed`` populates the ``embeddings`` table before this
     worker is dispatched).
  2. Download the source video from S3 to ephemeral storage.
  3. ffmpeg-cut each unique ``(start_sec, end_sec)`` into a small mp4.
  4. Upload the cut to ``derived/clips/<digest>/clip_<startms>_<endms>.mp4``.
  5. Call Pegasus (``invoke_model_with_response_stream``) on the cut clip
     and stream the answer.
  6. Upsert the result into ``clip_descriptions``.

We process clips serially because Pegasus' bedrock-runtime quota is per
account (and one Pegasus call per clip already saturates the relevant
quota for the demo). Adding a small ThreadPoolExecutor here would be a
mechanical change if/when we scale to many videos.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

import boto3
import psycopg


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("clip_pegasus_worker")


# ---------------------------------------------------------------------------
# Config (env)
# ---------------------------------------------------------------------------

REGION = os.environ["AWS_REGION"]
BUCKET = os.environ["S3_BUCKET"]
S3_KEY = os.environ["S3_KEY"]
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
PEGASUS_INFERENCE_ID = os.environ.get(
    "PEGASUS_INFERENCE_ID", "us.twelvelabs.pegasus-1-2-v1:0"
)
DERIVED_CLIPS_PREFIX = os.environ.get("DERIVED_CLIPS_PREFIX", "derived/clips").strip("/")
PROMPT_ID = os.environ.get("PEGASUS_PROMPT_ID", "inspector").strip() or "inspector"
PROMPT_TEXT_OVERRIDE = os.environ.get("PEGASUS_PROMPT", "").strip()
TEMPERATURE = float(os.environ.get("PEGASUS_TEMPERATURE", "0.0"))
FFMPEG_CRF = int(os.environ.get("FFMPEG_CRF", "23"))


# Curated prompts. Must mirror :mod:`scripts.pegasus._lib.PRESET_PROMPTS` so
# the cloud writes the *same* text the local UI renders. Keep both in sync.
_PRESET_PROMPTS: dict[str, str] = {
    "inspector": (
        "You are an energy-grid inspector. List up to 5 specific concerns "
        "visible in this aerial video that a maintenance crew should "
        "investigate. Format each as: '- <concern>: <where in the frame "
        "/ approx timestamp>'. If you see no concerns, say 'No issues "
        "detected.'"
    ),
    "summary": (
        "You are assisting an energy-grid inspection workflow. In 3-5 "
        "sentences, describe what is visible in this video and call out "
        "anything that looks relevant to power-line health: vegetation "
        "encroachment, sagging or damaged conductors, leaning poles, "
        "transformer or insulator condition, thermal anomalies, or other "
        "hazards. Be concrete; do not speculate beyond what is visible."
    ),
    "hashtags": (
        "Generate 6-10 lowercase hashtags that capture the main topics, "
        "objects, and conditions visible in this video. Return them on "
        "one line separated by single spaces. No commentary."
    ),
    "highlights": (
        "List the key moments in this video as a chronological list. "
        "Each entry should be: '- [MM:SS] <one-sentence description of "
        "what happens / what is visible>'. Cap the list at 8 entries."
    ),
}


def _resolve_prompt() -> str:
    if PROMPT_TEXT_OVERRIDE:
        return PROMPT_TEXT_OVERRIDE
    prompt = _PRESET_PROMPTS.get(PROMPT_ID)
    if prompt is None:
        raise RuntimeError(
            f"unknown PEGASUS_PROMPT_ID={PROMPT_ID!r} and PEGASUS_PROMPT was empty"
        )
    return prompt


# ---------------------------------------------------------------------------
# AWS clients (one each, reused for every clip)
# ---------------------------------------------------------------------------

s3 = boto3.client("s3", region_name=REGION)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
secrets = boto3.client("secretsmanager", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _db_url() -> str:
    payload = json.loads(
        secrets.get_secret_value(SecretId=DB_SECRET_ARN)["SecretString"]
    )
    return payload["url"]


def _account_id() -> str:
    return sts.get_caller_identity()["Account"]


def _list_clips(conn) -> list[tuple[float, float]]:
    """Return every unique ``(start_sec, end_sec)`` pair already embedded
    for this video. Visual / audio / transcription share time ranges, so
    we DISTINCT them here.
    """
    sql = (
        "SELECT DISTINCT start_sec, end_sec FROM embeddings "
        "WHERE s3_key = %s AND kind = 'clip' ORDER BY start_sec"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (S3_KEY,))
        rows = cur.fetchall()
    return [(float(r[0]), float(r[1])) for r in rows if float(r[1]) > float(r[0])]


def _existing_descriptions(conn, prompt_id: str) -> set[tuple[float, float]]:
    sql = (
        "SELECT start_sec, end_sec FROM clip_descriptions "
        "WHERE s3_key = %s AND prompt_id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (S3_KEY, prompt_id))
        return {(float(r[0]), float(r[1])) for r in cur.fetchall()}


def _upsert_description(
    conn,
    *,
    start_sec: float,
    end_sec: float,
    clip_s3_key: str,
    prompt_id: str,
    prompt: str,
    message: str,
    model_id: str,
) -> None:
    sql = """
        INSERT INTO clip_descriptions (
            s3_key, start_sec, end_sec, clip_s3_key,
            prompt_id, prompt, message, model_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (s3_key, start_sec, end_sec, prompt_id)
        DO UPDATE SET
            clip_s3_key = EXCLUDED.clip_s3_key,
            prompt      = EXCLUDED.prompt,
            message     = EXCLUDED.message,
            model_id    = EXCLUDED.model_id
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                S3_KEY,
                start_sec,
                end_sec,
                clip_s3_key,
                prompt_id,
                prompt,
                message,
                model_id,
            ),
        )


def _clip_keys(start_sec: float, end_sec: float) -> tuple[str, str]:
    digest = _digest(S3_KEY)
    name = (
        f"clip_{int(round(start_sec * 1000)):07d}"
        f"_{int(round(end_sec * 1000)):07d}.mp4"
    )
    return f"{DERIVED_CLIPS_PREFIX}/{digest}/{name}", name


def _cut_clip(source_path: Path, *, start_sec: float, end_sec: float, out_path: Path) -> None:
    duration = max(0.05, end_sec - start_sec)
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-y",
        "-ss", f"{start_sec:.3f}",
        "-i", str(source_path),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(FFMPEG_CRF),
        "-c:a", "aac",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


def _s3_object_exists(*, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001 — 404 is a ClientError; we just want a bool
        return False


def _stream_pegasus(
    *,
    account_id: str,
    clip_s3_key: str,
    prompt: str,
) -> str:
    body = {
        "inputPrompt": prompt,
        "mediaSource": {
            "s3Location": {
                "uri": f"s3://{BUCKET}/{clip_s3_key}",
                "bucketOwner": account_id,
            }
        },
        "temperature": TEMPERATURE,
    }
    response = bedrock.invoke_model_with_response_stream(
        modelId=PEGASUS_INFERENCE_ID,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    chunks: list[str] = []
    for event in response["body"]:
        chunk = event.get("chunk")
        if not chunk:
            continue
        payload = json.loads(chunk["bytes"])
        text = payload.get("message")
        if text:
            chunks.append(text)
    return "".join(chunks)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    started = time.time()
    prompt = _resolve_prompt()
    prompt_id = PROMPT_ID
    digest = _digest(S3_KEY)

    logger.info(
        "clip-pegasus start s3_key=%s digest=%s prompt_id=%s model=%s",
        S3_KEY,
        digest,
        prompt_id,
        PEGASUS_INFERENCE_ID,
    )

    db_url = _db_url()
    account_id = _account_id()

    # Figure out which clips need work before we waste time downloading the
    # source video.
    with psycopg.connect(db_url, sslmode="require") as conn:
        clips = _list_clips(conn)
        already_done = _existing_descriptions(conn, prompt_id)

    if not clips:
        logger.warning(
            "no clip rows in embeddings for s3_key=%s; finalize_clip_embed "
            "must run first.",
            S3_KEY,
        )
        return 0

    todo = [c for c in clips if c not in already_done]
    logger.info(
        "clips total=%d done=%d todo=%d",
        len(clips),
        len(already_done),
        len(todo),
    )
    if not todo:
        logger.info("nothing to do; all clips already described.")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="clip-pegasus-"))
    n_done = 0
    n_failed = 0
    try:
        local_source = tmp / Path(S3_KEY).name
        logger.info("download s3://%s/%s -> %s", BUCKET, S3_KEY, local_source)
        s3.download_file(BUCKET, S3_KEY, str(local_source))

        with psycopg.connect(db_url, sslmode="require") as conn:
            for start_sec, end_sec in todo:
                label = f"{start_sec:.2f}-{end_sec:.2f}s"
                clip_key, clip_name = _clip_keys(start_sec, end_sec)
                local_clip = tmp / clip_name
                try:
                    _cut_clip(
                        local_source,
                        start_sec=start_sec,
                        end_sec=end_sec,
                        out_path=local_clip,
                    )
                    if not _s3_object_exists(bucket=BUCKET, key=clip_key):
                        s3.upload_file(
                            str(local_clip),
                            BUCKET,
                            clip_key,
                            ExtraArgs={"ContentType": "video/mp4"},
                        )
                    message = _stream_pegasus(
                        account_id=account_id,
                        clip_s3_key=clip_key,
                        prompt=prompt,
                    )
                    if not message.strip():
                        raise RuntimeError("empty Pegasus response")

                    _upsert_description(
                        conn,
                        start_sec=start_sec,
                        end_sec=end_sec,
                        clip_s3_key=clip_key,
                        prompt_id=prompt_id,
                        prompt=prompt,
                        message=message,
                        model_id=PEGASUS_INFERENCE_ID,
                    )
                    conn.commit()
                    head = message.strip().splitlines()[0:1]
                    logger.info("ok %s | %s", label, (head[0] if head else "")[:120])
                    n_done += 1
                except subprocess.CalledProcessError as exc:
                    logger.exception("ffmpeg failed for %s: %s", label, exc)
                    n_failed += 1
                except Exception as exc:  # noqa: BLE001
                    logger.exception("pegasus failed for %s: %s", label, exc)
                    n_failed += 1
                finally:
                    try:
                        local_clip.unlink()
                    except OSError:
                        pass

        elapsed = time.time() - started
        logger.info(
            "clip-pegasus done s3_key=%s done=%d failed=%d elapsed=%.1fs",
            S3_KEY,
            n_done,
            n_failed,
            elapsed,
        )
        return 0 if n_failed == 0 else 2
    finally:
        try:
            shutil.rmtree(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
