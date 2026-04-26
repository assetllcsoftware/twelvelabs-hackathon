"""Tiny standalone FastAPI app: a local Marengo search UI on http://127.0.0.1:8001.

Phase A throwaway: same code paths as the CLI but behind a small page with a
text box, a drag-and-drop image zone, paste-from-clipboard, and a results grid
of embedded ``<video controls>`` players that auto-seek to the matched
segment via the URL fragment ``#t=<start_sec>``.

The shape of the JSON returned by ``/api/search/*`` is intentionally identical
to what we'll wire into the real portal in Phase C, so swapping the data layer
to Postgres later is a one-file change.

Run::

    set -a; source ./.aws-demo.env; set +a
    unset AWS_PROFILE
    export AWS_CONFIG_FILE=/dev/null
    export S3_BUCKET="$(terraform -chdir=infra output -raw bucket_name)"

    pipenv run python -m scripts.embed.serve
    pipenv run python -m scripts.embed.serve --port 8001 --reload
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import _lib
from scripts.pegasus import _lib as pegasus_lib
from scripts.yolo import _lib as yolo_lib


WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="EIHP // local Marengo search")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
# Per-frame thumbnails extracted by `embed_frames` live under
# data/embeddings/thumbs/<digest>/frame_NNNNN.jpg. We mount that directory so
# the result cards can render the actual matching frame.
if _lib.FRAMES_THUMB_DIR.exists():
    app.mount(
        "/thumbs",
        StaticFiles(directory=_lib.FRAMES_THUMB_DIR),
        name="thumbs",
    )


class _State:
    """Lazily-initialized AWS clients + in-memory normalized matrix."""

    def __init__(self) -> None:
        self.cfg: _lib.Config | None = None
        self.bedrock = None
        self.s3 = None
        self.matrix = None
        self.meta: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        # Pegasus uses a different inference profile id than Marengo, so we
        # cache the resolved id alongside the bedrock client.
        self.pegasus_inference_id: Optional[str] = None

    def ensure(self, *, rebuild_matrix: bool = False) -> None:
        with self.lock:
            if self.cfg is None:
                try:
                    self.cfg = _lib.load_config()
                except _lib.ConfigError as exc:
                    raise HTTPException(status_code=500, detail=str(exc)) from exc
                self.bedrock = _lib.bedrock_client(self.cfg.region)
                self.s3 = _lib.s3_client(self.cfg.region)
                try:
                    self.pegasus_inference_id = pegasus_lib.resolve_inference_id(
                        self.cfg.region
                    )
                except pegasus_lib.PegasusError:
                    # Pegasus isn't required for search; surface a missing
                    # mapping only when /api/describe is actually hit.
                    self.pegasus_inference_id = None
            if rebuild_matrix or self.matrix is None:
                self.matrix, self.meta = _lib.build_segment_matrix()


STATE = _State()


# ---------- helpers ---------------------------------------------------------


def _detections_for_result(
    *,
    s3_key: str,
    cache: Optional[dict[str, Any]],
    kind: str,
    frame_index: Optional[int],
    start_sec: float,
    end_sec: float,
) -> list[dict[str, Any]]:
    """Pull detections for one result row.

    * Frame results: exact lookup by ``frame_index`` (the row's matrix
      index inside the per-video frame array).
    * Clip results: union of every cached frame whose ``timestamp_sec``
      falls inside ``[start_sec, end_sec]`` — gives the UI enough boxes
      to draw a meaningful overlay even though the user clicked a clip.
    """
    if not cache:
        return []
    frames = cache.get("frames") or {}
    if kind == "frame":
        if frame_index is None:
            return []
        return list(frames.get(str(frame_index)) or [])
    # Clip: aggregate all frames whose timestamp lands in the window.
    span_start = float(start_sec)
    span_end = float(end_sec) if end_sec > start_sec else float(start_sec) + 0.5
    out: list[dict[str, Any]] = []
    for _fi, dets in frames.items():
        for d in dets or []:
            ts = float(d.get("timestamp_sec", -1.0))
            if ts < 0:
                continue
            if span_start - 0.05 <= ts <= span_end + 0.05:
                out.append(d)
    return out


def _enrich(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach presigned video URL (with #t=<timestamp>), thumbnail URL,
    pre-generated Pegasus text, and any cached YOLO detections.

    Frame results inherit Pegasus text from their containing clip when no
    frame-level row was generated (the local Pegasus pipeline only writes
    one row per clip).
    """
    default_prompt = pegasus_lib.DEFAULT_PROMPT
    inspector_prompt = pegasus_lib.resolve_preset("inspector") or default_prompt
    # Index by both inspector + summary so users get *something* even if
    # they pregenerated under a different preset.
    indexes = {
        "inspector": pegasus_lib.index_clip_descriptions(prompt=inspector_prompt),
        "summary": pegasus_lib.index_clip_descriptions(prompt=default_prompt),
    }

    # Memoize per-video YOLO cache reads so a 50-row result set doesn't
    # re-parse the same JSON file 50 times.
    yolo_cache_for: dict[str, Optional[dict[str, Any]]] = {}

    for r in results:
        r["presigned_url"] = (
            _lib.presigned_get(STATE.s3, STATE.cfg.bucket, r["s3_key"])
            + f"#t={r['timestamp_sec']:.2f}"
        )
        thumb_rel = r.get("thumb_rel")
        r["thumb_url"] = f"/thumbs/{thumb_rel}" if thumb_rel else None

        # Walk preset-priority order: inspector first (it's the default the
        # pregenerate script writes), then summary as a fallback.
        for preset_id in ("inspector", "summary"):
            hit = pegasus_lib.find_clip_text(
                indexes[preset_id],
                s3_key=r["s3_key"],
                start_sec=float(r.get("start_sec", 0.0)),
                end_sec=float(r.get("end_sec", 0.0)),
                timestamp_sec=float(r.get("timestamp_sec", 0.0)),
            )
            if not hit:
                continue
            inherited = (
                r.get("kind") == "frame"
                or abs(float(hit["start_sec"]) - float(r.get("start_sec", 0.0))) > 0.25
                or abs(float(hit["end_sec"]) - float(r.get("end_sec", 0.0))) > 0.25
            )
            r["pegasus"] = {
                "text": hit.get("message", ""),
                "preset": preset_id,
                "model": hit.get("model"),
                "clip_start_sec": float(hit["start_sec"]),
                "clip_end_sec": float(hit["end_sec"]),
                "inherited": bool(inherited),
            }
            break

        s3_key = r["s3_key"]
        if s3_key not in yolo_cache_for:
            yolo_cache_for[s3_key] = yolo_lib.load_detections(s3_key)
        dets = _detections_for_result(
            s3_key=s3_key,
            cache=yolo_cache_for[s3_key],
            kind=str(r.get("kind") or "clip"),
            frame_index=r.get("frame_index"),
            start_sec=float(r.get("start_sec", 0.0)),
            end_sec=float(r.get("end_sec", r.get("start_sec", 0.0))),
        )
        r["detections"] = dets
        # Build a per-row class summary so the UI can render badges /
        # per-card legends without re-walking the polygons.
        if dets:
            counts: dict[tuple[str, str], int] = {}
            for d in dets:
                key = (
                    str(d.get("model_name") or "unknown"),
                    str(d.get("class_name") or "unknown"),
                )
                counts[key] = counts.get(key, 0) + 1
            r["detection_classes"] = [
                {"model": m, "name": c, "count": n}
                for (m, c), n in sorted(
                    counts.items(), key=lambda kv: (-kv[1], kv[0][1])
                )
            ]
        else:
            r["detection_classes"] = []

    return results


def _search(query_vec, top_k: int) -> list[dict[str, Any]]:
    STATE.ensure()
    if STATE.matrix is None or STATE.matrix.shape[0] == 0:
        raise HTTPException(
            status_code=404,
            detail=(
                "no cached embeddings; run "
                "`pipenv run python -m scripts.embed.embed_videos` first"
            ),
        )
    ranked = _lib.rank_results(STATE.matrix, STATE.meta, query_vec, top_k=top_k)
    return _enrich(ranked)


def _embed_uploaded_image(file: UploadFile, *, with_text: str | None = None) -> list[float]:
    suffix = os.path.splitext(file.filename or "frame.jpg")[1] or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file.file.read())
        tmp_path = tmp.name
    try:
        if with_text is None:
            data = _lib.invoke_image_embedding(STATE.bedrock, STATE.cfg.inference_id, tmp_path)
        else:
            data = _lib.invoke_text_image_embedding(
                STATE.bedrock, STATE.cfg.inference_id, with_text, tmp_path
            )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if not data:
        raise HTTPException(status_code=502, detail="embedding response was empty")
    return data[0]["embedding"]


# ---------- routes ----------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/stats")
def stats():
    STATE.ensure()
    by_video_clips: dict[str, int] = {}
    by_video_frames: dict[str, int] = {}
    for m in STATE.meta:
        bucket = by_video_frames if m.get("kind") == "frame" else by_video_clips
        bucket[m["s3_key"]] = bucket.get(m["s3_key"], 0) + 1
    keys = sorted(set(by_video_clips) | set(by_video_frames))
    n_clips = sum(by_video_clips.values())
    n_frames = sum(by_video_frames.values())
    return {
        "videos": len(keys),
        "rows": int(STATE.matrix.shape[0]) if STATE.matrix is not None else 0,
        "clips": n_clips,
        "frames": n_frames,
        "model": STATE.cfg.inference_id if STATE.cfg else None,
        "bucket": STATE.cfg.bucket if STATE.cfg else None,
        "region": STATE.cfg.region if STATE.cfg else None,
        "by_video": [
            {
                "s3_key": k,
                "clips": by_video_clips.get(k, 0),
                "frames": by_video_frames.get(k, 0),
            }
            for k in keys
        ],
    }


@app.post("/api/refresh")
def refresh():
    STATE.ensure(rebuild_matrix=True)
    return stats()


@app.get("/api/detection-classes")
def detection_classes():
    """Catalogue of every YOLO class present in the local cache.

    The UI calls this once on load to populate the per-class toggle bar.
    Returns ``{"classes": [...], "models": [...], "status": "ok"|"empty"}``;
    when no cache files exist yet, ``status == "empty"`` and the toggle
    bar stays hidden.
    """
    return yolo_lib.detection_classes_summary()


class TextSearchPayload(BaseModel):
    q: str
    top_k: int = 10


@app.post("/api/search/text")
def search_text(payload: TextSearchPayload):
    q = payload.q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="missing q")
    STATE.ensure()
    data = _lib.invoke_text_embedding(STATE.bedrock, STATE.cfg.inference_id, q)
    if not data:
        raise HTTPException(status_code=502, detail="embedding response was empty")
    return _search(data[0]["embedding"], payload.top_k)


@app.post("/api/search/image")
async def search_image(file: UploadFile = File(...), top_k: int = Form(10)):
    STATE.ensure()
    vec = _embed_uploaded_image(file)
    return _search(vec, top_k)


@app.post("/api/search/text-image")
async def search_text_image(
    q: str = Form(...),
    file: UploadFile = File(...),
    top_k: int = Form(10),
):
    if not q.strip():
        raise HTTPException(status_code=400, detail="missing q")
    STATE.ensure()
    vec = _embed_uploaded_image(file, with_text=q.strip())
    return _search(vec, top_k)


# ---------- pegasus (video-to-text) ----------------------------------------
#
# /api/describe streams an NDJSON response: one JSON object per line, then
# a final {"type": "done", "cached": ..., "model": ...} marker. Frontend
# parses with a TextDecoder + line buffer. We use NDJSON instead of SSE
# because POST + JSON body composes more naturally with our existing
# search routes (and SSE is GET-only in the EventSource API).


class DescribePayload(BaseModel):
    s3_key: str
    preset: Optional[str] = None
    prompt: Optional[str] = None
    force: bool = False
    temperature: float = 0.0


@app.get("/api/describe/presets")
def describe_presets():
    """Return the curated prompt list shared with the CLI."""
    return {
        "presets": [
            {"id": p["id"], "label": p["label"], "prompt": p["prompt"]}
            for p in pegasus_lib.PRESET_PROMPTS
        ]
    }


def _ndjson_line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


def _describe_stream(
    *,
    s3_key: str,
    prompt: str,
    force: bool,
    temperature: float,
) -> Iterator[bytes]:
    """Generator that yields NDJSON byte chunks. Runs in a worker thread
    courtesy of FastAPI's StreamingResponse — boto3 invoke_model_with_response_stream
    is synchronous so we mustn't await on it from the event loop.
    """
    cfg = STATE.cfg
    bedrock = STATE.bedrock
    inference_id = STATE.pegasus_inference_id
    if cfg is None or bedrock is None or inference_id is None:
        yield _ndjson_line(
            {"type": "error", "message": "pegasus not initialized; check region/config"}
        )
        return

    yield _ndjson_line(
        {"type": "meta", "model": inference_id, "s3_key": s3_key, "prompt": prompt}
    )

    cache = pegasus_lib.read_cache(s3_key, prompt) if not force else None
    if cache is not None:
        # Replay the cached message in one delta so the UI can render it
        # immediately without an additional roundtrip.
        yield _ndjson_line(
            {"type": "delta", "content": cache.get("message", ""), "cached": True}
        )
        yield _ndjson_line(
            {"type": "done", "cached": True, "model": cache.get("model", inference_id)}
        )
        return

    chunks: list[str] = []
    try:
        for chunk in pegasus_lib.stream_describe(
            bedrock,
            inference_id=inference_id,
            bucket=cfg.bucket,
            account_id=cfg.account_id,
            s3_key=s3_key,
            prompt=prompt,
            temperature=temperature,
        ):
            chunks.append(chunk)
            yield _ndjson_line({"type": "delta", "content": chunk, "cached": False})
    except Exception as exc:  # noqa: BLE001 — surface boto3/Bedrock errors clearly
        yield _ndjson_line(
            {"type": "error", "message": f"{exc.__class__.__name__}: {exc}"}
        )
        return

    full = "".join(chunks).strip()
    if full:
        pegasus_lib.save_cache(
            s3_key=s3_key,
            prompt=prompt,
            message="".join(chunks),
            model_id=inference_id,
        )
    yield _ndjson_line({"type": "done", "cached": False, "model": inference_id})


@app.post("/api/describe")
def describe(payload: DescribePayload):
    STATE.ensure()
    if STATE.pegasus_inference_id is None:
        raise HTTPException(
            status_code=500,
            detail=(
                f"No Pegasus inference profile mapped for region "
                f"{STATE.cfg.region!r}; set PEGASUS_INFERENCE_ID."
            ),
        )

    prompt = pegasus_lib.resolve_preset(payload.preset)
    if prompt is None:
        prompt = (payload.prompt or "").strip() or pegasus_lib.DEFAULT_PROMPT

    s3_key = payload.s3_key.strip()
    if not s3_key:
        raise HTTPException(status_code=400, detail="missing s3_key")

    return StreamingResponse(
        _describe_stream(
            s3_key=s3_key,
            prompt=prompt,
            force=payload.force,
            temperature=payload.temperature,
        ),
        media_type="application/x-ndjson",
    )


# ---------- entrypoint ------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import uvicorn

    print(f"\nEIHP local search UI -> http://{args.host}:{args.port}\n")
    if args.reload:
        uvicorn.run(
            "scripts.embed.serve:app",
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=[str(Path(__file__).resolve().parent)],
        )
    else:
        uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
