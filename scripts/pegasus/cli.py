"""Run TwelveLabs Pegasus 1.2 on Bedrock against a video already in S3.

Pegasus is a video-to-text generative model: feed it an S3 URI plus a
text ``inputPrompt`` and it returns a description / answer. Same
bedrock-runtime client and credential flow as ``scripts.embed``.

Usage::

    # Default key + default prompt (utility-inspection summary). Streams
    # text as it arrives, then caches the final transcript under
    # data/pegasus/<sha>.json.
    pipenv run python -m scripts.pegasus.cli

    # Custom key / prompt.
    pipenv run python -m scripts.pegasus.cli \\
        --key raw-videos/pipeline_vegetation001.mp4 \\
        --prompt "What hazards to power infrastructure are visible here?"

    # Pre-canned prompts shared with the local UI.
    pipenv run python -m scripts.pegasus.cli --preset inspector
    pipenv run python -m scripts.pegasus.cli --preset hashtags

    # Run over every video already in S3 under raw-videos/ video-clips/.
    pipenv run python -m scripts.pegasus.cli --all

Cost is per-minute of analyzed video; ~cents per call for our short clips.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from scripts.embed import _lib as embed_lib

from . import _lib as pegasus_lib


def _resolve_target_keys(args: argparse.Namespace, *, s3, bucket: str) -> list[str]:
    if args.all:
        # Reuse the same scan that scripts.embed.embed_videos uses so we
        # only describe things we've actually got embeddings for.
        prefixes = args.prefix or list(embed_lib.DEFAULT_VIDEO_PREFIXES)
        keys = [e["key"] for e in embed_lib.list_video_keys(s3, bucket, prefixes)]
        return keys[: args.limit] if args.limit else keys
    if args.key:
        return [args.key]
    return [args.default_key]


def _resolve_prompt(args: argparse.Namespace) -> str:
    if args.preset:
        prompt = pegasus_lib.resolve_preset(args.preset)
        if prompt is None:
            embed_lib.die(
                f"unknown preset {args.preset!r}; choose from "
                + ", ".join(p["id"] for p in pegasus_lib.PRESET_PROMPTS)
            )
        return prompt  # type: ignore[return-value]
    return args.prompt


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--key", help="S3 object key to describe.")
    target.add_argument(
        "--all",
        action="store_true",
        help="Run the prompt over every video found under --prefix.",
    )
    parser.add_argument(
        "--default-key",
        default="raw-videos/pipeline_vegetation001.mp4",
        help="Key used when neither --key nor --all is given.",
    )
    parser.add_argument(
        "--prefix",
        action="append",
        default=None,
        help="Repeatable. Default: raw-videos/ video-clips/ (only with --all).",
    )
    prompt_group = parser.add_mutually_exclusive_group()
    prompt_group.add_argument(
        "--prompt",
        default=pegasus_lib.DEFAULT_PROMPT,
        help="Pegasus inputPrompt. Defaults to a utility-inspection summary.",
    )
    prompt_group.add_argument(
        "--preset",
        choices=[p["id"] for p in pegasus_lib.PRESET_PROMPTS],
        default=None,
        help="Use a curated preset prompt shared with the local UI.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Sampling temperature; 0 is deterministic (default).",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Use invoke_model instead of invoke_model_with_response_stream.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run Pegasus even when the (key, prompt) pair is cached.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="With --all, cap how many videos to describe.",
    )
    args = parser.parse_args()

    cfg = embed_lib.load_config_or_die()
    s3 = embed_lib.s3_client(cfg.region)
    bedrock = embed_lib.bedrock_client(cfg.region)
    inference_id = pegasus_lib.resolve_inference_id(cfg.region)
    prompt = _resolve_prompt(args)

    keys = _resolve_target_keys(args, s3=s3, bucket=cfg.bucket)
    if not keys:
        print("no video keys to describe")
        return 0

    print(
        f"region={cfg.region} bucket={cfg.bucket} "
        f"model={inference_id} videos={len(keys)}"
    )
    print(f"prompt: {prompt}\n")

    failed = 0
    for s3_key in keys:
        cache_path = pegasus_lib.cache_path_for(s3_key, prompt)
        rel = cache_path.relative_to(embed_lib.REPO_ROOT)
        print(f"== {s3_key} ==")

        if cache_path.exists() and not args.force:
            cached = pegasus_lib.read_cache(s3_key, prompt) or {}
            print(f"   cached -> {rel}")
            print(cached.get("message", ""))
            print()
            continue

        try:
            if args.no_stream:
                message = pegasus_lib.describe_sync(
                    bedrock,
                    inference_id=inference_id,
                    bucket=cfg.bucket,
                    account_id=cfg.account_id,
                    s3_key=s3_key,
                    prompt=prompt,
                    temperature=args.temperature,
                )
                print(message)
            else:
                chunks: list[str] = []
                for chunk in pegasus_lib.stream_describe(
                    bedrock,
                    inference_id=inference_id,
                    bucket=cfg.bucket,
                    account_id=cfg.account_id,
                    s3_key=s3_key,
                    prompt=prompt,
                    temperature=args.temperature,
                ):
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    chunks.append(chunk)
                sys.stdout.write("\n")
                sys.stdout.flush()
                message = "".join(chunks)
        except Exception as exc:  # noqa: BLE001 — surface boto3/Bedrock errors clearly
            print(f"   pegasus failed: {exc}", file=sys.stderr)
            failed += 1
            print()
            continue

        pegasus_lib.save_cache(
            s3_key=s3_key,
            prompt=prompt,
            message=message,
            model_id=inference_id,
        )
        print(f"   saved -> {rel}\n")

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
