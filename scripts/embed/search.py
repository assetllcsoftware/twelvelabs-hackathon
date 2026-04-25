"""Cosine similarity search over the locally cached Marengo video segments.

Embed a query (text, image, or text+image), L2-normalize, dot it against the
cached segment matrix, and print the top-K matches with presigned URLs that
already include ``#t=<start_sec>`` so you can paste straight into a browser.

Usage::

    pipenv run python -m scripts.embed.search text "two people in a car"
    pipenv run python -m scripts.embed.search image ./frame.jpg
    pipenv run python -m scripts.embed.search text-image "a hard hat" ./frame.jpg
    pipenv run python -m scripts.embed.search text "..." -k 10 --json
"""
from __future__ import annotations

import argparse
import json

from . import _lib


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-k", "--top-k", type=int, default=5)
    common.add_argument("--no-presign", action="store_true", help="Skip presigned URL generation.")
    common.add_argument("--json", action="store_true", help="Emit results as JSON.")

    sub = parser.add_subparsers(dest="kind", required=True)

    p_text = sub.add_parser("text", parents=[common], help="Search by text query.")
    p_text.add_argument("query")

    p_image = sub.add_parser("image", parents=[common], help="Search by image (path).")
    p_image.add_argument("path")

    p_ti = sub.add_parser("text-image", parents=[common], help="Search by text + image.")
    p_ti.add_argument("query")
    p_ti.add_argument("path")
    return parser


def main() -> int:
    args = _make_parser().parse_args()

    cfg = _lib.load_config_or_die()

    matrix, meta = _lib.build_segment_matrix()
    if matrix.shape[0] == 0:
        _lib.die(
            "no cached embeddings found. Run "
            "`pipenv run python -m scripts.embed.embed_videos` first."
        )

    n_clips = sum(1 for m in meta if m.get("kind") == "clip")
    n_frames = sum(1 for m in meta if m.get("kind") == "frame")

    bedrock = _lib.bedrock_client(cfg.region)

    if args.kind == "text":
        data = _lib.invoke_text_embedding(bedrock, cfg.inference_id, args.query)
        query_label = f"text: {args.query!r}"
    elif args.kind == "image":
        data = _lib.invoke_image_embedding(bedrock, cfg.inference_id, args.path)
        query_label = f"image: {args.path}"
    else:
        data = _lib.invoke_text_image_embedding(
            bedrock, cfg.inference_id, args.query, args.path
        )
        query_label = f"text+image: {args.query!r} + {args.path}"

    if not data:
        _lib.die("query embedding returned empty result")

    ranked = _lib.rank_results(matrix, meta, data[0]["embedding"], top_k=args.top_k)

    s3 = None if args.no_presign else _lib.s3_client(cfg.region)
    for r in ranked:
        if s3 is None:
            r["presigned_url"] = None
            continue
        r["presigned_url"] = (
            _lib.presigned_get(s3, cfg.bucket, r["s3_key"])
            + f"#t={r['timestamp_sec']:.2f}"
        )

    if args.json:
        print(json.dumps(ranked, indent=2))
        return 0

    print(f"\nquery: {query_label}")
    print(
        f"corpus: {matrix.shape[0]} row(s) — {n_clips} clip(s) + {n_frames} frame(s)\n"
    )
    print(f"top {len(ranked)} match(es):\n")
    for rank, r in enumerate(ranked, start=1):
        kind = r["kind"].upper()
        if r["kind"] == "clip" and r.get("refined_from_frame"):
            window = (
                f"[{r['start_sec']:.1f}s - {r['end_sec']:.1f}s] "
                f"-> frame @ {r['timestamp_sec']:.2f}s"
            )
        elif r["kind"] == "clip":
            window = f"[{r['start_sec']:.1f}s - {r['end_sec']:.1f}s]"
        else:
            window = f"frame @ {r['timestamp_sec']:.2f}s"
        print(
            f"  #{rank}  score={r['score']:+.4f}  {kind}  "
            f"{r['s3_key']}  {window}  ({r['embedding_option']})"
        )
        if r["presigned_url"]:
            print(f"        {r['presigned_url']}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
