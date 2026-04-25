"""Bulk-embed videos in our S3 portal bucket using Marengo Embed 3.0.

For every video under the configured prefixes, we kick off a Bedrock async
invocation, wait for it, fetch ``output.json`` from S3, and persist the raw
segment vectors locally under ``data/embeddings/`` keyed by the S3 object key.

Re-running is cheap: anything already cached is skipped unless ``--force``.

Usage::

    pipenv run python -m scripts.embed.embed_videos
    pipenv run python -m scripts.embed.embed_videos --limit 1
    pipenv run python -m scripts.embed.embed_videos --dry-run
    pipenv run python -m scripts.embed.embed_videos --force
    pipenv run python -m scripts.embed.embed_videos --prefix raw-videos/
"""
from __future__ import annotations

import argparse
import sys
import time

from . import _lib


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--prefix",
        action="append",
        default=None,
        help="S3 prefix to scan (repeatable). Default: raw-videos/ video-clips/",
    )
    parser.add_argument("--limit", type=int, default=None, help="Embed at most N videos.")
    parser.add_argument("--force", action="store_true", help="Re-embed even when cached.")
    parser.add_argument("--dry-run", action="store_true", help="List planned work and exit.")
    parser.add_argument(
        "--option",
        action="append",
        default=None,
        choices=["visual", "audio", "transcription"],
        help=(
            "embeddingOption to request (repeatable). "
            "Default mirrors AWS: visual + audio + transcription. "
            "Drop transcription for silent footage; drop audio for purely visual queries."
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=8,
        help="Seconds between get_async_invoke polls (default: 8).",
    )
    args = parser.parse_args()

    prefixes = args.prefix or list(_lib.DEFAULT_VIDEO_PREFIXES)
    options = args.option or list(_lib.DEFAULT_EMBEDDING_OPTIONS)

    cfg = _lib.load_config_or_die()
    s3 = _lib.s3_client(cfg.region)
    bedrock = _lib.bedrock_client(cfg.region)

    print(f"region={cfg.region} bucket={cfg.bucket} model={_lib.MARENGO_MODEL_ID}")
    print(f"scanning prefixes: {', '.join(prefixes)}")

    keys = _lib.list_video_keys(s3, cfg.bucket, prefixes)
    if args.limit:
        keys = keys[: args.limit]

    if not keys:
        print("no video keys found")
        return 0

    print(f"found {len(keys)} video(s)\n")

    embedded = 0
    skipped = 0
    failed = 0
    started_at = time.time()

    for entry in keys:
        s3_key = entry["key"]
        cache_path = _lib.cache_path_for_key(s3_key)
        rel = cache_path.relative_to(_lib.REPO_ROOT)
        if cache_path.exists() and not args.force:
            print(f"  cached  {s3_key}  ->  {rel}")
            skipped += 1
            continue

        if args.dry_run:
            print(f"  plan    {s3_key}")
            continue

        print(f"  embed   {s3_key}  ({entry['size'] / (1024 * 1024):.1f} MiB)")
        try:
            invocation_arn, output_dir = _lib.start_video_embedding(
                bedrock,
                bucket=cfg.bucket,
                account_id=cfg.account_id,
                video_key=s3_key,
                output_prefix=cfg.output_prefix,
                embedding_options=options,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"          start failed: {exc}", file=sys.stderr)
            failed += 1
            continue

        print(f"          arn={invocation_arn}")
        print(f"          out=s3://{cfg.bucket}/{output_dir}/")

        last_status: list[str | None] = [None]

        def _on_status(s: str) -> None:
            if s != last_status[0]:
                ts = time.strftime("%H:%M:%S", time.localtime())
                print(f"          [{ts}] status={s}")
                last_status[0] = s

        try:
            output = _lib.wait_for_async_output(
                bedrock,
                s3,
                bucket=cfg.bucket,
                output_dir=output_dir,
                invocation_arn=invocation_arn,
                poll_seconds=args.poll_seconds,
                on_status=_on_status,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"          wait failed: {exc}", file=sys.stderr)
            failed += 1
            continue

        path = _lib.save_video_cache(s3_key, output)
        n_segments = len(output.get("data", []) or [])
        print(f"          stored {n_segments} segment(s)  ->  {path.relative_to(_lib.REPO_ROOT)}")
        embedded += 1

    elapsed = time.time() - started_at
    print(
        f"\ndone in {elapsed:0.1f}s — embedded={embedded} skipped={skipped} "
        f"failed={failed} total={len(keys)}"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
