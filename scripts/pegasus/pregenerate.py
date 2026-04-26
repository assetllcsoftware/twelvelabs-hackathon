"""Pre-generate Pegasus video-text for every clip in our local embedding cache.

Pegasus only accepts an S3 URI as input, so for each unique Marengo clip
``(s3_key, start_sec, end_sec)`` we:

  1. Download the source video to ``data/source-videos/<digest>/<basename>``
     (cached, so subsequent clips of the same source are free).
  2. Cut the clip with ffmpeg into
     ``data/clips-cut/<digest>/clip_<start_ms>_<end_ms>.mp4``.
  3. Upload it to ``s3://<bucket>/derived/clips/<digest>/clip_<start_ms>_<end_ms>.mp4``.
  4. Run Pegasus on the cut clip with one of the curated prompts (default
     ``inspector``) and cache the result under
     ``data/pegasus/clips/<sha>.json`` (keyed on
     ``(s3_key, start_sec, end_sec, prompt)``).

Frames don't get their own Pegasus call — they inherit the text of the
clip whose time range contains them. The local FastAPI server does that
lookup at search time via :func:`scripts.pegasus._lib.find_clip_text`.

Usage::

    pipenv run python -m scripts.pegasus.pregenerate
    pipenv run python -m scripts.pegasus.pregenerate --preset inspector
    pipenv run python -m scripts.pegasus.pregenerate --limit 3 --force
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from scripts.embed import _lib as embed_lib

from . import _lib as pegasus_lib


# ---------------------------------------------------------------------------
# Discovery: collect unique clips from our local Marengo cache
# ---------------------------------------------------------------------------


def _unique_clips() -> list[dict[str, Any]]:
    """Walk every cached video and return unique (s3_key, start, end) clips.

    Marengo emits separate segments for visual / audio / transcription with
    matching time ranges; we dedupe across those because the cut clip is
    identical."""
    seen: set[tuple[str, float, float]] = set()
    out: list[dict[str, Any]] = []
    for video in embed_lib.iter_cached_videos():
        s3_key = video["s3_key"]
        for segment in video.get("segments", []) or []:
            start = float(segment.get("startSec", 0.0))
            end = float(segment.get("endSec", 0.0))
            if end <= start:
                continue
            key = (s3_key, round(start, 3), round(end, 3))
            if key in seen:
                continue
            seen.add(key)
            out.append({"s3_key": s3_key, "start_sec": start, "end_sec": end})
    out.sort(key=lambda c: (c["s3_key"], c["start_sec"]))
    return out


# ---------------------------------------------------------------------------
# Source video download (cached on disk so we only pay once per video)
# ---------------------------------------------------------------------------


def _source_digest(s3_key: str) -> str:
    return hashlib.sha256(s3_key.encode("utf-8")).hexdigest()[:24]


def _source_video_path(s3_key: str) -> Path:
    digest = _source_digest(s3_key)
    out_dir = pegasus_lib.SOURCE_VIDEO_DIR / digest
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / Path(s3_key).name


def ensure_source_video(s3, *, bucket: str, s3_key: str) -> Path:
    path = _source_video_path(s3_key)
    if path.exists() and path.stat().st_size > 0:
        return path
    print(f"  ↓ downloading s3://{bucket}/{s3_key} -> {path.relative_to(embed_lib.REPO_ROOT)}")
    s3.download_file(bucket, s3_key, str(path))
    return path


# ---------------------------------------------------------------------------
# ffmpeg clip cutting + S3 upload
# ---------------------------------------------------------------------------


def _clip_local_path(s3_key: str, start_sec: float, end_sec: float) -> Path:
    digest = _source_digest(s3_key)
    out_dir = pegasus_lib.CLIP_CUT_DIR / digest
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"clip_{int(round(start_sec * 1000)):07d}_{int(round(end_sec * 1000)):07d}.mp4"


def _clip_s3_key(s3_key: str, start_sec: float, end_sec: float) -> str:
    digest = _source_digest(s3_key)
    name = f"clip_{int(round(start_sec * 1000)):07d}_{int(round(end_sec * 1000)):07d}.mp4"
    return f"{pegasus_lib.DERIVED_CLIPS_S3_PREFIX}/{digest}/{name}"


def cut_clip(source_path: Path, *, start_sec: float, end_sec: float, out_path: Path) -> Path:
    """Cut ``[start_sec, end_sec]`` from ``source_path`` to ``out_path``.

    Re-encode (rather than -c copy) for keyframe-independent accurate cuts.
    The clips are short (~5s) so the cost is negligible. Pegasus accepts
    standard mp4 + AAC.
    """
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    duration = max(0.05, end_sec - start_sec)
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_sec:.3f}",
        "-i",
        str(source_path),
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path


def s3_object_exists(s3, *, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001 — head_object 404 raises ClientError; we just want a bool
        return False


def upload_clip(s3, *, bucket: str, local_path: Path, s3_key: str) -> None:
    if s3_object_exists(s3, bucket=bucket, key=s3_key):
        return
    s3.upload_file(str(local_path), bucket, s3_key, ExtraArgs={"ContentType": "video/mp4"})


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--preset",
        choices=[p["id"] for p in pegasus_lib.PRESET_PROMPTS],
        default="inspector",
        help="Curated preset id (matches the local UI dropdown). "
             "Default: inspector.",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Custom prompt that overrides --preset.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Sampling temperature; 0 is deterministic (default).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most this many clips (across all videos).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run Pegasus even when the (clip, prompt) pair is cached.",
    )
    parser.add_argument(
        "--keep-cuts",
        action="store_true",
        help="Keep the local cut-clip mp4 files after upload "
             "(default: delete to save disk).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the clips that would be processed and exit.",
    )
    args = parser.parse_args()

    cfg = embed_lib.load_config_or_die()
    s3 = embed_lib.s3_client(cfg.region)
    bedrock = embed_lib.bedrock_client(cfg.region)
    inference_id = pegasus_lib.resolve_inference_id(cfg.region)

    if args.prompt:
        prompt = args.prompt
        prompt_id = None
    else:
        prompt = pegasus_lib.resolve_preset(args.preset)
        prompt_id = args.preset
        if prompt is None:  # shouldn't happen; argparse limits to known ids
            embed_lib.die(f"unknown preset {args.preset!r}")

    clips = _unique_clips()
    if args.limit:
        clips = clips[: args.limit]

    if not clips:
        print("no clips found in local embedding cache; "
              "run scripts.embed.embed_videos first")
        return 0

    print(
        f"region={cfg.region} bucket={cfg.bucket} model={inference_id}"
    )
    print(f"prompt_id={prompt_id or '(custom)'} clips={len(clips)}\n")

    if args.dry_run:
        for c in clips:
            cached = pegasus_lib.read_clip_cache(
                c["s3_key"], c["start_sec"], c["end_sec"], prompt
            )
            tag = "cached" if cached else "queued"
            print(f"  [{tag}] {c['s3_key']}  {c['start_sec']:.2f}-{c['end_sec']:.2f}s")
        return 0

    if shutil.which("ffmpeg") is None:
        embed_lib.die(
            "ffmpeg is required for clip cutting. install it (e.g. `apt install ffmpeg`)."
        )

    n_cached = 0
    n_done = 0
    n_failed = 0
    started = time.time()

    for clip in clips:
        s3_key = clip["s3_key"]
        start = clip["start_sec"]
        end = clip["end_sec"]
        label = f"{s3_key} {start:.2f}-{end:.2f}s"

        if not args.force:
            cached = pegasus_lib.read_clip_cache(s3_key, start, end, prompt)
            if cached:
                n_cached += 1
                preview = (cached.get("message") or "").strip().splitlines()[0:1]
                preview_text = preview[0][:120] if preview else "(empty)"
                print(f"  · cached  {label}  | {preview_text}")
                continue

        print(f"  → {label}")
        try:
            source = ensure_source_video(s3, bucket=cfg.bucket, s3_key=s3_key)
            cut_path = _clip_local_path(s3_key, start, end)
            cut_clip(source, start_sec=start, end_sec=end, out_path=cut_path)
            clip_key = _clip_s3_key(s3_key, start, end)
            upload_clip(s3, bucket=cfg.bucket, local_path=cut_path, s3_key=clip_key)

            chunks: list[str] = []
            for chunk in pegasus_lib.stream_describe(
                bedrock,
                inference_id=inference_id,
                bucket=cfg.bucket,
                account_id=cfg.account_id,
                s3_key=clip_key,
                prompt=prompt,
                temperature=args.temperature,
            ):
                sys.stdout.write(chunk)
                sys.stdout.flush()
                chunks.append(chunk)
            sys.stdout.write("\n")
            message = "".join(chunks)
            if not message.strip():
                raise pegasus_lib.PegasusError("empty Pegasus response")

            cache_path = pegasus_lib.save_clip_cache(
                s3_key=s3_key,
                start_sec=start,
                end_sec=end,
                prompt=prompt,
                prompt_id=prompt_id,
                message=message,
                model_id=inference_id,
                clip_s3_key=clip_key,
            )
            rel = cache_path.relative_to(embed_lib.REPO_ROOT)
            print(f"     saved -> {rel}")
            n_done += 1

            if not args.keep_cuts:
                try:
                    cut_path.unlink()
                except OSError:
                    pass
        except subprocess.CalledProcessError as exc:
            print(f"     ffmpeg failed: {exc}", file=sys.stderr)
            n_failed += 1
        except Exception as exc:  # noqa: BLE001 — surface boto3/Bedrock errors clearly
            print(f"     pegasus failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
            n_failed += 1

    dur = time.time() - started
    print(
        f"\nsummary: cached={n_cached} new={n_done} failed={n_failed} "
        f"total={len(clips)} time={dur:.1f}s"
    )
    return 0 if n_failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
