# `scripts/embed/` — local Marengo video search

Plain-Python CLI for embedding the videos in our portal S3 bucket with
TwelveLabs Marengo Embed 3.0 on Bedrock and running text / image / text+image
similarity search **locally**, with no Postgres and no notebook. Cached
segment vectors live under `data/embeddings/` so re-runs are free.

## 1. Install deps

`boto3` is already in the project Pipfile. The only extra is `numpy`:

```bash
pipenv install --dev numpy
```

Or, if you don't want to touch the Pipfile:

```bash
pipenv run pip install -r scripts/embed/requirements-local.txt
```

## 2. Configure

Source the workshop credentials and tell the scripts which bucket to scan:

```bash
set -a; source ./.aws-demo.env; set +a
unset AWS_PROFILE
export AWS_CONFIG_FILE=/dev/null   # workaround for the demo creds
export S3_BUCKET="$(terraform -chdir=infra output -raw bucket_name)"
```

Optional overrides (auto-detected from `AWS_REGION` otherwise):

```bash
export AWS_REGION=us-east-1
export MARENGO_INFERENCE_ID=us.twelvelabs.marengo-embed-3-0-v1:0
```

Quick sanity check:

```bash
pipenv run python -m scripts.embed.embed_query text "hello world" --summary
# kind=text segments=1 dim=512
```

## 3. Bulk-embed the bucket

Defaults: scan `raw-videos/` and `video-clips/` for any `.mp4 .mov .mkv .avi
.webm .m4v`, kick off async Marengo jobs, poll, persist `output.json`
locally.

```bash
pipenv run python -m scripts.embed.embed_videos               # all videos
pipenv run python -m scripts.embed.embed_videos --limit 1     # first one
pipenv run python -m scripts.embed.embed_videos --dry-run     # plan only
pipenv run python -m scripts.embed.embed_videos --force       # re-embed
pipenv run python -m scripts.embed.embed_videos --prefix raw-videos/
```

Async jobs typically finish in a few minutes; status updates print live. The
Bedrock-side output also lands at `s3://<bucket>/embeddings/videos/<job-id>/`
so we have an off-laptop copy too.

## 3b. Embed per-frame thumbnails (frame-precise seek)

Marengo's clip embeddings average over ~6s windows, which is great for
"find the right shot" but vague when you want to scrub to the *exact* moment
that matched. Frame embeddings fix that: we extract one frame per second
with ffmpeg, embed each frame via Marengo's sync image API, and join them
into the same 512-d index. At search time the ranker picks the best
matching frame within each clip's window and seeks the preview there.

```bash
pipenv run python -m scripts.embed.embed_frames               # all cached videos, 1 fps
pipenv run python -m scripts.embed.embed_frames --fps 2       # every 0.5s (twice the cost)
pipenv run python -m scripts.embed.embed_frames --fps 0.5     # every 2s (half the cost)
pipenv run python -m scripts.embed.embed_frames --limit 1     # one video only
pipenv run python -m scripts.embed.embed_frames --force       # re-extract + re-embed
```

Requires `ffmpeg` on `PATH` (already there on most dev boxes; on Ubuntu:
`sudo apt install ffmpeg`). Frames double as UI thumbnails — they're stored
under `data/embeddings/thumbs/<digest>/frame_NNNNN.jpg` and served by the
local UI at `/thumbs/...`.

## 4. Search — CLI

```bash
# pure text
pipenv run python -m scripts.embed.search text "two people in a car" -k 5

# upload an image you have on disk
pipenv run python -m scripts.embed.search image ./somewhere/frame.jpg

# combined
pipenv run python -m scripts.embed.search text-image "person in a hard hat" ./frame.jpg

# machine-readable
pipenv run python -m scripts.embed.search text "..." --json
```

Each result line ends with a presigned URL containing `#t=<start_sec>`. Paste
into a browser — the video opens at the matched segment.

## 5. Search — local web UI (drag-and-drop, paste, embedded video players)

```bash
pipenv run python -m scripts.embed.serve              # http://127.0.0.1:8001
pipenv run python -m scripts.embed.serve --reload     # with file watcher
pipenv run python -m scripts.embed.serve --port 9000
```

Tabs for `TEXT` / `IMAGE` / `TEXT + IMG`. The image dropzone takes
drag-and-drop, click-to-choose, and **paste-from-clipboard**.

Each result card shows:

- the matched **frame thumbnail** (when frame embeddings are present),
- a `<video controls>` whose `src` ends in `#t=<frame timestamp>` — hit play
  and the video starts at the precise matched moment, not the start of the
  6-second clip,
- a `FRAME` or `CLIP` badge so you can tell whether the hit came directly
  from a frame embedding or from a clip embedding refined to its best frame,
- a confidence band (`STRONG / GOOD / WEAK / NOISE FLOOR`) computed from the
  cosine score so you can spot when ranking is meaningful vs noise.

The sidebar lists each video's row count broken into `<clips>c · <frames>f`
and a `REFRESH` button to re-read `data/embeddings/` after running
`embed_videos` or `embed_frames`.

**Definition of done for Phase A:** click play on a result card and the
video starts at the matched frame, with a thumbnail showing the matched
moment.

## 6. Inspecting a single query embedding

For debugging the API itself rather than retrieval:

```bash
pipenv run python -m scripts.embed.embed_query text "a forest" > /tmp/q.json
pipenv run python -m scripts.embed.embed_query image ./frame.jpg --summary
```

## Cache layout

```
data/embeddings/
  <sha256(s3_key)[:24]>.json          # clip embeddings (one file per video)
  frames/
    <sha256(s3_key)[:24]>.json        # frame embeddings (one file per video)
  thumbs/
    <sha256(s3_key)[:24]>/
      frame_00001.jpg                 # one JPEG per sampled frame
  _video-cache/
    <basename(s3_key)>                # downloaded source video, used for ffmpeg
```

Delete any one file to force a re-embed of that video at that granularity.
Delete `data/embeddings/` entirely to start over.

## Why a separate `requirements-local.txt`?

So the deployed FastAPI image stays small. `numpy` is only needed for the
local in-memory ranking; once we move to Postgres+pgvector (Phase B) the
deployed image will import `psycopg`/`pgvector` instead and the local-only
deps stay local.
