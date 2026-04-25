"""One-shot Marengo query embedding: text, image, or text+image.

Mostly useful for poking at the API and confirming you get a 512-dim vector
back; ``search.py`` is what you actually want for retrieval.

Usage::

    pipenv run python -m scripts.embed.embed_query text "two people in a car"
    pipenv run python -m scripts.embed.embed_query image path/to/frame.jpg
    pipenv run python -m scripts.embed.embed_query text-image "a hard hat" path/to/frame.jpg

By default the full payload (with the embedding vector) is printed to stdout
as JSON. Use ``--out`` to write it to a file instead, or ``--summary`` to
just print shape info.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import _lib


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", help="Write JSON payload to this file instead of stdout.")
    common.add_argument(
        "--summary",
        action="store_true",
        help="Only print shape/length, not the embedding vector.",
    )

    sub = parser.add_subparsers(dest="kind", required=True)

    p_text = sub.add_parser("text", parents=[common], help="Text-only embedding.")
    p_text.add_argument("query")

    p_image = sub.add_parser("image", parents=[common], help="Image-only embedding.")
    p_image.add_argument("path")

    p_ti = sub.add_parser("text-image", parents=[common], help="Text+image embedding.")
    p_ti.add_argument("query")
    p_ti.add_argument("path")
    return parser


def main() -> int:
    args = _make_parser().parse_args()

    cfg = _lib.load_config_or_die()
    bedrock = _lib.bedrock_client(cfg.region)

    if args.kind == "text":
        data = _lib.invoke_text_embedding(bedrock, cfg.inference_id, args.query)
    elif args.kind == "image":
        data = _lib.invoke_image_embedding(bedrock, cfg.inference_id, args.path)
    else:
        data = _lib.invoke_text_image_embedding(
            bedrock, cfg.inference_id, args.query, args.path
        )

    if not data:
        _lib.die("query embedding returned an empty result")

    if args.summary:
        first = data[0]
        dim = len(first.get("embedding", []))
        print(f"kind={args.kind} segments={len(data)} dim={dim}")
        return 0

    payload = {"kind": args.kind, "model": cfg.inference_id, "segments": data}
    rendered = json.dumps(payload, indent=2)
    if args.out:
        Path(args.out).write_text(rendered)
        print(f"wrote {args.out}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
