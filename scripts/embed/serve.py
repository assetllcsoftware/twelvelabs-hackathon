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
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import _lib


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

    def ensure(self, *, rebuild_matrix: bool = False) -> None:
        with self.lock:
            if self.cfg is None:
                try:
                    self.cfg = _lib.load_config()
                except _lib.ConfigError as exc:
                    raise HTTPException(status_code=500, detail=str(exc)) from exc
                self.bedrock = _lib.bedrock_client(self.cfg.region)
                self.s3 = _lib.s3_client(self.cfg.region)
            if rebuild_matrix or self.matrix is None:
                self.matrix, self.meta = _lib.build_segment_matrix()


STATE = _State()


# ---------- helpers ---------------------------------------------------------


def _enrich(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach presigned video URL (with #t=<timestamp>) and thumbnail URL."""
    for r in results:
        r["presigned_url"] = (
            _lib.presigned_get(STATE.s3, STATE.cfg.bucket, r["s3_key"])
            + f"#t={r['timestamp_sec']:.2f}"
        )
        thumb_rel = r.get("thumb_rel")
        r["thumb_url"] = f"/thumbs/{thumb_rel}" if thumb_rel else None
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
