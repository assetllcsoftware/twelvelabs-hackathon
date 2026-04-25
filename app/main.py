import hmac
import json
import os
import re
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional
from urllib.parse import urlparse

import boto3
import yt_dlp
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    description: str
    accept: tuple[str, ...]
    extensions: tuple[str, ...]
    icon: str
    kind: str


CATEGORY_DEFS: dict[str, Category] = {
    "raw-videos": Category(
        id="raw-videos",
        label="Raw Videos",
        description="Source captures uploaded by the team",
        accept=("video/*",),
        extensions=("mp4", "mov", "mkv", "avi", "webm", "m4v"),
        icon="video",
        kind="video",
    ),
    "video-clips": Category(
        id="video-clips",
        label="Video Clips",
        description="Trimmed and processed clips",
        accept=("video/*",),
        extensions=("mp4", "mov", "mkv", "avi", "webm", "m4v"),
        icon="film",
        kind="video",
    ),
    "frames": Category(
        id="frames",
        label="Frames",
        description="Extracted images and thumbnails",
        accept=("image/*",),
        extensions=("jpg", "jpeg", "png", "webp", "gif", "bmp"),
        icon="image",
        kind="image",
    ),
    "detections": Category(
        id="detections",
        label="Detections",
        description="Inference outputs as JSON or JSONL",
        accept=("application/json", "application/x-ndjson", ".json", ".jsonl", ".ndjson"),
        extensions=("json", "jsonl", "ndjson"),
        icon="data",
        kind="data",
    ),
}

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.environ["S3_BUCKET"]
PORTAL_CATEGORY_IDS = [
    cid.strip()
    for cid in os.getenv("PORTAL_CATEGORIES", ",".join(CATEGORY_DEFS)).split(",")
    if cid.strip()
]
ENABLED_CATEGORIES: dict[str, Category] = {
    cid: CATEGORY_DEFS[cid] for cid in PORTAL_CATEGORY_IDS if cid in CATEGORY_DEFS
}
if not ENABLED_CATEGORIES:
    raise RuntimeError("No valid PORTAL_CATEGORIES configured")

UPLOAD_PORTAL_TOKEN = os.getenv("UPLOAD_PORTAL_TOKEN", "dev-token")
COOKIE_NAME = "upload_portal_token"
PRESIGN_EXPIRES_SECONDS = 3600

YOUTUBE_TARGET_CATEGORY = os.getenv("YOUTUBE_TARGET_CATEGORY", "raw-videos")
YOUTUBE_MAX_FILESIZE = int(os.getenv("YOUTUBE_MAX_FILESIZE_BYTES", str(4 * 1024 * 1024 * 1024)))
YOUTUBE_MAX_HEIGHT = int(os.getenv("YOUTUBE_MAX_HEIGHT", "720"))
YOUTUBE_JOB_HISTORY = int(os.getenv("YOUTUBE_JOB_HISTORY", "40"))
ALLOWED_URL_SCHEMES = {"http", "https"}

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Hackathon Media Portal")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
s3 = boto3.client("s3", region_name=AWS_REGION)

youtube_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="yt-dlp")
youtube_jobs: dict[str, dict] = {}
youtube_jobs_lock = threading.Lock()


class UploadPresignRequest(BaseModel):
    category: str = Field(min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=255)


class DownloadPresignRequest(BaseModel):
    key: str = Field(min_length=1, max_length=1024)


class YoutubeDownloadRequest(BaseModel):
    url: str = Field(min_length=4, max_length=2048)
    filename: Optional[str] = Field(default=None, max_length=200)


def is_authorized(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME) or request.headers.get("x-upload-token") or ""
    return hmac.compare_digest(token, UPLOAD_PORTAL_TOKEN)


def require_authorized(request: Request) -> None:
    if not is_authorized(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


def safe_filename(filename: str) -> str:
    name = PurePosixPath(filename).name.strip()
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        raise HTTPException(status_code=400, detail="Filename is empty after sanitization")
    return name[:180]


def category_for(category_id: str) -> Category:
    if category_id not in ENABLED_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unknown category: {category_id}")
    return ENABLED_CATEGORIES[category_id]


def validate_extension(category: Category, filename: str) -> None:
    suffix = PurePosixPath(filename).suffix.lower().lstrip(".")
    if not suffix or suffix not in category.extensions:
        allowed = ", ".join(f".{ext}" for ext in category.extensions)
        raise HTTPException(
            status_code=400,
            detail=f"{category.label} only accepts {allowed}",
        )


def category_prefix(category: Category) -> str:
    return f"{category.id}/"


def object_key(category: Category, filename: str) -> str:
    return f"{category_prefix(category)}{safe_filename(filename)}"


def parse_object_key(key: str) -> tuple[Category, str]:
    normalized = key.lstrip("/")
    for category in ENABLED_CATEGORIES.values():
        prefix = category_prefix(category)
        if normalized.startswith(prefix) and normalized != prefix:
            name_part = normalized[len(prefix):]
            if PurePosixPath(name_part).name in {"", ".", ".."}:
                break
            return category, normalized
    raise HTTPException(status_code=400, detail="Invalid object key")


def serializable_categories() -> list[dict]:
    return [
        {
            "id": c.id,
            "label": c.label,
            "description": c.description,
            "accept": list(c.accept),
            "extensions": list(c.extensions),
            "icon": c.icon,
            "kind": c.kind,
        }
        for c in ENABLED_CATEGORIES.values()
    ]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not is_authorized(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "bucket": S3_BUCKET,
            "categories": list(ENABLED_CATEGORIES.values()),
            "categories_json": json.dumps(serializable_categories()),
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error},
    )


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    if not hmac.compare_digest(token, UPLOAD_PORTAL_TOKEN):
        return RedirectResponse("/login?error=invalid", status_code=303)

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 12,
    )
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/api/categories")
def api_categories(request: Request):
    require_authorized(request)
    return {"categories": serializable_categories()}


@app.post("/api/uploads/presign")
def create_upload_presign(payload: UploadPresignRequest, request: Request):
    require_authorized(request)
    category = category_for(payload.category)
    validate_extension(category, payload.filename)
    key = object_key(category, payload.filename)
    try:
        url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": S3_BUCKET,
                "Key": key,
                "ContentType": payload.content_type,
            },
            ExpiresIn=PRESIGN_EXPIRES_SECONDS,
        )
    except ClientError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "key": key,
        "url": url,
        "expires_seconds": PRESIGN_EXPIRES_SECONDS,
        "category": category.id,
    }


@app.get("/api/files")
def list_files(request: Request, category: str):
    require_authorized(request)
    cat = category_for(category)
    prefix = category_prefix(cat)
    paginator = s3.get_paginator("list_objects_v2")
    files: list[dict] = []
    try:
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key == prefix:
                    continue
                files.append(
                    {
                        "key": key,
                        "name": key[len(prefix):],
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"]
                        .astimezone(timezone.utc)
                        .isoformat(),
                    }
                )
    except ClientError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    files.sort(key=lambda item: item["last_modified"], reverse=True)
    return {"category": cat.id, "files": files}


@app.post("/api/files/presign-download")
def create_download_presign(payload: DownloadPresignRequest, request: Request):
    require_authorized(request)
    _, normalized = parse_object_key(payload.key)
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": normalized},
            ExpiresIn=PRESIGN_EXPIRES_SECONDS,
        )
    except ClientError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"url": url, "expires_seconds": PRESIGN_EXPIRES_SECONDS}


@app.delete("/api/files")
def delete_file(request: Request, key: str):
    require_authorized(request)
    _, normalized = parse_object_key(key)
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=normalized)
    except ClientError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse({"deleted": normalized})


# ---------------------------------------------------------------------------
# YouTube / yt-dlp ingest
# ---------------------------------------------------------------------------


def _job_snapshot(job: dict) -> dict:
    return {
        "id": job["id"],
        "url": job["url"],
        "status": job["status"],
        "message": job.get("message", ""),
        "progress": job.get("progress", 0.0),
        "downloaded_bytes": job.get("downloaded_bytes", 0),
        "total_bytes": job.get("total_bytes"),
        "speed": job.get("speed"),
        "eta": job.get("eta"),
        "title": job.get("title"),
        "key": job.get("key"),
        "category": job.get("category"),
        "filename": job.get("filename"),
        "error": job.get("error"),
        "started_at": job["started_at"],
        "updated_at": job.get("updated_at", job["started_at"]),
        "finished_at": job.get("finished_at"),
    }


def _update_job(job_id: str, **fields) -> None:
    with youtube_jobs_lock:
        job = youtube_jobs.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = datetime.now(timezone.utc).isoformat()


def _trim_job_history() -> None:
    with youtube_jobs_lock:
        if len(youtube_jobs) <= YOUTUBE_JOB_HISTORY:
            return
        finished = sorted(
            (j for j in youtube_jobs.values() if j["status"] in ("done", "error", "cancelled")),
            key=lambda j: j.get("finished_at") or j["started_at"],
        )
        excess = len(youtube_jobs) - YOUTUBE_JOB_HISTORY
        for job in finished[:excess]:
            youtube_jobs.pop(job["id"], None)


def _validate_video_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="URL is required")
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES or not parsed.netloc:
        raise HTTPException(status_code=400, detail="URL must be a valid http(s) link")
    return cleaned


def _ensure_unique_key(category: Category, filename: str) -> str:
    base = safe_filename(filename)
    stem = PurePosixPath(base).stem
    suffix = PurePosixPath(base).suffix or ".mp4"
    candidate = f"{stem}{suffix}"
    counter = 1
    while True:
        key = f"{category_prefix(category)}{candidate}"
        try:
            s3.head_object(Bucket=S3_BUCKET, Key=key)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code in ("404", "NoSuchKey", "NotFound"):
                return key
            raise
        counter += 1
        candidate = f"{stem} ({counter}){suffix}"
        if counter > 50:
            raise HTTPException(status_code=409, detail="Could not derive a unique filename")


def _yt_dlp_options(job_id: str, output_dir: Path) -> dict:
    def progress_hook(d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            progress = (downloaded / total * 100.0) if total else 0.0
            _update_job(
                job_id,
                status="downloading",
                message="downloading video",
                progress=round(progress, 1),
                downloaded_bytes=downloaded,
                total_bytes=total,
                speed=d.get("speed"),
                eta=d.get("eta"),
            )
        elif status == "finished":
            _update_job(
                job_id,
                status="processing",
                message="post-processing",
                progress=99.0,
            )

    return {
        "format": (
            f"bv*[height<={YOUTUBE_MAX_HEIGHT}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={YOUTUBE_MAX_HEIGHT}][ext=mp4]/"
            f"b[height<={YOUTUBE_MAX_HEIGHT}]"
        ),
        "merge_output_format": "mp4",
        "outtmpl": str(output_dir / "%(title).180s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "restrictfilenames": True,
        "max_filesize": YOUTUBE_MAX_FILESIZE,
        "progress_hooks": [progress_hook],
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
    }


def _run_youtube_job(job_id: str, url: str, override_filename: Optional[str]) -> None:
    category = ENABLED_CATEGORIES.get(YOUTUBE_TARGET_CATEGORY)
    if category is None:
        _update_job(
            job_id,
            status="error",
            error=f"Target category '{YOUTUBE_TARGET_CATEGORY}' is not enabled",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        return

    try:
        with tempfile.TemporaryDirectory(prefix="yt-dlp-") as tmp:
            output_dir = Path(tmp)
            opts = _yt_dlp_options(job_id, output_dir)
            _update_job(job_id, status="downloading", message="resolving video")

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    raise RuntimeError("yt-dlp returned no metadata")
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]
                title = info.get("title") or "video"
                _update_job(job_id, title=title)
                produced_path = Path(ydl.prepare_filename(info))
                if produced_path.suffix.lower() != ".mp4":
                    candidate = produced_path.with_suffix(".mp4")
                    if candidate.exists():
                        produced_path = candidate

            if not produced_path.exists():
                # fall back to the largest file that yt-dlp wrote
                files = sorted(
                    (p for p in output_dir.rglob("*") if p.is_file()),
                    key=lambda p: p.stat().st_size,
                    reverse=True,
                )
                if not files:
                    raise RuntimeError("yt-dlp did not produce an output file")
                produced_path = files[0]

            requested_name = override_filename or f"{title}.mp4"
            if not requested_name.lower().endswith(tuple(f".{ext}" for ext in category.extensions)):
                requested_name = f"{PurePosixPath(requested_name).stem}.mp4"

            key = _ensure_unique_key(category, requested_name)
            filename = key[len(category_prefix(category)):]
            size = produced_path.stat().st_size

            _update_job(
                job_id,
                status="uploading",
                message="uploading to S3",
                progress=99.5,
                filename=filename,
                key=key,
                category=category.id,
                total_bytes=size,
                downloaded_bytes=size,
            )

            s3.upload_file(
                str(produced_path),
                S3_BUCKET,
                key,
                ExtraArgs={"ContentType": "video/mp4"},
            )

            _update_job(
                job_id,
                status="done",
                message="completed",
                progress=100.0,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
    except yt_dlp.utils.DownloadError as exc:
        _update_job(
            job_id,
            status="error",
            error=str(exc) or "yt-dlp download failed",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    except ClientError as exc:
        _update_job(
            job_id,
            status="error",
            error=f"S3 upload failed: {exc}",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:  # noqa: BLE001
        _update_job(
            job_id,
            status="error",
            error=str(exc) or exc.__class__.__name__,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        _trim_job_history()


@app.post("/api/youtube/download")
def submit_youtube_job(payload: YoutubeDownloadRequest, request: Request):
    require_authorized(request)
    if YOUTUBE_TARGET_CATEGORY not in ENABLED_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"YouTube ingest target '{YOUTUBE_TARGET_CATEGORY}' is not enabled",
        )
    url = _validate_video_url(payload.url)
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "url": url,
        "status": "queued",
        "message": "queued",
        "progress": 0.0,
        "downloaded_bytes": 0,
        "total_bytes": None,
        "speed": None,
        "eta": None,
        "title": None,
        "key": None,
        "category": YOUTUBE_TARGET_CATEGORY,
        "filename": None,
        "error": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }
    with youtube_jobs_lock:
        youtube_jobs[job_id] = job
    youtube_executor.submit(_run_youtube_job, job_id, url, payload.filename)
    return _job_snapshot(job)


@app.get("/api/youtube/jobs")
def list_youtube_jobs(request: Request):
    require_authorized(request)
    with youtube_jobs_lock:
        snapshots = [_job_snapshot(job) for job in youtube_jobs.values()]
    snapshots.sort(key=lambda j: j["started_at"], reverse=True)
    return {"jobs": snapshots, "target_category": YOUTUBE_TARGET_CATEGORY}


@app.get("/api/youtube/jobs/{job_id}")
def get_youtube_job(job_id: str, request: Request):
    require_authorized(request)
    with youtube_jobs_lock:
        job = youtube_jobs.get(job_id)
        snapshot = _job_snapshot(job) if job else None
    if not snapshot:
        raise HTTPException(status_code=404, detail="Job not found")
    return snapshot


@app.delete("/api/youtube/jobs/{job_id}")
def delete_youtube_job(job_id: str, request: Request):
    require_authorized(request)
    with youtube_jobs_lock:
        job = youtube_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job["status"] in ("queued", "downloading", "uploading", "processing"):
            raise HTTPException(status_code=409, detail="Job is still active")
        youtube_jobs.pop(job_id, None)
    return JSONResponse({"deleted": job_id})
