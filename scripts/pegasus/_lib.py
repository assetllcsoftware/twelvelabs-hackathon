"""Shared helpers for the local Pegasus tools (CLI + FastAPI server).

All functions take an already-constructed bedrock-runtime client + Config so
they're trivially reusable from any caller. Caching is keyed by
``(s3_key, prompt)`` under ``data/pegasus/`` exactly like the CLI's old
private helpers.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from scripts.embed import _lib as embed_lib


PEGASUS_MODEL_ID = "twelvelabs.pegasus-1-2-v1:0"
PEGASUS_INFERENCE_PROFILES: dict[str, str] = {
    "us-east-1": "us.twelvelabs.pegasus-1-2-v1:0",
    "us-west-2": "us.twelvelabs.pegasus-1-2-v1:0",
    "eu-west-1": "eu.twelvelabs.pegasus-1-2-v1:0",
    "ap-northeast-2": "apac.twelvelabs.pegasus-1-2-v1:0",
}

CACHE_DIR = embed_lib.REPO_ROOT / "data" / "pegasus"
CLIP_CACHE_DIR = CACHE_DIR / "clips"
SOURCE_VIDEO_DIR = embed_lib.REPO_ROOT / "data" / "source-videos"
CLIP_CUT_DIR = embed_lib.REPO_ROOT / "data" / "clips-cut"
DERIVED_CLIPS_S3_PREFIX = "derived/clips"

# Default prompt is intentionally domain-specific: we want the demo to
# answer "what would a utility inspector write about this clip?". Any of
# the PRESET_PROMPTS below also work.
DEFAULT_PROMPT = (
    "You are assisting an energy-grid inspection workflow. In 3-5 sentences, "
    "describe what is visible in this video and call out anything that looks "
    "relevant to power-line health: vegetation encroachment, sagging or "
    "damaged conductors, leaning poles, transformer or insulator condition, "
    "thermal anomalies, or other hazards. Be concrete; do not speculate "
    "beyond what is visible."
)

# Curated prompt presets that show up in the UI dropdown. The "id" is what
# gets sent over the wire; the server maps it back to the prompt text so we
# don't need to ship the long strings to the browser. Keep the list short.
PRESET_PROMPTS: list[dict[str, str]] = [
    {
        "id": "inspector",
        "label": "Inspector concerns",
        "prompt": (
            "You are an energy-grid inspector. List up to 5 specific concerns "
            "visible in this aerial video that a maintenance crew should "
            "investigate. Format each as: '- <concern>: <where in the frame "
            "/ approx timestamp>'. If you see no concerns, say 'No issues "
            "detected.'"
        ),
    },
    {
        "id": "summary",
        "label": "Inspection summary",
        "prompt": DEFAULT_PROMPT,
    },
    {
        "id": "hashtags",
        "label": "Hashtags / topics",
        "prompt": (
            "Generate 6-10 lowercase hashtags that capture the main topics, "
            "objects, and conditions visible in this video. Return them on "
            "one line separated by single spaces. No commentary."
        ),
    },
    {
        "id": "highlights",
        "label": "Key moments",
        "prompt": (
            "List the key moments in this video as a chronological list. "
            "Each entry should be: '- [MM:SS] <one-sentence description of "
            "what happens / what is visible>'. Cap the list at 8 entries."
        ),
    },
]


class PegasusError(RuntimeError):
    """Raised when Pegasus invocation fails for a known reason."""


@dataclass
class DescribeResult:
    s3_key: str
    prompt: str
    message: str
    cached: bool


def resolve_inference_id(region: str, override: Optional[str] = None) -> str:
    if override:
        return override
    profile = PEGASUS_INFERENCE_PROFILES.get(region)
    if not profile:
        raise PegasusError(
            f"No Pegasus inference profile mapped for region {region!r}. "
            "Set PEGASUS_INFERENCE_ID explicitly."
        )
    return profile


def cache_path_for(s3_key: str, prompt: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{s3_key}|{prompt}".encode("utf-8")).hexdigest()[:24]
    return CACHE_DIR / f"{digest}.json"


def clip_cache_path_for(
    s3_key: str, start_sec: float, end_sec: float, prompt: str
) -> Path:
    CLIP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = f"{s3_key}|{start_sec:.3f}|{end_sec:.3f}|{prompt}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return CLIP_CACHE_DIR / f"{digest}.json"


def read_clip_cache(
    s3_key: str, start_sec: float, end_sec: float, prompt: str
) -> Optional[dict[str, Any]]:
    path = clip_cache_path_for(s3_key, start_sec, end_sec, prompt)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_clip_cache(
    *,
    s3_key: str,
    start_sec: float,
    end_sec: float,
    prompt: str,
    prompt_id: Optional[str],
    message: str,
    model_id: str,
    clip_s3_key: Optional[str] = None,
) -> Path:
    """Persist Pegasus output for a clip cut from ``s3_key`` over ``[start,end]``."""
    path = clip_cache_path_for(s3_key, start_sec, end_sec, prompt)
    payload = {
        "s3_key": s3_key,
        "start_sec": start_sec,
        "end_sec": end_sec,
        "prompt_id": prompt_id,
        "prompt": prompt,
        "message": message,
        "model": model_id,
        "clip_s3_key": clip_s3_key,
        "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def iter_cached_clip_descriptions() -> Iterator[dict[str, Any]]:
    """Yield every cached clip-level Pegasus result on disk."""
    if not CLIP_CACHE_DIR.exists():
        return
    for path in sorted(CLIP_CACHE_DIR.glob("*.json")):
        try:
            yield json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue


def index_clip_descriptions(prompt: Optional[str] = None) -> dict[str, list[dict[str, Any]]]:
    """Build ``{s3_key: [clip_record, ...]}`` filtered by prompt (if given).

    Used by the local FastAPI server to attach pre-generated text to search
    results without hitting Bedrock again.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for record in iter_cached_clip_descriptions():
        if prompt is not None and record.get("prompt") != prompt:
            continue
        out.setdefault(record["s3_key"], []).append(record)
    for clips in out.values():
        clips.sort(key=lambda r: float(r.get("start_sec", 0.0)))
    return out


def find_clip_text(
    index: dict[str, list[dict[str, Any]]],
    *,
    s3_key: str,
    start_sec: float,
    end_sec: float,
    timestamp_sec: Optional[float] = None,
    overlap_tolerance: float = 0.25,
) -> Optional[dict[str, Any]]:
    """Return the cached clip-text record best matching the given range.

    Resolution order:
      1. Exact (start, end) match (within ``overlap_tolerance`` seconds).
      2. The clip whose [start, end] contains ``timestamp_sec`` (for frames
         that sit inside a clip's range).
      3. The clip with the largest temporal overlap with [start, end].
    """
    clips = index.get(s3_key) or []
    if not clips:
        return None

    for clip in clips:
        cs, ce = float(clip["start_sec"]), float(clip["end_sec"])
        if abs(cs - start_sec) <= overlap_tolerance and abs(ce - end_sec) <= overlap_tolerance:
            return clip

    if timestamp_sec is not None:
        for clip in clips:
            cs, ce = float(clip["start_sec"]), float(clip["end_sec"])
            if cs - 0.001 <= timestamp_sec <= ce + 0.001:
                return clip

    best_clip = None
    best_overlap = 0.0
    for clip in clips:
        cs, ce = float(clip["start_sec"]), float(clip["end_sec"])
        overlap = max(0.0, min(ce, end_sec) - max(cs, start_sec))
        if overlap > best_overlap:
            best_overlap = overlap
            best_clip = clip
    return best_clip


def read_cache(s3_key: str, prompt: str) -> Optional[dict[str, Any]]:
    path = cache_path_for(s3_key, prompt)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_cache(*, s3_key: str, prompt: str, message: str, model_id: str) -> Path:
    path = cache_path_for(s3_key, prompt)
    payload = {
        "s3_key": s3_key,
        "model": model_id,
        "prompt": prompt,
        "message": message,
        "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def build_request_body(
    *,
    s3_uri: str,
    account_id: str,
    prompt: str,
    temperature: float = 0.0,
) -> dict[str, Any]:
    return {
        "inputPrompt": prompt,
        "mediaSource": {
            "s3Location": {
                "uri": s3_uri,
                "bucketOwner": account_id,
            }
        },
        "temperature": temperature,
    }


def stream_describe(
    bedrock,
    *,
    inference_id: str,
    bucket: str,
    account_id: str,
    s3_key: str,
    prompt: str,
    temperature: float = 0.0,
) -> Iterator[str]:
    """Stream Pegasus response chunks as raw text. Caller is responsible for
    aggregating + caching the final message."""
    s3_uri = f"s3://{bucket}/{s3_key}"
    body = build_request_body(
        s3_uri=s3_uri,
        account_id=account_id,
        prompt=prompt,
        temperature=temperature,
    )
    response = bedrock.invoke_model_with_response_stream(
        modelId=inference_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    for event in response["body"]:
        chunk = event.get("chunk")
        if not chunk:
            continue
        payload = json.loads(chunk["bytes"])
        text = payload.get("message")
        if text:
            yield text


def describe_sync(
    bedrock,
    *,
    inference_id: str,
    bucket: str,
    account_id: str,
    s3_key: str,
    prompt: str,
    temperature: float = 0.0,
) -> str:
    """Non-streaming variant of :func:`stream_describe`."""
    s3_uri = f"s3://{bucket}/{s3_key}"
    body = build_request_body(
        s3_uri=s3_uri,
        account_id=account_id,
        prompt=prompt,
        temperature=temperature,
    )
    response = bedrock.invoke_model(
        modelId=inference_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(response["body"].read())
    return payload.get("message", "")


def resolve_preset(preset_id: Optional[str]) -> Optional[str]:
    """Map a preset id from the wire to a full prompt string."""
    if not preset_id:
        return None
    for preset in PRESET_PROMPTS:
        if preset["id"] == preset_id:
            return preset["prompt"]
    return None
