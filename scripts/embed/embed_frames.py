"""Extract per-second frames from each cached video and embed them as images.

Frame embeddings live in the same 512-d Marengo space as clip and text/image
query embeddings, so they get joined into the unified search index. They give
us frame-precise seek points: the local UI uses the best-matching frame's
timestamp to scrub the preview, instead of the start of the ~6s clip.

Usage::

    pipenv run python -m scripts.embed.embed_frames                 # all cached videos
    pipenv run python -m scripts.embed.embed_frames --fps 0.5       # every 2 seconds
    pipenv run python -m scripts.embed.embed_frames --limit 1
    pipenv run python -m scripts.embed.embed_frames --force         # re-extract + re-embed

The script downloads each video to a scratch dir under ``data/embeddings/_video-cache``
once (idempotent), runs ffmpeg into ``data/embeddings/thumbs/<digest>/`` so the
JPEGs can double as thumbnails for the search UI, and writes one cache file
per video to ``data/embeddings/frames/<digest>.json``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import _lib


def _download_video(s3, bucket: str, key: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(bucket, key, str(dest))
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=1.0,
        help="Frames per second to sample (default: 1.0).",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=720,
        help="Frame width in pixels for thumbnails sent to Bedrock (default: 720).",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Embed at most N videos."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract frames and re-embed even when a cache file exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the per-video plan and exit without calling Bedrock.",
    )
    args = parser.parse_args()

    cfg = _lib.load_config_or_die()
    s3 = _lib.s3_client(cfg.region)
    bedrock = _lib.bedrock_client(cfg.region)

    cached_videos = list(_lib.iter_cached_videos())
    if args.limit:
        cached_videos = cached_videos[: args.limit]

    if not cached_videos:
        print(
            "no cached video embeddings found. Run `python -m scripts.embed.embed_videos` first.",
            file=sys.stderr,
        )
        return 1

    print(f"region={cfg.region} bucket={cfg.bucket} model={cfg.inference_id}")
    print(f"sampling at {args.fps} fps, width={args.width}px\n")

    scratch = _lib.CACHE_DIR / "_video-cache"
    embedded = 0
    skipped = 0
    failed = 0
    total_frames = 0
    started_at = time.time()

    for video in cached_videos:
        s3_key = video["s3_key"]
        cache_path = _lib.frames_cache_path_for(s3_key)
        thumb_dir = _lib.frames_thumb_dir_for(s3_key)

        if cache_path.exists() and not args.force:
            try:
                existing = len(json.loads(cache_path.read_text()).get("frames", []))
            except (json.JSONDecodeError, OSError):
                existing = 0
            print(f"  cached  {s3_key}  ({existing} frame(s))")
            skipped += 1
            continue

        if args.dry_run:
            print(f"  plan    {s3_key}")
            continue

        print(f"  embed   {s3_key}")

        # 1. Download the source video locally (cached on disk).
        local_video = scratch / Path(s3_key).name
        try:
            _download_video(s3, cfg.bucket, s3_key, local_video)
        except Exception as exc:  # noqa: BLE001
            print(f"          download failed: {exc}", file=sys.stderr)
            failed += 1
            continue

        # 2. Extract frames with ffmpeg into the thumbs dir so they can serve as
        # UI previews later.
        if args.force:
            for old in thumb_dir.glob("frame_*.jpg"):
                old.unlink()
        try:
            frames = _lib.extract_frames_with_ffmpeg(
                local_video, thumb_dir, fps=args.fps, width=args.width
            )
        except Exception as exc:  # noqa: BLE001
            print(f"          ffmpeg failed: {exc}", file=sys.stderr)
            failed += 1
            continue

        if not frames:
            print("          ffmpeg produced no frames", file=sys.stderr)
            failed += 1
            continue

        print(f"          extracted {len(frames)} frame(s) at {args.fps} fps")

        # 3. Embed each frame via Marengo's sync image API.
        frame_records: list[dict] = []
        try:
            for ts, frame_path in frames:
                with open(frame_path, "rb") as fh:
                    image_bytes = fh.read()
                data = _lib.invoke_image_embedding_bytes(
                    bedrock, cfg.inference_id, image_bytes
                )
                if not data or "embedding" not in data[0]:
                    raise RuntimeError("Marengo returned no embedding for frame")
                frame_records.append(
                    {
                        "timestamp_sec": round(ts, 3),
                        "thumb_name": frame_path.name,
                        "embedding": data[0]["embedding"],
                    }
                )
        except Exception as exc:  # noqa: BLE001
            print(f"          embed failed at {len(frame_records)}/{len(frames)}: {exc}", file=sys.stderr)
            failed += 1
            continue

        path = _lib.save_frames_cache(s3_key, frame_records, fps=args.fps)
        print(
            f"          stored {len(frame_records)} frame(s)  ->  {path.relative_to(_lib.REPO_ROOT)}"
        )
        embedded += 1
        total_frames += len(frame_records)

    elapsed = time.time() - started_at
    print(
        f"\ndone in {elapsed:0.1f}s — videos: embedded={embedded} skipped={skipped} "
        f"failed={failed} | total frames embedded this run={total_frames}"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
